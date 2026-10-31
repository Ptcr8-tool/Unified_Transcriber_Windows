"""Unified Japanese Windows Tkinter GUI; settings, installer and watch are modal."""
from __future__ import annotations

import os
import csv
import json
import re
import tempfile
import traceback
import zipfile
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from ui_icons import IconButton

from studio_ui import (StudioView, UI_FONT, bind_timer_cleanup, bind_variable_trace,
                       scrollable_body, theme_widgets)

from ui_controls import ToggleSwitch, input_folder, destination_buttons
from studio_workflow import CaptureSetupDialog, WelcomeDialog, ResultDialog, open_folder

from screen_capture import has_audio_stream, unique_destination
from gamebar_recording import (GameBarOptions, send_hotkey, captures_directory,
                               snapshot_mp4, find_completed_capture)
from external_recording import ExternalOptions, finalize_external
from resource_limits import WindowsCpuLimit, write_gpu_pacing
from diagnostic_logs import SessionLogs, log_directory
from merge_metadata import inspect_merge_file, oldest_first, show_datetime
from engine_progress import EngineUpdate, PREFIX as PROGRESS_PREFIX, parse_engine_line
from setup_progress import PREFIX as SETUP_PROGRESS_PREFIX, parse_setup_line
from output_commit import publish_outputs, publish_transcription
from cuda_probe import CudaAvailability, probe_cuda
from output_locations import OUTPUT_FOLDERS, default_output_root
from bulk_catalog import BulkPlan, MODEL_CHOICES, DEFAULT_MODELS, MODEL_LABELS, planned_summary, preset
from media_tools import (MediaMetadata, AUDIO_FORMATS, BITRATES, concat_audio, encode_live_recording,
                         format_date, media_binary, merge_text_files, metadata_header,
                         probe_metadata)
from merge_archive import archive_merge_sources, source_stamps
from process_control import ProcessRegistry, check_cancel, kill_owned_process, run_cancellable, owned_process
from hf_auth import (register_hf_token, credential_guard, write_verified_credential,
                     delete_credential, resolve_hf_token)
from live_config import (LiveOptions, collision_safe_path, unique_media_path,
                         RECORDING_CHANNELS, recording_channel_label, is_silent_pcm)
from capture_preferences import saved_options, serialize_options, match_window, recording_folder_preference
from app_hotkeys import WindowsHotkeys
from live_transcript import append_provisional, shifted_lines
from transcript_tools import (convert_transcript, export_transcript_csv, merge_transcript_csv,
                              speakers_in_transcript, replace_speaker_labels, speaker_utterances)
from wheel_support import install_wheel_routing
from core import (APP_VERSION, BASE, CODE_DIR, BY_ID, PROFILES, Config, app_config_path, build_command, default_directory, ensure_output_directories,
                  choose_output, output_name, env_python, load_config, redact, save_config,
                  supported_file, SUPPORTED_EXTENSIONS)

# Tkinter itself has no native Windows Explorer file-drop support.  Install only
# the small GUI dependency; engine virtual environments remain untouched.
GUI_DEPENDENCIES = CODE_DIR / 'gui_dependencies'
try:
    from dnd_support import available as _verified_dnd_available
    if not _verified_dnd_available(GUI_DEPENDENCIES):
        raise ImportError('The D&D package is missing or does not match the pinned wheel; repair required.')
    from tkinterdnd2 import DND_FILES, TkinterDnD
except (ImportError, OSError) as exc:
    DND_FILES = None
    TkinterDnD = None
    DND_IMPORT_ERROR = str(exc)
else:
    DND_IMPORT_ERROR = None


def parse_drop_paths(data: str, splitlist) -> list[Path]:
    """TkDnD sends a Tcl list, not a whitespace-separated sequence."""
    return [Path(raw) for raw in splitlist(data) if raw]


def dispatch_ui_callback(widget, callback, diagnostics):
    """Run queued worker results on the Tk thread if their window still exists."""
    try:
        if widget.winfo_exists():
            callback()
    except tk.TclError:
        # Closing a window may invalidate Tk; record errors on surviving windows.
        detail=traceback.format_exc()
        try:
            alive=widget.winfo_exists()
        except tk.TclError:
            alive=False
        if alive:
            diagnostics.error('UI更新エラー:\n'+detail)
    except Exception:
        diagnostics.error('UI更新エラー:\n'+traceback.format_exc())


def ui_window_cancel(widget):
    """Create a worker-safe cancellation signal on the Tk thread."""
    cancel=threading.Event()
    widget.bind('<Destroy>',lambda event:cancel.set() if event.widget is widget else None,add='+')
    return cancel


def queue_ui_callback(app, widget, callback, cancel=None):
    """Apply backpressure instead of dropping results; never call Tk from the worker."""
    while not getattr(app,'closing',False) and not (cancel and cancel.is_set()):
        try:
            app.ui_callbacks.put((widget, callback),timeout=0.2)
            return True
        except queue.Full:
            continue
    return False


def utf8_child_env() -> dict[str, str]:
    """Force UTF-8 in child stdout; Windows console/codepage defaults are CP932."""
    return {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1',
            'PYTHONUNBUFFERED': '1'}


def console_interpreter() -> str:
    """Use python.exe for subprocess logs even if the GUI runs under pythonw.exe."""
    exe = str(sys.executable)
    if os.name == 'nt' and exe.lower().endswith(('\\pythonw.exe', '/pythonw.exe')):
        return exe[:-len('pythonw.exe')] + 'python.exe'
    return exe


def hidden_child_flags() -> int:
    """Suppress Windows console activation for GUI-launched CLI subprocesses."""
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


PROFILE_LABELS = {p.name: p.id for p in PROFILES}
PROFILE_NAMES = {p.id: p.name for p in PROFILES}
INPUT_LOCATION = '（入力ファイルと同じ場所）'
DEFAULT_LOCATION = '（デフォルト：共通出力フォルダー）'

MEDIA_TYPES = [('音声・動画', ' '.join('*' + x for x in sorted(SUPPORTED_EXTENSIONS))),
               ('すべてのファイル', '*.*')]
MERGE_SCAN_ENTRY_LIMIT = 50000
MERGE_SCAN_DEPTH_LIMIT = 64
DIARIZATION_WARNING_PREFIX = '@@TRANSCRIBER_DIARIZATION_WARNING@@'


def diarization_warning_count(line: str) -> int | None:
    """Read the engine's bounded ASCII-only failed-segment count."""
    if not line.startswith(DIARIZATION_WARNING_PREFIX):
        return None
    count=line[len(DIARIZATION_WARNING_PREFIX):].strip()
    if not re.fullmatch(r'[0-9]{1,9}',count):
        return None
    value=int(count)
    return value if value>0 else None


def job_status_label(status: str, detail: str) -> str:
    if status=='一部完了':
        match=re.search(r' / (話者分離[0-9]{1,9}件失敗)。保存した文字起こしの話者ラベルを確認してください。$',detail)
        if match:
            return f'{status}（{match.group(1)}）'
    return status


def center(dialog: tk.Toplevel, parent: tk.Misc) -> None:
    dialog.update_idletasks()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width()-dialog.winfo_width()) // 2)
    y = parent.winfo_rooty() + max(0, (parent.winfo_height()-dialog.winfo_height()) // 2)
    dialog.geometry(f'+{x}+{y}')


def modal(parent: tk.Misc, title: str, width: int = 760) -> tk.Toplevel:
    win = tk.Toplevel(parent)
    bind_timer_cleanup(win)
    win.title(title)
    win.transient(parent)
    win.minsize(min(width, 640), 420)
    win.geometry(f'{width}x650')
    win.grab_set()
    win.bind('<Escape>', lambda _event: win.destroy())
    center(win, parent)
    win.after_idle(lambda: fit_dialog(win, parent))
    return win


def fit_dialog(win: tk.Toplevel, parent: tk.Misc) -> None:
    """Keep the final initial size on-screen after dialog-specific geometry calls."""
    try:
        if not win.winfo_exists():
            return
        win.update_idletasks()
        screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
        width = min(win.winfo_width(), max(320, screen_w-40))
        height = min(win.winfo_height(), max(280, screen_h-90))
        min_w, min_h = win.minsize()
        win.minsize(min(min_w, width), min(min_h, height))
        x = max(0, min(screen_w-width, parent.winfo_rootx()+(parent.winfo_width()-width)//2))
        y = max(0, min(screen_h-height-45, parent.winfo_rooty()+(parent.winfo_height()-height)//2))
        win.geometry(f'{width}x{height}+{x}+{y}')
        theme_widgets(win)
    except tk.TclError:
        pass  # The dialog may have been closed before this idle callback.


def show_error(parent: tk.Misc, text: str) -> None:
    messagebox.showerror('確認してください', text, parent=parent)


def save_secure_token(app, variable, parent):
    token = variable.get().strip()
    try:
        cleanup_failed = False
        with credential_guard():
            write_verified_credential(token)
            app.hf_token = token
            app.diagnostics.token = token
            if app.config.hf_token_json:
                clean = replace(app.config, hf_token_json='')
                try:
                    save_config(clean)
                    app.config = clean
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            messagebox.showwarning('平文設定が残っています',
                '資格情報には保存しましたが、従来の settings.json からトークンを削除できませんでした。'
                '保存先の権限を確認してください。', parent=parent)
            return False
        messagebox.showinfo('登録完了', 'Windows 資格情報マネージャーに保存しました。\n'
            'このWindowsユーザーのアプリで利用します。', parent=parent)
        return True
    except (OSError, ValueError) as exc:
        show_error(parent, str(exc))
        return False


def remove_secure_token(app, variable, parent):
    if not messagebox.askyesno('資格情報の削除',
            'このアプリの Windows 資格情報を削除します。\n'
            '環境変数や設定ファイルに残るトークンは変更しません。削除しますか？',
            parent=parent, default=messagebox.NO):
        return
    try:
        delete_credential()
        token = os.environ.get('HF_TOKEN', '').strip() or app.config.hf_token_json
        app.hf_token = token
        app.diagnostics.token = token
        variable.set(token)
        messagebox.showinfo('削除完了', 'このアプリの Windows 資格情報を削除しました。\n'
            + ('環境変数または以前の設定にあるトークンは引き続き利用されます。' if token else ''),
            parent=parent)
    except OSError as exc:
        show_error(parent, str(exc))


def cuda_setup_advisory(win: tk.Toplevel, parent: tk.Misc, callback, *, wraplength=700, events=None):
    """Display driver evidence on the Tk thread; a bounded query runs off-thread."""
    state = tk.StringVar(win, 'CUDAを事前確認しています…（Python・AI環境の導入前に確認）')
    frame = ttk.LabelFrame(parent, text='このPCのGPU / CUDA', padding=(10, 8))
    frame.cuda_status_variable = state  # Keep the Tcl variable after the one-shot callback returns.
    ttk.Label(frame, textvariable=state, wraplength=wraplength,
              foreground='#455269').pack(anchor='w', fill='x')
    results = queue.Queue()

    def worker():
        try:
            results.put(probe_cuda())
        except Exception:
            if events is not None:
                events.put(('errorlog', 'CUDA事前確認に失敗:\n'+traceback.format_exc()))
            results.put(CudaAvailability('unknown', 'CUDAの事前確認を完了できませんでした',
                'CPU構成を候補にしました。GPU構成は手動でも選べます。'))

    def poll():
        try:
            result = results.get_nowait()
        except queue.Empty:
            win.after(80, poll)
            return
        state.set(result.summary + '\n' + result.details)
        callback(result)

    threading.Thread(target=worker, daemon=True, name='cuda-preflight').start()
    win.after(20, poll)
    return frame


def setup_output_folder(parent: tk.Misc, variable: tk.StringVar, *, width=700):
    """One root for default logs, recording, transcripts, merges and exports."""
    frame = ttk.LabelFrame(parent, text='共通の出力先', padding=(10, 8))
    row = ttk.Frame(frame)
    row.pack(fill='x')
    ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True)

    def browse():
        chosen = filedialog.askdirectory(parent=parent, title='共通の出力フォルダーを選択',
            initialdir=variable.get() or str(default_output_root()))
        if chosen:
            variable.set(chosen)

    IconButton(row, text='参照…', command=browse).pack(side='left', padx=(7, 0))
    IconButton(row, text='ドキュメントの既定に戻す', icon='reset',
        command=lambda: variable.set(str(default_output_root()))).pack(side='left', padx=(7, 0))
    ttk.Label(frame, text='ログ・録音・文字起こし・結合・書き出しを種類別のフォルダーへ保存します。'
        '個別に指定した保存先は優先されます。既存ファイルは移動しません。'
        '起動時のログは次回起動から、診断ログは保存後から反映します。',
        wraplength=width, foreground='#596477').pack(anchor='w', pady=(6, 0))
    return frame


def preflight_output_root(config: Config) -> None:
    """Check actual writes before making a new destination the saved default."""
    config.validate()
    root = Path(config.output_root).expanduser() if config.output_root else default_output_root()
    for folder in (root, *(root/name for name in OUTPUT_FOLDERS.values())):
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w+b', dir=folder,
                prefix='.transcriber-write-check-', suffix='.tmp') as stream:
            stream.write(b'write check\n')
            stream.flush()
            os.fsync(stream.fileno())


def save_preferences_with_output(app, config: Config, *, persist=None) -> None:
    """Commit preferences and log destination together; retain the old state on failure."""
    previous = app.config
    preflight_output_root(config)
    persist = persist or save_config
    logs = app.diagnostics
    previous_paths = {key: getattr(logs, key) for key in ('folder', 'session_path', 'error_path')
                      if hasattr(logs, key)}
    persist(config)
    try:
        if hasattr(logs, 'refresh_location'):
            logs.refresh_location()
    except OSError as exc:
        # SessionLogs itself publishes new paths only after creating both files.
        # Restore defensively too, so a future logger retains the same guarantee.
        for key, value in previous_paths.items():
            setattr(logs, key, value)
        try:
            persist(previous)
        except OSError as rollback:
            raise OSError('出力先を切り替えられず、保存済み設定の復元にも失敗しました。'
                          '使用できる出力先を選んで再保存してください。\n'
                          + str(rollback)) from exc
        raise OSError('出力先を切り替えられませんでした。以前の設定とログ保存先を維持しました。\n'
                      + str(exc)) from exc
    app.config = config


def save_setup_output_root(app, value: str) -> None:
    value = value.strip()
    root = Path(value).expanduser() if value else default_output_root()
    if root == default_output_root():
        value = ''
    else:
        value = str(root)
    cfg = replace(app.config, output_root=value)
    save_preferences_with_output(app, cfg)


class SettingsDialog:
    def __init__(self, app: 'UnifiedApp'):
        self.app = app
        self.cfg = replace(app.config, python_overrides=dict(app.config.python_overrides))
        self.win = modal(app.root, '文字起こし・話者分離の詳細設定', 800)
        self.vars = {key: tk.StringVar(self.win, str(getattr(self.cfg, key))) for key in (
            'model_openai', 'model_faster', 'qwen_model', 'qwen_aligner', 'qwen_max_tokens',
            'language', 'device', 'compute_type', 'max_speakers', 'diarization_unit', 'speaker_mode',
            'speakers', 'threshold', 'segment_length', 'merge_gap', 'output_dir')}
        self.output_root = tk.StringVar(self.win, self.cfg.output_root or str(default_output_root()))
        self.same_input = tk.BooleanVar(self.win, self.cfg.output_same_as_input)
        if self.cfg.output_same_as_input:
            self.vars['output_dir'].set(INPUT_LOCATION)
        elif not self.cfg.output_dir:
            self.vars['output_dir'].set(DEFAULT_LOCATION)
        bind_variable_trace(self.win, self.vars['output_dir'], 'write', lambda *_: self.same_input.set(
            self.vars['output_dir'].get() == INPUT_LOCATION))
        self.vars['profile'] = tk.StringVar(self.win, PROFILE_NAMES[self.cfg.profile])
        self.output_flags = {
            key: tk.BooleanVar(self.win, getattr(self.cfg, key))
            for key in ('filename_timestamp','filename_model','filename_backend','filename_diarization',
                        'filename_media_creation','txt_metadata_header')
        }
        self.output_preview = tk.StringVar(self.win)
        self.caption_split = tk.BooleanVar(self.win, self.cfg.caption_split)
        self.caption_max_chars = tk.StringVar(self.win, str(self.cfg.caption_max_chars))
        self.caption_max_seconds = tk.StringVar(self.win, str(self.cfg.caption_max_seconds))
        for flag in self.output_flags.values():
            bind_variable_trace(self.win, flag, 'write', lambda *_: self._refresh_output_preview())
        for model_key in ('model_openai', 'model_faster','qwen_model'):
            bind_variable_trace(self.win, self.vars[model_key], 'write', lambda *_: self._refresh_output_preview())
        self.output_mode = tk.StringVar(self.win, self.cfg.output_mode)
        bind_variable_trace(self.win, self.output_mode, 'write', lambda *_: self._refresh_output_preview())
        self.overwrite = tk.BooleanVar(self.win, self.cfg.overwrite)
        self.token = tk.StringVar(self.win, app.hf_token or os.environ.get('HF_TOKEN', '') or self.cfg.hf_token_json)
        self.store_token_json = tk.BooleanVar(self.win, bool(self.cfg.hf_token_json))

        outer = ttk.Frame(self.win, padding=(14, 12))
        outer.pack(fill='both', expand=True)
        self.profile_box = ttk.Combobox(outer, textvariable=self.vars['profile'],
            values=list(PROFILE_LABELS), state='readonly', width=49)
        ttk.Label(outer, text='実行方式', font=(UI_FONT, 10, 'bold')).pack(anchor='w')
        self.profile_box.pack(anchor='w', fill='x', pady=(4, 3))
        self.profile_box.bind('<<ComboboxSelected>>', self._update_dynamic)
        self.profile_hint = ttk.Label(outer, foreground='#455269', wraplength=750)
        self.profile_hint.pack(fill='x', pady=(0, 8))
        tabs = ttk.Notebook(outer, height=395)
        tabs.pack(fill='both', expand=True)
        self.asr_tab, self.speaker_tab, self.output_tab = (ttk.Frame(tabs, padding=14) for _ in range(3))
        tabs.add(self.asr_tab, text='音声認識・モデル')
        tabs.add(self.speaker_tab, text='話者分離')
        tabs.add(self.output_tab, text='保存先・認証')
        self._create_asr()
        self._create_speaker()
        self._create_output()
        bar = ttk.Frame(outer)
        bar.pack(fill='x', side='bottom', before=tabs, pady=(13, 0))
        IconButton(bar, text='キャンセル', command=self.win.destroy).pack(side='right', padx=(8, 0))
        IconButton(bar, text='設定を保存', style='Primary.TButton', command=self._save).pack(side='right')
        bind_variable_trace(self.win, self.vars['speaker_mode'], 'write', lambda *_: self._update_dynamic())
        bind_variable_trace(self.win, self.vars['diarization_unit'], 'write', lambda *_: self._update_dynamic())
        self._update_dynamic()
        self._refresh_output_preview()

    @staticmethod
    def line(parent: ttk.Frame, row: int, title: str, widget: tk.Widget, desc: str = '') -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=title).grid(row=row*2, column=0, sticky='w', padx=(0, 14), pady=6)
        widget.grid(row=row*2, column=1, sticky='ew', pady=6)
        if desc:
            ttk.Label(parent, text=desc, foreground='#596477', wraplength=530).grid(
                row=row*2+1, column=1, sticky='w', pady=(0, 8))

    def field(self, parent: ttk.Frame, row: int, title: str, key: str, desc: str = '', values=None):
        if values:
            widget = ttk.Combobox(parent, textvariable=self.vars[key], values=values, state='readonly')
        else:
            widget = ttk.Entry(parent, textvariable=self.vars[key])
        self.line(parent, row, title, widget, desc)
        return widget

    def _create_asr(self) -> None:
        tab, self.asr_canvas = self.scrollable_tab(self.asr_tab)
        self.f_openai = self.field(tab, 0, 'OpenAI Whisperモデル', 'model_openai',
            '既定turboはlarge-v3-turboと同一。GPU推奨。',
            ('turbo', 'large-v3-turbo', 'large-v3', 'medium', 'small', 'base', 'tiny'))
        self.f_faster = self.field(tab, 1, 'faster-whisperモデル', 'model_faster',
            '既定large-v3-turbo。高速・高精度のバランスを重視。',
            ('large-v3-turbo', 'turbo', 'large-v3', 'medium', 'small', 'base', 'tiny'))
        self.f_qwen = self.field(tab, 2, 'Qwen日本語モデルID', 'qwen_model')
        self.f_aligner = self.field(tab, 3, 'Qwen Forced Aligner', 'qwen_aligner',
            'Qwenのタイムスタンプ付与に利用します。')
        self.f_tokens = self.field(tab, 4, 'Qwen最大トークン数', 'qwen_max_tokens')
        self.f_lang = self.field(tab, 5, '言語 (OpenAI版)', 'language',
            'OpenAI Whisperのみ対応。ja / en / auto。その他の元実装は日本語固定です。')
        self.f_device = self.field(tab, 6, '処理デバイス', 'device',
            'auto：セットアップ済みGPUを検出できればcuda。利用不可ならcpu。',
            ('auto','cuda','cpu'))
        self.f_compute = self.field(tab, 7, 'faster-whisper計算精度', 'compute_type',
            'auto推奨。CPU=int8、CUDA=float16が元実装の既定です。',
            ('auto','int8','float16','float32','int8_float16'))

    @staticmethod
    def scrollable_tab(parent: ttk.Frame) -> tuple[ttk.Frame, tk.Canvas]:
        """Keep all tab inputs accessible on smaller laptop screens."""
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        content = ttk.Frame(canvas, padding=(6, 2, 8, 14))
        window_id = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _ev: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))
        return content, canvas

    def _create_speaker(self) -> None:
        tab, self.speaker_canvas = self.scrollable_tab(self.speaker_tab)
        ttk.Label(tab, text='推奨：Resemblyzerは「発話単位」を標準に。'
                  '固定長は従来方式との比較や長い独話に使用します。',
                  foreground='#455269', wraplength=620).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        self.f_unit = self.field(tab, 1, '話者分析の区切り方', 'diarization_unit',
            'utterance＝ASR発話区間に基づく分析 / fixed＝一定秒数ごと。pyannoteは独自の話者区間を検出。',
            ('utterance', 'fixed'))
        self.f_max = self.field(tab, 2, '最大話者数', 'max_speakers',
            'Resemblyzer自動推定時の上限 / pyannoteの上限。')
        self.f_mode = self.field(tab, 3, '話者数の決め方', 'speaker_mode',
            'Resemblyzerのみ。auto＝声の距離で推定 / fixed＝人数を指定。',
            ('auto', 'fixed'))
        self.f_speakers = self.field(tab, 4, '人数固定の場合', 'speakers')
        self.f_threshold = self.field(tab, 5, 'クラスタ距離しきい値', 'threshold',
            '自動推定のみ。小さいほど別の話者と判定しやすくなります。')
        self.f_length = self.field(tab, 6, '固定長区間 (秒)', 'segment_length',
            '固定長を選んだときのみ適用。既定20秒。')
        self.f_gap = self.field(tab, 7, '発話結合の間隔 (秒)', 'merge_gap',
            'faster-whisper / Qwenの結果表示で隣接する同一話者を結合。')

    def _create_output(self) -> None:
        tab, _canvas = self.scrollable_tab(self.output_tab)
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text='出力フォルダー').grid(row=0, column=0, sticky='w', pady=8)
        pathrow = ttk.Frame(tab)
        pathrow.grid(row=0, column=1, sticky='ew')
        pathrow.columnconfigure(0, weight=1)
        ttk.Entry(pathrow, textvariable=self.vars['output_dir']).grid(row=0, column=0, sticky='ew')
        IconButton(pathrow, text='参照…', command=self._browse_output).grid(row=0, column=1, padx=(8,0))
        destination_buttons(pathrow,same_command=lambda:self.vars['output_dir'].set(INPUT_LOCATION),
            default_command=lambda:self.vars['output_dir'].set(DEFAULT_LOCATION)).grid(
            row=1,column=0,columnspan=2,sticky='w',pady=(6,0))
        ttk.Label(tab, text='デフォルトは共通出力先の Transcriptions。入力と同じ場所も選べます。監視モードはprocessedへ保存。',
            foreground='#596477', wraplength=500).grid(row=1, column=1, sticky='w', pady=(0,13))
        ttk.Label(tab,text='出力形式').grid(row=2,column=0,sticky='w',pady=8)
        ttk.Combobox(tab,textvariable=self.output_mode,values=('txt','csv','both'),
                     state='readonly',width=14).grid(row=2,column=1,sticky='w')
        ttk.Label(tab,text='txt＝従来形式 / csv＝Excelで扱える表形式 / both＝両方を保存',
                  foreground='#596477').grid(row=3,column=0,columnspan=2,sticky='w')
        ttk.Label(tab, text='ファイル名への自動付加',font=(UI_FONT,10,'bold')).grid(
            row=4,column=0,columnspan=2,sticky='w',pady=(5,8))
        for row, key, label in (
            (5, 'filename_timestamp','文字起こし実行日時（例：20000101_090000）'),
            (6, 'filename_model','音声認識モデル名（例：large-v3-turbo）'),
            (7, 'filename_backend','音声認識方式（例：faster-whisper）'),
            (8, 'filename_diarization','話者分離方式（例：resemblyzer-utterance）'),
            (9, 'filename_media_creation','メディア内の録音・作成日時（取得できるときのみ）'),
            (10, 'txt_metadata_header','TXT冒頭にメタデータ（日時・モデル等）を記載'),
        ):
            ToggleSwitch(tab,text=label,variable=self.output_flags[key]).grid(
                row=row,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Label(tab,text='保存ファイル名プレビュー',foreground='#596477').grid(
            row=11,column=0,columnspan=2,sticky='w',pady=(11,3))
        ttk.Label(tab,textvariable=self.output_preview,wraplength=630,
                  font=(UI_FONT,10,'bold')).grid(
            row=12,column=0,columnspan=2,sticky='w',pady=(0,7))
        ttk.Label(tab,text='録音日時はメディアタグを優先。取得できない場合はファイル名に付けません。'
                  'Windowsのファイル作成日時はコピー日時の場合があるため、TXTにも取得元を記録します。',
                  foreground='#596477',wraplength=630).grid(
            row=13,column=0,columnspan=2,sticky='w',pady=(0,12))
        ttk.Label(tab, text='既存のTXT・CSVがある場合は、処理時に各ファイルごとにスキップか上書きを確認します。',
                  foreground='#596477', wraplength=630).grid(row=14,column=0,columnspan=2, sticky='w', pady=(5,12))
        ttk.Separator(tab).grid(row=15,column=0,columnspan=2, sticky='ew',pady=(5,14))
        ttk.Label(tab, text='Hugging Faceトークン').grid(row=16,column=0, sticky='w', pady=8)
        ttk.Entry(tab, textvariable=self.token, show='●').grid(row=16,column=1,sticky='ew')
        ttk.Label(tab,text='Windows 資格情報マネージャーへの保存を推奨します。'
                  '既存の HF_TOKEN 環境変数も利用できますが、環境変数は平文です。',
                  foreground='#596477',wraplength=540).grid(row=17,column=1,sticky='w',pady=6)
        credentials = ttk.Frame(tab)
        credentials.grid(row=18,column=1,sticky='w',pady=(5,8))
        IconButton(credentials,text='資格情報に保存',command=self._save_secure_token).pack(side='left',padx=(0,5))
        IconButton(credentials,text='資格情報を削除',command=lambda:remove_secure_token(
            self.app,self.token,self.win)).pack(side='left',padx=(0,5))
        IconButton(credentials,text='環境変数に登録…',command=self._register_environment_token).pack(side='left')
        ToggleSwitch(tab,variable=self.store_token_json,
            text='非推奨：トークンをsettings.jsonに平文保存する（明示的に許可）').grid(
            row=19,column=0,columnspan=2,sticky='w',pady=(8,6))
        ttk.Label(tab,text='平文JSONは他のプログラムから読み取られる可能性があります。'
                  'OFFにすると次回保存時にJSONから削除します。起動時に安全に移行できる場合は資格情報へ移します。',
                  foreground='#995e22',wraplength=640).grid(
            row=20,column=0,columnspan=2,sticky='w',pady=(0,5))
        ttk.Separator(tab).grid(row=21,column=0,columnspan=2,sticky='ew',pady=(14,9))
        ToggleSwitch(tab,variable=self.caption_split,text='字幕向け：長い発話を短く分割する（標準OFF）').grid(
            row=22,column=0,columnspan=2,sticky='w',pady=5)
        caption_opts=ttk.Frame(tab)
        caption_opts.grid(row=23,column=0,columnspan=2,sticky='w',pady=5)
        ttk.Label(caption_opts,text='1発話の最大文字数').pack(side='left')
        ttk.Entry(caption_opts,textvariable=self.caption_max_chars,width=7).pack(side='left',padx=(7,16))
        ttk.Label(caption_opts,text='最大秒数').pack(side='left')
        ttk.Entry(caption_opts,textvariable=self.caption_max_seconds,width=7).pack(side='left',padx=7)
        ttk.Label(tab,text='Whisper/Qwenの単語時刻を優先します。単語時刻が得られない長文は時間を概算分割します。句読点は追加しません。',
                  foreground='#596477',wraplength=640).grid(row=24,column=0,columnspan=2,sticky='w',pady=(3,10))

        for child in tab.winfo_children():
            layout = child.grid_info()
            if layout:
                child.grid_configure(row=int(layout['row'])+2)
        setup_output_folder(tab, self.output_root, width=610).grid(
            row=0, column=0, columnspan=2, sticky='ew', pady=(0, 15))

    def _save_secure_token(self):
        if save_secure_token(self.app,self.token,self.win):
            self.store_token_json.set(False)

    def _register_environment_token(self):
        token = self.token.get().strip()
        if not token:
            show_error(self.win, 'Hugging Faceトークンを入力してください。')
            return
        if not messagebox.askyesno('環境変数に登録',
                '現在のWindowsユーザーの環境変数 HF_TOKEN に登録します。\n'
                'Windowsの環境変数は暗号化された秘密保管庫ではありません。\n'
                '資格情報に保存済みのトークンがある場合、次回起動時はそちらが優先されます。\n'
                'この方法で保存してよいですか？',parent=self.win):
            return
        try:
            register_hf_token(token)
            self.app.hf_token = token
            self.app.diagnostics.token = token
            messagebox.showinfo('登録完了',
                'HF_TOKENを現在のユーザー環境変数へ登録しました。\n'
                'このアプリではすぐ使用できます。既に開いている他のアプリは再起動が必要な場合があります。',
                parent=self.win)
        except (OSError, ValueError) as exc:
            show_error(self.win, str(exc))

    def _refresh_output_preview(self):
        try:
            cfg = replace(self.cfg,
                profile=PROFILE_LABELS[self.vars['profile'].get()],
                model_openai=self.vars['model_openai'].get(),
                model_faster=self.vars['model_faster'].get(),
                qwen_model=self.vars['qwen_model'].get(),
                diarization_unit=self.vars['diarization_unit'].get(),
                output_mode=self.output_mode.get(),
                **{key: var.get() for key,var in self.output_flags.items()})
            sample=output_name(Path('サンプル録音 01.m4a'),cfg,
                transcribed_at=datetime(2000,1,1,9,0,0),
                media_created_at=datetime(2000,1,1,8,0,0))
            self.output_preview.set((sample.replace('.txt','.csv') if cfg.output_mode=='csv' else
                sample+' ＋ '+sample.replace('.txt','.csv') if cfg.output_mode=='both' else sample))
        except (KeyError, ValueError):
            self.output_preview.set('設定を確認してください。')

    def _browse_output(self):
        path = filedialog.askdirectory(parent=self.win, title='出力フォルダーを選択')
        if path:
            self.vars['output_dir'].set(path)

    def _update_dynamic(self, _event=None):
        profile = BY_ID[PROFILE_LABELS[self.vars['profile'].get()]]
        self.profile_hint.configure(text=profile.description)
        self._refresh_output_preview()
        for widget, active in (
            (self.f_openai, profile.asr == 'openai'),
            (self.f_faster, profile.asr == 'faster'),
            (self.f_qwen, profile.asr == 'qwen'),
            (self.f_aligner, profile.asr == 'qwen'),
            (self.f_tokens, profile.asr == 'qwen'),
            (self.f_lang, profile.asr == 'openai'),
            (self.f_compute, profile.asr == 'faster'),
            (self.f_unit, profile.diarizer != 'pyannote'),
            (self.f_mode, profile.diarizer != 'pyannote'),
            (self.f_speakers, profile.diarizer != 'pyannote' and self.vars['speaker_mode'].get() == 'fixed'),
            (self.f_threshold, profile.diarizer != 'pyannote' and self.vars['speaker_mode'].get() == 'auto'),
            (self.f_length, profile.diarizer != 'pyannote' and self.vars['diarization_unit'].get() == 'fixed'),
            (self.f_gap, profile.asr != 'openai'),
        ):
            widget.configure(state='readonly' if active and isinstance(widget, ttk.Combobox)
                             else ('normal' if active else 'disabled'))

    def _save(self):
        try:
            kwargs = {key: var.get().strip() for key, var in self.vars.items() if key != 'profile'}
            kwargs.update({
                'profile': PROFILE_LABELS[self.vars['profile'].get()],
                'qwen_max_tokens': int(kwargs['qwen_max_tokens']),
                'max_speakers': int(kwargs['max_speakers']),
                'speakers': int(kwargs['speakers']),
                'threshold': float(kwargs['threshold']),
                'segment_length': float(kwargs['segment_length']),
                'merge_gap': float(kwargs['merge_gap']),
                'overwrite': False,  # v17: existing files are confirmed per input, not overwritten globally
                'output_root': ('' if Path(self.output_root.get().strip()).expanduser() == default_output_root()
                                else self.output_root.get().strip()),
                'output_mode': self.output_mode.get(),
                'output_same_as_input': self.same_input.get(),
                'output_dir': self.app.config.output_dir if self.same_input.get() else (
                    '' if kwargs['output_dir']==DEFAULT_LOCATION else kwargs['output_dir']),
                'cpu_limit_percent': self.app.config.cpu_limit_percent,
                'gpu_soft_percent': self.app.config.gpu_soft_percent,
                'caption_split': self.caption_split.get(),
                'caption_max_chars': int(self.caption_max_chars.get()),
                'caption_max_seconds': float(self.caption_max_seconds.get()),
                **{key: var.get() for key,var in self.output_flags.items()},
                'python_overrides': dict(self.app.config.python_overrides),
                'hf_token_json': self.token.get().strip() if self.store_token_json.get() else '',
            })
            cfg = replace(self.app.config, **kwargs)
            cfg.validate()
            if cfg.hf_token_json and not messagebox.askyesno('非推奨：平文保存',
                    'トークンをsettings.jsonに暗号化せず保存します。\n'
                    'バックアップやファイル共有で漏えいする可能性があります。\n保存しますか？', parent=self.win):
                return
            save_preferences_with_output(self.app, cfg)
            self.app.hf_token = self.token.get().strip()  # RAM only
            self.app.diagnostics.token = self.app.hf_token
            self.app.profile_var.set(PROFILE_NAMES[cfg.profile])
            self.app.update_profile_display()
            self.win.destroy()
        except (ValueError, OSError, TypeError) as exc:
            show_error(self.win, str(exc))


class CudaTestDialog:
    """Modal CUDA diagnostics for the selected virtual environment."""
    def __init__(self, app: 'UnifiedApp'):
        self.app = app
        self.win = modal(app.root, 'CUDA / GPU 利用診断', 760)
        self.win.geometry('760x520')
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.closed = False
        self.selected = tk.StringVar(value=PROFILE_NAMES[app.config.profile])
        self.state = tk.StringVar(value='対象方式を選び「CUDAをテスト」を押してください。')
        box = ttk.Frame(self.win, padding=18)
        box.pack(fill='both', expand=True)
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text='方式ごとに異なるPython環境でCUDAを検査します。',
                  wraplength=700).grid(row=0, column=0, sticky='w', pady=(0, 8))
        ttk.Combobox(box, textvariable=self.selected, values=list(PROFILE_LABELS),
                     state='readonly').grid(row=1, column=0, sticky='ew', pady=7)
        ttk.Label(box, text='PyTorchのGPU実計算と、faster-whisperではCTranslate2のCUDA対応を確認。'
                  ' AIモデルそのものの実行テストではありません。',
                  foreground='#596477', wraplength=700).grid(row=2, column=0, sticky='w', pady=(0, 10))
        self.output = tk.Text(box, height=14, wrap='word', state='disabled',
                              font=(UI_FONT, 10))
        self.output.grid(row=3, column=0, sticky='nsew')
        box.rowconfigure(3, weight=1)
        ttk.Label(box, textvariable=self.state, wraplength=700).grid(
            row=4, column=0, sticky='w', pady=(12, 8))
        actions = ttk.Frame(box)
        actions.grid(row=5, column=0, sticky='e')
        self.go = IconButton(actions, text='CUDAをテスト', command=self._start)
        self.go.pack(side='left', padx=(0, 8))
        IconButton(actions, text='閉じる', command=self._close).pack(side='left')
        self.win.protocol('WM_DELETE_WINDOW', self._close)
        self.win.after(100, self._poll)

    def _append(self, value: str):
        self.output.configure(state='normal')
        self.output.insert('end', value)
        self.output.see('end')
        self.output.configure(state='disabled')

    def _start(self):
        if self.running or self.app.busy:
            return
        key = PROFILE_LABELS[self.selected.get()]
        interpreter = env_python(key, self.app.config)
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.configure(state='disabled')
        if not interpreter.is_file():
            self._append(f'未セットアップ: {interpreter}\n'
                         '先に「環境セットアップ…」からCUDA構成をインストールしてください。\n')
            self.state.set('診断できません：選択方式のPython環境がありません。')
            return
        self.app._begin_work()
        self.running = True
        self.go.configure(state='disabled')
        self.state.set('CUDAを診断しています…')
        backend = BY_ID[key].asr
        threading.Thread(target=self._worker, args=(interpreter, backend), daemon=True).start()

    def _worker(self, interpreter: Path, backend: str):
        try:
            cmd = [str(interpreter), '-X', 'utf8', '-u', str(CODE_DIR / 'cuda_diagnostic.py'), '--backend', backend]
            with owned_process(self.app,cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding='utf-8', errors='replace',
                                  cwd=str(BASE), env=utf8_child_env(),
                                  creationflags=hidden_child_flags()) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.events.put(('line', line))
                result = proc.wait()
            self.events.put(('done', result))
        except (OSError, subprocess.SubprocessError) as exc:
            self.events.put(('line', '診断起動エラー: ' + str(exc) + '\n'))
            self.events.put(('done', 2))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'line':
                    self._append(value)
                else:
                    self.running = False
                    self.go.configure(state='normal')
                    self.state.set('CUDA診断結果：利用可能' if value == 0
                                   else 'CUDA診断結果：利用できません（詳細は上のログ）')
                    if value == 0:
                        self._append('\n※ CUDA計算の成功はモデル全体の動作やVRAM充足を保証しません。\n')
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def _close(self):
        self.closed = True
        self.win.destroy()


