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

from lustra import network, server, tags_learned, tags_updater  # noqa: E402
from lustra.aigen.session import GenRequest, GenSession  # noqa: E402
from lustra.defines import RATE_LIMIT_RX  # noqa: E402
from lustra.dlindex import DownloadIndex, IndexRecord  # noqa: E402
from lustra.downloaders import _make_downloader  # noqa: E402
from lustra.extgen.fetch import SafeFetcher  # noqa: E402
from lustra.extgen.sandbox import pack  # noqa: E402
from lustra.extgen.scaffold import create_draft, survey  # noqa: E402
from lustra.extgen.taglist import bundle_tag_list  # noqa: E402
from lustra.extgen.templates import make_manifest  # noqa: E402
from lustra.modspec import ExtensionManifest  # noqa: E402
from lustra.module import ProcModule  # noqa: E402
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
            # pages by number (pid), in the site's own order: like rule34.xxx, not by id (an after_id cursor would skip tags)
            limit = min(int(qs.get('limit', 100)), self.cap or 1000)
            pid = int(qs.get('pid', 0))
            rows = sorted(TAGS, key=lambda t: t[1])[pid * limit:(pid + 1) * limit]
            if not rows:
                return _xml('<?xml version="1.0" encoding="UTF-8"?><tag type="array"/>')  # rule34.xxx's empty page
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
        self.assertEqual(10.5, client.interval)  # the Crawl-delay, 1.5 times slower after the 429 (no rate stated)
        self.assertIn(3.0, t.sleeps)  # Retry-After
        self.assertEqual(5, len(times))  # robots.txt, the 429, pages 1 to 3
        self.assertTrue(all(b - a >= 7.0 - 1e-9 for a, b in itertools.pairwise(times)), times)  # paced by the Crawl-delay

    def test_rate_limit_text_sets_the_pace(self) -> None:
        # api.rule34.xxx answers 429 'You currently have a limit of 60 requests every 60 second(s).': the rest goes at that pace
        site = FakeTagSite()
        site.answers.append((429, b'You currently have a limit of 60 requests every 60 second(s).', {'content-type': 'text/html'}))
        t = FakeClock()
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0.5, allow_local=True, session=FakeSession(site),
                              sleep=t.sleep, clock=t.clock)
        told: list[str] = []
        client.notify = told.append
        tags = list(tags_updater._iter_gelbooru(client, '', 1, 0))
        self.assertEqual(set(EXPECTED), {x[0] for x in tags})
        self.assertAlmostEqual(60 / 60 * tags_updater.RATE_LIMIT_MARGIN, client.interval)
        self.assertEqual(tags_updater.RATE_LIMIT_WAITS[0], client.waited)
        self.assertEqual([f'{HOST} asks to wait (HTTP 429): waiting 5 s, then one request every 1.05 s'], told)  # the user sees why
        # rx: its published limit is the pace from the first request
        rx = PoliteClient(CURATED['rx'], delay=0.5)
        self.assertGreaterEqual(rx.interval, 1.05)

    def test_waits_grow_then_the_list_stops(self) -> None:
        site = FakeTagSite()
        site.answers.extend([(429, b'slow down', {'content-type': 'text/plain'})] * (len(tags_updater.RATE_LIMIT_WAITS) + 1))
        t = FakeClock()
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0, allow_local=True, session=FakeSession(site),
                              sleep=t.sleep, clock=t.clock)
        with self.assertRaisesRegex(RuntimeError, 'still asks to wait'):
            list(tags_updater._iter_gelbooru(client, '', 1, 0))
        waits = [s for s in t.sleeps if s in tags_updater.RATE_LIMIT_WAITS]
        self.assertEqual(list(tags_updater.RATE_LIMIT_WAITS), waits)
        self.assertEqual(sum(tags_updater.RATE_LIMIT_WAITS), client.waited)
        self.assertGreaterEqual(client.interval, 1.0)  # slowed down

    def test_a_stopped_update_continues_where_it_stopped(self) -> None:
        for engine in ('gelbooru_dapi', 'danbooru_json'):
            with self.subTest(engine=engine):
                out = self.out / f'resume-{engine}'
                seen: list[int] = []

                def failing(url: str, headers: dict, site=FakeTagSite(cap=2), calls=[0]):
                    qs = {k: v[0] for k, v in url_parse.parse_qs(url_parse.urlsplit(url).query).items()}
                    if 'pid' in qs or 'page' in qs:
                        calls[0] += 1
                        if calls[0] == 3:  # the third page fails: two pages (4 tags) are kept
                            return 500, b'oops', {'content-type': 'text/plain'}
                    return site(url, headers)
                src = TagSource('zq', engine, SITE, 'Fake')
                with self.assertRaises(RuntimeError):
                    update_tag_list('zq', out, source=src, delay=0, allow_local=True, session=FakeSession(failing))
                partial = out / tags_updater.PARTIAL_DIR / 'zq_tags.json'
                self.assertTrue(partial.is_file())
                self.assertFalse((out / 'zq_tags.json').exists())  # nothing half done is used as the list
                kept = json.loads(partial.read_text(encoding='utf-8'))
                self.assertEqual(tags_updater.RESUMABLE_ENGINES[engine], kept['cursorKind'])
                if engine == 'gelbooru_dapi':  # a partial of the old after_id reader (no cursor kind) is not continued from
                    old = dict(kept, cursorKind=None)
                    partial.with_name('old.json').write_text(json.dumps(old), encoding='utf-8')
                    self.assertIsNone(tags_updater._load_partial(partial.with_name('old.json'), src))
                    partial.with_name('old.json').unlink()

                def recording(url: str, headers: dict, site=FakeTagSite(cap=2)):
                    qs = {k: v[0] for k, v in url_parse.parse_qs(url_parse.urlsplit(url).query).items()}
                    if 'pid' in qs:
                        seen.append(int(qs['pid']))
                    elif 'page' in qs and str(qs['page']).startswith('a'):
                        seen.append(int(qs['page'][1:]))
                    return site(url, headers)
                notes: list[str] = []
                res = update_tag_list('zq', out, source=src, delay=0, allow_local=True, session=FakeSession(recording),
                                      progress=lambda msg, _n: notes.append(msg))
                self.assertTrue(res.written, res.message)
                self.assertEqual(set(EXPECTED), set(_read_list(res.path)))
                self.assertGreater(seen[0], 0)  # started from the saved cursor (page 2 / an id), not from the first page
                self.assertTrue(any('continuing a stopped update' in n for n in notes), notes)
                self.assertFalse(partial.exists())  # complete: nothing left to continue

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


