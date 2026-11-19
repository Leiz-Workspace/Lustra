# coding=UTF-8
"""
Lustra: builds the remote extension catalog from extensions/ (docs/operations.md, 3d).

  python tools/build_catalog.py --out <dir> [--base-url URL] [--extensions DIR] [--lists DIR]

Writes one zip per extension (<id>-<version>.zip, manifest.json at the root) and index.json listing them with the sha256
of each archive. The zips are deterministic: same files, same bytes, same sha256, so an unchanged extension keeps its
checksum between catalog builds. Sign the result with tools/sign_catalog.py, then publish index.json, index.json.sig and
the zips side by side under --base-url (the official catalog: the "catalog" release of the GitHub repository).

Tag lists stay out of the repository (docs/plans/ext-tag-jobs.md): an extension's manifest names its list ("tagList":
"<abbr>_tags.json") and --lists is the folder that holds those files (made with 'lustra-core tags --modules <abbr> --out
<dir>', or taken out of the zips of the catalog in use). Each list goes into its extension's zip, next to manifest.json, so
the extension has it as soon as it is installed. An extension whose list is not there is built without it: the app reads the
list with the extension's own update when the user asks (Tags A-Z, "Cập nhật"). A zip over the installer's limits is refused.
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lustra.extensions import ARCHIVE_ENTRIES_MAX, ARCHIVE_SIZE_MAX, ARCHIVE_UNPACKED_MAX  # noqa: E402

OFFICIAL_BASE_URL = 'https://github.com/Leiz-Workspace/Lustra/releases/download/catalog/'
SKIP_NAMES = {'__pycache__', '.DS_Store'}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _files(ext_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    return {p.relative_to(ext_dir).as_posix(): p for p in ext_dir.rglob('*')
            if p.is_file() and not SKIP_NAMES & set(p.relative_to(ext_dir).parts) and p.suffix != '.pyc'}


def build_archive(ext_dir: pathlib.Path, dest: pathlib.Path, extra: dict[str, pathlib.Path] | None = None) -> str:
    """Zips one extension folder deterministically, plus extra files (archive name -> source, ex. its tag list); returns the
    sha256 of the archive. Raises SystemExit when the zip would be refused by the installer (lustra/extensions.py limits)"""
    files = {**_files(ext_dir), **(extra or {})}
    unpacked = 0
    with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(files):
            data = files[name].read_bytes()
            unpacked += len(data)
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    size = dest.stat().st_size
    if size > ARCHIVE_SIZE_MAX or unpacked > ARCHIVE_UNPACKED_MAX or len(files) > ARCHIVE_ENTRIES_MAX:
        raise SystemExit(f'{dest.name}: {size / 1048576:.1f} MB packed, {unpacked / 1048576:.1f} MB unpacked, {len(files):d} files: '
                         f'over the installer limits ({ARCHIVE_SIZE_MAX // 1048576:d} MB, {ARCHIVE_UNPACKED_MAX // 1048576:d} MB, '
                         f'{ARCHIVE_ENTRIES_MAX:d} files)')
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def tag_list_source(ext_dir: pathlib.Path, manifest: dict, lists: pathlib.Path | None) -> pathlib.Path | None:
    """The tag list file to put into the zip of this extension (None: no list declared, or already in its folder, or not found)"""
    name = str(manifest.get('tagList') or '')
    if not name or pathlib.PurePosixPath(name).name != name or (ext_dir / name).is_file() or lists is None:
        return None
    path = lists / name
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as err:
        raise SystemExit(f'{path}: not a tag list ({err})')
    if not isinstance(data, dict) or not data:
        raise SystemExit(f'{path}: not a tag list (expected {{"tag": "N posts (type)", ...}})')
    return path


def build(extensions_dir: pathlib.Path, out: pathlib.Path, base_url: str, lists: pathlib.Path | None = None) -> dict:
    if not base_url.lower().startswith('https://'):
        raise SystemExit('--base-url must be https')
    if lists is not None and not lists.is_dir():
        raise SystemExit(f'--lists: {lists} is not a folder')
    base_url = base_url if base_url.endswith('/') else base_url + '/'
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for manifest_path in sorted(extensions_dir.glob('*/manifest.json')):
        ext_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        ext_id, version = manifest['id'], manifest['version']
        name = f'{ext_id}-{version}.zip'
        tag_list = tag_list_source(ext_dir, manifest, lists)
        sha256 = build_archive(ext_dir, out / name, {manifest['tagList']: tag_list} if tag_list else None)
        if manifest.get('tagList') and not tag_list and not (ext_dir / str(manifest['tagList'])).is_file():
            print(f'{ext_id}: no {manifest["tagList"]} in --lists: built without its tag list (the app reads it when asked)',
                  file=sys.stderr)
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
    ap.add_argument('--lists', default=None, help='folder with the tag lists named by the manifests ("tagList"), put into the zips')
    a = ap.parse_args(argv)
    index = build(pathlib.Path(a.extensions), pathlib.Path(a.out), a.base_url, pathlib.Path(a.lists) if a.lists else None)
    for e in index['extensions']:
        print(f"{e['id']} {e['version']} {e['sha256']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
