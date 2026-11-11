"""Stop acknowledgements must not interrupt recording save or steal focus."""
from pathlib import Path
from types import SimpleNamespace
import ctypes
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shortcut_feedback as feedback
import windows_background as background
import test_v25_studio_ui as fixtures


def app_for(kind='live', language='ja'):
    app = SimpleNamespace(busy=kind != 'idle', live_active=kind == 'live',
        gamebar_active=kind == 'gamebar', live_stopping=False, closing=False,
        all_stop_requested=False, stop_event=threading.Event(),
        live_cancel=threading.Event(), gamebar_stop=threading.Event(),
        config=SimpleNamespace(ui_language=language), log=MagicMock(),
        status=MagicMock(), tray=SimpleNamespace(notify=MagicMock(return_value=True)),
        restore_from_tray=MagicMock(), stop_all=MagicMock())
    def stop():
        if app.busy:
            if app.live_active or app.gamebar_active:
                app.live_stopping = True
                if app.gamebar_active:
                    app.gamebar_stop.set()
            else:
                app.stop_event.set()
    app.stop = MagicMock(side_effect=stop)
    return app


class StopFeedbackTests(unittest.TestCase):
    def test_stop_happens_before_notification_and_final_processing_is_preserved(self):
        app = app_for()
        def notify(title, body):
            self.assertEqual(app.stop.call_count, 1)
            self.assertTrue(app.live_stopping)
            self.assertFalse(app.live_cancel.is_set())
            self.assertFalse(app.stop_event.is_set())
            self.assertIn('受け付けました', title)
            self.assertNotIn('保存しました', title)
            self.assertIn('続けます', body)
            return True
        app.tray.notify.side_effect = notify
        feedback.stop_with_feedback(app)
        app.tray.notify.assert_called_once()
        app.stop_all.assert_not_called()
        app.restore_from_tray.assert_not_called()

    def test_video_and_ordinary_processing_have_distinct_text(self):
        for kind, expected in [('gamebar', '録画停止'), ('process', '処理の停止')]:
            with self.subTest(kind=kind):
                app = app_for(kind)
                feedback.stop_with_feedback(app)
                self.assertIn(expected, app.tray.notify.call_args.args[0])
                app.stop.assert_called_once()
                app.stop_all.assert_not_called()

    def test_idle_has_no_false_stop_confirmation(self):
        app = app_for('idle')
        feedback.stop_with_feedback(app)
        self.assertEqual(app.tray.notify.call_args.args[0], '停止する処理はありません')
        self.assertFalse(app.stop_event.is_set())

    def test_repeated_shortcuts_are_throttled_without_escalation(self):
        app = app_for()
        with patch.object(feedback.time, 'monotonic', side_effect=[100, 100.1, 103]):
            feedback.stop_with_feedback(app)
            feedback.stop_with_feedback(app)
            self.assertEqual(app.tray.notify.call_count, 1)
            feedback.stop_with_feedback(app)
        self.assertEqual(app.tray.notify.call_count, 2)
        self.assertIn('終了処理中', app.tray.notify.call_args.args[0])
        app.stop_all.assert_not_called()
        self.assertFalse(app.live_cancel.is_set())
        self.assertFalse(app.stop_event.is_set())

    def test_a_new_recording_is_not_suppressed_by_previous_recording_throttle(self):
        app = app_for()
        with patch.object(feedback.time, 'monotonic', side_effect=[100, 100.1]):
            feedback.stop_with_feedback(app)
            app.live_stopping = False
            feedback.stop_with_feedback(app)
        self.assertEqual(app.tray.notify.call_count, 2)

    def test_english_notification_keeps_recognition_settings_unchanged(self):
        app = app_for(language='en')
        app.config.language = 'ja'
        feedback.stop_with_feedback(app)
        self.assertEqual(app.tray.notify.call_args.args[0], 'Recording stop requested')
        self.assertEqual(app.config.language, 'ja')

    def test_notification_failure_does_not_undo_normal_stop(self):
        for result in (False, RuntimeError('Explorer unavailable')):
            with self.subTest(result=str(result)):
                app = app_for()
                if isinstance(result, Exception):
                    app.tray.notify.side_effect = result
                else:
                    app.tray.notify.return_value = result
                feedback.stop_with_feedback(app)
                self.assertTrue(app.live_stopping)
                self.assertFalse(app.stop_event.is_set())
                app.restore_from_tray.assert_not_called()
                self.assertIn('通知', app.log.call_args.args[0])

    def test_failed_stop_never_announces_success(self):
        app = app_for()
        app.stop.side_effect = OSError('cannot signal recorder')
        feedback.stop_with_feedback(app)
        self.assertEqual(app.tray.notify.call_args.args[0], '停止要求に失敗しました')

    def test_closed_app_does_not_stop_or_notify(self):
        app = app_for()
        app.closing = True
        feedback.stop_with_feedback(app)
        app.stop.assert_not_called()
        app.tray.notify.assert_not_called()


class TrayNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tray = background.WindowsTray(MagicMock(), Path(sys.executable))

    def tearDown(self):
        self.tray.close()

    def test_non_windows_does_not_start_native_thread(self):
        with patch.object(background.sys, 'platform', 'linux'):
            self.assertFalse(self.tray.notify('Title', 'Body'))
        self.assertIsNone(self.tray._thread)
        self.assertIsNone(self.tray._take_notice())

    def test_notify_never_waits_for_shell_and_keeps_only_the_latest_message(self):
        with patch.object(self.tray, '_start_async', return_value=True), \
             patch.object(self.tray, 'start', side_effect=AssertionError('blocking start')):
            self.assertTrue(self.tray.notify('First', 'one'))
            self.assertTrue(self.tray.notify('Second', 'two'))
        _expires, title, body = self.tray._take_notice()
        self.assertEqual((title, body), ('Second', 'two'))
        self.assertIsNone(self.tray._take_notice())

    def test_ready_tray_receives_a_posted_message_not_a_direct_shell_call(self):
        user = SimpleNamespace(PostMessageW=MagicMock(return_value=1))
        self.tray._hwnd, self.tray._user = 42, user
        with patch.object(self.tray, '_start_async', return_value=True):
            self.assertTrue(self.tray.notify('Title', 'Body'))
        user.PostMessageW.assert_called_once_with(42, background._WM_NOTIFY, 0, 0)

    def test_utf16_limits_strip_nuls_and_keep_surrogate_pairs_intact(self):
        text = background.notification_text('録\0音' + '\U0001f3a4' * 100, 63)
        self.assertNotIn('\0', text)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 63)
        self.assertNotIn('\ufffd', text)

    def test_native_payload_is_silent_realtime_and_consumed_once(self):
        class Notice(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint32), ('hWnd', ctypes.c_void_p),
                ('uID', ctypes.c_uint), ('uFlags', ctypes.c_uint),
                ('szInfo', ctypes.c_wchar * 256), ('szInfoTitle', ctypes.c_wchar * 64),
                ('dwInfoFlags', ctypes.c_uint32)]
        shell = SimpleNamespace(Shell_NotifyIconW=MagicMock(return_value=1))
        with patch.object(self.tray, '_start_async', return_value=True), \
             patch.object(background.time, 'monotonic', return_value=100):
            self.tray.notify('Title', 'Body')
            self.assertTrue(self.tray._show_pending_notification(shell, Notice, 42))
            self.assertFalse(self.tray._show_pending_notification(shell, Notice, 42))
        operation, pointer = shell.Shell_NotifyIconW.call_args.args
        info = pointer._obj
        self.assertEqual(operation, 1)  # NIM_MODIFY, not a second icon
        self.assertEqual(info.hWnd, 42)
        self.assertEqual(info.uID, 1)
        self.assertEqual(info.uFlags, 0x10 | 0x40)  # NIF_INFO | NIF_REALTIME
        self.assertEqual(info.dwInfoFlags, 0x01 | 0x10 | 0x80)
        self.assertEqual((info.szInfoTitle, info.szInfo), ('Title', 'Body'))
        shell.Shell_NotifyIconW.assert_called_once()

    def test_expired_notification_is_discarded_not_replayed(self):
        with patch.object(self.tray, '_start_async', return_value=True), \
             patch.object(background.time, 'monotonic', return_value=100):
            self.tray.notify('Old title', 'Old body')
        shell = SimpleNamespace(Shell_NotifyIconW=MagicMock())
        with patch.object(background.time, 'monotonic', return_value=120):
            self.assertFalse(self.tray._show_pending_notification(shell, MagicMock(), 42))
        shell.Shell_NotifyIconW.assert_not_called()

    def test_close_discards_pending_notification_and_prevents_restart(self):
        with patch.object(self.tray, '_start_async', return_value=True):
            self.tray.notify('Title', 'Body')
        self.tray.close()
        with patch.object(background.threading, 'Thread') as thread:
            self.assertFalse(self.tray.notify('Late', 'body'))
            self.assertFalse(self.tray.start())
        thread.assert_not_called()
        self.assertIsNone(self.tray._take_notice())

    def test_failed_post_reports_not_queued(self):
        self.tray._hwnd = 42
        self.tray._user = SimpleNamespace(PostMessageW=MagicMock(return_value=0))
        with patch.object(self.tray, '_start_async', return_value=True):
            self.assertFalse(self.tray.notify('Title', 'Body'))
        self.assertIsNone(self.tray._take_notice())


class StopFeedbackGuiTests(fixtures.StudioGUIFixture):
    def test_shortcut_stop_notifies_without_restoring_hidden_window_or_cancelling_asr(self):
        a = self.app
        a._begin_work()
        a._set_busy(True)
        a.live_active = True
        a.live_stop_file = self.folder / 'stop-request'
        a.in_tray = True
        self.root.withdraw()
        self.root.update()
        with patch.object(a.tray, 'notify', return_value=True) as notify, \
             patch.object(a, 'restore_from_tray') as restore:
            a._handle_shortcut('stop')
        notify.assert_called_once()
        restore.assert_not_called()
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertTrue(a.live_stop_file.is_file())
        self.assertFalse(a.stop_event.is_set())
        self.assertFalse(a.live_cancel.is_set())
        self.assertFalse(a.all_stop_requested)
        self.assertTrue(a.top_stop_armed)
        a.live_active = False
        a._set_busy(False)

    def test_top_stop_and_lower_stop_do_not_emit_shortcut_notifications(self):
        a = self.app
        a._begin_work()
        a._set_busy(True)
        a.live_active = True
        a.live_stop_file = self.folder / 'stop-request'
        with patch.object(a.tray, 'notify') as notify:
            a.stop_from_top()
            a.stop()
        notify.assert_not_called()
        a.live_active = False
        a._set_busy(False)


if __name__ == '__main__':
    unittest.main()
