"""收歌与 Set 独立：仅维护 DJTOOL/收歌 的文件引用，音频仍留在原路径。

待同步来源就是已提交的曲库记录，重启也不会丢失；不以 reviewed 标记控制同步。
Serato 运行期间只等待，关闭后由已完成原生验收的 Crate 适配器写入。
"""
import hashlib
import json
from pathlib import Path
import tempfile

from djtool.audio_tasks import check_cancel
from djtool.serato import (SeratoCrateAdapter, SeratoError,
                    _atomic_write, _check_no_symlinks, verify_native_set)

COLLECTION = {'id': 'djtool-auto-intake', 'name': '收歌', 'kind': 'set'}


def _load_state(path):
    _check_no_symlinks(path)
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        if (type(value.get('version')) is not int or value['version'] != 1
                or not isinstance(value.get('digest'), str)):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError) as error:
        raise SeratoError('Serato 自动导入记录损坏，未覆盖；歌曲仍保留在待整理') from error


def _native_received(plan):
    """只读复制真实库/WAL，在私有副本确认原生 Serato 已接收；失败不冒充已导入。"""
    from djtool.serato_bpm import _snapshot
    source = Path.home() / 'Library/Application Support/Serato/Library/root.sqlite'
    with tempfile.TemporaryDirectory(prefix='djtool-serato-intake-') as temporary:
        target = Path(temporary) / 'root.sqlite'
        _snapshot(source, target, lambda: False)
        return verify_native_set(plan, target)['native_content_matches']


def sync_library(library, *, adapter=None, cancel=lambda: False, native_check=None):
    check_cancel(cancel)
    adapter = adapter or SeratoCrateAdapter(library.root)
    # 原序稳定、不按标题猜测。相同播放路径只放一次，同名 remix 仍各自保留。
    rows = sorted(library.tracks(), key=lambda t: (t['created_at'], t['id']))
    tracks, seen, missing = [], set(), 0
    for track in rows:
        check_cancel(cancel)
        path = Path(track['path'])
        if not path.is_file():
            missing += 1
            continue
        key = str(path.resolve())
        if key not in seen:
            tracks.append(track)
            seen.add(key)
    def result(status, **extra):
        return {'status': status, 'count': len(tracks), 'missing': missing, **extra}
    if not tracks:
        return result('missing' if missing else 'empty')
    plan = adapter.preview_set(COLLECTION, tracks)
    digest = hashlib.sha256(plan.payload).hexdigest()
    state_path = library.root / '.djtool/serato-intake.json'
    state = _load_state(state_path)
    check_cancel(cancel)
    if state.get('digest') == digest:
        # 写入 Crate 与原生库已接收是两个状态，不能混为成功。
        if state.get('received') is True:
            return result('received')
        try:
            received = (native_check or _native_received)(plan)
        except (OSError, ValueError):
            received = False
        if received:
            check_cancel(cancel)
            state['received'] = True
            _atomic_write(state_path, json.dumps(state, ensure_ascii=False).encode('utf-8'))
            return result('received')
        return result('ready')
    # 不自动关闭 Serato；用户退出后，下轮检查会补同步。
    if adapter.process_checker():
        return result('waiting')
    check_cancel(cancel)
    written = adapter.sync_set(COLLECTION, tracks)
    state = {'version': 1, 'digest': digest, 'count': len(tracks), 'received': False}
    _check_no_symlinks(state_path)
    _atomic_write(state_path, json.dumps(state, ensure_ascii=False).encode('utf-8'))
    return result('ready', backup_path=written['backup_path'])


def status_text(result):
    count = result.get('count', 0)
    text = {
        'empty': 'Serato · 收歌后自动导入',
        'waiting': f'Serato · {count} 首待同步，关闭 Serato 后自动继续',
        'ready': f'Serato · {count} 首已准备导入，下次打开 Serato 即可查看',
        'received': f'Serato · 已导入 {count} 首 · DJTOOL / 收歌',
        'error': 'Serato · 自动导入未完成，歌曲已保留在待整理',
        'missing': 'Serato · 播放文件缺失，恢复文件后自动继续',
    }.get(result['status'], 'Serato · 正在核对收歌')
    if result.get('missing'):
        text += f" · {result['missing']} 首文件缺失未导入"
    return text
