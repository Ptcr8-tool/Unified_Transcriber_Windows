"""NSIS parent identity, isolated resources and maintenance compatibility."""
from contextlib import ExitStack, redirect_stdout
import ctypes
from dataclasses import FrozenInstanceError
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_context as context
import installer_ui as gui
from test_installer_ui import sample_package


class SetupContextTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.staged = self.root / 'Staged Setup 日本語'
        for name in ('Runtime/pythonw.exe', '_internal/installer_bootstrap.py',
                     'BUILD_INFO.json', 'payload.zip',
                     'Runtime/tcl/tcl8.6/init.tcl', 'Runtime/tcl/tk8.6/tk.tcl'):
            path = self.staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'sample setup fixture')
        self.origin = self.root / 'Downloaded Setup.exe'
        self.origin.write_bytes(b'MZ sample NSIS fixture')
        self.stack.enter_context(patch.object(context, '_context', None))
        self.stack.enter_context(patch.object(context, 'os', SimpleNamespace(
            name='nt', environ=os.environ, getppid=lambda: 12345,
            getpid=lambda: 12346, devnull=os.devnull)))
        self.stack.enter_context(patch.object(sys, 'executable', str(self.staged / 'Runtime/pythonw.exe')))
        self.stack.enter_context(patch.dict(os.environ, {
            'UNIFIED_SETUP_ORIGIN': str(self.origin), 'UNIFIED_SETUP_PARENT_PID': '12345',
            'UNIFIED_SETUP_COMMAND_LINE': '"sample setup.exe" --self-test',
            'TCL_LIBRARY': str(self.root / 'untrusted-tcl'),
            'TK_LIBRARY': str(self.root / 'untrusted-tk'), 'TCLLIBPATH': 'untrusted',
            'PYTHONPATH': 'untrusted', 'PYTHONHOME': 'untrusted',
            'PIP_INDEX_URL': 'https://untrusted.invalid', '_PYI_APPLICATION_HOME_DIR': 'untrusted'}))
        self.process = self.stack.enter_context(patch.object(context, 'process_image', return_value=self.origin))
        self.arguments = self.stack.enter_context(patch.object(context, 'command_line_arguments', return_value=('--self-test',)))

    def test_verified_context_is_immutable_consumed_and_isolates_tcl_before_tk(self):
        value = context.initialize_context(self.staged)
        self.assertEqual(value, context.SetupContext(self.origin, self.staged, 12345, ('--self-test',)))
        self.process.assert_called_once_with(12345)
        self.assertTrue(context.packaged_setup())
        self.assertEqual(context.setup_executable(), self.origin)
        with self.assertRaises(FrozenInstanceError):
            value.owner_pid = 1
        for name in ('UNIFIED_SETUP_ORIGIN', 'UNIFIED_SETUP_PARENT_PID', 'UNIFIED_SETUP_COMMAND_LINE',
                     'PYTHONPATH', 'PYTHONHOME', 'PIP_INDEX_URL', 'TCLLIBPATH', '_PYI_APPLICATION_HOME_DIR'):
            self.assertNotIn(name, os.environ)
        self.assertEqual(os.environ['TCL_LIBRARY'], str(self.staged / 'Runtime/tcl/tcl8.6'))
        self.assertEqual(os.environ['TK_LIBRARY'], str(self.staged / 'Runtime/tcl/tk8.6'))
        self.assertEqual(os.environ['PIP_CONFIG_FILE'], os.devnull)

    def test_forged_or_missing_parent_pid_never_queries_or_imports_installer(self):
        for pid in ('', '0', '-1', ' 12345', '+12345', '12346', '12347', '4294967296'):
            with self.subTest(pid=pid), patch.dict(os.environ, {
                    'UNIFIED_SETUP_ORIGIN': str(self.origin), 'UNIFIED_SETUP_PARENT_PID': pid,
                    'UNIFIED_SETUP_COMMAND_LINE': 'setup.exe --self-test'}):
                with self.assertRaises(RuntimeError):
                    context.initialize_context(self.staged)
        self.process.assert_not_called()
        self.arguments.assert_not_called()

    def test_forged_or_unqueryable_parent_is_rejected_before_argument_parsing(self):
        for result in (self.root / 'different.exe', RuntimeError('cannot query')):
            with self.subTest(result=str(result)), patch.dict(os.environ, {
                    'UNIFIED_SETUP_ORIGIN': str(self.origin), 'UNIFIED_SETUP_PARENT_PID': '12345',
                    'UNIFIED_SETUP_COMMAND_LINE': 'setup.exe --self-test'}):
                if isinstance(result, Exception):
                    self.process.side_effect = result
                else:
                    self.process.return_value = result
                with self.assertRaises(RuntimeError):
                    context.initialize_context(self.staged)
        self.arguments.assert_not_called()
        self.assertIsNone(context.get_context())

    def test_only_bundled_runtime_can_start_setup(self):
        with patch.object(sys, 'executable', str(self.root / 'external-pythonw.exe')):
            with self.assertRaises(RuntimeError):
                context.initialize_context(self.staged)
        self.process.assert_not_called()

    def test_missing_staged_tcl_or_payload_is_not_silently_used(self):
        (self.staged / 'Runtime/tcl/tcl8.6/init.tcl').unlink()
        with self.assertRaises(RuntimeError):
            context.initialize_context(self.staged)
        self.process.assert_not_called()

    def test_links_are_rejected_before_resolving(self):
        alias = self.root / 'Linked Setup'
        alias.symlink_to(self.staged, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            context.initialize_context(alias)
        self.process.assert_not_called()

    def test_resource_root_and_default_target_use_original_nsis_not_temp_python(self):
        (self.root / 'installation.json').write_text('{}')
        context.initialize_context(self.staged)
        with patch.object(sys, '_MEIPASS', str(self.root / 'untrusted'), create=True):
            self.assertEqual(context.resource_folder(Path('/fallback')), self.staged)
        self.assertEqual(gui.default_target(), self.root)

    def test_nsis_relaunch_uses_verified_parent_and_copies_complete_bootstrap(self):
        context.initialize_context(self.staged)
        copied = self.root.parent / (self.root.name + '-outside')
        copied.mkdir()
        self.addCleanup(lambda: __import__('shutil').rmtree(copied, ignore_errors=True))
        with patch.object(gui, 'os', SimpleNamespace(name='nt', getppid=lambda:12345)), \
             patch.object(gui.tempfile, 'mkdtemp', return_value=str(copied)), \
             patch.object(gui, 'reset_dll_search_path') as reset, \
             patch.object(gui.subprocess, 'Popen') as launch:
            self.assertTrue(gui.relaunch_outside(self.root, ['--uninstall']))
        command = launch.call_args.args[0]
        self.assertEqual(command[-2:], ['--wait-setup-pid', '12345'])
        self.assertEqual(Path(command[0]).read_bytes(), self.origin.read_bytes())
        self.assertFalse(launch.call_args.kwargs['shell'])
        self.assertNotIn('UNIFIED_SETUP_ORIGIN', launch.call_args.kwargs['env'])
        self.assertNotIn('PYINSTALLER_RESET_ENVIRONMENT', launch.call_args.kwargs['env'])
        reset.assert_called_once()

    def test_packaged_payload_self_test_reports_nsis_evidence_without_installing(self):
        context.initialize_context(self.staged)
        package = sample_package(self.root, gui.APP_VERSION)
        import hashlib
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        report = self.root / 'report.json'
        with patch.object(gui, 'resources', return_value=(package,digest)), \
             patch.object(gui, 'install_package') as install, redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-report', str(report)]), 0)
        result = json.loads(report.read_text())
        self.assertEqual(result['packaging'], 'nsis')
        self.assertTrue(result['setup_parent_verified'])
        self.assertEqual(result['setup_executable'], str(self.origin))
        self.assertEqual(result['setup_resource_root'], str(self.staged))
        self.assertEqual(result['setup_owner_pid'], 12345)
        install.assert_not_called()


class ProcessAndArgumentTests(unittest.TestCase):
    def test_parent_query_closes_full_width_handle_on_success_and_failure(self):
        handle = 0x123456789
        kernel = SimpleNamespace(OpenProcess=MagicMock(return_value=handle),
            QueryFullProcessImageNameW=MagicMock(), CloseHandle=MagicMock())
        def query(_handle, _flags, buffer, _length):
            buffer.value = str(Path('/sample-setup.exe').resolve())
            return True
        kernel.QueryFullProcessImageNameW.side_effect = query
        with patch.object(context, 'os', SimpleNamespace(name='nt')), \
             patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            self.assertEqual(context.process_image(12345), Path('/sample-setup.exe').resolve())
            self.assertEqual(kernel.OpenProcess.restype, ctypes.wintypes.HANDLE)
            kernel.QueryFullProcessImageNameW.side_effect = None
            kernel.QueryFullProcessImageNameW.return_value = False
            with self.assertRaises(RuntimeError):
                context.process_image(12345)
            kernel.OpenProcess.return_value = None
            with self.assertRaises(RuntimeError):
                context.process_image(12345)
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_full_length_unicode_command_line_is_parsed_once_and_freed(self):
        values = ('setup.exe', '--target', 'C:\\日本語 \\' + 'a' * 3000,
                  '--self-test-report', 'C:\\Sample\\quote"report.json')
        array = (ctypes.c_wchar_p * len(values))(*values)
        shell = SimpleNamespace(CommandLineToArgvW=MagicMock())
        kernel = SimpleNamespace(LocalFree=MagicMock())
        def parse(raw, count):
            ctypes.cast(count, ctypes.POINTER(ctypes.c_int)).contents.value = len(values)
            return array
        shell.CommandLineToArgvW.side_effect = parse
        with patch.object(ctypes, 'WinDLL', side_effect=[shell,kernel], create=True):
            self.assertEqual(context.command_line_arguments('"setup.exe" ' + 'a' * 5000), values[1:])
        shell.CommandLineToArgvW.assert_called_once()
        kernel.LocalFree.assert_called_once()

    def test_invalid_raw_command_line_is_rejected_without_windows_api(self):
        for raw in ('', 'a\x00b', 'a' * 32768):
            with self.subTest(raw=repr(raw[:20])), patch.object(ctypes, 'WinDLL', create=True) as api:
                with self.assertRaises(RuntimeError):
                    context.command_line_arguments(raw)
                api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
