"""设计变量：每个颜色只在这里定义一次，样式表和自绘控件都从这里取。

外观是用户选定的“唱片墙”：界面本身接近纯黑、没有侧栏，专辑封面是唯一的彩色。
暗色是默认（DJ 常在暗处用电脑）；浅色用同一套变量名换一组值。
强调色是控制器 Cue 垫的黄色，只用在“下一步动作”、选中和进度上。
"""
from functools import lru_cache
from pathlib import Path
from string import Template
import sys

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

THEME_KEY = 'appearance_theme'
ASSETS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2])) / 'assets'

THEMES = {
    'dark': {
        'window': '#09090A', 'sidebar': '#09090A', 'raised': '#17171A', 'field': '#17171A', 'pill': '#222226',
        'hover': '#131315', 'selected': '#222226', 'line': '#202024', 'line_strong': '#34343A',
        'ink': '#F2F2F0', 'ink2': '#9A9AA0', 'ink3': '#6C6C72',
        'accent': '#F7C63C', 'accent_hover': '#FFD657', 'accent_ink': '#09090A', 'accent_text': '#F7C63C',
        'ok': '#5CCB8F', 'warn': '#F0A04B', 'bad': '#F0715F', 'cover': '#1D1D21',
    },
    'light': {
        'window': '#F6F6F4', 'sidebar': '#F6F6F4', 'raised': '#FFFFFF', 'field': '#FFFFFF', 'pill': '#FFFFFF',
        'hover': '#EDEDEA', 'selected': '#E4E4E0', 'line': '#E2E2DE', 'line_strong': '#CACAC5',
        'ink': '#0C0C0C', 'ink2': '#5E5E5A', 'ink3': '#94948F',
        'accent': '#F7C63C', 'accent_hover': '#F2B92B', 'accent_ink': '#0C0C0C', 'accent_text': '#8A6400',
        'ok': '#1E8E57', 'warn': '#B4690E', 'bad': '#C4432F', 'cover': '#E2E2DE',
    },
}

