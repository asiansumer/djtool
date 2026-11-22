"""需要用户做决定或想细看的三个对话框：曲库放哪、新音乐存哪、一首歌的完整信息。"""
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from djtool.ui.widgets import Button, format_bpm, format_duration, format_key, label


class _FolderDialog(QDialog):
    """一句话说明 + 一个文件夹路径 + 取消 / 确认。"""

    def __init__(self, parent, window_title, title, description, hint, start, confirm, picker_title, name):
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setMinimumWidth(600)
        self.picker_title = picker_title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 22)
        layout.setSpacing(16)
        layout.addWidget(label(title, 'sectionTitle'))
        layout.addWidget(label(description, 'muted', wrap=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.path = QLineEdit(str(start))
        self.path.setAccessibleName(name)
        row.addWidget(self.path, 1)
        browse = Button('选择…', 'folder', 'outline')
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        layout.addLayout(row)
        layout.addWidget(label(hint, 'faint', wrap=True))
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        cancel = Button('取消', role='outline')
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        accept = Button(confirm, role='primary')
        accept.clicked.connect(self.accept)
        actions.addWidget(accept)
        layout.addLayout(actions)

    def browse(self):
        directory = QFileDialog.getExistingDirectory(self, self.picker_title, self.path.text())
        if directory:
            self.path.setText(directory)


class LibraryLocationDialog(_FolderDialog):
    def __init__(self, parent=None, current=None):
        super().__init__(
            parent, '曲库位置', '曲库记录放在哪里',
            '这里保存歌曲信息、分组和 Set。Set 只是对歌曲的引用，不会另外生成音频文件夹。',
            '选一个已有的 DJTOOL 曲库可以接着用；换位置不会搬走原来的曲库。',
            current or Path.home() / 'Documents' / 'Serato', '用这个位置', '选择曲库文件夹', '曲库文件夹')

    def selected_path(self):
        value = self.path.text().strip()
        if not value:
            raise ValueError('请选择曲库存放位置')
        return Path(value).expanduser()


class MusicStorageDialog(_FolderDialog):
    def __init__(self, library, parent=None):
        current = library.music_root
        if current and current.name == 'DJTOOL Audio':
            current = current.parent
        super().__init__(
            parent, '新音乐保存位置', '新收的歌放在哪里',
            '新下载和导入的音乐会放进这个目录里的「DJTOOL Audio」。已有的音频、Serato 数据库和 Set 都不动。',
            '选保存歌曲的外层目录，比如「文稿/Serato」；不要选 _Serato_ 里面的文件夹。',
            current or Path.home() / 'Documents' / 'Serato', '用这个目录', '选择保存音乐的目录', '音乐保存目录')

    def selected_path(self):
        value = self.path.text().strip()
        if not value:
            raise ValueError('请选择音乐保存目录')
        selected = Path(value).expanduser()
        return selected if selected.name == 'DJTOOL Audio' else selected / 'DJTOOL Audio'


class MetadataDialog(QDialog):
    """一首歌的全部信息：标签、音频文件、歌词、来源记录。只读。"""

    def __init__(self, library, track, parent=None):
        super().__init__(parent)
        self.setWindowTitle('歌曲信息与来源')
        self.resize(780, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 24, 26, 20)
        outer.setSpacing(10)
        outer.addWidget(label(track['title'], 'sectionTitle', wrap=True))
        outer.addWidget(label(track['artist'], 'muted'))
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(12)

        def row(name, value, selectable=True):
            text = label(str(value or '—'), wrap=True)
            if selectable:
                text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label(name, 'muted'), text)

        for name, key in (('专辑', 'album'), ('专辑艺人', 'album_artist'), ('发行日期', 'release_date'),
                          ('曲序', 'track_number'), ('碟号', 'disc_number'), ('风格', 'genre'),
                          ('作曲', 'composer'), ('发行方', 'publisher'), ('版权', 'copyright'),
                          ('ISRC', 'isrc'), ('原始评论', 'comment')):
            row(name, track.get(key))
        # BPM 和调性只显示 Serato 分析出来的值；文件标签里的值单列，不混在一起。
        row('Serato BPM', format_bpm(track))
        row('Serato 调性', format_key(track))
        row('Serato 读取时间', track.get('bpm_serato_synced_at') or '还没读到')
        row('文件里的 BPM 标签', track.get('bpm_tag'))
        row('文件里的调性标签', track.get('musical_key'))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        tabs.addTab(scroll, '音乐信息')

        online = library.source_for_track(track['id'])
        local_sources = library.local_sources_for_track(track['id'])
        origin = f"来源链接：{online['url']}" if online else f"导入路径：{track['source_path']}"
        kept = ('NCM 原文件保留在原处；提取和转换的细节在“来源记录”里。' if local_sources else
                '按原路径引用，没有复制或改写原音频。' if track['path'] == track['source_path'] else
                '本地导入的 MP3 是原文件的逐字节副本；在线下载的歌按来源格式转换并补上标签。')
        self._text(tabs, '音频文件', '\n\n'.join([
            f"格式：{track.get('codec') or 'MP3'}",
            f"码率：{(track.get('bitrate') or 0) / 1000:.0f} kbps",
            f"采样率：{track.get('sample_rate') or '—'} Hz",
            f"声道：{track.get('channels') or '—'}",
            f"时长：{format_duration(track['duration'])}",
            f"大小：{(track.get('file_size') or 0) / 1024 / 1024:.2f} MB",
            f"播放文件：{track['path']}", origin, kept]))
        self._text(tabs, '歌词', track.get('lyrics') or '文件里没有歌词')
        self._text(tabs, '全部标签', json.dumps(json.loads(track.get('tags_json') or '{}'), ensure_ascii=False, indent=2))
        if online:
            record = json.dumps(json.loads(online['metadata_json']), ensure_ascii=False, indent=2)
        elif local_sources:
            record = json.dumps([{'原始 NCM': item['path'], **json.loads(item['metadata_json'])}
                                 for item in local_sources], ensure_ascii=False, indent=2)
        else:
            record = '本地导入，没有在线来源记录。'
        self._text(tabs, '来源记录', record)
        close = Button('关闭', role='primary')
        close.clicked.connect(self.accept)
        outer.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)

    @staticmethod
    def _text(tabs, name, content):
        view = QPlainTextEdit(content)
        view.setReadOnly(True)
        tabs.addTab(view, name)
