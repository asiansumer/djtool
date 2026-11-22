"""发布包自检：独立临时曲库 + 合成静音，硬拒绝联网、播放和真实 Serato 读写。

用法：DJTOOL --check-runtime <输出目录>。它证明的是“打包后的程序能启动、
核心流程在离线夹具上走得通”，不代表真实下载、出声播放或原生 Serato 已验证。
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from unittest.mock import patch


def run(destination):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    import requests
    from djtool.audio_tasks import ffmpeg_path, verify_audio
    from djtool.library import Library
    from PySide6.QtCore import QTimer
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtNetwork import QNetworkAccessManager
    from PySide6.QtWidgets import QApplication
    from djtool.app import configure_application
    from djtool.ui import theme
    from djtool.ui.dialogs import LibraryLocationDialog, MetadataDialog, MusicStorageDialog
    from djtool.ui.main_window import MainWindow

    def forbidden(*args, **kwargs):
        raise AssertionError('发布包自检禁止联网、音频播放或真实 Serato 读写')

    original_source = QMediaPlayer.setSource

    def empty_only(player, url):
        if not url.isEmpty():
            forbidden()
        return original_source(player, url)

    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'result.json').write_text('{"state":"running"}')
    with tempfile.TemporaryDirectory(prefix='djtool-runtime-') as temporary, \
         patch.object(QMediaPlayer, 'play', forbidden), \
         patch.object(QMediaPlayer, 'setSource', empty_only), \
         patch.object(QNetworkAccessManager, 'get', forbidden), \
         patch.object(requests.sessions.Session, 'request', forbidden), \
         patch('djtool.serato.serato_is_running', forbidden), \
         patch('djtool.serato_intake._native_received', forbidden):
        root = Path(temporary).resolve()
        executable = ffmpeg_path()

        def silence(path, seconds):
            subprocess.run([executable, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'anullsrc=r=22050:cl=mono',
                            '-t', str(seconds), '-c:a', 'libmp3lame', str(path)], check=True)
            return path

        source = silence(root / 'silence.mp3', 9)
        library = Library(root / 'library')
        identifier, added = library.import_file(source)
        verify_audio(source)
        assert added and library.track(identifier)['bpm_estimate'] is None
        library.configure_music_root(root / 'Serato/DJTOOL Audio')
        collection_id = library.create_collection('发布包验证 Set', 'set')

        # Serato 的 BPM 和调性只从临时夹具里读；默认的真实数据库在这一轮被硬拒绝。
        from djtool.serato_bpm import sync_library
        fixture_path = root / 'serato-root.sqlite'
        playback = library.track(identifier)['path']
        with sqlite3.connect(fixture_path) as fixture:
            fixture.execute('PRAGMA user_version=203')
            fixture.execute('CREATE TABLE asset(portable_id TEXT,bpm REAL,key TEXT,file_size INTEGER,'
                            'type TEXT,third_party_type INTEGER,is_missing INTEGER,'
                            'is_corrupt INTEGER,is_stale INTEGER)')
            fixture.execute('INSERT INTO asset VALUES(?,?,?,?,?,?,?,?,?)',
                            (playback.lstrip('/'), 127.63, 'Am', Path(playback).stat().st_size, 'audio', 0, 0, 0, 0))
        with patch('djtool.serato_bpm.default_database', forbidden):
            bpm = sync_library(library, database=fixture_path)
        track = library.track(identifier)
        assert bpm['matched'] == 1 and track['bpm_serato'] == 127.63 and track['key_serato'] == 'Am'

        # 收歌 Crate 只写进临时目录里的隔离 Serato 文件夹。
        from djtool.serato import SeratoCrateAdapter
        from djtool.serato_intake import sync_library as sync_intake
        adapter = SeratoCrateAdapter(library.root, root / 'Serato/_Serato_', isolated=True,
                                     process_checker=lambda: False)
        intake = sync_intake(library, adapter=adapter)
        assert intake['status'] == 'ready' and intake['count'] == 1

        # 打包后这些依赖要能导入：SoundCloud 提取器、在线解析适配器。
        from yt_dlp.extractor.soundcloud import SoundcloudIE
        from djtool.netease_backend import create_music_client
        from djtool.netease_site import NeteaseSiteClient
        from djtool.online import download_track, normalize_link
        assert SoundcloudIE and isinstance(create_music_client(library), NeteaseSiteClient)
        assert normalize_link('https://soundcloud.com/someone/likes')[2] == 'playlist'

        jobs = library.enqueue_many([
            {'provider': 'netease', 'remote_id': str(number + 1), 'url': f'https://music.163.com/song?id={number + 1}',
             'title': f'离线收歌校验 {number + 1}', 'artist': '发布包合成数据', 'album': '隔离自检', 'duration': 9}
            for number in range(12)])
        library.set_job_state(jobs[0]['id'], 'complete', track_id=identifier)
        playlist, _ = library.save_link_source('https://music.163.com/playlist?id=1', 'netease', '发布包验证歌单',
                                               [job['id'] for job in jobs])
        library.update_link_source(playlist['id'], limit=5)
        assert download_track(json.loads(jobs[0]['metadata_json']), library) == (identifier, False)

        app = QApplication([])
        configure_application(app)
        theme.apply(app, 'dark')
        window = MainWindow(library.root, remember=False)
        window.resize(1320, 880)
        window.show()
        app.processEvents()

        def idle(seconds=30):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                if not window.queue.busy() and not window.queue.active_count():
                    return
                time.sleep(.02)
            raise AssertionError('收歌队列没有在限定时间内结束')

        # 隔离曲库不启动 Serato 定时器，也不读剪贴板。
        assert not window.serato_timer.isActive() and window.serato_worker is None
        assert window.player.width() == window.shell.width()

        # 曲库：选中、加入 Set。
        window.show_page('library')
        window.library_page.view().select_ids({identifier})
        assert [item['id'] for item in window.library_page.selected_tracks()] == [identifier]
        window.add_to_collection([identifier], collection_id)
        assert [item['id'] for item in library.tracks(collection_id)] == [identifier]
        window.grab().save(str(destination / 'library.png'))

        # 收歌：记住的歌单按范围算缺少的歌；收歌文件夹里的新文件走真实的本地导入。
        folder = root / 'downloads'
        folder.mkdir()
        fresh = silence(folder / 'fresh.mp3', 5)
        os.utime(fresh, (time.time() - 60, time.time() - 60))
        window.queue.watch().add(folder)
        window.show_page('intake')
        page = window.intake_page
        view = page.views[playlist['id']]
        assert (view['total'], view['in_scope'], view['pending'], view['have']) == (12, 5, 4, 1)
        assert page.new_file_count() == 1
        window.grab().save(str(destination / 'intake.png'))
        batch = window.queue.collect_folder(str(folder))
        idle()
        assert batch.state == 'done' and batch.added == 1 and len(library.tracks()) == 2
        page.refresh()
        assert fresh.is_file() and page.new_file_count() == 0
        page.open_source(playlist['id'])
        assert page.detail.proxy.rowCount() == 4
        window.grab().save(str(destination / 'intake-source.png'))

        # Sets 和主题切换。
        window.show_page('sets')
        window.open_collection(collection_id)
        window.grab().save(str(destination / 'set.png'))
        theme.apply(app, 'light')
        assert theme.mode() == 'light'
        window.show_page('library')
        window.grab().save(str(destination / 'library-light.png'))
        theme.apply(app, 'dark')

        for name, dialog in (('metadata', MetadataDialog(library, library.track(identifier), window)),
                             ('storage', MusicStorageDialog(library, window)),
                             ('location', LibraryLocationDialog(window, library.root))):
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(destination / f'{name}.png'))
            dialog.close()
        assert window.player.player.source().isEmpty()
        QTimer.singleShot(20, window.close)
        assert app.exec() == 0
        (destination / 'result.json').write_text(json.dumps({
            'state': 'pass', 'playback': False, 'network': False, 'real_serato_write': False,
            'hard_denial_guards': True, 'ffmpeg': executable, 'tracks': len(library.tracks()),
            'window': 'record-wall', 'docked_player': True, 'remembered_playlist_range': True,
            'watch_folder_import': True, 'serato_bpm_and_key_readonly': True, 'serato_intake_crate_isolated': True,
            'soundcloud_extractor_import': True, 'online_site_adapter': True, 'theme_switch': True,
            'dialogs': True}, ensure_ascii=False, indent=2))
    return 0
