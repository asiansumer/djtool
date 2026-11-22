"""耗时文件操作在后台进行，主界面只接收结果。"""

import json
import math
import os
from pathlib import Path
import time

import requests
from PySide6.QtCore import QThread, Signal
from djtool.audio_tasks import AudioCancelled
from djtool.intake import import_audio


class ImportWorker(QThread):
    progress = Signal(int, int, str)
    status = Signal(str)
    completed = Signal(dict)

    def __init__(self, library, paths, parent=None):
        super().__init__(parent)
        self.library = library
        self.paths = paths

    def run(self):
        result = {'added': 0, 'duplicates': 0, 'errors': [], 'cancelled': False}
        try:
            files = set()
            for entry in self.paths:
                path = Path(entry).expanduser()
                if path.is_dir():
                    def scan_error(error):
                        result['errors'].append(str(error))
                    for root, directories, names in os.walk(path, onerror=scan_error):
                        if self.isInterruptionRequested():
                            break
                        directories[:] = [name for name in directories if not name.startswith('.')]
                        for name in names:
                            if Path(name).suffix.lower() in ('.mp3', '.ncm'):
                                files.add(str((Path(root) / name).resolve()))
                else:
                    files.add(str(path.resolve()))
            ordered = sorted(files)
            if not ordered and not self.isInterruptionRequested():
                result['errors'].append('没有找到 MP3 或 NCM 文件。请选择音乐文件或包含它们的文件夹。')
            for index, source in enumerate(ordered):
                if self.isInterruptionRequested():
                    result['cancelled'] = True
                    break
                self.progress.emit(index, len(ordered), Path(source).name)
                try:
                    _, added = import_audio(self.library, source,
                        lambda message: self.status.emit(f'{index+1}/{len(ordered)} · {Path(source).name} · {message}'),
                        self.isInterruptionRequested)
                    result['added' if added else 'duplicates'] += 1
                except AudioCancelled:
                    result['cancelled'] = True
                    break
                except Exception as error:
                    result['errors'].append(f'{Path(source).name}：{error}')
            result['cancelled'] |= self.isInterruptionRequested()
            self.completed.emit(result)
        except Exception as error:
            result['errors'].append(str(error))
            self.completed.emit(result)


class ReferenceWorker(QThread):
    """把现有 Serato 音乐按原路径登记；不复制、不改标签、不读取 Serato 数据库。"""
    progress = Signal(int, int, str)
    status = Signal(str)
    completed = Signal(dict)

    def __init__(self, library, music_root, parent=None):
        super().__init__(parent)
        self.library, self.music_root = library, Path(music_root)

    def run(self):
        result = {'added': 0, 'duplicates': 0, 'errors': [], 'cancelled': False, 'references': True}
        try:
            files = []
            supported = {'.mp3', '.wav', '.flac', '.aif', '.aiff', '.m4a'}
            for root, directories, names in os.walk(self.music_root,
                    onerror=lambda error: result['errors'].append(str(error)), followlinks=False):
                if self.isInterruptionRequested():
                    break
                directories[:] = [name for name in directories if not name.startswith('.')
                    and name not in {'_Serato_', '_Serato_Backup'}
                    and not (Path(root) / name).is_symlink()]
                files.extend(Path(root) / name for name in names if Path(name).suffix.lower() in supported)
            for index, source in enumerate(sorted(files)):
                if self.isInterruptionRequested():
                    break
                self.progress.emit(index, len(files), source.name)
                try:
                    _, added = self.library.reference_existing(source, self.music_root,
                        cancel=self.isInterruptionRequested)
                    result['added' if added else 'duplicates'] += 1
                except AudioCancelled:
                    break
                except Exception as error:
                    result['errors'].append(f'{source.name}：{error}')
            result['cancelled'] = self.isInterruptionRequested()
        except Exception as error:
            result['errors'].append(str(error))
        self.completed.emit(result)


