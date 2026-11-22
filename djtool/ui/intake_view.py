"""收歌页。

设计时问的是：扩曲库这件事，怎样才最省事？答案是别让人每次都复制链接——
歌是在网易云的歌单、SoundCloud 的点赞里攒起来的，那里才是“想要的歌”的清单。
所以这一页记住的是“来源”：

- 歌单：读过一次的歌单链接都留在这里，带着现状（多少首、缺几首）。
  “一键收新歌”把所有歌单重读一遍，新加的歌直接下载。
- 文件夹：网易云客户端的下载目录这类地方，里面有没收过的 MP3 / NCM 就提示，点一下全收。
- 这一次打开应用期间做过的事（正在读、正在下、刚收完）排在最上面，占一行。
- 最底下是最近收进来的歌，点一下就能听——收完马上知道收到了什么。

单曲链接、拖进来的文件不需要管理，只在“正在收”里走一遍就进曲库。
"""
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, QRect, QSortFilterProxyModel, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QMenu, QMessageBox, QProgressBar, QScrollArea, QStackedWidget,
                               QStyle, QStyledItemDelegate, QTableView, QVBoxLayout, QWidget)

from djtool.intake_queue import JOB_LABELS, SKIP_STATES, Batch
from djtool.ui import theme
from djtool.ui.covers import loader
from djtool.ui.library_view import draw_cover
from djtool.ui.set_view import Mosaic
from djtool.ui.widgets import Button, Tool, label

JOB_ROLE = Qt.ItemDataRole.UserRole
ICONS = {'link': 'link', 'files': 'file-music', 'reference': 'folder'}
PROVIDERS = {'netease': '网易云', 'soundcloud': 'SoundCloud'}
TILE = 184


def elide(widget, text, width):
    widget.setText(widget.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, width))
    widget.setToolTip(text if widget.text() != text else '')


def batch_status(batch):
    """一个批次现在的状态，写成一句话。"""
    done, total = batch.progress
    if batch.state == 'reading':
        return '正在读取歌曲列表…'
    if batch.state == 'queued':
        return batch.detail or '排队中'
    if batch.state == 'running':
        return (f'{done} / {total}　' if total else '') + batch.detail
    if batch.kind == 'link' and batch.jobs and batch.playlist:
        pending = len(batch.pending())
        parts = [f'{len(batch.scope())} 首', f'缺 {pending} 首' if pending else '全部到齐']
        return '，'.join(parts) + ('。' + batch.detail if batch.detail else '')
    return batch.detail


class JobModel(QAbstractTableModel):
    HEADERS = ('', '标题', '艺人', '状态')

    def __init__(self, batch, parent=None):
        super().__init__(parent)
        self.batch = batch

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.batch.jobs)

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def in_scope(self, row):
        return not self.batch.limit or row < self.batch.limit

    def status(self, row):
        job = self.batch.jobs[row]
        if not self.in_scope(row) and job['state'] != 'complete':
            return '范围外'
        return self.batch.live.get(job['id']) or JOB_LABELS.get(job['state'], job['state'])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        job = self.batch.jobs[row]
        if role == JOB_ROLE:
            return job
        if role == Qt.ItemDataRole.DisplayRole:
            return (str(row + 1), job['title'], job['artist'], self.status(row))[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 3:
            return job.get('message') or None
        return None

    def refresh(self):
        self.beginResetModel()
        self.endResetModel()

    def refresh_job(self, identifier):
        for row, job in enumerate(self.batch.jobs):
            if job['id'] == identifier:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 3))
                return


class JobFilter(QSortFilterProxyModel):
    missing_only = True

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        if not self.missing_only:
            return True
        return model.in_scope(row) and model.batch.jobs[row]['state'] not in SKIP_STATES


class JobDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, theme.qcolor('selected'))
        job = index.data(JOB_ROLE)
        column = index.column()
        text = index.data() or ''
        token = 'ink'
        if column in (0, 2):
            token = 'ink3' if column == 0 else 'ink2'
        elif column == 3:
            token = ('ok' if job['state'] == 'complete' else
                     'bad' if job['state'] in ('failed', 'unavailable', 'missing_file') else
                     'accent_text' if text not in JOB_LABELS.values() and text != '范围外' else 'ink3')
        painter.setFont(theme.font(13, tabular=column == 0))
        painter.setPen(theme.qcolor(token))
        box = rect.adjusted(10, 0, -10, 0)
        align = Qt.AlignmentFlag.AlignRight if column == 0 else Qt.AlignmentFlag.AlignLeft
        painter.drawText(box, Qt.AlignmentFlag.AlignVCenter | align,
                         painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, box.width()))
        painter.restore()


class BatchRow(QFrame):
    """这一次打开应用期间做的一件事：读一个链接、下一批歌、导入一批文件。占一行。"""
    open_source = Signal(str)

    def __init__(self, batch, queue, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        self.batch, self.queue = batch, queue
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 11, 10, 11)
        outer.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.symbol = label()
        self.symbol.setFixedSize(18, 18)
        top.addWidget(self.symbol)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label('', 'cardTitle')
        self.status = label('', 'muted')
        text.addWidget(self.title)
        text.addWidget(self.status)
        top.addLayout(text, 1)
        self.action = Button('', role='primary' if batch.kind == 'link' else 'outline')
        self.action.clicked.connect(self._act)
        top.addWidget(self.action)
        self.look = Button('看歌单', role='outline')
        self.look.clicked.connect(lambda: self.open_source.emit(self.batch.source_id))
        top.addWidget(self.look)
        self.stop = Button('停止', role='outline')
        self.stop.clicked.connect(lambda: self.queue.stop(self.batch))
        top.addWidget(self.stop)
        self.close_button = Tool('x', '从这里移除', 16)
        self.close_button.clicked.connect(lambda: self.queue.remove(self.batch))
        top.addWidget(self.close_button)
        outer.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        outer.addWidget(self.bar)
        self.refresh()

    def retheme(self):
        failed = self.batch.state == 'failed'
        done = self.batch.state == 'done'
        symbol = 'alert-triangle' if failed else 'circle-check' if done else ICONS[self.batch.kind]
        self.symbol.setPixmap(theme.icon(symbol, 'bad' if failed else 'ok' if done else 'ink2', 18).pixmap(18, 18))

    def refresh(self):
        batch = self.batch
        self.retheme()
        active = batch.active()
        running = batch.state == 'running'
        self.title.setText(batch.title)
        self.status.setText(batch_status(batch))
        self.status.setVisible(bool(self.status.text()))
        pending = len(batch.pending()) if batch.kind == 'link' else 0
        cooling = batch.cooldown()
        if batch.kind == 'link':
            self.action.setVisible(bool(pending) and not active and bool(batch.jobs))
            self.action.setText(f'{cooling} 秒后可继续' if cooling else
                                f'收 {pending} 首' if batch.playlist else '再试一次')
            self.action.setEnabled(not cooling)
        else:
            self.action.setVisible(bool(batch.errors) and not active)
            self.action.setText(f'看 {len(batch.errors)} 个没收进来的原因')
        self.look.setVisible(bool(batch.source_id) and not active)
        self.stop.setVisible(active)
        self.close_button.setVisible(not active)
        done, total = batch.progress
        self.bar.setVisible(running or batch.state == 'reading')
        self.bar.setRange(0, total if running and total else 0)
        self.bar.setValue(done)

    def _act(self):
        if self.batch.kind == 'link':
            self.queue.start(self.batch)
        elif self.batch.errors:
            box = QMessageBox(self)
            box.setWindowTitle('没收进来的文件')
            box.setText(f'{len(self.batch.errors)} 个文件没能收进来，其余的已经入库。')
            box.setDetailedText('\n'.join(self.batch.errors))
            box.exec()


