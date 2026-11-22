"""读取本机 Serato 的 BPM 与调性；只写 DJTOOL 缓存，不改 Serato 或音频。

本机 schema 202（Serato 4.0）与 203（Serato 5.0）的 root.sqlite 已核对：
portable_id 从卷根开始，asset.bpm 保留 Serato 当前值的小数精度，asset.key
是 Serato 分析出的调性文本。master 的外置卷与流媒体不能套用这个路径规则。
"""
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata

from djtool.audio_tasks import AudioCancelled, check_cancel


class SeratoBPMError(ValueError):
    pass


class _SnapshotChanged(Exception):
    pass


MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_ROWS = 500_000
SUPPORTED_SCHEMAS = (202, 203)
REQUIRED_COLUMNS = {'portable_id', 'bpm', 'file_size', 'type', 'third_party_type',
                    'is_missing', 'is_corrupt', 'is_stale'}


def default_database():
    return Path.home() / 'Library/Application Support/Serato/Library/root.sqlite'


def _signature(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(source, destination, cancel):
    """只以 rb 打开真实库；WAL 在私有副本里由 SQLite 读取，不创建源库 SHM。

两轮检查覆盖复制期间的提交/检查点，正在变化或热回滚日志未完成时停止。
失败不会用半份结果覆盖上一轮 BPM。
"""
    wal = Path(str(source) + '-wal')
    journal = Path(str(source) + '-journal')
    for _ in range(2):
        check_cancel(cancel)
        before = {path: _signature(path) for path in (source, wal, journal)}
        if before[source] is None:
            raise SeratoBPMError('未找到 Serato 4.0 的本机曲库，请先在 Serato 导入歌曲。')
        if before[journal] and before[journal][2]:
            raise SeratoBPMError('Serato 数据库正在提交，请稍后再读取；上次 BPM 保留。')
        if any(info and info[2] > MAX_DATABASE_BYTES for info in before.values()):
            raise SeratoBPMError('Serato 数据库超过本次读取范围；上次 BPM 保留。')
        try:
            for path, target in ((source, destination), (wal, Path(str(destination) + '-wal'))):
                if before[path] is None:
                    target.unlink(missing_ok=True)
                    continue
                with path.open('rb') as stream, target.open('wb') as output:
                    if _signature(path) != before[path]:
                        raise _SnapshotChanged()
                    descriptor = os.fstat(stream.fileno())
                    if (descriptor.st_dev, descriptor.st_ino, descriptor.st_size,
                            descriptor.st_mtime_ns, descriptor.st_ctime_ns) != before[path]:
                        raise _SnapshotChanged()
                    remaining = before[path][2]
                    while remaining:
                        check_cancel(cancel)
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise _SnapshotChanged()
                        output.write(chunk)
                        remaining -= len(chunk)
                    if stream.read(1):
                        raise _SnapshotChanged()
            if any(_signature(path) != before[path] for path in before):
                raise _SnapshotChanged()
            return
        except (_SnapshotChanged, FileNotFoundError):
            continue
    raise SeratoBPMError('Serato 数据库正在更新，请稍后再读取；上次 BPM 保留。')


def _path_key(value, *, portable=False):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return None
    if '\\' in value or '://' in value:
        return None
    path = value if value.startswith('/') else '/' + value if portable else None
    if path is None or any(part in ('.', '..') for part in path.split('/')):
        return None
    # APFS 的 Unicode 等价名字对应同一播放路径；不按歌名、大小写或文件名猜测。
    return unicodedata.normalize('NFC', path)


def read_serato_bpms(tracks, database=None, cancel=lambda: False):
    """返回完整成功读取的路径→BPM，失败/取消不产生可应用的部分结果。"""
    check_cancel(cancel)
    source = Path(database or default_database()).expanduser().resolve()
    targets = {}
    for track in tracks:
        check_cancel(cancel)
        path = track['path']
        key = _path_key(path)
        if key is None:
            raise SeratoBPMError('曲库含有无效播放路径，未刷新 BPM。')
        targets.setdefault(key, []).append(path)
    values, blocked, signatures, keys = {}, set(), {}, {}
    skipped = 0
    try:
        with tempfile.TemporaryDirectory(prefix='djtool-serato-bpm-') as temporary:
            snapshot = Path(temporary) / 'root.sqlite'
            _snapshot(source, snapshot, cancel)
            check_cancel(cancel)
            # 真实目录从未交给 SQLite 打开；它可能需要的 WAL/SHM 只在临时副本中。
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('PRAGMA query_only=ON')
                db.set_progress_handler(lambda: int(bool(cancel())), 1000)
                db.execute('BEGIN')
                if db.execute('PRAGMA user_version').fetchone()[0] not in SUPPORTED_SCHEMAS:
                    raise SeratoBPMError('当前 Serato 数据库版本暂不支持，未刷新 BPM。')
                columns = {row[1] for row in db.execute('PRAGMA table_info(asset)')}
                if not REQUIRED_COLUMNS <= columns or 'location_id' in columns:
                    raise SeratoBPMError('Serato 本机曲库结构不匹配，未刷新 BPM。')
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise SeratoBPMError('Serato 曲库副本校验失败，未刷新 BPM。')
                count = 0
                # 调性列是可选的补充信息；缺列时只读 BPM，不影响原有校验。
                key_column = 'key' if 'key' in columns else "''"
                for portable, bpm, size, kind, provider, missing, corrupt, stale, musical_key in db.execute(
                        'SELECT portable_id,bpm,file_size,type,third_party_type,'
                        f'is_missing,is_corrupt,is_stale,{key_column} FROM asset'):
                    check_cancel(cancel)
                    count += 1
                    if count > MAX_ROWS:
                        raise SeratoBPMError('Serato 曲目数量超过本次读取范围，未刷新 BPM。')
                    key = _path_key(portable, portable=True)
                    paths = targets.get(key, [])
                    if not paths:
                        continue
                    if (kind != 'audio' or provider != 0 or missing != 0 or corrupt != 0 or stale != 0
                            or not isinstance(bpm, (float, int)) or isinstance(bpm, bool)
                            or not math.isfinite(bpm) or not 20 <= bpm <= 400
                            or not isinstance(size, int) or size <= 0 or len(paths) != 1):
                        skipped += 1
                        continue
                    path = paths[0]
                    if not Path(path).is_file():
                        skipped += 1
                        continue
                    info = _signature(Path(path))
                    if info is None or info[2] != size:
                        skipped += 1
                        continue
                    if key in blocked:
                        skipped += 1
                        continue
                    number = float(bpm)
                    if path in values and values[path] != number:
                        # 同一路径在库里有冲突值，不随查询顺序选择其中一个。
                        values.pop(path)
                        signatures.pop(path, None)
                        keys.pop(path, None)
                        blocked.add(key)
                        skipped += 1
                        continue
                    values[path], signatures[path] = number, info
                    if isinstance(musical_key, str) and 0 < len(musical_key.strip()) <= 12:
                        keys[path] = musical_key.strip()
        check_cancel(cancel)
        for path in list(values):
            if _signature(Path(path)) != signatures[path]:
                values.pop(path)
                keys.pop(path, None)
                skipped += 1
        return {'values': values, 'keys': keys, 'source_path': str(source), 'skipped': skipped}
    except AudioCancelled:
        raise
    except (OSError, sqlite3.Error) as error:
        check_cancel(cancel)
        raise SeratoBPMError('无法完整读取 Serato BPM，请稍后重试；上次 BPM 保留。') from error


def sync_library(library, database=None, cancel=lambda: False):
    """读完并核对后一次更新 DJTOOL，音频、Serato、整理与 Set 均不写入。"""
    tracks = library.tracks()
    result = read_serato_bpms(tracks, database, cancel)
    check_cancel(cancel)
    counts = library.replace_serato_bpms(result['values'], result['source_path'], cancel=cancel,
                                         keys=result['keys'])
    total = len(library.tracks())
    return {**counts, 'unmatched_serato': counts['missing'], 'missing': total-counts['matched'],
            'total': total, 'skipped': result['skipped'], 'cancelled': False}
