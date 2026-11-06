"""Regression coverage for Hub 0.36's optimistic Windows symlink probe."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hf_cache


class HuggingFaceCacheTests(unittest.TestCase):
    def setUp(self):
        self.hub = ModuleType('huggingface_hub')
        self.hub.__path__ = []
        self.download = ModuleType('huggingface_hub.file_download')
        self.hub.file_download = self.download
        self.modules = {
            'huggingface_hub': self.hub,
            'huggingface_hub.file_download': self.download,
        }
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, self.modules).start()
        patch.object(hf_cache, 'sys', SimpleNamespace(platform='win32')).start()

    def test_upstream_arguments_return_value_and_exception_are_preserved(self):
        calls = []

        def original(cache_dir=None):
            calls.append(cache_dir)
            if cache_dir == 'unreadable':
                raise PermissionError('cache unavailable')
            return cache_dir == 'supported'

        self.download.are_symlinks_supported = original
        with hf_cache.hf_cache_probes():
            self.assertTrue(self.download.are_symlinks_supported(cache_dir='supported'))
            self.assertFalse(self.download.are_symlinks_supported('unsupported'))
            with self.assertRaisesRegex(PermissionError, 'cache unavailable'):
                self.download.are_symlinks_supported('unreadable')
        self.assertEqual(calls, ['supported', 'unsupported', 'unreadable'])
        self.assertIs(self.download.are_symlinks_supported, original)

    def test_concurrent_download_waits_for_the_real_capability_result(self):
        cached = {}
        probe_started = threading.Event()
        release_probe = threading.Event()

        def original(cache_dir=None):
            # Mirrors the ordering in the pinned huggingface_hub 0.36 probe.
            if cache_dir not in cached:
                cached[cache_dir] = True
                probe_started.set()
                if not release_probe.wait(5):
                    raise RuntimeError('test probe was not released')
                cached[cache_dir] = False
            return cached[cache_dir]

        self.download.are_symlinks_supported = original

        def imported_download(cache_dir):
            # An already-imported hf_hub_download/_create_symlink function
            # still resolves the capability function from its module globals.
            if self.download.are_symlinks_supported(cache_dir):
                error = OSError('A required privilege is not held by the client')
                error.winerror = 1314
                raise error
            return 'upstream copy fallback'

        with hf_cache.hf_cache_probes(), ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.download.are_symlinks_supported, 'cache')
            try:
                self.assertTrue(probe_started.wait(5))
                second = workers.submit(imported_download, 'cache')
                with self.assertRaises(TimeoutError):
                    second.result(timeout=.1)
            finally:
                release_probe.set()
            self.assertFalse(first.result(timeout=5))
            self.assertEqual(second.result(timeout=5), 'upstream copy fallback')
        self.assertIs(self.download.are_symlinks_supported, original)

    def test_nested_contexts_restore_their_predecessor_on_error(self):
        original = lambda cache_dir=None: False
        self.download.are_symlinks_supported = original
        with hf_cache.hf_cache_probes():
            outer = self.download.are_symlinks_supported
            with self.assertRaisesRegex(ValueError, 'download failed'):
                with hf_cache.hf_cache_probes():
                    self.assertFalse(self.download.are_symlinks_supported('cache'))
                    raise ValueError('download failed')
            self.assertIs(self.download.are_symlinks_supported, outer)
        self.assertIs(self.download.are_symlinks_supported, original)

    def test_other_platforms_keep_the_upstream_probe(self):
        original = lambda cache_dir=None: True
        self.download.are_symlinks_supported = original
        with patch.object(hf_cache, 'sys', SimpleNamespace(platform='linux')):
            with hf_cache.hf_cache_probes():
                self.assertIs(self.download.are_symlinks_supported, original)

    def test_absent_optional_hub_and_unavailable_hook_are_supported(self):
        with hf_cache.hf_cache_probes():
            self.assertFalse(hasattr(self.download, 'are_symlinks_supported'))
        with patch.dict(sys.modules, {'huggingface_hub.file_download': None}):
            with hf_cache.hf_cache_probes():
                pass


if __name__ == '__main__':
    unittest.main()
