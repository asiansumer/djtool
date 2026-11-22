"""从项目 SVG 生成 macOS 图标，只生成本地构建资产。"""
import os
from pathlib import Path
import subprocess
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication,QImage,QPainter
from PySide6.QtSvg import QSvgRenderer
ROOT=Path(__file__).resolve().parents[1]
target=ROOT/'work/app-icon.icns'
if target.exists() and target.stat().st_mtime > (ROOT/'assets/app-icon.svg').stat().st_mtime:
    raise SystemExit(0)
app=QGuiApplication([])
folder=ROOT/'work/DJTOOL.iconset';folder.mkdir(parents=True,exist_ok=True)
renderer=QSvgRenderer(str(ROOT/'assets/app-icon.svg'))
for base in (16,32,128,256,512):
    for multiplier in (1,2):
        size=base*multiplier
        image=QImage(size,size,QImage.Format.Format_ARGB32);image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image);renderer.render(painter);painter.end()
        image.save(str(folder/f'icon_{base}x{base}{"@2x" if multiplier==2 else ""}.png'))
subprocess.run(['/usr/bin/iconutil','-c','icns',str(folder),'-o',str(ROOT/'work/app-icon.icns')],check=True)
