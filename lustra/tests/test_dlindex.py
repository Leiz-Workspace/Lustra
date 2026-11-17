# coding=UTF-8
"""
Lustra: local download index (lustra/dlindex.py, docs/plans/search-engine.md phase 3), offline
- schema (WAL), upsert by file path, search by tag / module / post date with paging, stats
- the writer: batches on a background thread, add() never waits for the database, errors are reported, not raised
- several processes writing at the same time (CLI jobs running in parallel)
- download pipeline: finished files are recorded from the scan's post info; '-skip_known' (off by default) skips posts known by
  module + id or by md5 (any module) whose file is still on disk
- sidecar methods index.search / index.stats, desktop app wiring (allowlist, -skip_known)
Run: python -m unittest lustra.tests.test_dlindex
"""
#########################################
#
#

import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from lustra.logger import Logger

Logger.init(True, True)

from lustra import dlindex  # noqa: E402
from lustra.cmdargs import prepare_arglist  # noqa: E402
from lustra.defines import ItemInfo  # noqa: E402
from lustra.downloaders import _make_downloader  # noqa: E402
from lustra.extensions import Extensions  # noqa: E402
from lustra.module import ProcModule  # noqa: E402
from lustra.network import FileDownloadResult  # noqa: E402
from lustra.tests.test_batch_c import FakeBooru, _make_posts  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _rec(module: str, post_id: int, path: str | pathlib.Path, *, md5: str = '', tags: str = '', score: int | None = None,
         rating: str = '', date: str = '', at: float = 0.0, size: int = 10) -> dlindex.IndexRecord:
    return dlindex.IndexRecord(module=module, post_id=str(post_id), path=str(path), md5=md5, tags=tags, score=score, rating=rating,
                               post_date=date, size=size, downloaded_at=at or time.time())


def _md5(n: int) -> str:
    return f'{n:032x}'


class _TempDir(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.db_path = self.tmp / 'index' / dlindex.INDEX_FILE_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _file(self, name: str, size: int = 3) -> pathlib.Path:
        p = self.tmp / 'files' / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'x' * size)
        return p

    def _write(self, records: list[dlindex.IndexRecord]) -> None:
        with dlindex.DownloadIndex(self.db_path, create=True) as index:
            index.add_many(records)


