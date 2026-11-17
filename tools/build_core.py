#!/usr/bin/env python3
# coding=UTF-8
"""
Builds the frozen Lustra core shipped with the desktop app: a PyInstaller one-dir bundle 'lustra-core' (entry lustra/frozen_main.py,
commands 'server' and 'cli ...') in app/build/core/lustra-core. electron-builder copies it to <resources>/core (app/package.json
build.extraResources) and app/main.js runs it instead of python when the app is packaged.

usage: python tools/build_core.py [--tags aliases|none|all|rx,db,...] [--out DIR] [--no-smoke]
The running interpreter needs the runtime requirements and PyInstaller:
  python -m pip install -r requirements.txt 'pyinstaller>=6.21'
Bundled data: builtin extension manifests, gender rules, the local extension catalog (extensions/), tag aliases and optionally
tag lists (2tags/, large: ~124 MB for all modules; the app can download fresh lists into its data dir instead).
"""
#########################################
#
#

import argparse
import ast
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = 'lustra-core'
ENTRY = ROOT / 'lustra' / 'frozen_main.py'
DEFAULT_OUT = ROOT / 'app' / 'build' / 'core'
TAGS_DIR = ROOT / '2tags'
ALIASES_FILE = 'all_tag_aliases.json'

# the legacy Tk GUI is not part of the core: keeps tkinter / Tcl out of the bundle
EXCLUDED_MODULES = (
    'tkinter', '_tkinter', 'tkinterdnd2', 'lxml', 'PyInstaller', 'pip', 'setuptools', 'ruff', 'pydoc', 'lib2to3',
    'lustra.tests', 'lustra.release',
)


