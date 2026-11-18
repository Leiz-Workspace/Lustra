# coding=UTF-8
"""
Lustra: tag lists updater (autocompletion / A-Z browser data)
Writes '<abbr>_tags.json' files in the same format as bundled lists: {"tag": "N posts (type)", ...}, one tag per line, sorted.
Sources (TagSource): the five reviewed modules (CURATED) and every extension made from a known engine, read from the
'$generator.engine' and 'site' of its manifest (lustra.extgen writes them), see ENGINE_TAG_SOURCES / tag_source.
Politeness, every source (docs/operations.md §3): the delay between requests, robots.txt Crawl-delay, Retry-After, a request
budget per list, no cookies, a stop on bot challenges, the user's own API key only (Lustra ships no shared key). robots.txt
(RFC 9309, lustra/robots.py) is applied strictly to extension sources; the five reviewed modules use the sites' documented
APIs and are exceptions to its Disallow lines (decision of 2026-11-18: Danbooru's robots.txt disallows '/*.json').
Used by: lustra-core tags / python -m lustra.tags_updater (the app's system schedule, cron), tools/update_tags.py,
the desktop app (Tags A-Z -> Danh sách tag, sidecar tags.update / tags.sources)
"""
#########################################
#
#

from __future__ import annotations

import csv
import datetime
import gzip
import html
import io
import ipaddress
import json
import math
import os
import pathlib
import re
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import NamedTuple
from urllib import parse as url_parse

import requests

from .defines import UTF8
from .denylist import denied, denied_message
from .network import LUSTRA_USER_AGENT, is_cloudflare_challenge, parse_retry_after
from .robots import ROBOTS_MAX_BYTES, RobotsRules

__all__ = ('CURATED', 'ENGINE_TAG_SOURCES', 'TagSource', 'TagSourceError', 'UpdateResult', 'tag_source', 'tag_sources',
           'update_tag_list', 'write_tag_list')

USER_AGENT = f'{LUSTRA_USER_AGENT} (tag list updater)'
ROBOTS_AGENT = 'Lustra'
'''product token looked up in robots.txt (the extension generator uses the same)'''
PAGE_LIMIT = 1000
SANITY_MIN_RATIO = 0.5
'''refuse to replace an existing list with one that has less than this ratio of tags (partial/failed fetch)'''
REQUEST_BUDGET = 5000
'''requests per list (robots.txt and retries count): 5M tags in 1000 tag pages. A list that needs more is not written'''
ENGINE_MIN_INTERVAL = 1.0
'''extension sources: at least this many seconds between requests (the generator's downloaders use the same floor)'''
CRAWL_DELAY_MAX = 60.0
TRIES = 5
TIMEOUT = (15, 120)
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
'''one answer holding a whole list: e621 database export, shimmie find_tags'''
PHILOMENA_PER_PAGE = 50
PHILOMENA_MAX_RESULTS = 10000
'''philomena's search answers this many results at most (elasticsearch window): its list is the most used tags'''
SZURUBOORU_PER_PAGE = 100

GELBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata', 6: 'general'}
DANBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata'}
E621_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'species', 6: 'invalid1', 7: 'metadata', 8: 'lore'}
MOEBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'artist', 6: 'metadata'}
'''moebooru: 5 circle (artist group), 6 faults'''
PHILOMENA_TYPES = {'character': 'character', 'oc': 'character', 'species': 'species', 'origin': 'artist', 'content-official': 'copyright',
                   'rating': 'metadata', 'spoiler': 'metadata', 'error': 'metadata'}

re_xml_tag = re.compile(r'<tag\s([^>]*?)/?>')
re_attr = re.compile(r'(\w+)=(["\'])(.*?)\2', re.DOTALL)
re_site = re.compile(r'^https://[a-z\d.-]+(?::\d+)?/$')
re_secret_param = re.compile(r'([?&](?:api_key|login|user_id|password|pass_hash|token|key)=)[^&#]*', re.IGNORECASE)

ProgressCb = Callable[[str, int], None]
'''(message, tags fetched so far)'''

