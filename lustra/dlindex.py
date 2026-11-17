# coding=UTF-8
"""
Lustra: local index of downloaded files, <data dir>/index/downloads.sqlite
- one row per file that finished downloading: module, post id, md5, path, tags, score, rating, post date, download time
- metadata only: files are never opened or hashed, the md5 is the one reported by the website
- written by a background thread in batches (download threads only enqueue, see IndexWriter), WAL journal: several CLI
  processes may write at the same time (writers wait for each other, BUSY_TIMEOUT), the desktop sidecar reads meanwhile
- read by '-skip_known' (posts whose md5 or module + id is known and whose file is still on disk are not downloaded again)
  and by the sidecar methods 'index.search' / 'index.stats'
- LUSTRA_DLINDEX=0 disables recording (reading still works)
- maintenance (lustra/dlindex_tools.py): clean_missing() drops rows whose file is gone (rows only, never files), the library
  import adds files already on disk, rename_paths() follows files renamed by the re-tag / sort tools
"""
#########################################
#
#

from __future__ import annotations

import contextlib
import datetime
import os
import pathlib
import queue
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .defines import FMT_DATE, ItemInfo
from .logger import trace

__all__ = (
    'ENV_DLINDEX', 'CleanResult', 'DownloadIndex', 'IndexRecord', 'IndexWriter', 'file_present', 'index_path', 'indexing_enabled',
    'norm_date', 'norm_md5', 'parse_search_terms', 'record_for', 'rename_indexed_files',
)

ENV_DLINDEX = 'LUSTRA_DLINDEX'
'''0: finished downloads are not recorded (the index stays readable, -skip_known keeps working)'''
INDEX_DIR_NAME = 'index'
INDEX_FILE_NAME = 'downloads.sqlite'
SCHEMA_VERSION = 1
BUSY_TIMEOUT = 10.0
'''seconds a connection waits while another process holds the write lock'''
WRITE_ATTEMPTS = 3
'''a batch is retried this many times when the database stays locked longer than BUSY_TIMEOUT, then dropped'''
BATCH_MAX = 200
FLUSH_INTERVAL = 1.5
'''seconds a record may wait in the writer queue before its batch is written'''
CLOSE_TIMEOUT = 45.0
SQL_CHUNK = 400
'''bound parameters per IN (...) list (old SQLite builds allow 999 per statement)'''
TAG_CACHE_MAX = 200_000
SEARCH_PAGE_SIZE_MAX = 200
SEARCH_TERMS_MAX = 24
TAG_MAX_CHAR = '\U0010ffff'
'''upper bound of a prefix range: name >= prefix AND name < prefix + this'''
CLEAN_BATCH = 500
'''rows checked on disk per step of clean_missing(): each step deletes in its own short transaction'''