class StorageOrderSite:
    """A Gelbooru tag index in storage order, like rule34.xxx: a tag whose post count changes, and every new tag, goes to the end"""

    def __init__(self, n: int = 20) -> None:
        self.rows = [[i, f'tag{i:02d}', 100 + i] for i in range(1, n + 1)]
        self.next_id = n + 1
        self.pids: list[int] = []
        self.fail_at: int | None = None  # answer 500 for this pid, once

    def bump(self, name: str, count: int) -> None:
        row = next(r for r in self.rows if r[1] == name)
        self.rows.remove(row)
        self.rows.append([row[0], name, count])

    def add(self, name: str, count: int) -> None:
        self.rows.append([self.next_id, name, count])
        self.next_id += 1

    def __call__(self, url: str, headers: dict) -> tuple[int, bytes, dict]:
        p = url_parse.urlsplit(url)
        if p.path == '/robots.txt':
            return 404, b'none', {'content-type': 'text/html'}
        qs = {k: v[0] for k, v in url_parse.parse_qs(p.query).items()}
        pid, limit = int(qs['pid']), tags_updater.PAGE_LIMIT
        if pid == self.fail_at:
            self.fail_at = None
            return 500, b'oops', {'content-type': 'text/plain'}
        self.pids.append(pid)
        rows = self.rows[pid * limit:(pid + 1) * limit]
        if not rows:
            return _xml('<?xml version="1.0" encoding="UTF-8"?><tag type="array"/>')
        return _xml('<?xml version="1.0" encoding="UTF-8"?><tags type="array">' + ''.join(
            f'<tag type="0" count="{n}" name="{name}" ambiguous="false" id="{i}"/>' for i, name, n in rows) + '</tags>')


