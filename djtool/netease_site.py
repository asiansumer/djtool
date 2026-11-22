"""wyapi 在线解析适配：使用公开 JSON 接口，音频仍从网易 CDN 下载。

歌单每批 500 首是接口分页，绝不采用网页的每页 10 首 ZIP 操作。
IP 握手、临时媒体 URL 只留在当前客户端内存；服务返回的 cookie 字段
无条件丢弃，不读取网易云账号、不把站点返回的凭据发送给网易或写入曲库。
"""
from datetime import datetime
import ipaddress
import json
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from djtool.network_errors import network_failure

from djtool.netease_common import AuthorizationError, IdentityUnavailable, MusicUnavailable, safe_media_url

SERVICE = 'https://nextmusic.toubiec.cn'
ORIGIN = 'https://wyapi.toubiec.cn'
ENDPOINTS = frozenset(('/api/ip', '/api/playlist_trackall', '/api/getSongInfo',
                       '/api/getSongUrl', '/api/getSongLyric'))
PAGE_SIZE = 500
MAX_RESPONSE = 8 * 1024 * 1024
QUALITIES = ('exhigh', 'lossless', 'hires')
# 首选档没有地址时，先保留 DJ 使用的音质，最后才尝试有损的较低档。
AUDIO_LEVELS = ('lossless', 'exhigh', 'hires', 'higher', 'standard')


class _QualityNotFound(Exception):
    """站点在单个音质查询上明确返回了结构化 404。"""


class SiteRateLimit(AuthorizationError):
    """站点限流时停止批次，保留队列；不换 IP、不循环绕过限额。"""
    stop_batch = True

    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        detail = f'，请在约 {math.ceil(retry_after)} 秒后重试' if retry_after else '，请稍后重试'
        super().__init__('在线解析服务已限流' + detail + '；未完成的歌曲已保留。', 429)


def _identifier(value):
    if type(value) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,18}', str(value)):
        raise IdentityUnavailable('在线解析返回的歌曲或歌单 ID 无效，未使用同名结果替代。')
    return str(value)


def _positive(value, label, maximum):
    if value in (None, ''):
        return None
    if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
        value = int(value)
    if type(value) is not int or not 0 < value <= maximum:
        raise IdentityUnavailable(f'在线解析返回的{label}无效，未下载。')
    return value


def _duration(value):
    # 公开站点的 duration 是 M:SS / H:MM:SS；数字格式按上游毫秒处理。
    if isinstance(value, str) and re.fullmatch(r'\d{1,4}:\d{2}(:\d{2})?', value):
        parts = [int(part) for part in value.split(':')]
        # 站点把毫秒四舍五入为秒时，可能返回 1:60 / 2:60 而未进位。
        # 只容许末位秒等于 60，由下方换算自然进位；中间分钟仍须小于 60。
        if any(part >= 60 for part in parts[1:-1]) or parts[-1] > 60:
            raise IdentityUnavailable('在线解析返回的歌曲时长无效。')
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
    elif type(value) in (int, float) and math.isfinite(value):
        seconds = value / 1000
    else:
        raise IdentityUnavailable('在线解析缺少有效歌曲时长，无法核对完整音频。')
    if not 0 < seconds <= 24 * 60 * 60:
        raise IdentityUnavailable('在线解析返回的歌曲时长无效。')
    return seconds


def _text(value, default=''):
    return value if isinstance(value, str) else default


