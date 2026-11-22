"""隔离曲库验证 Serato BPM 缓存：只匹配播放路径，不改原标签或成员关系。"""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from djtool.audio_tasks import AudioCancelled
from djtool.library import Library


class SeratoBpmCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'library')
        self.source = str(self.root / '_Serato_' / 'database V2')
        self.paths = {}
        self.original_files = {}
        with self.library.connection() as db:
            for number in range(1, 4):
                identifier = str(number)
                playback = self.root / f'playback-{number}.mp3'
                original = self.root / f'original-{number}.mp3'
                # 缓存不读取音频，因此用占位文件检查文件边界，无需生成音频或调用分析。
                playback.write_bytes(f'playback {number}'.encode())
                original.write_bytes(f'original {number}'.encode())
                self.original_files[playback] = playback.read_bytes()
                self.original_files[original] = original.read_bytes()
                self.paths[identifier] = str(playback)
                db.execute('INSERT INTO tracks(id,digest,title,artist,album,duration,path,'
                           'source_path,filename,bpm_tag,bpm_estimate,bpm_manual,reviewed,note) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, f'digest-{number}', '相同标题', '艺人', '专辑', 120,
                            str(playback), str(original), playback.name, 90, 91, 92, 1, '已整理'))
            db.execute("INSERT INTO collections(id,name,kind) VALUES('set','演出','set')")
            db.execute("INSERT INTO collections(id,name,kind) VALUES('group','分类','group')")
            db.executemany('INSERT INTO members(collection_id,track_id,position,note) VALUES(?,?,?,?)',
                           [('set', '2', 0, '第一首'), ('set', '1', 1, '第二首'),
                            ('group', '1', 0, '分组备注')])

    def tearDown(self):
        self.temp.cleanup()

    def cache_rows(self):
        with self.library.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM serato_bpm_cache ORDER BY track_id')]

    def untouched_rows(self):
        with self.library.connection() as db:
            return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in ('tracks', 'collections', 'members')}

    def test_persists_and_joins_library_and_set_tracks_without_changing_old_fields(self):
        before = self.untouched_rows()
        result = self.library.replace_serato_bpms(
            {self.paths['1']: 124.5, self.paths['2']: 128}, Path(self.source))
        self.assertEqual(result, {'matched': 2, 'changed': 2, 'cleared': 0, 'missing': 0})
        reopened = Library(self.library.root)
        first = reopened.track('1')
        self.assertEqual(first['bpm_serato'], 124.5)
        self.assertEqual(first['bpm_serato_source'], self.source)
        self.assertTrue(first['bpm_serato_synced_at'])
        self.assertEqual((first['bpm_tag'], first['bpm_estimate'], first['bpm_manual']), (90, 91, 92))
        self.assertIsNone(reopened.track('3')['bpm_serato'])
        self.assertIsNone(reopened.track('3')['bpm_serato_source'])
        self.assertIsNone(reopened.track('3')['bpm_serato_synced_at'])
        self.assertEqual([(t['id'], t['bpm_serato'], t['set_note']) for t in reopened.tracks('set')],
                         [('2', 128, '第一首'), ('1', 124.5, '第二首')])
        self.assertEqual({t['id']: t['bpm_serato'] for t in reopened.tracks()},
                         {'1': 124.5, '2': 128, '3': None})
        self.assertEqual(self.untouched_rows(), before)
        for path, original in self.original_files.items():
            self.assertEqual(path.read_bytes(), original)

    def test_only_exact_playback_path_matches_not_source_title_or_normalized_alias(self):
        first = self.library.track('1')
        alias = str(self.root) + '/elsewhere/../playback-1.mp3'
        values = {self.paths['1']: 100, first['source_path']: 110,
                  str(self.root / '相同标题.mp3'): 120, alias: 130,
                  self.paths['2'].upper(): 140}
        result = self.library.replace_serato_bpms(values, self.source)
        self.assertEqual(result, {'matched': 1, 'changed': 1, 'cleared': 0, 'missing': 4})
        self.assertEqual(self.library.track('1')['bpm_serato'], 100)
        self.assertIsNone(self.library.track('2')['bpm_serato'])
        self.assertIsNone(self.library.track('3')['bpm_serato'])

    def test_full_read_clears_stale_cache_and_reports_changes_without_counting_timestamp(self):
        self.library.replace_serato_bpms({self.paths['1']: 100, self.paths['2']: 120}, self.source)
        with self.library.connection() as db:
            db.execute("UPDATE serato_bpm_cache SET synced_at='2000-01-01 00:00:00'")
        result = self.library.replace_serato_bpms(
            {self.paths['1']: 100, str(self.root / 'not-in-djtool.mp3'): 130}, self.source)
        self.assertEqual(result, {'matched': 1, 'changed': 0, 'cleared': 1, 'missing': 1})
        self.assertIsNone(self.library.track('2')['bpm_serato'])
        self.assertNotEqual(self.library.track('1')['bpm_serato_synced_at'], '2000-01-01 00:00:00')
        result = self.library.replace_serato_bpms({self.paths['1']: 101}, self.source)
        self.assertEqual(result['changed'], 1)
        other_source = str(self.root / 'other-Serato' / 'database V2')
        result = self.library.replace_serato_bpms({self.paths['1']: 101}, other_source)
        self.assertEqual(result['changed'], 1)
        self.assertEqual(self.library.track('1')['bpm_serato_source'], other_source)
        self.assertEqual(self.library.replace_serato_bpms({}, other_source),
                         {'matched': 0, 'changed': 0, 'cleared': 1, 'missing': 0})
        self.assertEqual(self.cache_rows(), [])

    def test_invalid_mapping_entirely_rejected_before_any_changes(self):
        self.library.replace_serato_bpms({self.paths['1']: 100, self.paths['2']: 120}, self.source)
        original = self.cache_rows()
        invalid_values = (None, True, False, '130', 19.99, 400.01,
                          float('nan'), float('inf'), -float('inf'))
        for invalid in invalid_values:
            with self.subTest(bpm=invalid):
                with self.assertRaises(ValueError):
                    self.library.replace_serato_bpms(
                        {self.paths['1']: 160, self.paths['2']: invalid}, self.source)
                self.assertEqual(self.cache_rows(), original)
        invalid_paths = ('relative.mp3', '', '\0bad', Path(self.paths['1']))
        for invalid in invalid_paths:
            with self.subTest(path=invalid):
                with self.assertRaises(ValueError):
                    self.library.replace_serato_bpms({self.paths['2']: 160, invalid: 100}, self.source)
                self.assertEqual(self.cache_rows(), original)
        for invalid in ([], [('a', 100)], None):
            with self.assertRaises(ValueError):
                self.library.replace_serato_bpms(invalid, self.source)
            self.assertEqual(self.cache_rows(), original)
        for invalid in ('relative-db', '', '\0bad', None):
            with self.assertRaises(ValueError):
                self.library.replace_serato_bpms({}, invalid)
            self.assertEqual(self.cache_rows(), original)

    def test_numeric_boundaries_supported(self):
        self.library.replace_serato_bpms({self.paths['1']: 20, self.paths['2']: 400}, self.source)
        self.assertEqual(self.library.track('1')['bpm_serato'], 20)
        self.assertEqual(self.library.track('2')['bpm_serato'], 400)

    def test_cancellation_at_commit_rolls_back_updates_and_stale_clear(self):
        self.library.replace_serato_bpms({self.paths['1']: 100, self.paths['2']: 120}, self.source)
        original = self.cache_rows()
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            # 验证完整输入后，更新第一首并删除第二首，最终提交前停止。
            return calls == 6

        with self.assertRaises(AudioCancelled):
            self.library.replace_serato_bpms({self.paths['1']: 160}, self.source, cancel)
        self.assertEqual(calls, 6)
        self.assertEqual(self.cache_rows(), original)

    def test_sql_failure_rolls_back_partial_upserts(self):
        self.library.replace_serato_bpms({self.paths['1']: 100}, self.source)
        original = self.cache_rows()
        with self.library.connection() as db:
            db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON serato_bpm_cache "
                       "WHEN NEW.track_id='2' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.replace_serato_bpms({self.paths['1']: 160, self.paths['2']: 170}, self.source)
        self.assertEqual(self.cache_rows(), original)

    def test_additive_schema_four_and_track_deletion_cascades(self):
        before = self.untouched_rows()
        with self.library.connection() as db:
            db.execute('DROP TABLE serato_bpm_cache')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
        self.library = Library(self.library.root)
        self.assertEqual(self.untouched_rows(), before)
        self.library.replace_serato_bpms({self.paths['1']: 100}, self.source)
        with self.library.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            db.execute("DELETE FROM tracks WHERE id='1'")
        self.assertEqual(self.cache_rows(), [])
        self.assertEqual([t['id'] for t in self.library.tracks('set')], ['2'])


if __name__ == '__main__':
    unittest.main()
