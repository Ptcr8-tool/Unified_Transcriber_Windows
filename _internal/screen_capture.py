"""Legacy-neutral Windows window enumeration and MP4 inspection.

No screen recorder is included: video capture is delegated to Xbox Game Bar.
FFmpeg is used for audio muxing/inspection only.
"""
from __future__ import annotations

from process_control import run_cancellable
import ctypes
import os
import re
import subprocess
import time
from pathlib import Path

def _hidden_flags():
    return subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0

def windows() -> list[tuple[str,int]]:
    """Enumerate visible top-level window titles and their HWND, without dependencies."""
    if os.name!='nt':return []
    from ctypes import wintypes
    user32=ctypes.WinDLL('user32',use_last_error=True)
    result=[]
    cb_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user32.EnumWindows.argtypes=[cb_type,wintypes.LPARAM]
    user32.IsWindowVisible.argtypes=[wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes=[wintypes.HWND]
    user32.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    @cb_type
    def callback(hwnd,_param):
        if not user32.IsWindowVisible(hwnd):return True
        process=wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd,ctypes.byref(process))
        if process.value==os.getpid():return True
        length=user32.GetWindowTextLengthW(hwnd)
        if length<=0:return True
        title=ctypes.create_unicode_buffer(length+1)
        user32.GetWindowTextW(hwnd,title,length+1)
        label=title.value.strip()
        if label:result.append((label,int(hwnd)))
        return True
    if not user32.EnumWindows(callback,0):
        raise OSError('Windowsのウィンドウ一覧取得に失敗しました')
    return result


def has_audio_stream(media: Path, ffmpeg: str, *, cancel=None, registry=None) -> bool:
    """Inspect MP4 stream headers only (does not decode the video).

    Bundled imageio-ffmpeg has an ffmpeg.exe but often no ffprobe.exe, so
    inspect ffmpeg -i output rather than depending on a separate binary.
    An intentionally nonzero exit from ffmpeg with no output target is fine.
    """
    media=Path(media)
    if not media.is_file() or media.stat().st_size==0:
        return False
    try:
        result=run_cancellable([str(ffmpeg),'-hide_banner','-i',str(media)],
            capture_output=True,text=True,encoding='utf-8',errors='replace',
            creationflags=_hidden_flags(),timeout=20,cancel=cancel,registry=registry)
    except InterruptedError:
        raise
    except (subprocess.SubprocessError,OSError) as exc:
        raise RuntimeError(f'録画MP4の音声ストリーム検査に失敗しました: {exc}') from exc
    information=result.stderr+'\n'+result.stdout
    if not ('Input #0' in information or 'Stream #0' in information):
        raise RuntimeError('録画MP4の情報をFFmpegから読み取れませんでした: '+information[-1000:])
    return bool(re.search(r'^\s*Stream #\d+:\d+[^\n]*?: Audio:',information,re.MULTILINE))


def unique_destination(desired:Path)->Path:
    if not desired.exists():return desired
    stamp=time.strftime('%Y%m%d_%H%M%S')
    for i in range(1,10000):
        candidate=desired.with_name(f'{desired.stem}_{stamp}_{i}{desired.suffix}')
        if not candidate.exists():return candidate
    raise FileExistsError('保存先の名前を確保できません')
