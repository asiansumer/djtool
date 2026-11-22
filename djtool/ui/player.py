"""底部通栏的试听栏：只负责试听，不改音频、不影响 Serato 的输出设置。

占满窗口宽度，是为了让进度条尽量长：DJ 试听时要频繁跳到前奏、副歌和结尾，
进度条越长，落点越准。进度条整个高度都能点，悬停会显示那个位置的时间。
"""
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QActionGroup, QPainter, QPainterPath, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMenu, QSlider, QVBoxLayout

from djtool.ui import theme
from djtool.ui.widgets import Button, SeekSlider, Tool, format_duration, label


NAME_WIDTH = 210


class PlayerBar(QFrame):
    track_changed = Signal(object)
    notice = Signal(str)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.setObjectName('player')
        self.setFixedHeight(76)
        self.track = None
        self.queue = []
        self.lookup = lambda track_id: None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 20, 0)
        layout.setSpacing(10)
        self.art = label()
        self.art.setFixedSize(52, 52)
        layout.addWidget(self.art)
        names = QVBoxLayout()
        names.setSpacing(3)
        self.title = label('还没有在试听的歌', 'cardTitle')
        self.artist = label('双击任意一首开始', 'faint')
        self.title.setFixedWidth(NAME_WIDTH)
        self.artist.setFixedWidth(NAME_WIDTH)
        names.addStretch()
        names.addWidget(self.title)
        names.addWidget(self.artist)
        names.addStretch()
        layout.addLayout(names)
        layout.addSpacing(6)
        self.previous = Tool('player-skip-back', '上一首', 20)
        self.previous.clicked.connect(lambda: self.skip(-1))
        layout.addWidget(self.previous)
        self.play_button = Button(role='play')
        self.play_button.setFixedSize(40, 40)
        self.play_button.setAccessibleName('播放或暂停')
        self.play_button.clicked.connect(self.toggle)
        layout.addWidget(self.play_button)
        self.next = Tool('player-skip-forward', '下一首', 20)
        self.next.clicked.connect(lambda: self.skip(1))
        layout.addWidget(self.next)
        layout.addSpacing(6)
        self.elapsed = label('0:00', 'faint')
        self.elapsed.setFont(theme.font(12, tabular=True))
        self.elapsed.setFixedWidth(40)
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.elapsed)
        self.seek = SeekSlider()
        self.seek.setRange(0, 0)
        self.seek.setFixedHeight(36)
        self.seek.sliderReleased.connect(lambda: self.player.setPosition(self.seek.value()))
        layout.addWidget(self.seek, 1)
        self.total = label('0:00', 'faint')
        self.total.setFont(theme.font(12, tabular=True))
        self.total.setFixedWidth(40)
        layout.addWidget(self.total)
        layout.addSpacing(6)
        self.output_button = Tool('volume', '音频输出', 18)
        self.output_button.clicked.connect(self._outputs)
        layout.addWidget(self.output_button)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(settings.value('volume', 60)) if settings is not None else 60)
        self.volume.setFixedWidth(88)
        self.volume.setAccessibleName('试听音量')
        layout.addWidget(self.volume)

        from djtool.audio_output import AudioOutputController
        self.settings = settings
        self.audio = QAudioOutput(self)
        self.audio.setVolume(self.volume.value() / 100)
        self.outputs = AudioOutputController(self.audio, self, settings=settings)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.seek.seekRequested.connect(self.player.setPosition)
        self.volume.valueChanged.connect(self._volume)
        self.player.playbackStateChanged.connect(lambda _: self.retheme())
        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(self._duration)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.errorOccurred.connect(lambda error, message: message and self.notice.emit(f'这首歌没能播放：{message}'))
        self.retheme()
        self._enable(False)

    def retheme(self):
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState if hasattr(self, 'player') else False
        self.play_button.setIcon(theme.icon('pause-solid' if playing else 'play-solid', 'accent_ink', 20))
        self.play_button.setIconSize(QSize(20, 20))
        self.art.setPixmap(self._disc(self.track.get('cover_path') if self.track else None))

    @staticmethod
    def _disc(path):
        # 和封面墙一样是圆角方形，只是小一号。
        source = theme.cover(path, 52)
        result = QPixmap(104, 104)
        result.setDevicePixelRatio(2)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, 52, 52, 7, 7)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, source)
        painter.end()
        return result

    def _enable(self, enabled):
        for control in (self.play_button, self.previous, self.next, self.seek):
            control.setEnabled(enabled)

    def _volume(self, value):
        self.audio.setVolume(value / 100)
        if self.settings is not None:
            self.settings.setValue('volume', value)

    def _position(self, position):
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.elapsed.setText(format_duration(position / 1000))

    def _duration(self, duration):
        self.seek.setRange(0, duration)
        self.total.setText(format_duration(duration / 1000))

    def _status(self, status):
        # 一首放完自动接下一首，方便连着听一个 Set。
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.skip(1)

    def _outputs(self):
        from djtool.audio_output import device_key
        self.outputs.refresh()
        menu = QMenu(self)
        group = QActionGroup(menu)
        default = self.outputs.default_device.description() or '没有可用的设备'

        def choice(text, identifier):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(self.outputs.preferred_id == identifier)
            group.addAction(action)
            action.triggered.connect(lambda checked=False: self._choose(identifier))

        choice('跟随系统：' + default, '')
        menu.addSeparator()
        for device in self.outputs.devices:
            choice(device.description(), device_key(device))
        if self.outputs.unavailable:
            menu.addSeparator()
            menu.addAction('未连接：' + (self.outputs.preferred_name or '上次选择的设备')).setEnabled(False)
        menu.exec(self.output_button.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))

    def _choose(self, identifier):
        self.notice.emit('试听输出：' + self.outputs.description() if self.outputs.choose(identifier)
                         else '这个设备已经断开，换一个输出')

    def refresh_track(self):
        """曲库信息变了（改了标题等）时更新显示，不打断播放。"""
        if self.track:
            current = self.lookup(self.track['id'])
            if current is None:
                self.clear()
            else:
                self.track = current
                self._show()

    def _show(self):
        metrics = self.title.fontMetrics()
        self.title.setText(metrics.elidedText(self.track['title'], Qt.TextElideMode.ElideRight, NAME_WIDTH))
        self.title.setToolTip(self.track['title'])
        self.artist.setText(self.artist.fontMetrics().elidedText(self.track['artist'], Qt.TextElideMode.ElideRight, NAME_WIDTH))
        self.art.setPixmap(self._disc(self.track.get('cover_path')))

    def play(self, track_id, queue=None):
        track = self.lookup(track_id)
        if track is None:
            return False
        if not Path(track['path']).is_file():
            self.notice.emit('播放文件不在原来的位置，没法试听')
            return False
        if queue is not None:
            self.queue = list(queue)
        if not self.track or self.track['id'] != track_id:
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(track['path']))
        self.track = track
        self._show()
        self._enable(True)
        self.player.play()
        self.track_changed.emit(track_id)
        return True

    def toggle(self):
        if not self.track:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.duration() and self.player.position() >= self.player.duration():
                self.player.setPosition(0)
            self.player.play()

    def skip(self, offset):
        if self.track and self.track['id'] in self.queue:
            index = self.queue.index(self.track['id']) + offset
            if 0 <= index < len(self.queue):
                self.play(self.queue[index])

    def clear(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.track = None
        self.queue = []
        self.title.setText('还没有在试听的歌')
        self.artist.setText('双击任意一首开始')
        self.art.setPixmap(self._disc(None))
        self._enable(False)
        self.track_changed.emit(None)
