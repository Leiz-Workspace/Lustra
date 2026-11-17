# coding=UTF-8
"""
Lustra: tests of the extension generator (lustra.extgen) and the AI session (lustra.aigen), offline.
A fake gelbooru site is served through SafeFetcher's real policy code (robots, hosts, budget, redirects, challenges) with a
fake transport; the model is a scripted client. Nothing is installed: drafts stay in a temporary folder.
Run: python -m unittest lustra.tests.test_extgen
"""
#########################################
#
#

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest import TestCase
from urllib import parse as url_parse

from requests.structures import CaseInsensitiveDict

from lustra.logger import Logger

Logger.init(True, True)

from lustra.aigen.session import FALLBACK_BETA, GenRequest, GenSession, list_sessions  # noqa: E402
from lustra.aigen.template_agent import pick_tag  # noqa: E402
from lustra.aigen.tools import TOOLS, ToolBox  # noqa: E402
from lustra.extensions import data_dir  # noqa: E402
from lustra.extgen.engines import detect_engine  # noqa: E402
from lustra.extgen.fetch import FetchPolicyError, SafeFetcher, normalize_site  # noqa: E402
from lustra.extgen.policy import check_source, errors  # noqa: E402
from lustra.extgen.sandbox import check_draft, pack, run_sandbox  # noqa: E402
from lustra.extgen.scaffold import create_draft, survey  # noqa: E402
from lustra.extgen.templates import render_downloader  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOST = 'fakebooru.test'
SITE = f'https://{HOST}/'
POSTS = [(1000 + i, f'scenery {"sky" if i % 2 else "tree"} original tag_{i}') for i in range(6)]


def _post_xml(pid: int, tags: str) -> str:
    h = f'{pid:032x}'
    return (f'<post height="800" score="1" file_url="https://img.{HOST}/images/{pid}/{h}.png" parent_id="" '
            f'sample_url="https://{HOST}/samples/{pid}/sample_{h}.jpg" sample_width="800" sample_height="800" '
            f'preview_url="https://{HOST}/thumbnails/{pid}/thumbnail_{h}.jpg" rating="g" tags=" {tags} " id="{pid}" width="800" '
            f'change="1700000000" md5="{h}" creator_id="1" has_children="false" created_at="Thu Aug 21 22:55:00 +0200 2025" '
            f'status="active" source="" has_notes="false" has_comments="false" preview_width="150" preview_height="150"/>')


def fake_gelbooru(url: str, robots: str = '') -> tuple[int, bytes, dict]:
    p = url_parse.urlparse(url)
    if p.path == '/robots.txt':
        return (200, robots.encode(), {'content-type': 'text/plain'}) if robots else (404, b'not found', {'content-type': 'text/html'})
    if p.path == '/index.php':
        qs = url_parse.parse_qs(p.query)
        if qs.get('page') == ['dapi'] and qs.get('q') == ['index']:
            want = [t for t in qs.get('tags', [''])[0].replace('+', ' ').split() if ':' not in t and not t.startswith('-')]
            posts = [x for x in sorted(POSTS, reverse=True) if all(t in x[1].split() for t in want)]
            limit, page = int(qs.get('limit', ['100'])[0]), int(qs.get('pid', ['0'])[0])
            body = ''.join(_post_xml(*x) for x in posts[page * limit:(page + 1) * limit])
            xml = f'<?xml version="1.0" encoding="UTF-8"?><posts count="{len(posts)}" offset="{page * limit}">{body}</posts>'
            return 200, xml.encode(), {'content-type': 'text/xml;charset=UTF-8'}
    return 404, b'<html>not found</html>', {'content-type': 'text/html'}


class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes, headers: dict) -> None:
        self.url, self.status_code, self._body = url, status, body
        self.headers = CaseInsensitiveDict(headers)
        self.is_redirect = status in (301, 302, 303, 307, 308) and 'location' in self.headers
        self.is_permanent_redirect = status in (301, 308) and 'location' in self.headers

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
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers=None, **_):
        self.calls.append((method, url, dict(headers or {})))
        return FakeResponse(url, *self.route(url))


