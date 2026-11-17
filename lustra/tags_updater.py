# coding=UTF-8
"""
Lustra: tag lists updater (autocompletion / A-Z browser data)
Writes '<abbr>_tags.json' files in the same format as bundled lists: {"tag": "N posts (type)", ...}, one tag per line, sorted.
Used by: tools/update_tags.py (cron / systemd timer), desktop app (Settings -> Tag lists -> Update now)
"""
#########################################
#
#

from __future__ import annotations

import base64
import csv
import datetime
import gzip
import html
import io
import json
import os
import pathlib
import re
import time
import uuid
from collections.abc import Callable, Iterator

import requests

from .defines import API_KEY_DEFAULT_RX, API_USER_ID_DEFAULT_RX, UTF8
from .network import parse_retry_after

__all__ = ('SUPPORTED_MODULES', 'UpdateResult', 'update_tag_list', 'write_tag_list')

USER_AGENT = 'Lustra/2.0 tag list updater'
PAGE_LIMIT = 1000
SANITY_MIN_RATIO = 0.5
'''refuse to replace an existing list with one that has less than this ratio of tags (partial/failed fetch)'''
MAX_PAGES_HARD = 20000
'''20M tags: a cursor that keeps moving forever means the website ignores it'''

GELBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata', 6: 'general'}
DANBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata'}
E621_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'species', 6: 'invalid1', 7: 'metadata', 8: 'lore'}

re_gel_tag = re.compile(r'<tag ([^>]+)/>')
re_attr = re.compile(r'(\w+)="([^"]*)"')

ProgressCb = Callable[[str, int], None]
'''(message, tags fetched so far)'''


class UpdateResult:
    __slots__ = ('count', 'message', 'module', 'path', 'previous', 'written')

    def __init__(self, module: str, path: pathlib.Path) -> None:
        self.module, self.path = module, path
        self.count = self.previous = 0
        self.written = False
        self.message = ''

    def as_dict(self) -> dict:
        return {k: (getattr(self, k).as_posix() if k == 'path' else getattr(self, k)) for k in self.__slots__}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers['User-Agent'] = USER_AGENT
    return s


