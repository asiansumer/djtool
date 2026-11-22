"""本地 NCM 容器提取。格式依据用户的 NCM-Swift；不联网补封面、不碰源文件。"""
import base64
import json
from pathlib import Path
import struct
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
from Cryptodome.Util.strxor import strxor
from mutagen.flac import FLAC
from djtool.audio_tasks import AudioError, check_cancel, transcode_mp3, verify_audio
from djtool.tracks import read_mp3

CORE_KEY = bytes.fromhex('687a4852416d736f356b496e62617857')
META_KEY = bytes.fromhex('2331346c6a6b5f215c5d2630553c2728')
MAX_SOURCE = 1024 * 1024 * 1024


def read_exact(stream, count):
    data = stream.read(count)
    if len(data) != count:
        raise AudioError('NCM 文件不完整，请等待官方下载完成后再导入')
    return data


def uint32(stream):
    return struct.unpack('<I', read_exact(stream, 4))[0]


def read_block(stream, limit):
    length = uint32(stream)
    if length > limit:
        raise AudioError('NCM 数据段长度异常，未导入')
    return read_exact(stream, length)


def key_stream(key):
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (box[i] + j + key[i % len(key)]) & 255
        box[i], box[j] = box[j], box[i]
    return bytes(box[(box[i] + box[(i + box[i]) & 255]) & 255]
                 for i in list(range(1, 256)) + [0])


def _metadata_block(incoming):
    raw = bytes(b ^ 0x63 for b in read_block(incoming, 4_000_000))
    if not raw:
        return {}
    if not raw.startswith(b"163 key(Don't modify):"):
        raise AudioError('NCM 元数据标记无效')
    encrypted = base64.b64decode(raw[22:], validate=True)
    plain = unpad(AES.new(META_KEY, AES.MODE_ECB).decrypt(encrypted), 16)
    kind, payload = plain.split(b':', 1)
    meta = json.loads(payload)
    if kind == b'dj' and isinstance(meta, dict):
        meta = meta.get('mainMusic', {})
    if not isinstance(meta, dict):
        raise AudioError('NCM 元数据格式无效')
    return meta


def music_id(meta):
    """只接受容器内的数字歌曲 ID，绝不以文件名猜测版本。"""
    value = str(meta.get('musicId', ''))
    return str(int(value)) if value.isascii() and value.isdigit() and 0 < len(value) <= 20 and int(value) > 0 else None


def read_metadata(source):
    """扫描下载目录时只读有界头部；是否完整仍由导入时的全段解码决定。"""
    try:
        with Path(source).open('rb') as incoming:
            if read_exact(incoming, 8) != b'CTENFDAM':
                raise AudioError('不是有效的 NCM 文件')
            read_exact(incoming, 2)
            read_block(incoming, 65536)
            return _metadata_block(incoming)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, AudioError):
            raise
        raise AudioError('NCM 元数据损坏或版本暂不支持') from error


def extract(source, destination, cancel=lambda: False):
    """有界流式提取；元数据声称的 format 不参与实际音频格式判断。"""
    check_cancel(cancel)
    if source.stat().st_size > MAX_SOURCE:
        raise AudioError('NCM 文件超过 1 GB，暂不支持')
    try:
        with source.open('rb') as incoming:
            if read_exact(incoming, 8) != b'CTENFDAM':
                raise AudioError('不是有效的 NCM 文件')
            read_exact(incoming, 2)
            encrypted_key = bytes(b ^ 0x64 for b in read_block(incoming, 65536))
            plain_key = unpad(AES.new(CORE_KEY, AES.MODE_ECB).decrypt(encrypted_key), 16)
            if not plain_key.startswith(b'neteasecloudmusic') or len(plain_key) <= 17:
                raise AudioError('NCM 密钥数据损坏')
            stream_key = key_stream(plain_key[17:])
            meta = _metadata_block(incoming)
            read_exact(incoming, 5)  # CRC 与保留位；音频完整性另做完整解码验证。
            cover_space, cover_length = uint32(incoming), uint32(incoming)
            if not 0 <= cover_length <= cover_space <= 20_000_000:
                raise AudioError('NCM 封面数据长度异常')
            artwork = read_exact(incoming, cover_space)[:cover_length]
            # 每 256 字节重复；块长是其整数倍，避免跨块后错位。
            mask = stream_key * 4096
            with destination.open('xb') as outgoing:
                while chunk := incoming.read(len(mask)):
                    check_cancel(cancel)
                    outgoing.write(strxor(chunk, mask[:len(chunk)]))
        return meta, artwork
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, AudioError):
            raise
        raise AudioError('NCM 数据损坏或版本暂不支持') from error


