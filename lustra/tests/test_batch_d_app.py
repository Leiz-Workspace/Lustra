# coding=UTF-8
"""
Lustra: regression tests for audit batch D, desktop app side (docs/reports/audit-2026-11-16.md, D3 / D4), offline
- frozen core entry (lustra/frozen_main.py): 'server' / 'cli' dispatch used by the packaged app
- headers / cookies with '"' or '\\' as the app passes them ('-header name=value' / '-cookie name=value', see app/main.js connectionArgs)
- tools/build_core.py data staging: everything the frozen core needs at run time
Run: python -m unittest lustra.tests.test_batch_d_app
"""
#########################################
#
#

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest import TestCase

from lustra.logger import Logger

Logger.init(True, True)

from lustra.cmdargs import prepare_arglist  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _run_frozen_main(*args: str, stdin: str = '') -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LUSTRA_DATA_DIR': data_dir, 'PYTHONPATH': str(ROOT), 'LUSTRA_CATALOG_AUTOLOAD': '0'}
        return subprocess.run((sys.executable, '-m', 'lustra.frozen_main', *args), cwd=ROOT, env=env, input=stdin,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)


def _load_build_core():
    spec = importlib.util.spec_from_file_location('lustra_build_core', ROOT / 'tools' / 'build_core.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrozenMainTests(TestCase):
    def test_usage_and_unknown_command(self) -> None:
        r = _run_frozen_main()
        self.assertEqual(2, r.returncode)
        self.assertIn('usage: lustra-core', r.stderr)
        r = _run_frozen_main('bogus')
        self.assertEqual(2, r.returncode)
        r = _run_frozen_main('cli')  # no arguments: the legacy GUI is not bundled
        self.assertEqual(2, r.returncode)

    def test_server_ready_and_shutdown(self) -> None:
        r = _run_frozen_main('server', stdin='{"jsonrpc":"2.0","method":"app.shutdown"}\n')
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith('{')]
        self.assertTrue(lines and lines[0].get('method') == 'ready', r.stdout[-2000:])
        self.assertEqual('Lustra', lines[0]['params']['name'])

    def test_cli_parser(self) -> None:
        r = _run_frozen_main('cli', '--help')
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        self.assertIn('-module', r.stdout)


class HttpPairTests(TestCase):
    def test_header_and_cookie_values_with_quotes_and_backslashes(self) -> None:
        # what app/main.js connectionArgs sends: one 'name=value' per entry, split at the first '='
        value_h, value_c = 'a"b\\c=d', '"q\\z"'
        args = prepare_arglist(['-header', f'X-Test={value_h}', '-cookie', f'sid={value_c}', '-header', 'If-None-Match="etag"',
                                '-module', 'db', '--', 'scenery'])
        self.assertEqual([('X-Test', value_h), ('If-None-Match', '"etag"')], args.header)
        self.assertEqual([('sid', value_c)], args.cookie)
        self.assertEqual(['scenery'], args.tags)


class BuildCoreTests(TestCase):
    def test_extension_imports_are_bundled_as_hidden_imports(self) -> None:
        bc = _load_build_core()
        names = bc._extension_imports()
        self.assertIn('bs4', names)
        self.assertFalse(any(n.startswith('lustra') for n in names))

    def test_stage_data(self) -> None:
        bc = _load_build_core()
        with tempfile.TemporaryDirectory() as tmp:
            stage = pathlib.Path(tmp)
            data = dict((dest, src) for src, dest in bc._stage_data(stage, 'aliases'))
            self.assertEqual({'lustra/builtin_ext', 'lustra/gender_rules', 'extensions', '2tags', 'aigen_refs'}, set(data))
            self.assertTrue((data['lustra/builtin_ext'] / 'danbooru' / 'manifest.json').is_file())
            from lustra.aigen.tools import REF_FILES  # sources the AI generator shows the model (frozen core: no .py on disk)
            self.assertTrue(all((data['aigen_refs'] / rel).is_file() for rel in REF_FILES))
            self.assertTrue(any(data['lustra/gender_rules'].glob('*.json')))
            catalog = data['extensions']
            self.assertTrue(all((d / 'manifest.json').is_file() for d in catalog.iterdir() if d.is_dir()))
            self.assertFalse(any(catalog.rglob('__pycache__')))
            self.assertEqual(['all_tag_aliases.json'], sorted(f.name for f in data['2tags'].iterdir()))
        with tempfile.TemporaryDirectory() as tmp:
            data = dict((dest, src) for src, dest in bc._stage_data(pathlib.Path(tmp), 'none'))
            self.assertNotIn('2tags', data)


#
#
#########################################
