"""User-owned output defaults, early logs and cross-machine preferences."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import core
import diagnostic_logs
from gamebar_recording import GameBarOptions
from live_config import LiveOptions
import output_locations as locations
from settings_transfer import export_settings, import_settings


class OutputLocationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.documents = self.root / 'redirected Documents'
        self.settings = self.root / 'installation' / 'Data' / 'settings.json'
        self.addCleanup(patch.stopall)
        patch('output_locations.documents_directory', return_value=self.documents).start()

    def test_resolving_defaults_does_not_create_directories(self):
        expected = self.documents / 'UnifiedTranscriber'
        self.assertEqual(locations.output_root(config_path=self.settings), expected)
        for kind, name in locations.OUTPUT_FOLDERS.items():
            self.assertEqual(locations.default_directory(kind, config_path=self.settings), expected / name)
        self.assertFalse(self.documents.exists())
        self.assertFalse(self.settings.parent.exists())

    def test_persisted_root_is_available_before_core_gui_imports(self):
        selected = self.root / 'my outputs'
        locations.set_output_root(selected, config_path=self.settings)
        self.assertEqual(locations.output_root(config_path=self.settings), selected)
        self.assertFalse(selected.exists())
        source = Path(locations.__file__).resolve().parent
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'from output_locations import output_root; '
                'print(output_root(config_path=__import__("pathlib").Path(sys.argv[2])))')
        result = subprocess.run([sys.executable, '-I', '-c', code, str(source), str(self.settings)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), str(selected))

    def test_setter_preserves_unknown_settings_and_credentials(self):
        self.settings.parent.mkdir(parents=True)
        original = {'profile': 'qwen-res', 'future_setting': {'value': 42},
                    'hf_token_json': 'not-a-real-token', 'output_dir': str(self.root / 'custom')}
        self.settings.write_text(json.dumps(original), encoding='utf-8')
        selected = self.root / 'new folder'
        locations.set_output_root(selected, config_path=self.settings)
        current = json.loads(self.settings.read_text(encoding='utf-8'))
        self.assertEqual(current, {**original, 'output_root': str(selected)})
        locations.set_output_root('', config_path=self.settings)
        self.assertEqual(locations.output_root(config_path=self.settings), self.documents / 'UnifiedTranscriber')

    def test_invalid_existing_settings_are_not_overwritten(self):
        self.settings.parent.mkdir(parents=True)
        for content in ('broken JSON', '[]', '{"x": NaN}'):
            self.settings.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                locations.set_output_root(self.root / 'new', config_path=self.settings)
            self.assertEqual(self.settings.read_text(encoding='utf-8'), content)

    def test_invalid_roots_fail_config_validation(self):
        file = self.root / 'not-a-directory'
        file.write_text('data', encoding='utf-8')
        for value in ('relative/path', '\0bad', str(file)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.Config(output_root=value).validate()
        core.Config(output_root=str(self.root / 'valid')).validate()

    def test_default_root_settings_and_explicit_overrides_have_clear_precedence(self):
        cfg = core.Config(output_root=str(self.root / 'common'))
        source = self.root / 'input' / 'sample.wav'
        self.assertEqual(core.choose_output(source, cfg), self.root / 'common' / 'Transcriptions' / 'sample.txt')
        explicit = replace(cfg, output_dir=str(self.root / 'explicit'))
        self.assertEqual(core.choose_output(source, explicit), self.root / 'explicit' / 'sample.txt')
        same = replace(explicit, output_same_as_input=True)
        self.assertEqual(core.choose_output(source, same), source.with_suffix('.txt'))
        self.assertEqual(core.choose_output(source, same, watch_processed=self.root / 'processed'),
                         self.root / 'processed' / 'sample.txt')

    def test_recording_options_use_runtime_settings_instead_of_current_working_directory(self):
        expected = self.root / 'selected' / 'Recordings'
        with patch('live_config.default_directory', return_value=expected), \
                patch('gamebar_recording.default_directory', return_value=expected):
            self.assertEqual(LiveOptions().folder, expected)
            self.assertEqual(GameBarOptions().destination, expected)

    def test_core_root_uses_same_early_log_setting(self):
        selected = self.root / 'selected'
        locations.set_output_root(selected, config_path=self.settings)
        with patch('core.app_config_path', return_value=self.settings), \
                patch('output_locations.settings_path', return_value=self.settings):
            self.assertEqual(core.default_directory('logs'), selected / 'Logs')
            self.assertEqual(diagnostic_logs.log_directory(), selected / 'Logs')
        self.assertTrue((selected / 'Logs').is_dir())

    def test_changing_output_root_rotates_logs_and_retains_previous_content(self):
        first = self.root / 'first'
        second = self.root / 'second'
        with patch('diagnostic_logs.default_directory', return_value=first):
            logs = diagnostic_logs.SessionLogs('secret')
            logs.write('previous message')
            previous = logs.session_path
            self.assertFalse(logs.refresh_location())
        with patch('diagnostic_logs.default_directory', return_value=second):
            self.assertTrue(logs.refresh_location())
            logs.error('new secret')
            self.assertEqual(logs.folder, second)
            self.assertIn('previous message', previous.read_text(encoding='utf-8'))
            self.assertNotIn('secret', logs.error_path.read_text(encoding='utf-8'))
            self.assertIn('[HF_TOKEN_REDACTED]', logs.error_path.read_text(encoding='utf-8'))

    def test_output_root_exports_relative_to_user_and_rebases_for_another_user(self):
        old_home = r'C:\Users\sample'
        new_home = r'C:\Users\another'
        old_app = r'C:\Programs\UnifiedTranscriber'
        new_app = r'D:\Apps\UnifiedTranscriber'
        cfg = core.Config(output_root=old_home + r'\Documents\UnifiedTranscriber')
        data = export_settings(cfg, app_root=old_app, user_home=old_home)
        self.assertEqual(data['settings']['output_root'],
                         {'base': 'home', 'path': 'Documents/UnifiedTranscriber'})
        restored, missing = import_settings(data, app_root=new_app, user_home=new_home)
        self.assertEqual(restored.output_root, new_home + r'\Documents\UnifiedTranscriber')
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
