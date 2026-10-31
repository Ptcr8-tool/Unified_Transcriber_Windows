"""Standalone low-overhead audio capture process with Windows PortAudio/WASAPI loopback.

Runs inside the selected engine's Python 3.11 venv, not in the Tk GUI process.
Capture/WAV writing must never wait for ASR. A separate controller consumes the
atomically finalized segment WAVs. STOP is a file flag, not a forced process kill.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
import traceback
from contextlib import contextmanager, ExitStack
from pathlib import Path


def emit(kind: str, **payload) -> None:
    print(json.dumps({'event': kind, **payload}, ensure_ascii=False), flush=True)


class _PortAudioInput:
    """Stable PortAudio microphone descriptor; never imports SoundCard."""
    def __init__(self, index: int, info: dict):
        self.index = int(index)
        self.name = str(info['name'])
        self.info = info

    def recorder(self, *, samplerate, blocksize):
        import sounddevice as sd
        stream = sd.InputStream(device=self.index, samplerate=samplerate,
                                channels=1, dtype='float32', blocksize=blocksize)
        return _PortAudioRecorder(stream)


class _WASAPILoopbackInput:
    """Speaker loopback through PyAudioWPatch rather than SoundCard COM import."""
    def __init__(self, index: int, info: dict):
        self.index = int(index)
        self.name = str(info['name'])
        self.info = dict(info)

    def recorder(self, *, samplerate, blocksize):
        return _WPatchRecorder(self.index, int(samplerate), int(blocksize))


class _WPatchRecorder:
    def __init__(self, index, samplerate, blocksize):
        self.index = index
        self.rate = samplerate
        self.blocksize = blocksize
        self.backend = None
        self.stream = None
        self.channels = None
        self.audio = None

    def __enter__(self):
        import pyaudiowpatch as pa
        self.audio = pa.PyAudio()
        try:
            info = self.audio.get_device_info_by_index(self.index)
            if not info.get('isLoopbackDevice'):
                raise ValueError('指定デバイスはWASAPIループバックではありません。')
            channels = min(2, int(info.get('maxInputChannels') or 2))
            if channels <= 0:
                raise ValueError('ループバックデバイスに入力チャンネルがありません。')
            self.channels = channels
            self.backend = pa
            self.stream = self.audio.open(
                format=pa.paInt16, channels=channels, rate=self.rate, input=True,
                input_device_index=self.index, frames_per_buffer=self.blocksize)
        except Exception:
            self.audio.terminate()
            self.audio = None
            raise
        return self

    def __exit__(self, *_args):
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        finally:
            if self.audio is not None:
                self.audio.terminate()
            self.stream = None
            self.audio = None

    def record(self, numframes):
        import numpy as np
        data = self.stream.read(numframes, exception_on_overflow=False)
        if not data:
            return np.empty((0,self.channels), dtype=np.float32)
        samples = np.frombuffer(data, dtype='<i2')
        if samples.size % self.channels:
            raise RuntimeError('ループバック録音で不完全なPCMフレームを受信しました。')
        return (samples.reshape(-1,self.channels).astype(np.float32) / 32768.0)


def _wasapi_loopback_device(device_id: str = ''):
    import pyaudiowpatch as pa
    with pa.PyAudio() as backend:
        if device_id:
            if not device_id.startswith('wp:') or not device_id[3:].isdigit():
                raise ValueError('ループバックデバイスのIDが旧形式です。「デバイス更新」を押してください。')
            index = int(device_id[3:])
            info = backend.get_device_info_by_index(index)
            if not info.get('isLoopbackDevice'):
                raise ValueError('指定したループバックデバイスが利用できません。')
        else:
            info = backend.get_default_wasapi_loopback()
            index = int(info['index'])
        return _WASAPILoopbackInput(index, info)


def list_devices() -> list[dict[str, str]]:
    """Windows enumeration must work even if SoundCard's COM initializer fails."""
    if sys.platform != 'win32':
        # Preserve other OS/test compatibility without changing Windows selection.
        import soundcard as sc
        return [{'id': str(m.id), 'name': str(m.name),
                 'source': 'loopback' if m.isloopback else 'mic'}
                for m in sc.all_microphones(include_loopback=True)]
    devices = []
    failures = []
    try:
        import sounddevice as sd
        for index, info in enumerate(sd.query_devices()):
            if int(info.get('max_input_channels', 0)) > 0:
                devices.append({'id': f'pa:{index}', 'name': str(info['name']),
                                'source':'mic'})
    except Exception as exc:
        failures.append('マイク(PortAudio): ' + str(exc))
    try:
        import pyaudiowpatch as pa
        with pa.PyAudio() as backend:
            for info in backend.get_loopback_device_info_generator():
                devices.append({'id': f'wp:{int(info["index"])}',
                                'name': str(info['name']), 'source': 'loopback'})
    except Exception as exc:
        failures.append('PC再生音(PyAudioWPatch): ' + str(exc))
    if failures:
        emit('warning', message='一部デバイスの検索に失敗: ' + ' / '.join(failures))
    if not devices:
        raise RuntimeError('録音デバイスを列挙できませんでした: ' + ' / '.join(failures))
    return devices


