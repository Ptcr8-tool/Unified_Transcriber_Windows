"""Compatibility helpers for qwen-asr forced-alignment results.

qwen-asr 0.0.6 yields ASRTranscription.time_stamps = ForcedAlignResult(
    items=[ForcedAlignItem(text=..., start_time=..., end_time=...), ...]
)
not a list.  Older/custom wrappers may return lists/tuples/dicts.
"""
from __future__ import annotations

from typing import Any


def timestamp_entries(result: Any) -> list[Any]:
    """Return timestamp records, supporting official ForcedAlignResult.items."""
    timestamps = result.get('time_stamps') if isinstance(result, dict) else getattr(result, 'time_stamps', None)
    if timestamps is None:
        return []
    if isinstance(timestamps, dict):
        timestamps = timestamps.get('items', [])
    else:
        items = getattr(timestamps, 'items', None)
        # `.items` of dict is a method; ignore that kind of value.
        if items is not None and not callable(items):
            timestamps = items
    if not isinstance(timestamps, (list, tuple)):
        return []
    while len(timestamps) == 1 and isinstance(timestamps[0], (list, tuple)):
        timestamps = timestamps[0]
    return list(timestamps)


def stamp_value(stamp: Any, key: str) -> Any:
    if isinstance(stamp, dict):
        return stamp.get(key)
    value = getattr(stamp, key, None)
    if value is not None:
        return value
    if isinstance(stamp, (list, tuple)) and len(stamp) >= 3:
        return stamp[{'start_time': 0, 'end_time': 1, 'text': 2}[key]]
    return None


def qwen_dtype(torch: Any, device: str) -> Any:
    """RTX 20xx/30? select fp16 for GPUs without native bf16 (e.g. 2080 Ti)."""
    if device != 'cuda':
        return torch.float32
    try:
        major, _ = torch.cuda.get_device_capability(0)
        if major >= 8 and torch.cuda.is_bf16_supported():
            return torch.bfloat16
    except (AttributeError, RuntimeError, AssertionError):
        pass
    return torch.float16


def transcript_text(result: Any) -> str:
    value = result.get('text', '') if isinstance(result, dict) else getattr(result, 'text', '')
    return str(value or '').strip()


def pcm_wav_seconds(path: Any) -> float:
    import wave
    with wave.open(str(path), 'rb') as wav:
        return wav.getnframes() / wav.getframerate()


UNALIGNED_SPEAKER = 'UNALIGNED'
UNALIGNED_PREFIX = '[注意: Qwen整列不可・チャンク単位の概算時刻、話者未確定] '
