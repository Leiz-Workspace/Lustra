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

import inspect
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
from urllib import parse as url_parse

# protocol channel must stay clean: everything else printed to stdout goes to stderr
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

from .defines import ThreadInterruptException  # noqa: E402
from .extensions import ENV_CATALOG_AUTOLOAD, Extensions, data_dir  # noqa: E402
from .gender import decide, infer_from_tags, rules_for_module  # noqa: E402
from .help import APP_ADDRESS  # noqa: E402
from .logger import Logger  # noqa: E402
from .module import ProcModule  # noqa: E402
from .rex import re_api_credentials  # noqa: E402
from .safety import blocked_tag  # noqa: E402
from .tags import TAG_NAMES_BY_TYPE, TagTypes  # noqa: E402
from .tagsdb import TAG_ALIASES, TagsDB, is_wtag, load_tag_aliases, wildcard_pattern  # noqa: E402
from .task import KNOWN_META_NAMES, re_meta_name  # noqa: E402
from .vcs import APP_NAME, APP_VERSION  # noqa: E402

__all__ = ('main',)

PROTOCOL_VERSION = 1
ENV_TAGS_DIR = 'LUSTRA_TAGS_DIR'
REPO_TAGS_DIR = pathlib.Path(__file__).resolve().parent.parent / '2tags'

_out_lock = threading.Lock()
_core_lock = threading.RLock()
'''core keeps current module in global state: module-dependent calls are serialized.
Tag list methods (autocomplete, browse, status, update) do not take it: TagsDB has its own lock and is keyed by module'''
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
    TagsDB.locate_default_lists()


AUTOCOMPLETE_SCAN_MAX = 50000
'''autocomplete ranks at most this many prefix matches (alphabetically first ones)'''


TAG_TYPES_BY_NAME: dict[str, TagTypes] = {**{str(v): k for k, v in TAG_NAMES_BY_TYPE.items()}, 'meta': TagTypes.TAG_TYPE_META}
'''UI type names ('metadata', 'species'...) -> tag types. Ruxx enum names differ ('TAG_TYPE_META'): metadata filter did nothing'''