def _get(s: requests.Session, url: str, delay: float, tries=5) -> requests.Response:
    for attempt in range(tries):
        time.sleep(delay)
        try:
            r = s.get(url, timeout=30)
            if r.status_code in (429, 503):
                time.sleep(parse_retry_after(r.headers.get('Retry-After')) or 2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
    raise requests.RequestException(f'giving up: {url}')


def _iter_gelbooru(site: str, auth: str, min_posts: int, delay: float, max_pages: int) -> Iterator[tuple[str, int, str]]:
    s = _session()
    after_id, pages = 0, 0
    while True:
        r = _get(s, f'{site}index.php?page=dapi&s=tag&q=index&limit={PAGE_LIMIT:d}&after_id={after_id:d}{auth}', delay)
        if 'Missing authentication' in r.text:
            raise RuntimeError('website requires API key (use --api-key KEY,USER_ID)')
        tags = [dict(re_attr.findall(m.group(1))) for m in re_gel_tag.finditer(r.text)]
        if not tags:
            return
        for t in tags:
            count = int(t.get('count', '0') or 0)
            if count >= min_posts and t.get('name'):
                # xml attribute values are escaped: '&amp;' -> '&', '&#039;' -> "'"
                yield html.unescape(t['name']), count, GELBOORU_TYPES.get(int(t.get('type', '0') or 0), 'general')
        new_after_id = max(int(t.get('id', '0') or 0) for t in tags)
        if new_after_id <= after_id:
            raise RuntimeError(f'website ignores the after_id cursor (stuck at {after_id:d}), the list would be incomplete')
        after_id = new_after_id
        pages += 1
        if len(tags) < PAGE_LIMIT or (max_pages and pages >= max_pages):
            return
        if pages >= MAX_PAGES_HARD:
            raise RuntimeError(f'more than {MAX_PAGES_HARD:d} pages, aborting')


def _iter_danbooru(min_posts: int, delay: float, max_pages: int, api_key: str) -> Iterator[tuple[str, int, str]]:
    s = _session()
    auth = ''
    if api_key and ',' in api_key:
        key, login = api_key.split(',', 1)
        auth = f'&login={login}&api_key={key}'
    cursor, pages = 0, 0
    while True:
        # 'a<id>' cursor: tags with id greater than cursor, no page depth limit
        r = _get(s, f'https://danbooru.donmai.us/tags.json?limit={PAGE_LIMIT:d}&page=a{cursor:d}'
                    f'&search[hide_empty]=true&only=id,name,post_count,category{auth}', delay)
        tags = r.json()
        if not isinstance(tags, list) or not tags:
            return
        for t in tags:
            count = int(t.get('post_count') or 0)
            if count >= min_posts:
                yield str(t['name']), count, DANBOORU_TYPES.get(int(t.get('category') or 0), 'general')
        new_cursor = max(int(t['id']) for t in tags)
        if new_cursor <= cursor:
            raise RuntimeError(f'website ignores the id cursor (stuck at {cursor:d}), the list would be incomplete')
        cursor = new_cursor
        pages += 1
        if len(tags) < PAGE_LIMIT or (max_pages and pages >= max_pages):
            return
        if pages >= MAX_PAGES_HARD:
            raise RuntimeError(f'more than {MAX_PAGES_HARD:d} pages, aborting')


def _iter_e621_export(min_posts: int, delay: float) -> Iterator[tuple[str, int, str]]:
    """e621 publishes daily database exports, much lighter for the website than paging through the api"""
    s = _session()
    for days_back in range(4):
        day = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        r = s.get(f'https://e621.net/db_export/tags-{day}.csv.gz', timeout=120)
        time.sleep(delay)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        with gzip.open(io.BytesIO(r.content), 'rt', encoding=UTF8, newline='') as f:
            for row in csv.DictReader(f):
                count = int(row.get('post_count') or 0)
                if count >= min_posts:
                    yield row['name'], count, E621_TYPES.get(int(row.get('category') or 0), 'general')
        return
    raise RuntimeError('no recent e621 tag export found')


def _rx_auth(api_key: str) -> str:
    if api_key and ',' in api_key:
        key, user = api_key.split(',', 1)
    else:
        key, user = base64.b64decode(API_KEY_DEFAULT_RX).decode(), base64.b64decode(API_USER_ID_DEFAULT_RX).decode()
    return f'&api_key={key}&user_id={user}'


SUPPORTED_MODULES: dict[str, str] = {
    'rx': 'Rule34 (DAPI tag index, after_id cursor)',
    'db': 'Danbooru (tags.json, id cursor)',
    'xb': 'Xbooru (DAPI tag index)',
    'bb': 'Bbooru (DAPI tag index)',
    'en': 'e621 (daily db export)',
}


def _iter_module(module: str, min_posts: int, delay: float, max_pages: int, api_key: str) -> Iterator[tuple[str, int, str]]:
    if module == 'rx':
        return _iter_gelbooru('https://api.rule34.xxx/', _rx_auth(api_key), min_posts, delay, max_pages)
    if module == 'xb':
        return _iter_gelbooru('https://xbooru.com/', '', min_posts, delay, max_pages)
    if module == 'bb':
        return _iter_gelbooru('https://bbooru.com/', '', min_posts, delay, max_pages)
    if module == 'db':
        return _iter_danbooru(min_posts, delay, max_pages, api_key)
    if module == 'en':
        return _iter_e621_export(min_posts, delay)
    raise ValueError(f'module \'{module}\' has no tag list source (supported: {", ".join(SUPPORTED_MODULES)})')


def _count_lines(path: pathlib.Path) -> int:
    if not path.is_file():
        return 0
    with open(path, 'rb') as f:
        return max(0, sum(1 for _ in f) - 2)


def write_tag_list(path: pathlib.Path, tags: dict[str, tuple[int, str]]) -> None:
    """Atomic write in bundled list format (one entry per line, sorted by python string order for binary search)"""
    tmp = path.with_name(f'.{path.name}.{os.getpid():d}.{uuid.uuid4().hex[:8]}.tmp')  # concurrent writers never share a tmp file
    try:
        with open(tmp, 'wt', encoding=UTF8, newline='\n') as f:
            f.write('{\n')
            names = sorted(tags)
            for i, name in enumerate(names):
                count, ttype = tags[name]
                f.write(f'{json.dumps(name, ensure_ascii=False)}: "{count:d} posts ({ttype})"{"," if i < len(names) - 1 else ""}\n')
            f.write('}\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def update_tag_list(module: str, out_dir: pathlib.Path, *, min_posts=1, delay=0.5, max_pages=0, api_key='', force=False,
                    progress: ProgressCb | None = None, reference: pathlib.Path | None = None) -> UpdateResult:
    """reference: the list currently in use (may live in another folder). The sanity check compares against the bigger one,
    otherwise a failed first update into an empty folder would shadow the bundled list with an almost empty newer file"""
    out_dir.mkdir(parents=True, exist_ok=True)
    res = UpdateResult(module, out_dir / f'{module}_tags.json')
    res.previous = max(_count_lines(res.path), _count_lines(reference) if reference else 0)
    tags: dict[str, tuple[int, str]] = {}
    for i, (name, count, ttype) in enumerate(_iter_module(module, min_posts, delay, max_pages, api_key)):
        if name not in tags or tags[name][0] < count:
            tags[name] = (count, ttype)
        if progress and i % 5000 == 0:
            progress(f'[{module}] {len(tags):d} tags...', len(tags))
    res.count = len(tags)
    if res.count == 0:
        res.message = 'not written: no tags fetched'
        return res
    partial = bool(max_pages)
    if not force and (partial or res.count < res.previous * SANITY_MIN_RATIO):
        res.message = (f'not written: fetched {res.count:d} tags vs {res.previous:d} existing'
                       f'{" (partial fetch, use --force)" if partial else " (sanity check failed, use --force)"}')
        return res
    write_tag_list(res.path, tags)
    res.written = True
    res.message = f'written {res.count:d} tags (was {res.previous:d})'
    return res

#
#
#########################################
