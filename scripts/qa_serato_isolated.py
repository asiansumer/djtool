"""准备 Serato 4 原生隔离实验；默认不启动 Serato，绝不写入真实库。

输出 launch-argv.json 供人工审阅后由主任务启动。CFFIXED_USER_HOME 仅影响
原生目录查询，sandbox 同时拒绝真实用户目录读写，以应对 Serato 忽略该变量。
不改变 HOME，不读 Cookie/账号，不联网，不复制原音乐，不播放音频。
--native-empty 是明确授权后的有界原生生命周期实验：最长 45 秒，终止自己启动的进程。
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid
import wave
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from djtool.serato import (SeratoCrateAdapter, decode_crate_paths, inspect_serato_storage,
                    installation_fingerprint, plan_set, validate_native_evidence,
                    verify_native_set, VALIDATION_PHASES)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup_sqlite(source, target):
    # SQLite 的 WAL 模式即使已经关闭且没有 WAL，也可能尝试创建 SHM。
    # 无 WAL/SHM 且主任务已确认 Serato 关闭时，用不可变只读源避免对真实目录写入。
    # 存在 WAL 则必须用 SQLite online backup，不能逐文件拷贝忽略未 checkpoint 数据。
    has_wal = Path(str(source) + '-wal').exists() or Path(str(source) + '-shm').exists()
    before = source.stat()
    flags = '?mode=ro' if has_wal else '?mode=ro&immutable=1'
    with closing(sqlite3.connect(source.as_uri() + flags, uri=True)) as original:
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
            if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('隔离 SQLite 快照不完整')
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('复制过程中真实 Serato 数据库变化，不能作为静态隔离依据')


def sbpl_string(value):
    # SBPL 使用字符串语法；原始路径不插入 shell。
    return json.dumps(str(value), ensure_ascii=False)


def prepare():
    folder = ROOT / 'work/serato-adapter' / ('native-' + uuid.uuid4().hex[:8])
    fake_home = folder / 'home'
    isolated_serato = fake_home / 'Music/_Serato_'
    isolated_serato.mkdir(parents=True)
    isolated_support = fake_home / 'Library/Application Support/Serato/Library'
    isolated_support.mkdir(parents=True)
    tmp = folder / 'tmp'
    tmp.mkdir()
    source_serato = Path.home() / 'Music/_Serato_'
    source_support = Path.home() / 'Library/Application Support/Serato/Library'
    before = {}
    for name in ('master.sqlite', 'root.sqlite'):
        source = source_support / name
        if source.is_file():
            backup_sqlite(source, isolated_support / name)
            before[str(source)] = sha(source)
    for name in ('database V2', 'neworder.pref'):
        source = source_serato / name
        if source.is_file():
            shutil.copyfile(source, isolated_serato / name)
            before[str(source)] = sha(source)
    (isolated_serato / 'Subcrates').mkdir()
    for source in (source_serato / 'Subcrates').glob('*.crate'):
        shutil.copyfile(source, isolated_serato / 'Subcrates' / source.name)
        before[str(source)] = sha(source)
    music = folder / 'audio'
    music.mkdir()
    tracks = []
    for index in (1, 2, 3):
        path = music / f'隔离静音 {index}.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(44100)
            audio.writeframes(b'\x00\x00' * 44100)
        tracks.append({'id': f'qa-{index}', 'title': f'静音 {index}', 'path': str(path)})
    collection = {'id': 'native-roundtrip-01', 'kind': 'set', 'name': '隔离顺序验证'}
    adapter = SeratoCrateAdapter(music, isolated_serato, isolated=True, process_checker=lambda: False)
    # 准备不运行原生 App，进程状态由主任务在实际启动前从系统 inventory 确认。
    result = adapter.sync_set(collection, [tracks[2], tracks[0], tracks[1]])
    before_update = Path(result['crate_path']).read_bytes()
    updated = adapter.sync_set(collection, tracks[::-1])
    if decode_crate_paths(Path(updated['crate_path']).read_bytes()) != tuple(str(Path(t['path']).resolve()).lstrip('/') for t in tracks[::-1]):
        raise RuntimeError('隔离更新顺序不一致')
    adapter.restore_backup(Path(updated['backup_path']).name)
    if Path(result['crate_path']).read_bytes() != before_update:
        raise RuntimeError('隔离恢复不是原始文件')
    # 默认允许系统只读依赖，拒绝所有写入及网络；只对这一次实验目录打开读写。
    # 拒绝真实用户目录读取可避免隔离库按旧引用误加载用户音乐；例外只允许本实验。
    profile = folder / 'isolation.sb'
    profile.write_text('\n'.join([
        '(version 1)', '(allow default)', '(deny network*)', '(deny file-write*)',
        '(deny user-preference-write)',
        '(deny user-preference-read (preference-domain "com.serato.seratodj"))',
        '(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.securityd.xpc") (global-name "com.apple.secd") (global-name "com.apple.securityd.systemkeychain"))',
        f'(deny file-read* (subpath {sbpl_string(Path.home())}))',
        f'(allow file-read* file-write* (subpath {sbpl_string(folder)}))',
        # 原生运行时可读取当前 App；安装目录不在用户 Home 内。
        '(allow file-read* (subpath "/Applications/Serato DJ Pro.app"))',
    ]) + '\n', encoding='utf-8')
    executable = '/Applications/Serato DJ Pro.app/Contents/MacOS/Serato DJ Pro'
    launch = ['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/env',
              'CFFIXED_USER_HOME=' + str(fake_home), 'TMPDIR=' + str(tmp) + '/', executable]
    (folder / 'launch-argv.json').write_text(json.dumps(launch, ensure_ascii=False, indent=2), encoding='utf-8')
    # 不记录完整用户数据库内容，只记录元数据文件的哈希以核对真实库没变化。
    report = {'native_started': False, 'native_verified': False,
              'version': inspect_serato_storage(music)['installed_version'],
              'folder': str(folder), 'fake_home': str(fake_home), 'serato_dir': str(isolated_serato),
              'native_database': str(isolated_support / 'root.sqlite'),
              'collection': collection, 'tracks': [tracks[2], tracks[0], tracks[1]],
              'crate_path': result['crate_path'], 'original_metadata_sha256': before,
              'serialization_update_restore_passed': True,
              'network_allowed': False, 'real_home_read_write_allowed': False,
              'launch_argv': launch}
    (folder / 'plan.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('folder', 'crate_path', 'serialization_update_restore_passed', 'native_started')}, ensure_ascii=False))
    return folder


def check_unchanged(folder):
    report = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
    changed = [path for path, digest in report['original_metadata_sha256'].items()
               if not Path(path).is_file() or sha(Path(path)) != digest]
    print(json.dumps({'original_metadata_unchanged': not changed, 'changed_paths': changed}, ensure_ascii=False))
    return not changed


def ordered_tracks(report, order):
    tracks = {track['id'].rsplit('-', 1)[-1]: track for track in report['tracks']}
    keys = order.split(',') if order != 'empty' else []
    if len(keys) != len(set(keys)) or any(key not in tracks for key in keys):
        raise ValueError('顺序必须是唯一的静音编号，例如 3,1,2 或 empty')
    return [tracks[key] for key in keys]


def record_phase(folder, phase, order):
    report = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
    phases = folder / 'phases'
    phases.mkdir(exist_ok=True)
    snapshot = phases / (phase + '.sqlite')
    if not snapshot.exists():
        backup_sqlite(Path(report['native_database']), snapshot)
    plan = plan_set(report['collection'], ordered_tracks(report, order), report['serato_dir'])
    content = verify_native_set(plan, snapshot)
    step = {'phase': phase, 'process_closed': True, 'database': str(snapshot.resolve()),
            'database_sha256': sha(snapshot), 'collection_id': plan.collection_id,
            'name': plan.name, 'crate_filename': plan.crate_path.name, 'paths': list(plan.paths)}
    (phases / (phase + '.json')).write_text(json.dumps(step, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': phase, 'native_content_matches': content['native_content_matches'],
                      'count': content['count'], 'snapshot': str(snapshot)}, ensure_ascii=False))


def write_order(folder, order):
    report = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
    adapter = SeratoCrateAdapter(folder / 'audio', report['serato_dir'], isolated=True,
                                 process_checker=lambda: False)
    tracks = ordered_tracks(report, order)
    # 若 Serato 仅改写了 header，主任务应先明确核对 adopt_native_rewrite；此处不自动接管。
    result = adapter.sync_set(report['collection'], tracks)
    (folder / 'last-update.json').write_text(json.dumps({'order': order, **result}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


def restore_order(folder):
    report = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
    update = json.loads((folder / 'last-update.json').read_text(encoding='utf-8'))
    adapter = SeratoCrateAdapter(folder / 'audio', report['serato_dir'], isolated=True,
                                 process_checker=lambda: False)
    adapter.restore_backup(Path(update['backup_path']).name)
    print('Isolated owned crate restored; reopen native Serato to verify restored order.')


def finalize_evidence(folder):
    report = {'version': 1, 'fingerprint': installation_fingerprint(), 'steps': []}
    for phase in VALIDATION_PHASES:
        report['steps'].append(json.loads((folder / 'phases' / (phase + '.json')).read_text(encoding='utf-8')))
    target = folder / 'native-validation-evidence.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    receipt = validate_native_evidence(target)
    print(json.dumps({'evidence': str(target), **receipt}, ensure_ascii=False))


def native_empty_test(folder):
    report = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
    profile = folder / 'isolation.sb'
    text = profile.read_text(encoding='utf-8')
    required = ['(deny network*)', '(deny file-write*)', '(deny user-preference-write)',
                '(deny user-preference-read', '(deny mach-lookup',
                f'(deny file-read* (subpath {sbpl_string(Path.home())}))']
    if not all(rule in text for rule in required):
        raise RuntimeError('隔离配置缺少真实目录、偏好或网络限制')
    launch = json.loads((folder / 'launch-argv.json').read_text(encoding='utf-8'))
    expected = ['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/env',
                'CFFIXED_USER_HOME=' + report['fake_home'], 'TMPDIR=' + str(folder / 'tmp') + '/',
                '/Applications/Serato DJ Pro.app/Contents/MacOS/Serato DJ Pro']
    if launch != expected:
        raise RuntimeError('启动参数与已审阅的隔离方案不一致')
    preference = Path.home() / 'Library/Preferences/com.serato.seratodj.plist'
    preference_hash = sha(preference) if preference.exists() else None
    adapter = SeratoCrateAdapter(folder / 'audio', report['serato_dir'], isolated=True,
                                 process_checker=lambda: False)
    empty = adapter.sync_set(report['collection'], [])
    plan = plan_set(report['collection'], [], report['serato_dir'])
    process = None
    content = None
    error = None
    last_error = None
    exit_code = None
    try:
        with (folder / 'native-empty.log').open('wb') as output:
            process = subprocess.Popen(launch, stdout=output, stderr=subprocess.STDOUT)
            print(json.dumps({'owned_native_pid': process.pid, 'empty_test_started': True}), flush=True)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('隔离 Serato 提前退出：' + str(process.returncode))
                try:
                    content = verify_native_set(plan, report['native_database'])
                    break
                except ValueError as failure:
                    last_error = str(failure)
                time.sleep(1)
            if content is None:
                raise RuntimeError('45 秒内没有得到原生空 Set 证据：' + str(last_error))
    except BaseException as failure:
        error = failure
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            exit_code = process.returncode
        # 只停止本次 Popen 的子进程；绝不按名称结束用户进程或其它窗口。
        try:
            if process is not None:
                snapshot = folder / 'phases/empty.sqlite'
                backup_sqlite(Path(report['native_database']), snapshot)
                if content is not None:
                    content = verify_native_set(plan, snapshot)
        finally:
            adapter.restore_backup(Path(empty['backup_path']).name)
    unchanged = check_unchanged(folder)
    current_preference_hash = sha(preference) if preference.exists() else None
    preferences_unchanged = current_preference_hash == preference_hash
    first = json.loads((folder / 'phases/real-preferences-after-first.json').read_text(encoding='utf-8'))
    result = {'empty_native_verified': content is not None,
              'owned_process_finished': process is None or process.poll() is not None,
              'owned_process_exit_code': exit_code,
              'original_metadata_unchanged': unchanged,
              'real_preferences_unchanged_during_test': preferences_unchanged,
              'real_preferences_unchanged_since_first_launch': current_preference_hash == first['sha256'],
              'isolated_owned_crate_restored': decode_crate_paths(Path(report['crate_path']).read_bytes())
                  == tuple(str(Path(track['path']).resolve()).lstrip('/') for track in report['tracks'])}
    (folder / 'native-empty-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if error is not None:
        raise error
    if not all(result[key] for key in ('empty_native_verified', 'owned_process_finished',
                                     'original_metadata_unchanged', 'real_preferences_unchanged_during_test',
                                     'isolated_owned_crate_restored')):
        raise RuntimeError('原生空 Set 或隔离边界核对未通过')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-unchanged', type=Path)
    parser.add_argument('--verify-native', type=Path, help='已由原生 App 重写的隔离实验目录')
    parser.add_argument('--record-phase', type=Path)
    parser.add_argument('--phase', choices=VALIDATION_PHASES)
    parser.add_argument('--order', default='3,1,2')
    parser.add_argument('--write-order', type=Path)
    parser.add_argument('--restore-order', type=Path)
    parser.add_argument('--finalize-evidence', type=Path)
    parser.add_argument('--native-empty', type=Path)
    parser.add_argument('--closed-observed', action='store_true', help='主任务已通过系统 inventory/所属进程退出确认 Serato 关闭')
    arguments = parser.parse_args()
    if arguments.native_empty:
        if not arguments.closed_observed:
            parser.error('必须先确认所有 Serato 实例已关闭，再运行 --native-empty')
        native_empty_test(arguments.native_empty.resolve())
        sys.exit(0)
    if arguments.record_phase or arguments.write_order or arguments.restore_order:
        if not arguments.closed_observed:
            parser.error('必须先确认原生 Serato 已关闭，再传入 --closed-observed')
        if arguments.record_phase:
            if not arguments.phase:
                parser.error('--record-phase 需要 --phase')
            record_phase(arguments.record_phase.resolve(), arguments.phase, arguments.order)
        elif arguments.write_order:
            write_order(arguments.write_order.resolve(), arguments.order)
        else:
            restore_order(arguments.restore_order.resolve())
        sys.exit(0)
    if arguments.finalize_evidence:
        finalize_evidence(arguments.finalize_evidence.resolve())
        sys.exit(0)
    if arguments.check_unchanged:
        sys.exit(0 if check_unchanged(arguments.check_unchanged.resolve()) else 1)
    if arguments.verify_native:
        folder = arguments.verify_native.resolve()
        evidence = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
        plan = plan_set(evidence['collection'], evidence['tracks'], evidence['serato_dir'])
        report = verify_native_set(plan, evidence['native_database'])
        (folder / 'native-content-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                          encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
        sys.exit(0)
    prepare()
