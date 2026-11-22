"""纯文件音频处理：所有输出到文件或空设备，绝不调用播放器。"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


class AudioError(ValueError):
    pass


class AudioCancelled(AudioError):
    pass


def check_cancel(cancel):
    if cancel():
        raise AudioCancelled('已停止处理；已入库文件会保留')


def ffmpeg_path():
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'bin/ffmpeg'
    for candidate in (bundled, shutil.which('ffmpeg'), '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise AudioError('需要 FFmpeg 才能转换或分析音频，请安装后重试')


def run_file_process(command, cancel=lambda: False, timeout=180, env=None, new_session=True):
    """后台进程可停止；终止整组子进程，避免遗留 FFmpeg。错误输出不包含音频。"""
    check_cancel(cancel)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, env=env, start_new_session=new_session)
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancel(cancel)
                if time.monotonic() - started > timeout:
                    raise AudioError('文件处理超时，请稍后重试')
                time.sleep(.1)
            check_cancel(cancel)
            if process.returncode:
                raise AudioError('音频处理失败，文件可能损坏或格式不支持')
        finally:
            if process.poll() is None:
                try:
                    if new_session:
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        if new_session:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait()


def verify_audio(path, cancel=lambda: False):
    # 完整解码至 null muxer，不连接扬声器；文件头可读不代表内容完整。
    run_file_process([ffmpeg_path(), '-v', 'error', '-nostdin', '-xerror',
                      '-err_detect', 'explode', '-i', str(path), '-map', '0:a:0',
                      '-vn', '-f', 'null', '-'], cancel, timeout=900)


def transcode_mp3(source, destination, cancel=lambda: False):
    run_file_process([ffmpeg_path(), '-v', 'error', '-nostdin', '-xerror',
                      '-err_detect', 'explode', '-i', str(source), '-map', '0:a:0',
                      '-vn', '-c:a', 'libmp3lame', '-q:a', '0', str(destination)],
                     cancel, timeout=900)
