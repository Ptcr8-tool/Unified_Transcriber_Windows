"""Single GUI instance handling without requiring a display or real hotkeys."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import single_instance as si


class FakeGuard:
    def __init__(self, acquire_results, windows=(), activation=True):
        self.acquisitions = iter(acquire_results)
        self.windows = iter(windows)
        self.activated = []
        self.activation = activation
        self.closed = 0
        self.notified = 0

    def acquire(self):
        return next(self.acquisitions)

    def find_window(self):
        return next(self.windows, None)

    def activate(self, window):
        self.activated.append(window)
        return self.activation

    def close(self):
        self.closed += 1

    def notify_wait_timeout(self):
        self.notified += 1


class InstanceGateTests(unittest.TestCase):
    def test_first_launch_keeps_the_lock(self):
        guard = FakeGuard([True])
        with patch.object(si.sys, 'platform', 'win32'):
            self.assertIs(si.enter_instance(factory=lambda: guard), guard)
        self.assertEqual(guard.closed, 0)

    def test_second_launch_activates_existing_window(self):
        guard = FakeGuard([False], ['existing'])
        with patch.object(si.sys, 'platform', 'win32'):
            self.assertIsNone(si.enter_instance(factory=lambda: guard))
        self.assertEqual(guard.activated, ['existing'])
        self.assertEqual(guard.closed, 1)
        self.assertEqual(guard.notified, 0)

    def test_second_launch_takes_over_if_first_exits_during_startup(self):
        guard = FakeGuard([False, False, True], [None, None])
        with patch.object(si.sys, 'platform', 'win32'):
            self.assertIs(si.enter_instance(factory=lambda: guard, sleep=lambda _: None), guard)
        self.assertEqual(guard.closed, 0)
        self.assertEqual(guard.activated, [])

    def test_stale_window_does_not_prevent_takeover(self):
        guard = FakeGuard([False, True], ['stale'], activation=False)
        with patch.object(si.sys, 'platform', 'win32'):
            self.assertIs(si.enter_instance(factory=lambda: guard), guard)
        self.assertEqual(guard.activated, ['stale'])
        self.assertEqual(guard.closed, 0)

    def test_no_duplicate_window_when_first_launch_has_not_created_window(self):
        guard = FakeGuard([False])
        with patch.object(si.sys, 'platform', 'win32'):
            self.assertIsNone(si.enter_instance(factory=lambda: guard, timeout=0))
        self.assertEqual(guard.closed, 1)
        self.assertEqual(guard.notified, 1)

    def test_non_windows_does_not_use_native_lock(self):
        with patch.object(si.sys, 'platform', 'linux'):
            guard = si.enter_instance(factory=lambda: self.fail('Windows API unexpectedly used'))
        guard.attach(None)
        guard.close()


@unittest.skipUnless(sys.platform == 'win32', 'Windows kernel API')
class WindowsInstanceTests(unittest.TestCase):
    def test_mutex_releases_on_close_and_process_termination(self):
        name = r'Local\UnifiedTranscriber.Test.' + uuid.uuid4().hex
        first = si.WindowsInstance(mutex_name=name)
        second = si.WindowsInstance(mutex_name=name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
            second.close()
            code = ('import os, single_instance; '
                    f'g=single_instance.WindowsInstance(mutex_name={name!r}); '
                    'assert g.acquire(); os._exit(0)')
            subprocess.run([sys.executable, '-c', code], check=True, cwd=os.path.dirname(si.__file__))
            self.assertTrue(first.acquire())
        finally:
            first.close()
            second.close()

    def test_real_tk_window_can_be_identified_by_second_process(self):
        import tkinter as tk
        name = r'Local\UnifiedTranscriber.Test.' + uuid.uuid4().hex
        prop = 'UnifiedTranscriber.Test.' + uuid.uuid4().hex
        first = si.WindowsInstance(mutex_name=name, property_name=prop)
        root = tk.Tk()
        try:
            self.assertTrue(first.acquire())
            first.attach(root)
            code = ('import single_instance; '
                    f'g=single_instance.WindowsInstance(mutex_name={name!r}, property_name={prop!r}); '
                    'assert not g.acquire(); w=g.find_window(); assert w; assert g.activate(w); g.close()')
            subprocess.run([sys.executable, '-c', code], check=True,
                           cwd=os.path.dirname(si.__file__), timeout=15)
        finally:
            first.close()
            root.destroy()

    def test_second_launch_restores_a_withdrawn_tk_window(self):
        import tkinter as tk
        name = r'Local\UnifiedTranscriber.Test.' + uuid.uuid4().hex
        prop = 'UnifiedTranscriber.Test.' + uuid.uuid4().hex
        first = si.WindowsInstance(mutex_name=name, property_name=prop)
        root = tk.Tk()
        try:
            self.assertTrue(first.acquire())
            first.attach(root)
            first.set_restore(root.deiconify)
            root.withdraw()
            code = ('import single_instance; '
                    f'g=single_instance.WindowsInstance(mutex_name={name!r}, property_name={prop!r}); '
                    'assert not g.acquire(); w=g.find_window(); assert w; assert g.activate(w); g.close()')
            subprocess.run([sys.executable, '-c', code], check=True,
                           cwd=os.path.dirname(si.__file__), timeout=15)
            deadline = time.monotonic() + 2
            while root.state() == 'withdrawn' and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            self.assertEqual(root.state(), 'normal')
        finally:
            first.close()
            root.destroy()


if __name__ == '__main__':
    unittest.main()
