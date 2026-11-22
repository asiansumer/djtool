"""模拟网站短冷却的整歌单续跑；不联网，不实际等待或下载真实音乐。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from djtool.library import Library
from djtool.netease_common import AuthorizationError
from djtool.netease_site import SiteRateLimit
from djtool.workers import DownloadWorker


def info(number):
    return {'provider': 'netease', 'remote_id': str(number),
            'url': f'https://music.163.com/song?id={number}',
            'title': f'Cooldown fixture {number}', 'artist': 'Fixture', 'duration': 180}


class Clock:
    def __init__(self):
        self.now = 1000.
        self.sleeps = []
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SiteCooldownWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temporary.name) / 'library')
        self.client = SimpleNamespace(rate_limit=None, _ip='same-connection-fixture')

    def tearDown(self):
        self.temporary.cleanup()

    def jobs(self, count=2):
        return self.library.enqueue_many([info(number) for number in range(1000, 1000 + count)])

    def run_worker(self, jobs, download, wait=None):
        worker = DownloadWorker(self.library, jobs)
        results = []
        worker.completed.connect(results.append)
        with patch('djtool.netease_backend.create_music_client', return_value=self.client) as create, \
             patch('djtool.online.download_track', side_effect=download) as called, \
             patch.object(worker, 'wait_for_retry', side_effect=wait, return_value=True) as waited:
            worker.run()
        self.assertEqual(len(results), 1)
        create.assert_called_once()
        return results[0], called, waited

    def test_short_cooldown_retries_same_song_then_finishes_without_failure_count(self):
        events = iter([SiteRateLimit(37), ('first', True), ('second', True)])
        attempted = []
        def download(song, *args, **kwargs):
            attempted.append(song['remote_id'])
            self.assertIs(kwargs['music_client'], self.client)
            result = next(events)
            if isinstance(result, Exception):
                self.client.rate_limit = result
                raise result
            self.assertIsNone(self.client.rate_limit)
            return result
        counts, called, waited = self.run_worker(self.jobs(), download)
        self.assertEqual(attempted, ['1000', '1000', '1001'])
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (2, 2, 0))
        self.assertFalse(counts['cancelled'])
        self.assertNotIn('batch_error', counts)
        self.assertEqual(waited.call_count, 1)
        self.assertEqual(self.client._ip, 'same-connection-fixture')

    def test_retry_budget_resets_after_success_for_each_song(self):
        per_song = {}
        def download(song, *args, **kwargs):
            identifier = song['remote_id']
            per_song[identifier] = per_song.get(identifier, 0) + 1
            if per_song[identifier] <= 2:
                raise SiteRateLimit(2)
            return identifier, True
        counts, called, waited = self.run_worker(self.jobs(), download)
        self.assertEqual(called.call_count, 6)
        self.assertEqual(waited.call_count, 4)
        self.assertEqual(per_song, {'1000': 3, '1001': 3})
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (2, 2, 0))

    def test_third_rate_limit_on_same_song_stops_with_no_false_failure(self):
        jobs = self.jobs()
        counts, called, waited = self.run_worker(jobs, SiteRateLimit(37))
        self.assertEqual(called.call_count, 3)
        self.assertEqual(waited.call_count, 2)
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (0, 0, 0))
        self.assertIn('batch_error', counts)
        self.assertEqual(counts['retry_after'], 37)
        self.assertEqual([job['state'] for job in self.library.online_jobs()], ['ready', 'ready'])

    def test_missing_or_long_cooldown_stops_without_automatic_retry(self):
        for delay in (None, 0, -1, 301, 3600, True):
            with self.subTest(delay=delay):
                error = SiteRateLimit(delay)
                counts, called, waited = self.run_worker(self.jobs(), error)
                self.assertEqual(called.call_count, 1)
                waited.assert_not_called()
                self.assertEqual((counts['processed'], counts['failed']), (0, 0))
                self.assertIn('batch_error', counts)

    def test_can_wait_rejects_nonfinite_or_untrusted_retry_values(self):
        error = SiteRateLimit(None)
        for delay in (float('inf'), float('nan'), '37', True, None, 0, -1, 300.001):
            with self.subTest(delay=delay):
                error.retry_after = delay
                self.assertFalse(DownloadWorker.can_wait_for_retry(error))
        for delay in (.1, 37, 300):
            error.retry_after = delay
            self.assertTrue(DownloadWorker.can_wait_for_retry(error))
        self.assertFalse(DownloadWorker.can_wait_for_retry(AuthorizationError('other platform', 429)))

    def test_non_site_rate_limit_retains_stop_behavior(self):
        error = AuthorizationError('different service limit', 429)
        error.retry_after = 37
        counts, called, waited = self.run_worker(self.jobs(), error)
        self.assertEqual(called.call_count, 1)
        waited.assert_not_called()
        self.assertEqual((counts['processed'], counts['failed']), (1, 1))
        self.assertIn('batch_error', counts)

    def test_client_initialization_failure_stops_1312_jobs_after_one_factory_call(self):
        jobs = self.jobs(1312)
        worker = DownloadWorker(self.library, jobs)
        results = []
        worker.completed.connect(results.append)
        before = self.library.online_jobs()
        with patch('djtool.netease_backend.create_music_client', side_effect=ValueError('private invalid settings')) as create, \
             patch('djtool.online.download_track') as download, \
             patch.object(worker, 'wait_for_retry') as waited:
            worker.run()
        create.assert_called_once_with(self.library)
        download.assert_not_called()
        waited.assert_not_called()
        self.assertEqual(len(results), 1)
        counts = results[0]
        self.assertEqual((counts['total'], counts['processed'], counts['failed']), (1312, 0, 0))
        self.assertIn('batch_error', counts)
        self.assertNotIn('private invalid settings', counts['batch_error'])
        self.assertEqual(self.library.online_jobs(), before)
        self.assertEqual(self.library.tracks(), [])

    def test_individual_value_error_does_not_stop_remaining_songs(self):
        counts, called, waited = self.run_worker(self.jobs(3), ValueError('individual fixture failure'))
        self.assertEqual(called.call_count, 3)
        waited.assert_not_called()
        self.assertEqual((counts['processed'], counts['failed']), (3, 3))
        self.assertNotIn('batch_error', counts)

    def test_lyrics_cooldown_waits_before_next_song_without_redownloading_success(self):
        jobs = self.jobs(3)
        attempted = []
        def download(song, *args, **kwargs):
            attempted.append(song['remote_id'])
            if song['remote_id'] == '1000':
                self.client.rate_limit = SiteRateLimit(37)
            else:
                self.assertIsNone(self.client.rate_limit)
            return song['remote_id'], True
        counts, called, waited = self.run_worker(jobs, download)
        self.assertEqual(attempted, ['1000', '1001', '1002'])
        self.assertEqual(waited.call_count, 1)
        self.assertEqual(waited.call_args.args[1]['id'], jobs[1]['id'])
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (3, 3, 0))

    def test_final_song_lyrics_rate_limit_does_not_wait_after_batch_finished(self):
        def download(song, *args, **kwargs):
            self.client.rate_limit = SiteRateLimit(37)
            return song['remote_id'], True
        counts, called, waited = self.run_worker(self.jobs(1), download)
        waited.assert_not_called()
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (1, 1, 0))
        self.assertNotIn('batch_error', counts)

    def test_cancel_during_cooldown_preserves_unattempted_song_counts(self):
        counts, called, waited = self.run_worker(self.jobs(), SiteRateLimit(37), wait=lambda *args: False)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(waited.call_count, 1)
        self.assertTrue(counts['cancelled'])
        self.assertEqual((counts['processed'], counts['failed']), (0, 0))
        self.assertNotIn('batch_error', counts)

    def test_cancel_during_lyrics_cooldown_preserves_already_imported_song(self):
        def download(song, *args, **kwargs):
            self.client.rate_limit = SiteRateLimit(37)
            return song['remote_id'], True
        counts, called, waited = self.run_worker(self.jobs(), download, wait=lambda *args: False)
        self.assertEqual(called.call_count, 1)
        self.assertTrue(counts['cancelled'])
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (1, 1, 0))

    def test_one_1312_song_batch_automatically_resumes_every_ten_songs(self):
        remaining_quota = 10
        attempted = []
        def download(song, *args, **kwargs):
            nonlocal remaining_quota
            self.assertIs(kwargs['music_client'], self.client)
            if remaining_quota == 0:
                self.client.rate_limit = SiteRateLimit(37)
                raise self.client.rate_limit
            remaining_quota -= 1
            attempted.append(song['remote_id'])
            return song['remote_id'], True
        def wait(*args):
            nonlocal remaining_quota
            remaining_quota = 10
            return True
        counts, called, waited = self.run_worker(self.jobs(1312), download, wait)
        self.assertEqual(waited.call_count, 131)
        self.assertEqual(called.call_count, 1443)
        self.assertEqual(attempted, [str(number) for number in range(1000, 2312)])
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (1312, 1312, 0))
        self.assertNotIn('batch_error', counts)
        self.assertEqual(self.client._ip, 'same-connection-fixture')

    def test_wait_uses_server_delay_plus_margin_and_emits_second_countdown(self):
        jobs = self.jobs()
        worker = DownloadWorker(self.library, jobs)
        clock = Clock()
        messages = []
        worker.updated.connect(lambda identifier, message, percent: messages.append((identifier, message, percent)))
        counts = {'cancelled': False}
        with patch('djtool.workers.time.monotonic', side_effect=clock.monotonic), \
             patch('djtool.workers.time.sleep', side_effect=clock.sleep):
            self.assertTrue(worker.wait_for_retry(SiteRateLimit(2.2), jobs[0], counts))
        self.assertAlmostEqual(clock.now, 1003.2)
        self.assertTrue(all(0 < value <= .2 for value in clock.sleeps))
        self.assertEqual([message for _, message, _ in messages], [
            '网站限流，等待 4 秒后自动继续', '网站限流，等待 3 秒后自动继续',
            '网站限流，等待 2 秒后自动继续', '网站限流，等待 1 秒后自动继续'])
        self.assertTrue(all(identifier == jobs[0]['id'] and percent == 0
                            for identifier, _, percent in messages))
        self.assertFalse(counts['cancelled'])

    def test_wait_cancellation_is_checked_after_at_most_point_two_seconds(self):
        jobs = self.jobs()
        worker = DownloadWorker(self.library, jobs)
        clock = Clock()
        interrupted = False
        def sleep(seconds):
            nonlocal interrupted
            clock.sleep(seconds)
            interrupted = True
        counts = {'cancelled': False}
        with patch('djtool.workers.time.monotonic', side_effect=clock.monotonic), \
             patch('djtool.workers.time.sleep', side_effect=sleep), \
             patch.object(worker, 'isInterruptionRequested', side_effect=lambda: interrupted):
            self.assertFalse(worker.wait_for_retry(SiteRateLimit(37), jobs[0], counts))
        self.assertEqual(clock.sleeps, [.2])
        self.assertTrue(counts['cancelled'])


if __name__ == '__main__':
    unittest.main()
