"""Serato 隔离写入：顺序引用、托管范围、失败恢复与冲突。没有真实 Serato 写入。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from djtool import serato
from djtool.serato import (SeratoCrateAdapter, SeratoConflict, SeratoError, SeratoNotValidated,
                    decode_crate_paths, plan_set, verify_native_set, validate_native_evidence,
                    VALIDATION_PHASES)
import hashlib


class SeratoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.music = self.root / 'music'
        self.music.mkdir()
        self.target = self.root / '_Serato_'
        self.crates = self.target / 'Subcrates'
        self.crates.mkdir(parents=True)
        self.unrelated = self.crates / 'Personal.crate'
        self.unrelated.write_bytes(b'keep-my-crate')
        self.database = self.target / 'database V2'
        self.database.write_bytes(b'keep-database')
        self.a = self.music / '亚洲 夏天.mp3'
        self.b = self.music / 'Second.mp3'
        self.a.write_bytes(b'synthetic-audio-a')
        self.b.write_bytes(b'synthetic-audio-b')
        self.collection = {'id': 'party-set-01', 'name': '周五 Set', 'kind': 'set'}
        self.tracks = [{'title': 'Second', 'path': str(self.b)},
                       {'title': '夏天', 'path': str(self.a)}]
        self.adapter = SeratoCrateAdapter(self.music, self.target, isolated=True,
                                         process_checker=lambda: False)

    def tearDown(self):
        self.temp.cleanup()

    def native_fixture(self, path, plan):
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''PRAGMA user_version=202;
                CREATE TABLE container(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE dj_container_metadata(container_id INTEGER,filename TEXT);
                CREATE TABLE asset(id INTEGER PRIMARY KEY,portable_id TEXT);
                CREATE TABLE space_asset(id INTEGER PRIMARY KEY,asset_id INTEGER);
                CREATE TABLE container_asset(id INTEGER PRIMARY KEY,container_id INTEGER,
                    space_asset_id INTEGER,list_order INTEGER);
            ''')
            db.execute('INSERT INTO container VALUES(1,?)', (plan.name,))
            db.execute('INSERT INTO dj_container_metadata VALUES(1,?)', (plan.crate_path.name,))
            for index, reference in enumerate(plan.paths, 1):
                db.execute('INSERT INTO asset VALUES(?,?)', (index, reference))
                db.execute('INSERT INTO space_asset VALUES(?,?)', (index, index))
                db.execute('INSERT INTO container_asset VALUES(?,1,?,?)', (index,index,index))

    def native_evidence_fixture(self):
        fingerprint = {'installed_version': '4.0.10', 'bundle_id': 'com.serato.seratodj',
                       'binary_sha256': 'a'*64}
        steps = []
        for phase in VALIDATION_PHASES:
            tracks = self.tracks[::-1] if phase in ('update', 'reopen_update') else self.tracks
            plan = self.adapter.preview_set(self.collection, tracks)
            database = self.root / (phase + '.sqlite')
            self.native_fixture(database, plan)
            steps.append({'phase': phase, 'process_closed': True, 'database': str(database),
                          'database_sha256': hashlib.sha256(database.read_bytes()).hexdigest(),
                          'collection_id': plan.collection_id, 'name': plan.name,
                          'crate_filename': plan.crate_path.name, 'paths': list(plan.paths)})
        report = {'version': 1, 'fingerprint': fingerprint, 'steps': steps}
        path = self.root / 'evidence.json'
        path.write_text(json.dumps(report), encoding='utf-8')
        return fingerprint, report, path

    def test_plan_uses_same_file_in_custom_order_no_audio_copy(self):
        plan = self.adapter.preview_set(self.collection, self.tracks)
        self.assertEqual(plan.crate_path.name, 'DJTOOL%%周五 Set.crate')
        self.assertEqual(decode_crate_paths(plan.payload), (str(self.b).lstrip('/'), str(self.a).lstrip('/')))
        self.assertFalse(self.music.joinpath('.djtool').exists())
        self.assertEqual(sorted(p.name for p in self.music.iterdir()), ['Second.mp3', '亚洲 夏天.mp3'])

    def test_sync_and_update_preserve_database_unrelated_crates_audio(self):
        first = self.adapter.sync_set(self.collection, self.tracks)
        path = Path(first['crate_path'])
        self.adapter.sync_set(self.collection, self.tracks[::-1])
        self.assertEqual(decode_crate_paths(path.read_bytes()), (str(self.a).lstrip('/'), str(self.b).lstrip('/')))
        self.assertEqual(self.database.read_bytes(), b'keep-database')
        self.assertEqual(self.unrelated.read_bytes(), b'keep-my-crate')
        self.assertEqual(self.a.read_bytes(), b'synthetic-audio-a')
        self.assertEqual(self.b.read_bytes(), b'synthetic-audio-b')
        self.assertFalse((self.music / '周五 Set').exists())
        self.assertFalse(first['native_validated'])

    def test_same_audio_can_be_referenced_by_independent_sets(self):
        self.adapter.sync_set(self.collection, self.tracks)
        other = {**self.collection, 'id': 'other', 'name': 'Afterparty'}
        second = self.adapter.sync_set(other, self.tracks[:1])
        self.assertEqual(len(list(self.crates.glob('DJTOOL*.crate'))), 2)
        self.assertEqual(decode_crate_paths(Path(second['crate_path']).read_bytes()), (str(self.b).lstrip('/'),))

    def test_rename_removes_only_own_old_crate(self):
        result = self.adapter.sync_set(self.collection, self.tracks)
        renamed = {**self.collection, 'name': 'New Name'}
        current = self.adapter.sync_set(renamed, self.tracks)
        self.assertFalse(Path(result['crate_path']).exists())
        self.assertTrue(Path(current['crate_path']).is_file())
        self.assertEqual(self.unrelated.read_bytes(), b'keep-my-crate')

    def test_existing_unmanaged_crate_is_not_taken_over(self):
        path = self.crates / 'DJTOOL%%周五 Set.crate'
        path.write_bytes(b'personal')
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)
        self.assertEqual(path.read_bytes(), b'personal')
        self.assertFalse(self.adapter.manifest_path.exists())

    def test_external_edit_and_delete_refuse_update(self):
        result = self.adapter.sync_set(self.collection, self.tracks)
        path = Path(result['crate_path'])
        path.write_bytes(b'external edit')
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)
        self.assertEqual(path.read_bytes(), b'external edit')
        path.unlink()
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)

    def test_restore_create_removes_owned_crate_and_manifest(self):
        result = self.adapter.sync_set(self.collection, self.tracks)
        self.assertTrue(self.adapter.restore_backup(Path(result['backup_path']).name))
        self.assertFalse(Path(result['crate_path']).exists())
        self.assertFalse(self.adapter.manifest_path.exists())
        self.assertTrue(self.a.is_file())
        self.assertTrue(self.unrelated.is_file())

    def test_restore_update_recovers_exact_bytes_and_order(self):
        first = self.adapter.sync_set(self.collection, self.tracks)
        before = Path(first['crate_path']).read_bytes()
        second = self.adapter.sync_set(self.collection, self.tracks[::-1])
        self.adapter.restore_backup(Path(second['backup_path']).name)
        self.assertEqual(Path(first['crate_path']).read_bytes(), before)
        with self.assertRaises(SeratoConflict):
            self.adapter.restore_backup(Path(second['backup_path']).name)

    def test_restore_refuses_external_edits_and_stale_backup(self):
        first = self.adapter.sync_set(self.collection, self.tracks)
        second = self.adapter.sync_set(self.collection, self.tracks[::-1])
        with self.assertRaises(SeratoConflict):
            self.adapter.restore_backup(Path(first['backup_path']).name)
        Path(second['crate_path']).write_bytes(b'changed')
        with self.assertRaises(SeratoConflict):
            self.adapter.restore_backup(Path(second['backup_path']).name)
        self.assertEqual(Path(second['crate_path']).read_bytes(), b'changed')

    def test_failed_manifest_commit_rolls_back_audio_independent_update(self):
        first = self.adapter.sync_set(self.collection, self.tracks)
        path = Path(first['crate_path'])
        before = path.read_bytes()
        manifest = self.adapter.manifest_path.read_bytes()
        original_write = serato._atomic_write
        def disk_error(target, data):
            if target == self.adapter.manifest_path and data != manifest:
                raise OSError('disk full')
            original_write(target, data)
        with patch('djtool.serato._atomic_write', side_effect=disk_error):
            with self.assertRaises(OSError):
                self.adapter.sync_set(self.collection, self.tracks[::-1])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.adapter.manifest_path.read_bytes(), manifest)
        self.assertFalse(list(self.crates.glob('.djtool-*')))
        self.assertFalse((self.adapter.state_dir / 'sync.lock').exists())

    def test_preflight_external_change_does_not_get_rolled_back(self):
        first = self.adapter.sync_set(self.collection, self.tracks)
        path = Path(first['crate_path'])
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_bytes(b'new external edit')
            return False
        self.adapter.process_checker = check
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks[::-1])
        self.assertEqual(path.read_bytes(), b'new external edit')

    def test_native_live_sync_is_guarded_before_any_writes(self):
        live = SeratoCrateAdapter(self.music, self.target, process_checker=lambda: False)
        with self.assertRaises(SeratoNotValidated):
            live.sync_set(self.collection, self.tracks)
        self.assertFalse((self.music / '.djtool').exists())
        forbidden = SeratoCrateAdapter(Path.home() / 'Documents/Serato', self.target, isolated=True,
                                       process_checker=lambda: False)
        with self.assertRaises(SeratoError):
            forbidden.sync_set(self.collection, self.tracks)

    def test_running_or_unknown_process_state_refuses_write(self):
        self.adapter.process_checker = lambda: True
        with self.assertRaises(SeratoError):
            self.adapter.sync_set(self.collection, self.tracks)
        with patch('djtool.serato.subprocess.run', side_effect=PermissionError('sandbox')):
            with self.assertRaises(SeratoError):
                serato.serato_is_running()
        self.assertFalse(self.adapter.manifest_path.exists())

    def test_process_guard_distinguishes_main_program_from_helpers(self):
        from types import SimpleNamespace
        helpers = ('/Applications/Serato DJ Pro.app/Contents/MacOS/crashpad_handler\n'
                   '/Applications/Serato DJ Pro.app/Contents/qt/QtWebEngineProcess\n')
        with patch('djtool.serato.subprocess.run', return_value=SimpleNamespace(stdout=helpers)):
            self.assertFalse(serato.serato_is_running())
        for main in ('/Applications/Serato DJ Pro.app/Contents/MacOS/Serato DJ Pro', 'Serato DJ Lite'):
            with patch('djtool.serato.subprocess.run', return_value=SimpleNamespace(stdout=helpers + main + '\n')):
                self.assertTrue(serato.serato_is_running())

    def test_serato_5_evidence_requires_its_own_schema_and_fingerprint(self):
        fingerprint, report, evidence = self.native_evidence_fixture()
        fingerprint['installed_version'] = '5.0.0'
        evidence.write_text(json.dumps(report))
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            with self.assertRaises(SeratoNotValidated): validate_native_evidence(evidence)
        for step in report['steps']:
            with sqlite3.connect(step['database']) as db: db.execute('PRAGMA user_version=203')
            step['database_sha256'] = hashlib.sha256(Path(step['database']).read_bytes()).hexdigest()
        evidence.write_text(json.dumps(report))
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            self.assertEqual(validate_native_evidence(evidence)['fingerprint']['installed_version'], '5.0.0')

    def test_bad_names_paths_duplicate_and_missing_tracks_are_rejected(self):
        for name in ['../escape', 'x%%y', 'bad\nname', 'x:', 'x.' , '']:
            with self.assertRaises(SeratoError):
                plan_set({**self.collection, 'name': name}, self.tracks, self.target)
        with self.assertRaises(SeratoError):
            self.adapter.preview_set(self.collection, [self.tracks[0], self.tracks[0]])
        self.b.unlink()
        with self.assertRaises(SeratoError):
            self.adapter.sync_set(self.collection, self.tracks)

    def test_corrupt_crate_and_manifest_refuse_parsing_or_write(self):
        for data in [b'x', b'otrk' + struct.pack('>I', 100) + b'x',
                     b'otrk' + struct.pack('>I', 8) + b'ptrk' + struct.pack('>I', 0)]:
            with self.assertRaises(SeratoError):
                decode_crate_paths(data)
        self.adapter.state_dir.mkdir(parents=True)
        self.adapter.manifest_path.write_text(json.dumps({'version': 1, 'crates': {'x': {
            'name': 'x', 'filename': '../bad.crate', 'sha256': '0'*64}}}))
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)

    def test_symlink_and_concurrent_lock_do_not_overwrite(self):
        path = self.crates / 'DJTOOL%%周五 Set.crate'
        path.symlink_to(self.unrelated)
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)
        path.unlink()
        (self.adapter.state_dir / 'sync.lock').mkdir()
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)
        self.assertEqual(self.unrelated.read_bytes(), b'keep-my-crate')

    def test_native_sqlite_content_verifier_checks_filename_members_and_order(self):
        # 这是 verifier 的合成 schema 测试，不是原生 Serato 兼容性证据。
        database = self.root / 'root.sqlite'
        plan = self.adapter.preview_set(self.collection, self.tracks)
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript('''PRAGMA user_version=202;
                CREATE TABLE container(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE dj_container_metadata(container_id INTEGER,filename TEXT);
                CREATE TABLE asset(id INTEGER PRIMARY KEY,portable_id TEXT);
                CREATE TABLE space_asset(id INTEGER PRIMARY KEY,asset_id INTEGER);
                CREATE TABLE container_asset(id INTEGER PRIMARY KEY,container_id INTEGER,
                    space_asset_id INTEGER,list_order INTEGER);
            ''')
            db.execute('INSERT INTO container VALUES(1,?)', (plan.name,))
            db.execute('INSERT INTO dj_container_metadata VALUES(1,?)', (plan.crate_path.name,))
            for index, reference in enumerate(plan.paths, 1):
                db.execute('INSERT INTO asset VALUES(?,?)', (index, reference))
                db.execute('INSERT INTO space_asset VALUES(?,?)', (index, index))
                db.execute('INSERT INTO container_asset VALUES(?,1,?,?)', (index,index,index))
        before = database.read_bytes()
        result = verify_native_set(plan, database)
        self.assertTrue(result['native_content_matches'])
        self.assertFalse(result['native_reopen_verified'])
        self.assertEqual(database.read_bytes(), before)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE container_asset SET list_order=3-list_order')
        with self.assertRaises(SeratoError):
            verify_native_set(plan, database)

    def test_case_only_rename_refuses_destructive_alias_and_empty_set_is_valid(self):
        collection = {**self.collection, 'name': 'Party'}
        first = self.adapter.sync_set(collection, [])
        self.assertEqual(decode_crate_paths(Path(first['crate_path']).read_bytes()), ())
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set({**collection, 'name': 'party'}, [])
        self.assertTrue(Path(first['crate_path']).exists())

    def test_activation_requires_all_native_phases_and_fingerprint_then_live_write(self):
        fingerprint, report, evidence = self.native_evidence_fixture()
        live = SeratoCrateAdapter(self.music, self.target, process_checker=lambda: False)
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            self.assertFalse(live.native_sync_status()['enabled'])
            live.activate_native_sync(evidence)
            self.assertTrue(live.native_sync_status()['enabled'])
            result = live.sync_set(self.collection, self.tracks)
            self.assertTrue(result['native_validated'])
            self.assertTrue(result['pending_serato_read'])
        self.assertEqual(self.database.read_bytes(), b'keep-database')

    def test_incomplete_native_evidence_does_not_enable_live_writes(self):
        fingerprint, report, evidence = self.native_evidence_fixture()
        report['steps'].pop()
        evidence.write_text(json.dumps(report), encoding='utf-8')
        live = SeratoCrateAdapter(self.music, self.target, process_checker=lambda: False)
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            with self.assertRaises(SeratoNotValidated):
                live.activate_native_sync(evidence)
        self.assertFalse(live.activation_path.exists())

    def test_native_evidence_snapshot_changes_or_wrong_order_refuse_activation(self):
        fingerprint, report, evidence = self.native_evidence_fixture()
        source = Path(report['steps'][0]['database'])
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('UPDATE container_asset SET list_order=3-list_order')
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            with self.assertRaises(SeratoNotValidated):
                validate_native_evidence(evidence)
            report['steps'][0]['database_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
            evidence.write_text(json.dumps(report), encoding='utf-8')
            with self.assertRaises(SeratoError):
                validate_native_evidence(evidence)

    def test_installation_change_or_copied_activation_record_invalidates_live_mode(self):
        fingerprint, report, evidence = self.native_evidence_fixture()
        live = SeratoCrateAdapter(self.music, self.target, process_checker=lambda: False)
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            live.activate_native_sync(evidence)
        with patch('djtool.serato.installation_fingerprint', return_value={**fingerprint, 'binary_sha256': 'b'*64}):
            self.assertFalse(live.native_sync_available())
            with self.assertRaises(SeratoNotValidated):
                live.sync_set(self.collection, self.tracks)
        record = json.loads(live.activation_path.read_text())
        record['music_root'] = '/some/other/library'
        live.activation_path.write_text(json.dumps(record))
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            with self.assertRaises(SeratoNotValidated):
                live.sync_set(self.collection, self.tracks)

    def test_native_rewrite_requires_explicit_semantic_and_database_confirmation(self):
        first = self.adapter.sync_set(self.collection, self.tracks)
        plan = self.adapter.preview_set(self.collection, self.tracks)
        native = self.root / 'native.sqlite'
        self.native_fixture(native, plan)
        path = Path(first['crate_path'])
        path.write_bytes(path.read_bytes() + serato._record('xtra', b'native display fields'))
        with self.assertRaises(SeratoConflict):
            self.adapter.sync_set(self.collection, self.tracks)
        result = self.adapter.adopt_native_rewrite(self.collection, self.tracks, native)
        self.assertTrue(result['adopted'])
        self.adapter.sync_set(self.collection, self.tracks[::-1])
        path.write_bytes(serato.encode_crate(plan.paths))
        with self.assertRaises(SeratoConflict):
            self.adapter.adopt_native_rewrite(self.collection, self.tracks[::-1], native)

    def test_live_empty_sync_refused_without_overwriting_previous_owned_crate(self):
        fingerprint, report, evidence = self.native_evidence_fixture()
        live = SeratoCrateAdapter(self.music, self.target, process_checker=lambda: False)
        with patch('djtool.serato.installation_fingerprint', return_value=fingerprint):
            live.activate_native_sync(evidence)
            result = live.sync_set(self.collection, self.tracks)
            path = Path(result['crate_path'])
            before = path.read_bytes()
            manifest = live.manifest_path.read_bytes()
            with self.assertRaisesRegex(SeratoError, '先加入歌曲'):
                live.sync_set(self.collection, [])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(live.manifest_path.read_bytes(), manifest)


if __name__ == '__main__':
    unittest.main()
