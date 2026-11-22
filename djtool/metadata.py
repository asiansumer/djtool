"""完整保留可读标签；原文件副本仍包含未解释的二进制 DJ 标签。"""
import json
from mutagen.mp3 import MP3

EXTRA_FIELDS = ('album_artist', 'composer', 'release_date', 'track_number', 'disc_number',
                'publisher', 'copyright', 'isrc', 'comment', 'lyrics')
FRAMES = dict(zip(EXTRA_FIELDS, ('TPE2', 'TCOM', 'TDRC', 'TRCK', 'TPOS', 'TPUB', 'TCOP', 'TSRC', 'COMM', 'USLT')))


def extract_extra(path):
    audio = MP3(path)
    tags = audio.tags or {}
    result = {}
    for field, frame in FRAMES.items():
        values = audio.tags.getall(frame) if audio.tags else []
        result[field] = '\n'.join('/'.join(map(str, value.text)) if isinstance(value.text, list)
                                 else str(value.text) for value in values)
    # 保留全部文本帧，多个艺人、评论与自定义 TXXX 不会被第一项截断。
    raw = {}
    for key, value in tags.items():
        if hasattr(value, 'data'):
            raw[key] = {'binary_bytes': len(value.data), 'mime': getattr(value, 'mime', None),
                        'description': getattr(value, 'desc', '')}
        elif hasattr(value, 'text'):
            raw[key] = list(map(str, value.text)) if isinstance(value.text, list) else str(value.text)
        else:
            raw[key] = str(value)
    result.update(bitrate=audio.info.bitrate, sample_rate=audio.info.sample_rate,
                  channels=audio.info.channels, file_size=path.stat().st_size,
                  codec='MP3', tags_json=json.dumps(raw, ensure_ascii=False))
    return result


def embed_online(path, info, cover=None):
    """只为刚下载的临时 MP3 写标签，绝不改写用户导入的源文件。"""
    from mutagen.id3 import (TIT2, TPE1, TALB, TPE2, TCON, TDRC, TRCK,
                            TSRC, COMM, USLT, WOAS, APIC, TXXX)
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for frame, key in [(TIT2,'title'),(TPE1,'artist'),(TALB,'album'),(TPE2,'album_artist'),
                       (TCON,'genre'),(TDRC,'release_date'),(TRCK,'track_number'),(TSRC,'isrc')]:
        if info.get(key):
            tags.add(frame(encoding=3, text=str(info[key])))
    if info.get('description'):
        tags.add(COMM(encoding=3, lang='eng', desc='Source description', text=info['description']))
    if info.get('lyrics'):
        tags.add(USLT(encoding=3, lang='eng', desc='', text=info['lyrics']))
    tags.add(WOAS(url=info['url']))
    tags.add(TXXX(encoding=3, desc='DJTOOL_SOURCE', text=info['provider']))
    tags.add(TXXX(encoding=3, desc='DJTOOL_SOURCE_ID', text=str(info['remote_id'])))
    if cover:
        tags.add(APIC(encoding=3, mime=cover[0], type=3, desc='Cover', data=cover[1]))
    audio.save(v2_version=3)
