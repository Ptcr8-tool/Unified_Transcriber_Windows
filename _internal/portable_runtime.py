"""Application-local Python, settings and optional setup orchestration.

The packaged launcher calls this module before importing optional libraries.
Source checkouts keep their existing settings/cache locations unless they have a
valid installer-created portable marker.
"""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
from typing import Callable, Mapping

APP_ID = 'UnifiedTranscriberWindows'
MARKER_FORMAT = 'unified-transcriber-portable'
MAX_CONFIG_BYTES = 2 * 1024 * 1024


def is_portable(target: Path) -> bool:
    marker = Path(target) / 'portable.json'
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            return False
        value = json.loads(marker.read_text(encoding='utf-8'))
        return (isinstance(value, dict) and value.get('format') == MARKER_FORMAT
                and type(value.get('version')) is int and value['version'] == 1
                and value.get('app_id') == APP_ID
                and value.get('settings_directory') == 'Data'
                and isinstance(value.get('install_id'), str)
                and re.fullmatch(r'[0-9a-f]{32}', value['install_id']) is not None)
    except (OSError, UnicodeError, ValueError):
        return False


def runtime_environment(target: Path, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep credentials in memory, remove interpreter/pip configuration injection.

    Python's trusted script directory remains importable; the current working
    directory is never added explicitly. No process-global environment changes
    occur here.
    """
    target = Path(target).resolve()
    original = os.environ if inherited is None else inherited
    env = {key: value for key, value in original.items()
           if not key.upper().startswith(('PYTHON', 'PIP_', '_PYI_'))
           and key.upper() not in ('VIRTUAL_ENV', 'PY_PYTHON', 'PY_PYTHON3',
                                   'TCL_LIBRARY', 'TK_LIBRARY', 'TCLLIBPATH', '_MEIPASS2')}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONUNBUFFERED='1',
               PYTHONNOUSERSITE='1', PIP_CONFIG_FILE=os.devnull)
    if is_portable(target):
        models = target / 'Models'
        env.update(HF_HOME=str(models / 'huggingface'),
                   HF_HUB_CACHE=str(models / 'huggingface' / 'hub'),
                   HUGGINGFACE_HUB_CACHE=str(models / 'huggingface' / 'hub'),
                   TRANSFORMERS_CACHE=str(models / 'huggingface' / 'hub'),
                   TORCH_HOME=str(models / 'torch'),
                   XDG_CACHE_HOME=str(models / 'cache'),
                   PIP_CACHE_DIR=str(target / 'Data' / 'pip-cache'))
        runtime = target / 'Runtime'
        if os.name == 'nt' and runtime.is_dir():
            # Venv redirectors and native dependencies must find the bundled
            # Microsoft CRT without a machine-wide redistributable install.
            # Use the application path, never the current working directory.
            prefix = str(runtime)
            normalized = ntpath.normcase(ntpath.normpath(prefix))
            path_key = next((key for key in env if key.upper() == 'PATH'), 'PATH')
            entries = []
            for entry in env.get(path_key, '').split(';'):
                candidate = entry.strip().strip('"')
                identity = ntpath.normcase(ntpath.normpath(candidate))
                if not candidate or identity in ('.', normalized):
                    continue
                # Windows may spell the same installed directory using its
                # 8.3 alias. Only compare absolute paths; never resolve a PATH
                # entry against the launcher's current working directory.
                try:
                    duplicate = Path(candidate).is_absolute() and Path(candidate).samefile(runtime)
                except (OSError, ValueError):
                    duplicate = False
                if not duplicate:
                    entries.append(entry)
            env[path_key] = ';'.join([prefix, *entries])
    return env


def reset_dll_search_path() -> None:
    """Undo PyInstaller's DLL directory before starting an independent runtime."""
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel32.SetDllDirectoryW.restype = ctypes.c_int
        if not kernel32.SetDllDirectoryW(None):
            raise OSError(ctypes.get_last_error(), '外部プログラムのDLL検索先を初期化できません。')


def configure_portable_environment(target: Path) -> None:
    """Install local model/cache locations before model libraries are imported."""
    if not is_portable(target):
        return
    env = runtime_environment(target)
    for key in ('HF_HOME', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE', 'TRANSFORMERS_CACHE',
                'TORCH_HOME', 'XDG_CACHE_HOME', 'PIP_CACHE_DIR'):
        os.environ[key] = env[key]


def setup_commands(target: Path, *, recording: bool, minimal: bool,
                   hardware: str = 'cpu', model: str = 'large-v3-turbo') -> list[list[str]]:
    """Plan the selected recording environment and/or profile ② and its model."""
    from bulk_catalog import MODEL_CHOICES
    if type(recording) is not bool or type(minimal) is not bool or hardware not in ('cpu', 'cuda'):
        raise ValueError('セットアップの選択を確認してください。')
    if not isinstance(model, str) or model not in MODEL_CHOICES['faster']:
        raise ValueError('未対応の音声認識モデルです。')
    target = Path(target).resolve()
    python = str(target / 'Runtime' / 'python.exe')
    code = target / '_internal'
    if minimal:
        command = [python, '-X', 'utf8', '-u', str(code / 'bulk_setup.py'),
                   '--profile', f'faster-res:{hardware}', '--model', f'faster:{model}',
                   '--no-pyannote-model']
        if recording:
            command.append('--with-audio')
        return [command]
    if recording:
        return [[python, '-X', 'utf8', '-u', str(code / 'setup_live_capture.py')]]
    return []


def _stop_setup(process: subprocess.Popen) -> None:
    """Stop this setup's process tree without targeting unrelated Python jobs."""
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
            subprocess.run([str(system_root / 'System32' / 'taskkill.exe'),
                            '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
                           shell=False)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
    except (OSError, subprocess.SubprocessError):
        if process.poll() is None:
            process.kill()
    process.wait(timeout=10)


def setup_selected(target: Path, *, recording: bool = True, minimal: bool = False,
                   hardware: str = 'cpu', model: str = 'large-v3-turbo',
                   progress: Callable[[str], None] | None = None,
                   cancel: threading.Event | None = None, timeout: float = 3600) -> None:
    """Run selected setup from a worker thread; cancellation also handles silence.

    ``progress`` is called on the calling worker thread, never on a Tk thread.
    It should enqueue messages for the UI. A failed optional setup leaves the
    installed application and any completed downloads available for retry.
    """
    target = Path(target).resolve()
    commands = setup_commands(target, recording=recording, minimal=minimal, hardware=hardware, model=model)
    if not commands:
        return
    if not is_portable(target) or not (target / 'Runtime' / 'python.exe').is_file():
        raise RuntimeError('同梱のPython環境を確認できません。セットアップで修復してください。')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise ValueError('セットアップの待機時間が不正です。')
    notify = progress or (lambda _line: None)
    env = runtime_environment(target)
    token = env.get('HF_TOKEN', '')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    reset_dll_search_path()
    for command in commands:
        if cancel is not None and cancel.is_set():
            raise InterruptedError('セットアップを中止しました。')
        lines: queue.Queue[str] = queue.Queue(maxsize=1000)
        finished = threading.Event()
        process = subprocess.Popen(command, cwd=str(target), env=env, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', creationflags=flags,
            start_new_session=os.name != 'nt')

        def read_output() -> None:
            try:
                assert process.stdout is not None
                while not finished.is_set():
                    line = process.stdout.readline(65536)
                    if not line:
                        return
                    # Do not let an interrupted UI strand a reader on a full queue.
                    while not finished.is_set():
                        try:
                            lines.put(line, timeout=.1)
                            break
                        except queue.Full:
                            pass
            finally:
                finished.set()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None or not lines.empty() or not finished.is_set():
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('セットアップを中止しました。')
                if time.monotonic() - started > timeout:
                    raise TimeoutError('セットアップに時間がかかりすぎたため中止しました。あとから再実行できます。')
                try:
                    line = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                notify(line.replace(token, '[HF_TOKEN非表示]') if token else line)
            result = process.wait()
            if result != 0:
                raise RuntimeError(f'追加環境のセットアップに失敗しました (終了コード {result})。'
                    '直前の詳細ログを確認してください。アプリ内のセットアップから再実行できます。')
        except BaseException:
            _stop_setup(process)
            raise
        finally:
            finished.set()
            if process.stdout is not None:
                process.stdout.close()
            reader.join(timeout=2)
