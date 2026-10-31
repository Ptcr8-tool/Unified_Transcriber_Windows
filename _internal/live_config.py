"""Capture UI options with validation independent from Tkinter/hardware."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from output_locations import default_directory


RECORDING_CHANNELS = {
    '1（モノラル）': (1, 'mixed'),
    '2（ステレオ）': (2, 'mixed'),
    'L：PC音 / R：マイク': (2, 'split_lr'),
}


def recording_channel_label(channels: int, mode: str = 'mixed') -> str:
    return next(label for label, value in RECORDING_CHANNELS.items() if value == (channels, mode))


def is_silent_pcm(path: Path) -> bool:
    """Only exact-zero PCM qualifies; never discard quiet speech using a threshold."""
    import wave
    try:
        with wave.open(str(path),'rb') as stream:
            if stream.getnframes()==0:return False
            while True:
                block=stream.readframes(16384)
                if not block:return True
                if any(block):return False
    except (OSError,EOFError,wave.Error):
        return False


@dataclass(frozen=True)
class LiveOptions:
    source: str = 'mic'
    mode: str = 'both'
    chunk_seconds: int = 20
    device_id: str = ''
    mic_device_id: str = ''
    loopback_device_id: str = ''
    mic_gain: float = 1.0
    loopback_gain: float = 1.0
    folder: Path = field(default_factory=lambda: default_directory('recordings'))
    final_pass: bool = True
    recording_format: str = 'wav'  # original PCM throughout live capture, then optional encoding
    recording_bitrate: str = '192k'
    recording_sample_rate: int = 48000  # final encoded file (capture remains 48 kHz)
    recording_channels: int = 2
    recording_pcm_bits: int = 16
    channel_mode: str = 'mixed'
    post_transcribe: bool = False

    def validate(self):
        if self.recording_format not in ('wav', 'mp3', 'm4a', 'flac'):
            raise ValueError('録音形式は WAV / MP3 / M4A / FLAC から選択してください。')
        if self.recording_bitrate not in ('64k', '96k', '128k', '160k', '192k', '256k', '320k'):
            raise ValueError('録音ビットレートを確認してください。')
        if self.recording_sample_rate not in (44100, 48000):
            raise ValueError('保存サンプルレートは44.1kHzか48kHzです。')
        if self.recording_channels not in (1, 2):
            raise ValueError('保存チャンネル数は1か2です。')
        if self.channel_mode not in ('mixed', 'split_lr'):
            raise ValueError('保存チャンネルの割り当てを確認してください。')
        if self.channel_mode == 'split_lr' and (self.source != 'mix' or self.recording_channels != 2):
            raise ValueError('L/R分離録音にはマイク＋PC再生音と2チャンネルが必要です。')
        if not isinstance(self.post_transcribe, bool):
            raise ValueError('録音終了後の文字起こし設定を確認してください。')
        if self.recording_pcm_bits not in (16, 24):
            raise ValueError('PCMのビット深度は16bitか24bitです。')
        if self.source not in ('mic','loopback','mix'):
            raise ValueError('録音元を選択してください。')
        if not (0.0 <= self.mic_gain <= 2.0 and 0.0 <= self.loopback_gain <= 2.0):
            raise ValueError('音量は0～200%で指定してください。')
        if self.mode not in ('both','record','transcribe'):
            raise ValueError('録音/文字起こしモードを選択してください。')
        if self.chunk_seconds not in (10,20,30,60):
            raise ValueError('認識区間は10/20/30/60秒から選択してください。')
        if self.folder.exists() and not self.folder.is_dir():
            raise ValueError('保存先はフォルダーを指定してください。')
        if not str(self.folder):
            raise ValueError('保存先を選択してください。')

    @property
    def needs_transcription(self):
        return self.realtime_transcription or self.post_transcribe

    @property
    def realtime_transcription(self):
        return self.mode != 'record'

    @property
    def needs_final_transcription(self):
        return self.post_transcribe if self.mode == 'record' else self.final_pass

    @property
    def keep_audio(self):
        return self.mode != 'transcribe'


def collision_safe_path(folder: Path, at: datetime, *, stem='ライブ録音', extension='wav') -> Path:
    folder=Path(folder)
    initial=f'{stem}_{at:%Y%m%d_%H%M%S}'
    for i in range(10000):
        suffix=f'_{i}' if i else ''
        result=folder / f'{initial}{suffix}.{extension}'
        if not result.exists():
            return result
    raise FileExistsError('空きファイル名がありません。')


def unique_media_path(desired: Path) -> Path:
    """Find an unused filename for a transcoded recording, without clobbering media."""
    desired=Path(desired)
    for index in range(10000):
        candidate=(desired if index==0 else
                   desired.with_name(f'{desired.stem}_{index}{desired.suffix}'))
        if not candidate.exists():
            return candidate
    raise FileExistsError('録音出力先に空きファイル名がありません。')
