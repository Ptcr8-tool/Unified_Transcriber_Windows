"""Windows screen/window preview via Win32 GDI (stdlib only; no Pillow needed).

Image acquisition is deliberately on demand. Call capture_preview in a worker:
PrintWindow is synchronous and may block on a misbehaving third-party window.
No images are stored on disk or sent to an external service.
"""
from __future__ import annotations

import ctypes
import os
import json
from pathlib import Path
import subprocess
import sys
from ctypes import wintypes

SRCCOPY = 0x00CC0020
HALFTONE = 4


def load_thumbnail(hwnd: int, max_width=256, max_height=144):
    """Bound PrintWindow with a child process timeout, so one window cannot block the list."""
    python=Path(sys.executable)
    if python.name.lower()=='pythonw.exe':python=python.with_name('python.exe')
    result=subprocess.run([str(python),str(Path(__file__).resolve()),str(int(hwnd)),
                           str(max_width),str(max_height)],capture_output=True,timeout=3,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8',errors='replace')[-250:] or '画像を取得できません。')
    header,ppm=result.stdout.split(b'\n',1)
    dimensions=json.loads(header)
    if not ppm.startswith(b'P6\n'):
        raise ValueError('プレビュー画像を読み込めません。')
    return ppm,int(dimensions['width']),int(dimensions['height'])


def fit_preview(width: int, height: int, maximum_width: int = 430,
                maximum_height: int = 200) -> tuple[int, int]:
    if min(width, height, maximum_width, maximum_height) <= 0:
        raise ValueError('プレビューの画像サイズが不正です。')
    scale = min(maximum_width / width, maximum_height / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


def bgra_to_ppm(bgra: bytes, width: int, height: int) -> bytes:
    """Produce a binary PPM that tkinter.PhotoImage understands natively."""
    if len(bgra) != width * height * 4 or width <= 0 or height <= 0:
        raise ValueError('プレビューピクセルの長さが一致しません。')
    rgb = bytearray(width * height * 3)
    for src in range(0, len(bgra), 4):
        dest = src // 4 * 3
        rgb[dest] = bgra[src + 2]
        rgb[dest + 1] = bgra[src + 1]
        rgb[dest + 2] = bgra[src]
    return f'P6\n{width} {height}\n255\n'.encode('ascii') + rgb


class _BitmapHeader(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [('bmiHeader', _BitmapHeader), ('bmiColors', wintypes.DWORD * 1)]


def capture_preview(target: str, max_width: int = 430,
                    max_height: int = 200) -> tuple[bytes, int, int]:
    """Return PPM bytes, original width, original height for desktop or hwnd=NNN.

    PrintWindow sometimes returns black for accelerated/protected surfaces. That is
    not interpreted as a successful screen-recording capability check.
    """
    if os.name != 'nt':
        raise OSError('プレビューはWindowsでのみ利用できます。')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
    HDC = wintypes.HDC
    HBITMAP = wintypes.HBITMAP
    HWND = wintypes.HWND
    HANDLE = wintypes.HANDLE
    # On 64-bit Windows, handles must never default to ctypes.c_int.
    user32.GetDC.argtypes = [HWND]; user32.GetDC.restype = HDC
    user32.ReleaseDC.argtypes = [HWND, HDC]
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsWindow.argtypes = [HWND]
    user32.IsIconic.argtypes = [HWND]
    user32.PrintWindow.argtypes = [HWND, HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.argtypes = [HDC]; gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = HBITMAP
    gdi32.CreateDIBSection.argtypes = [HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                      ctypes.POINTER(ctypes.c_void_p), HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = HBITMAP
    gdi32.SelectObject.argtypes = [HDC, HANDLE]; gdi32.SelectObject.restype = HANDLE
    gdi32.DeleteObject.argtypes = [HANDLE]
    gdi32.DeleteDC.argtypes = [HDC]
    gdi32.BitBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.StretchBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.DWORD]
    gdi32.SetStretchBltMode.argtypes = [HDC, ctypes.c_int]

    desktop = target == 'desktop'
    hwnd = None
    if desktop:
        x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        y = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    elif target.startswith('hwnd=') and target[5:].isdecimal():
        hwnd = int(target[5:])
        if not user32.IsWindow(hwnd):
            raise ValueError('選択したウィンドウは既に閉じられています。一覧を更新してください。')
        if user32.IsIconic(hwnd):
            raise ValueError('ウィンドウが最小化されています。復元してからプレビューを更新してください。')
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError('選択したウィンドウの表示領域を取得できません。')
        x, y = rect.left, rect.top
        width, height = rect.right - rect.left, rect.bottom - rect.top
    else:
        raise ValueError('録画対象が不正です。')
    if min(width, height) <= 0 or width > 16000 or height > 10000:
        raise ValueError('ウィンドウのサイズが不正、または大きすぎます。')
    thumb_w, thumb_h = fit_preview(width, height, max_width, max_height)
    screen = user32.GetDC(None)
    if not screen:
        raise OSError('画面のデバイスコンテキストを取得できません。')
    source_dc = thumbnail_dc = source_bitmap = thumbnail_bitmap = None
    source_old = thumbnail_old = None
    try:
        source_dc = gdi32.CreateCompatibleDC(screen)
        thumbnail_dc = gdi32.CreateCompatibleDC(screen)
        if not source_dc or not thumbnail_dc:
            raise OSError('プレビュー用の描画領域を作成できません。')
        source_bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        if not source_bitmap:
            raise OSError('プレビュー元画像用のメモリを確保できません。')
        source_old = gdi32.SelectObject(source_dc, source_bitmap)
        if desktop:
            copied = bool(gdi32.BitBlt(source_dc, 0, 0, width, height, screen, x, y, SRCCOPY))
        else:
            copied = bool(user32.PrintWindow(hwnd, source_dc, 2))  # render full content, where supported
            if not copied:
                copied = bool(user32.PrintWindow(hwnd, source_dc, 0))
        if not copied:
            raise OSError('画面を取得できません。GPU描画や保護されたウィンドウの可能性があります。')
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapHeader)
        info.bmiHeader.biWidth = thumb_w
        info.bmiHeader.biHeight = -thumb_h  # top-down, so no line flipping
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        data_ptr = ctypes.c_void_p()
        thumbnail_bitmap = gdi32.CreateDIBSection(thumbnail_dc, ctypes.byref(info), 0,
                                                    ctypes.byref(data_ptr), None, 0)
        if not thumbnail_bitmap or not data_ptr.value:
            raise OSError('プレビュー縮小画像用のメモリを確保できません。')
        thumbnail_old = gdi32.SelectObject(thumbnail_dc, thumbnail_bitmap)
        gdi32.SetStretchBltMode(thumbnail_dc, HALFTONE)
        if not gdi32.StretchBlt(thumbnail_dc, 0, 0, thumb_w, thumb_h,
                                source_dc, 0, 0, width, height, SRCCOPY):
            raise OSError('プレビュー画像の縮小に失敗しました。')
        return bgra_to_ppm(ctypes.string_at(data_ptr, thumb_w * thumb_h * 4),
                           thumb_w, thumb_h), width, height
    finally:
        if thumbnail_old and thumbnail_dc:
            gdi32.SelectObject(thumbnail_dc, thumbnail_old)
        if source_old and source_dc:
            gdi32.SelectObject(source_dc, source_old)
        if thumbnail_bitmap:
            gdi32.DeleteObject(thumbnail_bitmap)
        if source_bitmap:
            gdi32.DeleteObject(source_bitmap)
        if thumbnail_dc:
            gdi32.DeleteDC(thumbnail_dc)
        if source_dc:
            gdi32.DeleteDC(source_dc)
        user32.ReleaseDC(None, screen)


if __name__=='__main__':
    try:
        image,width,height=capture_preview('hwnd='+str(int(sys.argv[1])),int(sys.argv[2]),int(sys.argv[3]))
        output=json.dumps({'width':width,'height':height}).encode('ascii')+b'\n'+image
        with os.fdopen(1,'wb',closefd=False) as stream:stream.write(output)
    except Exception as exc:
        with os.fdopen(2,'wb',closefd=False) as stream:stream.write(str(exc).encode('utf-8'))
        raise SystemExit(1)
