# coding=UTF-8
"""
Lustra: tag list sources for any extension (lustra/tags_updater.py, lustra/robots.py), offline.
A fake site answers every engine's tag listing through a fake transport; the updater's real policy code runs (robots.txt, pacing,
Retry-After, request budget, bot challenges, hosts). End to end: a Gelbooru-type extension made by the generator (lustra.extgen)
gets its list from the updater and autocompletion shows it.
Every new extension has a list at once: the generator puts the site's list into the draft and the archive (lustra/extgen/taglist.py),
the installed extension's list is used (TagsDB.extension_lists), and a site without a tag listing gets its list learned from the
posts previewed and downloaded (lustra/tags_learned.py).
Run: python -m unittest lustra.tests.test_tag_sources
"""
#########################################
#
#

import csv
import datetime
import gzip
import html
import io
import itertools
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import zipfile
from unittest import TestCase, mock
from urllib import parse as url_parse

from requests.structures import CaseInsensitiveDict

from lustra.logger import Logger

Logger.init(True, True)

from lustra import server, tags_learned, tags_updater  # noqa: E402
from lustra.aigen.session import GenRequest, GenSession  # noqa: E402
from lustra.dlindex import DownloadIndex, IndexRecord  # noqa: E402
from lustra.extgen.fetch import SafeFetcher  # noqa: E402
from lustra.extgen.sandbox import pack  # noqa: E402
from lustra.extgen.scaffold import create_draft, survey  # noqa: E402
from lustra.extgen.taglist import bundle_tag_list  # noqa: E402
from lustra.extgen.templates import make_manifest  # noqa: E402
from lustra.modspec import ExtensionManifest  # noqa: E402
from lustra.robots import RobotsRules  # noqa: E402
from lustra.tags import TagTypes  # noqa: E402
from lustra.tags_updater import CURATED, PoliteClient, TagSource, TagSourceError, tag_source, tag_sources, update_tag_list  # noqa: E402
from lustra.tagsdb import TagsDB  # noqa: E402

HOST = 'fakebooru.test'
SITE = f'https://{HOST}/'
WAIT = 10.0
TAGS = [  # id, name, posts, type code (gelbooru / danbooru / moebooru / e621: 1 artist, 3 copyright, 4 character)
    (1, 'scenery', 900, 0), (2, 'sky', 700, 0), (3, 'tom_&_jerry', 40, 3), (4, "it's", 12, 0), (5, 'some_artist', 30, 1),
    (6, 'hatsune_miku', 300, 4), (7, 'empty_tag', 0, 0),
]
EXPECTED = {name: posts for _, name, posts, _ in TAGS if posts > 0}
PHILOMENA_CATEGORY = {0: None, 1: 'origin', 3: 'content-official', 4: 'character'}
SZURU_CATEGORY = {0: 'default', 1: 'artist', 3: 'copyright', 4: 'character'}


class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes, headers: dict) -> None:
        self.url, self.status_code, self._body = url, status, body
        self.headers = CaseInsensitiveDict(headers)
        self.is_redirect = self.is_permanent_redirect = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, n: int):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class FakeSession:
    def __init__(self, route) -> None:
        self.route = route
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, headers=None, **_):
        self.calls.append((url, dict(headers or {})))
        return FakeResponse(url, *self.route(url, dict(headers or {})))


def _xml(body: str) -> tuple[int, bytes, dict]:
    return 200, body.encode(), {'content-type': 'text/xml'}


def _json(data) -> tuple[int, bytes, dict]:
    return 200, json.dumps(data).encode(), {'content-type': 'application/json'}


