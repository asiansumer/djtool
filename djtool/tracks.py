"""只读音频信息：不改标签、不转码，也不移动用户的源文件。"""

import math
from pathlib import Path
from mutagen import MutagenError
from mutagen import File as MutagenFile
from mutagen.mp3 import EasyMP3, MP3
from djtool.metadata import EXTRA_FIELDS, extract_extra
import json
import struct


def build_track_record(file_path: str) -> dict[str, str]:
    """保留第一轮路径函数；完整音频信息由 read_mp3 负责。"""
    document = Path(file_path).expanduser().resolve()
    if not document.is_file():
        raise FileNotFoundError("文件不存在或不是文件")
    return {"name": document.name, "path": str(document)}


def format_duration(seconds: float) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{remainder:02d}"


def first_tag(tags, key: str, fallback: str = "") -> str:
    values = tags.get(key, [])
    if not values:
        return fallback
    return str(values[0]).strip() or fallback


def positive_bpm(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 20 <= number <= 400 else None
    except (ValueError, TypeError):
        return None


def effective_bpm(track):
    """导出保留旧人工记录或文件标签；已停用的算法估计不再作为当前值。"""
    for key, label in (("bpm_manual", "已确认"), ("bpm_tag", "标签")):
        if track.get(key) is not None:
            return track[key], label
    return None, '无标签'


def read_mp3(path: Path, original_name: str | None = None):
    """确认真实 MPEG Layer III 音频；返回标签及可选的原有封面。"""
    try:
        audio = EasyMP3(path)
        if audio.info.layer != 3 or not math.isfinite(audio.info.length) or audio.info.length <= 0:
            raise ValueError("不是可读取的 MP3 音频")
        result = {
            "title": first_tag(audio, "title", Path(original_name or path.name).stem),
            "artist": ' / '.join(audio.get('artist', [])) or '未知艺人',
            "album": first_tag(audio, "album"),
            "genre": first_tag(audio, "genre"),
            "musical_key": first_tag(audio, "initialkey"),
            "duration": float(audio.info.length),
            "bpm_tag": positive_bpm(first_tag(audio, "bpm")),
        }
        cover = None
        tagged = MP3(path)
        if tagged.tags:
            for artwork in tagged.tags.getall("APIC"):
                if artwork.mime in ("image/jpeg", "image/png") and len(artwork.data) <= 20_000_000:
                    cover = (artwork.data, ".png" if artwork.mime == "image/png" else ".jpg")
                    break
        result.update(extract_extra(path))
        return result, cover
    except MutagenError as error:
        raise ValueError("无法读取 MP3，文件可能损坏或实际格式不符") from error


def _container_text(path, kind):
    """补读 WAV INFO / AIFF 文本块；限制文本长度并跳过音频块，始终以 rb 打开。"""
    aliases = ({b'INAM': 'title', b'IART': 'artist', b'IPRD': 'album', b'IGNR': 'genre',
                b'ICRD': 'release_date', b'ICMT': 'comment', b'ICOP': 'copyright'} if kind == 'WAVE'
               else {b'NAME': 'title', b'AUTH': 'artist', b'ANNO': 'comment', b'(c) ': 'copyright'})
    result = {}
    order = '<I' if kind == 'WAVE' else '>I'
    file_size = path.stat().st_size
    with path.open('rb') as stream:
        stream.seek(12)
        for _ in range(65536):
            header = stream.read(8)
            if len(header) != 8:
                break
            key, size = header[:4], struct.unpack(order, header[4:])[0]
            if size > file_size - stream.tell():
                break
            if size <= 1024 * 1024 and (key in aliases or (kind == 'WAVE' and key == b'LIST')):
                data = stream.read(size)
                if kind == 'WAVE' and key == b'LIST' and data[:4] == b'INFO':
                    offset = 4
                    while offset + 8 <= len(data):
                        tag = data[offset:offset + 4]
                        length = struct.unpack('<I', data[offset + 4:offset + 8])[0]
                        offset += 8
                        if length > len(data) - offset:
                            break
                        if tag in aliases:
                            result[aliases[tag]] = data[offset:offset + length].rstrip(b'\0').decode('utf-8', errors='replace').strip()
                        offset += length + length % 2
                elif key in aliases:
                    result[aliases[key]] = data.rstrip(b'\0').decode('utf-8', errors='replace').strip()
            else:
                stream.seek(size, 1)
            stream.seek(size % 2, 1)
    return result


def read_audio(path: Path, original_name: str | None = None):
    """只读现有 Serato 音乐；保留原格式、标签和播放路径，不作任何保存调用。"""
    path = Path(path)
    if path.suffix.lower() == '.mp3':
        return read_mp3(path, original_name)
    expected = {'.wav': 'WAVE', '.flac': 'FLAC', '.aif': 'AIFF', '.aiff': 'AIFF',
                '.m4a': 'MP4', '.mp4': 'MP4'}
    if path.suffix.lower() not in expected:
        raise ValueError('无法读取这个音频格式')
    try:
        audio = MutagenFile(path)
        if audio is None or type(audio).__name__ != expected[path.suffix.lower()] or not math.isfinite(audio.info.length) or audio.info.length <= 0:
            raise ValueError('不是可读取的音频，文件可能损坏或实际格式不符')
        tags = audio.tags or {}
        id3 = hasattr(tags, 'getall')
        mp4 = type(audio).__name__ == 'MP4'
        container_text = _container_text(path, type(audio).__name__) if type(audio).__name__ in ('WAVE', 'AIFF') else {}
        aliases = {
            'title': ('title', 'TIT2', '\xa9nam'), 'artist': ('artist', 'TPE1', '\xa9ART'),
            'album': ('album', 'TALB', '\xa9alb'), 'genre': ('genre', 'TCON', '\xa9gen'),
            'musical_key': ('initialkey', 'TKEY', '----:com.apple.iTunes:initialkey'),
            'bpm_tag': ('bpm', 'TBPM', 'tmpo'),
            'album_artist': ('albumartist', 'TPE2', 'aART'),
            'composer': ('composer', 'TCOM', '\xa9wrt'),
            'release_date': ('date', 'TDRC', '\xa9day'),
            'track_number': ('tracknumber', 'TRCK', 'trkn'),
            'disc_number': ('discnumber', 'TPOS', 'disk'),
            'publisher': ('organization', 'TPUB', '----:com.apple.iTunes:LABEL'),
            'copyright': ('copyright', 'TCOP', 'cprt'),
            'isrc': ('isrc', 'TSRC', '----:com.apple.iTunes:ISRC'),
            'comment': ('comment', 'COMM', '\xa9cmt'), 'lyrics': ('lyrics', 'USLT', '\xa9lyr')}

        def text(field):
            keys = aliases[field]
            if id3:
                values = [getattr(frame, 'text', '') for frame in tags.getall(keys[1])]
            else:
                values = tags.get(keys[2] if mp4 else keys[0], [])
            if isinstance(values, (str, bytes)):
                values = [values]
            output = []
            for value in values:
                if isinstance(value, tuple):
                    output.append('/'.join(str(n) for n in value if n))
                elif isinstance(value, list):
                    output.append(' / '.join(map(str, value)))
                elif isinstance(value, bytes):
                    output.append(value.decode('utf-8', errors='replace'))
                else:
                    output.append(str(value).strip())
            return ' / '.join(output) or container_text.get(field, '')

        result = {field: text(field) for field in ('title', 'artist', 'album', 'genre', 'musical_key', *EXTRA_FIELDS)}
        result.update(title=result['title'] or Path(original_name or path.name).stem,
                      artist=result['artist'] or '未知艺人', duration=float(audio.info.length),
                      bpm_tag=positive_bpm(text('bpm_tag')))
        raw = {}
        for key, value in tags.items():
            if hasattr(value, 'data'):
                raw[key] = {'binary_bytes': len(value.data), 'mime': getattr(value, 'mime', None)}
            elif hasattr(value, 'text'):
                raw[key] = str(value.text)
            elif isinstance(value, list):
                raw[key] = [{'binary_bytes': len(v)} if isinstance(v, bytes) else str(v) for v in value]
            else:
                raw[key] = str(value)
        if container_text:
            raw['container_text'] = container_text
        result.update(bitrate=getattr(audio.info, 'bitrate', None),
                      sample_rate=getattr(audio.info, 'sample_rate', None),
                      channels=getattr(audio.info, 'channels', None), file_size=path.stat().st_size,
                      codec={'WAVE': 'WAV', 'AIFF': 'AIFF', 'FLAC': 'FLAC', 'MP4': getattr(audio.info, 'codec', 'M4A')}.get(type(audio).__name__),
                      tags_json=json.dumps(raw, ensure_ascii=False))
        cover = None
        pictures = tags.getall('APIC') if id3 else getattr(audio, 'pictures', [])
        for artwork in pictures:
            if getattr(artwork, 'mime', '') in ('image/jpeg', 'image/png') and len(artwork.data) <= 20_000_000:
                cover = (artwork.data, '.png' if artwork.mime == 'image/png' else '.jpg')
                break
        if mp4:
            from mutagen.mp4 import MP4Cover
            for artwork in tags.get('covr', []):
                if len(artwork) <= 20_000_000 and artwork.imageformat in (MP4Cover.FORMAT_JPEG, MP4Cover.FORMAT_PNG):
                    cover = (bytes(artwork), '.png' if artwork.imageformat == MP4Cover.FORMAT_PNG else '.jpg')
                    break
        return result, cover
    except MutagenError as error:
        raise ValueError('无法读取音频，文件可能损坏或实际格式不符') from error