def add_metadata(path, meta, artwork, flac=None):
    """只补充临时副本缺失的标签，已有 MP3 自定义标签原样保留。"""
    from mutagen.mp3 import MP3
    from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TCON, TDRC, TRCK, TPOS, TCOM, TSRC, TCOP, TBPM, TKEY, USLT, APIC
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    artists = meta.get('artist') or []
    if isinstance(artists, str):
        artists = [artists]
    artists = [a[0] if isinstance(a, list) and a else a for a in artists]
    values = {'title': meta.get('musicName'), 'artist': [a for a in artists if isinstance(a, str)],
              'album': meta.get('album')}
    # FLAC 的文字字段与封面转换为 ID3；全部源字段另存来源记录。
    if flac:
        values.update({key: value for key, value in flac.items() if value})
    frames = [(TIT2,'title'),(TPE1,'artist'),(TALB,'album'),(TPE2,'albumartist'),
              (TCON,'genre'),(TDRC,'date'),(TRCK,'tracknumber'),(TPOS,'discnumber'),
              (TCOM,'composer'),(TSRC,'isrc'),(TCOP,'copyright'),(TBPM,'bpm'),(TKEY,'initialkey')]
    for frame, key in frames:
        value = values.get(key)
        if value and (flac is not None or not tags.getall(frame.__name__)):
            tags.add(frame(encoding=3, text=value))
    lyrics = values.get('lyrics')
    if lyrics and not tags.getall('USLT'):
        tags.add(USLT(encoding=3, lang='eng', desc='', text='\n'.join(lyrics) if isinstance(lyrics,list) else lyrics))
    if artwork and not tags.getall('APIC'):
        mime = 'image/png' if artwork.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if artwork.startswith(b'\xff\xd8\xff') else None
        if mime:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=artwork))
    audio.save()  # 不重编码 MP3 音频帧。


def convert(source, folder, cancel=lambda: False):
    raw = folder / 'extracted.audio'
    output = folder / 'converted.mp3'
    meta, artwork = extract(source, raw, cancel)
    flac_tags = None
    with raw.open('rb') as stream:
        signature = stream.read(4)
    if signature == b'fLaC':
        original = FLAC(raw)
        duration = original.info.length
        flac_tags = dict(original.tags or {})
        if not artwork and original.pictures:
            artwork = original.pictures[0].data[:20_000_001]
        source_format, treatment = 'FLAC', 'FLAC 转 MP3 · VBR 高质量'
        transcode_mp3(raw, output, cancel)
    else:
        original, _ = read_mp3(raw)
        duration = original['duration']
        source_format, treatment = 'MP3', '提取 MP3 · 未重编码音频'
        raw.rename(output)
    check_cancel(cancel)
    add_metadata(output, meta, artwork if len(artwork) <= 20_000_000 else b'', flac_tags)
    verify_audio(output, cancel)
    actual = read_mp3(output)[0]['duration']
    if abs(actual - duration) > max(.25, duration * .005):
        raise AudioError('转换前后时长不符，未导入')
    expected = meta.get('duration')
    if isinstance(expected, (float, int)) and expected > 0 and abs(actual - expected / 1000) > max(2, expected / 1000 * .02):
        raise AudioError('NCM 音频时长与元数据不符，可能未下载完整')
    return output, {'source_format': source_format, 'conversion': treatment,
                    'ncm_metadata': meta, 'source_flac_tags': flac_tags,
                    'source_duration': duration, 'output_duration': actual}
