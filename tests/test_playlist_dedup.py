"""整份歌单的曲库去重计划。仅使用隔离库和合成音频，不联网、不碰 Serato。"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from djtool.library import Library
from djtool.metadata import embed_online


def song(number, **extra):
    return {'provider': 'netease', 'remote_id': str(number),
            'url': f'https://music.163.com/song?id={number}',
            'title': 'Same title', 'artist': 'Fixture', 'duration': 3,
            'acquisition_backend': 'site', **extra}


class PlaylistDedupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory(prefix='djtool-dedup-sample-')
        cls.sample = Path(cls.samples.name) / 'original.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'sine=duration=3', '-c:a', 'libmp3lame', str(cls.sample)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='djtool-dedup-library-')
        self.root = Path(self.temporary.name)
        self.library = Library(self.root / 'library')

    def tearDown(self):
        self.temporary.cleanup()

    def imported(self, source=None):
        return self.library.import_file(source or self.sample)[0]

    def downloaded(self, number, track_id=None):
        track_id = track_id or self.imported()
        job = self.library.enqueue(song(number))
        self.library.set_job_state(job['id'], 'complete', '已入库', track_id)
        return track_id, job['id']

    def ncm_source(self, track_id, number, **details):
        with self.library.connection() as db:
            db.execute('INSERT INTO local_sources(digest,track_id,path,metadata_json) VALUES(?,?,?,?)',
                       (f'ncm-{number}', track_id, str(self.root / f'{number}.ncm'),
                        json.dumps({'ncm_metadata': {'musicId': str(number)},
                                    'output_duration': 3, **details})))

    def test_thousand_song_playlist_skips_five_hundred_known_ids_in_one_transaction(self):
        track_id = self.imported()
        self.library.update_track(track_id, {'reviewed': 1, 'note': 'already sorted'})
        group = self.library.create_collection('Existing group', 'group')
        set_id = self.library.create_collection('Existing Set', 'set')
        self.library.add_member(group, track_id)
        self.library.add_member(set_id, track_id)
        old = self.library.enqueue_many(song(i) for i in range(1, 501))
        # 合成夹具共享一份已验收音频，仅验证千首规划，不冒充千份真实音乐下载。
        with self.library.connection() as db:
            db.executemany("UPDATE online_jobs SET state='complete',track_id=? WHERE id=?",
                           [(track_id, job['id']) for job in old])
        before = Path(self.library.track(track_id)['path']).read_bytes()
        with patch.object(self.library, 'connection', wraps=self.library.connection) as connections, \
             patch.object(self.library, '_reconcile_local_jobs', wraps=self.library._reconcile_local_jobs) as reconcile:
            jobs = self.library.enqueue_many(song(i) for i in range(1, 1001))
        self.assertEqual(connections.call_count, 1)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual([job['remote_id'] for job in jobs], list(map(str, range(1, 1001))))
        self.assertEqual(sum(job['state'] == 'complete' for job in jobs), 500)
        self.assertEqual(sum(job['state'] == 'ready' for job in jobs), 500)
        self.assertEqual([job['id'] for job in jobs[:500]], [job['id'] for job in old])
        self.assertEqual(len(self.library.online_jobs()), 1000)
        self.assertEqual(self.library.track(track_id)['reviewed'], 1)
        self.assertEqual(self.library.track(track_id)['note'], 'already sorted')
        self.assertEqual(self.library.memberships(track_id), {group, set_id})
        self.assertEqual(Path(self.library.track(track_id)['path']).read_bytes(), before)

    def test_cross_playlist_reopen_and_different_title_reuse_same_job(self):
        track_id, job_id = self.downloaded(12)
        other = self.library.enqueue_many([song(99), song(12, title='renamed by platform')])
        self.assertEqual((other[1]['id'], other[1]['state'], other[1]['track_id']),
                         (job_id, 'complete', track_id))
        reopened = Library(self.library.root)
        self.assertEqual(reopened.enqueue_many([song(12)])[0]['id'], job_id)
        self.assertEqual(len(reopened.tracks()), 1)
        self.assertEqual(len(reopened.online_jobs()), 2)

    def test_completed_without_linked_audio_is_downloadable(self):
        job = self.library.enqueue(song(12))
        self.library.set_job_state(job['id'], 'complete')
        same = self.library.enqueue_many([song(12)])[0]
        self.assertEqual((same['id'], same['state'], same['track_id']), (job['id'], 'ready', None))

    def test_missing_file_preserves_original_set_and_is_not_a_new_download(self):
        track_id, job_id = self.downloaded(12)
        set_id = self.library.create_collection('Keep Set', 'set')
        self.library.add_member(set_id, track_id)
        track = self.library.track(track_id)
        path = Path(track['path'])
        content = path.read_bytes()
        path.unlink()
        same = self.library.enqueue_many([song(12)])[0]
        self.assertEqual((same['id'], same['state'], same['track_id']),
                         (job_id, 'missing_file', track_id))
        self.assertIn('恢复原文件', same['message'])
        self.assertEqual(self.library.track(track_id), track)
        self.assertEqual(self.library.memberships(track_id), {set_id})
        path.write_bytes(content)
        summary = self.library.reconcile_online_jobs()
        self.assertEqual(summary['matched'], 1)
        self.assertEqual(self.library.online_jobs()[0]['state'], 'complete')
        self.assertEqual(self.library.memberships(track_id), {set_id})

    def test_reconcile_marks_missing_and_zero_byte_files(self):
        track_id, _ = self.downloaded(12)
        Path(self.library.track(track_id)['path']).write_bytes(b'')
        self.assertEqual(self.library.reconcile_online_jobs(), {'matched': 0, 'missing': 1})
        self.assertEqual(self.library.online_jobs()[0]['state'], 'missing_file')
        self.assertEqual(Library(self.library.root).online_jobs()[0]['state'], 'missing_file')

    def test_ignored_job_is_preserved_even_when_audio_can_be_reused(self):
        track_id, job_id = self.downloaded(12)
        self.library.set_job_state(job_id, 'ignored', 'user chose')
        same = self.library.enqueue_many([song(12, title='different')])[0]
        self.assertEqual((same['state'], same['message'], same['track_id']),
                         ('ignored', 'user chose', track_id))

    def test_different_remix_ids_and_other_provider_do_not_match_title(self):
        _, job_id = self.downloaded(12)
        jobs = self.library.enqueue_many([song(12), song(13), song(12, provider='soundcloud')])
        self.assertEqual([job['state'] for job in jobs], ['complete', 'ready', 'ready'])
        self.assertEqual(len({job['id'] for job in jobs}), 3)
        self.assertEqual(jobs[0]['id'], job_id)

    def test_unlabelled_mp3_with_same_name_and_duration_is_not_skipped(self):
        track_id = self.imported()
        self.library.update_track(track_id, {'title': 'Same title', 'artist': 'Fixture'})
        self.assertEqual(self.library.enqueue_many([song(12)])[0]['state'], 'ready')

    def test_verified_ncm_import_before_playlist_skips_exact_id(self):
        track_id = self.imported()
        self.ncm_source(track_id, 12)
        jobs = self.library.enqueue_many([song(12), song(13)])
        self.assertEqual([job['state'] for job in jobs], ['complete', 'ready'])
        self.assertEqual(jobs[0]['track_id'], track_id)
        self.assertIn('NCM', jobs[0]['message'])

    def test_ncm_id_requires_completed_conversion_and_matching_duration(self):
        track_id = self.imported()
        self.ncm_source(track_id, 12, output_duration=None)
        self.ncm_source(track_id, 13)
        self.ncm_source(track_id, 14, output_duration=180)
        jobs = self.library.enqueue_many([song(12), song(13, duration=180), song(14)])
        self.assertTrue(all(job['state'] == 'ready' for job in jobs))

    def test_djtool_tags_recover_platform_id_after_reimport_without_queue(self):
        tagged = self.root / 'previous-download.mp3'
        shutil.copyfile(self.sample, tagged)
        embed_online(tagged, song(12))
        track_id = self.imported(tagged)
        self.library.update_track(track_id, {'reviewed': 1})
        jobs = self.library.enqueue_many([song(12), song(13)])
        self.assertEqual([job['state'] for job in jobs], ['complete', 'ready'])
        self.assertEqual(jobs[0]['track_id'], track_id)
        self.assertEqual(self.library.track(track_id)['reviewed'], 1)

    def test_djtool_tags_require_both_exact_frames_and_valid_matching_duration(self):
        track_id = self.imported()
        cases = [
            {'TXXX:SOURCE': ['netease'], 'TXXX:SOURCE_ID': ['12']},
            {'TXXX:DJTOOL_SOURCE': ['NetEase'], 'TXXX:DJTOOL_SOURCE_ID': ['12']},
            {'TXXX:DJTOOL_SOURCE': ['netease', 'soundcloud'], 'TXXX:DJTOOL_SOURCE_ID': ['12']},
            {'TXXX:DJTOOL_SOURCE': 'netease', 'TXXX:DJTOOL_SOURCE_ID': ['12']},
            {'TXXX:DJTOOL_SOURCE': ['netease'], 'TXXX:DJTOOL_SOURCE_ID': [12]},
            {'TXXX:DJTOOL_SOURCE': ['netease'], 'TXXX:DJTOOL_SOURCE_ID': ['１２']},
            {'TXXX:DJTOOL_SOURCE': ['netease'], 'TXXX:DJTOOL_SOURCE_ID': ['12.0']},
            {'TXXX:DJTOOL_SOURCE': ['netease'], 'TXXX:DJTOOL_SOURCE_ID': ['0']},
        ]
        for tags in cases:
            with self.subTest(tags=tags):
                with self.library.connection() as db:
                    db.execute('UPDATE tracks SET tags_json=? WHERE id=?', (json.dumps(tags), track_id))
                self.assertEqual(self.library.enqueue_many([song(12)])[0]['state'], 'ready')
        with self.library.connection() as db:
            db.execute('UPDATE tracks SET tags_json=? WHERE id=?',
                       (json.dumps({'TXXX:DJTOOL_SOURCE': ['netease'],
                                    'TXXX:DJTOOL_SOURCE_ID': ['12']}), track_id))
        for duration in (None, 0, -1, '3', float('nan'), 180):
            with self.subTest(duration=duration):
                with self.library.connection() as db:
                    db.execute('DELETE FROM online_jobs')
                    db.execute('DELETE FROM platform_tracks')
                self.assertEqual(self.library.enqueue_many([song(12, duration=duration)])[0]['state'], 'ready')

    def test_successful_download_identity_survives_deleting_queue_history(self):
        track_id, old_job_id = self.downloaded(12)
        with self.library.connection() as db:
            db.execute('DELETE FROM online_jobs WHERE id=?', (old_job_id,))
        job = Library(self.library.root).enqueue_many([song(12)])[0]
        self.assertNotEqual(job['id'], old_job_id)
        self.assertEqual((job['state'], job['track_id']), ('complete', track_id))

    def test_deleted_queue_history_and_missing_file_remain_a_restore_task(self):
        track_id, old_job_id = self.downloaded(12)
        Path(self.library.track(track_id)['path']).unlink()
        with self.library.connection() as db:
            db.execute('DELETE FROM online_jobs WHERE id=?', (old_job_id,))
        job = Library(self.library.root).enqueue_many([song(12)])[0]
        self.assertEqual((job['state'], job['track_id']), ('missing_file', track_id))
        self.assertEqual(len(self.library.tracks()), 1)

    def test_available_candidate_wins_before_missing_previous_download(self):
        missing_track, old_job = self.downloaded(12)
        other = self.root / 'other-copy.mp3'
        shutil.copyfile(self.sample, other)
        embed_online(other, song(12))
        available = self.imported(other)
        with self.library.connection() as db:
            db.execute('INSERT INTO platform_tracks(provider,remote_id,track_id) VALUES(?,?,?)',
                       ('netease', '12', available))
            db.execute('DELETE FROM online_jobs WHERE id=?', (old_job,))
        Path(self.library.track(missing_track)['path']).unlink()
        job = self.library.enqueue_many([song(12)])[0]
        self.assertEqual((job['state'], job['track_id']), ('complete', available))
        self.assertEqual(len(self.library.tracks()), 2)


    def test_duplicate_inputs_keep_first_order_and_metadata(self):
        jobs = self.library.enqueue_many([song(12, title='first'), song(13), song(12, title='second')])
        self.assertEqual([job['remote_id'] for job in jobs], ['12', '13'])
        self.assertEqual(jobs[0]['title'], 'first')
        self.assertEqual(len(self.library.online_jobs()), 2)

    def test_invalid_batch_rolls_back_every_new_job(self):
        with self.assertRaises(KeyError):
            self.library.enqueue_many([song(12), {'provider': 'netease', 'remote_id': '13'}])
        self.assertEqual(self.library.online_jobs(), [])
        self.assertEqual(self.library.enqueue_many([]), [])

    def test_new_import_stays_unreviewed_while_prior_organized_track_is_unchanged(self):
        old = self.imported()
        self.library.update_track(old, {'reviewed': 1, 'note': 'keep'})
        changed = self.root / 'new.mp3'
        shutil.copyfile(self.sample, changed)
        embed_online(changed, song(99))
        new = self.imported(changed)
        self.assertNotEqual(old, new)
        self.assertEqual(self.library.track(new)['reviewed'], 0)
        self.assertEqual(self.library.track(old)['reviewed'], 1)
        self.assertEqual(self.library.track(old)['note'], 'keep')
        self.assertEqual(hashlib.sha256(self.sample.read_bytes()).hexdigest(), self.library.track(old)['digest'])


if __name__ == '__main__':
    unittest.main()
