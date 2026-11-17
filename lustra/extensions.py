# coding=UTF-8
"""
Lustra: extension registry
Site downloaders are extensions: a folder with manifest.json (+ downloader.py for code not shipped with the core).
Sources (in load order): builtin (lustra/builtin_ext), user-installed (<data dir>/extensions), local catalog (repo extensions/)
"""
#########################################
#
#

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
from threading import RLock

from .defines import IDVAL_EQ_SEPARATORS, MODULES_BUILTIN, PLATFORM_DARWIN, PLATFORM_WINDOWS, TAG_SEPARATORS, UTF8, ModuleCaps
from .gender import register_rules_file
from .logger import trace
from .modspec import MANIFEST_FILE_NAME, ExtensionManifest, ManifestError, re_ext_id
from .module import ProcModule
from .safety import register_extra_tags

__all__ = ('ExtensionError', 'Extensions', 'data_dir')

ENV_DATA_DIR = 'LUSTRA_DATA_DIR'
ENV_CATALOG_DIR = 'LUSTRA_CATALOG_DIR'
ENV_CATALOG_URL = 'LUSTRA_CATALOG_URL'
ENV_CATALOG_AUTOLOAD = 'LUSTRA_CATALOG_AUTOLOAD'
ARCHIVE_SIZE_MAX = 10 * 1024 * 1024
ARCHIVE_UNPACKED_MAX = 50 * 1024 * 1024
ARCHIVE_ENTRIES_MAX = 2000
TREE_SIZE_MAX = ARCHIVE_UNPACKED_MAX
CATALOG_SIZE_MAX = 1024 * 1024
re_sha256 = re.compile(r'^[0-9a-fA-F]{64}$')
SOURCE_BUILTIN, SOURCE_USER, SOURCE_CATALOG = 'builtin', 'user', 'catalog'


class ExtensionError(Exception):
    pass


def data_dir() -> pathlib.Path:
    """Per-user data dir, matches Electron's app.getPath('userData') for app name 'Lustra'"""
    if env := os.environ.get(ENV_DATA_DIR):
        return pathlib.Path(env).expanduser()
    if sys.platform == PLATFORM_WINDOWS:
        base = pathlib.Path(os.environ.get('APPDATA') or pathlib.Path.home() / 'AppData' / 'Roaming')
    elif sys.platform == PLATFORM_DARWIN:
        base = pathlib.Path.home() / 'Library' / 'Application Support'
    else:
        base = pathlib.Path(os.environ.get('XDG_CONFIG_HOME') or pathlib.Path.home() / '.config')
    return base / 'Lustra'


def _version_key(version: str) -> tuple:
    """'1.10.2' -> (1, 10, 2); non numeric parts compare after numbers ('1.2.0-beta' < '1.2.0' is not modelled: equal)"""
    parts = []
    for p in str(version).split('-', 1)[0].split('.'):
        parts.append((0, int(p), '') if p.isdigit() else (1, 0, p))
    while parts and parts[-1] == (0, 0, ''):
        parts.pop()
    return tuple(parts)


def version_newer(candidate: str, current: str) -> bool:
    return _version_key(candidate) > _version_key(current)


