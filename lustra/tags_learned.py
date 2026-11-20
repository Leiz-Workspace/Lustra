# coding=UTF-8
"""
Lustra: tag lists learned from the posts already seen, for modules that have no list: sites without a tag listing (custom HTML,
shimmie without its api), sites whose robots.txt forbids it, or a list not downloaded yet. No request is made: only the tags of
the posts shown in Preview (server.m_search_preview) and of the downloaded files (the download index, lustra/dlindex.py).
The list is '<data dir>/tags/<abbr>_tags.json' in the bundled format, 'N posts (unknown)': N is the number of distinct posts seen
with the tag here, not the site's count. A list from a source always wins: once the updater, an installed extension with a tag
source or 2tags/ gives the module a list, the learning stops writing (the file it wrote last is stamped; any other file at that
path, or another list located for the module, means it is not ours any more).
On top of a bundled list: a list shipped inside an installed extension that has no tag source (TagsDB.Bundled: Rule34Hentai,
Paheal, Rule34.us) is never updated, so the learning goes on and its file holds only the learned tags that list lacks (its
'additions'); TagsDB serves the bundled list plus those, the bundled counts stay. The bundled list is never rewritten or parsed
again for it (rule34.us: 18.9 MB): only the tags of the newly counted posts are looked up in it, the whole learned set only when
the bundled list changed (an extension update, its uninstall: then the learned list is the module's whole list again).
State: '<data dir>/tags/learned/<abbr>.sqlite' (posts counted, tag counts, the index cursor, the stamp of the last write, the
bundled list the additions were computed for and those additions)
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

__all__ = ('ENV_TAGS_LEARN', 'LEARNED_TYPE', 'added_count', 'base_list', 'enabled', 'has_source_list', 'indexed', 'is_learned', 'learn',
           'learn_from_index', 'list_path', 'may_learn', 'refresh', 'status')

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
CREATE TABLE IF NOT EXISTS added (name TEXT PRIMARY KEY) WITHOUT ROWID;
'''
'''added: the learned tags the bundled list lacks (meta 'base': that list's path and stamp; empty without one)'''

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


def base_list(module: str) -> pathlib.Path | None:
    """The list the learning adds to: the module's located list when it ships inside an installed extension that has no tag source
    (TagsDB.Bundled), else None"""
    bundled = TagsDB.bundled(module)
    return bundled.path if bundled is not None and TagsDB.get_file(module) == bundled.path else None


def _path_is_ours(module: str) -> bool:
    """Nothing at the learned path, or the list the learning wrote last"""
    return not list_path(module).exists() or is_learned(module)


def may_learn(module: str) -> bool:
    """No list from a source: none located (and nothing else at the learned path), the located one is the learned list, or it is a
    bundled list without a source (base_list: the learning adds the tags it lacks)"""
    if not enabled() or not re_abbr.fullmatch(str(module or '')):
        return False
    located = TagsDB.get_file(module)
    if located is None:
        if TagsDB.is_loaded(module):
            return False  # a list registered in memory (tools, tests)
        return _path_is_ours(module)
    if base_list(module) is not None:
        return _path_is_ours(module)
    return is_learned(module, located)


def has_source_list(module: str) -> bool:
    """The module has a list that is not the learned one: from the updater, an installed extension with a tag source, 2tags/ or
    registered in memory (learning it is not needed). A bundled list without a source is not one (the learning adds to it), unless
    another list took the learned path"""
    if not re_abbr.fullmatch(str(module or '')):
        return False
    located = TagsDB.get_file(module)
    if located is None:
        return TagsDB.is_loaded(module) or (list_path(module).exists() and not is_learned(module))
    if base_list(module) is not None:
        return not _path_is_ours(module)
    return not is_learned(module, located)


def indexed(module: str) -> bool:
    """The download index holds files of this module (what the learning reads besides Preview)"""
    if not re_abbr.fullmatch(str(module or '')):
        return False
    from .dlindex import DownloadIndex
    with DownloadIndex() as index:
        return bool(index.posts_after(module, 0, 1))


def _norm(tag) -> str:
    """lowercase, spaces as '_' (the tag syntax of Lustra), no control characters"""
    t = str(tag).strip().lower().replace(' ', '_')
    return '' if not t or len(t) > TAG_MAX_LENGTH or any(ord(c) < 32 for c in t) else t


def _add(conn: sqlite3.Connection, posts: Iterable[tuple[object, Iterable[str]]], seen: set[str] | None = None) -> int:
    """seen: gets the tags of the posts counted now"""
    new = 0
    for post_id, tags in posts:
        pid = str(post_id or '').strip()
        if not pid or conn.execute('INSERT OR IGNORE INTO posts (id) VALUES (?)', (pid,)).rowcount != 1:
            continue  # a post counts once, seen again in Preview or downloaded later
        names = {n for n in map(_norm, list(tags)[:TAGS_PER_POST_MAX]) if n}
        conn.executemany('INSERT INTO tags (name, posts) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET posts = posts + 1',
                         [(n,) for n in names])
        if seen is not None:
            seen.update(names)
        new += 1
    return new


def _base_stamp(base: pathlib.Path | None) -> list | None:
    return None if base is None else [str(base), *(_stamp(base) or [])]


def _sync_base(module: str, conn: sqlite3.Connection, base: pathlib.Path | None, seen: set[str]) -> bool:
    """Keeps 'added' right for the list the learning adds to (base_list): the tags seen now that it lacks are added; when the
    bundled list changed (or is gone), the whole learned set is checked again. -> True: it changed (the written list must be redone)"""
    stamp = _base_stamp(base)
    changed = stamp != _meta(conn, 'base')
    if changed:
        conn.execute('DELETE FROM added')
        _set_meta(conn, 'base', stamp)
        if base is not None:
            seen = {name for (name,) in conn.execute('SELECT name FROM tags')}
    if base is not None and seen:
        conn.executemany('INSERT OR IGNORE INTO added (name) VALUES (?)', [(n,) for n in TagsDB.lacking(module, sorted(seen))])
    return changed


def _write(module: str, conn: sqlite3.Connection, on_base: bool) -> bool:
    """on_base: only the learned tags the bundled list lacks (an empty list replaces additions the bundled list now has)"""
    target = list_path(module)
    current = _stamp(target)
    if current is not None and current != _meta(conn, 'export'):
        return False  # another list took this path (the updater): the learning does not write any more
    rows = conn.execute('SELECT tags.name, tags.posts FROM added JOIN tags ON tags.name = added.name' if on_base else
                        'SELECT name, posts FROM tags')
    tags = {name: (posts, LEARNED_TYPE) for name, posts in rows}
    if not tags and not (on_base and current is not None):
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
    base = base_list(module)
    with _lock(module):
        conn = _connect(module)
        with contextlib.closing(conn):
            seen: set[str] = set()
            new = _add(conn, posts, seen)
            rebased = _sync_base(module, conn, base, seen)
            conn.commit()
            written = bool(new or rebased) and _write(module, conn, base is not None)
    if written:
        TagsDB.locate_default_lists(reload=(module,))
    return new


def learn_from_index(module: str) -> int:
    """The tags of this module's downloaded files (download index), from where the previous call stopped. -> new posts"""
    if not may_learn(module):
        return 0
    from .dlindex import DownloadIndex
    base = base_list(module)
    new = 0
    with _lock(module):
        conn = _connect(module)
        with contextlib.closing(conn), DownloadIndex() as index:
            rebased = _sync_base(module, conn, base, set())
            cursor = int(_meta(conn, 'index', 0) or 0)
            while rows := index.posts_after(module, cursor, INDEX_STEP):
                seen: set[str] = set()
                new += _add(conn, ((post_id, tags.split()) for _, post_id, tags in rows), seen)
                _sync_base(module, conn, base, seen)
                cursor = rows[-1][0]
                _set_meta(conn, 'index', cursor)
                conn.commit()
                if len(rows) < INDEX_STEP:
                    break
            conn.commit()
            written = bool(new or rebased) and _write(module, conn, base is not None)
    if written:
        TagsDB.locate_default_lists(reload=(module,))
    return new


