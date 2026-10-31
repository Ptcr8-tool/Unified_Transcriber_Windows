"""Explicit Python selection and nested-process output for optional setup.

Windows GUI processes do not necessarily have usable native standard handles.
Every nested setup command receives its own pipes instead of relying on those
handles to propagate through ``CREATE_NO_WINDOW`` children.
"""
from __future__ import annotations

from collections import deque
import codecs
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Sequence

from portable_runtime import is_portable, runtime_environment
from process_control import kill_owned_process

# Large model downloads can remain quiet for a long time. Bound a genuine
# stall without imposing a total time limit on a download making progress.
DEFAULT_IDLE_TIMEOUT = 30 * 60


def setup_interpreter(target: Path, *, profile: str | None = None) -> str:
    """Use the shipped console interpreter; never discover Python on PATH."""
    target = Path(target)
    if is_portable(target):
        runtime = 'MLRuntime' if profile in ('faster-res', 'live-capture') else 'Runtime'
        python = target / runtime / 'python.exe'
        if not python.is_file():
            raise RuntimeError('同梱Pythonが不足しています。セットアップexeの「修復」を実行してください。')
        return str(python)
    python = Path(sys.executable)
    if os.name == 'nt' and python.name.casefold() == 'pythonw.exe':
        python = python.with_name('python.exe')
        if not python.is_file():
            raise RuntimeError('同じPython環境のpython.exeが見つかりません。')
    return str(python)


def run_logged(command: Sequence[str], target: Path, *, check: bool = True,
               idle_timeout: float = DEFAULT_IDLE_TIMEOUT) -> subprocess.CompletedProcess:
    """Stream merged UTF-8 output to the caller and retain a bounded error tail.

    No shell, process environment or token is printed. The outer GUI and setup
    wizard can capture this stream identically, even through multiple levels of
    subprocesses. Unlike ambient Win32 handle inheritance, explicit pipes also
    preserve errors produced by a detached descendant.
    """
    if idle_timeout <= 0:
        raise ValueError('無出力の監視時間は正の値にしてください。')
    args = [str(value) for value in command]
    env = runtime_environment(target)
    token = env.get('HF_TOKEN', '')
    tail: deque[str] = deque(maxlen=40)
    pending = ''

    def emit(line: str) -> None:
        tail.append(line[-4096:])
        print(line, end='', flush=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen(args, cwd=str(target), env=env, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=flags, bufsize=0)
    try:
        assert process.stdout is not None
        # A blocking readline in this thread cannot notice a silent child.
        # Read bytes in a daemon thread so incomplete lines also count as
        # progress; a bounded queue caps output kept in memory.
        chunks = queue.Queue(maxsize=32)
        def read_output():
            try:
                while True:
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        chunks.put(('eof', None))
                        return
                    chunks.put(('data', chunk))
            except (OSError, ValueError) as exc:
                chunks.put(('error', exc))
        threading.Thread(target=read_output, daemon=True, name='setup-output-reader').start()
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        last_output = time.monotonic()

        def consume(text: str, *, final: bool = False) -> None:
            nonlocal pending
            data = pending + text
            if not token or final:
                pending = ''
                emit(data.replace(token, '[HF_TOKEN非表示]') if token else data)
                return
            cutoff = max(0, len(data) - len(token) + 1)
            # A complete token can straddle the prospective boundary. Move
            # the boundary past it before redaction so no prefix leaks.
            start = data.find(token)
            while start >= 0 and start < cutoff:
                cutoff = max(cutoff, start + len(token))
                start = data.find(token, start + len(token))
            visible, pending = data[:cutoff], data[cutoff:]
            if visible:
                emit(visible.replace(token, '[HF_TOKEN非表示]'))

        while True:
            remaining = idle_timeout - (time.monotonic() - last_output)
            if remaining <= 0:
                raise TimeoutError(f'セットアップが{idle_timeout:g}秒間出力を返さなかったため中止しました。')
            try:
                kind, chunk = chunks.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if kind == 'error':
                raise OSError('セットアップの出力を読み取れませんでした。') from chunk
            if kind == 'eof':
                consume(decoder.decode(b'', final=True), final=True)
                break
            last_output = time.monotonic()
            consume(decoder.decode(chunk))
        remaining = max(0.0, idle_timeout - (time.monotonic() - last_output))
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f'セットアップが{idle_timeout:g}秒間出力を返さなかったため中止しました。') from exc
        result = subprocess.CompletedProcess(args, status, ''.join(tail), None)
    except BaseException:
        kill_owned_process(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, output=result.stdout)
    return result