def fake_fetcher(route=fake_gelbooru, **kw) -> SafeFetcher:
    """real SafeFetcher rules; allow_local only skips the DNS lookup of the fake host"""
    f = SafeFetcher(allowed_hosts={HOST}, min_interval=0.0, allow_local=True, **kw)
    f._session = FakeSession(route)
    return f


# ----- scripted model

def _usage(inp: int = 1000, out: int = 200) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tool(name: str, args: dict | None = None, *, usage=None, stop: str = 'tool_use') -> SimpleNamespace:
    tool.n += 1
    block = SimpleNamespace(type='tool_use', id=f'toolu_{tool.n:03d}', name=name, input=args if args is not None else {})
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=f'calling {name}'), block], stop_reason=stop,
                           usage=usage or _usage())


tool.n = 0


class _Stream:
    def __init__(self, message) -> None:
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def __iter__(self):
        if isinstance(self.message, Exception):
            raise self.message
        for b in self.message.content:
            yield SimpleNamespace(type='content_block_start', content_block=b)

    def get_final_message(self):
        return self.message


class ScriptedClient:
    def __init__(self, steps: list) -> None:
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.requests.append({**params, 'messages': list(params['messages'])})
        step = self.steps.pop(0)
        return _Stream(step(params) if callable(step) else step)


def _tool_results(params: dict) -> list[dict]:
    last = params['messages'][-1]['content']
    return last if isinstance(last, list) else []


# ----- tests

class FetchPolicyTests(TestCase):
    def test_site_normalization_https_only(self) -> None:
        self.assertEqual(f'https://{HOST}/', normalize_site(f'{HOST}/index.php?page=post'))
        with self.assertRaises(FetchPolicyError) as ctx:
            normalize_site(f'http://{HOST}/')
        self.assertEqual('R10', ctx.exception.rule)

    def test_robots_disallow_and_hosts(self) -> None:
        f = fake_fetcher(lambda u: fake_gelbooru(u, robots='User-agent: *\nDisallow: /index.php\n'))
        with self.assertRaises(FetchPolicyError) as ctx:
            f.fetch(f'{SITE}index.php?page=dapi&s=post&q=index')
        self.assertEqual('R2', ctx.exception.rule)
        with self.assertRaises(FetchPolicyError) as ctx:
            f.fetch('https://other.example/x')
        self.assertEqual('R10', ctx.exception.rule)
        with self.assertRaises(FetchPolicyError) as ctx:
            f.fetch(f'https://user:pw@{HOST}/x')
        self.assertEqual('R1', ctx.exception.rule)
        g = SafeFetcher(allowed_hosts={HOST}, min_interval=0.0)
        g._session = FakeSession(fake_gelbooru)
        with self.assertRaises(FetchPolicyError) as ctx:  # no allow_local: plain http refused
            g.fetch(f'http://{HOST}/')
        self.assertEqual('R10', ctx.exception.rule)

    def test_budget_redirect_challenge_and_no_cookies(self) -> None:
        f = fake_fetcher(budget=3)
        f.fetch(f'{SITE}a')  # robots.txt + a
        f.fetch(f'{SITE}b')
        with self.assertRaises(FetchPolicyError) as ctx:
            f.fetch(f'{SITE}c')
        self.assertEqual('R4', ctx.exception.rule)

        def redirect(u: str):
            if u.endswith('/go'):
                return 302, b'', {'location': 'https://evil.example/steal'}
            return fake_gelbooru(u)
        f = fake_fetcher(redirect)
        with self.assertRaises(FetchPolicyError) as ctx:
            f.fetch(f'{SITE}go')
        self.assertEqual('R10', ctx.exception.rule)
        self.assertFalse(any('evil.example' in c[1] for c in f._session.calls))

        def challenge(u: str):
            return (403, b'<html>Just a moment</html>', {'cf-mitigated': 'challenge', 'set-cookie': 'cf=1'}) if 'posts' in u \
                else fake_gelbooru(u)
        f = fake_fetcher(challenge)
        with self.assertRaises(FetchPolicyError) as ctx:
            f.fetch(f'{SITE}posts')
        self.assertEqual('R5', ctx.exception.rule)
        self.assertEqual(0, len(f._session.cookies)) if hasattr(f._session, 'cookies') else None

    def test_names_avoid_known_extensions(self) -> None:
        from lustra.extgen.templates import existing_extensions, suggest_names, validate_names
        names = suggest_names('https://xbooru.com/')  # the catalog has an 'xbooru' extension (abbr xb)
        self.assertNotEqual('xbooru', names['id'])
        self.assertNotEqual('xb', names['abbr'])
        self.assertIn('xbooru', [e['id'] for e in existing_extensions('https://xbooru.com/')])
        self.assertEqual([], existing_extensions(SITE))
        with self.assertRaises(ValueError):
            validate_names('xbooru', 'zq6', 'Xbooru')

    def test_survey_detects_gelbooru(self) -> None:
        f = fake_fetcher()
        guess = detect_engine(f, SITE)
        self.assertEqual(('gelbooru_dapi', 'high'), (guess.engine, guess.confidence))
        sv = survey(SITE, f)
        self.assertEqual('none (404)', sv['robots']['status'])
        self.assertEqual('fakebooru', sv['names']['id'])
        self.assertEqual([], sv['existing'])
        self.assertTrue(sv['names']['abbr'])


