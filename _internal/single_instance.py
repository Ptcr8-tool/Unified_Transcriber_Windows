"""Allow one GUI per Windows session and return repeat launches to its window.

The kernel keeps the named object alive only while a process has a handle, so a
crash does not leave a stale lock file. CLI tools and the installer do not use it.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

_MUTEX_NAME = r'Local\UnifiedTranscriber.GUI.Instance.v1'
_WINDOW_PROPERTY = 'UnifiedTranscriber.GUI.Instance.v1'
_ERROR_ALREADY_EXISTS = 183


class WindowsInstance:
    def __init__(self, mutex_name=_MUTEX_NAME, property_name=_WINDOW_PROPERTY):
        self.mutex_name = mutex_name
        self.property_name = property_name
        self.handle = None
        self.window = None
        self.event = None
        self.root = None
        self.restore_callback = None
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.OpenEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
        self.user.SetPropW.restype = wintypes.BOOL
        self.user.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        self.user.GetPropW.restype = wintypes.HANDLE
        self.user.RemovePropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        self.user.RemovePropW.restype = wintypes.HANDLE
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.IsIconic.restype = wintypes.BOOL
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.SetForegroundWindow.restype = wintypes.BOOL
        self.user.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]

    def acquire(self):
        """Retain a handle if this process created the named object."""
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, self.mutex_name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == _ERROR_ALREADY_EXISTS:
            # Do not keep the object alive after the existing process exits.
            self.kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def attach(self, root):
        """Mark the actual top-level HWND, not the Tk child HWND."""
        root.update_idletasks()
        window = self.user.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
        if not window or not self.user.SetPropW(window, self.property_name, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        self.window = window
        self.root = root
        self.event = self.kernel.CreateEventW(None, False, False, self.mutex_name + '.Activate')
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        self._poll_activation()

    def set_restore(self, callback):
        self.restore_callback = callback

    def _poll_activation(self):
        if not self.event or not self.root:
            return
        if self.kernel.WaitForSingleObject(self.event, 0) == 0:
            if self.restore_callback:
                self.restore_callback()
            else:
                self.root.deiconify()
                self.root.lift()
        self.root.after(150, self._poll_activation)

    def find_window(self):
        """Find only windows marked by another instance of this application."""
        result = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL

        @callback_type
        def visit(window, _data):
            if self.user.GetPropW(window, self.property_name):
                pid = wintypes.DWORD()
                self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
                if pid.value and pid.value != os.getpid():
                    result.append(window)
                    return False
            return True

        self.user.EnumWindows(visit, 0)
        return result[0] if result else None

    def activate(self, window):
        # The first process can disappear between enumeration and activation.
        if not self.user.IsWindow(window) or not self.user.GetPropW(window, self.property_name):
            return False
        event = self.kernel.OpenEventW(0x0002, False, self.mutex_name + '.Activate')
        if event:
            try:
                if self.kernel.SetEvent(event):
                    return True
            finally:
                self.kernel.CloseHandle(event)
        if self.user.IsIconic(window):
            self.user.ShowWindow(window, 9)  # SW_RESTORE
        if not self.user.SetForegroundWindow(window):
            # Windows may deny foreground activation; draw attention to its taskbar item.
            self.user.FlashWindow(window, True)
        return True

    def notify_wait_timeout(self):
        self.user.MessageBoxW(None, 'アプリは既に起動しています。タスクバーから画面を開いてください。\n'
                                  '起動中の場合は少し待ってから再度お試しください。',
                              '文字起こし統合スタジオ', 0x40)

    def close(self):
        if self.event:
            self.kernel.CloseHandle(self.event)
            self.event = None
        self.root = None
        if self.window:
            self.user.RemovePropW(self.window, self.property_name)
            self.window = None
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _NoopInstance:
    def attach(self, _root):
        pass

    def close(self):
        pass

    def set_restore(self, _callback):
        pass


def enter_instance(*, factory=WindowsInstance, timeout=8.0, interval=0.1,
                   clock=time.monotonic, sleep=time.sleep):
    """Return the owner guard, or None when another window was activated.

    A second launch may arrive while the first is still creating its window.
    Retry without retaining a mutex handle, allowing takeover if the first exits.
    """
    if sys.platform != 'win32':
        return _NoopInstance()
    guard = factory()
    try:
        if guard.acquire():
            return guard
        deadline = clock() + timeout
        while clock() < deadline:
            window = guard.find_window()
            if window and guard.activate(window):
                guard.close()
                return None
            # If startup failed or was cancelled, the new launch may take over.
            if guard.acquire():
                return guard
            sleep(interval)
        guard.notify_wait_timeout()
        guard.close()
        return None
    except BaseException:
        guard.close()
        raise
