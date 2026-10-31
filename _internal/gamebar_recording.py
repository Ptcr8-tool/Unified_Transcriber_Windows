"""Coordinate Xbox Game Bar hotkeys and an independent recording stream.

No screen pixels are captured here; Game Bar is an external Windows recorder.
Sending a shortcut cannot prove Game Bar actually started. Only a finalized MP4
created after the trigger is treated as evidence of video capture.
"""
from __future__ import annotations
import ctypes
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from output_locations import default_directory

KEYS = {'win':0x5B, 'ctrl':0x11, 'alt':0x12, 'shift':0x10,
        'space':0x20, 'tab':0x09, 'enter':0x0D, 'esc':0x1B}
for n in range(1,13): KEYS[f'f{n}']=0x6F+n
for c in 'abcdefghijklmnopqrstuvwxyz0123456789': KEYS[c]=ord(c.upper())
MODIFIERS = frozenset({'win','ctrl','alt','shift'})


def parse_hotkey(value: str) -> tuple[int,...]:
    pieces=[p.strip().lower() for p in value.split('+')]
    if len(pieces)<2 or len(pieces)!=len(set(pieces)) or any(p not in KEYS for p in pieces):
        raise ValueError('ホットキーは win+alt+r / ctrl+shift+f9 の形式で指定してください。')
    modifiers=[p for p in pieces if p in MODIFIERS]
    regular=[p for p in pieces if p not in MODIFIERS]
    if not modifiers or len(regular)!=1:
        raise ValueError('1つ以上の修飾キーと、1つの通常キーを指定してください。')
    return tuple(KEYS[p] for p in modifiers+regular)


