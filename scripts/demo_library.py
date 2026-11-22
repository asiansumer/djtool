"""生成一个可以公开展示的演示曲库：歌名、艺人都是虚构的，封面是程序画的，音频是静音。

README 里的截图就是用它出的：真实曲库里的专辑封面有版权，也是个人数据，不能放进仓库。
全程不联网、不播放、不碰 Serato；BPM 和调性是直接写进缓存的演示数值，不是分析出来的。

用法：.venv/bin/python scripts/demo_library.py <新目录>
然后：.venv/bin/python scripts/qa_redesign.py <新目录> docs/screenshots
"""
import os
from pathlib import Path
import random
import subprocess
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QRadialGradient

from djtool.audio_tasks import ffmpeg_path
from djtool.library import Library
from djtool.metadata import embed_online

ARTISTS = ['Marlo Vance', 'Kaito Brume', 'Sela Okoro', 'Northwick', 'Dez Amari', 'Lune Halloway', 'Tavi Roque',
           'Bexley Stone', 'Ondine Park', 'Rafe Calder', 'Mira Solen', 'Juno Achebe', 'Castor Vey', 'Ilya Fenn',
           'Paloma Dray', 'Kesh Lindqvist', 'Otis Maren', 'Zadie Kwon', 'Hollis Reyes', 'Nia Thornbury']
EDITORS = ['KOVA', 'Tessel', 'Ridgeback', 'OHM.9', 'Lowtide', 'Saffron Kid', 'DJ Marrow', 'Pylon', 'Vesper Unit']
FIRST = ['Night', 'Glass', 'Paper', 'Neon', 'Velvet', 'Concrete', 'Summer', 'Static', 'Golden', 'Midnight',
         'Marble', 'Copper', 'Silent', 'Electric', 'Lemon', 'Harbour', 'Satin', 'Ember', 'Chrome', 'Tidal']
SECOND = ['Bus', 'Ceiling', 'Planes', 'Avenue', 'Rooms', 'Garden', 'Receipt', 'Weather', 'Hours', 'Signal',
          'Letters', 'Motel', 'Parade', 'Elevator', 'Postcard', 'Carousel', 'Arcade', 'Runway', 'Balcony', 'Tape']
STYLES = ['Jersey Club Edit', 'Baile Funk Flip', 'Amapiano Edit', 'Afro House Remix', 'Club Edit', 'Bootleg',
          'UKG Flip', 'Intro Clean', 'Halftime Edit']
ALBUMS = ['Late Shift', 'Blue Hour', 'Postcards', 'Second Summer', 'Low Ceiling', 'Open Tab']
KEYS = [f'{number}{letter}' for number in range(1, 13) for letter in 'AB']
PALETTES = [('#101a3a', '#e8553d', '#f4c95d'), ('#0d2b2a', '#2fbf9f', '#f2efe4'), ('#2a0f2e', '#d94f8a', '#ffd1a6'),
            ('#1b1b1f', '#f0b429', '#e9e4d8'), ('#07223b', '#4aa3df', '#f7e6c4'), ('#33150d', '#ef7b45', '#f9dcc4'),
            ('#12251a', '#9ccc65', '#fdf6d8'), ('#23153c', '#8f7af0', '#f3c6e8'), ('#3a1010', '#e04f5f', '#ffe1b3'),
            ('#0f1f2e', '#5ad1c8', '#f0e7ff')]


