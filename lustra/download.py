# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

from __future__ import annotations

import contextlib
import itertools
import json
import os
import pathlib
import sys
import time
from abc import abstractmethod
from argparse import Namespace
from collections import deque
from collections.abc import Callable, Iterable, Iterator, MutableSet
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from multiprocessing.dummy import current_process
from threading import Condition, Lock, local
from typing import Any

from .defines import (
    CONNECT_TIMEOUT_BASE,
    DATE_MAX_DEFAULT,
    DATE_MIN_DEFAULT,
    INT_BOUNDS_DEFAULT,
    PLATFORM_WINDOWS,
    SOURCE_DEFAULT,
    UTF8,
    APIKey,
    DownloaderOptions,
    DownloaderStates,
    DownloadModes,
    ItemInfo,
    Mem,
    ModuleCaps,
    ModuleConfigType,
    ThreadInterruptException,
)
from .download_base import DownloaderBase
from .file_parser import prepare_item_infos_dict
from .gender import GenderConf
from .gui_defines import NEWLINE, OPTION_CMD_APIKEY_CMD, UNDERSCORE
from .logger import trace
from .module import ProcModule
from .network import DownloadInterruptException, ThreadedHtmlWorker, thread_exit
from .rex import re_favorited_by_tag, re_infolist_filename, re_pool_tag
from .tagger import append_filtered_tags
from .tags_parser import convert_taglist
from .tagsdb import load_tag_aliases
from .task import extract_neg_and_groups, split_tags_into_tasks
from .utils import (
    confirm_yes_no,
    format_exception,
    format_score,
    garble_argument_values,
    make_subfolder_name,
    trim_underscores,
    unique_ordered,
)
from .vcs import __LUSTRA_DEBUG__, APP_NAME, APP_VERSION

__all__ = ('Downloader',)


PAGE_EMPTY_RETRIES = 3


class CursorUnsupported(Exception):
    """the website ignores the id cursor: the streaming pipeline falls back to page numbers"""


ENV_STREAMING = 'LUSTRA_STREAMING'
'''0 disables the streaming pipeline (debug / comparison)'''
STREAM_BACKLOG = 64
'''items queued for download beyond 4 per thread before scanning waits'''
'''an empty / overloaded search page is retried this many times, then skipped (Ruxx retried forever)'''
LINE_BREAKS_AT = 99 if sys.platform == PLATFORM_WINDOWS else 90
BR = '=' * LINE_BREAKS_AT
'''line breaker'''


