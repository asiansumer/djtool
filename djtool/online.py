"""链接收歌：认链接、读列表、下载并校验一首歌。不把试听片段当完整歌曲。"""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
from djtool.metadata import embed_online
from djtool.tracks import read_mp3
from djtool.netease_common import AuthorizationError, MusicUnavailable, safe_media_url
from djtool.network_errors import NetworkFailure, network_failure

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36'


# SoundCloud 上不是用户名的一级路径；这些页面没有可读的歌曲列表。
SC_RESERVED = ('discover', 'charts', 'search', 'stations', 'upload', 'settings', 'messages', 'notifications',
               'feed', 'stream', 'people', 'pages', 'popular', 'tags', 'mobile', 'jobs', 'imprint',
               'terms-of-use', 'signin', 'logout', 'library', 'charts', 'pro', 'go', 'for', 'player')
SC_LISTS = {'tracks': '的上传', 'likes': '的点赞', 'reposts': '的转发'}
SC_PAGES = (*SC_LISTS, 'albums', 'sets', 'popular-tracks', 'spotlight', 'comments', 'followers', 'following')
SC_LIST_LIMIT = 500   # 点赞页可能有几千首；只读最新的这么多


class OnlineError(Exception):
    pass


class Cancelled(OnlineError):
    pass


def check_cancel(cancel):
    if cancel():
        raise Cancelled('已停止；完成的歌曲会保留')


def ffmpeg_path():
    from djtool.audio_tasks import ffmpeg_path as locate, AudioError
    try:
        return locate()
    except AudioError as error:
        raise OnlineError(str(error)) from error


def normalize_link(text):
    match = re.search(r'https?://[^\s<>\[\]"()]+', text)
    if not match:
        raise OnlineError('请粘贴网易云或 SoundCloud 的歌曲、歌单链接')
    url = match.group(0).rstrip('。，,')
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise OnlineError('链接格式不受支持')
    if host in ('163cn.tv', 'on.soundcloud.com', 'm.soundcloud.com'):
        # 只跟随已知分享域名，最终仍须通过下面的平台白名单。
        response = requests.get(url, timeout=20, headers={'User-Agent': USER_AGENT})
        response.raise_for_status()
        url = response.url
        parsed, host = urlparse(url), urlparse(url).hostname
    if host in ('music.163.com', 'y.music.163.com'):
        path = parsed.path
        query = parsed.query
        if parsed.fragment.startswith('/'):
            fragment = urlparse(parsed.fragment)
            path, query = fragment.path, fragment.query
        kind = path.rstrip('/').split('/')[-1]
        identifier = parse_qs(query).get('id', [''])[0]
        if kind not in ('song', 'playlist') or not identifier.isdigit():
            raise OnlineError('网易云链接需要包含歌曲或歌单 ID')
        return 'netease', f'https://music.163.com/{kind}?id={identifier}', kind, identifier
    if host in ('soundcloud.com', 'www.soundcloud.com'):
        parts = [part for part in parsed.path.strip('/').split('/') if part]
        if parts and parts[0] == 'you':
            raise OnlineError('「你的点赞」要用公开地址才能读：打开自己的主页，复制 soundcloud.com/你的用户名/likes')
        if not parts or parts[0] in SC_RESERVED or len(parts) > 4:
            raise OnlineError('这个 SoundCloud 页面读不了。打开具体的歌曲、歌单，或某个人的主页、点赞页，再复制链接')
        base = 'https://soundcloud.com/'
        if len(parts) == 1:
            # 某个人的主页：读他自己上传的歌。
            return 'soundcloud', base + parts[0] + '/tracks', 'playlist', parts[0] + '/tracks'
        if len(parts) == 2 and parts[1] in SC_PAGES:
            if parts[1] not in SC_LISTS:
                raise OnlineError('这个 SoundCloud 页面读不了。可以读某个人的上传（/tracks）、点赞（/likes）、转发（/reposts）或具体歌单')
            return 'soundcloud', base + '/'.join(parts), 'playlist', '/'.join(parts)
        if parts[1] == 'sets':
            if len(parts) < 3:
                raise OnlineError('请打开具体的 SoundCloud 歌单后复制链接')
            return 'soundcloud', base + '/'.join(parts), 'playlist', '/'.join(parts[:3])
        if len(parts) > 3:
            raise OnlineError('这个 SoundCloud 页面读不了。打开具体的歌曲或歌单，再复制链接')
        secret = parse_qs(parsed.query).get('secret_token', [''])[0]
        tail = f'?secret_token={secret}' if secret else ''
        return 'soundcloud', base + '/'.join(parts) + tail, 'song', None
    raise OnlineError('当前只接受网易云和 SoundCloud 链接')


