"""百首 Set 批量引用：顺序、去重、原备注、事务与文件边界。"""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from djtool.library import Library


def make_tracks(library, count=140):
    with library.connection() as db:
        for index in range(count):
            identifier = f'track-{index:04}'
            path = library.audio_dir / f'{identifier}.mp3'
            path.write_bytes(b'fixture only ' + identifier.encode())
            db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename,reviewed,file_size,codec) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, identifier, ('电子 ' if index < 120 else '其他 ') + str(index),
                        'Fixture', 180, str(path), str(path), path.name, index % 2, path.stat().st_size, 'MP3'))


class BulkSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temp.name) / 'library')
        make_tracks(self.library)
        self.target = self.library.create_collection('千禧年电子', 'set')
        self.other = self.library.create_collection('其他演出', 'set')
        self.group = self.library.create_collection('分类', 'group')
        for identifier in ('track-0004', 'track-0001'):
            for collection in (self.target, self.other, self.group):
                self.library.add_member(collection, identifier)
        self.library.set_note(self.target, 'track-0004', '这条备注必须保留')

    def tearDown(self):
        self.temp.cleanup()

    def ids(self, collection=None):
        return [t['id'] for t in self.library.tracks(collection or self.target)]

    def snapshot(self):
        with self.library.connection() as db:
            return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in ('tracks', 'collections', 'members')}

    def test_append_hundred_skip_existing_preserve_notes_other_sets_and_files(self):
        before = self.snapshot()
        files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.library.audio_dir.iterdir()}
        ids = [f'track-{i:04}' for i in range(120)]
        result = self.library.add_members(self.target, ids + ids[:4])
        self.assertEqual(result, {'added': 118, 'existing': 2, 'total': 120})
        expected = ['track-0004', 'track-0001'] + [i for i in ids if i not in ('track-0004', 'track-0001')]
        self.assertEqual(self.ids(), expected)
        self.assertEqual(self.ids(self.other), ['track-0004', 'track-0001'])
        self.assertEqual(self.ids(self.group), ['track-0004', 'track-0001'])
        self.assertEqual(self.library.tracks(self.target)[0]['set_note'], '这条备注必须保留')
        after = self.snapshot()
        self.assertEqual(before['tracks'], after['tracks'])
        self.assertEqual(before['collections'], after['collections'])
        self.assertEqual(before['members'], after['members'][:len(before['members'])])
        self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
        self.assertEqual(self.library.add_members(self.target, ids), {'added': 0, 'existing': 120, 'total': 120})
        reopened = Library(self.library.root)
        self.assertEqual([t['id'] for t in reopened.tracks(self.target)], expected)

    def test_insert_preserves_existing_relative_order_and_skips_duplicates(self):
        self.library.add_members(self.target, ['track-0002', 'track-0004', 'track-0003'], index=1)
        self.assertEqual(self.ids(), ['track-0004', 'track-0002', 'track-0003', 'track-0001'])
        self.assertEqual(self.library.tracks(self.target)[0]['set_note'], '这条备注必须保留')

    def test_missing_track_or_collection_leaves_entire_batch_unchanged(self):
        before = self.snapshot()
        for collection, ids in ((self.target, ['track-0002', 'missing']), ('deleted', ['track-0002'])):
            with self.assertRaises(ValueError): self.library.add_members(collection, ids)
            self.assertEqual(self.snapshot(), before)

    def test_mid_batch_sql_failure_rolls_back_all_insertions(self):
        with self.library.connection() as db:
            db.execute("CREATE TRIGGER fail_mid_batch BEFORE INSERT ON members WHEN NEW.track_id='track-0003' "
                       "BEGIN SELECT RAISE(ABORT,'fixture disk failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.add_members(self.target, ['track-0002', 'track-0003', 'track-0005'])
        self.assertEqual(before, self.snapshot())

    def test_large_batch_beyond_sql_parameter_chunk_and_empty_input(self):
        # 复制已验证字段，不造音频解码；此测试只检查数据库参数分批。
        with self.library.connection() as db:
            for i in range(140, 1100):
                db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename) '
                           'VALUES(?,?,?,?,?,?,?,?)', (f'track-{i:04}', str(i), str(i), '', 180,
                            str(self.library.audio_dir / f'{i}.mp3'), '', f'{i}.mp3'))
        result = self.library.add_members(self.target, [f'track-{i:04}' for i in range(1100)])
        self.assertEqual((result['added'], len(self.ids())), (1098, 1100))
        self.assertEqual(self.library.add_members(self.target, []), {'added': 0, 'existing': 0, 'total': 0})


if __name__ == '__main__': unittest.main()
