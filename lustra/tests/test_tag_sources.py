# coding=UTF-8
"""
Lustra: tag list sources for any extension (lustra/tags_updater.py, lustra/robots.py), offline.
A fake site answers every engine's tag listing through a fake transport; the updater's real policy code runs (robots.txt, pacing,
Retry-After, request budget, bot challenges, hosts). End to end: a Gelbooru-type extension made by the generator (lustra.extgen)
gets its list from the updater and autocompletion shows it.
Every new extension has a list at once: the generator puts the site's list into the draft and the archive (lustra/extgen/taglist.py),
the installed extension's list is used (TagsDB.extension_lists), and a site without a tag listing gets its list learned from the
posts previewed and downloaded (lustra/tags_learned.py); a list from a source wins over a learned one.
A running update is listed by tags.sources until it ends; e621's tag export is found on its db_exports page (a trimmed copy in
lustra/tests/fixtures/) and checked against the SHA-256 listed there.
Run: python -m unittest lustra.tests.test_tag_sources
"""
#########################################
#
#

import csv
import datetime
import gzip
import hashlib
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

from lustra import network, server, tags_learned, tags_updater, tagsdb  # noqa: E402
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

    def test_progress_comes_with_time_not_with_a_count_of_tags(self) -> None:
        # a slow read (Bbooru: 1000 tags a page, a page a second or more) told only every 5000 tags looked stopped for minutes
        def counts(interval: float) -> list[int]:
            notes: list[tuple[str, int]] = []
            with mock.patch.object(tags_updater, 'PROGRESS_INTERVAL', interval):
                res, _ = self._update('danbooru_json', progress=lambda msg, n: notes.append((msg, n)))
            self.assertTrue(res.written, res.message)
            self.assertEqual(f'[zq] {len(EXPECTED):d} tags read, writing the list', notes[-1][0])
            return [n for msg, n in notes if msg.endswith(' tags...')]

        self.assertEqual(list(range(1, len(EXPECTED) + 1)), counts(0.0))  # each tag as it comes when the interval is over
        self.assertEqual([1], counts(3600.0))  # the first tag at once, then once per interval
        self.assertEqual(2.0, tags_updater.PROGRESS_INTERVAL)

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
        self.assertEqual((0, False), tags_learned.learn('zq', [('2', ['sky'])]))  # seen before: counted once

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
        self.assertEqual((0, False), tags_learned.learn('zq', [('2', ['sky', 'new'])]))
        self.assertEqual({'sky': '5000 posts (general)', 'skyline': '40 posts (general)'}, _read_list(real))
        self.assertFalse(tags_learned.status('zq')['learned'])
        self.assertFalse(tags_learned.may_learn('rp'))  # a list bundled in 2tags/
        TagsDB.load_from_dict('zm', {'sky': (TagTypes.TAG_TYPE_GENERAL, 3)})
        self.assertFalse(tags_learned.may_learn('zm'))  # a list registered in memory
        with mock.patch.dict(os.environ, {'LUSTRA_TAGS_LEARN': '0'}):
            self.assertFalse(tags_learned.may_learn('zn'))
            self.assertEqual((0, False), tags_learned.learn('zn', [('1', ['sky'])]))
        self.assertFalse(tags_learned.may_learn('../x'))
        with mock.patch.object(server.threading, 'Thread') as thread:  # Preview: nothing to learn, no thread
            server._learn_from_preview('rp', [{'id': '1', 'tags': ['a']}])
            thread.assert_not_called()

    def test_a_list_from_a_source_wins_over_a_newer_learned_list(self) -> None:
        # a list learned from Preview, then an extension with a tag source ships the site's list (older file). An extension
        # without a source (Rule34Hentai 1.0.2) gets the learning on top of its list instead: BundledListLearningTests
        tags_learned.learn('fq', [('1', ['sky'])])
        learned = tags_learned.list_path('fq')
        self.assertEqual(learned, TagsDB.get_file('fq'))
        folder = self.data / 'extensions' / 'fakebooru'
        folder.mkdir(parents=True)
        m = {**make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image']),
             'tagList': 'fq_tags.json'}
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        tags_updater.write_tag_list(folder / 'fq_tags.json', {'sky': (5000, 'general'), 'skyline': (40, 'general')})
        old = time.time() - 30 * 86400
        os.utime(folder / 'fq_tags.json', (old, old))
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual(folder / 'fq_tags.json', TagsDB.get_file('fq'))
        self.assertEqual(['sky', 'skyline'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sk'})])
        self.assertFalse(tags_learned.may_learn('fq'))
        self.assertTrue(tags_learned.has_source_list('fq'))
        self.assertEqual((0, False), tags_learned.learn('fq', [('2', ['sky', 'new'])]))
        shutil.rmtree(folder)  # uninstalled: the learned list is the module's list again
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual(learned, TagsDB.get_file('fq'))
        self.assertTrue(tags_learned.may_learn('fq'))

    def test_learn_says_why_nothing_was_learned(self) -> None:
        # "Học từ file đã tải" (Tags A-Z): nothing downloaded yet, nothing new, or no learning needed (a list from a source)
        keys = ('mayLearn', 'sourceList', 'indexed', 'newPosts')
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((True, False, False, 0), tuple(res[k] for k in keys))
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='zq', post_id='1', path='/x/zq_1.jpg', tags='sky')])
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((True, False, True, 1), tuple(res[k] for k in keys))
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((True, False, True, 0), tuple(res[k] for k in keys))
        self.assertEqual({'learned': True, 'posts': 1, 'tags': 1}, {k: res[k] for k in ('learned', 'posts', 'tags')})
        TagsDB.load_from_dict('zm', {'sky': (TagTypes.TAG_TYPE_GENERAL, 3)})
        res = server.m_tags_learn({'module': 'zm'})
        self.assertEqual((False, True, False, 0), tuple(res[k] for k in keys))
        self.assertFalse(server.m_tags_learn({'module': 'rp'})['mayLearn'])  # its list is bundled in 2tags/
        self.assertTrue(server.m_tags_learn({'module': 'rp'})['sourceList'])


