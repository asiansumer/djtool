"""离线覆盖元数据迁移、队列去重、试听拒绝和取消清理。联网证据另见 qa_online。"""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from mutagen.mp3 import MP3
from mutagen.id3 import TPE1, TPE2, TCOM, TDRC, TRCK, TPOS, TSRC, COMM, USLT, TXXX
from djtool.library import Library, SCHEMA
from djtool.online import (Cancelled, OnlineError, download_track, normalize_link,
                    validate_duration)


class OnlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory()
        cls.sample = Path(cls.samples.name)/'track.mp3'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=3',
                        '-c:a','libmp3lame',str(cls.sample)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = Library(self.root/'library')
        self.info = {'provider':'netease','remote_id':'123','url':'https://music.163.com/song?id=123',
                     'title':'Original','artist':'Artist','duration':3}

    def tearDown(self):
        self.temporary.cleanup()

    def test_metadata_multivalue_and_custom_tags_preserved(self):
        path = self.root/'tags.mp3'
        shutil.copy2(self.sample,path)
        audio = MP3(path)
        for frame in [TPE1(encoding=3,text=['A','B']),TPE2(encoding=3,text='Album artist'),
                      TCOM(encoding=3,text='Composer'),TDRC(encoding=3,text='2025-02-01'),
                      TRCK(encoding=3,text='3/12'),TPOS(encoding=3,text='1/2'),
                      TSRC(encoding=3,text='TEST123'),COMM(encoding=3,lang='eng',desc='',text='Original comment'),
                      USLT(encoding=3,lang='eng',desc='',text='Original lyric'),
                      TXXX(encoding=3,desc='replaygain_track_gain',text='-5 dB')]:
            audio.tags.add(frame)
        audio.save()
        original = path.read_bytes()
        identifier,_ = self.library.import_file(path)
        track = self.library.track(identifier)
        self.assertEqual(track['artist'],'A / B')
        self.assertEqual(track['album_artist'],'Album artist')
        self.assertEqual(track['track_number'],'3/12')
        self.assertEqual(track['disc_number'],'1/2')
        self.assertEqual(track['lyrics'],'Original lyric')
        self.assertEqual(track['composer'],'Composer')
        self.assertEqual(track['release_date'],'2025-02-01')
        self.assertGreater(track['bitrate'],0)
        self.assertIn('TXXX:replaygain_track_gain',json.loads(track['tags_json']))
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(Path(track['path']).read_bytes(),original)
        self.library.update_track(identifier, {'title':'Manually edited','bpm_manual':90})
        refreshed = self.library.refresh_metadata(identifier)
        self.assertEqual(refreshed['title'],'Manually edited')
        self.assertEqual(refreshed['bpm_manual'],90)

    def test_v1_migration_retains_collections(self):
        root = self.root/'old';(root/'.djtool').mkdir(parents=True)
        with sqlite3.connect(root/'.djtool/library.sqlite3') as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO collections(id,name,kind) VALUES('old','My Set','set')")
            db.execute('PRAGMA user_version=1')
        migrated = Library(root)
        self.assertEqual(migrated.collections()[0]['name'],'My Set')
        self.assertEqual(len(list((root/'.djtool/backups').glob('*.sqlite3'))),1)
        with migrated.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)
            self.assertIn('tags_json',{row[1] for row in db.execute('PRAGMA table_info(tracks)')})
        Library(root) # 重复打开不会重复迁移或删除数据。

    def test_link_normalization_and_invalid_targets(self):
        self.assertEqual(normalize_link('分享 https://music.163.com/#/playlist?id=123&x=y')[3],'123')
        self.assertEqual(normalize_link('https://soundcloud.com/a/b?si=tracking')[1],'https://soundcloud.com/a/b')
        for bad in ['https://evil.com/a/b','https://soundcloud.com.evil.com/a/b','file:///x',
                    'https://music.163.com/song?id=no','https://soundcloud.com/you/likes',
                    'https://soundcloud.com/discover','https://soundcloud.com/user/followers',
                    'https://u:p@soundcloud.com/a/b']:
            with self.assertRaises(OnlineError):normalize_link(bad)

    def test_user_playlist_tracking_link_is_normalized_without_network(self):
        # 分享参数只用于网易云分享追踪；解析接口应只收到验证过的歌单 ID。
        link = ('https://music.163.com/playlist?id=360000118&uct2='
                'U2FsdGVkX19x6oywjwxYW0Hj1P9dfH0MKewqiQEM2J8=')
        expected = ('netease', 'https://music.163.com/playlist?id=360000118',
                    'playlist', '360000118')
        with patch('djtool.online.requests.get') as network:
            self.assertEqual(normalize_link(link), expected)
            self.assertEqual(normalize_link('分享歌单 ' + link + '。'), expected)
            self.assertEqual(normalize_link(link.replace('/playlist?', '/#/playlist?')), expected)
            network.assert_not_called()

    def test_persistent_queue_identity_and_cancel_cleanliness(self):
        job = self.library.enqueue(self.info)
        same = self.library.enqueue({**self.info,'title':'Changed title'})
        self.assertEqual(job['id'],same['id'])
        self.assertEqual(len(Library(self.library.root).online_jobs()),1)
        with self.assertRaises(Cancelled):
            download_track(self.info,self.library,cancel=lambda:True)
        self.assertEqual(self.library.online_jobs()[0]['state'],'cancelled')
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list((self.library.root/'.djtool/downloads').iterdir()),[])

    def test_audio_with_the_wrong_duration_is_rejected(self):
        with self.assertRaisesRegex(OnlineError,'时长不符'):
            validate_duration(self.sample,180)
        self.assertGreater(validate_duration(self.sample,3),2)

    def test_official_denial_never_uses_legacy_download_or_imports_audio(self):
        from djtool.netease_common import MusicUnavailable
        with patch('djtool.netease_backend.create_music_client') as client:
            client.return_value.audio.side_effect = MusicUnavailable('开放平台不提供此曲音源')
            with self.assertRaisesRegex(MusicUnavailable, '不提供'):
                download_track(self.info, self.library)
        self.assertEqual(self.library.online_jobs()[0]['state'], 'unavailable')
        self.assertEqual(self.library.tracks(), [])
        self.assertEqual(list((self.library.root/'.djtool/downloads').iterdir()), [])

    def test_failure_persists_without_import(self):
        with patch('djtool.netease_backend.create_music_client') as client:
            client.return_value.audio.side_effect=OnlineError('没有权限')
            with self.assertRaisesRegex(OnlineError,'权限'):
                download_track(self.info,self.library)
        self.assertEqual(self.library.online_jobs()[0]['state'],'failed')
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list((self.library.root/'.djtool/downloads').iterdir()),[])

    def test_official_download_imports_full_audio_and_detects_cover_content(self):
        import base64, hashlib
        from unittest.mock import Mock
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')
        client = Mock()
        client.audio.return_value = {'url':'https://m1.music.126.net/file?signature=temporary-secret',
            'size':self.sample.stat().st_size, 'md5':hashlib.md5(self.sample.read_bytes()).hexdigest(),
            'br':128000,'song_id':'A'*32,'metadata':{**self.info,'thumbnail':'https://p1.music.126.net/cover'}}
        client.lyrics.return_value = '[00:00]测试歌词'
        def transfer(url,destination,*args,**kwargs):
            if 'cover' in url:destination.write_bytes(image)
            else:shutil.copyfile(self.sample,destination)
            return 'application/octet-stream'
        with patch('djtool.online.transfer',side_effect=transfer):
            identifier, added = download_track(self.info,self.library,music_client=client)
            self.assertTrue(added)
            self.assertEqual(download_track(self.info,self.library,music_client=client),(identifier,False))
        track = self.library.track(identifier)
        self.assertTrue(Path(track['cover_path']).is_file())
        self.assertIn('测试歌词',track['lyrics'])
        self.assertNotIn('temporary-secret',self.library.online_jobs()[0]['metadata_json'])
        client.audio.assert_called_once()

    def test_official_checksum_failure_preserves_empty_library(self):
        from unittest.mock import Mock
        client=Mock()
        client.audio.return_value={'url':'https://m1.music.126.net/test','size':self.sample.stat().st_size,
                                  'md5':'0'*32,'metadata':self.info}
        with patch('djtool.online.transfer',side_effect=lambda url,path,*args,**kwargs:shutil.copyfile(self.sample,path)):
            with self.assertRaisesRegex(OnlineError,'校验'):
                download_track(self.info,self.library,music_client=client)
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list((self.library.root/'.djtool/downloads').iterdir()),[])

    def test_rounded_site_duration_does_not_allow_checksum_valid_short_audio(self):
        import hashlib
        from unittest.mock import Mock
        from djtool.netease_site import _duration
        # 站点的 1:60 可换算为 120 秒，但校验正确的试听长度文件仍须拒绝入库。
        info = {**self.info, 'duration': _duration('1:60')}
        self.assertEqual(info['duration'], 120)
        client = Mock(backend='site')
        client.audio.return_value = {
            'url':'https://m1.music.126.net/test',
            'size':self.sample.stat().st_size,
            'md5':hashlib.md5(self.sample.read_bytes()).hexdigest(),
            'song_id':'123', 'metadata':info}
        client.lyrics.return_value = ''
        with patch('djtool.online.transfer', side_effect=lambda url,path,*args,**kwargs:
                   shutil.copyfile(self.sample,path)):
            with self.assertRaisesRegex(OnlineError, '时长不符'):
                download_track(info, self.library, music_client=client)
        self.assertEqual(self.library.tracks(), [])
        self.assertEqual(self.library.online_jobs()[0]['state'], 'failed')
        self.assertEqual(list((self.library.root/'.djtool/downloads').iterdir()), [])

    def test_site_empty_release_date_clears_old_queue_date_before_audio_import(self):
        import hashlib
        from unittest.mock import Mock
        # 旧任务误把站点响应生成时间当发行日期；详情留空时仅站点路径清除该值。
        # 本地/官方适配仍保留既有合并规则，不能因站点修复丢掉其可信日期。
        old_date = '2026-10-04'
        for backend, expected in (('site', ''), ('local', old_date), ('official', old_date)):
            with self.subTest(backend=backend):
                library = Library(self.root / backend)
                info = {**self.info, 'release_date': old_date}
                old_job = library.enqueue(info)
                self.assertEqual(json.loads(old_job['metadata_json'])['release_date'], old_date)
                client = Mock(backend=backend)
                client.audio.return_value = {
                    'url':'https://m1.music.126.net/test',
                    'size':self.sample.stat().st_size,
                    'md5':hashlib.md5(self.sample.read_bytes()).hexdigest(),
                    'song_id':'123', 'metadata':{**self.info, 'release_date':''}}
                client.lyrics.return_value = ''
                with patch('djtool.online.transfer', side_effect=lambda url,path,*args,**kwargs:
                           shutil.copyfile(self.sample,path)):
                    identifier, added = download_track(info, library, music_client=client)
                self.assertTrue(added)
                self.assertEqual(library.track(identifier)['release_date'], expected)
                jobs = library.online_jobs()
                self.assertEqual(len(jobs), 1)
                self.assertEqual((jobs[0]['id'], jobs[0]['state'], jobs[0]['track_id']),
                                 (old_job['id'], 'complete', identifier))
                self.assertEqual(json.loads(jobs[0]['metadata_json'])['release_date'], expected)
                if backend == 'site':
                    self.assertEqual(MP3(library.track(identifier)['path']).tags.getall('TDRC'), [])

    def test_reparse_updates_old_job_resources_and_availability_without_duplicate(self):
        original=self.library.enqueue(self.info)
        blocked={**self.info,'openapi_scope':'test','openapi_id':'A'*32,'availability_reason':'平台不可用'}
        unavailable=self.library.enqueue(blocked)
        self.assertEqual(unavailable['id'],original['id'])
        self.assertEqual(unavailable['state'],'unavailable')
        available=self.library.enqueue({**blocked,'availability_reason':''})
        self.assertEqual(available['state'],'ready')
        self.assertEqual(json.loads(available['metadata_json'])['availability_reason'],'')
        self.assertEqual(len(self.library.online_jobs()),1)


if __name__=='__main__':unittest.main()
