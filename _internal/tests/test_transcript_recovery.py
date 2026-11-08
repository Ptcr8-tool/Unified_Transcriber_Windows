"""Recovery checkpoints and error-before-cleanup regression tests."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.worker_io import TranscriptRecovery
from engines import pyannote_backend, segment_resemblyzer


class RecoveryJournalTests(unittest.TestCase):
    def test_journal_append_does_not_replace_previously_flushed_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'meeting.txt'
            recovery = TranscriptRecovery(output)
            self.assertTrue(recovery.append('0:00:01 話者不明: 初めの部分'))
            original = recovery.partial.read_bytes()
            self.assertTrue(recovery.append('0:04:01 話者不明: 次の部分'))
            journal = recovery.partial.read_bytes()
            self.assertTrue(journal.startswith(original))
            self.assertIn('初めの部分', journal.decode('utf-8'))
            self.assertIn('次の部分', journal.decode('utf-8'))
            output.write_text('完成版', encoding='utf-8')
            recovery.finalize()
            self.assertEqual(output.read_text(encoding='utf-8'), '完成版')
            self.assertFalse(recovery.partial.exists())

    def test_error_is_written_with_token_redacted_without_deleting_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery = TranscriptRecovery(Path(folder) / 'meeting.txt')
            recovery.append('すでに認識した発言')
            with mock.patch.dict(os.environ, {'HF_TOKEN': 'hf_1234567890abcdefghijkl'}):
                recovery.record_error(RuntimeError('token hf_1234567890abcdefghijkl is invalid'))
            details = recovery.failure.read_text(encoding='utf-8')
            self.assertIn('RuntimeError:', details)
            self.assertNotIn('hf_1234567890abcdefghijkl', details)
            self.assertTrue(recovery.partial.exists())


class EarlyWriteTests(unittest.TestCase):
    def test_final_transcript_exists_before_temp_wav_cleanup_in_both_engines(self):
        with tempfile.TemporaryDirectory() as folder:
            for engine in (pyannote_backend, segment_resemblyzer):
                with self.subTest(engine=engine.__name__):
                    source = Path(folder) / 'source.wav'
                    output = Path(folder) / (engine.__name__.split('.')[-1] + '.txt')
                    source.write_bytes(b'input-fixture')
                    args = types.SimpleNamespace(
                        input=source, output=output, max_speakers=2, merge_gap=1.2,
                        qwen_max_new_tokens=256, asr_backend='whisper', device='cpu',
                        model='small', compute_type=None, caption_max_chars=None,
                        caption_max_seconds=5., blank_lines=0, speakers=None,
                        threshold=.25, segment_length=2., diarization_unit='fixed')
                    stage = Path(folder) / ('stage-' + engine.__name__.split('.')[-1])
                    stage.mkdir(exist_ok=True)

                    class FailCleanup:
                        def __enter__(self): return str(stage)
                        def __exit__(self, *_):
                            assert output.exists(), 'Final TXT must be saved BEFORE WAV cleanup'
                            raise OSError('simulated cleanup termination')

                    contexts = [
                        mock.patch.object(engine, 'parse_args', return_value=args),
                        mock.patch.object(engine, 'check_ffmpeg', return_value='ffmpeg'),
                        mock.patch.object(engine, 'extract_audio'),
                        mock.patch.object(engine, 'release_cuda_memory'),
                        mock.patch.object(engine.tempfile, 'TemporaryDirectory', return_value=FailCleanup()),
                        mock.patch.object(engine, 'transcribe_whisper_words',
                                          return_value=[engine.TranscriptItem(0., 1., 'UNKNOWN', '保存されたテキスト')])]
                    if engine is pyannote_backend:
                        contexts += [
                            mock.patch.dict(os.environ, {'HF_TOKEN':'hf_diagnostic_test_token'}),
                            mock.patch.object(engine, 'load_diarization_pipeline', return_value=object()),
                            mock.patch.object(engine, 'get_speaker_turns', return_value=[
                                engine.SpeakerTurn(0., 1., 'speaker-A')])]
                    else:
                        contexts += [mock.patch.object(engine, 'get_resemblyzer_speaker_turns',
                            return_value=[engine.SpeakerTurn(0.,1., 'speaker-A')])]
                    with contextlib.ExitStack() as stack:
                        for patcher in contexts: stack.enter_context(patcher)
                        result = engine.main()
                    self.assertEqual(result, 1)  # cleanup failed, but output was saved
                    self.assertIn('保存されたテキスト', output.read_text(encoding='utf-8-sig'))

    def test_failure_log_is_created_before_temp_cleanup(self):
        engine = pyannote_backend
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.wav'
            source.write_bytes(b'input')
            output = Path(folder) / 'out.txt'
            stage = Path(folder) / 'stage'
            stage.mkdir()
            args = types.SimpleNamespace(input=source, output=output,
                max_speakers=2, merge_gap=1.2, qwen_max_new_tokens=256,
                asr_backend='whisper', model='small', device='cpu',
                compute_type=None, caption_max_chars=None, caption_max_seconds=5.,
                blank_lines=0)
            class CheckErrorBeforeCleanup:
                def __enter__(self): return str(stage)
                def __exit__(self, *_):
                    logs = list(Path(folder).glob('.*.error.log'))
                    assert logs, 'Error should be flushed before WAV cleanup'
                    assert 'ASR failure' in logs[0].read_text(encoding='utf-8')
                    return False
            with contextlib.ExitStack() as stack:
                for patcher in [
                    mock.patch.object(engine, 'parse_args', return_value=args),
                    mock.patch.object(engine, 'check_ffmpeg', return_value='ffmpeg'),
                    mock.patch.object(engine, 'extract_audio'),
                    mock.patch.object(engine, 'load_diarization_pipeline', return_value=object()),
                    mock.patch.object(engine, 'get_speaker_turns', return_value=[]),
                    mock.patch.object(engine, 'release_cuda_memory'),
                    mock.patch.object(engine.tempfile, 'TemporaryDirectory', return_value=CheckErrorBeforeCleanup()),
                    mock.patch.object(engine, 'transcribe_whisper_words',
                                      side_effect=RuntimeError('ASR failure')),
                    mock.patch.dict(os.environ, {'HF_TOKEN':'hf_diagnostic_test_token'})]:
                    stack.enter_context(patcher)
                self.assertEqual(engine.main(), 1)


class QwenPartialRecoveryTests(unittest.TestCase):
    def test_each_engine_yields_completed_chunk_before_next_chunk_fails(self):
        wav = Path('dummy.wav')
        for engine in (pyannote_backend, segment_resemblyzer):
            with self.subTest(engine=engine.__name__):
                observed = []
                fake_torch = types.ModuleType('torch')
                fake_torch.float32 = 'float32'
                fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False,
                                                        empty_cache=lambda: None)
                fake_qwen = types.ModuleType('qwen_asr')
                class FakeQwen:
                    @classmethod
                    def from_pretrained(cls, *_, **__): return cls()
                    def __init__(self): self.count = 0
                    def transcribe(self, **kwargs):
                        self.count += 1
                        if self.count == 2:
                            raise RuntimeError('simulated chunk 2 crash')
                        word = types.SimpleNamespace(start_time=.1,end_time=.7,text='途中の文')
                        return [types.SimpleNamespace(text='途中の文',
                            time_stamps=types.SimpleNamespace(items=[word]))]
                fake_qwen.Qwen3ASRModel = FakeQwen
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(sys.modules, {'torch':fake_torch,'qwen_asr':fake_qwen}))
                    stack.enter_context(mock.patch.object(engine,'create_qwen_chunks',
                        return_value=[(wav,0.,0.),(wav,1.,0.)]))
                    stack.enter_context(mock.patch.object(engine,'pcm_wav_seconds', return_value=2.))
                    stack.enter_context(mock.patch.object(engine,'emit_setup_progress'))
                    stack.enter_context(mock.patch.object(engine,'emit_gui_progress'))
                    stack.enter_context(mock.patch.object(engine,'hf_snapshot_progress', return_value=contextlib.nullcontext()))
                    with self.assertRaisesRegex(RuntimeError, 'chunk 2 crash'):
                        engine.transcribe_qwen_words(wav,'model','aligner','cpu',
                            Path('chunks'),256,on_progress=lambda items: observed.append([i.text for i in items]))
                self.assertEqual(observed, [['途中の文']])


if __name__ == '__main__':
    unittest.main()
