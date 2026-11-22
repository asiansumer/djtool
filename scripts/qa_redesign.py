"""新界面的截图检查：用隔离曲库，不联网、不播放、不碰真实 Serato。

用法：.venv/bin/python scripts/qa_redesign.py <曲库目录> [输出目录]
"""
import os
from pathlib import Path
import sys
import time
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from djtool.app import configure_application
from djtool.ui import theme
from djtool.ui.covers import loader
from djtool.ui.main_window import MainWindow
from djtool.intake_queue import Batch

library = sys.argv[1]
out = Path(sys.argv[2] if len(sys.argv) > 2 else 'work/redesign')
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
configure_application(app)
theme.apply(app, 'dark')
window = MainWindow(library, remember=False)
window.resize(1320, 880)
window.show()


def settle(seconds=1.2):
    # 封面在后台线程里准备，等它们到齐再截图。
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline or loader().pending:
        app.processEvents()
        time.sleep(.01)
        if time.monotonic() > deadline + 15:
            break


def shot(name):
    settle()
    window.grab().save(str(out / f'{name}.png'))


shot('wall')
page = window.library_page
wall = page.wall
if wall.model().rowCount() > 8:
    wall.select_ids({wall.model().index(2, 0).data(256)['id']})
    window.player.track = page.selected_tracks()[0]
    window.player._show()
    window.player._enable(True)
    window._playing_changed(window.player.track['id'])
    wall.hover_row = 4
shot('wall-selected')
ids = [wall.model().index(row, 0).data(256)['id'] for row in (2, 3, 7)]
wall.select_ids(set(ids))
window._drag_started(3)
shot('wall-dragging')
window.tray.hide()
wall.select_ids({ids[0]})
page.show_info(page.selected_tracks()[0])
shot('wall-info')
page.inspector.close_panel()
page.set_view('list')
shot('list')
page.set_view('wall')
window.show_page('sets')
shot('sets')
sets = [item for item in window.collections if item['kind'] == 'set']
if sets:
    window.open_collection(sets[0]['id'])
    shot('set')
# 收歌页：用曲库里已有的下载记录拼几个示例歌单，再放一个装着假文件的收歌文件夹。
# 全程不发起网络请求，也不导入任何文件。
import os as _os
queue = window.queue
lib = window.library
jobs = lib.online_jobs()
done = [job['id'] for job in jobs if job['state'] == 'complete']
todo = [job['id'] for job in jobs if job['state'] != 'complete']
if not any(source['url'].endswith('id=900001') for source in lib.link_sources()):
    lib.save_link_source('https://music.163.com/playlist?id=900001', 'netease', 'Hip-Hop 待收', todo[:12] + done[:140])
    lib.save_link_source('https://music.163.com/playlist?id=900002', 'netease', '千禧年电子', done[200:290])
    lib.save_link_source('https://soundcloud.com/qa/likes', 'soundcloud', 'qa 的点赞', done[400:430] + todo[20:23])
    lib.save_link_source('https://soundcloud.com/qa-edits/tracks', 'soundcloud', 'qa-edits 的上传', done[500:520] + todo[30:60], 'pick')
folder = Path('work/qa-watch/网易云音乐').resolve()
folder.mkdir(parents=True, exist_ok=True)
for name in ('Marlo Vance - Glass Avenue.ncm', 'Sela Okoro - Paper Planes.ncm', 'Northwick - Tidal Motel.ncm'):
    target = folder / name
    if not target.exists():
        target.write_bytes(b'qa placeholder')
        _os.utime(target, (time.time() - 60, time.time() - 60))
queue.watch().add(folder)
window.show_page('intake')
shot('intake')
running = Batch('files', '14 个 NCM 文件', state='running', detail='Juno Achebe - Ember Arcade.ncm', progress=(5, 14))
failed = Batch('link', '没能读取这个链接', state='failed', detail='在线解析服务暂未返回有效结果，请稍后重试。')
single = Batch('link', 'Dez Amari - Neon Runway (KOVA Baile Funk Flip)', state='done', detail='新入库 1 首', added=1)
for batch in (failed, single, running):
    queue.batches.insert(0, batch)
    queue.batch_added.emit(batch)
window.check_clipboard('https://music.163.com/playlist?id=360000118')
shot('intake-busy')
first = next(source for source in lib.link_sources() if source['url'].endswith('id=900001'))
window.intake_page.open_source(first['id'])
shot('intake-source')
window.intake_page.show_overview()
theme.apply(app, 'light')
window.show_page('library')
shot('wall-light')
print('saved to', out)