class BundledListLearningTests(_DataDirTest):
    """a list shipped inside an extension that has no tag source (Rule34Hentai, Paheal, Rule34.us 1.0.2) is never updated: the tags
    of the posts previewed and downloaded that it lacks are learned on top of it, in a small file of their own (rule34.us: 18.9 MB,
    544k tags, never rewritten or parsed again for it)"""
    BUNDLED = {'sky': (5000, 'general'), 'skyline': (40, 'general'), 'tree': (300, 'general')}

    def setUp(self) -> None:
        super().setUp()
        os.environ['LUSTRA_TAGS_LEARN'] = '1'  # off for the other tests (lustra/tests/__init__.py); restored by the patch
        self.folder = self.data / 'extensions' / 'fakebooru'

    def _source(self, tags: dict, version: str = '1.0.2', engine: str = 'custom', **extra) -> pathlib.Path:
        src = self.root / 'src' / version / 'fakebooru'
        src.mkdir(parents=True)
        m = {**make_manifest(ext_id='fakebooru', abbr=extra.pop('abbr', 'fq'), name='Fakebooru', site=extra.pop('site', SITE),
                             engine=engine, media=['image']), 'version': version, **extra}
        m['tagList'] = f'{m["abbr"]}_tags.json'
        (src / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        tags_updater.write_tag_list(src / m['tagList'], tags)
        return src

    def _install(self, tags: dict | None = None, version: str = '1.0.2', engine: str = 'custom', src: pathlib.Path | None = None,
                 **extra) -> dict:
        """ext.install of the core (the copy stands in for the transactional install, which imports the extension's code)"""
        src = src or self._source(self.BUNDLED if tags is None else tags, version, engine, **extra)

        def copy(path: pathlib.Path) -> ExtensionManifest:
            shutil.rmtree(self.folder, ignore_errors=True)
            shutil.copytree(path, self.folder)
            return ExtensionManifest.load(self.folder / 'manifest.json', 'user')

        with mock.patch.object(server.Extensions, 'install_from_dir', copy):
            return server.m_ext_install({'path': str(src)})

    def _learn(self, posts: list) -> list[tuple[str, dict]]:
        notes: list[tuple[str, dict]] = []
        with mock.patch.object(server, 'notify', lambda m, p: notes.append((m, p))):
            server._learn_quietly('fq', posts)
        return notes

    @staticmethod
    def _row(module: str = 'fq') -> dict:
        return next(r for r in server.m_tags_status({}) if r['module'] == module)

    @staticmethod
    def _all(module: str = 'fq') -> dict[str, int]:
        total, rows = TagsDB.browse(module, limit=1000)
        names = [name for name, _ in rows]
        assert len(names) == len(set(names)) == total, names  # never a duplicate
        return {name: info.posts_count for name, info in rows}

    def test_tags_the_bundled_list_lacks_are_learned_on_top_of_it(self) -> None:
        self._install()
        bundled = self.folder / 'fq_tags.json'
        before = bundled.stat()
        self.assertEqual((bundled, '1.0.2'), (TagsDB.bundled('fq').path, TagsDB.bundled('fq').version))
        self.assertTrue(tags_learned.may_learn('fq'))
        self.assertFalse(tags_learned.has_source_list('fq'))
        self.assertEqual({'bundled': True, 'version': '1.0.2', 'added': 0, 'learned': False},
                         {k: self._row()[k] for k in ('bundled', 'version', 'added', 'learned')})
        with mock.patch.object(server.threading, 'Thread') as thread:  # Preview starts the learning
            server._learn_from_preview('fq', [{'id': '1', 'tags': ['a']}])
            thread.assert_called_once()
        notes = self._learn([('1', ['Sky', 'new tag', 'tree']), ('2', ['new_tag', 'skyfall']), ('1', ['again'])])
        self.assertEqual(('tags.learned', {'module': 'fq', 'learned': False, 'posts': 2, 'tags': 4}), notes[-1])
        # its own file holds only what the bundled list lacks, out of the lists' names; the bundled list is untouched
        added = tags_learned.additions_path('fq')
        self.assertEqual(self.data / 'tags' / 'learned' / 'fq_added.json', added)
        self.assertEqual({'new_tag': '2 posts (unknown)', 'skyfall': '1 posts (unknown)'}, _read_list(added))
        self.assertFalse(tags_learned.list_path('fq').exists())
        self.assertEqual((before.st_size, before.st_mtime_ns), (bundled.stat().st_size, bundled.stat().st_mtime_ns))
        self.assertEqual((bundled, added), (TagsDB.get_file('fq'), TagsDB.get_additions('fq')))
        # autocompletion and Tags A-Z: the bundled list plus the learned tags, the bundled counts stay
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_tag': 2, 'skyfall': 1}, self._all())
        rows = server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sky'})
        self.assertEqual([('sky', 5000, 'general'), ('skyline', 40, 'general'), ('skyfall', 1, 'unknown')],
                         [(r['tag'], r['posts'], r['type']) for r in rows])
        self.assertEqual(['skyfall'], [r['tag'] for r in server.m_tags_fuzzy({'module': 'fq', 'text': 'skyfal'})][:1])
        res = server.m_tags_resolve({'module': 'fq', 'tags': ['new_tag', 'tree', 'nope']})
        self.assertEqual([True, True, False], [r['known'] for r in res['items']])
        self.assertEqual({'bundled': True, 'version': '1.0.2', 'added': 2, 'learned': False, 'path': bundled.as_posix()},
                         {k: self._row()[k] for k in ('bundled', 'version', 'added', 'learned', 'path')})
        self.assertEqual('tags.learned', self._learn([('3', ['tree', 'new_tag'])])[-1][0])  # the count of an addition went up
        self.assertEqual({'new_tag': '3 posts (unknown)', 'skyfall': '1 posts (unknown)'}, _read_list(added))
        self.assertEqual(3, self._all()['new_tag'])
        self.assertEqual([], self._learn([('3', ['tree', 'other'])]))  # counted before: nothing written, nothing told

    def test_a_preview_looks_up_only_its_own_tags_and_the_bundled_list_is_not_parsed_again(self) -> None:
        TagsDB.CacheMinSize = 0  # every list has its binary cache (lists over 1 MB, rule34.us 18.9 MB)
        self.addCleanup(setattr, TagsDB, 'CacheMinSize', tagsdb.TAGS_CACHE_MIN_SIZE)
        self._install()
        self._learn([('1', ['sky', 'old_a'])])
        self.assertEqual(1, self._all()['old_a'])
        looked: list[list[str]] = []
        parsed: list[pathlib.Path] = []
        lacking, parse = TagsDB.lacking, tagsdb._parse_tag_list
        with mock.patch.object(TagsDB, 'lacking', lambda m, names: looked.append(list(names)) or lacking(m, names)), \
                mock.patch.object(tagsdb, '_parse_tag_list', lambda path: parsed.append(pathlib.Path(path)) or parse(path)):
            self._learn([('2', ['tree', 'new_b']), ('3', ['new_b'])])
            self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'old_a': 1, 'new_b': 2}, self._all())
        self.assertEqual([['new_b', 'tree']], looked)  # the tags of the posts counted now, not the whole learned set
        self.assertEqual([tags_learned.additions_path('fq')], parsed)  # the additions only (small): the bundled list comes from its cache
        self.assertEqual(tags_learned.additions_path('fq'), TagsDB.get_additions('fq'))

    def test_an_update_of_the_extension_keeps_the_additions_its_new_list_lacks(self) -> None:
        self._install()
        self._learn([('1', ['sky', 'new_a', 'new_b'])])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'new_a': '1 posts (unknown)', 'new_b': '1 posts (unknown)'}, _read_list(added))
        newer = {**self.BUNDLED, 'new_a': (77, 'character')}
        # the new list alone, before the learned file is written again: no duplicate, the bundled count wins
        shutil.rmtree(self.folder)
        shutil.copytree(self._source(newer, '1.0.3'), self.folder)
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 77, 'new_b': 1}, self._all())
        self.assertEqual('character', server.m_tags_resolve({'module': 'fq', 'tags': ['new_a']})['items'][0]['type'])
        # ext.install of the update: the learned file drops what the new list has
        res = self._install(newer, '1.0.4')
        self.assertEqual('1.0.4', res['version'])
        self.assertEqual({'new_b': '1 posts (unknown)'}, _read_list(added))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 77, 'new_b': 1}, self._all())
        self.assertEqual({'version': '1.0.4', 'added': 1}, {k: self._row()[k] for k in ('version', 'added')})
        # a newer list that has every learned tag: the learned file is emptied, not left with stale additions
        self._install({**newer, 'new_b': (9, 'general')}, '1.0.5')
        self.assertEqual({}, _read_list(added))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 77, 'new_b': 9}, self._all())
        self.assertEqual(0, self._row()['added'])

    def test_uninstalling_the_extension_leaves_the_whole_learned_list(self) -> None:
        self._install()
        self._learn([('1', ['sky', 'new_a']), ('2', ['sky'])])
        self.assertEqual({'new_a': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual({'ok': True}, server.m_ext_uninstall({'id': 'fakebooru'}))
        self.assertFalse(self.folder.exists())
        self.assertEqual(tags_learned.list_path('fq'), TagsDB.get_file('fq'))
        self.assertFalse(tags_learned.additions_path('fq').exists())
        self.assertIsNone(TagsDB.bundled('fq'))
        self.assertEqual({'sky': '2 posts (unknown)', 'new_a': '1 posts (unknown)'}, _read_list(tags_learned.list_path('fq')))
        self.assertEqual({'sky': 2, 'new_a': 1}, self._all())
        self.assertEqual({'learned': True, 'bundled': False, 'added': 0},
                         {k: self._row()[k] for k in ('learned', 'bundled', 'added')})
        self.assertTrue(tags_learned.may_learn('fq'))

    def test_a_list_learned_before_the_extension_shipped_one_is_served_on_top(self) -> None:
        # Rule34Hentai before 1.0.2: a list learned from Preview; the update ships the site's list (older file)
        src = self._source(self.BUNDLED)
        old = time.time() - 30 * 86400
        os.utime(src / 'fq_tags.json', (old, old))
        tags_learned.learn('fq', [('1', ['sky', 'mine'])])
        self.assertTrue(tags_learned.is_learned('fq'))
        self._install(src=src)
        self.assertEqual(self.folder / 'fq_tags.json', TagsDB.get_file('fq'))
        # the learned list moved onto the bundled one: only what it lacks, in the additions file; the whole list is gone from the
        # lists' names (written again from the learning's state when the extension is uninstalled)
        self.assertEqual({'mine': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertFalse(tags_learned.list_path('fq').exists())
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'mine': 1}, self._all())

    def test_modules_with_a_source_never_learn_on_top_of_their_list(self) -> None:
        job = {'tagUpdate': {'entry': 'tags.py:read_tags'}, 'minSdkVersion': '1.1'}
        cases = {
            'engine': ({'engine': 'gelbooru_dapi'}, 'fq'),
            'job': ({**job}, 'fq'),
            'reviewed': ({'abbr': 'xb', 'site': 'https://xbooru.com/'}, 'xb'),
        }
        for name, (kw, module) in cases.items():
            with self.subTest(name):
                self._install(**kw)
                self.assertEqual(self.folder / f'{module}_tags.json', TagsDB.get_file(module))
                self.assertIsNone(TagsDB.bundled(module))
                self.assertFalse(tags_learned.may_learn(module))
                self.assertTrue(tags_learned.has_source_list(module))
                self.assertEqual((0, False), tags_learned.learn(module, [('1', ['sky', 'new'])]))
                self.assertFalse(tags_learned.list_path(module).exists())
                self.assertFalse(tags_learned.additions_path(module).exists())
                # the list in use is still the one the extension ships: its version, not the file's time (Tags A-Z)
                self.assertEqual((False, True, '1.0.2', 0), tuple(self._row(module)[k] for k in ('bundled', 'shipped', 'version', 'added')))
                res = server.m_tags_learn({'module': module})
                self.assertEqual((False, True, False), (res['mayLearn'], res['sourceList'], res['bundled']))
                shutil.rmtree(self.root / 'src')

    def test_a_shipped_list_with_a_source_reads_as_shipped_until_it_is_updated_here(self) -> None:
        """tags.status 'shipped': the list of an extension that has its own update is the shipped one until the user updates it;
        the update writes <data dir>/tags/<abbr>_tags.json, newer, and from then on the row gives that file's time"""
        self._install(tagUpdate={'entry': 'tags.py:read_tags'}, minSdkVersion='1.1')
        self.assertEqual({'bundled': False, 'shipped': True, 'version': '1.0.2', 'path': (self.folder / 'fq_tags.json').as_posix()},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'version', 'path')})
        updated = self.data / 'tags' / 'fq_tags.json'
        updated.parent.mkdir(parents=True, exist_ok=True)
        tags_updater.write_tag_list(updated, {'sky': (6000, 'general')})  # what update_tag_list writes
        later = (self.folder / 'fq_tags.json').stat().st_mtime + 60
        os.utime(updated, (later, later))
        TagsDB.locate_default_lists()
        self.assertEqual({'bundled': False, 'shipped': False, 'version': '', 'path': updated.as_posix()},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'version', 'path')})
        for row in server.m_tags_status({}):  # the lists in no extension (the repository's 2tags/) are never 'shipped'
            if row['module'] != 'fq':
                self.assertEqual((False, ''), (row['shipped'], row['version']), row['module'])

    def test_learning_off_learns_nothing_on_top(self) -> None:
        self._install()
        with mock.patch.dict(os.environ, {'LUSTRA_TAGS_LEARN': '0'}):
            self.assertFalse(tags_learned.may_learn('fq'))
            self.assertEqual((0, False), tags_learned.learn('fq', [('1', ['new'])]))
            res = server.m_tags_learn({'module': 'fq'})
            self.assertEqual((False, False), (res['mayLearn'], res['sourceList']))  # the app says the learning is off
            self.assertFalse(tags_learned.refresh('fq'))
        self.assertFalse(tags_learned.list_path('fq').exists())
        self.assertFalse(tags_learned.additions_path('fq').exists())

    def test_learn_from_downloads_on_top(self) -> None:
        # "Học từ file đã tải" (Tags A-Z) on a bundled list
        self._install()
        keys = ('mayLearn', 'sourceList', 'indexed', 'newPosts', 'bundled', 'added', 'written')
        self.assertEqual((True, False, False, 0, True, 0, False), tuple(server.m_tags_learn({'module': 'fq'})[k] for k in keys))
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='fq', post_id='1', path='/x/fq_1.jpg', tags='sky downloaded_tag'),
                            IndexRecord(module='fq', post_id='2', path='/x/fq_2.jpg', tags='tree')])
        # after a download job main forwards it as tags.learned when written (app/main.js learnTagList): 'learned' stays False here
        res = server.m_tags_learn({'module': 'fq'})
        self.assertEqual((True, False, True, 2, True, 1, True, False), tuple(res[k] for k in (*keys, 'learned')))
        self.assertEqual({'downloaded_tag': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual(1, self._all()['downloaded_tag'])
        self.assertEqual((True, False, True, 0, True, 1, False), tuple(server.m_tags_learn({'module': 'fq'})[k] for k in keys))
        with DownloadIndex(create=True) as index:  # downloaded posts whose tags the bundled list has: nothing to tell the app
            index.add_many([IndexRecord(module='fq', post_id='3', path='/x/fq_3.jpg', tags='sky tree')])
        res = server.m_tags_learn({'module': 'fq'})
        self.assertEqual((True, False, True, 1, True, 1, False), tuple(res[k] for k in keys))

    def test_a_preview_whose_tags_the_bundled_list_has_rewrites_nothing(self) -> None:
        # rule34.us: most Preview pages bring only tags its 544k list has; the additions file, the loaded list and the app's chip
        # hints stay as they are
        self._install()
        self._learn([('1', ['sky', 'old_a'])])
        added = tags_learned.additions_path('fq')
        stamp = (added.stat().st_size, added.stat().st_mtime_ns)
        self.assertEqual(1, self._all()['old_a'])
        self.assertTrue(TagsDB.is_loaded('fq'))
        writes: list[pathlib.Path] = []
        write = tags_learned.write_tag_list
        with mock.patch.object(tags_learned, 'write_tag_list', lambda path, tags: writes.append(path) or write(path, tags)), \
                mock.patch.object(TagsDB, 'unload', wraps=TagsDB.unload) as unload:
            self.assertEqual([], self._learn([('2', ['sky', 'tree']), ('3', ['Skyline'])]))
            self.assertEqual((2, False), tags_learned.learn('fq', [('4', ['tree']), ('5', ['sky'])]))
            unload.assert_not_called()
        self.assertEqual([], writes)
        self.assertEqual(stamp, (added.stat().st_size, added.stat().st_mtime_ns))
        self.assertTrue(TagsDB.is_loaded('fq'))
        self.assertEqual(5, tags_learned.status('fq')['posts'])  # still counted (an uninstall writes the whole learned list)
        notes = self._learn([('6', ['sky', 'old_a'])])  # an addition's count went up: written, the app is told
        self.assertEqual(['tags.learned'], [m for m, _ in notes])
        self.assertEqual({'old_a': '2 posts (unknown)'}, _read_list(added))
        self.assertEqual({'ok': True}, server.m_ext_uninstall({'id': 'fakebooru'}))
        self.assertEqual({'sky': 4, 'tree': 2, 'skyline': 1, 'old_a': 2}, self._all())

    def test_a_lost_stamp_never_hides_the_bundled_list(self) -> None:
        # the learning's stamp (its sqlite state) can be lost: a quit while it wrote, a profile copied without its file times, the
        # state removed. Neither the additions nor a whole learned list from before the bundled one then takes the module's list
        later = time.time() + 60
        tags_learned.learn('fq', [('0', ['sky', 'old'])])  # learned before the extension shipped its list
        whole = tags_learned.list_path('fq')
        kept = whole.read_bytes()
        self._install()
        self._learn([('1', ['sky', 'new_a'])])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'old': '1 posts (unknown)', 'new_a': '1 posts (unknown)'}, _read_list(added))
        whole.write_bytes(kept)  # left behind (a quit before it was removed), its stamp lost
        for f in (whole, added):
            os.utime(f, (later, later))
        shutil.rmtree(self.data / 'tags' / 'learned' / 'fq.sqlite', ignore_errors=True)
        (self.data / 'tags' / 'learned' / 'fq.sqlite').unlink(missing_ok=True)
        TagsDB.clear()
        TagsDB.locate_default_lists()  # a restart
        bundled = self.folder / 'fq_tags.json'
        self.assertEqual((bundled, added), (TagsDB.get_file('fq'), TagsDB.get_additions('fq')))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'old': 1, 'new_a': 1}, self._all())
        self.assertEqual(['sky'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sky'})][:1])
        self.assertEqual({'bundled': True, 'path': bundled.as_posix()}, {k: self._row()[k] for k in ('bundled', 'path')})
        self.assertTrue(tags_learned.may_learn('fq'))  # the learning goes on on top of it
        self.assertFalse(tags_learned.has_source_list('fq'))
        self.assertEqual('tags.learned', self._learn([('2', ['tree', 'new_b'])])[-1][0])
        self.assertEqual({'new_b': '1 posts (unknown)'}, _read_list(added))  # learned again from here (its state was lost)
        self.assertEqual(bundled, TagsDB.get_file('fq'))
        self.assertEqual(kept, whole.read_bytes())  # not the learning's any more: never removed or overwritten

    def test_a_write_cut_short_is_done_by_the_next_learn(self) -> None:
        # the counts are committed before the additions file is written: a quit in between (learning runs on a daemon thread) is
        # caught up by the next Preview, even one that brings nothing new
        self._install()
        self._learn([('1', ['sky', 'new_a'])])
        write = tags_learned.write_tag_list

        def quit_while_writing(path, tags):
            raise SystemExit('quit')

        with mock.patch.object(tags_learned, 'write_tag_list', quit_while_writing), self.assertRaises(SystemExit):
            tags_learned.learn('fq', [('2', ['new_a', 'new_b'])])
        self.assertEqual({'new_a': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        TagsDB.clear()
        TagsDB.locate_default_lists()  # a restart
        self.assertEqual(TagsDB.get_file('fq'), self.folder / 'fq_tags.json')
        with mock.patch.object(tags_learned, 'write_tag_list', wraps=write) as written:
            self.assertEqual('tags.learned', self._learn([('3', ['sky'])])[-1][0])
            self.assertEqual(1, written.call_count)
            self.assertEqual([], self._learn([('4', ['tree'])]))  # done: nothing written again
            self.assertEqual(1, written.call_count)
        self.assertEqual({'new_a': '2 posts (unknown)', 'new_b': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 2, 'new_b': 1}, self._all())

    def test_with_additions_merges_bytes_in_order(self) -> None:
        base = tagsdb._TagList.from_mapping({'b': (TagTypes.TAG_TYPE_GENERAL, 5), 'd': (TagTypes.TAG_TYPE_ARTIST, 7),
                                             'é': (TagTypes.TAG_TYPE_GENERAL, 2)})
        extra = tagsdb._TagList.from_mapping({'a': (TagTypes.TAG_TYPE_UNKNOWN, 1), 'b': (TagTypes.TAG_TYPE_UNKNOWN, 1),
                                              'c': (TagTypes.TAG_TYPE_UNKNOWN, 3), 'zz': (TagTypes.TAG_TYPE_UNKNOWN, 1),
                                              '日本': (TagTypes.TAG_TYPE_UNKNOWN, 4)})
        merged = tagsdb._with_additions(base, extra)
        self.assertEqual([('a', 1), ('b', 5), ('c', 3), ('d', 7), ('zz', 1), ('é', 2), ('日本', 4)],
                         [(merged.name(i), merged.info(i).posts_count) for i in range(len(merged))])
        self.assertEqual(TagTypes.TAG_TYPE_ARTIST, merged.info(merged.find('d')).tag_type)
        self.assertEqual({'a', 'c', 'zz', '日本'}, merged.added)
        self.assertEqual(range(1, 2), merged.prefix_range('b'))
        self.assertEqual([0, 1, 2, 3, 4, 5, 6], [merged.find(n) for n in ('a', 'b', 'c', 'd', 'zz', 'é', '日本')])
        self.assertIs(base, tagsdb._with_additions(base, tagsdb._TagList.from_mapping({'b': (TagTypes.TAG_TYPE_UNKNOWN, 1)})))
        self.assertIs(base, tagsdb._with_additions(base, tagsdb._EMPTY_LIST))


class RunningUpdateTests(_DataDirTest):
    """Tags A-Z shows a running update whoever started it (its button, the app's auto update, a new extension), also after a reload
    of the window: tags.sources says which modules run, with the count of tags read so far"""

    def test_a_running_update_is_listed_until_it_ends(self) -> None:
        shutil.copytree(_draft(self.root), self.data / 'extensions' / 'fakebooru')  # installed: fq has a tag source
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        notes: list[tuple[str, dict]] = []
        running_at_done: list[dict] = []

        def slow_update(module: str, out_dir: pathlib.Path, *, progress=None, **_kw):
            progress(f'[{module}] 1234 tags...', 1234)
            started.set()
            release.wait(WAIT)
            res = tags_updater.UpdateResult(module, out_dir / f'{module}_tags.json')
            res.message = 'not written: no tags fetched'
            return res

        def notify(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'tags.updateDone':
                running_at_done.append(server._running_tag_updates())
                done.set()

        def row() -> dict:
            return next(r for r in server.m_tags_sources({}) if r['module'] == 'fq')

        self.assertEqual((False, 0, ''), (row()['running'], row()['count'], row()['message']))
        out = self.root / 'out'
        with mock.patch.object(tags_updater, 'update_tag_list', slow_update), mock.patch.object(server, 'notify', notify):
            self.assertEqual('tags-fq', server.m_tags_update({'module': 'fq', 'outDir': str(out)})['job'])
            self.assertTrue(started.wait(WAIT))
            r = row()
            self.assertEqual((True, 1234, '[fq] 1234 tags...'), (r['running'], r['count'], r['message']))
            self.assertLessEqual(abs(time.time() - r['startedAt']), WAIT)
            with self.assertRaises(server.RpcError) as cm:  # one update of a module at a time
                server.m_tags_update({'module': 'fq', 'outDir': str(out)})
            self.assertEqual(-32004, cm.exception.code)
            release.set()
            self.assertTrue(done.wait(WAIT), notes)
        self.assertEqual([{}], running_at_done)  # gone before the app is told: its next tags.sources has it stopped
        self.assertEqual((False, 0, ''), (row()['running'], row()['count'], row()['message']))
        self.assertEqual(['tags.updateProgress', 'tags.updateDone'], [m for m, _ in notes])
        self.assertEqual({'job': 'tags-fq', 'module': 'fq', 'count': 1234, 'message': '[fq] 1234 tags...'}, notes[0][1])
        self.assertEqual(('fq', False), (notes[1][1]['module'], notes[1][1]['written']))


FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'


class E621ExportTests(_DataDirTest):
    """e621 lists its daily database exports on db_exports (db_export/ redirects there): the tag export is found there, its SHA-256
    checked, then read. A fake e621 serves a trimmed copy of the real page (lustra/tests/fixtures/e621_db_exports.html)"""
    LISTED = '702bfb79c59542891a0b6ec3a345ac90864e9edd978cc6f1136c3841fca728b7'
    '''the checksum of tags.csv.gz in the saved page (not the one of the small export here)'''

    def setUp(self) -> None:
        super().setUp()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['id', 'name', 'category', 'post_count'])
        w.writerows([i, name, c, n] for i, name, n, c in TAGS)
        self.export = gzip.compress(buf.getvalue().encode())
        self.page = (FIXTURES / 'e621_db_exports.html').read_text(encoding='utf-8')
        self.out = self.root / 'out'

    def _update(self, page: str) -> tuple[tags_updater.UpdateResult, FakeSession]:
        def route(url: str, _headers: dict) -> tuple[int, bytes, dict]:
            where = url_parse.urlsplit(url)
            where = (where.hostname, where.path)
            if where == ('e621.net', '/robots.txt'):
                return 200, b'User-agent: *\nDisallow: /posts.json\n', {'content-type': 'text/plain'}
            if where == ('e621.net', '/db_exports'):
                return 200, page.encode(), {'content-type': 'text/html; charset=utf-8'}
            if where == ('e621.net', '/db_export/'):
                return 301, b'', {'location': 'https://e621.net/db_exports'}
            if where == ('static1.e621.net', '/data/db_export/tags.csv.gz'):
                return 200, self.export, {'content-type': 'application/octet-stream'}
            return 404, b'<html>not found</html>', {'content-type': 'text/html'}

        session = FakeSession(route)
        return update_tag_list('en', self.out, delay=0, allow_local=True, session=session), session

    def test_the_tag_export_listed_on_db_exports_is_checked_and_read(self) -> None:
        self.assertIs(CURATED['en'], tag_source('en'))
        self.assertIn('db_exports', CURATED['en'].description)
        res, session = self._update(self.page.replace(self.LISTED, hashlib.sha256(self.export).hexdigest()))
        self.assertTrue(res.written, res.message)
        got = _read_list(res.path)
        self.assertEqual(EXPECTED, {k: int(v.split()[0]) for k, v in got.items()})
        self.assertEqual('40 posts (copyright)', got['tom_&_jerry'])
        self.assertEqual('300 posts (character)', got['hatsune_miku'])
        self.assertEqual(['https://e621.net/robots.txt', 'https://e621.net/db_exports',
                          'https://static1.e621.net/data/db_export/tags.csv.gz'], [u for u, _ in session.calls])

    def test_a_wrong_checksum_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._update(self.page)  # the page's checksum is the one of the real 33.7 MB export
        self.assertIn('SHA-256', str(cm.exception))
        self.assertFalse((self.out / 'en_tags.json').exists())

    def test_the_link_and_its_checksum_are_read_from_the_same_row(self) -> None:
        link = tags_updater._e621_export_link
        base = 'https://e621.net/db_exports'
        self.assertEqual(('https://static1.e621.net/data/db_export/tags.csv.gz', self.LISTED), link(self.page, base))
        # a dated name, a relative link; a row without its checksum, or with two links, gives none
        dated = self.page.replace('db_export/tags.csv.gz', 'db_export/tags-2026-11-19.csv.gz')
        self.assertEqual(('https://static1.e621.net/data/db_export/tags-2026-11-19.csv.gz', self.LISTED), link(dated, base))
        rel = self.page.replace('https://static1.e621.net/data/db_export/tags.csv.gz', '/data/tags.csv.gz')
        self.assertEqual(('https://e621.net/data/tags.csv.gz', self.LISTED), link(rel, base))
        self.assertEqual(('https://static1.e621.net/data/db_export/tags.csv.gz', ''), link(self.page.replace(self.LISTED, 'x'), base))
        flat = self.page.replace('<div class="row">', '<div class="cells">')
        self.assertEqual(('https://static1.e621.net/data/db_export/tags.csv.gz', ''), link(flat, base))
        self.assertEqual(('', ''), link(self.page.replace('tags.csv.gz', 'tags.json'), base))

    def test_without_a_link_the_dated_exports_of_before_are_tried(self) -> None:
        session = FakeSession(lambda url, _h: (404, b'<html>not found</html>', {'content-type': 'text/html'}))
        with self.assertRaises(RuntimeError) as cm:  # no page, and the dated files are gone (the error the owner saw, now clearer)
            update_tag_list('en', self.out, delay=0, allow_local=True, session=session)
        self.assertEqual('no recent tag export found on e621.net (db_exports lists no tags.csv.gz)', str(cm.exception))
        today = datetime.date.today()
        self.assertEqual(['https://e621.net/robots.txt', 'https://e621.net/db_exports',
                          *(f'https://e621.net/db_export/tags-{today - datetime.timedelta(days=d)}.csv.gz' for d in range(4))],
                         [u for u, _ in session.calls])


JOB_FILE = '''# coding=UTF-8
import re
from html import unescape


def read_tags(job):
    \"\"\"the whole list in one answer (Shimmie's find_tags), plus what the checks of the core must clean up\"\"\"
    text = job.get_text(f'{job.site}api/danbooru/find_tags', accept='text/xml')
    job.log(f'{len(text)} bytes')
    for count, name in re.findall(r"counts='(\\d+)' name='([^']*)'", text):
        yield unescape(name), int(count), 'general'
    yield 'two  words', 5, 'Character'
    yield '', 3
    yield 'bad_count', '7'
'''


class ExtensionJobTests(_DataDirTest):
    """an extension ships its list and its own update (manifest tagUpdate): run only when asked, its requests through the core"""

    def _install(self, ext_id: str = 'jobsite', abbr: str = 'jq', job: str = JOB_FILE, engine: str = 'custom',
                 where: pathlib.Path | None = None) -> pathlib.Path:
        m = make_manifest(ext_id=ext_id, abbr=abbr, name=ext_id.title(), site=SITE, engine=engine, media=['image'])
        m.update({'tagList': f'{abbr}_tags.json', 'tagUpdate': {'entry': 'tags.py:read_tags'}, 'minSdkVersion': '1.1'})
        folder = (where or self.data / 'extensions') / ext_id
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        (folder / 'downloader.py').write_text('# test\n', encoding='utf-8')
        (folder / 'tags.py').write_text(job, encoding='utf-8')
        return folder

    def _update(self, module: str, site=None, **kw):
        session = FakeSession(site or FakeTagSite())
        notes: list[str] = []
        res = update_tag_list(module, self.data / 'tags', session=session, allow_local=True, min_interval=0.0,
                              progress=lambda msg, _n: notes.append(msg), **kw)
        return res, session, notes

    def test_the_extensions_own_update_is_its_source_and_runs_only_when_asked(self) -> None:
        self._install()
        _install_engine = make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image'])
        (self.data / 'extensions' / 'fakebooru').mkdir(parents=True)
        (self.data / 'extensions' / 'fakebooru' / 'manifest.json').write_text(json.dumps(_install_engine), encoding='utf-8')
        src = tag_source('jq')
        self.assertEqual((tags_updater.EXTENSION_JOB, 'tags.py:read_tags', True, True), (src.engine, src.job, src.on_demand, src.robots))
        self.assertTrue(src.as_dict()['onDemand'])
        self.assertFalse(tag_source('fq').on_demand)
        self.assertIn('jq', [s.module for s in tag_sources()])
        # the app's system schedule ('auto') leaves it out; naming the module runs it
        auto = tags_updater._modules_arg('auto')
        self.assertIn('fq', auto)
        self.assertNotIn('jq', auto)
        self.assertEqual([*auto, 'jq'], tags_updater._modules_arg('auto,jq'))
        res, session, notes = self._update('jq')
        self.assertTrue(res.written, res.message)
        self.assertEqual([f'{SITE}robots.txt', f'{SITE}api/danbooru/find_tags'], [c[0] for c in session.calls])
        # job.log and the skipped rows: one line each in the progress, the module in front once
        self.assertEqual(1, sum(1 for n in notes if n.startswith('[jq] ') and n.endswith(' bytes')), notes)
        self.assertEqual(1, sum(1 for n in notes if n.startswith('[jq] 2 of ') and 'skipped' in n), notes)
        self.assertFalse([n for n in notes if '[jq] [jq]' in n], notes)
        got = _read_list(self.data / 'tags' / 'jq_tags.json')
        self.assertEqual({**EXPECTED, 'two_words': 5}, {k: int(v.split()[0]) for k, v in got.items()})
        self.assertEqual('5 posts (character)', got['two_words'])

    def test_the_job_reaches_only_its_site_and_follows_robots_txt(self) -> None:
        self._install(job="def read_tags(job):\n    job.get_text('https://other.test/tags')\n    yield 'x', 1\n")
        with self.assertRaises(TagSourceError) as ctx:
            self._update('jq')
        self.assertIn('not the site of this tag list', str(ctx.exception))
        shutil.rmtree(self.data / 'extensions' / 'jobsite')
        self._install()
        with self.assertRaises(TagSourceError) as ctx:
            self._update('jq', site=FakeTagSite(robots='User-agent: *\nDisallow: /api/\n'))
        self.assertEqual('robots', ctx.exception.kind)
        self.assertFalse((self.data / 'tags' / 'jq_tags.json').exists())

    def test_a_job_that_gives_nothing_writes_nothing(self) -> None:
        self._install(job="def read_tags(job):\n    yield ('only', 'bad')\n")
        with self.assertRaises(RuntimeError) as ctx:
            self._update('jq')
        self.assertIn('gave no tag', str(ctx.exception))
        self.assertFalse((self.data / 'tags' / 'jq_tags.json').exists())

    def test_the_manifest_entry_is_checked(self) -> None:
        base = make_manifest(ext_id='jobsite', abbr='jq', name='Jobsite', site=SITE, engine='custom', media=['image'])
        ExtensionManifest.from_dict({**base, 'tagUpdate': {'entry': 'tags.py:read_tags'}}, None, 'user')
        for bad in ({'entry': 'sub/tags.py:read_tags'}, {'entry': '../tags.py:read_tags'}, {'entry': 'tags:read_tags'},
                    {'entry': 'tags.py:read_tags', 'every': 7}, {'entry': 5}, 'tags.py:read_tags'):
            with self.assertRaises(Exception, msg=str(bad)):
                ExtensionManifest.from_dict({**base, 'tagUpdate': bad}, None, 'user')

    def test_the_generator_never_runs_a_drafts_job_but_checks_and_packs_it(self) -> None:
        from lustra.extgen.sandbox import _policy_checks
        marker = self.root / 'ran'
        draft = self._install(ext_id='fakebooru', abbr='fq', where=self.root / 'drafts',
                              job=f"import requests\n\n\ndef read_tags(job):\n    open({str(marker)!r}, 'w').close()\n    yield 'x', 1\n")
        res = bundle_tag_list(draft, session=FakeSession(FakeTagSite()), allow_local=True, min_interval=0.0)
        self.assertEqual('later', res['status'], res)
        self.assertIn('only when you ask', res['message'])
        self.assertFalse(marker.exists())
        (draft / 'fq_tags.json').write_text('{\n"scenery": "900 posts (general)",\n"sky": "700 posts (general)"\n}\n', encoding='utf-8')
        res = bundle_tag_list(draft, session=FakeSession(FakeTagSite()), allow_local=True, min_interval=0.0)
        self.assertEqual(('bundled', 2), (res['status'], res['count']), res)
        m = ExtensionManifest.load(draft / 'manifest.json', 'user')
        bad = [c for c in _policy_checks(draft, m) if not c['ok']]
        self.assertTrue(any(c['detail'].startswith('tags.py line 1') for c in bad), bad)
        with zipfile.ZipFile(pack(draft, self.root / 'fakebooru.zip')['path']) as zf:
            self.assertTrue({'fakebooru/tags.py', 'fakebooru/fq_tags.json'} <= set(zf.namelist()))
        self.assertFalse(marker.exists())

#
#
#########################################