class QuietLogger:
    def debug(self, message):
        pass
    def warning(self, message):
        pass
    def error(self, message):
        pass


def sc_options():
    return {'quiet': True, 'no_warnings': True, 'logger': QuietLogger(), 'noplaylist': True,
            'socket_timeout': 20, 'retries': 2, 'fragment_retries': 2, 'cachedir': False,
            'format': 'bestaudio[format_id!=download][format_id!*=preview]',
            'ffmpeg_location': ffmpeg_path()}


SC_FIELDS = ('id', 'title', 'uploader', 'uploader_id', 'uploader_url', 'artists', 'album', 'description',
             'duration', 'genres', 'tags', 'license', 'release_date', 'upload_date', 'release_timestamp',
             'timestamp', 'webpage_url', 'thumbnail', 'track_number', 'format_id', 'abr', 'ext', 'acodec')


def soundcloud_track(info, url):
    """把 yt-dlp 的曲目信息整理成收歌任务。只存元数据，不存短期签名的媒体地址和 Cookie。"""
    raw = {key: info.get(key) for key in SC_FIELDS if info.get(key) is not None}
    artists = info.get('artists') or [info.get('uploader') or '未知艺人']
    date = info.get('release_date') or info.get('upload_date') or ''
    date = f'{date[:4]}-{date[4:6]}-{date[6:8]}' if len(date) == 8 else date
    return {'provider': 'soundcloud', 'remote_id': str(info['id']), 'url': url,
            'title': info.get('title') or '', 'artist': ' / '.join(artists),
            'album': info.get('album') or '', 'genre': ' / '.join(info.get('genres') or []),
            'description': info.get('description') or '', 'duration': info.get('duration') or 0,
            'release_date': date, 'thumbnail': info.get('thumbnail') or '', 'source_metadata': raw}


def _soundcloud_details(downloader, identifiers, cancel):
    """歌单和点赞页只给出歌曲 ID 和地址；这里按 50 首一批补上标题、作者、时长和封面。

    用的是 SoundCloud 网页自己的曲目接口，一批一次请求；拿不到时返回已有的部分，
    列表照样能用（标题从地址里取），下载时还会再补一次完整信息。
    """
    result = {}
    try:
        extractor = downloader.get_info_extractor('Soundcloud')
        extractor.initialize()
        for offset in range(0, len(identifiers), 50):
            check_cancel(cancel)
            chunk = identifiers[offset:offset + 50]
            tracks = extractor._call_api(extractor._API_V2_BASE + 'tracks', chunk[0], 'Downloading track details',
                                         query={'ids': ','.join(chunk)}, headers=extractor._HEADERS)
            for track in tracks or []:
                user = track.get('user') or {}
                artwork = track.get('artwork_url') or user.get('avatar_url') or ''
                artist = ((track.get('publisher_metadata') or {}).get('artist') or '').strip()
                created = (track.get('created_at') or '')[:10].replace('-', '')
                result[str(track['id'])] = {
                    'id': str(track['id']), 'title': track.get('title'), 'uploader': user.get('username'),
                    'uploader_url': user.get('permalink_url'), 'artists': [artist] if artist else None,
                    'description': track.get('description'), 'duration': (track.get('duration') or 0) / 1000,
                    'genres': [track['genre']] if track.get('genre') else None, 'license': track.get('license'),
                    'upload_date': created if len(created) == 8 else None,
                    'webpage_url': track.get('permalink_url'),
                    # large 是 100px 的小图；换成 500px 的版本当封面。
                    'thumbnail': artwork.replace('-large.', '-t500x500.')}
    except Cancelled:
        raise
    except Exception:
        pass
    return result