class Downloader(DownloaderBase):
    """
    Downloader !Abstract!
    """
    @abstractmethod
    def __init__(self) -> None:
        super().__init__()
        self._thread_exception_lock = Lock()
        self._item_tls = local()
        self.failed_pages: list[int] = []
        self.task_counts: list[int] = []
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._executors_lock = Lock()
        self._dl_cond = Condition()
        self._dl_inflight = 0
        self.streaming_active: bool = False
        '''set when the run uses the streaming pipeline (downloads start while scanning)'''
        self.stream_scanning: bool = False
        self._streamed: bool = False
        self.cursor_base_tags: str = ''
        self.tags_limit: int = 0
        '''-tags_limit: account tags limit overriding the module default (0: module default)'''
        '''task tags of the cursor scan (modules append their cursor metatags to them)'''
        self.launch_error: str = ''
        '''set when the run stopped because of a fatal error / unhandled exception'''
        self.interrupted: bool = False
        self._thread_exceptions: dict[str, list[str]] = {}
        self._file_name_ext_cache: dict[str, tuple[str, str]] = {}
        self.no_conflict_delay: bool = False
        '''interactive frontends (desktop sidecar) skip the 2s pause after argument conflict warnings'''

    @property
    def total_count_all(self) -> int:
        return len(self.items_raw_all)

    def __del__(self) -> None:
        self.__cleanup()

    def __enter__(self) -> Downloader:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.__cleanup()

    def __cleanup(self) -> None:
        # self.current_state = DownloaderStates.IDLE
        # self._file_name_ext_cache.clear()  # do not
        self.raw_html_cache.clear()
        self.filtered_out_ids_cache.clear()
        if self.session:
            self.session.close()
            self.session = None
        self._thread_exceptions.clear()

    # threaded
    def _on_thread_exception(self, thread_name: str) -> None:
        with self._thread_exception_lock:
            if thread_name not in self._thread_exceptions:
                self._thread_exceptions[thread_name] = []
            self._thread_exceptions[thread_name].append(format_exception('full'))
            if not getattr(self._item_tls, 'active', False):
                if not self.is_killed() and not self.launch_error:
                    exc = sys.exc_info()[1]
                    self.launch_error = (str(exc) if isinstance(exc, ThreadInterruptException) else '') or \
                        f'{thread_name}: {format_exception("row")}'
                self.my_root_thread.killed = True

    # threaded
    def _process_item_safe(self, raw: str) -> None:
        """A failing item is counted as failed, it does not abort the whole run (Ruxx killed every thread)"""
        self._item_tls.active = True
        try:
            self._process_item(raw)
        except DownloadInterruptException:
            raise
        except Exception:  # thread_exit() for this item included
            if self.is_killed():
                raise  # cancelled
            item_id = ''
            with contextlib.suppress(Exception):
                item_id = self._extract_id(self._local_addr_from_string(str(raw)))
            trace(f'Warning (W3): item {item_id} failed: {format_exception("row")}', True)
            with self.item_lock:
                self.fail_count += 1
                self.failed_items.append(item_id or '?')
            self._inc_proc_count()
        finally:
            self._item_tls.active = False

    # threaded
    def _inc_proc_count(self) -> None:
        if self.maxthreads_items > 1:
            with self.item_lock:
                self.processed_count += 1
        else:
            self.processed_count += 1

    def save_cmdline(self, cmdline: Iterable[str]) -> None:
        self.cmdline = list(cmdline)

    def set_options(self, **options: bool | int | str) -> None:
        self.options = options

    def _at_launch(self) -> None:
        if self.verbose:
            if self.hide_personal_info or self.options.get(DownloaderOptions.OPTION_GARBLE_PERSONAL_INFO, False):
                api_key_default = self.get_module_specific_default_value(ModuleConfigType.CONFIG_API_KEY)
                api_key_is_default = self.api_key.key == api_key_default
                garble_argument_values(self.cmdline, *((OPTION_CMD_APIKEY_CMD,) if not api_key_is_default else ()))
            trace(f'Python {sys.version}\nBase args: {" ".join(_[_.rfind("/") + 1:] for _ in sys.argv)}'
                  f'\nMy args: {" ".join(self.cmdline)}')

    def get_tags_count(self, offset=0) -> int:
        """Public, needed by tests"""
        return len(self.tags_str_arr[offset].split(self._get_tags_concat_char()))

    def _register_parent_post(self, parents: MutableSet[str], item_id: str) -> None:
        if item_id not in self.known_parents:
            parents.add(item_id)

    def _try_append_extra_info(self, item_abbrname: str, maxlen: int) -> str:
        if not self.append_info:
            return item_abbrname

        if maxlen <= 0:
            return item_abbrname

        info = self.item_info_dict_all.get(item_abbrname)
        if info is None:
            return item_abbrname

        add_string = format_score(info.score)
        add_string = append_filtered_tags(add_string, info.tags, self.get_re_tags_to_process(), self.get_re_tags_to_exclude())
        add_string = trim_underscores(add_string)
        if len(add_string) > 0:
            add_string = f'{UNDERSCORE}{add_string}'
        while len(add_string) > maxlen:
            add_string = add_string[:max(0, add_string.rfind(UNDERSCORE))]

        return f'{item_abbrname}{add_string}'

    # threaded
    def _download(self, link: str, item_id: str, dest: pathlib.Path) -> None:
        if self.download_mode != DownloadModes.SKIP:
            with self.item_lock:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    thread_exit('ERROR: Unable to create subfolder!')

        try:
            result = self.download_file(link, item_id, dest, self.download_mode, self.preserve_date)
        except DownloadInterruptException:
            return

        if self.download_mode == DownloadModes.TOUCH or 0 < result.file_size == result.expected_size:
            result.result_str = f'{result.result_str}done ({result.file_size / Mem.MB:.2f} Mb)'
            with self.item_lock:
                self.success_count += 1
        else:
            result.result_str = f'{result.result_str}failed'
            if result.reason:
                result.result_str = f'{result.result_str} ({result.reason})'
            elif result.retries >= self.retries:
                result.result_str = f'{result.result_str} (could not download file after {result.retries:d} tries)'
            with self.item_lock:
                self.fail_count += 1
                self.failed_items.append(item_id)

        trace(result.result_str, True)

    @abstractmethod
    def _send_to_download(self, raw: str, item_id: str, is_video: bool) -> None:
        ...

    def _process_image(self, raw: str, item_id: str) -> None:
        self._send_to_download(raw, item_id, False)

    def _process_video(self, raw: str, item_id: str) -> None:
        self._send_to_download(raw, item_id, True)

    @abstractmethod
    def _process_item(self, raw: str) -> None:
        ...

    # threaded
    def _get_page_items(self, n: int, c_page: int, page_max: int) -> None:
        if self.is_killed():
            return

        items_per_page = self._get_items_per_page()
        pnum = n + 1
        prev_c = (c_page - 1) * items_per_page
        curmax_c = min(c_page * items_per_page, self.total_count_old)

        if not self.get_max_id:
            trace(f'page: {pnum:d} / {page_max + 1:d}\t({prev_c + 1:d}-{curmax_c:d} / {self.total_count_old:d})', True)

        try:
            attempts = 0
            while True:
                if self.is_killed():
                    return
                attempts += 1
                try:
                    raw_html_page = self.fetch_html(self._form_page_num_address(n))
                    if raw_html_page is None:
                        trace(f'ERROR: ProcPage: unable to retreive html for page {pnum:d}!', True)
                        raise ConnectionError

                    if self._is_search_overload_page(raw_html_page.html):
                        trace(f'Warning (W2): search was dropped on page {pnum:d} (too many threads?), retrying...', True)
                        raise KeyError

                    items_raw_temp = self._argswitch2(self._get_all_post_tags, raw_html_page.raw, raw_html_page.html, self._parses_raw_pages())
                    if len(items_raw_temp) == 0:
                        trace(f'ERROR: ProcPage: cannot find picture or video on page {pnum:d}, retrying...', True)
                        if __LUSTRA_DEBUG__:
                            trace(f'HTML:\n{raw_html_page!s}', True)
                        raise KeyError

                    break
                except ConnectionError:
                    items_raw_temp = []
                    with self.items_all_lock:
                        self.failed_pages.append(pnum)
                    break
                except (KeyError, ValueError):
                    items_raw_temp = []
                    if attempts >= PAGE_EMPTY_RETRIES:
                        # count was stale (posts deleted meanwhile) or the website keeps failing: accept an empty page
                        trace(f'Warning (W2): page {pnum:d} is still empty after {attempts:d} attempts, skipping it', True)
                        with self.items_all_lock:
                            self.failed_pages.append(pnum)
                        break
                    self.sleep_interruptible(min(10.0, max(CONNECT_TIMEOUT_BASE / 4, self.timeout / 2)))
                    continue

            # convert to pure strings
            page_items = [str(item) for item in items_raw_temp]
            with self.items_all_lock:
                self.items_raw_per_page[n] = page_items
                self.total_count = sum(len(v) for v in self.items_raw_per_page.values())

            if (page_delay := self._get_page_fetch_delay()) > 0:
                time.sleep(page_delay)
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    def _resolve_task_pages(self, tag_str: str) -> bool:
        """Count, search depth checks and page range (id / date boundaries) of a task.
        Returns True if pages minpage..maxpage must be scanned. A single match redirect fills items_raw_per_task directly"""
        self.current_state = DownloaderStates.SEARCHING
        self.url = self._form_tags_search_address(tag_str)

        page_size = self._get_items_per_page()
        total_count_or_html = self._get_items_query_size_or_html(self.url)

        if not isinstance(total_count_or_html, int):
            # we have been redirected to a page with our single result! compose item string manually
            if __LUSTRA_DEBUG__:
                trace('Warning (W1): A single match redirect was hit, forming item string manually')
            self.items_raw_per_task[:] = [self._form_item_string_manually(total_count_or_html)]
            self.total_count = 1
            return False

        self.total_count = total_count_or_html
        self.minpage = 0
        self.maxpage = (self.total_count - 1) // page_size

        if self.total_count <= 0:
            trace('Nothing to process: query result is empty')
            return False

        trace(f'Total {self.total_count:d} item(s) found across {self._num_pages():d} page(s)')

        has_native_id_filter = self._supports_native_id_filter()
        if 0 < self._get_max_search_depth() <= self.total_count:
            max_depth_exceeded = self._get_max_search_depth() < self.total_count
            max_depth_pages = (self._get_max_search_depth() + page_size - 1) // page_size
            msg_base = f'too many results, can only fetch html for {max_depth_pages:d} pages'
            if has_native_id_filter or max_depth_exceeded:
                if has_native_id_filter:
                    trace(f'\nFATAL: {msg_base}! Try adding an ID filter.\n')
                else:
                    trace(f'\nFATAL: {msg_base}! Try to refine your search.\n')
                self.total_count = 0
                return False
            else:
                trace(f'\nWarning (W3): {msg_base}!\n')
                self.sleep_interruptible(4.0)

        if has_native_id_filter is False and self._get_id_bounds() != INT_BOUNDS_DEFAULT:
            pageargs_ex1 = ((DownloaderStates.SCANNING_PAGES1, True), (DownloaderStates.SCANNING_PAGES1, False))

            def page_filter_ex1(st: DownloaderStates, di: bool) -> int:
                self.current_state = st
                trace(f'[{self._get_module_abbr().upper()}] Looking for {"min" if di else "max"} page by id...')
                return self._get_page_boundary_by_id(di, self._get_id_bounds()[1 - di])

            for fstate, direction in pageargs_ex1:
                if direction is True:
                    self.minpage = page_filter_ex1(fstate, direction)
                else:
                    self.maxpage = page_filter_ex1(fstate, direction)

        pageargs = ((DownloaderStates.SCANNING_PAGES1, True), (DownloaderStates.SCANNING_PAGES2, False))

        def page_filter(st: DownloaderStates, di: bool) -> int:
            self.current_state = st
            if not self.favorites_search_user and not self.pool_search_str and not self._supports_native_date_filter():
                trace(f'Looking for {"min" if di else "max"} page by date...')
                return self._get_page_boundary_by_date(di)
            else:
                return self.minpage if di else self.maxpage

        for fstate, direction in pageargs:
            if direction is True:
                self.minpage = page_filter(fstate, direction)
            else:
                self.maxpage = page_filter(fstate, direction)

        self.total_count = min(self._num_pages() * page_size, self.total_count)
        trace(f'new totalcount: {self.total_count:d}')
        trace(f'Scanning pages {self.minpage + 1:d} - {self.maxpage + 1:d}')

        self.total_count_old = self.total_count
        self.total_count = 0
        return True

    def _scan_pages(self, pages: list[int]) -> list[str]:
        """Fetches search pages (in parallel on the persistent 'scan' executor), returns their items in search order"""
        self.current_state = DownloaderStates.SCANNING_PAGES2
        self.items_raw_per_page.clear()
        if self.maxthreads_items > 1 and len(pages) > 1:
            self._run_parallel('scan', self._get_page_items, [(n, n - self.minpage + 1, self.maxpage) for n in pages])
        else:
            for n in pages:
                self.catch_cancel_or_ctrl_c()
                self._get_page_items(n, n - self.minpage + 1, self.maxpage)
        items = unique_ordered(itertools.chain(*(self.items_raw_per_page.get(n, []) for n in sorted(pages))))
        self.items_raw_per_page.clear()
        if self.failed_pages:
            trace(f'Warning (W3): {len(self.failed_pages):d} search page(s) could not be fetched, some posts may be missing: '
                  f'{", ".join(str(x) for x in sorted(self.failed_pages))}')
            self.failed_pages.clear()
        return items

    def _fetch_task_items(self, tag_str: str) -> None:
        if not self._resolve_task_pages(tag_str):
            return
        if self.maxthreads_items > 1 and self._num_pages() > 1:
            trace(f'  ...using {self._executor_workers("scan"):d} threads...')
        self.items_raw_per_task[:] = self._scan_pages(list(range(self.minpage, self.maxpage + 1)))

    # ---------------- persistent executors (C5) ----------------

    def _executor_workers(self, kind: str) -> int:
        if kind == 'scan':
            return max(2, self.maxthreads_items // max(1, self._get_page_scan_threads_divisor()))
        return max(1, self.maxthreads_items)

    def _get_executor(self, kind: str) -> ThreadPoolExecutor:
        """'scan' (search pages), 'info' (item pages of modules without item info in listings) and 'dl' (files) executors live
        for the whole run: worker threads and their keep-alive sessions are reused (Ruxx created a new pool per phase)"""
        with self._executors_lock:
            ex = self._executors.get(kind)
            if ex is None:
                ex = ThreadPoolExecutor(max_workers=self._executor_workers(kind), thread_name_prefix=kind)
                self._executors[kind] = ex
            return ex

    def _shutdown_executors(self) -> None:
        with self._executors_lock:
            executors = list(self._executors.values())
            self._executors.clear()
        for ex in executors:
            ex.shutdown(wait=True, cancel_futures=True)

    def _run_parallel(self, kind: str, fn: Callable[..., Any], args_list: Iterable[tuple], *, ordered=False,
                      on_result: Callable[[Future], None] | None = None, window=0) -> None:
        """Runs fn(*args) for every args on the executor 'kind' with at most 'window' calls queued (Ruxx queued everything at once:
        100k items ~ 155 MB of pending objects). Cancel is checked every 0.25 s; queued calls are dropped on cancel.
        ordered: on_result gets futures in submission order (else in completion order)"""
        ex = self._get_executor(kind)
        window = window or self._executor_workers(kind) * 4
        pending: deque[Future] = deque()
        it = iter(args_list)
        exhausted = False
        try:
            while True:
                while not exhausted and len(pending) < window:
                    args = next(it, None)
                    if args is None:
                        exhausted = True
                        break
                    pending.append(ex.submit(fn, *args))
                if not pending:
                    return
                self.catch_cancel_or_ctrl_c()
                if ordered:
                    wait((pending[0],), timeout=0.25)
                    while pending and pending[0].done():
                        fut = pending.popleft()
                        if on_result:
                            on_result(fut)
                else:
                    done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                    for fut in done:
                        pending.remove(fut)
                        if on_result:
                            on_result(fut)
        finally:
            for fut in pending:
                fut.cancel()

    # ---------------- streaming pipeline (C1, C7) ----------------

    def _can_stream_task(self) -> bool:
        """Single task queries are scanned in download order and downloaded while scanning continues.
        Multi task queries, parent posts, favorites and pools keep the classic scan-everything-then-download pipeline
        (their ordering / cross task filters need the complete lists)"""
        return (os.environ.get(ENV_STREAMING, '1') != '0' and self._tasks_count() == 1 and self.orig_tasks_count == 1
                and not self.include_parchi and not self.favorites_search_user and not self.pool_search_str
                and not self.get_max_id and not self.check_tags)

    def _order_for_download(self, items: list[str]) -> list[str]:
        """Same order as the classic pipeline: ascending ids (default sort) or search order (custom sort), reversed by -reverse"""
        if self.default_sort:
            return sorted(items, key=lambda x: int(self._extract_id(self._local_addr_from_string(str(x)))),
                          reverse=self.reverse_download_order)
        return list(reversed(items)) if self.reverse_download_order else list(items)

    def _iter_offset_batches(self, pages: list[int]) -> Iterator[tuple[list[str], bool, bool]]:
        group = self._executor_workers('scan') if self.maxthreads_items > 1 else 1
        for i in range(0, len(pages), group):
            chunk = pages[i:i + group]
            yield self._scan_pages(chunk), self.minpage in chunk, self.maxpage in chunk

    def _fetch_page_by_address(self, address: str, label: str) -> list[str]:
        for attempt in range(1, PAGE_EMPTY_RETRIES + 1):
            self.catch_cancel_or_ctrl_c()
            raw_html_page = self.fetch_html(address)
            if raw_html_page is None:
                thread_exit(f'ERROR: ProcPage: unable to retreive {label}, the result would be incomplete', -16)
            if err := self._page_error(raw_html_page.raw):
                thread_exit(f'ERROR: [{self._get_module_abbr().upper()}] {err}', -17)
            if self._is_search_overload_page(raw_html_page.html) and attempt < PAGE_EMPTY_RETRIES:
                trace(f'Warning (W2): search was dropped on {label}, retrying...', True)
                self.sleep_interruptible(min(10.0, max(CONNECT_TIMEOUT_BASE / 4, self.timeout / 2)))
                continue
            items = self._argswitch2(self._get_all_post_tags, raw_html_page.raw, raw_html_page.html, self._parses_raw_pages())
            return [str(item) for item in items]
        return []

    def _iter_cursor_batches(self, ascending: bool) -> Iterator[tuple[list[str], bool, bool]]:
        """Id cursor pages (danbooru 'page=a<id>' / 'page=b<id>', gelbooru 'id:>N sort:id:asc' / 'id:<N'): no page depth limit
        and no offset drift during long scans. Ends on an empty page (pages can be short when the website hides some posts).
        The first page is checked by the module (_cursor_first_page_ok): CursorUnsupported is raised before anything is yielded"""
        def fetch(cur: int | None, n: int) -> list[str]:
            trace(f'page: {n:d} (id cursor {cur if cur is not None else "start"})', True)
            page_items = self._fetch_page_by_address(self._form_cursor_page_address(cur, ascending), f'page {n:d}')
            if (page_delay := self._get_page_fetch_delay()) > 0:
                self.sleep_interruptible(page_delay)
            return page_items

        cursor: int | None = None
        num = 1
        self.current_state = DownloaderStates.SCANNING_PAGES2
        items = fetch(None, num)
        while items:
            ids = [int(self._extract_id(self._local_addr_from_string(x))) for x in items]
            if cursor is None and not self._cursor_first_page_ok(ids, ascending):
                raise CursorUnsupported
            if cursor is not None and (min(ids) <= cursor if ascending else max(ids) >= cursor):
                thread_exit(f'ERROR: the website ignored the id cursor (page {num:d}, cursor {cursor:d}), the result would be '
                            f'incomplete. Run again with LUSTRA_STREAMING=0 to use page numbers', -19)
            cursor = max(ids) if ascending else min(ids)
            num += 1
            # the next page only depends on the cursor: it is fetched while this batch is filtered and downloaded
            next_page: Future = self._get_executor('scan').submit(fetch, cursor, num)
            try:
                yield items, False, False
                self.current_state = DownloaderStates.SCANNING_PAGES2
                while not next_page.done():
                    self.catch_cancel_or_ctrl_c()
                    wait((next_page,), timeout=0.25)
                items = next_page.result()
            finally:
                next_page.cancel()

    def _filter_stream_batch(self, has_first_page: bool, has_last_page: bool) -> list[str]:
        """The per task filter chain of _process_tags applied to one batch (items_raw_per_task).
        Date edge filters only matter for the batches holding the first / last page of the range"""
        if has_last_page:
            self._apply_filter(DownloaderStates.FILTERING_ITEMS1, self._filter_last_items)
        if has_first_page:
            self._apply_filter(DownloaderStates.FILTERING_ITEMS2, self._filter_first_items)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS3, self._filter_items_by_type)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_existing_items)
        task_parents: set[str] = set()
        self._extract_cur_task_infos(task_parents)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_safety, task_parents)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_matching_negative_and_groups, task_parents)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_missing_required_tags, task_parents)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_by_module_filters, task_parents)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_by_gender, task_parents)
        self.item_info_dict_all.update(self.item_info_dict_per_task)
        self.item_info_dict_per_task.clear()
        return list(self.items_raw_per_task)

    def _submit_download(self, raw: str) -> None:
        with self._dl_cond:
            self._dl_inflight += 1
        self._get_executor('dl').submit(self._process_item_safe, raw).add_done_callback(self._on_download_done)

    def _on_download_done(self, _: Future) -> None:
        with self._dl_cond:
            self._dl_inflight -= 1
            self._dl_cond.notify_all()

    def _drain_downloads(self, keep: int) -> None:
        """Waits until at most 'keep' downloads are queued or running (backpressure: scanning never runs far ahead of downloading).
        O(1) per finished file (waiting on thousands of futures each time was quadratic)"""
        while True:
            self.catch_cancel_or_ctrl_c()
            with self._dl_cond:
                if self._dl_inflight <= keep:
                    return
                self._dl_cond.wait(0.25)

    def _process_tags_streaming(self, tag_str: str) -> None:
        task_tags = self._consume_custom_module_tags(tag_str)
        scan_backward = self.default_sort != self.reverse_download_order  # oldest first: last pages first
        use_cursor = self.default_sort and self._supports_id_cursor()
        batches: Iterator[tuple[list[str], bool, bool]] | None = None
        if use_cursor:
            self.current_state = DownloaderStates.SEARCHING
            self.cursor_base_tags = task_tags
            self.url = self._form_tags_search_address(task_tags)
            count = self._get_query_size_or_none(self.url)
            if count == 0:
                trace('Nothing to process: query result is empty')
                self.total_count = 0
                return
            if self._prefer_id_cursor(count):
                trace(f'Total {f"{count:d}" if count is not None else "unknown number of"} item(s) found, scanning by id cursor'
                      f' ({"oldest" if scan_backward else "newest"} first, no page limit)')
                cursor_pages = self._iter_cursor_batches(ascending=scan_backward)
                try:
                    first = next(cursor_pages, None)  # nothing is downloaded before the first page is validated
                    batches = itertools.chain([first] if first else [], cursor_pages)
                except CursorUnsupported:
                    trace('Warning (W2): the website does not sort by id as expected, scanning by page numbers instead', True)
        if batches is None:  # page numbers
            if self._resolve_task_pages(task_tags):
                pages = list(range(self.minpage, self.maxpage + 1))
                if scan_backward:
                    pages.reverse()
                batches = self._iter_offset_batches(pages)
            elif self.items_raw_per_task:  # single match redirect
                batches = iter([(list(self.items_raw_per_task), True, True)])
            else:
                return
        self._stream_run(batches)

    def _stream_run(self, batches: Iterator[tuple[list[str], bool, bool]]) -> None:
        load_tag_aliases()
        limit = self.download_limit
        threaded = self.maxthreads_items > 1
        backlog = self._executor_workers('dl') * 4 + STREAM_BACKLOG
        seen: set[str] = set()
        found = 0
        self.streaming_active = True
        self.stream_scanning = True
        self._existing_ids_cache = None
        trace(f'Streaming: downloads start while pages are scanned{f" (limit {limit:d})" if limit > 0 else ""}, '
              f'{self.maxthreads_items:d} thread(s) max\n')
        try:
            for items, has_first_page, has_last_page in batches:
                self.catch_cancel_or_ctrl_c()
                found += len(items)
                batch = []
                for x in items:
                    item_id = self._extract_id(self._local_addr_from_string(x))
                    if item_id not in seen:  # pages may overlap if posts were added during the scan
                        seen.add(item_id)
                        batch.append(x)
                if not batch:
                    continue
                self.items_raw_per_task[:] = batch
                self.total_count = len(batch)
                survivors = self._order_for_download(self._filter_stream_batch(has_first_page, has_last_page))
                if limit > 0:
                    survivors = survivors[:max(0, limit - self.total_count_all)]
                self.items_raw_all.extend(survivors)
                self.total_count = found
                self.current_state = DownloaderStates.DOWNLOADING
                for raw in survivors:
                    if threaded:
                        self._submit_download(raw)
                    else:
                        self.catch_cancel_or_ctrl_c()
                        self._process_item_safe(raw)
                if limit > 0 and self.total_count_all >= limit:
                    trace(f'\nDownload limit reached ({limit:d}): scanning stopped early')
                    break
                self._drain_downloads(max(backlog, len(survivors)))  # one batch may wait: the next one is scanned meanwhile
            self.stream_scanning = False
            self.current_state = DownloaderStates.DOWNLOADING
            trace(f'\nScan complete: {found:d} found, {self.total_count_all:d} item(s) scheduled')
            self._drain_downloads(0)
        finally:
            self.stream_scanning = False  # queued downloads are dropped by _shutdown_executors (cancel / error)
            self._existing_ids_cache = None
            self.raw_html_cache.clear()
        self._streamed = True
        if self.total_count_all > 0:
            minmax_ids = self._extract_minmax_id()
            skip_all = self.download_mode == DownloadModes.SKIP
            trace(f'\nAll {"skipped" if skip_all else "processed"} ({self.total_count_all:d} item(s)), '
                  f'bound {minmax_ids[0]:d} to {minmax_ids[1]:d}...')
        else:
            trace('\nNothing to download: queue is empty')

    def _extract_custom_argument_tags(self) -> None:
        fav_user_tags = list(filter(None, [re_favorited_by_tag.fullmatch(t) for t in self.tags_str_arr]))
        self._extract_favorite_user(fav_user_tags)
        if self.favorites_search_user and self._is_fav_search_conversion_required():
            [self.tags_str_arr.remove(f.string) for f in fav_user_tags]
        pool_tags = list(filter(None, [re_pool_tag.fullmatch(t) for t in self.tags_str_arr]))
        self._extract_pool_id(pool_tags)
        if self.pool_search_str and self._is_pool_search_conversion_required():
            [self.tags_str_arr.remove(f.string) for f in pool_tags]
        assert not (self.favorites_search_user and self.pool_search_str)

    def _try_preprocess_favorites(self) -> None:
        # all we need here is to gather post ids from user's favorites page(s)
        if self.favorites_search_user:
            convert_msg = ', additional search will be performed' if self._is_fav_search_conversion_required() else ''
            trace(f'Favorites search detected ({self.favorites_search_user}){convert_msg}')
            if self._is_fav_search_conversion_required():
                if self._is_fav_search_single_step():
                    return
                try:
                    self._fetch_task_items(self.favorites_search_user)
                    self._extract_cur_task_infos(set())
                    ids_list = sorted(int(self.item_info_dict_per_task[full_id].id) for full_id in self.item_info_dict_per_task)
                    cc = self._get_tags_concat_char()
                    sc = self._get_idval_equal_seaparator()
                    ids_tag_base = f'{cc}~{cc}'.join(f'id{sc}{ii:d}' for ii in ids_list)
                    self.tags_str_arr[0:] = [f'({cc}{ids_tag_base}{cc})' if len(ids_list) > 1 else ids_tag_base, *self.tags_str_arr]
                    self.items_raw_per_task.clear()
                    self.item_info_dict_per_task.clear()
                    self.total_count_old = self.total_count = 0
                except ThreadInterruptException:
                    trace(f'task {self.current_task_num:d} aborted...')
                    raise
                except Exception:
                    trace(f'task {self.current_task_num:d} failed...')
                    raise
            self._clean_favorite_user()

    def _try_preprocess_pool(self) -> None:
        # all we need here is to gather post ids from pool page(s)
        if self.pool_search_str:
            convert_msg = ', additional search will be performed' if self._is_pool_search_conversion_required() else ''
            trace(f'Pool search detected ({self.pool_search_str}){convert_msg}')
            if self._is_pool_search_conversion_required():
                try:
                    self._fetch_task_items(self.pool_search_str)
                    self._extract_cur_task_infos(set())
                    ids_list = sorted(int(self.item_info_dict_per_task[full_id].id) for full_id in self.item_info_dict_per_task)
                    cc = self._get_tags_concat_char()
                    sc = self._get_idval_equal_seaparator()
                    ids_tag_base = f'{cc}~{cc}'.join(f'id{sc}{ii:d}' for ii in ids_list)
                    self.tags_str_arr[0:] = [f'({cc}{ids_tag_base}{cc})' if len(ids_list) > 1 else ids_tag_base, *self.tags_str_arr]
                    self.items_raw_per_task.clear()
                    self.item_info_dict_per_task.clear()
                    self.total_count_old = self.total_count = 0
                except ThreadInterruptException:
                    trace(f'task {self.current_task_num:d} aborted...')
                    raise
                except Exception:
                    trace(f'task {self.current_task_num:d} failed...')
                    raise
            self._clean_pool_id()

    def _after_filter(self, sleep_time: float | None = None) -> None:
        self.total_count = len(self.items_raw_all if self.current_state == DownloaderStates.DOWNLOADING else self.items_raw_per_task)
        trace(f'new totalcount: {self.total_count:d}')
        if sleep_time:
            time.sleep(sleep_time)

    def _apply_filter(self, state: DownloaderStates, func: Callable[..., None], *args, sleep_time: float | None = None) -> None:
        self.current_state = state
        func(*args)
        self._after_filter(sleep_time)

    def _process_tags(self, tag_str: str) -> None:
        if self._can_stream_task():
            self._process_tags_streaming(tag_str)
            return
        self._fetch_task_items(self._consume_custom_module_tags(tag_str))

        if self.total_count <= 0:
            return

        self._after_filter(0.025)

        self._apply_filter(DownloaderStates.FILTERING_ITEMS1, self._filter_last_items)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS2, self._filter_first_items)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS3, self._filter_items_by_type)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_by_previous_tasks)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_existing_items)

        # store items info for future processing
        # custom filters may exclude certain items from the infos dict
        task_parents: set[str] = set()
        self._extract_cur_task_infos(task_parents)
        # safety blacklist: every task incl. parent tasks, only while the content filter is enabled (no-op otherwise)
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_safety, task_parents)

        if self.current_task_num <= self.orig_tasks_count:
            self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_matching_negative_and_groups, task_parents)
            self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_missing_required_tags, task_parents)
            self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_by_module_filters, task_parents)
            # gender filter last: runs on the fewest items; parent tasks are user-requested context and are not gender-filtered
            self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_by_gender, task_parents)

        self.raw_html_cache.clear()
        self.items_raw_all: list[str] = unique_ordered(itertools.chain(self.items_raw_all, self.items_raw_per_task))
        self.item_info_dict_all.update(self.item_info_dict_per_task)
        if self.current_task_num > 1:
            trace(f'overall totalcount: {self.total_count_all:d}')

        if len(task_parents) > 0:
            trace(f'\nParent post(s) detected! Scheduling {len(task_parents):d} extra task(s)!')
            parent_messages: list[str] = []
            for parent in sorted(task_parents):
                new_task_str = f'parent{self._get_idval_equal_seaparator()}{parent}'
                self.tags_str_arr.append(new_task_str)
                parent_messages.append(f' {new_task_str}')
            trace('\n'.join(parent_messages))
            self.known_parents.update(task_parents)

    def _download_all(self) -> None:
        if self._streamed:
            return  # downloaded while scanning
        if self.total_count_all > 0:
            if self.default_sort:
                self.items_raw_all = sorted(self.items_raw_all, key=lambda x: int(self._extract_id(x)))
                if self.current_task_num > 1:
                    trace(f'\nApplying overall date filter after {self._tasks_count()} tasks...')
                    self.items_raw_all.reverse()
                    self._apply_filter(DownloaderStates.DOWNLOADING, self._filter_last_items)
                    self._apply_filter(DownloaderStates.DOWNLOADING, self._filter_first_items)
                    self.items_raw_all.reverse()
                    ids_to_preserve = set[str]()
                    ids_to_pop = set[str]()
                    for hstr in self.items_raw_all:
                        h = self._local_addr_from_string(str(hstr))
                        ids_to_preserve.add(self._item_idstring(self._extract_id(h)))
                    for ifi in self.item_info_dict_all:
                        if ifi not in ids_to_preserve:
                            ids_to_pop.add(ifi)
                    [self.item_info_dict_all.pop(ifi) for ifi in ids_to_pop]

        if self.total_count_all <= 0:
            trace('\nNothing to download: queue is empty')
            return

        if self.reverse_download_order:
            self.items_raw_all.reverse()

        if self.download_limit > 0:
            if self.total_count_all > self.download_limit:
                trace(f'\nShrinking queue down to {self.download_limit:d} item(s)...')
                del self.items_raw_all[self.download_limit:]
            else:
                trace('\nShrinking queue down is not required!')

        minmax_ids = self._extract_minmax_id()
        trace(f'\nProcessing {self.total_count_all:d} item(s), bound {minmax_ids[0]:d} to {minmax_ids[1]:d}')

        load_tag_aliases()

        self.current_state = DownloaderStates.DOWNLOADING
        trace(f'{self.total_count_all:d} item(s) scheduled, {self.maxthreads_items:d} thread(s) max\nWorking...\n')

        if self.maxthreads_items > 1 and self.total_count_all > 1:
            self._run_parallel('dl', self._process_item_safe, ((iarr,) for iarr in self.items_raw_all))
        else:
            for iraw in self.items_raw_all:
                self.catch_cancel_or_ctrl_c()
                self._process_item_safe(iraw)

        skip_all = self.download_mode == DownloadModes.SKIP
        trace(f'\nAll {"skipped" if skip_all else "processed"} ({self.total_count_all:d} item(s))...')

    def _extract_negative_and_groups(self) -> None:
        split_always = self._split_or_group_into_tasks_always()
        self.required_tags.clear()
        self.tags_str_arr[:], self.neg_and_groups = extract_neg_and_groups(' '.join(self.tags_str_arr), split_always, self.required_tags,
                                                                           self.tags_limit)

    def _parse_tags(self) -> None:
        cc = self._get_tags_concat_char()
        sc = self._get_idval_equal_seaparator()
        split_always = self._split_or_group_into_tasks_always()
        # join by ' ' is required by tests, although normally len(args.tags) == 1
        for t in self.tags_str_arr:
            if len(t) > 2 and f'{t[0]}{t[-1]}' == '()' and f'{t[:2]}{t[-2:]}' != '(++)':
                thread_exit(f'Error: invalid tag \'{t}\'! Looks like \'or\' group but not fully contatenated by \'+\'')
        # conflict: non-default sorting
        self.default_sort = not any(self._is_custom_sort_tag(tag) for tag in self.tags_str_arr)
        self.tags_str_arr[:] = split_tags_into_tasks(self.tags_str_arr, cc, sc, split_always)
        self.orig_tasks_count = self._tasks_count()

    def _process_all_tags(self) -> None:
        if self.warn_nonempty and self.dest_base.is_dir():
            with os.scandir(self.dest_base.as_posix()) as listing:
                if next(listing, None):
                    if not confirm_yes_no('Download', f'Destination folder \'{self.dest_base}\' is not empty. Continue anyway?'):
                        return

        trace(f'\n{BR}\n{APP_NAME} core ver {APP_VERSION}')
        trace(f'Starting {self._get_module_abbr().upper()} downloader', timestamp=True)
        trace(f'\nDownload mode: \'{self.download_mode}\'')
        trace(f'{len(self.neg_and_groups):d} \'excluded tags combination\' custom filter(s) parsed')
        if self.required_tags:
            trace(f'{len(self.required_tags):d} tag(s) will be checked locally: {" ".join(self.required_tags)}')
        trace(f'{self._tasks_count():d} task(s) scheduled:\n{NEWLINE.join(self.tags_str_arr)}\n\n{BR}')
        self.current_task_num = 0
        while self.current_task_num < self._tasks_count():  # tasks count may increase during this loop
            self.current_task_num += 1
            cur_task_tags = self.tags_str_arr[self.current_task_num - 1]
            if self.options.get(DownloaderOptions.OPTION_CREATE_SUBFOLDERS, False) is True:
                self.current_task_subfolder = make_subfolder_name(
                    cur_task_tags,
                    self.options.get(DownloaderOptions.OPTION_SUBFOLDER_TASK_NUM, self.current_task_num),
                    self.options.get(DownloaderOptions.OPTION_SUBFOLDER_TASKS_COUNT, self._tasks_count()),
                )
            extra_task_num = self.current_task_num - self.orig_tasks_count
            extra_task_str = f'[extra {extra_task_num:d}] ' if extra_task_num > 0 else ''
            trace(f'\n{extra_task_str}task {self.current_task_num:d} / {self._tasks_count():d} in progress...\n{cur_task_tags}\n')
            try:
                self._process_tags(cur_task_tags)
            except ThreadInterruptException:
                trace(f'task {self.current_task_num:d} aborted...')
                raise
            except Exception:
                trace(f'task {self.current_task_num:d} failed...')
                raise
        try:
            self._download_all()
        except ThreadInterruptException:
            trace('download aborted...')
            raise
        except Exception:
            trace('download failed...')
            raise
        self._dump_all_info()
        total_files = min(self.success_count + self.fail_count, self.total_count_all)
        success_files = min(self.success_count, self.total_count_all - self.fail_count)
        trace(f'\n{self._tasks_count():d} task(s) completed, {success_files:d} / {total_files:d} item(s) succeded', False, True)
        if len(self.failed_items) > 0:
            trace(f'{len(self.failed_items):d} failed item(s):')
            trace('\n'.join(self.failed_items))

    def _check_tags(self) -> None:
        """Sum of the counts of all tasks. With several tasks (split OR groups) a post can match more than one task:
        the sum is an upper bound (task_counts holds the per task values)"""
        self.task_counts.clear()
        for task_str in self.tags_str_arr:
            self.catch_cancel_or_ctrl_c()
            self.url = self._form_tags_search_address(self._consume_custom_module_tags(task_str))
            total_count_or_html = self._get_items_query_size_or_html(self.url, tries=1)
            self.task_counts.append(total_count_or_html if isinstance(total_count_or_html, int) else 1)
        self.total_count = sum(self.task_counts)

    def _get_max_id(self) -> None:
        self.include_parchi = False
        self.url = self._form_tags_search_address(self._get_maxid_fetch_tags_string(), 1)
        count_or_html = self._get_items_query_size_or_html(self.url)
        if isinstance(count_or_html, int):
            self.total_count = count_or_html
            self._get_page_items(0, 1, self.maxpage)
            self.items_raw_per_task = self.items_raw_per_page[0][:1]
        else:
            self.total_count = 1
            self.items_raw_per_task = [self._form_item_string_manually(count_or_html)]
        trace(f'{self._get_module_abbr().upper()}: {self._extract_id(self.items_raw_per_task[0])}')

    def _launch(self, args: Namespace, thiscall: Callable[[], None], enable_preprocessing=True) -> None:
        self.current_state = DownloaderStates.LAUNCHING
        self.reset_root_thread(current_process())
        self.launch_error = ''
        self.interrupted = False
        self._streamed = False
        self.streaming_active = False
        try:
            self._parse_args(args, enable_preprocessing)
            self._at_launch()
            thiscall()
        except KeyboardInterrupt:
            self.interrupted = True
            trace(f'\nInterrupted by \'{format_exception("name")}\'!\n', True)
        except ThreadInterruptException as err:
            # thread_exit(): fatal error (message already logged), a failed worker thread or cancelled by user
            if not self.launch_error:
                if self.is_killed():
                    self.interrupted = True
                else:
                    self.launch_error = str(err) or 'fatal error'
            trace(f'\nInterrupted by \'{format_exception("name")}\'!\n', True)
        except Exception:
            self.launch_error = format_exception('row')
            trace(f'Unhandled exception: {self.launch_error}', True)
        finally:
            self._shutdown_executors()  # in-flight workers see the cancel flag and stop, queued work is dropped
            self.current_state = DownloaderStates.IDLE
        if self._thread_exceptions and self.maxthreads_items > 1:
            n = '\n'
            trace(f'Catched thread exception(s):\n{n.join(n.join(self._thread_exceptions[exck]) for exck in self._thread_exceptions)}')

    def launch_download(self, args: Namespace) -> None:
        """Public, needed by core"""
        self._launch(args, self._process_all_tags)

    def launch_check_tags(self, args: Namespace) -> None:
        """Public, needed by core"""
        self.check_tags = True
        self._launch(args, self._check_tags, False)

    def launch_get_max_id(self, args: Namespace) -> None:
        """Public, needed by core"""
        self.get_max_id = True
        self._launch(args, self._get_max_id, False)

    def _parse_args(self, args: Namespace, enable_preprocessing=True) -> None:
        assert hasattr(args, 'tags') and type(args.tags) is list
        self.api_key = APIKey(args.api_key) or self.api_key
        ThreadedHtmlWorker._parse_args(self, args)
        self.hide_personal_info = args.hide_personal_info or self.hide_personal_info
        self.add_filename_prefix = args.prefix or self.add_filename_prefix
        self.dump_tags = args.dump_tags or self.dump_tags
        self.dump_sources = args.dump_sources or self.dump_sources
        self.dump_comments = args.dump_comments or self.dump_comments
        self.dump_hashes = args.dump_hashes or self.dump_hashes
        self.dump_per_item = args.dump_per_item or self.dump_per_item
        self.merge_lists = args.merge_lists or self.merge_lists
        self.append_info = args.append_info or self.append_info
        self.download_mode = args.dmode or self.download_mode
        self.download_limit = args.dlimit or self.download_limit
        self.reverse_download_order = args.reverse or self.reverse_download_order
        self.maxthreads_items = args.threads or self.maxthreads_items
        self.include_parchi = args.include_parchi or self.include_parchi
        self.skip_images = args.skip_img or self.skip_images
        self.skip_videos = args.skip_vid or self.skip_videos
        self.prefer_webm = args.webm or self.prefer_webm
        self.prefer_mp4 = args.mp4 or self.prefer_mp4
        self.low_res = args.lowres or self.low_res
        self.date_min = args.mindate or self.date_min
        self.date_max = args.maxdate or self.date_max
        self.preserve_date = args.preserve_date or self.preserve_date
        self.dest_base = args.path or self.dest_base
        self.subfolder_vid = args.vidsub or self.subfolder_vid
        self.subfolder_img = args.imgsub or self.subfolder_img
        self.warn_nonempty = args.warn_nonempty or self.warn_nonempty
        self.api_key = APIKey(args.api_key) or self.api_key
        self.gender_select = frozenset(getattr(args, 'gender', None) or ()) or self.gender_select
        self.gender_mode = getattr(args, 'gender_mode', None) or self.gender_mode
        self.gender_min_conf = GenderConf.parse(args.gender_conf) if getattr(args, 'gender_conf', None) else self.gender_min_conf
        self.gender_strict = getattr(args, 'gender_strict', False) or self.gender_strict
        self.tags_limit = getattr(args, 'tags_limit', None) or self.tags_limit
        self.tags_str_arr[:] = [] if self.get_max_id else convert_taglist(args.tags)
        self._extract_negative_and_groups()
        self._extract_custom_argument_tags()
        if enable_preprocessing:
            self._try_preprocess_favorites()
            self._try_preprocess_pool()
        self._parse_tags()
        if self._solve_argument_conflicts() and not self.no_conflict_delay:
            time.sleep(2.0)

    def _solve_argument_conflicts(self) -> bool:
        # fatal
        if not self.default_sort:
            if self._tasks_count() > 1:
                thread_exit('Error: cannot use non-default sorting with multi-task query!')
            if self.date_min != DATE_MIN_DEFAULT or self.date_max != DATE_MAX_DEFAULT:
                thread_exit('Error: cannot use both sort tag and date filter at the same time!')
        # non-fatal
        ret = False
        if self.warn_nonempty and not self._has_gui():
            trace('Warning (W1): argument \'-warn_nonempty\' is ignored in non-GUI mode')
            self.warn_nonempty = False
            ret = True
        abbr = ProcModule.name().upper()
        if self._has_capability(ModuleCaps.API_KEY_REQUIRED) and not self.api_key:
            trace(f'Warning (W3): NO \'{abbr}\' API KEY PROVIDED! DEFAULT API KEY MAY STOP WORKING AT ANY MOMENT!')
            ret = True
        if not self._has_capability(ModuleCaps.API_KEY) and self.api_key:
            trace(f'Warning (W1): \'-api_key\' option is not available for \'{abbr}\' module. Ignored!')
            ret = True
        if not self._has_capability(ModuleCaps.VIDEO_FORMAT_CHOICE):
            if self.prefer_webm:
                trace(f'Warning (W1): \'-webm\' option is not available for \'{abbr}\' module. Ignored!')
                ret = True
            if self.prefer_mp4:
                trace(f'Warning (W1): \'-mp4\' option is not available for \'{abbr}\' module. Ignored!')
                ret = True
        if not self._has_capability(ModuleCaps.PARENT_CHILD) and self.include_parchi:
            trace(f'Warning (W1): \'{abbr}\' module is unable to collect parent posts. Disabled!')
            self.include_parchi = False
            ret = True
        if not self._has_capability(ModuleCaps.SOURCES) and self.dump_sources:
            trace(f'Warning (W1): \'{abbr}\' module is unable to collect sources. Disabled!')
            self.dump_sources = False
            ret = True
        if not self._has_capability(ModuleCaps.DATE_FILTER) and (self.date_min != DATE_MIN_DEFAULT or self.date_max != DATE_MAX_DEFAULT):
            trace(f'Warning (W1): \'{abbr}\' module is unable to filter by date. Disabled!')
            self.date_min, self.date_max = DATE_MIN_DEFAULT, DATE_MAX_DEFAULT
            ret = True
        if not self._has_capability(ModuleCaps.COMMENTS) and self.dump_comments:
            trace(f'Warning (W1): \'{abbr}\' module comments collection is disabled.')
            self.dump_comments = False
            ret = True
        if self.gender_select:
            if self.gender_mode == 'model':
                thread_exit('Error: \'-gender_mode model\' needs the gender model which is not available yet. Use \'-gender_mode tags\'')
            if not self._has_capability(ModuleCaps.GENDER_TAGS):
                trace(f'Warning (W2): \'{abbr}\' module has no measured gender tag rules, most posts will be labeled \'unknown\'')
                ret = True
        ret |= self._solve_module_specific_conflicts()
        return ret

    def _extract_cur_task_infos(self, parents: MutableSet[str]) -> None:
        def put_info(item_info: ItemInfo) -> None:
            if self.include_parchi is True:
                if item_info.has_children == 'true':
                    self._register_parent_post(parents, item_info.id)
                if item_info.parent_id.isnumeric():
                    self._register_parent_post(parents, item_info.parent_id)
            idstring = f'{(abbrp if self.add_filename_prefix else "")}{item_info.id}'
            self.item_info_dict_per_task[idstring] = item_info
            if len(item_info.source) < 2:
                item_info.source = SOURCE_DEFAULT
            if __LUSTRA_DEBUG__ and not self.favorites_search_user and not self.pool_search_str:
                for key in item_info.__slots__:
                    val = getattr(item_info, key)
                    if key not in ItemInfo.optional_slots and not val:
                        trace(f'Info: extract info {abbrp}{item_info.id}: uninitialized field \'{key}\'!')
                    if isinstance(val, str) and val and val[-1] == ' ':
                        trace(f'Info: extract info {abbrp}{item_info.id}: trailing space in field \'{key}\': {val}!')

        abbrp = self._get_module_abbr_p()
        if self._can_extract_item_info_without_fetch() or self.maxthreads_items < 2 or len(self.items_raw_per_task) < 2:
            for item in self.items_raw_per_task:
                self.catch_cancel_or_ctrl_c()
                res = self._extract_item_info(item)
                put_info(res)
        else:  # RS
            def on_info(fut: Future) -> None:
                try:
                    put_info(fut.result())
                except Exception:
                    if self.is_killed():
                        raise
                    trace(f'Warning (W3): unable to extract item info: {format_exception("row")}')
            self._run_parallel('info', self._extract_item_info, ((elem,) for elem in self.items_raw_per_task), ordered=True,
                               on_result=on_info)

    def _dump_all_info(self) -> None:
        if len(self.item_info_dict_all) == 0:
            return
        if not any((self.dump_tags, self.dump_sources, self.dump_comments, self.dump_hashes)):
            return

        try:
            self.dest_base_s.mkdir(parents=True, exist_ok=True)
        except Exception:
            thread_exit(f'ERROR: Unable to create folder {self.dest_base_s}!')

        abbrp = self._get_module_abbr_p()
        merged_files = self._try_merge_info_files(
            self.dest_base_s, self.item_info_dict_all, abbrp, self.add_filename_prefix,
        ) if self.merge_lists and self.dest_base_s.is_dir() else []
        item_info_list = sorted(self.item_info_dict_all.values())
        id_begin = item_info_list[0].id
        id_end = item_info_list[-1].id
        saved_files = set[pathlib.Path]()

        trace('\nSaving post infos...')

        if self.dump_per_item:
            for iinfo in item_info_list:
                ifilepath = self.dest_base_s / f'{abbrp}!info_{iinfo.id}.json'
                saved_files.add(ifilepath)
                if self._has_gui() and ifilepath.is_file():
                    if not confirm_yes_no(title='Save info', msg=f'File \'{ifilepath.as_posix()}\' already exists. Overwrite?'):
                        trace('Skipped.')
                        continue
                with open(ifilepath, 'wt', encoding=UTF8, errors='backslashreplace') as idump:
                    json.dump([asdict(iinfo)], idump, indent=1, sort_keys=False, ensure_ascii=False)
                    idump.write('\n')
        else:
            filepath = self.dest_base_s / f'{abbrp}!info_{id_begin}-{id_end}.json'
            saved_files.add(filepath)
            can_write = True
            if self._has_gui() and filepath.is_file():
                if not confirm_yes_no(title='Save info', msg=f'File \'{filepath.as_posix()}\' already exists. Overwrite?'):
                    trace('Skipped.')
                    can_write = False
            if can_write:
                with open(filepath, 'wt', encoding=UTF8, errors='backslashreplace') as dump:
                    json.dump([asdict(_) for _ in item_info_list], dump, indent=1, sort_keys=False, ensure_ascii=False)
                    dump.write('\n')

        trace('Done.')
        [merged_file.unlink() for merged_file in merged_files if merged_file not in saved_files]
        trace(BR)

    @staticmethod
    def _try_merge_info_files(dest: pathlib.Path, items_dict: dict[str, ItemInfo], prefix: str, add_prefix: bool) -> list[pathlib.Path]:
        parsed_files: list[pathlib.Path] = []
        with os.scandir(dest.as_posix()) as listing:
            info_lists = sorted(filter(
                None, (re_infolist_filename.fullmatch(f.name) for f in listing
                       if f.is_file() and f.name.startswith(f'{prefix}!'))), key=lambda m: m.string)
        parsed_dict: dict[str, ItemInfo] = {}
        for fmatch in info_lists:
            fmname = fmatch.string
            list_fullpath = dest / fmname
            try:
                suc, file_item_infos = prepare_item_infos_dict(list_fullpath, prefix if add_prefix else '')
                assert suc
                parsed_dict.update(file_item_infos)
                parsed_files.append(list_fullpath)
            except AssertionError:
                trace(f'Error reading from {fmname}. Skipped')
                continue
        for k, v in parsed_dict.items():
            if k not in items_dict:
                items_dict[k] = v
        return parsed_files

    def _has_gui(self) -> bool:
        return hasattr(self.my_root_thread, 'gui')

#
#
#########################################
