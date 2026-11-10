"""TOP stop must finalize recordings before the user explicitly cancels everything."""
from pathlib import Path
import json
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config
from live_config import LiveOptions
from unified_gui import UnifiedApp
from studio_ui import StudioView


def app_fixture():
    app = UnifiedApp.__new__(UnifiedApp)
    app.busy = True
    app.closing = False
    app.live_active = False
    app.gamebar_active = False
    app.gamebar_live_active = False
    app.live_stopping = False
    app.live_stop_file = None
    app.live_finalize_on_stop = False
    app.live_chunk_active = False
    app.stop_event = threading.Event()
    app.live_cancel = threading.Event()
    app.gamebar_stop = threading.Event()
    app.process_lock = threading.Lock()
    app.active_process = None
    app.processes = Mock()
    app.processes.active.return_value = []
    app.all_stop_requested = False
    app.all_stop_waiting = False
    app.top_stop_armed = False
    app.top_stop_ready_at = 0
    app.pending_speaker_edits = [Path('sample-transcript.txt')]
    app.pending_capture_target = 'live'
    app.watch_mode = False
    app.quit_when_idle = False
    app.events = queue.Queue()
    app.config = Config(device='cpu')
    app.stop_btn = Mock()
    app.ui = SimpleNamespace(stop_all_button=Mock(), refresh_top_stop=Mock())
    app.status = Mock()
    app.log = Mock()
    return app


