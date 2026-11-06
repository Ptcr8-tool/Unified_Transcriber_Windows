"""Serialize Hugging Face's Windows cache capability probe."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import sys
import threading


_context_lock = threading.RLock()
_probe_lock = threading.RLock()


@contextmanager
def hf_cache_probes():
    """Let every download worker see the completed symlink capability check.

    Hub 0.36 publishes a temporary True before testing the cache directory.
    A second worker can consume that value and fail with WinError 1314. Lock
    the existing probe so Hub can use its ordinary move/copy fallback.
    """
    if sys.platform != 'win32':
        yield
        return
    try:
        import huggingface_hub.file_download as file_download
    except ImportError:
        yield
        return

    # A separate lock protects nested/overlapping patch lifetimes while the
    # download's worker threads acquire only the short-lived probe lock.
    with _context_lock:
        original = getattr(file_download, 'are_symlinks_supported', None)
        if not callable(original):
            yield
            return

        @wraps(original)
        def probe(*args, **kwargs):
            with _probe_lock:
                return original(*args, **kwargs)

        file_download.are_symlinks_supported = probe
        try:
            yield
        finally:
            file_download.are_symlinks_supported = original
