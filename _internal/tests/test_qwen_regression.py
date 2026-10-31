"""Regression tests for the Windows Qwen3 ForcedAligner trace; no model downloads."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.qwen_compat import timestamp_entries, qwen_dtype, UNALIGNED_PREFIX
from engines import segment_resemblyzer, pyannote_backend
import setup_backend


class QwenSdkShapeTests(unittest.TestCase):
    def test_forced_align_result_items_official_structure(self):
        word = types.SimpleNamespace(text='こんにちは', start_time=0.1, end_time=0.9)
        sdk_result = types.SimpleNamespace(text='こんにちは', time_stamps=types.SimpleNamespace(items=[word]))
        self.assertEqual(timestamp_entries(sdk_result), [word])
        self.assertEqual(segment_resemblyzer._extract_timestamp_entries(sdk_result), [word])
        self.assertEqual(pyannote_backend._extract_timestamp_entries(sdk_result), [word])

    def test_older_list_nested_and_dict_formats(self):
        rec = {'start_time': 0, 'end_time': 0.1, 'text': 'あ'}
        for value in ([rec], [[rec]], {'items': [rec]}):
            with self.subTest(value=value):
                self.assertEqual(timestamp_entries({'time_stamps': value}), [rec])
        self.assertEqual(timestamp_entries(types.SimpleNamespace(time_stamps=None)), [])

    def test_gpu_dtype_rtx_2080ti_and_ampere(self):
        fake = types.SimpleNamespace(float32='fp32', float16='fp16', bfloat16='bf16',
            cuda=types.SimpleNamespace(get_device_capability=lambda i: (7, 5),
                                       is_bf16_supported=lambda: True))
        self.assertEqual(qwen_dtype(fake, 'cuda'), 'fp16')
        self.assertEqual(qwen_dtype(fake, 'cpu'), 'fp32')
        fake.cuda.get_device_capability = lambda i: (8, 6)
        self.assertEqual(qwen_dtype(fake, 'cuda'), 'bf16')
        fake.cuda.is_bf16_supported = lambda: False
        self.assertEqual(qwen_dtype(fake, 'cuda'), 'fp16')


class ActualEngineFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.audio = self.folder / 'voice.wav'
        with wave.open(str(self.audio), 'wb') as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(16000)
            file.writeframes(b'\0\0' * (16000 * 2))

    def execute(self, engine, stamp_items, text):
        captured = {}
        dummy_torch = types.ModuleType('torch')
        dummy_torch.float16 = 'fp16'
        dummy_torch.float32 = 'fp32'
        dummy_torch.bfloat16 = 'bf16'
        dummy_torch.cuda = types.SimpleNamespace(is_available=lambda: True,
            get_device_capability=lambda index: (7,5),
            is_bf16_supported=lambda: True,
            empty_cache=lambda: None)
        result = types.SimpleNamespace(text=text, time_stamps=types.SimpleNamespace(items=stamp_items))
        dummy_qwen = types.ModuleType('qwen_asr')
        class FakeASRModel:
            @classmethod
            def from_pretrained(cls, model_id, **kw):
                captured.update(kw)
                return cls()
            def transcribe(self, **kwargs):
                captured['transcribe_kwargs'] = kwargs
                return [result]
        dummy_qwen.Qwen3ASRModel = FakeASRModel
        with mock.patch.dict(sys.modules, {'torch': dummy_torch, 'qwen_asr': dummy_qwen}):
            items = engine.transcribe_qwen_words(self.audio, 'fake/asr',
                'fake/aligner', 'cuda', self.folder / 'chunks', 512)
        return items, captured

    def test_both_engines_extract_real_forced_align_items(self):
        stamps = [types.SimpleNamespace(start_time=0.20, end_time=.45, text='こんにちは'),
                  types.SimpleNamespace(start_time=.5, end_time=.88, text='世界')]
        for engine in (segment_resemblyzer, pyannote_backend):
            with self.subTest(engine=engine.__name__):
                items, kwargs = self.execute(engine, stamps, 'こんにちは世界')
                self.assertEqual([i.text for i in items], ['こんにちは', '世界'])
                self.assertEqual([i.start for i in items], [.2, .5])
                self.assertEqual(kwargs['dtype'], 'fp16')
                self.assertEqual(kwargs['forced_aligner_kwargs']['dtype'], 'fp16')
                self.assertTrue(kwargs['transcribe_kwargs']['return_time_stamps'])

    def test_missing_alignment_keeps_text_but_no_false_speaker(self):
        for engine in (segment_resemblyzer, pyannote_backend):
            with self.subTest(engine=engine.__name__):
                items, _ = self.execute(engine, [], 'タイムスタンプがないけど認識済み')
                self.assertEqual(len(items), 1)
                self.assertTrue(items[0].text.startswith(UNALIGNED_PREFIX))
                self.assertEqual(items[0].end, 2)
                turns = [engine.SpeakerTurn(0., 2., 'speaker-A')]
                merged = engine.merge_items(items, turns, 1.2)
                self.assertEqual(merged[0].speaker, 'UNKNOWN')

    def test_qwen_passes_hints_with_forced_aligner_for_both_engines(self):
        stamps = [types.SimpleNamespace(start_time=0.2, end_time=.45, text='原文')]
        with mock.patch.dict('os.environ', {'TRANSCRIBER_ASR_HINTS': 'サンプル社'}):
            for engine in (segment_resemblyzer, pyannote_backend):
                items, kwargs = self.execute(engine, stamps, '原文')
                self.assertEqual(kwargs['transcribe_kwargs']['context'], 'サンプル社')
                self.assertTrue(kwargs['transcribe_kwargs']['return_time_stamps'])
                self.assertEqual([item.text for item in items], ['原文'])

    def test_empty_asr_text_and_no_stamps_does_not_save_fake_text(self):
        for engine in (segment_resemblyzer, pyannote_backend):
            self.assertEqual(self.execute(engine, [], '')[0], [])


class EnvironmentRepairTests(unittest.TestCase):
    def test_missing_pip_is_repaired_with_ensurepip(self):
        missing = types.SimpleNamespace(returncode=1)
        present = types.SimpleNamespace(returncode=0)
        with mock.patch('setup_backend.subprocess.run', side_effect=[missing, present]), \
                mock.patch('setup_backend.run') as run:
            setup_backend.ensure_pip(Path('python.exe'))
            run.assert_called_once_with('python.exe', '-m', 'ensurepip', '--upgrade')

    def test_torch_ready_skips_large_reinstall_when_environment_matches(self):
        with mock.patch('setup_backend.subprocess.run', return_value=types.SimpleNamespace(returncode=0)):
            self.assertTrue(setup_backend.torch_ready(Path('python.exe'), 'cuda'))
        with mock.patch('setup_backend.subprocess.run', return_value=types.SimpleNamespace(returncode=1)):
            self.assertFalse(setup_backend.torch_ready(Path('python.exe'), 'cuda'))


if __name__ == '__main__':
    unittest.main()