ENGINE_TAG_SOURCES: dict[str, str] = {
    'gelbooru_dapi': 'DAPI tag index, after_id cursor',
    'danbooru_json': 'tags.json, id cursor',
    'moebooru_json': 'tag.json, pages by name',
    'e621_json': 'daily database export (db_export/tags-DATE.csv.gz)',
    'philomena_json': f'api/v1/json/search/tags, the {PHILOMENA_MAX_RESULTS:d} most used tags',
    'szurubooru_json': 'api/tags/, offset pages',
    'shimmie_api': 'api/danbooru/find_tags, the whole list in one answer',
}
'''engines (lustra.extgen.engines) with a tag listing -> how the list is read'''


class TagSourceError(RuntimeError):
    """A request Lustra refuses to make or to repeat: robots.txt, the request budget, a bot challenge, a disallowed address"""


@dataclass(frozen=True)
class TagSource:
    module: str
    engine: str
    site: str
    '''https://host/ the tag endpoints are under'''
    name: str = ''
    key: str = 'none'
    '''none | optional (used when the user has one) | required (the site refuses anonymous requests)'''
    key_format: str = ''
    robots: bool = True
    '''False: a reviewed exception to robots.txt Disallow lines (its Crawl-delay is still honored)'''
    curated: bool = False

    @property
    def description(self) -> str:
        return ENGINE_TAG_SOURCES.get(self.engine, '')

    def as_dict(self) -> dict:
        return {'module': self.module, 'name': self.name or self.module, 'engine': self.engine, 'site': self.site, 'key': self.key,
                'keyFormat': self.key_format, 'robots': self.robots, 'curated': self.curated, 'description': self.description}


CURATED: dict[str, TagSource] = {
    'rx': TagSource('rx', 'gelbooru_dapi', 'https://api.rule34.xxx/', 'Rule34', 'required', 'KEY,USER_ID', robots=False, curated=True),
    'db': TagSource('db', 'danbooru_json', 'https://danbooru.donmai.us/', 'Danbooru', 'optional', 'API_KEY,LOGIN', robots=False,
                    curated=True),
    'xb': TagSource('xb', 'gelbooru_dapi', 'https://xbooru.com/', 'Xbooru', robots=False, curated=True),
    'bb': TagSource('bb', 'gelbooru_dapi', 'https://bbooru.com/', 'Bbooru', robots=False, curated=True),
    'en': TagSource('en', 'e621_json', 'https://e621.net/', 'e621', robots=False, curated=True),
}
'''reviewed modules: the sites' documented APIs, exceptions to robots.txt Disallow lines (decision of 2026-11-18)'''


def _host(url: str) -> str:
    return (url_parse.urlsplit(url).hostname or '').lower().rstrip('.')


def _base_domain(host: str) -> str:
    return '.'.join(host.split('.')[-2:]) if host.count('.') >= 1 and not host.replace('.', '').isdigit() else host


def redact(text: str) -> str:
    return re_secret_param.sub(r'\1<REDACTED>', text)


# ----- sources -----

def _manifests(installed_only: bool) -> list:
    """Manifests only, no extension code is run: builtin + installed (user) extensions, then the local catalog if asked"""
    from .extensions import SOURCE_BUILTIN, SOURCE_CATALOG, SOURCE_USER, Extensions
    found = [*Extensions._scan(Extensions.builtin_dir(), SOURCE_BUILTIN), *Extensions._scan(Extensions.user_dir(), SOURCE_USER)]
    if not installed_only:
        found += Extensions._scan(Extensions.catalog_dir(), SOURCE_CATALOG)
    return found


def source_for_manifest(m) -> TagSource | None:
    """Tag source of an extension: a reviewed one for the same site, else from its engine ('$generator.engine') and 'site'"""
    site = str(m.site or '')
    cur = CURATED.get(m.abbr)
    if cur and _base_domain(_host(cur.site)) == _base_domain(_host(site)):
        return cur
    engine = str((m.generator or {}).get('engine', ''))
    if engine not in ENGINE_TAG_SOURCES or not re_site.fullmatch(site) or denied(site):
        return None
    auth = m.auth if isinstance(m.auth, dict) else {}
    if engine == 'gelbooru_dapi':
        key, fmt = ('required' if auth.get('apiKey') else 'optional'), 'API_KEY,USER_ID'
    elif engine == 'danbooru_json':
        key, fmt = 'optional', 'API_KEY,LOGIN'
    else:
        key, fmt = 'none', ''
    return TagSource(m.abbr, engine, site, m.name, key, str(auth.get('apiKeyFormat') or fmt) if key != 'none' else '')