def refresh(module: str) -> bool:
    """After an extension of the module was installed, updated or uninstalled: when the bundled list the learning adds to changed,
    the learned list is written again (the additions the new list has are dropped; with no bundled list any more, the whole learned
    list). Nothing learned yet: nothing to do. -> written"""
    if not may_learn(module) or not _store_path(module).is_file():
        return False
    base = base_list(module)
    with _lock(module):
        conn = _connect(module, create=False)
        if conn is None:
            return False
        with contextlib.closing(conn):
            rebased = _sync_base(module, conn, base, set())
            conn.commit()
            written = rebased and _write(module, conn, base is not None)
    if written:
        TagsDB.locate_default_lists(reload=(module,))
    return written


def added_count(module: str) -> int:
    """Learned tags the module's bundled list lacks (served on top of it)"""
    conn = _connect(module, create=False) if re_abbr.fullmatch(str(module or '')) else None
    if conn is None:
        return 0
    with contextlib.closing(conn):
        return int(conn.execute('SELECT COUNT(*) FROM added').fetchone()[0])


def status(module: str) -> dict:
    """{learned: the module's list is the learned one (not when it is only served on top of a bundled list), posts: posts counted,
    tags: tags known}"""
    conn = _connect(module, create=False) if re_abbr.fullmatch(str(module or '')) else None
    if conn is None:
        return {'learned': False, 'posts': 0, 'tags': 0}
    with contextlib.closing(conn):
        posts = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        tags = conn.execute('SELECT COUNT(*) FROM tags').fetchone()[0]
    located = TagsDB.get_file(module)
    return {'learned': located is not None and is_learned(module, located), 'posts': int(posts), 'tags': int(tags)}

#
#
#########################################
