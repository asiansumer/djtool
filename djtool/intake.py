"""统一本地入口：稳定副本 → NCM 按需转换 → 入库。"""
import hashlib
import json
from pathlib import Path
import tempfile
from djtool.audio_tasks import AudioError, check_cancel
from djtool.ncm import MAX_SOURCE, convert, music_id


def import_audio(library, source, progress=lambda message: None, cancel=lambda: False, *, expected_netease_ids=None):
    source = Path(source).expanduser().resolve()
    check_cancel(cancel)
    if source.suffix.lower() == '.mp3':
        if expected_netease_ids is not None:
            raise AudioError('MP3 缺少可核对的网易云歌曲 ID，请通过导入文件接收')
        identifier, added = library.import_file(source)
    elif source.suffix.lower() == '.ncm':
        progress('检查 NCM 文件')
        staging = library.root / '.djtool' / 'converting'
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as temporary:
            folder = Path(temporary)
            snapshot = folder / 'source.ncm'
            before = source.stat()
            if before.st_size > MAX_SOURCE:
                raise AudioError('NCM 文件超过 1 GB，暂不支持')
            digest = hashlib.sha256()
            copied = 0
            with source.open('rb') as incoming, snapshot.open('xb') as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    check_cancel(cancel)
                    copied += len(chunk)
                    if copied > MAX_SOURCE:
                        raise AudioError('NCM 文件过大或仍在下载，请完成下载后重试')
                    digest.update(chunk)
                    outgoing.write(chunk)
            after = source.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino) != (after.st_size,after.st_mtime_ns,after.st_ino):
                raise AudioError('NCM 文件仍在写入，请下载完成后再导入')
            fingerprint = digest.hexdigest()
            existing = library.local_source_by_digest(fingerprint)
            if existing:
                if expected_netease_ids is not None and music_id(json.loads(existing['metadata_json']).get('ncm_metadata') or {}) not in expected_netease_ids:
                    raise AudioError('文件与待收歌单 ID 不符，未导入')
                if not Path(library.track(existing['track_id'])['path']).is_file():
                    raise AudioError('歌曲已入库，但播放文件丢失，请先恢复曲库文件')
                identifier, added = existing['track_id'], False
                library.reconcile_local_jobs()
            else:
                progress('提取 NCM / 按需转 MP3')
                output, details = convert(snapshot, folder, cancel)
                if expected_netease_ids is not None and music_id(details['ncm_metadata']) not in expected_netease_ids:
                    raise AudioError('文件与待收歌单 ID 不符，未导入')
                named = folder / (source.stem[:70] + '.mp3')
                output.rename(named)
                check_cancel(cancel)
                identifier, added = library.import_file(named, local_source={
                    'digest': fingerprint, 'path': str(source), 'details': details})
    else:
        raise AudioError('请选择 MP3、NCM 文件，或包含它们的文件夹')
    progress('已入库')
    return identifier, added
