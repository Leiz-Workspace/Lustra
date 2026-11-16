# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

from __future__ import annotations

import datetime
import email.utils
import os
import pathlib
import re
import sys
import time
from abc import ABC, abstractmethod
from argparse import Namespace
from multiprocessing.dummy import current_process
from threading import Lock, Thread, local
from typing import NamedTuple
from warnings import filterwarnings

from bs4 import BeautifulSoup
from requests import Response, Session, adapters, exceptions, structures

from .defines import (
    CONNECT_RETRIES_BASE,
    CONNECT_RETRIES_CHUNK,
    CONNECT_RETRIES_PROXY_MULT,
    CONNECT_TIMEOUT_BASE,
    DOWNLOAD_CHUNK_SIZE,
    KNOWN_EXTENSIONS_VID,
    RETRY_AFTER_MAX_SECONDS,
    WRITE_CHUNK_SIZE,
    DownloadModes,
    ThreadInterruptException,
)
from .logger import trace
from .useragent import UAManager
from .utils import format_exception

__all__ = ('DownloadInterruptException', 'HTMLContent', 'ThreadedHtmlWorker', 'thread_exit')

filterwarnings('ignore', category=UserWarning)

CLIENT_CONNECTOR_ERRORS = (exceptions.ProxyError, exceptions.SSLError)
PART_SUFFIX = '.part'
'''incomplete downloads'''
'''errors which do not consume normal retries (proxy hiccups), see CONNECT_RETRIES_PROXY_MULT for their cap'''


def parse_retry_after(value: str | None) -> float:
    """Parses 'Retry-After' header (delta-seconds or HTTP-date), returns 0 if absent or invalid"""
    if not value:
        return 0.0
    value = value.strip()
    try:
        return min(float(max(0, int(value))), RETRY_AFTER_MAX_SECONDS)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        delta = (when - datetime.datetime.now(when.tzinfo)).total_seconds()
        return min(max(0.0, delta), RETRY_AFTER_MAX_SECONDS)
    except Exception:
        return 0.0


def is_cloudflare_challenge(response: Response | None) -> bool:
    """True if the response is a Cloudflare bot challenge page (cannot be passed by a plain HTTP client)"""
    if response is None or response.status_code not in (403, 429, 503):
        return False
    return response.headers.get('cf-mitigated', '').lower() == 'challenge'


CF_CHALLENGE_HINT = (
    'Website responded with a bot challenge page (cf-mitigated: challenge). Lustra does not bypass bot protection. '
    'If you have an account: log in using your browser, then copy your \'cf_clearance\' cookie and browser \'User-Agent\' '
    'into Connection -> Headers / Cookies. Otherwise try again later.'
)

re_content_range_str = re.compile(r'bytes (\d+)-(\d+)?(?:/(\d+))?')


class DownloadInterruptException(BaseException):
    pass


class HTMLContent(NamedTuple):
    html: BeautifulSoup
    raw: bytes

    @staticmethod
    def init_empty() -> HTMLContent:
        return HTMLContent(BeautifulSoup(), b'')

    def __bool__(self) -> bool:
        return bool(len(self.raw))


class FileDownloadResult:
    def __init__(self) -> None:
        self.file_size: int = 0
        self.expected_size: int = 0
        self.retries: int = 0
        self.result_str: str = ''


def thread_exit(err_str='', code=-1) -> None:
    trace(f'Exiting with code {code:d}, message:\n{err_str}', True)
    time.sleep(0.15)
    raise ThreadInterruptException


class ThreadedWorker(ABC):
    """
    ThreadedWorker !Abstract!
    """
    @abstractmethod
    def __init__(self) -> None:
        self.my_root_thread: Thread | None = None
        self.item_lock: Lock = Lock()
        self.items_all_lock: Lock = Lock()

    def reset_root_thread(self, thread: Thread) -> None:
        self.my_root_thread = thread

    def catch_cancel_or_ctrl_c(self, message: str = '') -> None:
        if self.is_killed():
            thread_exit(message, code=1)

    def is_killed(self) -> bool:
        return self.my_root_thread and getattr(self.my_root_thread, 'killed', False) is True


