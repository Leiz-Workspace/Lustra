# coding=UTF-8
"""
Lustra extension generator: manifest.json and downloader.py for engines with a known API (plan B.4, QĐ2).
Rendering is plain, controlled string substitution of validated values (no template engine executing code).
Other engines get a downloader.py written by the AI (or a developer) and checked by policy + sandbox tests.
"""
#########################################
#
#

from __future__ import annotations

import datetime
import json
import re

from lustra.module import ProcModule
from lustra.vcs.version import APP_VERSION

from .fetch import host_of

__all__ = ('TEMPLATE_ENGINES', 'make_manifest', 'render_downloader', 'suggest_names')

re_id = re.compile(r'^[a-z][a-z\d_-]{1,31}$')
re_abbr = re.compile(r'^[a-z][a-z\d]{1,3}$')
re_safe_name = re.compile(r'^[\w .,()\'&+-]{1,64}$')

GELBOORU_SYNTAX = {
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300,
    'metaCount': ':(?:(?:[<>]=?|=)?[a-z\\d\\-_]+?|[a-z\\d_]+:[a-z\\d_]+?)',
    'sort': 'sort(?::[^:]+?){1,2}', 'fav': 'favorited_by:\\d+?', 'pool': 'pool:\\d+?',
}
DANBOORU_SYNTAX = {
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'maxTags': 2, 'maxTagsSeparate': False, 'maxWildcards': 0,
    'freeMetaPrefixes': ['rating:', 'id:', 'date:', 'score:', 'status:', 'parent:'], 'localPositiveOverflow': True,
    'metaCount': ':(?:[<>]=?|!)?[a-z\\d\\-_.,]+?', 'sort': 'order:[a-z_]+', 'fav': 'favorited_by:[^:]+?', 'pool': 'pool:[^:]+?',
}
TEMPLATE_ENGINES = ('gelbooru_dapi', 'danbooru_json')


def _taken_abbrs() -> set[str]:
    """abbreviations of builtin, installed and catalog modules"""
    from lustra.extensions import Extensions
    Extensions.ensure_loaded(include_catalog=True)
    return set(ProcModule.PROC_MODULES_BY_NAME)


def known_extensions() -> list[dict]:
    """builtin, installed and catalog extensions: id, name, abbr, site, installed"""
    from lustra.extensions import Extensions
    return [{'id': r['id'], 'name': r.get('name', ''), 'abbr': r.get('abbr', ''), 'site': r.get('site', ''), 'installed': bool(r.get('installed'))}
            for r in Extensions.list_all()]


def existing_extensions(site: str) -> list[dict]:
    """known extensions for the same site (same host or parent domain): a new draft would duplicate them"""
    host = host_of(site)
    base = '.'.join(host.split('.')[-2:])
    out = []
    for e in known_extensions():
        h = host_of(e['site']) if e['site'] else ''
        if h and (h == host or '.'.join(h.split('.')[-2:]) == base):
            out.append(e)
    return out


def suggest_names(site: str) -> dict:
    """id / abbr / name proposals for a site, id and abbr not used by any known extension / module"""
    host = host_of(site)
    labels = [x for x in host.split('.') if x not in ('www', 'api')]
    core = labels[0] if len(labels) > 1 else (labels[0] if labels else 'site')
    ext_id = re.sub(r'[^a-z\d_-]', '', core.lower()) or 'site'
    if not ext_id[0].isalpha():
        ext_id = f's{ext_id}'
    ext_id = ext_id[:32]
    taken_ids = {e['id'] for e in known_extensions()}
    if ext_id in taken_ids:
        ext_id = next((f'{ext_id[:30]}{n}' for n in range(2, 100) if f'{ext_id[:30]}{n}' not in taken_ids), ext_id)
    taken = _taken_abbrs()
    letters = re.sub(r'[^a-z\d]', '', ext_id) or 'site'
    candidates = [letters[:2], letters[0] + letters[-1], letters[:3], letters[0] + letters[2:4]]
    candidates += [f'{letters[0]}{d}' for d in range(10)] + [f'{letters[:2]}{d}' for d in range(10)]
    abbr = next((c for c in candidates if re_abbr.fullmatch(c) and c not in taken), '')
    name = core.replace('-', ' ').replace('_', ' ').title()[:64] or 'Site'
    return {'id': ext_id if re_id.fullmatch(ext_id) else 'site', 'abbr': abbr, 'name': name}


def validate_names(ext_id: str, abbr: str, name: str) -> None:
    if not re_id.fullmatch(ext_id):
        raise ValueError(f'invalid id \'{ext_id}\' (lowercase letters, digits, _ -, 2-32 chars, starting with a letter)')
    if not re_abbr.fullmatch(abbr):
        raise ValueError(f'invalid abbr \'{abbr}\' (2-4 lowercase letters / digits, starting with a letter)')
    if ext_id in {e['id'] for e in known_extensions()}:
        raise ValueError(f'id \'{ext_id}\' is already used by another extension (builtin, installed or catalog)')
    if abbr in _taken_abbrs():
        raise ValueError(f'abbr \'{abbr}\' is already used by another module')
    if not re_safe_name.fullmatch(name):
        raise ValueError(f'invalid name \'{name}\'')


def class_name(abbr: str) -> str:
    return f'Downloader{abbr.capitalize()}'