class IndexDbTests(_TempDir):
    def test_readers_never_create_the_file(self) -> None:
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual(0, index.search('a')['total'])
            self.assertEqual(({}, {}), index.find_known('rx', ['1'], [_md5(1)]))
            st = index.stats()
        self.assertFalse(st['exists'])
        self.assertEqual(0, st['files'])
        self.assertFalse(self.db_path.exists())

    def test_schema_wal_and_upsert_by_path(self) -> None:
        f = self._file('1.jpg')
        self._write([_rec('rx', 1, f, md5=_md5(1).upper(), tags='a b', score=5, rating='e', date='2020-01-02')])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual('wal', conn.execute('PRAGMA journal_mode').fetchone()[0])
            self.assertEqual(dlindex.SCHEMA_VERSION, conn.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual([(1, _md5(1))], conn.execute('SELECT post_id, md5 FROM files').fetchall())
        finally:
            conn.close()
        # the same file downloaded again: one row, tags replaced
        self._write([_rec('rx', 1, f, md5=_md5(1), tags='b c')])
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual(1, index.stats()['files'])
            self.assertEqual(0, index.search('a')['total'])
            self.assertEqual(1, index.search('c')['total'])
            item = index.search('b')['items'][0]
        self.assertEqual(('rx', '1', 'b c', '1.jpg', True), (item['module'], item['id'], item['tags'], item['name'], item['exists']))

    def test_wal_switch_tries_again_while_locked(self) -> None:
        class Conn:  # SQLite reports the lock at once (no busy wait) while another process switches the same new file
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str) -> mock.Mock:
                self.calls.append(sql)
                if sql == 'PRAGMA journal_mode = WAL' and self.calls.count(sql) < 3:
                    raise sqlite3.OperationalError('database is locked')
                return mock.Mock(fetchone=mock.Mock(return_value=('delete',)))

        conn = Conn()
        with mock.patch('lustra.dlindex.time.sleep') as sleep:
            dlindex.DownloadIndex._enable_wal(conn)
        self.assertEqual(3, conn.calls.count('PRAGMA journal_mode = WAL'))
        self.assertEqual(2, sleep.call_count)
        # other errors and a lock that outlasts BUSY_TIMEOUT are raised
        conn = Conn()
        conn.execute = mock.Mock(side_effect=sqlite3.OperationalError('disk I/O error'))
        with self.assertRaises(sqlite3.OperationalError):
            dlindex.DownloadIndex._enable_wal(conn)
        conn.execute = mock.Mock(side_effect=sqlite3.OperationalError('database is locked'))
        with mock.patch('lustra.dlindex.time.sleep'), mock.patch('lustra.dlindex.time.monotonic', side_effect=[0.0, 1.0, 99.0]), \
                self.assertRaises(sqlite3.OperationalError):
            dlindex.DownloadIndex._enable_wal(conn)

    def test_search(self) -> None:
        recs = [
            _rec('rx', 1, self._file('1.jpg'), md5=_md5(1), tags='a b solo', score=10, rating='e', date='2020-01-01', at=100),
            _rec('rx', 2, self.tmp / 'gone' / '2.jpg', md5=_md5(2), tags='a c', score=3, rating='s', date='2020-02-01', at=200),
            _rec('db', 3, self._file('3.png'), md5=_md5(3), tags='a b sky', score=None, rating='g', date='2020-03-01', at=300),
            _rec('db', 4, self._file('4.png'), tags='b skyline', score=50, rating='q', date='', at=400),
        ]
        self._write(recs)
        with dlindex.DownloadIndex(self.db_path) as index:
            def ids(text: str = '', **kw) -> list[str]:
                return [x['id'] for x in index.search(text, **kw)['items']]

            self.assertEqual(['3', '2', '1'], ids('a'))  # newest download first
            self.assertEqual(['3', '1'], ids('a b'))
            self.assertEqual(['2'], ids('a -b'))
            self.assertEqual(['4', '3'], ids('sky*'))
            self.assertEqual(['4'], ids('sky* -sky'))
            self.assertEqual(['1'], ids('rating:e'))
            self.assertEqual(['3'], ids(f'md5:{_md5(3)}'))
            self.assertEqual(['2'], ids('id:2'))
            self.assertEqual(['4', '3'], ids(module='db'))
            self.assertEqual(['3', '2'], ids(date_from='2020-02-01', date_to='2020-03-01'))
            self.assertEqual(['2', '1'], ids(date_to='15-02-2020'))  # core date format too; posts without a date are left out
            self.assertEqual(['4', '1', '2', '3'], ids(order='score'))  # unknown score last
            self.assertEqual(['1', '2', '3', '4'], ids(order='downloaded_asc'))
            self.assertEqual(['3', '2', '1', '4'], ids('', order='date'))
            page = index.search('', page=1, page_size=3)
            self.assertEqual((4, 1, 3, ['1']), (page['total'], page['page'], page['pageSize'], [x['id'] for x in page['items']]))
            self.assertEqual([], index.search('', page=5)['items'])
            gone = index.search('id:2')['items'][0]
            self.assertFalse(gone['exists'])
            self.assertEqual(0, index.search('nope*')['total'])

    def test_parse_search_terms(self) -> None:
        self.assertEqual([(False, 'tag', 'a'), (True, 'tag', 'b_c'), (False, 'prefix', 'sky'), (False, 'rating', 'e'),
                          (True, 'md5', _md5(9)), (False, 'id', '12'), (False, 'tag', 'id:x')],  # a bad meta value is a plain tag
                         dlindex.parse_search_terms(f'A -b_c sky* rating:explicit -md5:{_md5(9).upper()} id:12 * - a*b id:x'))
        self.assertEqual([(False, 'tag', 'a'), (False, 'tag', 'b')], dlindex.parse_search_terms(['a', 'b ']))
        self.assertEqual(dlindex.SEARCH_TERMS_MAX, len(dlindex.parse_search_terms(' '.join(f't{i}' for i in range(100)))))

    def test_find_known_needs_the_file_on_disk(self) -> None:
        self._write([
            _rec('rx', 1, self._file('1.jpg'), md5=_md5(1)),
            _rec('rx', 2, self.tmp / 'missing.jpg', md5=_md5(2)),
            _rec('rx', 3, self._file('3.jpg', size=0), md5=_md5(3)),  # an empty file is not a download
            _rec('db', 4, self._file('4.jpg'), md5=_md5(4)),
        ])
        with dlindex.DownloadIndex(self.db_path) as index:
            by_id, by_md5 = index.find_known('rx', ['1', '2', '3', '4', 'x'], [_md5(n) for n in range(1, 6)] + ['bad'])
        self.assertEqual(['1'], sorted(by_id))
        self.assertEqual(sorted([_md5(1), _md5(4)]), sorted(by_md5))

    def test_stats(self) -> None:
        self._write([
            _rec('rx', 1, self._file('a'), md5=_md5(1), tags='a b', size=100, at=10),
            _rec('rx', 1, self._file('b'), md5=_md5(1), tags='a', size=100, at=20),  # same post in another folder
            _rec('db', 2, self._file('c'), tags='c', size=50, at=30),
        ])
        with dlindex.DownloadIndex(self.db_path) as index:
            st = index.stats()
        self.assertTrue(st['exists'])
        self.assertEqual((3, 2, 2, 3, 250, 10, 30), (st['files'], st['posts'], st['withMd5'], st['tags'], st['bytes'], st['first'], st['last']))
        self.assertEqual([('rx', 2, 200), ('db', 1, 50)], [(m['module'], m['files'], m['bytes']) for m in st['modules']])
        self.assertGreater(st['dbBytes'], 0)

    def test_record_for(self) -> None:
        info = ItemInfo()
        info.id, info.md5, info.tags, info.score, info.date, info.ext = '77', _md5(77).upper(), 'B a  b', '+12', '06-01-2020', 'png'
        rec = dlindex.record_for('rx', info, 'rx_77', self.tmp / 'rx_77.jpg', 1234, raw='<post id="77" rating="explicit" />')
        self.assertEqual(('rx', '77', _md5(77), 'b a', 12, 'e', '2020-01-06', 'jpg', 1234),
                         (rec.module, rec.post_id, rec.md5, rec.tags, rec.score, rec.rating, rec.post_date, rec.ext, rec.size))
        self.assertTrue(os.path.isabs(rec.path))
        empty = dlindex.record_for('db', ItemInfo(), '5', 'x/5', 1, raw='{"rating":"q"}')
        self.assertEqual(('5', '', None, 'q', '', ''), (empty.post_id, empty.md5, empty.score, empty.rating, empty.post_date, empty.ext))
        self.assertEqual('g', dlindex.rating_of('', 'x rating:general'))
        self.assertEqual('2026-11-16', dlindex.norm_date('2026-11-16T04:24:19.090-05:00'))