class PolicyTests(TestCase):
    def test_templates_pass(self) -> None:
        for engine in ('gelbooru_dapi', 'danbooru_json'):
            src = render_downloader(engine, ext_id='example', abbr='zq9', site='https://example.com/')
            self.assertEqual([], errors(check_source(src, allowed_hosts={'example.com'})), engine)

    def test_bad_code_is_rejected(self) -> None:
        head = 'from lustra.download import Downloader\n\n\nclass DownloaderZq9(Downloader):\n'
        bad = {
            'import os': 'import os\n',
            'socket': 'import socket\n',
            'eval': 'x = eval("1")\n',
            'open': 'open("/tmp/x", "w")\n',
            'dunder import': '__import__("os")\n',
            'computed getattr': 'def f(o, n):\n    return getattr(o, n)\n',
            'network internals': 'from lustra.network import ThreadedHtmlWorker\n',
            'override fetch_html': head + '    def fetch_html(self, *a, **k):\n        return None\n',
            'override download': head + '    def _download(self, *a):\n        return None\n',
            'fast rate': head + '    def _get_min_request_interval(self) -> float:\n        return 0.1\n',
            'dunder attr': 'x = (1).__class__.__subclasses__()\n',
        }
        for name, src in bad.items():
            self.assertTrue(errors(check_source(src)), f'not rejected: {name}')


class SandboxTests(TestCase):
    def test_sandbox_blocks_network_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sv = survey(SITE, fake_fetcher())
            draft = create_draft(pathlib.Path(tmp), sv, ext_id='evilsite', abbr='zq8', engine='custom')
            marker = pathlib.Path(tmp) / 'PWNED'
            (draft / 'downloader.py').write_text(
                'import socket, subprocess\n'
                f'try:\n    open({str(marker)!r}, "w").write("x")\nexcept Exception as e:\n    print("write blocked", e)\n'
                'try:\n    subprocess.run(["true"])\nexcept Exception as e:\n    print("subprocess blocked", e)\n'
                'socket.getaddrinfo("example.com", 443)\n', encoding='utf-8')
            report = run_sandbox(draft, query=['scenery'], limit=1, fetcher=fake_fetcher(), timeout=60)
            load = next(c for c in report['checks'] if c['id'] == 'load')
            self.assertFalse(load['ok'])
            self.assertFalse(marker.exists())
            self.assertIn('blocked', report.get('stderr', '') + json.dumps(report.get('log', [])) + load['detail'])


class SessionTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _installed_ids(self) -> set[str]:
        ext = data_dir() / 'extensions'
        return {p.name for p in ext.iterdir()} if ext.is_dir() else set()

    def test_template_mode_creates_a_checked_draft_and_installs_nothing(self) -> None:
        before = self._installed_ids()
        events = []
        s = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=self.root, fetcher=fake_fetcher(),
                       emit=lambda t, d: events.append((t, d)))
        report = s.run()
        self.assertEqual('ready', report['status'], report)
        self.assertEqual('fakebooru', report['extId'])
        draft = pathlib.Path(report['draftDir'])
        for f in ('manifest.json', 'downloader.py', 'specs.md', 'tests/expected.json', 'tests/fixtures/index.json'):
            self.assertTrue((draft / f).is_file(), f)
        self.assertEqual('scenery', json.loads((draft / 'tests/expected.json').read_text())['query'][0])
        self.assertTrue(all(c['ok'] for c in report['checks'] if c['severity'] == 'error'))
        self.assertEqual(['start', 'done'], [events[0][0], events[-1][0]])
        self.assertTrue((self.root / report['session'] / 'report.json').is_file())
        self.assertEqual(report['session'], list_sessions(self.root)[0]['session'])
        self.assertEqual(before, self._installed_ids())  # a draft, never an installed extension
        z1, z2 = pack(draft, self.root / 'a.zip'), pack(draft, self.root / 'b.zip')
        self.assertEqual(z1['sha256'], z2['sha256'])  # deterministic archive
        self.assertTrue(check_draft(draft)['ok'])

    def test_template_mode_refuses_other_engines(self) -> None:
        def no_api(u: str):
            if url_parse.urlparse(u).path == '/robots.txt':
                return 404, b'', {}
            return 200, b'<html><body>my gallery</body></html>', {'content-type': 'text/html'}
        report = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=self.root, fetcher=fake_fetcher(no_api)).run()
        self.assertEqual('failed', report['status'])
        self.assertIn('AI', report['summary'])

    def test_ai_session_writes_fixes_tests_and_finishes(self) -> None:
        good = render_downloader('gelbooru_dapi', ext_id='fakeai', abbr='zq9', site=SITE)
        client = ScriptedClient([
            tool('survey_site'),
            tool('create_draft', {'engine': 'custom', 'id': 'fakeai', 'abbr': 'zq9', 'name': 'Fake AI', 'media': ['image'],
                                  'description': 'test site'}),
            tool('write_file', {'path': 'downloader.py', 'content': 'import os\n' + good}),
            tool('static_check'),
            tool('write_file', {'path': '../escape.py', 'content': 'x = 1\n'}),
            tool('write_file', {'path': 'downloader.py', 'content': good}),
            tool('test_run', {'query': 'scenery', 'limit': 2}),
            tool('finish', {'status': 'ready', 'summary': 'Xong.', 'rule_ids': []}),
        ])
        report = GenSession(GenRequest(url=SITE), drafts_root=self.root, client=client, fetcher=fake_fetcher()).run()
        self.assertEqual('ready', report['status'], report)
        self.assertEqual(('fakeai', 'zq9'), (report['extId'], report['abbr']))
        reqs = client.requests
        self.assertEqual(8, len(reqs))
        first = reqs[0]
        self.assertEqual('claude-opus-5-5', first['model'])
        self.assertEqual([FALLBACK_BETA], first['betas'])
        self.assertEqual('default', first['fallbacks'])
        self.assertEqual({'effort': 'high'}, first['output_config'])
        self.assertNotIn('thinking', first)
        self.assertNotIn('tool_choice', first)
        self.assertEqual({'type': 'ephemeral'}, first['system'][0]['cache_control'])
        for t in first['tools']:
            self.assertEqual(t['name'] == 'write_file', bool(t.get('eager_input_streaming')), t['name'])
            self.assertEqual(t['name'] != 'write_file', bool(t.get('strict')), t['name'])
        static = json.loads(_tool_results(reqs[4])[0]['content'])
        self.assertFalse(static['ok'])  # import os was caught
        escape = _tool_results(reqs[5])[0]
        self.assertTrue(escape['is_error'])
        self.assertFalse((self.root / report['session'] / 'escape.py').exists())
        # history is append-only: every earlier assistant turn is sent back unchanged
        for i in range(1, len(reqs)):
            self.assertEqual(reqs[i - 1]['messages'], reqs[i]['messages'][:len(reqs[i - 1]['messages'])])
        self.assertGreater(report['usd'], 0)

    def test_refusal(self) -> None:
        refusal = SimpleNamespace(content=[], stop_reason='refusal', stop_details=SimpleNamespace(category='cyber'), usage=_usage())
        report = GenSession(GenRequest(url=SITE), drafts_root=self.root, client=ScriptedClient([refusal]), fetcher=fake_fetcher()).run()
        self.assertEqual(('refused', ['model-refusal']), (report['status'], report['ruleIds']))

    def test_cost_budget_stops_the_session(self) -> None:
        client = ScriptedClient([tool('survey_site', usage=_usage(inp=600_000)), tool('finish', {'status': 'ready', 'summary': '',
                                                                                                  'rule_ids': []})])
        report = GenSession(GenRequest(url=SITE, max_usd=2.0), drafts_root=self.root, client=client, fetcher=fake_fetcher()).run()
        self.assertEqual(('failed', ['budget']), (report['status'], report['ruleIds']))
        self.assertEqual(1, len(client.requests))

    def test_ready_without_a_test_run_fails_the_independent_check(self) -> None:
        client = ScriptedClient([tool('survey_site'), tool('create_draft', {'engine': 'gelbooru_dapi', 'id': 'fakeai', 'abbr': 'zq9',
                                                                           'name': 'Fake AI', 'media': ['image'], 'description': ''}),
                                 tool('finish', {'status': 'ready', 'summary': 'Xong.', 'rule_ids': []})])
        report = GenSession(GenRequest(url=SITE), drafts_root=self.root, client=client, fetcher=fake_fetcher()).run()
        self.assertEqual('failed', report['status'])
        self.assertIn('Kiểm tra độc lập', report['summary'])

    def test_truncated_tool_input_and_bad_json_are_not_run(self) -> None:
        cut = tool('write_file', {'path': 'downloader.py'}, stop='max_tokens')
        client = ScriptedClient([ValueError('bad json'), tool('survey_site'), cut,
                                 tool('finish', {'status': 'failed', 'summary': 'x', 'rule_ids': []})])
        report = GenSession(GenRequest(url=SITE), drafts_root=self.root, client=client, fetcher=fake_fetcher()).run()
        self.assertEqual('failed', report['status'])
        res = _tool_results(client.requests[3])[0]
        self.assertTrue(res['is_error'])
        self.assertIn('max_tokens', res['content'])

    def test_cancel(self) -> None:
        cancel = threading.Event()

        def step(_params):
            cancel.set()
            return tool('survey_site')
        report = GenSession(GenRequest(url=SITE), drafts_root=self.root, client=ScriptedClient([step]), fetcher=fake_fetcher(),
                            cancel=cancel).run()
        self.assertEqual('cancelled', report['status'])

    def test_write_file_rules(self) -> None:
        box = ToolBox(site_url=SITE, drafts_root=self.root, media=['image'], fetcher=fake_fetcher())
        box.run('survey_site', {})
        box.run('create_draft', {'engine': 'gelbooru_dapi', 'id': 'fakeai', 'abbr': 'zq9', 'name': 'Fake AI', 'media': ['image'],
                                 'description': ''})
        _, err = box.run('write_file', {'path': 'downloader.py'})
        self.assertTrue(err)
        m = json.loads((box.draft / 'manifest.json').read_text())
        _, err = box.run('write_file', {'path': 'manifest.json', 'content': json.dumps({**m, 'id': 'other'})})
        self.assertTrue(err)
        _, err = box.run('write_file', {'path': 'manifest.json', 'content': json.dumps({**m, 'name': 'Fake AI 2'})})
        self.assertFalse(err)
        _, err = box.run('write_file', {'path': 'downloader.py', 'content': 'def (:\n'})
        self.assertTrue(err)
        names = {t['name'] for t in TOOLS}
        self.assertNotIn('install', ' '.join(names))

    def test_pick_tag(self) -> None:
        body = ''.join(_post_xml(*x) for x in POSTS)
        self.assertEqual('scenery', pick_tag(body, 'gelbooru_dapi'))
        self.assertEqual('', pick_tag('<posts count="0"></posts>', 'gelbooru_dapi'))


