"""Launcher tests: windowless flow does not change model/engine environments."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
import unittest
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parents[1]
SOURCE = APP_DIR / 'start_gui.pyw'
SPEC = importlib.util.spec_from_loader('gui_startup_for_tests', loader=None)
startup = importlib.util.module_from_spec(SPEC)
startup.__file__ = str(SOURCE)
exec(compile(SOURCE.read_text(encoding='utf-8'), str(SOURCE), 'exec'), startup.__dict__)


class LauncherTests(unittest.TestCase):
    def test_default_launcher_is_vbs_and_requires_no_cmd(self):
        root = APP_DIR.parent
        vbs = (root / 'start_gui.vbs').read_text(encoding='ascii')
        self.assertIn('pyw.exe -3.11 -X utf8', vbs)
        self.assertIn('shell.Run command, 1, False', vbs)
        self.assertNotIn('cmd.exe /c', vbs)
        self.assertFalse((root / 'start_gui.bat').exists())

    def test_console_fallback_is_private(self):
        bat = (APP_DIR / 'tools' / 'start_gui_console.bat').read_text(encoding='ascii')
        self.assertIn('py -3.11 -X utf8', bat)
        self.assertIn('pause', bat)

    def test_launcher_uses_standard_library_bootstrap_not_pip(self):
        with patch('dnd_support.ensure_drag_drop', return_value=True) as installer:
            self.assertTrue(startup.ensure_drag_drop(sys.stdout))
            installer.assert_called_once()

    def test_failure_of_optional_dependency_does_not_prevent_gui(self):
        with patch('dnd_support.ensure_drag_drop', return_value=False):
            self.assertFalse(startup.ensure_drag_drop(sys.stdout))

    def test_missing_python_not_needed_for_pip(self):
        source=(APP_DIR/'start_gui.pyw').read_text(encoding='utf-8')
        self.assertNotIn('subprocess.run',source)
        self.assertIn('from dnd_support import ensure_drag_drop',source)

    def test_success_launch_uses_unified_gui_without_console(self):
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / 'startup.log'
            with patch.object(startup, 'startup_log_path', return_value=log_path), \
                 patch.object(startup, 'ensure_drag_drop') as dnd, \
                 patch.object(startup.runpy, 'run_path') as gui, \
                 patch.object(startup, 'show_launch_error') as error:
                rc = startup.main()
                self.assertEqual(rc, 0)
                gui.assert_called_once_with(str(APP_DIR / 'unified_gui.py'), run_name='__main__')
                dnd.assert_called_once()
                error.assert_not_called()
                self.assertIn('Python:', log_path.read_text(encoding='utf-8'))

    def test_gui_startup_failure_is_logged_and_shown(self):
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / 'startup.log'
            with patch.object(startup, 'startup_log_path', return_value=log_path), \
                 patch.object(startup, 'ensure_drag_drop'), \
                 patch.object(startup.runpy, 'run_path', side_effect=RuntimeError('simulated UI error')), \
                 patch.object(startup, 'show_launch_error') as error:
                self.assertEqual(startup.main(), 1)
                error.assert_called_once()
                self.assertIn('simulated UI error', log_path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
