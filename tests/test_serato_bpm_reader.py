"""隔离 Serato BPM 读取：真实 SQLite/WAL、精确文件关联与失败缓存边界。"""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from djtool.audio_tasks import AudioCancelled
from djtool.library import Library
from djtool.serato_bpm import SeratoBPMError, read_serato_bpms, sync_library


class SeratoBpmReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source_dir = self.root / 'native-library'
        self.source_dir.mkdir()
        self.database = self.source_dir / 'root.sqlite'
        self.first = self.root / '曲库' / '混音 #1.mp3'
        self.first.parent.mkdir()
        self.first.write_bytes(b'first synthetic file; no audio decoder required')
        self.second = self.root / '曲库' / 'second.mp3'
        self.second.write_bytes(b'second synthetic file')
        self.tracks = [{'id': 'first', 'path': str(self.first), 'title': '相同标题'},
                       {'id': 'second', 'path': str(self.second), 'title': '相同标题'}]

    def tearDown(self):
        self.temp.cleanup()

    def row(self, path=None, bpm=128.35, **overrides):
        path = Path(path or self.first)
        row = {'portable_id': str(path).lstrip('/'), 'bpm': bpm,
               'file_size': path.stat().st_size if path.exists() else 22,
               'is_missing': 0, 'is_corrupt': 0, 'is_stale': 0,
               'third_party_type': 0, 'type': 'audio'}
        row.update(overrides)
        return row

    def make_database(self, rows=(), *, version=202, omitted=(), master=False):
        columns = {'portable_id': 'TEXT', 'bpm': 'REAL', 'file_size': 'INTEGER',
                   'is_missing': 'INTEGER', 'is_corrupt': 'INTEGER', 'is_stale': 'INTEGER',
                   'third_party_type': 'INTEGER', 'type': 'TEXT'}
        columns = {name: kind for name, kind in columns.items() if name not in omitted}
        if master:
            columns['location_id'] = 'INTEGER'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(f'PRAGMA user_version={version}')
            db.execute('CREATE TABLE asset (' + ','.join(f'{name} {kind}' for name, kind in columns.items()) + ')')
            names = list(columns)
            sql = 'INSERT INTO asset (' + ','.join(names) + ') VALUES (' + ','.join('?' for _ in names) + ')'
            db.executemany(sql, [tuple(row.get(name, 1 if name == 'location_id' else None)
                                      for name in names) for row in rows])

    def fingerprint(self):
        # 不比较 atime；读取本身可能更新访问时间，大小/mtime/字节才是写入边界。
        return {str(path): (path.stat().st_size, path.stat().st_mtime_ns,
                            hashlib.sha256(path.read_bytes()).hexdigest())
                for path in [*self.source_dir.iterdir(), self.first, self.second] if path.is_file()}

    def isolated_library(self):
        library = Library(self.root / 'djtool-library')
        with library.connection() as db:
            for track in self.tracks:
                db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename,'
                           'bpm_tag,bpm_estimate,bpm_manual,note,reviewed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (track['id'], f"digest-{track['id']}", track['title'], '艺人', 120,
                            track['path'], track['path'], Path(track['path']).name,
                            91, 92, 93, '保留整理', 1))
            db.execute("INSERT INTO collections(id,name,kind) VALUES('set','演出','set')")
            db.executemany('INSERT INTO members(collection_id,track_id,position,note) VALUES(?,?,?,?)',
                           [('set', 'second', 0, '第一首'), ('set', 'first', 1, '第二首')])
        return library

    def cached_rows(self, library):
        with library.connection() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM serato_bpm_cache ORDER BY track_id')]

    def test_reads_precise_bpm_exact_playback_paths_without_decoding_or_writing(self):
        self.make_database([self.row(), self.row(self.second, 79.68)])
        before = self.fingerprint()
        with patch('djtool.tracks.read_audio', side_effect=AssertionError('不得解码或分析音频')):
            result = read_serato_bpms(self.tracks, database=self.database)
        self.assertEqual(result['values'], {str(self.first): 128.35, str(self.second): 79.68})
        self.assertEqual(result['source_path'], str(self.database))
        self.assertIsInstance(result['skipped'], int)
        self.assertEqual(self.fingerprint(), before)

    def test_accepts_valid_absolute_database_path_reference(self):
        self.make_database([self.row(portable_id=str(self.first))])
        result = read_serato_bpms(self.tracks, database=self.database)
        self.assertEqual(result['values'], {str(self.first): 128.35})

    def test_default_source_is_current_users_authoritative_root_database(self):
        self.source_dir = self.root / 'Library/Application Support/Serato/Library'
        self.source_dir.mkdir(parents=True)
        self.database = self.source_dir / 'root.sqlite'
        self.make_database([self.row(bpm=128.35)])
        # 旁边出现主库不能让读取器改用带外卷路径的 master。
        (self.source_dir / 'master.sqlite').write_bytes(b'not the authoritative local root database')
        before = self.fingerprint()
        with patch('djtool.serato_bpm.Path.home', return_value=self.root):
            result = read_serato_bpms(self.tracks)
        self.assertEqual(result['values'], {str(self.first): 128.35})
        self.assertEqual(result['source_path'], str(self.database))
        self.assertEqual(self.fingerprint(), before)

    def test_does_not_guess_filename_title_or_import_source_path(self):
        other = self.root / 'original' / self.first.name
        other.parent.mkdir()
        other.write_bytes(self.first.read_bytes())
        tracks = [{**self.tracks[0], 'source_path': str(other)}]
        self.make_database([self.row(other)])
        self.assertEqual(read_serato_bpms(tracks, database=self.database)['values'], {})

    def test_rejects_malformed_traversal_urls_external_relative_paths(self):
        valid = str(self.first).lstrip('/')
        malformed = [valid.replace('/曲库/', '/elsewhere/../曲库/'),
                     'file://' + str(self.first), 'streaming://spotify/123',
                     str(self.first) + '\x00', '\\' + valid,
                     '../' + valid, '2024 Halloween/' + self.first.name]
        self.make_database([self.row(portable_id=path) for path in malformed])
        result = read_serato_bpms(self.tracks, database=self.database)
        self.assertEqual(result['values'], {})
        self.assertIsInstance(result['skipped'], int)

    def test_rejects_missing_replaced_and_unknown_size_files(self):
        missing = self.root / 'not-downloaded.mp3'
        absent_track = {'id': 'missing', 'path': str(missing)}
        rows = [self.row(file_size=self.first.stat().st_size + 1),
                self.row(file_size=None), self.row(file_size=0),
                self.row(missing, 125)]
        self.make_database(rows)
        result = read_serato_bpms([*self.tracks, absent_track], database=self.database)
        self.assertEqual(result['values'], {})
        self.assertGreaterEqual(result['skipped'], len(rows))

    def test_file_replaced_by_directory_is_not_a_playback_file(self):
        directory = self.root / 'replacement.mp3'
        directory.mkdir()
        self.make_database([self.row(), self.row(directory, 150)])
        result = read_serato_bpms([*self.tracks, {'id': 'directory', 'path': str(directory)}],
                                 database=self.database)
        self.assertEqual(result['values'], {str(self.first): 128.35})

    def test_flags_streaming_and_non_audio_are_not_mistaken_for_analyzed_local_files(self):
        bad = [self.row(**{flag: 1}) for flag in ('is_missing', 'is_corrupt', 'is_stale')]
        bad += [self.row(third_party_type=7), self.row(type='video')]
        self.make_database(bad + [self.row(self.second, 122)])
        result = read_serato_bpms(self.tracks, database=self.database)
        self.assertEqual(result['values'], {str(self.second): 122})
        self.assertGreaterEqual(result['skipped'], len(bad))

    def test_valid_bpm_boundaries_and_invalid_or_nonfinite_values(self):
        self.make_database([self.row(bpm=20), self.row(self.second, 400)])
        self.assertEqual(read_serato_bpms(self.tracks, database=self.database)['values'],
                         {str(self.first): 20, str(self.second): 400})
        self.database.unlink()
        bad = [self.row(bpm=value) for value in (None, 0, -1, 19.99, 400.01,
                                                 float('inf'), float('-inf'), float('nan'), 'invalid')]
        self.make_database(bad)
        result = read_serato_bpms(self.tracks, database=self.database)
        self.assertEqual(result['values'], {})
        self.assertGreaterEqual(result['skipped'], len(bad))

    def test_conflicting_duplicate_rejects_whole_path_while_identical_duplicates_are_safe(self):
        self.make_database([self.row(bpm=100), self.row(bpm=120), self.row(bpm=100),
                            self.row(self.second, 122), self.row(self.second, 122)])
        result = read_serato_bpms(self.tracks, database=self.database)
        self.assertEqual(result['values'], {str(self.second): 122})
        self.assertGreaterEqual(result['skipped'], 1)

    def test_unknown_schema_missing_required_columns_and_master_database_fail_safe(self):
        for options in ({'version': 204}, {'omitted': ('file_size',)}, {'master': True}):
            with self.subTest(options=options):
                self.make_database([self.row()], **options)
                before = self.fingerprint()
                with self.assertRaises(SeratoBPMError):
                    read_serato_bpms(self.tracks, database=self.database)
                self.assertEqual(self.fingerprint(), before)
                self.database.unlink()

    def test_committed_wal_is_included_without_changing_source_main_wal_or_shm(self):
        self.make_database([self.row(bpm=100)])
        with closing(sqlite3.connect(self.database)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            writer.execute('UPDATE asset SET bpm=129.87')
            names = list(self.row(self.second, 80.25))
            writer.execute('INSERT INTO asset VALUES (' + ','.join('?' for _ in names) + ')',
                           tuple(self.row(self.second, 80.25)[name] for name in names))
            writer.commit()
            self.assertGreater(Path(str(self.database) + '-wal').stat().st_size, 32)
            # 主文件单独读取仍是旧值，证明下面验收确实依赖已提交 WAL。
            with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro&immutable=1', uri=True)) as bare:
                self.assertEqual(bare.execute('SELECT bpm FROM asset').fetchall(), [(100.0,)])
            before = self.fingerprint()
            result = read_serato_bpms(self.tracks, database=self.database)
            self.assertEqual(result['values'], {str(self.first): 129.87, str(self.second): 80.25})
            self.assertEqual(self.fingerprint(), before)

    def test_hot_journal_refuses_reading_and_preserves_previous_cache(self):
        self.make_database([self.row()])
        library = self.isolated_library()
        library.replace_serato_bpms({str(self.first): 95.5}, str(self.database))
        journal = Path(str(self.database) + '-journal')
        journal.write_bytes(b'nonempty uncommitted journal must not be recovered')
        before = self.fingerprint()
        cache = self.cached_rows(library)
        with self.assertRaises(SeratoBPMError):
            sync_library(library, database=self.database)
        self.assertEqual(self.cached_rows(library), cache)
        self.assertEqual(self.fingerprint(), before)

    def test_zero_byte_journal_is_safe_and_stays_unchanged(self):
        self.make_database([self.row()])
        Path(str(self.database) + '-journal').write_bytes(b'')
        before = self.fingerprint()
        self.assertEqual(read_serato_bpms(self.tracks, database=self.database)['values'],
                         {str(self.first): 128.35})
        self.assertEqual(self.fingerprint(), before)

    def test_missing_source_corrupt_database_and_failed_queries_preserve_previous_cache(self):
        library = self.isolated_library()
        library.replace_serato_bpms({str(self.first): 95.5}, str(self.database))
        cache = self.cached_rows(library)
        with self.assertRaises(SeratoBPMError):
            sync_library(library, database=self.database)
        self.assertFalse(self.database.exists())
        self.assertEqual(self.cached_rows(library), cache)
        self.database.write_bytes(b'not an SQLite database')
        before = self.fingerprint()
        with self.assertRaises(SeratoBPMError):
            sync_library(library, database=self.database)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.cached_rows(library), cache)
        self.database.unlink()
        # 合法 schema，但读取 view 需要未注册函数，触发真实 SQL 查询失败。
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('PRAGMA user_version=202')
            db.execute('CREATE VIEW asset AS SELECT "ignored" AS portable_id,'
                       'unavailable_serato_function(1) AS bpm,1 AS file_size,0 AS is_missing,'
                       '0 AS is_corrupt,0 AS is_stale,0 AS third_party_type,"audio" AS type')
        before = self.fingerprint()
        with self.assertRaises(SeratoBPMError):
            sync_library(library, database=self.database)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.cached_rows(library), cache)

    def test_cancellation_keeps_all_source_files_and_existing_cache(self):
        self.make_database([self.row(), self.row(self.second, 122)])
        library = self.isolated_library()
        library.replace_serato_bpms({str(self.first): 95.5}, str(self.database))
        cache = self.cached_rows(library)
        before = self.fingerprint()
        with self.assertRaises(AudioCancelled):
            read_serato_bpms(self.tracks, database=self.database, cancel=lambda: True)
        with self.assertRaises(AudioCancelled):
            sync_library(library, database=self.database, cancel=lambda: True)
        self.assertEqual(self.cached_rows(library), cache)
        self.assertEqual(self.fingerprint(), before)

    def test_cancellation_after_progress_discards_partial_read_and_preserves_cache(self):
        self.make_database([self.row(), self.row(self.second, 122)])
        library = self.isolated_library()
        library.replace_serato_bpms({str(self.first): 95.5}, str(self.database))
        cache = self.cached_rows(library)
        before = self.fingerprint()
        calls = 0

        def stop_after_progress():
            nonlocal calls
            calls += 1
            return calls >= 8

        with self.assertRaises(AudioCancelled):
            sync_library(library, database=self.database, cancel=stop_after_progress)
        self.assertGreaterEqual(calls, 8)
        self.assertEqual(self.cached_rows(library), cache)
        self.assertEqual(self.fingerprint(), before)

    def test_successful_sync_replaces_cache_and_reports_total_unmatched_library_tracks(self):
        self.make_database([self.row(bpm=128.35)])
        library = self.isolated_library()
        library.replace_serato_bpms({str(self.second): 95.5}, str(self.database))
        with library.connection() as db:
            original = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                        for table in ('tracks', 'members', 'collections')}
        before = self.fingerprint()
        result = sync_library(library, database=self.database)
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['missing'], 1)
        self.assertEqual(result['cleared'], 1)
        self.assertEqual(library.track('first')['bpm_serato'], 128.35)
        self.assertIsNone(library.track('second')['bpm_serato'])
        self.assertEqual(self.fingerprint(), before)
        with library.connection() as db:
            after = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                     for table in original}
        self.assertEqual(after, original)


if __name__ == '__main__':
    unittest.main()
