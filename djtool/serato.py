"""Serato Crate 适配边界：音频保持原路径，Set 只保存有序引用。

真实写入默认关闭；只有完整原生隔离证据和安装指纹匹配后才能明确启用。
绝不直接修改 master/root/location SQLite、database V2 或其他用户 Crate。
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import struct
import subprocess
import tempfile
import unicodedata
import uuid

VALIDATION_PHASES = ('create', 'reopen', 'update', 'reopen_update', 'restore')
NATIVE_SCHEMAS = {'4.0.10': 202, '5.0.0': 203}


class SeratoError(ValueError):
    pass


class SeratoConflict(SeratoError):
    pass


class SeratoNotValidated(SeratoError):
    pass


@dataclass(frozen=True)
class SeratoSetPlan:
    collection_id: str
    name: str
    crate_path: Path
    paths: tuple[str, ...]
    payload: bytes


def inspect_serato_storage(music_root, home=None, application=None):
    """只读展示音乐目录与 Serato 元数据目录；不把二者误认成同一目录。"""
    home = Path(home or Path.home()).expanduser().resolve()
    application = Path(application or '/Applications/Serato DJ Pro.app')
    version = None
    try:
        with (application / 'Contents/Info.plist').open('rb') as stream:
            version = plistlib.load(stream).get('CFBundleShortVersionString')
    except (OSError, plistlib.InvalidFileException):
        pass
    root = Path(music_root).expanduser().resolve()
    return {
        'music_root': str(root),
        'active_serato_dir': str(home / 'Music/_Serato_'),
        'application_library': str(home / 'Library/Application Support/Serato/Library'),
        'nested_serato_dir': str(root / '_Serato_') if (root / '_Serato_').is_dir() else None,
        'installed_version': version,
        'native_sync_validated': SeratoCrateAdapter(root, home / 'Music/_Serato_').native_sync_available(),
    }


def installation_fingerprint():
    """安装版本 + 可执行文件指纹，升级或替换安装后自动失效。"""
    application = Path('/Applications/Serato DJ Pro.app')
    try:
        with (application / 'Contents/Info.plist').open('rb') as stream:
            information = plistlib.load(stream)
        digest = hashlib.sha256()
        with (application / 'Contents/MacOS/Serato DJ Pro').open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return {'installed_version': information['CFBundleShortVersionString'],
                'bundle_id': information['CFBundleIdentifier'],
                'binary_sha256': digest.hexdigest()}
    except (OSError, KeyError, plistlib.InvalidFileException) as error:
        raise SeratoNotValidated('无法核对已验证的 Serato 安装') from error


def serato_is_running():
    """无法检查进程时停止写入；不启动 Serato，也不靠 WAL 存在猜测运行状态。"""
    try:
        result = subprocess.run(['/bin/ps', '-A', '-o', 'comm='], capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise SeratoError('无法确认 Serato 已关闭，请先完成隔离验证') from error
    # 崩溃报告和 WebEngine 辅助进程的父路径也含产品名，不能把它们当作持库主程序。
    return any(Path(line.strip()).name in ('Serato DJ Pro', 'Serato DJ Lite')
               for line in result.stdout.splitlines())


def _record(tag, value):
    if len(tag) != 4:
        raise SeratoError('Crate 字段标签必须为四字节')
    return tag.encode('ascii') + struct.pack('>I', len(value)) + value


def _records(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise SeratoError('Crate 文件尾部不完整')
        tag = data[offset:offset + 4]
        size = struct.unpack('>I', data[offset + 4:offset + 8])[0]
        end = offset + 8 + size
        if end > len(data):
            raise SeratoError('Crate 字段长度超出文件范围')
        yield tag, data[offset + 8:end]
        offset = end


def decode_crate_paths(data):
    """用于严格核对写回的成员和顺序，未知非曲目字段保持只读。"""
    paths = []
    seen_version = False
    try:
        for tag, body in _records(data):
            if not seen_version and tag != b'vrsn':
                raise SeratoError('Crate 缺少版本头')
            if tag == b'vrsn':
                if seen_version or body != '1.0/Serato ScratchLive Crate'.encode('utf-16-be'):
                    raise SeratoError('Crate 版本头无效或不在验证范围')
                seen_version = True
            if tag == b'otrk':
                records = list(_records(body))
                values = [value for key, value in records if key == b'ptrk']
                if len(values) != 1:
                    raise SeratoError('Crate 曲目路径字段不唯一')
                path = values[0].decode('utf-16-be')
                if not path or '\x00' in path:
                    raise SeratoError('Crate 包含无效路径')
                paths.append(path)
    except UnicodeDecodeError as error:
        raise SeratoError('Crate 路径不是有效 UTF-16BE') from error
    if not seen_version:
        raise SeratoError('Crate 缺少版本头')
    return tuple(paths)


def encode_crate(paths):
    """Serato 本机导出使用 UTF-16BE、4 字节标签和大端长度，otrk 顺序即 Set 顺序。"""
    output = _record('vrsn', '1.0/Serato ScratchLive Crate'.encode('utf-16-be'))
    for column, width in [('song', '250'), ('artist', '250'), ('album', '250'), ('length', '120')]:
        output += _record('ovct', _record('tvcn', column.encode('utf-16-be'))
                          + _record('tvcw', width.encode('utf-16-be')))
    # 不写 osrt 排序字段，避免歌名列排序掩盖用户编排的曲序。
    for path in paths:
        output += _record('otrk', _record('ptrk', path.encode('utf-16-be')))
    return output


def _crate_name(name):
    name = unicodedata.normalize('NFC', str(name)).strip()
    if not name or len(name) > 90 or len(name.encode('utf-8')) > 200 or '%%' in name or re.search(r'[/\\:\x00-\x1f\x7f]', name):
        raise SeratoError('Set 名称不能包含路径符号、%% 或控制字符，且不能过长')
    if name in ('.', '..') or name.endswith(('.', ' ')):
        raise SeratoError('Set 名称不能以点或空格结尾')
    return f'DJTOOL%%{name}.crate'


def plan_set(collection, tracks, serato_dir=None):
    if collection.get('kind', 'set') != 'set':
        raise SeratoError('仅 Set 可以同步为 Serato Crate')
    identifier = str(collection.get('id', ''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier):
        raise SeratoError('Set ID 无效')
    filename = _crate_name(collection['name'])
    references = []
    for track in tracks:
        path = Path(track['path']).expanduser().resolve()
        if not path.is_file():
            raise SeratoError(f'歌曲文件丢失：{track.get("title", path.name)}')
        if any(character in str(path) for character in ('\x00', '\r', '\n')):
            raise SeratoError('音频路径包含控制字符，无法安全同步')
        # 已检查本机内置盘的 Serato 4.0.10 导出：路径从卷根起，不带开头 /。
        # 外置盘使用相对卷根路径，尚未验证，拒绝悄悄写入错误引用。
        if str(path).startswith('/Volumes/'):
            raise SeratoNotValidated('外置盘 Serato 路径尚未完成隔离验证')
        reference = str(path).lstrip('/')
        if reference in references:
            raise SeratoError('Set 中存在重复的播放文件引用')
        references.append(reference)
    folder = Path(serato_dir or Path.home() / 'Music/_Serato_').expanduser().resolve()
    payload = encode_crate(references)
    if decode_crate_paths(payload) != tuple(references):
        raise SeratoError('Crate 写回核对失败')
    return SeratoSetPlan(identifier, collection['name'], folder / 'Subcrates' / filename,
                         tuple(references), payload)


def verify_native_set(plan, database):
    """只读检查已验证 Serato 202/203 数据库中的新列表和自定义曲序。

    此函数只能证明给定快照内容匹配；原生 App 的启动、重开及快照来源由实验记录证明。
    不将自己生成的 .crate 自检冒充 Serato 已接收，也不以此自动打开生产写入。
    """
    source = Path(database).expanduser().resolve()
    flags = '?mode=ro'
    if not Path(str(source) + '-wal').exists() and not Path(str(source) + '-shm').exists():
        flags += '&immutable=1'
    try:
        with closing(sqlite3.connect(source.as_uri() + flags, uri=True)) as db:
            db.execute('BEGIN')
            schema = db.execute('PRAGMA user_version').fetchone()[0]
            if schema not in NATIVE_SCHEMAS.values():
                raise SeratoNotValidated('原生 Serato 数据库版本不在本次验证范围')
            rows = db.execute('''SELECT c.id, c.name FROM container c
                JOIN dj_container_metadata m ON m.container_id=c.id
                WHERE m.filename=?''', (plan.crate_path.name,)).fetchall()
            if len(rows) != 1 or rows[0][1] != plan.name:
                raise SeratoError('原生 Serato 未生成唯一且名称匹配的 Set')
            paths = tuple(row[0] for row in db.execute('''
                SELECT a.portable_id FROM container_asset ca
                JOIN space_asset sa ON sa.id=ca.space_asset_id
                JOIN asset a ON a.id=sa.asset_id
                WHERE ca.container_id=? ORDER BY ca.list_order, ca.id''', (rows[0][0],)))
            if paths != plan.paths:
                raise SeratoError('原生 Serato Set 成员或自定义曲序不匹配')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise SeratoError('原生 Serato 数据库完整性检查失败')
            return {'schema_version': schema, 'collection_id': plan.collection_id,
                    'name': plan.name, 'count': len(paths), 'paths': list(paths),
                    'native_database': str(source), 'native_content_matches': True,
                    'native_reopen_verified': False}
    except sqlite3.Error as error:
        raise SeratoError('无法读取原生 Serato Set 快照') from error


def validate_native_evidence(report_path):
    """核对五个关闭后保留的原生快照；声明不能代替实际的 SQLite 成员证据。"""
    source = Path(report_path).expanduser().resolve()
    try:
        raw = source.read_bytes()
        report = json.loads(raw)
        fingerprint = installation_fingerprint()
        if (report['version'] != 1 or report['fingerprint'] != fingerprint
                or fingerprint['installed_version'] not in NATIVE_SCHEMAS):
            raise SeratoNotValidated('隔离证据不匹配当前 Serato 安装')
        steps = report['steps']
        if tuple(step['phase'] for step in steps) != VALIDATION_PHASES:
            raise SeratoNotValidated('需要创建、重开、更新、再重开与恢复的五份原生证据')
        references = []
        databases = []
        identities = []
        for step in steps:
            if step['process_closed'] is not True:
                raise SeratoNotValidated('原生快照需要在 Serato 关闭之后取得')
            database = Path(step['database']).expanduser().resolve()
            isolated_roots = (Path(__file__).resolve().parents[1] / 'work',
                              Path(tempfile.gettempdir()).resolve())
            if not any(database.is_relative_to(root) for root in isolated_roots):
                raise SeratoNotValidated('原生验收证据必须来自隔离目录')
            if hashlib.sha256(database.read_bytes()).hexdigest() != step['database_sha256']:
                raise SeratoNotValidated('原生验收快照已变化')
            paths = tuple(step['paths'])
            filename = _crate_name(step['name'])
            if step['crate_filename'] != filename or len(paths) < 2 or len(set(paths)) != len(paths):
                raise SeratoNotValidated('原生验收需要至少两首独立音频引用')
            plan = SeratoSetPlan(step['collection_id'], step['name'], Path(filename), paths, b'')
            content = verify_native_set(plan, database)
            if content['schema_version'] != NATIVE_SCHEMAS[fingerprint['installed_version']]:
                raise SeratoNotValidated('原生快照版本与安装版本不匹配')
            references.append(paths)
            databases.append(str(database))
            identities.append((step['collection_id'], step['name'], filename))
        if len(set(databases)) != len(VALIDATION_PHASES) or len(set(identities)) != 1:
            raise SeratoNotValidated('五阶段需要独立保留的快照与相同 Set')
        if not (references[0] == references[1] == references[4]
                and references[2] == references[3] and references[0] != references[2]):
            raise SeratoNotValidated('原生更新、重开或恢复的曲序证据不完整')
        return {'fingerprint': fingerprint, 'phases': list(VALIDATION_PHASES),
                'evidence_report_sha256': hashlib.sha256(raw).hexdigest()}
    except (KeyError, TypeError, json.JSONDecodeError, OSError) as error:
        raise SeratoNotValidated('原生验收记录无效或无法读取') from error


def _digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _atomic_write(path, data):
    """同目录临时文件 + fsync + 替换；失败时不留下半个 Crate。"""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.djtool-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_no_symlinks(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise SeratoError('Serato 写入目录不能经过符号链接')


class SeratoCrateAdapter:
    """仅管理 manifest 指定的 DJTOOL Crate，其他文件绝不写入。

    isolated=True 仅供 workspace/work 或系统临时目录实验，真实库写入保持关闭。
    原生验收完成后需要显式 activate_native_sync；不能用 isolated 开关放开真实目录。
    """
    def __init__(self, music_root, serato_dir=None, *, isolated=False, process_checker=None):
        original_root = Path(music_root).expanduser()
        original_serato = Path(serato_dir or Path.home() / 'Music/_Serato_').expanduser()
        _check_no_symlinks(original_root)
        _check_no_symlinks(original_serato)
        self.music_root = original_root.resolve()
        self.serato_dir = original_serato.resolve()
        self.state_dir = self.music_root / '.djtool/serato-sync'
        self.crate_dir = self.serato_dir / 'Subcrates'
        self.manifest_path = self.state_dir / 'manifest.json'
        self.activation_path = self.state_dir / 'native-validation.json'
        self.isolated = isolated
        self.process_checker = process_checker or serato_is_running

    def preview_set(self, collection, tracks):
        return plan_set(collection, tracks, self.serato_dir)

    def _write_guard(self):
        if self.isolated:
            allowed = (Path(__file__).resolve().parents[1] / 'work', Path(tempfile.gettempdir()).resolve())
            for target in (self.music_root, self.serato_dir):
                if not any(target != root and target.is_relative_to(root) for root in allowed):
                    raise SeratoError('隔离验收只能写入项目 work 或系统临时目录')
        else:
            self._validated_activation()
        for target in (self.music_root, self.serato_dir):
            _check_no_symlinks(target)
        if self.process_checker():
            raise SeratoError('请关闭 Serato 后再同步')
        _check_no_symlinks(self.state_dir)
        _check_no_symlinks(self.crate_dir)

    def _validated_activation(self):
        try:
            _check_no_symlinks(self.activation_path)
            record = json.loads(self.activation_path.read_text(encoding='utf-8'))
            if (record['version'] != 1 or record['fingerprint'] != installation_fingerprint()
                    or record['fingerprint']['installed_version'] not in NATIVE_SCHEMAS
                    or record['music_root'] != str(self.music_root)
                    or record['serato_dir'] != str(self.serato_dir)
                    or tuple(record['phases']) != VALIDATION_PHASES
                    or not re.fullmatch('[0-9a-f]{64}', record['evidence_report_sha256'])):
                raise SeratoNotValidated('原生验收记录不匹配当前安装或曲库目录')
            return record
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise SeratoNotValidated('Serato 原生隔离验收尚未完成，真实曲库同步未启用') from error

    def activate_native_sync(self, report_path):
        """原生验收后只在 DJTOOL 保存能力记录；实际 Crate 写入仍要求 Serato 已关闭。"""
        evidence = validate_native_evidence(report_path)
        for path in (self.music_root, self.serato_dir, self.state_dir, self.activation_path):
            _check_no_symlinks(path)
        if not self.music_root.is_dir() or not self.serato_dir.is_dir():
            raise SeratoError('曲库和 Serato 目录必须已存在')
        record = {'version': 1, 'music_root': str(self.music_root),
                  'serato_dir': str(self.serato_dir), **evidence}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.activation_path, json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))
        return record

    def native_sync_available(self):
        if self.isolated:
            return False
        try:
            self._validated_activation()
            return True
        except SeratoNotValidated:
            return False

    def native_sync_status(self):
        version = None
        try:
            with Path('/Applications/Serato DJ Pro.app/Contents/Info.plist').open('rb') as stream:
                version = plistlib.load(stream).get('CFBundleShortVersionString')
        except (OSError, plistlib.InvalidFileException):
            pass
        if self.isolated:
            return {'enabled': False, 'reason': '这是隔离实验目录，不能代表真实曲库同步', 'version': version}
        try:
            record = self._validated_activation()
            return {'enabled': True, 'reason': '已通过当前安装版本的隔离验收',
                    'version': record['fingerprint']['installed_version']}
        except SeratoNotValidated as error:
            return {'enabled': False, 'reason': str(error), 'version': version}

    def adopt_native_rewrite(self, collection, tracks, native_database):
        """显式接受原生重写的托管 Crate；必须同时核对文件与原生库的成员曲序。"""
        plan = self.preview_set(collection, tracks)
        with self._exclusive():
            manifest = self._read_manifest()
            entry = manifest['crates'].get(plan.collection_id)
            if entry is None or entry['filename'] != plan.crate_path.name:
                raise SeratoConflict('只能核对已托管且名称未变的 Set')
            data = self._file_bytes(entry['filename'])
            if data is None or decode_crate_paths(data) != plan.paths:
                raise SeratoConflict('Serato 修改了 Set 成员或曲序，不能接收为格式重写')
            verify_native_set(plan, native_database)
            if data != self._file_bytes(entry['filename']):
                raise SeratoConflict('原生核对过程中 Crate 又发生变化')
            changed = _digest(data) != entry['sha256']
            if changed:
                entry['sha256'] = _digest(data)
                _atomic_write(self.manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
            # 旧的按字节备份不再可直接覆盖新格式；后续同步会备份新的原生文件。
            return {'adopted': changed, 'count': len(plan.paths), 'crate_path': str(plan.crate_path)}

    @contextmanager
    def _exclusive(self):
        self._write_guard()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = self.state_dir / 'sync.lock'
        try:
            lock.mkdir()
        except FileExistsError as error:
            raise SeratoConflict('发现同步锁或未结束操作，请先检查隔离备份') from error
        try:
            yield
        finally:
            lock.rmdir()

    def _read_manifest(self):
        if self.manifest_path.is_symlink():
            raise SeratoConflict('同步记录是符号链接，已停止写入')
        if not self.manifest_path.exists():
            return {'version': 1, 'crates': {}}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            if manifest['version'] != 1 or not isinstance(manifest['crates'], dict):
                raise ValueError()
            for value in manifest['crates'].values():
                if value['filename'] != _crate_name(value['name']):
                    raise ValueError()
                if not re.fullmatch('[0-9a-f]{64}', value['sha256']):
                    raise ValueError()
            return manifest
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise SeratoConflict('同步记录损坏，已停止写入') from error

    def _file_bytes(self, filename):
        path = self.crate_dir / filename
        if path.is_symlink():
            raise SeratoConflict('目标 Crate 是符号链接，已停止写入')
        return path.read_bytes() if path.exists() else None

    def sync_set(self, collection, tracks):
        plan = self.preview_set(collection, tracks)
        # 原生实测：4.0.10 会忽略空 Crate 更新并保留旧成员，因此不能把它报成已清空。
        # 隔离模式保留零成员序列化能力供复现；生产同步只能提交非空 Set。
        if not self.isolated and not plan.paths:
            raise SeratoError('请先加入歌曲再同步；这个接口尚未验证清空 Set')
        with self._exclusive():
            manifest = self._read_manifest()
            old = manifest['crates'].get(plan.collection_id)
            filename = plan.crate_path.name
            if old and _digest(self._file_bytes(old['filename'])) != old['sha256']:
                raise SeratoConflict('这个 Set 的 Serato Crate 已被外部修改，未覆盖')
            key = lambda text: unicodedata.normalize('NFC', text).casefold()
            # macOS 常见的不区分大小写文件系统也不能覆盖同名用户 Crate。
            collisions = [path.name for path in self.crate_dir.glob('*.crate')
                          if key(path.name) == key(filename)]
            if collisions and (not old or collisions != [old['filename']]):
                raise SeratoConflict('Serato 已有同名 Crate，未接管或覆盖')
            if any(identifier != plan.collection_id and key(value['filename']) == key(filename)
                   for identifier, value in manifest['crates'].items()):
                raise SeratoConflict('其他 DJTOOL Set 已使用这个 Crate 名称')
            if old and old['filename'] != filename and key(old['filename']) == key(filename):
                raise SeratoConflict('仅大小写或 Unicode 形式变化的重命名需要先改为其他名称')
            before = {filename: self._file_bytes(filename)}
            after = {filename: plan.payload}
            if old and old['filename'] != filename:
                before[old['filename']] = self._file_bytes(old['filename'])
                after[old['filename']] = None
            original_manifest = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
            manifest['crates'][plan.collection_id] = {'name': plan.name, 'filename': filename,
                                                      'sha256': _digest(plan.payload)}
            new_manifest = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
            backup = self._backup(before, after, original_manifest, new_manifest)
            self.crate_dir.mkdir(parents=True, exist_ok=True)
            # 这一步发生冲突时不能回滚，外部修改并不属于本次操作。
            self._write_guard()
            for name, data in before.items():
                if self._file_bytes(name) != data:
                    raise SeratoConflict('预览之后 Crate 已变化，未覆盖')
            touched = {}
            try:
                for name, data in after.items():
                    if data is None:
                        (self.crate_dir / name).unlink()
                    else:
                        _atomic_write(self.crate_dir / name, data)
                    touched[name] = data
                _atomic_write(self.manifest_path, new_manifest)
                (backup / 'complete').write_text('ok', encoding='utf-8')
            except BaseException:
                # 本次仍持有同步锁；回滚仅限我们碰过的文件。
                for name, data in touched.items():
                    if self._file_bytes(name) != data:
                        raise SeratoConflict('失败后 Crate 又被外部修改，已保留备份并停止自动恢复')
                self._restore_files({name: before[name] for name in touched}, original_manifest)
                (backup / 'rolled-back').write_text('ok', encoding='utf-8')
                raise
            return {'crate_path': str(plan.crate_path), 'count': len(plan.paths),
                    'backup_path': str(backup), 'native_validated': not self.isolated,
                    'pending_serato_read': True}

    def _backup(self, before, after, manifest_before, manifest_after):
        directory = self.state_dir / 'backups' / uuid.uuid4().hex
        _check_no_symlinks(directory)
        directory.mkdir(parents=True)
        metadata = {'files': {}, 'manifest_before': _digest(manifest_before),
                    'manifest_after': _digest(manifest_after), 'serato_dir': str(self.serato_dir)}
        for index, (name, data) in enumerate(before.items()):
            storage = f'{index}.before'
            if data is not None:
                (directory / storage).write_bytes(data)
            metadata['files'][name] = {'before': _digest(data), 'after': _digest(after[name]),
                                       'storage': storage}
        if manifest_before is not None:
            (directory / 'manifest.before').write_bytes(manifest_before)
        (directory / 'transaction.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                                                    encoding='utf-8')
        return directory

    def _restore_files(self, before, manifest_before):
        for name, data in before.items():
            if data is None:
                (self.crate_dir / name).unlink(missing_ok=True)
            else:
                _atomic_write(self.crate_dir / name, data)
        if manifest_before is None:
            self.manifest_path.unlink(missing_ok=True)
        else:
            _atomic_write(self.manifest_path, manifest_before)

    def restore_backup(self, identifier):
        """只恢复最近匹配的托管写入；Serato 或用户改动后拒绝回滚。"""
        if not re.fullmatch('[0-9a-f]{32}', str(identifier)):
            raise SeratoError('备份 ID 无效')
        with self._exclusive():
            directory = self.state_dir / 'backups' / identifier
            _check_no_symlinks(directory)
            if not (directory / 'complete').is_file() or (directory / 'restored').exists():
                raise SeratoConflict('备份未提交或已经恢复')
            metadata = json.loads((directory / 'transaction.json').read_text(encoding='utf-8'))
            if metadata['serato_dir'] != str(self.serato_dir):
                raise SeratoConflict('备份不属于这个 Serato 目录')
            current_manifest = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
            if _digest(current_manifest) != metadata['manifest_after']:
                raise SeratoConflict('同步记录已变化，只能恢复仍匹配的最近一次备份')
            before = {}
            for name, entry in metadata['files'].items():
                if '/' in name or '\\' in name or not name.startswith('DJTOOL%%') or not name.endswith('.crate'):
                    raise SeratoConflict('备份包含无效文件名')
                if _digest(self._file_bytes(name)) != entry['after']:
                    raise SeratoConflict('Crate 已被外部修改，未用旧备份覆盖')
                storage = entry['storage']
                if not re.fullmatch(r'\d+\.before', storage):
                    raise SeratoConflict('备份路径无效')
                data = (directory / storage).read_bytes() if entry['before'] is not None else None
                if _digest(data) != entry['before']:
                    raise SeratoConflict('备份内容损坏，未恢复')
                before[name] = data
            manifest_before = ((directory / 'manifest.before').read_bytes()
                               if metadata['manifest_before'] is not None else None)
            if _digest(manifest_before) != metadata['manifest_before']:
                raise SeratoConflict('同步记录备份损坏，未恢复')
            self._restore_files(before, manifest_before)
            (directory / 'restored').write_text('ok', encoding='utf-8')
            return True