class SetupDialog:
    def __init__(self, app: 'UnifiedApp'):
        self.app = app
        self.win = modal(app.root, '実行環境のセットアップ・割り当て', 760)
        self.win.geometry('760x610')
        box, footer = scrollable_body(self.win, padding=(18, 14))
        box.columnconfigure(1,weight=1)
        self.profile = tk.StringVar(value=PROFILE_NAMES[app.config.profile])
        self.device = tk.StringVar(value='cpu')
        self.hardware_touched = False
        self.output_root = tk.StringVar(self.win, app.config.output_root or str(default_output_root()))
        self.custom = tk.StringVar()
        ttk.Label(box,text='対象方式').grid(row=0,column=0,sticky='w',pady=7)
        cb = ttk.Combobox(box,textvariable=self.profile, values=list(PROFILE_LABELS),state='readonly')
        cb.grid(row=0,column=1,columnspan=2,sticky='ew',pady=7)
        cb.bind('<<ComboboxSelected>>',self._refresh)
        ttk.Label(box,text='インストール構成').grid(row=1,column=0,sticky='w',pady=7)
        hardware_box = ttk.Combobox(box,textvariable=self.device,values=('cpu','cuda'),state='readonly',width=12)
        hardware_box.grid(row=1,column=1,sticky='w',pady=7)
        hardware_box.bind('<<ComboboxSelected>>', lambda _e: setattr(self, 'hardware_touched', True))
        ttk.Label(box,text='GPUはNVIDIAドライバーとCUDA対応PyTorchが必要です。',
                  foreground='#596477').grid(row=2,column=1,columnspan=2,sticky='w')
        ttk.Separator(box).grid(row=3,column=0,columnspan=3,sticky='ew',pady=18)
        ttk.Label(box,text='既存のPython環境').grid(row=4,column=0,sticky='w',pady=6)
        ttk.Entry(box,textvariable=self.custom).grid(row=4,column=1,sticky='ew',pady=6)
        IconButton(box,text='参照…',command=self._browse).grid(row=4,column=2,padx=(8,0))
        ttk.Label(box,text='互換性がある場合に限り、別のPython環境も割り当てられます。\n'
                         '各方式に必要な依存ライブラリが揃っている場合のみ使用できます。\n'
                         '空欄で「割り当て解除」すると、このアプリが作る専用環境を利用します。',
                  wraplength=510,foreground='#596477').grid(row=5,column=1,columnspan=2,sticky='w',pady=(4,12))
        IconButton(box,text='このPythonを割り当て',command=self._assign).grid(row=6,column=1,sticky='w',pady=8)
        IconButton(box,text='割り当て解除',command=self._clear).grid(row=6,column=1,sticky='e',pady=8)
        ttk.Label(box,text='※ 5つの方式はそれぞれ独立した環境にインストールされ、互いに上書きしません。\n'
                         '　初回セットアップとAIモデル取得にはネット接続・十分な容量が必要です。',
                  wraplength=650,foreground='#596477').grid(row=7,column=0,columnspan=3,sticky='w',pady=12)
        IconButton(footer,text='閉じる',command=self.win.destroy).pack(side='right',padx=(8,0))
        IconButton(footer,text='環境をセットアップ',style='Primary.TButton',command=self._install).pack(side='right')
        cuda_setup_advisory(self.win, box, self._cuda_ready, wraplength=650, events=self.app.events).grid(
            row=8,column=0,columnspan=3,sticky='ew',pady=(5,10))
        setup_output_folder(box, self.output_root, width=650).grid(
            row=9,column=0,columnspan=3,sticky='ew',pady=(0,10))
        self._refresh()

    def _cuda_ready(self, result):
        if not self.hardware_touched:
            self.device.set(result.recommended_hardware)

    def _refresh(self, _event=None):
        self.custom.set(self.app.config.python_overrides.get(PROFILE_LABELS[self.profile.get()],''))

    def _browse(self):
        path = filedialog.askopenfilename(parent=self.win,title='既存仮想環境のpython.exeを選択',
                 filetypes=[('Python', 'python.exe'), ('すべて','*.*')])
        if path:self.custom.set(path)

    def _assign(self):
        path = Path(self.custom.get().strip()).expanduser()
        if not path.is_file():
            show_error(self.win,'実在するpython.exeを指定してください。')
            return
        key=PROFILE_LABELS[self.profile.get()]
        self.app.config.python_overrides[key]=str(path)
        save_config(self.app.config)
        self.app.log(f'既存環境を割り当てました：{key} → {path}\n')
        messagebox.showinfo('割り当て完了','実行時に指定したPython環境を使います。',parent=self.win)

    def _clear(self):
        key=PROFILE_LABELS[self.profile.get()]
        self.app.config.python_overrides.pop(key,None)
        self.custom.set('')
        save_config(self.app.config)
        self.app.log(f'専用環境へ戻しました：{key}\n')

    def _install(self):
        key = PROFILE_LABELS[self.profile.get()]
        if key in self.app.config.python_overrides:
            if not messagebox.askyesno('環境の確認',
                'この方式には既存環境が割り当てられています。セットアップ後も既存環境が優先されます。続行しますか？',
                parent=self.win):
                return
        if self.app.busy:
            show_error(self.win,'別の処理が実行中です。')
            return
        try:
            save_setup_output_root(self.app, self.output_root.get())
        except (OSError, ValueError, TypeError) as exc:
            show_error(self.win, str(exc))
            return
        hardware = self.device.get()
        self.win.destroy()
        self.app.start_setup(key,hardware)


class BulkSetupDialog:
    """Choose engine environments and shared representative model downloads in one modal."""
    def __init__(self, app: 'UnifiedApp'):
        self.app = app
        self.win = modal(app.root, '一括セットアップ｜方式・CPU/GPU・モデル', 820)
        self.win.geometry('820x670')
        self.profile_flags = {p.id: tk.BooleanVar(self.win, False) for p in PROFILES}
        self.profile_devices = {p.id: tk.StringVar(self.win, 'cpu') for p in PROFILES}
        self.hardware_touched = set()
        self.cuda_status = None
        self.output_root = tk.StringVar(self.win, app.config.output_root or str(default_output_root()))
        self.model_flags = {(backend, model): tk.BooleanVar(self.win, False)
                            for backend, values in MODEL_CHOICES.items() for model in values}
        self.with_models = tk.BooleanVar(self.win, True)
        self.with_pyannote = tk.BooleanVar(self.win, True)
        self.with_audio = tk.BooleanVar(self.win, True)
        self.token = tk.StringVar(self.win, app.hf_token or os.environ.get('HF_TOKEN', ''))
        outer, footer = scrollable_body(self.win, padding=(18, 12))
        ttk.Label(outer, text='複数の実行方式とモデルを一括インストール',
                  font=(UI_FONT, 13, 'bold')).pack(anchor='w', pady=(0, 4))
        ttk.Label(outer, text='環境は方式ごとに独立。モデルは既存キャッシュを使い、同じものは原則再ダウンロードしません。'
                  ' GPU指定はその方式のCUDA用PyTorchを構築します。',
                  wraplength=760, foreground='#596477').pack(anchor='w', pady=(0, 10))
        self.cuda_frame = cuda_setup_advisory(self.win, outer, self._cuda_ready, events=self.app.events)
        self.cuda_frame.pack(fill='x', pady=(0, 10))
        presets = ttk.Frame(outer)
        presets.pack(fill='x', pady=(0, 8))
        IconButton(presets, text='② 推奨', icon='download', command=self._load_minimum).pack(
            side='left', padx=(0,7))
        for name, title in (
            ('recommended-cuda', '②＋④ GPU'),
            ('recommended-cpu', '②＋④ CPU'),
            ('all-cuda', '全方式 GPU'),
            ('all-cpu', '全方式 CPU')):
            IconButton(presets, text=title, icon='cpu', command=lambda x=name: self._load_preset(x)).pack(
                side='left', padx=(0, 7))
        tabs = ttk.Notebook(outer)
        self.setup_tabs = tabs
        tabs.pack(fill='x')
        profiles_tab = ttk.Frame(tabs, padding=13)
        models_tab = ttk.Frame(tabs, padding=13)
        tabs.add(profiles_tab, text='① 実行方式・CPU/GPU')
        tabs.add(models_tab, text='② モデルを事前取得', state='disabled')
        self.models_tab = models_tab
        ttk.Label(profiles_tab, text='使用する方式にチェックを付け、方式ごとのCPU/GPUを選んでください。',
                  foreground='#596477').grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))
        for i, profile in enumerate(PROFILES, 1):
            ToggleSwitch(profiles_tab, text=profile.name,
                            variable=self.profile_flags[profile.id]).grid(
                            row=i, column=0, sticky='w', padx=(0, 8), pady=9)
            device_box = ttk.Combobox(profiles_tab, textvariable=self.profile_devices[profile.id],
                         values=('cuda', 'cpu'), state='readonly', width=9)
            device_box.grid(row=i, column=1, sticky='e', padx=8)
            device_box.bind('<<ComboboxSelected>>',
                lambda _e, key=profile.id: self.hardware_touched.add(key))
            ttk.Label(profiles_tab, text='GPU / CPU', foreground='#596477').grid(
                row=i, column=2, sticky='w')
        ttk.Label(profiles_tab, text='CUDA対応GPUがないPCでは「cpu」を選択。'
                  ' 同じ方式でCPU版とGPU版の同時インストールはできません。',
                  foreground='#596477', wraplength=710).grid(row=6, column=0,
                    columnspan=3, sticky='w', pady=(15, 8))
        ToggleSwitch(profiles_tab,text='録音環境もセットアップ（PortAudio・WASAPI・FFmpeg／推奨）',
                        variable=self.with_audio).grid(row=7,column=0,columnspan=3,sticky='w',pady=(12,4))
        ToggleSwitch(models_tab, text='環境構築後、選んだモデルを事前ダウンロード',
                        variable=self.with_models).pack(anchor='w', pady=(0, 10))
        # Let the surrounding form scroll the whole selected tab.  A second
        # canvas here makes the remaining models easy to miss.
        model_frame = ttk.Frame(models_tab, padding=(6, 3))
        model_frame.pack(fill='x')
        row = 0
        for backend, caption in (('faster', 'faster-whisper（② 推奨）'), ('openai', 'OpenAI Whisper'),
                                 ('qwen', 'Qwen3-ASR（ForcedAlignerも同時取得）')):
            ttk.Label(model_frame, text=caption, font=(UI_FONT,10,'bold')).grid(
                row=row,column=0,sticky='w',pady=(9, 4))
            row += 1
            for model in MODEL_CHOICES[backend]:
                ToggleSwitch(model_frame, text=MODEL_LABELS.get(model, model),
                                variable=self.model_flags[(backend,model)]).grid(
                                row=row,column=0,sticky='w',pady=3,padx=(14,0))
                row += 1
        ToggleSwitch(model_frame, text='pyannote 3.1 の話者分離モデルも取得する',
                        variable=self.with_pyannote).grid(row=row,column=0,sticky='w',pady=(12,5))
        row += 1
        ttk.Label(model_frame, text='pyannoteはHugging Face上で2つの利用条件への同意とトークンが必要。',
                  foreground='#596477').grid(row=row,column=0,sticky='w')
        row += 1
        tokenrow = ttk.Frame(model_frame)
        tokenrow.grid(row=row,column=0,sticky='ew',pady=(7,13))
        ttk.Label(tokenrow,text='HF_TOKEN：').pack(side='left')
        ttk.Entry(tokenrow,textvariable=self.token,show='●',width=42).pack(side='left',fill='x',expand=True)
        IconButton(tokenrow,text='資格情報に保存',command=lambda:save_secure_token(
            self.app,self.token,self.win)).pack(side='left',padx=(8,0))
        IconButton(tokenrow,text='環境変数に登録…',command=self._register_token).pack(side='left',padx=(8,0))
        model_frame.columnconfigure(0, weight=1)
        def fit_selected_tab(_event=None):
            try:
                selected = tabs.nametowidget(tabs.select())
                selected.update_idletasks()
                height = max(1, selected.winfo_reqheight())
                if int(tabs.cget('height')) != height:
                    tabs.configure(height=height)
            except tk.TclError:
                pass  # The dialog may have closed before an idle callback.
        tabs.bind('<<NotebookTabChanged>>', fit_selected_tab, add='+')
        tabs.after_idle(fit_selected_tab)
        setup_output_folder(outer, self.output_root).pack(fill='x', pady=(10, 4))
        self.summary = tk.StringVar(self.win)
        ttk.Label(outer, textvariable=self.summary, wraplength=760,
                  foreground='#455269').pack(fill='x', pady=(10,4))
        ttk.Label(outer, text='大容量のモデル・PyTorchを選ぶと数GB〜数十GBが必要です。'
                  'ダウンロードはGUI内のログに表示されます。途中で停止・再実行できます。',
                  foreground='#6a6475', wraplength=760).pack(fill='x', pady=(0,9))
        IconButton(footer,text='キャンセル',command=self.win.destroy).pack(side='right',padx=(8,0))
        IconButton(footer,text='選択項目を一括セットアップ',style='Primary.TButton',command=self._start).pack(side='right')
        for var in [*self.profile_flags.values(), *self.profile_devices.values(),
                    *self.model_flags.values(), self.with_models, self.with_pyannote, self.with_audio]:
            bind_variable_trace(self.win, var, 'write', lambda *_:self._refresh())
        self._load_minimum()

    def _cuda_ready(self, result):
        self.cuda_status = result
        for key, variable in self.profile_devices.items():
            if key not in self.hardware_touched:
                variable.set(result.recommended_hardware)
        self.setup_tabs.tab(self.models_tab, state='normal')
        self._refresh()

    def _load_minimum(self):
        hardware = self.cuda_status.recommended_hardware if self.cuda_status else 'cpu'
        self.hardware_touched.clear()
        for profile in PROFILES:
            self.profile_flags[profile.id].set(profile.id == 'faster-res')
            self.profile_devices[profile.id].set(hardware)
        for item, variable in self.model_flags.items():
            variable.set(item == ('faster', 'large-v3-turbo'))
        self.with_models.set(True)
        self.with_pyannote.set(False)
        self._refresh()

    def _register_token(self):
        token=self.token.get().strip()
        if not token:
            show_error(self.win,'登録するHugging Faceトークンを入力してください。')
            return
        if not messagebox.askyesno('HF_TOKENを登録',
                'Windowsのユーザー環境変数にトークンを保存します。\n'
                '環境変数は暗号化された秘密保管庫ではありません。\n'
                '資格情報があれば次回起動時はそちらが優先されます。よろしいですか？',parent=self.win):
            return
        try:
            register_hf_token(token)
            self.app.hf_token=token
            self.app.diagnostics.token=token
            messagebox.showinfo('登録完了','HF_TOKENを登録しました。',parent=self.win)
        except (OSError, ValueError) as exc:
            show_error(self.win,str(exc))

    def _load_preset(self, key: str):
        self.hardware_touched.update(p.id for p in PROFILES)
        plan = preset(key)
        keys = dict(plan.profiles)
        for p in PROFILES:
            self.profile_flags[p.id].set(p.id in keys)
            self.profile_devices[p.id].set(keys.get(p.id, 'cuda' if key.endswith('cuda') else 'cpu'))
        selected_models = set(plan.models)
        for item, var in self.model_flags.items():
            var.set(item in selected_models)
        self.with_models.set(True)
        self.with_pyannote.set(True)
        self._refresh()

    def _plan(self) -> BulkPlan:
        chosen = tuple((p.id,self.profile_devices[p.id].get()) for p in PROFILES
                       if self.profile_flags[p.id].get())
        active_backends = {BY_ID[key].asr for key, _ in chosen}
        models = tuple((backend,model) for backend, options in MODEL_CHOICES.items()
                       if backend in active_backends for model in options
                       if self.model_flags[(backend,model)].get()) if self.with_models.get() else ()
        return BulkPlan(chosen, models, self.with_pyannote.get() and self.with_models.get())

    def _refresh(self):
        try:
            self.summary.set(planned_summary(self._plan()))
        except ValueError as exc:
            self.summary.set(str(exc))

    def _start(self):
        if self.cuda_status is None:
            show_error(self.win, 'CUDAの事前確認が終わるまで数秒お待ちください。')
            return
        if self.app.busy:
            show_error(self.win, '既に別の処理を実行中です。')
            return
        plan = self._plan()
        try:
            plan.validate()
            if self.with_models.get():
                active_backends = {BY_ID[key].asr for key,_ in plan.profiles}
                configured_backends = {backend for backend,_ in plan.models}
                if missing := (active_backends-configured_backends):
                    raise ValueError('モデルが未選択です: ' + ', '.join(sorted(missing)) +
                                     '\n各方式から最低1モデル選ぶか、モデル事前取得をOFFにしてください。')
            if (plan.pyannote and any(BY_ID[p].diarizer == 'pyannote' for p,_ in plan.profiles)
                and not self.token.get().strip()):
                raise ValueError('pyannoteのモデル取得にはHF_TOKENが必要です。'
                                 '「モデルを事前取得」タブで入力するか、pyannoteモデル取得をOFFにしてください。')
        except ValueError as exc:
            show_error(self.win, str(exc))
            return
        try:
            save_setup_output_root(self.app, self.output_root.get())
        except (OSError, ValueError, TypeError) as exc:
            show_error(self.win, str(exc))
            return
        # Don't persist a credential. It lives only as a child process environment variable.
        token = self.token.get().strip()
        if token:
            self.app.hf_token = token
            self.app.diagnostics.token = token
        self.win.destroy()
        self.app.start_bulk_setup(plan, token, with_audio=self.with_audio.get())


# Bind shared services to the running entry point.  In normal startup this
# module is executed via runpy as __main__, so a circular import is unsafe.
from gui_file_dialogs import (WatchDialog as _WatchDialog,
                              ConcatDialog as _ConcatDialog,
                              TranscriptConvertDialog as _TranscriptConvertDialog)


class _FileDialogServices:
    def _ui(self):
        return globals()


class WatchDialog(_FileDialogServices, _WatchDialog):
    pass


class ConcatDialog(_FileDialogServices, _ConcatDialog):
    pass


class TranscriptConvertDialog(_FileDialogServices, _TranscriptConvertDialog):
    pass