class ThreadedHtmlWorker(ThreadedWorker):
    """
    ThreadedHtmlWorker !Abstract!
    """
    @abstractmethod
    def __init__(self) -> None:
        super().__init__()
        self.verbose: bool = False
        self.raw_html_cache: dict[str, HTMLContent] = {}
        self.add_headers: structures.CaseInsensitiveDict[str, str] = structures.CaseInsensitiveDict()
        self.add_cookies: structures.CaseInsensitiveDict[str, str] = structures.CaseInsensitiveDict()
        self.ignore_proxy: bool = False
        self.ignore_proxy_dwn: bool = False
        self.proxies: dict[str, str] | None = None
        self.timeout: int = CONNECT_TIMEOUT_BASE
        self.retries: int = CONNECT_RETRIES_BASE
        self.etags: dict[str, str] = {}
        self.session: Session | None = None
        self._tls = local()
        self._sessions_lock: Lock = Lock()
        self._thread_sessions: list[Session] = []
        self._cookies_lock: Lock = Lock()
        self._rate_lock: Lock = Lock()
        self._last_request_ts: float = 0.0
        self.cf_challenge_hit: bool = False
        '''set if the website responded with a bot challenge, UI may show a hint'''

    @abstractmethod
    def _get_sitename(self) -> str:
        ...

    @abstractmethod
    def _get_module_specific_default_headers(self) -> dict[str, str]:
        ...

    @abstractmethod
    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        ...

    def _get_min_request_interval(self) -> float:
        """Minimal interval between API/html requests across all threads (seconds), modules override to respect site rate limits"""
        return 0.0

    def _use_chunked_download(self, is_video_ext: bool) -> bool:
        """Whether file should be downloaded using multiple ranged requests"""
        return True

    def _fix_link_after_download_error(self, link: str, original_link: str, err: Exception) -> str:
        """Module hook: returns a new link to retry with (ex. alternative cdn host) or the same link"""
        return link

    def make_session(self, *, for_download=False) -> Session:
        s = Session()
        s.adapters.clear()
        s.mount('http://', adapters.HTTPAdapter(pool_maxsize=1, max_retries=0))
        s.mount('https://', adapters.HTTPAdapter(pool_maxsize=1, max_retries=0))
        s.keep_alive = True
        s.headers['Referer'] = self._get_sitename()
        s.headers['User-Agent'] = UAManager.orig_user_agent()
        s.headers.update(self.add_headers.copy())
        s.cookies.update(self.add_cookies.copy())
        if self.proxies and not self.ignore_proxy and not (for_download and self.ignore_proxy_dwn):
            s.proxies.update(self.proxies.copy())
        return s

    def _thread_session(self) -> Session:
        """requests.Session is not guaranteed to be thread-safe: every worker thread gets its own session"""
        if current_process() == self.my_root_thread or self.my_root_thread is None:
            if self.session is None:
                self.session = self.make_session()
            return self.session
        s: Session | None = getattr(self._tls, 'session', None)
        if s is None:
            s = self.make_session()
            self._tls.session = s
            with self._sessions_lock:
                self._thread_sessions.append(s)
        return s

    def close_sessions(self) -> None:
        with self._sessions_lock:
            for s in self._thread_sessions:
                s.close()
            self._thread_sessions.clear()
        self._tls = local()

    def _respect_rate_limit(self) -> None:
        interval = self._get_min_request_interval()
        if interval <= 0:
            return
        with self._rate_lock:
            wait = self._last_request_ts + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _parse_args(self, args: Namespace) -> None:
        self.verbose = args.verbose or self.verbose
        self.ignore_proxy = args.noproxy or self.ignore_proxy
        self.ignore_proxy_dwn = args.proxynodown or self.ignore_proxy_dwn
        self.proxies = {'http': str(args.proxy), 'https': str(args.proxy)} if args.proxy else None
        self.timeout = args.timeout or self.timeout
        self.retries = args.retries or self.retries
        if args.headers:
            self.add_headers.update(args.headers)
        if args.cookies:
            self.add_cookies.update(args.cookies)
        for container_base, container_ext in zip((self.add_headers, self.add_cookies), (args.header, args.cookie), strict=True):
            pair: tuple[str, str]
            for pair in container_ext or []:
                if pair[0] in container_base:
                    trace(f'Warning (W1): Overriding json value at \'{pair[0]}\' from \'{container_base[pair[0]]}\' to \'{pair[1]}\'')
                container_base[pair[0]] = pair[1]
        self.add_headers.update(self._get_module_specific_default_headers())
        self.add_cookies.update(self._get_module_specific_default_cookies())
        self.close_sessions()
        self.session = self.make_session()

    # threaded
    def download_file(self, link: str, item_id: str, dest: pathlib.Path, mode: DownloadModes, orig_date: bool) -> FileDownloadResult:
        fullname = dest.name
        ext_full = fullname[fullname.rfind('.') + 1:]
        ext_char = ext_full[0]
        is_video_ext = ext_full in KNOWN_EXTENSIONS_VID
        oldlink = link

        result = FileDownloadResult()
        result.result_str = f'[{current_process().name}]{" <touch>" if mode == DownloadModes.TOUCH else ""} {item_id}({ext_char})... '

        # data is written to '<name>.part' and renamed only when complete: an interrupted run never leaves
        # a truncated file under the final name (which later runs would treat as already downloaded)
        part = dest.with_name(f'{dest.name}{PART_SUFFIX}')
        if mode == DownloadModes.FULL and dest.is_file() and dest.stat().st_size > 0:
            result.file_size = result.expected_size = dest.stat().st_size
            result.result_str = f'{result.result_str}already exists, '
            return result

        if mode == DownloadModes.TOUCH:
            dest.touch(exist_ok=True)  # never truncates an existing file
        elif mode == DownloadModes.FULL:
            conn_errors = 0
            with self.make_session(for_download=True) as s:
                s.stream = True
                while (not (dest.is_file() and result.file_size == result.expected_size)) and result.retries < self.retries:
                    if self.is_killed():
                        trace(f'{result.result_str} interrupted', True)
                        raise DownloadInterruptException
                    try:
                        def download_chunk(ofile, start: int, end: int, exp_size: int, chunk_num: int) -> None:
                            headers = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                                       'Accept-Language': 'en-US,en;q=0.5',
                                       'Accept-Encoding': 'gzip, deflate, br',
                                       'DNT': '1',
                                       # 'Host': reqhost,
                                       'Connection': 'keep-alive'}
                            if not single_chunk:
                                headers['Range'] = f'bytes={start:d}-{end:d}'
                            headers.update(self.add_headers.copy())
                            req = s.request('GET', link, timeout=self.timeout, stream=True, headers=headers, allow_redirects=False)
                            req.raise_for_status()
                            temp = b''
                            for chunk_w in req.iter_content(WRITE_CHUNK_SIZE):
                                temp += chunk_w
                            req.close()
                            etag = req.headers.get('etag', item_id)
                            content_range = req.headers.get('content-range') if not single_chunk else None
                            content_range_m = re_content_range_str.search(content_range) if content_range else None
                            c_start = content_range_m.group(1) if content_range_m else '0'
                            c_end = content_range_m.group(2) if content_range_m else '0'
                            c_total = content_range_m.group(3) if content_range_m else '0'
                            valid_chunk = True
                            errcode = 0
                            if len(temp) != exp_size or (not single_chunk and int(c_total) != result.expected_size):
                                valid_chunk = False
                                errcode = 1
                            elif etag != self.etags.get(item_id):
                                valid_chunk = False
                                errcode = 2
                            elif not single_chunk and not content_range:
                                valid_chunk = False
                                errcode = 3
                            elif not single_chunk and not content_range_m:
                                valid_chunk = False
                                errcode = 4
                            elif int(c_start) != start or (not single_chunk and int(c_end) != end):
                                valid_chunk = False
                                errcode = 5
                            if valid_chunk is False:
                                trace(f'Warning (W2): {item_id} invalid chunk {chunk_num:d} err {errcode:d}', True)
                                # website may send not an HTTP error but instead just a mismatched chunk and call it good
                                severe_err = errcode in range(1, 4 + 1)
                                response = Response()
                                response.status_code = 416 if severe_err else 417  # Expectation Failed
                                raise exceptions.HTTPError(response=response)
                            ofile.write(temp)

                        sreq = s.request('HEAD', link, timeout=self.timeout, allow_redirects=False, headers={'Bytes': str(10**12)})
                        sreq.raise_for_status()
                        last_modified = sreq.headers.get('last-modified', '') if orig_date else ''
                        modification_time_ns = (
                            int(datetime.datetime.strptime(last_modified, '%a, %d %b %Y %H:%M:%S %Z').timestamp() * 10 ** 9)
                            if last_modified else 0
                        )
                        result.expected_size = int(sreq.headers.get('content-length', '0'))
                        self.etags[item_id] = sreq.headers.get('etag', item_id)
                        # this code was left here after link replacements had been removed. DO NOT MOVE
                        # reqhost = re_link_host.search(link).group(1)
                        sreq.close()

                        if result.expected_size == 0:
                            err_msg = f'Warning (W2): fetched {item_id}({ext_char}) file is empty.'
                            raise ValueError(err_msg)

                        use_chunked = self._use_chunked_download(is_video_ext)
                        chunks = list(range(0, result.expected_size, DOWNLOAD_CHUNK_SIZE if use_chunked else result.expected_size))
                        single_chunk = len(chunks) == 1
                        with open(part, 'wb') as outf:
                            creation_time_ns = int(part.stat().st_ctime_ns) if modification_time_ns else 0
                            i = 0
                            chunk_tries = 0
                            while i < len(chunks):
                                if self.is_killed():
                                    raise ThreadInterruptException
                                chunk_begin = chunks[i]
                                chunk_end = result.expected_size - 1 if i == len(chunks) - 1 else chunks[i + 1] - 1
                                expected_chunk_size = (chunk_end - chunk_begin) + 1
                                try:
                                    download_chunk(outf, chunk_begin, chunk_end, expected_chunk_size, i + 1)
                                except Exception as err:
                                    if isinstance(err, (KeyboardInterrupt, ThreadInterruptException)):
                                        raise
                                    if chunk_tries >= CONNECT_RETRIES_CHUNK:
                                        trace(f'Warning (W2): at {item_id} chunk {i + 1:d} catched too many HTTPError 416s!', True)
                                        break
                                    chunk_tries += 1
                                    sleep_time = 2
                                    exc_p1, exc_p2 = tuple(str(sys.exc_info()[k]) for k in range(2))
                                    trace(f'Warning (W2): at {item_id} chunk {i + 1:d} catched {exc_p1}: {exc_p2} retrying...', True)
                                    if not self.is_killed():
                                        time.sleep(sleep_time)
                                    continue
                                chunk_tries = 0
                                i += 1

                        result.file_size = part.stat().st_size
                        if result.file_size != result.expected_size:
                            trace(f'Warning (W3): size mismatch for {item_id} ({result.file_size:d} / {result.expected_size:d}).'
                                  f' Retrying file.', True)
                            part.unlink(missing_ok=True)
                            result.file_size = 0
                            raise OSError
                        os.replace(part, dest)
                        if creation_time_ns and modification_time_ns:
                            os.utime(dest, ns=(creation_time_ns, modification_time_ns))
                    except (KeyboardInterrupt, ThreadInterruptException):
                        part.unlink(missing_ok=True)
                        result.file_size = 0
                        trace(f'{result.result_str}{("interrupted by user." if current_process() == self.my_root_thread else "")}', True)
                        raise DownloadInterruptException
                    except Exception as err:
                        if isinstance(err, exceptions.HTTPError) and is_cloudflare_challenge(err.response):
                            self.cf_challenge_hit = True
                            trace(f'{result.result_str}failed: {CF_CHALLENGE_HINT}', True)
                            result.retries = self.retries
                            break
                        link = self._fix_link_after_download_error(link, oldlink, err)
                        if isinstance(err, exceptions.HTTPError) and err.response.status_code == 416:  # Requested range is not satisfiable
                            trace(f'Warning (W3): {item_id} catched HTTPError 416!', True)
                            part.unlink(missing_ok=True)
                            result.file_size = 0
                        if not isinstance(err, CLIENT_CONNECTOR_ERRORS):
                            result.retries += 1
                        else:
                            conn_errors += 1
                            if conn_errors >= self.retries * CONNECT_RETRIES_PROXY_MULT:
                                trace(f'{result.result_str}too many proxy / ssl errors ({conn_errors:d}), giving up', True)
                                result.retries = self.retries
                                break
                        trace(f'{result.result_str}{format_exception("row")} retry {result.retries:d}...', True)
                        if not self.is_killed():
                            time.sleep(2)
                        continue
        return result

    # threaded
    def wrap_request(self, url: str, tries: int, method: str, **kwargs) -> Response | None:
        r: Response | None = None
        retries = 0
        conn_errors = 0
        max_conn_errors = max(1, tries) * CONNECT_RETRIES_PROXY_MULT
        session = self._thread_session()
        while retries < tries:
            self.catch_cancel_or_ctrl_c()
            r = None
            try:
                self._respect_rate_limit()
                r = session.request(method, url, timeout=self.timeout, stream=False, allow_redirects=True, **kwargs)
                r.raise_for_status()
                break
            except (KeyboardInterrupt, ThreadInterruptException):
                thread_exit('interrupted by user.', code=1)
            except Exception as err:
                if isinstance(err, CLIENT_CONNECTOR_ERRORS):
                    conn_errors += 1
                    if conn_errors >= max_conn_errors:
                        trace(f'Too many proxy / ssl errors ({conn_errors:d}), check your proxy settings. Aborting {url}', True)
                        return None
                else:
                    retries += 1
                sleep_time = 1.0
                threadname = f'{current_process().name}: ' if current_process() != self.my_root_thread else ''
                response = err.response if isinstance(err, exceptions.HTTPError) else None
                if is_cloudflare_challenge(response):
                    self.cf_challenge_hit = True
                    trace(f'{threadname}{CF_CHALLENGE_HINT}\nAborting {url}', True)
                    return None
                if response is not None and response.status_code == 404:
                    if r is not None and r.content and len(r.content.decode(errors='replace')) > 2:
                        trace(f'{threadname}received code 404 but received html'
                              f'\n{format_exception("row").strip()}.\nContinuing...', True)
                        break
                    trace(f'{threadname}catched err 404 {format_exception("row")}. Aborting...', True)
                    return None
                elif response is not None and response.status_code in (429, 503):  # Too Many Requests / Service Unavailable
                    retry_after = parse_retry_after(response.headers.get('Retry-After'))
                    sleep_time = retry_after if retry_after > 0 else sleep_time + float(min(9, retries))
                    trace(f'{threadname}catched {format_exception("row")}.'
                          f'{f" Reconnecting in {sleep_time:.1f} sec... {retries:d}" if retries < tries else ""}', True)
                else:
                    trace(f'{threadname}catched {format_exception("row")}.'
                          f'{f" Reconnecting in {sleep_time:.1f} sec... {retries:d}" if retries < tries else ""}', True)
                if not self.is_killed():
                    time.sleep(sleep_time)
                continue

        if retries >= tries:
            trace(f'Unable to connect. Aborting {url}', True)
            r = None
        elif r is None:
            trace('ERROR: Failed to receive any data', True)
        elif len(r.cookies) > 0:
            with self._cookies_lock:
                t = self.add_cookies.copy()
                self.add_cookies = r.cookies.copy()
                self.add_cookies.update(t)

        return r

    # threaded
    def fetch_html(self, url: str, *, tries=0, do_cache=False, method='GET', **kwargs) -> HTMLContent | None:
        if cached := self.raw_html_cache.get(url, HTMLContent.init_empty()):
            return cached
        r = self.wrap_request(url, tries or self.retries, method, **kwargs)
        content = r.content if r is not None else None
        result = HTMLContent(BeautifulSoup(content, 'html.parser'), content) if content else None
        if result and do_cache:
            self.raw_html_cache[url] = result
        return result

#
#
#########################################
