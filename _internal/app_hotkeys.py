"""Validated shortcut mapping and a Windows RegisterHotKey message pump."""
from __future__ import annotations
import ctypes
import queue
import sys
import threading

from gamebar_recording import KEYS, MODIFIERS, parse_hotkey

ACTION_LABELS = {'live':'録音を開始', 'gamebar':'Game Bar連動録画を開始', 'stop':'停止'}
ORDER = ('ctrl','shift','alt','win')


def _ignore_event(_value):
    """Do not retain application callbacks while the native worker exits."""


def normalize_hotkey(value):
    value = str(value).strip().lower()
    if not value:
        return ''
    if value.startswith('f') and value[1:].isdigit() and 1<=int(value[1:])<=12:
        return value
    parse_hotkey(value)
    pieces = [piece.strip() for piece in value.split('+')]
    return '+'.join([key for key in ORDER if key in pieces]+
                    [key for key in pieces if key not in MODIFIERS])


def validate_shortcuts(mapping, recording_shortcut='win+alt+r'):
    if not isinstance(mapping,dict) or any(key not in ACTION_LABELS for key in mapping):
        raise ValueError('ショートカット設定を確認してください。')
    values = [normalize_hotkey(mapping.get(key,'')) for key in ACTION_LABELS]
    used = [value for value in values if value]
    if len(used) != len(set(used)):
        raise ValueError('同じショートカットを複数の操作には割り当てられません。')
    if any(value=='f12' or value.endswith('+f12') for value in used):
        raise ValueError('F12はWindowsで予約されています。別のキーを選択してください。')
    if normalize_hotkey(recording_shortcut) in used:
        raise ValueError('Game Bar側の録画開始キーとアプリのショートカットは別のキーにしてください。')


def tk_shortcut(event, pressed, allow_function_only=True):
    """Read actual key events; accept left/right modifiers and function keys."""
    symbol = str(event.keysym).lower()
    modifier = {'control_l':'ctrl','control_r':'ctrl','shift_l':'shift','shift_r':'shift',
                'alt_l':'alt','alt_r':'alt','meta_l':'alt','meta_r':'alt',
                'super_l':'win','super_r':'win','win_l':'win','win_r':'win'}.get(symbol)
    if modifier:
        pressed.add(modifier)
        return None
    aliases = {'return':'enter','escape':'esc'}
    key = aliases.get(symbol,symbol)
    if key not in KEYS or key in MODIFIERS:
        return None
    state = int(event.state)
    modifiers = set(pressed)
    for name,mask in (('shift',1),('ctrl',4),('alt',0x20008),('win',0x40)):
        if state & mask:
            modifiers.add(name)
    if not modifiers:
        if allow_function_only and key.startswith('f') and key[1:].isdigit() and 1<=int(key[1:])<=11:
            return key
        return None
    return normalize_hotkey('+'.join([name for name in ORDER if name in modifiers]+[key]))


class WindowsHotkeys:
    def __init__(self, mapping, callback, report):
        self.mapping, self.callback, self.report = dict(mapping), callback, report
        self.commands = queue.Queue()
        self.paused = False
        self.thread = None
        self._closing = False
        if sys.platform == 'win32' and any(mapping.values()):
            self.thread = threading.Thread(target=self._run,daemon=True,name='recording-hotkeys')
            self.thread.start()

    def update(self, mapping):
        if self._closing:
            return
        self.mapping = dict(mapping)
        if self.thread is None and sys.platform=='win32' and any(mapping.values()):
            self.thread=threading.Thread(target=self._run,daemon=True,name='recording-hotkeys')
            self.thread.start()
        self._command('update', self.mapping)

    def suspend(self, paused=True):
        if self._closing:
            return
        self.paused = bool(paused)
        self._command('pause', self.paused)

    def close(self):
        self._closing = True
        # These callbacks can own the whole Tk application. Release them on the
        # caller's GUI thread even if a native API prevents timely termination.
        self.callback = _ignore_event
        self.report = _ignore_event
        thread = self.thread
        if thread is threading.current_thread():
            self.commands.put(('close', None, threading.Event()))
            return False
        self._command('close',None)
        if thread is not None:
            # The close acknowledgment precedes UnregisterHotKey in _run's
            # finally block. Wait for that cleanup before destroying Tk.
            thread.join(timeout=1.0)
        return thread is None or not thread.is_alive()

    def _command(self, kind, data):
        if self.thread is not None and self.thread.is_alive():
            ready = threading.Event()
            self.commands.put((kind,data,ready))
            ready.wait(.4)

    def _run(self):
        from ctypes import wintypes
        registered = {}
        try:
            api = ctypes.WinDLL('user32',use_last_error=True)
            api.RegisterHotKey.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_uint32,ctypes.c_uint32]
            api.RegisterHotKey.restype=wintypes.BOOL
            api.UnregisterHotKey.argtypes=[wintypes.HWND,ctypes.c_int]
            api.PeekMessageW.argtypes=[ctypes.POINTER(wintypes.MSG),wintypes.HWND,
                                      ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32]
            api.PeekMessageW.restype=wintypes.BOOL
            message=wintypes.MSG()
            # Create the thread's message queue before anyone suspends it.
            api.PeekMessageW(ctypes.byref(message),None,0,0,0)
            mapping=dict(self.mapping); paused=False
            def register():
                for identity in list(registered):
                    api.UnregisterHotKey(None,identity)
                registered.clear()
                if paused:
                    return
                for identity,action in enumerate(ACTION_LABELS,1):
                    value=normalize_hotkey(mapping.get(action,''))
                    if not value:
                        continue
                    pieces=value.split('+')
                    flags=0x4000  # MOD_NOREPEAT
                    for modifier,flag in (('alt',1),('ctrl',2),('shift',4),('win',8)):
                        if modifier in pieces:
                            flags |= flag
                    if api.RegisterHotKey(None,identity,flags,KEYS[pieces[-1]]):
                        registered[identity]=action
                    else:
                        self.report(f'{ACTION_LABELS[action]}: {value} を登録できません。他のアプリとの重複を確認してください。')
            register()
            while True:
                try:
                    kind,data,ready=self.commands.get(timeout=.025)
                except queue.Empty:
                    kind=None
                if kind:
                    if kind=='close':
                        ready.set();break
                    if kind=='update':mapping=dict(data)
                    if kind=='pause':paused=data
                    register();ready.set()
                while api.PeekMessageW(ctypes.byref(message),None,0,0,1):
                    if message.message==0x0312 and not paused and message.wParam in registered:
                        self.callback(registered[message.wParam])
        except (OSError,AttributeError,ValueError) as exc:
            self.report('ショートカットを登録できません: '+str(exc))
        finally:
            for identity in registered:
                api.UnregisterHotKey(None,identity)
