# 本机 Apple Silicon 打包；发布给其他 Mac 前仍需签名、公证和干净环境验收。
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files
from djtool.audio_tasks import ffmpeg_path

project = Path(SPECPATH)
data = [('assets/icons', 'assets/icons'), ('assets/app-icon.svg', 'assets')]
data += collect_data_files('yt_dlp')
hidden = []
a = Analysis(['main.py'], pathex=[str(project)], binaries=[(ffmpeg_path(), 'bin')],
    datas=data, hiddenimports=hidden, hookspath=[], runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore','PySide6.QtWebEngineWidgets','PySide6.QtWebEngineQuick',
              'matplotlib','IPython','pytest','tkinter','librosa','numba','numpy','scipy','llvmlite'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='DJTOOL',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    target_arch='arm64', codesign_identity=None, entitlements_file=None)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='DJTOOL')
app = BUNDLE(collection, name='DJTOOL.app', bundle_identifier='com.asumer.djtool',
    icon=str(project/'work/app-icon.icns'),
    info_plist={'CFBundleName':'DJTOOL','CFBundleDisplayName':'DJTOOL',
                'CFBundleShortVersionString':'0.13.1','CFBundleVersion':'24',
                'NSHighResolutionCapable':True})