re_md5 = re.compile(r'[0-9a-f]{32}')
re_int = re.compile(r'[-+]?\d+')
re_rating_attr = re.compile(r'\brating["\']?\s*[=:]\s*["\']?([A-Za-z]+)')
RATINGS = {
    'g': 'g', 'general': 'g', 's': 's', 'safe': 's', 'sensitive': 's', 'q': 'q', 'questionable': 'q', 'e': 'e', 'explicit': 'e',
}
'''website ratings -> one letter (danbooru 's' is 'sensitive', gelbooru 's' is 'safe': kept as reported)'''

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY,
        module TEXT NOT NULL,
        post_id INTEGER NOT NULL,
        md5 TEXT,
        path TEXT NOT NULL UNIQUE,
        ext TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        tags TEXT NOT NULL DEFAULT '',
        score INTEGER,
        rating TEXT NOT NULL DEFAULT '',
        post_date TEXT NOT NULL DEFAULT '',
        downloaded_at REAL NOT NULL
    )''',
    'CREATE INDEX IF NOT EXISTS files_module_post ON files(module, post_id)',
    'CREATE INDEX IF NOT EXISTS files_md5 ON files(md5)',
    'CREATE INDEX IF NOT EXISTS files_post_date ON files(post_date)',
    'CREATE INDEX IF NOT EXISTS files_downloaded_at ON files(downloaded_at)',
    'CREATE TABLE IF NOT EXISTS tag_names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)',
    '''CREATE TABLE IF NOT EXISTS file_tags (
        tag_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        PRIMARY KEY (tag_id, file_id)
    ) WITHOUT ROWID''',
    'CREATE INDEX IF NOT EXISTS file_tags_file ON file_tags(file_id)',
)
'''post_id has INTEGER affinity: numeric ids are stored (and compared) as numbers'''

UPSERT_FILE = (
    'INSERT INTO files (module, post_id, md5, path, ext, size, tags, score, rating, post_date, downloaded_at) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
    'ON CONFLICT(path) DO UPDATE SET module = excluded.module, post_id = excluded.post_id, md5 = excluded.md5, ext = excluded.ext, '
    'size = excluded.size, tags = excluded.tags, score = excluded.score, rating = excluded.rating, post_date = excluded.post_date, '
    'downloaded_at = excluded.downloaded_at'
)
'''one row per file path: downloading a post again into the same file refreshes its row'''

ROW_COLUMNS = 'f.module, f.post_id, f.md5, f.path, f.ext, f.size, f.tags, f.score, f.rating, f.post_date, f.downloaded_at'
SEARCH_ORDERS = {
    'downloaded': 'f.downloaded_at DESC, f.id DESC',
    'downloaded_asc': 'f.downloaded_at ASC, f.id ASC',
    'date': 'f.post_date DESC, f.downloaded_at DESC',
    'date_asc': "f.post_date = '' ASC, f.post_date ASC, f.downloaded_at ASC",
    'score': 'f.score IS NULL ASC, f.score DESC, f.downloaded_at DESC',
    'id': 'f.module ASC, f.post_id DESC',
}
TAG_FILES_EQ = 'SELECT file_id FROM file_tags WHERE tag_id = (SELECT id FROM tag_names WHERE name = ?)'
TAG_FILES_PREFIX = 'SELECT file_id FROM file_tags WHERE tag_id IN (SELECT id FROM tag_names WHERE name >= ? AND name < ?)'


def index_path() -> pathlib.Path:
    from .extensions import data_dir  # local import: keeps this module free of the extensions loader at import time
    return data_dir() / INDEX_DIR_NAME / INDEX_FILE_NAME


def indexing_enabled() -> bool:
    return os.environ.get(ENV_DLINDEX, '1').strip() != '0'


# ----- normalization -----

def norm_md5(value: str) -> str:
    v = str(value or '').strip().lower()
    return v if re_md5.fullmatch(v) else ''


def norm_date(value: str) -> str:
    """Core 'DD-MM-YYYY' or ISO 'YYYY-MM-DD[...]' -> 'YYYY-MM-DD' ('' if not a date)"""
    v = str(value or '').strip()[:10]
    if not v:
        return ''
    with contextlib.suppress(ValueError):
        return datetime.datetime.strptime(v, FMT_DATE).date().isoformat()
    with contextlib.suppress(ValueError):
        return datetime.date.fromisoformat(v).isoformat()
    return ''


def norm_score(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    m = re_int.search(str(value or ''))
    return int(m.group()) if m else None


def norm_rating(value: str) -> str:
    return RATINGS.get(str(value or '').strip().lower(), '')


def rating_of(raw: str, tags: str) -> str:
    """Rating from the raw item string (rating="e", "rating":"e") or a 'rating:x' tag, '' if unknown"""
    if raw and (m := re_rating_attr.search(raw)) and (r := norm_rating(m.group(1))):
        return r
    for t in str(tags or '').split():
        if t.startswith('rating:') and (r := norm_rating(t[7:])):
            return r
    return ''


def norm_tags(tags: str) -> list[str]:
    return list(dict.fromkeys(t for t in str(tags or '').lower().split() if t))


def _db_post_id(post_id: str | int) -> str | int:
    s = str(post_id).strip()
    return int(s) if s.isdigit() else s


def _chunks(seq: Sequence, n: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _qs(n: int) -> str:
    return ','.join('?' * n)


def file_present(path: str) -> bool:
    """The indexed file is still on disk (regular, not empty). A stat only, the file is not opened"""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


@dataclass(slots=True)
class IndexRecord:
    """A finished file. post_date: 'YYYY-MM-DD' or ''"""
    module: str
    post_id: str
    path: str
    md5: str = ''
    tags: str = ''
    score: int | None = None
    rating: str = ''
    post_date: str = ''
    ext: str = ''
    size: int = 0
    downloaded_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class CleanResult:
    """clean_missing() counts. missing: rows whose file is gone; missing_dirs: those of them whose folder is gone too (an unplugged
    drive looks the same: kept unless asked); removed: rows deleted (0 for a dry run)"""
    total: int = 0
    checked: int = 0
    missing: int = 0
    missing_dirs: int = 0
    removed: int = 0
    dry_run: bool = False
    include_missing_dirs: bool = False
    cancelled: bool = False
    folders: list[str] = field(default_factory=list)
    '''missing folders (at most CLEAN_FOLDERS_MAX), for the report'''

    def as_dict(self) -> dict:
        return {'total': self.total, 'checked': self.checked, 'missing': self.missing, 'missingDirs': self.missing_dirs,
                'removed': self.removed, 'dryRun': self.dry_run, 'missingFolders': self.include_missing_dirs, 'cancelled': self.cancelled,
                'folders': list(self.folders)}


CLEAN_FOLDERS_MAX = 50


def record_for(module: str, info: ItemInfo, post_id: str, dest: str | os.PathLike, size: int, raw: str = '') -> IndexRecord:
    """IndexRecord of a downloaded file from the post info collected during the scan (no extra request, the file is not read)"""
    path = os.path.abspath(os.fspath(dest))
    ext = os.path.splitext(path)[1][1:].lower() or str(info.ext or '').lower()
    return IndexRecord(module=module, post_id=str(info.id or post_id), path=path, md5=norm_md5(info.md5),
                       tags=' '.join(norm_tags(info.tags)), score=norm_score(info.score), rating=rating_of(raw, info.tags),
                       post_date=norm_date(info.date), ext=ext, size=int(size))


def parse_search_terms(text: str | Sequence[str]) -> list[tuple[bool, str, str]]:
    """'tag -tag pre* rating:e md5:<hash> id:<n>' -> [(negated, kind, value)], kind: tag | prefix | rating | md5 | id.
    Unusable terms are dropped, at most SEARCH_TERMS_MAX are kept"""
    words = text.split() if isinstance(text, str) else [w for x in text for w in str(x).split()]
    terms: list[tuple[bool, str, str]] = []
    for word in words:
        w = word.strip().lower()
        neg = w.startswith('-')
        w = w.lstrip('-')
        if not w or w == '*':
            continue
        name, _, value = w.partition(':')
        if name == 'rating' and (r := norm_rating(value)):
            terms.append((neg, 'rating', r))
        elif name == 'md5' and (m := norm_md5(value)):
            terms.append((neg, 'md5', m))
        elif name == 'id' and value.isdigit():
            terms.append((neg, 'id', value))
        elif w.endswith('*') and '*' not in w[:-1]:
            terms.append((neg, 'prefix', w[:-1]))
        elif '*' not in w:
            terms.append((neg, 'tag', w))
        if len(terms) >= SEARCH_TERMS_MAX:
            break
    return terms


class DownloadIndex:
    """A connection to the index (one thread at a time). Readers never create the file: an index that does not exist yet is
    empty. Writers (create=True) create it and its schema"""

    def __init__(self, path: str | os.PathLike | None = None, *, create: bool = False) -> None:
        self.path = pathlib.Path(path) if path else index_path()
        self._create = create
        self._conn: sqlite3.Connection | None = None
        self._tag_ids: dict[str, int] = {}

    def __enter__(self) -> DownloadIndex:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def _connection(self) -> sqlite3.Connection | None:
        if self._conn is None:
            if not self._create and not self.path.is_file():
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT, isolation_level=None, check_same_thread=False)
            try:
                conn.execute(f'PRAGMA busy_timeout = {int(BUSY_TIMEOUT * 1000):d}')
                conn.execute('PRAGMA journal_mode = WAL')
                conn.execute('PRAGMA synchronous = NORMAL')
                self._ensure_schema(conn)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        if conn.execute('PRAGMA user_version').fetchone()[0] >= SCHEMA_VERSION:
            return
        conn.execute('BEGIN IMMEDIATE')  # another process may be creating it right now
        try:
            if conn.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION:
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION:d}')
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise

    # ----- write -----

    def _tag_ids_for(self, conn: sqlite3.Connection, names: set[str]) -> dict[str, int]:
        if len(self._tag_ids) + len(names) > TAG_CACHE_MAX:
            self._tag_ids.clear()
        missing = [n for n in names if n not in self._tag_ids]
        if missing:
            conn.executemany('INSERT OR IGNORE INTO tag_names (name) VALUES (?)', ((n,) for n in missing))
            for chunk in _chunks(missing, SQL_CHUNK):
                for tag_id, name in conn.execute(f'SELECT id, name FROM tag_names WHERE name IN ({_qs(len(chunk))})', chunk):
                    self._tag_ids[name] = tag_id
        return {n: self._tag_ids[n] for n in names}

    def add_many(self, records: Sequence[IndexRecord]) -> int:
        """Writes records in one transaction (upsert by file path), returns their number"""
        if not records:
            return 0
        conn = self._connection()
        if conn is None:
            raise FileNotFoundError(f'download index does not exist: {self.path}')
        conn.execute('BEGIN IMMEDIATE')
        try:
            tag_ids = self._tag_ids_for(conn, {t for r in records for t in r.tags.split()})
            for r in records:
                conn.execute(UPSERT_FILE, (r.module, _db_post_id(r.post_id), norm_md5(r.md5) or None, r.path, r.ext, int(r.size),
                                           r.tags, r.score, r.rating, r.post_date, float(r.downloaded_at)))
                file_id = conn.execute('SELECT id FROM files WHERE path = ?', (r.path,)).fetchone()[0]
                conn.execute('DELETE FROM file_tags WHERE file_id = ?', (file_id,))
                conn.executemany('INSERT OR IGNORE INTO file_tags (tag_id, file_id) VALUES (?, ?)',
                                 ((tag_ids[t], file_id) for t in set(r.tags.split())))
            conn.execute('COMMIT')
        except BaseException:
            self._tag_ids.clear()  # ids cached by a rolled back transaction do not exist
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return len(records)

    # ----- read -----

    def find_known(self, module: str, post_ids: Iterable[str] = (), md5s: Iterable[str] = ()) -> tuple[dict[str, str], dict[str, str]]:
        """({post id: path} of this module's posts, {md5: path} of any module) whose indexed file is still on disk"""
        by_id: dict[str, str] = {}
        by_md5: dict[str, str] = {}
        conn = self._connection()
        if conn is None:
            return by_id, by_md5
        ids = list(dict.fromkeys(str(x).strip() for x in post_ids if str(x).strip()))
        for chunk in _chunks(ids, SQL_CHUNK):
            sql = f'SELECT post_id, path FROM files WHERE module = ? AND post_id IN ({_qs(len(chunk))})'
            for post_id, path in conn.execute(sql, (module, *(_db_post_id(x) for x in chunk))):
                if str(post_id) not in by_id and file_present(path):
                    by_id[str(post_id)] = path
        hashes = list(dict.fromkeys(h for h in (norm_md5(x) for x in md5s) if h))
        for chunk in _chunks(hashes, SQL_CHUNK):
            for md5, path in conn.execute(f'SELECT md5, path FROM files WHERE md5 IN ({_qs(len(chunk))})', chunk):
                if md5 not in by_md5 and file_present(path):
                    by_md5[md5] = path
        return by_id, by_md5

    @staticmethod
    def _search_filter(terms: list[tuple[bool, str, str]], module: str, date_from: str, date_to: str) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if module:
            clauses.append('f.module = ?')
            params.append(module)
        if d := norm_date(date_from):
            clauses.append('f.post_date >= ?')
            params.append(d)
        if d := norm_date(date_to):
            clauses.append("f.post_date <> '' AND f.post_date <= ?")
            params.append(d)
        for neg, kind, value in terms:
            if kind == 'rating':
                clause, args = 'f.rating = ?', [value]
            elif kind == 'md5':
                clause, args = 'f.md5 = ?', [value]
            elif kind == 'id':
                clause, args = 'f.post_id = ?', [_db_post_id(value)]
            elif kind == 'prefix':
                clause, args = f'f.id IN ({TAG_FILES_PREFIX})', [value, f'{value}{TAG_MAX_CHAR}']
            else:
                clause, args = f'f.id IN ({TAG_FILES_EQ})', [value]
            clauses.append(f'NOT ({clause})' if neg else clause)
            params.extend(args)
        return ' AND '.join(clauses) or '1', params

    @staticmethod
    def _row(r: tuple) -> dict:
        module, post_id, md5, path, ext, size, tags, score, rating, post_date, downloaded_at = r
        return {'module': module, 'id': str(post_id), 'md5': md5 or '', 'path': path, 'name': os.path.basename(path), 'ext': ext,
                'size': size, 'tags': tags, 'score': score, 'rating': rating, 'date': post_date, 'downloadedAt': downloaded_at,
                'exists': file_present(path)}

    def search(self, tags: str | Sequence[str] = '', *, module: str = '', date_from: str = '', date_to: str = '', page: int = 0,
               page_size: int = 50, order: str = 'downloaded') -> dict:
        """Files matching all terms (see parse_search_terms), module, post date range (inclusive). Pages start at 0"""
        page_size = max(1, min(SEARCH_PAGE_SIZE_MAX, int(page_size or 50)))
        page = max(0, int(page or 0))
        terms = parse_search_terms(tags)
        res = {'total': 0, 'page': page, 'pageSize': page_size, 'items': [], 'terms': [f'{"-" if n else ""}{k}:{v}' for n, k, v in terms]}
        conn = self._connection()
        if conn is None:
            return res
        where, params = self._search_filter(terms, module, date_from, date_to)
        res['total'] = conn.execute(f'SELECT COUNT(*) FROM files f WHERE {where}', params).fetchone()[0]
        order_by = SEARCH_ORDERS.get(order, SEARCH_ORDERS['downloaded'])
        sql = f'SELECT {ROW_COLUMNS} FROM files f WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?'
        res['items'] = [self._row(r) for r in conn.execute(sql, (*params, page_size, page * page_size))]
        return res

    def stats(self) -> dict:
        """Totals for the settings page. Files are not checked on disk (a stat per file would be slow on large indexes)"""
        db_bytes = 0
        for suffix in ('', '-wal', '-shm'):
            with contextlib.suppress(OSError):
                db_bytes += os.path.getsize(f'{self.path}{suffix}')
        out = {'path': str(self.path), 'exists': False, 'schema': 0, 'files': 0, 'posts': 0, 'withMd5': 0, 'tags': 0, 'bytes': 0,
               'dbBytes': db_bytes, 'first': None, 'last': None, 'modules': [], 'recording': indexing_enabled()}
        conn = self._connection()
        if conn is None:
            return out
        files, with_md5, size, first, last = conn.execute(
            'SELECT COUNT(*), COUNT(md5), COALESCE(SUM(size), 0), MIN(downloaded_at), MAX(downloaded_at) FROM files').fetchone()
        out.update({
            'exists': True, 'schema': conn.execute('PRAGMA user_version').fetchone()[0], 'files': files, 'withMd5': with_md5,
            'bytes': size, 'first': first, 'last': last,
            'posts': conn.execute('SELECT COUNT(*) FROM (SELECT 1 FROM files GROUP BY module, post_id)').fetchone()[0],
            'tags': conn.execute('SELECT COUNT(*) FROM tag_names').fetchone()[0],
            'modules': [{'module': m, 'files': n, 'bytes': b, 'last': t} for m, n, b, t in conn.execute(
                'SELECT module, COUNT(*), COALESCE(SUM(size), 0), MAX(downloaded_at) FROM files GROUP BY module ORDER BY COUNT(*) DESC')],
        })
        return out

    # ----- maintenance (lustra/dlindex_tools.py) -----

    def known_paths(self, paths: Iterable[str]) -> set[str]:
        """The given absolute paths that already have a row (whether the file is still on disk or not)"""
        conn = self._connection()
        out: set[str] = set()
        if conn is None:
            return out
        for chunk in _chunks(list(dict.fromkeys(paths)), SQL_CHUNK):
            out.update(p for (p,) in conn.execute(f'SELECT path FROM files WHERE path IN ({_qs(len(chunk))})', chunk))
        return out

    def rows_for_paths(self, paths: Iterable[str]) -> dict[str, dict]:
        """{path: row (see search items)} of the given absolute paths"""
        conn = self._connection()
        out: dict[str, dict] = {}
        if conn is None:
            return out
        for chunk in _chunks(list(dict.fromkeys(paths)), SQL_CHUNK):
            for r in conn.execute(f'SELECT {ROW_COLUMNS} FROM files f WHERE f.path IN ({_qs(len(chunk))})', chunk):
                out[r[3]] = self._row(r)
        return out

    def rows_for_posts(self, module: str, post_ids: Iterable[str]) -> dict[str, dict]:
        """{post id: row} of this module's posts (the most recent download of each), any path"""
        conn = self._connection()
        out: dict[str, dict] = {}
        if conn is None:
            return out
        ids = list(dict.fromkeys(str(x).strip() for x in post_ids if str(x).strip()))
        for chunk in _chunks(ids, SQL_CHUNK):
            sql = (f'SELECT {ROW_COLUMNS} FROM files f WHERE f.module = ? AND f.post_id IN ({_qs(len(chunk))}) '
                   'ORDER BY f.downloaded_at ASC')
            for r in conn.execute(sql, (module, *(_db_post_id(x) for x in chunk))):
                out[str(r[1])] = self._row(r)
        return out

    def rename_paths(self, pairs: Sequence[tuple[str, str]]) -> int:
        """Files moved / renamed on disk (re-tag, sort into subfolders): their rows follow (old path -> new path, absolute).
        A row already at the new path (a stale one) is replaced. Returns the number of rows moved"""
        pairs = [(str(a), str(b)) for a, b in pairs if str(a) != str(b)]
        conn = self._connection()
        if conn is None or not pairs:
            return 0
        moved = 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            for old, new in pairs:
                if conn.execute('SELECT 1 FROM files WHERE path = ?', (old,)).fetchone() is None:
                    continue
                if (stale := conn.execute('SELECT id FROM files WHERE path = ?', (new,)).fetchone()) is not None:
                    conn.execute('DELETE FROM file_tags WHERE file_id = ?', stale)
                    conn.execute('DELETE FROM files WHERE id = ?', stale)
                conn.execute('UPDATE files SET path = ? WHERE path = ?', (new, old))
                moved += 1
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return moved

    def clean_missing(self, *, include_missing_dirs: bool = False, dry_run: bool = False, batch: int = CLEAN_BATCH,
                      progress: Callable[[CleanResult], None] | None = None, cancel: threading.Event | None = None,
                      pause: float = 0.0) -> CleanResult:
        """Drops the rows whose file is no longer on disk (a stat per row, files are never opened, moved or deleted). Rows are read
        in id order, `batch` at a time; each batch's deletions are one short transaction, so download jobs keep recording meanwhile.
        A row rewritten meanwhile (the file downloaded again: new downloaded_at) is kept. Rows whose folder is gone as well are kept
        unless include_missing_dirs (an unplugged drive or unmounted share looks the same). The tag names stay (writers cache their
        ids). progress(result) after each batch; cancel stops between batches"""
        res = CleanResult(dry_run=dry_run, include_missing_dirs=include_missing_dirs)
        conn = self._connection()
        if conn is None:
            return res
        batch = max(1, int(batch))
        res.total = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        dirs: dict[str, bool] = {}
        last_id = 0
        while True:
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                break
            rows = conn.execute('SELECT id, path, downloaded_at FROM files WHERE id > ? ORDER BY id LIMIT ?', (last_id, batch)).fetchall()
            if not rows:
                break
            last_id = rows[-1][0]
            gone: list[tuple[int, str, float]] = []
            for file_id, path, at in rows:
                if file_present(path):
                    continue
                res.missing += 1
                folder = os.path.dirname(path)
                if (folder_ok := dirs.get(folder)) is None:
                    folder_ok = dirs[folder] = os.path.isdir(folder)
                    if not folder_ok and len(res.folders) < CLEAN_FOLDERS_MAX:
                        res.folders.append(folder)
                if not folder_ok:
                    res.missing_dirs += 1
                    if not include_missing_dirs:
                        continue
                gone.append((file_id, path, at))
            res.checked += len(rows)
            if gone and not dry_run:
                res.removed += self._delete_rows(conn, gone)
            if progress is not None:
                progress(res)
            if pause > 0:
                time.sleep(pause)
        return res

    @staticmethod
    def _delete_rows(conn: sqlite3.Connection, rows: Sequence[tuple[int, str, float]]) -> int:
        removed = 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            for file_id, path, at in rows:
                # same path and download time: a row the writer of a running job rewrote meanwhile is not touched
                if conn.execute('DELETE FROM files WHERE id = ? AND path = ? AND downloaded_at = ?', (file_id, path, at)).rowcount:
                    conn.execute('DELETE FROM file_tags WHERE file_id = ?', (file_id,))
                    removed += 1
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return removed


def rename_indexed_files(pairs: Sequence[tuple[str | os.PathLike, str | os.PathLike]]) -> int:
    """Best effort for file tools (re-tag, un-tag, sort): moved files keep their index rows. Never raises, an index that does not
    exist is not created. Returns the number of rows moved"""
    if not pairs:
        return 0
    try:
        with DownloadIndex() as index:
            return index.rename_paths([(os.path.abspath(os.fspath(a)), os.path.abspath(os.fspath(b))) for a, b in pairs])
    except Exception as err:
        trace(f'Warning (W2): download index: unable to follow {len(pairs):d} renamed file(s): {type(err).__name__}: {err}', True)
        return 0


class IndexWriter:
    """Download threads only enqueue records (add() never touches the database); a daemon thread writes them in batches, one
    transaction per batch. Failures never reach the download: they are counted and reported once"""
    _STOP = object()

    def __init__(self, path: str | os.PathLike | None = None, *, batch_max: int = BATCH_MAX, interval: float = FLUSH_INTERVAL) -> None:
        self._path = path
        self._batch_max = max(1, batch_max)
        self._interval = max(0.0, interval)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.written = 0
        self.failed = 0
        self.error = ''

    def add(self, record: IndexRecord) -> None:
        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='dlindex', daemon=True)
                self._thread.start()
        self._queue.put(record)

    def close(self, timeout: float = CLOSE_TIMEOUT) -> bool:
        """Writes what is queued and stops the thread. False if it did not finish in time (it keeps going as a daemon thread)"""
        with self._lock:
            if self._closed:
                thread = None
            else:
                self._closed = True
                thread = self._thread
                if thread is not None:
                    self._queue.put(self._STOP)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        index = DownloadIndex(self._path, create=True)
        batch: list[IndexRecord] = []
        deadline = 0.0
        try:
            while True:
                try:
                    item = self._queue.get(timeout=max(0.0, deadline - time.monotonic()) if batch else None)
                except queue.Empty:
                    item = None
                if item is self._STOP:
                    self._flush(index, batch)
                    break
                if item is not None:
                    if not batch:
                        deadline = time.monotonic() + self._interval
                    batch.append(item)
                if batch and (item is None or len(batch) >= self._batch_max or time.monotonic() >= deadline):
                    self._flush(index, batch)
        finally:
            index.close()

    def _flush(self, index: DownloadIndex, batch: list[IndexRecord]) -> None:
        if not batch:
            return
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                self.written += index.add_many(batch)
                break
            except sqlite3.OperationalError as err:
                busy = 'locked' in str(err) or 'busy' in str(err)
                if not busy or attempt == WRITE_ATTEMPTS:
                    self._fail(len(batch), err)
                    break
            except Exception as err:
                self._fail(len(batch), err)
                break
        batch.clear()

    def _fail(self, count: int, err: Exception) -> None:
        self.failed += count
        if not self.error:
            self.error = f'{type(err).__name__}: {err}'
            trace(f'Warning (W2): download index: unable to record {count:d} file(s): {self.error}', True)

#
#
#########################################