class TopStopTests(unittest.TestCase):
    def test_live_first_click_keeps_final_asr_and_second_cancels_all(self):
        app = app_fixture()
        app.live_active = True
        with tempfile.TemporaryDirectory() as temp:
            app.live_stop_file = Path(temp) / 'stop'
            audio = Path(temp) / 'sample.wav'
            audio.write_bytes(b'previous recording')
            with patch('unified_gui.time.monotonic', return_value=100) as clock:
                app.stop_from_top()
                self.assertTrue(app.live_stop_file.is_file())
                self.assertTrue(app.live_stopping)
                self.assertFalse(app.stop_event.is_set())
                self.assertFalse(app.live_cancel.is_set())
                self.assertFalse(app.all_stop_requested)
                self.assertTrue(app.pending_speaker_edits)
                app.processes.stop_all.assert_not_called()
                clock.return_value = 100.2
                app.stop_from_top()
                self.assertFalse(app.all_stop_requested)
                clock.return_value = 100.7
                app.stop_from_top()
                self.assertTrue(app.all_stop_requested)
                self.assertTrue(app.stop_event.is_set())
                self.assertTrue(app.live_cancel.is_set())
                self.assertTrue(app.gamebar_stop.is_set())
                app.processes.stop_all.assert_called_once()
                app.stop_from_top()
                app.processes.stop_all.assert_called_once()
            self.assertEqual(audio.read_bytes(), b'previous recording')

    def test_gamebar_first_click_is_normal_recording_stop(self):
        app = app_fixture()
        app.gamebar_active = True
        app.gamebar_live_active = True
        app.stop_from_top()
        self.assertTrue(app.gamebar_stop.is_set())
        self.assertFalse(app.stop_event.is_set())
        self.assertFalse(app.live_cancel.is_set())
        app.processes.stop_all.assert_not_called()
        app.top_stop_ready_at = 0
        app.stop_from_top()
        app.processes.stop_all.assert_called_once()

    def test_nonrecording_job_uses_existing_stop_first(self):
        app = app_fixture()
        app.stop_from_top()
        self.assertTrue(app.stop_event.is_set())
        self.assertFalse(app.live_cancel.is_set())
        app.processes.stop_all.assert_not_called()
        self.assertTrue(app.pending_speaker_edits)
        app.top_stop_ready_at = 0
        app.stop_from_top()
        self.assertFalse(app.pending_speaker_edits)
        app.processes.stop_all.assert_called_once()

    def test_idle_is_noop_and_new_work_resets_escalation(self):
        app = app_fixture()
        app.busy = False
        app.stop_from_top()
        self.assertFalse(app.top_stop_armed)
        app.processes.stop_all.assert_not_called()
        app.busy = True
        app.live_active = True
        app.stop_from_top()
        app._begin_work()
        self.assertFalse(app.top_stop_armed)
        self.assertEqual(app.top_stop_ready_at, 0)
        app.stop_from_top()
        self.assertFalse(app.all_stop_requested)
        app.processes.stop_all.assert_not_called()

    def test_lower_stop_also_arms_explicit_top_escalation(self):
        app = app_fixture()
        app.live_active = True
        app.stop()
        self.assertTrue(app.top_stop_armed)
        self.assertFalse(app.live_cancel.is_set())
        app.top_stop_ready_at = 0
        app.stop_from_top()
        self.assertTrue(app.live_cancel.is_set())

    def test_registry_only_job_is_cooperative_before_all_stop(self):
        app = app_fixture()
        app.busy = False
        app.processes.active.return_value = [object()]
        app.stop_from_top()
        self.assertTrue(app.stop_event.is_set())
        app.processes.stop_all.assert_not_called()
        app.top_stop_ready_at = 0
        app.stop_from_top()
        app.processes.stop_all.assert_called_once()

    def test_explicit_all_stop_remains_immediate_for_exit(self):
        app = app_fixture()
        app.live_active = True
        app.stop_all()
        self.assertTrue(app.all_stop_requested)
        app.processes.stop_all.assert_called_once()

    def test_label_guard_idle_reset_and_english(self):
        app = app_fixture()
        button = Mock()
        view = SimpleNamespace(app=app, stop_all_button=button)
        app.live_active = True
        StudioView.refresh_top_stop(view)
        self.assertEqual(button.configure.call_args.kwargs['text'], '録音を停止')
        with patch('studio_ui.time.monotonic', return_value=100) as clock:
            app.stop_from_top()
            StudioView.refresh_top_stop(view)
            self.assertEqual(button.configure.call_args.kwargs['text'], 'すべて停止')
            self.assertEqual(button.configure.call_args.kwargs['state'], 'disabled')
            clock.return_value = 100.7
            StudioView.refresh_top_stop(view)
            self.assertEqual(button.configure.call_args.kwargs['state'], 'normal')
            app.config.ui_language = 'en'
            StudioView.refresh_top_stop(view)
            self.assertEqual(button.configure.call_args.kwargs['text'], 'Stop all')
        app.busy = False
        StudioView.refresh_top_stop(view)
        self.assertFalse(app.top_stop_armed)
        self.assertEqual(button.configure.call_args.kwargs['text'], 'Stop')
        self.assertEqual(button.configure.call_args.kwargs['state'], 'disabled')

    def test_normal_top_stop_reaches_post_recording_transcription(self):
        app = app_fixture()
        del app.processes
        app.live_active = True
        app._resolve_device = Mock(return_value='cpu')
        app._run_one = Mock(return_value=('完了', 'sample'))
        app._recognize_live_chunk = Mock()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            class Capture:
                def __init__(self, command, **kwargs):
                    destination = Path(command[command.index('--output') + 1])
                    with wave.open(str(destination), 'wb') as audio:
                        audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                        audio.writeframes(b'\x10\x00' * 480)
                    self.stdout = self.lines()
                def lines(self):
                    yield json.dumps({'event': 'started'}) + '\n'
                    app.stop_from_top()
                    yield json.dumps({'event': 'finished', 'seconds': .01}) + '\n'
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def wait(self, **kwargs): return 0
            with patch('unified_gui.subprocess.Popen', Capture):
                app._live_worker(LiveOptions(mode='record', post_transcribe=True,
                    recording_channels=1, folder=folder), Path(sys.executable),
                    Path(sys.executable), Config(device='cpu', output_dir=str(folder)), '')
            app._run_one.assert_called_once()
            self.assertEqual(len(list(folder.glob('*.wav'))), 1)
            self.assertFalse(app.stop_event.is_set())
            self.assertFalse(app.live_cancel.is_set())


if __name__ == '__main__':
    unittest.main()
