"""收歌文件夹：记住几个常用的下载目录（比如网易云客户端的下载文件夹），
回到应用时数一数里面有没有还没收过的 MP3 / NCM，点一下就全部收进来。

只在本机读目录，不联网。源文件始终留在原处；这里只记录“哪些文件已经处理过”，
这样同一个文件不会一遍遍被提示。设置保存在曲库的 .djtool/watch-folders.json。
"""
import json
import os
from pathlib import Path
import tempfile
import time

AUDIO = ('.mp3', '.ncm')
SKIP_DIRECTORIES = ('_Serato_', '_Serato_Backup')
_MAX_BYTES = 16 * 1024 * 1024
_CORRUPT = '收歌文件夹的设置文件损坏，没有读取或改动它'


class WatchFolders:
    def __init__(self, library_root, defaults=()):
        self.root = Path(library_root)
        self.path = self.root / '.djtool' / 'watch-folders.json'
        # 默认文件夹（存在才算）不写进设置；用户移除后记在 removed 里，不再自动出现。
        self.defaults = [str(Path(item).expanduser()) for item in defaults]

    # ---- 设置文件 -------------------------------------------------------

    def _load(self):
        if not self.path.exists():
            return {'folders': [], 'removed': [], 'seen': {}}
        try:
            if self.path.is_symlink() or self.path.stat().st_size > _MAX_BYTES:
                raise ValueError(_CORRUPT)
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or not isinstance(value.get('folders'), list)
                    or not isinstance(value.get('seen'), dict) or not isinstance(value.get('removed', []), list)
                    or not all(isinstance(item, dict) and isinstance(item.get('path'), str)
                               for item in value['folders'])):
                raise ValueError(_CORRUPT)
            value.setdefault('removed', [])
            return value
        except (OSError, TypeError, ValueError) as error:
            # 不悄悄重置：重置会让所有旧文件重新被当成“新文件”。
            raise ValueError(_CORRUPT) from error

    def _save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.watch-folders-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # ---- 文件夹 ---------------------------------------------------------

    def folders(self):
        value = self._load()
        result = [{'path': item['path'], 'auto': bool(item.get('auto'))} for item in value['folders']]
        listed = {item['path'] for item in result}
        for path in self.defaults:
            if path not in listed and path not in value['removed'] and Path(path).is_dir():
                result.append({'path': path, 'auto': False})
        return result

    def add(self, path):
        path = str(Path(path).expanduser().resolve())
        if any(part in SKIP_DIRECTORIES for part in Path(path).parts):
            raise ValueError('Serato 自己的文件夹不能用来收歌')
        if not Path(path).is_dir():
            raise ValueError('这个文件夹不存在')
        value = self._load()
        value['removed'] = [item for item in value['removed'] if item != path]
        if path not in {item['path'] for item in value['folders']}:
            value['folders'].append({'path': path, 'auto': False})
        self._save(value)
        return path

    def remove(self, path):
        value = self._load()
        value['folders'] = [item for item in value['folders'] if item['path'] != path]
        if path in self.defaults and path not in value['removed']:
            value['removed'].append(path)
        self._save(value)

    def set_auto(self, path, auto):
        value = self._load()
        for item in value['folders']:
            if item['path'] == path:
                item['auto'] = bool(auto)
                break
        else:
            value['folders'].append({'path': path, 'auto': bool(auto)})
        self._save(value)

    # ---- 找新文件 -------------------------------------------------------

    def new_files(self, folder, known=(), now=None):
        """文件夹里还没处理过的 MP3 / NCM，新的排前面。known 是曲库已经记下的源文件路径。"""
        seen = self._load()['seen']
        now = time.time() if now is None else now
        library = str(self.root) + os.sep
        found = []
        for root, directories, names in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories
                              if not name.startswith('.') and name not in SKIP_DIRECTORIES]
            if (root + os.sep).startswith(library):
                directories[:] = []
                continue
            for name in names:
                if name.startswith('.') or Path(name).suffix.lower() not in AUDIO:
                    continue
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                # 还在下载的文件（几秒内刚改动过）先不算，等它写完。
                if not stat.st_size or now - stat.st_mtime < 3:
                    continue
                if seen.get(path) == [stat.st_size, stat.st_mtime_ns] or path in known:
                    continue
                found.append((stat.st_mtime, path))
        return [path for _, path in sorted(found, reverse=True)]

    def mark_seen(self, paths):
        value = self._load()
        for path in paths:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            value['seen'][str(path)] = [stat.st_size, stat.st_mtime_ns]
        self._save(value)
