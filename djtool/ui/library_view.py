"""曲库页：默认是一面封面墙，可以切成列表；两种视图共用同一份数据、排序和筛选。

刷新数据时不重建控件，滚动位置和选择都保留；文件是否存在在装载数据时一次查完，
封面在后台线程里准备，绘制每一格时不碰磁盘。
选中歌曲不会让版面移动：操作在主窗口底部的悬浮条里，详情是从右侧盖上来的面板。
"""
import os

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QPoint, QRect, QRectF, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLineEdit, QListView, QMenu, QStackedWidget, QStyle,
                               QStyledItemDelegate, QTableView, QVBoxLayout, QWidget)

from djtool.ui import theme
from djtool.ui.covers import loader
from djtool.ui.widgets import (Button, Tool, format_added, format_bpm, format_duration, format_key,
                               is_recent, label, rule)

TRACK_ROLE = Qt.ItemDataRole.UserRole
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_HEIGHT = 40
SORTS = (('最近添加', 5, Qt.SortOrder.DescendingOrder), ('BPM', 2, Qt.SortOrder.AscendingOrder),
         ('调', 3, Qt.SortOrder.AscendingOrder), ('艺人', 1, Qt.SortOrder.AscendingOrder),
         ('标题', 0, Qt.SortOrder.AscendingOrder))


def draw_cover(painter, rect, path, radius, small=None):
    """画一张圆角封面；图还没准备好时画一块安静的底色。"""
    pixmap = loader().small_pixmap(path, small) if small else loader().large_pixmap(path)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(rect), radius, radius)
    painter.save()
    painter.setClipPath(clip)
    if pixmap is None:
        painter.fillRect(rect, theme.qcolor('cover'))
        if not path or path in loader().failed:
            side = max(12, min(rect.width(), rect.height()) // 3)
            theme.icon('music', 'ink3', 24).paint(painter, QRect(rect.center().x() - side // 2,
                                                                 rect.center().y() - side // 2, side, side))
    else:
        painter.drawPixmap(rect, pixmap)
    painter.restore()


class TrackModel(QAbstractTableModel):
    HEADERS = ('标题', '艺人', 'BPM', '调', '时长', '添加')
    RIGHT = (2, 4, 5)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks = []
        self.missing = set()
        self.playing_id = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        track = self.tracks[index.row()]
        column = index.column()
        if role == TRACK_ROLE:
            return track
        if role == Qt.ItemDataRole.DisplayRole:
            return (track['title'], track['artist'], format_bpm(track), format_key(track),
                    format_duration(track['duration']), format_added(track.get('created_at')))[column]
        if role == SORT_ROLE:
            return (track['title'].casefold(), track['artist'].casefold(), track.get('bpm_serato'),
                    (track.get('key_serato') or '').casefold() or None, track['duration'],
                    track.get('created_at') or '')[column]
        if role == Qt.ItemDataRole.ToolTipRole:
            if track['id'] in self.missing:
                return '播放文件不在原来的位置'
            return f"{track['title']}\n{track['artist']}"
        return None

    def set_tracks(self, tracks):
        """同一批歌只更新内容；歌曲增减或换序才整体重置。"""
        missing = {track['id'] for track in tracks if not os.path.isfile(track['path'])}
        same = [t['id'] for t in tracks] == [t['id'] for t in self.tracks]
        if same:
            self.tracks, self.missing = list(tracks), missing
            if tracks:
                self.dataChanged.emit(self.index(0, 0), self.index(len(tracks) - 1, self.columnCount() - 1))
            return False
        self.beginResetModel()
        self.tracks, self.missing = list(tracks), missing
        self.endResetModel()
        return True

    def set_playing(self, track_id):
        if track_id != self.playing_id:
            self.playing_id = track_id
            if self.tracks:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.tracks) - 1, 0))


class TrackFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ''
        self.recent_only = False
        self.genre = ''
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def configure(self, **values):
        for key, value in values.items():
            setattr(self, key, value)
        self.invalidate()

    def filterAcceptsRow(self, row, parent):
        track = self.sourceModel().tracks[row]
        if self.recent_only and not is_recent(track.get('created_at')):
            return False
        if self.genre and track.get('genre') != self.genre:
            return False
        if self.text:
            # 文件名只取主干，否则搜 “3” 会因为扩展名 mp3 命中每一首。
            stem = os.path.splitext(track.get('filename') or '')[0]
            haystack = ' '.join([stem, *(str(track.get(key) or '') for key in
                                         ('title', 'artist', 'album', 'genre', 'note'))]).casefold()
            return all(part in haystack for part in self.text.split())
        return True

    def lessThan(self, left, right):
        a, b = left.data(SORT_ROLE), right.data(SORT_ROLE)
        # 没有数值的歌（例如 Serato 还没分析）无论升序降序都排在最后。
        if a is None or b is None:
            if a is None and b is None:
                return False
            return (a is None) == (self.sortOrder() == Qt.SortOrder.DescendingOrder)
        return a < b


class TrackViewMixin:
    """封面墙和列表共用的行为：选择、悬停、拖到 Set 上（不走系统拖放循环）。"""

    def _init_track_view(self):
        self.hover_row = -1
        self._press = None
        self._dragging = False
        self._target = None
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.doubleClicked.connect(lambda index: self.activated_track.emit(index.data(TRACK_ROLE)['id']))

    def source_model(self):
        return self.model().sourceModel()

    def visible_ids(self):
        proxy = self.model()
        return [proxy.index(row, 0).data(TRACK_ROLE)['id'] for row in range(proxy.rowCount())]

    def selected_tracks(self):
        rows = sorted({index.row() for index in self.selectionModel().selectedIndexes()})
        return [self.model().index(row, 0).data(TRACK_ROLE) for row in rows]

    def selected_ids(self):
        return [track['id'] for track in self.selected_tracks()]

    def select_ids(self, identifiers):
        selection = self.selectionModel()
        selection.clearSelection()
        if not identifiers:
            return
        flags = selection.SelectionFlag.Select | selection.SelectionFlag.Rows
        proxy = self.model()
        for row in range(proxy.rowCount()):
            index = proxy.index(row, 0)
            if index.data(TRACK_ROLE)['id'] in identifiers:
                selection.select(index, flags)

    def _set_hover(self, row):
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        plain = event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
        self._press = event.position().toPoint() if plain and index.isValid() else None
        self._dragging = False
        if plain and not index.isValid():
            self.clearSelection()
        # 在已选中的多项上按下时先不改选择，这样整批可以一起拖走。
        if self._press is not None and len(self.selected_ids()) > 1 and self.selectionModel().isSelected(index):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._dragging or (point - self._press).manhattanLength() >= QApplication.startDragDistance():
                if not self._dragging:
                    self._dragging = True
                    self.viewport().setCursor(Qt.CursorShape.DragCopyCursor)
                    self.drag_started.emit(len(self.selected_ids()))
                self._hover_target(self._target_at(event.globalPosition().toPoint()))
                event.accept()
                return
        index = self.indexAt(point)
        self._set_hover(index.row() if index.isValid() else -1)
        if not event.buttons():
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            target = self._target_at(event.globalPosition().toPoint())
            identifiers = self.selected_ids()
            self._hover_target(None)
            self.viewport().unsetCursor()
            self._press, self._dragging = None, False
            self.drag_finished.emit()
            if target is not None:
                target.accept_tracks(identifiers)
            event.accept()
            return
        index = self.indexAt(event.position().toPoint())
        if self._press is not None and index.isValid():
            if self.play_hit(index, event.position().toPoint()):
                self._press = None
                self.activated_track.emit(index.data(TRACK_ROLE)['id'])
                event.accept()
                return
            if len(self.selected_ids()) > 1:
                # 只是点了一下（没有拖），按普通单击处理：改为只选这一首。
                self.selectionModel().select(index, self.selectionModel().SelectionFlag.ClearAndSelect |
                                             self.selectionModel().SelectionFlag.Rows)
        self._press = None
        super().mouseReleaseEvent(event)

    def play_hit(self, index, point):
        return False

    def leaveEvent(self, event):
        self._set_hover(-1)
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.activated_track.emit(self.currentIndex().data(TRACK_ROLE)['id'])
        elif event.key() == Qt.Key.Key_Escape:
            self.clearSelection()
        else:
            super().keyPressEvent(event)

    def _target_at(self, point):
        widget = QApplication.widgetAt(point)
        while widget is not None and not hasattr(widget, 'accept_tracks'):
            widget = widget.parentWidget()
        return widget

    def _hover_target(self, target):
        if target is self._target:
            return
        for widget, value in ((self._target, False), (target, True)):
            if widget is not None:
                widget.setProperty('dropping', value)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
        self._target = target


class TableDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        view = self.parent()
        track = index.data(TRACK_ROLE)
        source = view.source_model()
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        if selected:
            painter.fillRect(rect, theme.qcolor('selected'))
        elif index.row() == view.hover_row:
            painter.fillRect(rect, theme.qcolor('hover'))
        column = index.column()
        missing = track['id'] in source.missing
        playing = track['id'] == source.playing_id
        if column == 0:
            if selected:
                painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), theme.qcolor('accent'))
            art = QRect(rect.left() + 28, rect.top() + (rect.height() - 28) // 2, 28, 28)
            draw_cover(painter, art, track.get('cover_path'), 4, small=28)
            text = QRect(art.right() + 13, rect.top(), rect.right() - art.right() - 23, rect.height())
            if missing:
                theme.icon('alert-triangle', 'bad', 14).paint(painter, QRect(text.left(), rect.center().y() - 7, 14, 14))
                text.setLeft(text.left() + 20)
            painter.setFont(theme.font(13, QFont.Weight.DemiBold if playing else QFont.Weight.Medium))
            painter.setPen(theme.qcolor('accent_text' if playing else 'ink3' if missing else 'ink'))
            painter.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(index.data(), Qt.TextElideMode.ElideRight, text.width()))
        else:
            right = column in TrackModel.RIGHT
            painter.setFont(theme.font(13, tabular=right))
            painter.setPen(theme.qcolor('ink' if column == 2 and index.data() != '—' else 'ink3' if column == 5 else 'ink2'))
            text = rect.adjusted(10, 0, -28 if column == 5 else -10, 0)
            painter.drawText(text, Qt.AlignmentFlag.AlignVCenter |
                             (Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft),
                             painter.fontMetrics().elidedText(index.data() or '', Qt.TextElideMode.ElideRight, text.width()))
        painter.restore()


class SortHeader(QHeaderView):
    """表头自己画排序箭头，位置跟着列的对齐方向。"""

    def __init__(self, table):
        super().__init__(Qt.Orientation.Horizontal, table)
        self.setSectionsClickable(True)

    def paintSection(self, painter, rect, index):
        painter.save()
        active = index == self.sortIndicatorSection()
        arrow = ' ↓' if self.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder else ' ↑'
        painter.fillRect(rect, theme.qcolor('window'))
        painter.setPen(theme.qcolor('line'))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.setFont(theme.font(12, QFont.Weight.DemiBold if active else QFont.Weight.Normal))
        painter.setPen(theme.qcolor('accent_text' if active else 'ink3'))
        text = TrackModel.HEADERS[index] + (arrow if active else '')
        box = rect.adjusted(28 if index == 0 else 10, 0, -28 if index == 5 else -10, 0)
        painter.drawText(box, Qt.AlignmentFlag.AlignVCenter |
                         (Qt.AlignmentFlag.AlignRight if index in TrackModel.RIGHT else Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


class TrackTable(TrackViewMixin, QTableView):
    """列表视图：一屏能看得多，点表头排序。"""
    activated_track = Signal(str)
    selection_changed = Signal()
    drag_started = Signal(int)
    drag_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model_ = TrackModel(self)
        self.proxy = TrackFilter(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)
        self._init_track_view()
        self.setItemDelegate(TableDelegate(self))
        self.setShowGrid(False)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header = SortHeader(self)
        self.setHorizontalHeader(header)
        header.setHighlightSections(False)
        header.setFixedHeight(32)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 240), (2, 72), (3, 60), (4, 68), (5, 112)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive if column == 1
                                        else QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(column, width)
        header.setSortIndicatorShown(False)
        header.setSortIndicator(5, Qt.SortOrder.DescendingOrder)
        self.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit())

    def source_model(self):
        return self.model_

    def set_tracks(self, tracks):
        selected = set(self.selected_ids())
        scroll = self.verticalScrollBar().value()
        if self.model_.set_tracks(tracks):
            self.select_ids(selected)
            self.verticalScrollBar().setValue(scroll)
            return True
        return False