QSS = Template('''
* { font-family: ".AppleSystemUIFont", "PingFang SC"; font-size: 13px; color: $ink; outline: none; }
QMainWindow, QDialog { background: $window; }
QWidget#page { background: $window; }
QLabel { background: transparent; }
QLabel#pageTitle { font-size: 30px; font-weight: 800; }
QLabel#hero { font-size: 34px; font-weight: 800; }
QLabel#sectionTitle { font-size: 20px; font-weight: 600; }
QLabel#cardTitle { font-size: 14px; font-weight: 600; }
QLabel#muted, QLabel#eyebrow { color: $ink2; }
QLabel#faint { color: $ink3; font-size: 12px; }
QLabel#wordmark { font-size: 15px; font-weight: 700; letter-spacing: 2px; }
QLabel#badge { background: $accent; color: $accent_ink; border-radius: 8px; padding: 0 6px; font-size: 11px; font-weight: 600; }
QFrame#segbar { background: $raised; border-radius: 11px; }
QFrame#player { background: $raised; border: none; border-top: 1px solid $line; }
QFrame#selbar { background: $pill; border: 1px solid $line_strong; border-radius: 23px; }
QFrame#tray { background: $pill; border: 1px solid $accent; border-radius: 22px; }
QFrame#inspector { background: $raised; border-left: 1px solid $line_strong; }
QFrame#card { background: $raised; border: none; border-radius: 16px; }
QFrame#dropzone { background: transparent; border: 2px dashed $line_strong; border-radius: 24px; }
QFrame#dropzone[dropping="true"] { border-color: $accent; background: $raised; }
QFrame#toast { background: $ink; border-radius: 9px; }
QFrame#toast QLabel { color: $window; }
QFrame#toast QPushButton { color: $window; border: 1px solid $ink3; padding: 3px 10px; }
QFrame#toast QPushButton:hover { background: $ink2; }
QFrame#rule { background: $line; max-height: 1px; min-height: 1px; border: none; }

QPushButton { background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 6px 12px; }
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $selected; }
QPushButton:disabled { color: $ink3; }
QPushButton#primary { background: $accent; color: $accent_ink; border-color: $accent; font-weight: 600; padding: 7px 16px; }
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:disabled { background: $selected; border-color: $selected; color: $ink3; }
QPushButton#outline { border: 1px solid $line_strong; }
QPushButton#outline:hover { background: $hover; }
QPushButton#link { color: $accent_text; padding: 2px 4px; }
QPushButton#nav { text-align: left; padding: 7px 10px; color: $ink2; }
QPushButton#nav:hover { color: $ink; }
QPushButton#nav:checked { background: $selected; color: $ink; font-weight: 600; }
QPushButton#nav[dropping="true"] { background: $accent; color: $accent_ink; }
QPushButton#seg { padding: 6px 22px; border-radius: 8px; color: $ink2; font-weight: 600; border: none; }
QPushButton#seg:hover { color: $ink; background: transparent; }
QPushButton#seg:checked { background: $ink; color: $window; }
QPushButton#sort { color: $ink2; padding: 4px 9px; border: none; font-size: 14px; }
QPushButton#sort:hover { color: $ink; background: transparent; }
QPushButton#sort:checked { color: $accent_text; font-weight: 600; }
QPushButton#drop { border: 1px dashed $line_strong; border-radius: 15px; padding: 7px 16px; color: $ink; }
QPushButton#drop[dropping="true"] { background: $accent; color: $accent_ink; border: 1px solid $accent; font-weight: 600; }
QPushButton#suggest { border: 1px solid $line_strong; border-radius: 12px; padding: 3px 14px; min-height: 18px; color: $ink2; font-size: 12px; }
QPushButton#suggest:hover { color: $ink; border-color: $accent; background: transparent; }
QPushButton#chip { border: 1px solid $line; border-radius: 13px; padding: 4px 12px; color: $ink2; }
QPushButton#chip:hover { border-color: $line_strong; color: $ink; }
QPushButton#chip:checked { background: $ink; color: $window; border-color: $ink; font-weight: 600; }
QPushButton#pill { border: 1px solid $line; border-radius: 13px; padding: 4px 11px; color: $ink2; font-size: 12px; }
QPushButton#round { background: $ink; border-radius: 18px; padding: 0; }
QPushButton#round:hover { background: $accent; }
QPushButton#play { background: $accent; border-radius: 20px; padding: 0; }
QPushButton#play:hover { background: $accent_hover; }
QPushButton#play:disabled { background: $selected; }
QToolButton { background: transparent; border: none; border-radius: 6px; padding: 5px; }
QToolButton:hover { background: $hover; }
QToolButton:pressed { background: $selected; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {
  background: $field; border: 1px solid $line; border-radius: 7px; padding: 6px 9px;
  selection-background-color: $accent; selection-color: $accent_ink; }
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: $accent; }
QLineEdit#intake { background: $raised; border: 1px solid $raised; border-radius: 26px;
  padding: 0 18px; font-size: 15px; }
QLineEdit#intake:focus { border: 1px solid $accent; }
QLineEdit#intake[dropping="true"] { border: 1px solid $accent; background: $selected; }
QLineEdit#search { background: $raised; border: 1px solid $raised; border-radius: 15px; padding: 0 10px; }
QLineEdit#search:focus { border: 1px solid $line_strong; }
QLineEdit#flat, QLineEdit#flatTitle { background: transparent; border: 1px solid transparent; padding: 3px 5px; }
QLineEdit#flatTitle { font-size: 17px; font-weight: 600; }
QLineEdit#flat:hover, QLineEdit#flatTitle:hover { border-color: $line; }
QLineEdit#flat:focus, QLineEdit#flatTitle:focus { border-color: $accent; background: $field; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: $raised; border: 1px solid $line_strong; selection-background-color: $selected; }

QAbstractItemView { background: transparent; border: none; selection-background-color: $selected; selection-color: $ink; }
QTableWidget, QTreeWidget { alternate-background-color: $raised; }
QHeaderView { background: transparent; }
QHeaderView::section { background: $window; color: $ink3; border: none; border-bottom: 1px solid $line;
  padding: 7px 10px; font-size: 12px; }
QHeaderView::section:hover { color: $ink; }
QHeaderView::up-arrow, QHeaderView::down-arrow { width: 0; height: 0; }
QTableCornerButton::section { background: transparent; border: none; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $line_strong; min-height: 32px; border-radius: 3px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $line_strong; min-width: 32px; border-radius: 3px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSlider::groove:horizontal { height: 4px; background: $selected; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $ink2; border-radius: 2px; }
QSlider::handle:horizontal { background: $ink; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
QSlider#seek::groove:horizontal { height: 6px; border-radius: 3px; }
QSlider#seek::sub-page:horizontal { background: $accent; border-radius: 3px; }
QSlider#seek::handle:horizontal { width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }
QSlider::handle:horizontal:disabled { background: $ink3; }

QProgressBar { background: $selected; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }
QProgressBar::chunk { background: $accent; border-radius: 2px; }

QMenu { background: $raised; border: 1px solid $line_strong; border-radius: 8px; padding: 5px; }
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: $selected; }
QMenu::item:disabled { color: $ink3; }
QMenu::separator { height: 1px; background: $line; margin: 4px 8px; }
QToolTip { background: $raised; color: $ink; border: 1px solid $line_strong; padding: 5px 8px; }
QCheckBox { spacing: 8px; }
QSplitter::handle { background: $line; }
QTabWidget::pane { border: 1px solid $line; border-radius: 8px; }
QTabBar::tab { background: transparent; color: $ink2; padding: 8px 14px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: $ink; border-bottom: 2px solid $accent; }
QMessageBox { background: $window; }
''')