def _image_url(value):
    if not value:
        return ''
    try:
        parts = urlsplit(safe_media_url(value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except (AuthorizationError, ValueError):
        return ''


def _audio_time_ms(value):
    # 当前站点 time 是响应生成时间，不是歌曲时长或发行日期。
    # 只接受已实查的完整日期格式；保留数字毫秒时长的额外核对以兼容上游。
    if value is None:
        return None
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}', value):
        try:
            datetime.strptime(value, '%Y/%m/%d %H:%M:%S')
            return None
        except ValueError:
            pass
    raise IdentityUnavailable('在线解析音频时间字段无效，未下载。')


class NeteaseSiteClient:
    backend = 'site'
    requires_account = False
    source_label = '网易云 · wyapi 在线解析'

    def __init__(self, quality='exhigh', session=None, clock=None):
        if quality not in QUALITIES:
            raise AuthorizationError('在线解析音质设置无效。')
        self.quality = quality
        self.session = session if session is not None else requests.Session()
        self.clock = clock or time.time
        self._ip = None
        self.rate_limit = None
        self.songs = {}
        self.session.headers.update({'Content-Type': 'application/json', 'Origin': ORIGIN,
                                     'Referer': ORIGIN + '/',
                                     'User-Agent': 'DJTOOL/0.11'})

    @property
    def logged_in(self):
        return False

    def _post(self, path, parameters, cancel):
        try:
            return self._post_response(path, parameters, cancel)
        except requests.RequestException as error:
            # 原始 Requests 错误可能带 IP 或短期媒体地址，只保留固定类别与阶段。
            raise network_failure(error, stage='service') from error
        finally:
            self.session.cookies.clear()

    def _post_response(self, path, parameters, cancel):
        from djtool.online import check_cancel
        if path not in ENDPOINTS:
            raise AuthorizationError('在线解析接口不受支持。')
        check_cancel(cancel)
        # 站点与 CDN 使用不同的 Session；每次丢弃服务器 Cookie，避免隐式账号请求。
        self.session.cookies.clear()
        response = self.session.post(SERVICE + path, json=parameters, timeout=(10, 35),
                                     allow_redirects=False, stream=True)
        try:
            raw = bytearray()
            for block in response.iter_content(65536):
                check_cancel(cancel)
                raw.extend(block)
                if len(raw) > MAX_RESPONSE:
                    raise AuthorizationError('在线解析返回过大的数据，已停止读取。')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                result = None
            if response.status_code == 429 or (isinstance(result, dict) and result.get('code') == 429):
                data = result.get('data') if isinstance(result, dict) else None
                retry = data.get('retryAfter') if isinstance(data, dict) else None
                if retry is None:
                    retry = response.headers.get('Retry-After')
                try:
                    retry = float(retry)
                    if not math.isfinite(retry) or retry <= 0:
                        retry = None
                except (ValueError, TypeError, OverflowError):
                    retry = None
                if retry is None and isinstance(data, dict):
                    reset_at = data.get('resetAt')
                    if type(reset_at) in (int, float) and math.isfinite(reset_at):
                        retry = max(0, reset_at / 1000 - self.clock()) or None
                self.rate_limit = SiteRateLimit(retry)
                raise self.rate_limit
            if (path == '/api/getSongUrl' and response.status_code == 404
                    and isinstance(result, dict) and result.get('code') == 404
                    and 'data' in result and result['data'] is None):
                raise _QualityNotFound
            if 300 <= response.status_code < 400:
                raise AuthorizationError('在线解析地址发生跳转，未跟随发送请求。')
            response.raise_for_status()
            if not isinstance(result, dict) or type(result.get('code')) is not int:
                raise AuthorizationError('在线解析响应格式异常，请稍后重试。')
            if result['code'] != 200:
                # 不转述可能含 IP/临时地址/凭据的原始服务消息。
                raise AuthorizationError('在线解析服务暂未返回有效结果，请稍后重试。', result['code'])
            check_cancel(cancel)
            return result.get('data')
        finally:
            response.close()
            self.session.cookies.clear()

    def _call(self, path, parameters, cancel=lambda: False):
        # 歌词是可选字段；若它触发限流被上层降级，本批后续也不能继续请求。
        if self.rate_limit is not None:
            raise self.rate_limit
        if self._ip is None:
            data = self._post('/api/ip', {'timestamp': int(self.clock() * 1000)}, cancel)
            try:
                self._ip = str(ipaddress.ip_address(data.get('ip') if isinstance(data, dict) else ''))
            except ValueError:
                raise AuthorizationError('在线解析服务未完成连接检测，请稍后重试。') from None
        return self._post(path, {**parameters, 'timestamp': int(self.clock() * 1000),
                                 'ip': self._ip}, cancel)

    def track_info(self, song):
        if not isinstance(song, dict):
            raise IdentityUnavailable('在线解析歌曲信息格式异常。')
        identifier = _identifier(song.get('id'))
        title = _text(song.get('name'), '未知歌曲')
        artist = _text(song.get('singer'), '未知艺人')
        album = _text(song.get('album'))
        duration = _duration(song.get('duration'))
        thumbnail = _image_url(song.get('picimg'))
        # 字段白名单：不持久化整个响应，更不保存 cookie/IP/媒体 URL。
        public = {'id': identifier, 'name': title, 'singer': artist,
                  'album': album, 'duration': duration, 'picimg': thumbnail}
        return {'provider': 'netease', 'remote_id': identifier,
                'url': 'https://music.163.com/song?id=' + identifier,
                'title': title, 'artist': artist, 'album': album, 'album_artist': '',
                'duration': duration, 'thumbnail': thumbnail, 'track_number': '',
                'release_date': '', 'source_metadata': public,
                'availability_reason': '', 'acquisition_backend': 'site'}

    def _song(self, identifier, cancel):
        data = self._call('/api/getSongInfo', {'id': identifier}, cancel)
        track = self.track_info(data)
        if track['remote_id'] != identifier:
            raise IdentityUnavailable('在线解析歌曲 ID 不一致，未使用同名结果替代。')
        return track

    def listing(self, kind, identifier, cancel=lambda: False):
        from djtool.online import check_cancel
        identifier = _identifier(identifier)
        if kind == 'song':
            track = self._song(identifier, cancel)
            self.songs[identifier] = track
            return '网易云单曲', [track]
        if kind != 'playlist':
            raise IdentityUnavailable('目前只支持网易云单曲或歌单链接。')
        tracks, seen, total, offset, name = [], set(), None, 0, '网易云歌单'
        while True:
            check_cancel(cancel)
            data = self._call('/api/playlist_trackall',
                              {'id': identifier, 'limit': PAGE_SIZE, 'offset': offset}, cancel)
            if not isinstance(data, dict) or _identifier(data.get('id')) != identifier:
                raise IdentityUnavailable('在线解析返回的歌单 ID 不一致。')
            count, rows = data.get('songCount'), data.get('songs')
            if type(count) is not int or count < 0 or not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise IdentityUnavailable('在线解析返回的歌单数量或列表异常。')
            if total is not None and count != total:
                raise IdentityUnavailable('歌单在读取期间发生变化，请重新读取。')
            total = count
            name = _text(data.get('name'), name)
            for row in rows:
                track = self.track_info(row)
                song_id = track['remote_id']
                if song_id in seen:
                    raise IdentityUnavailable('在线解析歌单返回重复歌曲 ID，未把不完整列表当作成功。')
                seen.add(song_id)
                tracks.append(track)
            if len(tracks) > total:
                raise IdentityUnavailable('在线解析歌单歌曲数量不一致。')
            if len(tracks) == total:
                break
            if len(rows) < PAGE_SIZE:
                raise IdentityUnavailable('在线解析只返回部分歌单，未把不完整列表当作成功。')
            offset += len(rows)
        check_cancel(cancel)
        self.songs.update({track['remote_id']: track for track in tracks})
        return name, tracks

    def audio(self, info, cancel=lambda: False):
        from djtool.online import check_cancel
        if not isinstance(info, dict):
            raise IdentityUnavailable('歌曲信息无效，请重新读取链接。')
        identifier = _identifier(info.get('remote_id'))
        metadata = self._song(identifier, cancel)
        for requested_level in dict.fromkeys((self.quality, *AUDIO_LEVELS)):
            check_cancel(cancel)
            try:
                data = self._call('/api/getSongUrl', {'id': identifier, 'level': requested_level}, cancel)
            except _QualityNotFound:
                continue
            if not isinstance(data, dict) or _identifier(data.get('id')) != identifier:
                raise IdentityUnavailable('在线解析音频歌曲 ID 不一致，未下载。')
            if any(data.get(key) for key in ('freeTrialInfo', 'trialInfo', 'freeTrail')):
                raise MusicUnavailable('在线解析仅返回试听片段，未作为完整歌曲入库。')
            # 只有同 ID、明确为空的地址表示当前档无源。协议异常、拒绝或限流直接交还调用方。
            if 'url' not in data or (data['url'] is not None and not isinstance(data['url'], str)):
                raise IdentityUnavailable('在线解析音频地址字段异常，未下载。')
            if data['url']:
                break
        else:
            raise MusicUnavailable('这首歌的五个音质档均未返回完整音源，可稍后重试。')
        actual = _audio_time_ms(data.get('time'))
        if actual is not None:
            expected = metadata['duration'] * 1000
            if abs(actual - expected) > max(2000, expected * .02):
                raise MusicUnavailable('在线解析返回的音频时长不完整，未下载。')
        url = safe_media_url(data['url'])
        size = _positive(data.get('size'), '文件大小', 1024**3)
        bitrate = _positive(data.get('br'), '音频码率', 100000000)
        md5 = data.get('md5')
        if md5 not in (None, '') and (not isinstance(md5, str) or not re.fullmatch(r'[A-Fa-f0-9]{32}', md5)):
            raise IdentityUnavailable('在线解析音频校验值无效，未下载。')
        level = data.get('level') if data.get('level') in AUDIO_LEVELS else ''
        check_cancel(cancel)
        return {'url': url, 'size': size, 'md5': md5.lower() if md5 else None,
                'br': bitrate, 'level': level, 'metadata': metadata,
                'song_id': identifier, 'source_label': self.source_label}

    def lyrics(self, song_id):
        data = self._call('/api/getSongLyric', {'id': _identifier(song_id)})
        if not isinstance(data, dict):
            return ''
        lyric = data.get('lrc')
        # 支持站点字符串与上游 {lyric: ...}，均不保存其他响应字段。
        if isinstance(lyric, dict):
            lyric = lyric.get('lyric')
        return _text(lyric)
