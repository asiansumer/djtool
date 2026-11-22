"""限定歌单之后的跨歌单去重：隔离库、真实合成 MP3、本地导入，不联网。"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from djtool.audio_tasks import ffmpeg_path
from djtool.library import Library
from djtool.online import OnlineError, download_track


def song(number, **changes):
    return {
        'provider': 'netease', 'remote_id': str(number),
        'url': f'https://music.163.com/song?id={number}',
        'title': '相同歌名', 'artist': '隔离测试', 'duration': 3,
        'acquisition_backend': 'site', **changes,
    }


class PlaylistRangeDedupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory(prefix='djtool-range-dedup-audio-')
        cls.sample = Path(cls.samples.name) / 'synthetic.mp3'
        subprocess.run([
            ffmpeg_path(), '-v', 'error', '-nostdin', '-f', 'lavfi',
            '-i', 'sine=duration=3', '-c:a', 'libmp3lame', str(cls.sample),
        ], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='djtool-range-dedup-library-')
        self.library = Library(Path(self.temporary.name) / 'library')
        # 任何测试漏掉假音源都会立刻失败，不能意外访问用户授权的线上服务。
        self.no_network = patch('requests.sessions.Session.request',
                                side_effect=AssertionError('隔离去重测试禁止联网'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.addCleanup(self.temporary.cleanup)

    def download_fixture(self, number):
        """只替换远程获取；时长、MD5、完整解码、标签与入库均走实际实现。"""
        info = song(number)
        client = Mock(backend='site')
        client.audio.return_value = {
            'metadata': info, 'song_id': info['remote_id'],
            'url': 'https://m1.music.126.net/isolated-synthetic.mp3',
            'size': self.sample.stat().st_size,
            'md5': hashlib.md5(self.sample.read_bytes()).hexdigest(),
            'br': 128000, 'source_label': '隔离测试音源',
        }
        client.lyrics.return_value = ''
        with patch('djtool.online.transfer', side_effect=lambda url, destination, *args, **kwargs:
                   shutil.copyfile(self.sample, destination)) as transfer:
            identifier, added = download_track(info, self.library, music_client=client)
        self.assertTrue(added)
        client.audio.assert_called_once()
        transfer.assert_called_once()
        return identifier

    def skip_without_network(self, info, library=None):
        client = Mock(backend='site')
        client.audio.side_effect = AssertionError('已入库 ID 不应再次解析音源')
        client.lyrics.side_effect = AssertionError('已入库 ID 不应再次读取歌词')
        with patch('djtool.online.transfer', side_effect=AssertionError('已入库 ID 不应再次下载')) as transfer:
            result = download_track(info, library or self.library, music_client=client)
        client.audio.assert_not_called()
        client.lyrics.assert_not_called()
        transfer.assert_not_called()
        return result

    def organized_snapshot(self, identifiers):
        with self.library.connection() as db:
            collections = [dict(row) for row in db.execute('SELECT * FROM collections ORDER BY id')]
            members = [dict(row) for row in db.execute(
                'SELECT * FROM members ORDER BY collection_id,position,track_id')]
        return {
            'tracks': [self.library.track(identifier) for identifier in identifiers],
            'collections': collections, 'members': members,
            'files': {identifier: Path(self.library.track(identifier)['path']).read_bytes()
                      for identifier in identifiers},
        }

    def test_other_playlist_reuses_boundary_downloads_and_preserves_organized_set(self):
        # 第一首和第 940 首经过实际本地导入；第 941 首只是已读取的待下载任务。
        # 这里只验全局 ID 关系，界面如何限定范围由 UI 范围回归另行验收。
        jobs = self.library.enqueue_many(song(number) for number in range(1, 1316))
        first = self.download_fixture(1)
        last_in_range = self.download_fixture(940)
        group = self.library.create_collection('已整理', 'group')
        dj_set = self.library.create_collection('保持顺序的 Set', 'set')
        for identifier in (last_in_range, first):
            self.library.add_member(group, identifier)
            self.library.add_member(dj_set, identifier)
            self.library.set_note(dj_set, identifier, '保留各首衔接备注 ' + identifier)
            self.library.update_track(identifier, {
                'reviewed': 1, 'title': '用户整理的标题 ' + identifier, 'note': '保留整理备注',
            })
        before = self.organized_snapshot((first, last_in_range))

        second_playlist = [song(940, title='另一个歌单的新标题'), song(941),
                           song(2000), song(1, artist='平台改过艺人')]
        second_jobs = self.library.enqueue_many(second_playlist)
        self.assertEqual([job['state'] for job in second_jobs], ['complete', 'ready', 'ready', 'complete'])
        self.assertEqual([job['remote_id'] for job in second_jobs], ['940', '941', '2000', '1'])
        self.assertEqual(second_jobs[0]['id'], jobs[939]['id'])
        self.assertEqual(second_jobs[1]['id'], jobs[940]['id'])
        self.assertEqual(second_jobs[3]['id'], jobs[0]['id'])
        self.assertEqual(self.skip_without_network(second_playlist[0]), (last_in_range, False))
        self.assertEqual(self.skip_without_network(second_playlist[3]), (first, False))
        self.assertEqual(self.organized_snapshot((first, last_in_range)), before)
        self.assertEqual(len(self.library.tracks()), 2)
        self.assertEqual(len(self.library.online_jobs()), 1316)
        # 范围外没有全局忽略标记；以后从其他歌单明确选择仍可下载。
        self.assertEqual(second_jobs[1]['state'], 'ready')

    def test_platform_mapping_skips_after_queue_history_is_cleared_and_library_reopens(self):
        identifier = self.download_fixture(940)
        dj_set = self.library.create_collection('已有 Set', 'set')
        self.library.add_member(dj_set, identifier)
        self.library.update_track(identifier, {'reviewed': 1, 'note': '历史清理后保留'})
        with self.library.connection() as db:
            old_job = db.execute('SELECT id FROM online_jobs WHERE remote_id=?', ('940',)).fetchone()['id']
            db.execute('DELETE FROM online_jobs WHERE id=?', (old_job,))
            # 排除专用标签恢复路径，确保独立 platform_tracks 才是本次复用依据。
            db.execute('UPDATE tracks SET tags_json=? WHERE id=?', ('{}', identifier))
            bindings = [tuple(row) for row in db.execute(
                'SELECT provider,remote_id,track_id FROM platform_tracks')]
        self.assertEqual(bindings, [('netease', '940', identifier)])
        before = self.organized_snapshot((identifier,))

        reopened = Library(self.library.root)
        job = reopened.enqueue_many([song(940, title='后续歌单里的不同标题')])[0]
        self.assertNotEqual(job['id'], old_job)
        self.assertEqual((job['state'], job['track_id']), ('complete', identifier))
        self.assertEqual(self.skip_without_network(song(940), reopened), (identifier, False))
        self.assertEqual(self.organized_snapshot((identifier,)), before)
        self.assertEqual(len(reopened.tracks()), 1)

    def test_missing_or_empty_file_is_a_restore_task_after_queue_history_cleared(self):
        identifier = self.download_fixture(940)
        dj_set = self.library.create_collection('不可丢失引用的 Set', 'set')
        self.library.add_member(dj_set, identifier)
        self.library.set_note(dj_set, identifier, '保留播放顺序和备注')
        self.library.update_track(identifier, {'reviewed': 1})
        original = self.library.track(identifier)
        path = Path(original['path'])
        content = path.read_bytes()
        set_before = self.library.tracks(dj_set)
        with self.library.connection() as db:
            db.execute('DELETE FROM online_jobs')

        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    path.unlink()
                else:
                    path.write_bytes(b'')
                reopened = Library(self.library.root)
                job = reopened.enqueue_many([song(940)])[0]
                self.assertEqual((job['state'], job['track_id']), ('missing_file', identifier))
                self.assertIn('恢复原文件', job['message'])
                with self.assertRaisesRegex(OnlineError, '丢失|缺失'):
                    self.skip_without_network(song(940), reopened)
                self.assertEqual(reopened.track(identifier), original)
                self.assertEqual(reopened.tracks(dj_set), set_before)
                self.assertEqual(len(reopened.tracks()), 1)
                path.write_bytes(content)
                self.assertEqual(reopened.reconcile_online_jobs(), {'matched': 1, 'missing': 0})
                self.assertEqual(self.skip_without_network(song(940), reopened), (identifier, False))


if __name__ == '__main__':
    unittest.main()
