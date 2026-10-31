"""Single-file, per-user installer and maintenance wizard. No shell scripting."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import APP_VERSION, Config
from installation import (InstallationError, get_verified_maintenance_path, inspect_installation, install_package,
                          uninstall_installation, validate_package, version_tuple)
from portable_runtime import MAX_CONFIG_BYTES, reset_dll_search_path, runtime_environment, setup_selected
from studio_ui import bind_variable_trace
from ui_controls import ToggleSwitch, install_switch_style
from wheel_support import install_wheel_routing
from setup_context import get_context, packaged_setup, resource_folder, setup_executable
from output_locations import (default_output_root, output_root, set_output_root,
                              validate_output_root)
from cuda_probe import CudaAvailability, probe_cuda
from bulk_catalog import MODEL_CHOICES, MODEL_LABELS
from setup_progress import PREFIX as SETUP_PROGRESS_PREFIX, parse_setup_line


def resources() -> tuple[Path, str]:
    folder = resource_folder(Path(__file__).resolve().parent)
    info = json.loads((folder / 'BUILD_INFO.json').read_text(encoding='utf-8'))
    if not isinstance(info, dict):
        raise InstallationError('セットアップの検証情報が不正です。再ダウンロードしてください。')
    digest = info.get('payload_sha256', '')
    if info.get('version') != APP_VERSION or not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise InstallationError('セットアップのバージョン・検証情報が不正です。再ダウンロードしてください。')
    return folder / 'payload.zip', digest


def default_target() -> Path:
    executable = setup_executable()
    if packaged_setup() and (executable.parent/'installation.json').is_file():
        return executable.parent
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local'))) / 'Programs' / 'UnifiedTranscriber'


def wait_for_process(pid: int | None, target: Path, timeout: float = 120) -> None:
    """Wait for the updater's own app; never kill another process."""
    if pid is None:
        return
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        raise InstallationError('終了待ちのプロセス指定が不正です。')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise InstallationError('終了待ちの時間が不正です。')
    if os.name != 'nt':
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        raise InstallationError('アプリを終了してから再実行してください。')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise InstallationError('アプリの終了を確認できません。閉じてから再実行してください。')
    try:
        # A launcher can retain a handle to its already-exited child. Windows
        # may then open the process object but cannot query its former image.
        status = kernel.WaitForSingleObject(handle, 0)
        if status == 0:
            return
        if status != 0x102:  # WAIT_TIMEOUT: still running
            raise InstallationError('アプリの終了状態を安全に確認できません。')
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            # It may also exit between the nonblocking probe and image query.
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return
            raise InstallationError('終了待ちのアプリを確認できません。')
        expected_root = target.resolve()
        process_path = Path(path.value).resolve()
        # Only the installed launcher, its bundled Python, or its own setup may
        # be the wait target. Do not trust an unrelated exe merely below root.
        allowed = {expected_root / 'UnifiedTranscriber.exe', expected_root / 'UnifiedTranscriberSetup.exe',
                   expected_root / 'Runtime/python.exe', expected_root / 'Runtime/pythonw.exe'}
        if process_path not in allowed:
            raise InstallationError('このインストール先以外のプロセスは待機しません。')
        if kernel.WaitForSingleObject(handle, int(timeout*1000)) != 0:
            raise InstallationError('アプリがまだ実行中です。終了してから再実行してください。')
    finally:
        kernel.CloseHandle(handle)


def maintenance_owner_pid(current: Path) -> int:
    """Wait for the verified onefile bootloader, not just its Python child.

    PyInstaller's parent removes its extracted resources after the Python
    child exits. On Windows that parent can still lock the installed exe.
    """
    context = get_context()
    if context is not None:
        from setup_context import process_image
        if (context.owner_pid != os.getppid() or current.resolve() != context.executable
                or process_image(context.owner_pid) != context.executable):
            raise InstallationError('セットアップの起動元を安全に確認できません。')
        return context.owner_pid
    import ctypes
    from ctypes import wintypes
    parent = os.getppid()
    if parent <= 0 or parent == os.getpid():
        raise InstallationError('セットアップの起動元を確認できません。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000,False,parent)
    if not handle:
        raise InstallationError('セットアップの起動元を安全に確認できません。閉じてから再実行してください。')
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):
            raise InstallationError('セットアップの起動元を安全に確認できません。')
        # A verified different image is an onedir/non-onefile parent. An
        # unqueryable parent is never treated as that fallback.
        return parent if Path(image.value).resolve() == current.resolve() else os.getpid()
    finally:
        kernel.CloseHandle(handle)


