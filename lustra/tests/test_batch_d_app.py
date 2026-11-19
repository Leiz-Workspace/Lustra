# coding=UTF-8
"""
Lustra: regression tests for audit batch D, desktop app side (docs/reports/audit-2026-11-16.md, D3 / D4), offline
- frozen core entry (lustra/frozen_main.py): 'server' / 'cli' dispatch used by the packaged app
- headers / cookies with '"' or '\\' as the app passes them ('-header name=value' / '-cookie name=value', see app/main.js connectionArgs)
- tools/build_core.py data staging: everything the frozen core needs at run time; the catalog extensions without a tag source carry
  their 2tags/ list, which an install from that catalog brings to the core
- tools/check_selftest.py: a failed step names its false checks and shows each value on a line of its own
Run: python -m unittest lustra.tests.test_batch_d_app
"""
#########################################
#
#

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import TestCase, mock

from lustra.logger import Logger

Logger.init(True, True)

from lustra.cmdargs import prepare_arglist  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _run_frozen_main(*args: str, stdin: str = '') -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LUSTRA_DATA_DIR': data_dir, 'PYTHONPATH': str(ROOT), 'LUSTRA_CATALOG_AUTOLOAD': '0'}
        return subprocess.run((sys.executable, '-m', 'lustra.frozen_main', *args), cwd=ROOT, env=env, input=stdin,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f'lustra_{name}', ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_build_core():
    return _load_tool('build_core')


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
            self.assertEqual({'lustra/builtin_ext', 'lustra/gender_rules', 'lustra/safety_data', 'extensions', '2tags', 'aigen_refs'},
                             set(data))
            # domain denylist + trusted catalog keys: read at run time by the core (fail closed when missing)
            self.assertTrue((data['lustra/safety_data'] / 'denied_domains.txt').is_file())
            self.assertTrue((data['lustra/safety_data'] / 'catalog_keys.json').is_file())
            self.assertTrue((data['lustra/builtin_ext'] / 'danbooru' / 'manifest.json').is_file())
            from lustra.aigen.tools import REF_FILES  # sources the AI generator shows the model (frozen core: no .py on disk)
            self.assertTrue(all((data['aigen_refs'] / rel).is_file() for rel in REF_FILES))
            self.assertTrue(any(data['lustra/gender_rules'].glob('*.json')))
            catalog = data['extensions']
            self.assertTrue(all((d / 'manifest.json').is_file() for d in catalog.iterdir() if d.is_dir()))
            self.assertFalse(any(catalog.rglob('__pycache__')))
            self.assertTrue((catalog / 'rule34hentai' / 'rn_tags.json').is_file())  # _stage_catalog, below
            self.assertFalse((catalog / 'bbooru' / 'bb_tags.json').exists())
            self.assertEqual(['all_tag_aliases.json'], sorted(f.name for f in data['2tags'].iterdir()))
        with tempfile.TemporaryDirectory() as tmp:
            data = dict((dest, src) for src, dest in bc._stage_data(pathlib.Path(tmp), 'none'))
            self.assertNotIn('2tags', data)

    def test_catalog_extensions_without_a_tag_source_carry_their_list(self) -> None:
        # Rule34Hentai, Paheal, Rule34.us read no list from their site (no tagUpdate, not reviewed, no engine): the bundled catalog
        # holds their 2tags/ list next to the manifest. Bbooru, e621, Xbooru read theirs (the core stays small)
        bc = _load_build_core()
        with tempfile.TemporaryDirectory() as tmp:
            catalog = bc._stage_catalog(pathlib.Path(tmp) / 'extensions')
            with_list = {p.parent.name: p.name for p in catalog.glob('*/*_tags.json')}
            self.assertEqual({'rule34hentai': 'rn_tags.json', 'paheal': 'rp_tags.json', 'rule34us': 'rs_tags.json'}, with_list)
            for ext_id, name in with_list.items():
                self.assertEqual((ROOT / '2tags' / name).stat().st_size, (catalog / ext_id / name).stat().st_size)
                manifest = json.loads((catalog / ext_id / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual((name, '1.0.2'), (manifest['tagList'], manifest['version']))  # 1.0.1 installs are offered the update
            for ext_id in ('bbooru', 'e621', 'xbooru'):
                self.assertEqual([], list((catalog / ext_id).glob('*_tags.json')))
            self._install_staged(catalog, pathlib.Path(tmp) / 'data')

    def _install_staged(self, catalog: pathlib.Path, data_dir: pathlib.Path) -> None:
        """An install (or an update of 1.0.1) from the staged catalog brings the list, the core uses it; the others fit the limits"""
        from lustra import tagsdb
        from lustra.denylist import denied_in_extension
        from lustra.downloaders import DOWNLOADERS_BY_PROC_MODULE
        from lustra.extensions import Extensions
        from lustra.module import ProcModule
        from lustra.tagsdb import TagsDB
        loaded, downloaders, catalog_loaded = dict(Extensions.loaded), dict(DOWNLOADERS_BY_PROC_MODULE), Extensions._catalog_loaded

        def restore() -> None:
            Extensions.loaded.clear()
            Extensions.loaded.update(loaded)
            DOWNLOADERS_BY_PROC_MODULE.clear()
            DOWNLOADERS_BY_PROC_MODULE.update(downloaders)
            Extensions._catalog_loaded = catalog_loaded
            TagsDB.clear()

        self.addCleanup(restore)
        # the registry lives for the whole test process: an earlier test may have loaded the catalog's 1.0.2 (test_tag_sources), and
        # the 1.0.1 installed below would then not register (Extensions._register keeps a loaded id)
        if (stale := Extensions.loaded.pop('rule34hentai', None)) is not None:
            DOWNLOADERS_BY_PROC_MODULE.pop(ProcModule.PROC_MODULES_BY_NAME.get(stale.abbr, -1), None)
        # the frozen core has no 2tags/rn_tags.json (tools/build_core.py bundles the aliases only): its tagsdb.py is elsewhere
        frozen_tagsdb = (data_dir.parent / 'core' / 'lustra' / 'tagsdb.py').as_posix()
        with mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': data_dir.as_posix(), 'LUSTRA_CATALOG_DIR': catalog.as_posix()}), \
                mock.patch.object(tagsdb, '__file__', frozen_tagsdb):
            os.environ.pop('LUSTRA_TAGS_DIR', None)  # restored by the patch
            old = data_dir.parent / 'old' / 'rule34hentai'  # 1.0.1 as installed before: no list
            shutil.copytree(catalog / 'rule34hentai', old, ignore=shutil.ignore_patterns('*_tags.json'))
            manifest = json.loads((old / 'manifest.json').read_text(encoding='utf-8'))
            (old / 'manifest.json').write_text(json.dumps({**manifest, 'version': '1.0.1'}), encoding='utf-8')
            Extensions.install_from_dir(old)
            TagsDB.clear()
            TagsDB.locate_default_lists()
            self.assertIsNone(TagsDB.get_file('rn'))
            row = next(r for r in Extensions.list_all() if r['id'] == 'rule34hentai')
            self.assertEqual((True, '1.0.2'), (row['updateAvailable'], row['catalogVersion']))  # "có bản v1.0.2"
            m = Extensions.install_from_catalog('rule34hentai')  # "Cập nhật lên v1.0.2"
            self.assertEqual(('rn', '1.0.2'), (m.abbr, m.version))
            installed = Extensions.user_dir() / 'rule34hentai' / 'rn_tags.json'
            self.assertTrue(installed.is_file())
            TagsDB.clear()
            TagsDB.locate_default_lists(reload=('rn',))
            self.assertEqual(installed, TagsDB.get_file('rn'))
            self.assertGreater(TagsDB.count('rn'), 10000)
            for ext_id in ('paheal', 'rule34us'):  # within the installer's limits (size, files, denied domains)
                Extensions._check_tree(catalog / ext_id)
                self.assertIsNone(denied_in_extension(catalog / ext_id))
            Extensions.uninstall('rule34hentai')



class CheckSelftestTests(TestCase):
    """tools/check_selftest.py reads the app's self-test report in the release workflow"""

    def test_failed_step_names_its_false_checks(self) -> None:
        tool = _load_tool('check_selftest')
        report = {'core': {'ok': True}, 'contract': {'ok': True, 'notAllowed': {'ok': False}},
                  'indexTools': {'ok': False, 'cleanOk': True, 'importOk': False, 'importJob': {'status': 'done', 'totalAll': 0},
                                 'importCard': {'text': 'x' * 5000}, 'ui': {'ok': True, 'page': 'y' * 5000}}}
        self.assertEqual(['indexTools'], tool.failed_steps(report))  # contract.notAllowed is false on purpose
        lines = tool.describe('indexTools', report['indexTools'])
        self.assertEqual('  indexTools: false: importOk', lines[0])
        self.assertIn('    importJob: {"status": "done", "totalAll": 0}', lines)  # not hidden behind the long card text
        self.assertTrue(all(len(x) <= tool.VALUE_MAX + 40 for x in lines))
        self.assertFalse(any(x.startswith('    ui:') for x in lines))  # a nested step that passed is left out
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'report.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            out = subprocess.run((sys.executable, str(ROOT / 'tools' / 'check_selftest.py'), str(path)), capture_output=True, text=True,
                                 timeout=60)
        self.assertEqual(1, out.returncode)
        self.assertIn('self-test failed: indexTools', out.stderr)
        self.assertIn('indexTools: false: importOk', out.stderr)


#
#
#########################################