def _send_key_sequence(keys, key_event, *, sleep=None, clock=None):
    """Hold modifiers before pressing the action key, and release every sent key."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    pressed = []
    try:
        for key in keys[:-1]:
            key_event(key, False)
            pressed.append(key)
            sleep(.045)
        sleep(.20)
        key_event(keys[-1], False)
        pressed.append(keys[-1])
        triggered = clock()
        sleep(.10)
    finally:
        release_errors = []
        for key in reversed(pressed):
            try:
                key_event(key, True)
            except OSError as exc:
                release_errors.append(exc)
        if release_errors:
            raise release_errors[0]
    return triggered


def send_hotkey(value: str, *, hwnd: int|None=None) -> float:
    keys=parse_hotkey(value)
    if os.name != 'nt':
        raise OSError('Game BarのホットキーはWindows専用です。')
    from ctypes import wintypes
    user32=ctypes.WinDLL('user32',use_last_error=True)
    # HWND is pointer-sized. Without argtypes ctypes silently truncates HWND on 64-bit Windows.
    for function in (user32.IsWindow,user32.IsIconic,user32.SetForegroundWindow):
        function.argtypes=[wintypes.HWND]
        function.restype=wintypes.BOOL
    user32.GetForegroundWindow.restype=wintypes.HWND
    user32.GetAsyncKeyState.argtypes=[ctypes.c_int]
    user32.GetAsyncKeyState.restype=ctypes.c_int16
    user32.MapVirtualKeyW.argtypes=[ctypes.c_uint32,ctypes.c_uint32]
    user32.MapVirtualKeyW.restype=ctypes.c_uint32
    if hwnd:
        if not user32.IsWindow(int(hwnd)):
            raise RuntimeError('録画対象ウィンドウが閉じられています。選択し直してください。')
        if user32.IsIconic(int(hwnd)):
            raise RuntimeError('録画対象ウィンドウが最小化されています。元に戻してください。')
        if user32.GetForegroundWindow()!=int(hwnd) and not user32.SetForegroundWindow(int(hwnd)):
            raise RuntimeError('録画対象を前面にできませんでした。対象アプリを手前にして再試行してください。')
        time.sleep(.35)
    # Do not release keys physically held by the user, or add a shortcut to
    # an unrelated Ctrl/Shift/Alt/Win combination.
    checked = set(keys) | {0x5B, 0x5C, 0x10, 0x11, 0x12}
    deadline=time.monotonic()+1.2
    while any(user32.GetAsyncKeyState(key) & 0x8000 for key in checked):
        if time.monotonic()>=deadline:
            raise RuntimeError('修飾キーまたは録画キーが押されたままです。キーを離して再試行してください。')
        time.sleep(.03)
    # Key down in the requested order; key up in reverse order even on failure.
    # VK_PACKET is not used: Game Bar shortcuts require actual virtual keys.
    ULONG_PTR=ctypes.c_size_t
    WORD, DWORD, LONG = ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int32
    class KEYBDINPUT(ctypes.Structure):
        _fields_=[('wVk',WORD),('wScan',WORD),
                  ('dwFlags',DWORD),('time',DWORD),('dwExtraInfo',ULONG_PTR)]
    class MOUSEINPUT(ctypes.Structure):
        _fields_=[('dx',LONG),('dy',LONG),('mouseData',DWORD),('dwFlags',DWORD),
                  ('time',DWORD),('dwExtraInfo',ULONG_PTR)]
    class HARDWAREINPUT(ctypes.Structure):
        _fields_=[('uMsg',DWORD),('wParamL',WORD),('wParamH',WORD)]
    class INPUTUNION(ctypes.Union):
        _fields_=[('ki',KEYBDINPUT),('mi',MOUSEINPUT),('hi',HARDWAREINPUT)]
    class INPUT(ctypes.Structure):
        _fields_=[('type',DWORD),('union',INPUTUNION)]
    user32.SendInput.argtypes=(wintypes.UINT,ctypes.POINTER(INPUT),ctypes.c_int)
    user32.SendInput.restype=wintypes.UINT
    def key_event(vk,up):
        scan=user32.MapVirtualKeyW(vk,0)
        flags=(0x0008 if scan else 0) | (0x0002 if up else 0)
        if vk in (0x5B,0x5C) or scan & 0xFF00:
            flags |= 0x0001
        item=INPUT()
        item.type=1
        item.union.ki=KEYBDINPUT(0 if scan else vk,scan & 0xFF,flags,0,0)
        if user32.SendInput(1,ctypes.byref(item),ctypes.sizeof(INPUT))!=1:
            raise OSError('Windowsへのショートカット送信に失敗しました（管理者権限等を確認）。')
    return _send_key_sequence(keys,key_event)


def captures_directory() -> Path:
    """Best-effort default; supports redirected folders via manual GUI override."""
    if os.name != 'nt':return Path.home()/'Videos'/'Captures'
    try:
        # Known Folder Videos: {18989B1D-99B5-455B-841C-AB7C74E4DDFC}
        import uuid
        from ctypes import wintypes
        class GUID(ctypes.Structure):
            _fields_=[('Data1',wintypes.DWORD),('Data2',wintypes.WORD),('Data3',wintypes.WORD),('Data4',ctypes.c_ubyte*8)]
        data=uuid.UUID('18989b1d-99b5-455b-841c-ab7c74e4ddfc').bytes_le
        guid=GUID.from_buffer_copy(data)
        result=ctypes.c_wchar_p()
        shell32=ctypes.WinDLL('shell32')
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid),0,None,ctypes.byref(result))==0 and result.value:
            path=Path(result.value)/'Captures'
            ctypes.WinDLL('ole32').CoTaskMemFree(ctypes.cast(result,ctypes.c_void_p))
            return path
    except (OSError,ValueError,AttributeError):pass
    return Path.home()/'Videos'/'Captures'


def snapshot_mp4(folder:Path)->dict[str,tuple[int,int]]:
    if not folder.is_dir():return {}
    answer={}
    for file in folder.glob('*.mp4'):
        try:
            info=file.stat()
            answer[str(file.resolve())]=(info.st_mtime_ns,info.st_size)
        except OSError:continue
    return answer


def find_completed_capture(folder:Path, before:dict[str,tuple[int,int]],
                           after_timestamp:float, *,timeout:float=22.0,
                           stop=None,sleep=time.sleep, clock=time.monotonic) -> Path|None:
    """Find one NEW stable Game Bar clip. Reject ambiguity to avoid wrong video."""
    deadline=clock()+max(0,timeout)
    stable={}  # path -> (size, consecutive checks)
    while True:
        ready=[]
        for path_str, (modified_ns,size) in snapshot_mp4(folder).items():
            prev=before.get(path_str)
            if (prev is not None and prev == (modified_ns,size)) or modified_ns/1e9 < after_timestamp-3:
                continue
            if size < 1024:continue
            old_size,count=stable.get(path_str,(0,0))
            count=(count+1) if size==old_size else 1
            stable[path_str]=(size,count)
            if count>=3:ready.append(Path(path_str))
        if len(ready)==1:return ready[0]
        if len(ready)>1:
            raise RuntimeError('新しいMP4が複数見つかりました。誤結合を防ぐため自動選択を中止します。')
        if clock()>=deadline or (stop is not None and stop.is_set()):return None
        sleep(.5)


@dataclass(frozen=True)
class GameBarOptions:
    shortcut:str='win+alt+r'
    target_hwnd:int|None=None
    source:str='mix'
    captures:Path=Path.home()/'Videos'/'Captures'
    destination:Path=field(default_factory=lambda: default_directory('recordings'))
    auto_transcribe:bool=True
    create_synced_video:bool=True
    audio_format:str='wav'
    mic_device_id:str=''
    loopback_device_id:str=''
    channel_mode:str='mixed'
    recording_channels:int=2
    target_title:str=''
    realtime_transcription:bool=False
    chunk_seconds:int=20
    def validate(self):
        parse_hotkey(self.shortcut)
        if not isinstance(self.realtime_transcription,bool):
            raise ValueError('ライブ文字起こしの設定を確認してください。')
        if self.chunk_seconds not in (10,20,30,60):
            raise ValueError('認識区間は10/20/30/60秒から選択してください。')
        if self.source not in ('mic','loopback','mix'):
            raise ValueError('録音入力元を選択してください。')
        if self.audio_format not in ('wav','mp3','m4a','flac'):
            raise ValueError('録音保存形式を確認してください。')
        if self.recording_channels not in (1,2) or self.channel_mode not in ('mixed','split_lr'):
            raise ValueError('録音チャンネルを確認してください。')
        if self.channel_mode=='split_lr' and (self.source!='mix' or self.recording_channels!=2):
            raise ValueError('L/R分離録音にはマイク＋PC再生音と2チャンネルが必要です。')
        if self.target_hwnd is not None and self.target_hwnd<=0:
            raise ValueError('録画対象のウィンドウを選択してください。')
        if self.destination.is_file():raise ValueError('録音保存先にはフォルダーを指定してください。')
        if self.captures.is_file():raise ValueError('Game Barの保存先にはフォルダーを指定してください。')