class LiveCaptureDialog:
    """Non-blocking modal controls; audio capture remains in another process."""
    MODES = {'録音＋文字起こし': 'both', '録音のみ': 'record', '文字起こしのみ': 'transcribe'}
    SOURCES = {'マイク入力': 'mic', 'PC再生音（WASAPIループバック）': 'loopback',
               'マイク＋PC再生音（同時録音）': 'mix'}

    def __init__(self, app: 'UnifiedApp'):
        self.app = app
        self.win = modal(app.root, 'リアルタイム録音・文字起こし', 800)
        self.ui_cancel=ui_window_cancel(self.win)
        self.win.geometry('800x750')
        shell=ttk.Frame(self.win,padding=(16,12))
        shell.pack(fill='both',expand=True)
        body=ttk.Frame(shell)
        body.pack(fill='both',expand=True)
        canvas=tk.Canvas(body,highlightthickness=0)
        scrollbar=ttk.Scrollbar(body,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left',fill='both',expand=True)
        scrollbar.pack(side='right',fill='y')
        frame=ttk.Frame(canvas,padding=(4,4,12,12))
        window_id=canvas.create_window((0,0),window=frame,anchor='nw')
        frame.bind('<Configure>',lambda _event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window_id,width=event.width))
        ttk.Label(frame, text='音声を連続録音しながら、別処理で区間ごとに文字起こし',
                  font=(UI_FONT, 12, 'bold')).pack(anchor='w', pady=(0,12))
        self.mode = tk.StringVar(value='録音＋文字起こし')
        self.source = tk.StringVar(value='マイク入力')
        self.interval = tk.StringVar(value='20')
        self.folder = tk.StringVar(value=str(app.config.live_recording_dir or default_directory('recordings')))
        self.final_pass = tk.BooleanVar(value=True)
        self.post_transcribe = tk.BooleanVar(self.win, app.config.live_post_transcribe)
        self.recording_format = tk.StringVar(value=app.config.live_recording_format)
        self.recording_bitrate = tk.StringVar(value=app.config.live_recording_bitrate)
        self.recording_pcm_bits = tk.StringVar(value=str(app.config.live_recording_pcm_bits))
        self.recording_sample_rate = tk.StringVar(value=str(app.config.live_recording_sample_rate))
        self.recording_channels = tk.StringVar(self.win, recording_channel_label(
            app.config.live_recording_channels, app.config.live_channel_mode))
        self.devices = {'既定の録音デバイス': ''}
        self.device_var = tk.StringVar(value='既定の録音デバイス')
        def field(title, widget, hint=''):
            ttk.Label(frame, text=title).pack(anchor='w', pady=(10,3))
            widget.pack(fill='x')
            if hint:
                ttk.Label(frame, text=hint, foreground='#526178',wraplength=730).pack(anchor='w',pady=(3,0))
        field('動作モード',ttk.Combobox(frame, textvariable=self.mode, values=list(self.MODES),state='readonly'),
              '文字起こしのみでも一時WAVを内部に保持し、処理が終わると削除します。')
        field('音声入力元',ttk.Combobox(frame,textvariable=self.source,values=list(self.SOURCES),state='readonly'),
              'PC音とマイクの混合、またはL/Rに分けた録音ができます。')
        devices_row=ttk.Frame(frame)
        field('入力デバイス', devices_row)
        self.device_box=ttk.Combobox(devices_row,textvariable=self.device_var,
                                     values=['既定の録音デバイス'],state='readonly')
        self.device_box.pack(side='left',fill='x',expand=True)
        IconButton(devices_row,text='デバイス更新',command=self._refresh_devices).pack(side='left',padx=(8,0))
        IconButton(devices_row,text='入力テスト',command=self._probe_devices).pack(side='left',padx=(8,0))
        self.mix_box=ttk.LabelFrame(frame,text='同時録音：2つの入力デバイスと音量',padding=10)
        self.mic_device_var=tk.StringVar(value='既定の録音デバイス')
        self.loopback_device_var=tk.StringVar(value='既定の録音デバイス')
        self.mic_gain_var=tk.StringVar(value='100')
        self.loopback_gain_var=tk.StringVar(value='100')
        self.mix_device_boxes={}
        for label,key,variable,gain in (
                ('マイク','mic',self.mic_device_var,self.mic_gain_var),
                ('PC再生音','loopback',self.loopback_device_var,self.loopback_gain_var)):
            section=ttk.Frame(self.mix_box)
            section.pack(fill='x',pady=(4,7))
            ttk.Label(section,text=label,width=12).pack(side='left')
            entry=ttk.Combobox(section,textvariable=variable,
                   values=['既定の録音デバイス'],state='readonly')
            entry.pack(side='left',fill='x',expand=True)
            self.mix_device_boxes[key]=entry
            ttk.Label(section,text='音量').pack(side='left',padx=(10,3))
            ttk.Spinbox(section,from_=0,to=200,increment=5,
                   textvariable=gain,width=5).pack(side='left')
            ttk.Label(section,text='%').pack(side='left')
        ttk.Label(self.mix_box,text='初期値は両方100%。音割れ防止のため、ピークが超過すると自動減衰します。'
                  '入力の一方が無音でも、もう一方の音声は継続して記録します。',
                  foreground='#526178',wraplength=680).pack(anchor='w',pady=(3,0))
        self.interval_combo=ttk.Combobox(frame,textvariable=self.interval,
                       values=['10','20','30','60'],state='readonly')
        field('文字起こしの区切り（秒）',self.interval_combo,
              'GPUなどの推論が遅れた場合も、音声は独立して録音し、未処理区間を順番に処理します。')
        recording_opts=ttk.LabelFrame(frame,text='録音形式・コーデック（終了後に変換）',padding=(10,8))
        recording_opts.pack(fill='x',pady=(12,5))
        for row, (label,variable,choices) in enumerate((
                ('保存形式',self.recording_format,('wav','mp3','m4a','flac')),
                ('ビットレート',self.recording_bitrate,BITRATES),
                ('WAV／FLAC ビット深度',self.recording_pcm_bits,('16','24')),
                ('保存サンプルレート',self.recording_sample_rate,('44100','48000')),
                ('保存チャンネル',self.recording_channels,tuple(RECORDING_CHANNELS)))):
            rr,col=divmod(row,2)
            ttk.Label(recording_opts,text=label).grid(row=rr,column=col*2,sticky='w',padx=(2,8),pady=5)
            combo=ttk.Combobox(recording_opts,textvariable=variable,values=choices,
                               width=19,state='readonly')
            combo.grid(row=rr,column=col*2+1,sticky='ew',padx=(0,14),pady=5)
            if row==1:self.bitrate_combo=combo
            if row==2:self.pcm_combo=combo
        recording_opts.columnconfigure(1,weight=1)
        recording_opts.columnconfigure(3,weight=1)
        bind_variable_trace(self.win, self.recording_format, 'write',lambda *_:self._refresh_codec_controls())
        ttk.Label(recording_opts,text='録音中は欠落を避けるためPCM WAVを連続保存します。'
            '停止後に指定形式へ変換。変換に失敗した場合はWAVを保護します。',
            foreground='#526178',wraplength=680).grid(row=3,column=0,columnspan=4,sticky='w',pady=(7,2))
        bind_variable_trace(self.win, self.recording_channels, 'write', lambda *_: self._channel_changed())
        self._refresh_codec_controls()
        folder_row=ttk.Frame(frame)
        field('出力先フォルダー',folder_row)
        ttk.Entry(folder_row,textvariable=self.folder).pack(side='left',fill='x',expand=True)
        IconButton(folder_row,text='参照…',command=self._browse).pack(side='left',padx=(8,0))
        destination_buttons(frame,default_command=lambda:self.folder.set(str(default_directory('recordings')))).pack(anchor='w',pady=(5,0))
        self.final_box=ToggleSwitch(frame,text='録音終了後に全体を再解析して最終版TXT／CSVを生成（推奨）',
                        variable=self.final_pass)
        self.final_box.pack(anchor='w',pady=(14,3))
        self.live_hint=ttk.Label(frame,text='区間ごとの速報は .live.txt に出力します。区間をまたぐ話者IDは暫定です。'
              '内部収録は連続WAVです。保存形式は上の設定に従い録音終了後に変換します。音声認識エンジンはメイン画面の選択方式を使用。',
              foreground='#526178',wraplength=730)
        self.live_hint.pack(anchor='w',pady=(3,10))
        preferences=ttk.Frame(shell)
        preferences.pack(fill='x',pady=(6,0))
        self.skip_settings=tk.BooleanVar(self.win,app.config.skip_live_settings)
        ToggleSwitch(preferences,text='次回以降は設定画面をスキップする',variable=self.skip_settings).pack(side='left')
        IconButton(preferences,text='設定を保存',command=lambda:self._start(save_only=True)).pack(side='right')
        IconButton(preferences,text='ショートカット…',command=app.edit_shortcuts).pack(side='right',padx=6)
        actions=ttk.Frame(shell)
        actions.pack(fill='x',pady=(7,7))
        self.setup_btn=IconButton(actions,text='録音環境をセットアップ',command=app.start_capture_setup)
        self.setup_btn.pack(side='left')
        self.start_btn=IconButton(actions,text='● 録音を開始',style='Primary.TButton',command=self._start)
        self.start_btn.pack(side='right',padx=(7,0))
        self.end_btn=IconButton(actions,text='■ 録音を終了',command=app.stop)
        self.end_btn.pack(side='right')
        IconButton(actions,text='閉じる',command=self.win.destroy).pack(side='right',padx=(7,0))
        IconButton(actions,text='認識を中断',command=self.app.cancel_live_transcription).pack(side='left',padx=(8,0))
        self.status=tk.StringVar(value='録音環境のインストール後、デバイス一覧を更新してください。')
        ttk.Label(shell,textvariable=self.status,foreground='#526178',wraplength=740).pack(anchor='w')
        bind_variable_trace(self.win, self.source, 'write',lambda *_:self._filter_devices())
        bind_variable_trace(self.win, self.mode, 'write',lambda *_:self._refresh_controls())
        self._refresh_controls()
        self._load_previous()
        self._tick()

    def _load_previous(self):
        options=saved_options(self.app.config,'live')
        if options is not None:
            self.mode.set(next(label for label,value in self.MODES.items() if value==options.mode))
            self.source.set(next(label for label,value in self.SOURCES.items() if value==options.source))
            for variable,value in ((self.interval,options.chunk_seconds),(self.folder,options.folder),
                    (self.final_pass,options.final_pass),(self.post_transcribe,options.post_transcribe),
                    (self.recording_format,options.recording_format),(self.recording_bitrate,options.recording_bitrate),
                    (self.recording_pcm_bits,options.recording_pcm_bits),(self.recording_sample_rate,options.recording_sample_rate),
                    (self.mic_gain_var,round(options.mic_gain*100)),(self.loopback_gain_var,round(options.loopback_gain*100))):
                variable.set(value)
            self.recording_channels.set(recording_channel_label(options.recording_channels,options.channel_mode))
            self.all_devices={}
            for kind,identity,variable in (('mic',options.mic_device_id,self.mic_device_var),
                    ('loopback',options.loopback_device_id,self.loopback_device_var),
                    (options.source,options.device_id,self.device_var)):
                if identity:
                    label=f'前回のデバイス [{identity}]'
                    self.all_devices[label]=(kind,identity);variable.set(label)
        self._channel_changed()
        self._filter_devices()

    def _refresh_codec_controls(self):
        fmt=self.recording_format.get()
        self.bitrate_combo.configure(state='readonly' if fmt in ('mp3','m4a') else 'disabled')
        self.pcm_combo.configure(state='readonly' if fmt in ('wav','flac') else 'disabled')

    def _refresh_controls(self):
        recording_only=self.mode.get() == '録音のみ'
        self.interval_combo.configure(state='disabled' if recording_only else 'readonly')
        self.final_box.configure(state='normal',
            variable=self.post_transcribe if recording_only else self.final_pass,
            text='録音終了後に文字起こしする（録音中は認識しない）' if recording_only
                 else '録音終了後に全体を再解析して最終版TXT／CSVを生成（推奨）')
        self.live_hint.configure(text='録音中はAI認識を行いません。録音終了後はキューへ追加し、スイッチがONのときだけTXT／CSVを生成します。'
            if recording_only else '区間ごとの速報は .live.txt に出力します。区間をまたぐ話者IDは暫定です。'
            '保存した録音はキューへ追加します。録音終了後の最終解析には、メイン画面で選択した認識方式を使用します。')

    def _channel_changed(self):
        if RECORDING_CHANNELS[self.recording_channels.get()][1] == 'split_lr':
            self.source.set('マイク＋PC再生音（同時録音）')

    def _browse(self):
        result=filedialog.askdirectory(parent=self.win,initialdir=self.folder.get())
        if result:self.folder.set(result)

    def _filter_devices(self):
        source=self.SOURCES[self.source.get()]
        if source!='mix' and RECORDING_CHANNELS[self.recording_channels.get()][1]=='split_lr':
            self.recording_channels.set('2（ステレオ）')
        values=['既定の録音デバイス'] + [label for label,(kind,_id) in getattr(self,'all_devices',{}).items()
                                                if kind == source]
        self.device_box.configure(values=values,state='disabled' if source=='mix' else 'readonly')
        if self.device_var.get() not in values:self.device_var.set(values[0])
        if source=='mix':
            if not self.mix_box.winfo_manager():
                self.mix_box.pack(fill='x',pady=(6,5),after=self.device_box.master)
        else:
            self.mix_box.pack_forget()
        for key, variable in (('mic',self.mic_device_var),
                              ('loopback',self.loopback_device_var)):
            candidates=['既定の録音デバイス'] + [label for label,(kind,_id) in
                         getattr(self,'all_devices',{}).items() if kind==key]
            self.mix_device_boxes[key].configure(values=candidates)
            if variable.get() not in candidates:variable.set(candidates[0])

    def _refresh_devices(self):
        if self.app.busy:
            self.status.set('別の処理が実行中です。')
            return
        python=self.app.live_capture_python()
        if not python.is_file():
            self.status.set('先に「録音環境をセットアップ」を実行してください。')
            return
        self.app._begin_work()
        self.status.set('録音デバイスを検索中…')
        def run():
            try:
                proc=run_cancellable([str(python),'-X','utf8','-u',str(CODE_DIR/'live_capture.py'),
                                     '--list-devices'],capture_output=True,text=True,
                                    encoding='utf-8',errors='replace',timeout=25,cancel=self.app.stop_event,registry=self.app.processes,
                                    env=utf8_child_env(),creationflags=hidden_child_flags())
                if proc.returncode:
                    raise RuntimeError(proc.stdout or proc.stderr)
                events=[json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith('{')]
                items=next(msg['devices'] for msg in events if msg.get('event')=='devices')
                warnings=[str(msg.get('message','')) for msg in events if msg.get('event')=='warning']
                queue_ui_callback(self.app,self.win,lambda:self._got_devices(items,warnings),self.ui_cancel)
            except Exception as exc:
                err=str(exc)
                self.app.events.put(('errorlog','デバイス取得失敗: '+err+'\n'+traceback.format_exc()))
                queue_ui_callback(self.app,self.win,lambda:self.status.set('デバイスの取得に失敗: '+err[:160]),self.ui_cancel)
        threading.Thread(target=run,daemon=True).start()

    def _probe_devices(self):
        """Probe the selected hardware in the independent recording environment."""
        if self.app.busy:
            self.status.set('録音／別の処理が終了してから入力テストを実行してください。')
            return
        python=self.app.live_capture_python()
        if not python.is_file():
            self.status.set('先に録音環境をセットアップしてください。')
            return
        source=self.SOURCES[self.source.get()]
        cmd=[str(python),'-X','utf8','-u',str(CODE_DIR/'live_capture.py'),
             '--probe-device','--source',source]
        if source=='mix':
            for kind,variable,flag in (('mic',self.mic_device_var,'--mic-device-id'),
                                        ('loopback',self.loopback_device_var,'--loopback-device-id')):
                selected_kind,device_id=getattr(self,'all_devices',{}).get(variable.get(),(kind,''))
                if selected_kind!=kind:
                    self.status.set('音声入力元に対応したデバイスを選択してください。')
                    return
                cmd.extend((flag,device_id))
        else:
            selected_kind,device_id=getattr(self,'all_devices',{}).get(self.device_var.get(),(source,''))
            if selected_kind!=source:
                self.status.set('音声入力元に対応したデバイスを選択してください。')
                return
            cmd.extend(('--device-id',device_id))
        self.app._begin_work()
        self.status.set('選択した入力デバイスをテスト中…')
        self.app.events.put(('log','[入力テスト] 入力元='+source+' / プロセス='+str(python)+'\n'))
        def run():
            try:
                proc=run_cancellable(cmd,capture_output=True,text=True,encoding='utf-8',
                    errors='replace',timeout=30,cancel=self.app.stop_event,registry=self.app.processes,env=utf8_child_env(),
                    cwd=str(BASE),creationflags=hidden_child_flags())
                results=[];errors=[]
                for line in proc.stdout.splitlines():
                    try:msg=json.loads(line)
                    except json.JSONDecodeError:
                        errors.append(line)
                        continue
                    if msg.get('event')=='probe_ok':
                        results.append(f"{msg['device']}: {msg['frames']}フレーム、"
                           f"ピーク {msg['peak']:.4f}" + ('（無音）' if msg['silence'] else ''))
                    elif msg.get('event')=='error':
                        errors.append(msg.get('message','不明なエラー'))
                        if msg.get('detail'): errors.append(msg['detail'])
                if proc.returncode or not results:
                    detail='\n'.join(errors) or proc.stderr or f'終了コード {proc.returncode} / 出力なし'
                    message='入力テスト失敗：'+detail
                    self.app.events.put(('errorlog',
                        f'入力テスト異常終了 (code={proc.returncode}): {detail}\n'
                        f'--- 標準出力 ---\n{proc.stdout}\n--- 標準エラー ---\n{proc.stderr}'))
                else:
                    message='入力テスト成功：'+' / '.join(results)
                self.app.events.put(('log',message+'\n'))
                queue_ui_callback(self.app,self.win,lambda:self.status.set(message[:180] +
                    (' 〔詳細: 診断ログを開く〕' if len(message)>180 else '')),self.ui_cancel)
            except (OSError,subprocess.TimeoutExpired) as exc:
                message=f'入力テスト失敗：{type(exc).__name__}: {exc}'
                self.app.events.put(('log',message+'\n'))
                self.app.events.put(('errorlog',message+'\n'+traceback.format_exc()))
                queue_ui_callback(self.app,self.win,lambda:self.status.set(message[:180] +
                    (' 〔詳細: 診断ログを開く〕' if len(message)>180 else '')),self.ui_cancel)
        threading.Thread(target=run,daemon=True).start()

    def _got_devices(self,items,warnings=None):
        self.all_devices={f'{x["name"]} ({x["source"]}) [{i}]':(x['source'],x['id'])
                          for i,x in enumerate(items)}
        self._filter_devices()
        if warnings:
            for message in warnings:
                self.app.events.put(('log','[録音デバイス] '+message+'\n'))
            self.status.set(f'{len(items)}件検出。ただし一部の検索が失敗: '+warnings[0][:140])
        else:
            self.status.set(f'{len(items)}件の録音デバイスを検出しました。')

    def _start(self,save_only=False):
        try:
            selection=self.device_var.get()
            source=self.SOURCES[self.source.get()]
            kind,device_id=getattr(self,'all_devices',{}).get(selection,(source,''))
            if source != 'mix' and kind!=source:
                raise ValueError('録音元とデバイスの種類が一致しません。')
            mic_device_id=loopback_device_id=''
            mic_gain=loopback_gain=1.0
            if source=='mix':
                for source_key,selected in (('mic',self.mic_device_var.get()),
                                            ('loopback',self.loopback_device_var.get())):
                    selected_kind,selected_id=getattr(self,'all_devices',{}).get(selected,(source_key,''))
                    if selected_kind!=source_key:
                        raise ValueError('ミックスするデバイスの種類が一致しません。')
                    if source_key=='mic':mic_device_id=selected_id
                    else:loopback_device_id=selected_id
                mic_gain=float(self.mic_gain_var.get())/100
                loopback_gain=float(self.loopback_gain_var.get())/100
            opts=LiveOptions(source=source, mode=self.MODES[self.mode.get()],
                    chunk_seconds=int(self.interval.get()),device_id=device_id if source!='mix' else '',
                    mic_device_id=mic_device_id,loopback_device_id=loopback_device_id,
                    mic_gain=mic_gain,loopback_gain=loopback_gain,
                    folder=Path(self.folder.get()).expanduser(),final_pass=self.final_pass.get(),
                    recording_format=self.recording_format.get(),
                    recording_bitrate=self.recording_bitrate.get(),
                    recording_sample_rate=int(self.recording_sample_rate.get()),
                    recording_channels=RECORDING_CHANNELS[self.recording_channels.get()][0],
                    channel_mode=RECORDING_CHANNELS[self.recording_channels.get()][1],
                    post_transcribe=self.post_transcribe.get(),
                    recording_pcm_bits=int(self.recording_pcm_bits.get()))
            opts.validate()
            self.app.save_capture_preferences('live',opts,self.skip_settings.get())
            if save_only:self.status.set('設定を保存しました。')
            else:self.app.start_live(opts)
        except (ValueError,OSError) as exc:
            show_error(self.win,str(exc))

    def _tick(self):
        if not self.win.winfo_exists():return
        if self.app.live_active:
            self.start_btn.configure(state='disabled')
            self.end_btn.configure(state='disabled' if self.app.live_stopping else 'normal')
            self.status.set(self.app.status.get() if self.app.live_stopping else
                '録音／文字起こしを実行中。終了ボタンで録音を確定します。')
        else:
            self.end_btn.configure(state='disabled')
            self.start_btn.configure(state='normal' if not self.app.busy else 'disabled')
            main_status=self.app.status.get()
            if main_status.startswith('ライブ録音エラー:'):
                self.status.set(main_status)
        self.win.after(350,self._tick)


def _preview_pages(text: str, max_chars: int = 400, max_newlines: int = 12) -> list[str]:
    """Bound each inline preview page without dropping or modifying text."""
    pages, start, newlines = [], 0, 0
    for index, char in enumerate(text):
        newlines += char == '\n'
        if index + 1 - start >= max_chars or newlines >= max_newlines:
            pages.append(text[start:index+1])
            start, newlines = index+1, 0
    if start < len(text):
        pages.append(text[start:])
    return pages or ['']


class SpeakerEditDialog:
    """Rename only recognized speaker-label fields, with a read-only preview.

    Drag-and-drop accepts one TXT/CSV file. Preview groups never alter words or
    source files, even if a phrase contains a string like SPEAKER_00.
    """

    def __init__(self, app: 'UnifiedApp', source: Path | None = None):
        self.app = app
        self.win = modal(app.root, '話者名を設定（本文は変更しない）', 820)
        self.win.geometry('820x710')
        self.input = tk.StringVar(self.win)
        self.output = tk.StringVar(self.win)
        self.rename_vars: dict[str, tk.StringVar] = {}
        self.utterances: dict[str, list[tuple[str, str]]] = {}
        self.preview_panels: dict[str, ttk.Frame] = {}
        self.preview_buttons: dict[str, ttk.Button] = {}
        self.open_previews: set[str] = set()
        self.loaded_path: Path | None = None

        container = ttk.Frame(self.win, padding=16)
        container.pack(fill='both', expand=True)
        ttk.Label(container,
            text='話者分離済みTXT／CSVを読み込み、発話内容を確認しながら話者名を割り当てます。',
            wraplength=765).pack(anchor='w', pady=(0, 8))
        inputrow = ttk.Frame(container)
        inputrow.pack(fill='x', pady=4)
        ttk.Label(inputrow, text='入力ファイル').pack(side='left', padx=(0, 8))
        self.input_entry = ttk.Entry(inputrow, textvariable=self.input)
        self.input_entry.pack(side='left', fill='x', expand=True)
        IconButton(inputrow, text='参照…', command=self._browse).pack(side='left', padx=(7, 0))
        IconButton(inputrow, text='解析', command=self._scan).pack(side='left', padx=(7, 0))
        self.drop_hint = ttk.Label(container, text='TXT／CSVをここへドラッグ＆ドロップ（1ファイル）',
                                   foreground='#456480', padding=(8, 8), relief='groove')
        self.drop_hint.pack(fill='x', pady=(3, 8))
        self._register_dnd(inputrow)

        heading = ttk.Frame(container)
        heading.pack(fill='x', pady=(6, 3))
        ttk.Label(heading, text='各話者の名前と発話プレビュー',
                  font=(UI_FONT, 10, 'bold')).pack(side='left')
        IconButton(heading, text='すべて閉じる', command=self._collapse_all).pack(side='right')
        IconButton(heading, text='すべて開く', command=self._expand_all).pack(side='right', padx=(0, 6))
        ttk.Label(container,
            text='▶ で発話を展開できます。時刻と本文は確認用の表示で、名前保存時には話者ラベルだけを変更します。',
            foreground='#56667d', wraplength=765).pack(anchor='w', pady=(0, 6))
        wrap = ttk.Frame(container)
        wrap.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0)
        scroll = ttk.Scrollbar(wrap, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.mapping_frame = ttk.Frame(self.canvas, padding=(4, 3, 10, 8))
        frame_id = self.canvas.create_window((0, 0), window=self.mapping_frame, anchor='nw')
        self.mapping_frame.bind('<Configure>',
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',
            lambda e: self.canvas.itemconfigure(frame_id, width=e.width))
        ttk.Label(self.mapping_frame,
            text='ファイルを選択するとSPEAKERごとの発話が表示されます。',
            foreground='#64718a').pack(anchor='w', pady=14)

        self.hint = ttk.Label(container,
            text='同じSPEAKER_00でも、録音ファイルが異なれば別人の可能性があります。',
            wraplength=765, foreground='#725728')
        self.hint.pack(anchor='w', pady=(8, 6))
        outrow = ttk.Frame(container)
        outrow.pack(fill='x', pady=(4, 9))
        ttk.Label(outrow, text='別名保存').pack(side='left', padx=(0, 8))
        ttk.Entry(outrow, textvariable=self.output).pack(side='left', fill='x', expand=True)
        IconButton(outrow, text='参照…', command=self._output_browse).pack(side='left', padx=(7, 0))
        destination_buttons(container,same_command=self._same_input_location,
            default_command=self._default_location).pack(anchor='w',pady=(0,7))
        foot = ttk.Frame(container)
        foot.pack(fill='x', pady=(4, 0))
        IconButton(foot, text='キャンセル', command=self.win.destroy).pack(side='right', padx=(8, 0))
        IconButton(foot, text='話者名を置換して保存', style='Primary.TButton', command=self._save).pack(side='right')
        if getattr(app, 'pending_speaker_edits', ()):
            def cancel_remaining():
                app.pending_speaker_edits.clear()
                self.win.destroy()
            IconButton(foot, text='残りの自動表示を終了', command=cancel_remaining).pack(side='left')
        if source is not None:
            self.input.set(str(source))
            self._scan()

    def _register_dnd(self, inputrow: ttk.Frame) -> None:
        if self.app.dnd_startup_error or DND_FILES is None:
            self.drop_hint.configure(text='D&Dを利用できません。メイン画面の「D&D 診断・修復…」または参照を使用してください。')
            return
        try:
            for widget in (self.input_entry, self.drop_hint, inputrow):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._drop_input)
                widget.dnd_bind('<<DropEnter>>', lambda _event: 'copy')
        except (tk.TclError, AttributeError, RuntimeError) as exc:
            self.drop_hint.configure(text='D&Dの登録に失敗しました。参照ボタンを利用してください。')
            self.app.log(f'[話者名設定] D&D登録失敗: {exc}\n')

    def _drop_input(self, event) -> str:
        try:
            paths = parse_drop_paths(event.data, event.widget.tk.splitlist)
            if len(paths) != 1:
                raise ValueError('話者名設定は1ファイルずつ処理します。TXTまたはCSVを1件だけドロップしてください。')
            path = paths[0]
            if not path.is_file() or path.suffix.lower() not in ('.txt', '.csv'):
                raise ValueError('TXTまたはCSVのファイルをドロップしてください。')
            self.input.set(str(path))
            self._scan()
        except (ValueError, OSError, tk.TclError) as exc:
            show_error(self.win, str(exc))
        return 'copy'

    def _browse(self):
        chosen = filedialog.askopenfilename(parent=self.win,
                filetypes=[('話者付きTXT・CSV', '*.txt *.csv')])
        if chosen:
            self.input.set(chosen)
            self._scan()

    def _scan(self):
        try:
            path = Path(self.input.get().strip())
            if not path.is_file():
                raise ValueError('存在するTXTまたはCSVを選択してください。')
            utterances = speaker_utterances(path)
            if not utterances:
                raise ValueError('話者ラベルが見つかりません。')
            # Build the full view only after parsing succeeds. Avoid leaving
            # stale mappings in the UI when a new file cannot be parsed.
            for widget in self.mapping_frame.winfo_children():
                widget.destroy()
            self.utterances = utterances
            self.rename_vars.clear()
            self.preview_panels.clear()
            self.preview_buttons.clear()
            self.open_previews.clear()
            for speaker, lines in utterances.items():
                group = ttk.LabelFrame(self.mapping_frame, text=speaker, padding=(9, 7))
                group.pack(fill='x', expand=True, pady=(2, 8))
                row = ttk.Frame(group)
                row.pack(fill='x')
                ttk.Label(row, text='割当名：').pack(side='left')
                name_var = tk.StringVar(self.win)
                ttk.Entry(row, textvariable=name_var).pack(side='left', fill='x', expand=True,
                                                            padx=(2, 10))
                self.rename_vars[speaker] = name_var
                toggle = IconButton(row, text=f'▶ 発話を表示（{len(lines)}件）',
                                    command=lambda key=speaker: self._toggle_speaker(key))
                toggle.pack(side='right')
                preview = ttk.Frame(group)
                self.preview_panels[speaker] = preview
                self.preview_buttons[speaker] = toggle
            self.loaded_path = path.resolve()
            self.output.set(str(default_directory('speaker_edits') / (path.stem + '_話者名設定' + path.suffix)))
            total = sum(map(len, utterances.values()))
            self.hint.configure(text=f'{len(utterances)}人・{total}件の発話を検出しました。'
                                     '原文とメタデータは変更せず、話者ラベルだけを置換します。')
        except (OSError, ValueError, UnicodeError, csv.Error) as exc:
            self.loaded_path = None
            show_error(self.win, str(exc))

    def _toggle_speaker(self, speaker: str) -> None:
        pane = self.preview_panels[speaker]
        button = self.preview_buttons[speaker]
        if speaker in self.open_previews:
            pane.pack_forget()
            self.open_previews.remove(speaker)
            button.configure(text=f'▶ 発話を表示（{len(self.utterances[speaker])}件）')
            return
        if not pane.winfo_children():
            lines = self.utterances[speaker]
            full_text = ''.join((f'[{when}] ' if when else '[時刻不明] ') +
                                (text if text else '（本文なし）') + '\n\n'
                                for when, text in lines)
            pages = _preview_pages(full_text)
            preview_wrap = ttk.Frame(pane)
            preview_wrap.pack(fill='x', expand=True, pady=(8, 2))
            box = tk.Text(preview_wrap, wrap='word', height=3, width=55,
                          font=(UI_FONT, 10), relief='flat', borderwidth=2,
                          padx=8, pady=6, takefocus=1)
            box._scroll_outer = True
            box.pack(fill='x', expand=True)
            controls = ttk.Frame(pane)
            controls.pack(fill='x', pady=(0, 4))
            current = [0]
            page_label = tk.StringVar(self.win)
            def resize_preview(_event=None):
                try:
                    count = box.count('1.0', 'end-1c', 'displaylines')
                    height = max(3, (count[0] if count else 0) + 1)
                    if int(box.cget('height')) != height:
                        box.configure(height=height)
                except tk.TclError:
                    pass
            def show_page(offset=0):
                current[0] = max(0, min(len(pages)-1, current[0]+offset))
                box.configure(state='normal')
                box.delete('1.0', 'end')
                box.insert('1.0', pages[current[0]])
                box.configure(state='disabled')
                page_label.set(f'{current[0]+1} / {len(pages)} ページ')
                previous.configure(state='normal' if current[0] else 'disabled')
                following.configure(state='normal' if current[0]+1 < len(pages) else 'disabled')
                box.after_idle(resize_preview)
            previous = IconButton(controls, text='前のページ', command=lambda:show_page(-1))
            previous.pack(side='left')
            following = IconButton(controls, text='次のページ', command=lambda:show_page(1))
            following.pack(side='left', padx=(6, 0))
            ttk.Label(controls, textvariable=page_label, style='Small.TLabel').pack(side='left', padx=8)
            def copy_all():
                self.win.clipboard_clear()
                self.win.clipboard_append(full_text)
            IconButton(controls, text='この話者の発話をコピー', icon='copy', command=copy_all).pack(side='right')
            box.bind('<Configure>', resize_preview, add='+')
            show_page()
        pane.pack(fill='x', expand=True)
        self.open_previews.add(speaker)
        button.configure(text='▼ 発話を閉じる')

    def _expand_all(self) -> None:
        for speaker in self.utterances:
            if speaker not in self.open_previews:
                self._toggle_speaker(speaker)

    def _collapse_all(self) -> None:
        for speaker in list(self.open_previews):
            self._toggle_speaker(speaker)

    def _same_input_location(self):
        try:
            source = Path(self.input.get().strip()).expanduser()
            folder = input_folder(self.input.get())
            name = Path(self.output.get()).name if self.output.get() else source.stem+'_話者名設定'+source.suffix
            self.output.set(str(Path(folder)/name))
        except ValueError as exc:
            show_error(self.win,str(exc))

    def _output_browse(self):
        extension = Path(self.input.get()).suffix.lower() or '.txt'
        chosen = filedialog.asksaveasfilename(parent=self.win, defaultextension=extension,
                    filetypes=[('元ファイルと同じ形式', '*' + extension)],
                    initialdir=str(default_directory('speaker_edits')))
        if chosen:
            self.output.set(chosen)

    def _default_location(self):
        if not self.input.get().strip():show_error(self.win,'先に入力ファイルを選択してください。');return
        source=Path(self.input.get())
        name=Path(self.output.get()).name if self.output.get() else source.stem+'_話者名設定'+source.suffix
        self.output.set(str(default_directory('speaker_edits')/name))

    def _save(self):
        if self.app.busy:
            show_error(self.win, '処理中は話者名の変更ができません。')
            return
        try:
            source = Path(self.input.get().strip())
            dest = Path(self.output.get().strip())
            if not source.is_file():
                raise ValueError('入力ファイルを選択してください。')
            if self.loaded_path is None or source.resolve() != self.loaded_path:
                raise ValueError('入力ファイルが変更されています。「解析」を押してから保存してください。')
            names = {key: var.get() for key, var in self.rename_vars.items()}
            saved, count = replace_speaker_labels(source, dest, names)
            self.app.log(f'[話者名変更] {count}発話のラベルを置換: {saved}\n')
            messagebox.showinfo('保存完了', f'{count}発話の話者ラベルを変更しました。\n{saved}',
                                parent=self.win)
            self.win.destroy()
        except (OSError, ValueError, UnicodeError) as exc:
            show_error(self.win, str(exc))


