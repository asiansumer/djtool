"""本机开源适配器贯通下载/转码/标签/入库；假网络配真实解码器，不访问账号。"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from djtool.library import Library
from djtool.netease_common import safe_media_url
from djtool.online import OnlineError, download_track, transfer


class LocalDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory()
        cls.audio = {}
        for ext, codec in [('mp3', 'libmp3lame'), ('flac', 'flac')]:
            path = Path(cls.samples.name) / ('silence.' + ext)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                            '-t', '6', '-c:a', codec, str(path)], check=True)
            cls.audio[ext] = path

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temporary.name) / 'library')
        self.info = {'provider': 'netease', 'remote_id': '123', 'title': 'Older title', 'artist': 'Old artist',
                     'url': 'https://music.163.com/song?id=123', 'duration': 6}
        self.song = {'id': 123, 'name': 'Exact Remix', 'dt': 6000, 'ar': [{'name': 'Artist'}],
                     'al': {'name': 'Album', 'picUrl': 'https://p1.music.126.net/cover.jpg'}, 'no': 2}

    def tearDown(self):
        self.temporary.cleanup()

    def offline_download_client(self, info=None):
        """音源解析是假网络，复制现有静音样本后仍走真实时长与解码校验。"""
        info = dict(info or self.info)
        sample = self.audio['mp3']
        client = Mock(backend='site')
        client.audio.return_value = {
            'url': 'https://m1.music.126.net/isolated-silence.mp3',
            'size': sample.stat().st_size,
            'md5': hashlib.md5(sample.read_bytes()).hexdigest(),
            'br': 128000, 'song_id': info['remote_id'], 'metadata': info,
            'source_label': '隔离测试音源',
        }
        client.lyrics.return_value = '[00:00]隔离测试歌词'
        return client

    def download_fixture(self, info=None):
        info = dict(info or self.info)
        client = self.offline_download_client(info)
        with patch('djtool.online.transfer', side_effect=lambda url, destination, *args, **kwargs:
                   shutil.copyfile(self.audio['mp3'], destination)) as fetch:
            identifier, added = download_track(info, self.library, music_client=client)
        self.assertTrue(added)
        fetch.assert_called_once()
        client.audio.assert_called_once()
        return identifier, client

    def test_new_download_enters_inbox_without_collection_memberships(self):
        identifier, _ = self.download_fixture()
        track = self.library.track(identifier)
        self.assertEqual(track['reviewed'], 0)
        self.assertEqual(self.library.memberships(identifier), set())
        self.assertTrue(Path(track['path']).is_file())
        self.assertAlmostEqual(track['duration'], 6, delta=.2)
        self.assertEqual(self.library.source_for_track(identifier)['state'], 'complete')

    def test_existing_platform_id_with_new_title_skips_audio_and_preserves_organization(self):
        identifier, client = self.download_fixture()
        group = self.library.create_collection('已整理分类', 'group')
        dj_set = self.library.create_collection('现有 Set', 'set')
        self.library.add_member(group, identifier)
        self.library.add_member(dj_set, identifier)
        self.library.set_note(dj_set, identifier, '保留现有 Set 备注')
        self.library.update_track(identifier, {'reviewed': 1, 'title': '我的编辑标题', 'note': '已整理备注'})
        before = self.library.track(identifier)
        set_before = self.library.tracks(dj_set)
        client.reset_mock()
        changed = {**self.info, 'title': '平台后来改过标题', 'artist': '平台后来改过艺人'}
        with patch('djtool.online.transfer') as fetch:
            self.assertEqual(download_track(changed, self.library, music_client=client), (identifier, False))
        client.audio.assert_not_called()
        client.lyrics.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(self.library.track(identifier), before)
        self.assertEqual(self.library.memberships(identifier), {group, dj_set})
        self.assertEqual(self.library.tracks(dj_set), set_before)
        self.assertEqual(len(self.library.tracks()), 1)
        self.assertEqual(len(self.library.online_jobs()), 1)


    def test_missing_linked_playback_file_blocks_audio_requests_and_keeps_track_record(self):
        identifier, client = self.download_fixture()
        group = self.library.create_collection('已有分类', 'group')
        self.library.add_member(group, identifier)
        self.library.update_track(identifier, {'reviewed': 1})
        before = self.library.track(identifier)
        Path(before['path']).unlink()  # 仅删除本测试的隔离静音副本。
        job = self.library.enqueue(self.info)
        self.assertEqual((job['state'], job['track_id']), ('missing_file', identifier))
        client.reset_mock()
        with patch('djtool.online.transfer') as fetch:
            with self.assertRaisesRegex(OnlineError, '丢失|缺失|不存在'):
                download_track(self.info, self.library, music_client=client)
        client.audio.assert_not_called()
        client.lyrics.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(self.library.track(identifier), before)
        self.assertEqual(self.library.memberships(identifier), {group})
        self.assertEqual(self.library.source_for_track(identifier)['state'], 'missing_file')
        self.assertEqual(len(self.library.tracks()), 1)
        self.assertEqual(list(self.library.audio_dir.iterdir()), [])



    def test_cdn_redirect_cannot_fetch_another_host(self):
        first = Mock(status_code=302, headers={'Location': 'https://other.example/private'})
        destination = Path(self.temporary.name) / 'download'
        with patch('djtool.online.requests.get', return_value=first) as get:
            with self.assertRaisesRegex(Exception, '地址格式异常'):
                transfer('https://m1.music.126.net/original', destination, lambda *a: None,
                         lambda: False, url_validator=safe_media_url)
        self.assertEqual(get.call_count, 1)
        first.close.assert_called_once()
        self.assertFalse(destination.exists())
