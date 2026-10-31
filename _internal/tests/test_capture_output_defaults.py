"""Saved capture defaults follow the common output folder after it changes."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import capture_preferences
from capture_preferences import recording_folder_preference, saved_options, serialize_options
from core import Config, load_config, save_config
from gamebar_recording import GameBarOptions
from live_config import LiveOptions


class CaptureOutputDefaults(unittest.TestCase):
    def test_legacy_flat_recording_default_is_reset_but_custom_override_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / 'settings.json'
            with patch('core.BASE', root / 'application'):
                old = root / 'application/Recordings'
                save_config(Config(live_recording_dir=str(old)), settings)
                self.assertEqual(load_config(settings).live_recording_dir, '')
                custom = root / 'Custom Recordings'
                save_config(Config(live_recording_dir=str(custom)), settings)
                self.assertEqual(load_config(settings).live_recording_dir, str(custom))

    def test_saved_default_follows_changed_common_root_after_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / 'settings.json'
            first, second = root / 'first', root / 'second'
            with patch('output_locations.settings_path', return_value=settings):
                cfg = Config(output_root=str(first))
                save_config(cfg, settings)
                cfg.capture_preferences = {
                    'live': serialize_options(LiveOptions(folder=first / 'Recordings', mode='record')),
                    'gamebar': serialize_options(GameBarOptions(destination=first / 'Recordings')),
                }
                self.assertNotIn('folder', cfg.capture_preferences['live'])
                self.assertNotIn('destination', cfg.capture_preferences['gamebar'])
                self.assertEqual(recording_folder_preference(first / 'Recordings'), '')
                save_config(cfg, settings)
                changed = replace(load_config(settings), output_root=str(second))
                save_config(changed, settings)
                restored = load_config(settings)
                self.assertEqual(saved_options(restored, 'live').folder, second / 'Recordings')
                self.assertEqual(saved_options(restored, 'gamebar').destination, second / 'Recordings')
                self.assertEqual(saved_options(restored, 'live').mode, 'record')
                self.assertFalse(first.exists())
                self.assertFalse(second.exists())

    def test_custom_capture_destinations_remain_fixed_after_common_root_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom = root / 'custom recording folder'
            original = custom / 'existing.wav'
            custom.mkdir()
            original.write_bytes(b'original media')
            cfg = Config(output_root=str(root / 'new common'), capture_preferences={
                'live': serialize_options(LiveOptions(folder=custom)),
                'gamebar': serialize_options(GameBarOptions(destination=custom)),
            })
            self.assertEqual(saved_options(cfg, 'live').folder, custom)
            self.assertEqual(saved_options(cfg, 'gamebar').destination, custom)
            self.assertEqual(recording_folder_preference(custom), str(custom))
            self.assertEqual(original.read_bytes(), b'original media')

    def test_only_known_old_app_default_rebinds_without_moving_old_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'application'
            legacy = app / 'Recordings'
            legacy.mkdir(parents=True)
            original = legacy / 'existing.wav'
            original.write_bytes(b'old recording remains')
            custom = app / 'MyRecordings'
            with patch('capture_preferences.APP_ROOT', app):
                cfg = Config(output_root=str(root / 'new common'), capture_preferences={
                    'live': {'folder': str(legacy), 'mode': 'record'},
                    'gamebar': {'destination': str(legacy)},
                })
                self.assertEqual(saved_options(cfg, 'live').folder, root / 'new common' / 'Recordings')
                self.assertEqual(saved_options(cfg, 'gamebar').destination, root / 'new common' / 'Recordings')
                self.assertEqual(recording_folder_preference(legacy), '')
                self.assertEqual(cfg.capture_preferences['live']['folder'], str(legacy))
                cfg.capture_preferences['live']['folder'] = str(custom)
                self.assertEqual(saved_options(cfg, 'live').folder, custom)
                self.assertEqual(original.read_bytes(), b'old recording remains')


if __name__ == '__main__':
    unittest.main()
