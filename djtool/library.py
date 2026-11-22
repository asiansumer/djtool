"""曲库的数据与文件边界。分类、排序只改数据库，不移动播放文件。"""

from contextlib import closing, contextmanager
from collections.abc import Mapping
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
from djtool.tracks import positive_bpm, read_audio, read_mp3
from djtool.metadata import EXTRA_FIELDS
from djtool.storage import (configure_music_root as save_music_root, load_music_root,
                     checked_path, reference_path, storage_path, validate_music_root)

EXTRA_COLUMNS = {**{field: "TEXT NOT NULL DEFAULT ''" for field in EXTRA_FIELDS},
                 'bitrate': 'INTEGER', 'sample_rate': 'INTEGER', 'channels': 'INTEGER',
                 'file_size': 'INTEGER', 'codec': "TEXT DEFAULT 'MP3'", 'tags_json': "TEXT DEFAULT '{}'"}
LEGACY_ANALYSIS_COLUMNS = {'bpm_status': "TEXT NOT NULL DEFAULT 'pending'",
                    'bpm_message': "TEXT NOT NULL DEFAULT ''",
                    'bpm_method': "TEXT NOT NULL DEFAULT ''", 'bpm_analyzed_at': 'TEXT'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
 id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, artist TEXT NOT NULL, album TEXT NOT NULL DEFAULT '',
 genre TEXT NOT NULL DEFAULT '', musical_key TEXT NOT NULL DEFAULT '',
 duration REAL NOT NULL, path TEXT NOT NULL UNIQUE, source_path TEXT NOT NULL,
 filename TEXT NOT NULL, cover_path TEXT, bpm_tag REAL, bpm_estimate REAL, bpm_manual REAL,
 energy INTEGER, phase TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 reviewed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
 kind TEXT NOT NULL CHECK(kind IN ('group','set')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(kind, name)
);
CREATE TABLE IF NOT EXISTS members (
 collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(collection_id, track_id)
);
CREATE INDEX IF NOT EXISTS members_order ON members(collection_id, position);
CREATE TABLE IF NOT EXISTS serato_bpm_cache (
 track_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
 bpm REAL NOT NULL CHECK(bpm >= 20 AND bpm <= 400),
 source_path TEXT NOT NULL, synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Library:
    def __init__(self, root):
        self.root = checked_path(root).resolve()
        self.audio_dir = load_music_root(self.root)
        self.music_root = self.audio_dir if storage_path(self.root).exists() else None
        self.art_dir = self.root / "Artwork"
        self.database = self.root / ".djtool" / "library.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.music_root is None:
            self.audio_dir.mkdir(exist_ok=True)
        self.art_dir.mkdir(exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError("这个曲库由较新版本创建，请勿用旧版本打开")
            if version in (1, 2, 3):
                backup_dir = self.database.parent / 'backups'
                backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / ('before-v4-' + uuid.uuid4().hex[:12] + '.sqlite3')
                with closing(sqlite3.connect(backup_path)) as backup:
                    db.backup(backup)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute('PRAGMA table_info(tracks)')}
            for name, definition in {**EXTRA_COLUMNS, **LEGACY_ANALYSIS_COLUMNS}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE tracks ADD COLUMN {name} {definition}')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS online_jobs (
                 id TEXT PRIMARY KEY, provider TEXT NOT NULL, remote_id TEXT NOT NULL,
                 url TEXT NOT NULL, title TEXT NOT NULL, artist TEXT NOT NULL,
                 state TEXT NOT NULL DEFAULT 'ready', message TEXT NOT NULL DEFAULT '',
                 metadata_json TEXT NOT NULL, track_id TEXT REFERENCES tracks(id),
                 updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(provider, remote_id));
                CREATE TABLE IF NOT EXISTS local_sources (
                 digest TEXT PRIMARY KEY, track_id TEXT NOT NULL REFERENCES tracks(id),
                 path TEXT NOT NULL, metadata_json TEXT NOT NULL,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS platform_tracks (
                 provider TEXT NOT NULL, remote_id TEXT NOT NULL,
                 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                 PRIMARY KEY(provider,remote_id,track_id));
            ''')
            # 收歌页的“歌单”：一条链接 + 上次读到的歌曲顺序 + 只收前几首的范围。
            first_time = not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='link_sources'").fetchone()
            db.execute('''CREATE TABLE IF NOT EXISTS link_sources (
                 id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, name TEXT NOT NULL,
                 mode TEXT NOT NULL DEFAULT 'all', limit_count INTEGER NOT NULL DEFAULT 0,
                 items_json TEXT NOT NULL DEFAULT '[]', checked_at TEXT,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            if first_time:
                self._seed_link_sources(db)
            if 'musical_key' not in {row[1] for row in db.execute('PRAGMA table_info(serato_bpm_cache)')}:
                # Serato 分析的调性与 BPM 同源、同一次读取；原文件标签里的调性不覆盖。
                db.execute("ALTER TABLE serato_bpm_cache ADD COLUMN musical_key TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA user_version=4")
            self._reconcile_local_jobs(db)

    @contextmanager
    def connection(self):
        # 每次操作用自己的连接，后台导入不会与主界面共用连接。
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def tracks(self, collection_id=None):
        with self.connection() as db:
            # Serato 的节拍信息独立缓存；原标签、历史分析与用户整理字段不覆盖。
            fields = ('t.*, s.bpm AS bpm_serato, s.source_path AS bpm_serato_source, '
                      's.synced_at AS bpm_serato_synced_at, s.musical_key AS key_serato')
            if collection_id:
                rows = db.execute(
                    f"SELECT {fields}, m.note AS set_note FROM tracks t "
                    "JOIN members m ON t.id=m.track_id "
                    "LEFT JOIN serato_bpm_cache s ON s.track_id=t.id "
                    "WHERE m.collection_id=? ORDER BY m.position, t.id", (collection_id,)
                ).fetchall()
            else:
                rows = db.execute(f"SELECT {fields} FROM tracks t "
                                  "LEFT JOIN serato_bpm_cache s ON s.track_id=t.id "
                                  "ORDER BY t.created_at DESC, t.rowid DESC").fetchall()
            return [dict(row) for row in rows]

    def track(self, track_id):
        with self.connection() as db:
            row = db.execute("SELECT t.*, s.bpm AS bpm_serato, "
                             "s.source_path AS bpm_serato_source, s.musical_key AS key_serato, "
                             "s.synced_at AS bpm_serato_synced_at FROM tracks t "
                             "LEFT JOIN serato_bpm_cache s ON s.track_id=t.id "
                             "WHERE t.id=?", (track_id,)).fetchone()
            if row is None:
                raise ValueError("歌曲记录已不存在")
            return dict(row)

    def replace_serato_bpms(self, values, source_path, cancel=lambda: False, keys=None):
        """完整读取成功后替换 Serato 缓存，只按精确播放路径匹配，不读写音频。"""
        from djtool.audio_tasks import check_cancel
        check_cancel(cancel)
        if not isinstance(values, Mapping):
            raise ValueError('Serato BPM 必须是完整的路径映射')
        if isinstance(source_path, Path):
            source_path = str(source_path)
        if (not isinstance(source_path, str) or not source_path or '\0' in source_path
                or not os.path.isabs(source_path)):
            raise ValueError('Serato 数据库路径必须是绝对路径')
        # 先验证整个结果再开事务；空结果仅代表一次成功的完整读取，不代表读取失败。
        validated = {}
        for path, bpm in values.items():
            check_cancel(cancel)
            if (not isinstance(path, str) or not path or '\0' in path
                    or not os.path.isabs(path)):
                raise ValueError('Serato 歌曲路径必须是绝对路径字符串')
            if not isinstance(bpm, Real) or isinstance(bpm, bool):
                raise ValueError('Serato BPM 应为 20–400 的有限数值')
            try:
                number = float(bpm)
            except (ValueError, OverflowError) as error:
                raise ValueError('Serato BPM 应为 20–400 的有限数值') from error
            if not math.isfinite(number) or not 20 <= number <= 400:
                raise ValueError('Serato BPM 应为 20–400 的有限数值')
            validated[path] = number
        # 调性只是随 BPM 一起显示的文本；只接受有 BPM 的同一路径上的短字符串。
        key_values = {path: value.strip() for path, value in (keys or {}).items()
                      if path in validated and isinstance(value, str) and 0 < len(value.strip()) <= 12}
        check_cancel(cancel)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            tracks = {row['path']: row['id'] for row in db.execute('SELECT id,path FROM tracks')}
            previous = {row['track_id']: dict(row)
                        for row in db.execute('SELECT * FROM serato_bpm_cache')}
            matches = {tracks[path]: bpm for path, bpm in validated.items() if path in tracks}
            key_matches = {tracks[path]: value for path, value in key_values.items() if path in tracks}
            stale = set(previous) - set(matches)
            changed = sum(identifier not in previous
                          or previous[identifier]['bpm'] != bpm
                          or previous[identifier]['source_path'] != source_path
                          for identifier, bpm in matches.items())
            # 同一次同步使用相同时间；数值未变也记录这次成功核对的时间。
            synced_at = db.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0]
            for identifier, bpm in matches.items():
                check_cancel(cancel)
                db.execute('INSERT INTO serato_bpm_cache(track_id,bpm,source_path,synced_at,musical_key) '
                           'VALUES(?,?,?,?,?) ON CONFLICT(track_id) DO UPDATE SET '
                           'bpm=excluded.bpm,source_path=excluded.source_path,'
                           'synced_at=excluded.synced_at,musical_key=excluded.musical_key',
                           (identifier, bpm, source_path, synced_at, key_matches.get(identifier, '')))
            for identifier in stale:
                check_cancel(cancel)
                db.execute('DELETE FROM serato_bpm_cache WHERE track_id=?', (identifier,))
            # 点击停止时，所有缓存更新及旧值清理一并回滚，保留上次完整结果。
            check_cancel(cancel)
        return {'matched': len(matches), 'changed': changed, 'cleared': len(stale),
                'missing': len(validated) - len(matches)}

    @property
    def music_root_configured(self):
        return self.music_root is not None

    def configure_music_root(self, path):
        """曲库数据库和所有旧播放路径保留，只改变下一次复制或下载的保存目录。"""
        destination = save_music_root(self.root, path)
        self.audio_dir = destination
        self.music_root = destination
        return destination

    @staticmethod
    def _file_signature(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    @staticmethod
    def _insert_track(db, values):
        db.execute(
            "INSERT INTO tracks (id,digest,title,artist,album,genre,musical_key,duration,path,"
            "source_path,filename,cover_path,bpm_tag) VALUES "
            "(:id,:digest,:title,:artist,:album,:genre,:musical_key,:duration,:path,"
            ":source_path,:filename,:cover_path,:bpm_tag)", values)
        extra = {key: values[key] for key in EXTRA_COLUMNS}
        db.execute('UPDATE tracks SET ' + ','.join(f'{key}=?' for key in extra) + ' WHERE id=?',
                   (*extra.values(), values['id']))

    def reference_existing(self, source, allowed_root, cancel=lambda: False):
        """只读索引所选目录内的既有音频；Set 只引用歌曲记录，不创建音频文件夹。"""
        from djtool.audio_tasks import check_cancel
        check_cancel(cancel)
        source = reference_path(source, allowed_root)
        before = self._file_signature(source.stat())
        digest = hashlib.sha256()
        with source.open('rb') as stream:
            if self._file_signature(os.fstat(stream.fileno())) != before:
                raise ValueError('源文件在读取前发生变化，请稍后重试')
            while chunk := stream.read(1024 * 1024):
                check_cancel(cancel)
                digest.update(chunk)
        check_cancel(cancel)
        metadata, cover = read_audio(source)
        if self._file_signature(source.stat()) != before:
            raise ValueError('源文件在读取时发生变化，请等下载或编辑完成后重试')
        fingerprint = digest.hexdigest()
        identifier = uuid.uuid4().hex
        created = []
        try:
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                check_cancel(cancel)
                reference_path(source, allowed_root)
                if self._file_signature(source.stat()) != before:
                    raise ValueError('源文件在索引时发生变化，请稍后重试')
                existing = db.execute('SELECT id,path FROM tracks WHERE digest=?', (fingerprint,)).fetchone()
                if existing:
                    if not Path(existing['path']).is_file():
                        raise ValueError('这首歌已在曲库中，但播放文件丢失；请先恢复原曲库文件')
                    return existing['id'], False
                cover_path = None
                if cover:
                    artwork = self.art_dir / (identifier + cover[1])
                    with artwork.open('xb') as stream:
                        created.append(artwork)
                        stream.write(cover[0])
                    cover_path = str(artwork)
                self._insert_track(db, {**metadata, 'id': identifier, 'digest': fingerprint,
                                      'path': str(source), 'source_path': str(source),
                                      'filename': source.name, 'cover_path': cover_path})
                check_cancel(cancel)
                if self._file_signature(source.stat()) != before:
                    raise ValueError('源文件在索引时发生变化，请稍后重试')
            return identifier, True
        except Exception:
            # 原文件始终只读；失败仅清理本次索引创建的封面缓存。
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise

    def import_file(self, source, local_source=None):
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError("源文件不存在或不是文件")
        if source.suffix.lower() != ".mp3":
            raise ValueError("曲库存储层只接收 MP3；NCM 应通过导入入口转换")
        validate_music_root(self.audio_dir)
        if not self.audio_dir.is_dir():
            raise FileNotFoundError('音乐保存目录不存在或离线，请重新连接后重试')
        created = []
        temporary = None
        try:
            before = source.stat()
            digest = hashlib.sha256()
            with source.open("rb") as incoming, tempfile.NamedTemporaryFile(
                prefix=".import-", suffix=".mp3", dir=self.audio_dir, delete=False
            ) as outgoing:
                temporary = Path(outgoing.name)
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("源文件在导入时发生变化，请等下载完成后重试")
            fingerprint = digest.hexdigest()
            metadata, cover = read_mp3(temporary, source.name)
            identifier = uuid.uuid4().hex
            destination = self.audio_dir / f"{source.stem[:60]}--{identifier[:12]}.mp3"
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT id, path FROM tracks WHERE digest=?", (fingerprint,)).fetchone()
                if local_source:
                    existing = db.execute('SELECT t.id,t.path FROM tracks t JOIN local_sources s '
                                          'ON s.track_id=t.id WHERE s.digest=?',
                                          (local_source['digest'],)).fetchone() or existing
                if existing:
                    if not Path(existing["path"]).is_file():
                        raise ValueError("这首歌已在曲库中，但播放文件丢失；请先恢复原曲库文件")
                    self._record_local_source(db, local_source, existing['id'])
                    return existing["id"], False
                # 独占创建，避免覆盖任何已有文件；数据库成功后才算导入完成。
                with destination.open("xb") as saved, temporary.open("rb") as staged:
                    created.append(destination)
                    shutil.copyfileobj(staged, saved)
                    saved.flush()
                    os.fsync(saved.fileno())
                cover_path = None
                if cover:
                    artwork = self.art_dir / (identifier + cover[1])
                    with artwork.open("xb") as stream:
                        created.append(artwork)
                        stream.write(cover[0])
                    cover_path = str(artwork)
                values = {
                    **metadata, "id": identifier, "digest": fingerprint,
                    "path": str(destination), "source_path": local_source['path'] if local_source else str(source),
                    "filename": source.name, "cover_path": cover_path,
                }
                self._insert_track(db, values)
                self._record_local_source(db, local_source, identifier)
            return identifier, True
        except Exception:
            # 只清理由这次失败导入创建的文件，永远不碰源文件。
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def collections(self, kind=None):
        with self.connection() as db:
            query = "SELECT c.*, COUNT(m.track_id) AS count FROM collections c LEFT JOIN members m ON c.id=m.collection_id"
            args = ()
            if kind:
                query += " WHERE c.kind=?"
                args = (kind,)
            return [dict(row) for row in db.execute(query + " GROUP BY c.id ORDER BY c.created_at, c.rowid", args)]

    def create_collection(self, name, kind):
        name = name.strip()
        if not name or len(name) > 80 or kind not in ("set", "group"):
            raise ValueError("请输入 1–80 字的名称")
        identifier = uuid.uuid4().hex
        try:
            with self.connection() as db:
                db.execute("INSERT INTO collections(id,name,kind) VALUES(?,?,?)", (identifier, name, kind))
        except sqlite3.IntegrityError as error:
            raise ValueError("已经有同名的分组或 Set") from error
        return identifier

    def rename_collection(self, identifier, name):
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("请输入 1–80 字的名称")
        try:
            with self.connection() as db:
                db.execute("UPDATE collections SET name=? WHERE id=?", (name, identifier))
        except sqlite3.IntegrityError as error:
            raise ValueError("已经有同名的分组或 Set") from error

    def delete_collection(self, identifier):
        with self.connection() as db:
            db.execute("DELETE FROM collections WHERE id=?", (identifier,))

    def add_member(self, collection_id, track_id, index=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM members WHERE collection_id=? AND track_id=?", (collection_id, track_id)).fetchone():
                return False
            rows = db.execute("SELECT track_id FROM members WHERE collection_id=? ORDER BY position", (collection_id,)).fetchall()
            order = [row[0] for row in rows]
            order.insert(len(order) if index is None else max(0, min(index, len(order))), track_id)
            db.execute("INSERT INTO members(collection_id,track_id,position) VALUES(?,?,?)", (collection_id, track_id, 0))
            for position, member in enumerate(order):
                db.execute("UPDATE members SET position=? WHERE collection_id=? AND track_id=?", (position, collection_id, member))
            return True

    def remove_member(self, collection_id, track_id):
        with self.connection() as db:
            db.execute("DELETE FROM members WHERE collection_id=? AND track_id=?", (collection_id, track_id))

    def add_members(self, collection_id, track_ids, index=None):
        """一次事务加入整批引用；已有成员不移动、不覆盖备注，失败整批回滚。"""
        if isinstance(track_ids, (str, bytes)):
            raise ValueError('请提供歌曲列表')
        identifiers = list(track_ids)
        if any(not isinstance(item, str) or not item for item in identifiers):
            raise ValueError('歌曲 ID 无效')
        identifiers = list(dict.fromkeys(identifiers))
        if index is not None and type(index) is not int:
            raise ValueError('插入位置应为整数')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone():
                raise ValueError('这个 Set 或分组已经不存在，请重新选择')
            found = set()
            for offset in range(0, len(identifiers), 400):
                batch = identifiers[offset:offset + 400]
                found.update(row[0] for row in db.execute(
                    'SELECT id FROM tracks WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch))
            if found != set(identifiers):
                raise ValueError('部分歌曲已经不存在，未加入本批歌曲；请刷新后重试')
            rows = db.execute('SELECT track_id,position FROM members WHERE collection_id=? '
                              'ORDER BY position,track_id', (collection_id,)).fetchall()
            existing = {row[0] for row in rows}
            added = [identifier for identifier in identifiers if identifier not in existing]
            if added:
                start = max((row[1] for row in rows), default=-1) + 1
                db.executemany('INSERT INTO members(collection_id,track_id,position) VALUES(?,?,?)',
                               [(collection_id, identifier, start + pos) for pos, identifier in enumerate(added)])
                if index is not None:
                    # 拖入中间时仅更新位置；普通批量追加不改旧成员的任何字段。
                    order = [row[0] for row in rows]
                    position = max(0, min(index, len(order)))
                    order[position:position] = added
                    db.executemany('UPDATE members SET position=? WHERE collection_id=? AND track_id=?',
                                   [(pos, collection_id, identifier) for pos, identifier in enumerate(order)])
        return {'added': len(added), 'existing': len(identifiers) - len(added), 'total': len(identifiers)}

    def reorder(self, collection_id, track_ids):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = {row[0] for row in db.execute("SELECT track_id FROM members WHERE collection_id=?", (collection_id,))}
            if len(track_ids) != len(existing) or set(track_ids) != existing:
                raise ValueError("歌曲列表发生变化，请刷新后重新排序")
            for position, identifier in enumerate(track_ids):
                db.execute("UPDATE members SET position=? WHERE collection_id=? AND track_id=?", (position, collection_id, identifier))

    def update_track(self, identifier, values, group_ids=None):
        allowed = {"title", "artist", "genre", "phase", "energy", "note", "bpm_manual", "reviewed"}
        if not values or not set(values) <= allowed:
            raise ValueError("包含不支持的歌曲字段")
        if "title" in values and not values["title"].strip():
            raise ValueError("歌曲标题不能为空")
        if values.get("bpm_manual") is not None and positive_bpm(values["bpm_manual"]) is None:
            raise ValueError("BPM 应为 20–400，或留空")
        if values.get("energy") is not None and values["energy"] not in range(1, 6):
            raise ValueError("能量应为 1–5，或留空")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE tracks SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), identifier))
            if group_ids is not None:
                valid = {row[0] for row in db.execute("SELECT id FROM collections WHERE kind='group'")}
                if not set(group_ids) <= valid:
                    raise ValueError("分组已发生变化，请重新打开歌曲详情")
                db.execute("DELETE FROM members WHERE track_id=? AND collection_id IN (SELECT id FROM collections WHERE kind='group')", (identifier,))
                for group_id in group_ids:
                    position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM members WHERE collection_id=?", (group_id,)).fetchone()[0]
                    db.execute("INSERT INTO members(collection_id,track_id,position) VALUES(?,?,?)", (group_id, identifier, position))

    def memberships(self, track_id):
        with self.connection() as db:
            return {row[0] for row in db.execute("SELECT collection_id FROM members WHERE track_id=?", (track_id,))}

    def set_note(self, collection_id, track_id, note):
        with self.connection() as db:
            db.execute("UPDATE members SET note=? WHERE collection_id=? AND track_id=?", (note, collection_id, track_id))

    def refresh_metadata(self, identifier):
        """补读旧曲库的扩展标签；用户整理过的标题/BPM/备注不覆盖。"""
        track = self.track(identifier)
        path = Path(track['path'])
        before = self._file_signature(path.stat())
        metadata, _ = read_audio(path)
        if self._file_signature(path.stat()) != before:
            raise ValueError('音频在读取标签时发生变化，请稍后重试')
        # 原地引用可能是 WAV/FLAC/AIFF/M4A；只更新扩展标签和格式信息。
        # 基本标题、艺人、风格、BPM、分类与备注保留用户在 DJTOOL 中的整理。
        extra = {key: metadata[key] for key in EXTRA_COLUMNS}
        with self.connection() as db:
            db.execute('UPDATE tracks SET ' + ','.join(f'{key}=?' for key in extra) + ' WHERE id=?',
                       (*extra.values(), identifier))
        return self.track(identifier)

    def enqueue(self, info):
        return self.enqueue_many([info])[0]

    def enqueue_many(self, infos):
        """整份列表一次提交；跨歌单仍共享平台歌曲 ID，重复输入保留第一次的顺序。"""
        ordered = {}
        for info in infos:
            item = dict(info)
            item['remote_id'] = str(item['remote_id'])
            key = (item['provider'], item['remote_id'])
            ordered.setdefault(key, item)
        if not ordered:
            return []
        # 先编码完整输入，再开始事务。错误列表不会留下一部分新任务。
        inserts = [(uuid.uuid4().hex, item['provider'], item['remote_id'], item['url'],
                    item['title'], item['artist'], json.dumps(item, ensure_ascii=False))
                   for item in ordered.values()]
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.executemany('INSERT INTO online_jobs(id,provider,remote_id,url,title,artist,metadata_json) '
                           'VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,remote_id) DO NOTHING', inserts)
            # 不对每首歌开连接、扫描 NCM 来源或提交一次事务；千首列表也只核对一次。
            existing = {(row['provider'], row['remote_id']): dict(row)
                        for row in self._key_rows(db, 'online_jobs', ordered)}
            for key, item in ordered.items():
                self._refresh_online_job(db, existing[key], item)
            self._reconcile_local_jobs(db, ordered)
            refreshed = {(row['provider'], row['remote_id']): dict(row)
                         for row in self._key_rows(db, 'online_jobs', ordered)}
            return [refreshed[key] for key in ordered]

    @staticmethod
    def _key_rows(db, table, keys=None):
        if table not in ('online_jobs', 'platform_tracks'):
            raise ValueError('无效的歌曲来源表')
        if keys is None:
            return list(db.execute(f'SELECT * FROM {table}'))
        keys = list(keys)
        rows = []
        for offset in range(0, len(keys), 400):
            batch = keys[offset:offset + 400]
            placeholders = ','.join('(?,?)' for _ in batch)
            rows.extend(db.execute(f'SELECT * FROM {table} WHERE (provider,remote_id) IN (VALUES {placeholders})',
                                   [value for key in batch for value in key]))
        return rows

    @staticmethod
    def _refresh_online_job(db, row, info, explicit=True):
        """共享已有队列 ID；订阅刷新不会反复重试同一音源已拒绝的歌曲。"""
        row = dict(row)
        if row['state'] == 'complete' and row['track_id']:
            track = db.execute('SELECT path,duration FROM tracks WHERE id=?', (row['track_id'],)).fetchone()
            if track:
                db.execute('INSERT OR IGNORE INTO platform_tracks(provider,remote_id,track_id) VALUES(?,?,?)',
                           (row['provider'], row['remote_id'], row['track_id']))
            if not track or not Library._playable_record(track):
                row['state'] = 'missing_file'
                db.execute("UPDATE online_jobs SET state='missing_file',message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           ('原播放文件缺失 · 请恢复原文件后重新核对', row['id']))
        elif row['state'] == 'complete':
            row['state'] = 'ready'
            db.execute("UPDATE online_jobs SET state='ready',message='',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (row['id'],))
        if row['state'] in ('complete', 'ignored'):
            return
        previous = json.loads(row['metadata_json'])
        old_backend = previous.get('acquisition_backend') or ('official' if previous.get('openapi_scope') else '')
        backend = info.get('acquisition_backend') or ('official' if info.get('openapi_scope') else '')
        backend_changed = bool(backend and backend != old_backend)
        merged = dict(previous)
        merged.update({key:value for key,value in info.items() if value not in ('',None) or key=='availability_reason'})
        availability_checked = bool(info.get('openapi_scope') or backend in ('site', 'local', 'official'))
        if availability_checked:
            # 新音源的完整列表不继承旧音源的不可用原因。
            merged['availability_reason'] = info.get('availability_reason', '')
            if backend in ('site', 'local', 'official'):
                # 官方旧结构只带 openapi_scope；推断后也要保存，避免每轮误判切换。
                merged['acquisition_backend'] = backend
        db.execute('UPDATE online_jobs SET title=?,artist=?,metadata_json=? WHERE id=?',
                   (merged['title'],merged['artist'],json.dumps(merged,ensure_ascii=False),row['id']))
        if availability_checked and row['state'] != 'missing_file':
            reason = merged['availability_reason']
            if reason:
                db.execute("UPDATE online_jobs SET state='unavailable',message=? WHERE id=?",(reason,row['id']))
            elif (row['state'] == 'unavailable' and (explicit or backend_changed)) or (
                    row['state'] == 'waiting_auth' and backend_changed):
                db.execute("UPDATE online_jobs SET state='ready',message='' WHERE id=?",(row['id'],))

    def reset_netease_backend_jobs(self):
        """首次启用或切换音源时恢复旧权限阻塞；已完成、忽略及其他失败不变。"""
        changed = 0
        with self.connection() as db:
            for row in db.execute("SELECT id,metadata_json FROM online_jobs WHERE provider='netease' "
                                  "AND state IN ('waiting_auth','unavailable')").fetchall():
                metadata = json.loads(row['metadata_json'])
                metadata['availability_reason'] = ''
                db.execute("UPDATE online_jobs SET state='ready',message='',metadata_json=?, "
                           "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (json.dumps(metadata,ensure_ascii=False),row['id']))
                changed += 1
        return changed

    def reset_netease_pending(self):
        """用户更新登录凭据时，复用同样的权限阻塞恢复边界。"""
        return self.reset_netease_backend_jobs()

    def online_jobs(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM online_jobs ORDER BY rowid DESC')]

    def set_job_state(self, identifier, state, message='', track_id=None):
        if state not in ('ready', 'downloading', 'complete', 'failed', 'cancelled', 'ignored', 'waiting_auth', 'unavailable', 'missing_file'):
            raise ValueError('无效的下载状态')
        with self.connection() as db:
            db.execute('UPDATE online_jobs SET state=?, message=?, track_id=COALESCE(?,track_id), '
                       'updated_at=CURRENT_TIMESTAMP WHERE id=?', (state, message, track_id, identifier))
            if state == 'complete':
                # 只有已经完成入库的任务才成为平台 ID 依据；失败和普通文件标签不算依据。
                db.execute('INSERT OR IGNORE INTO platform_tracks(provider,remote_id,track_id) '
                           'SELECT provider,remote_id,track_id FROM online_jobs '
                           'WHERE id=? AND track_id IS NOT NULL', (identifier,))

    # ---- 收歌页的歌单来源 ------------------------------------------------

    def _seed_link_sources(self, db):
        """建表时只做一次：把以前订阅过、设过下载范围的歌单带过来，用户不用重新粘贴链接。"""
        found = {}
        try:
            for row in db.execute("SELECT id,address,name FROM intake_sources WHERE kind='playlist'"):
                items = [item['job_id'] for item in db.execute(
                    'SELECT job_id FROM source_items WHERE source_id=? AND job_id IS NOT NULL ORDER BY rowid',
                    (row['id'],))]
                found[row['address']] = {'name': row['name'], 'items': items, 'limit': 0}
        except sqlite3.Error:
            pass   # 新建的曲库没有旧版的订阅表
        # 旧版把“只收前几首”存在一个单独的 JSON 里；读不出来就当作没有，不影响建表。
        limits = {}
        try:
            ranges = self.root / '.djtool' / 'playlist-ranges.json'
            if ranges.is_file() and not ranges.is_symlink() and ranges.stat().st_size < 8 * 1024 * 1024:
                value = json.loads(ranges.read_text(encoding='utf-8')).get('limits', {})
                limits = {key: limit for key, limit in value.items()
                          if key.startswith('netease:playlist:') and key.rsplit(':', 1)[-1].isdigit()
                          and type(limit) is int and 0 <= limit <= 1_000_000}
        except (OSError, ValueError, AttributeError):
            limits = {}
        for key, limit in limits.items():
            identifier = key.rsplit(':', 1)[-1]
            entry = found.setdefault(f'https://music.163.com/playlist?id={identifier}',
                                     {'name': '', 'items': [], 'limit': 0})
            entry['limit'] = limit
        for url, entry in found.items():
            db.execute('INSERT OR IGNORE INTO link_sources(id,url,provider,name,limit_count,items_json) '
                       'VALUES(?,?,?,?,?,?)', (uuid.uuid4().hex, url, 'netease', entry['name'],
                                               entry['limit'], json.dumps(entry['items'])))

    @staticmethod
    def _link_source(row):
        value = dict(row)
        value['items'] = json.loads(value.pop('items_json'))
        value['limit'] = value.pop('limit_count')
        return value

    def link_sources(self):
        with self.connection() as db:
            return [self._link_source(row) for row in db.execute('SELECT * FROM link_sources ORDER BY rowid')]

    def link_source(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT * FROM link_sources WHERE id=?', (identifier,)).fetchone()
        return self._link_source(row) if row else None

    def save_link_source(self, url, provider, name, job_ids, mode='all'):
        """读完一个歌单后记下它；返回 (来源, 比上次多出来的歌曲数)。第一次读取时后者为 None。"""
        job_ids = list(dict.fromkeys(job_ids))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM link_sources WHERE url=?', (url,)).fetchone()
            if row:
                before = set(json.loads(row['items_json']))
                fresh = len([item for item in job_ids if item not in before]) if row['checked_at'] else None
                db.execute('UPDATE link_sources SET name=?, items_json=?, checked_at=CURRENT_TIMESTAMP WHERE id=?',
                           (name or row['name'], json.dumps(job_ids), row['id']))
                identifier = row['id']
            else:
                identifier, fresh = uuid.uuid4().hex, None
                db.execute('INSERT INTO link_sources(id,url,provider,name,mode,items_json,checked_at) '
                           'VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                           (identifier, url, provider, name, mode, json.dumps(job_ids)))
            saved = db.execute('SELECT * FROM link_sources WHERE id=?', (identifier,)).fetchone()
            return self._link_source(saved), fresh

    def update_link_source(self, identifier, *, limit=None, mode=None):
        if limit is not None and (type(limit) is not int or not 0 <= limit <= 1_000_000):
            raise ValueError('只收前几首必须是 0 到 1000000 的整数；0 表示全部')
        if mode is not None and mode not in ('all', 'pick'):
            raise ValueError('无效的收歌方式')
        with self.connection() as db:
            if limit is not None:
                db.execute('UPDATE link_sources SET limit_count=? WHERE id=?', (limit, identifier))
            if mode is not None:
                db.execute('UPDATE link_sources SET mode=? WHERE id=?', (mode, identifier))

    def remove_link_source(self, identifier):
        """只是不再列出这个歌单；已经收进来的歌和下载记录都不动。"""
        with self.connection() as db:
            db.execute('DELETE FROM link_sources WHERE id=?', (identifier,))

    def jobs_for(self, identifiers):
        """按给定顺序取下载任务；已经不存在的 ID 跳过。"""
        identifiers = list(identifiers)
        found = {}
        with self.connection() as db:
            for offset in range(0, len(identifiers), 500):
                chunk = identifiers[offset:offset + 500]
                marks = ','.join('?' * len(chunk))
                for row in db.execute(f'SELECT * FROM online_jobs WHERE id IN ({marks})', chunk):
                    found[row['id']] = dict(row)
        return [found[item] for item in identifiers if item in found]

    def known_source_paths(self):
        """已经收过的本地文件的原始路径，用来判断文件夹里哪些是新文件。"""
        with self.connection() as db:
            return {row[0] for row in db.execute(
                'SELECT path FROM local_sources UNION SELECT source_path FROM tracks WHERE source_path IS NOT NULL')}

    def source_for_track(self, track_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM online_jobs WHERE track_id=? LIMIT 1', (track_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _record_local_source(db, source, identifier):
        if source:
            db.execute('INSERT INTO local_sources(digest,track_id,path,metadata_json) '
                       'VALUES(?,?,?,?) ON CONFLICT(digest) DO NOTHING',
                       (source['digest'], identifier, source['path'],
                        json.dumps(source['details'], ensure_ascii=False)))
            Library._reconcile_local_jobs(db)

    @staticmethod
    def _playable_record(track, expected=None):
        """已通过导入验收的曲库记录，当前文件仍在；这里不逐首重新解码整份歌单。"""
        duration = track['duration']
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            return False
        if expected:
            if type(expected) not in (int, float) or not math.isfinite(expected) or expected <= 0:
                return False
            if abs(duration - expected) > max(2, expected * .02):
                return False
        try:
            path = Path(track['path'])
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _reconcile_online_jobs(db, keys=None):
        """只用成功下载关系或已验证 NCM ID 复用音频；名称、艺人和普通标签不参与。"""
        from djtool.ncm import music_id
        jobs = [dict(row) for row in Library._key_rows(db, 'online_jobs', keys)]
        summary = {'matched': 0, 'missing': 0}
        if not jobs:
            return summary
        relevant_keys = {(job['provider'], job['remote_id']) for job in jobs}
        bindings = Library._key_rows(db, 'platform_tracks', relevant_keys)
        linked_ids = {row['track_id'] for row in bindings} | {job['track_id'] for job in jobs if job['track_id']}
        tracks = {}
        linked_ids = list(linked_ids)
        for offset in range(0, len(linked_ids), 400):
            batch = linked_ids[offset:offset + 400]
            tracks.update((row['id'], dict(row)) for row in db.execute(
                'SELECT id,path,duration,tags_json FROM tracks WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch))
        # 在 SQL 中先缩到本次平台 ID；单首下载不会把千首队列和整库标签带回 Python 对账。
        safe_tags = "CASE WHEN json_valid(tags_json) THEN tags_json ELSE '{}' END"
        tag_provider = f"json_extract({safe_tags}, '$.\"TXXX:DJTOOL_SOURCE\"[0]')"
        tag_id = f"json_extract({safe_tags}, '$.\"TXXX:DJTOOL_SOURCE_ID\"[0]')"
        keys_list = list(relevant_keys)
        for offset in range(0, len(keys_list), 400):
            batch = keys_list[offset:offset + 400]
            placeholders = ','.join('(?,?)' for _ in batch)
            tracks.update((row['id'], dict(row)) for row in db.execute(
                f'SELECT id,path,duration,tags_json FROM tracks WHERE ({tag_provider},{tag_id}) IN (VALUES {placeholders})',
                [value for key in batch for value in key]))
        # 保留旧版本完成任务的来源关系；以后即使清理队列，也不会丢掉平台 ID 去重依据。
        db.executemany('INSERT OR IGNORE INTO platform_tracks(provider,remote_id,track_id) VALUES(?,?,?)',
                       [(job['provider'], job['remote_id'], job['track_id']) for job in jobs
                        if job['state'] == 'complete' and job['track_id'] in tracks])
        candidates = {}
        # 包括这轮为旧完成任务回填的绑定。
        for row in Library._key_rows(db, 'platform_tracks', relevant_keys):
            candidates.setdefault((row['provider'], row['remote_id']), []).append((row['track_id'], 'download'))
        for track in tracks.values():
            try:
                tags = json.loads(track['tags_json'])
            except (ValueError, TypeError):
                continue
            if not isinstance(tags, dict):
                continue
            # 只识别 DJTOOL 下载时写入的一对专用文本帧；普通 ID、ISRC 和文件名均不使用。
            provider = tags.get('TXXX:DJTOOL_SOURCE')
            identifier = tags.get('TXXX:DJTOOL_SOURCE_ID')
            if not isinstance(provider, list) or len(provider) != 1 or provider[0] not in ('netease', 'soundcloud'):
                continue
            if not isinstance(identifier, list) or len(identifier) != 1 or not isinstance(identifier[0], str):
                continue
            remote_id = music_id({'musicId': identifier[0]})
            if remote_id:
                candidates.setdefault((provider[0], remote_id), []).append((track['id'], 'tags'))
        for row in db.execute('SELECT s.metadata_json,t.id,t.path,t.duration,t.tags_json FROM local_sources s '
                              'JOIN tracks t ON t.id=s.track_id'):
            details = json.loads(row['metadata_json'])
            remote_id = music_id(details.get('ncm_metadata') or {})
            output_duration = details.get('output_duration')
            if not remote_id or type(output_duration) not in (int, float) or not math.isfinite(output_duration) or output_duration <= 0:
                continue
            if ('netease', remote_id) not in relevant_keys:
                continue
            if not Library._playable_record(row, output_duration):
                continue
            tracks[row['id']] = dict(row)
            candidates.setdefault(('netease', remote_id), []).append((row['id'], 'ncm'))
        for job in jobs:
            if job['state'] == 'ignored':
                continue
            expected = json.loads(job['metadata_json']).get('duration')
            track = tracks.get(job['track_id'])
            if job['state'] == 'complete':
                if track and Library._playable_record(track):
                    continue
                # 历史状态不等于文件仍在。单独提示恢复文件，不再次下载或改变 Set 引用。
                if job['track_id']:
                    db.execute("UPDATE online_jobs SET state='missing_file',message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               ('原播放文件缺失 · 请恢复原文件后重新核对', job['id']))
                    summary['missing'] += 1
                    job['state'] = 'missing_file'
                else:
                    db.execute("UPDATE online_jobs SET state='ready',message='',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (job['id'],))
            selected = next(((identifier, origin) for identifier, origin in
                             candidates.get((job['provider'], job['remote_id']), [])
                             if identifier in tracks and (origin != 'tags' or
                                 (type(expected) in (int, float) and math.isfinite(expected) and expected > 0))
                             and Library._playable_record(tracks[identifier], expected)), None)
            if not selected:
                # 队列历史可清理，已成功下载的 ID 关系仍有效；文件遗失不能变成新下载。
                missing = next((identifier for identifier, origin in
                                candidates.get((job['provider'], job['remote_id']), [])
                                if origin == 'download' and identifier in tracks and
                                not Library._playable_record(tracks[identifier])), None)
                if missing and job['state'] != 'missing_file':
                    db.execute("UPDATE online_jobs SET state='missing_file',message=?,track_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               ('原播放文件缺失 · 请恢复原文件后重新核对', missing, job['id']))
                    summary['missing'] += 1
                continue
            identifier, origin = selected
            message = ('官方下载 · NCM 已转换入库 · 歌曲 ID 已核对' if origin == 'ncm' else '已在曲库 · 平台歌曲 ID 已核对')
            db.execute("UPDATE online_jobs SET state='complete',message=?,track_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (message, identifier, job['id']))
            db.execute('INSERT OR IGNORE INTO platform_tracks(provider,remote_id,track_id) VALUES(?,?,?)',
                       (job['provider'], job['remote_id'], identifier))
            summary['matched'] += 1
        return summary

    @staticmethod
    def _reconcile_local_jobs(db, keys=None):
        return Library._reconcile_online_jobs(db, keys)['matched']

    def reconcile_online_jobs(self):
        with self.connection() as db:
            return self._reconcile_online_jobs(db)

    def reconcile_local_jobs(self):
        with self.connection() as db:
            return self._reconcile_local_jobs(db)

    def local_source_by_digest(self, digest):
        with self.connection() as db:
            row = db.execute('SELECT * FROM local_sources WHERE digest=?', (digest,)).fetchone()
            return dict(row) if row else None

    def local_sources_for_track(self, identifier):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM local_sources WHERE track_id=?', (identifier,))]