class FakeTagSite:
    """One site, every engine's tag listing (and the gelbooru post index the generator's engine detection reads)"""

    def __init__(self, robots: str = '', need_key: bool = False, cap: int = 0, inclusive: bool = False) -> None:
        self.robots = robots
        self.need_key = need_key
        self.cap = cap
        '''answers at most this many tags per page, whatever 'limit' asks (some sites cap it)'''
        self.inclusive = inclusive
        '''the id cursor includes its own id (the last tag of a page comes again on the next one)'''
        self.answers: list[tuple[int, bytes, dict]] = []
        '''answered first, before the routes (429, challenges...)'''

    def __call__(self, url: str, headers: dict) -> tuple[int, bytes, dict]:
        p = url_parse.urlsplit(url)
        qs = {k: v[0] for k, v in url_parse.parse_qs(p.query).items()}
        if p.path == '/robots.txt':
            return (200, self.robots.encode(), {'content-type': 'text/plain'}) if self.robots else (404, b'none', {'content-type': 'text/html'})
        if self.answers:
            return self.answers.pop(0)
        live = sorted((t for t in TAGS if t[2] > 0), key=lambda t: t[0])

        def page(rows: list, after: int, limit: int) -> list:
            return [t for t in rows if (t[0] >= after if self.inclusive and after else t[0] > after)][:min(limit, self.cap or limit)]
        if p.path == '/index.php' and qs.get('page') == 'dapi':
            if self.need_key and 'api_key' not in qs:
                return _xml('"Missing authentication. Go to api.gelbooru.com"')
            if qs.get('s') == 'post':
                return _xml('<?xml version="1.0" encoding="UTF-8"?><posts count="1" offset="0"><post id="1" tags=" scenery "/></posts>')
            rows = page(sorted(TAGS), int(qs.get('after_id', 0)), int(qs.get('limit', 100)))
            return _xml('<?xml version="1.0" encoding="UTF-8"?><tags type="array">' + ''.join(
                f'<tag type="{c}" count="{n}" name="{html.escape(name, quote=True)}" ambiguous="false" id="{i}"/>' for i, name, n, c in rows)
                + '</tags>')
        if p.path == '/tags.json':
            rows = page(live, int(qs['page'][1:]), int(qs['limit']))
            return _json([{'id': i, 'name': name, 'post_count': n, 'category': c} for i, name, n, c in rows])
        if p.path == '/tag.json':
            limit, page = int(qs['limit']), int(qs['page'])
            rows = sorted(TAGS, key=lambda t: t[1])[(page - 1) * limit:page * limit]
            return _json([{'id': i, 'name': name, 'count': n, 'type': c, 'ambiguous': False} for i, name, n, c in rows])
        if p.path.startswith('/db_export/'):
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            if p.path != f'/db_export/tags-{yesterday}.csv.gz':
                return 404, b'not found', {'content-type': 'text/html'}
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['id', 'name', 'category', 'post_count'])
            w.writerows([i, name, c, n] for i, name, n, c in TAGS)
            return 200, gzip.compress(buf.getvalue().encode()), {'content-type': 'application/gzip'}
        if p.path == '/api/v1/json/search/tags':
            per_page, page = int(qs['per_page']), int(qs['page'])
            least = int(qs['q'].split(':', 1)[1])
            rows = [{'name': name.replace('_', ' '), 'images': n, 'category': PHILOMENA_CATEGORY[c], 'aliased_tag': None}
                    for i, name, n, c in sorted(TAGS, key=lambda t: -t[2]) if n >= least]
            rows.insert(1, {'name': 'skies', 'images': 700, 'category': None, 'aliased_tag': 'sky'})  # an alias: not a tag of its own
            return _json({'tags': rows[(page - 1) * per_page:page * per_page], 'total': len(rows)})
        if p.path == '/api/tags/':
            if headers.get('Accept') != 'application/json':
                return 406, b'<html>not acceptable</html>', {'content-type': 'text/html'}
            offset, limit = int(qs['offset']), int(qs['limit'])
            rows = [{'names': [name, f'{name}_alias'], 'category': SZURU_CATEGORY[c], 'usages': n} for i, name, n, c in TAGS]
            return _json({'query': '', 'offset': offset, 'limit': limit, 'total': len(rows), 'results': rows[offset:offset + limit]})
        if p.path == '/api/danbooru/find_tags':
            return _xml('<tags>\n' + ''.join(f"<tag type='0' counts='{n}' name='{html.escape(name, quote=True)}' id='{i}'/>\n"
                                             for i, name, n, c in TAGS) + '</tags>')
        return 404, b'<html>not found</html>', {'content-type': 'text/html'}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def clock(self) -> float:
        return self.now


def _read_list(path: pathlib.Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding='utf-8'))