def _sse(events: list[dict]) -> bytes:
    return ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()


def _sse_tool_turn(n: int, name: str, args: dict, *, inp: int = 1200, out: int = 80) -> bytes:
    usage = {'input_tokens': inp, 'output_tokens': 1, 'cache_creation_input_tokens': 900, 'cache_read_input_tokens': 300}
    return _sse([
        {'type': 'message_start', 'message': {'id': f'msg_{n}', 'type': 'message', 'role': 'assistant', 'model': 'claude-opus-5-5',
                                              'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': f'step {n}'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': f'toolu_{n:02d}', 'name': name, 'input': {}}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args)}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': out}},
        {'type': 'message_stop'},
    ])


class SdkWireTests(TestCase):
    """The real anthropic SDK against a local SSE server: request shape and the tool loop, no key and no network"""

    def test_session_through_the_sdk(self) -> None:
        try:
            import anthropic
        except ImportError:
            self.skipTest('anthropic is not installed (optional extra: lustra[ai])')
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen: list[tuple[str, dict, dict]] = []
        turns = [_sse_tool_turn(1, 'survey_site', {}),
                 _sse_tool_turn(2, 'finish', {'status': 'failed', 'summary': 'thử', 'rule_ids': []})]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers['content-length'])))
                seen.append((self.path, dict(self.headers), body))
                payload = turns[min(len(seen), len(turns)) - 1]
                self.send_response(200)
                self.send_header('content-type', 'text/event-stream')
                self.send_header('content-length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                return None

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = anthropic.Anthropic(api_key='sk-ant-wire-test', base_url=f'http://127.0.0.1:{server.server_port}', max_retries=0)
            with tempfile.TemporaryDirectory() as tmp:
                report = GenSession(GenRequest(url=SITE), drafts_root=pathlib.Path(tmp), client=client, fetcher=fake_fetcher()).run()
        finally:
            server.shutdown()
        self.assertEqual(('failed', 'thử'), (report['status'], report['summary']), report)
        self.assertEqual(2, len(seen))
        path, headers, body = seen[0]
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertTrue(path.startswith('/v1/messages'))
        self.assertIn(FALLBACK_BETA, headers.get('anthropic-beta', ''))
        # the SDK authenticates with x-api-key or, on newer API versions, Authorization: Bearer
        self.assertIn('sk-ant-wire-test', (headers.get('x-api-key', ''), headers.get('authorization', '').removeprefix('Bearer ')))
        self.assertEqual(('claude-opus-5-5', True, 'default'), (body['model'], body['stream'], body['fallbacks']))
        self.assertEqual({'effort': 'high'}, body['output_config'])
        self.assertEqual({'type': 'ephemeral'}, body['cache_control'])
        self.assertEqual({'type': 'ephemeral'}, body['system'][0]['cache_control'])
        self.assertNotIn('thinking', body)
        tools = {t['name']: t for t in body['tools']}
        self.assertTrue(tools['write_file'].get('eager_input_streaming'))
        self.assertTrue(tools['survey_site'].get('strict'))
        second = seen[1][2]['messages']
        self.assertEqual(['user', 'assistant', 'user'], [m['role'] for m in second])
        self.assertEqual(['text', 'tool_use'], [b['type'] for b in second[1]['content']])
        result = second[2]['content'][0]
        self.assertEqual(('tool_result', 'toolu_01'), (result['type'], result['tool_use_id']))
        self.assertIn('gelbooru_dapi', result['content'])
        self.assertEqual({'input': 2400, 'output': 160, 'cacheRead': 600, 'cacheWrite': 1800, 'requests': 2}, report['usage'])


class WorkerTests(TestCase):
    def _run(self, cfg: dict) -> tuple[int, list[dict], str]:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'LUSTRA_DATA_DIR': tmp}
            p = subprocess.run([sys.executable, '-m', 'lustra.aigen.worker'], input=json.dumps(cfg) + '\n', capture_output=True,
                               text=True, cwd=ROOT, env=env, timeout=120)
        events = [json.loads(x[6:]) for x in p.stdout.splitlines() if x.startswith('@@GEN ')]
        return p.returncode, events, p.stdout + p.stderr

    def test_protocol_and_key_never_leaks(self) -> None:
        code, events, out = self._run({'cmd': 'start', 'url': f'http://{HOST}/', 'mode': 'ai', 'apiKey': 'sk-ant-SECRET-VALUE'})
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual(['R10'], events[-1]['ruleIds'])
        self.assertEqual(1, code)
        self.assertNotIn('SECRET-VALUE', out)
        code, events, _ = self._run({'cmd': 'start', 'url': SITE, 'mode': 'ai'})
        self.assertEqual((2, 'error'), (code, events[-1]['status']))
        code, events, _ = self._run({'cmd': 'nope'})
        self.assertEqual(2, code)

#
#
#########################################