class Extensions:
    """Extensions !Static!"""
    _lock = RLock()
    loaded: dict[str, ExtensionManifest] = {}
    '''by extension id'''
    errors: dict[str, str] = {}
    '''by extension folder, last load errors'''
    _catalog_loaded = False

    def __init__(self) -> None:
        raise RuntimeError(f'{self.__class__.__name__} class should never be instanced!')

    # ----- locations -----

    @staticmethod
    def builtin_dir() -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parent / 'builtin_ext'

    @staticmethod
    def user_dir() -> pathlib.Path:
        return data_dir() / 'extensions'

    @staticmethod
    def catalog_dir() -> pathlib.Path | None:
        if env := os.environ.get(ENV_CATALOG_DIR):
            return pathlib.Path(env).expanduser()
        candidates = [pathlib.Path(__file__).resolve().parent.parent / 'extensions']
        if getattr(sys, 'frozen', False):
            candidates.insert(0, pathlib.Path(sys.executable).resolve().parent / 'extensions')
        return next((c for c in candidates if c.is_dir()), None)

    @staticmethod
    def _scan(base: pathlib.Path | None, source: str) -> list[ExtensionManifest]:
        result: list[ExtensionManifest] = []
        if not base or not base.is_dir():
            return result
        for sub in sorted(base.iterdir()):
            if sub.name.startswith('.') or not sub.is_dir():
                continue  # staging / backup folders of an install in progress
            mpath = sub / MANIFEST_FILE_NAME
            if not mpath.is_file():
                continue
            try:
                m = ExtensionManifest.load(mpath, source)
                if m.id != sub.name:
                    raise ManifestError(f'extension id \'{m.id}\' does not match its folder name \'{sub.name}\'')
                result.append(m)
            except Exception as err:
                Extensions.errors[sub.as_posix()] = str(err)
                trace(f'Warning (W2): extension at \'{sub}\' is invalid: {err}')
        return result

    # ----- loading -----

    @staticmethod
    def _import_entry(m: ExtensionManifest) -> type:
        from .download import Downloader
        target, cls_name = m.entry.split(':', 1)
        if m.is_file_entry:
            assert m.path
            file_path = (m.path / target).resolve()
            if m.path.resolve() not in file_path.parents or not file_path.is_file():
                raise ExtensionError(f'{m.id}: entry file \'{target}\' not found inside extension folder')
            mod_name = f'lustra_ext_{m.id.replace("-", "_")}'
            spec = importlib.util.spec_from_file_location(mod_name, file_path)
            if spec is None or spec.loader is None:
                raise ExtensionError(f'{m.id}: unable to load \'{target}\'')
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(mod_name, None)
                raise
        else:
            if not target.startswith('lustra.') or m.source != SOURCE_BUILTIN:
                raise ExtensionError(f'{m.id}: module entries are only allowed for builtin extensions')
            module = importlib.import_module(target)
        cls = getattr(module, cls_name, None)
        if not isinstance(cls, type) or not issubclass(cls, Downloader):
            raise ExtensionError(f'{m.id}: \'{cls_name}\' is not a Downloader subclass')
        return cls

    @staticmethod
    def _register(m: ExtensionManifest) -> None:
        from .downloaders import DOWNLOADERS_BY_PROC_MODULE
        from .file_parser import register_module_prefix
        from .tags_parser import register_module_syntax
        from .task import register_module_limits

        with Extensions._lock:
            if m.id in Extensions.loaded:
                return
            if other := next((e for e in Extensions.loaded.values() if e.abbr == m.abbr), None):
                raise ExtensionError(f'{m.id}: abbreviation \'{m.abbr}\' is already used by extension \'{other.id}\'')
            cls = Extensions._import_entry(m)
            proc_id = ProcModule.register(m.abbr)
            s = m.syntax
            register_module_syntax(proc_id, s.metaCount, s.sort, s.fav, s.pool)
            register_module_limits(proc_id, s.maxTagsStringLen, s.maxTags, s.maxTagsSeparate, s.maxWildcards,
                                   s.freeMetaPrefixes, s.localPositiveOverflow)
            register_module_prefix(proc_id, f'{m.abbr}_')
            IDVAL_EQ_SEPARATORS.setdefault(m.abbr, s.idSep)
            TAG_SEPARATORS.setdefault(m.abbr, s.concat)
            if m.safetyTags:
                register_extra_tags(m.safetyTags)
            if m.genderRules and m.path:
                rules_path = (m.path / m.genderRules).resolve()
                if m.path.resolve() in rules_path.parents and rules_path.is_file():
                    register_rules_file(m.abbr, rules_path)
                    cls.CAPABILITIES = frozenset((*cls.CAPABILITIES, ModuleCaps.GENDER_TAGS))
            DOWNLOADERS_BY_PROC_MODULE[proc_id] = cls
            Extensions.loaded[m.id] = m

    @staticmethod
    def _load_many(manifests: list[ExtensionManifest]) -> None:
        for m in manifests:
            if m.id in Extensions.loaded:
                continue
            try:
                Extensions._register(m)
            except Exception as err:
                Extensions.errors[(m.path or pathlib.Path(m.id)).as_posix()] = str(err)
                trace(f'Warning (W2): failed to load extension \'{m.id}\': {err}')

    @staticmethod
    def ensure_loaded(include_catalog=False) -> None:
        """Loads builtin and user-installed extensions (and local catalog ones if requested: legacy cli/gui behavior)"""
        with Extensions._lock:
            if not any(e.source == SOURCE_BUILTIN for e in Extensions.loaded.values()):
                try:
                    Extensions._cleanup_staging()
                except OSError as err:
                    trace(f'Warning (W2): unable to clean up extension staging folders: {err}')
                Extensions._load_many(Extensions._scan(Extensions.builtin_dir(), SOURCE_BUILTIN))
                Extensions._load_many(Extensions._scan(Extensions.user_dir(), SOURCE_USER))
            if include_catalog and not Extensions._catalog_loaded:
                Extensions._catalog_loaded = True
                Extensions._load_many(Extensions._scan(Extensions.catalog_dir(), SOURCE_CATALOG))

    @staticmethod
    def catalog_autoload() -> bool:
        """CLI / legacy GUI load not installed catalog extensions on demand. The desktop app (sidecar, jobs) only uses installed ones"""
        return os.environ.get(ENV_CATALOG_AUTOLOAD, '1') != '0'

    @staticmethod
    def get_by_abbr(abbr: str) -> ExtensionManifest | None:
        return next((e for e in Extensions.loaded.values() if e.abbr == abbr), None)

    @staticmethod
    def is_builtin_abbr(abbr: str) -> bool:
        return abbr in MODULES_BUILTIN

    # ----- listing -----

    @staticmethod
    def list_all() -> list[dict]:
        """All known extensions: loaded ones plus not installed catalog ones"""
        Extensions.ensure_loaded()
        rows: dict[str, dict] = {}
        from .downloaders import DOWNLOADERS_BY_PROC_MODULE
        for m in Extensions.loaded.values():
            cls = DOWNLOADERS_BY_PROC_MODULE.get(ProcModule.PROC_MODULES_BY_NAME.get(m.abbr, -1))
            caps = sorted(cls.CAPABILITIES) if cls else []
            rows[m.id] = {**m.to_public_dict(), 'installed': True, 'loaded': True, 'capabilities': caps}
        user_ids = {m.id for m in Extensions._scan(Extensions.user_dir(), SOURCE_USER)}
        for m in Extensions._scan(Extensions.catalog_dir(), SOURCE_CATALOG):
            if m.id in rows:
                row = rows[m.id]
                row['installed'] = row['source'] == SOURCE_BUILTIN or m.id in user_ids
                if row['source'] != SOURCE_BUILTIN and row['installed']:  # a newer copy in the local catalog: offered as an update
                    row['catalogVersion'] = m.version
                    row['updateAvailable'] = version_newer(m.version, row['version'])
                continue
            rows[m.id] = {**m.to_public_dict(), 'installed': False, 'loaded': False}
        return sorted(rows.values(), key=lambda r: (not r['default'], r['source'] != SOURCE_BUILTIN, r['name'].lower()))

    # ----- install / uninstall -----

    @staticmethod
    def _valid_ext_id(ext_id: object) -> str:
        if not isinstance(ext_id, str) or not re_ext_id.fullmatch(ext_id):
            raise ExtensionError('invalid extension id')
        return ext_id

    @staticmethod
    def _user_ext_path(ext_id: object) -> pathlib.Path:
        """Path of an installed extension. Never resolves outside of the user extensions folder"""
        ext_id = Extensions._valid_ext_id(ext_id)
        base = Extensions.user_dir().resolve()
        path = (base / ext_id).resolve()
        if path.parent != base:
            raise ExtensionError('invalid extension id')
        return path

    @staticmethod
    def _check_tree(src: pathlib.Path) -> None:
        """Rejects symlinks and oversized trees before anything is copied"""
        total, count = 0, 0
        for item in src.rglob('*'):
            if item.is_symlink():
                raise ExtensionError(f'symbolic links are not allowed in extensions: {item.relative_to(src)}')
            if item.is_file():
                count += 1
                total += item.stat().st_size
            if count > ARCHIVE_ENTRIES_MAX or total > TREE_SIZE_MAX:
                raise ExtensionError('extension is too large')

    @staticmethod
    def _probe_entry(m: ExtensionManifest) -> None:
        """Imports the entry of a staged extension under a throwaway module name (install validation)"""
        if not m.is_file_entry:
            raise ExtensionError(f'{m.id}: module entries are only allowed for builtin extensions')
        probe = dataclasses.replace(m, id=f'probe_{uuid.uuid4().hex}')
        mod_name = f'lustra_ext_{probe.id.replace("-", "_")}'
        try:
            Extensions._import_entry(probe)
        finally:
            sys.modules.pop(mod_name, None)

    @staticmethod
    def _abbr_conflict(m: ExtensionManifest) -> str:
        on_disk = [*Extensions._scan(Extensions.user_dir(), SOURCE_USER), *Extensions._scan(Extensions.catalog_dir(), SOURCE_CATALOG)]
        for other in (*Extensions.loaded.values(), *on_disk):
            if other.abbr == m.abbr and other.id != m.id:
                return other.id
        return ''

    @staticmethod
    def _install_tree(src: pathlib.Path) -> ExtensionManifest:
        """Transactional install: validate -> stage (.id.tmp) -> import probe -> swap (old version kept as .id.bak until success)"""
        src = pathlib.Path(src)
        if src.is_symlink() or not src.is_dir():
            raise ExtensionError('extension source must be a folder')
        m = ExtensionManifest.load(src / MANIFEST_FILE_NAME, SOURCE_USER)
        if Extensions.is_builtin_abbr(m.abbr) or m.id in {e.id for e in Extensions._scan(Extensions.builtin_dir(), SOURCE_BUILTIN)}:
            raise ExtensionError(f'\'{m.id}\' is a builtin extension and cannot be replaced')
        if other := Extensions._abbr_conflict(m):
            raise ExtensionError(f'{m.id}: abbreviation \'{m.abbr}\' is already used by extension \'{other}\'')
        Extensions._check_tree(src)
        dest = Extensions._user_ext_path(m.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp, bak = dest.with_name(f'.{m.id}.tmp'), dest.with_name(f'.{m.id}.bak')
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            staged = ExtensionManifest.load(tmp / MANIFEST_FILE_NAME, SOURCE_USER)
            Extensions._probe_entry(staged)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(bak, ignore_errors=True)
        if dest.exists():
            dest.rename(bak)
        tmp.rename(dest)
        try:
            installed = ExtensionManifest.load(dest / MANIFEST_FILE_NAME, SOURCE_USER)
            if installed.id not in Extensions.loaded:
                Extensions._register(installed)
            else:
                trace(f'Info: extension \'{installed.id}\' updated to {installed.version}, restart to use the new version')
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            if bak.exists():
                bak.rename(dest)
            raise
        shutil.rmtree(bak, ignore_errors=True)
        return installed

    @staticmethod
    def restart_required(m: ExtensionManifest) -> bool:
        """True if an older version of this extension is still the one imported in this process"""
        return Extensions.loaded.get(m.id) is not m

    @staticmethod
    def _cleanup_staging() -> None:
        """Recovers from an interrupted install: restores .id.bak if the swap did not finish, removes stale staging folders"""
        base = Extensions.user_dir()
        if not base.is_dir():
            return
        for item in base.iterdir():
            name = item.name
            if not name.startswith('.') or not item.is_dir():
                continue
            ext_id = name[1:].rsplit('.', 1)[0]
            if name.endswith('.bak') and re_ext_id.fullmatch(ext_id) and not (base / ext_id).exists():
                item.rename(base / ext_id)
            elif name.endswith(('.tmp', '.bak')):
                shutil.rmtree(item, ignore_errors=True)

    @staticmethod
    def install_from_dir(src: pathlib.Path) -> ExtensionManifest:
        """Local folder install (CLI / developers). The desktop app never forwards folder paths from the renderer"""
        with Extensions._lock:
            return Extensions._install_tree(pathlib.Path(src))

    @staticmethod
    def install_from_catalog(ext_id: str) -> ExtensionManifest:
        ext_id = Extensions._valid_ext_id(ext_id)
        cat = Extensions.catalog_dir()
        if not cat or not (cat / ext_id / MANIFEST_FILE_NAME).is_file():
            raise ExtensionError(f'extension \'{ext_id}\' is not available in local catalog')
        return Extensions.install_from_dir(cat / ext_id)

    @staticmethod
    def _valid_sha256(sha256: object) -> str:
        if not isinstance(sha256, str) or not re_sha256.fullmatch(sha256.strip()):
            raise ExtensionError('sha256 of the archive is required (64 hex characters)')
        return sha256.strip().lower()

    @staticmethod
    def install_from_archive(data: bytes, sha256: str) -> ExtensionManifest:
        """Installs a zip archive (manifest.json at root or in a single top folder). sha256 of the archive is mandatory"""
        sha256 = Extensions._valid_sha256(sha256)
        if len(data) > ARCHIVE_SIZE_MAX:
            raise ExtensionError('extension archive is too large')
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise ExtensionError(f'archive checksum mismatch (expected {sha256}, got {digest})')
        with tempfile.TemporaryDirectory(prefix='lustra-ext-') as tmpdir:
            root = pathlib.Path(tmpdir).resolve()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                infos = zf.infolist()
                if len(infos) > ARCHIVE_ENTRIES_MAX or sum(i.file_size for i in infos) > ARCHIVE_UNPACKED_MAX:
                    raise ExtensionError('extension archive is too large when unpacked')
                for info in infos:
                    target = (root / info.filename).resolve()
                    if root not in target.parents and target != root:
                        raise ExtensionError(f'unsafe path in archive: {info.filename}')
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ExtensionError(f'symbolic links are not allowed in archive: {info.filename}')
                zf.extractall(root)
            src = root if (root / MANIFEST_FILE_NAME).is_file() else next(
                (d for d in root.iterdir() if (d / MANIFEST_FILE_NAME).is_file()), None)
            if src is None:
                raise ExtensionError('manifest.json not found in archive')
            with Extensions._lock:
                return Extensions._install_tree(src)

    @staticmethod
    def install_from_archive_file(path: pathlib.Path, sha256: str) -> ExtensionManifest:
        path = pathlib.Path(path)
        if not path.is_file() or path.stat().st_size > ARCHIVE_SIZE_MAX:
            raise ExtensionError('extension archive is missing or too large')
        return Extensions.install_from_archive(path.read_bytes(), sha256)

    @staticmethod
    def _https_get(url: object, limit: int, timeout: int) -> bytes:
        import requests
        if not isinstance(url, str) or not url.lower().startswith('https://'):
            raise ExtensionError('only https:// urls are allowed')
        with requests.get(url, timeout=timeout, stream=True, headers={'User-Agent': 'Lustra extension manager'}) as r:
            if not r.url.lower().startswith('https://'):
                raise ExtensionError('redirect to a non-https url refused')
            r.raise_for_status()
            buf = io.BytesIO()
            for chunk in r.iter_content(64 * 1024):
                buf.write(chunk)
                if buf.tell() > limit:
                    raise ExtensionError('response is too large')
        return buf.getvalue()

    @staticmethod
    def fetch_remote_catalog(url: str | None = None, timeout=15) -> list[dict]:
        """Remote catalog index (https only): {"extensions": [{"id", "name", "abbr", "version", "url", "sha256", "description"}, ...]}"""
        url = url or os.environ.get(ENV_CATALOG_URL, '')
        if not url:
            return []
        data = json.loads(Extensions._https_get(url, CATALOG_SIZE_MAX, timeout))
        items = data.get('extensions', []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict) and isinstance(i.get('id'), str) and isinstance(i.get('url'), str)
                and isinstance(i.get('sha256'), str) and re_sha256.fullmatch(i['sha256'])]

    @staticmethod
    def install_from_url(url: str, sha256: str, timeout=30) -> ExtensionManifest:
        sha256 = Extensions._valid_sha256(sha256)
        return Extensions.install_from_archive(Extensions._https_get(url, ARCHIVE_SIZE_MAX, timeout), sha256)

    @staticmethod
    def uninstall(ext_id: str) -> None:
        from .downloaders import DOWNLOADERS_BY_PROC_MODULE
        with Extensions._lock:
            dest = Extensions._user_ext_path(ext_id)
            m = Extensions.loaded.get(ext_id)
            if (m and m.source == SOURCE_BUILTIN) or ext_id in {e.id for e in Extensions._scan(Extensions.builtin_dir(), SOURCE_BUILTIN)}:
                raise ExtensionError(f'\'{ext_id}\' is a builtin extension and cannot be uninstalled')
            if dest.is_symlink() or not (dest / MANIFEST_FILE_NAME).is_file():
                raise ExtensionError(f'extension \'{ext_id}\' is not installed')
            shutil.rmtree(dest)
            # imported code stays in memory until restart, but the module can no longer be used
            if m and m.source == SOURCE_USER:
                Extensions.loaded.pop(ext_id, None)
                DOWNLOADERS_BY_PROC_MODULE.pop(ProcModule.PROC_MODULES_BY_NAME.get(m.abbr, -1), None)

    @staticmethod
    def write_manifest(path: pathlib.Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding=UTF8)

#
#
#########################################
