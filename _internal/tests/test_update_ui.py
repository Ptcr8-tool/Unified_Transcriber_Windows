"""Update controller lifecycle and UI-thread boundaries without real networking."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config
import app_updates
import update_ui


class Root:
    def __init__(self):
        self.jobs = {}
        self.main_thread = threading.get_ident()
        self.next_id = 0

    def after(self, delay, callback):
        assert threading.get_ident() == self.main_thread, 'Tk used from worker'
        self.next_id += 1
        self.jobs[self.next_id] = (delay, callback)
        return self.next_id

    def after_cancel(self, job):
        assert threading.get_ident() == self.main_thread, 'Tk used from worker'
        self.jobs.pop(job, None)

    def winfo_children(self):
        assert threading.get_ident() == self.main_thread, 'Tk used from worker'
        return []


class ControllerTests(unittest.TestCase):
    def setUp(self):
        root = Root()
        self.app = SimpleNamespace(root=root, config=Config(), busy=False, closing=False,
                                   processes=SimpleNamespace(active=lambda:False),
                                   status=SimpleNamespace(set=Mock()), log=Mock(), close=Mock(), request_exit=Mock())
        self.controller = update_ui.UpdateController(self.app)
        self.save = patch.object(update_ui.core, 'save_config').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.controller.close)
        self.thread = patch.object(update_ui.threading, 'Thread', side_effect=self._thread).start()

    @staticmethod
    def _thread(target, **kwargs):
        # update_ui.threading is the shared threading module, so save the real
        # constructor outside the patch via ThreadFactory below.
        class Worker:
            def start(self):
                actual = REAL_THREAD(target=target, daemon=True)
                actual.start()
                actual.join(5)
                if actual.is_alive():
                    raise AssertionError('Worker did not finish')
        return Worker()

    def test_constructor_has_no_network_and_start_is_idempotent(self):
        self.thread.assert_not_called()
        self.assertFalse(self.app.root.jobs)
        self.controller.start()
        jobs = dict(self.app.root.jobs)
        self.controller.start()
        self.assertEqual(jobs, self.app.root.jobs)
        self.controller.close()
        self.assertFalse(self.app.root.jobs)

    def test_background_check_records_attempt_and_only_poll_touches_ui(self):
        worker_ids = []
        def latest(_version):
            worker_ids.append(threading.get_ident())
            return None
        with patch.object(app_updates, 'check_latest', side_effect=latest), \
             patch.object(update_ui.messagebox, 'showinfo') as show:
            self.assertTrue(self.controller.check(manual=True))
            self.assertTrue(self.controller.checking)
            show.assert_not_called()
            self.controller._poll()
            show.assert_called_once()
        self.assertNotEqual(worker_ids, [self.app.root.main_thread])
        self.assertFalse(self.controller.checking)
        self.assertTrue(self.app.config.update_last_checked.endswith('+00:00'))
        self.save.assert_called_once()

    def test_auto_private_or_offline_failure_is_nonmodal_manual_failure_is_explained(self):
        with patch.object(app_updates, 'check_latest', side_effect=app_updates.UpdateUnavailable('公開更新なし')), \
             patch.object(update_ui.messagebox, 'showinfo') as show:
            self.controller.check(manual=False)
            self.controller._poll()
            show.assert_not_called()
            self.app.log.assert_called_with('[更新] 公開更新なし\n')
            self.controller.check(manual=True)
            self.controller._poll()
            show.assert_called_once()

    def test_every_launch_starts_only_one_auto_check_after_manual_attempt(self):
        self.app.config = replace(self.app.config, update_check_mode='every')
        with patch.object(app_updates, 'check_latest', return_value=None) as check:
            self.controller.check(manual=False)
            self.controller._startup_check()
            self.controller._poll()
            self.controller._startup_check()
            check.assert_called_once()

    def test_new_version_waits_for_idle_then_appears_once(self):
        info = SimpleNamespace(version='2.2.0')
        self.app.busy = True
        self.controller.events.put(('checked', info, False))
        with patch.object(self.controller, '_show_available') as show:
            self.controller._poll()
            show.assert_not_called()
            self.app.busy = False
            self.controller._poll()
            self.controller._poll()
            show.assert_called_once_with(info)

    def test_closing_discards_network_results_and_stops_callbacks(self):
        self.controller.start()
        self.controller.close()
        with patch.object(app_updates, 'check_latest') as check:
            self.assertFalse(self.controller.check())
            self.controller._poll()
            check.assert_not_called()
        self.assertFalse(self.app.root.jobs)

    def test_busy_app_or_declined_ready_update_never_launches(self):
        update = SimpleNamespace(info=SimpleNamespace(version='2.2.0'), path=Path('/no-update/test.exe'))
        self.app.busy = True
        with patch.object(update_ui.messagebox, 'showinfo'), \
             patch.object(app_updates, 'launch_installer') as launch:
            self.controller._ready(update)
            launch.assert_not_called()
            self.app.close.assert_not_called()
            self.app.request_exit.assert_not_called()
        self.app.busy = False
        with patch.object(update_ui.messagebox, 'askyesno', return_value=False), \
             patch.object(app_updates, 'launch_installer') as launch:
            self.controller._ready(update)
            launch.assert_not_called()
            self.app.close.assert_not_called()
            self.app.request_exit.assert_not_called()

    def test_verified_launch_closes_app_only_after_success_and_uses_current_pid(self):
        update = SimpleNamespace(info=SimpleNamespace(version='2.2.0'), path=Path('/no-update/test.exe'))
        with patch.object(update_ui.messagebox, 'askyesno', return_value=True), \
             patch.object(app_updates, 'launch_installer') as launch:
            self.controller._ready(update)
            launch.assert_called_once_with(update, update_ui.core.BASE, wait_pid=update_ui.os.getpid())
            self.app.request_exit.assert_called_once()
            self.app.close.assert_not_called()
            self.assertTrue(self.controller.closed.is_set())

    def test_unverified_release_has_no_download_or_browser_side_effect(self):
        with patch.object(update_ui.messagebox, 'showerror'), \
             patch.object(app_updates, 'download_verified') as download, \
             patch.object(update_ui.webbrowser, 'open') as browser:
            self.assertFalse(self.controller.download(SimpleNamespace(has_provenance=False)))
            download.assert_not_called()
            browser.assert_not_called()
            bad = SimpleNamespace(version='2.2.0', tag='v2.2.0', release_url='https://evil.test')
            with self.assertRaises(app_updates.UpdateSecurityError):
                self.controller.open_release(bad)
            browser.assert_not_called()


REAL_THREAD = threading.Thread

if __name__ == '__main__':
    unittest.main()