def source_name(view):
    return view['name'] or PROVIDERS.get(view['provider'], '') + '歌单'


def source_summary(view):
    """歌单的现状，一句话。"""
    if not view['checked_at'] and not view['total']:
        return '还没读取'
    parts = [f"{view['total']} 首"]
    if view['mode'] == 'pick':
        parts.append(f"已收 {len(view['track_ids'])} 首")
        return '，'.join(parts)
    if view['limit'] and view['limit'] < view['total']:
        parts.append(f"只收前 {view['in_scope']} 首")
    parts.append(f"缺 {view['pending']} 首" if view['pending'] else '全部到齐')
    return '，'.join(parts)


class SourceTile(QWidget):
    """一个记住的歌单：封面拼图、名字、现状，和当前最该点的那个按钮。"""
    opened = Signal(str)
    menu_requested = Signal(str, QPoint)

    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self.queue = queue
        self.view = None
        self.setFixedWidth(TILE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.mosaic = Mosaic(TILE)
        self.mosaic.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.mosaic)
        layout.addSpacing(5)
        self.name = label('', 'cardTitle')
        self.meta = label('', 'faint')
        layout.addWidget(self.name)
        layout.addWidget(self.meta)
        layout.addSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.collect = Button('', role='primary')
        self.collect.clicked.connect(lambda: self.queue.download_source(self.view['id']))
        row.addWidget(self.collect)
        self.check = Button('检查新歌', 'refresh', 'link')
        self.check.clicked.connect(lambda: self.queue.check(self.view['id']))
        row.addWidget(self.check)
        self.stop = Button('停止', role='link')
        self.stop.clicked.connect(lambda: self.view['batch'] and self.queue.stop(self.view['batch']))
        row.addWidget(self.stop)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

    def set_view(self, view, covers):
        self.view = view
        self.mosaic.set_paths(covers)
        elide(self.name, source_name(view), TILE)
        self.refresh_state()

    def refresh_state(self):
        """只更新文字和按钮；下载过程中每秒会被调用很多次，所以不碰数据库。"""
        view = self.view
        batch = self.queue.batch_for(view['id'])
        view['batch'] = batch
        active = bool(batch and batch.active())
        cooling = batch.cooldown() if batch else 0
        elide(self.meta, batch_status(batch) if active else source_summary(view), TILE)
        self.mosaic.setToolTip(PROVIDERS.get(view['provider'], '') + '　' + view['url'])
        wanted = view['pending'] and view['mode'] == 'all'
        self.collect.setVisible(bool(wanted) and not active)
        self.collect.setText(f'{cooling} 秒后可继续' if cooling else f"收 {view['pending']} 首")
        self.collect.setEnabled(not cooling)
        self.check.setVisible(not active)
        self.check.setText('' if wanted else '检查新歌')
        self.check.setToolTip('重新读一遍这个歌单，看有没有新加的歌')
        self.stop.setVisible(active)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= self.meta.geometry().bottom():
            self.opened.emit(self.view['id'])
        elif event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(self.view['id'], event.globalPosition().toPoint())


