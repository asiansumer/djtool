"""未来音频保存位置与只读索引边界；不操作 Serato 的数据库或 Set 文件。"""
import json
import os
from pathlib import Path
import tempfile

STORAGE_VERSION = 1
PROTECTED_NAMES = frozenset({'_serato_', '_serato_backup'})
AUDIO_SUFFIXES = frozenset({'.mp3', '.wav', '.flac', '.aiff', '.aif', '.m4a', '.mp4'})
DEFAULT_SERATO_ROOT = Path.home() / 'Documents' / 'Serato'


def _absolute(path):
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    # macOS 的 /var、/tmp 是固定系统别名；只接受其标准 /private 对应路径。
    # 用户音乐目录中的软链接仍逐层拒绝，不允许用它跳出所选目录。
    for alias, physical in ((Path('/var'), Path('/private/var')), (Path('/tmp'), Path('/private/tmp'))):
        if alias.is_symlink() and alias.resolve() == physical and (candidate == alias or alias in candidate.parents):
            candidate = physical / candidate.relative_to(alias)
    return candidate


def checked_path(path):
    """保留用户选择的真实目录边界；任何一层软链接都不作托管写入或索引。"""
    candidate = _absolute(path)
    if any(part.casefold() in PROTECTED_NAMES for part in candidate.parts):
        raise ValueError('不能在 Serato 数据库或备份目录内保存或索引音乐')
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise ValueError('音乐路径包含软链接，请选择实际目录或文件')
    return candidate


def validate_music_root(path):
    candidate = checked_path(path)
    if candidate.exists() and not candidate.is_dir():
        raise ValueError('音乐保存位置必须是文件夹')
    for parent in candidate.parents:
        if parent.exists() and not parent.is_dir():
            raise ValueError('音乐保存位置的父路径不是文件夹')
    return candidate


def storage_path(library_root):
    return Path(library_root) / '.djtool' / 'storage.json'


def load_music_root(library_root):
    """旧曲库默认 Audio；读取配置不创建外部目录，也不移动任何已有文件。"""
    root = Path(library_root)
    config = storage_path(root)
    if config.is_symlink() or config.parent.is_symlink():
        raise ValueError('曲库保存设置包含软链接，未读取')
    if not config.exists():
        return root / 'Audio'
    try:
        if config.stat().st_size > 65536:
            raise ValueError
        value = json.loads(config.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != STORAGE_VERSION:
            raise ValueError
        music_root = value.get('music_root')
        if not isinstance(music_root, str) or not Path(music_root).is_absolute():
            raise ValueError
        return validate_music_root(music_root)
    except (TypeError, ValueError) as error:
        raise ValueError('曲库保存设置损坏，请重新选择音乐保存目录') from error


def configure_music_root(library_root, path):
    """只更改未来入库位置；配置失败只清理本次新建且仍为空的目录。"""
    destination = validate_music_root(path)
    config = storage_path(library_root)
    if config.is_symlink() or config.parent.is_symlink():
        raise ValueError('曲库保存设置包含软链接，未写入')
    created = []
    temporary = None
    try:
        missing = []
        current = destination
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
        validate_music_root(destination)
        config.parent.mkdir(parents=True, exist_ok=True)
        content = {'version': STORAGE_VERSION, 'music_root': str(destination)}
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config.parent,
                                         prefix='.storage-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(content, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config)
        return destination
    except Exception:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                # 并发放入的用户文件不属于本次创建，绝不递归删除。
                pass
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def reference_path(source, allowed_root):
    root = checked_path(allowed_root)
    if not root.is_dir():
        raise ValueError('要索引的音乐目录不存在或离线')
    path = checked_path(source)
    if path == root or root not in path.parents:
        raise ValueError('歌曲不在所选择的音乐目录内，未索引')
    if not path.is_file():
        raise FileNotFoundError('音乐文件不存在或离线')
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError('只读索引支持 MP3、WAV、FLAC、AIFF 和 M4A')
    return path


def preview_existing(root):
    """只枚举文件和数量；不读写 Serato database、标签或任何 SQLite。"""
    root = checked_path(root)
    if not root.is_dir():
        raise ValueError('音乐目录不存在或离线')
    paths = []
    skipped = 0
    def read_error(error):
        raise error
    for parent, directories, files in os.walk(root, followlinks=False, onerror=read_error):
        keep = []
        for name in directories:
            if name.casefold() in PROTECTED_NAMES or (Path(parent) / name).is_symlink():
                skipped += 1
            elif not name.startswith('.'):
                keep.append(name)
        directories[:] = keep
        for name in files:
            path = Path(parent) / name
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            try:
                paths.append(str(reference_path(path, root)))
            except (ValueError, OSError):
                skipped += 1
    return {'root': str(root), 'audio_count': len(paths), 'paths': paths, 'skipped': skipped}