def _tag_row(name: str, info) -> dict:
    return {'tag': name, 'posts': info.posts_count, 'type': str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown'))}


RESOLVE_TAGS_MAX = 64
FUZZY_LIMIT_MAX = 50
RELATED_LIMIT_MAX = 200
PREVIEW_PAGE_SIZE_DEFAULT = 200
'''default preview page size cap (rule34 lists 1000 posts per api page)'''


def _plain_tag(raw: Any) -> str:
    """User input -> tag name: trimmed, lowercase, spaces as underscores, no negation"""
    return '_'.join(str(raw).strip().lower().split()).lstrip('-')


def _is_metatag(tag: str) -> bool:
    m = re_meta_name.match(tag)
    return bool(m and m.group(1) in KNOWN_META_NAMES)


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
            args = prepare_arglist(['-module', abbr, *extra_args, '--', *tags.split()])
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
    """Thread-safe on its own (TagsDB lock): never waits for _core_lock, so it stays responsive while a preview / count runs"""
    module, prefix = p.get('module', 'rx'), str(p.get('prefix', '')).lower()
    neg = prefix.startswith('-')
    base = prefix.lstrip('-')
    if not module or len(base) < 1:
        return []
    limit = int(p.get('limit', 12))
    matches = TagsDB.top_matches(module, base, limit, scan_max=AUTOCOMPLETE_SCAN_MAX)
    return [{**_tag_row(k, info), 'insert': f'{"-" if neg else ""}{k}'} for k, info in matches]


def m_tags_browse(p: dict) -> dict:
    """Same locking as tags.autocomplete. Popularity sorted / filtered results are cached by TagsDB, paging reuses them"""
    module = p.get('module', 'rx')
    prefix = str(p.get('prefix', '')).lower()
    types = [TAG_TYPES_BY_NAME[str(t).lower()] for t in p.get('types', []) if str(t).lower() in TAG_TYPES_BY_NAME]
    total, rows = TagsDB.browse(module, prefix, offset=int(p.get('offset', 0)), limit=int(p.get('limit', 100)), min_posts=int(p.get('minPosts', 0)),
                                tag_types=types, by_posts=p.get('sort') == 'posts', non_alpha=prefix == '#')  # '#': not starting with a latin letter
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


def m_tags_resolve(p: dict) -> dict:
    """Alias, existence, posts count and type of each tag (one item per input tag, same order, at most RESOLVE_TAGS_MAX).
    all_tag_aliases.json is a cross-site normalization map (Ruxx file tagging: 'oral_sex' -> 'oral', 'male' -> '1+boys'), not the
    site's own aliases: a tag present in the module list is never rewritten, the alias is used only when it leads to a listed tag.
    Wildcard patterns: type 'wildcard', known when some listed tag matches. Same locking as tags.autocomplete"""
    module = p.get('module', 'rx')
    raw = p.get('tags') or []
    tags = [_plain_tag(t) for t in (raw.split() if isinstance(raw, str) else list(raw))[:RESOLVE_TAGS_MAX]]
    load_tag_aliases()
    has_list = TagsDB.count(module) > 0
    items = []
    for tag in tags:
        row = {'tag': tag, 'canonical': tag, 'alias': False, 'posts': 0, 'type': 'unknown', 'known': False}
        items.append(row)
        if not tag:
            continue
        if _is_metatag(tag):
            row.update(type='meta', known=True)
            continue
        info = TagsDB.find_tag(module, tag)
        if info is None and (target := TAG_ALIASES.get(tag)) and target != tag and (target_info := TagsDB.find_tag(module, target)):
            info = target_info
            row.update(canonical=target, alias=True)
        if info is not None:
            row.update(known=True, posts=info.posts_count, type=str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown')))
        elif is_wtag(tag):
            row.update(type='wildcard', known=has_list and TagsDB.has_wildcard_match(module, tag))
    return {'items': items, 'hasList': has_list}


def m_tags_fuzzy(p: dict) -> list[dict]:
    """Typo tolerant suggestions: up to 2 edits (1 for texts shorter than 6 chars), missing / extra '_' free ('catears' -> 'cat_ears',
    distance 0), ranked by distance then posts. Same locking as tags.autocomplete; the index is built on the first call per list"""
    module = p.get('module', 'rx')
    limit = max(1, min(FUZZY_LIMIT_MAX, int(p.get('limit') or 8)))
    return [{**_tag_row(name, info), 'distance': dist} for name, info, dist in TagsDB.fuzzy_matches(module, _plain_tag(p.get('text', '')), limit)]


def m_tags_status(_: dict) -> list[dict]:
    rows = []
    for module, path in sorted(TagsDB.files().items()):
        st = path.stat() if path.is_file() else None
        rows.append({'module': module, 'path': path.as_posix(), 'size': st.st_size if st else 0, 'mtime': st.st_mtime if st else 0,
                     'loaded': TagsDB.is_loaded(module)})
    return rows


_tags_updates_lock = threading.Lock()
_tags_updates: set[str] = set()


def m_tags_update(p: dict) -> dict:
    from .tags_updater import SUPPORTED_MODULES, update_tag_list
    module = p.get('module', '')
    if module not in SUPPORTED_MODULES:
        raise RpcError(-32602, f'tag list update is not supported for \'{module}\'')
    out_dir = pathlib.Path(p['outDir']) if p.get('outDir') else tags_dirs()[0] if os.environ.get(ENV_TAGS_DIR) else data_dir() / 'tags'
    job = f'tags-{module}'
    with _tags_updates_lock:
        if module in _tags_updates:
            raise RpcError(-32004, f'tag list update for \'{module}\' is already running')
        _tags_updates.add(module)
    reference = TagsDB.get_file(module)

    def run() -> None:
        try:
            res = update_tag_list(module, out_dir, api_key=p.get('apiKey', ''), delay=float(p.get('delay', 0.5)), reference=reference,
                                  progress=lambda msg, n: notify('tags.updateProgress', {'job': job, 'module': module, 'count': n, 'message': msg}))
            if res.written:
                TagsDB.locate_default_lists(reload=(module,))  # TagsDB locks itself: readers keep the old list until this swap
            notify('tags.updateDone', {'job': job, 'ok': True, **res.as_dict()})
        except Exception as err:
            notify('tags.updateDone', {'job': job, 'module': module, 'ok': False, 'written': False, 'message': _redact(f'failed: {err}')})
        finally:
            with _tags_updates_lock:
                _tags_updates.discard(module)

    threading.Thread(target=run, daemon=True, name=job).start()
    return {'job': job, 'outDir': out_dir.as_posix()}


def m_search_count(p: dict) -> dict:
    def fn(dwn, args) -> dict:
        dwn.launch_check_tags(args)
        if dwn.launch_error or dwn.interrupted:
            raise RpcError(-32002, dwn.launch_error or 'cancelled', {'log': list(_tls.log or []), 'cfChallenge': dwn.cf_challenge_hit})
        counts = list(dwn.task_counts)
        return {'count': int(dwn.total_count), 'taskCounts': counts, 'approximate': len(counts) > 1 or bool(dwn.required_tags),
                'requiredTags': list(dwn.required_tags), 'cfChallenge': dwn.cf_challenge_hit}
    return _with_downloader(p.get('module', 'rx'), p.get('tags', ''), list(p.get('args', [])), fn)


_maxlim_support: dict[type, bool] = {}


def _takes_page_size(cls: type) -> bool:
    """Whether a downloader's search address takes a page size (gelbooru based, danbooru, e621, paheal; not rule34.us / rule34hentai)"""
    if (ok := _maxlim_support.get(cls)) is None:
        try:
            ok = 'maxlim' in inspect.signature(cls._form_tags_search_address).parameters
        except (TypeError, ValueError):
            ok = False
        _maxlim_support[cls] = ok
    return ok


def _results_page_size(dwn, requested: Any = None) -> tuple[int, int | None]:
    """(page size, maxlim for _form_tags_search_address). Default min(module page size, PREVIEW_PAGE_SIZE_DEFAULT), at most the module
    page size. Favorites / pool pages (their page addresses are post offsets in module page units) and modules whose search address
    takes no page size keep the module page size"""
    module_size = max(1, int(dwn._get_items_per_page()))
    if dwn.favorites_search_user or dwn.pool_search_str or not _takes_page_size(type(dwn)):
        return module_size, None
    try:
        size = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        size = 0
    size = min(module_size, size if size > 0 else PREVIEW_PAGE_SIZE_DEFAULT)
    return size, size


def _id_order(item_id: str) -> tuple[int, str]:
    return (int(item_id), '') if item_id.isdigit() else (-1, item_id)


def _fetch_results_page(dwn, page: int, page_size: int, maxlim: int | None) -> tuple[list[str], bool]:
    """Raw items of page `page` of every task (OR groups split into tasks: one request per task), deduplicated by id.
    Several tasks: merged newest first (multi-task queries always use the default id order). Second value: some task gave a full page"""
    tasks = list(dwn.tags_str_arr) or ['']
    by_id: dict[str, str] = {}
    has_more = False
    for task in tasks:
        tags = dwn._consume_custom_module_tags(task)
        dwn.url = dwn._form_tags_search_address(tags) if maxlim is None else dwn._form_tags_search_address(tags, maxlim)
        raw = dwn.fetch_html(dwn._form_page_num_address(page))
        if raw is None:
            raise RpcError(-32003, 'unable to fetch search page', {'cfChallenge': dwn.cf_challenge_hit})
        page_items = dwn._get_all_post_tags(raw.raw if dwn._parses_raw_pages() else raw.html)  # json pages: no html parsing
        has_more = has_more or len(page_items) >= page_size
        for h in map(str, page_items):
            by_id.setdefault(dwn._extract_id(dwn._local_addr_from_string(h)), h)
    if len(tasks) > 1:
        return [by_id[i] for i in sorted(by_id, key=_id_order, reverse=True)], has_more
    return list(by_id.values()), has_more


class _ResultsFilter:
    """Local filters of a results page, as the download applies them: safety blacklist (gender filter on), required tags,
    negative groups, gender. Counts what it hides"""

    def __init__(self, dwn) -> None:
        self.dwn = dwn
        self.required = {t.lower() for t in dwn.required_tags}
        self.gender_on = bool(dwn.gender_select)
        self.rules = rules_for_module(dwn._get_module_abbr()) if self.gender_on else None
        self.with_tags = bool(dwn._can_extract_item_info_without_fetch())
        self.gender_stats: dict[str, int] = {}
        self.blocked = 0
        self.hidden_no_tags = 0

    def item(self, h: str) -> dict | None:
        """Preview item, None if it must not be shown at all"""
        dwn = self.dwn
        item = {'id': dwn._extract_id(dwn._local_addr_from_string(h)), 'preview': dwn.extract_preview_url(h), 'sample': _attr(h, 'sample_url'),
                'isVideo': bool(dwn._is_video(h))}
        if not self.with_tags:
            if self.gender_on:
                # tags are unknown without fetching each post: the safety blacklist cannot be checked -> fail closed,
                # the post (and its thumbnail url) is not returned at all
                self.hidden_no_tags += 1
                return None
            return item
        info = dwn._extract_item_info(h)
        tags = info.tags.split(' ') if info.tags else []
        item.update({'tags': tags, 'score': info.score, 'width': info.width, 'height': info.height, 'ext': info.ext,
                     'date': info.date, 'rating': _attr(h, 'rating')})
        if self.gender_on and blocked_tag(tags):
            self.blocked += 1  # safety: never returned, not even a thumbnail url
            return None
        tl = {t.lower() for t in tags}
        excluded_by = []
        if self.required and not self.required.issubset(tl):
            excluded_by.append('required')
        if any(all(any(pt.fullmatch(t) for t in tl) for pt in pl) for pl in dwn.neg_and_groups):
            excluded_by.append('neg')
        if self.rules is not None:
            res = infer_from_tags(tags, self.rules)
            keep, eff = decide(res, dwn.gender_select, dwn.gender_min_conf, dwn.gender_strict)
            item['gender'] = {**res.as_dict(), 'effective': str(eff)}
            self.gender_stats[str(eff)] = self.gender_stats.get(str(eff), 0) + 1
            if not keep:
                excluded_by.append('gender')
        item['excluded'] = bool(excluded_by)
        item['excludedBy'] = excluded_by
        return item


def m_search_preview(p: dict) -> dict:
    """Page `page` of search results with thumbnails, local filters (required tags / negative groups) applied.
    pageSize: see _results_page_size. Multi-task queries: page `page` of every task, merged (see _fetch_results_page)"""
    page = max(0, int(p.get('page', 0)))

    def fn(dwn, args) -> dict:
        dwn._parse_args(args, False)
        page_size, maxlim = _results_page_size(dwn, p.get('pageSize'))
        items_raw, has_more = _fetch_results_page(dwn, page, page_size, maxlim)
        filt = _ResultsFilter(dwn)
        result = [item for item in map(filt.item, items_raw) if item is not None]
        return {'page': page, 'pageSize': page_size, 'hasMore': has_more, 'items': result, 'tasks': list(dwn.tags_str_arr),
                'requiredTags': list(dwn.required_tags), 'site': dwn._get_sitename(), 'genderStats': filt.gender_stats,
                'blockedCount': filt.blocked, 'hiddenNoTags': filt.hidden_no_tags, 'genderEnabled': filt.gender_on}
    return _with_downloader(p.get('module', 'rx'), p.get('tags', ''), list(p.get('args', [])), fn)


def _query_tag_matchers(query: str) -> tuple[set[str], list[re.Pattern]]:
    """Tags of a query, negated ones and members of '(a~b)' / '-(a,b)' groups included: (plain names, wildcard patterns)"""
    names: set[str] = set()
    for word in query.lower().split():
        word = word.lstrip('-')
        if len(word) > 2 and word[0] == word[-1] == '%':  # escaped tag
            names.add(word[1:-1])
        elif word.startswith('(') and word.endswith(')') and ('~' in word or ',' in word):
            names.update(t.lstrip('-') for t in re.split(r'[~,]', word[1:-1]))
        else:
            names.add(word)
    names.discard('')
    return names, [wildcard_pattern(n) for n in names if is_wtag(n)]


def m_search_related(p: dict) -> dict:
    """Tags co-occurring in the first results page of the query (posts kept by the local filters), the query's own tags and metatags
    excluded. count: sampled posts having the tag; order: count desc, then rarer overall (TagsDB posts asc), then name.
    Uses the posts' tags of the listing: modules without tags in listings give nothing (no extra requests)"""
    module = p.get('module', 'rx')
    query = str(p.get('tags', ''))
    limit = max(1, min(RELATED_LIMIT_MAX, int(p.get('limit') or 30)))

    def fn(dwn, args) -> tuple[dict[str, int], int]:
        dwn._parse_args(args, False)
        if not dwn._can_extract_item_info_without_fetch():
            return {}, 0
        page_size, maxlim = _results_page_size(dwn)
        items_raw, _ = _fetch_results_page(dwn, 0, page_size, maxlim)
        filt = _ResultsFilter(dwn)
        names, patterns = _query_tag_matchers(query)
        counts: dict[str, int] = {}
        sample = 0
        for h in items_raw:
            item = filt.item(h)
            if item is None or item.get('excluded'):
                continue
            sample += 1
            for tag in {t.lower() for t in item.get('tags', ()) if t}:
                if tag not in names and not _is_metatag(tag) and not any(pt.fullmatch(tag) for pt in patterns):
                    counts[tag] = counts.get(tag, 0) + 1
        return counts, sample

    counts, sample = _with_downloader(module, query, list(p.get('args', [])), fn)
    # tag list lookups after the core lock is released (a cold list load must not block other searches)
    rows = []
    for tag, count in counts.items():
        info = TagsDB.find_tag(module, tag)
        rows.append({'tag': tag, 'count': count, 'posts': info.posts_count if info else 0,
                     'type': str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown')) if info else 'unknown'})
    rows.sort(key=lambda r: (-r['count'], r['posts'], r['tag']))
    return {'items': rows[:limit], 'sample': sample}


def _attr(h: str, name: str) -> str:
    key = f' {name}="'
    i = h.find(key)
    return h[i + len(key):h.find('"', i + len(key))] if i >= 0 else ''


def resolve_user_path(raw: str) -> pathlib.Path:
    """'~/x' and relative paths are resolved against the home folder (the desktop app does the same for jobs)"""
    path = pathlib.Path(str(raw).strip() or '.').expanduser()
    return path if path.is_absolute() else pathlib.Path.home() / path


# ----- search by file name / post link -----

FILES_IDENTIFY_MAX = 500
ID_LIST_MODULES = frozenset(('db',))
'''modules accepting lists in one metatag: 'id:1,2,3' / 'md5:a,b' (danbooru, checked 2026-11-17); others get an OR group'''
ID_LIST_CHUNK = 100
re_scheme = re.compile(r'^[a-z][a-z0-9+.-]*://', re.IGNORECASE)
re_md5_token = re.compile(r'(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])')
re_prefixed_id = re.compile(r'^([a-z0-9]{2,4})_(\d+)(?:[_.(\s-]|$)')
re_plain_id = re.compile(r'^(\d+)(?:[_.(\s-]|$)')
re_danbooru_download = re.compile(r'^__.+__[0-9a-f]{32}$')
POST_URL_PATTERNS = (
    re.compile(r'/posts?/(?:view/|show/)?(\d+)(?:[/?#]|$)'),  # danbooru, e621 /posts/N; shimmie /post/view/N
    re.compile(r'[?&]id=(\d+)(?:&|$)'),  # gelbooru 'page=post&s=view&id=N', rule34.us 'r=posts/view&id=N'
)


def _site_hosts() -> dict[str, str]:
    """host -> module abbr of every known module (builtin, installed and catalog extensions)"""
    hosts: dict[str, str] = {}
    for row in Extensions.list_all():
        site = str(row.get('site') or '')
        abbr = str(row.get('abbr') or '')
        host = (url_parse.urlparse(site).hostname or '').lower()
        if host and abbr:
            # full host and its base domain: file links come from cdn hosts (cdn.donmai.us, wimg.rule34.xxx)
            for h in (host, host.removeprefix('www.'), host.removeprefix('api.'), '.'.join(host.split('.')[-2:])):
                hosts.setdefault(h, abbr)
    return hosts


def _host_module(host: str, hosts: dict[str, str]) -> str:
    host = host.lower()
    return hosts.get(host) or hosts.get('.'.join(host.split('.')[-2:]), '')


def identify_file_name(raw: str, module: str, hosts: dict[str, str]) -> dict:
    """One pasted / dropped item: a file name (Lustra 'db_12345_score(3).jpg' or '12345.jpg', booru original '<md5>.jpg',
    danbooru download '__tags__<md5>.jpg'), a path, or a post / file link. Returns {name, module, kind: 'id'|'md5'|'', value, note}"""
    s = str(raw).strip().strip('"\'')
    item = {'name': s, 'module': module, 'kind': '', 'value': '', 'note': ''}
    known = ProcModule.PROC_MODULES_BY_NAME
    if re_scheme.match(s):
        u = url_parse.urlparse(s)
        if host_module := _host_module(u.hostname or '', hosts):
            item['module'] = host_module
        last = u.path.rsplit('/', 1)[-1].lower()
        if m := re_md5_token.search(last):  # file links: cdn.donmai.us/original/0a/1b/<md5>.jpg
            item.update(kind='md5', value=m.group(1))
            return item
        target = f'{u.path}?{u.query}'
        for rx in POST_URL_PATTERNS:
            if m := rx.search(target):
                item.update(kind='id', value=m.group(1))
                if not host_module:
                    item['note'] = 'unknown site, current module assumed'
                return item
        item['note'] = 'no post id or md5 in the link'
        return item
    name = s.replace('\\', '/').rsplit('/', 1)[-1]
    stem = (name.rsplit('.', 1)[0] if '.' in name.lstrip('.') else name).lower()
    if '!info_' in stem:
        item['note'] = 'info list file, not a post'
    elif (m := re_prefixed_id.match(stem)) and m.group(1) in known:
        item.update(module=m.group(1), kind='id', value=str(int(m.group(2))))
    elif m := re_plain_id.match(stem):
        item.update(kind='id', value=str(int(m.group(1))))
    elif m := re_md5_token.search(stem):
        item.update(kind='md5', value=m.group(1))
        if re_danbooru_download.match(stem) and 'db' in known:
            item['module'] = 'db'
    else:
        item['note'] = 'not recognized'
    return item


def build_file_queries(items: list[dict]) -> list[dict]:
    """Recognized items -> one query per module: id / md5 lists (danbooru) or an OR group of metatags (one search per item,
    at most task.MAX_SPLIT_TASKS). Queries are ordered by number of items"""
    from .task import MAX_SPLIT_TASKS
    by_module: dict[str, dict[str, list[str]]] = {}
    for it in items:
        if it['kind']:
            vals = by_module.setdefault(it['module'], {'id': [], 'md5': []})[it['kind']]
            if it['value'] not in vals:
                vals.append(it['value'])
    queries = []
    for module, v in by_module.items():
        terms: list[str] = []
        if module in ID_LIST_MODULES:
            for kind in ('id', 'md5'):
                for i in range(0, len(v[kind]), ID_LIST_CHUNK):
                    terms.append(f'{kind}:{",".join(v[kind][i:i + ID_LIST_CHUNK])}')
        else:
            terms = [f'id:{x}' for x in v['id']] + [f'md5:{x}' for x in v['md5']]
        total = len(v['id']) + len(v['md5'])
        dropped = 0
        if len(terms) > MAX_SPLIT_TASKS:
            dropped = len(terms) - MAX_SPLIT_TASKS if module not in ID_LIST_MODULES else 0
            terms = terms[:MAX_SPLIT_TASKS]
        tags = terms[0] if len(terms) == 1 else f'({"~".join(terms)})'
        queries.append({'module': module, 'tags': tags, 'count': total - dropped, 'dropped': dropped})
    queries.sort(key=lambda q: -q['count'])
    return queries


def m_files_identify(p: dict) -> dict:
    """{names: [...], module} -> {items: [{name, module, kind, value, note}], queries: [{module, tags, count, dropped}]}"""
    module = str(p.get('module') or 'rx')
    names = p.get('names') or []
    if isinstance(names, str):
        names = names.splitlines()
    names = [str(x) for x in names if str(x).strip()][:FILES_IDENTIFY_MAX]
    hosts = _site_hosts()
    items = [identify_file_name(n, module, hosts) for n in names]
    return {'items': items, 'queries': build_file_queries(items), 'max': FILES_IDENTIFY_MAX}


def m_files_nonempty(p: dict) -> dict:
    path = resolve_user_path(p.get('path', ''))
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
    'tags.resolve': m_tags_resolve,
    'tags.fuzzy': m_tags_fuzzy,
    'tags.status': m_tags_status,
    'tags.update': m_tags_update,
    'search.count': m_search_count,
    'search.preview': m_search_preview,
    'search.related': m_search_related,
    'files.nonEmpty': m_files_nonempty,
    'files.identify': m_files_identify,
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
    os.environ[ENV_CATALOG_AUTOLOAD] = '0'  # only builtin and installed extensions are usable from the app
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