class RobotsTests(TestCase):
    def test_danbooru_like_rules_longest_match(self) -> None:
        r = RobotsRules.parse('User-agent: *\nDisallow: /\nAllow: /$\nDisallow: /*.json\nAllow: /tags\nAllow: /posts\n', 'Lustra')
        self.assertFalse(r.allows('https://x/tags.json?limit=1000'))  # '/*.json' (7) beats '/tags' (5)
        self.assertTrue(r.allows('https://x/tags?search=a'))
        self.assertTrue(r.allows('https://x/'))
        self.assertFalse(r.allows('https://x/other'))
        self.assertTrue(r.allows('https://x/robots.txt'))

    def test_agent_group_wildcards_tie_and_delay(self) -> None:
        text = ('# comment\nDisallow: /before-any-group\n'
                'User-agent: *\nDisallow: /\n\n'
                'User-agent: SomeBot\nUser-agent: lustra/2.0\nDisallow: /private\nAllow: /private/ok$\nCrawl-delay: 3\n\n'
                'User-agent: Lustra\nDisallow: /*.xml$\nAllow: /a\nDisallow: /a\n')
        r = RobotsRules.parse(text, 'Lustra')
        self.assertEqual(3.0, r.crawl_delay)
        self.assertTrue(r.allows('/index.php?page=dapi'))  # the agent's groups are merged, the '*' group is not used
        self.assertFalse(r.allows('/private/x'))
        self.assertTrue(r.allows('/private/ok'))
        self.assertFalse(r.allows('/private/ok/more'))  # '$' anchors
        self.assertFalse(r.allows('/feed.xml'))
        self.assertTrue(r.allows('/feed.xml?x=1'))
        self.assertTrue(r.allows('/a'))  # same length: Allow wins
        self.assertTrue(r.allows('/before-any-group'))  # a rule before any user-agent line belongs to no group
        self.assertFalse(RobotsRules.parse(text, 'Other').allows('/index.php'))  # no group of its own: the '*' group
        self.assertTrue(RobotsRules.allow_all('none (404)').allows('/anything'))
        self.assertFalse(RobotsRules.disallow_all('unreachable (503)').allows('/anything'))


class EngineSourceTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)
        self.patches = [mock.patch.object(tags_updater, 'ENGINE_MIN_INTERVAL', 0.0), mock.patch.object(tags_updater, 'PAGE_LIMIT', 3),
                        mock.patch.object(tags_updater, 'PHILOMENA_PER_PAGE', 3), mock.patch.object(tags_updater, 'SZURUBOORU_PER_PAGE', 3)]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _update(self, engine: str, site: FakeTagSite | None = None, **kw):
        site = site or FakeTagSite()
        session = FakeSession(site)
        src = kw.pop('source', None) or TagSource('zq', engine, SITE, 'Fake')
        res = update_tag_list('zq', self.out / engine, source=src, delay=0, allow_local=True, session=session, **kw)
        return res, session

    def test_every_engine_reads_the_whole_list(self) -> None:
        types = {'gelbooru_dapi': True, 'danbooru_json': True, 'moebooru_json': True, 'e621_json': True, 'philomena_json': True,
                 'szurubooru_json': True, 'shimmie_api': False}
        for engine, typed in types.items():
            with self.subTest(engine=engine):
                res, session = self._update(engine)
                self.assertTrue(res.written, res.message)
                got = _read_list(res.path)
                self.assertEqual(EXPECTED, {k: int(v.split()[0]) for k, v in got.items()})
                if typed:
                    self.assertEqual('40 posts (copyright)', got['tom_&_jerry'])
                    self.assertEqual('30 posts (artist)', got['some_artist'])
                    self.assertEqual('300 posts (character)', got['hatsune_miku'])
                self.assertEqual(f'{SITE}robots.txt', session.calls[0][0])  # robots.txt first, always
                self.assertEqual(len(session.calls), res.requests)
                self.assertTrue(all(url_parse.urlsplit(u).hostname == HOST for u, _ in session.calls))

    def test_capped_page_size_and_inclusive_cursor(self) -> None:
        for engine in ('gelbooru_dapi', 'danbooru_json'):
            for cap, inclusive in ((2, False), (0, True), (2, True)):
                with self.subTest(engine=engine, cap=cap, inclusive=inclusive):
                    res, _ = self._update(engine, FakeTagSite(cap=cap, inclusive=inclusive))
                    self.assertTrue(res.written, res.message)
                    self.assertEqual(set(EXPECTED), set(_read_list(res.path)))
        with mock.patch.object(tags_updater, 'PAGE_LIMIT', 1000):  # a small site: one more request tells the short page is the end
            res, _ = self._update('gelbooru_dapi')
        self.assertEqual(3, res.requests)  # robots.txt, the whole list, the empty page after it

    def test_robots_txt_is_strict_for_extensions_and_reviewed_modules_are_exceptions(self) -> None:
        site = FakeTagSite(robots='User-agent: *\nDisallow: /\nAllow: /$\nDisallow: /*.json\nAllow: /tags\n')
        with self.assertRaises(TagSourceError) as cm:
            self._update('danbooru_json', site)
        self.assertIn('robots.txt', str(cm.exception))
        session = FakeSession(site)
        with self.assertRaises(TagSourceError):
            update_tag_list('zq', self.out, source=TagSource('zq', 'danbooru_json', SITE), delay=0, allow_local=True, session=session)
        self.assertEqual([f'{SITE}robots.txt'], [u for u, _ in session.calls])  # nothing else was asked
        reviewed = TagSource('zq', 'danbooru_json', SITE, robots=False, curated=True)  # like db: the site's documented api
        res, _ = self._update('danbooru_json', site, source=reviewed)
        self.assertTrue(res.written, res.message)
        self.assertFalse(CURATED['db'].robots)
        self.assertTrue(all(s.curated and not s.robots for s in CURATED.values()))

    def test_crawl_delay_and_retry_after(self) -> None:
        site = FakeTagSite(robots='User-agent: *\nCrawl-delay: 7\n')
        site.answers.append((429, b'slow down', {'retry-after': '3'}))
        t = FakeClock()
        times: list[float] = []

        def timed(url: str, headers: dict):
            times.append(t.now)
            return site(url, headers)

        client = PoliteClient(TagSource('zq', 'danbooru_json', SITE), delay=0.5, allow_local=True, session=FakeSession(timed),
                              sleep=t.sleep, clock=t.clock)
        tags = list(tags_updater._iter_danbooru(client, '', 1, 0))
        self.assertEqual(set(EXPECTED), {x[0] for x in tags})
        self.assertEqual(7.0, client.interval)
        self.assertIn(3.0, t.sleeps)  # Retry-After
        self.assertEqual(5, len(times))  # robots.txt, the 429, pages 1 to 3
        self.assertTrue(all(b - a >= 7.0 - 1e-9 for a, b in itertools.pairwise(times)), times)  # paced by the Crawl-delay

    def test_budget_error_is_raised_and_nothing_is_written(self) -> None:
        with self.assertRaises(TagSourceError) as cm:
            self._update('gelbooru_dapi', budget=2)
        self.assertIn('budget', str(cm.exception))
        self.assertFalse((self.out / 'gelbooru_dapi' / 'zq_tags.json').exists())

    def test_challenge_redirect_https_and_keys(self) -> None:
        site = FakeTagSite()
        site.answers.append((503, b'<html>challenge</html>', {'cf-mitigated': 'challenge'}))
        with self.assertRaises(TagSourceError) as cm:
            self._update('danbooru_json', site)
        self.assertIn('challenge', str(cm.exception))
        site.answers.append((302, b'', {'location': 'https://elsewhere.test/tags.json'}))
        with self.assertRaises(TagSourceError) as cm:
            self._update('danbooru_json', site)
        self.assertIn('elsewhere.test', str(cm.exception))
        with self.assertRaises(TagSourceError):
            self._update('danbooru_json', source=TagSource('zq', 'danbooru_json', f'http://{HOST}/'))
        # a site that refuses anonymous requests: the user's own key or nothing, never a request without it
        need = TagSource('zq', 'gelbooru_dapi', SITE, key='required', key_format='API_KEY,USER_ID')
        session = FakeSession(FakeTagSite(need_key=True))
        with self.assertRaises(ValueError) as cm:
            update_tag_list('zq', self.out, source=need, delay=0, allow_local=True, session=session)
        self.assertIn('your own API key', str(cm.exception))
        self.assertEqual([], session.calls)
        res = update_tag_list('zq', self.out, source=need, delay=0, allow_local=True, session=session, api_key='SECRET1, 42')
        self.assertTrue(res.written, res.message)
        self.assertTrue(any('api_key=SECRET1&user_id=42' in u for u, _ in session.calls))
        self.assertEqual('https://x/tags.json?login=<REDACTED>&api_key=<REDACTED>',
                         tags_updater.redact('https://x/tags.json?login=me&api_key=SECRET1'))


class SourceResolutionTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': self.data.as_posix()})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _install_manifest(self, ext_id: str, abbr: str, engine: str, site: str = SITE, api_key=False) -> dict:
        m = make_manifest(ext_id=ext_id, abbr=abbr, name=ext_id.title(), site=site, engine=engine, media=['image'], api_key=api_key)
        folder = self.data / 'extensions' / ext_id
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        return m

    def test_generator_sets_tag_list_for_engines_with_a_source(self) -> None:
        for engine in tags_updater.ENGINE_TAG_SOURCES:
            self.assertEqual('zq_tags.json', make_manifest(ext_id='zqsite', abbr='zq', name='Zq', site=SITE, engine=engine,
                                                            media=['image'])['tagList'], engine)
        for engine in ('shimmie', 'custom'):
            self.assertNotIn('tagList', make_manifest(ext_id='zqsite', abbr='zq', name='Zq', site=SITE, engine=engine, media=['image']))

    def test_sources_of_installed_extensions(self) -> None:
        self._install_manifest('fakebooru', 'fq', 'gelbooru_dapi')
        self._install_manifest('keybooru', 'kq', 'gelbooru_dapi', site='https://keybooru.test/', api_key=True)
        self._install_manifest('moesite', 'mq', 'moebooru_json', site='https://moe.test/')
        self._install_manifest('htmlsite', 'hq', 'custom', site='https://html.test/')
        srcs = {s.module: s for s in tag_sources()}
        self.assertEqual({'db', 'rx', 'fq', 'kq', 'mq'}, set(srcs))
        self.assertEqual(('gelbooru_dapi', SITE, True, False, 'optional'),
                         (srcs['fq'].engine, srcs['fq'].site, srcs['fq'].robots, srcs['fq'].curated, srcs['fq'].key))
        self.assertEqual('required', srcs['kq'].key)
        self.assertEqual('none', srcs['mq'].key)
        self.assertIs(CURATED['db'], srcs['db'])
        self.assertIsNone(tag_source('hq'))
        self.assertIsNone(tag_source('nope'))
        self.assertEqual(['db', 'fq', 'mq'], tags_updater._modules_arg('auto'))  # the system schedule: no module that needs a key
        self.assertEqual(['xb', 'db', 'fq', 'mq'], tags_updater._modules_arg('xb,auto,db'))

    def test_a_reviewed_abbreviation_on_another_site_is_not_reviewed(self) -> None:
        raw = make_manifest(ext_id='otherxb', abbr='xb', name='Other', site='https://other.test/', engine='gelbooru_dapi', media=['image'])
        m = ExtensionManifest.from_dict(raw, None, 'user')
        src = tags_updater.source_for_manifest(m)
        self.assertEqual(('https://other.test/', True, False), (src.site, src.robots, src.curated))
        raw = {**raw, 'site': 'http://other.test/'}
        self.assertIsNone(tags_updater.source_for_manifest(ExtensionManifest.from_dict(raw, None, 'user')))  # https only

    def test_scheduled_command_auto(self) -> None:
        self._install_manifest('fakebooru', 'fq', 'gelbooru_dapi')
        seen: list[str] = []

        def fake_update(module, out_dir, **kw):
            seen.append(module)
            self.assertEqual(module, kw['source'].module)
            res = tags_updater.UpdateResult(module, out_dir / f'{module}_tags.json')
            res.message = 'written 3 tags (was 0)'
            return res

        with mock.patch.object(tags_updater, 'update_tag_list', fake_update), mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(0, tags_updater.main(['--modules', 'auto', '--out', (self.data / 'tags').as_posix()]))
        self.assertEqual(['db', 'fq'], seen)