class TailUpdateTests(TestCase):
    """Rule34: a weekly update reads only the last pages (new tags and tags whose count changed are there) and merges them"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)
        self.patches = [mock.patch.object(tags_updater, 'PAGE_LIMIT', 3), mock.patch.object(tags_updater, 'TAIL_QUIET_PAGES', 2),
                        mock.patch.object(tags_updater, 'TAIL_MIN_PAGES', 1), mock.patch.object(tags_updater, 'ENGINE_MIN_INTERVAL', 0.0)]
        for x in self.patches:
            x.start()
        self.src = TagSource('zq', 'gelbooru_dapi', SITE, 'Fake', tail_updates=True)
        self.site = StorageOrderSite()

    def tearDown(self) -> None:
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def _update(self, **kw):
        self.site.pids.clear()
        notes: list[str] = []
        src = kw.pop('source', self.src)
        res = update_tag_list('zq', self.out, source=src, delay=0, allow_local=True, session=FakeSession(self.site),
                              progress=lambda msg, _n: notes.append(msg), **kw)
        return res, notes

    def _meta(self) -> dict:
        return json.loads((self.out / tags_updater.META_DIR / 'zq_tags.json').read_text(encoding='utf-8'))

    def test_first_update_reads_everything_then_the_tail_only(self) -> None:
        res, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual(list(range(8)), self.site.pids)  # 20 tags, 3 a page: 7 pages and the empty one
        self.assertIn('full read of 7 pages', res.message)
        self.assertEqual(7, self._meta()['pages'])
        full_at = self._meta()['fullAt']
        self.site.bump('tag03', 999)  # its count changed: its row moves to the end
        self.site.add('newtag', 5)
        res, notes = self._update()
        self.assertTrue(res.written, res.message)
        # back from the last read's end while pages change (6: tag03 moved there), 2 quiet pages (5, 4), then on to the empty page
        self.assertEqual([6, 5, 4, 7], self.site.pids)
        got = _read_list(res.path)
        self.assertEqual(21, len(got))  # every tag of the full read is kept
        self.assertEqual('999 posts (general)', got['tag03'])
        self.assertEqual('5 posts (general)', got['newtag'])
        self.assertEqual('101 posts (general)', got['tag01'])  # not in the tail: kept as it was
        self.assertIn('the end of the list only: pages 4-6 of 7, 1 new, 1 updated', res.message)
        self.assertTrue(any('reading the end of the list only' in n for n in notes), notes)
        self.assertEqual(7, self._meta()['pages'])
        self.assertEqual(full_at, self._meta()['fullAt'])  # a tail is not a full read

    def test_the_tail_reads_back_as_far_as_the_counts_changed(self) -> None:
        self._update()
        changed = [f'tag{i:02d}' for i in range(5, 14)]  # 9 rows: 3 pages at the end
        for i, name in enumerate(changed):
            self.site.bump(name, 500 + i)
        res, _ = self._update()
        self.assertEqual([6, 5, 4, 3, 2, 1, 7], self.site.pids)  # 4 pages with changed rows (3 holds tag05), 2 quiet, the end
        got = _read_list(res.path)
        self.assertEqual({f'{500 + i} posts (general)' for i in range(9)}, {got[n] for n in changed})
        with mock.patch.object(tags_updater, 'TAIL_MAX_PAGES', 2):  # never longer than TAIL_MAX_PAGES back
            for name in changed:
                self.site.bump(name, 1)
            self._update()
        self.assertEqual([6, 5, 7], self.site.pids)

    def test_full_read_when_asked_too_old_or_unknown(self) -> None:
        self._update()
        res, _ = self._update(full=True)
        self.assertEqual(0, self.site.pids[0])
        self.assertIn('full read', res.message)
        meta_path = self.out / tags_updater.META_DIR / 'zq_tags.json'
        meta = self._meta()
        meta['fullAt'] -= tags_updater.FULL_EVERY + 60
        meta_path.write_text(json.dumps(meta), encoding='utf-8')
        self._update()
        self.assertEqual(0, self.site.pids[0])  # the last full read is too old
        meta_path.unlink()
        self._update(source=TagSource('zq', 'gelbooru_dapi', SITE, 'Fake'))
        self.assertEqual(0, self.site.pids[0])  # a source without tail updates always reads everything
        self.assertFalse(meta_path.exists())  # and keeps no read state
        self._update()
        self.assertEqual(0, self.site.pids[0])  # how the list was read is unknown
        (self.out / 'zq_tags.json').unlink()
        self._update()
        self.assertEqual(0, self.site.pids[0])  # nothing to merge into

    def test_a_stopped_full_read_continues_before_any_tail(self) -> None:
        self._update()
        self.site.fail_at = 3
        with self.assertRaises(RuntimeError):
            self._update(full=True)
        res, notes = self._update()  # auto: the stopped full read goes on, it is not replaced by a tail
        self.assertTrue(res.written, res.message)
        self.assertEqual(3, self.site.pids[0])
        self.assertIn('full read', res.message)
        self.assertTrue(any('continuing a stopped update' in n for n in notes), notes)

    def test_a_stopped_tail_continues_as_a_tail(self) -> None:
        self._update()
        self.site.add('late', 7)
        self.site.bump('tag20', 1)
        self.site.fail_at = 5
        with self.assertRaises(RuntimeError):
            self._update()
        kept = json.loads((self.out / tags_updater.PARTIAL_DIR / 'zq_tags.json').read_text(encoding='utf-8'))
        self.assertEqual('tail', kept['mode'])
        self.assertLess(len(kept['tags']), 10)  # only the tail's tags, not the whole list
        res, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual(5, self.site.pids[0])  # where it stopped, not the end of the list
        got = _read_list(res.path)
        self.assertEqual(21, len(got))
        self.assertEqual('7 posts (general)', got['late'])
        self.assertEqual('1 posts (general)', got['tag20'])

    def test_read_tag_list_reads_what_write_tag_list_wrote(self) -> None:
        tags = {'plain': (5, 'general'), 'say_"hi"': (3, 'artist'), 'back\\slash': (2, 'character'), 'ünïcode': (1, 'metadata')}
        path = self.out / 'zz_tags.json'
        tags_updater.write_tag_list(path, tags)
        self.assertEqual(tags, tags_updater.read_tag_list(path))


class KeyRateLimitTests(_DataDirTest):
    """A limit per API key (rule34.xxx: 60 requests every 60 s) that the site's admins may raise for the user's key: the app writes the
    new limit to <data dir>/ratelimit/limits.json, every core process paces the key with it, together (shared_rate_wait)"""

    def _write(self, limits: dict) -> None:
        f = self.data / 'ratelimit' / network.RATE_LIMITS_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(limits), encoding='utf-8')
        os.utime(f, ns=(time.time_ns(), time.time_ns() + 1))  # a new stamp even within the same clock tick

    def test_published_file_and_env(self) -> None:
        os.environ.pop('LUSTRA_RATE_LIMIT_RX', None)
        self.assertEqual((60, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # nothing set: what the site publishes
        self.assertAlmostEqual(1.05, network.key_rate_interval('rx', RATE_LIMIT_RX))
        self.assertIsNone(network.key_rate_limit('zq', None))
        self.assertEqual(0.0, network.key_rate_interval('zq', None))
        self._write({'rx': 120, 'xb': -1, 'bb': 'x', 'db': True})
        self.assertEqual((120, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # the app's setting
        self.assertAlmostEqual(0.525, network.key_rate_interval('rx', RATE_LIMIT_RX))
        for bad in ('xb', 'bb', 'db'):
            self.assertIsNone(network.key_rate_limit(bad, None), bad)
        self._write({'rx': 240})
        self.assertEqual((240, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # read again once the file changed
        with mock.patch.dict(os.environ, {'LUSTRA_RATE_LIMIT_RX': '90'}):
            self.assertEqual((90, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # CLI / cron
        with mock.patch.dict(os.environ, {'LUSTRA_RATE_LIMIT_RX': 'lots'}):
            self.assertEqual((240, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))

    def test_downloads_and_tag_updates_pace_the_key_together(self) -> None:
        os.environ.pop('LUSTRA_RATE_LIMIT_RX', None)
        dwn = _make_downloader(ProcModule.RX)
        self.assertAlmostEqual(1.05, dwn._get_min_request_interval())
        dwn.favorites_search_user = '1'  # html pages of the main site: not the API
        self.assertEqual(0.0, dwn._get_min_request_interval())
        dwn.favorites_search_user = ''
        self._write({'rx': 120})
        self.assertAlmostEqual(0.525, dwn._get_min_request_interval())
        client = PoliteClient(CURATED['rx'], delay=0)
        self.assertAlmostEqual(0.525, client.interval)
        self.assertEqual(dwn._get_sitename(), client._shared)  # the same lock file as the downloads of that key
        calls: list[tuple[str, float]] = []
        with mock.patch.object(tags_updater, 'shared_rate_wait', lambda site, interval: calls.append((site, interval)) or True):
            client._pace()
        self.assertEqual([(CURATED['rx'].site, 0.525)], calls)
        self.assertFalse(PoliteClient(TagSource('zq', 'danbooru_json', SITE), delay=0, allow_local=True)._shared)  # no known limit

    def test_shared_wait_spaces_two_clients(self) -> None:
        with mock.patch.dict(os.environ, {'LUSTRA_RATE_LIMIT_ZQ': '600'}):  # 0.105 s
            src = TagSource('zq', 'danbooru_json', SITE, rate_limit=(1, 1))
            a, b = PoliteClient(src, delay=0, allow_local=True), PoliteClient(src, delay=0, allow_local=True)
            started = time.monotonic()
            for c in (a, b, a, b):
                c._pace()
            self.assertGreaterEqual(time.monotonic() - started, 3 * 0.105 * 0.9)  # 4 requests of two clients: 3 gaps

    def test_extensions_declare_their_limit_in_the_manifest(self) -> None:
        self.assertEqual((30, 60), tags_updater.manifest_rate_limit({'rateLimit': {'requests': 30, 'seconds': 60}}))
        for bad in ({}, {'rateLimit': {'requests': 0, 'seconds': 60}}, {'rateLimit': {'requests': True, 'seconds': 60}},
                    {'rateLimit': [60, 60]}, None):
            self.assertIsNone(tags_updater.manifest_rate_limit(bad), bad)
        rx = json.loads((pathlib.Path(tags_updater.__file__).parent / 'builtin_ext' / 'rule34' / 'manifest.json').read_text('utf-8'))
        self.assertEqual(RATE_LIMIT_RX, tags_updater.manifest_rate_limit(rx['auth']))  # what the app shows = what the core uses


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