def soundcloud_listing(url, kind='song', cancel=lambda: False):
    if kind != 'playlist':
        with yt_dlp.YoutubeDL(sc_options()) as downloader:
            info = downloader.extract_info(url, download=False)
        return 'SoundCloud 单曲', [soundcloud_track(info, url)]
    options = {**sc_options(), 'noplaylist': False, 'extract_flat': 'in_playlist', 'playlistend': SC_LIST_LIMIT}
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        entries = {}
        for entry in info.get('entries') or []:
            # 点赞和转发里也会有整张歌单，这里只收单曲。
            if entry and entry.get('id') and entry.get('url') and entry.get('ie_key') == 'Soundcloud':
                entries.setdefault(str(entry['id']), entry)
        check_cancel(cancel)
        details = _soundcloud_details(downloader, list(entries), cancel)
    tracks = []
    for identifier, entry in entries.items():
        detail = details.get(identifier) or {}
        page = detail.get('webpage_url') or entry['url']
        slugs = urlparse(page).path.strip('/').split('/')
        fallback = {'id': identifier, 'title': entry.get('title') or slugs[-1].replace('-', ' '),
                    'uploader': slugs[0] if len(slugs) > 1 and 'api' not in (urlparse(page).hostname or '') else None}
        merged = {**fallback, **{key: value for key, value in detail.items() if value not in (None, '')}}
        tracks.append(soundcloud_track(merged, page))
    if not tracks:
        raise OnlineError('这个 SoundCloud 页面里没有读到歌曲；确认它是公开的')
    title = info.get('title') or 'SoundCloud 歌单'
    match = re.fullmatch(r'(.*) \((Tracks|Likes|Reposts)\)', title)
    if match:
        title = match.group(1) + SC_LISTS[match.group(2).lower()]
    return title, tracks


def fetch_listing(text, client=None, cancel=lambda: False, library=None):
    provider, url, kind, identifier = normalize_link(text)
    check_cancel(cancel)
    if provider == 'netease':
        if client is None:
            from djtool.netease_backend import create_music_client
            client = create_music_client(library)
        return client.listing(kind, identifier, cancel)
    return soundcloud_listing(url, kind, cancel)


def transfer(url, destination, progress, cancel, limit=1024*1024*1024, url_validator=None):
    try:
        return _transfer(url, destination, progress, cancel, limit, url_validator)
    except requests.RequestException as error:
        raise network_failure(error, stage='media') from error


