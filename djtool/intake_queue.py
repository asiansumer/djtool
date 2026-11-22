"""统一的收歌队列：链接、文件、文件夹都变成一个“批次”，排队依次处理。

界面只和这里打交道；真正的读取、下载、转换、校验、入库仍由原有的
online / intake / workers 模块完成。同一时间只运行一个后台任务，
后来的批次排队，这样不会出现“正在导入，请稍后再下载”的互相拒绝。

两样会被记住的东西：
- 歌单（Library 的 link_sources）：读过的歌单链接。以后不用再粘贴，
  “检查新歌”重读一遍，“一键收新歌”把所有歌单里新加的歌都收下来。
- 收歌文件夹（djtool/watch.py）：常用的下载目录，里面的新文件点一下全收。
批次只是这一次打开应用期间的工作记录，不保存。
"""
from dataclasses import dataclass, field
from pathlib import Path
import re
import sqlite3
import time
import uuid

from PySide6.QtCore import QObject, QThread, Signal

SKIP_STATES = ('complete', 'ignored', 'missing_file')
JOB_LABELS = {'ready': '待下载', 'downloading': '上次未完成', 'complete': '已入库', 'failed': '失败',
              'cancelled': '已停止', 'waiting_auth': '未完成', 'unavailable': '暂无音源',
              'ignored': '已忽略', 'missing_file': '文件丢失'}
URL = re.compile(r'https?://\S+')


@dataclass
class Batch:
    kind: str                      # link / files / reference
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = 'queued'          # queued / reading / ready / running / done / stopped / failed
    detail: str = ''
    text: str = ''                 # 链接批次的原始输入
    paths: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    live: dict = field(default_factory=dict)   # 正在处理的任务的即时提示
    source_id: str | None = None   # 对应的歌单（link_sources.id）；单曲和文件没有
    limit: int = 0                 # 只收歌单前 N 首；0 表示全部
    playlist: bool = False
    auto: bool = False             # 读完直接下载缺少的，不等用户再点一次
    fresh: int | None = None       # 比上次读取多出来几首；第一次读取为 None
    watched: list = field(default_factory=list)   # 来自收歌文件夹的文件，处理完记为“已处理”
    progress: tuple = (0, 0)
    added: int = 0
    existing: int = 0
    errors: list = field(default_factory=list)
    retry_at: float = 0

    def scope(self):
        # 先按歌单原始顺序截取，再排除已有的歌；不是“取前 N 首缺少的”。
        return self.jobs[:self.limit] if self.limit else self.jobs

    def pending(self):
        return [job for job in self.scope() if job['state'] not in SKIP_STATES]

    def have(self):
        return sum(job['state'] == 'complete' for job in self.scope())

    def downloaded_track_ids(self):
        return list(dict.fromkeys(job['track_id'] for job in self.scope()
                                  if job['state'] == 'complete' and job.get('track_id')))

    def cooldown(self):
        return max(0, round(self.retry_at - time.monotonic()))

    def active(self):
        return self.state in ('queued', 'reading', 'running')


