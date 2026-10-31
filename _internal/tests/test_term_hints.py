"""Dictionary behavior and safe round trips through both exchange paths."""
import dataclasses
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from settings_transfer import export_settings, import_settings
import term_hints as hints


class TermHintsTests(unittest.TestCase):
    def sample(self):
        return dataclasses.replace(core.Config(), hint_enabled=True,
            hint_groups={'共通': [{'term': '株式会社青空', 'reading': 'あおぞら'}],
                         '取材': [{'term': '青空駅', 'reading': 'あおぞらえき'},
                                {'term': '株式会社青空', 'reading': ''}],
                         '使わない': [{'term': '対象外', 'reading': ''}]},
            hint_selected_groups=['取材'])

    def test_defaults_and_disabled_preserve_existing_behavior(self):
        self.assertEqual(hints.build_hint_text(core.Config()), '')
        cfg = dataclasses.replace(self.sample(), hint_enabled=False)
        self.assertEqual(hints.build_hint_text(cfg), '')

    def test_common_selected_dedup_and_reading_are_not_replacements(self):
        cfg = self.sample()
        self.assertEqual(hints.build_hint_text(cfg), '株式会社青空、青空駅')
        self.assertEqual(hints.hint_summary(cfg)['included'], 2)
        self.assertNotIn('あおぞら', hints.build_hint_text(cfg))
        self.assertNotIn('対象外', hints.build_hint_text(cfg))

    def test_hint_limit_never_cuts_a_term(self):
        terms = [{'term': ('名' * 110) + str(i), 'reading': ''} for i in range(30)]
        cfg = dataclasses.replace(core.Config(), hint_enabled=True, hint_groups={'共通': terms})
        result = hints.hint_summary(cfg)
        self.assertLessEqual(len(result['text']), hints.MAX_HINT_LENGTH)
        self.assertGreater(result['omitted'], 0)
        self.assertTrue(all(x in [entry['term'] for entry in terms] for x in result['text'].split('、')))

    def test_disabled_dictionary_removes_inherited_hints(self):
        env = {'TRANSCRIBER_ASR_HINTS': 'stale', 'OTHER': 'kept'}
        hints.apply_hint_environment(env, core.Config())
        self.assertEqual(env, {'OTHER': 'kept'})
        hints.apply_hint_environment(env, self.sample())
        self.assertEqual(env['TRANSCRIBER_ASR_HINTS'], '株式会社青空、青空駅')

    def test_persistence_and_full_settings_transfer(self):
        cfg = self.sample()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            core.save_config(cfg, path)
            self.assertEqual(core.load_config(path).hint_groups, cfg.hint_groups)
        transferred, _ = import_settings(export_settings(cfg), core.Config())
        self.assertEqual(hints.build_hint_text(transferred), hints.build_hint_text(cfg))

    def test_dictionary_only_roundtrip_and_reject_malformed(self):
        cfg = self.sample()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'dictionary.json'
            hints.write_dictionary(path, cfg)
            imported = hints.read_dictionary(path)
            self.assertEqual(imported['hint_groups'], cfg.hint_groups)
            imported['hint_groups']['共通'].clear()
            self.assertEqual(len(cfg.hint_groups['共通']), 1)
            data = hints.dictionary_data(cfg)
            data['hint_selected_groups'] = ['存在しない']
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                hints.read_dictionary(path)
            self.assertEqual(len(cfg.hint_groups['共通']), 1)

    def test_oversized_or_bad_inputs_are_rejected(self):
        cfg = self.sample()
        cfg.hint_groups['共通'].append({'term': '改行\n混入', 'reading': ''})
        with self.assertRaises(ValueError):
            cfg.validate()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'huge.json'
            path.write_bytes(b'x' * (hints.MAX_DICTIONARY_BYTES + 1))
            with self.assertRaises(ValueError):
                hints.read_dictionary(path)

    def test_oversized_settings_do_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            core.save_config(core.Config(), path)
            original = path.read_bytes()
            with patch.object(core, 'MAX_CONFIG_BYTES', len(original)):
                with self.assertRaises(ValueError):
                    core.save_config(self.sample(), path)
            self.assertEqual(path.read_bytes(), original)

    def test_word_separator_and_unpaired_surrogate_are_rejected(self):
        cfg = self.sample()
        for value in ('別名、別表記', '\ud800'):
            cfg.hint_groups['共通'][0]['term'] = value
            with self.assertRaises(ValueError):
                cfg.validate()


if __name__ == '__main__':
    unittest.main()