class GeneratedExtensionEndToEndTests(TestCase):
    """the generator makes a Gelbooru-type extension, it is installed, the app's update button (tags.update) reads its list,
    autocompletion (tags.autocomplete) shows its tags"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': (self.root / 'data').as_posix()})
        self.env.start()
        os.environ.pop('LUSTRA_TAGS_DIR', None)  # restored by the patch
        TagsDB.clear()

    def tearDown(self) -> None:
        TagsDB.clear()
        self.env.stop()
        self.tmp.cleanup()

    def test_generated_gelbooru_extension_gets_its_tag_list(self) -> None:
        site = FakeTagSite(robots='User-agent: *\nDisallow: /admin\n')
        fetcher = SafeFetcher({HOST}, min_interval=0.0, allow_local=True)
        fetcher._session = FakeSession(lambda url, headers=None: site(url, headers or {}))
        sv = survey(SITE, fetcher)
        self.assertEqual('gelbooru_dapi', sv['engine']['engine'])
        draft = create_draft(self.root / 'drafts', sv, ext_id='fakebooru', abbr='fq', name='Fakebooru')
        manifest = json.loads((draft / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual('fq_tags.json', manifest['tagList'])
        self.assertIn('fq_tags.json', (draft / 'specs.md').read_text(encoding='utf-8'))
        shutil.copytree(draft, self.root / 'data' / 'extensions' / 'fakebooru')  # installed (the updater reads manifests only)

        rows = {r['module']: r for r in server.m_tags_sources({})}
        self.assertEqual({'module': 'fq', 'name': 'Fakebooru', 'engine': 'gelbooru_dapi', 'site': SITE, 'robots': True, 'curated': False},
                         {k: rows['fq'][k] for k in ('module', 'name', 'engine', 'site', 'robots', 'curated')})

        session = FakeSession(site)
        done = threading.Event()
        notes: list[tuple[str, dict]] = []

        def notify(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'tags.updateDone':
                done.set()

        public = [(2, 1, 6, '', ('93.184.216.34', 443))]  # the fake host 'resolves' to a public address
        with (mock.patch.object(tags_updater, '_session', lambda: session), mock.patch.object(tags_updater.socket, 'getaddrinfo', return_value=public),
              mock.patch.object(tags_updater, 'ENGINE_MIN_INTERVAL', 0.0), mock.patch.object(server, 'notify', notify)):
            self.assertEqual([], server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'}))
            server.m_tags_update({'module': 'fq', 'delay': 0})
            self.assertTrue(done.wait(WAIT), notes)
        self.assertTrue(notes[-1][1]['ok'] and notes[-1][1]['written'], notes[-1])
        self.assertEqual((self.root / 'data' / 'tags' / 'fq_tags.json').as_posix(), notes[-1][1]['path'])
        self.assertEqual(f'{SITE}robots.txt', session.calls[0][0])
        self.assertEqual(['scenery'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'})])
        self.assertEqual(['tom_&_jerry'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'tom'})])



def _draft(root: pathlib.Path, engine: str = 'gelbooru_dapi', api_key: bool = False, abbr: str = 'fq') -> pathlib.Path:
    draft = root / 'drafts' / 'fakebooru'
    draft.mkdir(parents=True)
    m = make_manifest(ext_id='fakebooru', abbr=abbr, name='Fakebooru', site=SITE, engine=engine, media=['image'], api_key=api_key)
    (draft / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
    (draft / 'downloader.py').write_text('# test\n', encoding='utf-8')
    return draft


class _DataDirTest(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / 'data'
        self.env = mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': self.data.as_posix()})
        self.env.start()
        os.environ.pop('LUSTRA_TAGS_DIR', None)  # restored by the patch
        TagsDB.clear()
        TagsDB.locate_default_lists()

    def tearDown(self) -> None:
        TagsDB.clear()
        self.env.stop()
        self.tmp.cleanup()


class DraftTagListTests(_DataDirTest):
    """the generator puts the site's list into the draft; what does not fit is left to the app (read after the install)"""

    def _bundle(self, draft: pathlib.Path, site: FakeTagSite | None = None, **kw) -> tuple[dict, FakeSession]:
        session = FakeSession(site or FakeTagSite())
        return bundle_tag_list(draft, session=session, allow_local=True, min_interval=0.0, **kw), session

    def test_the_list_goes_into_the_draft_and_the_archive(self) -> None:
        draft = _draft(self.root)
        res, session = self._bundle(draft)
        self.assertEqual(('bundled', 'fq_tags.json', len(EXPECTED)), (res['status'], res['file'], res['count']), res)
        self.assertEqual(EXPECTED, {k: int(v.split()[0]) for k, v in _read_list(draft / 'fq_tags.json').items()})
        self.assertEqual([], [p.name for p in draft.iterdir() if p.name.startswith('.taglist-')])
        self.assertEqual(f'{SITE}robots.txt', session.calls[0][0])
        z = pack(draft, self.root / 'fakebooru.zip')
        with zipfile.ZipFile(z['path']) as zf:
            self.assertIn('fakebooru/fq_tags.json', zf.namelist())

    def test_what_does_not_fit_is_left_to_the_app_or_learned(self) -> None:
        cases = {
            'requests': ({'budget': 2}, FakeTagSite(), 'later'),
            'size': ({'max_bytes': 10}, FakeTagSite(), 'later'),
            'robots': ({}, FakeTagSite(robots='User-agent: *\nDisallow: /index.php\n'), 'learned'),
            'no answer': ({}, FakeTagSite(robots='User-agent: *\nAllow: /\n'), 'failed'),
        }
        for name, (kw, site, status) in cases.items():
            with self.subTest(name):
                draft = _draft(self.root / name)
                if name == 'no answer':
                    site.answers.append((500, b'oops', {'content-type': 'text/html'}))
                res, _ = self._bundle(draft, site, **kw)
                self.assertEqual(status, res['status'], res)
                self.assertFalse((draft / 'fq_tags.json').exists())
        res, session = self._bundle(_draft(self.root / 'key', api_key=True))  # the site needs the user's key: no request here
        self.assertEqual('later', res['status'])
        self.assertEqual([], session.calls)
        res, session = self._bundle(_draft(self.root / 'html', engine='custom'))
        self.assertEqual('learned', res['status'])
        self.assertEqual([], session.calls)

    def test_a_ready_generator_session_ships_the_list(self) -> None:
        from lustra.tests.test_extgen import fake_fetcher, fake_gelbooru
        tags = FakeTagSite()

        def route(url: str):
            return tags(url, {}) if 's=tag' in url else fake_gelbooru(url)

        report = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=self.root / 'gen', fetcher=fake_fetcher(route)).run()
        self.assertEqual('ready', report['status'], report['summary'])
        self.assertEqual('bundled', report['tagList']['status'], report['tagList'])
        self.assertIn(f'{report["abbr"]}_tags.json', report['files'])
        off = GenSession(GenRequest(url=SITE, mode='template', tag_list=False), drafts_root=self.root / 'off', fetcher=fake_fetcher(route)).run()
        self.assertEqual(('ready', {}), (off['status'], off['tagList']))

    def test_installed_extension_list_is_used_and_a_newer_list_wins(self) -> None:
        draft = _draft(self.root)
        self._bundle(draft)
        installed = self.data / 'extensions' / 'fakebooru'
        shutil.copytree(draft, installed)
        TagsDB.locate_default_lists()
        self.assertEqual(installed / 'fq_tags.json', TagsDB.get_file('fq'))
        self.assertEqual(['scenery'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'})])
        newer = self.data / 'tags' / 'fq_tags.json'
        newer.parent.mkdir(parents=True)
        tags_updater.write_tag_list(newer, {'scenery': (999, 'general'), 'scarf': (5, 'general')})
        later = time.time() + 60
        os.utime(newer, (later, later))
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual(newer, TagsDB.get_file('fq'))
        self.assertEqual(['scenery', 'scarf'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'})])

    def test_an_extension_ships_only_its_own_list(self) -> None:
        folder = self.data / 'extensions' / 'fakebooru'
        folder.mkdir(parents=True)
        m = {**make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image']),
             'tagList': 'rx_tags.json'}
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        tags_updater.write_tag_list(folder / 'rx_tags.json', {'fake': (1, 'general')})
        self.assertNotIn('rx', TagsDB.extension_lists())
        self.assertNotIn('fq', TagsDB.extension_lists())


class LearnedTagListTests(_DataDirTest):
    """a site without a tag listing: the list is learned from the posts previewed and downloaded, no request"""

    def setUp(self) -> None:
        super().setUp()
        os.environ['LUSTRA_TAGS_LEARN'] = '1'  # off for the other tests (lustra/tests/__init__.py); restored by the patch

    def test_preview_posts_teach_a_module_without_a_list(self) -> None:
        self.assertTrue(tags_learned.may_learn('zq'))
        notes: list[tuple[str, dict]] = []
        with mock.patch.object(server, 'notify', lambda m, p: notes.append((m, p))):
            server._learn_quietly('zq', [('1', ['Sky', 'cloud', 'blue sky']), ('2', ['sky']), ('1', ['sky', 'again'])])
        self.assertEqual('tags.learned', notes[-1][0])
        self.assertEqual({'module': 'zq', 'learned': True, 'posts': 2, 'tags': 3}, notes[-1][1])
        self.assertEqual({'sky': '2 posts (unknown)', 'cloud': '1 posts (unknown)', 'blue_sky': '1 posts (unknown)'},
                         _read_list(tags_learned.list_path('zq')))
        self.assertEqual(['sky'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'zq', 'prefix': 'sk'})])
        row = next(r for r in server.m_tags_status({}) if r['module'] == 'zq')
        self.assertTrue(row['learned'])
        self.assertEqual(0, tags_learned.learn('zq', [('2', ['sky'])]))  # seen before: counted once

    def test_downloads_teach_it_from_the_index(self) -> None:
        tags_learned.learn('zq', [('1', ['sky'])])  # seen in Preview first
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='zq', post_id='1', path='/x/zq_1.jpg', tags='sky'),
                            IndexRecord(module='zq', post_id='2', path='/x/zq_2.jpg', tags='sky tree'),
                            IndexRecord(module='other', post_id='3', path='/x/o_3.jpg', tags='ignored')])
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((1, True, 2), (res['newPosts'], res['learned'], res['posts']))
        self.assertEqual(0, server.m_tags_learn({'module': 'zq'})['newPosts'])  # the index cursor moved on
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='zq', post_id='4', path='/x/zq_4.jpg', tags='tree')])
        self.assertEqual(1, server.m_tags_learn({'module': 'zq'})['newPosts'])
        self.assertEqual({'sky': '2 posts (unknown)', 'tree': '2 posts (unknown)'}, _read_list(tags_learned.list_path('zq')))
        with self.assertRaises(server.RpcError):
            server.m_tags_learn({'module': '../x'})

    def test_a_list_from_a_source_stops_the_learning(self) -> None:
        tags_learned.learn('zq', [('1', ['sky'])])
        real = tags_learned.list_path('zq')
        tags_updater.write_tag_list(real, {'sky': (5000, 'general'), 'skyline': (40, 'general')})  # the updater got the real list
        TagsDB.locate_default_lists(reload=('zq',))
        self.assertFalse(tags_learned.may_learn('zq'))
        self.assertEqual(0, tags_learned.learn('zq', [('2', ['sky', 'new'])]))
        self.assertEqual({'sky': '5000 posts (general)', 'skyline': '40 posts (general)'}, _read_list(real))
        self.assertFalse(tags_learned.status('zq')['learned'])
        self.assertFalse(tags_learned.may_learn('rp'))  # a list bundled in 2tags/
        TagsDB.load_from_dict('zm', {'sky': (TagTypes.TAG_TYPE_GENERAL, 3)})
        self.assertFalse(tags_learned.may_learn('zm'))  # a list registered in memory
        with mock.patch.dict(os.environ, {'LUSTRA_TAGS_LEARN': '0'}):
            self.assertFalse(tags_learned.may_learn('zn'))
            self.assertEqual(0, tags_learned.learn('zn', [('1', ['sky'])]))
        self.assertFalse(tags_learned.may_learn('../x'))
        with mock.patch.object(server.threading, 'Thread') as thread:  # Preview: nothing to learn, no thread
            server._learn_from_preview('rp', [{'id': '1', 'tags': ['a']}])
            thread.assert_not_called()

#
#
#########################################
