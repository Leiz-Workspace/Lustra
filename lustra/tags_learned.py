# coding=UTF-8
"""
Lustra: tag lists learned from the posts already seen, for modules that have no list: sites without a tag listing (custom HTML,
shimmie without its api), sites whose robots.txt forbids it, or a list not downloaded yet. No request is made: only the tags of
the posts shown in Preview (server.m_search_preview) and of the downloaded files (the download index, lustra/dlindex.py).
The list is '<data dir>/tags/<abbr>_tags.json' in the bundled format, 'N posts (unknown)': N is the number of distinct posts seen
with the tag here, not the site's count. A list from a source always wins: once the updater, an installed extension or 2tags/
gives the module a list, the learning stops writing (the file it wrote last is stamped; any other file at that path, or another
list located for the module, means it is not ours any more).
State: '<data dir>/tags/learned/<abbr>.sqlite' (posts counted, tag counts, the index cursor, the stamp of the last write)
"""
#########################################
#
#

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import threading
from collections.abc import Iterable

from .modspec import re_abbr
from .tags_updater import write_tag_list
from .tagsdb import TagsDB

__all__ = ('ENV_TAGS_LEARN', 'LEARNED_TYPE', 'enabled', 'is_learned', 'learn', 'learn_from_index', 'list_path', 'may_learn', 'status')

ENV_TAGS_LEARN = 'LUSTRA_TAGS_LEARN'
'''0: nothing is learned (the offline tests set it, lustra/tests/__init__.py; the learning tests turn it on)'''

LEARNED_TYPE = 'unknown'
'''the posts do not say a tag's category'''
TAG_MAX_LENGTH = 200
TAGS_PER_POST_MAX = 1000
INDEX_STEP = 5000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, posts INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID;
'''

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock(module: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(module, threading.Lock())


def _tags_dir() -> pathlib.Path:
    from .extensions import data_dir
    return data_dir() / 'tags'


def list_path(module: str) -> pathlib.Path:
    return _tags_dir() / f'{module}_tags.json'


def _store_path(module: str) -> pathlib.Path:
    return _tags_dir() / 'learned' / f'{module}.sqlite'


def _connect(module: str, create: bool = True) -> sqlite3.Connection | None:
    path = _store_path(module)
    if not create and not path.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.executescript(SCHEMA)
    return conn


def _meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute('SELECT v FROM meta WHERE k = ?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute('INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)', (key, json.dumps(value)))


def _stamp(path: pathlib.Path) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def is_learned(module: str, path: pathlib.Path | None = None) -> bool:
    """The list at '<data dir>/tags/<abbr>_tags.json' (path: only when it is that file) is the one the learning wrote last"""
    target = list_path(module)
    if path is not None and pathlib.Path(path).resolve() != target.resolve():
        return False
    if (stamp := _stamp(target)) is None:
        return False
    conn = _connect(module, create=False)
    if conn is None:
        return False
    with contextlib.closing(conn):
        return _meta(conn, 'export') == stamp


def enabled() -> bool:
    return os.environ.get(ENV_TAGS_LEARN, '1').strip() != '0'


def may_learn(module: str) -> bool:
    """No list from a source: none located (and nothing else at the learned path), or the located one is the learned list"""
    if not enabled() or not re_abbr.fullmatch(str(module or '')):
        return False
    located = TagsDB.get_file(module)
    if located is None:
        if TagsDB.is_loaded(module):
            return False  # a list registered in memory (tools, tests)
        return not list_path(module).exists() or is_learned(module)
    return is_learned(module, located)


def _norm(tag) -> str:
    """lowercase, spaces as '_' (the tag syntax of Lustra), no control characters"""
    t = str(tag).strip().lower().replace(' ', '_')
    return '' if not t or len(t) > TAG_MAX_LENGTH or any(ord(c) < 32 for c in t) else t


def _add(conn: sqlite3.Connection, posts: Iterable[tuple[object, Iterable[str]]]) -> int:
    new = 0
    for post_id, tags in posts:
        pid = str(post_id or '').strip()
        if not pid or conn.execute('INSERT OR IGNORE INTO posts (id) VALUES (?)', (pid,)).rowcount != 1:
            continue  # a post counts once, seen again in Preview or downloaded later
        names = {n for n in map(_norm, list(tags)[:TAGS_PER_POST_MAX]) if n}
        conn.executemany('INSERT INTO tags (name, posts) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET posts = posts + 1',
                         [(n,) for n in names])
        new += 1
    return new


def _write(module: str, conn: sqlite3.Connection) -> bool:
    target = list_path(module)
    current = _stamp(target)
    if current is not None and current != _meta(conn, 'export'):
        return False  # another list took this path (the updater): the learning does not write any more
    tags = {name: (posts, LEARNED_TYPE) for name, posts in conn.execute('SELECT name, posts FROM tags')}
    if not tags:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    write_tag_list(target, tags)
    _set_meta(conn, 'export', _stamp(target))
    conn.commit()
    return True


def learn(module: str, posts: Iterable[tuple[object, Iterable[str]]]) -> int:
    """Counts the posts (post id, tags) not counted before and writes the list. -> new posts (0: nothing new, or the module has a
    list from a source)"""
    if not may_learn(module):
        return 0
    with _lock(module):
        conn = _connect(module)
        with contextlib.closing(conn):
            new = _add(conn, posts)
            conn.commit()
            written = bool(new) and _write(module, conn)
    if written:
        TagsDB.locate_default_lists(reload=(module,))
    return new


def learn_from_index(module: str) -> int:
    """The tags of this module's downloaded files (download index), from where the previous call stopped. -> new posts"""
    if not may_learn(module):
        return 0
    from .dlindex import DownloadIndex
    new = 0
    with _lock(module):
        conn = _connect(module)
        with contextlib.closing(conn), DownloadIndex() as index:
            cursor = int(_meta(conn, 'index', 0) or 0)
            while rows := index.posts_after(module, cursor, INDEX_STEP):
                new += _add(conn, ((post_id, tags.split()) for _, post_id, tags in rows))
                cursor = rows[-1][0]
                _set_meta(conn, 'index', cursor)
                conn.commit()
                if len(rows) < INDEX_STEP:
                    break
            written = bool(new) and _write(module, conn)
    if written:
        TagsDB.locate_default_lists(reload=(module,))
    return new


def status(module: str) -> dict:
    """{learned: the module's list is the learned one, posts: posts counted, tags: tags known}"""
    conn = _connect(module, create=False) if re_abbr.fullmatch(str(module or '')) else None
    if conn is None:
        return {'learned': False, 'posts': 0, 'tags': 0}
    with contextlib.closing(conn):
        posts = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        tags = conn.execute('SELECT COUNT(*) FROM tags').fetchone()[0]
    return {'learned': is_learned(module), 'posts': int(posts), 'tags': int(tags)}

#
#
#########################################
