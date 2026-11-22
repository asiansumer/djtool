"""真实隔离 SQLite/文件验证：允许 Serato 标准日志别名，拒绝其他软链接。"""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from djtool.library import Library

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'connect_serato_library.py'
SPEC = importlib.util.spec_from_file_location('connect_serato_library_under_test', SCRIPT)
CONNECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONNECT)


class ConnectSeratoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory(prefix='djtool-connect-sample-')
        cls.sample = Path(cls.samples.name).resolve() / 'sample.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                        '-t', '2', '-c:a', 'libmp3lame', str(cls.sample)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='djtool-connect-')
        self.root = Path(self.temporary.name).resolve()
        self.serato = self.root / 'Documents' / 'Serato'
        self.serato.mkdir(parents=True)
        self.home = self.root / 'home'
        self.logs = self.home / 'Music' / '_Serato_' / 'Logs'
        self.logs.mkdir(parents=True)
        self.log = self.logs / '20261004-141212.8773.log'
        self.log.write_bytes(b'original native Serato log')
        self.alias = self.logs / 'DJ.INFO'
        self.alias.symlink_to(self.log.name)
        self.serato_database = self.home / 'Music' / '_Serato_' / 'database V2'
        self.serato_database.write_bytes(b'original native Serato database')
        self.library = Library(self.root / 'Existing DJTOOL')
        self.track_id, _ = self.library.import_file(self.sample)
        self.old_track = self.library.track(self.track_id)
        self.collection = self.library.create_collection('Existing Set', 'set')
        self.library.add_member(self.collection, self.track_id)
        shutil.copy2(self.sample, self.serato / 'reference.mp3')
        self.args = SimpleNamespace(library=self.library.root, serato_root=self.serato,
                                    serato_data_root=[], expected_audio_count=1, apply=True)

    def tearDown(self):
        self.temporary.cleanup()

    def run_connect(self):
        with patch.object(CONNECT.Path, 'home', return_value=self.home), redirect_stdout(io.StringIO()):
            return CONNECT.run(self.args)

    def test_standard_relative_log_alias_is_snapshotted_and_target_hashed_in_apply(self):
        before = CONNECT._standard_log_link(self.alias)
        report = self.run_connect()
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['duplicates'], 1)
        self.assertEqual(report['added'], 0)
        self.assertEqual(report['skipped_serato_links'], [])
        self.assertEqual(report['serato_log_links_before'], [before])
        self.assertEqual(report['serato_log_link_changes'], [])
        self.assertIn(str(self.log), {entry['path'] for entry in report['serato_data_before']})
        self.assertEqual(CONNECT._standard_log_link(self.alias), before)
        self.assertEqual(self.log.read_bytes(), b'original native Serato log')
        self.assertEqual(self.serato_database.read_bytes(), b'original native Serato database')
        self.assertEqual(self.library.track(self.track_id), self.old_track)
        self.assertEqual(self.library.tracks(self.collection)[0]['id'], self.track_id)
        self.assertTrue(Path(report['backup_path']).is_file())
        self.assertEqual(Library(self.library.root).music_root, self.serato / 'DJTOOL Audio')

    def test_log_alias_outside_same_logs_directory_halts_before_library_configuration(self):
        outside = self.logs.parent / 'outside.log'
        outside.write_bytes(b'do not follow this link')
        self.alias.unlink()
        self.alias.symlink_to('../outside.log')
        report = self.run_connect()
        self.assertFalse(report['passed'])
        self.assertIn(str(self.alias), report['skipped_serato_links'])
        self.assertEqual(report['added'], 0)
        self.assertEqual(report['duplicates'], 0)
        self.assertIsNone(Library(self.library.root).music_root)
        self.assertFalse((self.serato / 'DJTOOL Audio').exists())
        self.assertEqual(outside.read_bytes(), b'do not follow this link')
        self.assertEqual(self.library.track(self.track_id), self.old_track)

    def test_any_other_log_symlink_is_rejected_even_for_same_directory_target(self):
        other = self.logs / 'OTHER.INFO'
        other.symlink_to(self.log.name)
        report = self.run_connect()
        self.assertFalse(report['passed'])
        self.assertIn(str(other), report['skipped_serato_links'])
        self.assertIsNone(Library(self.library.root).music_root)
        self.assertEqual(report['serato_log_link_changes'], [])

    def test_log_link_target_change_is_detected_without_following_or_restoring_it(self):
        before = CONNECT._standard_log_link(self.alias)
        another = self.logs / 'later.log'
        another.write_bytes(b'concurrent new user log')
        self.alias.unlink()
        self.alias.symlink_to(another.name)
        self.assertEqual(CONNECT._verify_log_links([before]), [str(self.alias)])
        self.assertEqual(self.alias.readlink(), Path(another.name))
        self.assertEqual(another.read_bytes(), b'concurrent new user log')
        self.assertEqual(self.log.read_bytes(), b'original native Serato log')


if __name__ == '__main__':
    unittest.main()