def human_readable_engine_progress(update: EngineUpdate) -> str:
    """Translate structured progress into a readable line for detailed logs."""
    fraction = f' {update.fraction:.0%}' if update.fraction is not None else ''
    info = f' / {update.detail}' if update.detail else ''
    snippet = update.snippet.strip().replace('\n', ' ')
    # Text preview keeps the full recognition; log keeps a compact summary.
    if len(snippet) > 180:
        snippet = snippet[:180] + '…'
    text = f' / 認識: {snippet}' if snippet else ''
    return f'[進捗] {update.stage}{fraction}{info}{text}'


class GameBarRecordingDialog:
    """One-click Windows Game Bar hotkey + separate, continuous audio capture."""
    def __init__(self, app):
        self.app=app
        self.win=modal(app.root,'Windows標準録画＋アプリ内音声録音',800)
        self.ui_cancel=ui_window_cancel(self.win)
        self.win.geometry(f'800x{min(760,max(560,self.win.winfo_screenheight()-90))}')
        shell=ttk.Frame(self.win,padding=16)
        shell.pack(fill='both',expand=True)
        buttons=ttk.Frame(shell)
        buttons.pack(fill='x',side='bottom')
        IconButton(buttons,text='録音環境をセットアップ',command=app.start_capture_setup).pack(side='left')
        IconButton(buttons,text='閉じる',command=self.win.destroy).pack(side='right',padx=(9,0))
        IconButton(buttons,text='■ 録画を停止',command=app.stop).pack(side='right',padx=5)
        IconButton(buttons,text='● 録画を開始',style='Primary.TButton',command=self._start).pack(side='right',padx=5)
        area=ttk.Frame(shell);area.pack(fill='both',expand=True)
        canvas=tk.Canvas(area,highlightthickness=0)
        vbar=ttk.Scrollbar(area,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side='left',fill='both',expand=True);vbar.pack(side='right',fill='y')
        body=ttk.Frame(canvas,padding=(3,2,11,10))
        item=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda ev:canvas.itemconfigure(item,width=ev.width))
        ttk.Label(body,text='Game Bar録画と音声録音を連動',font=(UI_FONT,14,'bold')).pack(anchor='w',pady=(0,8))
        ttk.Label(body,text='映像はWindowsのGame Bar（録画対象はアプリ単位）、音声はこのアプリで独立して連続収録。'
                  '画面の直接FFmpeg録画は使用しません。Game Barが無効の場合も音声WAVは保持します。',
                  wraplength=740,foreground='#526178').pack(anchor='w',pady=(0,12))
        self.shortcut=tk.StringVar(self.win,value=getattr(app.config,'gamebar_shortcut','win+alt+r'))
        self.source=tk.StringVar(self.win,value=getattr(app.config,'gamebar_audio_source','mix'))
        self.captures=tk.StringVar(self.win,value=getattr(app.config,'gamebar_captures_dir','') or str(captures_directory()))
        self.folder=tk.StringVar(self.win,value=getattr(app.config,'live_recording_dir','') or str(default_directory('recordings')))
        self.auto=tk.BooleanVar(self.win,value=True)
        self.realtime=tk.BooleanVar(self.win,value=False)
        self.interval=tk.StringVar(self.win,value='20')
        self.sync=tk.BooleanVar(self.win,value=True)
        self.audio_format=tk.StringVar(self.win,value=app.config.live_recording_format)
        self.recording_channels=tk.StringVar(self.win,recording_channel_label(
            app.config.live_recording_channels,app.config.live_channel_mode))
        self.windows={}
        self.target=tk.StringVar(self.win)
        def field(title,widget,helptext=None):
            ttk.Label(body,text=title).pack(anchor='w',pady=(8,3))
            widget.pack(fill='x')
            if helptext:
                ttk.Label(body,text=helptext,wraplength=730,foreground='#526178').pack(anchor='w',pady=(2,2))
        targets=ttk.Frame(body)
        self.window_combo=ttk.Entry(targets,textvariable=self.target,state='readonly')
        self.window_combo.pack(side='left',fill='x',expand=True)
        self.window_combo.bind('<Button-1>',lambda _e:self._pick_window())
        IconButton(targets,text='画面を選ぶ…',command=self._pick_window).pack(side='left',padx=7)
        field('録画対象アプリ（開始時に選択）',targets,
              '設定の保存時は未選択で構いません。Game Barはアプリ単位の録画です。対象アプリは最小化しないでください。')
        shortcut_row=ttk.Frame(body)
        ttk.Entry(shortcut_row,textvariable=self.shortcut).pack(side='left',fill='x',expand=True)
        IconButton(shortcut_row,text='キー入力…',command=self._edit_trigger).pack(side='left',padx=(7,0))
        field('録画開始／停止のショートカット',shortcut_row,
              '既定 win+alt+r。Game Bar側で変更した場合は同じキーを指定（例：ctrl+shift+f9）。')
        sources=ttk.Combobox(body,textvariable=self.source,state='readonly',
            values=('mix（マイク＋PC再生音）','mic（マイク）','loopback（PC再生音）'))
        if self.source.get() in ('mix','mic','loopback'):
            sources.set(next(label for label in sources.cget('values') if label.startswith(self.source.get()+'（')))
        field('アプリ内で独立録音する音声',sources)
        self.source_combo=sources
        field('保存チャンネル',ttk.Combobox(body,textvariable=self.recording_channels,
              values=tuple(RECORDING_CHANNELS),state='readonly'),
              'L/R分離はLにPC音（モノラル）、Rにマイクを保存します。分離した編集にはWAVを利用できます。')
        bind_variable_trace(self.win, self.recording_channels, 'write',lambda *_:self._channel_changed())
        bind_variable_trace(self.win, self.source, 'write',lambda *_:self._source_changed())
        self.device_choices={}
        self.mic_device=tk.StringVar(self.win,value='既定のマイク')
        self.loopback_device=tk.StringVar(self.win,value='既定のPC再生音')
        device_wrap=ttk.LabelFrame(body,text='録音デバイス（任意・未選択ならWindows既定）',padding=(8,5))
        device_wrap.pack(fill='x',pady=(7,2))
        self.mic_list=ttk.Combobox(device_wrap,textvariable=self.mic_device,state='readonly',values=['既定のマイク'])
        self.mic_list.pack(fill='x',pady=2)
        self.loopback_list=ttk.Combobox(device_wrap,textvariable=self.loopback_device,state='readonly',values=['既定のPC再生音'])
        self.loopback_list.pack(fill='x',pady=2)
        IconButton(device_wrap,text='録音デバイス一覧を更新',command=self._refresh_devices).pack(anchor='w',pady=3)
        self.devices_status=tk.StringVar(self.win,value='既定デバイスを使用／個別に指定する場合は一覧更新')
        ttk.Label(device_wrap,textvariable=self.devices_status,foreground='#526178').pack(anchor='w')
        field('音声保存形式（WAVを安全に録音し、停止後に変換）',
              ttk.Combobox(body,textvariable=self.audio_format,state='readonly',values=('wav','mp3','m4a','flac')),
              'WAV以外を選択した場合も、復旧のため元WAVを残します。ビットレート等は詳細設定の録音設定を使用。')
        for caption,var in [('Game Barの動画保存フォルダー',self.captures),('アプリ側の録音保存先',self.folder)]:
            group=ttk.Frame(body)
            ttk.Entry(group,textvariable=var).pack(side='left',fill='x',expand=True)
            def browse(v=var):
                initial = (captures_directory() if v is self.captures else default_directory('recordings'))
                folder=filedialog.askdirectory(parent=self.win,initialdir=v.get() or str(initial))
                if folder:v.set(folder)
            IconButton(group,text='参照…',command=browse).pack(side='left',padx=7)
            field(caption,group)
            if var is self.folder:
                destination_buttons(body,same_text='動画の入力と同じ場所',same_command=lambda:self.folder.set(self.captures.get()),
                    default_command=lambda:self.folder.set(str(default_directory('recordings')))).pack(anchor='w',pady=5)
        ToggleSwitch(body,text='録画中にライブ文字起こし（左のプレビューへ追記）',
                     variable=self.realtime,command=self._refresh_live_controls).pack(anchor='w',pady=(12,2))
        interval_row=ttk.Frame(body);interval_row.pack(fill='x',pady=4)
        ttk.Label(interval_row,text='ライブ認識の区間（秒）').pack(side='left',padx=(0,12))
        self.interval_combo=ttk.Combobox(interval_row,textvariable=self.interval,
            values=('10','20','30','60'),width=6,state='disabled')
        self.interval_combo.pack(side='left')
        ttk.Label(body,text='速報は .live.txt にも保存します。話者番号は区間ごとの暫定値です。',
                  wraplength=735,foreground='#526178').pack(anchor='w',pady=(2,6))
        ToggleSwitch(body,text='停止後に音声WAVを自動文字起こし（全文を最初から最終解析）',variable=self.auto).pack(anchor='w',pady=(4,2))
        ToggleSwitch(body,text='Game BarのMP4が見つかれば、独立録音を同期して別MP4も作成',variable=self.sync).pack(anchor='w')
        ttk.Label(body,text='録音は先に開始し、対象ウィンドウへフォーカスを切り替えた後でショートカットを送信します。'
              'Windowsが録画を受け付けたかはMP4の生成で確認します。検出失敗時も音声を保存・文字起こしします。',
              wraplength=735,foreground='#526178').pack(anchor='w',pady=(10,7))
        prefs=ttk.Frame(shell);prefs.pack(fill='x',side='bottom',pady=7,before=area)
        self.skip_settings=tk.BooleanVar(self.win,app.config.skip_gamebar_settings)
        ToggleSwitch(prefs,text='次回以降は設定画面をスキップする',variable=self.skip_settings).pack(side='left')
        IconButton(prefs,text='設定を保存',command=lambda:self._start(save_only=True)).pack(side='right')
        IconButton(prefs,text='ショートカット…',command=app.edit_shortcuts).pack(side='right',padx=6)
        self._load_previous()
        self._refresh_windows()

    def _channel_changed(self):
        if RECORDING_CHANNELS[self.recording_channels.get()][1]=='split_lr':
            self.source.set('mix（マイク＋PC再生音）')

    def _refresh_live_controls(self):
        self.interval_combo.configure(state='readonly' if self.realtime.get() else 'disabled')

    def _source_changed(self):
        if not self.source.get().startswith('mix') and RECORDING_CHANNELS[self.recording_channels.get()][1]=='split_lr':
            self.recording_channels.set('2（ステレオ）')

    def _edit_trigger(self):
        from hotkey_ui import KeyCaptureDialog
        KeyCaptureDialog(self.win,self.shortcut.get(),self.shortcut.set,getattr(self.app,'hotkeys',None),
                         allow_function_only=False)

    def _load_previous(self):
        previous=saved_options(self.app.config,'gamebar')
        self.previous_target=previous
        if previous is not None:
            for variable,value in ((self.shortcut,previous.shortcut),(self.captures,previous.captures),
                    (self.folder,previous.destination),(self.auto,previous.auto_transcribe),
                    (self.sync,previous.create_synced_video),(self.audio_format,previous.audio_format),
                    (self.realtime,previous.realtime_transcription),(self.interval,previous.chunk_seconds)):
                variable.set(value)
            self.source.set(next(value for value in self.source_combo.cget('values') if value.startswith(previous.source+'（')))
            self.recording_channels.set(recording_channel_label(previous.recording_channels,previous.channel_mode))
            for kind,identity,variable in (('mic',previous.mic_device_id,self.mic_device),
                    ('loopback',previous.loopback_device_id,self.loopback_device)):
                if identity:
                    label=f'前回のデバイス [{identity}]'
                    self.device_choices[label]=(kind,identity);variable.set(label)
        self._channel_changed()
        self._refresh_live_controls()

    def _refresh_windows(self):
        from screen_capture import windows
        self.window_titles={hwnd:title for title,hwnd in windows() if '文字起こし統合スタジオ' not in title}
        self.windows={f'{title[:85]}  [HWND {hwnd}]':hwnd for hwnd,title in self.window_titles.items()
                      if '文字起こし統合スタジオ' not in title}
        if self.target.get() not in self.windows:
            previous=getattr(self,'previous_target',None)
            identity=match_window(previous,[(title,hwnd) for hwnd,title in self.window_titles.items()]) if previous else None
            self.target.set(next((title for title,hwnd in self.windows.items() if hwnd==identity),
                                 '画面を選択してください' if self.windows else '録画対象が見つかりません'))

    def _pick_window(self):
        from window_picker import WindowPickerDialog
        def choose(title,hwnd):
            label=f'{title[:85]}  [HWND {hwnd}]'
            self.windows[label]=hwnd;self.window_titles[hwnd]=title;self.target.set(label)
        return WindowPickerDialog(self.win,self.windows.get(self.target.get()),choose)


    def _refresh_devices(self):
        python=self.app.live_capture_python()
        if not python.is_file():
            self.devices_status.set('録音環境が未セットアップです。一括セットアップまたは下のボタンを使用してください。')
            return
        if self.app.busy:return
        self.app._begin_work()
        self.devices_status.set('入力デバイスを取得中…')
        def run():
            try:
                cmd=[str(python),'-X','utf8','-u',str(CODE_DIR/'live_capture.py'),'--list-devices']
                result=run_cancellable(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace',
                        timeout=25,cancel=self.app.stop_event,registry=self.app.processes,creationflags=hidden_child_flags(),env=utf8_child_env())
                if result.returncode:raise RuntimeError((result.stderr or result.stdout)[-900:])
                events=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
                devices=next(ev['devices'] for ev in events if ev.get('event')=='devices')
                def display():
                    if not self.win.winfo_exists():return
                    self.device_choices={f'{it["name"]} [{it["id"]}]':(it['source'],it['id']) for it in devices}
                    for kind,var,combo,fallback in [('mic',self.mic_device,self.mic_list,'既定のマイク'),
                                                    ('loopback',self.loopback_device,self.loopback_list,'既定のPC再生音')]:
                        labels=[fallback]+[label for label,(source,_) in self.device_choices.items() if source==kind]
                        combo.configure(values=labels)
                        if var.get() not in labels:var.set(fallback)
                    self.devices_status.set(f'{len(devices)}件のデバイスを取得')
                queue_ui_callback(self.app,self.win,display,self.ui_cancel)
            except Exception as exc:
                message=f'デバイス一覧を取得できません: {exc}'
                self.app.events.put(('errorlog',message+'\n'+traceback.format_exc()))
                queue_ui_callback(self.app,self.win,lambda:self.devices_status.set(message[:200]),self.ui_cancel)
        threading.Thread(target=run,daemon=True).start()

    def _preview(self):
        if self.target.get() not in self.windows:
            show_error(self.win,'録画対象を選択してください。');return
        viewer=tk.Toplevel(self.win)
        viewer_cancel=ui_window_cancel(viewer)
        viewer.title('録画対象の静止画プレビュー')
        viewer.geometry('490x330')
        label=ttk.Label(viewer,text='プレビューを読み込み中…',anchor='center')
        label.pack(fill='both',expand=True,padx=10,pady=10)
        target=f'hwnd={self.windows[self.target.get()]}'
        def worker():
            try:
                from window_preview import capture_preview
                ppm,width,height=capture_preview(target,440,245)
                def display():
                    if not viewer.winfo_exists():return
                    try:
                        photo=tk.PhotoImage(data=ppm,format='PPM',master=viewer)
                        label.configure(image=photo,text='')
                        label.image=photo
                        viewer.title(f'ウィンドウプレビュー {width} × {height}（静止画）')
                    except (tk.TclError,ValueError) as exc:
                        label.configure(text='画像を取得できません: '+str(exc)[:180])
                queue_ui_callback(self.app,viewer,display,viewer_cancel)
            except Exception as exc:
                message='画像を取得できません: '+str(exc)[:180]
                self.app.events.put(('errorlog','画面プレビュー取得失敗:\n'+traceback.format_exc()))
                queue_ui_callback(self.app,viewer,lambda:label.configure(text=message),viewer_cancel)
        threading.Thread(target=worker,daemon=True).start()

    def _start(self,save_only=False):
        try:
            key=self.shortcut.get().strip().lower()
            from gamebar_recording import parse_hotkey
            parse_hotkey(key)
            source=self.source_combo.get().split('（',1)[0]
            identity=self.windows.get(self.target.get())
            opts=GameBarOptions(shortcut=key,target_hwnd=identity,
                mic_device_id=self.device_choices.get(self.mic_device.get(),('mic',''))[1],
                loopback_device_id=self.device_choices.get(self.loopback_device.get(),('loopback',''))[1],
                source=source,captures=Path(self.captures.get()).expanduser(),
                destination=Path(self.folder.get()).expanduser(),
                auto_transcribe=self.auto.get(),create_synced_video=self.sync.get(),
                audio_format=self.audio_format.get(),
                recording_channels=RECORDING_CHANNELS[self.recording_channels.get()][0],
                channel_mode=RECORDING_CHANNELS[self.recording_channels.get()][1],
                realtime_transcription=self.realtime.get(),chunk_seconds=int(self.interval.get()),
                target_title=self.window_titles.get(identity,''))
            opts.validate()
            self.app.save_capture_preferences('gamebar',opts,self.skip_settings.get())
            if save_only:messagebox.showinfo('保存完了','録画設定を保存しました。',parent=self.win)
            elif identity is not None:
                if self.app.start_gamebar_recording(opts):self.win.destroy()
            else:
                from window_picker import WindowPickerDialog
                def begin(title,hwnd):
                    if self.app.start_gamebar_recording(replace(opts,target_hwnd=hwnd,target_title=title)):
                        self.win.destroy()
                WindowPickerDialog(self.win,None,begin,accept_text='選択して録画を開始')
        except (OSError,ValueError) as exc:show_error(self.win,str(exc))


class StandardRecordingDialog:
    """Import clips recorded by Windows Game Bar / Snipping Tool, optionally sync audio."""
    def __init__(self, app):
        self.app=app
        self.win=modal(app.root,'Windows標準録画を取り込み・音声同期・文字起こし',820)
        self.win.geometry('820x695')
        shell=ttk.Frame(self.win,padding=(12,8))
        shell.pack(fill='both',expand=True)
        scroll=ttk.Frame(shell)
        scroll.pack(fill='both',expand=True)
        canvas=tk.Canvas(scroll,highlightthickness=0)
        bar=ttk.Scrollbar(scroll,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side='left',fill='both',expand=True)
        bar.pack(side='right',fill='y')
        root=ttk.Frame(canvas,padding=(5,4,8,12))
        widget=canvas.create_window((0,0),window=root,anchor='nw')
        root.bind('<Configure>',lambda _ev:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda ev:canvas.itemconfigure(widget,width=ev.width))
        ttk.Label(root,text='Windowsの録画機能を使って軽く収録し、動画を取り込む',
                  font=(UI_FONT,13,'bold')).pack(anchor='w',pady=(0,8))
        ttk.Label(root,text='① 録画対象のアプリで Win + Alt + R を押す（録画開始／停止）。'
              ' Win + G で設定を開けます。録画は通常「ビデオ\\キャプチャ」に保存されます。\n'
              '※ デスクトップ全体やエクスプローラーの収録には、Windows 11の切り取りツール（Win + Shift + R）等をご利用ください。',
                  foreground='#40516a',wraplength=765,justify='left').pack(anchor='w',pady=4)
        shortcuts=ttk.Frame(root)
        shortcuts.pack(fill='x',pady=(2,12))
        IconButton(shortcuts,text='Game Barを開く（Win+G）',command=self.open_game_bar).pack(side='left')
        IconButton(shortcuts,text='Windowsのキャプチャフォルダーを開く',command=self.open_captures).pack(side='left',padx=7)
        self.video=tk.StringVar(self.win)
        self.audio=tk.StringVar(self.win)
        self.offset=tk.StringVar(self.win,value='0.0')
        self.mode=tk.StringVar(self.win,value='元動画の音声を残して別音声を追加（ミックス）')
        self.dest=tk.StringVar(self.win,value=str(default_directory('recordings')))
        self.auto=tk.BooleanVar(self.win,value=True)
        def picker(title,var,types):
            ttk.Label(root,text=title).pack(anchor='w',pady=(9,4))
            line=ttk.Frame(root);line.pack(fill='x')
            entry=ttk.Entry(line,textvariable=var)
            entry.pack(side='left',fill='x',expand=True)
            def browse():
                path=filedialog.askopenfilename(parent=self.win,filetypes=types)
                if path:var.set(path)
            IconButton(line,text='参照…',command=browse).pack(side='left',padx=6)
            # Optional native Explorer drop, no dependency if D&D is unavailable.
            if DND_FILES is not None and hasattr(entry,'drop_target_register'):
                entry.drop_target_register(DND_FILES)
                entry.dnd_bind('<<Drop>>',lambda e:var.set(str(parse_drop_paths(e.data,self.win.tk.splitlist)[0])) if parse_drop_paths(e.data,self.win.tk.splitlist) else None)
        picker('② Windows標準録画で保存した動画（MP4など）',self.video,
               [('動画ファイル','*.mp4 *.mkv *.mov *.avi *.webm'),('すべて','*.*')])
        picker('③ 別録音した音声（任意・動画に音が入っていれば空欄のままでOK）',self.audio,
               [('音声ファイル','*.wav *.m4a *.mp3 *.flac *.aac *.ogg *.wma'),('すべて','*.*')])
        ttk.Label(root,text='Game Barで音声も録れていれば②だけ選べばよく、音声合成・再エンコードはしません。',
                foreground='#40516a',wraplength=760).pack(anchor='w',pady=5)
        self.audio_panel=ttk.LabelFrame(root,text='別録音の音声がある場合だけ使用する設定',padding=10)
        self.audio_panel.pack(fill='x',pady=(10,5))
        ttk.Label(self.audio_panel,text='音声の合成方法').grid(row=0,column=0,sticky='w',pady=5)
        ttk.Combobox(self.audio_panel,textvariable=self.mode,state='readonly',width=43,
           values=['元動画の音声を残して別音声を追加（ミックス）','別音声で元動画の音声を置き換える']).grid(row=0,column=1,sticky='w',padx=12)
        ttk.Label(self.audio_panel,text='別音声の開始位置の補正（秒）').grid(row=1,column=0,sticky='w',pady=5)
        ttk.Spinbox(self.audio_panel,from_=-3600,to=3600,increment=0.1,textvariable=self.offset,width=13).grid(row=1,column=1,sticky='w',padx=12)
        ttk.Label(self.audio_panel,text='＋は別音声を遅らせる／−は別音声の冒頭をカット。例：音声を5秒先に録音開始したなら「-5」。\n'
                 '収録機器の開始時刻・音声が異なる場合は自動同期できないため、必要なら試聴して微調整してください。',
                 foreground='#4c607b',wraplength=735,justify='left').grid(row=2,column=0,columnspan=2,sticky='w',pady=5)
        row=ttk.Frame(root);row.pack(fill='x',pady=(8,3))
        ttk.Label(row,text='④ 別音声を合成したMP4の保存先').pack(side='left')
        ttk.Entry(row,textvariable=self.dest).pack(side='left',fill='x',expand=True,padx=8)
        IconButton(row,text='参照…',command=self.browse_dest).pack(side='left')
        destination_buttons(root,same_command=self.same_input_destination,
            default_command=lambda:self.dest.set(str(default_directory('recordings')))).pack(anchor='w',pady=5)
        ToggleSwitch(root,text='⑤ 完了後に自動で文字起こし（メイン画面の方式・TXT/CSV出力設定を使用）',
                        variable=self.auto).pack(anchor='w',pady=(12,4))
        ttk.Label(root,text='動画と別音声の原本は変更しません。別音声付きMP4は映像をコピーし、音声だけAACへ変換します。'
            '\n文字起こしに失敗しても録画は残ります。',foreground='#4c607b',wraplength=755,justify='left').pack(anchor='w',pady=(2,8))
        buttons=ttk.Frame(shell);buttons.pack(fill='x',side='bottom',pady=(7,1))
        IconButton(buttons,text='取り込み・文字起こしを実行',command=self.submit).pack(side='right')
        IconButton(buttons,text='閉じる',command=self.win.destroy).pack(side='right',padx=8)
        bind_variable_trace(self.win, self.video, 'write',lambda *_:self._show_state())
        bind_variable_trace(self.win, self.audio, 'write',lambda *_:self._show_state())
        self.state=tk.StringVar(self.win)
        ttk.Label(buttons,textvariable=self.state,foreground='#4c607b',wraplength=350).pack(side='left',fill='x',expand=True)
        self._show_state()

    def _show_state(self):
        if not hasattr(self,'state'):return
        self.state.set('別音声なし：元MP4をそのまま認識します。' if not self.audio.get().strip()
                       else '別音声あり：映像は再圧縮せず、同期後の音声入りMP4を別名で保存します。')

    def open_game_bar(self):
        try:
            if sys.platform=='win32':os.startfile('ms-gamingoverlay:')
            else:raise OSError('Game BarはWindows専用です。')
        except OSError as exc:show_error(self.win,'Xbox Game Barを起動できません。Windows側でWin+Gを押してください。\n'+str(exc))

    def open_captures(self):
        folder=Path.home()/'Videos'/'Captures'
        if not folder.exists():
            show_error(self.win,'標準のキャプチャフォルダーが見つかりません。録画したアプリからMP4の保存先をご確認ください。')
            return
        try:
            if sys.platform=='win32':os.startfile(str(folder))
            else:subprocess.Popen(['xdg-open',str(folder)])
        except OSError as exc:show_error(self.win,str(exc))

    def same_input_destination(self):
        try:self.dest.set(input_folder(self.video.get()))
        except ValueError as exc:show_error(self.win,str(exc))

    def browse_dest(self):
        path=filedialog.askdirectory(parent=self.win,initialdir=self.dest.get())
        if path:self.dest.set(path)

    def submit(self):
        try:
            video=Path(self.video.get().strip()).expanduser()
            audio=Path(self.audio.get().strip()).expanduser() if self.audio.get().strip() else None
            offset=float(self.offset.get().strip()) if audio else 0.0
            mode='replace' if self.mode.get().startswith('別音声で') else 'mix'
            folder=Path(self.dest.get().strip()).expanduser()
            # Existing output is never touched; choose a collision-free path.
            desired=unique_destination(folder/(video.stem+'_音声同期.mp4')) if audio else None
            options=ExternalOptions(video=video,audio=audio,offset=offset,mode=mode,destination=desired)
            options.validate()
            if self.app.start_standard_import(options, auto_transcribe=self.auto.get()):
                self.win.destroy()
        except (OSError,ValueError) as exc:show_error(self.win,str(exc))

