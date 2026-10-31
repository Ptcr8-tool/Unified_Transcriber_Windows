"""Opt-in Windows startup and an ordinary notification-area icon.

Only the installed application executable is registered; no shell commands,
downloaded code, elevated privileges or third-party tray libraries are used.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

_RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_RUN_NAME = 'UnifiedTranscriber'


def startup_command(directory: Path) -> str:
    return subprocess.list2cmdline([str(Path(directory) / 'UnifiedTranscriber.exe'), '--background'])


def startup_enabled(directory: Path) -> bool:
    if sys.platform != 'win32':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, _RUN_NAME)
    except FileNotFoundError:
        return False
    return kind == winreg.REG_SZ and value.casefold() == startup_command(directory).casefold()


def set_startup(directory: Path, enabled: bool) -> None:
    if sys.platform != 'win32':
        raise OSError('Windows のインストール版で利用できます。')
    import winreg
    directory = Path(directory)
    if enabled and not (directory / 'UnifiedTranscriber.exe').is_file():
        raise OSError('自動起動はインストール版で利用できます。')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            existing, kind = winreg.QueryValueEx(key, _RUN_NAME)
        except FileNotFoundError:
            existing, kind = None, None
        # Do not remove or replace another installation's registration.
        if existing is not None and (kind != winreg.REG_SZ or
                existing.casefold() != startup_command(directory).casefold()):
            raise OSError('別のインストール先が自動起動に登録されています。先にその設定を解除してください。')
        if enabled:
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, startup_command(directory))
        elif existing is not None:
            winreg.DeleteValue(key, _RUN_NAME)


def remove_startup_if_matches(directory: Path) -> None:
    if sys.platform == 'win32' and startup_enabled(directory):
        set_startup(directory, False)


class WindowsTray:
    """A message-pump thread that posts actions to the GUI's thread-safe queue."""
    def __init__(self, emit, executable: Path):
        self.emit = emit
        self.executable = Path(executable)
        self._ready = threading.Event()
        self._thread = None
        self._hwnd = None
        self._user = None
        self.error = ''

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return bool(self._hwnd)
        if sys.platform != 'win32':
            self.error = '通知領域への格納は Windows で利用できます。'
            return False
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='notification-area')
        self._thread.start()
        if not self._ready.wait(4):
            self.error = '通知領域アイコンの準備がタイムアウトしました。'
            return False
        return bool(self._hwnd)

    def close(self):
        hwnd, user, thread = self._hwnd, self._user, self._thread
        self.emit = lambda _action: None
        if hwnd and user:
            user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self):
        try:
            self._windows_pump()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._hwnd = None
            self._ready.set()

    def _windows_pump(self):
        import ctypes
        from ctypes import wintypes as w
        user = ctypes.WinDLL('user32', use_last_error=True)
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        class WNDCLASS(ctypes.Structure):
            _fields_ = [('style', w.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                        ('cbWndExtra', ctypes.c_int), ('hInstance', w.HINSTANCE), ('hIcon', w.HICON),
                        ('hCursor', w.HANDLE), ('hbrBackground', w.HANDLE),
                        ('lpszMenuName', w.LPCWSTR), ('lpszClassName', w.LPCWSTR)]
        class GUID(ctypes.Structure):
            _fields_ = [('a', w.DWORD), ('b', w.WORD), ('c', w.WORD), ('d', ctypes.c_ubyte * 8)]
        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [('cbSize', w.DWORD), ('hWnd', w.HWND), ('uID', w.UINT), ('uFlags', w.UINT),
                        ('uCallbackMessage', w.UINT), ('hIcon', w.HICON), ('szTip', w.WCHAR * 128),
                        ('dwState', w.DWORD), ('dwStateMask', w.DWORD), ('szInfo', w.WCHAR * 256),
                        ('uTimeoutOrVersion', w.UINT), ('szInfoTitle', w.WCHAR * 64),
                        ('dwInfoFlags', w.DWORD), ('guidItem', GUID), ('hBalloonIcon', w.HICON)]
        def proto(dll, name, args, result):
            fn = getattr(dll, name); fn.argtypes = args; fn.restype = result; return fn
        proto(kernel, 'GetModuleHandleW', [w.LPCWSTR], w.HMODULE)
        proto(user, 'RegisterClassW', [ctypes.POINTER(WNDCLASS)], w.ATOM)
        proto(user, 'UnregisterClassW', [w.LPCWSTR, w.HINSTANCE], w.BOOL)
        proto(user, 'CreateWindowExW', [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
              ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p], w.HWND)
        proto(user, 'DefWindowProcW', [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t)
        proto(user, 'DestroyWindow', [w.HWND], w.BOOL)
        proto(user, 'PostQuitMessage', [ctypes.c_int], None)
        proto(user, 'PostMessageW', [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL)
        proto(user, 'GetMessageW', [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], ctypes.c_int)
        proto(user, 'TranslateMessage', [ctypes.POINTER(w.MSG)], w.BOOL)
        proto(user, 'DispatchMessageW', [ctypes.POINTER(w.MSG)], ctypes.c_ssize_t)
        proto(user, 'CreatePopupMenu', [], w.HMENU)
        proto(user, 'AppendMenuW', [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR], w.BOOL)
        proto(user, 'DestroyMenu', [w.HMENU], w.BOOL)
        proto(user, 'GetCursorPos', [ctypes.POINTER(w.POINT)], w.BOOL)
        proto(user, 'SetForegroundWindow', [w.HWND], w.BOOL)
        proto(user, 'TrackPopupMenu', [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p], w.UINT)
        proto(user, 'RegisterWindowMessageW', [w.LPCWSTR], w.UINT)
        proto(user, 'LoadIconW', [w.HINSTANCE, ctypes.c_void_p], w.HICON)
        proto(user, 'DestroyIcon', [w.HICON], w.BOOL)
        proto(shell, 'ExtractIconW', [w.HINSTANCE, w.LPCWSTR, w.UINT], w.HICON)
        proto(shell, 'Shell_NotifyIconW', [w.DWORD, ctypes.POINTER(NOTIFYICONDATA)], w.BOOL)
        instance = kernel.GetModuleHandleW(None)
        class_name = 'UnifiedTranscriberTray' + uuid.uuid4().hex
        taskbar_created = user.RegisterWindowMessageW('TaskbarCreated')
        icon = shell.ExtractIconW(instance, str(self.executable), 0)
        owned_icon = bool(icon and icon != 1)
        if not owned_icon:
            icon = user.LoadIconW(None, ctypes.c_void_p(32512))
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(data); data.uID = 1
        data.uFlags = 0x01 | 0x02 | 0x04; data.uCallbackMessage = 0x8001
        data.hIcon = icon; data.szTip = '文字起こし統合スタジオ'
        actions = {1:'show', 2:'live', 3:'gamebar', 4:'stop', 5:'exit'}
        def menu(hwnd):
            popup = user.CreatePopupMenu()
            if not popup:return
            try:
                for ident, label in ((1,'画面を開く'), (2,'録音を開始'), (3,'Game Bar 録画を開始'),
                                     (4,'処理を停止'), (5,'アプリを終了')):
                    user.AppendMenuW(popup, 0, ident, label)
                point = w.POINT(); user.GetCursorPos(ctypes.byref(point))
                user.SetForegroundWindow(hwnd)
                choice = user.TrackPopupMenu(popup, 0x0100 | 0x0080 | 0x0002,
                                              point.x, point.y, 0, hwnd, None)
                if choice in actions:self.emit(actions[choice])
                user.PostMessageW(hwnd, 0, 0, 0)
            finally:
                user.DestroyMenu(popup)
        @WNDPROC
        def proc(hwnd, message, wp, lp):
            if message == 0x8001:
                if lp == 0x0202:self.emit('show')  # left button up
                elif lp == 0x0205:menu(hwnd)       # right button up
                return 0
            if taskbar_created and message == taskbar_created:
                shell.Shell_NotifyIconW(0, ctypes.byref(data))
                return 0
            if message == 0x0010:
                user.DestroyWindow(hwnd); return 0
            if message == 0x0002:
                shell.Shell_NotifyIconW(2, ctypes.byref(data))
                user.PostQuitMessage(0); return 0
            return user.DefWindowProcW(hwnd, message, wp, lp)
        wc = WNDCLASS(); wc.lpfnWndProc = proc; wc.hInstance = instance; wc.lpszClassName = class_name
        if not user.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = None
        try:
            hwnd = user.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, None, None, instance, None)
            if not hwnd:raise ctypes.WinError(ctypes.get_last_error())
            data.hWnd = hwnd
            if not shell.Shell_NotifyIconW(0, ctypes.byref(data)):
                raise OSError('Windows の通知領域にアイコンを登録できませんでした。')
            self._user, self._hwnd = user, hwnd
            self._ready.set()
            msg = w.MSG()
            while user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(msg)); user.DispatchMessageW(ctypes.byref(msg))
        finally:
            if hwnd:
                shell.Shell_NotifyIconW(2, ctypes.byref(data))
                user.DestroyWindow(hwnd)
            user.UnregisterClassW(class_name, instance)
            if owned_icon:user.DestroyIcon(icon)
