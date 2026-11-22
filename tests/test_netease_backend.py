"""站点音源升级、旧权限队列恢复及匿名订阅验收；不联网、不读真实账号。"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from djtool.library import Library
from djtool.netease_backend import SETTINGS_VERSION, create_music_client, load_settings, save_settings


def song(identifier, **values):
    return {'provider':'netease', 'remote_id':str(identifier),
            'url':f'https://music.163.com/song?id={identifier}',
            'title':f'Song {identifier}', 'artist':'Fixture', 'duration':180, **values}


def completed_fixture(library, job_id, message='整理完成'):
    # 状态测试仍需一个真实存在的隔离播放路径，不能仅设置 complete 就冒充下载完成。
    path = library.root / f'{job_id}.mp3'
    path.write_bytes(b'isolated state fixture; decoding tested separately')
    with library.connection() as db:
        db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename) '
                   'VALUES(?,?,?,?,?,?,?,?)',
                   (job_id, job_id, 'Fixture', 'Fixture', 180, str(path), str(path), path.name))
    library.set_job_state(job_id, 'complete', message, job_id)
    return job_id


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='djtool-backend-')
        self.library = Library(Path(self.temporary.name)/'library')
        self.path = self.library.root/'.djtool/netease-settings.json'

    def tearDown(self):
        self.temporary.cleanup()

    def jobs(self):
        return {job['remote_id']:job for job in self.library.online_jobs()}

    def blocked(self, identifier=1, state='unavailable', **values):
        job = self.library.enqueue(song(identifier, availability_reason='旧官方音源不可用',
                                        openapi_scope='old-app', **values))
        self.library.set_job_state(job['id'], state, '旧结果')
        if state == 'complete':
            completed_fixture(self.library, job['id'], '旧结果')
        return self.jobs()[str(identifier)]

    def test_first_upgrade_resets_only_netease_permission_blocks_once(self):
        previous = {}
        for number, state in enumerate(('waiting_auth','unavailable','complete','ignored',
                                         'failed','cancelled','downloading','ready'), 1):
            previous[str(number)] = self.blocked(number, state)
        soundcloud = self.library.enqueue({**song(99), 'provider':'soundcloud'})
        self.library.set_job_state(soundcloud['id'], 'waiting_auth', '其他平台等待')
        collection = self.library.create_collection('Existing Set', 'set')
        with self.library.connection() as db:
            db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename) '
                       "VALUES('local','fixture','Local','Artist',3,'/fixture/local.mp3','/fixture/original.mp3','local.mp3')")
            db.execute("INSERT INTO local_sources(digest,track_id,path,metadata_json) "
                       "VALUES('local-digest','local','/fixture/original.mp3','{}')")
        self.library.add_member(collection, 'local')
        self.library.set_note(collection, 'local', 'Keep order and note')
        with self.library.connection() as db:
            local_before = [tuple(row) for row in db.execute('SELECT * FROM local_sources')]
            members_before = [tuple(row) for row in db.execute('SELECT * FROM members')]
        self.assertEqual(load_settings(self.library), {'backend':'site','quality':'exhigh'})
        after = self.jobs()
        for number in ('1','2'):
            self.assertEqual(after[number]['id'], previous[number]['id'])
            self.assertEqual(after[number]['state'], 'ready')
            self.assertEqual(after[number]['message'], '')
            self.assertEqual(json.loads(after[number]['metadata_json'])['availability_reason'], '')
        for number in ('3','4','5','6','7','8'):
            self.assertEqual(after[number], previous[number])
        self.assertEqual(after['99']['state'], 'waiting_auth')
        with self.library.connection() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM local_sources')], local_before)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM members')], members_before)
        self.assertEqual(self.library.collections('set')[0]['id'], collection)
        self.library.set_job_state(after['2']['id'], 'unavailable', '新音源也不可用')
        load_settings(Library(self.library.root))
        self.assertEqual(self.jobs()['2']['state'], 'unavailable')
        self.assertEqual(json.loads(self.path.read_text()),
                         {'format_version':SETTINGS_VERSION,'backend':'site','quality':'exhigh'})

    def test_legacy_official_preferences_migrate_once_to_site_without_account_read(self):
        self.path.write_text(json.dumps({'backend':'official','quality':'hires'}), encoding='utf-8')
        job = self.blocked(1, 'waiting_auth')
        site = Mock(return_value=object())
        # keyring 不再是依赖；把它在 sys.modules 里置空，任何 import keyring 都会直接报错。
        with patch.dict(sys.modules, {'djtool.netease_site':SimpleNamespace(NeteaseSiteClient=site), 'keyring':None}):
            self.assertEqual(load_settings(self.library), {'backend':'site','quality':'hires'})
            client = create_music_client(self.library)
            self.assertIs(client, site.return_value)
            site.assert_called_once_with(quality='hires')
        self.assertEqual(self.jobs()['1']['id'], job['id'])
        self.assertEqual(self.jobs()['1']['state'], 'ready')
        self.assertEqual(json.loads(self.path.read_text()),
                         {'format_version':SETTINGS_VERSION,'backend':'site','quality':'hires'})
        self.library.set_job_state(job['id'], 'unavailable', '当前音源不可用')
        self.assertEqual(load_settings(self.library)['backend'], 'site')
        self.assertEqual(self.jobs()['1']['state'], 'unavailable')

    def test_legacy_native_cookie_preferences_migrate_once_and_keep_quality(self):
        self.path.write_text(json.dumps({'backend':'local','quality':'lossless'}), encoding='utf-8')
        self.blocked(1, 'waiting_auth')
        self.assertEqual(load_settings(self.library), {'backend':'site','quality':'lossless'})
        self.assertEqual(self.jobs()['1']['state'], 'ready')
        self.library.set_job_state(self.jobs()['1']['id'], 'waiting_auth', '旧任务兼容状态')
        load_settings(self.library)
        self.assertEqual(self.jobs()['1']['state'], 'waiting_auth')

    def test_v2_native_preferences_migrate_once_to_site_and_keep_quality(self):
        self.path.write_text(json.dumps({'format_version':2, 'backend':'local',
                                        'quality':'lossless'}), encoding='utf-8')
        previous = self.blocked(1, 'unavailable', acquisition_backend='local')
        self.assertEqual(load_settings(self.library), {'backend':'site','quality':'lossless'})
        self.assertEqual(self.jobs()['1']['id'], previous['id'])
        self.assertEqual(self.jobs()['1']['state'], 'ready')
        self.assertEqual(json.loads(self.path.read_text()),
                         {'format_version':SETTINGS_VERSION,'backend':'site','quality':'lossless'})
        self.library.set_job_state(previous['id'], 'unavailable', '站点也暂时不可用')
        load_settings(Library(self.library.root))
        self.assertEqual(self.jobs()['1']['state'], 'unavailable')

    def test_v2_official_preferences_also_migrate_to_site(self):
        self.path.write_text(json.dumps({'format_version':2, 'backend':'official',
                                        'quality':'exhigh'}), encoding='utf-8')
        self.assertEqual(load_settings(self.library), {'backend':'site','quality':'exhigh'})

    def test_removed_backends_in_a_current_file_are_upgraded_to_site_once(self):
        # 以前可以在设置里选“官方账号”或“本机客户端”；这两种方式移除后，
        # 留下的设置升级一次：音质保留，之前卡在权限上的任务放回待下载。
        for backend in ('local', 'official'):
            with self.subTest(backend=backend):
                self.path.write_text(json.dumps({'format_version':SETTINGS_VERSION, 'backend':backend,
                                                'quality':'hires'}), encoding='utf-8')
                self.blocked(1, 'waiting_auth')
                self.assertEqual(load_settings(self.library), {'backend':'site','quality':'hires'})
                self.assertEqual(self.jobs()['1']['state'], 'ready')
                self.blocked(1, 'waiting_auth')
                load_settings(self.library)
                self.assertEqual(self.jobs()['1']['state'], 'waiting_auth')
                with self.assertRaises(ValueError):
                    save_settings(self.library, backend, 'hires')

    def test_future_format_is_not_overwritten_and_queue_is_preserved(self):
        self.path.write_text(json.dumps({'format_version':SETTINGS_VERSION+1,
                                        'backend':'official','quality':'hires'}), encoding='utf-8')
        job = self.blocked(1, 'waiting_auth')
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            load_settings(self.library)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.jobs()['1'], job)

    def test_legacy_migration_queue_failure_restores_exact_old_preferences(self):
        for version, backend in ((1, 'official'), (2, 'local')):
            with self.subTest(version=version):
                self.path.write_text(json.dumps({'format_version':version, 'backend':backend,
                                                'quality':'hires'}), encoding='utf-8')
                job = self.blocked(1, 'waiting_auth')
                original = self.path.read_bytes()
                with patch.object(self.library, 'reset_netease_backend_jobs', side_effect=sqlite3.OperationalError('locked')):
                    with self.assertRaises(sqlite3.OperationalError):
                        load_settings(self.library)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.jobs()['1'], job)

    def test_quality_change_does_not_reset_permission_blocks(self):
        load_settings(self.library)
        self.blocked()
        self.assertEqual(save_settings(self.library, 'site', 'lossless'), {'backend':'site','quality':'lossless'})
        self.assertEqual(self.jobs()['1']['state'], 'unavailable')

    def test_new_cookie_reset_does_not_retry_other_failures(self):
        self.blocked(1, 'waiting_auth')
        self.blocked(2, 'unavailable')
        self.blocked(3, 'failed')
        self.blocked(4, 'ignored')
        self.assertEqual(self.library.reset_netease_pending(), 2)
        self.assertEqual({key:value['state'] for key,value in self.jobs().items()},
                         {'1':'ready','2':'ready','3':'failed','4':'ignored'})

    def test_invalid_preferences_leave_saved_preferences_and_queue_unchanged(self):
        load_settings(self.library)
        job = self.blocked()
        original = self.path.read_bytes()
        for backend, quality in [('remote','exhigh'),('local','unknown')]:
            with self.assertRaises(ValueError):
                save_settings(self.library, backend, quality)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.jobs()['1'], job)

    def test_malformed_preferences_do_not_silently_reset_queue_and_can_be_saved_again(self):
        job = self.blocked()
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_settings(self.library)
        self.assertEqual(self.jobs()['1'], job)
        save_settings(self.library, 'site', 'exhigh')
        self.assertEqual(load_settings(self.library), {'backend':'site','quality':'exhigh'})
        self.assertEqual(self.jobs()['1']['state'], 'ready')

    def test_atomic_settings_write_failure_preserves_preferences_and_queue(self):
        save_settings(self.library, 'site', 'exhigh')
        job = self.blocked()
        original = self.path.read_bytes()
        with patch('djtool.netease_backend.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                save_settings(self.library, 'site', 'hires')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.jobs()['1'], job)
        self.assertEqual(list(self.path.parent.glob('.netease-settings-*')), [])

    def test_queue_transaction_failure_restores_previous_settings(self):
        # 损坏的设置重新保存时会重置任务；任务事务失败就把原文件原样放回去。
        self.path.write_text('{broken', encoding='utf-8')
        original = self.path.read_bytes()
        with patch.object(self.library, 'reset_netease_backend_jobs', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                save_settings(self.library, 'site', 'hires')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('.netease-settings-*')), [])

    def test_explicit_native_reparse_restores_old_blocked_job_without_duplicate(self):
        previous = self.blocked()
        new = self.library.enqueue(song(1, title='Refreshed', acquisition_backend='local'))
        self.assertEqual(new['id'], previous['id'])
        self.assertEqual(new['state'], 'ready')
        self.assertEqual(json.loads(new['metadata_json'])['availability_reason'], '')
        self.assertEqual(len(self.library.online_jobs()), 1)
        self.library.set_job_state(new['id'], 'waiting_auth')
        self.assertEqual(self.library.enqueue(song(1, acquisition_backend='local'))['state'], 'waiting_auth')

    def test_site_reparse_recovers_old_native_block_without_duplicate(self):
        for identifier, state in ((1, 'waiting_auth'), (2, 'unavailable')):
            with self.subTest(state=state):
                previous = self.blocked(identifier, state, acquisition_backend='local')
                current = self.library.enqueue(song(identifier, title='Site details', acquisition_backend='site'))
                self.assertEqual(current['id'], previous['id'])
                self.assertEqual(current['state'], 'ready')
                metadata = json.loads(current['metadata_json'])
                self.assertEqual(metadata['acquisition_backend'], 'site')
                self.assertEqual(metadata['availability_reason'], '')
        self.assertEqual(len(self.library.online_jobs()), 2)

    def test_site_reparse_preserves_completed_and_ignored_jobs(self):
        for identifier, state in ((1, 'complete'), (2, 'ignored')):
            previous = self.blocked(identifier, state, acquisition_backend='local')
            current = self.library.enqueue(song(identifier, title='Site details', acquisition_backend='site'))
            self.assertEqual(current, previous)




if __name__ == '__main__':
    unittest.main()