class UnifiedApp:
    def __init__(self,root:tk.Tk):
        self.root=root
        self.config=load_config()
        ensure_output_directories()
        self.hf_token,self.config,credential_warning=resolve_hf_token(self.config,save_config)
        self.diagnostics = SessionLogs(self.hf_token)
        self.events:queue.Queue[tuple]=queue.Queue()
        self.ui_callbacks:queue.Queue=queue.Queue(maxsize=128)
        self.job_sources:dict[str,Path]={}
        self.job_outputs:dict[str,list[Path]] = {}
        self.pending_speaker_edits:list[Path] = []
        self.speaker_window = None
        self.pending_capture_target = None
        self.capture_verified = False
        self.job_options:dict[str,tuple[str,str]]={}  # per-file output mode and directory
        self.job_policies:dict[str,str]={}  # ask / skip / overwrite / rename
        self.queue_policy_lock=threading.Lock()
        self.active_job_key: str | None = None
        self.collision_batch_choice: str | None = None
        self.busy=False
        self.interactive_collisions=True
        self.progress_mode = "idle"
        self.spinner_position = 0
        self.last_progress_stage = ""
        self.stop_event=threading.Event()
        self.processes=ProcessRegistry(self.stop_event)
        self.all_stop_requested=False
        self.all_stop_waiting=False
        self.active_process:subprocess.Popen | None=None
        self.process_lock=threading.Lock()
        self.cpu_limit=WindowsCpuLimit()
        self.resource_control_file=CODE_DIR / f'.gpu_pacing_{os.getpid()}.json'
        write_gpu_pacing(self.resource_control_file,self.config.gpu_soft_percent)
        self.device_cache:dict[tuple[str,str],bool]={}
        self.watch_mode=False
        self.live_active=False
        self.gamebar_active=False
        self.gamebar_live_active=False
        self.gamebar_stop=threading.Event()
        self.live_stop_file:Path|None=None
        self.live_stopping=False
        self.live_cancel=threading.Event()
        self.worker:threading.Thread | None=None
        self.closing=False
        self.quit_when_idle=False
        self.in_tray=False
        from windows_background import WindowsTray
        self.tray=WindowsTray(lambda action:self.events.put(('tray',action)), BASE/'UnifiedTranscriber.exe')
        self.detached_logbox = None
        self.dnd_startup_error = getattr(root, '_dnd_startup_error', None)
        self.root.title(f'文字起こし統合スタジオ v{APP_VERSION}')
        width = min(1180, max(790, self.root.winfo_screenwidth()-80))
        height = min(900, max(550, self.root.winfo_screenheight()-100))
        self.root.geometry(f'{width}x{height}')
        self.root.minsize(900,640)
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.bind('<Unmap>',self._on_minimize,add='+')
        install_wheel_routing(self.root)
        self._make_ui()
        self.log(f'[診断ログ] {self.diagnostics.session_path}\n'
                 f'[エラーログ] {self.diagnostics.error_path}\n')
        if credential_warning:
            self.log('[認証] '+credential_warning+'\n')
        self.update_profile_display()
        self.hotkeys=WindowsHotkeys(self.config.shortcut_keys,
            lambda action:self.events.put(('shortcut',action)),
            lambda message:self.events.put(('hotkey-error',message)))
        self.configure_shortcuts()
        self.root.after(100,self._poll)

    def _make_ui(self):
        from hints_dialog import HintsDialog
        self.ui = StudioView(self, {
            'settings': SettingsDialog, 'setup': SetupDialog, 'bulk': BulkSetupDialog,
            'cuda': CudaTestDialog, 'concat': ConcatDialog, 'convert': TranscriptConvertDialog,
            'speaker': SpeakerEditDialog, 'live': LiveCaptureDialog,
            'gamebar': GameBarRecordingDialog, 'standard': StandardRecordingDialog,
            'watch': WatchDialog, 'hints': HintsDialog,
        })

    def show_app_updates(self):
        from update_ui import UpdateController
        if not hasattr(self, 'updates'):
            self.updates = UpdateController(self)
            self.updates.start()
        self.ui.close_drawers()
        self.updates.show_settings()

    def _refresh_queue_display(self):
        view = getattr(self, 'ui', None)
        if view is not None:
            view.refresh_queue()

    def offer_initial_setup(self):
        if self.closing or self.config.onboarding_seen:
            return
        if self.in_tray:
            return
        if self.busy or any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()):
            self.root.after(1000, self.offer_initial_setup)
            return
        WelcomeDialog(self)

    def open_capture_setup(self, target=None):
        if self.busy:
            return
        self.ui.close_drawers()
        return CaptureSetupDialog(self, target)

    def open_capture(self, target, *, force_settings=False):
        if self.busy or target not in ('live', 'gamebar'):
            return
        needs_setup = (not self.config.capture_setup_complete or not self.live_capture_python().is_file()
                       or not self.media_tools_ready())
        if needs_setup:
            self.restore_from_tray()
            return self.open_capture_setup(target)
        previous=saved_options(self.config,target)
        skip=self.config.skip_live_settings if target=='live' else self.config.skip_gamebar_settings
        if force_settings or not skip or previous is None:
            self.restore_from_tray()
            return self.ui._dialog(target)
        if target=='live':
            return self.start_live(previous)
        from window_picker import WindowPickerDialog
        self.restore_from_tray()
        self.ui.close_drawers()
        def choose(title,identity):
            if self.busy or self.closing:return
            options=replace(previous,target_hwnd=identity,target_title=title)
            self.start_gamebar_recording(options)
        return WindowPickerDialog(self.root,None,choose,accept_text='選択して録画を開始')

    def save_capture_preferences(self,kind,options,skip):
        options.validate()
        preferences=dict(self.config.capture_preferences)
        preferences[kind]=serialize_options(options)
        changes={'capture_preferences':preferences,'skip_'+kind+'_settings':bool(skip)}
        if kind=='live':
            changes.update(live_recording_dir=recording_folder_preference(options.folder),live_recording_format=options.recording_format,
                live_recording_bitrate=options.recording_bitrate,live_recording_pcm_bits=options.recording_pcm_bits,
                live_recording_sample_rate=options.recording_sample_rate,live_recording_channels=options.recording_channels,
                live_channel_mode=options.channel_mode,live_post_transcribe=options.post_transcribe)
        else:
            changes.update(gamebar_shortcut=options.shortcut,gamebar_audio_source=options.source,
                gamebar_captures_dir=str(options.captures),live_recording_dir=recording_folder_preference(options.destination),
                live_recording_format=options.audio_format,live_recording_channels=options.recording_channels,
                live_channel_mode=options.channel_mode)
        cfg=replace(self.config,**changes)
        save_config(cfg);self.config=cfg

    def configure_shortcuts(self):
        self.hotkeys.update(self.config.shortcut_keys)
        for binding in getattr(self,'_shortcut_bindings',[]):
            self.root.unbind(binding)
        self._shortcut_bindings=[]
        if sys.platform!='win32':
            for action,value in self.config.shortcut_keys.items():
                if not value:continue
                pieces=value.split('+')
                names={'ctrl':'Control','shift':'Shift','alt':'Alt','win':'Mod4'}
                key=pieces[-1].upper() if pieces[-1].startswith('f') and pieces[-1][1:].isdigit() else pieces[-1]
                binding='<'+ '-'.join([names[p] for p in pieces[:-1]]+[key])+'>'
                self.root.bind(binding,lambda _e,a=action:self._handle_shortcut(a))
                self._shortcut_bindings.append(binding)

    def export_preferences(self):
        from settings_ui import SettingsExportDialog
        self.ui.close_drawers()
        return SettingsExportDialog(self)

    def import_preferences(self,source=None):
        from settings_ui import load_settings_file
        return load_settings_file(self,source)

    def _handle_shortcut(self,action):
        if action=='stop':self.stop()
        elif self.busy:
            self.restore_from_tray()
            self.status.set('別の処理を実行中です。終了後に録音を開始してください。')
        elif self.root.grab_current() is None:self.open_capture(action)
        return 'break'

    def edit_shortcuts(self):
        from hotkey_ui import ShortcutSettingsDialog
        self.ui.close_drawers()
        return ShortcutSettingsDialog(self)

    def edit_background(self):
        from background_ui import BackgroundSettingsDialog
        self.ui.close_drawers()
        return BackgroundSettingsDialog(self)

    def ensure_tray(self):
        return self.tray.start()

    def hide_to_tray(self):
        if not self.ensure_tray():
            self.restore_from_tray()
            messagebox.showerror('通知領域を使用できません',
                (self.tray.error or 'アイコンを登録できませんでした。') + '\n画面を表示したままにします。', parent=self.root)
            return False
        self.in_tray=True
        self.root.withdraw()
        return True

    def restore_from_tray(self):
        was_hidden = self.in_tray or self.root.state() in ('iconic','withdrawn')
        self.in_tray=False
        if was_hidden:self.root.deiconify()
        self.root.lift()
        try:self.root.focus_force()
        except tk.TclError:pass
        if was_hidden and not self.config.onboarding_seen:
            self.root.after(250,self.offer_initial_setup)

    def _on_minimize(self,event):
        if event.widget is self.root and self.config.tray_minimize and not self.in_tray:
            self.root.after_idle(self._minimize_to_tray)

    def _minimize_to_tray(self):
        if not self.closing and not self.in_tray and self.root.state() == 'iconic' and self.root.grab_current() is None:
            self.hide_to_tray()

    def _tray_action(self, action):
        if action == 'show':
            self.restore_from_tray()
        elif action == 'exit':
            self.restore_from_tray()
            self.request_exit()
        elif action in ('live','gamebar','stop'):
            self._handle_shortcut(action)

    def _selected_key(self):
        selection = self.tree.selection()
        if not selection:
            show_error(self.root, '先にファイル一覧から対象を選択してください。')
            return None
        return selection[0]

    def show_selected_result(self):
        key = self._selected_key()
        if key:
            self.ui.close_drawers()
            return ResultDialog(self, key)

    def open_selected_source(self):
        key = self._selected_key()
        if key:
            self.open_job_location(key, original=True)

    def open_selected_output(self):
        key = self._selected_key()
        if key:
            self.open_job_location(key)

    def open_job_location(self, key, *, original=False):
        try:
            if original:
                target = self.job_sources[key]
            else:
                paths = self.job_outputs.get(key, ())
                if not paths:
                    raise ValueError('保存済みの文字起こし結果がありません。')
                target = paths[0]
            open_folder(target)
        except (OSError, ValueError, KeyError) as exc:
            show_error(self.root, str(exc))

    def edit_selected_speakers(self):
        key = self._selected_key()
        if key:
            self.edit_job_speakers(key)

    def edit_job_speakers(self, key):
        if self.busy:
            show_error(self.root, '処理の終了後に話者名を設定してください。')
            return
        paths = self.job_outputs.get(key, ())
        target = next((p for p in paths if p.suffix.lower() == '.txt' and p.is_file()), None)
        target = target or next((p for p in paths if p.is_file()), None)
        if target is None:
            show_error(self.root, '出力ファイルが見つかりません。移動・削除されていないか確認してください。')
            return
        self.open_speaker_editor(target)

    def open_speaker_editor(self, source):
        if self.busy or self.closing:
            return
        if self.speaker_window is not None and self.speaker_window.winfo_exists():
            self.speaker_window.lift()
            return
        editor = SpeakerEditDialog(self, source=Path(source))
        self.speaker_window = editor.win
        def closed(event):
            if event.widget is editor.win:
                self.speaker_window = None
                if not self.closing:
                    self.root.after_idle(self._open_next_speaker)
        editor.win.bind('<Destroy>', closed, add='+')
        return editor

    def _open_next_speaker(self):
        if self.busy or self.closing or not self.config.auto_open_speaker:
            return
        if self.speaker_window is not None and self.speaker_window.winfo_exists():
            return
        while self.pending_speaker_edits:
            path = self.pending_speaker_edits.pop(0)
            if path.is_file():
                self.open_speaker_editor(path)
                return

    def update_profile_display(self):
        p=BY_ID[self.config.profile]
        py=env_python(p.id,self.config)
        env_state='セットアップ済み' if py.is_file() else '未セットアップ'
        self.ui.refresh_profile(p, py)
        self.status.set(f'選択方式: {p.name}（{env_state}）')

    def _on_profile(self,_event=None):
        if self.busy:
            self.profile_var.set(PROFILE_NAMES[self.config.profile])
            return
        self.config.profile=PROFILE_LABELS[self.profile_var.get()]
        save_config(self.config)
        self.update_profile_display()

    def open_diagnostic_folder(self):
        folder=self.diagnostics.folder
        try:
            if sys.platform == 'win32':
                os.startfile(str(folder))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(folder)])
            else:
                subprocess.Popen(['xdg-open', str(folder)])
        except OSError as exc:
            show_error(self.root, f'診断ログフォルダーを開けません: {exc}\n{folder}')

    def export_diagnostics(self):
        destination=filedialog.asksaveasfilename(
            parent=self.root,title='診断ログをZIPで保存',
            initialdir=str(default_directory('exports')),
            defaultextension='.zip',filetypes=[('ZIPファイル','*.zip')],
            initialfile=datetime.now().strftime('Transcriber_Diagnostics_%Y%m%d_%H%M%S.zip'))
        if not destination:return
        paths = (self.diagnostics.session_path, self.diagnostics.error_path)
        try:
            with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for path in paths:
                    if path.exists():archive.write(path,arcname=path.name)
                archive.writestr('READ_ME.txt',
                    f'文字起こし統合スタジオ v{APP_VERSION} 診断ログ\n'
                    '注意: ログにはPCユーザー名、音声ファイル名、保存先パス、デバイス名が含まれる場合があります。\n'
                    'Hugging Faceトークン自体は意図的にマスクされます。送信前に内容をご確認ください。\n')
            self.log('診断ログを保存しました: '+str(destination)+'\n')
        except OSError as exc:
            show_error(self.root,'診断ログの保存に失敗: '+str(exc))

    def log(self,message:str):
        self.diagnostics.write(message)
        # Always keep the embedded log and any detached viewer in sync.
        for widget in (self.logbox, self.detached_logbox):
            if widget is None:
                continue
            try:
                if not widget.winfo_exists():
                    continue
                widget.configure(state='normal')
                widget.insert('end',message)
                widget.see('end')
                widget.configure(state='disabled')
            except tk.TclError:
                if widget is self.detached_logbox:
                    self.detached_logbox=None

    def open_detached_log(self):
        """Display full processing history live without losing the main window."""
        try:
            if self.detached_logbox is not None and self.detached_logbox.winfo_exists():
                self.detached_logbox.winfo_toplevel().lift()
                return
        except tk.TclError:
            self.detached_logbox = None
        win=tk.Toplevel(self.root)
        win.title('文字起こし統合スタジオ · 詳細ログ')
        win.geometry('1000x580')
        area=ttk.Frame(win,padding=10)
        area.pack(fill='both',expand=True)
        area.columnconfigure(0,weight=1)
        area.rowconfigure(1,weight=1)
        ttk.Label(area,text='全実行ログ（画面の表示中もリアルタイム更新）').grid(row=0,column=0,sticky='w',pady=(0,6))
        widget=tk.Text(area,wrap='word',font=(UI_FONT,10),state='normal')
        widget.grid(row=1,column=0,sticky='nsew')
        bar=ttk.Scrollbar(area,orient='vertical',command=widget.yview)
        bar.grid(row=1,column=1,sticky='ns')
        widget.configure(yscrollcommand=bar.set)
        widget.insert('1.0',self.logbox.get('1.0','end-1c'))
        widget.see('end')
        widget.configure(state='disabled')
        self.detached_logbox=widget
        def on_close():
            self.detached_logbox=None
            win.destroy()
        win.protocol('WM_DELETE_WINDOW',on_close)

    def _enable_drag_drop(self, treeframe: ttk.Frame):
        """Enable Explorer drops; expose actionable status if the backend is missing."""
        if self.dnd_startup_error:
            self.drop_hint.configure(text='D&D無効: TkDND初期化失敗。右のボタンで診断してください。')
            self.diagnostics.error(f'D&D TkDND init failure: {self.dnd_startup_error}')
            return
        if TkinterDnD is None or DND_FILES is None:
            self.drop_hint.configure(text='D&D無効: tkinterdnd2を読み込めません。右のボタンで修復してください。')
            if DND_IMPORT_ERROR:
                self.diagnostics.error(f'D&D import failure: {DND_IMPORT_ERROR}')
            return
        try:
            targets = [treeframe, self.tree, self.drop_hint]
            empty_hint = getattr(self, 'empty_drop_hint', None)
            if isinstance(empty_hint, tk.Widget):
                targets.append(empty_hint)
            for widget in targets:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._on_file_drop)
                widget.dnd_bind('<<DropEnter>>', lambda event: 'copy')
            self.drop_hint.configure(text='D&D有効: 音声・動画ファイルをこのエリアにドロップできます。')
            self.dnd_repair_btn.pack_forget()
            # Elevated GUI cannot receive drops from the non-elevated Explorer
            # on Windows (UIPI), even if tkdnd itself is correctly installed.
            if sys.platform=='win32':
                try:
                    import ctypes
                    if ctypes.windll.shell32.IsUserAnAdmin():
                        self.drop_hint.configure(text='D&D読み込み済み。ただし管理者起動中はExplorerからドロップ不可。通常権限で起動してください。')
                except (OSError, AttributeError):
                    pass
        except (tk.TclError, AttributeError, RuntimeError) as exc:
            self.drop_hint.configure(text='D&D無効: ドロップ先の登録に失敗しました。右のボタンで診断してください。')
            self.diagnostics.error(f'D&D widget registration failure: {exc}')

    def repair_dnd(self):
        """Repair D&D in a background thread, never opening cmd.exe."""
        if not messagebox.askokcancel('D&D診断・修復',
                'ドラッグ＆ドロップの依存ライブラリを確認・修復します。\n'
                '修復後はGUIの再起動が必要です。音声認識用の環境は変更しません。',
                parent=self.root):
            return
        self.dnd_repair_btn.configure(state='disabled')
        self.drop_hint.configure(text='D&Dライブラリを修復中…')
        def worker():
            import io
            from dnd_support import ensure_drag_drop
            log = io.StringIO()
            try:
                success = ensure_drag_drop(log=log, force=True)
            except Exception as exc:
                success = False
                log.write(f'{type(exc).__name__}: {exc}\n')
                self.events.put(('errorlog','D&D修復失敗:\n'+traceback.format_exc()))
            output = log.getvalue()
            def finish():
                try:
                    self.dnd_repair_btn.configure(state='normal')
                    if success:
                        self.drop_hint.configure(text='D&D修復成功。GUIを閉じ、start_gui.vbsから起動し直してください。')
                        messagebox.showinfo('D&D修復成功',
                            'D&Dライブラリの導入が完了しました。\nGUIの再起動で有効になります。',parent=self.root)
                    else:
                        self.drop_hint.configure(text='D&D修復に失敗。詳細をダイアログで確認してください。')
                        messagebox.showerror('D&D修復失敗', output[-3500:],parent=self.root)
                except tk.TclError:
                    pass
            queue_ui_callback(self,self.root,finish)
        threading.Thread(target=worker, daemon=True).start()

    def _on_file_drop(self, event):
        if self.busy:
            self.status.set('処理中はファイルを追加できません。')
            return 'break'
        try:
            dropped = parse_drop_paths(event.data, self.root.tk.splitlist)
            files = []
            folders = 0
            for item in dropped:
                if item.is_dir():
                    folders += 1
                    files.extend(sorted(item.iterdir()))  # identical to browse folder, not recursive
                else:
                    files.append(item)
            self._add_paths(files)
            if not any(supported_file(p) for p in files):
                self.status.set('対応する音声・動画がありません。')
            elif folders:
                self.status.set(f'D&D: フォルダー{folders}件を含むファイルを受け付けました。待機中: {len(self.job_sources)}件')
        except (OSError, ValueError, tk.TclError) as exc:
            self.status.set(f'D&Dでの追加に失敗: {exc}')
        return 'copy'

    def add_files(self):
        if self.busy:return
        paths=filedialog.askopenfilenames(parent=self.root,title='音声・動画を選択',filetypes=MEDIA_TYPES)
        self._add_paths([Path(p) for p in paths])

    def add_folder(self):
        if self.busy:return
        path=filedialog.askdirectory(parent=self.root,title='音声・動画の入ったフォルダー')
        if path:self._add_paths(sorted(Path(path).iterdir()))

    def _add_paths(self,paths, *, allow_missing=False):
        new=0
        for raw in paths:
            try:
                p=Path(raw).resolve()
                if (p.suffix.lower() not in SUPPORTED_EXTENSIONS if allow_missing else not supported_file(p)) or str(p) in self.job_sources:continue
                try:size=f'{p.stat().st_size / (1024*1024):.1f} MB'
                except FileNotFoundError:
                    if not allow_missing:continue
                    # A recording may have been converted before this event is
                    # consumed on the UI thread; the following move event will
                    # update this row to the final, committed media path.
                    size='—'
            except OSError:
                continue
            self.job_sources[str(p)]=p
            outdir=(str(p.parent) if self.config.output_same_as_input else
                    self.config.output_dir.strip() or str(default_directory('transcriptions')))
            self.job_options[str(p)]=(self.config.output_mode,outdir)
            with self.queue_policy_lock:self.job_policies[str(p)]='ask'
            self.tree.insert('', 'end', iid=str(p), values=(p.name,size,'待機中','実行時に確認',str(p.parent),
                self.config.output_mode.upper(),outdir))
            new+=1
        if new:self.status.set(f'{new}件追加しました。待機中: {len(self.job_sources)}件')
        self._refresh_queue_display()

    def _recorded_queue_item(self, source: Path, status: str) -> None:
        """Register a finalized capture on the Tk thread, without duplicating rows."""
        source=Path(source).resolve()
        key=str(source)
        existed=key in self.job_sources
        self._add_paths([source],allow_missing=True)
        if self.tree.exists(key) and (not existed or status in ('処理中','失敗','中止')):
            row=list(self.tree.item(key,'values'))
            row[2]=status
            self.tree.item(key,values=row)

    def _move_recorded_queue_item(self, source: Path, replacement: Path) -> None:
        """Retarget a recording row after a successful, committed conversion."""
        old=str(Path(source).resolve()); new_path=Path(replacement).resolve(); new=str(new_path)
        if old==new or old not in self.job_sources or not self.tree.exists(old):return
        if new in self.job_sources:
            # A separately registered destination keeps its existing output
            # preferences, while any results already attached to the WAV are
            # retained on the surviving row. The removed WAV must not remain
            # as a stale queue entry.
            old_results=self.job_outputs.pop(old,[])
            existing=self.job_outputs.get(new,[])
            self.job_outputs[new]=list(dict.fromkeys([*existing,*old_results]))
            if old_results and not existing and self.tree.exists(new):
                row=list(self.tree.item(new,'values'))
                row[2]=self.tree.item(old,'values')[2]
                self.tree.item(new,values=row)
            selected=old in self.tree.selection()
            self.tree.delete(old);self.job_sources.pop(old,None);self.job_options.pop(old,None)
            with self.queue_policy_lock:self.job_policies.pop(old,None)
            if selected and self.tree.exists(new):self.tree.selection_set(new)
            if self.active_job_key==old:self.active_job_key=new
            self.log(f'[キュー] 変換済み録音の結果を既存行に統合しました: {new}\n')
            self._refresh_queue_display()
            return
        row=list(self.tree.item(old,'values'))
        row[0]=new_path.name
        try:row[1]=f'{new_path.stat().st_size / (1024*1024):.1f} MB'
        except OSError:row[1]='—'
        row[4]=str(new_path.parent)
        if self.config.output_same_as_input:
            row[6]=str(new_path.parent)
        position=self.tree.index(old)
        selected=old in self.tree.selection()
        self.tree.delete(old)
        self.tree.insert('',position,iid=new,values=row)
        if selected:self.tree.selection_set(new)
        self.job_sources.pop(old,None);self.job_sources[new]=new_path
        options=self.job_options.pop(old,None)
        if options is not None:
            self.job_options[new]=(options[0],str(new_path.parent) if self.config.output_same_as_input else options[1])
        if old in self.job_outputs:self.job_outputs[new]=self.job_outputs.pop(old)
        with self.queue_policy_lock:
            if old in self.job_policies:self.job_policies[new]=self.job_policies.pop(old)
        if self.active_job_key==old:self.active_job_key=new
        self._refresh_queue_display()

    def remove_selected(self):
        if self.busy:return
        for item in self.tree.selection():
            self.job_sources.pop(item,None)
            self.job_outputs.pop(item,None)
            self.job_options.pop(item,None)
            with self.queue_policy_lock:self.job_policies.pop(item,None)
            self.tree.delete(item)
        self._refresh_queue_display()

    def clear_files(self):
        if self.busy:return
        for item in self.tree.get_children():self.tree.delete(item)
        self.job_sources.clear()
        self.job_outputs.clear()
        self.job_options.clear()
        with self.queue_policy_lock:self.job_policies.clear()
        self._refresh_queue_display()

    def edit_runtime_limits(self):
        """Limits can be changed during processing. GPU option is advisory."""
        win=modal(self.root,'CPU / GPU の負荷制限',720)
        frame=ttk.Frame(win,padding=18);frame.pack(fill='both',expand=True)
        labels=('未指定','10%','20%','25%','30%','40%','50%','60%','70%','75%','80%','90%','100%')
        def fmt(v):return '未指定' if v is None else f'{v}%'
        cpu=tk.StringVar(win,value=fmt(self.config.cpu_limit_percent))
        gpu=tk.StringVar(win,value=fmt(self.config.gpu_soft_percent))
        ttk.Label(frame,text='CPU上限（Windows Job ObjectによるプロセスCPU時間の制限）').pack(anchor='w')
        ttk.Combobox(frame,textvariable=cpu,values=labels,state='readonly').pack(fill='x',pady=(4,12))
        ttk.Label(frame,text='GPU ソフト負荷抑制（推論区間の間に待機）').pack(anchor='w')
        ttk.Combobox(frame,textvariable=gpu,values=labels,state='readonly').pack(fill='x',pady=(4,12))
        ttk.Label(frame,text='既定値は両方とも「未指定」。処理中でも変更できます。\n'
                  'CPU制限はWindowsが適用可能な場合のみ有効です。CPU全体に対する時間枠の上限です。\n'
                  'GPUは実際の使用率やVRAMを指定値以下に保証しません。\n'
                  '発話区間の境界で待機するだけなので、長い推論カーネルやモデル読み込み中には即時反映されません。\n'
                  'ライブ録音プロセスにはCPU制限を適用しません（録音欠落防止）。',
                  foreground='#596477',wraplength=655,justify='left').pack(anchor='w',pady=(3,16))
        def parse(s):return None if s=='未指定' else int(s.rstrip('%'))
        def apply():
            try:
                c,g=parse(cpu.get()),parse(gpu.get())
                new=replace(self.config,cpu_limit_percent=c,gpu_soft_percent=g)
                new.validate()
                write_gpu_pacing(self.resource_control_file,g)
                self.config=new
                save_config(new)
                # If an ASR job is running, update the Windows kernel limit directly.
                if self.busy and self.cpu_limit.job:
                    try:
                        state=self.cpu_limit.update(c)
                        self.log('[負荷制限] '+state+'\n')
                    except OSError as exc:
                        self.log('[負荷制限] CPUの更新不可: '+str(exc)+'\n')
                        messagebox.showwarning('CPU制限を適用できません',str(exc),parent=win)
                self.log(f'[負荷制限] CPU={fmt(c)} / GPUソフト待機={fmt(g)}\n')
                win.destroy()
            except (ValueError,OSError) as exc:show_error(win,str(exc))
        row=ttk.Frame(frame);row.pack(fill='x')
        IconButton(row,text='適用して保存',style='Primary.TButton',command=apply).pack(side='right')
        IconButton(row,text='閉じる',command=win.destroy).pack(side='right',padx=(0,9))

    def _on_tree_double_click(self,event):
        if self.tree.identify_region(event.x,event.y)=='cell':
            target=self.tree.identify_row(event.y)
            if target:
                self.tree.selection_set(target)
                column = self.tree.column(self.tree.identify_column(event.x), 'id')
                if column == 'policy':
                    self.edit_queue_policies()
                elif self.job_outputs.get(target):
                    if column == 'out':
                        self.open_job_location(target, original=True)
                    elif column == 'destination':
                        self.open_job_location(target)
                    else:
                        ResultDialog(self, target)
                elif self.busy:
                    self.edit_queue_policies()
                else:self.edit_file_outputs()

    def edit_queue_policies(self):
        """Change queued collision behaviour, including upcoming jobs during a batch."""
        selection=list(self.tree.selection())
        if not selection:
            show_error(self.root,'変更するキューを選択してください。');return
        with self.queue_policy_lock:
            if self.active_job_key in selection:
                show_error(self.root,'現在実行中の項目は変更できません。次のキューを選択してください。');return
            initial=self.job_policies.get(selection[0],'ask')
        dialog=modal(self.root,f'キューの既存出力の扱い（{len(selection)}件）',660)
        frame=ttk.Frame(dialog,padding=18);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='対象: '+', '.join(self.job_sources[k].name for k in selection[:3]),wraplength=605).pack(anchor='w',pady=(0,12))
        chosen=tk.StringVar(dialog,value=initial)
        descriptions=(('ask','実行時に確認：同名のTXT/CSVがあるときだけ選択する'),
                      ('skip','スキップ：このキューは実行しない'),
                      ('overwrite','上書き：指定名と同じ既存出力を置き換える'),
                      ('rename','リネーム保存：同名があれば別名で新規保存する'))
        for code,label in descriptions:
            ttk.Radiobutton(frame,text=label,variable=chosen,value=code).pack(anchor='w',pady=8)
        ttk.Label(frame,text='保存名が現在の詳細設定と異なる旧TXT/CSVは、重複とは判定せず新規文字起こしします。\n'
                  '実行中でも、まだ開始していないキューの設定は変更できます。',wraplength=605,
                  foreground='#596477').pack(anchor='w',pady=(12,16))
        def commit():
            with self.queue_policy_lock:
                for key in selection:
                    if key==self.active_job_key:continue
                    self.job_policies[key]=chosen.get()
                    if self.tree.exists(key):
                        row=list(self.tree.item(key,'values'))
                        row[3]={'ask':'実行時に確認','skip':'スキップ','overwrite':'上書き',
                                'rename':'リネーム保存'}[chosen.get()]
                        self.tree.item(key,values=row)
            dialog.destroy()
        IconButton(frame,text='キューへ適用',style='Primary.TButton',command=commit).pack(side='right')
        IconButton(frame,text='キャンセル',command=dialog.destroy).pack(side='right',padx=6)

    def edit_file_outputs(self):
        """Edit one or several queued jobs, never alter settings defaults."""
        if self.busy:return
        selection=list(self.tree.selection())
        if not selection:
            show_error(self.root,'先にファイル一覧から変更したい行を選択してください。');return
        first=self.job_options[selection[0]]
        win=modal(self.root, f'ファイル別の出力設定（{len(selection)}件）',710)
        mode=tk.StringVar(win,value=first[0])
        destination=tk.StringVar(win,value=first[1])
        frame=ttk.Frame(win,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='対象: '+(', '.join(self.job_sources[x].name for x in selection[:3]))+
                  (' …' if len(selection)>3 else ''),wraplength=660).pack(anchor='w',pady=(0,14))
        ttk.Label(frame,text='出力形式').pack(anchor='w')
        ttk.Combobox(frame,textvariable=mode,values=('txt','csv','both'),state='readonly').pack(fill='x',pady=(4,12))
        ttk.Label(frame,text='保存先フォルダー（各ファイルに適用）').pack(anchor='w')
        pathrow=ttk.Frame(frame);pathrow.pack(fill='x',pady=(4,8))
        ttk.Entry(pathrow,textvariable=destination).pack(side='left',fill='x',expand=True)
        def browse():
            value=filedialog.askdirectory(parent=win,initialdir=(str(self.job_sources[selection[0]].parent) if destination.get()==INPUT_LOCATION
                else str(default_directory()) if destination.get()==DEFAULT_LOCATION else destination.get()) or str(default_directory()))
            if value:destination.set(value)
        IconButton(pathrow,text='参照…',command=browse).pack(side='left',padx=(7,0))
        destination_buttons(frame,same_command=lambda:destination.set(INPUT_LOCATION),
            default_command=lambda:destination.set(DEFAULT_LOCATION)).pack(anchor='w',pady=(0,6))
        ttk.Label(frame,text='新しく追加するファイルは詳細設定のデフォルト値になります。\n'
                  '既存ファイルへの動作は「スキップ・上書き・別名保存…」で指定できます。',
                  foreground='#536078',wraplength=655).pack(anchor='w',pady=(4,16))
        buttons=ttk.Frame(frame);buttons.pack(fill='x')
        def save():
            try:
                same = destination.get() == INPUT_LOCATION
                dst=default_directory() if destination.get()==DEFAULT_LOCATION else Path(destination.get().strip()).expanduser()
                if not destination.get().strip():raise ValueError('保存先フォルダーを入力してください。')
                if not same and dst.is_file():raise ValueError('保存先はファイルではなくフォルダーにしてください。')
                if mode.get() not in ('txt','csv','both'):raise ValueError('出力形式が不正です。')
                for key in selection:
                    folder = self.job_sources[key].parent if same else dst
                    self.job_options[key]=(mode.get(),str(folder))
                    row=list(self.tree.item(key,'values'))
                    row[5]=mode.get().upper();row[6]=str(folder)
                    self.tree.item(key,values=row)
                win.destroy()
            except (OSError,ValueError) as exc:show_error(win,str(exc))
        IconButton(buttons,text='この設定を適用',style='Primary.TButton',command=save).pack(side='right')
        IconButton(buttons,text='キャンセル',command=win.destroy).pack(side='right',padx=(0,8))

    def open_output(self):
        if self.config.output_same_as_input and self.tree.selection():
            self.open_selected_output() if self.job_outputs.get(self.tree.selection()[0]) else self.open_selected_source()
            return
        folder=Path(self.config.output_dir).expanduser() if self.config.output_dir.strip() else default_directory('transcriptions')
        try:
            folder.mkdir(parents=True,exist_ok=True)
            if sys.platform=='win32':os.startfile(str(folder))
            elif sys.platform=='darwin':subprocess.Popen(['open',str(folder)])
            else:subprocess.Popen(['xdg-open',str(folder)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except OSError as exc:show_error(self.root,str(exc))

    def _set_busy(self,busy:bool,what=''):
        self.busy=busy
        self.start_btn.configure(state='disabled' if busy else 'normal')
        self.edit_output_btn.configure(state='disabled' if busy else 'normal')
        self.edit_policy_btn.configure(state='normal')
        self.stop_btn.configure(state='normal' if busy else 'disabled',text='■ 停止' if not busy else self.stop_btn.cget('text'))
        self.settings_btn.configure(state='disabled' if busy else 'normal')
        self.setup_btn.configure(state='disabled' if busy else 'normal')
        self.bulk_btn.configure(state='disabled' if busy else 'normal')
        self.cuda_btn.configure(state='disabled' if busy else 'normal')
        self.text_merge_btn.configure(state='disabled' if busy else 'normal')
        self.audio_merge_btn.configure(state='disabled' if busy else 'normal')
        self.csv_merge_btn.configure(state='disabled' if busy else 'normal')
        self.convert_btn.configure(state='disabled' if busy else 'normal')
        self.speaker_edit_btn.configure(state='disabled' if busy else 'normal')
        self.screen_btn.configure(state='disabled' if busy else 'normal')
        self.standard_btn.configure(state='disabled' if busy else 'normal')
        self.live_btn.configure(state='disabled' if busy else 'normal')
        if busy:
            self._show_progress(EngineUpdate('準備中'))
        else:
            self.progress_mode='idle'
            self.progress_text.pack_forget()
            self.spinner.pack_forget()
            self.progress.pack_forget()
            self.progress_label.set('')
        if what:self.status.set(what)
        view = getattr(self, 'ui', None)
        if view is not None:
            view.sync_busy(busy)

    def _animate_busy(self):
        """An animated circular glyph only while progress cannot be measured."""
        if self.closing:return
        if self.busy and self.progress_mode == 'spinner':
            self.spinner_position=(self.spinner_position+1)%4
            self.spinner.configure(text=('◴','◷','◶','◵')[self.spinner_position])
        self._animation_job=self.root.after(130,self._animate_busy)

    def _show_progress(self,update:EngineUpdate):
        if not self.busy:return
        self.progress_label.set(update.stage + ((' · '+update.detail) if update.detail else ''))
        self.progress_text.pack(side='left',padx=(0,7))
        if update.fraction is not None:
            self.progress_mode='determinate'
            self.spinner.pack_forget()
            self.progress.configure(value=round(100*update.fraction,1))
            self.progress.pack(side='left')
            self.progress_label.set(f'{update.stage} {update.fraction:.0%}' +
                                    ((' · '+update.detail) if update.detail else ''))
        else:
            self.progress_mode='spinner'
            self.progress.pack_forget()
            self.spinner.pack(side='left')
        if update.snippet:
            self.recognized_box.configure(state='normal')
            self.recognized_box.insert('end',update.snippet.strip()+'\n')
            # Keep this session's entire preview, including earlier live chunks,
            # so the Copy action does not silently omit the beginning.
            self.recognized_box.see('end')
            self.recognized_box.configure(state='disabled')

    def copy_recognition_preview(self):
        text=self.recognized_box.get('1.0','end-1c')
        if not text.strip():
            self.status.set('コピーできる認識テキストはまだありません。');return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set('認識プレビューをコピーしました。')

    def _reset_recognition_preview(self):
        self.recognized_box.configure(state='normal')
        self.recognized_box.delete('1.0','end')
        self.recognized_box.configure(state='disabled')
        if self.busy:self._show_progress(EngineUpdate('準備中'))

    def media_tools_ready(self) -> bool:
        if os.name != 'nt':
            return True
        from secure_media import verified_media_binary
        return verified_media_binary(BASE, 'ffmpeg') is not None

    def _require_media_tools(self) -> bool:
        if self.media_tools_ready():
            return True
        if getattr(self,'in_tray',False):self.restore_from_tray()
        show_error(self.root,'FFmpeg / FFprobeの更新が必要です。\n\n'
                   '「録音環境をセットアップ」または「一括セットアップ」を実行してください。'
                   '既存の設定・モデルは保持します。')
        return False

    def _require_environment(self, cfg:Config) -> Path|None:
        python=env_python(cfg.profile,cfg)
        if not python.is_file():
            if getattr(self,'in_tray',False):self.restore_from_tray()
            show_error(self.root,'この方式のPython実行環境がありません。\n\n「環境セットアップ…」からインストールするか、既存の環境を割り当ててください。\n'+str(python))
            return None
        if not self._require_media_tools():
            return None
        if BY_ID[cfg.profile].diarizer == 'pyannote' and not (self.hf_token or os.environ.get('HF_TOKEN','')):
            if getattr(self,'in_tray',False):self.restore_from_tray()
            show_error(self.root,'pyannote方式にはHugging Faceトークンが必要です。\n\n詳細設定 → 保存先・認証 に入力するか、環境変数 HF_TOKEN を設定してください。')
            return None
        return python

    def start_batch(self):
        if self.busy:return
        if not self.job_sources:
            show_error(self.root,'ファイルを追加してください。');return
        cfg=replace(self.config,python_overrides=dict(self.config.python_overrides))
        try:cfg.validate()
        except ValueError as exc:show_error(self.root,str(exc));return
        python=self._require_environment(cfg)
        if python is None:return
        files=list(self.job_sources.values())
        job_options=dict(self.job_options)
        self._begin_work()
        self.collision_batch_choice=None
        self.watch_mode=False
        self._set_busy(True,'一括文字起こし中…')
        self.worker=threading.Thread(target=self._batch_worker,args=(files,cfg,python,self.hf_token,job_options),daemon=True)
        self.worker.start()

    def _request_overwrite(self,source:Path,targets:list[Path]) -> str:
        """Only the Tk event thread opens a dialog; background workers wait safely."""
        if getattr(self,'collision_batch_choice',None):
            return self.collision_batch_choice
        reply=threading.Event()
        result={'choice':'skip'}
        self.events.put(('collision',source,targets,reply,result))
        while not reply.wait(0.1):
            if self.stop_event.is_set() or self.closing:
                return 'cancel'
        return result['choice']

    def start_transcript_conversion(self, jobs:list[tuple[Path,Path]]):
        if self.busy:
            show_error(self.root,'別の処理を実行中です。');return
        self._begin_work()
        self._set_busy(True,'TXT ⇔ CSV変換中…')
        self.worker=threading.Thread(target=self._conversion_worker,args=(jobs,),daemon=True)
        self.worker.start()

    def _conversion_worker(self,jobs:list[tuple[Path,Path]]):
        completed=skipped=failed=0
        try:
            for idx,(source,dest) in enumerate(jobs,1):
                if self.stop_event.is_set():break
                self.events.put(('status',f'変換中 {idx}/{len(jobs)}: {source.name}'))
                overwrite=False
                if dest.exists():
                    choice=self._request_overwrite(source,[dest])
                    if choice=='cancel':break
                    if choice=='skip':
                        skipped+=1
                        self.events.put(('log',f'[スキップ] {source.name} → {dest}\n'))
                        continue
                    overwrite=True
                try:
                    convert_transcript(source,dest,overwrite=overwrite,cancel=self.stop_event)
                    completed+=1
                    self.events.put(('log',f'[変換完了] {source.name} → {dest}\n'))
                except InterruptedError:
                    break
                except (OSError,ValueError,UnicodeError,csv.Error) as exc:
                    failed+=1
                    self.events.put(('log',f'[変換失敗] {source.name}: {exc}\n'))
                    self.events.put(('errorlog',f'TXT/CSV変換失敗: {source} → {dest}: {exc}'))
        finally:
            self.events.put(('done',f'TXT/CSV変換終了: 完了{completed}件・スキップ{skipped}件・失敗{failed}件'))

    def _resolve_device(self, cfg:Config, python:Path) -> str:
        if cfg.device != 'auto':
            return cfg.device
        key = (cfg.profile, str(python))
        if key not in self.device_cache:
            try:
                result = run_cancellable([
                    str(python), '-X', 'utf8', '-u', str(CODE_DIR / 'cuda_diagnostic.py'),
                    '--backend', BY_ID[cfg.profile].asr
                ], cancel=getattr(self,'stop_event',None), registry=getattr(self,'processes',None), capture_output=True, timeout=40, text=True,
                   encoding='utf-8', errors='replace', env=utf8_child_env(),
                   creationflags=hidden_child_flags())
                self.device_cache[key] = result.returncode == 0
                if not self.device_cache[key]:
                    self.events.put(('log', 'auto設定: CUDA診断が成功しなかったためCPUへ切替。'
                                             '「CUDA診断…」で原因を確認できます。\n'))
            except InterruptedError:
                raise
            except (subprocess.SubprocessError, OSError):
                self.device_cache[key] = False
        return 'cuda' if self.device_cache[key] else 'cpu'

    def start_text_merge(self, sources: list[Path], destination: Path, headings: bool, *, move_originals=False):
        if self.busy:
            show_error(self.root,'別の処理を実行中です。')
            return
        self._begin_work()
        self._set_busy(True,'TXTを結合中…')
        self.worker=threading.Thread(target=self._text_merge_worker,
            args=(sources,destination,headings,move_originals),daemon=True)
        self.worker.start()

    def _archive_merged_sources(self,sources,output,stamps):
        if self.stop_event.is_set():return ''
        result=archive_merge_sources(sources,output,stamps=stamps,cancel=self.stop_event,
            log=lambda text:self.events.put(('log',text+'\n')))
        return ' '+result.message()

    def _text_merge_worker(self,sources,dest,headings,move_originals=False):
        try:
            check_cancel(self.stop_event)
            stamps=source_stamps(sources) if move_originals else None
            result=merge_text_files(sources,dest,headings=headings,cancel=self.stop_event)
            note=self._archive_merged_sources(sources,result,stamps) if move_originals else ''
            self.events.put(('log',f'TXT結合完了: {result}\n'+note+'\n'))
            self.events.put(('done','TXT結合が完了しました。'+note))
        except InterruptedError:
            self.events.put(('done','TXT結合を中止しました。'))
        except Exception as exc:
            self.events.put(('errorlog','TXT結合エラー:\n'+traceback.format_exc()))
            self.events.put(('log',f'TXT結合エラー: {exc}\n'))
            self.events.put(('done','TXT結合に失敗しました。'))

    def start_csv_merge(self, sources: list[Path], destination: Path, *, move_originals=False):
        if self.busy:
            show_error(self.root,'別の処理を実行中です。')
            return
        self._begin_work()
        self._set_busy(True,'CSVを結合中…')
        self.worker=threading.Thread(target=self._csv_merge_worker,
            args=(sources,destination,move_originals),daemon=True)
        self.worker.start()

    def _csv_merge_worker(self,sources,dest,move_originals=False):
        try:
            check_cancel(self.stop_event)
            stamps=source_stamps(sources) if move_originals else None
            result=merge_transcript_csv(sources,dest,cancel=self.stop_event)
            note=self._archive_merged_sources(sources,result,stamps) if move_originals else ''
            self.events.put(('log',f'CSV結合完了: {result}\n'+note+'\n'))
            self.events.put(('done','CSV結合が完了しました。'+note))
        except InterruptedError:
            self.events.put(('done','CSV結合を中止しました。'))
        except Exception as exc:
            self.events.put(('errorlog','CSV結合エラー:\n'+traceback.format_exc()))
            self.events.put(('log',f'CSV結合エラー: {exc}\n'))
            self.events.put(('done','CSV結合に失敗しました。'))

    def start_audio_merge(self,sources:list[Path],destination:Path,*,fmt:str,
                          bitrate:str,channels:int,sample_rate:int,auto_transcribe:bool,move_originals=False):
        if self.busy:
            show_error(self.root,'別の処理を実行中です。')
            return
        cfg=replace(self.config,python_overrides=dict(self.config.python_overrides))
        python=None
        if auto_transcribe:
            python=self._require_environment(cfg)
            if python is None:
                return
        ffmpeg=media_binary('ffmpeg',python)
        if not ffmpeg:
            for profile in PROFILES:
                ffmpeg=media_binary('ffmpeg',env_python(profile.id,self.config))
                if ffmpeg:break
        if not ffmpeg:
            show_error(self.root,'FFmpegを見つけられませんでした。\n'
                '文字起こし方式をセットアップするか、FFmpegをPATHへ追加してください。')
            return
        self._begin_work()
        self.collision_batch_choice=None
        self._set_busy(True,'音声・動画の結合中…')
        self.worker=threading.Thread(target=self._audio_merge_worker,
            args=(sources,destination,fmt,bitrate,channels,sample_rate,
                  auto_transcribe,cfg,python,ffmpeg,self.hf_token,move_originals),daemon=True)
        self.worker.start()

    def _audio_merge_worker(self,sources:list[Path],dest:Path,fmt:str,bitrate:str,
                            channels:int,sample_rate:int,auto_transcribe:bool,
                            cfg:Config,python:Path|None,ffmpeg:str,token:str,move_originals=False):
        try:
            check_cancel(self.stop_event)
            stamps=source_stamps(sources) if move_originals else None
            components = [probe_metadata(src,python,cancel=self.stop_event,registry=getattr(self,'processes',None)) for src in sources]
            self.events.put(('log',f'音声結合：{len(sources)}件 / {fmt} / {bitrate if fmt in ("mp3","m4a") else "可逆・非圧縮"}\n'))
            concat_audio(sources,dest,ffmpeg=ffmpeg,fmt=fmt,bitrate=bitrate,
                channels=channels,sample_rate=sample_rate,cancel=self.stop_event,registry=getattr(self,'processes',None),
                progress=lambda message:self.events.put(('log',message+'\n')))
            note=self._archive_merged_sources(sources,dest,stamps) if move_originals else ''
            self.events.put(('log',f'音声結合完了: {dest}\n'+note+'\n'))
            if self.stop_event.is_set() or not auto_transcribe:
                self.events.put(('done','音声結合を完了しました。'+note))
                return
            assert python is not None
            started=datetime.now()
            created=next((m.created for m in components if m.created is not None),None)
            text=choose_output(dest,cfg,transcribed_at=started,media_created_at=created)
            device=self._resolve_device(cfg,python)
            self.events.put(('status',f'結合した音声を文字起こし中: {dest.name}'))
            result,info=self._run_one(dest,text,cfg,python,token,device,
                                      transcribed_at=started,components=components)
            self.events.put(('log',f'[{result}] {info}\n'))
            self.events.put(('done',('結合・文字起こしが完了しました。' if result=='完了' else
                '音声結合は完了しましたが、文字起こしは'+result+'です。')+note))
        except InterruptedError:
            self.events.put(('log','音声結合を中止しました。\n'))
            self.events.put(('done','音声結合を中止しました。'))
        except Exception as exc:
            self.events.put(('errorlog','音声結合エラー:\n'+redact(traceback.format_exc(),token)))
            self.events.put(('log','音声結合エラー: '+redact(str(exc),token)+'\n'))
            self.events.put(('done','音声結合に失敗しました。'))

    def _batch_worker(self,files:list[Path],cfg:Config,python:Path,token:str,job_options=None):
        try:
            device=self._resolve_device(cfg,python)
            self.events.put(('log',f'選択方式: {PROFILE_NAMES[cfg.profile]} / 実行デバイス: {device}\n'))
            for i,source in enumerate(files,1):
                if self.stop_event.is_set():break
                self.events.put(('status',f'処理中 {i}/{len(files)}: {source.name}'))
                self.events.put(('new-file', source.name, i, len(files)))
                started=datetime.now()
                created=(probe_metadata(source,python,cancel=self.stop_event,registry=getattr(self,'processes',None)).created if cfg.filename_media_creation else None)
                options=(job_options or {}).get(str(source))
                item_cfg=(replace(cfg,output_mode=options[0],output_dir=options[1],output_same_as_input=False) if options else cfg)
                dest=choose_output(source,item_cfg,transcribed_at=started,media_created_at=created)
                with getattr(self,'queue_policy_lock',threading.Lock()):
                    policy=getattr(self,'job_policies',{}).get(str(source),'ask')
                    self.active_job_key=str(source)
                result,info=self._run_one(source,dest,item_cfg,python,token,device,
                                           transcribed_at=started,collision_policy=policy)
                with getattr(self,'queue_policy_lock',threading.Lock()):self.active_job_key=None
                self.events.put(('job',str(source),job_status_label(result,info)))
                self.events.put(('log',f'[{result}] {info}\n'))
                if result=='中止':
                    self.stop_event.set()
                    break
        except Exception as exc:
            self.events.put(('errorlog','一括処理エラー:\n'+redact(traceback.format_exc(),token)))
            self.events.put(('log',f'致命的エラー: {redact(str(exc),token)}\n'))
        finally:
            self.events.put(('done','一括処理を終了しました。'))

    def _run_one(self,source:Path,dest:Path,cfg:Config,python:Path,token:str,device:str,
                 *,transcribed_at:datetime|None=None,components=(),collision_policy:str='ask'):
        if getattr(self,'stop_event',None) is not None and self.stop_event.is_set():return ('中止','処理を中止しました。')
        if not source.is_file():return ('失敗','入力ファイルがなくなりました: '+str(source))
        if source.resolve()==dest.resolve():return ('失敗','入力・出力のパスが同一です。')
        target_txt=dest if cfg.output_mode in ('txt','both') else None
        target_csv=dest.with_suffix('.csv') if cfg.output_mode in ('csv','both') else None
        targets=[p for p in (target_txt,target_csv) if p is not None]
        # The desired filename (including model/date suffix) is authoritative.
        # Other old output names do NOT count as collisions and must not skip a new run.
        if collision_policy not in ('ask','skip','overwrite','rename'):
            return ('失敗', f'既存出力の扱いが不正です: {collision_policy}')
        if collision_policy=='skip':
            return ('スキップ',f'キューでスキップ指定: {source.name}')
        permitted_overwrite=cfg.overwrite if collision_policy=='ask' else collision_policy=='overwrite'
        existing=[p for p in targets if p.exists()]
        if existing and collision_policy=='ask' and getattr(self,'interactive_collisions',False):
            choice=self._request_overwrite(source,existing)
            if choice=='cancel':return ('中止',f'ユーザーがキャンセルしました: {source.name}')
            if choice=='skip':return ('スキップ',f'個別確認でスキップ: {source.name}')
            if choice=='rename':collision_policy='rename'
            permitted_overwrite=(choice=='overwrite')
        if collision_policy=='rename' and existing:
            # Keep TXT/CSV companion names identical and reserve both destinations.
            from queue_policies import rename_destination
            dest=rename_destination(dest, cfg.output_mode)
            target_txt=dest if cfg.output_mode in ('txt','both') else None
            target_csv=dest.with_suffix('.csv') if cfg.output_mode in ('csv','both') else None
            targets=[p for p in (target_txt,target_csv) if p is not None]
            permitted_overwrite=False
        if any(p.exists() for p in targets) and not permitted_overwrite:
            return ('スキップ',f'既存の指定名を保護しました: {source.name} / {targets}')
        try:
            dest.parent.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            return ('失敗',f'出力フォルダーを作成できません: {exc}')
        stage=dest.parent / f'.{dest.stem}.{uuid.uuid4().hex[:12]}.processing.txt'
        args=build_command(source,stage,cfg,python,device)
        env=utf8_child_env()
        from term_hints import apply_hint_environment
        apply_hint_environment(env,cfg)
        if hasattr(self,'resource_control_file'):
            env['TRANSCRIBER_RESOURCE_CONTROL']=str(self.resource_control_file)
        if BY_ID[cfg.profile].diarizer == 'pyannote':
            if token:env['HF_TOKEN']=token
        else:
            env.pop('HF_TOKEN', None)
        try:
            diarization_failures=0
            flags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
            with owned_process(self,args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                  text=True,encoding='utf-8',errors='replace',env=env,
                                  cwd=str(BASE),creationflags=flags,bufsize=1) as proc:
                with self.process_lock:self.active_process=proc
                if hasattr(self,'cpu_limit'):
                    try:
                        cap_state=self.cpu_limit.attach(proc.pid,self.config.cpu_limit_percent)
                        if self.config.cpu_limit_percent is not None:
                            self.events.put(('log','[CPU] '+cap_state+'\n'))
                    except OSError as exc:
                        self.events.put(('log',f'[CPU] 上限制御の適用失敗: {exc}\n'))
                assert proc.stdout is not None
                for line in proc.stdout:
                    safe_line=redact(line,token)
                    failures=diarization_warning_count(safe_line)
                    if failures is not None:
                        diarization_failures=max(diarization_failures,failures)
                        self.events.put(('log',f'[話者分離注意] {failures}件の話者分離に失敗しました。文字起こしは保存します。\n'))
                        continue
                    update=parse_engine_line(safe_line)
                    if update is not None:
                        self.events.put(('engine-progress',update))
                    if safe_line.startswith(SETUP_PROGRESS_PREFIX) and update is not None:
                        # Frequent setup/download updates belong in the progress
                        # display; avoid flooding the log with machine JSON.
                        pass
                    elif safe_line.startswith(PROGRESS_PREFIX):
                        # Raw machine-readable JSON is not useful to users.
                        # Mirror the progress to the detailed log in Japanese.
                        if update is not None:
                            self.events.put(('log',human_readable_engine_progress(update)+'\n'))
                        else:
                            # Preserve unrecognized machine events for diagnostics.
                            self.events.put(('log',safe_line))
                    else:
                        self.events.put(('log',safe_line))
                    if self.stop_event.is_set() and proc.poll() is None:self._kill(proc)
                rc=proc.wait()
            if self.stop_event.is_set():return ('中止',f'中止しました: {source.name}')
            if rc!=0:return ('失敗',f'{source.name}：エンジン終了コード{rc}。上のログを確認してください。')
            if not stage.is_file() or stage.stat().st_size==0:
                return ('失敗',f'{source.name}：出力TXTが作成されませんでした。')
            if any(p.exists() for p in targets) and not permitted_overwrite:
                return ('スキップ',f'処理中に同名ファイルが作成されたため保護: {targets}')
            if cfg.txt_metadata_header:
                metadata = probe_metadata(source,python,cancel=self.stop_event,registry=getattr(self,'processes',None))
                profile = BY_ID[cfg.profile]
                model = (cfg.model_openai if profile.asr=='openai' else
                         cfg.model_faster if profile.asr=='faster' else cfg.qwen_model)
                if components:  # Combined media: report source-specific recording times.
                    metadata = MediaMetadata(source, None, '結合による新規生成')
                prefix = metadata_header(metadata,transcribed_at=transcribed_at or datetime.now(),
                    profile=profile.name,model=model,device=device,components=components)
                header_stage=stage.with_name(stage.stem+'.header.txt')
                try:
                    with header_stage.open('wb') as out, stage.open('rb') as src:
                        out.write(prefix.encode('utf-8-sig'))
                        shutil.copyfileobj(src,out)
                    header_stage.replace(stage)
                finally:
                    header_stage.unlink(missing_ok=True)
            if self.stop_event.is_set():return ('中止','処理を中止しました。')
            # Preserve recognized text first. CSV conversion is best effort and
            # must never discard an already completed TXT transcription.
            try:
                status, saved, problem = publish_transcription(
                    stage, txt=target_txt, csv_path=target_csv,
                    exporter=export_transcript_csv,
                    source_name=source.name, overwrite=permitted_overwrite)
                if diarization_failures:
                    status='一部完了'
                self.events.put(('transcript-saved', str(source), [str(p) for p in saved], status))
                detail = f'{source.name} → {" ".join(str(p) for p in saved)}'
                if problem:
                    self.events.put(('log', f'[CSV注意] {problem}\n'))
                    detail += ' / ' + problem
                if diarization_failures:
                    warning=f'話者分離{diarization_failures}件失敗。保存した文字起こしの話者ラベルを確認してください。'
                    detail += ' / '+warning
                    self.events.put(('errorlog',f'{source.name}: {warning}'))
                return status, detail
            except Exception as exc:
                self.events.put(('errorlog','文字起こし出力の保存失敗:\n'+redact(traceback.format_exc(),token)))
                return ('失敗', redact(f'{source.name}：出力保存失敗: {exc}', token))
        except InterruptedError:
            return ('中止','処理を中止しました。')
        except (OSError,subprocess.SubprocessError,ValueError) as exc:
            return ('失敗',redact(f'{source.name}：{exc}',token))
        finally:
            if hasattr(self,'cpu_limit'):self.cpu_limit.close()
            with self.process_lock:self.active_process=None
            try:stage.unlink(missing_ok=True)
            except OSError:pass

    def _begin_work(self):
        self.stop_event.clear()
        self.all_stop_requested=False
        self.all_stop_waiting=False

    @staticmethod
    def _kill(proc):
        kill_owned_process(proc)

    def stop_all(self):
        registry=getattr(self,'processes',None)
        if not self.busy and not (registry is not None and registry.active()):return
        if getattr(self,'all_stop_requested',False):return
        self.all_stop_requested=True
        self.all_stop_waiting=True
        self.stop_event.set();self.live_cancel.set();self.gamebar_stop.set()
        self.watch_mode=False
        self.pending_capture_target=None;self.pending_speaker_edits.clear()
        self.live_stopping=bool(self.live_active or self.gamebar_active)
        if self.live_stop_file is not None:
            try:self.live_stop_file.touch()
            except OSError as exc:self.log(f'録音の終了要求: {exc}\n')
        if registry is not None:registry.stop_all()
        with self.process_lock:process=self.active_process
        if process:threading.Thread(target=self._kill,args=(process,),daemon=True).start()
        self.stop_btn.configure(state='disabled');self.ui.stop_all_button.configure(state='disabled')
        self.status.set('すべての処理を停止中… 録音の確定と終了を待っています。')
        self.log('全停止を要求しました。待機中の認識・自動処理も中止します。\n')

    def stop(self):
        if not self.busy:return
        if self.gamebar_active:
            self.gamebar_stop.set()
            if getattr(self,'gamebar_live_active',False) and not self.live_stopping:
                self.live_stopping=True
                if getattr(self,'live_finalize_on_stop',False) and getattr(self,'live_chunk_active',False):
                    with self.process_lock:proc=self.active_process
                    if proc:threading.Thread(target=self._kill,args=(proc,),daemon=True).start()
            self.stop_btn.configure(text='■ 録画停止中…',state='disabled')
            self.status.set('Game Barと音声録音を連動停止中…')
            return
        if self.live_active:
            if not self.live_stopping:
                self.live_stopping=True
                if self.live_stop_file is not None:self.live_stop_file.touch()
                self.stop_btn.configure(text='■ 録音終了処理中…',state='disabled')
                self.status.set('録音終了を要求しました。WAVを確定し、残りの認識を処理中…')
                self.events.put(('log','ライブ録音終了要求。録音ファイルを閉じて残りの文字起こしを完了します。\n'))
                if getattr(self,'live_finalize_on_stop',False) and getattr(self,'live_chunk_active',False):
                    with self.process_lock:proc=self.active_process
                    if proc:threading.Thread(target=self._kill,args=(proc,),daemon=True).start()
            else:
                self.status.set('録音を終了しています。残りの認識が完了すると自動で終了します。')
            return
        self.stop_event.set()
        self.status.set('停止要求を受け付けました。処理を終了しています…')
        with self.process_lock:proc=self.active_process
        if proc:
            threading.Thread(target=self._kill,args=(proc,),daemon=True).start()

    def cancel_live_transcription(self):
        if not (self.live_active or self.gamebar_active):return
        self.live_cancel.set()
        self.stop_event.set()
        if not self.live_stopping:self.stop()
        with self.process_lock:proc=self.active_process
        if proc:threading.Thread(target=self._kill,args=(proc,),daemon=True).start()
        self.status.set('文字起こしの中断を要求しました。録音ファイルは保持します。')

    @staticmethod
    def live_capture_python() -> Path:
        directory = CODE_DIR / 'environments' / 'live-capture'
        return directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

    def start_capture_setup(self):
        if self.busy:
            show_error(self.root,'ほかの処理が実行中です。');return
        self._begin_work()
        self._set_busy(True,'録音専用環境を準備中…')
        self.worker=threading.Thread(target=self._capture_setup_worker,daemon=True)
        self.worker.start()

    def _capture_setup_worker(self):
        try:
            args=[console_interpreter(),'-X','utf8','-u',str(CODE_DIR/'setup_live_capture.py')]
            with owned_process(self,args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                      encoding='utf-8',errors='replace',env=utf8_child_env(),cwd=str(BASE),
                      creationflags=hidden_child_flags(),bufsize=1) as proc:
                with self.process_lock:self.active_process=proc
                for line in proc.stdout:
                    self.events.put(('log',line))
                    if self.stop_event.is_set():self._kill(proc)
                rc=proc.wait()
            if rc == 0 and not self.stop_event.is_set():
                self.events.put(('capture-ready',))
            self.events.put(('done','録音環境のセットアップ完了' if rc==0 and not self.stop_event.is_set() else
                                 '録音環境のセットアップ失敗。ログを確認してください。'))
        except Exception as exc:
            self.events.put(('errorlog','録音環境セットアップ失敗:\n'+traceback.format_exc()))
            self.events.put(('log','録音環境セットアップ失敗: '+str(exc)+'\n'))
            self.events.put(('done','録音環境セットアップ失敗'))
        finally:
            with self.process_lock:self.active_process=None

    def remember_live_settings(self, opts:LiveOptions) -> None:
        """Persist recording codec and folder without modifying ASR credentials."""
        cfg=replace(self.config,
            live_recording_dir=recording_folder_preference(opts.folder),
            live_recording_format=opts.recording_format,
            live_recording_bitrate=opts.recording_bitrate,
            live_recording_pcm_bits=opts.recording_pcm_bits,
            live_recording_sample_rate=opts.recording_sample_rate,
            live_recording_channels=opts.recording_channels,
            live_channel_mode=opts.channel_mode,live_post_transcribe=opts.post_transcribe)
        cfg.validate()
        try:
            save_config(cfg)
            self.config=cfg
        except OSError as exc:
            self.events.put(('log',f'[設定] 録音設定の保存に失敗: {exc}\n'))

    def start_standard_import(self, options:ExternalOptions, *, auto_transcribe:bool=True)->bool:
        """Import an OS-recorded video; transcribe only after safe audio remux."""
        if self.busy:
            show_error(self.root,'ほかの処理の終了後に取り込んでください。')
            return False
        try:options.validate()
        except (OSError,ValueError) as exc:show_error(self.root,str(exc));return False
        cfg=replace(self.config,python_overrides=dict(self.config.python_overrides))
        transcriber=env_python(cfg.profile,cfg) if auto_transcribe else None
        ffmpeg=media_binary('ffmpeg',transcriber)
        if not ffmpeg:
            for profile in PROFILES:
                ffmpeg=media_binary('ffmpeg',env_python(profile.id,self.config))
                if ffmpeg:break
        if not ffmpeg:
            show_error(self.root,'動画の音声確認・合成にFFmpegが必要です。文字起こし環境をセットアップするか、FFmpegを導入してください。')
            return False
        self._begin_work()
        self.collision_batch_choice=None
        self._set_busy(True,'Windows標準録画を取り込み中…')
        self.worker=threading.Thread(target=self._standard_import_worker,
            args=(options,ffmpeg,auto_transcribe,cfg,transcriber,self.hf_token),daemon=True)
        self.worker.start()
        return True

    def _standard_import_worker(self, options:ExternalOptions,ffmpeg:str,auto_transcribe:bool,
                                cfg:Config,transcriber:Path|None,token:str):
        media=None
        try:
            media=finalize_external(options,ffmpeg,has_audio=lambda video,engine:has_audio_stream(video,engine,cancel=self.stop_event,registry=getattr(self,'processes',None)),
                stop=self.stop_event,log=lambda line:self.events.put(('log',line+'\n')),registry=getattr(self,'processes',None))
            media=Path(media).resolve()
            self.events.put(('capture-queue',media,'処理中' if auto_transcribe and not self.stop_event.is_set() and not self.quit_when_idle else '録音済み'))
            self.events.put(('log',f'[標準録画] 認識対象: {media}\n'))
            if not auto_transcribe or self.stop_event.is_set() or self.quit_when_idle:
                self.events.put(('done',f'動画を取り込みました: {media}'))
                return
            if transcriber is None or not transcriber.is_file():
                self.events.put(('job',str(media),'録音済み'))
                self.events.put(('done','動画は保存済みです。選択中のAI環境をセットアップしてから文字起こししてください。'))
                return
            if BY_ID[cfg.profile].diarizer=='pyannote' and not token:
                self.events.put(('job',str(media),'録音済み'))
                self.events.put(('done','動画は保存済みです。pyannoteに必要なHF_TOKENが設定されていません。'))
                return
            if not has_audio_stream(media,ffmpeg):
                self.events.put(('job',str(media),'スキップ'))
                self.events.put(('done','動画は保存済みです。音声ストリームがないため文字起こしをスキップしました。'))
                return
            status,detail=self._transcribe_completed_screen(media,cfg,transcriber,token)
            self.events.put(('job',str(media),status))
            self.events.put(('log',f'[標準録画の文字起こし] {status}: {detail}\n'))
            message=(f'標準録画の取り込み・文字起こしが完了しました: {media}' if status=='完了'
                     else f'動画は保存済みです。文字起こしは{status}です: {detail}')
            if status in ('失敗','一部完了'):self.events.put(('errorlog',message))
            self.events.put(('done',message))
        except InterruptedError as exc:
            if media is not None:self.events.put(('job',str(media),'中止'))
            self.events.put(('done',str(exc)))
        except Exception as exc:
            if media is not None:self.events.put(('job',str(media),'失敗'))
            detail=redact(traceback.format_exc(),token)
            self.events.put(('errorlog','標準録画の取り込み失敗: '+detail))
            self.events.put(('done',f'標準録画の取り込みに失敗しました: {exc}。原本は保存されています。'))

    def start_gamebar_recording(self, opts:GameBarOptions)->bool:
        """Initiate OS recording without starting any FFmpeg video-grab process."""
        if self.busy:
            show_error(self.root,'他の処理中です。録画を開始できません。');return False
        if sys.platform!='win32':
            show_error(self.root,'Game Barとの連動はWindows専用です。');return False
        try:opts.validate()
        except (OSError,ValueError) as exc:
            show_error(self.root,str(exc));return False
        capture_python=self.live_capture_python()
        if not capture_python.is_file():
            show_error(self.root,'一括セットアップの「録音環境」または「録音環境をセットアップ」を実行してください。');return False
        if not self._require_media_tools():
            return False
        cfg=replace(self.config,python_overrides=dict(self.config.python_overrides))
        cfg.gamebar_shortcut=opts.shortcut
        cfg.gamebar_audio_source=opts.source
        cfg.gamebar_captures_dir=str(opts.captures)
        cfg.live_recording_dir=recording_folder_preference(opts.destination)
        cfg.live_recording_format=opts.audio_format
        cfg.live_recording_channels=opts.recording_channels
        cfg.live_channel_mode=opts.channel_mode
        transcriber=env_python(cfg.profile,cfg) if opts.auto_transcribe else None
        if opts.realtime_transcription:
            transcriber=self._require_environment(cfg)
            if transcriber is None:return False
        try:
            save_config(cfg)
            self.config=cfg
        except OSError as exc:
            self.events.put(('log',f'[設定注意] Game Bar設定を保存できません: {exc}\n'))
        ffmpeg=media_binary('ffmpeg',transcriber)
        if not ffmpeg:
            for p in PROFILES:
                ffmpeg=media_binary('ffmpeg',env_python(p.id,cfg))
                if ffmpeg:break
        if not ffmpeg:
            ffmpeg=media_binary('ffmpeg',capture_python)
        self._begin_work()
        self.gamebar_stop.clear()
        self.live_cancel.clear()
        self.live_stopping=False
        self.live_finalize_on_stop=opts.realtime_transcription and opts.auto_transcribe
        self.live_chunk_active=False
        self.collision_batch_choice=None
        self.gamebar_active=True
        self.gamebar_live_active=opts.realtime_transcription
        self._set_busy(True,'Windows標準録画＋音声録音を準備中…')
        if opts.realtime_transcription:self._reset_recognition_preview()
        self.stop_btn.configure(text='■ 録画停止')
        self.worker=threading.Thread(target=self._gamebar_worker,
            args=(opts,cfg,capture_python,transcriber,ffmpeg,self.hf_token),daemon=True)
        self.worker.start()
        return True

    def _gamebar_worker(self, opts:GameBarOptions,cfg:Config,
                        capture_python:Path, transcriber:Path|None,
                        ffmpeg:str|None,token:str):
        """Single lifecycle: start WAV -> shortcut -> stop shortcut + WAV -> ASR.

        The independent audio is authoritative: an absent Game Bar MP4 must not
        cause data loss or prevent transcription. Never claim OS video success
        before actually discovering a finalized, new MP4.
        """
        from gamebar_recording import snapshot_mp4, find_completed_capture, send_hotkey
        import queue as std_queue
        audio_proc=None
        stop_file=None
        output=None
        hotkey_started=False
        hotkey_stopped=False
        started_at=None
        lead_seconds=0.0
        audio_ready_clock=None
        lines=std_queue.Queue()
        audio_finished=threading.Event()
        backlog=std_queue.Queue()
        recognizer=None
        recognition_closed=False
        temporary=None
        preview=None
        def notify(line):self.events.put(('log',line+'\n'))
        def finish_recognition():
            nonlocal recognition_closed
            if recognizer is None or recognition_closed:return
            recognition_closed=True
            self.live_stopping=True
            if opts.auto_transcribe and self.live_chunk_active:
                with self.process_lock:proc=self.active_process
                if proc:self._kill(proc)
            backlog.put(None)
            self.events.put(('status','WAV確定済み。ライブ認識を終了中…'))
            recognizer.join()
        try:
            opts.destination.mkdir(parents=True,exist_ok=True)
            start=datetime.now()
            output=unique_destination(opts.destination/f'標準録画_音声_{start:%Y%m%d_%H%M%S}.wav').resolve()
            if opts.realtime_transcription:
                if transcriber is None or not transcriber.is_file():
                    raise RuntimeError('ライブ文字起こしのAI環境がありません。')
                transcript_folder=(opts.destination if cfg.output_same_as_input else
                    Path(cfg.output_dir).expanduser() if cfg.output_dir.strip() else default_directory('transcriptions'))
                transcript_folder.mkdir(parents=True,exist_ok=True)
                preview=transcript_folder/(output.stem+'.live.txt')
                if preview.exists():raise FileExistsError('ライブ出力が既に存在します: '+str(preview))
                temporary=tempfile.TemporaryDirectory(prefix='.gamebar_live_',dir=opts.destination)
                work=Path(temporary.name)
                device=self._resolve_device(cfg,transcriber)
                def recognize():
                    while True:
                        item=backlog.get()
                        try:
                            if item is None:break
                            if not self.live_cancel.is_set() and not self.stop_event.is_set() and not (self.live_stopping and opts.auto_transcribe):
                                index,offset,path=item
                                self._recognize_live_chunk(path,index,offset,preview,
                                    cfg,transcriber,token,device,work)
                        except Exception:
                            self.events.put(('errorlog','Game Barライブ認識: '+redact(traceback.format_exc(),token)))
                        finally:backlog.task_done()
                recognizer=threading.Thread(target=recognize,name='gamebar-live-asr',daemon=True)
                recognizer.start()
            stop_file=CODE_DIR/f'.gamebar_stop_{uuid.uuid4().hex}.flag'
            before=snapshot_mp4(opts.captures)
            cmd=[str(capture_python),'-X','utf8','-u',str(CODE_DIR/'live_capture.py'),
                '--output',str(output),'--stop-file',str(stop_file),'--source',opts.source,
                '--sample-rate','48000','--channel-mode',opts.channel_mode]
            if opts.realtime_transcription:
                cmd+=['--chunks',str(work/'segments'),'--chunk-seconds',str(opts.chunk_seconds)]
            if opts.source=='mix':
                if opts.mic_device_id:cmd+=['--mic-device-id',opts.mic_device_id]
                if opts.loopback_device_id:cmd+=['--loopback-device-id',opts.loopback_device_id]
            else:
                cmd+=['--device-id',opts.mic_device_id if opts.source=='mic' else opts.loopback_device_id]
            audio_proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
                bufsize=1,env=utf8_child_env(),creationflags=hidden_child_flags())
            if getattr(self,'processes',None) is not None:self.processes.add(audio_proc,recording=True)
            def read_audio():
                try:
                    for line in audio_proc.stdout:
                        self.events.put(('log','[音声] '+line.rstrip()+'\n'))
                        if line.lstrip().startswith('{'):
                            try:item=json.loads(line)
                            except json.JSONDecodeError:continue
                            kind=item.get('event')
                            if kind=='chunk' and recognizer is not None:
                                backlog.put((int(item['index']),float(item['offset']),Path(item['path'])))
                            elif kind=='finished':audio_finished.set()
                            elif kind in ('started','error'):lines.put(item)
                            elif kind=='progress':
                                self.events.put(('status',f'● Game Bar音声収録中 {item.get("seconds")}秒'
                                    +(f' / ライブ認識待ち {backlog.qsize()}区間' if recognizer else '')))
                except (OSError,ValueError) as exc:
                    lines.put({'event':'error','message':str(exc)})
            thread=threading.Thread(target=read_audio,name='gamebar-audio-log',daemon=True)
            thread.start()
            deadline=time.monotonic()+20
            ready=False
            while time.monotonic()<deadline:
                if self.gamebar_stop.is_set():
                    raise InterruptedError('録画開始前にキャンセルされました。')
                if audio_proc.poll() is not None:
                    raise RuntimeError(f'音声録音が初期化前に終了しました (exit={audio_proc.returncode})')
                try:item=lines.get(timeout=.15)
                except std_queue.Empty:continue
                if item.get('event')=='started':
                    audio_ready_clock=time.monotonic()
                    ready=True;break
                if item.get('event')=='error':
                    raise RuntimeError('音声録音開始エラー: '+str(item.get('message')))
            if not ready:raise TimeoutError('音声入力を開始できません（20秒）。録音デバイス設定を確認してください。')
            notify(f'[Game Bar] 音声収録開始: {output}')
            # Give Windows a moment to commit the first frames before the
            # recording shortcut. Save the offset to align video later.
            notify(f'[Game Bar] 対象アプリを前面にして {opts.shortcut} を送信します。')
            if self.gamebar_stop.is_set():raise InterruptedError('録画開始前に停止が要求されました。')
            send_monotonic=time.monotonic()
            try:
                trigger_clock=send_hotkey(opts.shortcut,hwnd=opts.target_hwnd)
                trigger_clock=trigger_clock if isinstance(trigger_clock,(int,float)) else send_monotonic
                started_at=time.time()-(time.monotonic()-trigger_clock)
                lead_seconds=max(0.0,trigger_clock-(audio_ready_clock or send_monotonic))
                hotkey_started=True
                notify('[Game Bar] ショートカットを送信。録画の成功は停止後のMP4で確認します。')
                self.events.put(('status','標準録画＋音声収録中 · 「■ 録画停止」で双方停止'))
            except (OSError,RuntimeError) as exc:
                notify(f'[Game Bar警告] 録画ショートカットの送信に失敗: {exc}。WAVのみ録音を続けます。')
                self.events.put(('errorlog',f'Game Barショートカット送信エラー: {exc}'))
                self.events.put(('status','音声WAVのみ録音中 · 「■ 録画停止」で終了'))
            while not self.gamebar_stop.wait(.2):
                if audio_finished.is_set():break
                if audio_proc.poll() is not None:
                    notify('[警告] 音声プロセスが途中終了しました。動画を停止してください。')
                    break
            # Never send a toggle-style STOP if START could not be dispatched.
            if hotkey_started:
                try:
                    send_hotkey(opts.shortcut,hwnd=opts.target_hwnd)
                    hotkey_stopped=True
                    notify('[Game Bar] 録画停止ショートカットを送信しました。')
                except Exception as exc:
                    self.events.put(('errorlog','Game Bar停止失敗:\n'+redact(traceback.format_exc(),token)))
                    notify(f'[警告] Game Bar停止ショートカット送信失敗: {exc}。Windows側でも録画停止してください。')
            if stop_file is not None:stop_file.touch()
            deadline=time.monotonic()+15
            while True:
                committed=audio_finished.is_set()
                try:
                    rc=audio_proc.wait(timeout=3 if committed else .2)
                    break
                except subprocess.TimeoutExpired:
                    if committed or time.monotonic()>=deadline:
                        audio_proc.kill();rc=audio_proc.wait(timeout=5)
                        if committed:
                            rc=0
                            notify('[音声] WAV確定後のデバイス待機プロセスを終了しました。')
                        else:notify('[警告] 音声録音の正常終了がタイムアウトしました。WAVを確認してください。')
                        break
            thread.join(timeout=2)
            finish_recognition()
            if not output.is_file() or output.stat().st_size<=44:
                raise RuntimeError(f'音声WAVが生成されていません（終了コード {rc}）。録音デバイスを確認してください。')
            self.events.put(('capture-queue',output,'処理中' if opts.auto_transcribe and not self.stop_event.is_set() and not self.quit_when_idle else '録音済み'))
            if preview is not None and preview.is_file() and not opts.auto_transcribe:
                self.events.put(('transcript-saved',str(output),[str(preview)],'ライブ完了'))
            notify(f'[Game Bar] 音声保存: {output} (終了コード {rc})')
            if preview is not None and preview.is_file():notify(f'[Game Bar] ライブ文字起こし保存: {preview}')
            self.gamebar_active=False
            self.events.put(('buttontext','■ 停止'))
            video=None
            if hotkey_started:
                self.events.put(('status','Game BarのMP4確定を確認中…'))
                try:
                    video=find_completed_capture(opts.captures,before,started_at or time.time(),timeout=20,stop=self.stop_event)
                except RuntimeError as exc:
                    notify('[Game Bar] '+str(exc))
                if video is None:
                    notify('[Game Bar] 新しいMP4を確認できません。Win+Alt+RやGame Barの状態、保存先を確認してください。'
                           '音声ファイルの文字起こしは続行します。')
                else:notify(f'[Game Bar] 新しいMP4を確認: {video}')
            if video is not None and opts.create_synced_video and ffmpeg and not self.stop_event.is_set():
                try:
                    from external_recording import ExternalOptions, finalize_external
                    # Audio starts before the video trigger; trim its leading portion.
                    matched=ExternalOptions(video=video,audio=output,
                        offset=-lead_seconds,mode='replace',
                        destination=unique_destination(opts.destination/(video.stem+'_音声同期.mp4')))
                    result=finalize_external(matched,ffmpeg,has_audio=lambda video,engine:has_audio_stream(video,engine,cancel=self.stop_event,registry=getattr(self,'processes',None)),
                                             log=notify,stop=self.stop_event,registry=getattr(self,'processes',None))
                    notify(f'[Game Bar] 音声同期MP4: {result}（元動画とWAVを保護）')
                except Exception as exc:
                    self.events.put(('errorlog','Game Bar音声同期失敗:\n'+redact(traceback.format_exc(),token)))
                    notify(f'[Game Bar] MP4の音声同期に失敗: {exc}。元MP4とWAVは残しています。')
            elif video is not None and opts.create_synced_video:
                notify('[Game Bar] FFmpegが見つからないため同期MP4生成をスキップ。音声WAVは保持。')
            if self.stop_event.is_set():
                self.events.put(('job',str(output),'中止'))
                self.events.put(('done',f'処理を中止しました。音声WAVを保持: {output}'))
                return
            if opts.audio_format!='wav':
                if ffmpeg:
                    try:
                        encoded=encode_live_recording(output,
                            unique_destination(output.with_suffix('.'+opts.audio_format)),
                            ffmpeg=ffmpeg,fmt=opts.audio_format,
                            bitrate=cfg.live_recording_bitrate,pcm_bits=cfg.live_recording_pcm_bits,
                            channels=cfg.live_recording_channels,sample_rate=cfg.live_recording_sample_rate,cancel=self.stop_event,registry=getattr(self,'processes',None))
                        notify(f'[Game Bar] 音声{opts.audio_format.upper()}も保存: {encoded}（元WAVを保持）')
                    except Exception as exc:
                        self.events.put(('errorlog','Game Bar録音形式の変換失敗:\n'+redact(traceback.format_exc(),token)))
                        notify(f'[Game Bar] 録音形式の変換に失敗: {exc}。WAVは残しています。')
                else:notify('[Game Bar] FFmpegが見つからないため音声形式変換を省略。WAVは保存されています。')
            elif ffmpeg:
                try:
                    with wave.open(str(output),'rb') as recorded:
                        needs_conversion=(cfg.live_recording_pcm_bits!=recorded.getsampwidth()*8 or
                            cfg.live_recording_channels!=recorded.getnchannels() or
                            cfg.live_recording_sample_rate!=recorded.getframerate())
                    if needs_conversion:
                        encoded=encode_live_recording(output,
                            unique_destination(output.with_name(output.stem+'_PCM.wav')),ffmpeg=ffmpeg,fmt='wav',
                            bitrate=cfg.live_recording_bitrate,pcm_bits=cfg.live_recording_pcm_bits,
                            channels=cfg.live_recording_channels,sample_rate=cfg.live_recording_sample_rate,cancel=self.stop_event,registry=getattr(self,'processes',None))
                        notify(f'[Game Bar] 設定済みWAVも保存: {encoded}（元WAVを保持）')
                except Exception as exc:
                    self.events.put(('errorlog','Game Bar WAV変換失敗:\n'+redact(traceback.format_exc(),token)))
                    notify(f'[Game Bar] WAVの形式変換に失敗: {exc}。元WAVは残しています。')
            if not opts.auto_transcribe or self.quit_when_idle or self.stop_event.is_set():
                self.events.put(('done',f'録音が完了しました: {output}'))
                return
            if is_silent_pcm(output):
                self.events.put(('job',str(output),'スキップ'))
                self.events.put(('done',f'音声を保存しました: {output}。無音PCMのため認識は省略しました。'))
                return
            if transcriber is None or not transcriber.is_file():
                self.events.put(('job',str(output),'録音済み'))
                self.events.put(('done',f'音声保存済み: {output}。選択中のAI環境が未セットアップのため自動認識はスキップ。'))
                return
            if BY_ID[cfg.profile].diarizer=='pyannote' and not token:
                self.events.put(('job',str(output),'録音済み'))
                self.events.put(('done',f'音声保存済み: {output}。pyannoteのHF_TOKENが必要なため認識をスキップ。'))
                return
            # Run ASR on the WAV; game bar capture availability is not a dependency.
            self.events.put(('log',f'[録画後認識] 独立録音のWAVを文字起こし: {output}\n'))
            status,detail=self._transcribe_completed_screen(output,cfg,transcriber,token)
            self.events.put(('job',str(output),status))
            self.events.put(('done',f'音声保存済み: {output} / 文字起こし: {status} / {detail}'))
            if status in ('失敗','一部完了'):
                self.events.put(('errorlog',f'録画後の文字起こし: {detail}'))
        except InterruptedError as exc:
            if output is not None and output.is_file():self.events.put(('capture-queue',output,'中止'))
            self.events.put(('done',f'{exc}。録音済みファイルがある場合は残しています。'))
        except Exception as exc:
            if output is not None and output.is_file():self.events.put(('capture-queue',output,'失敗'))
            self.events.put(('errorlog','Game Bar連動録画エラー: '+redact(traceback.format_exc(),token)))
            self.events.put(('done',f'録画連動に失敗しました: {exc}。音声ファイルと診断ログをご確認ください。'))
        finally:
            self.gamebar_active=False
            self.gamebar_live_active=False
            self.events.put(('buttontext','■ 停止'))
            if hotkey_started and not hotkey_stopped:
                try:send_hotkey(opts.shortcut,hwnd=opts.target_hwnd)
                except Exception as exc:
                    self.events.put(('errorlog','Game Bar停止失敗:\n'+redact(traceback.format_exc(),token)))
                    notify(f'[警告] Game Bar停止失敗: {exc}。Windows側で停止してください。')
            if stop_file is not None:
                try:stop_file.touch()
                except OSError:pass
            if audio_proc is not None and audio_proc.poll() is None:
                try:audio_proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    audio_proc.kill();audio_proc.wait(timeout=4)
            if getattr(self,'processes',None) is not None and audio_proc is not None:self.processes.remove(audio_proc)
            finish_recognition()
            if temporary is not None:temporary.cleanup()
            self.live_stopping=False
            self.live_chunk_active=False
            if stop_file is not None:stop_file.unlink(missing_ok=True)

    def _transcribe_completed_screen(self, media: Path, cfg: Config, python: Path,
                                     token: str) -> tuple[str, str]:
        """Reuse the ordinary ASR pipeline on the published MP4, not a temporary MKV.

        This must be called after gamebar_active=False so a second Stop cancels ASR
        instead of asking the recording process to stop again.
        """
        if self.stop_event.is_set():
            return '中止','録画は保存済みです。自動文字起こしを中止しました。'
        started=datetime.now()
        metadata=probe_metadata(media,python) if cfg.filename_media_creation else None
        destination=choose_output(media,cfg,transcribed_at=started,
                     media_created_at=metadata.created if metadata else None)
        self.events.put(('new-file',media.name,1,1))
        self.events.put(('status',f'録画完了 / 文字起こし中: {media.name}'))
        device=self._resolve_device(cfg,python)
        self.events.put(('log',f'[録画後認識] {PROFILE_NAMES[cfg.profile]} / {device} / 出力: {cfg.output_mode}\n'))
        status, detail=self._run_one(media,destination,cfg,python,token,device,
                                      transcribed_at=started,collision_policy='ask')
        self.events.put(('log',f'[録画後認識] [{status}] {detail}\n'))
        return status, detail

    def start_live(self,opts:LiveOptions):
        if self.busy:
            if self.in_tray:self.restore_from_tray()
            show_error(self.root,'別の処理を実行中です。');return
        try:opts.validate()
        except ValueError as exc:
            if self.in_tray:self.restore_from_tray()
            show_error(self.root,str(exc));return
        capture_python=self.live_capture_python()
        if not capture_python.is_file():
            if self.in_tray:self.restore_from_tray()
            show_error(self.root,'録音専用の環境がありません。リアルタイム録音画面の「録音環境をセットアップ」を実行してください。')
            return
        if not self._require_media_tools():
            return
        cfg=replace(self.config,python_overrides=dict(self.config.python_overrides))
        transcriber=None
        if opts.needs_transcription:
            transcriber=self._require_environment(cfg)
            if transcriber is None:return
        try:opts.folder.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            if self.in_tray:self.restore_from_tray()
            show_error(self.root,'出力先の作成に失敗: '+str(exc));return
        self._begin_work()
        self.live_cancel.clear()
        self.live_stopping=False
        self.live_active=True
        self.live_finalize_on_stop=opts.needs_final_transcription
        self.live_chunk_active=False
        self.watch_mode=False
        self._set_busy(True,'ライブ録音を準備中…')
        if opts.realtime_transcription:self._reset_recognition_preview()
        self.worker=threading.Thread(target=self._live_worker,
            args=(opts,capture_python,transcriber,cfg,self.hf_token),daemon=True)
        self.worker.start()

    def _recognize_live_chunk(self,segment:Path,index:int,offset:float,preview:Path,
                    cfg:Config,python:Path,token:str,device:str,remaining:Path):
        if self.live_cancel.is_set() or self.stop_event.is_set():return
        text=remaining / f'recognized_{index:06d}.txt'
        args=build_command(segment,text,cfg,python,device)
        env=utf8_child_env()
        from term_hints import apply_hint_environment
        apply_hint_environment(env,cfg)
        if BY_ID[cfg.profile].diarizer=='pyannote' and token:env['HF_TOKEN']=token
        else:env.pop('HF_TOKEN',None)
        try:
            if is_silent_pcm(segment):
                self.events.put(('log',f'[ライブ認識] 区間 {index+1} は無音PCMのため認識を省略しました。\n'))
                return
            self.live_chunk_active=True
            streamed_text=False
            self.events.put(('status',f'録音継続中 / 区間 {index+1} を文字起こし中…'))
            with owned_process(self,args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                  text=True,encoding='utf-8',errors='replace',cwd=str(BASE),
                  env=env,creationflags=hidden_child_flags(),bufsize=1) as proc:
                with self.process_lock:self.active_process=proc
                if self.live_cancel.is_set() or self.stop_event.is_set() or (self.live_stopping and getattr(self,'live_finalize_on_stop',False)):self._kill(proc)
                for line in proc.stdout:
                    safe_line=redact(line,token)
                    failures=diarization_warning_count(safe_line)
                    if failures is not None:
                        warning=f'[ライブ認識] 区間 {index+1}: 話者分離{failures}件失敗。話者ラベルを確認してください。'
                        self.events.put(('log',warning+'\n'))
                        self.events.put(('errorlog',warning))
                        continue
                    update=parse_engine_line(safe_line)
                    if update is not None:
                        streamed_text=streamed_text or bool(update.snippet.strip())
                        self.events.put(('engine-progress',replace(update,
                            stage=f'ライブ認識 · 区間 {index+1} · {update.stage}')))
                    readable=(human_readable_engine_progress(update)+'\n'
                        if update is not None and safe_line.startswith(PROGRESS_PREFIX) else safe_line)
                    if not (safe_line.startswith(SETUP_PROGRESS_PREFIX) and update is not None):
                        self.events.put(('log','[ライブ認識] '+readable))
                    if (self.live_cancel.is_set() or (self.live_stopping and
                            getattr(self,'live_finalize_on_stop',False))) and proc.poll() is None:self._kill(proc)
                rc=proc.wait()
            if self.live_cancel.is_set():
                self.events.put(('log','[ライブ認識] 文字起こしを中断しました。\n'))
                return
            if self.live_stopping and getattr(self,'live_finalize_on_stop',False):
                self.events.put(('log','[ライブ認識] 速報の処理を終了し、録音全体の最終解析へ進みます。\n'))
                return
            if rc!=0 or not text.is_file():
                self.events.put(('errorlog',f'区間 {index+1} の文字起こし異常終了: code={rc}, path={segment}'))
                self.events.put(('log',f'[注意] 区間 {index+1} の認識に失敗。元音声は録音ファイルに残っています。\n'))
                return
            count=append_provisional(text,preview,offset=offset,index=index,
                                     first=not preview.exists())
            if not streamed_text:
                # Older/external engines may save TXT without structured text events.
                self.events.put(('engine-progress',EngineUpdate('ライブ認識',
                    snippet='\n'.join(shifted_lines(text,offset)))))
            self.events.put(('log',f'[ライブ認識] 区間 {index+1} を追加 ({count}行、開始位置 {offset:.1f}秒): {preview}\n'))
        except Exception as exc:
            self.events.put(('errorlog',f'区間 {index+1}: {redact(traceback.format_exc(),token)}'))
            self.events.put(('log',f'[注意] 区間 {index+1}: {redact(str(exc),token)}\n'))
        finally:
            self.live_chunk_active=False
            with self.process_lock:self.active_process=None
            text.unlink(missing_ok=True)
            segment.unlink(missing_ok=True)

    def _live_worker(self,opts:LiveOptions,capture_python:Path,
                     transcriber:Path|None,cfg:Config,token:str):
        if not hasattr(self,'stop_event'):self.stop_event=threading.Event()
        recording=None
        actual_recording=None
        silent_final=False
        try:
            started=datetime.now()
            recording=collision_safe_path(opts.folder,started,
                          stem='ライブ録音_Mic+PC' if opts.source=='mix' else 'ライブ録音')
            transcript_folder=(opts.folder if cfg.output_same_as_input else Path(cfg.output_dir).expanduser() if cfg.output_dir.strip()
                               else default_directory('transcriptions'))
            transcript_folder.mkdir(parents=True,exist_ok=True)
            preview=transcript_folder / (recording.stem+'.live.txt')
            if opts.realtime_transcription and preview.exists():
                raise FileExistsError('ライブ文字起こしの出力先が既に存在します: '+str(preview))
            with tempfile.TemporaryDirectory(prefix='.live_work_',dir=opts.folder) as temporary:
                temp=Path(temporary)
                self.live_stop_file=temp/'stop.flag'
                if self.live_stopping:self.live_stop_file.touch()
                actual_recording=recording if opts.keep_audio else temp / recording.name
                segments=temp/'segments' if opts.realtime_transcription else None
                backlog=queue.Queue()
                device='cpu'
                if opts.realtime_transcription:
                    assert transcriber is not None
                    device=self._resolve_device(cfg,transcriber)
                def recognize():
                    while True:
                        item=backlog.get()
                        if item is None:
                            backlog.task_done()
                            break
                        idx,offset,path=item
                        try:
                            if not self.live_cancel.is_set() and not self.stop_event.is_set() and not (self.live_stopping and opts.needs_final_transcription):
                                self._recognize_live_chunk(path,idx,offset,preview,
                                                    cfg,transcriber,token,device,temp)
                        finally:
                            backlog.task_done()
                recognizer=None
                if opts.realtime_transcription:
                    recognizer=threading.Thread(target=recognize,daemon=True)
                    recognizer.start()
                cmd=[str(capture_python),'-X','utf8','-u',str(CODE_DIR/'live_capture.py'),
                     '--source',opts.source,'--device-id',opts.device_id,
                     '--output',str(actual_recording),'--stop-file',str(self.live_stop_file),
                     '--sample-rate','48000','--chunk-seconds',str(opts.chunk_seconds),
                     '--channel-mode',opts.channel_mode]
                if opts.source=='mix':
                    cmd += ['--mic-device-id',opts.mic_device_id,
                            '--loopback-device-id',opts.loopback_device_id,
                            '--mic-gain',str(opts.mic_gain),
                            '--loopback-gain',str(opts.loopback_gain)]
                if segments:cmd += ['--chunks',str(segments)]
                rc=-1
                capture_messages=[]
                capture_error=None
                capture_finished=False
                try:
                    with owned_process(self,cmd,recording=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                          text=True,encoding='utf-8',errors='replace',env=utf8_child_env(),
                          cwd=str(BASE),creationflags=hidden_child_flags(),bufsize=1) as proc:
                        self.capture_process=proc
                        for line in proc.stdout:
                            try:msg=json.loads(line)
                            except json.JSONDecodeError:
                                capture_messages.append(line.rstrip())
                                capture_messages=capture_messages[-12:]
                                self.events.put(('log','[録音] '+line));continue
                            kind=msg.get('event')
                            if kind=='chunk':
                                backlog.put((int(msg['index']),float(msg['offset']),Path(msg['path'])))
                                if backlog.qsize()>5 and backlog.qsize()%5==0:
                                    self.events.put(('log',f'文字起こし待機が{backlog.qsize()}区間。'
                                                 '録音自体は継続します。\n'))
                            elif kind=='started':
                                if msg.get('source')=='mix':
                                    label=f'マイク: {msg.get("mic")} / PC再生音: {msg.get("loopback")}'
                                else:
                                    label=str(msg.get('device'))
                                self.events.put(('log',f'[録音開始] {label} → {actual_recording}\n'))
                                self.events.put(('status','● ライブ録音中。停止で録音終了・認識処理を完了します。'))
                            elif kind=='progress':
                                self.events.put(('status',f'● 録音中 {msg.get("seconds")}秒 / 待機中 {backlog.qsize()}区間'))
                            elif kind=='finished':
                                self.events.put(('log',f'[録音完了] {msg.get("seconds")}秒 → {actual_recording}\n'))
                                capture_finished=True
                                break
                            elif kind in ('warning','error'):
                                self.events.put(('log',f'[{kind}] {msg.get("message")}\n'))
                                if kind=='error':
                                    capture_error=msg.get('message') or '不明なエラー'
                                    if msg.get('detail'):
                                        self.events.put(('log','[録音詳細] '+str(msg['detail'])+'\n'))
                                    self.events.put(('errorlog',
                                        '[録音プロセス] '+str(capture_error)+'\n'+str(msg.get('detail',''))))
                        try:rc=proc.wait(timeout=3 if capture_finished else None)
                        except subprocess.TimeoutExpired:
                            # The child has already closed the WAV before its
                            # finished event. Device teardown may still block.
                            proc.kill();proc.wait(timeout=3);rc=0
                            self.events.put(('log','[録音] WAV確定後のデバイス待機プロセスを終了しました。\n'))
                finally:
                    self.capture_process=None
                    if recognizer:
                        backlog.put(None)
                        self.events.put(('status','WAV確定済み。残りの文字起こし区間を処理中…'))
                        recognizer.join()
                if not actual_recording.is_file() or actual_recording.stat().st_size<=44:
                    hint=(capture_error or (' / '.join(capture_messages[-3:]) if capture_messages else '')
                          or '録音プロセスから診断メッセージが返っていません')
                    self.events.put(('errorlog',f'録音ファイル未生成: exit={rc} / '+hint))
                    raise RuntimeError('録音を開始できませんでした。'
                        f' 録音プロセス終了コード={rc}。詳細：{hint}。'
                        '「入力テスト」でデバイスを検査し、Windowsのマイク許可と既定デバイスを確認してください。')
                # The queue is updated by the Tk thread.  Its event precedes
                # any final ASR and survives even when ASR is disabled or the
                # user stops processing.  Temporary transcription-only audio
                # must never be retained in the queue.
                actual_recording=actual_recording.resolve()
                if opts.keep_audio:
                    will_transcribe=(opts.needs_final_transcription and opts.needs_transcription and
                                     not self.live_cancel.is_set() and not self.stop_event.is_set())
                    self.events.put(('capture-queue',actual_recording,'処理中' if will_transcribe else '録音済み'))
                    if opts.realtime_transcription and preview.is_file() and not opts.needs_final_transcription:
                        self.events.put(('transcript-saved',str(actual_recording),[str(preview)],'ライブ完了'))
                if rc != 0:
                    self.events.put(('log','録音デバイスが正常終了しませんでした。'
                                               '保存できた録音は保持します。\n'))
                if opts.needs_final_transcription and opts.needs_transcription and not self.live_cancel.is_set() and not self.stop_event.is_set():
                    if is_silent_pcm(actual_recording):
                        silent_final=True
                        if opts.keep_audio:self.events.put(('job',str(actual_recording),'スキップ'))
                        self.events.put(('log','[最終認識] 全体が無音PCMのため認識を省略します。録音ファイルは保存します。\n'))
                    else:
                        if not opts.realtime_transcription:
                            assert transcriber is not None
                            device=self._resolve_device(cfg,transcriber)
                        self.events.put(('status','録音全体から最終版の文字起こしを作成中…'))
                        target_cfg=replace(cfg,output_dir=str(transcript_folder),output_same_as_input=False)
                        dest=choose_output(actual_recording,target_cfg,transcribed_at=started)
                        self.events.put(('new-file',actual_recording.name,1,1))
                        result,info=self._run_one(actual_recording,dest,target_cfg,
                                           transcriber,token,device,transcribed_at=started)
                        if opts.keep_audio:self.events.put(('job',str(actual_recording),result))
                        self.events.put(('log',f'最終版: [{result}] {info}\n'))
                        if result!='完了':
                            self.events.put(('log','部分文字起こしは .live.txt に残っています。\n'))
                if self.stop_event.is_set():
                    if opts.keep_audio:self.events.put(('job',str(actual_recording),'中止'))
                    self.events.put(('done','処理を中止しました。保存済みの録音は保持します。'))
                    return
                if opts.keep_audio and opts.recording_format != 'wav':
                    encoded=actual_recording.with_suffix('.'+opts.recording_format)
                    if encoded.exists():
                        # Never replace an existing media file, even if the WAV path is unique.
                        encoded=unique_media_path(encoded)
                    try:
                        ffmpeg=media_binary('ffmpeg',capture_python)
                        if not ffmpeg:
                            raise RuntimeError('FFmpegが見つかりません。録音環境をセットアップし直してください。')
                        self.events.put(('status','録音済みWAVを'+opts.recording_format.upper()+'へ変換中…'))
                        encoded=encode_live_recording(actual_recording,encoded,ffmpeg=ffmpeg,
                              fmt=opts.recording_format,bitrate=opts.recording_bitrate,
                              pcm_bits=opts.recording_pcm_bits,channels=opts.recording_channels,
                              sample_rate=opts.recording_sample_rate,cancel=self.stop_event,registry=getattr(self,'processes',None))
                        actual_recording.unlink()  # delete original PCM only after committed output
                        self.events.put(('capture-moved',actual_recording,encoded))
                        actual_recording=encoded
                        self.events.put(('log',f'[音声保存] {opts.recording_format.upper()} → {encoded}\n'))
                    except Exception as exc:
                        self.events.put(('errorlog','録音形式への変換失敗: '+traceback.format_exc()))
                        self.events.put(('log',f'[注意] {opts.recording_format.upper()}への変換失敗。'
                                         f' 元のWAVは残しています: {actual_recording} / {exc}\n'))
                elif opts.keep_audio and opts.recording_format == 'wav':
                    # Upgrade PCM only when requested WAV settings differ from capture.
                    try:
                        with wave.open(str(actual_recording),'rb') as recorded:
                            needs_conversion=(opts.recording_pcm_bits != recorded.getsampwidth()*8
                                 or opts.recording_sample_rate != recorded.getframerate()
                                 or opts.recording_channels != recorded.getnchannels())
                        if not needs_conversion:
                            self.events.put(('log',f'[音声保存] 収録PCM WAVを保持: {actual_recording}\n'))
                            # No codec conversion required.
                        else:
                            converted=self._encode_pcm_wav(actual_recording,opts,capture_python,started)
                            self.events.put(('capture-moved',actual_recording,converted))
                            actual_recording=converted
                    except Exception as exc:
                        self.events.put(('errorlog','PCM WAV変換失敗: '+traceback.format_exc()))
                        self.events.put(('log',f'[注意] PCM WAV変換失敗。元のWAVを保護: {actual_recording} / {exc}\n'))
                if opts.mode=='transcribe':
                    self.events.put(('log','文字起こしのみモードの一時音声を消去します。\n'))
                if opts.keep_audio and rc!=0:
                    self.events.put(('job',str(actual_recording),'失敗'))
                self.events.put(('done',('録音を保存しました。全体が無音のため認識は省略しました。' if silent_final else
                                        '録音・文字起こしを終了しました。') if rc==0 else
                                          '録音がエラー終了しました。ログと保存済み音声を確認してください。'))
        except Exception as exc:
            try:
                if opts.keep_audio and actual_recording is not None and actual_recording.is_file() and actual_recording.stat().st_size>44:
                    self.events.put(('capture-queue',actual_recording,'失敗'))
            except OSError:
                pass  # Preserve the original diagnostic even if stat now fails.
            details='ライブ録音エラー: '+redact(str(exc),token)+'\n'+redact(traceback.format_exc(),token)
            self.events.put(('errorlog',details))
            self.events.put(('log',details+'\n'))
            self.events.put(('done','ライブ録音エラー: '+redact(str(exc),token)[:170]+
                             '。診断ログで詳細を確認できます。'))
        finally:
            self.live_stop_file=None
            self.live_stopping=False
            self.live_active=False

    def _encode_pcm_wav(self,actual_recording:Path,opts:LiveOptions,
                        capture_python:Path,started:datetime) -> Path:
        ffmpeg=media_binary('ffmpeg',capture_python)
        if not ffmpeg:
            raise RuntimeError('FFmpegが見つかりません。録音環境を再セットアップしてください。')
        replacement=unique_media_path(actual_recording.with_name(actual_recording.stem+'_PCM.wav'))
        replacement=encode_live_recording(actual_recording,replacement,ffmpeg=ffmpeg,
            fmt='wav',bitrate=opts.recording_bitrate,pcm_bits=opts.recording_pcm_bits,
            channels=opts.recording_channels,sample_rate=opts.recording_sample_rate,cancel=self.stop_event,registry=getattr(self,'processes',None))
        actual_recording.unlink()
        self.events.put(('log',f'[音声保存] 設定済みPCM WAV → {replacement}\n'))
        return replacement

    def start_setup(self,key:str,hardware:str):
        if self.busy:return
        self._begin_work()
        self._set_busy(True, f'依存パッケージをセットアップ中: {PROFILE_NAMES[key]} / {hardware}')
        self.log(f'\n=== セットアップ: {PROFILE_NAMES[key]} / {hardware} ===\n')
        self.worker=threading.Thread(target=self._setup_worker,args=(key,hardware),daemon=True)
        self.worker.start()

    def _setup_worker(self,key:str,hardware:str):
        try:
            # The GUI itself uses only standard library modules.
            args=[console_interpreter(),'-X','utf8','-u',str(CODE_DIR/'setup_backend.py'),
                  '--profile',key,'--hardware',hardware]
            flags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
            with owned_process(self,args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                  text=True,encoding='utf-8',errors='replace',
                                  cwd=str(BASE),creationflags=flags,bufsize=1,
                                  env=utf8_child_env()) as proc:
                with self.process_lock:self.active_process=proc
                if hasattr(self,'cpu_limit'):
                    try:
                        cap_state=self.cpu_limit.attach(proc.pid,self.config.cpu_limit_percent)
                        if self.config.cpu_limit_percent is not None:
                            self.events.put(('log','[CPU] '+cap_state+'\n'))
                    except OSError as exc:
                        self.events.put(('log',f'[CPU] 上限制御の適用失敗: {exc}\n'))
                assert proc.stdout is not None
                for line in proc.stdout:
                    update = parse_setup_line(line)
                    if update is not None:
                        self.events.put(('engine-progress', update))
                    if not line.strip().startswith(SETUP_PROGRESS_PREFIX):
                        self.events.put(('log',line))
                    if self.stop_event.is_set() and proc.poll() is None:self._kill(proc)
                rc=proc.wait()
            self.events.put(('log',f'セットアップ終了コード: {rc}\n'))
            self.events.put(('done','セットアップ完了。' if rc==0 else 'セットアップに失敗しました。ログを確認してください。'))
        except (OSError,subprocess.SubprocessError) as exc:
            self.events.put(('log',f'セットアップ起動失敗: {exc}\n'))
            self.events.put(('done','セットアップを起動できませんでした。'))
        finally:
            with self.process_lock:self.active_process=None

    def start_bulk_setup(self, plan: BulkPlan, token: str = '', *, with_audio:bool=True):
        if self.busy:
            return
        plan.validate()
        self._begin_work()
        self.watch_mode=False
        self._set_busy(True, '環境とモデルを一括セットアップ中…')
        self.log('\n=== 一括セットアップ開始 ===\n' + planned_summary(plan) + '\n')
        self.worker=threading.Thread(target=self._bulk_setup_worker,
                                     args=(plan, token, with_audio), daemon=True)
        self.worker.start()

    def _bulk_setup_worker(self, plan: BulkPlan, token: str, with_audio:bool=True):
        try:
            args=[console_interpreter(),'-X','utf8','-u',str(CODE_DIR/'bulk_setup.py')]
            for profile, device in plan.profiles:
                args += ['--profile',f'{profile}:{device}']
            for backend, model in plan.models:
                args += ['--model',f'{backend}:{model}']
            if not plan.models:
                args.append('--skip-models')
            if not plan.pyannote:
                args.append('--no-pyannote-model')
            if with_audio:
                args.append('--with-audio')
            env=utf8_child_env()
            if token:
                env['HF_TOKEN']=token
            flags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
            with owned_process(self,args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                  text=True,encoding='utf-8',errors='replace',
                                  cwd=str(BASE),creationflags=flags,bufsize=1,
                                  env=env) as proc:
                with self.process_lock:self.active_process=proc
                if hasattr(self,'cpu_limit'):
                    try:
                        cap_state=self.cpu_limit.attach(proc.pid,self.config.cpu_limit_percent)
                        if self.config.cpu_limit_percent is not None:
                            self.events.put(('log','[CPU] '+cap_state+'\n'))
                    except OSError as exc:
                        self.events.put(('log',f'[CPU] 上限制御の適用失敗: {exc}\n'))
                assert proc.stdout is not None
                for line in proc.stdout:
                    safe_line=redact(line,token)
                    update = parse_setup_line(safe_line)
                    if update is not None:
                        self.events.put(('engine-progress', update))
                    if not safe_line.strip().startswith(SETUP_PROGRESS_PREFIX):
                        self.events.put(('log',safe_line))
                    if self.stop_event.is_set() and proc.poll() is None:
                        self._kill(proc)
                rc=proc.wait()
            if with_audio and rc == 0 and not self.stop_event.is_set():
                self.events.put(('capture-ready',))
            self.events.put(('done', '一括セットアップを中止しました。' if self.stop_event.is_set()
                             else '一括セットアップ完了。' if rc==0
                             else '一部セットアップに失敗しました。ログを確認してください。'))
        except (OSError,subprocess.SubprocessError) as exc:
            self.events.put(('log','一括セットアップ起動失敗: '+redact(str(exc),token)+'\n'))
            self.events.put(('done','一括セットアップを起動できませんでした。'))
        finally:
            with self.process_lock:self.active_process=None

    def start_watch(self,folder:Path):
        if self.busy:
            show_error(self.root,'ほかの処理が進行しています。');return
        cfg=replace(self.config,python_overrides=dict(self.config.python_overrides))
        python=self._require_environment(cfg)
        if python is None:return
        try:
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'processed').mkdir(exist_ok=True)
            (folder/'failed').mkdir(exist_ok=True)
        except OSError as exc:
            show_error(self.root,str(exc));return
        self._begin_work()
        self.collision_batch_choice=None
        self.watch_mode=True
        self._set_busy(True,f'監視中: {folder}')
        self.log(f'\n=== フォルダー監視開始: {folder} ===\n')
        self.worker=threading.Thread(target=self._watch_worker,
               args=(folder,cfg,python,self.hf_token),daemon=True)
        self.worker.start()

    @staticmethod
    def _collision_safe(folder:Path,name:str) -> Path:
        candidate=folder/name
        if not candidate.exists():return candidate
        source=Path(name)
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
        for i in range(1,10000):
            candidate=folder/f'{source.stem}_{stamp}_{i}{source.suffix}'
            if not candidate.exists():return candidate
        raise RuntimeError('移動先のファイル名を確保できません。')

    @staticmethod
    def _unique_processed_pair(folder: Path, source: Path, cfg: Config,
                               started: datetime) -> tuple[Path, Path]:
        """Find available media/transcript names; handle optional output suffixes."""
        stamp=started.strftime('%Y%m%d_%H%M%S')
        for i in range(10000):
            stem = source.stem if i == 0 else f'{source.stem}_{stamp}_{i}'
            media=folder/(stem+source.suffix)
            text=choose_output(media, cfg, watch_processed=folder, transcribed_at=started,
                               media_created_at=getattr(cfg, '_watch_creation', None))
            other=text.with_suffix('.csv')
            if not media.exists() and not text.exists() and (cfg.output_mode=='txt' or not other.exists()):
                return media,text
        raise RuntimeError('成功ファイルの移動先を確保できません。')

    def _watch_worker(self,folder:Path,cfg:Config,python:Path,token:str):
        processed=folder/'processed'
        failed=folder/'failed'
        observed:dict[str,tuple[int,int]]={}
        partial_deferred:dict[str,tuple[int,int]]={}
        try:
            device=self._resolve_device(cfg,python)
            self.events.put(('log',f'監視方式: {PROFILE_NAMES[cfg.profile]} / デバイス: {device}\n'))
            while not self.stop_event.is_set():
                paths=sorted((p for p in folder.iterdir() if supported_file(p)),key=lambda p:p.name.lower())
                live={str(p) for p in paths}
                for old in list(observed):
                    if old not in live:observed.pop(old,None)
                for old in list(partial_deferred):
                    if old not in live:partial_deferred.pop(old,None)
                for source in paths:
                    if self.stop_event.is_set():break
                    try:
                        source_stat=source.stat()
                        size=source_stat.st_size
                    except (OSError,PermissionError):continue
                    fingerprint=(size,source_stat.st_mtime_ns)
                    if partial_deferred.get(str(source))==fingerprint:
                        continue
                    partial_deferred.pop(str(source),None)
                    previous=observed.get(str(source))
                    stable=(previous[1]+1 if previous and previous[0]==size and size>0 else 0)
                    observed[str(source)]=(size,stable)
                    if stable<2:continue # twice unchanged across polling cycles
                    observed.pop(str(source),None)
                    self.events.put(('watch-file',source))
                    self.events.put(('status',f'監視処理中: {source.name}'))
                    # The original watch implementation moves successful media and TXT.
                    if cfg.filename_media_creation:
                        cfg._watch_creation=probe_metadata(source,python,cancel=self.stop_event,registry=getattr(self,'processes',None)).created
                    new_media,dest=self._unique_processed_pair(processed,source,cfg,datetime.now())
                    result,info=self._run_one(source,dest,cfg,python,token,device)
                    self.events.put(('job',str(source),job_status_label(result,info)))
                    self.events.put(('log',f'[{result}] {info}\n'))
                    if result=='完了':
                        try:shutil.move(str(source),str(new_media))
                        except OSError as exc:self.events.put(('log',f'元ファイルの移動失敗: {exc}\n'))
                    elif result=='一部完了':
                        try:
                            source.stat()  # Never suppress a replacement observed during processing.
                            partial_deferred[str(source)]=fingerprint
                            self.events.put(('log','[注意] 一部完了の元ファイルは保持し、変更されるまで同じ監視中の再処理を止めます。\n'))
                        except OSError as exc:
                            self.events.put(('log',f'元ファイルの確認に失敗: {exc}\n'))
                    elif result=='失敗' and not self.stop_event.is_set():
                        try:
                            error_source=self._collision_safe(failed,source.name)
                            shutil.move(str(source),str(error_source))
                            stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
                            error_log=log_directory()/f'watch_{source.stem[:80]}_{stamp}_{uuid.uuid4().hex[:6]}.error.log'
                            with error_log.open('x',encoding='utf-8',newline='\n') as stream:
                                stream.write(f'失敗日時: {datetime.now():%Y-%m-%d %H:%M:%S}\n'
                                    f'対象: {error_source}\n{redact(info,token)}\n')
                            self.events.put(('log',f'[監視エラーログ] {error_log}\n'))
                        except OSError as exc:self.events.put(('log',f'失敗ファイル移動エラー: {exc}\n'))
                self.stop_event.wait(3.0)
        except (OSError,RuntimeError) as exc:
            self.events.put(('log',f'監視エラー: {redact(str(exc),token)}\n'))
        finally:
            self.events.put(('done','フォルダー監視を停止しました。'))

    def _poll(self):
        if self.closing:return
        queue_changed = False
        callback_queue=getattr(self,'ui_callbacks',None)
        if callback_queue is not None:
            for _ in range(128):
                try:
                    widget,callback=callback_queue.get_nowait()
                except queue.Empty:
                    break
                dispatch_ui_callback(widget,callback,self.diagnostics)
        try:
            for _ in range(250):
                item=self.events.get_nowait()
                if item[0]=='errorlog':self.diagnostics.error(item[1])
                elif item[0]=='log':self.log(item[1])
                elif item[0]=='status':self.status.set(item[1])
                elif item[0]=='buttontext':self.stop_btn.configure(text=item[1],state='normal' if self.busy else 'disabled')
                elif item[0]=='new-file':
                    self._reset_recognition_preview()
                    self.status.set(f'処理中 {item[2]}/{item[3]}: {item[1]}')
                elif item[0]=='engine-progress':self._show_progress(item[1])
                elif item[0]=='job':
                    _,key,result=item
                    if self.tree.exists(key):
                        row=list(self.tree.item(key,'values'))
                        row[2]=result
                        self.tree.item(key,values=row)
                        queue_changed = True
                elif item[0]=='capture-ready':
                    self.config.capture_setup_complete = True
                    self.capture_verified = True
                    try:
                        save_config(self.config)
                    except OSError as exc:
                        self.log(f'セットアップ状態の保存に失敗しました: {exc}\n')
                elif item[0]=='shortcut':
                    self._handle_shortcut(item[1])
                elif item[0]=='hotkey-error':
                    self.log('[ショートカット] '+item[1]+'\n')
                    if self.in_tray:
                        self.restore_from_tray()
                        messagebox.showwarning('ショートカットを登録できません',item[1],parent=self.root)
                elif item[0]=='tray':
                    self._tray_action(item[1])
                elif item[0]=='transcript-saved':
                    _, key, names, result = item
                    paths = [Path(name) for name in names]
                    self.job_outputs[key] = paths
                    if self.tree.exists(key):
                        row = list(self.tree.item(key, 'values'))
                        row[2] = result
                        row[6] = str(paths[0].parent) if paths else row[6]
                        self.tree.item(key, values=row)
                        queue_changed = True
                    if self.config.auto_open_speaker and paths and not self.stop_event.is_set():
                        target = next((p for p in paths if p.suffix.lower() == '.txt'), paths[0])
                        if target not in self.pending_speaker_edits:
                            self.pending_speaker_edits.append(target)
                elif item[0]=='collision':
                    _,source,targets,reply,result=item
                    try:
                        if self.stop_event.is_set():
                            result['choice']='cancel';reply.set();continue
                        if self.collision_batch_choice:
                            result['choice']=self.collision_batch_choice
                        else:
                            self.restore_from_tray()
                            dialog=modal(self.root,'既存の文字起こし出力を検出',660)
                            panel=ttk.Frame(dialog,padding=20);panel.pack(fill='both',expand=True)
                            ttk.Label(panel,text=f'{source.name} の指定された出力名が既に存在します。',
                                      font=(UI_FONT,12,'bold'),wraplength=600).pack(anchor='w',pady=(0,10))
                            ttk.Label(panel,text='\n'.join(str(path) for path in targets),wraplength=605).pack(anchor='w',pady=6)
                            ttk.Label(panel,text='「以後すべてスキップ」は、この一括処理中の今後の重複確認を省略します。\n'
                                      'リネーム保存は既存のTXT・CSVを残し、新しい名前で保存します。',
                                      wraplength=605,foreground='#596477').pack(anchor='w',pady=14)
                            def select(choice):
                                result['choice']=choice
                                if choice=='skip_all':
                                    self.collision_batch_choice='skip'
                                    result['choice']='skip'
                                dialog.destroy()
                                reply.set()
                            buttons=ttk.Frame(panel);buttons.pack(fill='x',pady=12)
                            for title,choice in [('スキップ','skip'),('以後すべてスキップ','skip_all'),
                                                 ('上書き','overwrite'),('リネーム保存','rename'),
                                                 ('処理を中断','cancel')]:
                                IconButton(buttons,text=title,command=lambda c=choice:select(c)).pack(side='left',padx=3)
                            dialog.protocol('WM_DELETE_WINDOW',lambda:select('cancel'))
                    except Exception:
                        result['choice']='cancel';reply.set()
                        self.diagnostics.error('出力の重複確認に失敗:\n'+traceback.format_exc())
                elif item[0]=='capture-queue':
                    self._recorded_queue_item(item[1],item[2])
                    queue_changed = True
                elif item[0]=='capture-moved':
                    self._move_recorded_queue_item(item[1],item[2])
                    queue_changed = True
                elif item[0]=='watch-file':
                    source=item[1]
                    if str(source) not in self.job_sources:self._add_paths([source])
                elif item[0]=='done':
                    if ((self.worker is not None and self.worker.is_alive()) or
                            (getattr(self,'processes',None) is not None and self.processes.active())):
                        self.root.after(100,lambda done=item:self.events.put(done));continue
                    if getattr(self,'all_stop_requested',False):
                        self.log(item[1]+'\n')
                        item=('done','すべての処理を停止しました。保存済みのファイルは保持しています。')
                    self.all_stop_waiting=False
                    if '失敗' in item[1] or 'エラー' in item[1]:self.diagnostics.error(item[1])
                    self._set_busy(False,item[1])
                    self.update_profile_display()
                    self.status.set(item[1])
                    target = self.pending_capture_target
                    self.pending_capture_target = None
                    if target and self.capture_verified and not self.quit_when_idle:
                        self.root.after_idle(lambda key=target: self.open_capture(key,force_settings=True))
                    if self.pending_speaker_edits and not self.quit_when_idle:
                        self.restore_from_tray()
                        self.root.after_idle(self._open_next_speaker)
                    if self.quit_when_idle:
                        self._finish_close()
                        return
        except queue.Empty:pass
        if queue_changed:
            self._refresh_queue_display()
        active=self.busy or bool(self.processes.active())
        self.ui.stop_all_button.configure(state='normal' if active and not getattr(self,'all_stop_requested',False) else 'disabled')
        if not active and getattr(self,'all_stop_waiting',False):
            self.all_stop_waiting=False
            self.status.set('すべての処理を停止しました。保存済みのファイルは保持しています。')
            if self.quit_when_idle:
                self._finish_close();return
        self.root.after(100,self._poll)

    def close(self):
        if self.config.tray_close and self.hide_to_tray():
            return
        if self.config.tray_close:
            return  # An unavailable notification area must not silently exit.
        self.request_exit()

    def request_exit(self):
        if self.busy or self.processes.active():
            if not messagebox.askyesno('終了確認','実行中の処理を停止してアプリを終了しますか？',parent=self.root):
                return
            # Wait for the worker to terminate the child process before destroying Tk.
            self.quit_when_idle=True
            self.stop_all()
            self.status.set('実行プロセスの終了を待っています…')
            return
        self._finish_close()

    def _finish_close(self):
        self.closing=True
        if hasattr(self,'updates'):self.updates.close()
        if hasattr(self,'hotkeys'):self.hotkeys.close()
        if hasattr(self,'tray'):self.tray.close()
        self.cpu_limit.close()
        self.resource_control_file.unlink(missing_ok=True)
        self.root.destroy()


def main() -> None:
    # The queue, capture state and configuration are owned by this GUI process.
    # A repeat launch returns to it instead of racing its hotkeys and outputs.
    from single_instance import enter_instance
    guard = enter_instance()
    if guard is None:
        return
    try:
        if TkinterDnD is None:
            root = tk.Tk()
        else:
            try:
                root = TkinterDnD.Tk()
            except (tk.TclError, RuntimeError, OSError) as exc:
                root = tk.Tk()  # file-picker fallback, with visible reason
                root._dnd_startup_error = str(exc)
        guard.attach(root)
        app = UnifiedApp(root)
        guard.set_restore(app.restore_from_tray)
        from update_ui import UpdateController
        app.updates = UpdateController(app)
        app.updates.start()
        if '--background' in sys.argv[1:]:
            root.after_idle(app.hide_to_tray)
        root.after(250, app.offer_initial_setup)
        root.mainloop()
    finally:
        guard.close()


if __name__=='__main__':main()

