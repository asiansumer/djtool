"""显式把现有 Serato 音乐原地登记到已有 DJTOOL；Serato 数据只做字节摘要核对。"""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from djtool.library import Library
from djtool.storage import checked_path, preview_existing


def _signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _digest(path):
    if path.is_symlink():
        raise ValueError(f'文件已变为软链接，未读取：{path}')
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise ValueError(f'文件在摘要读取前发生变化：{path}')
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if _signature(path.stat()) != _signature(before):
        raise ValueError(f'文件在摘要读取时发生变化：{path}')
    return {'path': str(path), 'sha256': digest.hexdigest(), 'size': before.st_size,
            'mtime_ns': before.st_mtime_ns, 'inode': before.st_ino, 'device': before.st_dev,
            'ctime_ns': before.st_ctime_ns}


def _progress(phase, completed, total):
    if completed == total or completed % 50 == 0:
        print(json.dumps({'phase': phase, 'completed': completed, 'total': total}), flush=True)


def _hash_all(paths, phase):
    records = []
    for number, path in enumerate(paths, 1):
        records.append(_digest(path))
        _progress(phase, number, len(paths))
    return records


def _verify_files(records, phase):
    changes = []
    for number, original in enumerate(records, 1):
        path = Path(original['path'])
        try:
            current = _digest(path)
            if any(current[key] != original[key] for key in ('sha256', 'size', 'mtime_ns', 'inode', 'device', 'ctime_ns')):
                changes.append(str(path))
        except (OSError, ValueError):
            changes.append(str(path))
        _progress(phase, number, len(records))
    return changes


def _standard_log_link(path):
    """Serato 的 Logs/DJ.INFO 只作同目录日志别名；不允许其他软链接或越界目标。"""
    if path.name != 'DJ.INFO' or path.parent.name != 'Logs' or not path.is_symlink():
        return None
    before = path.lstat()
    target_name = os.readlink(path)
    if (Path(target_name).is_absolute() or Path(target_name).name != target_name
            or '/' in target_name or '\\' in target_name or not target_name.endswith('.log')):
        return None
    target = path.parent / target_name
    if (target.is_symlink() or not target.is_file()
            or target.resolve().parent != path.parent.resolve()):
        return None
    if _signature(path.lstat()) != _signature(before):
        raise ValueError(f'Serato 日志别名在读取时发生变化：{path}')
    return {'path': str(path), 'target': target_name, 'size': before.st_size,
            'mtime_ns': before.st_mtime_ns, 'inode': before.st_ino,
            'device': before.st_dev, 'ctime_ns': before.st_ctime_ns, 'mode': before.st_mode}


def _verify_log_links(records):
    changes = []
    for original in records:
        try:
            # 比较链接本身的文字及 lstat，不沿链接打开文件；目标另按普通日志文件核对摘要。
            current = _standard_log_link(Path(original['path']))
            if current != original:
                changes.append(original['path'])
        except (OSError, ValueError):
            changes.append(original['path'])
    return changes


