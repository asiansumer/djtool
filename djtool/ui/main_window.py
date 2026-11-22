"""主窗口（“唱片墙”）：没有侧栏，顶部切换收歌 / 曲库 / Sets，中间一个收歌入口，
底部一整行是试听栏，选中后的操作条悬浮在它上面。

设计时反复问的一件事：打开应用以后，怎样用最少的动作把歌收进来、听一下、排进 Set。
所以——链接在剪贴板里就直接提示读取；粘贴即开始；选中歌曲不移动版面；
拖动歌曲时目标自己浮出来；Serato 的同步和读取都在后台自动做，不需要按钮。

界面不直接写 SQL、不直接碰音频文件；数据操作交给 Library，收歌交给 IntakeQueue。
"""
from hashlib import sha256
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

from PySide6.QtCore import QPoint, QProcess, QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QInputDialog, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from djtool.intake_queue import IntakeQueue, URL
from djtool.ui import theme
from djtool.ui.covers import loader
from djtool.ui.intake_view import IntakePage
from djtool.ui.library_view import LibraryPage
from djtool.ui.player import PlayerBar
from djtool.ui.set_view import SetPage
from djtool.ui.widgets import Button, Toast, Tool, format_bpm, format_duration, format_key, label

SERATO_STATES = {
    'received': ('ok', 'Serato 已同步'),
    'ready': ('accent', '重开 Serato 后生效'),
    'waiting': ('warn', '关闭 Serato 后同步'),
    'missing': ('warn', '有文件丢失'),
    'error': ('bad', 'Serato 同步出错'),
    'empty': ('ink3', 'Serato'),
}
SET_STATES = {'synced': '已同步到 Serato', 'waiting': '关闭 Serato 后同步'}
LINK_HOSTS = ('music.163.com', 'y.music.163.com', '163cn.tv', 'soundcloud.com', 'www.soundcloud.com',
              'on.soundcloud.com', 'm.soundcloud.com')


def dot_icon(token):
    pixmap = QPixmap(20, 20)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.color(token)))
    painter.drawEllipse(2, 2, 6, 6)
    painter.end()
    return QIcon(pixmap)


def shadow(widget, blur=36, offset=10, alpha=150):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, offset)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


class SeratoWorker(QThread):
    """后台做三件事：把曲库同步成 Serato 里的「DJTOOL / 收歌」；把改动过的 Set 写成
    Serato 的列表；把 Serato 分析好的 BPM 和调性读回来。写入只在 Serato 关闭时进行。"""
    synced = Signal(dict)
    sets_synced = Signal(dict)
    analysed = Signal(dict)

    def __init__(self, library, last_signature, failed_sets, force=False, parent=None):
        super().__init__(parent)
        self.library, self.last_signature, self.force = library, last_signature, force
        self.failed_sets = dict(failed_sets)

    @staticmethod
    def signature():
        from djtool.serato_bpm import _signature, default_database
        source = default_database()
        return (_signature(source), _signature(Path(str(source) + '-wal')))

    def sync_sets(self):
        """只同步内容和上次写入不同的 Set；同一份内容失败过就不再反复重试。"""
        from djtool.serato import SeratoCrateAdapter, SeratoError
        adapter = SeratoCrateAdapter(self.library.root)
        if not adapter.native_sync_available():
            return {}
        running = adapter.process_checker()
        try:
            written = adapter._read_manifest()['crates']
        except SeratoError as error:
            return {'*': ('error', str(error), '')}
        statuses = {}
        for collection in self.library.collections('set'):
            if self.isInterruptionRequested():
                break
            identifier = collection['id']
            tracks = self.library.tracks(identifier)
            if not tracks:
                continue
            try:
                plan = adapter.preview_set(collection, tracks)
            except (SeratoError, OSError) as error:
                statuses[identifier] = ('error', str(error), '')
                continue
            digest = sha256(plan.payload).hexdigest()
            entry = written.get(plan.collection_id)
            if entry and entry['sha256'] == digest and entry['filename'] == plan.crate_path.name:
                statuses[identifier] = ('synced', '', digest)
            elif self.failed_sets.get(identifier, ('', ''))[0] == digest:
                statuses[identifier] = ('error', self.failed_sets[identifier][1], digest)
            elif running:
                statuses[identifier] = ('waiting', '', digest)
            else:
                try:
                    adapter.sync_set(collection, tracks)
                    statuses[identifier] = ('synced', '', digest)
                except (SeratoError, OSError) as error:
                    statuses[identifier] = ('error', str(error), digest)
        return statuses

    def run(self):
        from djtool.audio_tasks import AudioCancelled
        try:
            from djtool.serato_intake import sync_library
            self.synced.emit(sync_library(self.library, cancel=self.isInterruptionRequested))
        except AudioCancelled:
            return
        except Exception as error:
            self.synced.emit({'status': 'error', 'message': str(error)})
        try:
            self.sets_synced.emit(self.sync_sets())
        except Exception as error:
            self.sets_synced.emit({'*': ('error', str(error), '')})
        signature = self.signature()
        if signature == self.last_signature and not self.force:
            return
        try:
            # Serato 的数据库没变就不重读；变了才复制一份只读副本来查。
            from djtool.serato_bpm import sync_library as read_bpm
            self.analysed.emit({**read_bpm(self.library, cancel=self.isInterruptionRequested),
                                'signature': signature})
        except AudioCancelled:
            return
        except Exception as error:
            self.analysed.emit({'error': str(error), 'signature': signature})


