"""新界面共用的小部件：按钮、标签、进度滑条、底部提示条，以及显示用的格式化函数。"""
from datetime import datetime, timedelta, timezone
import math

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QStyle,
                               QStyleOptionSlider, QToolButton, QToolTip)

from djtool.ui import theme


def label(text='', role=None, wrap=False):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        item.setObjectName(role)
    item.setWordWrap(wrap)
    return item


class _Themed:
    """按钮记住图标名，切换主题时重新上色。"""
    _symbol = None
    _token = 'ink2'
    _icon_size = 18

    def set_symbol(self, symbol, token='ink2', size=18):
        self._symbol, self._token, self._icon_size = symbol, token, size
        self.retheme()

    def retheme(self):
        if self._symbol:
            self.setIcon(theme.icon(self._symbol, self._token, self._icon_size))
            self.setIconSize(QSize(self._icon_size, self._icon_size))


class Button(QPushButton, _Themed):
    def __init__(self, text='', symbol=None, role=None, token=None):
        super().__init__(text)
        if role:
            self.setObjectName(role)
        if symbol:
            self.set_symbol(symbol, token or ('accent_ink' if role in ('primary', 'play') else 'ink2'), 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class Tool(QToolButton, _Themed):
    def __init__(self, symbol, tip, size=18):
        super().__init__()
        self.set_symbol(symbol, 'ink2', size)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


def rule():
    line = QFrame()
    line.setObjectName('rule')
    return line


def format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes}:{seconds:02d}'


def format_bpm(track):
    """只显示从 Serato 读到的 BPM；文件标签和旧估计值不作替补。"""
    value = track.get('bpm_serato')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return '—'
    return f'{value:.1f}'.rstrip('0').rstrip('.')


def format_key(track):
    return track.get('key_serato') or '—'


def format_added(created_at, now=None):
    """SQLite 的 CURRENT_TIMESTAMP 是 UTC；显示成本地的相对日期。"""
    try:
        moment = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return ''
    now = now or datetime.now().astimezone()
    days = (now.date() - moment.date()).days
    if days <= 0:
        return '今天'
    if days == 1:
        return '昨天'
    if days < 7:
        return f'{days} 天前'
    return f'{moment.month}月{moment.day}日' if moment.year == now.year else f'{moment.year}/{moment.month}/{moment.day}'


def is_recent(created_at, days=14, now=None):
    try:
        moment = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) - moment <= timedelta(days=days)


class SeekSlider(QSlider):
    """试听进度：点击轨道立即跳转，手柄仍按系统的拖动方式操作。"""
    seekRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName('seek')
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._track_press = False

    def _parts(self):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderHandle, self)
        return option, groove, handle

    def _value_at(self, point):
        option, groove, handle = self._parts()
        position = round(point.x()) - (handle.width() - 1) // 2 - groove.left()
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), position,
                                              max(0, groove.width() - handle.width()), option.upsideDown)

    def mousePressEvent(self, event):
        if not self.isEnabled() or self.minimum() == self.maximum():
            event.ignore()
            return
        _, _, handle = self._parts()
        if event.button() == Qt.MouseButton.LeftButton and not handle.contains(event.position().toPoint()):
            # 独立的用户信号，避免把播放器回传的 setValue 再当成跳转请求。
            self._track_press = True
            self.setSliderDown(True)
            self.setSliderPosition(self._value_at(event.position()))
            self.seekRequested.emit(self.sliderPosition())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._track_press:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.setSliderPosition(self._value_at(event.position()))
            event.accept()
            return
        if not event.buttons() and self.maximum() > self.minimum():
            # 悬停时显示鼠标所在位置的时间，点下去之前就知道会跳到哪里。
            QToolTip.showText(event.globalPosition().toPoint(), format_duration(self._value_at(event.position()) / 1000), self)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._track_press and event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(self._value_at(event.position()))
            self._track_press = False
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class Toast(QFrame):
    """浮在内容区底部的一行反馈；可带一个动作（例如撤销），几秒后自己消失。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('toast')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 8, 8)
        layout.setSpacing(12)
        self.text = label()
        layout.addWidget(self.text)
        self.action = QPushButton()
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(self._run)
        layout.addWidget(self.action)
        self._callback = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, message, action=None, callback=None, seconds=5):
        self.text.setText(message)
        self._callback = callback
        self.action.setVisible(bool(action))
        self.action.setText(action or '')
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.timer.start(int(seconds * 1000))

    def reposition(self):
        parent = self.parentWidget()
        if parent:
            self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 18)

    def _run(self):
        callback, self._callback = self._callback, None
        self.hide()
        if callback:
            callback()
