"""Display-language preference and locale detection must not modify ASR language."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config, load_config, save_config
import installer_ui
import ui_locale


class LocaleDetection(unittest.TestCase):
    def tearDown(self):
        ui_locale.set_language('ja')

    def test_japanese_english_unknown_and_unavailable(self):
        self.assertEqual(ui_locale.language_for_locale('ja_JP'), 'ja')
        self.assertEqual(ui_locale.language_for_locale('en-US'), 'en')
        self.assertEqual(ui_locale.language_for_locale('fr_FR'), 'en')
        self.assertEqual(ui_locale.language_for_locale(''), 'ja')
        self.assertEqual(ui_locale.language_for_locale(None), 'ja')

    def test_system_locale_is_consulted_and_missing_falls_back_to_ja(self):
        with patch.object(ui_locale.sys, 'platform', 'linux'):
            with patch.object(ui_locale.locale, 'getlocale', return_value=('en_US', 'UTF-8')):
                self.assertEqual(ui_locale.system_language(), 'en')
            with patch.object(ui_locale.locale, 'getlocale', return_value=('ja_JP', 'UTF-8')):
                self.assertEqual(ui_locale.system_language(), 'ja')
            with patch.object(ui_locale.locale, 'getlocale', side_effect=ValueError('unavailable')):
                self.assertEqual(ui_locale.system_language(), 'ja')

    def test_settings_keep_asr_unchanged_and_reject_invalid_ui_language(self):
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'settings.json'
            cfg = replace(Config(), ui_language='en', language='ja')
            save_config(cfg, filename)
            loaded = load_config(filename)
            self.assertEqual((loaded.ui_language, loaded.language), ('en', 'ja'))
            with self.assertRaises(ValueError):
                replace(cfg, ui_language='fr').validate()

    def test_preferred_installer_language_prioritizes_existing_user_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(installer_ui, 'system_language', return_value='en'):
                self.assertEqual(installer_ui.preferred_installer_language(root), 'en')
            path = root / 'Data/settings.json'
            path.parent.mkdir()
            path.write_text('{"ui_language":"ja"}', encoding='utf-8')
            with patch.object(installer_ui, 'system_language', return_value='en'):
                self.assertEqual(installer_ui.preferred_installer_language(root), 'ja')
            path.write_text('{"ui_language":"fr"}', encoding='utf-8')
            with patch.object(installer_ui, 'system_language', return_value='en'):
                self.assertEqual(installer_ui.preferred_installer_language(root), 'en')

    def test_new_install_persists_choice_legacy_settings_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            created = installer_ui.seed_preferences(root, minimal=False, hardware='cpu',
                recording_ready=False, ui_language='en')
            self.assertTrue(created)
            configpath = root / 'Data/settings.json'
            original = configpath.read_bytes()
            self.assertEqual(load_config(configpath).ui_language, 'en')
            self.assertEqual(load_config(configpath).language, 'ja')
            self.assertFalse(installer_ui.seed_preferences(root, minimal=False,
                hardware='cpu', recording_ready=False, ui_language='ja'))
            self.assertEqual(configpath.read_bytes(), original)

    def test_selected_installer_language_preserves_unknown_fields_on_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'Data/settings.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'ui_language': 'ja',
                'unrecognized_setting': {'preserve': 123}}, ensure_ascii=False), encoding='utf-8')
            import hashlib
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            installer_ui.update_setup_preferences(root, minimal=False, hardware='cpu',
                recording_ready=False, model='large-v3-turbo',
                expected_sha256=digest, ui_language='en')
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(data['ui_language'], 'en')
            self.assertEqual(data['unrecognized_setting'], {'preserve': 123})
            path.write_bytes(b'corrupted\x00data')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            installer_ui.update_setup_preferences(root, minimal=False, hardware='cpu',
                recording_ready=False, model='large-v3-turbo',
                expected_sha256=digest, ui_language='en')
            self.assertEqual(path.read_bytes(), b'corrupted\x00data')

    def test_translation_does_not_change_coded_profile_or_asr_values(self):
        ui_locale.set_language('en')
        self.assertEqual(ui_locale.translate('録音・録画'), 'Record & Capture')
        self.assertEqual(ui_locale.translate('文字起こし統合スタジオ v1.0.3'),
            'Unified Transcriber Studio v1.0.3')
        self.assertEqual(ui_locale.translate('faster-whisper'), 'faster-whisper')
        ui_locale.set_language('ja')
        self.assertEqual(ui_locale.translate('録音・録画'), '録音・録画')


class WidgetTranslation(unittest.TestCase):
    def tearDown(self):
        ui_locale.set_language('ja')

    def test_switch_is_reversible_and_does_not_change_textvariables(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk desktop display unavailable')
        try:
            root.withdraw()
            label = tk.Label(root, text='録音・録画')
            value = tk.StringVar(root, '録音・録画')
            field = tk.Entry(root, textvariable=value)
            ui_locale.translate_widgets(root, 'en')
            self.assertEqual(label.cget('text'), 'Record & Capture')
            self.assertEqual(field.get(), '録音・録画')
            ui_locale.translate_widgets(root, 'ja')
            self.assertEqual(label.cget('text'), '録音・録画')
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