class Flow(QWidget):
    """固定宽度的格子按窗口宽度自动换行。"""

    def __init__(self, cell, gap=22, parent=None):
        super().__init__(parent)
        self.cell, self.gap = cell, gap
        self.items = []
        self.columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(gap)
        self.grid.setVerticalSpacing(gap)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def set_items(self, items):
        self.items = list(items)
        self.columns = 0
        self._reflow(self.width())

    def _reflow(self, width):
        columns = max(1, (width + self.gap) // (self.cell + self.gap))
        if columns == self.columns:
            return
        self.columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index, item in enumerate(self.items):
            self.grid.addWidget(item, index // columns, index % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow(event.size().width())


class FolderRow(QFrame):
    """一个收歌文件夹：里面有几个还没收的文件，点一下全收。"""

    def __init__(self, view, queue, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        self.view, self.queue = view, queue
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 11, 10, 11)
        layout.setSpacing(12)
        self.symbol = label()
        self.symbol.setFixedSize(18, 18)
        layout.addWidget(self.symbol)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(view['name'], 'cardTitle'))
        count = len(view['files'])
        state = ('这个文件夹现在打不开' if not view['exists'] else
                 f'有 {count} 个还没收的文件' + ('，会自动收' if view['auto'] else '') if count else
                 '没有新文件' + ('，有了会自动收' if view['auto'] else ''))
        text.addWidget(label(state, 'muted'))
        layout.addLayout(text, 1)
        if count:
            collect = Button(f'收进来 {count} 个', role='primary')
            collect.clicked.connect(lambda: self.queue.collect_folder(view['path'], view['files']))
            layout.addWidget(collect)
        more = Tool('dots', '更多', 18)
        more.clicked.connect(lambda: self._menu(more))
        layout.addWidget(more)
        self.retheme()

    def retheme(self):
        self.symbol.setPixmap(theme.icon('folder', 'ink2', 18).pixmap(18, 18))

    def _menu(self, anchor):
        path = self.view['path']
        menu = QMenu(self)
        auto = menu.addAction('有新文件时自动收')
        auto.setCheckable(True)
        auto.setChecked(self.view['auto'])
        auto.triggered.connect(lambda checked: self._change(lambda watch: watch.set_auto(path, checked)))
        menu.addAction('在访达中显示', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        menu.addSeparator()
        menu.addAction('不再看这个文件夹', lambda: self._change(lambda watch: watch.remove(path)))
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))

    def _change(self, operation):
        try:
            operation(self.queue.watch())
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, '这一步没有完成', str(error))
        self.queue.sources_changed.emit()


