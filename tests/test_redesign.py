"""新界面与统一收歌队列；隔离曲库，不联网、不播放、不碰真实 Serato。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from djtool.audio_tasks import ffmpeg_path
from djtool.intake_queue import Batch, IntakeQueue
from djtool.ui import theme
from djtool.ui.library_view import TrackTable
from djtool.ui.main_window import MainWindow
from djtool.ui.widgets import format_added, format_bpm
from djtool.library import Library
from test_bulk_set import make_tracks


def wait_until(app, condition, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(.02)
    return False


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply(cls.app, 'dark')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.temp.cleanup()


class TableTests(Base):
    def setUp(self):
        super().setUp()
        make_tracks(self.library, 6)
        self.library.replace_serato_bpms(
            {str(self.library.audio_dir / 'track-0001.mp3'): 128.04, str(self.library.audio_dir / 'track-0003.mp3'): 90},
            '/tmp/root.sqlite', keys={str(self.library.audio_dir / 'track-0001.mp3'): 'Am'})
        self.table = TrackTable()
        self.table.set_tracks(self.library.tracks())

    def test_serato_bpm_and_key_are_shown_and_missing_values_sort_last_both_ways(self):
        track = self.library.track('track-0001')
        self.assertEqual((format_bpm(track), track['key_serato']), ('128', 'Am'))
        self.assertEqual(format_bpm(self.library.track('track-0000')), '—')
        self.table.sortByColumn(2, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.table.visible_ids()[:2], ['track-0003', 'track-0001'])
        self.table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.table.visible_ids()[:2], ['track-0001', 'track-0003'])

    def test_search_matches_every_word_and_refresh_keeps_selection(self):
        self.table.proxy.configure(text='电子 3')
        self.assertEqual(self.table.visible_ids(), ['track-0003'])
        self.table.proxy.configure(text='')
        self.table.select_ids({'track-0002', 'track-0004'})
        self.library.update_track('track-0002', {'title': '改过的名字'})
        self.table.set_tracks(self.library.tracks())
        self.assertEqual(set(self.table.selected_ids()), {'track-0002', 'track-0004'})
        self.assertIn('改过的名字', [t['title'] for t in self.table.selected_tracks()])

    def test_missing_files_are_found_once_when_loading_not_while_painting(self):
        (self.library.audio_dir / 'track-0005.mp3').unlink()
        self.table.set_tracks(self.library.tracks())
        self.assertEqual(self.table.source_model().missing, {'track-0005'})
        with patch('os.path.isfile', side_effect=AssertionError('绘制时不应访问磁盘')):
            self.table.resize(900, 400)
            self.table.grab()

    def test_added_date_is_relative(self):
        self.assertEqual(format_added('not a date'), '')
        self.assertIn(format_added(self.library.track('track-0000')['created_at']), ('今天', '昨天'))


class CoverTests(Base):
    def test_covers_load_in_the_background_and_leave_a_thumbnail(self):
        from PySide6.QtGui import QColor, QImage
        from djtool.ui.covers import CoverLoader
        source = Path(self.temp.name) / 'cover.png'
        image = QImage(900, 600, QImage.Format.Format_RGB32)
        image.fill(QColor('#cc3344'))
        image.save(str(source))
        covers = CoverLoader()
        covers.set_library(self.library.root)
        self.assertIsNone(covers.large_pixmap(str(source)))         # 第一次还没准备好，不阻塞
        self.assertTrue(wait_until(self.app, lambda: covers.large_pixmap(str(source)) is not None))
        pixmap = covers.large_pixmap(str(source))
        self.assertEqual((pixmap.width(), pixmap.height()), (360, 360))
        self.assertEqual(covers.small_pixmap(str(source), 28).width(), 56)
        self.assertEqual(len(list((self.library.root / '.djtool' / 'thumbs').glob('*.jpg'))), 1)
        self.assertIsNone(covers.large_pixmap(str(Path(self.temp.name) / 'missing.png')))
        self.assertTrue(wait_until(self.app, lambda: not covers.pending))
        self.assertIn(str(Path(self.temp.name) / 'missing.png'), covers.failed)
        covers.wait()


class QueueTests(Base):
    def make_mp3(self, name):
        path = Path(self.temp.name) / name
        subprocess.run([ffmpeg_path(), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'anullsrc=r=22050:cl=mono',
                        '-t', '2', '-c:a', 'libmp3lame', str(path)], check=True)
        return path

    def test_dropped_files_are_imported_through_the_queue(self):
        first, second = self.make_mp3('a.mp3'), self.make_mp3('b.mp3')
        second.write_bytes(first.read_bytes())   # 内容相同的文件按整文件去重
        broken = Path(self.temp.name) / 'broken.mp3'
        broken.write_bytes(b'not audio')
        queue = IntakeQueue(self.library)
        changes = []
        queue.library_changed.connect(lambda: changes.append(1))
        batch = queue.submit('\n'.join(map(str, (first, second, broken))))
        self.assertEqual(batch.kind, 'files')
        self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        self.assertEqual((batch.state, batch.added, batch.existing, len(batch.errors)), ('done', 1, 1, 1))
        self.assertEqual(len(self.library.tracks()), 1)
        self.assertTrue(changes and not queue.busy())

    def test_unrecognised_text_is_not_queued(self):
        queue = IntakeQueue(self.library)
        self.assertIsNone(queue.submit('随便写的一句话'))
        self.assertIsNone(queue.submit('   '))
        self.assertEqual(queue.batches, [])

    def listing(self, count=5):
        return '夏天的歌', [{'provider': 'netease', 'remote_id': str(100 + index), 'url': f'https://music.163.com/song?id={100 + index}',
                          'title': f'歌 {index}', 'artist': '艺人', 'duration': 200} for index in range(count)]

    def test_playlist_link_is_read_then_waits_for_the_user_and_remembers_range(self):
        queue = IntakeQueue(self.library)
        with patch('djtool.online.normalize_link', return_value=('netease', 'https://music.163.com/playlist?id=42', 'playlist', '42')), \
             patch('djtool.online.fetch_listing', return_value=self.listing()):
            batch = queue.submit('看看这个 https://music.163.com/playlist?id=42')
            self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        self.assertEqual((batch.state, batch.title, batch.playlist, len(batch.jobs)), ('ready', '夏天的歌', True, 5))
        self.assertEqual(len(batch.pending()), 5)
        queue.set_limit(batch.source_id, 2)
        # 范围按歌单原始顺序截取；范围外的歌不下载，但也没有被忽略。
        self.assertEqual([job['title'] for job in batch.pending()], ['歌 0', '歌 1'])
        self.assertTrue(all(job['state'] == 'ready' for job in batch.jobs))
        # 读过的歌单被记住：名字、链接、歌曲顺序和范围都在，不用再粘贴。
        source = self.library.link_sources()[0]
        self.assertEqual((source['name'], source['url'], source['limit'], len(source['items'])),
                         ('夏天的歌', 'https://music.163.com/playlist?id=42', 2, 5))
        with patch('djtool.online.normalize_link', return_value=('netease', 'https://music.163.com/playlist?id=42', 'playlist', '42')), \
             patch('djtool.online.fetch_listing', return_value=self.listing(7)):
            again = queue.submit('https://music.163.com/playlist?id=42')
            self.assertTrue(wait_until(self.app, lambda: not again.active() and not queue.busy()))
        # 再读一遍：还是同一个歌单、同一个范围，只是知道多了两首；旧的那次记录让位。
        self.assertEqual((again.limit, again.fresh, again.source_id), (2, 2, source['id']))
        self.assertEqual((queue.batches, len(self.library.link_sources())), ([again], 1))
        view = queue.source_views()[0]
        self.assertEqual((view['total'], view['in_scope'], view['pending'], view['have']), (7, 2, 2, 0))

    def test_download_runs_only_pending_jobs_in_range_and_reports_result(self):
        queue = IntakeQueue(self.library)
        make_tracks(self.library, 1)
        with patch('djtool.online.normalize_link', return_value=('netease', 'https://music.163.com/playlist?id=7', 'playlist', '7')), \
             patch('djtool.online.fetch_listing', return_value=self.listing(4)):
            batch = queue.submit('https://music.163.com/playlist?id=7')
            self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        queue.set_limit(batch.source_id, 3)
        seen = []

        def fake_download(info, library, progress, cancel, music_client=None):
            seen.append(info['title'])
            if info['title'] == '歌 1':
                raise ValueError('模拟的单曲失败')
            job = library.enqueue(info)
            library.set_job_state(job['id'], 'complete', '已入库', 'track-0000')
            return 'track-0000', True

        with patch('djtool.online.download_track', fake_download), patch('djtool.netease_backend.create_music_client', return_value=None):
            queue.start(batch)
            self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        self.assertEqual(seen, ['歌 0', '歌 1', '歌 2'])
        self.assertEqual((batch.state, batch.have()), ('stopped', 2))
        self.assertEqual([job['title'] for job in batch.pending()], ['歌 1'])
        self.assertIn('1 首没拿到', batch.detail)
        self.assertEqual(batch.downloaded_track_ids(), ['track-0000'])

    def test_failed_link_becomes_a_failed_batch_and_queue_continues(self):
        from djtool.online import OnlineError
        queue = IntakeQueue(self.library)
        with patch('djtool.online.normalize_link', side_effect=OnlineError('当前只接受网易云和 SoundCloud 链接')):
            batch = queue.submit('https://example.com/x')
            self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        self.assertEqual((batch.state, batch.detail), ('failed', '当前只接受网易云和 SoundCloud 链接'))
        self.assertFalse(queue.busy())


class SourceTests(Base):
    """记住的歌单、SoundCloud 歌单读取、收歌文件夹；全部离线。"""

    def listing(self, count, name='待收'):
        return name, [{'provider': 'netease', 'remote_id': str(500 + index), 'url': f'https://music.163.com/song?id={500 + index}',
                       'title': f'歌 {index}', 'artist': '艺人', 'duration': 200} for index in range(count)]

    def read(self, queue, link, listing, parsed, **options):
        with patch('djtool.online.normalize_link', return_value=parsed), patch('djtool.online.fetch_listing', return_value=listing):
            batch = queue.submit(link, **options)
            self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        return batch

    def fake_download(self, seen):
        def download(info, library, progress, cancel, music_client=None):
            seen.append(info['title'])
            job = library.enqueue(info)
            library.set_job_state(job['id'], 'complete', '已入库', 'track-0000')
            return 'track-0000', True
        return download

    def test_one_click_sync_rereads_every_collect_all_source_and_downloads_what_is_missing(self):
        make_tracks(self.library, 1)
        queue = IntakeQueue(self.library)
        self.read(queue, 'https://music.163.com/playlist?id=1', self.listing(2),
                  ('netease', 'https://music.163.com/playlist?id=1', 'playlist', '1'))
        browse = self.read(queue, 'https://soundcloud.com/someone', self.listing(3, '某人的上传'),
                           ('soundcloud', 'https://soundcloud.com/someone/tracks', 'playlist', 'someone/tracks'))
        # 某个人的上传页是“挑着收”的，一键收新歌不会把它整页下载下来。
        self.assertEqual([source['mode'] for source in self.library.link_sources()], ['all', 'pick'])
        seen = []
        with patch('djtool.online.normalize_link', return_value=('netease', 'https://music.163.com/playlist?id=1', 'playlist', '1')), \
             patch('djtool.online.fetch_listing', return_value=self.listing(4)), \
             patch('djtool.online.download_track', self.fake_download(seen)), \
             patch('djtool.netease_backend.create_music_client', return_value=None):
            self.assertEqual(queue.sync_all(), 1)
            self.assertTrue(wait_until(self.app, lambda: not queue.busy() and not queue.active_count()))
        self.assertEqual(seen, ['歌 0', '歌 1', '歌 2', '歌 3'])
        first = queue.source_views()[0]
        self.assertEqual((first['total'], first['pending'], first['batch'].state), (4, 0, 'done'))
        self.assertEqual(browse.state, 'ready')

    def test_missing_songs_can_be_downloaded_from_the_remembered_list_without_rereading(self):
        make_tracks(self.library, 1)
        queue = IntakeQueue(self.library)
        self.read(queue, 'https://music.163.com/playlist?id=9', self.listing(3),
                  ('netease', 'https://music.163.com/playlist?id=9', 'playlist', '9'))
        source = self.library.link_sources()[0]
        later = IntakeQueue(self.library)      # 相当于下次打开应用：没有批次，只有记住的歌单
        self.assertEqual(later.source_views()[0]['pending'], 3)
        seen = []
        with patch('djtool.online.fetch_listing', side_effect=AssertionError('不应该重新读取列表')), \
             patch('djtool.online.download_track', self.fake_download(seen)), \
             patch('djtool.netease_backend.create_music_client', return_value=None):
            batch = later.download_source(source['id'])
            self.assertTrue(wait_until(self.app, lambda: not batch.active() and not later.busy()))
        self.assertEqual((seen, later.source_views()[0]['pending']), (['歌 0', '歌 1', '歌 2'], 0))
        self.assertTrue(later.remove_source(source['id']))
        self.assertEqual((self.library.link_sources(), len(self.library.jobs_for(source['items']))), ([], 3))

    def test_previously_subscribed_playlists_and_ranges_are_carried_over_once(self):
        root = Path(self.temp.name) / 'old'
        (root / '.djtool').mkdir(parents=True)
        (root / '.djtool' / 'playlist-ranges.json').write_text(
            '{"version": 1, "limits": {"netease:playlist:360000118": 940, "netease:playlist:77": 0}}', encoding='utf-8')
        import sqlite3
        with sqlite3.connect(root / '.djtool' / 'library.sqlite3') as db:
            db.executescript("""CREATE TABLE intake_sources (id TEXT PRIMARY KEY, kind TEXT, address TEXT, name TEXT);
                CREATE TABLE source_items (source_id TEXT, remote_id TEXT, eligible INTEGER, job_id TEXT);
                INSERT INTO intake_sources VALUES('s1','playlist','https://music.163.com/playlist?id=77','DJ');
                INSERT INTO intake_sources VALUES('s2','folder','/tmp/x','下载');
                INSERT INTO source_items VALUES('s1','1',1,'job-a');""")
        library = Library(root)
        sources = {source['url']: source for source in library.link_sources()}
        self.assertEqual(set(sources), {'https://music.163.com/playlist?id=77', 'https://music.163.com/playlist?id=360000118'})
        self.assertEqual((sources['https://music.163.com/playlist?id=77']['name'],
                          sources['https://music.163.com/playlist?id=77']['items'],
                          sources['https://music.163.com/playlist?id=360000118']['limit']), ('DJ', ['job-a'], 940))
        library.remove_link_source(sources['https://music.163.com/playlist?id=77']['id'])
        self.assertEqual(len(Library(root).link_sources()), 1)      # 移除过的不会在下次打开时回来

    def test_soundcloud_links_cover_tracks_sets_likes_and_creator_pages(self):
        from djtool.online import OnlineError, normalize_link
        self.assertEqual(normalize_link('https://soundcloud.com/a/b?si=x'), ('soundcloud', 'https://soundcloud.com/a/b', 'song', None))
        self.assertEqual(normalize_link('https://soundcloud.com/a/sets/club-edits?si=x')[1:], ('https://soundcloud.com/a/sets/club-edits', 'playlist', 'a/sets/club-edits'))
        self.assertEqual(normalize_link('https://soundcloud.com/a/likes')[1:], ('https://soundcloud.com/a/likes', 'playlist', 'a/likes'))
        self.assertEqual(normalize_link('https://soundcloud.com/a')[1:], ('https://soundcloud.com/a/tracks', 'playlist', 'a/tracks'))
        for bad in ('https://soundcloud.com/you/likes', 'https://soundcloud.com/discover/sets/x', 'https://soundcloud.com/a/sets'):
            with self.assertRaises(OnlineError):
                normalize_link(bad)

    def test_soundcloud_playlist_listing_keeps_order_skips_nested_sets_and_survives_missing_details(self):
        from djtool import online

        class FakeExtractor:
            _API_V2_BASE, _HEADERS = 'https://api-v2.soundcloud.com/', {}
            calls = []

            def initialize(self):
                pass

            def _call_api(self, url, video_id, note, query, headers):
                self.calls.append(query['ids'])
                return [{'id': 11, 'title': 'Song A (Jersey Club Edit)', 'duration': 181000, 'genre': 'Jersey Club',
                         'permalink_url': 'https://soundcloud.com/dj-one/song-a-edit', 'created_at': '2026-03-04T10:00:00Z',
                         'artwork_url': 'https://i1.sndcdn.com/artworks-abc-large.jpg',
                         'user': {'username': 'DJ One', 'permalink_url': 'https://soundcloud.com/dj-one'}}]

        class FakeDownloader:
            options = None

            def __init__(self, options):
                FakeDownloader.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                return {'_type': 'playlist', 'title': 'someone (Likes)', 'entries': [
                    {'id': '11', 'url': 'https://soundcloud.com/dj-one/song-a-edit', 'ie_key': 'Soundcloud', 'title': 'Song A'},
                    {'id': '90', 'url': 'https://soundcloud.com/x/sets/whole-set', 'ie_key': None},
                    {'id': '12', 'url': 'https://soundcloud.com/dj-two/song-b-mashup', 'ie_key': 'Soundcloud'},
                    {'id': '11', 'url': 'https://soundcloud.com/dj-one/song-a-edit', 'ie_key': 'Soundcloud'}]}

            def get_info_extractor(self, name):
                return FakeExtractor()

        with patch.object(online.yt_dlp, 'YoutubeDL', FakeDownloader), patch.object(online, 'ffmpeg_path', return_value='ffmpeg'):
            name, tracks = online.soundcloud_listing('https://soundcloud.com/someone/likes', 'playlist')
        self.assertEqual((name, FakeDownloader.options['extract_flat'], FakeExtractor.calls), ('someone的点赞', 'in_playlist', ['11,12']))
        self.assertEqual([(track['remote_id'], track['title'], track['artist'], track['duration']) for track in tracks],
                         [('11', 'Song A (Jersey Club Edit)', 'DJ One', 181.0), ('12', 'song b mashup', 'dj-two', 0)])
        self.assertEqual((tracks[0]['thumbnail'], tracks[0]['genre'], tracks[0]['release_date']),
                         ('https://i1.sndcdn.com/artworks-abc-t500x500.jpg', 'Jersey Club', '2026-03-04'))
        self.assertNotIn('formats', tracks[0]['source_metadata'])

    def make_mp3(self, path):
        subprocess.run([ffmpeg_path(), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'anullsrc=r=22050:cl=mono',
                        '-t', '2', '-c:a', 'libmp3lame', str(path)], check=True)
        os.utime(path, (time.time() - 60, time.time() - 60))
        return path

    def test_watch_folder_counts_only_unprocessed_files_and_never_looks_inside_serato(self):
        from djtool.watch import WatchFolders
        folder = Path(self.temp.name) / 'downloads'
        (folder / '_Serato_' / 'Subcrates').mkdir(parents=True)
        (folder / 'deep').mkdir()
        old = time.time() - 60
        for name in ('a.mp3', 'deep/b.ncm', '_Serato_/c.mp3', 'notes.txt', '.hidden.mp3'):
            (folder / name).write_bytes(b'x')
            os.utime(folder / name, (old, old))
        (folder / 'still-downloading.mp3').write_bytes(b'x')
        watch = WatchFolders(self.library.root, defaults=[folder])
        self.assertEqual(watch.folders(), [{'path': str(folder), 'auto': False}])
        found = watch.new_files(str(folder))
        self.assertEqual(sorted(Path(path).name for path in found), ['a.mp3', 'b.ncm'])
        watch.mark_seen(found[:1])
        self.assertEqual(len(watch.new_files(str(folder))), 1)
        self.assertEqual(watch.new_files(str(folder), known={str(folder / 'a.mp3'), str(folder / 'deep' / 'b.ncm')}), [])
        with self.assertRaises(ValueError):
            watch.add(folder / '_Serato_')
        watch.remove(str(folder))
        self.assertEqual(WatchFolders(self.library.root, defaults=[folder]).folders(), [])
        watch.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):       # 损坏的设置报错，不悄悄重置成“全部是新文件”
            watch.folders()

    def test_collecting_a_watch_folder_imports_new_files_once(self):
        folder = Path(self.temp.name) / 'netease'
        folder.mkdir()
        self.make_mp3(folder / 'one.mp3')
        queue = IntakeQueue(self.library)
        queue.watch().add(folder)
        view = queue.folder_views()[0]
        self.assertEqual((view['name'], len(view['files'])), ('netease', 1))
        self.assertEqual(queue.collect_auto_folders(), [])            # 没开自动收时只数数，不动手
        batch = queue.collect_folder(view['path'])
        self.assertTrue(wait_until(self.app, lambda: not batch.active() and not queue.busy()))
        self.assertEqual((batch.state, batch.added, len(self.library.tracks())), ('done', 1, 1))
        self.assertTrue((folder / 'one.mp3').is_file())               # 源文件留在原处
        self.assertEqual(queue.folder_views()[0]['files'], [])
        self.assertIsNone(queue.collect_folder(view['path']))
        queue.watch().set_auto(view['path'], True)
        self.make_mp3(folder / 'two.mp3')
        started = queue.collect_auto_folders()
        self.assertEqual(len(started), 1)
        self.assertTrue(wait_until(self.app, lambda: not queue.busy() and not queue.active_count()))
        self.assertEqual(len(self.library.tracks()), 1)               # 内容相同，按整文件去重


class WindowTests(Base):
    def setUp(self):
        super().setUp()
        make_tracks(self.library, 12)
        self.set_id = self.library.create_collection('周五', 'set')
        self.library.add_members(self.set_id, ['track-0001', 'track-0002', 'track-0003'])
        self.library.set_note(self.set_id, 'track-0002', '接下一首前先降低音')
        self.window = MainWindow(self.library.root, remember=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        super().tearDown()

    def test_isolated_session_never_starts_serato_work(self):
        self.assertFalse(self.window.serato_timer.isActive())
        self.window.check_serato(force=True)
        self.assertIsNone(self.window.serato_worker)
        self.assertTrue(self.window.serato_pill.isHidden())

    def test_library_and_set_pages_show_the_same_tracks_as_the_database(self):
        self.assertEqual(len(self.window.library_page.table.visible_ids()), 12)
        self.window.open_collection(self.set_id)
        self.assertIs(self.window.pages.currentWidget(), self.window.set_page)
        self.assertEqual(self.window.set_page.list.ids(), ['track-0001', 'track-0002', 'track-0003'])
        self.assertIn('3 首', self.window.set_page.meta.text())

    def test_adding_skips_existing_members_and_keeps_order(self):
        self.window.add_to_collection(['track-0003', 'track-0007', 'track-0005'], self.set_id)
        self.assertEqual([t['id'] for t in self.library.tracks(self.set_id)],
                         ['track-0001', 'track-0002', 'track-0003', 'track-0007', 'track-0005'])
        self.assertIn('2 首', self.window.toast.text.text())
        self.assertIn('1 首原本就在里面', self.window.toast.text.text())

    def test_remove_can_be_undone_with_position_and_note(self):
        self.window.open_collection(self.set_id)
        self.window.remove_from_collection(self.set_id, ['track-0002'])
        self.assertEqual(self.window.set_page.list.ids(), ['track-0001', 'track-0003'])
        self.window.toast.action.click()
        restored = self.library.tracks(self.set_id)
        self.assertEqual([t['id'] for t in restored], ['track-0001', 'track-0002', 'track-0003'])
        self.assertEqual(restored[1]['set_note'], '接下一首前先降低音')

    def test_dragging_a_row_reorders_the_set(self):
        self.window.open_collection(self.set_id)
        listing = self.window.set_page.list
        self.app.processEvents()
        start = listing.visualItemRect(listing.item(0)).center()
        end = listing.visualItemRect(listing.item(2)).center() + QPoint(0, 12)
        QTest.mousePress(listing.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(listing.viewport(), start + QPoint(0, 30))
        QTest.mouseMove(listing.viewport(), end)
        QTest.mouseRelease(listing.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual([t['id'] for t in self.library.tracks(self.set_id)], ['track-0002', 'track-0003', 'track-0001'])

    def test_details_panel_edits_save_and_keep_the_selection(self):
        page = self.window.library_page
        page.wall.select_ids({'track-0004'})
        self.app.processEvents()
        self.assertTrue(page.inspector.isHidden())      # 选中不会自动弹出面板、不挪动版面
        page.show_info(page.selected_tracks()[0])
        self.assertFalse(page.inspector.isHidden())
        page.inspector.title.setText('新的标题')
        page.inspector.title.editingFinished.emit()
        self.assertEqual(self.library.track('track-0004')['title'], '新的标题')
        self.assertEqual(page.wall.selected_ids(), ['track-0004'])
        self.assertEqual(page.inspector.title.text(), '新的标题')

    def test_wall_and_list_share_sorting_filtering_and_selection(self):
        page = self.window.library_page
        self.assertIs(page.view(), page.wall)
        page.wall.select_ids({'track-0003', 'track-0008'})
        page.set_view('list')
        self.assertEqual(set(page.table.selected_ids()), {'track-0003', 'track-0008'})
        self.assertEqual(page.wall.selected_ids(), [])
        page.sort_buttons[0].click()                      # 按标题排序，两种视图顺序一致
        self.assertEqual(page.table.visible_ids(), page.wall.visible_ids())
        self.assertTrue(page.sort_buttons[0].isChecked())
        first = page.table.visible_ids()
        page.sort_buttons[0].click()                      # 再点一次反向
        self.assertEqual(page.table.visible_ids(), first[::-1])
        self.window.search.setText('电子 7')
        self.assertEqual(page.wall.visible_ids(), ['track-0007'])
        self.assertIn('1 / 12', page.count.text())

    def test_selection_bar_follows_selection_and_page(self):
        page = self.window.library_page
        self.assertTrue(self.window.selbar.isHidden())
        page.wall.select_ids({'track-0002'})
        self.app.processEvents()
        self.assertFalse(self.window.selbar.isHidden())
        self.assertIn('电子 2', self.window.sel_text.text())
        self.assertFalse(self.window.sel_info.isHidden())
        page.wall.select_ids({'track-0002', 'track-0005'})
        self.app.processEvents()
        self.assertIn('已选 2 首', self.window.sel_text.text())
        self.assertTrue(self.window.sel_info.isHidden())
        self.window.show_page('sets')
        self.assertTrue(self.window.selbar.isHidden())
        self.window.show_page('library')
        self.assertFalse(self.window.selbar.isHidden())
        page.clear_selection()
        self.app.processEvents()
        self.assertTrue(self.window.selbar.isHidden())

    def test_dragging_tracks_offers_every_set_as_a_target(self):
        from djtool.ui.main_window import DropChip
        self.window._drag_started(2)
        chips = self.window.tray.findChildren(DropChip)
        self.assertEqual([chip.text() for chip in chips if not chip.isHidden()], ['周五', '新建 Set'])
        self.assertFalse(self.window.tray.isHidden())
        chips[0].accept_tracks(['track-0009', 'track-0001'])
        self.assertEqual([t['id'] for t in self.library.tracks(self.set_id)][-1], 'track-0009')
        self.assertIn('1 首原本就在里面', self.window.toast.text.text())

    def test_sets_wall_lists_collections_with_cover_mosaic_data(self):
        self.window.show_page('sets')
        self.assertFalse(self.window.set_page.showing_detail())
        entries = [self.window.set_page.wall.item(row).data(256) for row in range(self.window.set_page.wall.count())]
        self.assertEqual(entries[0]['name'], '周五')
        self.assertEqual((entries[0]['count'], entries[0]['duration']), (3, 540))
        self.assertEqual(entries[-1], '__new__')
        self.window.set_page.wall.opened.emit(self.set_id)
        self.assertTrue(self.window.set_page.showing_detail())
        self.assertTrue(self.window.nav['sets'].isChecked())
        self.window.set_page.back.emit()
        self.assertFalse(self.window.set_page.showing_detail())

    def test_clipboard_link_is_offered_once_and_only_for_supported_sites(self):
        self.assertFalse(self.window.check_clipboard('随便一段文字'))
        self.assertFalse(self.window.check_clipboard('https://example.com/song?id=1'))
        self.assertTrue(self.window.suggest.isHidden())
        self.assertTrue(self.window.check_clipboard('分享歌单 https://music.163.com/playlist?id=42 (来自网易云)'))
        self.assertFalse(self.window.suggest.isHidden())
        self.assertEqual(self.window.suggest.property('link'), 'https://music.163.com/playlist?id=42')
        self.assertFalse(self.window.check_clipboard('https://music.163.com/playlist?id=42'))

    def test_set_sync_does_nothing_without_a_validated_serato_install(self):
        from djtool.ui.main_window import SeratoWorker
        worker = SeratoWorker(self.library, None, {})
        self.assertEqual(worker.sync_sets(), {})
        self.assertFalse((self.library.root / '.djtool' / 'serato-sync').exists())

    def test_submitting_text_routes_to_the_intake_page(self):
        self.window.submit_text('这不是链接')
        self.assertIs(self.window.pages.currentWidget(), self.window.library_page)
        batch = Batch('files', '示例', state='done')
        self.window.queue.batches.insert(0, batch)
        self.window.queue.batch_added.emit(batch)
        self.assertIn(batch.id, self.window.intake_page.rows)
        self.window.queue.remove(batch)
        self.assertNotIn(batch.id, self.window.intake_page.rows)

    def test_intake_page_lists_remembered_playlists_folders_and_recent_tracks(self):
        infos = [{'provider': 'netease', 'remote_id': str(900 + index), 'url': f'https://music.163.com/song?id={900 + index}',
                  'title': f'待收 {index}', 'artist': '艺人', 'duration': 200} for index in range(3)]
        jobs = self.library.enqueue_many(infos)
        self.library.set_job_state(jobs[0]['id'], 'complete', '已入库', 'track-0001')
        source, _ = self.library.save_link_source('https://music.163.com/playlist?id=5', 'netease', '周末要放的',
                                                  [job['id'] for job in jobs])
        folder = Path(self.temp.name) / 'downloads'
        folder.mkdir()
        (folder / 'new.ncm').write_bytes(b'x')
        os.utime(folder / 'new.ncm', (time.time() - 60, time.time() - 60))
        self.window.queue.watch().add(folder)
        self.window.show_page('intake')
        page = self.window.intake_page
        tile = page.tiles[source['id']]
        self.assertEqual((tile.name.text(), tile.meta.text(), tile.collect.text()), ('周末要放的', '3 首，缺 2 首', '收 2 首'))
        self.assertFalse(tile.collect.isHidden())
        self.assertEqual((page.new_file_count(), self.window.nav['intake'].text()), (1, '收歌 +1'))
        self.assertEqual(len(page.strip.tracks), 12)
        self.assertTrue(page.empty.isHidden())
        # 点开歌单：先看缺少的两首；回到收歌页时回到总览。
        page.open_source(source['id'])
        self.assertIs(page.stack.currentWidget(), page.detail)
        self.assertEqual((page.detail.proxy.rowCount(), page.detail.collect.text()), (2, '收缺少的 2 首'))
        self.window.queue.set_limit(source['id'], 1)
        self.app.processEvents()
        page.refresh()
        self.assertEqual(page.detail.meta.text(), '网易云　3 首，只收前 1 首，全部到齐')
        self.window.show_page('intake')
        self.assertIs(page.stack.currentIndex(), 0)
        self.window.grab()

    def test_player_is_docked_across_the_whole_window_with_a_long_seek_track(self):
        self.window.resize(1320, 880)
        self.app.processEvents()
        player = self.window.player
        self.assertEqual((player.width(), player.geometry().bottom()),
                         (self.window.shell.width(), self.window.shell.height() - 1))
        self.assertGreater(player.seek.width(), 560)
        self.assertGreaterEqual(player.seek.height(), 32)       # 整个高度都能点，不用瞄准细线

    def test_theme_switch_restyles_open_window_without_touching_data(self):
        before = self.library.tracks()
        theme.apply(self.app, 'light')
        self.assertEqual(theme.mode(), 'light')
        self.window.grab()
        theme.apply(self.app, 'dark')
        self.assertEqual(self.library.tracks(), before)


if __name__ == '__main__':
    unittest.main()