class IndexWriterTests(_TempDir):
    def _rows(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        finally:
            conn.close()

    def test_batches_and_close_flushes(self) -> None:
        batches: list[tuple[int, str]] = []
        real = dlindex.DownloadIndex.add_many

        def spy(index, records):
            batches.append((len(records), threading.current_thread().name))
            return real(index, records)

        writer = dlindex.IndexWriter(self.db_path, batch_max=3, interval=60)
        with mock.patch.object(dlindex.DownloadIndex, 'add_many', spy):
            for i in range(7):
                writer.add(_rec('rx', i, self.tmp / f'{i}.jpg', tags='a'))
            self.assertTrue(writer.close(10))
        self.assertEqual((7, 0, ''), (writer.written, writer.failed, writer.error))
        self.assertEqual([3, 3, 1], [n for n, _ in batches])
        self.assertEqual({'dlindex'}, {name for _, name in batches})
        self.assertEqual(7, self._rows())
        writer.add(_rec('rx', 99, self.tmp / '99.jpg'))  # closed: ignored
        self.assertTrue(writer.close())
        self.assertEqual(7, self._rows())

    def test_add_never_waits_for_a_locked_database(self) -> None:
        self._write([_rec('rx', 0, self.tmp / '0.jpg')])
        blocker = sqlite3.connect(self.db_path, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')  # another process writing
        writer = dlindex.IndexWriter(self.db_path, batch_max=1, interval=0)
        try:
            t0 = time.monotonic()
            for i in range(1, 6):
                writer.add(_rec('rx', i, self.tmp / f'{i}.jpg'))
            self.assertLess(time.monotonic() - t0, 0.5)
            time.sleep(0.3)
            self.assertEqual(0, writer.written)
        finally:
            blocker.execute('COMMIT')
            blocker.close()
        self.assertTrue(writer.close(20))
        self.assertEqual((5, 0), (writer.written, writer.failed))
        self.assertEqual(6, self._rows())

    def test_errors_are_counted_not_raised(self) -> None:
        not_a_dir = self._file('plain-file')
        writer = dlindex.IndexWriter(not_a_dir / 'index' / 'downloads.sqlite', interval=0)
        with mock.patch('lustra.dlindex.trace') as trace:
            writer.add(_rec('rx', 1, self.tmp / '1.jpg'))
            writer.add(_rec('rx', 2, self.tmp / '2.jpg'))
            self.assertTrue(writer.close(10))
        self.assertEqual((0, 2), (writer.written, writer.failed))
        self.assertTrue(writer.error)
        self.assertEqual(1, trace.call_count)  # reported once

    def test_a_failing_report_does_not_stop_the_writer(self) -> None:
        # outside the app the logger has no window and raises: the thread still counts every record and finishes
        not_a_dir = self._file('plain-file')
        writer = dlindex.IndexWriter(not_a_dir / 'index' / 'downloads.sqlite', interval=0)
        with mock.patch('lustra.dlindex.trace', side_effect=AssertionError('Logger window is uninitialized!')):
            for i in range(3):
                writer.add(_rec('rx', i, self.tmp / f'{i}.jpg'))
            self.assertTrue(writer.close(10))
        self.assertEqual((0, 3), (writer.written, writer.failed))


WRITER_SCRIPT = r'''
import sys, time
from lustra import dlindex
path, module, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
w = dlindex.IndexWriter(path, batch_max=7, interval=0)
for i in range(count):
    w.add(dlindex.IndexRecord(module=module, post_id=str(i), path=f'{path}.{module}.{i}', md5=f'{i:032x}',
                              tags=f'shared t{i % 13} {module}', downloaded_at=time.time()))
    if i % 25 == 0:
        time.sleep(0.01)
ok = w.close(60)
print(w.written, w.failed, w.error, ok)
sys.exit(0 if ok and w.written == count and not w.failed else 1)
'''


OPEN_SCRIPT = r'''
import sys, time
from lustra import dlindex
start = float(sys.argv[2])
while time.time() < start:
    time.sleep(0.001)
with dlindex.DownloadIndex(sys.argv[1], create=True) as index:
    print(index.stats()['files'])
'''


class MultiProcessTests(_TempDir):
    def test_concurrent_first_open(self) -> None:
        # several processes open a new index at the same moment: every one switches it to WAL or finds it switched
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'LUSTRA_DATA_DIR': str(self.tmp)}
        start = time.time() + 1.5
        procs = [subprocess.Popen((sys.executable, '-c', OPEN_SCRIPT, str(self.db_path), str(start)), cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual((0, '0'), (p.returncode, out.strip()), err[-2000:])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual('wal', conn.execute('PRAGMA journal_mode').fetchone()[0])
        finally:
            conn.close()

    def test_concurrent_writers(self) -> None:
        count, modules = 150, ('p1', 'p2', 'p3', 'p4')
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'LUSTRA_DATA_DIR': str(self.tmp)}
        procs = [subprocess.Popen((sys.executable, '-c', WRITER_SCRIPT, str(self.db_path), m, str(count)), cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for m in modules]
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(0, p.returncode, f'{out}\n{err[-2000:]}')
        with dlindex.DownloadIndex(self.db_path) as index:
            st = index.stats()
            self.assertEqual(count * len(modules), st['files'])
            self.assertEqual(dict.fromkeys(modules, count), {m['module']: m['files'] for m in st['modules']})
            self.assertEqual(count * len(modules), index.search('shared')['total'])
            self.assertEqual(count, index.search('p3')['total'])
            self.assertEqual(len(range(5, count, 13)) * len(modules), index.search('t5')['total'])
            self.assertEqual(13 + 1 + len(modules), st['tags'])


# ---------------- download pipeline ----------------

class PipelineTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Extensions.ensure_loaded()
        posts = _make_posts(160, 11)
        cls.db = FakeBooru(posts, 40)
        cls.rx = FakeBooru(posts, 50)
        cls.posts = {p['id']: p for p in posts}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': str(self.tmp / 'data'), dlindex.ENV_DLINDEX: '1'})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()
        ProcModule.set(ProcModule.RX)

    def _run(self, module: int, dest: str, argv: list[str], streaming: bool = True) -> tuple[list[str], object]:
        """Fake website and fake (but real on disk) file downloads, returns (downloaded item ids, downloader)"""
        done: list[str] = []
        lock = threading.Lock()

        def fake_download(link: str, item_id: str, path: pathlib.Path, mode, orig_date) -> FileDownloadResult:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'abc')
            with lock:
                done.append(item_id)
            r = FileDownloadResult()
            r.file_size = r.expected_size = 3
            return r

        abbr = ProcModule.PROC_MODULES_NAMES_BY_ID[module]
        server = self.db if module == ProcModule.DB else self.rx
        ProcModule.set(module)
        args = prepare_arglist(['-module', abbr, '-path', str(self.tmp / dest), *argv])
        current_process().killed = False
        with mock.patch.dict(os.environ, {'LUSTRA_STREAMING': '1' if streaming else '0'}), _make_downloader(module) as dwn:
            dwn.fetch_html = server.db_fetch if module == ProcModule.DB else server.rx_fetch
            dwn.download_file = fake_download
            with mock.patch.object(type(dwn), '_get_items_per_page', return_value=server.page_size), \
                    mock.patch('lustra.network.time.sleep'), mock.patch('lustra.download.time.sleep'):
                dwn.launch_download(args)
            if dwn.launch_error:
                raise AssertionError(dwn.launch_error)
            self.assertEqual(streaming, dwn.streaming_active)
        return done, dwn

    def _index(self) -> dlindex.DownloadIndex:
        return dlindex.DownloadIndex(self.tmp / 'data' / dlindex.INDEX_DIR_NAME / dlindex.INDEX_FILE_NAME)

    def test_finished_files_are_recorded_off_the_download_threads(self) -> None:
        threads: list[str] = []
        real = dlindex.DownloadIndex.add_many

        def spy(index, records):
            threads.append(threading.current_thread().name)
            return real(index, records)

        with mock.patch.object(dlindex.DownloadIndex, 'add_many', spy):
            done, _ = self._run(ProcModule.DB, 'one', ['-threads', '4', '--', 'a'])
        self.assertTrue(done)
        self.assertEqual({'dlindex'}, set(threads))
        with self._index() as index:
            res = index.search('', module='db', page_size=200)
        self.assertEqual(len(done), res['total'])
        for item in res['items']:
            post = self.posts[int(item['id'])]
            self.assertEqual(_md5(post['id']), item['md5'])
            self.assertEqual(' '.join(post['tags']), item['tags'])
            self.assertEqual((post['score'], 'g', post['date'].date().isoformat()), (item['score'], item['rating'], item['date']))
            self.assertTrue(item['exists'])
            # macOS: the temp folder is reached through a link (/var -> /private/var), the index keeps the real path
            self.assertEqual(os.path.realpath(self.tmp / 'one'), os.path.realpath(os.path.dirname(item['path'])))

    def test_skip_known_is_off_by_default(self) -> None:
        first, _ = self._run(ProcModule.DB, 'one', ['--', 'sky'])
        again, dwn = self._run(ProcModule.DB, 'two', ['--', 'sky'])
        self.assertEqual(sorted(first), sorted(again))
        self.assertEqual(0, dwn.known_skipped_count)

    def test_skip_known_by_module_and_id(self) -> None:
        for streaming in (True, False):
            with self.subTest(streaming=streaming), mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': str(self.tmp / f'data{streaming}')}):
                first, _ = self._run(ProcModule.DB, f'one{streaming}', ['--', 'a'], streaming)
                self.assertGreater(len(first), 4)
                # two of the files are gone: those are downloaded again, the others are skipped
                for name in sorted(os.listdir(self.tmp / f'one{streaming}'))[:2]:
                    os.unlink(self.tmp / f'one{streaming}' / name)
                again, dwn = self._run(ProcModule.DB, f'two{streaming}', ['-skip_known', '--', 'a'], streaming)
                self.assertEqual(2, len(again))
                self.assertEqual(len(first) - 2, dwn.known_skipped_count)

    def test_skip_known_by_md5_across_modules(self) -> None:
        first, _ = self._run(ProcModule.DB, 'db', ['--', 'sky'])
        done, dwn = self._run(ProcModule.RX, 'rx', ['-skip_known', '--', 'sky'])
        self.assertEqual([], done)
        self.assertEqual(len(first), dwn.known_skipped_count)
        done, _ = self._run(ProcModule.RX, 'rx2', ['--', 'sky'])
        self.assertEqual(sorted(first), sorted(done))

    def test_prefixed_file_names_record_plain_ids(self) -> None:
        done, _ = self._run(ProcModule.DB, 'one', ['-prefix', '--', 'solo'])
        self.assertTrue(all(x.startswith('db_') for x in done))
        with self._index() as index:
            ids = sorted(x['id'] for x in index.search('solo', page_size=200)['items'])
        self.assertEqual(sorted(x[3:] for x in done), ids)
        again, dwn = self._run(ProcModule.DB, 'two', ['-prefix', '-skip_known', '--', 'solo'])
        self.assertEqual(([], len(done)), (again, dwn.known_skipped_count))

    def test_skip_known_without_an_index(self) -> None:
        done, dwn = self._run(ProcModule.DB, 'one', ['-skip_known', '--', 'sky'])
        self.assertTrue(done)
        self.assertEqual(0, dwn.known_skipped_count)

    def test_recording_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {dlindex.ENV_DLINDEX: '0'}):
            done, _ = self._run(ProcModule.DB, 'one', ['--', 'sky'])
        self.assertTrue(done)
        self.assertFalse((self.tmp / 'data' / dlindex.INDEX_DIR_NAME).exists())


# ---------------- cli, sidecar, desktop app ----------------

class FrontendTests(TestCase):
    def test_cli_option(self) -> None:
        Extensions.ensure_loaded()
        self.assertFalse(prepare_arglist(['-module', 'rx', '--', 'a']).skip_known)
        self.assertTrue(prepare_arglist(['-module', 'rx', '-skip_known', '--', 'a']).skip_known)
        parsed = prepare_arglist(['-module', 'rx', '--', '-skip_known'])  # after '--' it is a negative tag
        self.assertEqual((False, ['-skip_known']), (parsed.skip_known, parsed.tags))

    def test_sidecar_methods(self) -> None:
        from lustra import server
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'LUSTRA_DATA_DIR': tmp}):
            self.assertFalse(server.METHODS['index.stats']({})['exists'])
            self.assertEqual(0, server.METHODS['index.search']({'tags': 'a'})['total'])
            with dlindex.DownloadIndex(create=True) as index:
                index.add_many([_rec('rx', i, pathlib.Path(tmp) / f'{i}.jpg', tags=f'a t{i % 2}', date=f'2020-01-0{i}') for i in range(1, 6)])
            st = server.METHODS['index.stats']({})
            self.assertEqual((True, 5), (st['exists'], st['files']))
            res = server.METHODS['index.search']({'tags': ['a', '-t1'], 'module': 'rx', 'pageSize': 1, 'page': 1, 'order': 'date'})
            self.assertEqual((2, 1, ['2']), (res['total'], res['pageSize'], [x['id'] for x in res['items']]))
            res = server.METHODS['index.search']({'dateFrom': '2020-01-02', 'dateTo': '2020-01-03', 'pageSize': 9999})
            self.assertEqual((2, dlindex.SEARCH_PAGE_SIZE_MAX), (res['total'], res['pageSize']))
            for bad in ({'module': 'RX!'}, {'pageSize': 'x'}, {'page': [1]}):
                with self.subTest(bad=bad), self.assertRaises(server.RpcError):
                    server.METHODS['index.search'](bad)

    def test_desktop_wiring(self) -> None:
        main_js = (ROOT / 'app' / 'main.js').read_text(encoding='utf-8')
        allowed = re.search(r"handle\('rpc'.*?const allowed = \[(.*?)\];", main_js, re.S)
        self.assertIsNotNone(allowed)
        self.assertIn("'index.search'", allowed.group(1))
        self.assertIn("'index.stats'", allowed.group(1))
        self.assertIn("if (d.skipKnown === true) a.push('-skip_known');", main_js)
        self.assertRegex(main_js, r'skipKnown: false, //')
        html = (ROOT / 'app' / 'renderer' / 'index.html').read_text(encoding='utf-8')
        app_js = (ROOT / 'app' / 'renderer' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="f-skip-known"', html)
        self.assertIn('id="index-stats"', html)
        self.assertIn("skipKnown: $('#f-skip-known').checked", app_js)
        self.assertIn("L.rpc('index.stats')", app_js)

#
#
#########################################