def tag_source(module: str, manifests: list | None = None) -> TagSource | None:
    """Source of one module: its manifest (builtin, installed or local catalog) or, with no manifest, a reviewed module"""
    manifests = _manifests(installed_only=False) if manifests is None else manifests
    m = next((x for x in manifests if x.abbr == module), None)
    return source_for_manifest(m) if m is not None else CURATED.get(module)


def tag_sources(manifests: list | None = None) -> list[TagSource]:
    """Installed modules (builtin + user extensions) that have a tag source"""
    result: dict[str, TagSource] = {}
    for m in _manifests(installed_only=True) if manifests is None else manifests:
        if m.abbr not in result and (src := source_for_manifest(m)) is not None:
            result[m.abbr] = src
    return list(result.values())


# ----- polite client -----

class _Answer(NamedTuple):
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode(UTF8, 'replace')

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers['User-Agent'] = USER_AGENT
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # no cookies, ever
    return s


class PoliteClient:
    """Every request of one list update: pacing (delay, Crawl-delay), robots.txt, Retry-After, the request budget.
    allow_local: tests only (fake sites), skips the public address check; never set by the app or the command line"""

    def __init__(self, source: TagSource, *, delay=0.5, budget=REQUEST_BUDGET, allow_local=False, session=None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None) -> None:
        self.source = source
        self.site = source.site
        host = _host(source.site)
        self.hosts = {host, _base_domain(host)}
        self.interval = max(0.0, float(delay), 0.0 if source.curated else ENGINE_MIN_INTERVAL)
        self.budget = max(1, int(budget))
        self.used = 0
        self.allow_local = allow_local
        self.session = session or _session()
        self.robots: RobotsRules | None = None
        self._public: set[str] = set()
        self._sleep, self._clock = sleep or time.sleep, clock or time.monotonic
        self._last = -math.inf

    def _check_url(self, url: str) -> None:
        p = url_parse.urlsplit(url)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https':
            raise TagSourceError(f'only https is used: {redact(url)}')
        if not host or not any(host == h or host.endswith(f'.{h}') for h in self.hosts):
            raise TagSourceError(f'{host or url} is not the site of this tag list ({self.site})')
        if p.username or p.password:
            raise TagSourceError('credentials in urls are not allowed')
        if d := denied(host):
            raise TagSourceError(denied_message(d))
        if self.allow_local or host in self._public:
            return
        try:
            infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as err:
            raise requests.ConnectionError(f'cannot resolve {host}: {err}')
        bad = [i[4][0] for i in infos if not (ipaddress.ip_address(i[4][0]).is_global and not ipaddress.ip_address(i[4][0]).is_multicast)]
        if bad or not infos:
            raise TagSourceError(f'{host} resolves to a non public address ({", ".join(bad) or "none"})')
        self._public.add(host)

    def _spend(self) -> None:
        if self.used >= self.budget:
            raise TagSourceError(f'request budget of this list ({self.budget:d}) used up before the list was complete: not written')
        self.used += 1

    def _pace(self) -> None:
        wait = self._last + self.interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def _request(self, url: str, accept: str, max_bytes: int, truncate: bool = False) -> _Answer:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._check_url(current)
            self._spend()
            headers = {'Accept': accept} if accept else {}
            with self.session.request('GET', current, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('location'):
                    current = url_parse.urljoin(current, r.headers['location'])
                    continue
                if is_cloudflare_challenge(r):
                    raise TagSourceError(f'{_host(current)} answered with a bot challenge: Lustra does not bypass it, try again later')
                body = bytearray()
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > max_bytes:
                        if truncate:
                            del body[max_bytes:]
                            break
                        raise TagSourceError(f'answer of {_host(current)} is larger than {max_bytes // 1048576:d} MB: not read')
                return _Answer(current, r.status_code, {k.lower(): v for k, v in r.headers.items()}, bytes(body))
        raise TagSourceError(f'too many redirects: {redact(url)}')

    def _robots_rules(self) -> RobotsRules:
        if self.robots is None:
            p = url_parse.urlsplit(self.site)
            try:
                self._pace()
                r = self._request(f'{p.scheme}://{p.netloc}/robots.txt', 'text/plain', ROBOTS_MAX_BYTES, truncate=True)
                if 200 <= r.status < 300:
                    rules = RobotsRules.parse(r.text, ROBOTS_AGENT)
                elif 400 <= r.status < 500:
                    rules = RobotsRules.allow_all(f'none ({r.status:d})')
                else:
                    rules = RobotsRules.disallow_all(f'unreachable ({r.status:d})')
            except requests.RequestException as err:
                rules = RobotsRules.disallow_all(f'unreachable ({type(err).__name__})')
            self.robots = rules
            if rules.crawl_delay:
                self.interval = max(self.interval, min(rules.crawl_delay, CRAWL_DELAY_MAX))
        return self.robots

    def get(self, url: str, *, accept: str = '', max_bytes: int = MAX_PAGE_BYTES, ok: tuple[int, ...] = (200,)) -> _Answer:
        """GET after robots.txt and pacing; 429 / 503 wait for Retry-After, network errors are retried (TRIES in all)"""
        rules = self._robots_rules()
        if self.source.robots and not rules.allows(url):
            path = url_parse.urlsplit(url).path
            raise TagSourceError(f'robots.txt of {_host(url)} does not allow {path} ({rules.status}): the tag list is not updated')
        for attempt in range(TRIES):
            self._pace()
            try:
                r = self._request(url, accept, max_bytes)
            except TagSourceError:
                raise
            except requests.RequestException:
                if attempt == TRIES - 1:
                    raise
                self._sleep(2.0 * (attempt + 1))
                continue
            if r.status in (429, 503):
                self._sleep(parse_retry_after(r.headers.get('retry-after')) or 2.0 * (attempt + 1))
                continue
            if r.status not in ok:
                raise RuntimeError(f'{_host(url)} answered HTTP {r.status:d} for {redact(url_parse.urlsplit(url).path)}')
            return r
        raise RuntimeError(f'{_host(url)} still asks to wait (HTTP 429 / 503) after {TRIES:d} tries: try again later')


# ----- readers: (name, posts count, type name) -----

def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rx_auth(api_key: str) -> str:
    # the user's own key only: Lustra ships no shared key
    if not (api_key and ',' in api_key):
        raise ValueError('rx needs your own API key, KEY,USER_ID (rule34.xxx: My Account > Options > API Access Credentials)')
    key, user = api_key.split(',', 1)
    return f'&api_key={key.strip()}&user_id={user.strip()}'


def _dapi_auth(source: TagSource, api_key: str) -> str:
    if source.module == 'rx' and source.curated:
        return _rx_auth(api_key)
    if api_key and ',' in api_key:
        key, user = api_key.split(',', 1)
        return f'&api_key={key.strip()}&user_id={user.strip()}'
    if source.key == 'required':
        raise ValueError(f'{source.module} needs your own API key, {source.key_format or "API_KEY,USER_ID"} (the site refuses requests'
                         f' without one)')
    return ''


def _id_cursor_pages(fetch: Callable[[int], list[dict]], max_pages: int, what: str) -> Iterator[list[dict]]:
    """Pages of a listing by id cursor (tags with an id above the cursor), each tag once. Ends at an empty page or at a page
    shorter than the site's page size. A short first page is the end or a site that answers fewer tags than asked (some cap
    'limit'): one more request tells. A cursor that includes its own id (only the last tag again) ends cleanly, a cursor the site
    ignores (the same tags again) is an error: the list would be incomplete"""
    cursor, pages, page_size = 0, 0, PAGE_LIMIT
    while True:
        tags = fetch(cursor)
        if not tags:
            return
        fresh = [t for t in tags if _int(t.get('id')) > cursor]
        if not fresh:
            if pages and len(tags) == 1:
                return
            raise RuntimeError(f'website ignores the {what} cursor (stuck at {cursor:d}), the list would be incomplete')
        yield fresh
        cursor = max(_int(t.get('id')) for t in fresh)
        pages += 1
        if max_pages and pages >= max_pages:
            return
        if len(tags) < page_size:
            if pages > 1:
                return
            page_size = len(tags)


def _iter_gelbooru(client: PoliteClient, auth: str, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    def fetch(after_id: int) -> list[dict]:
        text = client.get(f'{client.site}index.php?page=dapi&s=tag&q=index&limit={PAGE_LIMIT:d}&after_id={after_id:d}{auth}').text
        if 'Missing authentication' in text:
            raise RuntimeError('website requires API key (use --api-key KEY,USER_ID)')
        return [{k: v for k, _, v in re_attr.findall(m.group(1))} for m in re_xml_tag.finditer(text)]

    for page in _id_cursor_pages(fetch, max_pages, 'after_id'):
        for t in page:
            count = _int(t.get('count'))
            if count >= min_posts and t.get('name'):
                # xml attribute values are escaped: '&amp;' -> '&', '&#039;' -> "'"
                yield html.unescape(t['name']), count, GELBOORU_TYPES.get(_int(t.get('type')), 'general')


def _iter_danbooru(client: PoliteClient, api_key: str, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    auth = ''
    if api_key and ',' in api_key:
        key, login = api_key.split(',', 1)
        auth = f'&login={login.strip()}&api_key={key.strip()}'

    def fetch(cursor: int) -> list[dict]:
        # 'a<id>' cursor: tags with id greater than cursor, no page depth limit
        tags = client.get(f'{client.site}tags.json?limit={PAGE_LIMIT:d}&page=a{cursor:d}'
                          f'&search[hide_empty]=true&only=id,name,post_count,category{auth}', accept='application/json').json()
        return [t for t in tags if isinstance(t, dict)] if isinstance(tags, list) else []

    for page in _id_cursor_pages(fetch, max_pages, 'id'):
        for t in page:
            count = _int(t.get('post_count'))
            if count >= min_posts and t.get('name'):
                yield str(t['name']), count, DANBOORU_TYPES.get(_int(t.get('category')), 'general')


def _iter_e621_export(client: PoliteClient, min_posts: int) -> Iterator[tuple[str, int, str]]:
    """e621 publishes daily database exports, much lighter for the website than paging through the api"""
    for days_back in range(4):
        day = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        r = client.get(f'{client.site}db_export/tags-{day}.csv.gz', max_bytes=MAX_EXPORT_BYTES, ok=(200, 404))
        if r.status == 404:
            continue
        with gzip.open(io.BytesIO(r.body), 'rt', encoding=UTF8, newline='') as f:
            for row in csv.DictReader(f):
                count = _int(row.get('post_count'))
                if count >= min_posts and row.get('name'):
                    yield row['name'], count, E621_TYPES.get(_int(row.get('category')), 'general')
        return
    raise RuntimeError(f'no recent tag export found on {_host(client.site)}')


def _iter_moebooru(client: PoliteClient, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    seen: set[str] = set()
    page = 1
    while True:
        tags = client.get(f'{client.site}tag.json?limit={PAGE_LIMIT:d}&page={page:d}&order=name', accept='application/json').json()
        if not isinstance(tags, list) or not tags:
            return
        new = 0
        for t in tags:
            name = str(t.get('name') or '') if isinstance(t, dict) else ''
            if not name or name in seen:
                continue
            seen.add(name)
            new += 1
            if (count := _int(t.get('count'))) >= min_posts:
                yield name, count, MOEBOORU_TYPES.get(_int(t.get('type')), 'general')
        if not new:
            raise RuntimeError(f'website ignores the page parameter (page {page:d} repeats earlier tags), the list would be incomplete')
        if len(tags) < PAGE_LIMIT or (max_pages and page >= max_pages):
            return
        page += 1


def _iter_philomena(client: PoliteClient, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    """most used first; names with spaces get '_' (the philomena downloaders send '_' as ' ')"""
    page, seen = 1, 0
    query = url_parse.urlencode({'q': f'images.gte:{max(1, min_posts):d}', 'per_page': PHILOMENA_PER_PAGE})
    while True:
        data = client.get(f'{client.site}api/v1/json/search/tags?{query}&page={page:d}', accept='application/json').json()
        tags = data.get('tags') if isinstance(data, dict) else None
        if not isinstance(tags, list) or not tags:
            return
        for t in tags:
            if not isinstance(t, dict) or t.get('aliased_tag'):
                continue
            name = str(t.get('name') or '').strip().replace(' ', '_')
            if name and (count := _int(t.get('images'))) >= min_posts:
                yield name, count, PHILOMENA_TYPES.get(str(t.get('category') or ''), 'general')
        seen += len(tags)
        total = data.get('total') if isinstance(data.get('total'), int) else PHILOMENA_MAX_RESULTS
        if len(tags) < PHILOMENA_PER_PAGE or seen >= min(total, PHILOMENA_MAX_RESULTS) or (max_pages and page >= max_pages):
            return
        page += 1


def _szuru_type(category: str) -> str:
    c = category.lower()
    return next((t for k, t in (('artist', 'artist'), ('character', 'character'), ('copyright', 'copyright'), ('series', 'copyright'),
                                ('meta', 'metadata'), ('species', 'species')) if k in c), 'general')


def _iter_szurubooru(client: PoliteClient, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    offset, pages = 0, 0
    while True:
        data = client.get(f'{client.site}api/tags/?offset={offset:d}&limit={SZURUBOORU_PER_PAGE:d}&fields=names,category,usages',
                          accept='application/json').json()
        results = data.get('results') if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            return
        for t in results:
            names = t.get('names') if isinstance(t, dict) else None
            name = str(names[0]).strip().replace(' ', '_') if isinstance(names, list) and names else ''
            if name and (count := _int(t.get('usages'))) >= min_posts:
                yield name, count, _szuru_type(str(t.get('category') or ''))
        offset += len(results)
        pages += 1
        total = data.get('total')
        if (isinstance(total, int) and offset >= total) or len(results) < SZURUBOORU_PER_PAGE or (max_pages and pages >= max_pages):
            return


def _iter_shimmie(client: PoliteClient, min_posts: int) -> Iterator[tuple[str, int, str]]:
    text = client.get(f'{client.site}api/danbooru/find_tags', accept='application/xml, text/xml', max_bytes=MAX_EXPORT_BYTES).text
    if '<tags' not in text:
        raise RuntimeError(f'{_host(client.site)} has no tag listing (api/danbooru/find_tags)')
    for m in re_xml_tag.finditer(text):
        t = {k: html.unescape(v) for k, _, v in re_attr.findall(m.group(1))}
        count = _int(t.get('count') or t.get('counts'))  # shimmie writes 'counts'
        if t.get('name') and count >= min_posts:
            yield t['name'], count, 'general'


def _iter_source(source: TagSource, client: PoliteClient, api_key: str, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    engine = source.engine
    if engine == 'gelbooru_dapi':
        return _iter_gelbooru(client, _dapi_auth(source, api_key), min_posts, max_pages)
    if engine == 'danbooru_json':
        return _iter_danbooru(client, api_key, min_posts, max_pages)
    if engine == 'e621_json':
        return _iter_e621_export(client, min_posts)
    if engine == 'moebooru_json':
        return _iter_moebooru(client, min_posts, max_pages)
    if engine == 'philomena_json':
        return _iter_philomena(client, min_posts, max_pages)
    if engine == 'szurubooru_json':
        return _iter_szurubooru(client, min_posts, max_pages)
    if engine == 'shimmie_api':
        return _iter_shimmie(client, min_posts)
    raise ValueError(f'engine \'{engine}\' has no tag list source')


# ----- list files -----

class UpdateResult:
    __slots__ = ('count', 'message', 'module', 'path', 'previous', 'requests', 'written')

    def __init__(self, module: str, path: pathlib.Path) -> None:
        self.module, self.path = module, path
        self.count = self.previous = self.requests = 0
        self.written = False
        self.message = ''

    def as_dict(self) -> dict:
        return {k: (getattr(self, k).as_posix() if k == 'path' else getattr(self, k)) for k in self.__slots__}


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
                    progress: ProgressCb | None = None, reference: pathlib.Path | None = None, source: TagSource | None = None,
                    budget=REQUEST_BUDGET, allow_local=False, session=None) -> UpdateResult:
    """reference: the list currently in use (may live in another folder). The sanity check compares against the bigger one,
    otherwise a failed first update into an empty folder would shadow the bundled list with an almost empty newer file.
    source: default tag_source(module); allow_local / session: tests (fake sites)"""
    src = source or tag_source(module)
    if src is None:
        raise ValueError(f'module \'{module}\' has no tag list source (reviewed: {", ".join(CURATED)}; extensions made from the engines '
                         f'{", ".join(ENGINE_TAG_SOURCES)})')
    out_dir.mkdir(parents=True, exist_ok=True)
    res = UpdateResult(module, out_dir / f'{module}_tags.json')
    res.previous = max(_count_lines(res.path), _count_lines(reference) if reference else 0)
    client = PoliteClient(src, delay=delay, budget=budget, allow_local=allow_local, session=session)
    tags: dict[str, tuple[int, str]] = {}
    try:
        for i, (name, count, ttype) in enumerate(_iter_source(src, client, api_key, min_posts, max_pages)):
            if name not in tags or tags[name][0] < count:
                tags[name] = (count, ttype)
            if progress and i % 5000 == 0:
                progress(f'[{module}] {len(tags):d} tags...', len(tags))
    finally:
        res.requests = client.used
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
    res.message = f'written {res.count:d} tags (was {res.previous:d}, {res.requests:d} requests)'
    return res


def _modules_arg(value: str) -> list[str]:
    """'auto': every installed module with a tag source that needs no key of the user (the app's system schedule)"""
    mods: list[str] = []
    for m in (x.strip() for x in value.split(',')):
        if m == 'auto':
            mods += [s.module for s in tag_sources() if s.key != 'required']
        elif m:
            mods.append(m)
    return list(dict.fromkeys(mods))


def main(argv: list[str] | None = None, default_out: str | os.PathLike | None = None) -> int:
    """CLI: python -m lustra.tags_updater, lustra-core tags (the app's scheduled update), tools/update_tags.py.
    Keys: --api-key for every module, else LUSTRA_API_KEY_<MODULE> (KEY,USER_ID); rx fails with a clear message without one.
    Exit code: 0 = every requested list updated (or kept by the sanity check), 1 = at least one module failed"""
    import argparse

    from .extensions import data_dir
    ap = argparse.ArgumentParser(prog='lustra-core tags',
                                 description='Update Lustra tag lists used for autocompletion and the A-Z tag browser')
    ap.add_argument('--modules', default='rx,db', help=f'comma separated module abbreviations (reviewed: {",".join(CURATED)}; '
                    f'extensions made from a known engine), or \'auto\': every installed module that needs no API key')
    ap.add_argument('--out', default=str(default_out or data_dir() / 'tags'), help='output folder (default: <data dir>/tags)')
    ap.add_argument('--min-posts', type=int, default=1, help='skip tags with fewer posts (default 1)')
    ap.add_argument('--delay', type=float, default=0.5, help='delay between requests, seconds (be polite, default 0.5; a robots.txt '
                    f'Crawl-delay wins, extension sites get {ENGINE_MIN_INTERVAL:g} at least)')
    ap.add_argument('--max-requests', type=int, default=REQUEST_BUDGET, help=f'request budget per list (default {REQUEST_BUDGET:d})')
    ap.add_argument('--max-pages', type=int, default=0, help='debug: stop after N pages (partial lists are not written without --force)')
    ap.add_argument('--api-key', default='', help='KEY,USER_ID (gelbooru based) or API_KEY,LOGIN (danbooru based), for every module')
    ap.add_argument('--force', action='store_true', help='write even if the sanity check fails')
    ap.add_argument('--log', default='', help='append the output to this file (scheduled runs: no shell redirection needed)')
    args = ap.parse_args(argv)
    log = open(args.log, 'a', encoding=UTF8) if args.log else None  # noqa: SIM115 (closed below)

    def say(line: str, err: bool = False) -> None:
        line = redact(line)
        print(line, file=sys.stderr if err else sys.stdout, flush=True)
        if log:
            log.write(line + '\n')
            log.flush()

    failed = 0
    try:
        modules = _modules_arg(args.modules)
        if not modules:
            say(f'[{time.strftime("%F %T")}] no installed module with a tag source that needs no API key: nothing to update')
        for module in modules:
            started = time.monotonic()
            try:
                src = tag_source(module)
                if src is not None:  # none: update_tag_list says why
                    say(f'[{time.strftime("%F %T")}] {module}: updating ({src.name or module}: {src.description})...')
                key = args.api_key or os.environ.get(f'LUSTRA_API_KEY_{module.upper()}', '').strip()
                res = update_tag_list(module, pathlib.Path(args.out), min_posts=args.min_posts, delay=args.delay, max_pages=args.max_pages,
                                      api_key=key, force=args.force, progress=lambda msg, _n: say(f'  {msg}'), source=src,
                                      budget=args.max_requests)
                say(f'[{time.strftime("%F %T")}] {module}: {res.message} in {time.monotonic() - started:.0f}s -> {res.path}')
            except Exception as err:
                failed += 1
                say(f'[{time.strftime("%F %T")}] {module}: FAILED: {err}', True)
    finally:
        if log:
            log.close()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
