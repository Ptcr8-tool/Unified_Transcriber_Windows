"""Common helpers for media extraction and safe transcript publication."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path


def read_pcm_for_whisper(path: Path, *, sample_rate: int = 16000):
    """Read our FFmpeg-produced PCM without depending on PyAV's API version.

    Faster-whisper accepts float32 arrays as well as file paths. Processing
    blocks bounds temporary memory to a few MiB even for long recordings.
    """
    import numpy as np
    try:
        with wave.open(str(path), 'rb') as source:
            if (source.getnchannels() != 1 or source.getsampwidth() != 2
                    or source.getframerate() != sample_rate
                    or source.getcomptype() != 'NONE'):
                raise RuntimeError('文字起こし用の中間音声が想定外の形式です。音声の変換をやり直してください。')
            total = source.getnframes()
            try:
                audio = np.empty(total, dtype=np.float32)
            except (MemoryError, ValueError) as exc:
                raise RuntimeError('音声を読み込むメモリが不足しています。録音を短いファイルに分けて再試行してください。') from exc
            position = 0
            while position < total:
                raw = source.readframes(min(262144, total - position))
                if not raw or len(raw) % 2:
                    raise RuntimeError('文字起こし用の中間音声が途中で途切れています。音声の変換をやり直してください。')
                samples = np.frombuffer(raw, dtype='<i2')
                end = position + samples.size
                if end > total:
                    raise RuntimeError('文字起こし用の中間音声が不正です。音声の変換をやり直してください。')
                audio[position:end] = samples
                audio[position:end] *= np.float32(1.0 / 32768.0)
                position = end
            return audio
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError('文字起こし用の中間音声を読み込めません。音声の変換をやり直してください。') from exc


def extract_pcm_audio(ffmpeg: str, source: Path, destination: Path, *,
                      sample_rate: int = 16000, stall_seconds: float = 600.0) -> None:
    """Decode audio to WAV; stop a stalled process without limiting media length.

    FFmpeg's numeric output timestamp is monitored, rather than its periodic
    heartbeat: a child that keeps printing the same timestamp is still stuck.
    Both pipes are drained concurrently and stderr memory is bounded.
    """
    if stall_seconds <= 0:
        raise ValueError('stall_seconds must be positive')
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostats',
               '-stats_period', '5', '-progress', 'pipe:1', '-y', '-i', str(source),
               '-vn', '-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le',
               str(destination)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, shell=False)
    activity = {'at': time.monotonic(), 'timestamp': -1}
    activity_lock = threading.Lock()
    errors = bytearray()

    def read_progress():
        try:
            for raw in iter(process.stdout.readline, b''):
                key, separator, value = raw.strip().partition(b'=')
                if not separator or key not in (b'out_time_us', b'out_time_ms'):
                    continue
                try:
                    timestamp = int(value)
                except ValueError:
                    continue
                with activity_lock:
                    if timestamp > activity['timestamp']:
                        activity['timestamp'] = timestamp
                        activity['at'] = time.monotonic()
        finally:
            process.stdout.close()

    def read_errors():
        try:
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                errors.extend(chunk)
                if len(errors) > 16384:
                    del errors[:-16384]
        finally:
            process.stderr.close()

    workers = [threading.Thread(target=read_progress, daemon=True),
               threading.Thread(target=read_errors, daemon=True)]
    for worker in workers:
        worker.start()
    stalled = False
    try:
        while True:
            try:
                code = process.wait(timeout=min(1.0, stall_seconds))
                break
            except subprocess.TimeoutExpired:
                with activity_lock:
                    stalled = time.monotonic() - activity['at'] >= stall_seconds
                if stalled:
                    if process.poll() is not None:
                        stalled = False
                        code = process.wait()
                        break
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # The child may finish between the poll and the kill.
                        pass
                    code = process.wait()
                    if code == 0:
                        stalled = False
                    break
    except BaseException:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        for worker in workers:
            worker.join(timeout=3)
    detail = bytes(errors).decode('utf-8', errors='replace').strip()
    if stalled:
        raise RuntimeError(f'FFmpegによる音声抽出が{stall_seconds:g}秒間進まないため停止しました。\n{detail}')
    if code:
        raise RuntimeError(f'FFmpegで音声を取り出せませんでした（終了コード {code}）。\n{detail}')


def write_transcript_atomic(destination: Path, text: str, *, overwrite: bool = True) -> None:
    """Publish a fully written UTF-8 BOM text without destroying old output."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + destination.name + '.', suffix='.tmp', dir=destination.parent)
    stage = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='\n') as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            os.replace(stage, destination)
        elif os.name == 'nt':
            os.rename(stage, destination)  # Windows rename rejects an existing destination.
        else:
            os.link(stage, destination)  # atomic no-overwrite create on POSIX
            stage.unlink()
    finally:
        stage.unlink(missing_ok=True)
