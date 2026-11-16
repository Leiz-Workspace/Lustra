# coding=UTF-8
"""
Lustra desktop sidecar: JSON-RPC 2.0 over stdio (one JSON object per line)
Started by the Electron main process: python -m lustra.server
Handles short queries (tags, preview, count, extensions, tag list updates). Downloads run as separate CLI processes
(LUSTRA_IPC=1 python -m lustra ...), so module-global core state never mixes between jobs.
"""
#########################################
#
#

from __future__ import annotations

import io
import json
import os
import pathlib
import platform
import re
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.dummy import current_process
from typing import Any

# protocol channel must stay clean: everything else printed to stdout goes to stderr
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

from .defines import ThreadInterruptException  # noqa: E402
from .extensions import Extensions, data_dir  # noqa: E402
from .gender import decide, infer_from_tags, rules_for_module  # noqa: E402
from .help import APP_ADDRESS  # noqa: E402
from .logger import Logger  # noqa: E402
from .module import ProcModule  # noqa: E402
from .rex import re_api_credentials  # noqa: E402
from .safety import blocked_tag  # noqa: E402
from .tags import TAG_NAMES_BY_TYPE, TagTypes  # noqa: E402
from .tagsdb import TagsDB  # noqa: E402
from .vcs import APP_NAME, APP_VERSION  # noqa: E402

__all__ = ('main',)

PROTOCOL_VERSION = 1
ENV_TAGS_DIR = 'LUSTRA_TAGS_DIR'
REPO_TAGS_DIR = pathlib.Path(__file__).resolve().parent.parent / '2tags'

_out_lock = threading.Lock()
_core_lock = threading.RLock()
'''core keeps current module in global state: module-dependent calls are serialized'''
_tls = threading.local()


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    with _out_lock:
        _PROTO_OUT.write(line + '\n')
        _PROTO_OUT.flush()


def notify(method: str, params: dict) -> None:
    _send({'jsonrpc': '2.0', 'method': method, 'params': params})


re_secret_args = re.compile(r'(-api_key|-cookies|-headers|-proxy)(\s+|=)(\S+)')


def _redact(text: str) -> str:
    """Hides credentials in anything that may reach the UI (logs, error messages)"""
    return re_secret_args.sub(r'\1\2<REDACTED>', re_api_credentials.sub(r'\1=<REDACTED>', str(text)))


def _log_sink(text: str) -> None:
    buf: list[str] | None = getattr(_tls, 'log', None)
    text = _redact(text.rstrip('\n'))
    if buf is not None:
        buf.append(text)
    else:
        notify('log', {'text': text})


# ----- tag lists -----

def tags_dirs() -> list[pathlib.Path]:
    """Updated lists (env / data dir) first, bundled lists as fallback"""
    dirs = []
    if env := os.environ.get(ENV_TAGS_DIR):
        dirs.append(pathlib.Path(env).expanduser())
    dirs.append(data_dir() / 'tags')
    dirs.append(REPO_TAGS_DIR)
    return dirs


def _init_tags_db() -> None:
    """Env dir wins, otherwise the most recently updated list per module (cron updates repo 2tags/, the app updates data dir)"""
    TagsDB.clear()
    env_dir = os.environ.get(ENV_TAGS_DIR)
    for d in reversed(tags_dirs()):
        if not d.is_dir():
            continue
        for f in d.glob('*_tags.json'):
            module = f.name[:-len('_tags.json')]
            cur = TagsDB.DBFiles.get(module)
            if cur is None or (env_dir and d == pathlib.Path(env_dir).expanduser()) or f.stat().st_mtime > cur.stat().st_mtime:
                TagsDB.DBFiles[module] = f
        if (alias := d / 'all_tag_aliases.json').is_file() and 'all_tag_aliases.json' not in TagsDB.AuxDBFiles:
            TagsDB.AuxDBFiles['all_tag_aliases.json'] = alias