class DownloadWorker(QThread):
    """逐首下载一批链接任务。限流不算单曲失败：短冷却自动等待续跑，长冷却停批并保留队列。"""
    updated = Signal(str, str, int)
    batch_updated = Signal(dict)
    completed = Signal(dict)
    def __init__(self, library, jobs, parent=None):
        super().__init__(parent)
        self.library, self.jobs = library, jobs

    @staticmethod
    def can_wait_for_retry(error):
        from djtool.netease_site import SiteRateLimit
        retry = getattr(error, 'retry_after', None)
        return (isinstance(error, SiteRateLimit) and type(retry) in (int, float) and
                math.isfinite(retry) and 0 < retry <= 300)

    def wait_for_retry(self, error, job, counts):
        """只等待站点明确给出的短冷却；保持同一客户端/IP，随时响应停止。"""
        deadline = time.monotonic() + error.retry_after + 1
        previous_second = None
        while True:
            if self.isInterruptionRequested():
                counts['cancelled'] = True
                return False
            left = deadline - time.monotonic()
            if left <= 0:
                return True
            remaining = math.ceil(left)
            if remaining != previous_second:
                self.updated.emit(job['id'], f'网站限流，等待 {remaining} 秒后自动继续', 0)
                previous_second = remaining
            time.sleep(min(.2, left))

    def run(self):
        from djtool.netease_backend import create_music_client
        from djtool.netease_common import AuthorizationError, MusicUnavailable
        from djtool.netease_site import SiteRateLimit
        from djtool.network_errors import NetworkFailure, network_failure
        from djtool.online import download_track, friendly_error
        counts = {'added':0,'existing':0,'failed':0,'unavailable':0,'cancelled':False,
                  'total':len(self.jobs),'processed':0}
        client = None
        consecutive_network_failures = 0
        cooldown_attempts = 0
        index = 0
        self.batch_updated.emit(dict(counts))
        while index < len(self.jobs):
            job = self.jobs[index]
            if self.isInterruptionRequested():
                counts['cancelled'] = True
                break
            info = json.loads(job['metadata_json'])
            if info['provider'] == 'netease' and client is None:
                try:
                    client = create_music_client(self.library)
                except Exception as error:
                    # 初始化/设置错误影响整个客户端，尚未尝试任何歌曲；只报一次并保留队列。
                    if self.isInterruptionRequested():
                        counts['cancelled'] = True
                    else:
                        counts['batch_error'] = friendly_error(error)
                        self.updated.emit(job['id'], counts['batch_error'], 0)
                    self.batch_updated.emit(dict(counts))
                    break
            try:
                identifier, added = download_track(info, self.library,
                    lambda message, percent: self.updated.emit(job['id'],message,int(percent)),
                    self.isInterruptionRequested, music_client=client)
                counts['added' if added else 'existing'] += 1
                consecutive_network_failures = 0
                cooldown_attempts = 0
                counts['processed'] += 1
                index += 1
                if self.isInterruptionRequested():
                    counts['cancelled'] = True
                self.updated.emit(job['id'], '已入库', 100)
                self.batch_updated.emit(dict(counts))
                if counts['cancelled'] or index == len(self.jobs):
                    break
                rate_limit = getattr(client, 'rate_limit', None)
                if isinstance(rate_limit, AuthorizationError) and rate_limit.code == 429:
                    # 歌词触发限流时音频已入库；等待后处理下一首，绝不重下成功歌曲。
                    if self.can_wait_for_retry(rate_limit):
                        if not self.wait_for_retry(rate_limit, self.jobs[index], counts):
                            counts['cancelled'] = True
                            break
                        client.rate_limit = None
                        continue
                    counts['batch_error'] = friendly_error(rate_limit)
                    if rate_limit.retry_after:
                        counts['retry_after'] = rate_limit.retry_after
                    break
            except Exception as error:
                if isinstance(error, SiteRateLimit):
                    # 限流不是歌曲下载失败；保留待下载状态与当前索引，按服务器冷却再试。
                    consecutive_network_failures = 0
                    self.library.set_job_state(job['id'], 'ready', friendly_error(error))
                    if self.isInterruptionRequested():
                        counts['cancelled'] = True
                        break
                    if self.can_wait_for_retry(error) and cooldown_attempts < 2:
                        cooldown_attempts += 1
                        if not self.wait_for_retry(error, job, counts):
                            counts['cancelled'] = True
                            break
                        if client is not None:
                            client.rate_limit = None
                        continue
                    self.updated.emit(job['id'], friendly_error(error), 0)
                    counts['batch_error'] = friendly_error(error)
                    retry_after = getattr(error, 'retry_after', None)
                    if type(retry_after) in (int, float) and math.isfinite(retry_after) and retry_after > 0:
                        counts['retry_after'] = retry_after
                    self.batch_updated.emit(dict(counts))
                    break
                # TLS/代理故障是共同配置问题；连接/超时连续三首失败后也先停下，
                # 保留其余歌曲，避免一处网络故障把上千首任务全部烧成失败。
                if isinstance(error, requests.RequestException):
                    error = network_failure(error, stage='download')
                consecutive_network_failures = (consecutive_network_failures + 1
                    if isinstance(error, NetworkFailure) and error.transient else 0)
                self.updated.emit(job['id'], friendly_error(error), 0)
                if self.isInterruptionRequested():
                    counts['cancelled'] = True
                    break
                counts['unavailable' if isinstance(error, MusicUnavailable) else 'failed'] += 1
                counts['processed'] += 1
                index += 1
                cooldown_attempts = 0
                self.batch_updated.emit(dict(counts))
                # 其他认证/网络硬故障仍结束本批，不把它们当作站点短冷却自动重试。
                if consecutive_network_failures >= 3 or getattr(error, 'stop_batch', False) or (isinstance(error, AuthorizationError) and
                        error.code in (301,401,403,429,-444,-446,-447,-460,-461)):
                    counts['batch_error'] = friendly_error(error)
                    retry_after = getattr(error, 'retry_after', None)
                    if type(retry_after) in (int, float) and math.isfinite(retry_after) and retry_after > 0:
                        counts['retry_after'] = retry_after
                    break
        self.completed.emit(counts)
