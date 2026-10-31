"""Structured, stage-specific progress events for nested setup processes."""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import json
import math
import re
import threading
import time

from engine_progress import EngineUpdate

PREFIX = '@@SETUP_PROGRESS@@'
_print_lock = threading.Lock()


def emit_setup_progress(stage: str, fraction: float | None = None, detail: str = '') -> None:
    # A complete newline-terminated record survives the nested stdout pipes.
    # Only static labels, numeric counts and allowlisted model names go here.
    event = {'stage': str(stage)[:80], 'fraction': fraction, 'detail': str(detail)[:100]}
    with _print_lock:
        print(PREFIX + json.dumps(event, ensure_ascii=True, separators=(',', ':')), flush=True)


def parse_setup_line(line: str) -> EngineUpdate | None:
    text = line.strip()
    if text.startswith(PREFIX):
        try:
            raw = json.loads(text[len(PREFIX):])
            if not isinstance(raw, dict) or not isinstance(raw.get('stage'), str):
                return None
            stage = raw['stage'].strip()[:80]
            detail = raw.get('detail', '')
            if not stage or not isinstance(detail, str):
                return None
            value = raw.get('fraction')
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                    return None
                if not 0 <= value <= 1:
                    return None
            return EngineUpdate(stage, value, detail=detail[:100])
        except (ValueError, TypeError, OverflowError):
            return None
    # Task counts do not estimate overall work: setup tasks have very different
    # duration. Show the current task but keep the indicator indeterminate.
    m = re.match(r'^\[(\d{1,5})/(\d{1,5})\]\s+(.{1,120})$', text)
    if m and 0 < int(m[1]) <= int(m[2]):
        return EngineUpdate('セットアップ', detail=f'{m[1]}/{m[2]} · {m[3]}')
    m = re.match(r'^Downloading\s+([A-Za-z0-9_.+-]{1,100}\.(?:whl|tar\.gz|zip))(?:\s|$)', text)
    if m:
        return EngineUpdate('パッケージを取得中', detail=m[1])
    if text.startswith('Installing collected packages:'):
        return EngineUpdate('パッケージをインストール中')
    return None


def progress_tqdm(stage: str):
    """Provide real per-transfer or per-file-count progress where supported."""
    try:
        from tqdm.auto import tqdm
    except ImportError:
        return None

    class SetupTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            self._setup_last = 0.0
            # tqdm auto-disables on redirected pipes in some dependency versions.
            kwargs.pop('name', None)  # HF's own tqdm supports this extra keyword.
            kwargs['disable'] = False
            super().__init__(*args, **kwargs)

        def display(self, msg=None, pos=None):
            now = time.monotonic()
            value = getattr(self, 'n', 0)
            total = getattr(self, 'total', None)
            try:
                count, maximum = float(value), float(total)
                fraction = min(1.0, max(0.0, count / maximum)) if maximum > 0 else None
            except (ValueError, TypeError, OverflowError):
                return
            if fraction is not None and not math.isfinite(fraction):
                return
            if now - self._setup_last < .25 and fraction != 1:
                return
            self._setup_last = now
            unit = str(getattr(self, 'unit', '') or '')
            detail = '転送中のデータ（個別ファイルまたは転送単位）' if unit.lower().endswith('b') else 'ファイル数'
            # Newer HF versions build the total incrementally while files are
            # discovered. That changing denominator cannot support an honest
            # percentage even when it momentarily equals the bytes so far.
            description = str(getattr(self, 'desc', '') or '').lower()
            if unit.lower().endswith('b') and ('incomplete total' in description or 'unknown total' in description):
                fraction = None
                detail = f'転送中（合計未確定、{max(0, count)/1048576:.1f} MiB）'
            if detail == 'ファイル数' and maximum > 0:
                detail += f' {int(count)}/{int(maximum)}'
            emit_setup_progress(stage, fraction, detail)

    return SetupTqdm


@contextmanager
def hf_snapshot_progress(stage: str):
    """Intercept only this setup subprocess's HF snapshots, without changing downloads."""
    hub = original = progress = None
    try:
        import huggingface_hub as hub
        original = hub.snapshot_download
        progress = progress_tqdm(stage)
        if 'tqdm_class' not in inspect.signature(original).parameters:
            progress = None
    except (ImportError, AttributeError, ValueError, TypeError):
        progress = None
    if progress is None:
        yield
        return

    def wrapped(*args, **kwargs):
        # faster-whisper normally disables tqdm; replace that display class for
        # this call while preserving its revision/cache/download arguments.
        kwargs['tqdm_class'] = progress
        return original(*args, **kwargs)

    # In huggingface_hub 0.36 the public snapshot progress class covers file
    # counts only. Its HTTP/Xet byte bars are created separately inside the
    # file downloader. Bridge those bars only when this known hook is present;
    # the download, authentication and cache logic remain entirely upstream's.
    file_module = old_context = None
    try:
        import huggingface_hub.file_download as file_module
        old_context = getattr(file_module, '_get_progress_bar_context', None)
        if old_context is not None:
            parameters = inspect.signature(old_context).parameters

            def byte_context(*args, **kwargs):
                name = str(kwargs.get('name', ''))
                if not (name.endswith('.http_get') or name.endswith('.xet_get')):
                    return old_context(*args, **kwargs)
                if kwargs.get('_tqdm_bar') is not None:
                    return old_context(*args, **kwargs)
                if 'tqdm_class' in parameters:
                    kwargs['tqdm_class'] = progress
                    return old_context(*args, **kwargs)
                if not args and 'total' in kwargs and 'initial' in kwargs:
                    return progress(total=kwargs['total'], initial=kwargs['initial'],
                                    unit='B', unit_scale=True)
                return old_context(*args, **kwargs)

            file_module._get_progress_bar_context = byte_context
    except (ImportError, AttributeError, ValueError, TypeError):
        file_module = old_context = None
    hub.snapshot_download = wrapped
    try:
        yield
    finally:
        hub.snapshot_download = original
        if file_module is not None and old_context is not None:
            file_module._get_progress_bar_context = old_context
