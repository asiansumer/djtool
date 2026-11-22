"""收歌自动引用的隔离验证，不写真实 Serato，不播放或计算 BPM。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from djtool.audio_tasks import AudioCancelled
from djtool.library import Library
from djtool.serato import SeratoCrateAdapter, SeratoConflict, SeratoError, SeratoNotValidated, decode_crate_paths
from djtool.serato_intake import COLLECTION, status_text, sync_library


class SeratoIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.library = Library(self.root / 'library')
        self.serato = self.root / '_Serato_'
        self.serato.mkdir()
        self.adapter = SeratoCrateAdapter(self.library.root, self.serato,
                                         isolated=True, process_checker=lambda: False)
        self.add_track('one', reviewed=1)
        self.add_track('two')
        self.set_id = self.library.create_collection('演出', 'set')
        self.library.add_member(self.set_id, 'two')
        self.library.add_member(self.set_id, 'one')
        self.library.set_note(self.set_id, 'one', '保留我的过渡备注')
        (self.serato / 'database V2').write_bytes(b'original database')
        (self.serato / 'Subcrates').mkdir()
        (self.serato / 'Subcrates/Personal.crate').write_bytes(b'personal crate')

    def tearDown(self):
        self.temp.cleanup()

    def add_track(self, identifier, reviewed=0):
        path = self.library.audio_dir / (identifier + '.mp3')
        path.write_bytes(b'no decoding fixture ' + identifier.encode())
        with self.library.connection() as db:
            db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename,reviewed) '
                       'VALUES(?,?,?,?,?,?,?,?,?)', (identifier, identifier, '相同歌名', '艺人',
                        60, str(path), str(path), path.name, reviewed))

    def rows(self):
        with self.library.connection() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                    for name in ('tracks', 'collections', 'members')}

    def sync(self, **kwargs):
        return sync_library(self.library, adapter=self.adapter, native_check=lambda plan: False, **kwargs)

    def test_all_collected_tracks_independent_of_review_and_set_preserve_everything(self):
        before = self.rows()
        files = {p: p.read_bytes() for p in self.library.audio_dir.iterdir()}
        result = self.sync()
        self.assertEqual((result['status'], result['count']), ('ready', 2))
        crate = self.adapter.preview_set(COLLECTION, self.library.tracks()).crate_path
        self.assertEqual(len(decode_crate_paths(crate.read_bytes())), 2)
        self.assertEqual(before, self.rows())
        self.assertEqual(files, {p: p.read_bytes() for p in files})
        self.assertEqual((self.serato / 'database V2').read_bytes(), b'original database')
        self.assertEqual((self.serato / 'Subcrates/Personal.crate').read_bytes(), b'personal crate')
        self.assertEqual(sorted(p.name for p in self.library.audio_dir.iterdir()), ['one.mp3', 'two.mp3'])

    def test_running_waits_and_restart_reconstructs_pending_from_library(self):
        self.adapter.process_checker = lambda: True
        self.assertEqual(self.sync()['status'], 'waiting')
        self.assertFalse(self.adapter.manifest_path.exists())
        self.library = Library(self.library.root)
        self.add_track('three')
        self.adapter.process_checker = lambda: False
        self.assertEqual(self.sync()['count'], 3)

    def test_new_track_updates_reference_once_without_duplicate_backup_on_idle(self):
        self.sync()
        self.sync()
        self.assertEqual(len(list((self.adapter.state_dir / 'backups').iterdir())), 1)
        self.add_track('remix')
        self.assertEqual(self.sync()['count'], 3)
        self.sync()
        self.assertEqual(len(list((self.adapter.state_dir / 'backups').iterdir())), 2)

    def test_native_receipt_is_separate_from_ready_and_persists(self):
        self.sync()
        self.assertFalse(json.loads((self.library.root / '.djtool/serato-intake.json').read_text())['received'])
        result = sync_library(self.library, adapter=self.adapter, native_check=lambda plan: True)
        self.assertEqual(result['status'], 'received')
        with patch('djtool.serato_intake._native_received', side_effect=AssertionError('must use persisted receipt')):
            self.assertEqual(self.sync()['status'], 'received')

    def test_failed_or_unknown_native_read_never_reports_received(self):
        self.sync()
        def failure(plan): raise SeratoNotValidated('unknown')
        self.assertEqual(sync_library(self.library, adapter=self.adapter, native_check=failure)['status'], 'ready')

    def test_missing_track_does_not_block_valid_new_tracks_or_remove_library_rows(self):
        self.library.track('one')
        Path(self.library.track('one')['path']).unlink()
        before = self.rows()
        result = self.sync()
        self.assertEqual((result['count'], result['missing'], result['status']), (1, 1, 'ready'))
        self.assertEqual(self.rows(), before)
        self.assertIn('1 首文件缺失', status_text(result))

    def test_cancel_before_write_and_write_failure_preserve_previous_crate(self):
        self.sync()
        old = self.adapter.preview_set(COLLECTION, self.library.tracks()).crate_path.read_bytes()
        self.add_track('three')
        with self.assertRaises(AudioCancelled): self.sync(cancel=lambda: True)
        with patch.object(self.adapter, 'sync_set', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.sync()
        self.assertEqual(self.adapter.preview_set(COLLECTION, self.library.tracks()).crate_path.read_bytes(), old)
        self.assertEqual(self.sync()['count'], 3)

    def test_external_edit_never_overwritten(self):
        self.sync()
        path = self.adapter.preview_set(COLLECTION, self.library.tracks()).crate_path
        path.write_bytes(b'user changed it')
        self.add_track('three')
        with self.assertRaises(SeratoConflict): self.sync()
        self.assertEqual(path.read_bytes(), b'user changed it')

    def test_unvalidated_production_refuses_writes_and_corrupt_state_preserved(self):
        live = SeratoCrateAdapter(self.library.root, self.serato, process_checker=lambda: False)
        with self.assertRaises(SeratoNotValidated): sync_library(self.library, adapter=live)
        self.assertFalse(live.manifest_path.exists())
        state = self.library.root / '.djtool/serato-intake.json'
        state.write_bytes(b'broken state')
        with self.assertRaises(SeratoError): self.sync()
        self.assertEqual(state.read_bytes(), b'broken state')


if __name__ == '__main__': unittest.main()
