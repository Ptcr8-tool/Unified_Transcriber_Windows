"""Thread-independent, timestamp-aligned two-source streaming PCM mixer.

SoundCard may return blocks with different channel counts and different timing.
The capture threads timestamp and enqueue blocks; only the writer thread ever
uses this mixer or writes the final WAV. Missing input is explicitly silenced,
not interpreted as the other source having the same clock.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


SOURCES = ('mic', 'loopback')


@dataclass
class StreamClock:
    """Map each device's consecutive samples onto one monotonic timeline."""
    sample_rate: int
    origin: float
    next_frame: int | None = None
    discontinuities: int = 0

    def place(self, frames: int, completed_at: float) -> int:
        wall_start = round((completed_at - self.origin) * self.sample_rate) - frames
        if self.next_frame is None:
            position = wall_start
        else:
            position = self.next_frame
            drift = wall_start - position
            # A stall can create real gaps. Retain these as silence rather than
            # compressing the recording timeline. Ignore OS callback jitter.
            if drift > self.sample_rate * 0.45:
                position = wall_start
                self.discontinuities += 1
        self.next_frame = position + frames
        return position


@dataclass
class _Source:
    pending: deque = field(default_factory=deque)
    missing_frames: int = 0
    last_frame: int = 0


class AlignedAudioMixer:
    """Mix data using sample indices; supports mono/stereo, with clip protection.

    pull(start,count) returns stereo float32, and the number of absent frames
    per device. Each data frame belongs to exactly one mixed output frame.
    """
    def __init__(self, sample_rate: int, mic_gain: float = 1.0, loopback_gain: float = 1.0,
                 channel_mode: str = 'mixed'):
        if sample_rate <= 0 or not (0 <= mic_gain <= 2 and 0 <= loopback_gain <= 2):
            raise ValueError('サンプルレートと入力音量は有効な範囲で指定してください。')
        if channel_mode not in ('mixed', 'split_lr'):
            raise ValueError('保存チャンネルの割り当てが無効です。')
        self.channel_mode = channel_mode
        self.rate = sample_rate
        self.gains = {'mic': mic_gain, 'loopback': loopback_gain}
        self.sources = {key: _Source() for key in SOURCES}
        self.cursor = 0

    def push(self, key: str, first: int, samples) -> None:
        import numpy as np
        if key not in self.sources:
            raise ValueError('不明な録音元です。')
        data = np.asarray(samples, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, None]
        if data.ndim != 2 or data.shape[1] not in (1, 2):
            raise ValueError('チャンネル数が不正です。')
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        end = first + len(data)
        if end <= self.cursor:
            return  # Device returned old data too late to safely change saved WAV
        if first < self.cursor:
            data = data[self.cursor - first:]
            first = self.cursor
        self.sources[key].pending.append((first, data.copy()))
        self.sources[key].last_frame = max(end, self.sources[key].last_frame)

    def pull(self, frames: int):
        import numpy as np
        if frames < 0:
            raise ValueError('フレーム数が負数です。')
        out = np.zeros((frames, 2), dtype=np.float32)
        start = self.cursor
        end = start + frames
        missing = {}
        for key, source in self.sources.items():
            component = np.zeros((frames, 2), dtype=np.float32)
            present = np.zeros(frames, dtype=np.bool_)
            while source.pending and source.pending[0][0] + len(source.pending[0][1]) <= start:
                source.pending.popleft()
            for first, block in source.pending:
                block_end = first + len(block)
                if first >= end:
                    break
                lo, hi = max(first, start), min(block_end, end)
                if hi <= lo:
                    continue
                component[lo-start:hi-start] = block[lo-first:hi-first]
                present[lo-start:hi-start] = True
            while source.pending and source.pending[0][0] + len(source.pending[0][1]) <= end:
                source.pending.popleft()
            missed = int(frames - np.count_nonzero(present))
            source.missing_frames += missed
            missing[key] = missed
            if self.channel_mode == 'split_lr':
                # Preserve each source on its own channel. PC stereo is folded
                # to mono on L; the microphone alone occupies R.
                out[:, 0 if key == 'loopback' else 1] = component.mean(axis=1) * self.gains[key]
            else:
                out += component * self.gains[key]
        # Per-output-block peak limiter prevents clipping when both sources peak.
        # Unlike 50/50 fixed averaging, an isolated quiet mic remains audible.
        if self.channel_mode == 'split_lr' and frames:
            peaks = np.max(np.abs(out), axis=0)
            out *= np.minimum(1, .98 / np.maximum(peaks, .98))
        else:
            peak = float(np.max(np.abs(out))) if frames else 0.0
            if peak > 0.98:
                out *= (0.98 / peak)
        self.cursor = end
        return out, missing
