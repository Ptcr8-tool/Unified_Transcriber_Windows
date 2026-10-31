"""Startup and runtime logs share the configured user output folder."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostic_logs

APP = Path(__file__).resolve().parents[1]

class TestUserLogLocation(unittest.TestCase):
    def test_log_location_is_selected_user_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            expected = Path(temp) / 'Documents' / 'UnifiedTranscriber' / 'Logs'
            with patch('diagnostic_logs.default_directory', return_value=expected):
                self.assertEqual(diagnostic_logs.log_directory(), expected)
                self.assertTrue(expected.is_dir())

    def test_startup_and_session_same_folder(self):
        source = APP / 'start_gui.pyw'
        spec = importlib.util.spec_from_loader('log_location_launcher', loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(source)
        exec(compile(source.read_text(encoding='utf8'), str(source), 'exec'), module.__dict__)
        with tempfile.TemporaryDirectory() as tmp:
            with patch('diagnostic_logs.log_directory', return_value=Path(tmp)):
                session = diagnostic_logs.SessionLogs('secret')
                session.write('sample')
                session.error('error secret')
                self.assertEqual(session.folder, Path(tmp))
                self.assertIn('sample', session.session_path.read_text(encoding='utf8'))
                self.assertNotIn('secret', session.error_path.read_text(encoding='utf8'))
            with patch.object(module, 'log_directory', return_value=Path(tmp)):
                self.assertEqual(module.startup_log_path(), Path(tmp) / 'gui_startup.log')

    def test_startup_directory_unwritable_shows_clear_error(self):
        source = APP / 'start_gui.pyw'
        spec = importlib.util.spec_from_loader('log_location_launcher_failure', loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(source)
        exec(compile(source.read_text(encoding='utf8'), str(source), 'exec'), module.__dict__)
        with patch.object(module, 'startup_log_path', side_effect=PermissionError('denied')):
            with patch.object(module, 'show_launch_error') as show:
                self.assertEqual(module.main(), 1)
                self.assertIn('ログフォルダー', show.call_args.args[1])

if __name__ == '__main__':
    unittest.main()
