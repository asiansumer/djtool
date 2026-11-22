"""网易云下载的音质偏好。只有一种取歌方式（在线解析），不读取网易云账号或 Cookie。"""
import json
import os
from pathlib import Path
import tempfile

DEFAULT_SETTINGS = {'backend': 'site', 'quality': 'exhigh'}
BACKENDS = ('site', 'local', 'official')
QUALITIES = ('exhigh', 'lossless', 'hires')
SETTINGS_VERSION = 3


def _settings_path(library):
    return Path(library.root) / '.djtool' / 'netease-settings.json'


def _validate(backend, quality):
    if backend not in BACKENDS or quality not in QUALITIES:
        raise ValueError('网易云音源或音质设置无效，请重新选择。')
    return {'backend': backend, 'quality': quality}


def _read_record(path):
    try:
        # 设置只有少量文本；损坏文件不应触发回退或重置已有队列。
        if path.stat().st_size > 65536:
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError
        version = value.get('format_version', 1)
        if type(version) is not int or version not in (1, 2, SETTINGS_VERSION):
            raise ValueError
        return _validate(value.get('backend'), value.get('quality')), version
    except (ValueError, TypeError) as error:
        raise ValueError('网易云音源设置文件无法读取，请在设置中重新保存。') from error


def _read(path):
    return _read_record(path)[0]


def _write_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent,
                                         prefix='.netease-settings-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # 同目录原子替换，不留下半份 JSON；文件内不含 Cookie、签名 URL 或账号。
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write(path, settings):
    content = {'format_version': SETTINGS_VERSION, **settings}
    _write_bytes(path, (json.dumps(content, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


def _persist(library, path, settings, reset):
    previous = path.read_bytes() if path.exists() else None
    # 先完成原子文件替换，磁盘失败时不修改队列；队列事务失败则恢复旧偏好。
    _write(path, settings)
    if reset:
        try:
            library.reset_netease_backend_jobs()
        except Exception:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes(path, previous)
            raise


def load_settings(library=None):
    if library is None:
        return dict(DEFAULT_SETTINGS)
    path = _settings_path(library)
    if path.exists():
        settings, version = _read_record(path)
        if version < SETTINGS_VERSION or settings['backend'] != 'site':
            # 旧版本有过“官方账号”和“本机客户端”两种取歌方式，都已经移除。
            # 留下的设置只升级一次：保留音质，把之前因为权限卡住的任务放回待下载。
            settings = {'backend': 'site', 'quality': settings['quality']}
            _persist(library, path, settings, reset=True)
        return settings
    # 保存文件即是首次升级标记。成功后再次打开不会自动重试不可用歌曲。
    _persist(library, path, DEFAULT_SETTINGS, reset=True)
    return dict(DEFAULT_SETTINGS)


def save_settings(library, backend, quality):
    settings = _validate(backend, quality)
    if backend != 'site':
        raise ValueError('网易云音源或音质设置无效，请重新选择。')
    path = _settings_path(library)
    try:
        previous = _read(path) if path.exists() else None
    except ValueError:
        # 用户明确重新保存可修复损坏偏好；普通读取仍报错，绝不悄悄回退。
        previous = None
    _persist(library, path, settings, reset=previous is None or previous['backend'] != backend)
    return dict(settings)


def create_music_client(library=None):
    from djtool.netease_site import NeteaseSiteClient
    return NeteaseSiteClient(quality=load_settings(library)['quality'])
