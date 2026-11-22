"""用临时合成 MP3 验证文件边界、事务、去重与各 Set 的独立顺序。"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from mutagen.mp3 import EasyMP3
from djtool.library import Library
from djtool.tracks import effective_bpm


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory()
        cls.sample = Path(cls.samples.name) / 'sample.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=330:duration=4',
                        '-c:a', 'libmp3lame', '-q:a', '7', str(cls.sample)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / '中文 歌曲.mp3'
        shutil.copy2(self.sample, self.source)
        self.library = Library(self.root / 'library')

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name, title=None):
        source = self.root / name
        shutil.copy2(self.sample, source)
        if title:
            audio = EasyMP3(source)
            audio['title'] = title
            audio['artist'] = '测试艺人'
            audio['bpm'] = '96'
            audio.save()
        return self.library.import_file(source)[0]

    def test_source_unchanged_and_duplicate_content_shared(self):
        before = self.source.read_bytes()
        identifier, added = self.library.import_file(self.source)
        self.assertTrue(added)
        renamed = self.root / '另一个名字.MP3'
        renamed.write_bytes(before)
        self.assertEqual(self.library.import_file(renamed), (identifier, False))
        self.assertEqual(len(self.library.tracks()), 1)
        track = self.library.track(identifier)
        self.assertEqual(track['title'], '中文 歌曲')
        self.assertEqual(track['artist'], '未知艺人')
        self.assertEqual(Path(track['path']).read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertNotEqual(track['path'], str(self.source))
        self.assertEqual(len(list(self.library.audio_dir.iterdir())), 1)

    def test_same_filename_different_content_kept(self):
        first = self.add('same.mp3', '版本 A')
        second = self.add('same.mp3', '版本 B')
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.library.tracks()), 2)

    def test_corruption_and_write_failure_leave_no_records_or_partials(self):
        corrupt = self.root / 'bad.mp3'
        corrupt.write_text('not audio')
        with self.assertRaises(ValueError):
            self.library.import_file(corrupt)
        original = self.source.read_bytes()
        with patch('djtool.library.shutil.copyfileobj', side_effect=OSError('磁盘空间不足')):
            with self.assertRaises(OSError):
                self.library.import_file(self.source)
        self.assertEqual(self.library.tracks(), [])
        self.assertEqual(list(self.library.audio_dir.iterdir()), [])
        self.assertEqual(original, self.source.read_bytes())

    def test_sets_groups_order_and_notes_are_independent_and_persist(self):
        ids = [self.add(f'{i}.mp3', f'歌曲 {i}') for i in range(3)]
        first = self.library.create_collection('周五', 'set')
        second = self.library.create_collection('周六', 'set')
        group = self.library.create_collection('暖场', 'group')
        original_paths = [self.library.track(identifier)['path'] for identifier in ids]
        for identifier in ids:
            self.library.add_member(first, identifier)
            self.library.add_member(second, identifier)
            self.library.add_member(group, identifier)
        self.assertFalse(self.library.add_member(first, ids[0]))
        self.library.reorder(first, list(reversed(ids)))
        self.library.set_note(first, ids[0], '两拍之后切入')
        reopened = Library(self.library.root)
        self.assertEqual([t['id'] for t in reopened.tracks(first)], list(reversed(ids)))
        self.assertEqual([t['id'] for t in reopened.tracks(second)], ids)
        self.assertEqual(reopened.tracks(first)[-1]['set_note'], '两拍之后切入')
        self.assertEqual(reopened.tracks(second)[0]['set_note'], '')
        reopened.delete_collection(first)
        reopened.remove_member(group, ids[0])
        self.assertEqual([t['id'] for t in reopened.tracks(second)], ids)
        self.assertTrue(all(Path(path).is_file() for path in original_paths))
        self.assertEqual(len(list(self.library.audio_dir.glob('*.mp3'))), 3)

    def test_bad_reorder_is_atomic(self):
        ids = [self.add(f'{i}.mp3', str(i)) for i in range(2)]
        collection = self.library.create_collection('Set', 'set')
        for identifier in ids:
            self.library.add_member(collection, identifier)
        for bad_order in ([ids[0], ids[0]], [ids[1]], ids+['missing']):
            with self.assertRaises(ValueError):
                self.library.reorder(collection, bad_order)
        self.assertEqual([t['id'] for t in self.library.tracks(collection)], ids)

    def test_manual_bpm_and_group_updates_persist(self):
        identifier = self.add('bpm.mp3', 'Track')
        group = self.library.create_collection('Warm-up', 'group')
        self.library.update_track(identifier, {'bpm_manual': 192, 'energy': 3, 'reviewed': 1}, [group])
        track = Library(self.library.root).track(identifier)
        self.assertEqual(effective_bpm(track), (192, '已确认'))
        self.assertEqual(track['bpm_tag'], 96)
        with self.assertRaises(ValueError):
            self.library.update_track(identifier, {'title': '不应保存'}, ['nonexistent'])
        self.assertEqual(self.library.track(identifier)['title'], 'Track')
        self.assertEqual(self.library.memberships(identifier), {group})
        self.library.import_file(self.root / 'bpm.mp3')
        self.assertEqual(self.library.track(identifier)['bpm_manual'], 192)

    def test_missing_file_and_case_insensitive_collection_name(self):
        with self.assertRaises(FileNotFoundError):
            self.library.import_file(self.root / 'missing.mp3')
        self.library.create_collection('Party', 'set')
        with self.assertRaises(ValueError):
            self.library.create_collection('party', 'set')
        identifier = self.library.import_file(self.source)[0]
        Path(self.library.track(identifier)['path']).unlink()
        with self.assertRaisesRegex(ValueError, '丢失'):
            self.library.import_file(self.source)


if __name__ == '__main__':
    unittest.main()