def _lustra_modules() -> list[str]:
    """Core modules importable without tkinter (GUI modules fail to import and are left out). Catalog extensions are loaded from
    files at run time and import lustra modules the core itself may not use, so every importable module is bundled"""
    names = []
    for pkg in ('lustra', 'lustra/vcs'):
        for p in sorted((ROOT / pkg).glob('*.py')):
            if p.stem not in ('__init__', '__main__', 'frozen_main'):
                names.append(f'{pkg.replace("/", ".")}.{p.stem}')
    probe = (
        'import importlib, json, sys\n'
        'sys.modules["tkinter"] = None\n'
        'ok = []\n'
        'for n in json.loads(sys.argv[1]):\n'
        '    try:\n'
        '        importlib.import_module(n)\n'
        '        ok.append(n)\n'
        '    except BaseException:\n'  # dev scripts may exit on import
        '        pass\n'
        'sys.__stdout__.write("@@" + json.dumps(ok) + "\\n")\n'  # lustra.server points sys.stdout at stderr
    )
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LUSTRA_DATA_DIR': data_dir, 'PYTHONPATH': str(ROOT)}
        out = subprocess.run((sys.executable, '-c', probe, json.dumps(names)), cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return json.loads(next(x for x in out.stdout.splitlines() if x.startswith('@@'))[2:])


def _extension_imports() -> list[str]:
    """Top-level modules imported by catalog extensions (their downloader.py files are not analyzed by PyInstaller)"""
    names: set[str] = set()
    for src in (ROOT / 'extensions').glob('*/*.py'):
        for node in ast.walk(ast.parse(src.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
    # collections.abc is always bundled (base_library.zip) but PyInstaller reports it as a missing hidden import
    return sorted(n for n in names if not n.startswith('lustra') and n != 'collections.abc')


def _stage_data(stage: pathlib.Path, tags: str) -> list[tuple[pathlib.Path, str]]:
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.*')
    data = [
        (ROOT / 'lustra' / 'builtin_ext', 'lustra/builtin_ext'),
        (ROOT / 'lustra' / 'gender_rules', 'lustra/gender_rules'),
    ]
    catalog = stage / 'extensions'
    shutil.copytree(ROOT / 'extensions', catalog, ignore=ignore)
    data.append((catalog, 'extensions'))
    tag_files: list[pathlib.Path] = []
    if tags != 'none' and (TAGS_DIR / ALIASES_FILE).is_file():
        tag_files.append(TAGS_DIR / ALIASES_FILE)
    if tags == 'all':
        tag_files.extend(sorted(TAGS_DIR.glob('*_tags.json')))
    elif tags not in ('none', 'aliases'):
        for abbr in (t.strip() for t in tags.split(',') if t.strip()):
            f = TAGS_DIR / f'{abbr}_tags.json'
            if not f.is_file():
                raise SystemExit(f'tag list not found: {f}')
            tag_files.append(f)
    if tag_files:
        tags_stage = stage / '2tags'
        tags_stage.mkdir()
        for f in tag_files:
            shutil.copy2(f, tags_stage / f.name)
        data.append((tags_stage, '2tags'))
    return data


def _dir_size(path: pathlib.Path) -> int:
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file() and not f.is_symlink())


def _smoke_test(exe: pathlib.Path) -> None:
    """The bundle must start the sidecar (ready notification, builtin modules loaded) and run the CLI argument parser"""
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LUSTRA_DATA_DIR': data_dir}
        env.pop('PYTHONPATH', None)
        r = subprocess.run((str(exe), 'server'), input='{"jsonrpc":"2.0","method":"app.shutdown"}\n', env=env, capture_output=True,
                           text=True, encoding='utf-8', timeout=120)
        ready = next((json.loads(line) for line in r.stdout.splitlines() if line.startswith('{') and '"ready"' in line), None)
        if r.returncode != 0 or not ready:
            raise SystemExit(f'smoke test failed: server exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print(f'smoke test: server ready, version {ready["params"].get("version")}, python {ready["params"].get("python")}')
        r = subprocess.run((str(exe), 'cli', '--help'), env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
        if r.returncode != 0 or '-module' not in r.stdout:
            raise SystemExit(f'smoke test failed: cli --help exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print('smoke test: cli ok')


def build(out_dir: pathlib.Path, tags: str, smoke: bool) -> pathlib.Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit('PyInstaller is not installed: python -m pip install -r requirements.txt \'pyinstaller>=6.21\'')
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lustra-core-build-') as tmp:
        stage = pathlib.Path(tmp)
        data = _stage_data(stage, tags)
        cmd = [
            sys.executable, '-m', 'PyInstaller', str(ENTRY),
            '--name', NAME, '--onedir', '--console', '--noconfirm', '--clean', '--noupx', '--log-level', 'WARN',
            '--distpath', str(out_dir), '--workpath', str(stage / 'work'), '--specpath', str(stage),
            '--paths', str(ROOT),
            '--collect-data', 'fake_useragent',
            '--hidden-import', 'socks', '--hidden-import', 'urllib3.contrib.socks',
        ]
        for name in (*_lustra_modules(), *_extension_imports()):
            cmd += ['--hidden-import', name]
        for name in EXCLUDED_MODULES:
            cmd += ['--exclude-module', name]
        for src, dest in data:
            cmd += ['--add-data', f'{src}{os.pathsep}{dest}']
        print(' '.join(cmd))
        subprocess.run(cmd, cwd=ROOT, check=True)
    bundle = out_dir / NAME
    exe = bundle / (f'{NAME}.exe' if sys.platform == 'win32' else NAME)
    if not exe.is_file():
        raise SystemExit(f'build failed: {exe} not found')
    print(f'\n{bundle}: {_dir_size(bundle) / 1048576:.1f} MiB')
    if smoke:
        _smoke_test(exe)
    return bundle


def main() -> None:
    ap = argparse.ArgumentParser(description='Build the frozen Lustra core (PyInstaller one-dir) for the desktop app')
    ap.add_argument('--out', type=pathlib.Path, default=DEFAULT_OUT, help=f'output folder (default: {DEFAULT_OUT})')
    ap.add_argument('--tags', default='aliases',
                    help='tag lists to bundle from 2tags/: aliases (default: aliases file only), none, all, or abbreviations: rx,db')
    ap.add_argument('--no-smoke', action='store_true', help='skip starting the built core')
    args = ap.parse_args()
    build(args.out.resolve(), args.tags.strip().lower(), not args.no_smoke)


if __name__ == '__main__':
    main()

#
#
#########################################