def make_manifest(*, ext_id: str, abbr: str, name: str, site: str, engine: str, media: list[str], description: str = '',
                  syntax: dict | None = None, api_key: bool = False, generator: dict | None = None) -> dict:
    """manifest.json content. '$generator' records the origin (modspec ignores '$' keys)"""
    m = {
        'id': ext_id, 'name': name, 'abbr': abbr, 'version': '1.0.0', 'apiVersion': 1, 'kind': 'downloader', 'default': False,
        'site': site, 'media': [x for x in media if x in ('image', 'video', 'audio')] or ['image', 'video'],
        'entry': f'downloader.py:{class_name(abbr)}', 'description': description[:300],
        'syntax': syntax if syntax is not None else (GELBOORU_SYNTAX if engine == 'gelbooru_dapi' else DANBOORU_SYNTAX),
        '$generator': {'tool': 'lustra.extgen', 'toolVersion': APP_VERSION, 'engine': engine, 'sourceUrl': site,
                       'createdAt': datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ'), **(generator or {})},
    }
    if api_key:
        m['auth'] = {'apiKey': True, 'apiKeyFormat': 'API_KEY,USER_ID' if engine == 'gelbooru_dapi' else 'API_KEY,LOGIN', 'cookies': []}
    return m


_HEADER = '''# coding=UTF-8
"""
Lustra extension '{ext_id}' ({abbr}) for {site}
Generated by lustra.extgen from the '{engine}' template on {date}. Review before installing.
"""
#########################################
#
#
'''

_GELBOORU = _HEADER + '''
import re
from typing import Never, final

from lustra.defines import ModuleCaps
from lustra.download_gelbooru import DownloaderGelbooru
from lustra.rex import re_tags_exclude_rx, re_tags_to_process_rx

__all__ = ('{cls}',)

SITENAME = {site_lit}
MODULE_ABBR = {abbr_lit}
FILE_NAME_PREFIX = f'{{MODULE_ABBR}}_'
ITEMS_PER_PAGE = {per_page}  # dapi 'limit' (gelbooru 0.2 sites accept 100 at least)
ITEMS_PER_PAGE_F = 50  # favorites html page
ITEMS_PER_PAGE_P = 20000  # pool html page: all posts at once
MAX_SEARCH_DEPTH = 200000 + ITEMS_PER_PAGE - 1
re_post_page = re.compile(r'^(?:\\?page=post&(?:amp;)?s=list|document\\.location=\\'\\?page=favorites|\\?page=pool&(?:amp;)?s=show)&.+?$')


@final
class {cls}(DownloaderGelbooru):
    CAPABILITIES = frozenset((ModuleCaps.PARENT_CHILD, ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW{api_caps}))

    def __init__(self) -> None:
        super().__init__()

    def _get_min_request_interval(self) -> float:
        return {interval}  # polite floor for a site without a published rate limit

    def _get_id_bounds(self) -> Never:
        raise NotImplementedError

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _get_re_post_page(self) -> re.Pattern:
        return re_post_page

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX

    def _get_items_per_page(self) -> int:
        return ITEMS_PER_PAGE_F if self.favorites_search_user else ITEMS_PER_PAGE_P if self.pool_search_str else ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return MAX_SEARCH_DEPTH

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return '+'

    def _get_idval_equal_seaparator(self) -> str:
        return ':'

    def _extract_comments(self, item_id: str) -> None:
        return  # comments are not collected (capability not declared)
{api_key_method}
#
#
#########################################
'''

_GELBOORU_API_KEY = '''
    def _get_api_key(self) -> str:
        return f'&api_key={self.api_key.key}&user_id={self.api_key.user_id}' if self.api_key else ''
'''

_DANBOORU = _HEADER + '''
from typing import final

from lustra.download_db import DownloaderDb

__all__ = ('{cls}',)

SITENAME = {site_lit}
MODULE_ABBR = {abbr_lit}
FILE_NAME_PREFIX = f'{{MODULE_ABBR}}_'


@final
class {cls}(DownloaderDb):
    """Danbooru compatible API: everything but the address and the module name is the builtin danbooru downloader"""

    def _get_min_request_interval(self) -> float:
        return {interval}  # polite floor for a site without a published rate limit

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX

#
#
#########################################
'''


def render_downloader(engine: str, *, ext_id: str, abbr: str, site: str, api_key: bool = False, per_page: int = 100,
                      interval: float = 0.5) -> str:
    if engine not in TEMPLATE_ENGINES:
        raise ValueError(f'no template for engine \'{engine}\'')
    validate_names(ext_id, abbr, 'x')
    if not re.fullmatch(r'https://[a-z\d.-]+(?::\d+)?/', site):
        raise ValueError(f'unexpected site address {site!r}')
    values = {
        'ext_id': ext_id, 'abbr': abbr, 'site': site, 'engine': engine, 'date': datetime.date.today().isoformat(),
        'cls': class_name(abbr), 'site_lit': json.dumps(site), 'abbr_lit': json.dumps(abbr),
        'per_page': int(max(10, min(per_page, 1000))), 'interval': float(max(0.5, interval)),
        'api_caps': ', ModuleCaps.API_KEY' if api_key else '', 'api_key_method': _GELBOORU_API_KEY if api_key else '',
    }
    return (_GELBOORU if engine == 'gelbooru_dapi' else _DANBOORU).format(**values)

#
#
#########################################