class RecentStrip(QWidget):
    """最近收进来的歌，一排封面；点一下就试听。"""
    play = Signal(str, list)
    SIDE, GAP = 132, 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks = []
        self.hover = -1
        self.playing_id = None
        self.setFixedHeight(self.SIDE + 48)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        loader().ready.connect(lambda _: self.update())

    def set_tracks(self, tracks):
        self.tracks = sorted(tracks, key=lambda track: track.get('created_at') or '', reverse=True)[:16]
        self.update()

    def shown(self):
        return self.tracks[:max(1, (self.width() + self.GAP) // (self.SIDE + self.GAP))]

    def index_at(self, point):
        index, offset = divmod(int(point.x()), self.SIDE + self.GAP)
        return index if offset < self.SIDE and index < len(self.shown()) else -1

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        for index, track in enumerate(self.shown()):
            left = index * (self.SIDE + self.GAP)
            cover = QRect(left, 0, self.SIDE, self.SIDE)
            draw_cover(painter, cover, track.get('cover_path'), 10)
            if index == self.hover:
                button = QRect(cover.right() - 44, cover.bottom() - 44, 34, 34)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.qcolor('accent'))
                painter.drawEllipse(button)
                theme.icon('play-solid', 'accent_ink', 18).paint(painter, button.adjusted(9, 8, -7, -8))
            painter.setFont(theme.font(13, QFont.Weight.DemiBold))
            painter.setPen(theme.qcolor('accent_text' if track['id'] == self.playing_id else 'ink'))
            line = QRect(left, self.SIDE + 8, self.SIDE, 18)
            painter.drawText(line, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(track['title'], Qt.TextElideMode.ElideRight, self.SIDE))
            painter.setFont(theme.font(12))
            painter.setPen(theme.qcolor('ink3'))
            painter.drawText(line.translated(0, 19), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(track['artist'], Qt.TextElideMode.ElideRight, self.SIDE))
        painter.end()

    def mouseMoveEvent(self, event):
        index = self.index_at(event.position())
        if index != self.hover:
            self.hover = index
            self.update()

    def leaveEvent(self, event):
        self.hover = -1
        self.update()

    def mousePressEvent(self, event):
        index = self.index_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index >= 0:
            self.play.emit(self.tracks[index]['id'], [track['id'] for track in self.tracks])


class SourceDetail(QWidget):
    """点开一个歌单：哪些已经有了、哪些还缺、哪些没拿到。"""
    back = Signal()
    play = Signal(str, list)
    add_to_set = Signal(list, QPoint)
    menu_requested = Signal(str, QPoint)

    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self.setObjectName('page')
        self.queue = queue
        self.view = None
        self.listing = Batch('link', '')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 24, 0)
        outer.setSpacing(14)
        crumb = Button('收歌', 'chevron-left', 'link', token='ink2')
        crumb.clicked.connect(self.back)
        outer.addWidget(crumb, 0, Qt.AlignmentFlag.AlignLeft)
        head = QHBoxLayout()
        head.setSpacing(20)
        self.mosaic = Mosaic(112)
        head.addWidget(self.mosaic)
        text = QVBoxLayout()
        text.setSpacing(6)
        self.title = label('', 'pageTitle')
        self.meta = label('', 'muted')
        text.addStretch()
        text.addWidget(self.title)
        text.addWidget(self.meta)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.collect = Button('', role='primary')
        self.collect.clicked.connect(lambda: self.queue.download_source(self.view['id']))
        actions.addWidget(self.collect)
        self.stop = Button('停止', role='outline')
        self.stop.clicked.connect(lambda: self.view['batch'] and self.queue.stop(self.view['batch']))
        actions.addWidget(self.stop)
        self.check = Button('检查新歌', 'refresh', 'outline')
        self.check.clicked.connect(lambda: self.queue.check(self.view['id']))
        actions.addWidget(self.check)
        self.more = Tool('dots', '更多', 18)
        self.more.clicked.connect(lambda: self.menu_requested.emit(
            self.view['id'], self.more.mapToGlobal(QPoint(0, self.more.height() + 4))))
        actions.addWidget(self.more)
        actions.addStretch()
        text.addSpacing(4)
        text.addLayout(actions)
        text.addStretch()
        head.addLayout(text, 1)
        outer.addLayout(head)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.missing_chip = Button('缺少的', role='chip')
        self.all_chip = Button('全部', role='chip')
        for chip in (self.missing_chip, self.all_chip):
            chip.setCheckable(True)
            chip.setAutoExclusive(True)
            chip.clicked.connect(self._filter)
            chips.addWidget(chip)
        self.all_chip.setChecked(True)
        chips.addStretch()
        self.hint = label('双击一首：已有的试听，缺少的单独收', 'faint')
        chips.addWidget(self.hint)
        outer.addLayout(chips)
        self.model = JobModel(self.listing, self)
        self.proxy = JobFilter(self)
        self.proxy.missing_only = False
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setItemDelegate(JobDelegate(self.table))
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.doubleClicked.connect(self._activated)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(3, 220)
        outer.addWidget(self.table, 1)

    def set_view(self, view, covers):
        first = self.view is None or self.view['id'] != view['id']
        self.view = view
        self.mosaic.set_paths(covers)
        elide(self.title, source_name(view), max(200, self.width() - 240))
        self.listing.jobs, self.listing.limit = view['jobs'], view['limit']
        self.model.refresh()
        if first:
            # 有缺的先看缺的；都到齐了就看全部。
            self.missing_chip.setChecked(bool(view['pending']))
            self.all_chip.setChecked(not view['pending'])
            self._filter()
        self.refresh_state()

    def refresh_state(self):
        view = self.view
        if view is None:
            return
        batch = self.queue.batch_for(view['id'])
        view['batch'] = batch
        self.listing.live = batch.live if batch else {}
        active = bool(batch and batch.active())
        cooling = batch.cooldown() if batch else 0
        self.meta.setText(batch_status(batch) if active else
                          PROVIDERS.get(view['provider'], '') + '　' + source_summary(view))
        self.collect.setVisible(bool(view['pending']) and not active)
        self.collect.setText(f'{cooling} 秒后可继续' if cooling else f"收缺少的 {view['pending']} 首")
        self.collect.setEnabled(not cooling)
        self.stop.setVisible(active)
        self.check.setVisible(not active)
        self.missing_chip.setText(f"缺少的 {view['pending']}")
        self.all_chip.setText(f"全部 {view['total']}")

    def refresh_job(self, identifier):
        batch = self.queue.batch_for(self.view['id']) if self.view else None
        if batch:
            self.listing.live = batch.live
            self.model.refresh_job(identifier)

    def _filter(self):
        self.proxy.missing_only = self.missing_chip.isChecked()
        self.proxy.invalidate()

    def _activated(self, index):
        job = index.data(JOB_ROLE)
        if job['state'] == 'complete' and job.get('track_id'):
            self.play.emit(job['track_id'], self.view['track_ids'])
        elif job['state'] not in SKIP_STATES:
            self.queue.download_source(self.view['id'], [job])