def cover(rng):
    """一张抽象封面：渐变底色加几个几何形状。每张都不一样，不模仿任何真实专辑。"""
    dark, strong, light = (QColor(value) for value in rng.choice(PALETTES))
    image = QImage(600, 600, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, 600 * rng.random(), 600)
    gradient.setColorAt(0, dark.lighter(100 + rng.randrange(60)))
    gradient.setColorAt(1, dark.darker(140))
    painter.fillRect(0, 0, 600, 600, gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    layout = rng.randrange(5)
    if layout == 0:      # 一轮大圆
        glow = QRadialGradient(QPointF(300 + rng.randint(-90, 90), 320 + rng.randint(-90, 90)), 260)
        glow.setColorAt(0, light)
        glow.setColorAt(.55, strong)
        glow.setColorAt(1, QColor(strong.red(), strong.green(), strong.blue(), 0))
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(300, 320), 270, 270)
    elif layout == 1:    # 横条
        top = rng.randint(60, 200)
        for index in range(rng.randint(4, 7)):
            painter.setBrush(strong if index % 2 else light)
            painter.drawRect(QRectF(0, top + index * 62, 600 - index * rng.randint(30, 80), 30))
    elif layout == 2:    # 同心环
        centre = QPointF(rng.randint(180, 420), rng.randint(180, 420))
        for index, radius in enumerate(range(300, 30, -45)):
            painter.setBrush((strong, dark.darker(120), light)[index % 3])
            painter.drawEllipse(centre, radius, radius)
    elif layout == 3:    # 斜切色块
        painter.save()
        painter.translate(300, 300)
        painter.rotate(rng.choice((-32, -18, 24, 40)))
        painter.setBrush(strong)
        painter.drawRect(QRectF(-500, -60 + rng.randint(-80, 80), 1000, 190))
        painter.setBrush(light)
        painter.drawRect(QRectF(-500, 150 + rng.randint(-40, 40), 1000, 34))
        painter.restore()
    else:                # 点阵
        step = rng.choice((60, 75, 100))
        for x in range(step // 2, 600, step):
            for y in range(step // 2, 600, step):
                painter.setBrush(light if (x + y) // step % 3 == 0 else strong)
                size = step * (.18 + .3 * rng.random())
                painter.drawEllipse(QPointF(x, y), size, size)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'JPG', 86)
    return bytes(data)


def main(destination, count=84):
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        sys.exit('请给一个新的或空的目录，演示曲库不会写进已有内容的目录')
    # 画封面要有一个 Qt 应用实例；留着引用，函数结束前不能被回收。
    _app = QGuiApplication.instance() or QGuiApplication([])
    rng = random.Random(20261122)
    scratch = destination / '.build'
    scratch.mkdir(parents=True)
    library = Library(destination)
    ffmpeg = ffmpeg_path()
    titles = [f'{first} {second}' for first in FIRST for second in SECOND]
    rng.shuffle(titles)
    infos, tracks, bpms, keys = [], [], {}, {}
    for index in range(count):
        artist = rng.choice(ARTISTS)
        title = titles[index]
        if rng.random() < .55:
            title = f'{title} ({rng.choice(EDITORS)} {rng.choice(STYLES)})'
        provider = 'soundcloud' if '(' in title and rng.random() < .6 else 'netease'
        seconds = rng.randint(118, 262)
        info = {'provider': provider, 'remote_id': str(700000 + index), 'title': title, 'artist': artist,
                'album': rng.choice(ALBUMS) if provider == 'netease' else '', 'duration': seconds,
                'url': (f'https://music.163.com/song?id={700000 + index}' if provider == 'netease'
                        else f'https://soundcloud.com/demo/{700000 + index}')}
        infos.append(info)
        # 最后十几首留着不“下载”，这样记住的歌单会显示“缺 N 首”。
        if index >= count - 14:
            continue
        path = scratch / f'{index:03d}.mp3'
        subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'anullsrc=r=22050:cl=mono',
                        '-t', str(seconds), '-b:a', '32k', '-c:a', 'libmp3lame', str(path)], check=True)
        embed_online(path, info, cover=('image/jpeg', cover(rng)))
        identifier, _ = library.import_file(path)
        tracks.append(identifier)
        playback = library.track(identifier)['path']
        # 演示数值：真实使用时这两项只来自 Serato 的分析结果。少数几首留空，界面显示“—”。
        if rng.random() < .9:
            bpms[playback] = float(rng.choice((88, 92, 96, 100, 104, 110, 118, 124, 128, 132, 140, 150)))
            keys[playback] = rng.choice(KEYS)
    jobs = library.enqueue_many(infos)
    for job, identifier in zip(jobs, tracks):
        library.set_job_state(job['id'], 'complete', track_id=identifier)
    library.replace_serato_bpms(bpms, str(scratch / 'demo-serato.sqlite'), keys=keys)
    ids = [job['id'] for job in jobs]
    done, todo = ids[:len(tracks)], ids[len(tracks):]
    library.save_link_source('https://music.163.com/playlist?id=900001', 'netease', 'Hip-Hop 待收', todo[:6] + done[:34])
    library.save_link_source('https://music.163.com/playlist?id=900002', 'netease', '暖场慢歌', done[34:52])
    library.save_link_source('https://soundcloud.com/demo/likes', 'soundcloud', 'demo 的点赞', todo[6:9] + done[52:64])
    library.save_link_source('https://soundcloud.com/kova/tracks', 'soundcloud', 'KOVA 的上传',
                             done[64:70] + todo[9:], 'pick')
    for name, members in (('周五 Hip-Hop', tracks[2:16]), ('Baile / Jersey 热场', tracks[20:31]),
                          ('收尾慢歌', tracks[40:47])):
        library.add_members(library.create_collection(name, 'set'), members)
    for path in scratch.glob('*.mp3'):
        path.unlink()
    scratch.rmdir()
    print(f'演示曲库：{destination}（{len(tracks)} 首，{len(todo)} 首待收）')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