def _tag_row(name: str, info) -> dict:
    return {'tag': name, 'posts': info.posts_count, 'type': str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown'))}


# ----- helpers -----

def _module_id(abbr: str) -> int:
    Extensions.ensure_loaded()
    from .downloaders import DOWNLOADERS_BY_PROC_MODULE
    pid = ProcModule.PROC_MODULES_BY_NAME.get(abbr or '')
    if pid is None or pid not in DOWNLOADERS_BY_PROC_MODULE:
        raise RpcError(-32001, f'module \'{abbr}\' is not installed')
    return pid


def _with_downloader(abbr: str, tags: str, extra_args: list[str], fn: Callable[[Any, Any], Any]) -> Any:
    """Creates a downloader for module and runs fn(downloader, parsed_args) with log capture"""
    from .cmdargs import prepare_arglist
    from .downloaders import _make_downloader
    pid = _module_id(abbr)
    with _core_lock:
        _tls.log = []
        try:
            ProcModule.set(pid)
            args = prepare_arglist(['-module', abbr, *extra_args, *tags.split()])
            with _make_downloader(pid) as dwn:
                me = current_process()
                me.killed = False
                dwn.reset_root_thread(me)
                dwn.no_conflict_delay = True
                return fn(dwn, args)
        except SystemExit as err:  # argparse error
            raise RpcError(-32602, 'invalid arguments', {'log': _tls.log, 'code': err.code})
        except ThreadInterruptException:
            raise RpcError(-32002, 'operation failed', {'log': _tls.log})
        finally:
            log = _tls.log
            _tls.log = None
            if log:
                notify('log', {'text': '\n'.join(log), 'scope': abbr})


# ----- methods -----

def m_app_info(_: dict) -> dict:
    return {
        'name': APP_NAME, 'version': APP_VERSION, 'protocol': PROTOCOL_VERSION, 'upstream': APP_ADDRESS,
        'python': platform.python_version(), 'dataDir': data_dir().as_posix(),
        'tagsDirs': [d.as_posix() for d in tags_dirs()],
    }


def m_ext_list(_: dict) -> list[dict]:
    return Extensions.list_all()


def m_ext_install(p: dict) -> dict:
    if p.get('id'):
        m = Extensions.install_from_catalog(p['id'])
    elif p.get('url'):
        m = Extensions.install_from_url(p['url'], p.get('sha256', ''))
    elif p.get('archivePath'):
        m = Extensions.install_from_archive_file(pathlib.Path(p['archivePath']), p.get('sha256', ''))
    elif p.get('path'):
        m = Extensions.install_from_dir(pathlib.Path(p['path']))
    else:
        raise RpcError(-32602, 'id, url, archivePath or path required')
    return {**m.to_public_dict(), 'restartRequired': Extensions.restart_required(m)}


def m_ext_uninstall(p: dict) -> dict:
    Extensions.uninstall(p['id'])
    return {'ok': True}


def m_ext_remote_catalog(p: dict) -> list[dict]:
    return Extensions.fetch_remote_catalog(p.get('url'))


def m_tags_autocomplete(p: dict) -> list[dict]:
    module, prefix = p.get('module', 'rx'), str(p.get('prefix', '')).lower()
    neg = prefix.startswith('-')
    base = prefix.lstrip('-')
    if not module or len(base) < 1:
        return []
    with _core_lock:
        TagsDB._load(module)
        keys = TagsDB.DBKeys.get(module) or []
        import bisect
        lb = bisect.bisect_left(keys, base)
        matches = []
        for i in range(lb, min(len(keys), lb + 50000)):
            if not keys[i].startswith(base):
                break
            matches.append(keys[i])
        matches.sort(key=lambda k: TagsDB.DB[module][k].posts_count, reverse=True)
        limit = int(p.get('limit', 12))
        return [{**_tag_row(k, TagsDB.DB[module][k]), 'insert': f'{"-" if neg else ""}{k}'} for k in matches[:limit]]


def m_tags_browse(p: dict) -> dict:
    module = p.get('module', 'rx')
    prefix = str(p.get('prefix', '')).lower()
    types = [TagTypes[f'TAG_TYPE_{t.upper()}'] for t in p.get('types', []) if f'TAG_TYPE_{t.upper()}' in TagTypes.__members__]
    with _core_lock:
        if prefix == '#':  # anything not starting with a latin letter
            TagsDB._load(module)
            keys = [k for k in (TagsDB.DBKeys.get(module) or []) if not ('a' <= k[:1] <= 'z')]
            rows = [(k, TagsDB.DB[module][k]) for k in keys
                    if TagsDB.DB[module][k].posts_count >= int(p.get('minPosts', 0)) and (not types or TagsDB.DB[module][k].tag_type in types)]
            if p.get('sort') == 'posts':
                rows.sort(key=lambda r: r[1].posts_count, reverse=True)
            off, lim = int(p.get('offset', 0)), int(p.get('limit', 100))
            return {'total': len(rows), 'items': [_tag_row(*r) for r in rows[off:off + lim]]}
        if p.get('sort') == 'posts':
            total, rows = TagsDB.browse(module, prefix, offset=0, limit=10 ** 9, min_posts=int(p.get('minPosts', 0)), tag_types=types)
            rows.sort(key=lambda r: r[1].posts_count, reverse=True)
            off, lim = int(p.get('offset', 0)), int(p.get('limit', 100))
            return {'total': total, 'items': [_tag_row(*r) for r in rows[off:off + lim]]}
        total, rows = TagsDB.browse(module, prefix, offset=int(p.get('offset', 0)), limit=int(p.get('limit', 100)),
                                    min_posts=int(p.get('minPosts', 0)), tag_types=types)
        return {'total': total, 'items': [_tag_row(*r) for r in rows]}


def m_tags_validate(p: dict) -> dict:
    from .tags_parser import parse_tags, reset_last_tags
    abbr = p.get('module', 'rx')
    pid = _module_id(abbr)
    with _core_lock:
        ProcModule.set(pid)
        reset_last_tags()
        ok, tags = parse_tags(str(p.get('tags', '')))
        return {'valid': bool(ok), 'tags': list(tags)}


def m_tags_status(_: dict) -> list[dict]:
    rows = []
    for module, path in sorted(TagsDB.DBFiles.items()):
        st = path.stat() if path.is_file() else None
        rows.append({'module': module, 'path': path.as_posix(), 'size': st.st_size if st else 0, 'mtime': st.st_mtime if st else 0,
                     'loaded': module in TagsDB.DB})
    return rows


def m_tags_update(p: dict) -> dict:
    from .tags_updater import SUPPORTED_MODULES, update_tag_list
    module = p.get('module', '')
    if module not in SUPPORTED_MODULES:
        raise RpcError(-32602, f'tag list update is not supported for \'{module}\'')
    out_dir = pathlib.Path(p['outDir']) if p.get('outDir') else tags_dirs()[0] if os.environ.get(ENV_TAGS_DIR) else data_dir() / 'tags'
    job = f'tags-{module}'

    def run() -> None:
        try:
            res = update_tag_list(module, out_dir, api_key=p.get('apiKey', ''), delay=float(p.get('delay', 0.5)),
                                  progress=lambda msg, n: notify('tags.updateProgress', {'job': job, 'module': module, 'count': n, 'message': msg}))
            with _core_lock:
                _init_tags_db()
            notify('tags.updateDone', {'job': job, **res.as_dict()})
        except Exception as err:
            notify('tags.updateDone', {'job': job, 'module': module, 'written': False, 'message': _redact(f'failed: {err}')})

    threading.Thread(target=run, daemon=True, name=job).start()
    return {'job': job, 'outDir': out_dir.as_posix()}


def m_search_count(p: dict) -> dict:
    def fn(dwn, args) -> dict:
        dwn.launch_check_tags(args)
        return {'count': int(dwn.total_count), 'requiredTags': list(dwn.required_tags), 'cfChallenge': dwn.cf_challenge_hit}
    return _with_downloader(p.get('module', 'rx'), p.get('tags', ''), list(p.get('args', [])), fn)


def m_search_preview(p: dict) -> dict:
    """First page(s) of search results with thumbnails, local filters (required tags / negative groups) applied"""
    page = max(0, int(p.get('page', 0)))

    def fn(dwn, args) -> dict:
        dwn._parse_args(args, False)
        tasks = list(dwn.tags_str_arr)
        dwn.url = dwn._form_tags_search_address(dwn._consume_custom_module_tags(tasks[0]))
        raw = dwn.fetch_html(dwn._form_page_num_address(page))
        if raw is None:
            raise RpcError(-32003, 'unable to fetch search page', {'cfChallenge': dwn.cf_challenge_hit})
        items_raw = [str(i) for i in dwn._argswitch2(dwn._get_all_post_tags, raw.raw, raw.html, dwn._parses_raw_pages())]
        req = {t.lower() for t in dwn.required_tags}
        gender_on = bool(dwn.gender_select)
        rules = rules_for_module(dwn._get_module_abbr()) if gender_on else None
        gender_stats: dict[str, int] = {}
        blocked = 0
        hidden_no_tags = 0
        result = []
        for h in items_raw:
            h_local = dwn._local_addr_from_string(h)
            item = {'id': dwn._extract_id(h_local), 'preview': dwn.extract_preview_url(h), 'sample': _attr(h, 'sample_url'),
                    'isVideo': bool(dwn._is_video(h))}
            if dwn._can_extract_item_info_without_fetch():
                info = dwn._extract_item_info(h)
                tags = info.tags.split(' ') if info.tags else []
                item.update({'tags': tags, 'score': info.score, 'width': info.width, 'height': info.height, 'ext': info.ext,
                             'date': info.date, 'rating': _attr(h, 'rating')})
                tl = {t.lower() for t in tags}
                if gender_on and blocked_tag(tags):
                    blocked += 1  # safety: never returned, not even a thumbnail url
                    continue
                excluded_by = []
                if req and not req.issubset(tl):
                    excluded_by.append('required')
                if any(all(any(pt.fullmatch(t) for t in tl) for pt in pl) for pl in dwn.neg_and_groups):
                    excluded_by.append('neg')
                if rules is not None:
                    res = infer_from_tags(tags, rules)
                    keep, eff = decide(res, dwn.gender_select, dwn.gender_min_conf, dwn.gender_strict)
                    item['gender'] = {**res.as_dict(), 'effective': str(eff)}
                    gender_stats[str(eff)] = gender_stats.get(str(eff), 0) + 1
                    if not keep:
                        excluded_by.append('gender')
                item['excluded'] = bool(excluded_by)
                item['excludedBy'] = excluded_by
            elif gender_on:
                # tags are unknown without fetching each post: the safety blacklist cannot be checked -> fail closed,
                # the post (and its thumbnail url) is not returned at all
                hidden_no_tags += 1
                continue
            result.append(item)
        return {'page': page, 'pageSize': dwn._get_items_per_page(), 'items': result, 'tasks': tasks,
                'requiredTags': list(dwn.required_tags), 'site': dwn._get_sitename(),
                'genderStats': gender_stats, 'blockedCount': blocked, 'hiddenNoTags': hidden_no_tags, 'genderEnabled': gender_on}
    return _with_downloader(p.get('module', 'rx'), p.get('tags', ''), list(p.get('args', [])), fn)


def _attr(h: str, name: str) -> str:
    key = f' {name}="'
    i = h.find(key)
    return h[i + len(key):h.find('"', i + len(key))] if i >= 0 else ''


def m_files_nonempty(p: dict) -> dict:
    path = pathlib.Path(p.get('path', ''))
    return {'exists': path.is_dir(), 'nonEmpty': path.is_dir() and next(path.iterdir(), None) is not None}


METHODS: dict[str, Callable[[dict], Any]] = {
    'app.info': m_app_info,
    'ext.list': m_ext_list,
    'ext.install': m_ext_install,
    'ext.uninstall': m_ext_uninstall,
    'ext.remoteCatalog': m_ext_remote_catalog,
    'tags.autocomplete': m_tags_autocomplete,
    'tags.browse': m_tags_browse,
    'tags.validate': m_tags_validate,
    'tags.status': m_tags_status,
    'tags.update': m_tags_update,
    'search.count': m_search_count,
    'search.preview': m_search_preview,
    'files.nonEmpty': m_files_nonempty,
}


def _handle(req: dict) -> None:
    rid = req.get('id')
    method = req.get('method', '')
    try:
        fn = METHODS.get(method)
        if fn is None:
            raise RpcError(-32601, f'method not found: {method}')
        result = fn(req.get('params') or {})
        if rid is not None:
            _send({'jsonrpc': '2.0', 'id': rid, 'result': result})
    except RpcError as err:
        if rid is not None:
            data = err.data
            if isinstance(data, dict) and isinstance(data.get('log'), list):
                data = {**data, 'log': [_redact(x) for x in data['log']]}
            _send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': err.code, 'message': _redact(err.message), 'data': data}})
    except Exception as err:
        if rid is not None:
            _send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': _redact(str(err) or type(err).__name__),
                                                            'data': {'trace': _redact(traceback.format_exc(limit=6))}}})


def main() -> int:
    Logger.init(False)
    Logger.append_to_window_proc = _log_sink
    Extensions.ensure_loaded()
    _init_tags_db()
    notify('ready', m_app_info({}))
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix='rpc') as pool:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
                continue
            if not isinstance(req, dict):  # batches / scalars are not supported
                _send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}})
                continue
            if req.get('method') == 'app.shutdown':
                break
            pool.submit(_handle, req)
    return 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
