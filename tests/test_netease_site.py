"""wyapi 适配验收：假流式网络、临时曲库，不联网、不读取账号或真实音乐。"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

from djtool.library import Library
from djtool.netease_common import IdentityUnavailable, MusicUnavailable
from djtool.netease_common import AuthorizationError
from djtool.netease_site import (MAX_RESPONSE, ORIGIN, PAGE_SIZE, SERVICE,
                         NeteaseSiteClient, SiteRateLimit)
from djtool.online import Cancelled, OnlineError, download_track


def song(identifier=123, **fields):
    return {'id': identifier, 'name': 'Exact Remix', 'singer': 'Artist / Guest',
            'album': 'Album', 'picimg': 'http://p3.music.126.net/cover.jpg?token=cover-secret#fragment',
            'duration': '3:00', 'time': '2024-03-02', 'cookie': 'service-cookie-secret',
            'ip': '203.0.113.9', 'url': 'https://m1.music.126.net/song?token=media-secret',
            'account': {'MUSIC_U': 'account-secret'}, **fields}


def audio(identifier=123, **fields):
    return {'id': identifier, 'url': 'http://m1.music.126.net/audio.mp3?token=media-secret',
            'md5': 'A' * 32, 'size': '100', 'br': '320000', 'time': 180000,
            'level': 'exhigh', 'cookie': 'service-cookie-secret', 'ip': '203.0.113.9', **fields}


def page(rows, total=None, **fields):
    return {'id': 9, 'name': 'Full playlist', 'songCount': len(rows) if total is None else total,
            'songs': rows, 'cookie': 'playlist-cookie-secret', **fields}


def ok(data):
    return {'code': 200, 'data': data}


class Response:
    def __init__(self, value=None, status=200, headers=None, raw=None, blocks=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = raw if raw is not None else json.dumps(value).encode('utf-8')
        self.blocks = blocks
        self.closed = False

    def iter_content(self, size):
        yield from self.blocks if self.blocks is not None else (
            self.content[offset:offset + size] for offset in range(0, len(self.content), size))

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('fixture service failure')


class Transport:
    def __init__(self, values):
        self.values = list(values)
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set('MUSIC_U', 'old-session-secret')
        self.calls = []
        self.responses = []
        self.sent_cookies = []

    def post(self, url, **parameters):
        self.calls.append((url, parameters))
        self.sent_cookies.append(dict(self.cookies))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        response = value if isinstance(value, Response) else Response(value)
        self.responses.append(response)
        # 模拟 requests 将响应 Set-Cookie 写入 Session，下一次请求仍应清掉。
        self.cookies.set('service_session', 'server-session-secret')
        return response


class SiteTests(unittest.TestCase):
    def client(self, values, **options):
        self.transport = Transport([ok({'ip': '203.0.113.9'}), *values])
        return NeteaseSiteClient(session=self.transport, clock=lambda: 1700000000.125, **options)

    def test_ip_handshake_is_anonymous_once_and_requests_include_timestamp(self):
        client = self.client([ok(song()), ok(song()), ok(audio()), ok({'lrc': {'lyric': '[00:00]line'}})])
        # keyring 不再是依赖；把它在 sys.modules 里置空，任何 import keyring 都会直接报错。
        with patch.dict(sys.modules, {'keyring': None}):
            name, tracks = client.listing('song', '123')
            media = client.audio(tracks[0])
            self.assertEqual(client.lyrics('123'), '[00:00]line')
        self.assertEqual(name, '网易云单曲')
        self.assertFalse(client.requires_account)
        self.assertFalse(client.logged_in)
        self.assertEqual(client.backend, 'site')
        self.assertEqual(self.transport.headers['Origin'], ORIGIN)
        self.assertEqual(self.transport.calls[0][1]['json'], {'timestamp': 1700000000125})
        self.assertEqual([url for url, _ in self.transport.calls], [SERVICE + path for path in
                         ('/api/ip', '/api/getSongInfo', '/api/getSongInfo', '/api/getSongUrl', '/api/getSongLyric')])
        for _, request in self.transport.calls[1:]:
            self.assertEqual(request['json']['ip'], '203.0.113.9')
            self.assertEqual(request['json']['timestamp'], 1700000000125)
            self.assertFalse(request['allow_redirects'])
            self.assertTrue(request['stream'])
            self.assertNotIn('cookies', request)
        self.assertEqual(self.transport.calls[3][1]['json']['level'], 'exhigh')
        self.assertTrue(all(not cookies for cookies in self.transport.sent_cookies))
        self.assertEqual(dict(self.transport.cookies), {})
        self.assertTrue(all(response.closed for response in self.transport.responses))
        self.assertEqual(media['url'], 'https://m1.music.126.net/audio.mp3?token=media-secret')
        saved = json.dumps({'tracks': tracks, 'metadata': media['metadata'], 'cache': client.songs})
        for secret in ('cookie', '203.0.113.9', 'media-secret', 'cover-secret', 'MUSIC_U', 'account-secret'):
            self.assertNotIn(secret, saved)
        self.assertEqual(tracks[0]['thumbnail'], 'https://p3.music.126.net/cover.jpg')
        self.assertEqual(tracks[0]['source_metadata']['duration'], 180)

    def test_invalid_ip_handshake_stops_before_song_request(self):
        for data in ({}, {'ip': None}, {'ip': 'not-an-ip'}, {'ip': '1.2.3.4\nsecret'}, None):
            with self.subTest(data=data):
                transport = Transport([ok(data)])
                client = NeteaseSiteClient(session=transport)
                with self.assertRaises(AuthorizationError):
                    client.listing('song', '123')
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(transport.responses[0].closed)
                self.assertEqual(dict(transport.cookies), {})

    def test_ipv6_handshake_is_canonicalized(self):
        transport = Transport([ok({'ip': '2001:0db8::1'}), ok(song())])
        NeteaseSiteClient(session=transport).listing('song', '123')
        self.assertEqual(transport.calls[1][1]['json']['ip'], '2001:db8::1')

    def test_unsupported_endpoint_does_not_request_or_initialize_ip(self):
        client = self.client([])
        with self.assertRaises(AuthorizationError):
            client._post('/api/account', {}, lambda: False)
        self.assertEqual(self.transport.calls, [])

    def test_all_playlist_pages_are_read_in_exact_source_order(self):
        for total in (0, 1, 10, 500, 501, 833, 1001):
            with self.subTest(total=total):
                identifiers = list(range(total, 0, -1))
                slices = [identifiers[offset:offset + PAGE_SIZE]
                          for offset in range(0, total, PAGE_SIZE)] or [[]]
                client = self.client([ok(page([song(identifier) for identifier in rows], total))
                                      for rows in slices])
                name, tracks = client.listing('playlist', '9')
                self.assertEqual(name, 'Full playlist')
                self.assertEqual([track['remote_id'] for track in tracks], list(map(str, identifiers)))
                self.assertEqual(len(client.songs), total)
                parameters = [request['json'] for _, request in self.transport.calls[1:]]
                self.assertEqual([entry['offset'] for entry in parameters],
                                 list(range(0, total, PAGE_SIZE)) or [0])
                self.assertTrue(all(entry['limit'] == 500 for entry in parameters))
                self.assertTrue(all(entry['id'] == '9' for entry in parameters))

    def test_rounded_seconds_from_360000118_preserve_playlist_and_page_boundary(self):
        # 用户故障歌单首批 500 首中这四行返回秒位 60；它们不应阻断整个列表。
        rows = [song(identifier) for identifier in range(1, 502)]
        observed = (
            (181, 1310241345, 'Hit The Road Jack', '1:60', 120),
            (241, 2030602005, 'VVSane', '2:60', 180),
            (421, 2610829045, 'see through', '2:60', 180),
            (498, 1388821480, 'Dumebi', '2:60', 180),
        )
        for position, identifier, title, duration, _ in observed:
            rows[position - 1] = song(identifier, name=title, duration=duration)
        # 同时覆盖末位 60 出现在第 500 首和下一页第 501 首的情形。
        rows[499]['duration'] = '1:60'
        rows[500]['duration'] = '0:01:60'
        client = self.client([ok(page(rows[:500], 501)), ok(page(rows[500:], 501))])
        name, tracks = client.listing('playlist', '9')
        self.assertEqual(name, 'Full playlist')
        self.assertEqual([track['remote_id'] for track in tracks],
                         [str(row['id']) for row in rows])
        self.assertEqual(len(tracks), 501)
        self.assertEqual(len(client.songs), 501)
        for position, identifier, title, _, expected in observed:
            track = tracks[position - 1]
            self.assertEqual(track['remote_id'], str(identifier))
            self.assertEqual(track['title'], title)
            self.assertEqual(track['duration'], expected)
        self.assertEqual(tracks[499]['duration'], 120)
        self.assertEqual(tracks[500]['duration'], 120)
        self.assertEqual([request['json']['offset'] for _, request in self.transport.calls[1:]],
                         [0, 500])

    def test_incomplete_duplicate_or_changed_playlist_is_rejected_without_partial_cache(self):
        first = [song(identifier) for identifier in range(1, 501)]
        cases = {
            'early short page': [page(first[:10], 833)],
            'empty next page': [page(first, 833), page([], 833)],
            'repeated page': [page(first, 833), page(first[:333], 833)],
            'duplicate within page': [page([song(1), song(1)], 2)],
            'quantity changed': [page(first, 833), page([song(501)], 501)],
            'wrong first playlist': [page([song(1)], id=99)],
            'wrong later playlist': [page(first, 501), page([song(501)], 501, id=99)],
            'over reported total': [page([song(1), song(2)], 1)],
            'oversized page': [page([song(identifier) for identifier in range(1, 502)], 501)],
            'invalid row': [page([{'id': 1, 'name': 'Missing duration'}], 1)],
        }
        for label, pages in cases.items():
            with self.subTest(case=label):
                client = self.client([ok(value) for value in pages])
                client.songs['777'] = {'remote_id': '777'}
                with self.assertRaises(IdentityUnavailable):
                    client.listing('playlist', '9')
                self.assertEqual(client.songs, {'777': {'remote_id': '777'}})
                self.assertTrue(all(response.closed for response in self.transport.responses))

    def test_invalid_playlist_count_or_rows_is_rejected(self):
        for fields in ({'songCount': -1}, {'songCount': '1'}, {'songCount': True},
                       {'songCount': None}, {'songs': {}}, {'songs': None}):
            with self.subTest(fields=fields):
                client = self.client([ok(page([song()], **fields))])
                with self.assertRaises(IdentityUnavailable):
                    client.listing('playlist', '9')

    def test_invalid_input_ids_do_not_send_a_request(self):
        for identifier in (None, True, False, 0, -1, 1.5, '01', '1&cookie=secret', '1' * 20):
            with self.subTest(identifier=identifier):
                client = self.client([])
                with self.assertRaises(IdentityUnavailable):
                    client.listing('song', identifier)
                self.assertEqual(self.transport.calls, [])
        client = self.client([])
        with self.assertRaises(IdentityUnavailable):
            client.listing('album', '123')
        self.assertEqual(self.transport.calls, [])

    def test_song_and_audio_ids_must_match_requested_id(self):
        for values in ([ok(song(456))], [ok(song()), ok(audio(456))]):
            with self.subTest(audio=len(values) == 2):
                client = self.client(values)
                with self.assertRaises(IdentityUnavailable):
                    if len(values) == 1:
                        client.listing('song', '123')
                    else:
                        client.audio({'remote_id': '123'})

    def test_malformed_song_or_audio_objects_and_returned_ids_are_rejected(self):
        for value in (None, [], 'song', song(id=None), song(id=True), song(id='01')):
            with self.subTest(song=value):
                client = self.client([ok(value)])
                with self.assertRaises(IdentityUnavailable):
                    client.listing('song', '123')
        for value in (None, [], 'audio', audio(id=None), audio(id=True), audio(id='01')):
            with self.subTest(audio=value):
                client = self.client([ok(song()), ok(value)])
                with self.assertRaises(IdentityUnavailable):
                    client.audio({'remote_id': '123'})
        for info in (None, [], {'remote_id': '01'}, {'remote_id': True}, {}):
            with self.subTest(info=info):
                client = self.client([])
                with self.assertRaises(IdentityUnavailable):
                    client.audio(info)
                self.assertEqual(self.transport.calls, [])

    def test_supported_duration_formats_and_generated_time_is_not_release_date(self):
        client = self.client([])
        for value, seconds in (('3:00', 180), ('01:02:03', 3723),
                               ('1:60', 120), ('2:60', 180), ('0:60', 60),
                               ('00:01:60', 120), ('01:59:60', 7200),
                               ('23:59:60', 86400),
                               (180000, 180), (180123.5, 180.1235)):
            with self.subTest(value=value):
                self.assertEqual(client.track_info(song(duration=value))['duration'], seconds)
        for value in ('3:61', '3:99', '1:60:01', '1:60:60', '00:00', '00:00:00',
                      '24:00:01', '24:00:60', '300', '', None, True, 0, -1,
                      float('nan'), float('inf'), '9999:59'):
            with self.subTest(value=value), self.assertRaises(IdentityUnavailable):
                client.track_info(song(duration=value))
        for generated_time in ('2024-03-02', 1700000000000, '2026/10/05 02:47:47',
                               '2024-02-31', 'unknown'):
            with self.subTest(generated_time=generated_time):
                self.assertEqual(client.track_info(song(time=generated_time))['release_date'], '')

    def test_audio_metadata_is_sanitized_and_quality_is_forwarded(self):
        client = self.client([ok(song()), ok(audio(level='lossless'))], quality='lossless')
        result = client.audio({'remote_id': '123'})
        self.assertEqual(result['song_id'], '123')
        self.assertEqual(result['size'], 100)
        self.assertEqual(result['br'], 320000)
        self.assertEqual(result['md5'], 'a' * 32)
        self.assertEqual(result['level'], 'lossless')
        self.assertEqual(self.transport.calls[-1][1]['json']['level'], 'lossless')
        self.assertNotIn('cookie', result)
        self.assertNotIn('ip', result)
        self.assertNotIn('cookie', result['metadata'])
        with self.assertRaises(AuthorizationError):
            NeteaseSiteClient(quality='unknown', session=Transport([]))

    def test_trial_or_short_audio_is_unavailable_without_more_requests(self):
        for fields in ({'freeTrialInfo': {'end': 30}}, {'trialInfo': {'end': 30}},
                       {'freeTrail': True, 'url': None}, {'time': 30000}):
            with self.subTest(fields=fields):
                client = self.client([ok(song()), ok(audio(**fields))])
                with self.assertRaises(MusicUnavailable):
                    client.audio({'remote_id': '123'})

    def test_empty_quality_tries_same_id_and_keeps_actual_returned_level(self):
        client = self.client([ok(song()), ok(audio(url=None)), ok(audio(url='')),
                              ok(audio(level='higher', br=192000))])
        media = client.audio({'remote_id': '123'})
        requests = [args['json'] for _, args in self.transport.calls[2:]]
        self.assertEqual([(p['id'], p['level']) for p in requests],
                         [('123', 'exhigh'), ('123', 'lossless'), ('123', 'hires')])
        self.assertEqual((media['level'], media['br']), ('higher', 192000))

    def test_all_qualities_empty_checks_each_once_for_every_preference(self):
        for preference in ('exhigh', 'lossless', 'hires'):
            with self.subTest(preference=preference):
                client = self.client([ok(song()), *[ok(audio(url=None)) for _ in range(5)]],
                                     quality=preference)
                with self.assertRaisesRegex(MusicUnavailable, '五个音质档'):
                    client.audio({'remote_id': '123'})
                levels = [args['json']['level'] for _, args in self.transport.calls[2:]]
                self.assertEqual(levels[0], preference)
                self.assertCountEqual(levels, ['exhigh', 'lossless', 'hires', 'higher', 'standard'])

    def test_only_structured_audio_404_advances_quality(self):
        from djtool.network_errors import NetworkFailure
        miss = Response({'code': 404, 'message': 'Not Found', 'data': None}, status=404)
        client = self.client([ok(song()), miss, ok(audio(level='lossless'))])
        self.assertEqual(client.audio({'remote_id': '123'})['level'], 'lossless')
        for responses in ([miss], [ok(song()), Response(status=404, raw=b'Not Found')]):
            with self.subTest(requests=len(responses)):
                client = self.client(responses)
                with self.assertRaises(NetworkFailure):
                    client.audio({'remote_id': '123'})

    def test_fallback_stops_on_identity_error_rate_limit_or_malformed_url(self):
        for response, error in ((ok(audio(456)), IdentityUnavailable),
                                (Response(status=429), SiteRateLimit),
                                (ok(audio(url=False)), IdentityUnavailable),
                                (ok({k:v for k,v in audio().items() if k != 'url'}), IdentityUnavailable)):
            with self.subTest(error=error):
                client = self.client([ok(song()), ok(audio(url=None)), response])
                with self.assertRaises(error):
                    client.audio({'remote_id': '123'})
                self.assertEqual(len(self.transport.calls), 4)

    def test_rounded_metadata_seconds_allow_full_audio_but_reject_preview(self):
        for identifier, duration, actual in ((1310241345, '1:60', 119997),
                                             (2030602005, '2:60', 179002)):
            with self.subTest(identifier=identifier, audio='full'):
                client = self.client([ok(song(identifier, duration=duration)),
                                      ok(audio(identifier, time=actual))])
                result = client.audio({'remote_id': str(identifier)})
                self.assertEqual(result['song_id'], str(identifier))
                self.assertEqual(result['metadata']['duration'],
                                 120 if duration == '1:60' else 180)
            for fields in ({'time': 30000}, {'time': actual, 'freeTrialInfo': {'end': 30}}):
                with self.subTest(identifier=identifier, audio=fields):
                    client = self.client([ok(song(identifier, duration=duration)),
                                          ok(audio(identifier, **fields))])
                    with self.assertRaises(MusicUnavailable):
                        client.audio({'remote_id': str(identifier)})

    def test_observed_site_generation_times_are_not_audio_duration_or_release_date(self):
        observed = ((1310241345, '1:60', '2026/10/05 02:47:47', 4796125),
                    (2030602005, '2:60', '2026/10/05 02:47:48', 7187565))
        for identifier, duration, generated_time, size in observed:
            with self.subTest(identifier=identifier):
                client = self.client([ok(song(identifier, duration=duration, time=generated_time)),
                                      ok(audio(identifier, time=generated_time, size=size))])
                result = client.audio({'remote_id': str(identifier)})
                self.assertEqual(result['song_id'], str(identifier))
                self.assertEqual(result['size'], size)
                self.assertEqual(result['metadata']['duration'],
                                 120 if duration == '1:60' else 180)
                self.assertEqual(result['metadata']['release_date'], '')
                self.assertNotIn('time', result['metadata']['source_metadata'])
                self.assertNotIn('cookie', result)
                self.assertNotIn('cookie', result['metadata'])

    def test_unknown_or_invalid_generation_times_are_rejected(self):
        for value in ('2026/02/30 02:47:47', '2026/10/05 24:47:47',
                      '2026/13/05 02:47:47', '0000/10/05 02:47:47',
                      '2026/10/5 02:47:47', '2026/10/05 02:47:60',
                      '2026/10/05', '2026-10-05 02:47:47',
                      '2026/10/05 02:47:47\nsecret', 'unknown', ''):
            with self.subTest(value=value):
                client = self.client([ok(song()), ok(audio(time=value))])
                with self.assertRaises(IdentityUnavailable):
                    client.audio({'remote_id': '123'})

    def test_malformed_audio_duration_size_bitrate_or_md5_is_rejected(self):
        for fields in ({'time': '180000'}, {'time': True}, {'time': 0}, {'time': float('nan')},
                       {'size': 0}, {'size': True}, {'size': 1024**3 + 1}, {'size': '100x'},
                       {'br': -1}, {'br': 100000001}, {'md5': 'secret'}, {'md5': []}):
            with self.subTest(fields=fields):
                client = self.client([ok(song()), ok(audio(**fields))])
                with self.assertRaises(IdentityUnavailable):
                    client.audio({'remote_id': '123'})

    def test_untrusted_audio_urls_are_rejected_and_cover_urls_are_omitted(self):
        urls = ('https://unknown.example/audio.mp3?token=secret', 'https://127.0.0.1/audio',
                'file:///tmp/audio', 'https://user:password@m1.music.126.net/audio',
                'https://m1.music.126.net:8080/audio', 'https://m1.music.126.net.evil.example/audio')
        for url in urls:
            with self.subTest(url=url):
                client = self.client([ok(song()), ok(audio(url=url))])
                with self.assertRaises(AuthorizationError):
                    client.audio({'remote_id': '123'})
                self.assertEqual(client.track_info(song(picimg=url))['thumbnail'], '')

    def test_lyrics_accept_string_or_nested_lyric_without_other_fields(self):
        for data, expected in (({'lrc': '[00:00]line'}, '[00:00]line'),
                               ({'lrc': {'lyric': '[00:01]other', 'cookie': 'secret'}}, '[00:01]other'),
                               ({'lrc': {'lyric': 42}}, ''), ({}, ''), (None, '')):
            with self.subTest(data=data):
                client = self.client([ok(data)])
                self.assertEqual(client.lyrics('123'), expected)
                self.assertEqual(self.transport.calls[-1][1]['json']['id'], '123')

    def test_http_or_json_rate_limit_preserves_retry_after_and_stops_batch(self):
        cases = (
            (Response({'message': 'service-cookie-secret'}, status=429, headers={'Retry-After': '60'}), 60),
            (Response({'code': 429, 'data': {'retryAfter': 120}, 'message': 'media-secret'}), 120),
            (Response({'code': 429, 'data': {'retryAfter': '2.5'}}), 2.5),
            (Response(raw=b'not json', status=429, headers={'Retry-After': 'NaN'}), None),
            (Response({'code': 429, 'data': {'retryAfter': -1}}), None),
        )
        for response, retry_after in cases:
            with self.subTest(retry=retry_after):
                client = self.client([response])
                with self.assertRaises(SiteRateLimit) as caught:
                    client.listing('song', '123')
                self.assertEqual(caught.exception.code, 429)
                self.assertEqual(caught.exception.retry_after, retry_after)
                self.assertTrue(caught.exception.stop_batch)
                self.assertTrue(response.closed)
                self.assertEqual(len(self.transport.calls), 2)
                self.assertEqual(dict(self.transport.cookies), {})
                self.assertNotIn('secret', str(caught.exception))

    def test_redirect_invalid_json_or_service_error_is_sanitized_and_closed(self):
        cases = (Response(status=302, headers={'Location': 'https://other.example/secret'}),
                 Response(raw=b'not json'), Response({'code': True, 'data': song()}),
                 Response({'code': 500, 'message': 'service-cookie-secret'}), Response([]))
        for response in cases:
            with self.subTest(status=response.status_code):
                client = self.client([response])
                with self.assertRaises(AuthorizationError) as caught:
                    client.listing('song', '123')
                self.assertTrue(response.closed)
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(len(self.transport.calls), 2)
                self.assertEqual(dict(self.transport.cookies), {})

    def test_oversized_response_stops_and_closes(self):
        response = Response(blocks=[b'x' * MAX_RESPONSE, b'x'])
        client = self.client([response])
        with self.assertRaises(AuthorizationError):
            client.listing('song', '123')
        self.assertTrue(response.closed)
        self.assertEqual(dict(self.transport.cookies), {})

    def test_cancel_before_request_does_not_contact_service(self):
        for operation in ('listing', 'audio'):
            with self.subTest(operation=operation):
                client = self.client([])
                with self.assertRaises(Cancelled):
                    if operation == 'listing':
                        client.listing('playlist', '9', cancel=lambda: True)
                    else:
                        client.audio({'remote_id': '123'}, cancel=lambda: True)
                self.assertEqual(self.transport.calls, [])

    def test_cancel_during_response_closes_and_drops_cookies(self):
        response = Response(ok(song()), blocks=[b'{"code":', b'200}'])
        client = self.client([response])
        # 在读取歌曲流块时取消；IP 握手已成功，返回响应仍须关闭。
        with self.assertRaises(Cancelled):
            client.listing('song', '123', cancel=lambda: len(self.transport.calls) >= 2)
        self.assertTrue(response.closed)
        self.assertEqual(dict(self.transport.cookies), {})
        self.assertEqual(client.songs, {})

    def test_cancel_between_pages_keeps_no_partial_playlist(self):
        first = [song(identifier) for identifier in range(1, 501)]
        response = Response(ok(page(first, 501)))
        client = self.client([response])
        # 响应关闭后才取消，使第一页完整读取，第二页没有发出。
        with self.assertRaises(Cancelled):
            client.listing('playlist', '9', cancel=lambda: response.closed)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(client.songs, {})


class SiteDownloadGuardTests(unittest.TestCase):
    def test_md5_mismatch_stops_before_decode_and_leaves_no_audio_or_secret_metadata(self):
        with tempfile.TemporaryDirectory(prefix='djtool-site-md5-') as directory:
            library = Library(Path(directory) / 'library')
            payload = b'synthetic wrong audio'
            transport = Transport([ok({'ip': '203.0.113.9'}), ok(song()),
                                   ok(audio(size=len(payload), md5=hashlib.md5(b'different').hexdigest()))])
            client = NeteaseSiteClient(session=transport)
            info = client.track_info(song())
            with patch('djtool.online.transfer', side_effect=lambda url, destination, *args, **kwargs:
                       destination.write_bytes(payload)), patch('djtool.online.to_mp3') as decoder:
                with self.assertRaisesRegex(OnlineError, '音频校验'):
                    download_track(info, library, music_client=client)
            decoder.assert_not_called()
            self.assertEqual(library.tracks(), [])
            self.assertEqual(list(library.audio_dir.iterdir()), [])
            self.assertEqual(list((library.root / '.djtool/downloads').iterdir()), [])
            job = library.online_jobs()[0]
            self.assertEqual(job['state'], 'failed')
            for secret in ('cookie', '203.0.113.9', 'media-secret', 'cover-secret'):
                self.assertNotIn(secret, job['metadata_json'])


class MediaUrlTests(unittest.TestCase):
    def test_media_url_is_limited_to_official_cdn_without_credentials(self):
        from djtool.netease_common import safe_media_url
        for url in ['https://music.126.net.evil.test/file', 'file:///etc/passwd',
                    'https://user:pass@m1.music.126.net/file', 'https://127.0.0.1/file']:
            with self.assertRaises(AuthorizationError):
                safe_media_url(url)
        self.assertEqual(safe_media_url('http://m801.music.126.net/a/b.mp3?x=1'), 'https://m801.music.126.net/a/b.mp3?x=1')


if __name__ == '__main__':
    unittest.main()
