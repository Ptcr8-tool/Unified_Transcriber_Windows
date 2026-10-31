"""Regression checks for preserving data when operations fail or race."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import installation
from external_recording import ExternalOptions, finalize_external


class UninstallRecoveryTests(unittest.TestCase):
    def receipt(self, target):
        target.mkdir()
        data = dict(format=installation.RECEIPT_FORMAT, app_id=installation.APP_ID,
                    install_id='a' * 32, version='2.1.3', managed={})
        receipt = target / installation.RECEIPT_NAME
        receipt.write_text(json.dumps(data), encoding='utf-8')
        return receipt, receipt.read_bytes()

    def test_failed_file_removal_keeps_receipt_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            # Windows TEMP may use an 8.3 short name. uninstall_installation
            # canonicalizes the path, so the injected failure must match the
            # same resolved path (and actually exercise the recovery branch).
            target = (Path(temp) / 'app').resolve()
            receipt, original = self.receipt(target)
            locked = target / 'locked.bin'
            locked.write_bytes(b'user data')
            original_unlink = Path.unlink

            def fail_locked(path, *args, **kwargs):
                if path == locked:
                    raise PermissionError('file is in use')
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, 'unlink', fail_locked):
                with self.assertRaises(PermissionError):
                    installation.uninstall_installation(target, remove_data=True)
            self.assertEqual(receipt.read_bytes(), original)
            self.assertTrue(locked.exists())
            installation.uninstall_installation(target, remove_data=True)
            self.assertFalse(target.exists())

    def test_failed_final_directory_removal_restores_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            target = (Path(temp) / 'app').resolve()
            receipt, original = self.receipt(target)
            original_rmdir = Path.rmdir
            failed = False

            def fail_once(path):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    raise PermissionError('directory is in use')
                return original_rmdir(path)

            with patch.object(Path, 'rmdir', fail_once):
                with self.assertRaises(PermissionError):
                    installation.uninstall_installation(target, remove_data=True)
            self.assertTrue(failed, 'the final directory failure must be injected')
            self.assertEqual(receipt.read_bytes(), original)
            installation.uninstall_installation(target, remove_data=True)
            self.assertFalse(target.exists())


class ExternalRecordingRaceTests(unittest.TestCase):
    def test_a_destination_created_during_mux_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video, audio, dest = root / 'source.mp4', root / 'mic.wav', root / 'result.mp4'
            video.write_bytes(b'video')
            audio.write_bytes(b'audio')

            class FakeMux:
                returncode = 0
                def __init__(self, command, **kwargs):
                    Path(command[-1]).write_bytes(b'complete mux' * 20)
                    dest.write_bytes(b'file made by another process')
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def communicate(self, timeout=None): return '', ''

            with patch('external_recording.video_duration', return_value=1), \
                 patch('external_recording.subprocess.Popen', FakeMux):
                with self.assertRaises(FileExistsError):
                    finalize_external(ExternalOptions(video, audio, destination=dest),
                                      'ffmpeg', has_audio=lambda *args: False)
            self.assertEqual(dest.read_bytes(), b'file made by another process')
            self.assertEqual(video.read_bytes(), b'video')
            self.assertEqual(audio.read_bytes(), b'audio')


class ImportedPythonTrustTests(unittest.TestCase):
    def test_absolute_path_tag_rejects_relative_and_invalid_values(self):
        from settings_transfer import decode_path
        for value in ('../escape', 'relative/file', 'bad\0path', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_path({'base': 'absolute', 'path': value}, '/app', '/home', {})
        windows = r'C:\\Users\\Sample\\output'
        self.assertEqual(decode_path({'base': 'absolute', 'path': windows}, '/app', '/home', {}), windows)
        self.assertEqual(decode_path({'base': 'absolute', 'path': '/other/output'}, '/app', '/home', {}), '/other/output')

    def test_untrusted_override_is_not_applied_without_confirmation(self):
        from core import Config
        from settings_ui import confirm_imported_python_overrides
        current = Config()
        imported = replace(current, python_overrides={'faster-res': 'python.exe'},
                           theme_mode='dark')
        with patch('settings_ui.messagebox.askyesno', return_value=False) as prompt:
            result, kept = confirm_imported_python_overrides(imported, current, None)
        self.assertTrue(kept)
        self.assertEqual(result.python_overrides, {})
        self.assertEqual(result.theme_mode, 'dark')
        prompt.assert_called_once()
        self.assertIn('python.exe', prompt.call_args.args[1])
        self.assertEqual(prompt.call_args.kwargs['default'], 'no')

    def test_explicitly_confirmed_override_is_preserved(self):
        from core import Config
        from settings_ui import confirm_imported_python_overrides
        current = Config()
        imported = replace(current, python_overrides={'faster-res': 'python.exe'})
        with patch('settings_ui.messagebox.askyesno', return_value=True):
            result, kept = confirm_imported_python_overrides(imported, current, None)
        self.assertFalse(kept)
        self.assertEqual(result.python_overrides, imported.python_overrides)


class SystemProcessPathTests(unittest.TestCase):
    def test_taskkill_is_resolved_using_windows_system_directory(self):
        import ctypes
        from process_control import _windows_taskkill_path
        class SystemDirectory:
            def __call__(self, buffer, size):
                buffer.value = 'trusted-system-dir'
                return len(buffer.value)
        class Kernel:
            GetSystemDirectoryW = SystemDirectory()
        with patch.object(ctypes, 'WinDLL', return_value=Kernel(), create=True):
            result = _windows_taskkill_path()
        self.assertEqual(Path(result), Path('trusted-system-dir') / 'taskkill.exe')


class InvalidCliInputTests(unittest.TestCase):
    def test_invalid_configuration_returns_two_without_traceback(self):
        import cli
        from core import Config
        from io import StringIO
        current = Config()
        with patch('cli.load_config', return_value=current), \
             patch('sys.stderr', new_callable=StringIO) as stderr:
            result = cli.main(['--segment-length', '0', 'sample.wav'])
        self.assertEqual(result, 2)
        self.assertIn('設定が不正です', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