class WallDelegate(QStyledItemDelegate):
    PAD = 10
    TEXT = 56

    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def cover_rect(self, cell):
        side = cell.width() - 2 * self.PAD
        return QRect(cell.left() + self.PAD, cell.top() + 8, side, side)

    def play_rect(self, cell):
        cover = self.cover_rect(cell)
        return QRect(cover.right() - 46, cover.bottom() - 46, 36, 36)

    def paint(self, painter, option, index):
        view = self.parent()
        track = index.data(TRACK_ROLE)
        source = view.source_model()
        cell = option.rect
        cover = self.cover_rect(cell)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = index.row() == view.hover_row
        playing = track['id'] == source.playing_id
        missing = track['id'] in source.missing
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        draw_cover(painter, cover, track.get('cover_path'), 10)
        if missing:
            painter.setBrush(theme.qcolor('window', 170))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(cover, 10, 10)
            theme.icon('alert-triangle', 'bad', 24).paint(painter, QRect(cover.center().x() - 12, cover.center().y() - 12, 24, 24))
        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor('accent'), 3))
            painter.drawRoundedRect(QRectF(cover).adjusted(-4.5, -4.5, 4.5, 4.5), 13, 13)
        if hovered and not missing:
            button = self.play_rect(cell)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor('accent'))
            painter.drawEllipse(button)
            theme.icon('play-solid', 'accent_ink', 18).paint(painter, button.adjusted(10, 9, -8, -9))
        text = QRect(cover.left(), cover.bottom() + 9, cover.width(), 20)
        painter.setFont(theme.font(14, QFont.Weight.Bold))
        painter.setPen(theme.qcolor('accent_text' if playing else 'ink'))
        painter.drawText(text, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(track['title'], Qt.TextElideMode.ElideRight, text.width()))
        facts = ' · '.join(value for value in (format_bpm(track), format_key(track)) if value != '—')
        painter.setFont(theme.font(12, tabular=True))
        width = painter.fontMetrics().horizontalAdvance(facts) if facts else 0
        line = QRect(cover.left(), text.bottom() + 1, cover.width(), 18)
        painter.setPen(theme.qcolor('ink2'))
        painter.drawText(line, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, facts)
        painter.setFont(theme.font(12))
        painter.setPen(theme.qcolor('ink3'))
        room = line.width() - width - (10 if facts else 0)
        painter.drawText(line, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(track['artist'], Qt.TextElideMode.ElideRight, room))
        painter.restore()


class WallView(TrackViewMixin, QListView):
    """封面墙：一格一首歌，悬停时右下角出现播放键，双击也能播放。"""
    activated_track = Signal(str)
    selection_changed = Signal()
    drag_started = Signal(int)
    drag_finished = Signal()
    MARGIN = 18

    def __init__(self, proxy, parent=None):
        super().__init__(parent)
        self.setModel(proxy)
        self._init_track_view()
        self.delegate = WallDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setWrapping(True)
        self.setSpacing(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportMargins(self.MARGIN, 0, self.MARGIN - 10, 0)
        self.verticalScrollBar().setSingleStep(36)
        self.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit())
        self._fit()

    def _fit(self, width=None):
        # 列数跟着窗口宽度走，每一格尽量接近 190 点；格子宽度取整，墙面两侧对齐。
        # Qt 排格子时会在可用宽度里再给滚动条留一段（即使滚动条已经占了位置），
        # 这里先把它减掉，否则算出来的最后一列会被挤到下一行。
        reserve = max(self.verticalScrollBar().width(), self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)) + 2
        width = max(360, (self.viewport().width() if width is None else width) - reserve)
        columns = max(3, round(width / 196))
        cell = width // columns
        size = QSize(cell, cell - 2 * WallDelegate.PAD + 8 + WallDelegate.TEXT)
        if size != self.gridSize():
            self.setGridSize(size)

    def resizeEvent(self, event):
        # 滚动条出现或消失会改变可用宽度；用事件里的新宽度重算，才不会少排一列。
        self._fit(event.size().width())
        super().resizeEvent(event)

    def play_hit(self, index, point):
        return self.delegate.play_rect(self.visualRect(index)).contains(point)


