"""Sets：先是一面拼图墙（每个 Set 用它前四首的封面拼成一格），点进去是有顺序的歌单。

歌单里拖动行调整顺序，BPM 和调性排在一起方便看衔接；删除后可以撤销。
"""
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QHBoxLayout, QListView, QListWidget,
                               QListWidgetItem, QMenu, QStackedWidget, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

from djtool.ui import theme
from djtool.ui.covers import loader
from djtool.ui.library_view import draw_cover
from djtool.ui.widgets import Button, Tool, format_bpm, format_duration, format_key, label

TRACK_ROLE = Qt.ItemDataRole.UserRole
NEW_TILE = '__new__'


def draw_mosaic(painter, rect, paths, radius):
    """用最多四张封面拼成一格；不足四张时用第一张铺满。"""
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(rect), radius, radius)
    painter.save()
    painter.setClipPath(clip)
    painter.fillRect(rect, theme.qcolor('cover'))
    paths = [path for path in paths if path]
    if len(paths) >= 4:
        half = rect.width() // 2
        for index, path in enumerate(paths[:4]):
            cell = QRect(rect.left() + (index % 2) * half, rect.top() + (index // 2) * half,
                         rect.width() - half if index % 2 else half, rect.height() - half if index // 2 else half)
            pixmap = loader().large_pixmap(path)
            if pixmap is not None:
                painter.drawPixmap(cell, pixmap)
    elif paths:
        pixmap = loader().large_pixmap(paths[0])
        if pixmap is not None:
            painter.drawPixmap(rect, pixmap)
    else:
        side = rect.width() // 4
        theme.icon('playlist', 'ink3', 24).paint(painter, QRect(rect.center().x() - side // 2,
                                                                rect.center().y() - side // 2, side, side))
    painter.restore()


class SetsDelegate(QStyledItemDelegate):
    PAD = 10
    TEXT = 56

    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        view = self.parent()
        data = index.data(TRACK_ROLE)
        cell = option.rect
        side = cell.width() - 2 * self.PAD
        cover = QRect(cell.left() + self.PAD, cell.top() + 8, side, side)
        hovered = index.row() == view.hover_row
        dropping = index.row() == view.drop_row
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        if data == NEW_TILE:
            painter.setBrush(theme.qcolor('raised') if hovered else Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor('line_strong'), 2, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(cover).adjusted(1, 1, -1, -1), 12, 12)
            theme.icon('plus', 'ink2', 28).paint(painter, QRect(cover.center().x() - 14, cover.center().y() - 14, 28, 28))
            painter.setFont(theme.font(14, QFont.Weight.Bold))
            painter.setPen(theme.qcolor('ink2'))
            painter.drawText(QRect(cover.left(), cover.bottom() + 9, side, 20), Qt.AlignmentFlag.AlignVCenter, '新建 Set')
            painter.restore()
            return
        draw_mosaic(painter, cover, data['covers'], 12)
        if dropping or hovered:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor('accent' if dropping else 'line_strong'), 3))
            painter.drawRoundedRect(QRectF(cover).adjusted(-4.5, -4.5, 4.5, 4.5), 15, 15)
        painter.setFont(theme.font(14, QFont.Weight.Bold))
        painter.setPen(theme.qcolor('ink'))
        text = QRect(cover.left(), cover.bottom() + 9, side, 20)
        painter.drawText(text, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(data['name'], Qt.TextElideMode.ElideRight, side))
        painter.setFont(theme.font(12, tabular=True))
        painter.setPen(theme.qcolor('ink3'))
        meta = f"{data['count']} 首" + (f"，{format_duration(data['duration'])}" if data['count'] else '')
        if data['kind'] == 'group':
            meta += '，分组'
        painter.drawText(QRect(cover.left(), text.bottom() + 1, side, 18), Qt.AlignmentFlag.AlignVCenter, meta)
        painter.restore()


class SetsWall(QListWidget):
    opened = Signal(str)
    create = Signal()
    rename = Signal(str)
    delete = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_row = -1
        self.drop_row = -1
        self.setItemDelegate(SetsDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(18, 0, 8, 0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context)
        self.itemClicked.connect(self._clicked)
        loader().ready.connect(lambda _: self.viewport().update())
        self._fit()

    def _fit(self, width=None):
        width = max(360, (self.viewport().width() if width is None else width) - 2)
        columns = max(3, round(width / 216))
        cell = width // columns
        size = QSize(cell, cell - 2 * SetsDelegate.PAD + 8 + SetsDelegate.TEXT)
        if size != self.gridSize():
            self.setGridSize(size)

    def resizeEvent(self, event):
        self._fit(event.size().width())
        super().resizeEvent(event)

    def populate(self, entries):
        self.clear()
        for entry in entries:
            item = QListWidgetItem()
            item.setData(TRACK_ROLE, entry)
            self.addItem(item)
        item = QListWidgetItem()
        item.setData(TRACK_ROLE, NEW_TILE)
        self.addItem(item)

    def _clicked(self, item):
        data = item.data(TRACK_ROLE)
        if data == NEW_TILE:
            self.create.emit()
        else:
            self.opened.emit(data['id'])

    def _context(self, point):
        item = self.itemAt(point)
        if not item or item.data(TRACK_ROLE) == NEW_TILE:
            return
        identifier = item.data(TRACK_ROLE)['id']
        menu = QMenu(self)
        menu.addAction('打开', lambda: self.opened.emit(identifier))
        menu.addAction('重命名…', lambda: self.rename.emit(identifier))
        menu.addAction('删除这个列表…', lambda: self.delete.emit(identifier))
        menu.exec(self.viewport().mapToGlobal(point))

    def mouseMoveEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        row = self.row(item) if item else -1
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if item else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)


class SetDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        track = index.data(TRACK_ROLE) or {}
        return QSize(200, 68 if track.get('set_note') else 52)

    def paint(self, painter, option, index):
        view = self.parent()
        track = index.data(TRACK_ROLE)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        if selected:
            painter.fillRect(rect, theme.qcolor('selected'))
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), theme.qcolor('accent'))
        elif index.row() == view.hover_row:
            painter.fillRect(rect, theme.qcolor('hover'))
        playing = track['id'] == view.playing_id
        base = rect.top() + 26
        painter.setFont(theme.font(13, tabular=True))
        painter.setPen(theme.qcolor('accent_text' if playing else 'ink3'))
        painter.drawText(QRect(rect.left() + 14, base - 12, 34, 24),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, str(index.row() + 1))
        art = QRect(rect.left() + 62, base - 18, 36, 36)
        draw_cover(painter, art, track.get('cover_path'), 5, small=36)
        right = rect.right() - 28
        columns = ((format_duration(track['duration']), 60, 'ink2'), (format_key(track), 52, 'ink2'),
                   (format_bpm(track), 64, 'ink'))
        for text, width, token in columns:
            painter.setPen(theme.qcolor(token if text != '—' else 'ink3'))
            painter.drawText(QRect(right - width, base - 12, width, 24),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
            right -= width
        text_left = art.right() + 14
        width = max(40, right - text_left - 12)
        painter.setFont(theme.font(13, QFont.Weight.DemiBold if playing else QFont.Weight.Medium))
        painter.setPen(theme.qcolor('accent_text' if playing else 'ink'))
        painter.drawText(QRect(text_left, base - 19, width, 20), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(track['title'], Qt.TextElideMode.ElideRight, width))
        painter.setFont(theme.font(12))
        painter.setPen(theme.qcolor('ink2'))
        painter.drawText(QRect(text_left, base + 1, width, 18), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(track['artist'], Qt.TextElideMode.ElideRight, width))
        if track.get('set_note'):
            painter.setPen(theme.qcolor('ink3'))
            room = rect.right() - text_left - 28
            painter.drawText(QRect(text_left, base + 21, room, 18), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(track['set_note'], Qt.TextElideMode.ElideRight, room))
        painter.restore()


class SetList(QListWidget):
    """有序列表。排序自己处理鼠标，不进入 macOS 的系统拖放循环。"""
    order_changed = Signal(list)
    activated_track = Signal(str)
    remove_requested = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_row = -1
        self.playing_id = None
        self._press = None
        self._press_id = None
        self._dragging = False
        self._drop_at = None
        self.setItemDelegate(SetDelegate(self))
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.itemDoubleClicked.connect(lambda item: self.activated_track.emit(item.data(TRACK_ROLE)['id']))
        loader().ready.connect(lambda _: self.viewport().update())

    def populate(self, tracks):
        selected = set(self.selected_ids())
        scroll = self.verticalScrollBar().value()
        self.clear()
        for track in tracks:
            item = QListWidgetItem()
            item.setData(TRACK_ROLE, track)
            self.addItem(item)
            item.setSelected(track['id'] in selected)
        self.verticalScrollBar().setValue(scroll)

    def ids(self):
        return [self.item(row).data(TRACK_ROLE)['id'] for row in range(self.count())]

    def selected_ids(self):
        return [self.item(row).data(TRACK_ROLE)['id'] for row in range(self.count()) if self.item(row).isSelected()]

    def set_playing(self, track_id):
        self.playing_id = track_id
        self.viewport().update()

    def _drop_index(self, point):
        item = self.itemAt(point)
        if not item:
            return self.count()
        row = self.row(item)
        return row + (point.y() > self.visualItemRect(item).center().y())

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        plain = event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
        self._press = event.position().toPoint() if plain and item else None
        self._press_id = item.data(TRACK_ROLE)['id'] if item else None
        self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._dragging or (point - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._dragging = True
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                self._drop_at = self._drop_index(point)
                bar = self.verticalScrollBar()
                if point.y() < 28:
                    bar.setValue(bar.value() - 14)
                elif point.y() > self.viewport().height() - 28:
                    bar.setValue(bar.value() + 14)
                self.viewport().update()
                event.accept()
                return
        row = self.row(self.itemAt(point)) if self.itemAt(point) else -1
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            position = self._drop_index(event.position().toPoint())
            order = self.ids()
            if self._press_id in order:
                old = order.index(self._press_id)
                order.remove(self._press_id)
                order.insert(position - (old < position), self._press_id)
                if order != self.ids():
                    self.order_changed.emit(order)
            self._press = self._press_id = self._drop_at = None
            self._dragging = False
            self.viewport().unsetCursor()
            self.viewport().update()
            event.accept()
            return
        self._press = None
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.selected_ids():
            self.remove_requested.emit(self.selected_ids())
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentItem():
            self.activated_track.emit(self.currentItem().data(TRACK_ROLE)['id'])
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_at is not None and self.count():
            if self._drop_at < self.count():
                y = self.visualItemRect(self.item(self._drop_at)).top()
            else:
                y = self.visualItemRect(self.item(self.count() - 1)).bottom()
            painter = QPainter(self.viewport())
            painter.fillRect(QRect(28, y - 1, self.viewport().width() - 56, 2), theme.qcolor('accent'))
            painter.end()


class Mosaic(QWidget):
    def __init__(self, size, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.paths = []
        loader().ready.connect(lambda _: self.update())

    def set_paths(self, paths):
        self.paths = list(paths)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        draw_mosaic(painter, self.rect(), self.paths, 12)
        painter.end()


class SetPage(QWidget):
    """Sets 的两层：拼图墙，和点进去以后的一个歌单。"""
    play = Signal(str, list)
    reorder = Signal(str, list)
    remove = Signal(str, list)
    note = Signal(str, dict)
    rename = Signal(str)
    delete = Signal(str)
    create = Signal()
    opened = Signal(str)
    back = Signal()
    reveal = Signal(str)
    details = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('page')
        self.collection = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        overview = QWidget()
        overview.setObjectName('page')
        layout = QVBoxLayout(overview)
        layout.setContentsMargins(0, 18, 0, 0)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(28, 0, 24, 0)
        self.wall_title = label('Sets', 'pageTitle')
        head.addWidget(self.wall_title)
        head.addStretch()
        layout.addLayout(head)
        self.wall = SetsWall()
        self.wall.opened.connect(self.opened)
        self.wall.create.connect(self.create)
        self.wall.rename.connect(self.rename)
        self.wall.delete.connect(self.delete)
        layout.addWidget(self.wall, 1)
        self.stack.addWidget(overview)

        detail = QWidget()
        detail.setObjectName('page')
        outer = QVBoxLayout(detail)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.setContentsMargins(28, 0, 24, 0)
        head.setSpacing(18)
        self.mosaic = Mosaic(104)
        head.addWidget(self.mosaic)
        text = QVBoxLayout()
        text.setSpacing(4)
        crumb = Button('Sets', 'chevron-left', 'link')
        crumb.clicked.connect(self.back)
        text.addWidget(crumb, 0, Qt.AlignmentFlag.AlignLeft)
        self.title = label('', 'pageTitle')
        text.addWidget(self.title)
        self.meta = label('', 'muted')
        text.addWidget(self.meta)
        head.addLayout(text, 1)
        self.play_all = Button('从头播放', 'play-solid', 'primary')
        self.play_all.clicked.connect(lambda: self.list.count() and self.play.emit(self.list.ids()[0], self.list.ids()))
        head.addWidget(self.play_all, 0, Qt.AlignmentFlag.AlignBottom)
        more = Tool('dots', '重命名或删除')
        more.clicked.connect(lambda: self._menu(more))
        head.addWidget(more, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(head)
        columns = QHBoxLayout()
        columns.setSpacing(0)
        columns.setContentsMargins(28, 4, 38, 0)
        self.hint = label('拖动行调整顺序', 'faint')
        columns.addWidget(self.hint, 1)
        for name, width in (('BPM', 64), ('调', 52), ('时长', 60)):
            item = label(name, 'faint')
            item.setFixedWidth(width)
            item.setAlignment(Qt.AlignmentFlag.AlignRight)
            columns.addWidget(item)
        outer.addLayout(columns)
        self.list = SetList()
        self.list.order_changed.connect(lambda order: self.reorder.emit(self.collection['id'], order))
        self.list.activated_track.connect(lambda identifier: self.play.emit(identifier, self.list.ids()))
        self.list.remove_requested.connect(lambda identifiers: self.remove.emit(self.collection['id'], identifiers))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context)
        outer.addWidget(self.list, 1)
        self.empty = label('这个 Set 还是空的。\n回到曲库，选中歌曲后点“加入 Set”，或直接拖过来。', 'muted')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty, 1)
        self.stack.addWidget(detail)

    def show_wall(self, entries):
        self.wall.populate(entries)
        self.stack.setCurrentIndex(0)

    def refresh_wall(self, entries):
        self.wall.populate(entries)

    def showing_detail(self):
        return self.stack.currentIndex() == 1

    def show_collection(self, collection, tracks, status=''):
        self.collection = collection
        self.title.setText(collection['name'])
        total = format_duration(sum(track['duration'] for track in tracks))
        parts = [f'{len(tracks)} 首，共 {total}'] if tracks else []
        if status:
            parts.append(status)
        self.meta.setText('　'.join(parts))
        self.mosaic.set_paths([track.get('cover_path') for track in tracks if track.get('cover_path')][:4])
        self.play_all.setVisible(bool(tracks))
        self.hint.setText('拖动行调整顺序' if collection['kind'] == 'set' else '分组没有固定顺序')
        self.list.populate(tracks)
        self.list.setVisible(bool(tracks))
        self.empty.setVisible(not tracks)
        self.stack.setCurrentIndex(1)

    def set_playing(self, track_id):
        self.list.set_playing(track_id)

    def _menu(self, anchor):
        if not self.collection:
            return
        menu = QMenu(self)
        menu.addAction('重命名…', lambda: self.rename.emit(self.collection['id']))
        menu.addAction('删除这个列表…', lambda: self.delete.emit(self.collection['id']))
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))

    def _context(self, point):
        item = self.list.itemAt(point)
        if not item:
            return
        track = item.data(TRACK_ROLE)
        identifiers = self.list.selected_ids()
        if track['id'] not in identifiers:
            identifiers = [track['id']]
        menu = QMenu(self)
        menu.addAction('试听', lambda: self.play.emit(track['id'], self.list.ids()))
        menu.addAction('写衔接备注…', lambda: self.note.emit(self.collection['id'], track))
        menu.addSeparator()
        menu.addAction('在 Finder 中显示', lambda: self.reveal.emit(track['path']))
        menu.addAction('完整信息…', lambda: self.details.emit(track['id']))
        menu.addSeparator()
        menu.addAction(f'从这里移除 {len(identifiers)} 首' if len(identifiers) > 1 else '从这里移除',
                       lambda: self.remove.emit(self.collection['id'], identifiers))
        menu.exec(self.list.viewport().mapToGlobal(point))