class IntakePage(QWidget):
    play = Signal(str, list)
    add_to_set = Signal(list, QPoint)
    choose_files = Signal()
    open_library = Signal()
    notice = Signal(str)

    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self.setObjectName('page')
        self.queue = queue
        self.cover_of = lambda track_id: None
        self.tracks = []
        self.rows = {}
        self.tiles = {}
        self.folders = []
        self.views = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        overview = QWidget()
        overview.setObjectName('page')
        outer = QVBoxLayout(overview)
        outer.setContentsMargins(0, 18, 0, 0)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.setContentsMargins(28, 0, 24, 0)
        head.setSpacing(8)
        head.addWidget(label('收歌', 'pageTitle'))
        head.addStretch()
        files = Button('选择文件', 'file-music', 'outline')
        files.clicked.connect(self.choose_files)
        head.addWidget(files)
        self.sync = Button('一键收新歌', 'refresh', 'primary')
        self.sync.setToolTip('把下面每个歌单重新读一遍，新加的歌直接收下来')
        self.sync.clicked.connect(self._sync_all)
        head.addWidget(self.sync)
        outer.addLayout(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        host.setObjectName('page')
        self.body = QVBoxLayout(host)
        self.body.setContentsMargins(28, 2, 24, 28)
        self.body.setSpacing(10)

        self.activity = QVBoxLayout()
        self.activity.setSpacing(8)
        self.body.addLayout(self.activity)

        self.empty = QFrame()
        self.empty.setObjectName('dropzone')
        self.empty.setMinimumHeight(230)
        empty = QVBoxLayout(self.empty)
        empty.setSpacing(10)
        empty.addStretch()
        self.empty_icon = label()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_icon)
        headline = label('把歌丢进来', 'hero')
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = label('粘贴网易云或 SoundCloud 的歌单链接，它会被记在这里，以后点一下就能收新加的歌。\n'
                     '也可以把 MP3、NCM、整个文件夹拖进窗口；NCM 会转成 MP3。', 'muted')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(headline)
        empty.addWidget(hint)
        empty.addStretch()
        self.body.addWidget(self.empty)

        self.sources_title = self._section('歌单', '粘贴过的歌单链接都记在这里')
        self.flow = Flow(TILE)
        self.body.addWidget(self.flow)

        folders_head = QHBoxLayout()
        self.folders_title = label('文件夹', 'sectionTitle')
        folders_head.addWidget(self.folders_title)
        folders_head.addSpacing(10)
        folders_head.addWidget(label('下载到这些文件夹里的 MP3、NCM，点一下全部收进来', 'faint'), 0, Qt.AlignmentFlag.AlignBottom)
        folders_head.addStretch()
        add_folder = Button('添加文件夹', 'plus', 'link')
        add_folder.clicked.connect(self._add_folder)
        folders_head.addWidget(add_folder)
        folders_head.setContentsMargins(0, 22, 0, 2)
        self.body.addLayout(folders_head)
        self.folder_list = QVBoxLayout()
        self.folder_list.setSpacing(8)
        self.body.addLayout(self.folder_list)
        self.no_folders = label('还没有收歌文件夹。把常用的下载目录加进来，以后不用再拖文件。', 'muted')
        self.body.addWidget(self.no_folders)

        recent_head = QHBoxLayout()
        self.recent_title = label('最近收进来的', 'sectionTitle')
        recent_head.addWidget(self.recent_title)
        recent_head.addStretch()
        self.recent_more = Button('去曲库看全部', 'chevron-right', 'link')
        self.recent_more.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.recent_more.clicked.connect(self.open_library)
        recent_head.addWidget(self.recent_more)
        recent_head.setContentsMargins(0, 22, 0, 4)
        self.body.addLayout(recent_head)
        self.strip = RecentStrip()
        self.strip.play.connect(self.play)
        self.body.addWidget(self.strip)
        self.body.addStretch()
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)
        self.stack.addWidget(overview)

        self.detail = SourceDetail(queue)
        self.detail.back.connect(self.show_overview)
        self.detail.play.connect(self.play)
        self.detail.menu_requested.connect(self._source_menu)
        self.stack.addWidget(self.detail)

        # 下载时信号很密；重读数据库的完整刷新合并到 250 ms 一次。
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        queue.batch_added.connect(self._added)
        queue.batch_changed.connect(self._changed)
        queue.batch_removed.connect(self._removed)
        queue.job_changed.connect(self._job_changed)
        queue.sources_changed.connect(self.timer.start)
        queue.library_changed.connect(self.timer.start)
        self.retheme()
        self.refresh()

    def _section(self, title, hint):
        row = QHBoxLayout()
        heading = label(title, 'sectionTitle')
        row.addWidget(heading)
        row.addSpacing(10)
        note = label(hint, 'faint')
        row.addWidget(note, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        row.setContentsMargins(0, 14, 0, 6)
        self.body.addLayout(row)
        heading.note = note
        return heading

    def retheme(self):
        self.empty_icon.setPixmap(theme.icon('download', 'ink3', 40).pixmap(40, 40))

    # ---- 数据 -----------------------------------------------------------

    def set_tracks(self, tracks):
        self.tracks = tracks
        self.strip.set_tracks(tracks)
        self.timer.start()

    def set_playing(self, track_id):
        self.strip.playing_id = track_id
        self.strip.update()

    def covers(self, view):
        return [path for path in (self.cover_of(track_id) for track_id in view['track_ids'][:12]) if path][:4]

    def refresh(self):
        """重读歌单和文件夹的现状并更新版面；不联网。"""
        views = self.queue.source_views()
        self.views = {view['id']: view for view in views}
        for identifier in [key for key in self.tiles if key not in self.views]:
            tile = self.tiles.pop(identifier)
            tile.hide()
            tile.deleteLater()
        for view in views:
            tile = self.tiles.get(view['id'])
            if tile is None:
                tile = self.tiles[view['id']] = SourceTile(self.queue)
                tile.opened.connect(self.open_source)
                tile.menu_requested.connect(self._source_menu)
            tile.set_view(view, self.covers(view))
        self.flow.set_items([self.tiles[view['id']] for view in views])
        self.sources_title.setVisible(bool(views))
        self.sources_title.note.setVisible(bool(views))
        self.flow.setVisible(bool(views))
        self.sync.setVisible(any(view['mode'] == 'all' for view in views))

        while self.folder_list.count():
            item = self.folder_list.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.folders = self.queue.folder_views()
        for view in self.folders:
            self.folder_list.addWidget(FolderRow(view, self.queue))
        self.no_folders.setVisible(not self.folders)

        has_tracks = bool(self.tracks)
        self.recent_title.setVisible(has_tracks)
        self.recent_more.setVisible(has_tracks)
        self.strip.setVisible(has_tracks)
        self.empty.setVisible(not views and not self.rows)
        if self.stack.currentWidget() is self.detail:
            view = self.views.get(self.detail.view['id']) if self.detail.view else None
            if view:
                self.detail.set_view(view, self.covers(view))
            else:
                self.show_overview()

    def new_file_count(self):
        return sum(len(view['files']) for view in self.folders)

    # ---- 歌单 -----------------------------------------------------------

    def open_source(self, identifier):
        view = self.views.get(identifier)
        if view:
            self.detail.view = None
            self.detail.set_view(view, self.covers(view))
            self.stack.setCurrentWidget(self.detail)

    def show_overview(self):
        self.stack.setCurrentIndex(0)

    def _sync_all(self):
        count = self.queue.sync_all()
        self.notice.emit(f'正在检查 {count} 个歌单，新加的歌会直接收下来' if count else '还没有可以检查的歌单')

    def _source_menu(self, identifier, point):
        view = self.views.get(identifier)
        if not view:
            return
        menu = QMenu(self)
        menu.addAction('检查新歌', lambda: self.queue.check(identifier))
        if view['total']:
            menu.addAction('只收前几首…', lambda: self._limit(view))
        include = menu.addAction('一键收新歌时包含它')
        include.setCheckable(True)
        include.setChecked(view['mode'] == 'all')
        include.triggered.connect(lambda checked: self.queue.set_mode(identifier, 'all' if checked else 'pick'))
        if view['track_ids']:
            menu.addAction(f"把已收的 {len(view['track_ids'])} 首加入 Set…",
                           lambda: self.add_to_set.emit(list(dict.fromkeys(view['track_ids'])), point))
        menu.addAction('在浏览器里打开', lambda: QDesktopServices.openUrl(QUrl(view['url'])))
        menu.addSeparator()
        menu.addAction('不再记住这个歌单', lambda: self._forget(view))
        menu.exec(point)

    def _limit(self, view):
        value, accepted = QInputDialog.getInt(
            self, '只收前几首', '按歌单原来的顺序，只收前多少首（0 表示全部）。\n前 N 首里已经有的歌也算在内。',
            view['limit'], 0, max(1, view['total']))
        if accepted:
            try:
                self.queue.set_limit(view['id'], value)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, '没能保存这个范围', str(error))

    def _forget(self, view):
        if not self.queue.remove_source(view['id']):
            self.notice.emit('这个歌单正在收歌，先停止再移除')
            return
        self.notice.emit(f'不再记住「{source_name(view)}」；已经收进来的歌都还在')

    # ---- 文件夹 ---------------------------------------------------------

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择一个经常下载音乐的文件夹', str(Path.home() / 'Music'))
        if not folder or not self.queue.library:
            return
        try:
            self.queue.watch().add(folder)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, '没能添加这个文件夹', str(error))
        self.refresh()

    # ---- 这一次的批次 ---------------------------------------------------

    def _added(self, batch):
        row = BatchRow(batch, self.queue)
        row.open_source.connect(self.open_source)
        self.rows[batch.id] = row
        self.activity.insertWidget(0, row)
        self.empty.hide()

    def _changed(self, batch):
        row = self.rows.get(batch.id)
        if row:
            row.refresh()
        tile = self.tiles.get(batch.source_id)
        if tile:
            tile.refresh_state()
        if self.detail.view and self.detail.view['id'] == batch.source_id:
            self.detail.refresh_state()
        if not batch.active():
            self.timer.start()

    def _removed(self, batch):
        row = self.rows.pop(batch.id, None)
        if row:
            row.hide()
            row.deleteLater()
        self.timer.start()

    def _job_changed(self, batch, identifier):
        if self.detail.view and self.detail.view['id'] == batch.source_id:
            self.detail.refresh_job(identifier)
