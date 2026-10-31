"""Native hotkey shutdown releases GUI callbacks before the owner thread exits."""
import ctypes
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_hotkeys import WindowsHotkeys


class HotkeyShutdownTests(unittest.TestCase):
    def test_close_waits_for_native_unregister_and_releases_owner_on_caller_thread(self):
        api = SimpleNamespace(**{name: MagicMock() for name in
            ('RegisterHotKey', 'UnregisterHotKey', 'PeekMessageW')})
        api.RegisterHotKey.return_value = 1
        api.PeekMessageW.return_value = 0
        registered = threading.Event()
        unregistered = threading.Event()
        api.RegisterHotKey.side_effect = lambda *_: (registered.set() or 1)
        def unregister(*_):
            # A close acknowledgment alone is insufficient: native teardown
            # still owns the worker until this operation completes.
            time.sleep(.08)
            unregistered.set()
            return 1
        api.UnregisterHotKey.side_effect = unregister
        finalized = []
        owner_thread = threading.get_ident()
        class Owner:
            def event(self, _value):
                pass
        owner = Owner()
        owner_ref = weakref.ref(owner, lambda _ref: finalized.append(threading.get_ident()))
        with patch('app_hotkeys.sys.platform', 'win32'), \
             patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            manager = WindowsHotkeys({'live': 'f8'}, owner.event, owner.event)
            owner = None
            try:
                self.assertTrue(registered.wait(1))
                self.assertTrue(manager.close())
                self.assertTrue(unregistered.is_set())
                self.assertFalse(manager.thread.is_alive())
                self.assertIsNone(owner_ref())
                self.assertEqual(finalized, [owner_thread])
                manager.update({'live': 'f9'})
                self.assertEqual(api.RegisterHotKey.call_count, 1)
            finally:
                manager.close()

    def test_slow_worker_reports_incomplete_shutdown_without_retaining_application(self):
        release = threading.Event()
        acknowledged = threading.Event()
        manager = WindowsHotkeys({}, lambda _value: None, lambda _value: None)
        class Owner:
            def event(self, _value):
                pass
        owner = Owner()
        reference = weakref.ref(owner)
        manager.callback = manager.report = owner.event
        owner = None
        def worker():
            _kind, _data, ready = manager.commands.get(timeout=2)
            ready.set()
            acknowledged.set()
            release.wait(4)
        manager.thread = threading.Thread(target=worker, daemon=True)
        manager.thread.start()
        try:
            self.assertFalse(manager.close())
            self.assertTrue(acknowledged.is_set())
            self.assertTrue(manager.thread.is_alive())
            self.assertIsNone(reference())
        finally:
            release.set()
            manager.thread.join(timeout=2)
        self.assertFalse(manager.thread.is_alive())

    def test_close_from_worker_does_not_join_or_wait_on_itself(self):
        manager = WindowsHotkeys({}, lambda _value: None, lambda _value: None)
        results = []
        manager.thread = threading.Thread(target=lambda: results.append(manager.close()))
        manager.thread.start()
        manager.thread.join(timeout=1)
        self.assertFalse(manager.thread.is_alive())
        self.assertEqual(results, [False])


if __name__ == '__main__':
    unittest.main()