def relaunch_outside(target: Path, argv: list[str]) -> bool:
    """A running Windows executable cannot remove/replace itself in its folder."""
    if os.name != 'nt' or not packaged_setup():
        return False
    current = setup_executable()
    if not current.is_relative_to(target.resolve()):
        return False
    owner_pid = maintenance_owner_pid(current)
    folder = Path(tempfile.mkdtemp(prefix='UnifiedTranscriber-Setup-'))
    copied = folder / 'UnifiedTranscriberSetup.exe'
    shutil.copyfile(current, copied)
    with copied.open('rb') as copied_file, current.open('rb') as current_file:
        same = hashlib.file_digest(copied_file, 'sha256').digest() == hashlib.file_digest(current_file, 'sha256').digest()
    if not same:
        shutil.rmtree(folder, ignore_errors=True)
        raise InstallationError('一時セットアップのコピーに失敗しました。')
    env = runtime_environment(target)
    if getattr(sys, 'frozen', False):
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    reset_dll_search_path()
    try:
        subprocess.Popen([str(copied), '--target', str(target), *argv, '--wait-setup-pid', str(owner_pid)], shell=False, env=env,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return True


def seed_preferences(target: Path, *, minimal: bool, hardware: str, recording_ready: bool,
                     output_location: str = '', model: str = 'large-v3-turbo') -> bool:
    path = target / 'Data' / 'settings.json'
    # Existing settings, including newer/unknown fields or malformed JSON the
    # user may want to recover, belong to the user. Never reserialize them here.
    if os.path.lexists(path):
        return False
    data = path.parent
    if data.is_symlink() or (os.name == 'nt' and data.exists()
            and data.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise InstallationError('設定の保存先がリンク・ジャンクションのため移行しませんでした。')
    data.mkdir(parents=True, exist_ok=True)
    legacy = (Path(os.environ['APPDATA']) if os.environ.get('APPDATA') else Path.home()/'.config') / 'UnifiedTranscriberV12/settings.json'
    if legacy.is_file():
        if legacy.is_symlink() or legacy.stat().st_size > MAX_CONFIG_BYTES:
            raise InstallationError('以前の設定を安全にコピーできませんでした。元の設定は保持しています。')
        # Exclusive creation also protects a concurrently created destination
        # and a dangling symlink. Keep original bytes and unknown fields.
        with legacy.open('rb') as source:
            payload = source.read(MAX_CONFIG_BYTES + 1)
        if len(payload) > MAX_CONFIG_BYTES:
            raise InstallationError('以前の設定が大きすぎるため移行しませんでした。元の設定は保持しています。')
        _write_new_settings(path, payload)
        return False
    cfg = Config(profile='faster-res' if minimal else Config().profile,
                 device=hardware if minimal else 'auto', capture_setup_complete=recording_ready,
                 onboarding_seen=minimal,
                 output_root=validate_output_root(output_location), model_faster=model)
    cfg.validate()
    values = asdict(cfg)
    values.pop('hf_token_json', None)
    return _write_new_settings(path, (json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8'))


def update_setup_preferences(target: Path, *, minimal: bool, hardware: str,
                             recording_ready: bool, model: str, expected_sha256: str,
                             initial_preferences: bool = True) -> None:
    """Record successful initial setup without discarding unknown preferences."""
    from output_locations import _read_settings
    path = target / 'Data/settings.json'
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        return  # An app opened during setup may already have saved user choices.
    values = _read_settings(path)
    if minimal and initial_preferences:
        values.update(profile='faster-res', device=hardware, model_faster=model)
    # The welcome dialog requests the recommended environment and model.  Mark
    # it complete only after their actual setup succeeded, and only if the
    # current preference points to the model just installed. Existing users'
    # choices must not be changed merely because they ran the maintenance tool.
    defaults = Config()
    if (minimal and values.get('profile', defaults.profile) == 'faster-res'
            and values.get('model_faster', defaults.model_faster) == model
            and values.get('device', defaults.device) in ('auto', hardware)):
        values['onboarding_seen'] = True
    if recording_ready:
        values['capture_setup_complete'] = True
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
            prefix='.setup-preferences-', suffix='.tmp', delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump(values, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            return
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def _write_new_settings(path: Path, payload: bytes) -> bool:
    """Create once; a concurrent app preference save always wins."""
    try:
        handle = path.open('xb')
    except FileExistsError:
        return False
    identity = os.fstat(handle.fileno())
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and not path.is_symlink():
                path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


class InstallerWindow:
    def __init__(self, root: tk.Tk, args, package_info=None):
        self.root, self.args = root, args
        self.package_info = package_info
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.phase = 'idle'
        self.closed = False
        self.control_states = {}
        self.operation_target = None
        self.open_target = None
        self.preference_target = None
        self.installed = False
        self.poll_job = None
        self.auto_start_job = None
        self.output_modified = False
        self.hardware_modified = False
        self.cuda_probe_started = False
        self.cuda_received = False
        root.title(f'文字起こし統合スタジオ セットアップ v{APP_VERSION}')
        root.geometry('850x760')
        root.minsize(780, 600)
        root.configure(bg='#f3f6fb')
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        font = ('Yu Gothic UI' if os.name == 'nt' else 'Noto Sans CJK JP', 10)
        style.configure('.', font=font, background='#ffffff', foreground='#17283f')
        style.configure('TFrame', background='#ffffff')
        style.configure('Title.TLabel', font=(font[0], 21, 'bold'))
        style.configure('Muted.TLabel', foreground='#61738a')
        style.configure('Section.TLabel', font=(font[0], 11, 'bold'))
        style.configure('Primary.TButton', background='#2563eb', foreground='white', padding=(18, 10))
        style.map('Primary.TButton', background=[('active','#1d4ed8')])
        install_switch_style(root, style, {'disabled':'#e6ebf2','green':'#209760','switch_off':'#b9c2cd',
            'surface':'#ffffff','ink':'#17283f','muted':'#61738a'})
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True, padx=16, pady=16)
        ttk.Label(frame, text='文字起こし統合スタジオ', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text=f'セットアップ v{APP_VERSION}  ·  ① 保存先と構成 → ② インストール → ③ 完了',
                  style='Muted.TLabel').pack(anchor='w', pady=(5, 17))
        # Pin actions before reserving the resizable content area so the form
        # can never push installation/close buttons below a small screen.
        footer = ttk.Frame(frame)
        footer.pack(side='bottom', fill='x', pady=(15, 0))
        self.action = ttk.Button(footer,text='インストールする',style='Primary.TButton',command=self.begin)
        self.action.pack(side='right')
        self.cancel_button = ttk.Button(footer,text='キャンセル',command=self.close)
        self.cancel_button.pack(side='right',padx=(0,8))
        self.open_button = ttk.Button(footer,text='アプリを起動',command=self.open_app)
        self.log = tk.Text(frame,height=3,wrap='word',state='disabled',font=(font[0],9),
            relief='solid',borderwidth=1,background='#f8fafd',foreground='#17283f')
        self.log.pack(side='bottom',fill='x',pady=(6,0))
        status_area = ttk.Frame(frame)
        status_area.pack(side='bottom',fill='x',pady=(8,0))
        self.status = tk.StringVar(root, '保存先と必要な構成を選び、開始してください。')
        ttk.Label(status_area,textvariable=self.status,wraplength=740).pack(anchor='w',pady=(0,6))
        self.progress = ttk.Progressbar(status_area,mode='indeterminate')
        self.progress.pack(fill='x')
        scroll_area = ttk.Frame(frame)
        scroll_area.pack(fill='both',expand=True)
        self.canvas = tk.Canvas(scroll_area,background='#ffffff',highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(scroll_area,orient='vertical',command=self.canvas.yview)
        self.scrollbar.pack(side='right',fill='y')
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side='left',fill='both',expand=True)
        body = ttk.Frame(self.canvas,padding=(0,0,10,8))
        self.form_window = self.canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda _event:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda event:self.canvas.itemconfigure(self.form_window,width=event.width))
        self.form = ttk.Frame(body)
        self.form.pack(fill='x')
        ttk.Label(self.form, text='インストール先', style='Section.TLabel').pack(anchor='w')
        self.target = tk.StringVar(root, str(args.target or default_target()))
        row = ttk.Frame(self.form)
        row.pack(fill='x', pady=(5, 4))
        self.path_entry = ttk.Entry(row, textvariable=self.target)
        self.path_entry.pack(side='left', fill='x', expand=True)
        self.path_entry.bind('<FocusOut>', lambda _e:self.refresh())
        self.browse = ttk.Button(row, text='フォルダーを選ぶ…', command=self.choose_target)
        self.browse.pack(side='left', padx=(8, 0))
        self.detected = tk.StringVar(root)
        self.detected_label = ttk.Label(self.form, textvariable=self.detected, wraplength=740, style='Muted.TLabel')
        self.detected_label.pack(anchor='w', pady=(0, 10))
        self.mode = tk.StringVar(root, 'uninstall' if args.uninstall else 'auto')
        self.maintenance = ttk.Frame(self.form)
        self.maintenance.pack(fill='x')
        self.repair_button = ttk.Radiobutton(self.maintenance, text='修復', variable=self.mode, value='repair')
        self.uninstall_button = ttk.Radiobutton(self.maintenance, text='アンインストール', variable=self.mode, value='uninstall')
        self.repair_button.pack(side='left', padx=(0, 20))
        self.uninstall_button.pack(side='left')
        bind_variable_trace(self.root, self.mode, 'write', lambda *_:self.update_action())
        self.options = ttk.Frame(self.form)
        self.options.pack(fill='x', pady=(12, 0))
        ttk.Label(self.options, text='ログ・録音・文字起こしなどの共通保存先', style='Section.TLabel').pack(anchor='w')
        self.output_location = tk.StringVar(root, str(default_output_root()))
        row = ttk.Frame(self.options)
        row.pack(fill='x', pady=(5, 4))
        self.output_entry = ttk.Entry(row, textvariable=self.output_location)
        self.output_entry.pack(side='left', fill='x', expand=True)
        self.output_entry.bind('<KeyRelease>', lambda _e:setattr(self, 'output_modified', True))
        ttk.Button(row, text='選択…', command=self.choose_output).pack(side='left', padx=(8, 0))
        ttk.Button(row, text='ドキュメントに戻す', command=self.default_output).pack(side='left', padx=(8, 0))
        ttk.Label(self.options, text='用途別のサブフォルダーを作成します。既存の出力ファイルは移動しません。',
                  wraplength=740, style='Muted.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Label(self.options, text='導入するもの', style='Section.TLabel').pack(anchor='w')
        self.register = tk.BooleanVar(root, True)
        self.shortcut = tk.BooleanVar(root, True)
        self.recording = tk.BooleanVar(root, not args.update)
        self.minimal = tk.BooleanVar(root, False)
        # Opt in for this run; leave existing installations and settings intact.
        self.launch_after_setup = tk.BooleanVar(root, False)
        for text, variable in (('プログラム一覧に登録（アンインストールの案内）',self.register),
                ('スタートメニューに追加',self.shortcut),('録音環境をセットアップ',self.recording),
                ('推奨AIセット（② faster-whisper + Resemblyzer）',self.minimal),
                ('完了後にアプリを自動で起動する',self.launch_after_setup)):
            ToggleSwitch(self.options, text=text, variable=variable).pack(anchor='w')
        self.cuda_status = tk.StringVar(root, 'CUDAの利用見込みを確認しています…（Python・AIライブラリの事前導入は不要）')
        ttk.Label(self.options, textvariable=self.cuda_status, wraplength=740,
                  style='Muted.TLabel').pack(anchor='w', pady=(8, 4))
        row = ttk.Frame(self.options)
        row.pack(fill='x', pady=(6, 4))
        ttk.Label(row, text='AIの実行環境').pack(side='left', padx=(0, 15))
        self.hardware = tk.StringVar(root, 'cpu')
        ttk.Radiobutton(row,text='CPU',variable=self.hardware,value='cpu',
            command=lambda:setattr(self, 'hardware_modified', True)).pack(side='left',padx=(0,20))
        ttk.Radiobutton(row,text='NVIDIA GPU（CUDA）',variable=self.hardware,value='cuda',
            command=lambda:setattr(self, 'hardware_modified', True)).pack(side='left')
        row = ttk.Frame(self.options)
        row.pack(fill='x', pady=(6, 4))
        ttk.Label(row, text='②の音声認識モデル').pack(side='left', padx=(0, 15))
        self.model = tk.StringVar(root, MODEL_LABELS['large-v3-turbo'])
        self.model_choices = {MODEL_LABELS[name]:name for name in MODEL_CHOICES['faster']}
        self.model_menu = ttk.Combobox(row, textvariable=self.model, state='disabled',
            values=tuple(self.model_choices), width=40)
        self.model_menu.pack(side='left', fill='x', expand=True)
        ttk.Label(self.options, text='Pythonは同梱されています。録音環境とAIはインターネットから追加します。\n'
            'AIセットは数GBの空き容量が必要です。OFFでも、後からアプリ内で導入できます。',
            wraplength=740,style='Muted.TLabel').pack(anchor='w',pady=(6,8))
        self.refresh()
        self.cuda_probe_started = True
        cuda_messages = self.messages  # A closing Tk window must not be retained by a worker.
        def check_cuda():
            try:
                availability = probe_cuda()
            except Exception:
                availability = CudaAvailability('unknown', 'CUDAの利用見込みを確認できませんでした。',
                    'CPUで導入できます。NVIDIAドライバーを確認後、CUDAを手動で選ぶこともできます。')
            cuda_messages.put(('cuda', availability))
        threading.Thread(target=check_cuda, daemon=True).start()
        install_wheel_routing(root._root())
        self.poll_job = root.after(100, self.poll)
        if args.update and self.mode.get() == 'upgrade' and 'disabled' not in self.action.state():
            self.auto_start_job = root.after_idle(self.begin)

    def choose_target(self):
        chosen = filedialog.askdirectory(parent=self.root,initialdir=self.target.get(),title='インストール先フォルダー')
        if chosen:
            self.target.set(chosen)
            self.refresh()

    def choose_output(self):
        chosen = filedialog.askdirectory(parent=self.root, initialdir=self.output_location.get(),
                                        title='ログ・録音・文字起こしの共通保存先')
        if chosen:
            self.output_location.set(chosen)
            self.output_modified = True

    def default_output(self):
        self.output_location.set(str(default_output_root()))
        self.output_modified = True

    def refresh(self):
        if self.busy:
            return
        try:
            info = inspect_installation(Path(self.target.get()), APP_VERSION)
            if info.state == 'unknown':
                self.detected.set('専用の空フォルダー、または既存の本アプリのフォルダーを選んでください。')
                self.action.state(['disabled'])
                return
            self.action.state(['!disabled'])
            self.repair_button.state(['!disabled'] if 'repair' in info.actions else ['disabled'])
            self.uninstall_button.state(['!disabled'] if 'uninstall' in info.actions else ['disabled'])
            if info.version:
                self.detected.set(f'インストール済み: v{info.version}  ／  このセットアップ: v{APP_VERSION}。設定・録音・モデルを引き継ぎます。')
                if version_tuple(info.version) >= version_tuple(APP_VERSION):
                    self.maintenance.pack(fill='x', after=self.detected_label)
                    if self.mode.get() not in info.actions:
                        self.mode.set('repair')
                else:
                    self.maintenance.pack_forget()
                    self.mode.set('uninstall' if self.args.uninstall and 'uninstall' in info.actions else 'upgrade')
            else:
                self.detected.set('新しくインストールします。保存先フォルダーは自動で作成します。')
                self.maintenance.pack_forget()
                self.mode.set('install')
            if self.preference_target != info.target:
                self.register.set(info.registered_program if info.version else True)
                self.shortcut.set(info.create_shortcut if info.version else True)
                self.preference_target = info.target
                self.output_location.set(str(output_root(info.target, config_path=info.target/'Data/settings.json')))
                self.output_modified = False
            self.update_action()
        except (OSError, InstallationError) as exc:
            self.detected.set(str(exc))
            self.action.state(['disabled'])

    def update_action(self):
        mode = self.mode.get()
        self.action.configure(text={'repair':'修復する','uninstall':'アンインストールする','upgrade':'更新する'}.get(mode,'インストールする'))
        if mode == 'uninstall':
            self.options.pack_forget()
        elif not self.options.winfo_manager():
            anchor = self.maintenance if self.maintenance.winfo_manager() else self.detected_label
            self.options.pack(fill='x',pady=(12,0),after=anchor)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        inputs = (ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Radiobutton, ttk.Combobox, ttk.Spinbox, ttk.Scale)
        def descend(parent):
            for child in parent.winfo_children():
                yield child
                yield from descend(child)
        if busy:
            self.control_states = {}
            for widget in descend(self.form):
                if isinstance(widget, inputs):
                    self.control_states[widget] = widget.instate(('disabled',))
                    widget.state(['disabled'])
            self.action.state(['disabled'])
            self.open_button.state(['disabled'])
            self.set_phase('waiting')
            self.progress.configure(mode='indeterminate', value=0)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode='determinate', maximum=100, value=0)
            self.phase = 'idle'
            for widget, disabled in self.control_states.items():
                if widget.winfo_exists():
                    widget.state(['disabled'] if disabled else ['!disabled'])
            self.control_states.clear()
            if self.cuda_received:
                self.model_menu.configure(state='readonly')
            self.cancel_button.state(['!disabled'])
            self.cancel_button.configure(text='閉じる')
            self.open_button.state(['!disabled'])

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        if phase == 'optional':
            self.cancel_button.configure(text='追加環境の構築を中断')
            self.cancel_button.state(['!disabled'])
        else:
            self.cancel_button.configure(text='完了までお待ちください')
            self.cancel_button.state(['disabled'])

    def begin(self):
        if self.auto_start_job is not None:
            self.root.after_cancel(self.auto_start_job)
            self.auto_start_job = None
        if self.busy:
            return
        self.refresh()
        if 'disabled' in self.action.state():
            return
        target, mode = Path(self.target.get()).expanduser().absolute(), self.mode.get()
        if mode != 'uninstall' and self.minimal.get() and not self.cuda_received:
            self.status.set('CUDAの事前調査を待ち、表示された結果と音声認識モデルを確認してから開始してください。')
            return
        try:
            selected_output = validate_output_root(self.output_location.get()) if mode != 'uninstall' else ''
            selected_output = '' if Path(selected_output) == default_output_root() else selected_output
            model = self.model_choices[self.model.get()]
        except (ValueError, KeyError) as exc:
            messagebox.showerror('保存先とモデルを確認してください', str(exc), parent=self.root)
            return
        if mode == 'uninstall' and not messagebox.askyesno('アンインストール',
                f'次のフォルダー全体を削除します。\n{target}\n\n'
                'この中の設定・録音・文字起こし・AIモデルも削除されます。必要なデータは先に移してください。\n'
                'フォルダー外の保存先は削除しません。続けますか？',parent=self.root):
            return
        choices = dict(recording=self.recording.get(),minimal=self.minimal.get(),hardware=self.hardware.get(),
                       register_program=self.register.get(),create_shortcut=self.shortcut.get(),
                       output_location=selected_output, output_modified=self.output_modified, model=model)
        self.operation_target = target
        self.cancel.clear()
        self.set_busy(True)
        wait_pid = self.args.wait_pid
        def worker():
            try:
                self.messages.put(('status','実行中のアプリの終了を確認しています…'))
                wait_for_process(wait_pid,target)
                if mode == 'uninstall':
                    self.messages.put(('phase','uninstall'))
                    result = uninstall_installation(target,remove_data=True)
                    self.messages.put(('complete','アンインストールが完了しました。',False,result.warnings))
                    return
                existing = inspect_installation(target, APP_VERSION)
                if mode == 'repair' and existing.version and version_tuple(existing.version) > version_tuple(APP_VERSION):
                    self.messages.put(('status','インストール済みの新しい修復プログラムを検証しています…'))
                    maintenance = get_verified_maintenance_path(target)
                    self.messages.put(('handoff', target, maintenance))
                    return
                destination = Path(choices['output_location']) if choices['output_location'] else default_output_root()
                # Check user-file storage before changing an existing installation.
                # This also reports protected Documents folders clearly, without
                # asking for elevated execution or changing their protection.
                try:
                    destination.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=destination, prefix='.setup-write-check-'):
                        pass
                except OSError as exc:
                    raise InstallationError('共通保存先に書き込めません。別の保存先を選択してください。'
                                            f' 保存先: {destination}') from exc
                self.messages.put(('phase','install'))
                self.messages.put(('status','配布ファイルを検証し、本体を配置しています…'))
                package,digest = self.package_info or resources()
                extra = {'UnifiedTranscriberSetup.exe':setup_executable()} if packaged_setup() else None
                result = install_package(package,target,APP_VERSION,expected_sha256=digest,mode=mode,
                    register_program=choices['register_program'],create_shortcut=choices['create_shortcut'],extra_files=extra)
                # Save the selected user-output location before spawning optional
                # setup processes, which also need this location for diagnostics.
                new_preferences = seed_preferences(target, minimal=False, hardware=choices['hardware'], recording_ready=False,
                    output_location=choices['output_location'], model=choices['model'])
                if choices['output_modified']:
                    set_output_root(choices['output_location'], app_root=target, config_path=target/'Data/settings.json')
                preferences_path = target/'Data/settings.json'
                # Do not read an existing settings symlink or junction during repair.
                unsafe_settings = any(p.is_symlink() or (os.name == 'nt' and p.exists()
                    and p.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    for p in (preferences_path.parent, preferences_path))
                preference_digest = (hashlib.sha256(preferences_path.read_bytes()).hexdigest()
                                     if not unsafe_settings else '')
                self.messages.put(('status','本体の配置が完了しました。選択した追加環境を準備しています…'))
                setup_succeeded = False
                try:
                    self.messages.put(('phase','optional'))
                    log_folder = output_root(target, config_path=target/'Data/settings.json') / 'Logs'
                    log_folder.mkdir(parents=True, exist_ok=True)
                    log_path = log_folder / ('setup_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+os.urandom(3).hex()+'.log')
                    self.messages.put(('log', f'追加環境の詳細ログ: {log_path}'))
                    with log_path.open('x', encoding='utf-8') as detail_log:
                        def notify(line):
                            token = os.environ.get('HF_TOKEN', '')
                            safe = str(line).replace(token, '[HF_TOKEN非表示]') if token else str(line)
                            update = parse_setup_line(safe)
                            if update is not None:
                                self.messages.put(('setup-progress', update))
                            if not safe.strip().startswith(SETUP_PROGRESS_PREFIX):
                                detail_log.write(safe.rstrip()+'\n')
                                detail_log.flush()
                                self.messages.put(('log', safe))
                        setup_selected(target,recording=choices['recording'],minimal=choices['minimal'],hardware=choices['hardware'],
                            model=choices['model'],progress=notify,cancel=self.cancel)
                    setup_succeeded = True
                    text='セットアップが完了しました。アプリを起動できます。'
                except (OSError,RuntimeError,ValueError) as exc:
                    text='本体はインストール済みです。追加環境の構築は完了していません。アプリ内の一括セットアップから再試行できます。'
                    self.messages.put(('log',str(exc)))
                try:
                    if preference_digest and setup_succeeded and (choices['minimal'] or choices['recording']):
                        update_setup_preferences(target, minimal=choices['minimal'], hardware=choices['hardware'],
                            recording_ready=choices['recording'], model=choices['model'], expected_sha256=preference_digest,
                            initial_preferences=new_preferences)
                except (OSError,RuntimeError,ValueError) as exc:
                    self.messages.put(('log','初期設定を保存できませんでした。既存の設定は保持しています: '+str(exc)))
                    text='本体の配置が完了しました。起動後に設定の保存先を確認してください。'
                    setup_succeeded = False
                self.messages.put(('complete',text,True,result.warnings,setup_succeeded))
            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
                self.messages.put(('error',str(exc)))
        self.worker = threading.Thread(target=worker,daemon=True)
        self.worker.start()

    def poll(self):
        if self.closed:
            return
        if self.poll_job is not None:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        try:
            while True:
                item = self.messages.get_nowait()
                if item[0] == 'log':
                    self.log.configure(state='normal')
                    self.log.insert('end',item[1].rstrip()+'\n')
                    self.log.see('end')
                    self.log.configure(state='disabled')
                elif item[0] == 'cuda':
                    self.cuda_received = True
                    self.cuda_status.set(item[1].summary+'\n'+item[1].details)
                    if not self.hardware_modified and not self.busy:
                        self.hardware.set(item[1].recommended_hardware)
                    if not self.busy:
                        self.model_menu.configure(state='readonly')
                elif item[0] == 'status':
                    self.status.set(item[1])
                elif item[0] == 'setup-progress':
                    update = item[1]
                    detail = (' · ' + update.detail) if update.detail else ''
                    if update.fraction is None:
                        if str(self.progress.cget('mode')) != 'indeterminate':
                            self.progress.configure(mode='indeterminate', value=0)
                            self.progress.start(12)
                        self.status.set(update.stage + detail)
                    else:
                        self.progress.stop()
                        self.progress.configure(mode='determinate', maximum=100,
                                                value=round(update.fraction * 100, 1))
                        self.status.set(f'{update.stage} {update.fraction:.0%}' + detail)
                elif item[0] == 'phase':
                    self.set_phase(item[1])
                elif item[0] == 'handoff':
                    try:
                        reset_dll_search_path()
                        env = runtime_environment(item[1])
                        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
                        subprocess.Popen([str(item[2]), '--target', str(item[1])], shell=False, env=env,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        self.set_busy(False)
                        self.close()
                        return
                    except (OSError,RuntimeError,ValueError) as exc:
                        self.messages.put(('error',str(exc)))
                elif item[0] in ('complete','error'):
                    self.set_busy(False)
                    if item[0] == 'complete':
                        # A usable app may remain after optional setup fails.
                        # Only show 100% when every selected task succeeded.
                        if len(item) < 5 or item[4]:
                            self.progress.configure(value=100)
                        self.installed=item[2]
                        self.open_target = self.operation_target if self.installed else None
                        if self.installed:
                            self.open_button.pack(side='left')
                            self.refresh()
                        else:
                            self.open_button.pack_forget()
                            self.action.state(['disabled'])
                        for warning in item[3]:
                            self.log.configure(state='normal');self.log.insert('end',warning+'\n');self.log.configure(state='disabled')
                    else:
                        self.refresh()
                        messagebox.showerror('セットアップを完了できませんでした',item[1],parent=self.root)
                    self.status.set(item[1])
                    # Never launch after uninstall, cancellation, or a partial
                    # optional-setup failure.  Those states remain visible for
                    # review and the existing manual launch button is retained.
                    if (item[0] == 'complete' and self.installed and len(item) >= 5
                            and item[4] and not self.cancel.is_set()
                            and self.launch_after_setup.get()):
                        self.open_app()
                        if self.closed:
                            return
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_job = self.root.after(100,self.poll)

    def open_app(self):
        if self.busy or self.open_target is None:
            return
        executable=self.open_target/'UnifiedTranscriber.exe'
        if executable.is_file():
            try:
                reset_dll_search_path()
                env = runtime_environment(executable.parent)
                env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
                subprocess.Popen([str(executable)],cwd=str(executable.parent),env=env,shell=False,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.close()
            except (OSError,RuntimeError,ValueError) as exc:
                messagebox.showerror('アプリを起動できませんでした',str(exc),parent=self.root)
        else:
            messagebox.showerror('アプリを起動できませんでした',
                                 'インストールしたアプリ本体が見つかりません。',parent=self.root)

    def close(self):
        if self.busy:
            if self.phase != 'optional':
                self.status.set('安全に処理しています。完了するまでお待ちください。')
                return
            if messagebox.askyesno('セットアップの中断','追加環境の構築を中断しますか？配置済みのアプリ本体は保持されます。',parent=self.root):
                self.cancel.set()
                self.cancel_button.state(['disabled'])
                self.cancel_button.configure(text='中断しています…')
                self.status.set('追加環境の構築を中断しています。本体の配置中は完了までお待ちください。')
            return
        if self.poll_job is not None:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        if self.auto_start_job is not None:
            self.root.after_cancel(self.auto_start_job)
            self.auto_start_job = None
        self.progress.stop()
        self.closed = True
        self.root.destroy()


def gui_self_test() -> dict:
    """Initialize the real frozen wizard without installing or touching user data."""
    root = None
    wizard = None
    with tempfile.TemporaryDirectory(prefix='UnifiedTranscriber-GuiSelfTest-') as temporary:
        target = Path(temporary) / 'Sample App'
        try:
            root = tk.Tk()
            root.withdraw()
            # Never forward caller maintenance flags or their actual install
            # target into a diagnostic window with startup callbacks.
            args = argparse.Namespace(target=target, update=False, uninstall=False,
                                      wait_pid=None, wait_setup_pid=None)
            wizard = InstallerWindow(root,args)
            root.update_idletasks()
            root.update()
            controls = (wizard.path_entry,wizard.browse,wizard.action,wizard.cancel_button,
                        wizard.canvas,wizard.log,wizard.repair_button,wizard.uninstall_button)
            if (any(not widget.winfo_exists() for widget in controls)
                    or wizard.busy or wizard.mode.get() != 'install'
                    or wizard.auto_start_job is not None or target.exists()):
                raise InstallationError('セットアップ画面の自己検査に失敗しました。')
            # Fallback images keep damaged installs usable; a release must
            # carry actual icon pixels rather than silently accepting blanks.
            from ui_icons import _mask, tint_png
            image = tk.PhotoImage(master=root, data=tint_png(_mask('download',18),'#17283f'))
            if not any(not image.transparency_get(x,y)
                       for x in range(image.width()) for y in range(image.height())):
                raise InstallationError('セットアップ画面のアイコンを読み込めません。')
            result = dict(gui_ok=True, wizard_initialized=True, controls_ok=True, icons_ok=True,
                        selected_profile='faster-res', selected_model=wizard.model_choices[wizard.model.get()],
                        output_root=wizard.output_location.get(), cuda_probe_started=wizard.cuda_probe_started,
                        python=sys.version, python_version='.'.join(str(v) for v in sys.version_info[:3]),
                        tcl_version=str(root.tk.call('info','patchlevel')),
                        tk_version=str(root.tk.call('set','tk_patchLevel')))
            context = get_context()
            if os.name == 'nt' and context is not None:
                result.update(_setup_parent_window_state(context.owner_pid))
            return result
        finally:
            if wizard is not None:
                wizard.close()
            elif root is not None:
                root.destroy()


def _setup_parent_window_state(parent_pid: int) -> dict:
    """Record the real NSIS parent's visible windows for the Windows smoke test."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    matching = []
    @callback_type
    def visit(hwnd, _parameter):
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == parent_pid:
            matching.append(bool(user32.IsWindowVisible(hwnd)))
        return True
    if not user32.EnumWindows(visit, 0):
        raise OSError(ctypes.get_last_error(), 'セットアップの親画面を検査できません。')
    return {'setup_parent_window_count': len(matching),
            'setup_parent_visible_window_count': sum(matching)}


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description='文字起こし統合スタジオ セットアップ')
    parser.add_argument('--version',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--self-test-gui',action='store_true')
    parser.add_argument('--self-test-relaunch',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--self-test-report',type=Path)
    parser.add_argument('--target',type=Path)
    parser.add_argument('--update',action='store_true')
    parser.add_argument('--uninstall',action='store_true')
    parser.add_argument('--wait-pid',type=int)
    parser.add_argument('--wait-setup-pid',type=int,help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    args.self_test = args.self_test or args.self_test_report is not None or args.self_test_relaunch or args.self_test_gui
    if args.version:
        print(APP_VERSION);return 0
    if args.self_test:
        try:
            if args.self_test_gui and (args.update or args.uninstall or args.self_test_relaunch
                    or args.wait_pid is not None or args.wait_setup_pid is not None):
                raise InstallationError('画面の自己検査は更新・削除・再起動などの操作と同時に実行できません。')
            if args.self_test_relaunch:
                if (os.name != 'nt' or not packaged_setup()
                        or args.target is None or args.self_test_report is None
                        or args.update or args.uninstall):
                    raise InstallationError('再起動の自己検査には導入済みのWindowsセットアップと外部の結果ファイルを指定してください。')
                maintenance = get_verified_maintenance_path(args.target)
                target = maintenance.parent
                report_path = args.self_test_report.expanduser().absolute()
                if report_path.resolve().is_relative_to(target.resolve()):
                    args.self_test_report = None
                    raise InstallationError('自己検査の結果ファイルはアプリのフォルダー外を指定してください。')
                if setup_executable() != maintenance:
                    raise InstallationError('導入済みの正規セットアップからのみ再起動の自己検査を実行できます。')
                if relaunch_outside(target,['--self-test','--self-test-report',str(report_path)]):
                    return 0
                raise InstallationError('自己検査用の一時セットアップを開始できませんでした。')
            if args.wait_setup_pid is not None:
                if args.target is None:
                    raise InstallationError('終了待ちにはインストール先を指定してください。')
                wait_for_process(args.wait_setup_pid,args.target)
            package,digest=resources()
            names=validate_package(package,APP_VERSION,digest)
            result={'ok':True,'version':APP_VERSION,'files':len(names),'payload_sha256':digest}
            context = get_context()
            if context is not None:
                result.update(packaging='nsis', setup_parent_verified=True,
                              setup_owner_pid=context.owner_pid,
                              setup_executable=str(context.executable),
                              setup_resource_root=str(context.resource_root),
                              requested_target=str(args.target) if args.target is not None else None)
            if args.self_test_gui:
                result.update(gui_self_test())
            if args.wait_setup_pid is not None:
                maintenance = get_verified_maintenance_path(args.target)
                current = setup_executable()
                with current.open('rb') as current_file, maintenance.open('rb') as original_file:
                    same = hashlib.file_digest(current_file,'sha256').digest() == hashlib.file_digest(original_file,'sha256').digest()
                if current.is_relative_to(maintenance.parent) or not same:
                    raise InstallationError('一時セットアップの自己検査に失敗しました。')
                result.update(copied_outside=True,waited_setup_pid=args.wait_setup_pid,
                              installed_setup_copy_verified=True)
        except (OSError,ValueError,KeyError,RuntimeError,tk.TclError) as exc:
            result={'ok':False,'version':APP_VERSION,'error':str(exc)}
        if args.self_test_report:
            try:
                args.self_test_report.write_text(json.dumps(result),encoding='utf-8')
            except OSError as exc:
                result={'ok':False,'version':APP_VERSION,'error':'自己検査の結果ファイルを保存できませんでした: '+str(exc)}
        print(json.dumps(result));return 0 if result['ok'] else 1
    target=args.target or default_target()
    forward=[]
    if args.update:forward.append('--update')
    if args.uninstall:forward.append('--uninstall')
    if args.wait_pid is not None:forward.extend(('--wait-pid',str(args.wait_pid)))
    root = None
    try:
        if relaunch_outside(target,forward):return 0
        reset_dll_search_path()
        if args.wait_setup_pid is not None:
            wait_for_process(args.wait_setup_pid, target)
        root=tk.Tk()
        InstallerWindow(root,args)
        root.mainloop()
        return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError,tk.TclError) as exc:
        if root is not None:
            try:root.destroy()
            except tk.TclError:pass
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(exc),'セットアップを開始できませんでした',0x10)
        else:
            print(str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