def mode():
    app = QApplication.instance()
    return 'light' if app and app.property('djtool_theme') == 'light' else 'dark'


def color(token):
    return THEMES[mode()][token]


def qcolor(token, alpha=None):
    value = QColor(color(token))
    if alpha is not None:
        value.setAlpha(alpha)
    return value


def load(settings=None):
    value = settings.value(THEME_KEY, 'dark') if settings is not None else 'dark'
    return value if value in THEMES else 'dark'


def apply(app, name, settings=None):
    if name not in THEMES:
        raise ValueError('无效的外观主题')
    # 旧对话框仍读取这个属性来决定图标颜色；新旧界面共用同一个开关。
    app.setProperty('djtool_theme', name)
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark if name == 'dark' else Qt.ColorScheme.Light)
    if settings is not None:
        settings.setValue(THEME_KEY, name)
    tokens = THEMES[name]
    palette = QPalette()
    for role, token in ((QPalette.ColorRole.Window, 'window'), (QPalette.ColorRole.WindowText, 'ink'),
                        (QPalette.ColorRole.Base, 'window'), (QPalette.ColorRole.AlternateBase, 'raised'),
                        (QPalette.ColorRole.Text, 'ink'), (QPalette.ColorRole.Button, 'raised'),
                        (QPalette.ColorRole.ButtonText, 'ink'), (QPalette.ColorRole.Highlight, 'selected'),
                        (QPalette.ColorRole.HighlightedText, 'ink'), (QPalette.ColorRole.ToolTipBase, 'raised'),
                        (QPalette.ColorRole.ToolTipText, 'ink'), (QPalette.ColorRole.PlaceholderText, 'ink3'),
                        (QPalette.ColorRole.Link, 'accent_text')):
        palette.setColor(role, QColor(tokens[token]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(tokens['ink3']))
    app.setPalette(palette)
    icon.cache_clear()
    cover.cache_clear()
    app.setStyleSheet(QSS.substitute(tokens))
    for widget in app.allWidgets():
        refresh = getattr(widget, 'retheme', None)
        if callable(refresh):
            refresh()
        widget.update()


@lru_cache(maxsize=256)
def icon(name, token='ink2', size=20):
    """按当前主题给线条图标上色；token 也可以直接是十六进制颜色。"""
    tint = token if token.startswith('#') else color(token)
    data = (ASSETS / 'icons' / f'{name}.svg').read_bytes().replace(b'currentColor', tint.encode())
    renderer = QSvgRenderer(QByteArray(data))
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return QIcon(pixmap)


@lru_cache(maxsize=2048)
def cover(path, size):
    """方形封面，按 2x 像素缓存；没有封面时画一个安静的占位。"""
    pixels = size * 2
    source = QPixmap(path) if path else QPixmap()
    result = QPixmap(pixels, pixels)
    result.setDevicePixelRatio(2)
    if source.isNull():
        result.fill(QColor(color('cover')))
        painter = QPainter(result)
        inset = size * .3
        icon('music', 'ink3', 24).paint(painter, int(inset), int(inset), int(size - 2 * inset), int(size - 2 * inset))
        painter.end()
        return result
    scaled = source.scaled(pixels, pixels, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    result = scaled.copy((scaled.width() - pixels) // 2, (scaled.height() - pixels) // 2, pixels, pixels)
    result.setDevicePixelRatio(2)
    return result


def font(size=13, weight=QFont.Weight.Normal, tabular=False):
    value = QFont('.AppleSystemUIFont')
    value.setPixelSize(size)
    value.setWeight(weight)
    if tabular:
        # 数字等宽，BPM 和时长在列里才能上下对齐。
        try:
            value.setFeature(QFont.Tag('tnum'), 1)
        except (AttributeError, TypeError):
            pass
    return value
