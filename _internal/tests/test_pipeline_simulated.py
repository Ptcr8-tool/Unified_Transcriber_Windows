"""Simulated engine checks subprocess handling without any ML model downloads."""
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config
from unified_gui import UnifiedApp


class SimulatedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / '音声 テスト.wav'
        self.source.write_bytes(b'fake audio')
        self.dest = self.folder / 'transcript.txt'
        self.script = self.folder / 'fake.py'
        self.app = UnifiedApp.__new__(UnifiedApp)
        self.app.events = queue.Queue()
        self.app.stop_event = threading.Event()
        self.app.active_process = None
        self.app.process_lock = threading.Lock()

    def build(self, source, destination, config, python, device):
        return [sys.executable, '-u', str(self.script), str(destination)]

    def test_success_and_no_overwrite(self):
        self.script.write_text('''import sys
from pathlib import Path
Path(sys.argv[1]).write_text("[00:00:01] SPEAKER_00: OK\\n", encoding="utf-8-sig")
''', encoding='utf-8')
        with mock.patch('unified_gui.build_command', side_effect=self.build):
            status, _ = self.app._run_one(self.source, self.dest, Config(), Path(sys.executable), '', 'cpu')
            self.assertEqual(status, '完了')
            self.assertIn('SPEAKER_00', self.dest.read_text(encoding='utf-8-sig'))
            status, _ = self.app._run_one(self.source, self.dest, Config(), Path(sys.executable), '', 'cpu')
            self.assertEqual(status, 'スキップ')

    def test_error_preserves_original_text(self):
        self.dest.write_text('original', encoding='utf-8')
        self.script.write_text('raise SystemExit(7)', encoding='utf-8')
        with mock.patch('unified_gui.build_command', side_effect=self.build):
            status, _ = self.app._run_one(self.source, self.dest, Config(overwrite=True),
                                           Path(sys.executable), '', 'cpu')
        self.assertEqual(status, '失敗')
        self.assertEqual(self.dest.read_text(encoding='utf-8'), 'original')
        self.assertEqual(list(self.folder.glob('.*.processing.txt')), [])

    def test_cancel_before_commit_preserves_output(self):
        self.dest.write_text('original', encoding='utf-8')
        self.script.write_text('''import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text("new output")
print("worked", flush=True)
time.sleep(0.2)
''', encoding='utf-8')
        with mock.patch('unified_gui.build_command', side_effect=self.build):
            self.app.stop_event.set()
            result, _ = self.app._run_one(self.source, self.dest, Config(overwrite=True),
                                           Path(sys.executable), '', 'cpu')
        self.assertEqual(result, '中止')
        self.assertEqual(self.dest.read_text(encoding='utf-8'), 'original')


if __name__ == '__main__':
    unittest.main()