def get_device(source: str, device_id: str):
    if sys.platform == 'win32':
        if source == 'loopback':
            return _wasapi_loopback_device(device_id)
        if source != 'mic':
            raise ValueError('録音元が無効です。')
        import sounddevice as sd
        entries = sd.query_devices()
        if device_id:
            if not device_id.startswith('pa:') or not device_id[3:].isdigit():
                raise ValueError('マイクIDが旧形式です。「デバイス更新」を押してください。')
            index = int(device_id[3:])
        else:
            index = sd.default.device[0]
        if index is None or int(index) < 0 or int(index) >= len(entries):
            raise RuntimeError('Windows既定のマイク入力がありません。')
        info = entries[int(index)]
        if int(info.get('max_input_channels',0)) < 1:
            raise ValueError('選択したデバイスはマイク入力に対応していません。')
        return _PortAudioInput(int(index), info)
    import soundcard as sc
    if device_id:
        devices = sc.all_microphones(include_loopback=True)
        for mic in devices:
            if str(mic.id) == device_id and bool(mic.isloopback) == (source == 'loopback'):
                return mic
        raise ValueError('指定した録音デバイスが接続されていません。')
    if source == 'mic':
        return sc.default_microphone()
    speaker = sc.default_speaker()
    if speaker is None:
        raise ValueError('既定のスピーカーが見つかりません。')
    return sc.get_microphone(id=str(speaker.id), include_loopback=True)


