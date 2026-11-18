# coding=UTF-8
"""
Lustra: builds the remote extension catalog from extensions/ (docs/operations.md, 3d).

  python tools/build_catalog.py --out <dir> [--base-url URL] [--extensions DIR]

Writes one zip per extension (<id>-<version>.zip, manifest.json at the root) and index.json listing them with the sha256
of each archive. The zips are deterministic: same files, same bytes, same sha256, so an unchanged extension keeps its
checksum between catalog builds. Sign the result with tools/sign_catalog.py, then publish index.json, index.json.sig and
the zips side by side under --base-url (the official catalog: the "catalog" release of the GitHub repository).
"""
#########################################
#
#

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OFFICIAL_BASE_URL = 'https://github.com/Leiz-Workspace/Lustra/releases/download/catalog/'
SKIP_NAMES = {'__pycache__', '.DS_Store'}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _files(ext_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted((p for p in ext_dir.rglob('*') if p.is_file() and not SKIP_NAMES & set(p.relative_to(ext_dir).parts)
                   and p.suffix != '.pyc'), key=lambda p: p.relative_to(ext_dir).as_posix())


def build_archive(ext_dir: pathlib.Path, dest: pathlib.Path) -> str:
    """Zips one extension folder deterministically; returns the sha256 of the archive"""
    with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in _files(ext_dir):
            info = zipfile.ZipInfo(path.relative_to(ext_dir).as_posix(), date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, path.read_bytes())
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def build(extensions_dir: pathlib.Path, out: pathlib.Path, base_url: str) -> dict:
    if not base_url.lower().startswith('https://'):
        raise SystemExit('--base-url must be https')
    base_url = base_url if base_url.endswith('/') else base_url + '/'
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for manifest_path in sorted(extensions_dir.glob('*/manifest.json')):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        ext_id, version = manifest['id'], manifest['version']
        name = f'{ext_id}-{version}.zip'
        sha256 = build_archive(manifest_path.parent, out / name)
        entries.append({'id': ext_id, 'name': manifest.get('name', ext_id), 'abbr': manifest.get('abbr', ''), 'version': version,
                        'url': base_url + name, 'sha256': sha256, 'site': manifest.get('site', ''),
                        'description': manifest.get('description', '')})
    index = {'extensions': entries}
    (out / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Build the Lustra remote extension catalog (zips + index.json)')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--base-url', default=OFFICIAL_BASE_URL, help='https url the files are published under')
    ap.add_argument('--extensions', default=str(ROOT / 'extensions'), help='folder with one extension per subfolder')
    a = ap.parse_args(argv)
    index = build(pathlib.Path(a.extensions), pathlib.Path(a.out), a.base_url)
    for e in index['extensions']:
        print(f"{e['id']} {e['version']} {e['sha256']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
