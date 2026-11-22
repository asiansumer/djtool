"""隔离验证未来保存位置、原地只读索引、Set 引用和失败文件边界。"""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mutagen.id3 import APIC
from mutagen.mp3 import EasyMP3, MP3
from djtool.audio_tasks import AudioCancelled
from djtool.library import Library
from djtool.storage import preview_existing, storage_path
from djtool.tracks import read_audio


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory(prefix='djtool-storage-audio-')
        cls.audio = {}
        for extension, codec in [('mp3', 'libmp3lame'), ('wav', 'pcm_s16le'), ('flac', 'flac'),
                                 ('aiff', 'pcm_s16be'), ('m4a', 'aac')]:
            path = Path(cls.samples.name).resolve() / ('fixture.' + extension)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                            '-t', '3', '-c:a', codec, '-metadata', 'title=Read only song',
                            '-metadata', 'artist=Fixture Artist', str(path)], check=True)
            cls.audio[extension] = path

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='djtool-storage-')
        self.root = Path(self.temporary.name).resolve()
        self.library = Library(self.root / 'Existing DJTOOL')
        self.serato = self.root / 'Documents' / 'Serato'
        self.serato.mkdir(parents=True)
        self.destination = self.serato / 'DJTOOL Audio'
        self.source = self.root / 'original.mp3'
        shutil.copy2(self.audio['mp3'], self.source)

    def tearDown(self):
        self.temporary.cleanup()

    def test_destination_changes_only_future_copies_and_persists_without_set_directories(self):
        old_id, _ = self.library.import_file(self.source)
        old_path = self.library.track(old_id)['path']
        collection = self.library.create_collection('Friday Set', 'set')
        self.library.add_member(collection, old_id)
        source_bytes = self.source.read_bytes()
        self.assertIsNone(self.library.music_root)
        self.assertEqual(self.library.configure_music_root(self.destination), self.destination)
        reopened = Library(self.library.root)
        self.assertEqual(reopened.music_root, self.destination)
        self.assertEqual(reopened.track(old_id)['path'], old_path)
        self.assertEqual(reopened.tracks(collection)[0]['id'], old_id)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        second = self.root / 'second.mp3'
        shutil.copy2(self.source, second)
        tags = EasyMP3(second)
        tags['title'] = 'Another recording'
        tags.save()
        before = second.read_bytes()
        new_id, added = reopened.import_file(second)
        self.assertTrue(added)
        self.assertEqual(Path(reopened.track(new_id)['path']).parent, self.destination)
        self.assertEqual(second.read_bytes(), before)
        before_dirs = {p for p in self.root.rglob('*') if p.is_dir()}
        another = reopened.create_collection('Saturday Set', 'set')
        reopened.add_member(collection, new_id)
        reopened.add_member(another, new_id)
        reopened.reorder(collection, [new_id, old_id])
        reopened.delete_collection(another)
        self.assertEqual({p for p in self.root.rglob('*') if p.is_dir()}, before_dirs)
        self.assertTrue(Path(old_path).is_file())

    def test_failed_config_write_keeps_old_config_paths_and_only_cleans_created_empty_directories(self):
        self.library.configure_music_root(self.destination)
        original = storage_path(self.library.root).read_bytes()
        failed = self.root / 'New parent' / 'new audio'
        with patch('djtool.storage.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.library.configure_music_root(failed)
        self.assertEqual(self.library.audio_dir, self.destination)
        self.assertEqual(storage_path(self.library.root).read_bytes(), original)
        self.assertFalse((self.root / 'New parent').exists())
        self.assertEqual(list(storage_path(self.library.root).parent.glob('.storage-*')), [])

    def test_protected_or_symlink_destination_never_changes_config(self):
        original = self.library.audio_dir
        outside = self.root / 'Other folder'
        outside.mkdir()
        linked = self.serato / 'linked'
        linked.symlink_to(outside, target_is_directory=True)
        for target in [self.serato / '_Serato_' / 'Audio', self.serato / '_Serato_Backup' / 'Audio',
                       linked / 'Audio', self.source]:
            with self.subTest(path=target), self.assertRaises(ValueError):
                self.library.configure_music_root(target)
            self.assertEqual(self.library.audio_dir, original)
            self.assertFalse(storage_path(self.library.root).exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_constructor_rejects_serato_internal_or_linked_roots_before_creating_files(self):
        protected = self.serato / '_Serato_'
        protected.mkdir()
        original = protected / 'database V2'
        original.write_bytes(b'original Serato database')
        backup = self.serato / '_Serato_Backup'
        backup.mkdir()
        outside = self.root / 'Untouched'
        outside.mkdir()
        linked = self.serato / 'linked library'
        linked.symlink_to(outside, target_is_directory=True)
        for path in (protected, protected / 'App', backup, linked, linked / 'App'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Library(path)
        self.assertEqual(list(protected.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b'original Serato database')
        self.assertEqual(list(backup.iterdir()), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_missing_or_replaced_destination_does_not_recreate_or_write_elsewhere(self):
        old_id, _ = self.library.import_file(self.source)
        old_path = self.library.track(old_id)['path']
        self.library.configure_music_root(self.destination)
        self.destination.rmdir()
        reopened = Library(self.library.root)
        self.assertFalse(self.destination.exists())
        self.assertEqual(reopened.track(old_id)['path'], old_path)
        with self.assertRaises(FileNotFoundError):
            reopened.import_file(self.source)
        outside = self.root / 'Outside'
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            reopened.import_file(self.source)
        self.assertEqual(list(outside.iterdir()), [])

    def test_read_only_index_preserves_supported_formats_tags_paths_and_content(self):
        for extension, fixture in self.audio.items():
            with self.subTest(format=extension):
                source = self.serato / ('song.' + extension)
                shutil.copy2(fixture, source)
                before = source.read_bytes()
                before_stat = source.stat()
                identifier, added = self.library.reference_existing(source, self.serato)
                self.assertTrue(added)
                track = self.library.track(identifier)
                self.assertEqual(track['path'], str(source))
                self.assertEqual(track['source_path'], str(source))
                self.assertEqual(track['title'], 'Read only song')
                # ffmpeg 的 AIFF 写入 NAME，但不把 artist 映射为 AUTH；未知字段不编造。
                self.assertEqual(track['artist'], '未知艺人' if extension == 'aiff' else 'Fixture Artist')
                self.assertAlmostEqual(track['duration'], 3, delta=.2)
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(source.stat().st_mtime_ns, before_stat.st_mtime_ns)
                self.assertEqual(self.library.reference_existing(source, self.serato), (identifier, False))
        self.assertEqual(len(self.library.tracks()), 5)
        self.assertEqual(list(self.library.audio_dir.iterdir()), [])

    def test_read_only_reference_deduplicates_with_existing_copy_without_changing_its_path(self):
        identifier, _ = self.library.import_file(self.source)
        old = self.library.track(identifier)
        reference = self.serato / 'same bytes.mp3'
        shutil.copy2(self.source, reference)
        self.assertEqual(self.library.reference_existing(reference, self.serato), (identifier, False))
        self.assertEqual(self.library.track(identifier), old)
        self.assertEqual(reference.read_bytes(), self.source.read_bytes())

    def test_refresh_metadata_supports_all_reference_formats_and_preserves_user_edits(self):
        edited = {'title': 'User display title', 'artist': 'User artist', 'genre': 'User genre',
                  'phase': '暖场', 'note': 'Keep this note', 'bpm_manual': 90, 'reviewed': 1}
        for extension, fixture in self.audio.items():
            with self.subTest(format=extension):
                source = self.serato / ('refresh.' + extension)
                shutil.copy2(fixture, source)
                identifier, _ = self.library.reference_existing(source, self.serato)
                self.library.update_track(identifier, edited)
                before = source.read_bytes()
                refreshed = self.library.refresh_metadata(identifier)
                for key, value in edited.items():
                    self.assertEqual(refreshed[key], value)
                self.assertEqual(refreshed['path'], str(source))
                self.assertEqual(refreshed['file_size'], len(before))
                self.assertGreater(refreshed['sample_rate'], 0)
                self.assertEqual(source.read_bytes(), before)

    def test_refresh_metadata_changed_source_keeps_previous_database_metadata(self):
        source = self.serato / 'refresh.mp3'
        shutil.copy2(self.source, source)
        identifier, _ = self.library.reference_existing(source, self.serato)
        previous = self.library.track(identifier)
        original_read = read_audio
        def changed(path):
            result = original_read(path)
            with path.open('ab') as stream:
                stream.write(b'fixture change')
            return result
        with patch('djtool.library.read_audio', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                self.library.refresh_metadata(identifier)
        self.assertEqual(self.library.track(identifier), previous)

    def test_reference_rejects_root_escape_protected_contents_links_and_wrong_format(self):
        protected = self.serato / '_Serato_'
        protected.mkdir()
        inside = protected / 'song.mp3'
        shutil.copy2(self.source, inside)
        link = self.serato / 'escape.mp3'
        link.symlink_to(self.source)
        fake = self.serato / 'wrong.wav'
        shutil.copy2(self.source, fake)
        for source in (self.source, inside, link, fake):
            with self.subTest(path=source), self.assertRaises(ValueError):
                self.library.reference_existing(source, self.serato)
        self.assertEqual(self.library.tracks(), [])
        self.assertTrue(self.source.is_file())

    def test_changed_reference_and_cancel_never_commit_partial_index(self):
        source = self.serato / 'song.mp3'
        shutil.copy2(self.source, source)
        original_read = read_audio
        def changed(path):
            result = original_read(path)
            with path.open('ab') as stream:
                stream.write(b'changed by fixture')
            return result
        with patch('djtool.library.read_audio', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                self.library.reference_existing(source, self.serato)
        self.assertEqual(self.library.tracks(), [])
        with self.assertRaises(AudioCancelled):
            self.library.reference_existing(source, self.serato, cancel=lambda: True)
        self.assertEqual(self.library.tracks(), [])
        self.assertTrue(source.is_file())

    def test_cancel_during_hash_stops_without_modifying_source(self):
        source = self.serato / 'song.mp3'
        shutil.copy2(self.source, source)
        before = source.read_bytes()
        checks = 0
        def cancel():
            nonlocal checks
            checks += 1
            return checks >= 2
        with self.assertRaises(AudioCancelled):
            self.library.reference_existing(source, self.serato, cancel=cancel)
        self.assertEqual(checks, 2)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.library.tracks(), [])

    def test_broken_configuration_does_not_fall_back_or_create_new_audio_location(self):
        config = storage_path(self.library.root)
        for value in ['{broken', json.dumps({'version':2,'music_root':str(self.destination)}),
                      json.dumps({'version':1,'music_root':'relative/location'})]:
            config.write_text(value, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '设置损坏'):
                Library(self.library.root)
            self.assertEqual(config.read_text(encoding='utf-8'), value)
            self.assertFalse(self.destination.exists())

    def test_reference_database_failure_removes_only_created_artwork_never_source(self):
        source = self.serato / 'covered.mp3'
        shutil.copy2(self.source, source)
        tagged = MP3(source)
        if tagged.tags is None:
            tagged.add_tags()
        tagged.tags.add(APIC(encoding=3, mime='image/png', type=3, desc='cover', data=b'fixture PNG'))
        tagged.save()
        before = source.read_bytes()
        existing_art = self.library.art_dir / 'user-art.png'
        existing_art.write_bytes(b'user cover')
        with patch.object(self.library, '_insert_track', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.library.reference_existing(source, self.serato)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.library.tracks(), [])
        self.assertEqual(list(self.library.art_dir.iterdir()), [existing_art])
        self.assertEqual(existing_art.read_bytes(), b'user cover')

    def test_future_import_database_failure_removes_only_created_copy(self):
        self.library.configure_music_root(self.destination)
        existing = self.destination / 'user track.wav'
        existing.write_bytes(b'user bytes')
        before = self.source.read_bytes()
        with patch.object(self.library, '_insert_track', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.library.import_file(self.source)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.destination.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b'user bytes')
        self.assertEqual(self.library.tracks(), [])

    def test_preview_skips_serato_databases_and_apply_changes_only_json_configuration(self):
        source = self.serato / 'existing.mp3'
        shutil.copy2(self.source, source)
        protected = self.serato / '_Serato_'
        protected.mkdir()
        database = protected / 'database V2'
        database.write_bytes(b'serato user database')
        hidden_audio = protected / 'ignored.mp3'
        shutil.copy2(self.source, hidden_audio)
        self.assertEqual(preview_existing(self.serato)['audio_count'], 1)
        before_db = self.library.database.read_bytes()
        before_serato = database.read_bytes()
        before_audio = source.read_bytes()
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'qa_storage_live.py'
        preview = subprocess.run([sys.executable, str(script), '--preview', '--serato-root', str(self.serato)],
                                 check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(preview.stdout)['audio_count'], 1)
        self.assertFalse(storage_path(self.library.root).exists())
        applied = subprocess.run([sys.executable, str(script), '--apply', '--library', str(self.library.root),
                                  '--serato-root', str(self.serato)], check=True, capture_output=True, text=True)
        self.assertFalse(json.loads(applied.stdout)['sqlite_opened'])
        self.assertEqual(self.library.database.read_bytes(), before_db)
        self.assertEqual(database.read_bytes(), before_serato)
        self.assertEqual(source.read_bytes(), before_audio)
        self.assertEqual(Library(self.library.root).music_root, self.destination)


if __name__ == '__main__':
    unittest.main()