class Inspector(QFrame):
    """一首歌的详情，从右侧盖在曲库上面；可以直接改标题、艺人、风格。"""
    edited = Signal(str, dict)
    reveal = Signal(str)
    details = Signal(str)
    closed = Signal()

    WIDTH = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('inspector')
        self.setFixedWidth(self.WIDTH)
        self.track = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addStretch()
        close = Tool('x', '关闭详情（Esc）', 16)
        close.clicked.connect(self.close_panel)
        top.addWidget(close)
        layout.addLayout(top)
        self.art = label()
        self.art.setFixedSize(self.WIDTH - 36, self.WIDTH - 36)
        layout.addWidget(self.art)
        self.title = QLineEdit()
        self.title.setObjectName('flatTitle')
        self.title.editingFinished.connect(lambda: self._commit('title', self.title.text().strip()))
        layout.addWidget(self.title)
        self.artist = QLineEdit()
        self.artist.setObjectName('flat')
        self.artist.editingFinished.connect(lambda: self._commit('artist', self.artist.text().strip() or '未知艺人'))
        layout.addWidget(self.artist)
        layout.addWidget(rule())
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(7)
        self.facts = {}
        for row, (key, name) in enumerate((('bpm', 'BPM'), ('key', '调'), ('duration', '时长'),
                                           ('format', '格式'), ('added', '添加'))):
            grid.addWidget(label(name, 'faint'), row, 0)
            self.facts[key] = label()
            self.facts[key].setContentsMargins(6, 0, 0, 0)
            grid.addWidget(self.facts[key], row, 1)
        grid.addWidget(label('风格', 'faint'), 5, 0)
        self.genre = QLineEdit()
        self.genre.setObjectName('flat')
        self.genre.setPlaceholderText('未填写')
        self.genre.editingFinished.connect(lambda: self._commit('genre', self.genre.text().strip()))
        grid.addWidget(self.genre, 5, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.sets = label('', 'muted', wrap=True)
        layout.addWidget(self.sets)
        layout.addStretch()
        row = QHBoxLayout()
        finder = Button('在 Finder 中显示', role='link')
        finder.clicked.connect(lambda: self.track and self.reveal.emit(self.track['path']))
        more = Button('完整信息', role='link')
        more.clicked.connect(lambda: self.track and self.details.emit(self.track['id']))
        row.addWidget(finder)
        row.addStretch()
        row.addWidget(more)
        layout.addLayout(row)
        self.hide()

    def _commit(self, field, value):
        if self.track and value != (self.track.get(field) or '') and (value or field == 'genre'):
            self.edited.emit(self.track['id'], {field: value})

    def close_panel(self):
        self.hide()
        self.closed.emit()

    def show_track(self, track, set_names=()):
        self.track = track
        self.art.setPixmap(theme.cover(track.get('cover_path'), self.WIDTH - 36))
        self.title.setText(track['title'])
        self.title.setCursorPosition(0)
        self.artist.setText(track['artist'])
        self.artist.setCursorPosition(0)
        self.genre.setText(track.get('genre') or '')
        bitrate = f" {round(track['bitrate'] / 1000)} kbps" if track.get('bitrate') else ''
        for key, value in (('bpm', format_bpm(track)), ('key', format_key(track)),
                           ('duration', format_duration(track['duration'])),
                           ('format', (track.get('codec') or 'MP3') + bitrate),
                           ('added', format_added(track.get('created_at')))):
            self.facts[key].setText(value)
        self.sets.setText('在 ' + '、'.join(set_names) + ' 里' if set_names else '还没有加入任何 Set')
        self.show()
        self.raise_()


class LibraryPage(QWidget):
    play = Signal(str, list)
    add_to = Signal(list, object)
    edited = Signal(str, dict)
    reveal = Signal(str)
    details = Signal(str)
    selection_changed = Signal(list)
    drag_started = Signal(int)
    drag_finished = Signal()

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.setObjectName('page')
        self.settings = settings
        self.collections = []
        self.genres = []
        self.memberships = lambda track_id: set()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 18, 0, 0)
        outer.setSpacing(10)
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 0, 24, 0)
        bar.setSpacing(4)
        self.count = label('', 'pageTitle')
        bar.addWidget(self.count)
        bar.addSpacing(14)
        self.sort_buttons = {}
        for title, column, order in SORTS:
            item = Button(title, role='sort')
            item.setCheckable(True)
            item.clicked.connect(lambda checked=False, c=column, o=order: self._sort_clicked(c, o))
            self.sort_buttons[column] = item
            bar.addWidget(item, 0, Qt.AlignmentFlag.AlignBottom)
        bar.addStretch()
        self.recent_chip = Button('只看最近两周', role='chip')
        self.recent_chip.setCheckable(True)
        self.recent_chip.clicked.connect(self._filter)
        bar.addWidget(self.recent_chip)
        self.genre_chip = Button('风格', 'chevron-down', 'chip')
        self.genre_chip.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.genre_chip.clicked.connect(self._genre_menu)
        bar.addSpacing(6)
        bar.addWidget(self.genre_chip)
        bar.addSpacing(10)
        self.wall_button = Tool('layout-grid', '封面墙', 18)
        self.list_button = Tool('list', '列表', 18)
        for item, mode in ((self.wall_button, 'wall'), (self.list_button, 'list')):
            item.setCheckable(True)
            item.clicked.connect(lambda checked=False, value=mode: self.set_view(value))
            bar.addWidget(item)
        outer.addLayout(bar)
        self.table = TrackTable()
        self.wall = WallView(self.table.proxy)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.wall)
        self.stack.addWidget(self.table)
        self.empty = label('', 'muted')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty)
        outer.addWidget(self.stack, 1)
        for view in (self.wall, self.table):
            view.activated_track.connect(lambda identifier, v=view: self.play.emit(identifier, v.visible_ids()))
            view.selection_changed.connect(lambda v=view: self._selection(v))
            view.drag_started.connect(self.drag_started)
            view.drag_finished.connect(self.drag_finished)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda point, v=view: self._context(v, point))
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._mark_sort)
        loader().ready.connect(lambda _: self.view().viewport().update())
        self.inspector = Inspector(self)
        self.inspector.edited.connect(self.edited)
        self.inspector.reveal.connect(self.reveal)
        self.inspector.details.connect(self.details)
        self.mode = 'wall'
        self.set_view(settings.value('library_view', 'wall') if settings is not None else 'wall')
        self._mark_sort()

    # ---- 视图 -----------------------------------------------------------

    def view(self):
        return self.table if self.mode == 'list' else self.wall

    def set_view(self, mode):
        mode = mode if mode in ('wall', 'list') else 'wall'
        previous = self.view()
        selected = set(previous.selected_ids())
        self.mode = mode
        if self.settings is not None:
            self.settings.setValue('library_view', mode)
        self.wall_button.setChecked(mode == 'wall')
        self.list_button.setChecked(mode == 'list')
        if self.view() is not previous:
            previous.clearSelection()
            self.view().select_ids(selected)
        self._show_stack()

    def _show_stack(self):
        shown = self.table.proxy.rowCount()
        self.stack.setCurrentWidget(self.view() if shown else self.empty)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.inspector.setGeometry(self.width() - Inspector.WIDTH, 0, Inspector.WIDTH, self.height())

    # ---- 数据 -----------------------------------------------------------

    def set_tracks(self, tracks, collections):
        self.collections = collections
        selected = set(self.view().selected_ids())
        scroll = self.wall.verticalScrollBar().value()
        if self.table.set_tracks(tracks):
            self.view().select_ids(selected)
            self.wall.verticalScrollBar().setValue(scroll)
        self.genres = sorted({track['genre'] for track in tracks if track.get('genre')})
        self.genre_chip.setVisible(bool(self.genres))
        self._update_count()
        if self.inspector.isVisible() and self.inspector.track:
            current = next((t for t in tracks if t['id'] == self.inspector.track['id']), None)
            if current:
                self.show_info(current)
            else:
                self.inspector.close_panel()

    def set_playing(self, track_id):
        self.table.source_model().set_playing(track_id)
        self.wall.viewport().update()

    def set_search(self, text):
        self.table.proxy.configure(text=text.strip().casefold())
        self._update_count()

    def selected_tracks(self):
        return self.view().selected_tracks()

    def clear_selection(self):
        self.view().clearSelection()

    def select_all(self):
        self.view().selectAll()

    def _filter(self, *_):
        self.table.proxy.configure(recent_only=self.recent_chip.isChecked())
        self._update_count()

    def _sort_clicked(self, column, default_order):
        header = self.table.horizontalHeader()
        if header.sortIndicatorSection() == column:
            order = (Qt.SortOrder.AscendingOrder if header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
                     else Qt.SortOrder.DescendingOrder)
        else:
            order = default_order
        header.setSortIndicator(column, order)
        self.wall.scrollToTop()

    def _mark_sort(self, *_):
        header = self.table.horizontalHeader()
        column, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        for key, item in self.sort_buttons.items():
            title = next(name for name, c, _ in SORTS if c == key)
            item.setChecked(key == column)
            item.setText(title + (' ↓' if order == Qt.SortOrder.DescendingOrder else ' ↑') if key == column else title)

    def _genre_menu(self):
        menu = QMenu(self)
        current = self.table.proxy.genre
        for name in ['', *self.genres]:
            action = menu.addAction(name or '全部风格')
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda checked=False, value=name: self._pick_genre(value))
        menu.exec(self.genre_chip.mapToGlobal(QPoint(0, self.genre_chip.height() + 4)))

    def _pick_genre(self, value):
        self.table.proxy.configure(genre=value)
        self.genre_chip.setText(value or '风格')
        self._update_count()

    def _update_count(self):
        total, shown = self.table.source_model().rowCount(), self.table.proxy.rowCount()
        self.count.setText(f'{total} 首' if shown == total else f'{shown} / {total} 首')
        filtered = bool(self.table.proxy.text or self.recent_chip.isChecked() or self.table.proxy.genre)
        self.empty.setText('没有符合条件的歌。换个关键词，或去掉筛选。' if filtered and total else
                           '曲库还是空的。把链接粘到上面，或把歌拖进窗口。')
        self._show_stack()

    # ---- 选择与菜单 ------------------------------------------------------

    def _selection(self, view):
        if view is self.view():
            self.selection_changed.emit(view.selected_tracks())

    def show_info(self, track):
        member_of = self.memberships(track['id'])
        self.inspector.show_track(track, [item['name'] for item in self.collections if item['id'] in member_of])

    def set_menu(self, identifiers):
        menu = QMenu(self)
        for item in self.collections:
            menu.addAction(item['name'], lambda target=item['id']: self.add_to.emit(identifiers, target))
        if self.collections:
            menu.addSeparator()
        menu.addAction('新建 Set…', lambda: self.add_to.emit(identifiers, None))
        return menu

    def _context(self, view, point):
        index = view.indexAt(point)
        if not index.isValid():
            return
        track = index.data(TRACK_ROLE)
        identifiers = view.selected_ids()
        if track['id'] not in identifiers:
            identifiers = [track['id']]
        menu = QMenu(self)
        menu.addAction('试听', lambda: self.play.emit(track['id'], view.visible_ids()))
        adding = self.set_menu(identifiers)
        adding.setTitle(f'把 {len(identifiers)} 首加入 Set' if len(identifiers) > 1 else '加入 Set')
        menu.addMenu(adding)
        menu.addSeparator()
        menu.addAction('详情', lambda: self.show_info(track))
        menu.addAction('在 Finder 中显示', lambda: self.reveal.emit(track['path']))
        menu.exec(view.viewport().mapToGlobal(point))