class _Task(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        from djtool.online import friendly_error
        try:
            self.result.emit(self.operation(self.isInterruptionRequested))
        except Exception as error:
            self.failed.emit(friendly_error(error))


class IntakeQueue(QObject):
    batch_added = Signal(object)
    batch_changed = Signal(object)
    batch_removed = Signal(object)
    job_changed = Signal(object, str)
    library_changed = Signal()
    sources_changed = Signal()

    def __init__(self, library=None, parent=None):
        super().__init__(parent)
        self.library = library
        self.watch_defaults = ()       # 主窗口在日常模式下填入网易云客户端的下载文件夹
        self.batches = []
        self._queue = []
        self._worker = None
        self._current = None

    # ---- 对外入口 -------------------------------------------------------

    def busy(self):
        return self._worker is not None

    def active_count(self):
        return sum(batch.active() for batch in self.batches)

    def submit(self, text, auto=False):
        """收歌栏里的任何输入：含链接就读链接，否则把每一行当作文件或文件夹路径。"""
        text = (text or '').strip()
        if not text:
            return None
        match = URL.search(text)
        if match:
            # 链接正好是一个已经记住的歌单时，等同于在那个歌单上点“检查新歌”。
            known = next((source for source in self._sources() if source['url'] == match.group(0)), None)
            if known:
                return self.check(known['id'], download=auto)
            batch = Batch('link', '正在读取链接…', text=text, state='queued', auto=auto)
            return self._enqueue(batch, 'read')
        paths = [line.strip() for line in text.splitlines() if line.strip()]
        if paths and all(Path(path).expanduser().exists() for path in paths):
            return self.add_files(paths)
        return None

    def add_files(self, paths):
        paths = [str(Path(path).expanduser()) for path in paths if path]
        if not paths:
            return None
        first = Path(paths[0])
        if len(paths) == 1:
            title = first.name
        else:
            title = f'{first.name} 等 {len(paths)} 项'
        return self._enqueue(Batch('files', title, paths=paths), 'import')

    def add_reference(self, root):
        return self._enqueue(Batch('reference', f'读取 {Path(root).name} 里已有的音乐', paths=[str(root)]), 'reference')

    def start(self, batch, jobs=None):
        """下载这个链接批次里缺少的歌；jobs 给定时只下载这几首。"""
        if batch.kind != 'link' or batch.active():
            return
        if batch.cooldown():
            batch.detail = f'网站暂时限流，约 {batch.cooldown()} 秒后可以继续'
            self.batch_changed.emit(batch)
            return
        batch.state = 'queued'
        batch.detail = '排队中' if self.busy() else ''
        self._queue.append((batch, 'download', [job['id'] for job in jobs] if jobs else None))
        self.batch_changed.emit(batch)
        self._pump()

    def stop(self, batch):
        if self._current is batch and self._worker:
            self._worker.requestInterruption()
            batch.detail = '正在停止，等当前这首处理完…'
        else:
            self._queue = [item for item in self._queue if item[0] is not batch]
            if batch.active():
                batch.state = 'stopped' if batch.jobs or batch.kind != 'link' else 'failed'
                batch.detail = '已停止'
        self.batch_changed.emit(batch)

    def remove(self, batch):
        if batch.active():
            return
        self.batches.remove(batch)
        self.batch_removed.emit(batch)

    def stop_all(self):
        """退出或切换曲库前调用；返回 True 表示已经没有后台任务。"""
        self._queue.clear()
        if self._worker:
            self._worker.requestInterruption()
            return False
        return True

    # ---- 歌单 -----------------------------------------------------------

    def _sources(self):
        try:
            return self.library.link_sources() if self.library else []
        except sqlite3.Error:
            return []

    def batch_for(self, source_id):
        return next((batch for batch in self.batches if batch.source_id == source_id), None)

    def source_views(self):
        """收歌页上每个歌单的现状：多少首、已有多少、还缺多少。只读数据库，不联网。"""
        views = []
        for source in self._sources():
            jobs = self.library.jobs_for(source['items'])
            scope = Batch('link', source['name'], jobs=jobs, limit=source['limit'])
            pending = scope.pending()
            views.append({**source, 'jobs': jobs, 'total': len(jobs), 'in_scope': len(scope.scope()),
                          'have': scope.have(), 'pending': len(pending),
                          'failed': sum(job['state'] in ('failed', 'unavailable') for job in pending),
                          'track_ids': [job['track_id'] for job in jobs
                                        if job['state'] == 'complete' and job.get('track_id')],
                          'batch': self.batch_for(source['id'])})
        return views

    def check(self, source_id, download=False):
        """重新读一遍这个歌单，看有没有新加的歌；download 为真时读完直接收下缺少的。"""
        source = self.library.link_source(source_id)
        if not source:
            return None
        current = self.batch_for(source_id)
        if current and current.active():
            current.auto = current.auto or download
            return current
        if current:
            self.remove(current)
        batch = Batch('link', source['name'] or '正在读取歌单…', text=source['url'], source_id=source_id,
                      playlist=True, limit=source['limit'], auto=download)
        return self._enqueue(batch, 'read')

    def sync_all(self):
        """一键收新歌：把每个“全收”的歌单重读一遍，缺的直接下载。返回涉及的歌单数。"""
        count = 0
        for source in self._sources():
            if source['mode'] == 'all' and self.check(source['id'], download=True):
                count += 1
        return count

    def download_source(self, source_id, jobs=None):
        """按上次读到的列表下载这个歌单缺少的歌，不重新联网读取列表。"""
        source = self.library.link_source(source_id)
        if not source:
            return None
        batch = self.batch_for(source_id)
        if batch and batch.active():
            return batch
        fresh = self.library.jobs_for(source['items'])
        if batch is None:
            batch = Batch('link', source['name'], text=source['url'], source_id=source_id, playlist=True,
                          state='ready', limit=source['limit'], jobs=fresh)
            self.batches.insert(0, batch)
            self.batch_added.emit(batch)
        else:
            batch.jobs, batch.limit = fresh, source['limit']
        self.start(batch, jobs)
        return batch

    def set_limit(self, source_id, limit):
        self.library.update_link_source(source_id, limit=limit)
        batch = self.batch_for(source_id)
        if batch:
            batch.limit = limit
            self.batch_changed.emit(batch)
        self.sources_changed.emit()

    def set_mode(self, source_id, mode):
        self.library.update_link_source(source_id, mode=mode)
        self.sources_changed.emit()

    def remove_source(self, source_id):
        batch = self.batch_for(source_id)
        if batch and batch.active():
            return False
        if batch:
            self.remove(batch)
        self.library.remove_link_source(source_id)
        self.sources_changed.emit()
        return True

    # ---- 收歌文件夹 -----------------------------------------------------

    def watch(self):
        from djtool.watch import WatchFolders
        return WatchFolders(self.library.root, self.watch_defaults)

    def folder_views(self):
        """每个收歌文件夹里有多少还没收的文件。只读本机目录。"""
        if not self.library:
            return []
        try:
            watch = self.watch()
            known = self.library.known_source_paths()
            busy = {path for batch in self.batches if batch.active() for path in batch.watched}
            views = []
            for folder in watch.folders():
                exists = Path(folder['path']).is_dir()
                files = [path for path in watch.new_files(folder['path'], known) if path not in busy] if exists else []
                views.append({**folder, 'name': Path(folder['path']).name, 'files': files, 'exists': exists})
            return views
        except (OSError, ValueError, sqlite3.Error):
            return []

    def collect_folder(self, path, files=None):
        """把一个收歌文件夹里的新文件全部收进来。"""
        if files is None:
            files = next((view['files'] for view in self.folder_views() if view['path'] == path), [])
        if not files:
            return None
        batch = Batch('files', f'{Path(path).name} 里的 {len(files)} 个新文件', paths=list(files), watched=list(files))
        return self._enqueue(batch, 'import')

    def collect_auto_folders(self):
        """回到应用时调用：开了“自动收”的文件夹里有新文件就直接收。"""
        started = []
        for view in self.folder_views():
            if view['auto'] and view['files']:
                started.append(self.collect_folder(view['path'], view['files']))
        return started

    # ---- 调度 -----------------------------------------------------------

    def _enqueue(self, batch, action):
        self.batches.insert(0, batch)
        self._queue.append((batch, action, None))
        self.batch_added.emit(batch)
        self._pump()
        return batch

    def _pump(self):
        if self._worker is not None or not self._queue:
            return
        batch, action, argument = self._queue.pop(0)
        self._current = batch
        getattr(self, '_run_' + action)(batch, argument)

    def _launch(self, worker):
        self._worker = worker
        worker.finished.connect(self._finished)
        worker.start()

    def _finished(self):
        worker, self._worker = self._worker, None
        batch, self._current = self._current, None
        # 线程已结束：脱离父对象，交给 Python 回收。不用 deleteLater，
        # 否则父对象先销毁时，排队中的延迟删除会再删一次已经不存在的线程。
        worker.setParent(None)
        if batch is not None:
            self.batch_changed.emit(batch)
        self._pump()

    # ---- 读取链接 -------------------------------------------------------

    def _run_read(self, batch, _):
        from djtool.online import fetch_listing, normalize_link
        library, text = self.library, batch.text
        batch.state = 'reading'
        self.batch_changed.emit(batch)

        def operation(cancel):
            provider, url, kind, identifier = normalize_link(text)
            name, tracks = fetch_listing(url, cancel=cancel, library=library)
            # 某个人的上传 / 转发是“逛”的：列出来挑着收，一键收新歌时不包含。
            pick = provider == 'soundcloud' and (identifier or '').endswith(('/tracks', '/reposts'))
            return name, tracks, url, provider, kind == 'playlist', 'pick' if pick else 'all'

        worker = _Task(operation, self)
        worker.result.connect(lambda result: self._listed(batch, worker, result))
        worker.failed.connect(lambda message: self._fail(batch, message))
        self._launch(worker)

    def _listed(self, batch, worker, result):
        if worker.isInterruptionRequested():
            batch.state, batch.detail = 'failed', '已停止读取'
            return
        name, tracks, url, provider, playlist, mode = result
        try:
            # 整份歌单一次事务入队，同时用平台歌曲 ID 认出已经在曲库里的歌。
            batch.jobs = self.library.enqueue_many(tracks)
            if playlist:
                source, batch.fresh = self.library.save_link_source(
                    url, provider, name, [job['id'] for job in batch.jobs], mode)
                # 同一个歌单以前的批次（比如从粘贴的短链接读的那次）让位给这一次。
                for other in [item for item in self.batches if item is not batch
                              and item.source_id == source['id'] and not item.active()]:
                    self.remove(other)
                batch.source_id, batch.limit, name = source['id'], source['limit'], source['name']
        except (OSError, ValueError, sqlite3.Error) as error:
            self._fail(batch, f'这个链接没能保存到收歌队列：{error}')
            return
        batch.title = name if playlist else (tracks[0]['title'] if tracks else name)
        batch.playlist = playlist
        batch.state = 'ready'
        batch.detail = f'比上次多了 {batch.fresh} 首' if batch.fresh else ''
        if not batch.pending():
            batch.state = 'done'
            batch.detail = '没有新歌，全部到齐' if playlist else '这首已经在曲库里'
        elif not playlist or batch.auto:
            # 单曲不用再确认，直接下载；歌单留给用户看一眼数量再开始（一键收新歌除外）。
            self._queue.insert(0, (batch, 'download', None))
            batch.state = 'queued'
        if playlist:
            self.sources_changed.emit()

    def _fail(self, batch, message):
        batch.state, batch.detail = 'failed', message
        if batch.kind == 'link' and not batch.jobs:
            batch.title = '没能读取这个链接'

    # ---- 下载 -----------------------------------------------------------

    def _reload_jobs(self, batch):
        self.library.reconcile_online_jobs()
        current = {job['id']: job for job in self.library.jobs_for([job['id'] for job in batch.jobs])}
        batch.jobs = [current.get(job['id'], job) for job in batch.jobs]

    def _run_download(self, batch, only):
        from djtool.workers import DownloadWorker
        self._reload_jobs(batch)
        jobs = [job for job in batch.pending() if only is None or job['id'] in only]
        if not jobs:
            batch.state, batch.detail = 'done', ''
            self._current = None
            self.batch_changed.emit(batch)
            self._pump()
            return
        batch.state, batch.detail = 'running', ''
        batch.progress, batch.live = (0, len(jobs)), {}
        batch.added = batch.existing = 0
        batch.errors = []
        self.batch_changed.emit(batch)
        worker = DownloadWorker(self.library, jobs, self)
        titles = {job['id']: job['title'] for job in jobs}

        def updated(identifier, message, percent):
            batch.live[identifier] = message
            batch.detail = f'{titles.get(identifier, "")}：{message}'
            self.job_changed.emit(batch, identifier)
            self.batch_changed.emit(batch)

        def counted(counts):
            grew = counts['added'] > batch.added
            batch.progress = (counts['processed'], counts['total'])
            batch.added, batch.existing = counts['added'], counts['existing']
            self.batch_changed.emit(batch)
            if grew:
                self.library_changed.emit()

        def completed(counts):
            self._reload_jobs(batch)
            batch.live = {}
            failed = counts.get('failed', 0) + counts.get('unavailable', 0)
            left = len(batch.pending())
            parts = [f'新入库 {counts["added"]} 首']
            if counts.get('existing'):
                parts.append(f'{counts["existing"]} 首原本就有')
            if failed:
                parts.append(f'{failed} 首没拿到')
            batch.detail = '，'.join(parts)
            if counts.get('batch_error'):
                batch.state = 'stopped'
                batch.detail += '。' + counts['batch_error']
            elif counts.get('cancelled'):
                batch.state = 'stopped'
                batch.detail += '。已停止'
            else:
                batch.state = 'done' if not left else 'stopped'
            if counts.get('retry_after'):
                batch.retry_at = time.monotonic() + counts['retry_after']
            self.library_changed.emit()

        worker.updated.connect(updated)
        worker.batch_updated.connect(counted)
        worker.completed.connect(completed)
        self._launch(worker)

    # ---- 本地文件 -------------------------------------------------------

    def _run_import(self, batch, _):
        from djtool.workers import ImportWorker
        self._run_files(batch, ImportWorker(self.library, batch.paths, self))

    def _run_reference(self, batch, _):
        from djtool.workers import ReferenceWorker
        self._run_files(batch, ReferenceWorker(self.library, batch.paths[0], self))

    def _run_files(self, batch, worker):
        batch.state, batch.detail = 'running', '正在检查文件…'
        self.batch_changed.emit(batch)

        def progress(index, total, name):
            batch.progress = (index, total)
            batch.detail = name
            self.batch_changed.emit(batch)

        def completed(result):
            batch.added, batch.existing = result['added'], result['duplicates']
            batch.errors = list(result['errors'])
            done = batch.added + batch.existing + len(batch.errors)
            batch.progress = (done, done)
            parts = [f'新入库 {batch.added} 首']
            if batch.existing:
                parts.append(f'{batch.existing} 首原本就有')
            if batch.errors:
                parts.append(f'{len(batch.errors)} 个没能导入')
            batch.detail = '，'.join(parts) + ('。已停止' if result['cancelled'] else '')
            batch.state = 'stopped' if result['cancelled'] else 'failed' if batch.errors and not (batch.added or batch.existing) else 'done'
            if batch.watched and not result['cancelled']:
                # 处理过的文件（包括原本就有的和转不了的）不再提示；没处理完就留到下次。
                try:
                    self.watch().mark_seen(batch.watched)
                except (OSError, ValueError):
                    pass
            self.library_changed.emit()

        worker.progress.connect(progress)
        worker.completed.connect(completed)
        self._launch(worker)
