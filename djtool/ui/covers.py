"""封面加载：解码和缩放放在后台线程，界面先画占位、图好了再补上。

封面墙一屏有几十张图，曲库有上千张。如果在绘制时同步解码原图，滚动会一顿一顿；
这里把每张封面缩成一张固定尺寸的缩略图，存在曲库的 .djtool/thumbs 里，
下次打开直接读缩略图。内存里只保留最近用到的一批。
"""
from collections import OrderedDict
import hashlib
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QImage, QPixmap

BASE = 360          # 缩略图边长（像素）；封面墙里一格约 180 点，2x 屏正好
KEEP_LARGE = 320    # 内存里保留的大图数量，约 160 MB 上限
KEEP_SMALL = 2000


class _Emitter(QObject):
    loaded = Signal(str, QImage)


class _Job(QRunnable):
    def __init__(self, path, thumb, emitter):
        super().__init__()
        self.path, self.thumb, self.emitter = path, thumb, emitter

    def run(self):
        image = QImage(str(self.thumb)) if self.thumb and self.thumb.is_file() else QImage()
        if image.isNull():
            source = QImage(self.path)
            if not source.isNull():
                side = min(source.width(), source.height())
                image = source.copy((source.width() - side) // 2, (source.height() - side) // 2, side, side).scaled(
                    BASE, BASE, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                if self.thumb:
                    try:
                        self.thumb.parent.mkdir(parents=True, exist_ok=True)
                        image.save(str(self.thumb), 'JPG', 86)
                    except OSError:
                        pass   # 缩略图只是加速用，写不了就每次现算
        try:
            self.emitter.loaded.emit(self.path, image)
        except RuntimeError:
            pass   # 应用正在退出，接收方已经销毁


class CoverLoader(QObject):
    ready = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.emitter = _Emitter(self)
        self.emitter.loaded.connect(self._loaded)
        self.large = OrderedDict()
        self.small = OrderedDict()
        self.pending = set()
        self.failed = set()
        self.cache_dir = None

    def set_library(self, root):
        self.cache_dir = Path(root) / '.djtool' / 'thumbs' if root else None

    def _thumb(self, path):
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(path.encode('utf-8')).hexdigest() + '.jpg')

    def large_pixmap(self, path):
        """封面墙用的大图；还没准备好时返回 None，并在后台开始加载。"""
        if not path or path in self.failed:
            return None
        pixmap = self.large.get(path)
        if pixmap is not None:
            self.large.move_to_end(path)
            return pixmap
        if path not in self.pending:
            self.pending.add(path)
            self.pool.start(_Job(path, self._thumb(path), self.emitter))
        return None

    def small_pixmap(self, path, size):
        """列表里的小封面（按点计）；从大图缩出来并缓存。"""
        key = (path, size)
        pixmap = self.small.get(key)
        if pixmap is not None:
            return pixmap
        large = self.large_pixmap(path)
        if large is None:
            return None
        pixmap = large.scaled(size * 2, size * 2, Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(2)
        self.small[key] = pixmap
        if len(self.small) > KEEP_SMALL:
            self.small.popitem(last=False)
        return pixmap

    def _loaded(self, path, image):
        self.pending.discard(path)
        if image.isNull():
            self.failed.add(path)
            return
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(2)
        self.large[path] = pixmap
        if len(self.large) > KEEP_LARGE:
            self.large.popitem(last=False)
        self.ready.emit(path)

    def wait(self):
        self.pool.waitForDone(3000)


_loader = None


def loader():
    global _loader
    if _loader is None:
        _loader = CoverLoader()
    return _loader
