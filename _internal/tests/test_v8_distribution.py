"""Clean installation, private layout, and no-focus-stealing regression tests."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from core import BASE, CODE_DIR, environment_folder, app_config_path, BY_ID
import unified_gui


class V8DistributionTests(unittest.TestCase):
    def test_clean_layout_only_launcher_docs_visible(self):
        self.assertTrue({'start_gui.vbs', 'start_gui.pyw', 'README.md', '_internal'}
                        <= {p.name for p in BASE.iterdir()})
        self.assertTrue((CODE_DIR / 'tools' / 'repair_dnd.bat').is_file())
        self.assertTrue((CODE_DIR / 'docs' / 'CHANGELOG.md').is_file())

    def test_environment_private_for_all_profiles(self):
        for key in BY_ID:
            with self.subTest(key=key):
                self.assertEqual(environment_folder(key), CODE_DIR / 'environments' / key)

    def test_distinct_config_storage_from_legacy(self):
        with patch.dict(os.environ, {'APPDATA': '/tmp/fake_settings_root'}):
            self.assertEqual(app_config_path().parent.name, 'UnifiedTranscriberV12')

    def test_child_process_windowless_on_windows(self):
        with patch.object(unified_gui.os, 'name', 'nt'), \
             patch.object(unified_gui.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True):
            self.assertEqual(unified_gui.hidden_child_flags(),
                             unified_gui.subprocess.CREATE_NO_WINDOW)

    def test_gui_setup_does_not_spawn_pythonw(self):
        with patch.object(unified_gui.os, 'name', 'nt'), \
             patch.object(unified_gui.sys, 'executable', r'C:\Python311\pythonw.exe'):
            self.assertEqual(unified_gui.console_interpreter(), r'C:\Python311\python.exe')

    def test_package_does_not_do_legacy_cleanup(self):
        start = (CODE_DIR / 'start_gui.pyw').read_text(encoding='utf-8')
        self.assertNotIn('organize_previous_version', start)
        self.assertFalse((CODE_DIR / 'organize_legacy.py').exists())

if __name__ == '__main__': unittest.main()
