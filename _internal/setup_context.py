"""Validated identity for the standard NSIS setup's isolated Python child.

The setup entry validates its real parent before importing Tk or installation
code. Environment values are transport only, never an executable trust anchor.
Source checkouts and the old frozen setup retain their existing behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import sys


@dataclass(frozen=True)
class SetupContext:
    executable: Path
    resource_root: Path
    owner_pid: int
    arguments: tuple[str, ...] = ()


_context: SetupContext | None = None


def get_context() -> SetupContext | None:
    return _context


def _ordinary(path: Path, *, directory: bool = False) -> bool:
    """Reject a link/reparse point before following any path component."""
    try:
        for candidate in (path, *path.parents):
            info = candidate.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                return False
        return path.is_dir() if directory else path.is_file()
    except (OSError, ValueError):
        return False


def process_image(pid: int) -> Path:
    """Query an existing Windows process with full-width HANDLE declarations."""
    if os.name != 'nt':
        raise RuntimeError('セットアップの起動元をWindowsで確認できません。')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise RuntimeError('セットアップの起動元を安全に確認できません。')
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise RuntimeError('セットアップの起動元を安全に確認できません。')
        return Path(image.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def command_line_arguments(raw: str) -> tuple[str, ...]:
    """Parse the original UTF-16 command line once, without a shell or NSIS's
    limited-length string stack. Only user arguments are returned.
    """
    if not raw or '\x00' in raw or len(raw) > 32767:
        raise RuntimeError('セットアップの起動引数が不正です。')
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(raw, ctypes.byref(count))
    if not pointer:
        raise RuntimeError('セットアップの起動引数を読み込めません。')
    try:
        if count.value < 1:
            raise RuntimeError('セットアップの起動引数が不正です。')
        return tuple(pointer[index] for index in range(1, count.value))
    finally:
        kernel.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))


def initialize_context(resource_root: Path) -> SetupContext:
    """Consume NSIS context and bind it to the actual parent and staged runtime.

    No command-line arguments can override these values. The parent keeps its
    private extraction folder alive while waiting for this Python process.
    """
    global _context
    origin_raw = os.environ.pop('UNIFIED_SETUP_ORIGIN', '')
    owner_raw = os.environ.pop('UNIFIED_SETUP_PARENT_PID', '')
    command_line = os.environ.pop('UNIFIED_SETUP_COMMAND_LINE', '')
    if _context is not None:
        raise RuntimeError('セットアップの起動情報が重複しています。')
    if (os.name != 'nt' or not origin_raw or '\x00' in origin_raw
            or re.fullmatch(r'[1-9][0-9]{0,9}', owner_raw) is None):
        raise RuntimeError('正規のセットアップexeから起動してください。')
    owner_pid = int(owner_raw)
    if owner_pid > 0xffffffff or owner_pid != os.getppid() or owner_pid == os.getpid():
        raise RuntimeError('セットアップの起動元が一致しません。')
    origin = Path(origin_raw)
    root = Path(resource_root)
    if not origin.is_absolute() or not root.is_absolute():
        raise RuntimeError('セットアップの起動情報が不正です。')
    if (not _ordinary(origin) or not _ordinary(root, directory=True)
            or not _ordinary(root / 'Runtime/pythonw.exe')
            or not _ordinary(root / '_internal/installer_bootstrap.py')
            or not _ordinary(root / 'BUILD_INFO.json')
            or not _ordinary(root / 'payload.zip')
            or not _ordinary(root / 'Runtime/tcl/tcl8.6/init.tcl')
            or not _ordinary(root / 'Runtime/tcl/tk8.6/tk.tcl')):
        raise RuntimeError('セットアップの実行環境が不足しているかリンクされています。再ダウンロードしてください。')
    origin, root = origin.resolve(), root.resolve()
    if Path(sys.executable).resolve() != root / 'Runtime/pythonw.exe':
        raise RuntimeError('セットアップに同梱された実行環境から起動してください。')
    if process_image(owner_pid) != origin:
        raise RuntimeError('セットアップの起動元が一致しません。')
    arguments = command_line_arguments(command_line)
    # -I isolates Python but Tcl also reads environment before creating Tk.
    # Replace all interpreter/tool injection, then select only bundled Tcl/Tk.
    for key in tuple(os.environ):
        upper = key.upper()
        if (upper.startswith(('PYTHON', 'PIP_', '_PYI_'))
                or upper in ('VIRTUAL_ENV', 'PY_PYTHON', 'PY_PYTHON3',
                             'TCL_LIBRARY', 'TK_LIBRARY', 'TCLLIBPATH', '_MEIPASS2')):
            os.environ.pop(key, None)
    os.environ.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1',
                      PIP_CONFIG_FILE=os.devnull,
                      TCL_LIBRARY=str(root / 'Runtime/tcl/tcl8.6'),
                      TK_LIBRARY=str(root / 'Runtime/tcl/tk8.6'))
    _context = SetupContext(origin, root, owner_pid, arguments)
    return _context


def packaged_setup() -> bool:
    return _context is not None or bool(getattr(sys, 'frozen', False))


def setup_executable() -> Path:
    return _context.executable if _context is not None else Path(sys.executable).resolve()


def resource_folder(fallback: Path) -> Path:
    return (_context.resource_root if _context is not None
            else Path(getattr(sys, '_MEIPASS', fallback)))
