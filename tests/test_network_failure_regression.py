"""离线回归共同网络故障：安全诊断、提前停止、队列与 Set 不受影响。

使用隔离曲库与模拟网络异常；这些测试不替代冻结 App 的真实下载验收。
"""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
import requests
from urllib3.exceptions import MaxRetryError

from djtool.library import Library
from djtool.network_errors import NetworkFailure, network_failure
from djtool.online import friendly_error
from djtool.workers import DownloadWorker


PRIVATE = 'signature=private-network-fixture&ip=192.0.2.99&cookie=secret-cookie'


def http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = 'https://m1.music.126.net/audio?' + PRIVATE
    response.headers['Retry-After'] = '25'
    return requests.HTTPError(response.url, response=response)


def info(number):
    return {'provider': 'netease', 'remote_id': str(number),
            'url': f'https://music.163.com/song?id={number}',
            'title': f'Network fixture {number}', 'artist': 'Fixture', 'duration': 180}


class NetworkDiagnosticTests(unittest.TestCase):
    def test_classification_is_safe_and_preserves_only_whitelist_fields(self):
        cases = ((requests.exceptions.SSLError(PRIVATE), 'tls', True, False),
                 (requests.exceptions.ProxyError(PRIVATE), 'proxy', True, False),
                 (requests.ConnectTimeout(PRIVATE), 'timeout', False, True),
                 (requests.ReadTimeout(PRIVATE), 'timeout', False, True),
                 (requests.ConnectionError(PRIVATE), 'connection', False, True),
                 (http_error(429), 'rate_limit', True, False),
                 (http_error(404), 'http', False, False),
                 (requests.RequestException(PRIVATE), 'request', False, False))
        for raw, kind, stop_batch, transient in cases:
            with self.subTest(kind=kind):
                error = network_failure(raw, stage='media')
                self.assertEqual((error.kind, error.stop_batch, error.transient),
                                 (kind, stop_batch, transient))
                self.assertIn('音频下载', str(error))
                exposed = str(error) + friendly_error(error) + json.dumps(error.diagnostic())
                for private in ('private-network-fixture', '192.0.2.99', 'secret-cookie',
                                'music.126.net', 'signature='):
                    self.assertNotIn(private, exposed)
        self.assertEqual(network_failure(http_error(429)).retry_after, 25)
        self.assertIn('HTTP 404', str(network_failure(http_error(404))))

    def test_nested_exception_diagnostics_include_only_type_and_numeric_errno(self):
        reason = OSError(8, PRIVATE)
        wrapped = MaxRetryError(None, 'https://m1.music.126.net/audio?' + PRIVATE, reason)
        error = network_failure(requests.ConnectionError(wrapped), stage='service')
        self.assertEqual(error.stage, 'service')
        self.assertEqual(error.exception_types, ('ConnectionError', 'MaxRetryError', 'OSError'))
        self.assertEqual(error.errnos, (8,))
        self.assertNotIn('private-network-fixture', json.dumps(error.diagnostic()))

    def test_certificate_failure_is_distinguished_without_reading_exception_text(self):
        cause = ssl.SSLCertVerificationError(1, PRIVATE)
        raw = requests.exceptions.SSLError(PRIVATE)
        raw.__cause__ = cause
        error = network_failure(raw, stage='media')
        self.assertEqual(error.kind, 'certificate')
        self.assertTrue(error.stop_batch)
        self.assertIn('TLS 证书验证失败', str(error))

    def test_exception_string_is_never_accessed_or_reemitted(self):
        class NoStringConnection(requests.ConnectionError):
            def __str__(self):
                raise AssertionError('Original network text must not be inspected')
        error = network_failure(NoStringConnection(PRIVATE), stage=PRIVATE)
        self.assertEqual(error.kind, 'connection')
        self.assertEqual(error.stage, 'network')
        self.assertEqual(error.exception_types, ())
        self.assertNotIn('private-network-fixture', str(error))

    def test_diagnostic_constructor_rejects_arbitrary_fields_and_nonfinite_retry(self):
        error = NetworkFailure(PRIVATE, PRIVATE, exception_types=(PRIVATE, 'SSLError'),
                               errnos=(True, '8', 8), status_code=PRIVATE, retry_after=float('inf'))
        self.assertEqual((error.stage, error.kind), ('network', 'request'))
        self.assertEqual(error.exception_types, ('SSLError',))
        self.assertEqual(error.errnos, (8,))
        self.assertIsNone(error.status_code)
        self.assertIsNone(error.retry_after)
        self.assertNotIn('private-network-fixture', json.dumps(error.diagnostic()))

    def test_service_request_configuration_errors_stop_but_single_media_errors_do_not(self):
        for cls in (requests.exceptions.InvalidHeader, requests.exceptions.InvalidSchema,
                    requests.exceptions.MissingSchema, requests.exceptions.InvalidURL,
                    requests.exceptions.InvalidJSONError):
            with self.subTest(type=cls.__name__):
                raw = cls(PRIVATE)
                service = network_failure(raw, stage='service')
                self.assertEqual(service.kind, 'configuration')
                self.assertTrue(service.stop_batch)
                self.assertFalse(service.transient)
                self.assertIn(cls.__name__, service.exception_types)
                self.assertIn('在线解析服务：请求配置无效', str(service))
                media = network_failure(raw, stage='media')
                self.assertEqual(media.kind, 'request')
                self.assertFalse(media.stop_batch)
                for error in (service, media):
                    self.assertNotIn('private-network-fixture', str(error))
                    self.assertNotIn('192.0.2.99', json.dumps(error.diagnostic()))

    def test_nested_os_permission_denials_stop_without_exposing_network_target(self):
        for code in (1, 13):
            with self.subTest(errno=code):
                wrapped = MaxRetryError(None, 'https://m1.music.126.net/audio?' + PRIVATE,
                                       OSError(code, PRIVATE))
                error = network_failure(requests.ConnectionError(wrapped), stage='service')
                self.assertEqual(error.kind, 'permission')
                self.assertTrue(error.stop_batch)
                self.assertFalse(error.transient)
                self.assertEqual(error.errnos, (code,))
                self.assertIn('PermissionError', error.exception_types)
                self.assertNotIn('private-network-fixture', str(error))
                self.assertNotIn('192.0.2.99', json.dumps(error.diagnostic()))

    def test_ssl_error_errno_one_remains_tls_diagnostic(self):
        ssl_error = ssl.SSLError(1, PRIVATE)
        raw = requests.exceptions.SSLError(PRIVATE)
        raw.__cause__ = ssl_error
        error = network_failure(raw, stage='media')
        self.assertEqual(error.kind, 'tls')
        self.assertTrue(error.stop_batch)
        self.assertNotIn('系统拒绝', str(error))


class DownloadNetworkCircuitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temporary.name) / 'library')

    def tearDown(self):
        self.temporary.cleanup()

    def run_worker(self, jobs, effect):
        worker = DownloadWorker(self.library, jobs)
        result = []
        worker.completed.connect(result.append)
        with patch('djtool.netease_backend.create_music_client', return_value=object()), \
             patch('djtool.online.download_track', side_effect=effect) as download:
            worker.run()
        self.assertEqual(len(result), 1)
        return result[0], download.call_count

    def test_ssl_and_proxy_failures_stop_after_one_attempt(self):
        for raw in (requests.exceptions.SSLError(PRIVATE), requests.exceptions.ProxyError(PRIVATE)):
            with self.subTest(type=type(raw).__name__):
                jobs = self.library.enqueue_many([info(number) for number in range(100, 110)])
                counts, calls = self.run_worker(jobs, raw)
                self.assertEqual(calls, 1)
                self.assertEqual((counts['processed'], counts['failed'], counts['total']), (1, 1, 10))
                self.assertIn('batch_error', counts)
                self.assertNotIn('private-network-fixture', counts['batch_error'])

    def test_three_consecutive_connection_or_timeout_failures_stop_without_retry(self):
        for raw in (requests.ConnectionError(PRIVATE), requests.ReadTimeout(PRIVATE)):
            with self.subTest(type=type(raw).__name__):
                jobs = self.library.enqueue_many([info(number) for number in range(100, 110)])
                counts, calls = self.run_worker(jobs, raw)
                self.assertEqual(calls, 3)
                self.assertEqual((counts['processed'], counts['failed'], counts['total']), (3, 3, 10))
                self.assertIn('batch_error', counts)

    def test_service_configuration_and_system_permission_denial_stop_after_one_attempt(self):
        raw_permission = requests.ConnectionError(OSError(13, PRIVATE))
        for error in (network_failure(requests.exceptions.InvalidHeader(PRIVATE), stage='service'),
                      network_failure(raw_permission, stage='service')):
            with self.subTest(kind=error.kind):
                jobs = self.library.enqueue_many([info(number) for number in range(100, 110)])
                counts, calls = self.run_worker(jobs, error)
                self.assertEqual((calls, counts['processed'], counts['failed']), (1, 1, 1))
                self.assertIn('batch_error', counts)
                self.assertNotIn('private-network-fixture', counts['batch_error'])

    def test_single_media_url_configuration_error_does_not_stop_whole_playlist(self):
        jobs = self.library.enqueue_many([info(number) for number in range(100, 110)])
        error = network_failure(requests.exceptions.InvalidURL(PRIVATE), stage='media')
        counts, calls = self.run_worker(jobs, error)
        self.assertEqual((calls, counts['processed'], counts['failed']), (10, 10, 10))
        self.assertNotIn('batch_error', counts)

    def test_success_and_individual_http_failure_reset_consecutive_network_count(self):
        jobs = self.library.enqueue_many([info(number) for number in range(100, 109)])
        events = iter([requests.ConnectionError(PRIVATE), requests.ConnectionError(PRIVATE), None,
                       requests.ReadTimeout(PRIVATE), requests.ReadTimeout(PRIVATE), http_error(404),
                       requests.ConnectionError(PRIVATE), requests.ConnectionError(PRIVATE), None])
        def download(*args, **kwargs):
            event = next(events)
            if event is not None:
                raise event
            return 'isolated-placeholder', True
        counts, calls = self.run_worker(jobs, download)
        self.assertEqual(calls, 9)
        self.assertEqual((counts['processed'], counts['added'], counts['failed']), (9, 2, 7))
        self.assertNotIn('batch_error', counts)

    def test_http_rate_limit_stops_and_other_http_statuses_remain_individual_failures(self):
        jobs = self.library.enqueue_many([info(number) for number in range(100, 110)])
        counts, calls = self.run_worker(jobs, http_error(429))
        self.assertEqual((calls, counts['processed'], counts['failed']), (1, 1, 1))
        self.assertEqual(counts['retry_after'], 25)
        counts, calls = self.run_worker(jobs, http_error(404))
        self.assertEqual((calls, counts['processed'], counts['failed']), (10, 10, 10))
        self.assertNotIn('batch_error', counts)

    def test_global_failure_preserves_1311_unattempted_jobs_original_file_and_set(self):
        old = self.library.root / 'existing.mp3'
        original = b'Isolated existing audio presence fixture'
        old.write_bytes(original)
        with self.library.connection() as db:
            db.execute('INSERT INTO tracks(id,digest,title,artist,duration,path,source_path,filename) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       ('old-track', 'old-digest', 'Existing', 'Artist', 180, str(old), str(old), old.name))
        collection = self.library.create_collection('Existing Set', 'set')
        self.library.add_member(collection, 'old-track')
        jobs = self.library.enqueue_many([info(number) for number in range(1000, 2312)])
        worker = DownloadWorker(self.library, jobs)
        result = []
        worker.completed.connect(result.append)
        client = SimpleNamespace(audio=Mock(side_effect=network_failure(
            requests.exceptions.SSLError(PRIVATE), stage='media')))
        # 调用真实 download_track 的状态持久化与暂存清理，只模拟其网络客户端。
        with patch('djtool.netease_backend.create_music_client', return_value=client):
            worker.run()
        self.assertEqual(client.audio.call_count, 1)
        self.assertEqual((result[0]['processed'], result[0]['failed'], result[0]['total']), (1, 1, 1312))
        states = [job['state'] for job in self.library.online_jobs()]
        self.assertEqual(states.count('failed'), 1)
        self.assertEqual(states.count('ready'), 1311)
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual([track['id'] for track in self.library.tracks(collection)], ['old-track'])
        self.assertEqual(len(self.library.tracks()), 1)
        self.assertEqual(list((self.library.root / '.djtool' / 'downloads').iterdir()), [])
        for job in self.library.online_jobs():
            self.assertNotIn('private-network-fixture', job['message'])


if __name__ == '__main__':
    unittest.main()
