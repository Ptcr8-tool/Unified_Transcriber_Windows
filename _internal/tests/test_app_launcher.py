"""Launcher isolation, diagnostics and explicit runtime entry dispatch."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app_launcher as launcher
import launch_runtime


class AppLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'Sample App & audio'
        (self.folder/'Runtime').mkdir(parents=True)
        (self.folder/'_internal').mkdir()
        for name in ('Runtime/python.exe','Runtime/pythonw.exe','_internal/launch_runtime.py'):
            (self.folder/name).write_text('sample executable or script')

    def test_normal_launch_uses_fixed_bundled_python_isolated_mode_and_no_shell(self):
        with patch.object(launcher,'application_root',return_value=self.folder), \
             patch.object(launcher,'reset_dll_search') as reset, \
             patch.object(launcher.subprocess,'Popen') as launch, \
             patch.dict(os.environ,{'PYTHONPATH':'untrusted-modules','_PYI_APPLICATION_HOME_DIR':'untrusted-bundle'}):
            self.assertEqual(launcher.main([]),0)
        command=launch.call_args.args[0]
        self.assertEqual(command,[str(self.folder/'Runtime/pythonw.exe'),'-I','-X','utf8',str(self.folder/'_internal/launch_runtime.py')])
        self.assertFalse(launch.call_args.kwargs['shell'])
        self.assertEqual(launch.call_args.kwargs['cwd'],str(self.folder))
        self.assertEqual(launch.call_args.kwargs['stdin'],subprocess.DEVNULL)
        env=launch.call_args.kwargs['env']
        self.assertNotIn('PYTHONPATH',env)
        self.assertNotIn('_PYI_APPLICATION_HOME_DIR',env)
        reset.assert_called_once()

    def test_report_path_alone_selects_console_runtime_self_test(self):
        report=self.folder/'self-test.json'
        result=SimpleNamespace(returncode=0,stdout='{"portable":true}',stderr='')
        with patch.object(launcher,'application_root',return_value=self.folder), \
             patch.object(launcher,'reset_dll_search'), patch.object(launcher.subprocess,'run',return_value=result) as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--self-test-report',str(report)]),0)
        command=run.call_args.args[0]
        self.assertEqual(command[0],str(self.folder/'Runtime/python.exe'))
        self.assertEqual(command[-1],'--self-test')
        self.assertTrue(json.loads(report.read_text())['ok'])
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertEqual(run.call_args.kwargs['timeout'],45)

    def test_missing_runtime_produces_failure_report_without_launch(self):
        (self.folder/'Runtime/python.exe').unlink()
        report=self.folder/'self-test.json'
        with patch.object(launcher,'application_root',return_value=self.folder), \
             patch.object(launcher.subprocess,'run') as run,redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main(['--self-test','--self-test-report',str(report)]),1)
        self.assertFalse(json.loads(report.read_text())['ok'])
        run.assert_not_called()

    def test_self_test_timeout_is_reported_as_failure(self):
        report=self.folder/'self-test.json'
        with patch.object(launcher,'application_root',return_value=self.folder),patch.object(launcher,'reset_dll_search'), \
             patch.object(launcher.subprocess,'run',side_effect=subprocess.TimeoutExpired('sample-runtime',45)), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main(['--self-test-report',str(report)]),1)
        self.assertFalse(json.loads(report.read_text())['ok'])

    def test_invalid_report_destination_returns_error_without_recursive_exception(self):
        report=self.folder/'missing-directory/report.json'
        result=SimpleNamespace(returncode=0,stdout='ok',stderr='')
        with patch.object(launcher,'application_root',return_value=self.folder), patch.object(launcher,'reset_dll_search'), \
             patch.object(launcher.subprocess,'run',return_value=result),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main(['--self-test-report',str(report)]),1)

    def test_runtime_entry_import_has_no_gui_side_effects_and_dispatches_fixed_script(self):
        with patch.object(sys,'path',sys.path.copy()), patch.object(launch_runtime.runpy,'run_path') as run:
            self.assertEqual(launch_runtime.main([]),0)
        run.assert_called_once_with(str(Path(launch_runtime.__file__).resolve().parent/'start_gui.pyw'),run_name='__main__')

    def test_runtime_self_test_always_destroys_the_tk_window(self):
        window=MagicMock()
        with patch.object(sys,'path',sys.path.copy()), patch('tkinter.Tk',return_value=window),redirect_stdout(io.StringIO()):
            self.assertEqual(launch_runtime.main(['--self-test']),0)
        window.withdraw.assert_called_once()
        window.destroy.assert_called_once()
        window.update.side_effect=RuntimeError('sample Tk failure')
        window.destroy.reset_mock()
        with patch.object(sys,'path',sys.path.copy()), patch('tkinter.Tk',return_value=window), self.assertRaises(RuntimeError):
            launch_runtime.main(['--self-test'])
        window.destroy.assert_called_once()

    def test_unrecognized_runtime_arguments_do_not_start_the_gui(self):
        with patch.object(launch_runtime.runpy,'run_path') as run,redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            launch_runtime.main(['--unexpected-script','sample.py'])
        run.assert_not_called()


if __name__=='__main__':
    unittest.main()
