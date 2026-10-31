"""Vocabulary is only sent as a hint and does not rewrite recognized text."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.asr_hints import ENV_KEY, options_for, _whisper_hints


class HintOptionTests(unittest.TestCase):
    def test_no_hint_preserves_original_recognizer_arguments(self):
        with patch.dict(os.environ, {}, clear=True):
            for kind in ('openai', 'faster', 'qwen'):
                self.assertEqual(options_for(kind), {})

    def test_options_are_specific_to_each_engine(self):
        class Whisper:
            def transcribe(self, audio, initial_prompt=None, carry_initial_prompt=False):
                pass
        with patch.dict(os.environ, {ENV_KEY: 'サンプル社、テスト会議'}):
            self.assertEqual(options_for('faster'), {'hotwords': 'サンプル社、テスト会議'})
            self.assertEqual(options_for('openai', model=Whisper()),
                             {'initial_prompt': 'サンプル社、テスト会議', 'carry_initial_prompt': True})
            self.assertEqual(options_for('qwen'), {'context': 'サンプル社、テスト会議'})

    def test_token_limit_never_splits_a_registered_word_or_logs_it(self):
        vocab = '秘密の名称、' + '会社' * 100 + '、短い語'
        output = io.StringIO()
        with patch.dict(os.environ, {ENV_KEY: vocab}), contextlib.redirect_stdout(output):
            result = options_for('faster')
        self.assertEqual(result, {'hotwords': '秘密の名称、短い語'})
        self.assertIn('上限', output.getvalue())
        self.assertNotIn('秘密の名称', output.getvalue())

    def test_real_tokenizer_counts_tokens_not_characters(self):
        tokenizer = SimpleNamespace(encode=lambda text, add_special_tokens=False:
                                    SimpleNamespace(ids=list(text)))
        model = SimpleNamespace(hf_tokenizer=tokenizer)
        with patch.dict(os.environ, {ENV_KEY: '名称' * 35}):
            self.assertEqual(options_for('faster', model=model), {'hotwords': '名称' * 35})
        self.assertEqual(_whisper_hints('長い語、短い語', lambda s: len(s))[0], '長い語、短い語')

    def test_invalid_or_unsupported_hint_fails_before_calling_model(self):
        with patch.dict(os.environ, {ENV_KEY: '項目\n別項目'}):
            with self.assertRaises(ValueError):
                options_for('faster')
        with patch.dict(os.environ, {ENV_KEY: '用語'}):
            with self.assertRaises(ValueError):
                options_for('unknown')

    def test_old_openai_version_keeps_supported_arguments_only(self):
        class OldWhisper:
            def transcribe(self, audio, initial_prompt=None):
                pass
        with patch.dict(os.environ, {ENV_KEY: '名称'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(options_for('openai', model=OldWhisper()), {'initial_prompt': '名称'})


if __name__ == '__main__':
    unittest.main()
