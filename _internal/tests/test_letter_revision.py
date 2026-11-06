"""Lettered revision labels, numeric PE metadata and safe update ordering."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import zipfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / 'tools'))
import installation
import app_updates
import native_launcher


class RevisionVersionTests(unittest.TestCase):
    def test_revision_order_matches_installation_and_update_checks(self):
        versions = ['0.9.9', '1.0.0', '1.0.0-a', '1.0.0-b', '1.0.0-z', '1.0.1', '2.0.0']
        keys = [installation.version_tuple(value) for value in versions]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(installation.version_tuple('1.0.0-a'), (1, 0, 0, 1))
        for value in versions:
            self.assertEqual(installation.version_tuple(value), app_updates.parse_installed_version(value))

    def test_only_one_lowercase_letter_is_accepted(self):
        for value in ('1.0.0-', '1.0.0-aa', '1.0.0-A', '1.0.0-a1', '1.0.0-a\n',
                      '1.0.0-a;calc', '1.0.0-α', '01.0.0-a', 'v1.0.0-a', None, 1):
            with self.subTest(value=value):
                with self.assertRaises(installation.InstallationError):
                    installation.version_tuple(value)
                with self.assertRaises(app_updates.UpdateSecurityError):
                    app_updates.parse_installed_version(value)

    def test_public_release_validation_remains_strict(self):
        self.assertEqual(app_updates.parse_version('1.0.1'), (1, 0, 1))
        for value in ('1.0.0-a', '1.0.0-rc1', '1.0.0+build'):
            with self.assertRaises(app_updates.UpdateSecurityError):
                app_updates.parse_version(value)

    def test_update_check_never_downgrades_revision_to_old_base(self):
        from test_app_updates import release
        data = release()
        data.update(tag_name='v1.0.0', html_url=app_updates.RELEASE_BASE + 'tag/v1.0.0')
        self.assertIsNone(app_updates.parse_release(data, '1.0.0-a'))
        self.assertEqual(app_updates.parse_release(release(), '1.0.0-a').version, '2.1.0')

    def test_launcher_metadata_separates_display_and_numeric_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            icon = root / 'icon.ico'
            icon.write_bytes(b'test icon')
            native_launcher.write_project(root / 'native', '1.0.0-a', icon, 'v143')
            header = (root / 'native/native_launcher_version.h').read_text()
            self.assertIn('UNIFIED_VERSION_TUPLE 1,0,0,1', header)
            self.assertIn('UNIFIED_VERSION_STRING "1.0.0-a"', header)
            self.assertIn('VIProductVersion "${NUMERIC_VERSION}"',
                          (CODE / 'tools/installer_bootstrap.nsi').read_text())

    @staticmethod
    def package(root, version):
        path = root / ('package-' + version + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in {
                'VERSION': version + '\n', 'UnifiedTranscriber.exe': 'test launcher',
                'Runtime/python.exe': 'test runtime', 'Runtime/pythonw.exe': 'test runtime',
                '_internal/core.py': "APP_VERSION = '" + version + "'\n",
            }.items():
                archive.writestr(name, data)
        return path

    def test_upgrade_from_base_retains_settings_and_supports_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'App'
            old = self.package(root, '1.0.0')
            new = self.package(root, '1.0.0-a')
            installation.install_package(old, target, '1.0.0')
            settings = target / 'Data/settings.json'
            settings.parent.mkdir(exist_ok=True)
            settings.write_text('{"txt_blank_lines":2}', encoding='utf-8')
            info = installation.inspect_installation(target, '1.0.0-a')
            self.assertIn('upgrade', info.actions)
            installation.install_package(new, target, '1.0.0-a', mode='upgrade')
            installation.install_package(new, target, '1.0.0-a', mode='repair')
            self.assertEqual(json.loads(settings.read_text()), {'txt_blank_lines': 2})
            self.assertEqual(installation.inspect_installation(target).version, '1.0.0-a')
            with self.assertRaises(installation.InstallationError):
                installation.install_package(old, target, '1.0.0', mode='repair')

    def test_a_to_b_upgrade_keeps_settings_and_detects_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'App'
            installation.install_package(self.package(root, '1.0.0-a'), target, '1.0.0-a')
            settings = target / 'Data/settings.json'
            settings.parent.mkdir(exist_ok=True)
            settings.write_bytes(b'{"theme_mode":"dark"}')
            installation.install_package(self.package(root, '1.0.0-b'), target, '1.0.0-b', mode='upgrade')
            self.assertEqual(settings.read_bytes(), b'{"theme_mode":"dark"}')
            self.assertEqual(installation.inspect_installation(target).version, '1.0.0-b')

    def test_refuses_to_overwrite_newer_numeric_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'App'
            installation.install_package(self.package(root, '1.0.1'), target, '1.0.1')
            with self.assertRaises(installation.InstallationError):
                installation.install_package(self.package(root, '1.0.0-a'), target, '1.0.0-a', mode='upgrade')
            self.assertEqual(installation.inspect_installation(target).version, '1.0.1')