def _transfer(url, destination, progress, cancel, limit, url_validator):
    if url.startswith('http://'):
        url = 'https://' + url[7:]
    # 网易云媒体只允许官方 CDN；重定向逐次核对，不能让签名地址转到任意主机。
    from urllib.parse import urljoin
    for redirect in range(6):
        check_cancel(cancel)
        if url_validator:
            url = url_validator(url)
        response = requests.get(url, stream=True, timeout=(10, 25),
                                headers={'User-Agent':USER_AGENT}, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get('Location')
        response.close()
        if not location or redirect == 5:
            raise OnlineError('媒体地址重定向异常，未下载')
        url = urljoin(url, location)
    with response:
        response.raise_for_status()
        total = int(response.headers.get('Content-Length', 0))
        if total > limit:
            raise OnlineError('返回的文件超过大小限制')
        count = 0
        with Path(destination).open('xb') as stream:
            for chunk in response.iter_content(128*1024):
                check_cancel(cancel)
                count += len(chunk)
                if count > limit:
                    raise OnlineError('返回的文件超过大小限制')
                stream.write(chunk)
                progress(f'下载中 · {count/1024/1024:.1f} MB', min(90,int(count/total*90)) if total else 0)
        if total and count != total:
            raise OnlineError('文件未下载完整，请重试')
        return response.headers.get('Content-Type','').split(';')[0]


def to_mp3(source, destination, cancel):
    # 实際解码而非改扩展名。转码失败的文件不会入库。
    with tempfile.TemporaryFile() as errors:
        command = [ffmpeg_path(), '-v', 'error', '-nostdin', '-i', str(source), '-map','0:a:0',
                   '-vn','-c:a','libmp3lame','-q:a','0',str(destination)]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
        started = time.monotonic()
        while process.poll() is None:
            if cancel() or time.monotonic()-started > 900:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                check_cancel(cancel)
                raise OnlineError('转换超时，请重试')
            time.sleep(.1)
        if process.returncode:
            raise OnlineError('音频解码或 MP3 转换失败；未导入不完整文件')


def validate_duration(path, expected):
    actual = read_mp3(path)[0]['duration']
    if expected and (type(expected) not in (int, float) or not math.isfinite(expected) or expected <= 0):
        raise OnlineError('歌曲参考时长无效，无法核对完整音频')
    if expected and abs(actual-expected) > max(2, expected*.02):
        raise OnlineError(f'音频时长不符（得到 {actual:.0f} 秒，应为 {expected:.0f} 秒），未入库')
    return actual


def download_track(info, library, progress=lambda *args: None, cancel=lambda: False, music_client=None):
    info = dict(info)
    job = library.enqueue(info)
    if job['state'] == 'missing_file':
        raise OnlineError('已下载歌曲的播放文件丢失；请先恢复原曲库文件，再继续收歌')
    if job['track_id'] and Path(library.track(job['track_id'])['path']).is_file():
        return job['track_id'], False
    library.set_job_state(job['id'], 'downloading')
    try:
        staging = library.root / '.djtool' / 'downloads'
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as temporary:
            folder = Path(temporary)
            output = folder / 'audio.mp3'
            check_cancel(cancel)
            progress('正在解析可用音频', 0)
            if info['provider'] == 'soundcloud':
                def hook(status):
                    check_cancel(cancel)
                    if status['status'] == 'downloading':
                        total = status.get('total_bytes') or status.get('total_bytes_estimate') or 0
                        size = status.get('downloaded_bytes',0)
                        progress(f'下载中 · {size/1024/1024:.1f} MB', min(85,int(size/total*85)) if total else 0)
                options = {**sc_options(), 'outtmpl': str(folder/'stream.%(ext)s'),
                           'progress_hooks':[hook]}
                with yt_dlp.YoutubeDL(options) as downloader:
                    extracted = downloader.extract_info(info['url'], download=True)
                if str(extracted['id']) != str(info['remote_id']):
                    raise OnlineError('链接对应的歌曲发生变化，请重新解析')
                # 从歌单或点赞页进来的任务只有简略信息；这里用完整信息补齐，标签才写得全。
                for key, value in soundcloud_track(extracted, info['url']).items():
                    if key not in ('provider', 'remote_id', 'url', 'source_metadata') and value not in ('', None, 0):
                        info[key] = value
                source = folder / ('stream.' + extracted['ext'])
                info['source_quality'] = f"{extracted.get('format_id','')} · {extracted.get('abr') or '?'} kbps"
                if extracted['ext'] == 'mp3':
                    source.rename(output)
                else:
                    progress('正在转换为 MP3', 90)
                    to_mp3(source, output, cancel)
            else:
                from djtool.netease_backend import create_music_client
                client = music_client if music_client is not None else create_music_client(library)
                progress('正在核对网易云音源与完整时长', 0)
                data = client.audio(info, cancel)
                # API 临时音频 URL 留在 data 内存；持久化的是白名单元数据。
                for key, value in data['metadata'].items():
                    if value not in ('', None) or key == 'availability_reason' or (
                            key == 'release_date' and getattr(client, 'backend', None) == 'site'):
                        # 旧站点适配误把响应生成时间写作发行日期；新详情留空也要清掉它。
                        info[key] = value
                backend = getattr(client, 'backend', None)
                info['acquisition_backend'] = backend if isinstance(backend, str) and backend in ('site', 'local') else 'official'
                source = folder / 'download.audio'
                if type(info.get('duration')) not in (int, float) or not math.isfinite(info['duration']) or info['duration'] <= 0:
                    raise OnlineError('网易云歌曲缺少有效时长，未下载')
                transfer(data['url'], source, progress, cancel, url_validator=safe_media_url)
                if data.get('size') and source.stat().st_size != data['size']:
                    raise OnlineError('音频文件大小与网易云记录不符，未入库，请重试。')
                if data.get('md5'):
                    with source.open('rb') as downloaded:
                        digest = hashlib.file_digest(downloaded, 'md5').hexdigest()
                    if digest != data['md5'].lower():
                        raise OnlineError('音频校验与网易云记录不符，未入库，请重试。')
                bitrate = int(data.get('br') or 0)//1000
                info['source_quality'] = (data.get('source_label') or '网易云官方音源') + (f' · {bitrate} kbps' if bitrate else '')
                if data.get('level'):
                    info['source_quality'] += ' · ' + data['level']
                try:
                    read_mp3(source)
                    source.rename(output)
                except ValueError:
                    progress('正在转换为 MP3', 90)
                    to_mp3(source, output, cancel)
                try:
                    info['lyrics'] = client.lyrics(data['song_id'])
                except (AuthorizationError, requests.RequestException, NetworkFailure):
                    info['metadata_notice'] = '歌词暂未取得'
            check_cancel(cancel)
            validate_duration(output, info.get('duration'))
            from djtool.audio_tasks import verify_audio
            verify_audio(output, cancel)
            progress('正在保存封面与标签', 95)
            cover = None
            warning = info.get('metadata_notice', '')
            if info.get('thumbnail'):
                try:
                    cover_file = folder/'cover'
                    transfer(info['thumbnail'], cover_file, lambda *_: None, cancel, limit=20_000_000,
                             **({'url_validator': safe_media_url} if info['provider'] == 'netease' else {}))
                    image = cover_file.read_bytes()
                    # CDN 可能返回 image/jpg / application/octet-stream，按实际内容识别。
                    mime = ('image/jpeg' if image.startswith(b'\xff\xd8\xff') else
                            'image/png' if image.startswith(b'\x89PNG\r\n\x1a\n') else None)
                    if mime:
                        cover = (mime, image)
                    else:
                        warning = '封面格式未支持'
                except (AuthorizationError, OnlineError, requests.RequestException, NetworkFailure):
                    check_cancel(cancel)
                    warning = '封面暂未取得'
            embed_online(output, info, cover)
            check_cancel(cancel)
            # 使用可辨识文件名，平台 ID 去重独立于标签或标题的变化。
            name = re.sub(r'[/\\:\x00-\x1f]', '_', info['title'])[:70].strip('.') or info['remote_id']
            named = folder / f'{name}.mp3'
            output.rename(named)
            identifier, added = library.import_file(named)
            with library.connection() as db:
                db.execute('UPDATE online_jobs SET metadata_json=? WHERE id=?',
                           (json.dumps(info,ensure_ascii=False),job['id']))
            library.set_job_state(job['id'], 'complete', '已入库' + (f' · {warning}' if warning else ''), identifier)
            progress('已入库', 100)
            return identifier, added
    except Exception as error:
        state = ('cancelled' if cancel() or isinstance(error, Cancelled) else
                 'unavailable' if isinstance(error, MusicUnavailable) else
                 'waiting_auth' if isinstance(error, AuthorizationError) and error.code in (301, 401) else 'failed')
        library.set_job_state(job['id'], state, friendly_error(error))
        raise


def friendly_error(error):
    if isinstance(error, NetworkFailure):
        return str(error)
    if isinstance(error, (OnlineError, AuthorizationError)):
        return str(error)
    if isinstance(error, requests.RequestException):
        return '网络请求失败或超时，请检查连接后重试'
    text = str(error)
    if isinstance(error, yt_dlp.utils.DownloadError):
        if 'DRM' in text or 'No video formats' in text or 'Requested format' in text:
            return '没有可获取的完整音频流（可能受账号、地区或加密限制）'
        if '429' in text:
            return '平台暂时限制请求，请稍后重试'
        return 'SoundCloud 解析或下载失败，请检查曲目可播放性后重试'
    return f'任务未完成：{type(error).__name__}；请重试或检查应用日志'
