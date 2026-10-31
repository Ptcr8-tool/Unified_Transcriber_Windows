"""Installer safety and migration contracts using disposable directories."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installation as install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.target = self.folder / 'Sample App'

    def package(self, version='2.1.0', files=None, prefix=''):
        path = self.folder / ('package-' + version + '.zip')
        payload = {
            'VERSION': version + '\n', 'UnifiedTranscriber.exe': 'sample app',
            'Runtime/python.exe': 'sample python', 'Runtime/pythonw.exe': 'sample pythonw',
            '_internal/core.py': 'sample source',
        }
        payload.update(files or {})
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                # ZipInfo(string) rewrites Windows backslashes before writing.
                # Set wire names afterwards so malformed-name tests exercise
                # the received ZIP bytes on every host, including Windows.
                info = zipfile.ZipInfo()
                info.filename = info.orig_filename = prefix + name
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        return path

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def files(folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes()
                for p in folder.rglob('*') if p.is_file() and not p.is_symlink()}

    def test_fresh_install_receipt_and_portable_marker(self):
        package = self.package()
        result = install.install_package(package, self.target, '2.1.0', expected_sha256=self.digest(package))
        self.assertEqual(result.mode, 'install')
        info = install.inspect_installation(self.target, '2.2.0')
        self.assertEqual((info.state, info.version, info.actions), ('installed', '2.1.0', ('upgrade', 'cancel')))
        receipt = json.loads((self.target / install.RECEIPT_NAME).read_text())
        self.assertEqual(receipt['managed']['UnifiedTranscriber.exe'], hashlib.sha256(b'sample app').hexdigest())
        marker = json.loads((self.target / install.PORTABLE_MARKER).read_text())
        self.assertEqual(marker['format'], 'unified-transcriber-portable')
        self.assertEqual(marker['version'], 1)
        self.assertEqual(marker['settings_directory'], 'Data')

    def test_numeric_versions(self):
        self.assertGreater(install.version_tuple('2.10.0'), install.version_tuple('2.9.9'))
        for bad in ('2', '2.1', 'v2.1.0', '02.1.0', '2.1.0;whoami', '2.1.0-beta', '2.1.0\n', None, 2):
            with self.subTest(bad=bad), self.assertRaises(install.InstallationError):
                install.version_tuple(bad)

    def test_update_preserves_preferences_models_environments_and_recordings(self):
        install.install_package(self.package('2.0.0', {'_internal/obsolete.py': 'old'}), self.target, '2.0.0')
        preserved = {
            'Data/settings.json': b'{"theme_mode":"dark"}',
            'Models/cache/model.bin': b'downloaded model',
            '_internal/environments/faster-res/library.bin': b'existing environment',
            '_internal/gui_dependencies/custom.bin': b'existing optional package',
            '_internal/log/session.log': b'local diagnostic',
            'Recordings/sample.wav': b'RIFF sample', 'my-notes.txt': b'unmanaged notes',
        }
        for name, data in preserved.items():
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        result = install.install_package(self.package('2.1.0', {'_internal/core.py': 'updated code', 'Runtime/python.exe': 'updated runtime'}), self.target, '2.1.0')
        self.assertEqual(result.mode, 'upgrade')
        self.assertEqual((self.target / '_internal/core.py').read_text(), 'updated code')
        self.assertEqual((self.target / 'Runtime/python.exe').read_text(), 'updated runtime')
        self.assertFalse((self.target / '_internal/obsolete.py').exists())
        self.assertEqual({name: (self.target / name).read_bytes() for name in preserved}, preserved)

    def test_matching_version_requires_explicit_repair_and_repairs_corrupt_code(self):
        package = self.package()
        install.install_package(package, self.target, '2.1.0')
        (self.target / '_internal/core.py').write_text('damaged')
        with self.assertRaises(install.MaintenanceChoiceRequired):
            install.install_package(package, self.target, '2.1.0')
        result = install.install_package(package, self.target, '2.1.0', mode='repair')
        self.assertEqual(result.mode, 'repair')
        self.assertEqual((self.target / '_internal/core.py').read_text(), 'sample source')

    def test_newer_installation_never_silently_downgrades_for_repair(self):
        install.install_package(self.package('2.2.0'), self.target, '2.2.0')
        info = install.inspect_installation(self.target, '2.1.0')
        self.assertEqual(info.actions, ('repair', 'uninstall', 'cancel'))
        before = self.files(self.target)
        for mode in ('auto', 'install', 'upgrade', 'repair'):
            with self.subTest(mode=mode), self.assertRaises(install.MaintenanceChoiceRequired):
                install.install_package(self.package(), self.target, '2.1.0', mode=mode)
        self.assertEqual(self.files(self.target), before)

    def test_recognizes_previous_source_distribution_without_relabeling_unknown_folder(self):
        self.target.mkdir()
        (self.target / 'VERSION').write_text('2.0.0\n')
        (self.target / 'start_gui.pyw').write_text('sample launcher')
        (self.target / '_internal').mkdir()
        (self.target / '_internal/core.py').write_text('sample old source')
        (self.target / 'sample-recording.wav').write_bytes(b'RIFF sample')
        self.assertEqual(install.inspect_installation(self.target, '2.1.0').state, 'legacy')
        install.install_package(self.package(), self.target, '2.1.0')
        self.assertEqual((self.target / 'sample-recording.wav').read_bytes(), b'RIFF sample')

    def test_nonempty_unrecognized_folder_is_untouched(self):
        self.target.mkdir()
        (self.target / 'user.txt').write_text('keep')
        with self.assertRaises(install.InstallationError):
            install.install_package(self.package(), self.target, '2.1.0')
        self.assertEqual(self.files(self.target), {'user.txt': b'keep'})

    def test_git_source_archive_wrapper_is_normalized(self):
        package = self.package(prefix='Unified_Transcriber_Windows/')
        install.install_package(package, self.target, '2.1.0')
        self.assertTrue((self.target / 'UnifiedTranscriber.exe').is_file())
        self.assertFalse((self.target / 'Unified_Transcriber_Windows').exists())

    def test_bad_digest_does_not_create_destination(self):
        with self.assertRaises(install.InstallationError):
            install.install_package(self.package(), self.target, '2.1.0', expected_sha256='0' * 64)
        self.assertFalse(self.target.exists())

    def test_package_version_must_match_advertised_release(self):
        with self.assertRaises(install.InstallationError):
            install.validate_package(self.package('2.0.0'), '2.1.0')

    def test_package_requires_app_and_both_python_launchers(self):
        package = self.folder / 'incomplete.zip'
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('VERSION', '2.1.0')
        with self.assertRaises(install.InstallationError):
            install.validate_package(package, '2.1.0')

    def test_validation_is_read_only(self):
        package = self.package()
        before = self.files(self.folder)
        names = install.validate_package(package, '2.1.0', self.digest(package))
        self.assertIn('UnifiedTranscriber.exe', names)
        self.assertEqual(self.files(self.folder), before)

    def test_malicious_archive_names_are_rejected_before_writing(self):
        bad_names = ('../outside.txt', '/outside.txt', 'C:/outside.txt', r'folder\outside.txt',
                     'file.txt:stream', 'CON', 'aux.txt', 'LPT1.exe', 'COM¹.txt',
                     'folder/../outside.txt', 'folder//outside.txt', './outside.txt',
                     'folder/trailing.', 'folder/trailing ', 'a\x01b.txt', 'a?b.txt',
                     'nul\x00ignored.py', r'folder\..\outside.txt')
        for index, name in enumerate(bad_names):
            with self.subTest(name=name):
                package = self.package(files={name: 'malicious'})
                with zipfile.ZipFile(package) as archive:
                    self.assertIn(name, [info.orig_filename for info in archive.infolist()])
                target = self.folder / f'Invalid Package {index}'
                with self.assertRaises(install.InstallationError):
                    install.install_package(package, target, '2.1.0')
                self.assertFalse(target.exists())
                self.assertFalse((self.folder / 'outside.txt').exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 8.3 path identity')
    def test_short_and_long_installation_paths_have_one_canonical_identity(self):
        import ctypes
        from ctypes import wintypes
        self.target.mkdir()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        kernel.GetShortPathNameW.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetShortPathNameW(str(self.target), buffer, len(buffer))
        self.assertGreater(length, 0)
        self.assertLess(length, len(buffer))
        alias = Path(buffer.value)
        canonical = self.target.resolve()
        self.assertEqual(install._target_path(alias), canonical)
        self.assertEqual(install._target_path(self.target), canonical)
        maintenance = self.folder / 'maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        install.install_package(self.package(), alias, '2.1.0',
            extra_files={'UnifiedTranscriberSetup.exe': maintenance})
        self.assertEqual(install.get_verified_maintenance_path(alias), canonical / 'UnifiedTranscriberSetup.exe')
        self.assert_installation_locked(alias)

    def test_package_cannot_replace_private_data(self):
        names = ('Data/settings.json', 'Models/model.bin', 'Recordings/sample.wav',
                 '_internal/environments/faster-res/lib.dll', '_internal/log/session.log',
                 '_internal/gui_dependencies/extra.dll', 'settings.json', '.env.production',
                 '_internal/hf_token.txt', 'transcriber-settings-sample.json',
                 install.RECEIPT_NAME, install.PORTABLE_MARKER, '.git/config')
        for name in names:
            with self.subTest(name=name), self.assertRaises(install.InstallationError):
                install.validate_package(self.package(files={name: 'private'}), '2.1.0')

    def test_case_insensitive_duplicates_are_rejected(self):
        package = self.package(files={'Folder/file.py': 'one', 'folder/FILE.py': 'two'})
        with self.assertRaises(install.InstallationError):
            install.validate_package(package, '2.1.0')

    def test_file_cannot_be_ancestor_of_another_file(self):
        package = self.package(files={'folder': 'one', 'folder/child.py': 'two'})
        with self.assertRaises(install.InstallationError):
            install.validate_package(package, '2.1.0')

    def test_zip_symlink_is_rejected(self):
        package = self.package()
        with zipfile.ZipFile(package, 'a') as archive:
            link = zipfile.ZipInfo('outside-link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '../outside')
        with self.assertRaises(install.InstallationError):
            install.validate_package(package, '2.1.0')

    def test_unpack_size_and_file_count_limits_are_enforced(self):
        package = self.package()
        for limit in ('MAX_FILES', 'MAX_FILE_BYTES', 'MAX_UNPACKED_BYTES'):
            with self.subTest(limit=limit), patch.object(install, limit, 1), self.assertRaises(install.InstallationError):
                install.validate_package(package, '2.1.0')

    def test_archive_size_is_bounded(self):
        with patch.object(install, 'MAX_ARCHIVE_BYTES', 1), self.assertRaises(install.InstallationError):
            install.validate_package(self.package(), '2.1.0')

    def test_receipt_cannot_expand_managed_paths_into_user_data(self):
        install.install_package(self.package(), self.target, '2.1.0')
        receipt_path = self.target / install.RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text())
        receipt['managed']['../outside.txt'] = '0' * 64
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaises(install.InstallationError):
            install.uninstall_installation(self.target, remove_data=True)
        self.assertTrue(self.target.exists())

    def test_invalid_receipt_types_are_rejected_with_a_setup_error(self):
        install.install_package(self.package(), self.target, '2.1.0')
        receipt_path = self.target / install.RECEIPT_NAME
        original = json.loads(receipt_path.read_text())
        for field, value in (('version', 21), ('install_id', []), ('managed', [])):
            receipt = dict(original, **{field: value})
            receipt_path.write_text(json.dumps(receipt))
            with self.subTest(field=field), self.assertRaises(install.InstallationError):
                install.inspect_installation(self.target)

    def test_target_and_child_symlinks_never_redirect_installation(self):
        outside = self.folder / 'outside'
        outside.mkdir()
        target_link = self.folder / 'linked-app'
        try:
            target_link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Some Windows test accounts do not have symlink creation rights.
            return
        with self.assertRaises(install.InstallationError):
            install.install_package(self.package(), target_link, '2.1.0')
        install.install_package(self.package('2.0.0'), self.target, '2.0.0')
        core = self.target / '_internal/core.py'
        core.unlink()
        core.symlink_to(outside / 'keep.py')
        with self.assertRaises(install.InstallationError):
            install.install_package(self.package(), self.target, '2.1.0')
        self.assertFalse((outside / 'keep.py').exists())

    def test_replace_failure_restores_every_original_file(self):
        install.install_package(self.package('2.0.0'), self.target, '2.0.0')
        before = self.files(self.target)
        replace = os.replace
        def fail_new_file(source, destination):
            if '.unified-stage-' in str(source) and str(destination).endswith('new.py'):
                raise PermissionError('simulated locked file')
            return replace(source, destination)
        with patch.object(install.os, 'replace', side_effect=fail_new_file), self.assertRaises(PermissionError):
            install.install_package(self.package(files={'_internal/new.py': 'new'}), self.target, '2.1.0')
        self.assertEqual(self.files(self.target), before)
        self.assertFalse(list(self.folder.glob('.unified-rollback-*')))

    def test_failed_rollback_retains_backup_and_reports_recovery_directory(self):
        install.install_package(self.package('2.0.0'), self.target, '2.0.0')
        original = (self.target / 'UnifiedTranscriber.exe').read_bytes()
        replace = os.replace
        def fail_commit_and_recovery(source, destination):
            if '.unified-stage-' in str(source) and str(destination).endswith('new.py'):
                raise PermissionError('simulated commit failure')
            if '.unified-rollback-' in str(source) and str(destination).endswith('UnifiedTranscriber.exe'):
                raise PermissionError('simulated restoration failure')
            return replace(source, destination)
        with patch.object(install.os, 'replace', side_effect=fail_commit_and_recovery):
            with self.assertRaises(install.InstallationError) as error:
                install.install_package(self.package(files={'_internal/new.py': 'new'}), self.target, '2.1.0')
        backups = list(self.folder.glob('.unified-rollback-*'))
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), str(error.exception))
        self.assertEqual((backups[0] / 'UnifiedTranscriber.exe').read_bytes(), original)
        self.assertTrue((backups[0] / 'RECOVERY.json').is_file())
        self.assertEqual((self.target / 'VERSION').read_text(), '2.0.0\n')

    def test_source_change_between_validation_and_use_fails_digest_check(self):
        package = self.package()
        expected = self.digest(package)
        real_lock = install._installation_lock
        @contextmanager
        def change_after_validation(target):
            self.package(files={'_internal/core.py': 'substituted'})
            with real_lock(target):
                yield
        with patch.object(install, '_installation_lock', change_after_validation), self.assertRaises(install.InstallationError):
            install.install_package(package, self.target, '2.1.0', expected_sha256=expected)
        self.assertFalse(self.target.exists())

    def test_full_uninstall_deletes_selected_folder_and_keeps_external_files(self):
        install.install_package(self.package(), self.target, '2.1.0')
        (self.target / 'Data').mkdir()
        (self.target / 'Data/settings.json').write_text('{"theme_mode":"dark"}')
        outside = self.folder / 'outside.wav'
        outside.write_bytes(b'RIFF external')
        result = install.uninstall_installation(self.target, remove_data=True)
        self.assertTrue(result.removed_folder)
        self.assertFalse(result.data_preserved)
        self.assertFalse(self.target.exists())
        self.assertEqual(outside.read_bytes(), b'RIFF external')

    def test_uninstall_does_not_follow_child_directory_link(self):
        install.install_package(self.package(), self.target, '2.1.0')
        outside = self.folder / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        try:
            (self.target / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        install.uninstall_installation(self.target, remove_data=True)
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')

    def test_partial_uninstall_keeps_data_and_unmanaged_files(self):
        install.install_package(self.package(), self.target, '2.1.0')
        (self.target / 'Data').mkdir()
        (self.target / 'Data/settings.json').write_text('keep settings')
        (self.target / 'custom.txt').write_text('keep custom')
        result = install.uninstall_installation(self.target)
        self.assertTrue(result.data_preserved)
        self.assertFalse((self.target / 'UnifiedTranscriber.exe').exists())
        self.assertEqual((self.target / 'Data/settings.json').read_text(), 'keep settings')
        self.assertEqual((self.target / 'custom.txt').read_text(), 'keep custom')

    def test_arbitrary_folder_cannot_be_uninstalled(self):
        self.target.mkdir()
        (self.target / 'keep.txt').write_text('keep')
        with self.assertRaises(install.InstallationError):
            install.uninstall_installation(self.target, remove_data=True)
        self.assertTrue((self.target / 'keep.txt').exists())

    def test_root_and_home_are_not_install_targets(self):
        for path in (Path.home(), Path(self.folder.anchor)):
            with self.subTest(path=path), self.assertRaises(install.InstallationError):
                install.inspect_installation(path)

    def test_optional_windows_registration_failure_preserves_working_install(self):
        with patch.object(install, 'register_windows', side_effect=OSError('simulated registration failure')):
            result = install.install_package(self.package(), self.target, '2.1.0', register_program=True)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(install.inspect_installation(self.target).version, '2.1.0')

    def test_full_setup_copy_is_managed_without_recursive_packaging(self):
        setup = self.folder / 'sample-setup.exe'
        setup.write_bytes(b'MZ sample setup')
        install.install_package(self.package(), self.target, '2.1.0', extra_files={'UnifiedTranscriberSetup.exe': setup})
        receipt = json.loads((self.target / install.RECEIPT_NAME).read_text())
        self.assertEqual((self.target / 'UnifiedTranscriberSetup.exe').read_bytes(), setup.read_bytes())
        self.assertIn('UnifiedTranscriberSetup.exe', receipt['managed'])

    def assert_installation_locked(self, target):
        command = [sys.executable, '-c',
            'from pathlib import Path; import installation; '
            'installation.install_package(Path(__import__("sys").argv[1]),Path(__import__("sys").argv[2]),"2.1.0")',
            str(self.package()), str(target)]
        env = dict(os.environ, PYTHONPATH=str(Path(install.__file__).parent))
        with install._installation_lock(install._target_path(target)):
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('InstallationError', result.stderr)

    def test_cross_process_installation_lock_blocks_concurrent_operation(self):
        self.assert_installation_locked(self.target)
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