def _read_connection(database):
    # 仅 DJTOOL 自己的 SQLite 用只读连接；Serato SQLite 从不传入这里。
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def _snapshot(database):
    with closing(_read_connection(database)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('现有 DJTOOL 数据库完整性检查失败，未开始接入')
        return {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('tracks', 'collections', 'members')}


def _backup(database):
    directory = checked_path(database.parent / 'backups')
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = directory / f'before-serato-connect-{stamp}-{uuid.uuid4().hex[:12]}.sqlite3'
    # 在线备份包含 WAL 已提交状态；独占新路径，不复制数据库主文件冒充完整快照。
    with backup.open('xb'):
        pass
    try:
        with closing(_read_connection(database)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('DJTOOL 在线备份完整性检查失败，未开始接入')
        return backup
    except Exception:
        # 即使失败也保留此次备份，便于核对；不删除已有备份或源数据库。
        raise


def _preserved(previous, current):
    changes = {}
    keys = {'tracks': ('id',), 'collections': ('id',), 'members': ('collection_id', 'track_id')}
    for table, fields in keys.items():
        index = {tuple(row[field] for field in fields): row for row in current[table]}
        changed = []
        for original in previous[table]:
            identity = tuple(original[field] for field in fields)
            present = index.get(identity)
            if present is None or any(present.get(key) != value for key, value in original.items()):
                changed.append(list(identity))
        changes[table] = changed
    return changes


def _serato_files(serato_root, extra_roots):
    # 包括所选音乐目录的两份 Serato 数据、当前 Mac 原生 Serato 数据及用户指定位置。
    roots = [serato_root / '_Serato_', serato_root / '_Serato_Backup',
             Path.home() / 'Music' / '_Serato_', Path.home() / 'Music' / '_Serato_Backup',
             Path.home() / 'Library' / 'Application Support' / 'Serato' / 'Library', *extra_roots]
    paths = set()
    scanned = []
    skipped_links = []
    log_links = {}
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        if root.is_symlink():
            skipped_links.append(str(root))
            continue
        if not root.is_dir():
            raise ValueError(f'Serato 数据核对位置必须是目录：{root}')
        root = root.resolve()
        if str(root) in scanned:
            continue
        scanned.append(str(root))
        def scan_error(error):
            raise error
        for parent, directories, names in os.walk(root, followlinks=False, onerror=scan_error):
            kept = []
            for name in directories:
                path = Path(parent) / name
                if path.is_symlink():
                    skipped_links.append(str(path))
                else:
                    kept.append(name)
            directories[:] = kept
            for name in names:
                path = Path(parent) / name
                if path.is_symlink():
                    standard_log = _standard_log_link(path)
                    if standard_log is None:
                        skipped_links.append(str(path))
                    else:
                        log_links[str(path)] = standard_log
                        # 目标仍是普通文件；无论扫描顺序，都把实际日志加入字节摘要列表。
                        paths.add(path.parent / standard_log['target'])
                elif path.is_file():
                    paths.add(path)
    return scanned, sorted(paths), skipped_links, list(log_links.values())


def run(args):
    library_root = checked_path(args.library)
    serato_root = checked_path(args.serato_root)
    database = checked_path(library_root / '.djtool' / 'library.sqlite3')
    if not database.is_file():
        raise ValueError('--library 必须指向已有 DJTOOL 曲库，不会创建或重置旧数据库')
    report = {'version': 1, 'mode': 'apply' if args.apply else 'preview',
              'library_root': str(library_root), 'serato_root': str(serato_root),
              'music_root': str(serato_root / 'DJTOOL Audio'), 'backup_path': None,
              'added': 0, 'duplicates': 0, 'errors': [], 'fatal_error': None,
              'source_changes': [], 'serato_data_changes': [], 'old_record_changes': {},
              'serato_log_link_changes': [], 'serato_log_links_before': [],
              'serato_sqlite_opened': False, 'audio_copied_or_moved': False, 'passed': False}
    before_audio = []
    before_serato = []
    before_log_links = []
    old = None
    try:
        inventory = preview_existing(serato_root)
        paths = sorted(Path(path) for path in inventory['paths'])
        report.update(audio_count=len(paths), skipped_audio_entries=inventory['skipped'])
        if args.expected_audio_count is not None and len(paths) != args.expected_audio_count:
            raise ValueError('音乐数量与明确指定的预览数量不一致，未开始接入')
        # 只读预览不创建备份。实际接入先取得完整 SQLite 在线备份，再初始化 Library。
        if args.apply:
            backup = _backup(database)
            report['backup_path'] = str(backup)
            old = _snapshot(backup)
        else:
            old = _snapshot(database)
        report['old_counts'] = {table: len(rows) for table, rows in old.items()}
        before_audio = _hash_all(paths, 'hash_original_audio')
        data_roots, data_paths, skipped_links, before_log_links = _serato_files(serato_root, args.serato_data_root)
        report.update(serato_data_roots=data_roots, serato_data_file_count=len(data_paths),
                      skipped_serato_links=skipped_links, serato_log_links_before=before_log_links)
        if skipped_links:
            raise ValueError('Serato 数据目录含软链接，无法完成全部原文件验证，未开始接入')
        before_serato = _hash_all(data_paths, 'hash_original_serato_data')
        report['audio_before'] = before_audio
        report['serato_data_before'] = before_serato
        if args.apply:
            library = Library(library_root)
            library.configure_music_root(serato_root / 'DJTOOL Audio')
            for number, source in enumerate(paths, 1):
                try:
                    _, added = library.reference_existing(source, serato_root)
                    report['added' if added else 'duplicates'] += 1
                except Exception as error:
                    report['errors'].append({'path': str(source), 'error': str(error)})
                _progress('reference_existing_audio', number, len(paths))
        report['old_record_changes'] = _preserved(old, _snapshot(database))
    except Exception as error:
        report['fatal_error'] = str(error)
    finally:
        # 发生并发变更也只报告差异；不替用户删除、恢复或覆盖任何音乐/Serato 文件。
        report['source_changes'] = _verify_files(before_audio, 'verify_original_audio')
        report['serato_data_changes'] = _verify_files(before_serato, 'verify_original_serato_data')
        report['serato_log_link_changes'] = _verify_log_links(before_log_links)
        if report['source_changes'] or report['serato_data_changes'] or report['serato_log_link_changes']:
            report['fatal_error'] = report['fatal_error'] or '原始音频或 Serato 数据在接入期间发生变化；只报告差异，未删除或恢复用户文件'
        if old is not None:
            try:
                report['old_record_changes'] = _preserved(old, _snapshot(database))
            except Exception as error:
                report['fatal_error'] = report['fatal_error'] or str(error)
        report['passed'] = not (report['fatal_error'] or report['errors'] or report['source_changes']
                                or report['serato_data_changes']
                                or report['serato_log_link_changes']
                                or any(report['old_record_changes'].values()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--library', type=Path, required=True, help='已有 DJTOOL 曲库的准确根目录')
    parser.add_argument('--serato-root', type=Path, required=True, help='含既有音乐的准确外层目录')
    parser.add_argument('--serato-data-root', type=Path, action='append', default=[],
                        help='额外只做摘要验证的 Serato 数据目录，可重复指定')
    parser.add_argument('--expected-audio-count', type=int)
    parser.add_argument('--report', type=Path, default=ROOT / 'work' / 'serato-connect' / 'result.json')
    args = parser.parse_args()
    try:
        report = run(args)
    except Exception as error:
        report = {'passed': False, 'fatal_error': str(error), 'mode': 'apply' if args.apply else 'preview'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {key: value for key, value in report.items()
               if key not in ('audio_before', 'serato_data_before')}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
