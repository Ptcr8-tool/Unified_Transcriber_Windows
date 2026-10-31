"""Advisory GPU workload pacing between recognition progress events.

Not an NVIDIA GPU-utilization cap: long kernels and diarization phases cannot
be preempted by this module. Receives changes through atomic JSON file.
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

_last_asr_time = None


def pace_if_needed(stage: str) -> None:
    global _last_asr_time
    if stage != 'asr':
        return
    now = time.monotonic()
    delta = None if _last_asr_time is None else max(0, now - _last_asr_time)
    _last_asr_time = now
    path = os.environ.get('TRANSCRIBER_RESOURCE_CONTROL', '')
    if not path or delta is None:
        return
    try:
        pct = json.loads(Path(path).read_text(encoding='utf-8')).get('gpu_soft_percent')
        if pct is None or not 10 <= int(pct) < 100:
            return
        # Bounded duty-cycle cooling *between* units; no in-flight kernel control.
        pause = min(3.0, delta * (100 / int(pct) - 1))
        if pause >= 0.05:
            time.sleep(pause)
    except (OSError, ValueError, TypeError, ZeroDivisionError):
        pass
