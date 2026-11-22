"""显式联网验收：和窗口用同一个 DownloadWorker，只写项目隔离曲库。

日常启动不会执行；必须传入验收目录与具体歌曲 ID。报告只包含公开元数据、
固定错误类别，不记录签名地址、服务检测 IP、Cookie 或代理配置。
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch


def run(destination, song_ids):
    from PySide6.QtCore import QCoreApplication, QThread, Signal
    from djtool.library import Library
    from djtool.netease_site import NeteaseSiteClient
    from djtool.online import friendly_error
    from djtool.workers import DownloadWorker
    from djtool.online import download_track
    from djtool.network_errors import NetworkFailure
    import certifi

    project = Path(sys.executable).resolve().parents[4] if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    destination = Path(destination).resolve()
    if not destination.is_relative_to(project/'work') or not song_ids or len(song_ids) > 10 or any(
            not value.isdigit() or not 0 < int(value) < 10**19 for value in song_ids):
        raise ValueError('联网验收只接受项目 work 下的隔离目录和最多 10 个歌曲 ID')
    destination.mkdir(parents=True, exist_ok=True)
    library = Library(destination/'library')
    library.configure_music_root(destination/'audio')
    report = {'frozen': bool(getattr(sys, 'frozen', False)), 'song_ids': song_ids,
              'certificate_bundle_exists': Path(certifi.where()).is_file(),
              'account_read': False, 'real_serato_write': False, 'downloaded': [], 'errors': []}
    app = QCoreApplication([])

    class Listing(QThread):
        ready = Signal(object)
        failed = Signal(str)
        def run(self):
            try:
                client = NeteaseSiteClient()
                infos = [client.listing('song', value)[1][0] for value in song_ids]
                self.ready.emit(library.enqueue_many(infos))
            except Exception as error:
                if isinstance(error, NetworkFailure):
                    report.setdefault('network_diagnostics', []).append(error.diagnostic())
                self.failed.emit(friendly_error(error))

    listing = Listing()
    workers = [listing]
    def save():
        (destination/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    def failure(message):
        report['errors'].append(message)
        report['passed'] = False
        save()
    def finished(counts):
        report['counts'] = counts
        # 缓存复用证明去重，不能算作此次真实联网下载的证据。
        report['fresh_downloads'] = counts['added']
        report['reused_downloads'] = counts['existing']
        report['network_path_verified'] = counts['added'] == len(set(song_ids))
        for job in library.online_jobs():
            if job['state'] == 'complete':
                track = library.track(job['track_id'])
                report['downloaded'].append({key:track[key] for key in
                    ('title', 'artist', 'duration', 'bitrate', 'path', 'reviewed')})
            else:
                report['errors'].append({'song_id':job['remote_id'], 'state':job['state'], 'error':job['message']})
        report['passed'] = not report['errors'] and len(report['downloaded']) == len(set(song_ids))
        save()
    def start(jobs):
        worker = DownloadWorker(library, jobs)
        workers.append(worker)
        worker.completed.connect(finished)
        worker.finished.connect(app.quit)
        worker.start()
    listing.ready.connect(start)
    listing.failed.connect(failure)
    listing.failed.connect(app.quit)
    def checked_download(*args, **kwargs):
        try:
            return download_track(*args, **kwargs)
        except NetworkFailure as error:
            report.setdefault('network_diagnostics', []).append(error.diagnostic())
            raise
    with patch('djtool.online.download_track', checked_download):
        listing.start()
        app.exec()
        for worker in workers:
            worker.wait()
    print(json.dumps({'passed':report.get('passed',False), 'downloaded':len(report['downloaded']),
                      'errors':report['errors'], 'report':str(destination/'result.json')}, ensure_ascii=False), flush=True)
    return 0 if report.get('passed') else 1