class RecordingWriter:
    """One contiguous PCM WAV + atomic, ordered recognition chunks."""
    def __init__(self, output: Path, *, sample_rate: int, channels: int,
                 chunk_dir: Path | None, chunk_seconds: float = 20):
        if sample_rate < 1 or channels not in (1, 2) or chunk_seconds <= 0:
            raise ValueError('録音設定が無効です。')
        self.output = Path(output)
        self.rate = sample_rate
        self.channels = channels
        self.chunk_dir = Path(chunk_dir) if chunk_dir else None
        self.chunk_frames = int(round(chunk_seconds * sample_rate))
        self.total_frames = 0
        self.segment_frames = 0
        self.segment_index = 0
        self.segment_bytes = bytearray()
        self.wav = None
        self.stream = None

    def __enter__(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        if self.output.exists():
            raise FileExistsError(f'録音の上書きを防止しました: {self.output}')
        # Use exclusive creation, so concurrent sessions cannot overwrite data.
        stream = self.output.open('xb')
        try:
            self.stream = stream
            self.wav = wave.open(stream, 'wb')
            self.wav.setnchannels(self.channels)
            self.wav.setsampwidth(2)
            self.wav.setframerate(self.rate)
        except Exception:
            stream.close()
            raise
        if self.chunk_dir is not None:
            self.chunk_dir.mkdir(parents=True, exist_ok=True)
        return self

    def feed(self, frame_array):
        import numpy as np
        data = np.asarray(frame_array)
        if data.ndim == 1:
            data = data[:, None]
        if data.ndim != 2:
            raise ValueError('録音デバイスから予期しない音声データが返りました。')
        if data.shape[0] == 0:
            return []
        if self.channels == 1:
            data = data.mean(axis=1, keepdims=True)
        elif data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        else:
            data = data[:, :2]
        pcm = (np.clip(data, -1, 1) * 32767).astype('<i2').tobytes()
        return self.feed_pcm(pcm)

    def feed_pcm(self, pcm: bytes) -> list[tuple[int, float, Path]]:
        block = self.channels * 2
        if len(pcm) % block:
            raise ValueError('PCM音声データのサイズが不正です。')
        if self.wav is None:
            raise RuntimeError('録音ファイルが開かれていません。')
        # RIFF WAV is limited to 4GiB. Never produce a silently corrupted file.
        if (self.total_frames * block) + len(pcm) > 0xFFFFFF00:
            raise OverflowError('WAV 4GiB上限に達しました。録音を終了して新しいファイルを作成してください。')
        self.wav.writeframesraw(pcm)
        self.total_frames += len(pcm) // block
        ready = []
        if self.chunk_dir is not None:
            remaining = memoryview(pcm)
            while remaining:
                take_frames = min(self.chunk_frames - self.segment_frames, len(remaining) // block)
                n = take_frames * block
                self.segment_bytes.extend(remaining[:n])
                self.segment_frames += take_frames
                remaining = remaining[n:]
                if self.segment_frames >= self.chunk_frames:
                    ready.append(self._flush_chunk())
        return ready

    def _flush_chunk(self) -> tuple[int, float, Path]:
        assert self.chunk_dir is not None
        index = self.segment_index
        # Segment boundaries are sample-count based, with no deliberate gaps.
        offset = index * self.chunk_frames / self.rate
        result = self.chunk_dir / f'segment_{index:06d}.wav'
        temp = self.chunk_dir / f'.segment_{index:06d}.working'
        with wave.open(str(temp), 'wb') as chunk:
            chunk.setnchannels(self.channels)
            chunk.setsampwidth(2)
            chunk.setframerate(self.rate)
            chunk.writeframes(bytes(self.segment_bytes))
        temp.replace(result)
        self.segment_bytes.clear()
        self.segment_frames = 0
        self.segment_index += 1
        return index, offset, result

    def finish(self) -> list[tuple[int, float, Path]]:
        ready = []
        if self.segment_frames and self.chunk_dir is not None:
            ready.append(self._flush_chunk())
        return ready

    def __exit__(self, *_args):
        try:
            if self.wav is not None:
                self.wav.close()
                self.wav = None
        finally:
            if self.stream is not None:
                self.stream.close()
                self.stream = None


class _PortAudioRecorder:
    """Adapter making sounddevice.InputStream compatible with SoundCard's record()."""
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        try:
            self.stream.start()
        except Exception:
            self.stream.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.stream.stop()
        finally:
            self.stream.close()

    def record(self, numframes):
        data, overflowed = self.stream.read(numframes)
        if overflowed:
            emit('warning', message='PortAudioの入力がオーバーフローしました。音声欠落の可能性があります。')
        return data


def _portaudio_device(device_name: str, allow_default: bool = False):
    """Find a matching mic; never silently substitute a different named input."""
    import sounddevice as sd
    devices = sd.query_devices()
    candidates = [(i, entry) for i, entry in enumerate(devices)
                  if entry.get('max_input_channels', 0) > 0]
    if not candidates:
        raise RuntimeError('PortAudioでマイク入力を検出できませんでした。')
    requested = device_name.casefold().strip()
    # SoundCard and PortAudio can include different API-specific suffixes.
    matched = [(i, entry) for i, entry in candidates
               if requested == str(entry['name']).casefold().strip()]
    if not matched and requested:
        matched = [(i, entry) for i, entry in candidates
                   if requested in str(entry['name']).casefold()
                   or str(entry['name']).casefold() in requested]
    if matched:
        return matched[0]
    # When using the default SoundCard mic, the default PortAudio input may
    # have a different display name; use only a defensible name similarity.
    if requested and not allow_default:
        raise RuntimeError(f'PortAudio側で同じマイクを特定できません: {device_name}')
    default_input = sd.default.device[0]
    if default_input is None or default_input < 0:
        raise RuntimeError('PortAudio側に既定のマイクがありません。')
    return int(default_input), devices[int(default_input)]


def _open_portaudio_mic(stack: ExitStack, device, rate: int, allow_default: bool = False):
    import sounddevice as sd
    device_id, info = _portaudio_device(str(getattr(device, 'name', '')), allow_default)
    # Use mono input if possible. Windows SoundCard mono issue does not apply
    # to PortAudio. Native device rate is used only as a last fallback.
    candidates = list(dict.fromkeys((rate, 44100, int(info.get('default_samplerate') or rate))))
    failures = []
    for candidate in candidates:
        try:
            stream = sd.InputStream(device=device_id, samplerate=candidate,
                                    channels=1, dtype='float32', blocksize=0)
            recorder = stack.enter_context(_PortAudioRecorder(stream))
            emit('warning', message=f'SoundCardでマイクを開けなかったため、PortAudioに切り替えました: '
                 f'{info["name"]} / {candidate}Hz')
            return recorder, candidate, max(256, int(candidate * .20))
        except Exception as exc:
            failures.append(f'{candidate}Hz: {type(exc).__name__}: {exc}')
    raise RuntimeError('PortAudioでもマイクを開けませんでした: ' + ' / '.join(failures))


def open_recorder_with_fallback(stack: ExitStack, device, sample_rate: int,
                                block_seconds: float = .20, *, source: str = 'mic',
                                allow_portaudio: bool = True,
                                portaudio_default: bool = False):
    """WASAPI devices may reject 48kHz even when they were enumerated."""
    choices = (sample_rate, 44100) if sample_rate == 48000 else (sample_rate,)
    failures = []
    for candidate in choices:
        frames = max(256, int(candidate * block_seconds))
        try:
            recorder = stack.enter_context(device.recorder(samplerate=candidate,
                                                           blocksize=frames))
            if candidate != sample_rate:
                emit('warning',message=f'{sample_rate}Hzで録音デバイスを開けないため、{candidate}Hzで録音します。')
            return recorder, candidate, frames
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(f'{candidate}Hz: {type(exc).__name__}: {exc}')
    if source == 'mic' and allow_portaudio and sys.platform == 'win32':
        try:
            return _open_portaudio_mic(stack, device, sample_rate, portaudio_default)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            failures.append('PortAudio: ' + str(exc))
    raise RuntimeError('録音デバイスの初期化に失敗しました。'+' / '.join(failures))


def _capture_single(*, output: Path, chunks: Path | None, stop_file: Path,
            source: str = 'mic', device_id: str = '', sample_rate: int = 48000,
            chunk_seconds: float = 20.0, device=None) -> int:
    """Finalize on STOP even when a silent endpoint's blocking read never returns."""
    import queue
    import threading
    if source not in ('mic', 'loopback'):
        raise ValueError('録音元は mic / loopback を選択してください。')
    stop_file = Path(stop_file)
    channels = 2 if source == 'loopback' else 1
    events=queue.Queue(maxsize=12)
    end_capture=threading.Event()
    def publish(message):
        while not end_capture.is_set():
            try:events.put(message,timeout=.05);return
            except queue.Full:continue
    def acquire():
        try:
            with windows_audio_com(), ExitStack() as stack:
                microphone=device if device is not None else get_device(source,device_id)
                rec,rate,frames=open_recorder_with_fallback(stack,microphone,sample_rate,
                    source=source,portaudio_default=(not device_id))
                publish(('ready',rate,frames,str(microphone.name)))
                start=time.monotonic(); delivered=0
                while not end_capture.is_set() and not stop_file.exists():
                    samples=rec.record(numframes=frames)
                    publish(('samples',samples))
                    delivered+=len(samples)
                    if len(samples)==0:time.sleep(.02)
                    ahead=delivered/rate-(time.monotonic()-start)
                    if ahead>.25:time.sleep(min(.3,ahead-.15))
        except Exception as exc:
            publish(('error',str(exc)))
        finally:
            publish(('end',))
    worker=threading.Thread(target=acquire,daemon=True,name='audio-single')
    worker.start()
    try:
        first=events.get(timeout=20)
        if first[0]!='ready':
            raise RuntimeError('録音デバイスの初期化に失敗しました: '+str(first[1:]))
        _,sample_rate,requested_frames,name=first
        with RecordingWriter(output,sample_rate=sample_rate,channels=channels,
                             chunk_dir=chunks,chunk_seconds=chunk_seconds) as writer:
            emit('started',device=name,source=source,path=str(output))
            start=time.monotonic(); ended=False; error=None
            while True:
                try:message=events.get(timeout=.05)
                except queue.Empty:
                    if stop_file.exists():
                        target=max(writer.total_frames,round((time.monotonic()-start)*sample_rate))
                        while writer.total_frames<target:
                            count=min(requested_frames,target-writer.total_frames)
                            for idx,offset,path in writer.feed_pcm(bytes(count*channels*2)):
                                emit('chunk',index=idx,offset=offset,path=str(path))
                        break
                    continue
                if message[0]=='samples':
                    samples=message[1]
                    if len(samples):
                        ready=writer.feed(samples)
                    else:
                        deficit=max(0,int((time.monotonic()-start)*sample_rate)-writer.total_frames)
                        ready=writer.feed_pcm(bytes(min(deficit,requested_frames)*channels*2))
                    for idx,offset,path in ready:
                        emit('chunk',index=idx,offset=offset,path=str(path))
                elif message[0]=='error':
                    error=message[1];ended=True
                elif message[0]=='end':ended=True
                if writer.total_frames % (sample_rate*20)<requested_frames:
                    emit('progress',seconds=round(writer.total_frames/sample_rate,1))
                if ended or (stop_file.exists() and events.empty()):break
            for idx, offset, path in writer.finish():
                emit('chunk', index=idx, offset=offset, path=str(path))
            total = writer.total_frames / sample_rate
        if error and not stop_file.exists():raise RuntimeError(error)
    except queue.Empty:
        raise TimeoutError('録音デバイスの初期化がタイムアウトしました。') from None
    finally:
        end_capture.set()
        worker.join(timeout=.4)
    emit('finished', path=str(output), seconds=round(total, 2))
    return 0



def capture(*, output: Path, chunks: Path | None, stop_file: Path,
            source: str = 'mic', device_id: str = '', sample_rate: int = 48000,
            chunk_seconds: float = 20.0, device=None) -> int:
    # COM must be initialized on the thread opening the WASAPI stream.
    with windows_audio_com():
        return _capture_single(output=output, chunks=chunks, stop_file=stop_file,
            source=source, device_id=device_id, sample_rate=sample_rate,
            chunk_seconds=chunk_seconds, device=device)


def probe_device(*, source: str = 'mic', device_id: str = '',
                 sample_rate: int = 48000, frames: int = 4800, device=None) -> dict:
    """Brief real device read; silence is valid, zero returned frames is not."""
    import numpy as np
    with windows_audio_com():
        selected = device if device is not None else get_device(source, device_id)
        with ExitStack() as stack:
            rec, sample_rate, frames = open_recorder_with_fallback(
                stack, selected, sample_rate, block_seconds=frames/sample_rate, source=source,
                portaudio_default=(not device_id))
            samples = np.asarray(rec.record(numframes=frames), dtype=np.float32)
        if samples.size == 0 or (samples.ndim == 2 and samples.shape[0] == 0):
            raise RuntimeError('デバイスは開けましたが音声フレームが返りませんでした。')
        count = int(samples.shape[0])
        if samples.ndim not in (1, 2):
            raise RuntimeError(f'予期しない録音データの形状: {samples.shape}')
        peak = float(np.max(np.abs(samples)))
        return {'source': source, 'device': str(selected.name),
                'sample_rate': sample_rate, 'frames': count, 'peak': round(peak, 5),
                'silence': peak < 0.0001}


@contextmanager
def windows_audio_com():
    """Initialize WASAPI COM in each capture thread, not just on main thread."""
    if sys.platform != 'win32':
        yield
        return
    import ctypes
    ole32 = ctypes.windll.ole32
    hr = int(ole32.CoInitializeEx(None, 0))
    # RPC_E_CHANGED_MODE: this thread already has a COM apartment; usable.
    if hr not in (0,1) and (hr & 0xffffffff) != 0x80010106:
        raise OSError(f'WASAPI COM初期化に失敗: 0x{hr & 0xffffffff:08X}')
    try:
        yield
    finally:
        if hr in (0,1):
            ole32.CoUninitialize()


def capture_mix(*, output: Path, chunks: Path | None, stop_file: Path,
                mic_device_id: str = '', loopback_device_id: str = '',
                mic_gain: float = 1.0, loopback_gain: float = 1.0,
                sample_rate: int = 48000, chunk_seconds: float = 20.0,
                devices: dict | None = None, clock=None, channel_mode: str = 'mixed') -> int:
    """Simultaneous mic + PC capture with a single ordered WAV writer.

    Input acquisition happens on two separate threads, using a shared monotonic
    timeline. ASR remains in the GUI's separate process. The mixer buffers
    about 500ms to accommodate callback jitter, and substitutes silence where
    an endpoint returns no samples. Input delivery gaps are warned about.
    """
    import queue
    import threading
    import numpy as np
    from live_mix import AlignedAudioMixer, StreamClock

    if sample_rate <= 0:
        raise ValueError('サンプルレートが無効です。')
    stop_file = Path(stop_file)
    # Windows audio endpoints are opened inside each capture thread.
    if devices is None:
        devices = {'mic': None, 'loopback': None}
    for source in ('mic', 'loopback'):
        if source not in devices:
            raise ValueError('マイクとPC再生音の両方を選択してください。')
    mixer = AlignedAudioMixer(sample_rate, mic_gain, loopback_gain, channel_mode=channel_mode)
    # Bound queued device blocks if disk writing stalls, while letting STOP
    # release producers waiting for the writer instead of deadlocking them.
    events = queue.Queue(maxsize=128)
    end_capture = threading.Event()
    begin_capture = threading.Event()
    queue_backpressure = threading.Event()
    started = set()
    names = {}
    errors = []
    requested_frames = max(256, int(sample_rate * 0.10))
    origin_holder = [0.0]
    last_received = {'mic': -1, 'loopback': -1}
    now = clock if clock is not None else time.monotonic

    def publish(event):
        while not end_capture.is_set() and not stop_file.exists():
            try:
                events.put(event, timeout=.05)
                return True
            except queue.Full:
                queue_backpressure.set()
                continue
        return False

    def read_device(source, device):
        try:
            with windows_audio_com():
                if device is None:
                    selected_id = mic_device_id if source=='mic' else loopback_device_id
                    device = get_device(source, selected_id)
                read_from_device(source, device)
        except Exception as exc:
            publish(('fatal',source,f'{type(exc).__name__}: {exc}'))
            end_capture.set()
        finally:
            publish(('end',source))

    def read_from_device(source, device):
        with ExitStack() as device_stack:
            if source == 'mic':
                rec, opened_rate, _ = open_recorder_with_fallback(
                    device_stack, device, sample_rate, block_seconds=.10, source='mic',
                    portaudio_default=(not mic_device_id))
                if opened_rate != sample_rate:
                    raise RuntimeError('ミックス録音のマイク側が48kHzで開けませんでした。'
                                       'デバイスの既定フォーマットを48kHzに設定してください。')
            else:
                rec = device_stack.enter_context(device.recorder(
                    samplerate=sample_rate, blocksize=requested_frames))
            if not publish(('ready', source, str(device.name))):
                return
            if not begin_capture.wait(timeout=20):
                raise TimeoutError('音声デバイスの同期開始がタイムアウトしました。')
            clock_map = StreamClock(sample_rate, origin_holder[0])
            while not end_capture.is_set() and not stop_file.exists():
                samples = np.asarray(rec.record(numframes=requested_frames),dtype=np.float32)
                timestamp = now()
                if samples.ndim == 1:
                    samples = samples[:, None]
                if samples.size == 0:
                    # WASAPI loopback can emit empty blocks while the PC is
                    # silent. The main writer preserves these as silence.
                    time.sleep(0.01)
                    continue
                if samples.ndim != 2 or samples.shape[1] < 1:
                    raise ValueError(f'{source}: 無効な音声データ形状 {samples.shape}')
                if samples.shape[1] > 2:
                    # Multi-input recording interfaces are common. Mix mic
                    # channels down; keep front L/R for surround playback.
                    samples=(samples.mean(axis=1,keepdims=True) if source=='mic'
                             else samples[:,:2])
                first = clock_map.place(len(samples), timestamp)
                if not publish(('block', source, first, samples)):
                    break
                if clock_map.discontinuities:
                    publish(('warning', f'{source}: 入力の遅延／欠落を検出。無音補完で時間軸を保持します。'))
                    clock_map.discontinuities = 0
                # Some WASAPI loopback devices deliver silence faster than
                # real time; avoid accumulating imaginary future samples.
                ahead = clock_map.next_frame/sample_rate - (now()-origin_holder[0])
                if ahead > 0.20:
                    time.sleep(min(0.20,ahead-0.12))

    workers = [threading.Thread(target=read_device,args=(key,devices[key]),daemon=True,
                                name=f'audio-{key}') for key in ('mic','loopback')]
    missing_notice = set()
    backpressure_notified = False
    ended = set()
    stopping_at = None
    with ExitStack() as writer_stack:
        for worker in workers:
            worker.start()
        try:
            deadline=now()+18
            # Ensure both WASAPI endpoints are open before establishing the
            # common recording epoch.
            while len(started)<2 and not errors and now()<deadline:
                try: event=events.get(timeout=0.12)
                except queue.Empty: continue
                if event[0]=='ready':
                    started.add(event[1])
                    names[event[1]]=event[2]
                elif event[0]=='fatal':
                    errors.append(f'{event[1]}: {event[2]}')
                elif event[0]=='end':
                    ended.add(event[1])
            if len(started)<2:
                errors.append('マイクとPC再生音の両方を初期化できませんでした。')
                raise RuntimeError('; '.join(errors))
            # A device-open failure must not leave a misleading empty WAV.
            writer = writer_stack.enter_context(RecordingWriter(
                output, sample_rate=sample_rate, channels=2,
                chunk_dir=chunks, chunk_seconds=chunk_seconds))
            origin=now()
            origin_holder[0]=origin
            begin_capture.set()
            emit('started',device='マイク + PC再生音',source='mix',
                 mic=names['mic'],loopback=names['loopback'],path=str(output),channel_mode=channel_mode)
            while True:
                if queue_backpressure.is_set() and not backpressure_notified:
                    emit('warning', message='録音データの保存が追いついていません。'
                         '録音デバイスで音声が欠落する可能性があります。')
                    backpressure_notified = True
                finished = stop_file.exists() or end_capture.is_set()
                if finished:
                    if stopping_at is None:
                        stopping_at = now()
                    end_capture.set()
                # Pull all currently delivered audio before rendering.
                while True:
                    try: event=events.get_nowait()
                    except queue.Empty: break
                    kind=event[0]
                    if kind=='block':
                        _,source,first,data=event
                        mixer.push(source,first,data)
                        last_received[source]=max(last_received[source],first+len(data))
                    elif kind=='fatal':
                        errors.append(f'{event[1]}: {event[2]}')
                        end_capture.set()
                    elif kind=='end':
                        ended.add(event[1])
                    elif kind=='warning':
                        emit('warning',message=event[1])
                if stopping_at is not None:
                    # Freeze the timeline at STOP. Silent WASAPI reads can
                    # block indefinitely; waiting must never extend the WAV.
                    target=max(writer.total_frames,round((stopping_at-origin)*sample_rate))
                else:
                    # Hold back the writing cursor to absorb input timing jitter.
                    target=max(0,int((now()-origin-0.55)*sample_rate))
                while writer.total_frames < target:
                    count=min(requested_frames,target-writer.total_frames)
                    audio,missing=mixer.pull(count)
                    for source,count_missing in missing.items():
                        if (count_missing and source not in missing_notice and
                                mixer.sources[source].missing_frames >= sample_rate*2):
                            emit('warning',message=f'{source}: 入力の無音／欠落区間を検出しました。'
                                   '録音時間軸を維持するため無音補完しています。')
                            missing_notice.add(source)
                    for idx,offset,path in writer.feed(audio):
                        emit('chunk',index=idx,offset=offset,path=str(path))
                    if writer.total_frames % (sample_rate*20)<requested_frames:
                        emit('progress',seconds=round(writer.total_frames/sample_rate,1))
                if stopping_at is not None and (len(ended)==2 or now()-stopping_at >= .8):
                    break
                time.sleep(0.015)
            for idx,offset,path in writer.finish():
                emit('chunk',index=idx,offset=offset,path=str(path))
            total=writer.total_frames/sample_rate
        finally:
            end_capture.set()
            begin_capture.set()
            join_deadline=time.monotonic()+.7
            for worker in workers:
                worker.join(timeout=max(0,join_deadline-time.monotonic()))
            if any(worker.is_alive() for worker in workers):
                emit('warning',message='無音の入力待ちを終了しました。停止時点までのWAVを確定しました。')
    emit('finished',path=str(output),seconds=round(total,2),source='mix')
    if errors:
        for message in errors:
            emit('warning',message=message)
        return 1
    return 0

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Windows microphone / WASAPI loopback capture')
    parser.add_argument('--list-devices', action='store_true')
    parser.add_argument('--probe-device', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--chunks', type=Path)
    parser.add_argument('--stop-file', type=Path)
    parser.add_argument('--source', choices=('mic', 'loopback', 'mix'), default='mic')
    parser.add_argument('--device-id', default='')
    parser.add_argument('--mic-device-id', default='')
    parser.add_argument('--loopback-device-id', default='')
    parser.add_argument('--mic-gain', type=float, default=1.0)
    parser.add_argument('--loopback-gain', type=float, default=1.0)
    parser.add_argument('--sample-rate', type=int, default=48000)
    parser.add_argument('--chunk-seconds', type=float, default=20)
    parser.add_argument('--channel-mode', choices=('mixed', 'split_lr'), default='mixed')
    args = parser.parse_args(argv)
    try:
        if args.list_devices:
            with windows_audio_com():
                emit('devices', devices=list_devices())
            return 0
        if args.probe_device:
            sources = ('mic', 'loopback') if args.source == 'mix' else (args.source,)
            for source in sources:
                device_id = (args.mic_device_id if source == 'mic' else args.loopback_device_id) if args.source == 'mix' else args.device_id
                emit('probe_ok', **probe_device(source=source, device_id=device_id, sample_rate=args.sample_rate, frames=max(256, int(args.sample_rate * 0.25))))
            return 0
        if args.output is None or args.stop_file is None:
            parser.error('--output and --stop-file required')
        if args.channel_mode == 'split_lr' and args.source != 'mix':
            raise ValueError('L/R分離録音にはマイク＋PC再生音を指定してください。')
        if args.source == 'mix':
            return capture_mix(output=args.output, chunks=args.chunks,stop_file=args.stop_file,
                  mic_device_id=args.mic_device_id,loopback_device_id=args.loopback_device_id,
                  mic_gain=args.mic_gain,loopback_gain=args.loopback_gain,
                  sample_rate=args.sample_rate,chunk_seconds=args.chunk_seconds,channel_mode=args.channel_mode)
        return capture(output=args.output, chunks=args.chunks, stop_file=args.stop_file,
                       source=args.source, device_id=args.device_id, sample_rate=args.sample_rate,
                       chunk_seconds=args.chunk_seconds)
    except (Exception, KeyboardInterrupt) as exc:
        emit('error', message=f'{type(exc).__name__}: {exc}',
             detail=traceback.format_exc(limit=8))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