class IntakeField(QLineEdit):
    """窗口里唯一的收歌入口：链接、文件、文件夹都从这里进。"""
    submitted = Signal(str)
    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('intake')
        self.setPlaceholderText('粘贴链接，或把 MP3、NCM、文件夹拖进来')
        self.setAcceptDrops(True)
        self.setFixedHeight(52)
        self.icon_action = self.addAction(theme.icon('download', 'ink3', 18), QLineEdit.ActionPosition.LeadingPosition)
        self.returnPressed.connect(self._submit)

    def retheme(self):
        self.icon_action.setIcon(theme.icon('download', 'ink3', 18))

    def _submit(self):
        text = self.text().strip()
        if text:
            self.clear()
            self.submitted.emit(text)

    def set_dropping(self, value):
        self.setProperty('dropping', value)
        self.style().unpolish(self)
        self.style().polish(self)

    def paste_from_clipboard(self):
        data = QApplication.clipboard().mimeData()
        files = [url.toLocalFile() for url in data.urls() if url.isLocalFile()] if data.hasUrls() else []
        if files:
            self.dropped.emit(files)
            return
        self.insert(data.text())
        # 粘贴进来的是链接就直接开始，不用再按回车。
        if URL.search(self.text()):
            self._submit()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_from_clipboard()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            self.set_dropping(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.set_dropping(False)

    def dropEvent(self, event):
        self.set_dropping(False)
        data = event.mimeData()
        files = [url.toLocalFile() for url in data.urls() if url.isLocalFile()] if data.hasUrls() else []
        if files:
            self.dropped.emit(files)
        elif data.hasText() or data.hasUrls():
            self.submitted.emit(data.text() or data.urls()[0].toString())
        event.acceptProposedAction()


class DropChip(Button):
    """拖动歌曲时浮出来的目标：落在上面就加入那个 Set。"""
    dropped = Signal(object, list)

    def __init__(self, text, collection_id):
        super().__init__(text, role='drop')
        self.collection_id = collection_id

    def accept_tracks(self, identifiers):
        self.dropped.emit(self.collection_id, list(identifiers))


class MainWindow(QMainWindow):
    def __init__(self, library_root=None, remember=True):
        super().__init__()
        self.setWindowTitle('DJTOOL')
        self.resize(1320, 880)
        self.setMinimumSize(1000, 680)
        self.remember = remember
        self.settings = QSettings('DJTOOL', 'DJTOOL') if remember else None
        self.library = None
        self.tracks = []
        self.by_id = {}
        self.collections = []
        self.current_collection = None
        self.page = 'library'
        self.serato_worker = None
        self.serato_signature = None
        self.serato_status = {'status': 'empty'}
        self.serato_note = ''
        self.set_status = {}
        self.failed_sets = {}
        self.offered_link = ''
        self.closing = False
        self.queue = IntakeQueue(parent=self)
        if remember:
            # 网易云客户端默认把歌下载到这里；存在的话就当作第一个收歌文件夹。
            self.queue.watch_defaults = (Path.home() / 'Music' / '网易云音乐',)
        self._build()
        self.retheme()
        self.queue.library_changed.connect(self.schedule_reload)
        self.queue.library_changed.connect(self.check_serato)
        self.queue.batch_added.connect(lambda _: self._update_badge())
        self.queue.batch_changed.connect(self._batch_changed)
        self.queue.batch_removed.connect(lambda _: self._update_badge())
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(400)
        self.reload_timer.timeout.connect(self.reload)
        remembered = self.settings.value('library_root', '') if self.settings else ''
        if library_root:
            self.attach_library(library_root)
        elif remembered and (Path(remembered) / '.djtool/library.sqlite3').is_file():
            try:
                self.attach_library(remembered)
            except (OSError, ValueError, sqlite3.Error) as error:
                self.notify(f'没能打开上次的曲库：{error}')
        elif remembered:
            self.notify('上次的曲库暂时打不开。检查磁盘，或在设置里重新选择。')
        self.reload()
        self.show_page('library' if self.tracks else 'intake')
        self.serato_timer = QTimer(self)
        self.serato_timer.setInterval(15000)
        self.serato_timer.timeout.connect(self.check_serato)
        # 用 --library 打开的隔离曲库不碰真实的 Serato，也不读剪贴板。
        self.serato_pill.setVisible(remember)
        if remember:
            self.serato_timer.start()
            QTimer.singleShot(2000, self.check_serato)
            QApplication.instance().applicationStateChanged.connect(self._app_state)
            QTimer.singleShot(600, self.check_clipboard)
            QTimer.singleShot(900, self.check_folders)

    # ---- 搭界面 ---------------------------------------------------------

    def _build(self):
        self.shell = QWidget()
        self.shell.setObjectName('page')
        root = QVBoxLayout(self.shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(28, 14, 24, 8)
        brand = QHBoxLayout()
        brand.setSpacing(9)
        mark = label()
        mark.setFixedSize(24, 24)
        mark.setPixmap(QIcon(str(theme.ASSETS / 'app-icon.svg')).pixmap(48, 48))
        mark.setScaledContents(True)
        brand.addWidget(mark)
        brand.addWidget(label('DJTOOL', 'wordmark'))
        brand.addStretch()
        left = QWidget()
        left.setLayout(brand)
        left.setFixedWidth(300)
        head.addWidget(left)
        head.addStretch()
        segments = QFrame()
        segments.setObjectName('segbar')
        row = QHBoxLayout(segments)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        self.nav = {}
        for key, title in (('intake', '收歌'), ('library', '曲库'), ('sets', 'Sets')):
            item = Button(title, role='seg')
            item.setCheckable(True)
            item.clicked.connect(lambda checked=False, page=key: self.show_page(page))
            self.nav[key] = item
            row.addWidget(item)
        head.addWidget(segments)
        head.addStretch()
        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addStretch()
        self.search = QLineEdit()
        self.search.setObjectName('search')
        self.search.setPlaceholderText('搜索')
        self.search.setClearButtonEnabled(True)
        self.search.setFixedSize(160, 32)
        self.search_action = self.search.addAction(theme.icon('search', 'ink3', 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._search)
        tools.addWidget(self.search)
        self.serato_pill = Button('Serato', role='pill')
        self.serato_pill.clicked.connect(self._serato_menu)
        tools.addWidget(self.serato_pill)
        settings = Tool('settings', '设置', 18)
        settings.clicked.connect(lambda: self._settings_menu(settings))
        tools.addWidget(settings)
        right = QWidget()
        right.setLayout(tools)
        right.setFixedWidth(300)
        head.addWidget(right)
        root.addLayout(head)

        slot = QVBoxLayout()
        slot.setContentsMargins(0, 2, 0, 0)
        slot.setSpacing(6)
        self.intake = IntakeField()
        self.intake.setFixedWidth(640)
        self.intake.submitted.connect(self.submit_text)
        self.intake.dropped.connect(self.submit_files)
        slot.addWidget(self.intake, 0, Qt.AlignmentFlag.AlignHCenter)
        self.suggest = Button('', 'clipboard', 'suggest')
        self.suggest.clicked.connect(self._take_suggestion)
        self.suggest.hide()
        slot.addWidget(self.suggest, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addLayout(slot)

        self.pages = QStackedWidget()
        self.intake_page = IntakePage(self.queue)
        self.intake_page.cover_of = lambda identifier: (self.by_id.get(identifier) or {}).get('cover_path')
        self.intake_page.play.connect(lambda identifier, queue: self.player.play(identifier, queue))
        self.intake_page.add_to_set.connect(self._pick_set_for)
        self.intake_page.choose_files.connect(self.choose_files)
        self.intake_page.open_library.connect(lambda: self.show_page('library'))
        self.intake_page.notice.connect(self.notify)
        self.library_page = LibraryPage(self.settings)
        self.library_page.memberships = lambda identifier: self.library.memberships(identifier) if self.library else set()
        self.library_page.play.connect(lambda identifier, queue: self.player.play(identifier, queue))
        self.library_page.add_to.connect(self.add_to_collection)
        self.library_page.edited.connect(self.edit_track)
        self.library_page.reveal.connect(self.reveal)
        self.library_page.details.connect(self.show_details)
        self.library_page.selection_changed.connect(self._selection_changed)
        self.library_page.drag_started.connect(self._drag_started)
        self.library_page.drag_finished.connect(lambda: self.tray.hide())
        self.set_page = SetPage()
        self.set_page.play.connect(lambda identifier, queue: self.player.play(identifier, queue))
        self.set_page.reorder.connect(self.reorder)
        self.set_page.remove.connect(self.remove_from_collection)
        self.set_page.note.connect(self.edit_note)
        self.set_page.rename.connect(self.rename_collection)
        self.set_page.delete.connect(self.delete_collection)
        self.set_page.create.connect(lambda: self.new_collection('set'))
        self.set_page.opened.connect(self.open_collection)
        self.set_page.back.connect(lambda: self.show_page('sets'))
        self.set_page.reveal.connect(self.reveal)
        self.set_page.details.connect(self.show_details)
        for page in (self.intake_page, self.library_page, self.set_page):
            self.pages.addWidget(page)
        root.addWidget(self.pages, 1)
        # 试听栏占满窗口底部一整行：进度条越长越好拖。
        self.player = PlayerBar(self.settings, self.shell)
        self.player.lookup = lambda identifier: self.by_id.get(identifier)
        self.player.notice.connect(self.notify)
        self.player.track_changed.connect(self._playing_changed)
        root.addWidget(self.player)
        self.setCentralWidget(self.shell)

        # 悬浮层：选中后的操作条、拖动时的 Set 目标、提示。
        self.selbar = QFrame(self.shell)
        self.selbar.setObjectName('selbar')
        bar = QHBoxLayout(self.selbar)
        bar.setContentsMargins(18, 0, 6, 0)
        bar.setSpacing(8)
        self.sel_text = label()
        bar.addWidget(self.sel_text)
        bar.addSpacing(8)
        self.sel_play = Button('试听', 'play-solid', 'outline')
        self.sel_play.clicked.connect(self._play_selection)
        bar.addWidget(self.sel_play)
        self.sel_add = Button('加入 Set', 'plus', 'primary')
        self.sel_add.clicked.connect(self._add_selection)
        bar.addWidget(self.sel_add)
        self.sel_info = Button('详情', 'info-circle', 'outline')
        self.sel_info.clicked.connect(self._info_selection)
        bar.addWidget(self.sel_info)
        clear = Tool('x', '取消选择（Esc）', 16)
        clear.clicked.connect(self.library_page.clear_selection)
        bar.addWidget(clear)
        self.selbar.setFixedHeight(46)
        shadow(self.selbar, 28, 8, 130)
        self.selbar.hide()
        self.tray = QFrame(self.shell)
        self.tray.setObjectName('tray')
        self.tray_layout = QHBoxLayout(self.tray)
        self.tray_layout.setContentsMargins(18, 8, 10, 8)
        self.tray_layout.setSpacing(8)
        shadow(self.tray, 28, 8, 130)
        self.tray.hide()
        self.toast = Toast(self.shell)
        self.setAcceptDrops(True)

        QShortcut(QKeySequence.StandardKey.Find, self, self._find)
        QShortcut(QKeySequence('Ctrl+L'), self, lambda: (self.intake.setFocus(), self.intake.selectAll()))
        QShortcut(QKeySequence.StandardKey.Open, self, self.choose_files)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.player.toggle)
        QShortcut(QKeySequence('Ctrl+I'), self, self._info_selection)
        for number, page in enumerate(('intake', 'library', 'sets'), 1):
            QShortcut(QKeySequence(f'Ctrl+{number}'), self, lambda page=page: self.show_page(page))

    def _place_overlays(self):
        width, height = self.shell.width(), self.shell.height()
        top = height - self.player.height()
        if self.selbar.isVisible():
            self.selbar.adjustSize()
            self.selbar.move((width - self.selbar.width()) // 2, top - 46 - 12)
            top = self.selbar.y()
        if self.toast.isVisible():
            self.toast.move((width - self.toast.width()) // 2, top - self.toast.height() - 12)
        if self.tray.isVisible():
            self.tray.adjustSize()
            self.tray.move((width - self.tray.width()) // 2, 66)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_overlays()

    def showEvent(self, event):
        super().showEvent(event)
        self._place_overlays()

    # ---- 曲库 -----------------------------------------------------------

    def guard(self, operation):
        try:
            return operation()
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, '这一步没有完成', str(error))
            return None

    def notify(self, message, action=None, callback=None):
        self.toast.show_message(message, action, callback)
        self._place_overlays()

    def attach_library(self, root):
        from djtool.library import Library
        if self.queue.busy() or self.serato_worker:
            raise ValueError('等后台任务结束或停止后，再切换曲库')
        self.library = Library(root)
        self.queue.library = self.library
        loader().set_library(self.library.root)
        if self.settings:
            self.settings.setValue('library_root', str(self.library.root))
        self.current_collection = None
        self.serato_signature = None
        self.set_status, self.failed_sets = {}, {}
        self.player.clear()
        self.reload()

    def ensure_library(self):
        if self.library:
            return True
        from djtool.ui.dialogs import LibraryLocationDialog
        dialog = LibraryLocationDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        return self.guard(lambda: (self.attach_library(dialog.selected_path()), True)[1]) is True

    def schedule_reload(self):
        self.reload_timer.start()

    def reload(self):
        self.tracks = self.library.tracks() if self.library else []
        self.by_id = {track['id']: track for track in self.tracks}
        self.collections = self.library.collections() if self.library else []
        if self.current_collection not in {item['id'] for item in self.collections}:
            if self.current_collection is not None and self.page == 'set':
                self.page = 'sets'
            self.current_collection = None
        self.library_page.set_tracks(self.tracks, self.collections)
        self.intake_page.set_tracks(self.tracks)
        self._refresh_sets()
        self.player.refresh_track()

    def _set_entries(self):
        entries = []
        for item in sorted(self.collections, key=lambda item: item['kind'] != 'set'):
            tracks = self.library.tracks(item['id'])
            entries.append({**item, 'duration': sum(track['duration'] for track in tracks),
                            'covers': [track['cover_path'] for track in tracks if track.get('cover_path')][:4]})
        return entries

    def _refresh_sets(self):
        if not self.library:
            self.set_page.show_wall([])
            return
        collection = next((item for item in self.collections if item['id'] == self.current_collection), None)
        if self.page == 'set' and collection:
            self.set_page.show_collection(collection, self.library.tracks(collection['id']), self._set_status_text(collection['id']))
        else:
            self.set_page.show_wall(self._set_entries())

    def _set_status_text(self, identifier):
        state = self.set_status.get(identifier)
        if not state:
            return ''
        return SET_STATES.get(state[0]) or '没能同步到 Serato：' + state[1]

    def show_page(self, page):
        self.page = page
        widget = {'intake': self.intake_page, 'library': self.library_page,
                  'sets': self.set_page, 'set': self.set_page}[page]
        self.pages.setCurrentWidget(widget)
        for key, item in self.nav.items():
            item.setChecked(key == page or (key == 'sets' and page == 'set'))
        if page in ('sets', 'set'):
            self._refresh_sets()
        if page == 'intake':
            self.intake_page.show_overview()
            self.intake_page.refresh()
            self._update_badge()
        self.selbar.setVisible(page == 'library' and bool(self.library_page.selected_tracks()))
        self.toast.hide()
        self._place_overlays()

    def open_collection(self, identifier):
        self.current_collection = identifier
        self.show_page('set')

    def _playing_changed(self, track_id):
        self.library_page.set_playing(track_id)
        self.intake_page.set_playing(track_id)
        self.set_page.set_playing(track_id)

    def _search(self, text):
        if text and self.page != 'library':
            self.show_page('library')
        self.library_page.set_search(text)

    def _find(self):
        self.search.setFocus()
        self.search.selectAll()

    def _update_badge(self):
        # 正在收的批次数；空闲时提示收歌文件夹里有几个还没收的文件。
        count = self.queue.active_count()
        waiting = self.intake_page.new_file_count()
        self.nav['intake'].setText(f'收歌 {count}' if count else f'收歌 +{waiting}' if waiting else '收歌')

    def _batch_changed(self, batch):
        self._update_badge()
        # 一批收完而人不在收歌页时，说一声，并给一个去看的入口。
        if batch.state == 'done' and batch.added and self.page != 'intake' and not getattr(batch, 'announced', False):
            batch.announced = True
            self.notify(f'「{batch.title}」收好了，新入库 {batch.added} 首', '去曲库', lambda: self.show_page('library'))

    # ---- 选中后的操作条 ---------------------------------------------------

    def _selection_changed(self, tracks):
        if not tracks:
            self.selbar.hide()
            self._place_overlays()
            return
        if len(tracks) == 1:
            track = tracks[0]
            facts = '　'.join(value for value in (format_bpm(track), format_key(track)) if value != '—')
            title = track['title'] if len(track['title']) <= 26 else track['title'][:25] + '…'
            self.sel_text.setText(title + ('　' + facts if facts else ''))
        else:
            total = format_duration(sum(track['duration'] for track in tracks))
            self.sel_text.setText(f'已选 {len(tracks)} 首　共 {total}')
        self.sel_info.setVisible(len(tracks) == 1)
        self.selbar.setVisible(self.page == 'library')
        self._place_overlays()

    def _play_selection(self):
        tracks = self.library_page.selected_tracks()
        if tracks:
            queue = [track['id'] for track in tracks] if len(tracks) > 1 else self.library_page.view().visible_ids()
            self.player.play(tracks[0]['id'], queue)

    def _add_selection(self):
        identifiers = [track['id'] for track in self.library_page.selected_tracks()]
        if identifiers:
            menu = self.library_page.set_menu(identifiers)
            menu.exec(self.sel_add.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6)))

    def _info_selection(self):
        tracks = self.library_page.selected_tracks()
        if self.page == 'library' and len(tracks) == 1:
            self.library_page.show_info(tracks[0])

    def _drag_started(self, count):
        while self.tray_layout.count():
            item = self.tray_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.tray_layout.addWidget(label(f'把这 {count} 首放进', 'muted'))
        for collection in self.collections:
            chip = DropChip(collection['name'], collection['id'])
            chip.dropped.connect(lambda identifier, tracks: self.add_to_collection(tracks, identifier))
            self.tray_layout.addWidget(chip)
        chip = DropChip('新建 Set', None)
        chip.dropped.connect(lambda identifier, tracks: self.add_to_collection(tracks, None))
        self.tray_layout.addWidget(chip)
        self.tray.show()
        self.tray.raise_()
        self._place_overlays()

    # ---- 收歌 -----------------------------------------------------------

    def submit_text(self, text):
        if not self.ensure_library():
            return
        self.offered_link = text if URL.search(text) else self.offered_link
        self.suggest.hide()
        if self.queue.submit(text) is None:
            self.notify('这里没认出链接或文件。粘贴网易云、SoundCloud 的歌曲或歌单链接，或拖入音乐文件。')
            return
        self.show_page('intake')

    def submit_files(self, paths):
        if paths and self.ensure_library():
            self.queue.add_files(paths)
            self.show_page('intake')

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择音乐文件', str(Path.home() / 'Music'),
                                                '音乐文件 (*.mp3 *.MP3 *.ncm *.NCM)')
        self.submit_files(paths)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择包含 MP3 或 NCM 的文件夹', str(Path.home() / 'Music'))
        if folder:
            self.submit_files([folder])

    def _app_state(self, state):
        if state == Qt.ApplicationState.ApplicationActive:
            self.check_clipboard()
            self.check_folders()

    def check_folders(self):
        """回到应用时看一眼收歌文件夹：开了自动收的直接收，其余的只更新数量。只读本机目录。"""
        if not self.library or self.closing:
            return
        started = [batch for batch in self.queue.collect_auto_folders() if batch]
        self.intake_page.refresh()
        self._update_badge()
        if started and self.page != 'intake':
            self.notify(f'收歌文件夹里有 {sum(len(batch.paths) for batch in started)} 个新文件，正在收进来')

    def check_clipboard(self, text=None):
        """回到应用时，如果剪贴板里正好是一个能收的链接，就在收歌栏下面提示一下；不自动联网。"""
        text = QApplication.clipboard().text() if text is None else text
        match = URL.search(text or '')
        if not match:
            return False
        url = match.group(0).rstrip('。，,')
        host = (urlparse(url).hostname or '').lower()
        if host not in LINK_HOSTS or url == self.offered_link or any(url in batch.text for batch in self.queue.batches):
            return False
        self.offered_link = url
        shown = url.split('://', 1)[1]
        self.suggest.setText('读取剪贴板里的链接　' + (shown if len(shown) <= 46 else shown[:45] + '…'))
        self.suggest.setProperty('link', url)
        self.suggest.show()
        return True

    def _take_suggestion(self):
        self.submit_text(self.suggest.property('link'))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.intake.set_dropping(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.intake.set_dropping(False)

    def dropEvent(self, event):
        self.intake.set_dropping(False)
        self.submit_files([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    # ---- Set 与分组 ------------------------------------------------------

    def new_collection(self, kind, open_it=True):
        if not self.ensure_library():
            return None
        name, accepted = QInputDialog.getText(self, '新建 Set' if kind == 'set' else '新建分组', '名字')
        if not accepted:
            return None
        identifier = self.guard(lambda: self.library.create_collection(name, kind))
        if identifier:
            self.reload()
            if open_it:
                self.open_collection(identifier)
        return identifier

    def _pick_set_for(self, identifiers, point):
        menu = QMenu(self)
        for item in self.collections:
            menu.addAction(item['name'], lambda target=item['id']: self.add_to_collection(identifiers, target))
        if self.collections:
            menu.addSeparator()
        menu.addAction('新建 Set…', lambda: self.add_to_collection(identifiers, None))
        menu.exec(point)

    def add_to_collection(self, identifiers, collection_id):
        if not self.library or not identifiers:
            return
        if collection_id is None:
            collection_id = self.new_collection('set', open_it=False)
            if not collection_id:
                return
        result = self.guard(lambda: self.library.add_members(collection_id, list(identifiers)))
        if result is None:
            return
        self.reload()
        self.check_serato()
        name = next((item['name'] for item in self.collections if item['id'] == collection_id), '')
        message = f'已把 {result["added"]} 首加入「{name}」'
        if result['existing']:
            message += f'，另外 {result["existing"]} 首原本就在里面'
        self.notify(message, '打开', lambda: self.open_collection(collection_id))

    def reorder(self, collection_id, order):
        self.guard(lambda: self.library.reorder(collection_id, order))
        self._refresh_sets()

    def remove_from_collection(self, collection_id, identifiers):
        before = self.library.tracks(collection_id)

        def remove():
            for identifier in identifiers:
                self.library.remove_member(collection_id, identifier)
            return True

        if not self.guard(remove):
            return
        self.reload()

        def undo():
            def restore():
                self.library.add_members(collection_id, list(identifiers))
                self.library.reorder(collection_id, [track['id'] for track in before])
                for track in before:
                    if track['id'] in identifiers and track.get('set_note'):
                        self.library.set_note(collection_id, track['id'], track['set_note'])
            self.guard(restore)
            self.reload()

        self.notify(f'已移除 {len(identifiers)} 首，歌曲还在曲库里', '撤销', undo)

    def edit_note(self, collection_id, track):
        text, accepted = QInputDialog.getMultiLineText(self, '衔接备注', '只保存在这个 Set 里', track.get('set_note', ''))
        if accepted:
            self.guard(lambda: self.library.set_note(collection_id, track['id'], text.strip()))
            self._refresh_sets()

    def rename_collection(self, identifier):
        current = next(item for item in self.collections if item['id'] == identifier)
        name, accepted = QInputDialog.getText(self, '重命名', '名字', text=current['name'])
        if accepted:
            self.guard(lambda: self.library.rename_collection(identifier, name))
            self.reload()

    def delete_collection(self, identifier):
        current = next(item for item in self.collections if item['id'] == identifier)
        answer = QMessageBox.question(self, '删除这个列表？', f'删除「{current["name"]}」和它的顺序、备注。\n歌曲文件和其他列表不受影响。',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.guard(lambda: self.library.delete_collection(identifier))
            self.reload()
            if self.page == 'set':
                self.show_page('sets')

    # ---- 歌曲 -----------------------------------------------------------

    def edit_track(self, identifier, values):
        if self.guard(lambda: (self.library.update_track(identifier, values), True)[1]):
            self.reload()

    def reveal(self, path):
        if Path(path).exists():
            QProcess.startDetached('/usr/bin/open', ['-R', str(path)])
        else:
            self.notify('播放文件不在原来的位置')

    def show_details(self, identifier):
        from djtool.ui.dialogs import MetadataDialog
        track = self.guard(lambda: self.library.refresh_metadata(identifier))
        if track:
            MetadataDialog(self.library, track, self).exec()

    # ---- Serato ---------------------------------------------------------

    def check_serato(self, force=False):
        if self.closing or not self.remember or not self.library or self.serato_worker:
            return
        self.serato_worker = SeratoWorker(self.library, self.serato_signature, self.failed_sets, force is True, self)
        self.serato_worker.synced.connect(self._serato_synced)
        self.serato_worker.sets_synced.connect(self._sets_synced)
        self.serato_worker.analysed.connect(self._serato_analysed)
        self.serato_worker.finished.connect(self._serato_stopped)
        self.serato_worker.start()

    def _serato_stopped(self):
        worker, self.serato_worker = self.serato_worker, None
        if worker:
            worker.setParent(None)

    def _serato_synced(self, result):
        self.serato_status = result
        self.retheme()

    def _sets_synced(self, statuses):
        changed = statuses != self.set_status
        self.set_status = statuses
        # 记住哪一版内容失败过，避免每 15 秒对同一份内容重试一次。
        self.failed_sets = {identifier: (digest, message) for identifier, (state, message, digest) in statuses.items()
                            if state == 'error' and digest}
        self.retheme()
        if changed and self.page == 'set':
            self._refresh_sets()

    def _serato_analysed(self, result):
        self.serato_signature = result.get('signature')
        if result.get('error'):
            self.serato_note = '没能读取 BPM：' + result['error']
            return
        self.serato_note = f"已从 Serato 读到 {result['matched']} 首的 BPM 和调性"
        if result.get('changed') or result.get('cleared'):
            self.schedule_reload()

    def retheme(self):
        status = self.serato_status.get('status')
        states = {state for state, _, _ in self.set_status.values()}
        if status in ('received', 'empty'):
            status = 'error' if 'error' in states else 'waiting' if 'waiting' in states else status
        token, text = SERATO_STATES.get(status, ('ink3', 'Serato'))
        self.serato_pill.setIcon(dot_icon(token))
        self.serato_pill.setText(text)
        self.search_action.setIcon(theme.icon('search', 'ink3', 16))

    def _serato_menu(self):
        from djtool.serato_intake import status_text
        menu = QMenu(self)
        lines = [status_text(self.serato_status).replace('Serato · ', ''), self.serato_status.get('message', ''),
                 self.serato_note]
        names = {item['id']: item['name'] for item in self.collections}
        for identifier, (state, message, _) in self.set_status.items():
            if state == 'error':
                lines.append(f"「{names.get(identifier, 'Set')}」没能同步：{message}")
            elif state == 'waiting':
                lines.append(f"「{names.get(identifier, 'Set')}」等 Serato 关闭后同步")
        for line in lines:
            if line:
                menu.addAction(line if len(line) <= 60 else line[:59] + '…').setEnabled(False)
        menu.addSeparator()
        menu.addAction('现在同步并读取 BPM', lambda: self._force_serato())
        menu.exec(self.serato_pill.mapToGlobal(QPoint(0, self.serato_pill.height() + 4)))

    def _force_serato(self):
        self.failed_sets = {}
        self.check_serato(force=True)

    # ---- 设置 -----------------------------------------------------------

    def _settings_menu(self, anchor):
        menu = QMenu(self)
        menu.addAction('曲库位置…', self.change_library)
        menu.addAction('新歌保存到哪里…', self.change_music_folder)
        existing = menu.addAction('把 Serato 文件夹里已有的歌登记进来', self.index_existing)
        existing.setEnabled(bool(self.library))
        menu.addAction('新建分组…', lambda: self.new_collection('group'))
        if self.library:
            menu.addAction('在 Finder 中打开音乐文件夹',
                           lambda: QProcess.startDetached('/usr/bin/open', [str(self.library.audio_dir)]))
            menu.addSeparator()
            quality = menu.addMenu('下载音质')
            try:
                from djtool.netease_backend import load_settings
                current = load_settings(self.library)
            except (OSError, ValueError):
                current = {'backend': 'site', 'quality': 'exhigh'}
            for key, name in (('exhigh', '极高（320 kbps）'), ('lossless', '无损'), ('hires', 'Hi-Res')):
                action = quality.addAction(name)
                action.setCheckable(True)
                action.setChecked(current['quality'] == key)
                action.triggered.connect(lambda checked=False, value=key, backend=current['backend']:
                                         self.guard(lambda: self._save_quality(backend, value)))
        menu.addSeparator()
        dark = theme.mode() == 'dark'
        menu.addAction('切换到浅色' if dark else '切换到深色',
                       lambda: theme.apply(QApplication.instance(), 'light' if dark else 'dark', self.settings))
        menu.exec(anchor.mapToGlobal(QPoint(anchor.width() - menu.sizeHint().width(), anchor.height() + 6)))

    def _save_quality(self, backend, quality):
        from djtool.netease_backend import save_settings
        save_settings(self.library, backend, quality)
        self.notify('之后下载的歌会用这个音质；没有这一档时自动换别的档')

    def change_library(self):
        from djtool.ui.dialogs import LibraryLocationDialog
        if self.queue.busy():
            self.notify('等收歌结束或停止后，再切换曲库')
            return
        dialog = LibraryLocationDialog(self, self.library.root if self.library else None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.guard(lambda: self.attach_library(dialog.selected_path()))

    def change_music_folder(self):
        from djtool.ui.dialogs import MusicStorageDialog
        if not self.ensure_library():
            return False
        if self.queue.busy():
            self.notify('等收歌结束或停止后，再改保存位置')
            return False
        dialog = MusicStorageDialog(self.library, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        if self.guard(lambda: (self.library.configure_music_root(dialog.selected_path()), True)[1]):
            self.reload()
            self.notify('之后的新歌会保存到这里，已有的歌不动')
            return True
        return False

    def index_existing(self):
        if not self.library.music_root and not self.change_music_folder():
            return
        root = self.library.music_root
        # 新歌放在固定的「DJTOOL Audio」子目录；登记已有音乐时读它外面那一层。
        if root.name == 'DJTOOL Audio':
            root = root.parent
        self.queue.add_reference(root)
        self.show_page('intake')

    # ---- 窗口 -----------------------------------------------------------

    def keyPressEvent(self, event):
        # 不在输入框里时，⌘V 直接把剪贴板交给收歌栏；⌘A 全选曲库。
        if event.matches(QKeySequence.StandardKey.Paste):
            self.intake.paste_from_clipboard()
        elif event.matches(QKeySequence.StandardKey.SelectAll) and self.page == 'library':
            self.library_page.select_all()
        elif event.key() == Qt.Key.Key_Escape and self.library_page.inspector.isVisible():
            self.library_page.inspector.close_panel()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closing = True
        self.serato_timer.stop()
        waiting = not self.queue.stop_all()
        if self.serato_worker:
            self.serato_worker.requestInterruption()
            waiting = True
        if waiting:
            # 等后台线程把手上的这一步做完再退出，不留半个文件。
            self.notify('正在收尾，马上关闭…')
            QTimer.singleShot(300, self.close)
            event.ignore()
            return
        self.player.player.stop()
        loader().wait()
        event.accept()
