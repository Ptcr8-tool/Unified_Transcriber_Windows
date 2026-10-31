import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from setup_progress import (PREFIX, emit_setup_progress, parse_setup_line,
                            progress_tqdm, hf_snapshot_progress)


class SetupProgressTests(unittest.TestCase):
    def test_structured_percentage_is_stage_scoped(self):
        output = io.StringIO()
        with redirect_stdout(output):
            emit_setup_progress('モデル取得 · turbo', .42, '転送中のデータ')
        update = parse_setup_line(output.getvalue())
        self.assertEqual(update.stage, 'モデル取得 · turbo')
        self.assertEqual(update.fraction, .42)
        self.assertEqual(update.detail, '転送中のデータ')

    def test_normal_transcription_parser_recognizes_model_download(self):
        from engine_progress import parse_engine_line
        record = PREFIX + json.dumps({'stage': 'モデル取得', 'fraction': .3,
                                      'detail': '個別ファイル'})
        update = parse_engine_line(record)
        self.assertEqual(update.stage, 'モデル取得')
        self.assertEqual(update.fraction, .3)
        self.assertEqual(update.detail, '個別ファイル')
        self.assertEqual(update.snippet, '')

    def test_task_count_is_not_misrepresented_as_total_percentage(self):
        update = parse_setup_line('[2/3] モデル: large-v3-turbo')
        self.assertIsNone(update.fraction)
        self.assertIn('2/3', update.detail)
        self.assertIn('モデル', update.detail)

    def test_invalid_progress_does_not_corrupt_ui(self):
        for value in (-1, 2, float('nan'), True, '0.5'):
            record = PREFIX + json.dumps({'stage': 'モデル', 'fraction': value})
            self.assertIsNone(parse_setup_line(record))
        self.assertIsNone(parse_setup_line(PREFIX + '{broken'))

    def test_tqdm_reports_real_counts_even_with_redirected_output(self):
        fake_tqdm, fake_auto = ModuleType('tqdm'), ModuleType('tqdm.auto')
        class Base:
            def __init__(self, *args, **kwargs):
                self.total = kwargs.get('total')
                self.unit = kwargs.get('unit', 'it')
                self.n = kwargs.get('initial', 0)
                self.disabled = kwargs.get('disable')
            def update(self, n):
                self.n += n
                self.display()
        fake_auto.tqdm = Base
        output = io.StringIO()
        with patch.dict(sys.modules, {'tqdm': fake_tqdm, 'tqdm.auto': fake_auto}), redirect_stdout(output):
            cls = progress_tqdm('モデル取得 · turbo')
            bar = cls(total=100, unit='B', disable=True, name='ignored')
            self.assertFalse(bar.disabled)
            bar.update(42)
        update = parse_setup_line(output.getvalue())
        self.assertEqual(update.fraction, .42)
        self.assertEqual(update.detail, '転送中のデータ（個別ファイルまたは転送単位）')

    def test_dynamic_hub_total_never_implies_a_final_percentage(self):
        fake_tqdm, fake_auto = ModuleType('tqdm'), ModuleType('tqdm.auto')
        class Base:
            def __init__(self, **kwargs):
                self.total, self.n = kwargs['total'], 0
                self.unit, self.desc = kwargs['unit'], kwargs['desc']
            def update(self, n):
                self.n += n
                self.display()
        fake_auto.tqdm = Base
        output = io.StringIO()
        with patch.dict(sys.modules, {'tqdm': fake_tqdm, 'tqdm.auto': fake_auto}), redirect_stdout(output):
            bar = progress_tqdm('モデル取得')(total=100, unit='B', desc='Downloading (incomplete total...)')
            bar.update(100)
        update = parse_setup_line(output.getvalue())
        self.assertIsNone(update.fraction)
        self.assertIn('合計未確定', update.detail)

    def test_hf_wrapper_preserves_download_arguments_and_restores_on_error(self):
        hub, calls = ModuleType('huggingface_hub'), []
        def snapshot_download(repo_id, *, revision=None, token=None, tqdm_class=None):
            calls.append((repo_id, revision, token, tqdm_class))
            raise RuntimeError('failed')
        hub.snapshot_download = snapshot_download
        with patch.dict(sys.modules, {'huggingface_hub': hub}), \
             patch('setup_progress.progress_tqdm', return_value=object):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                with hf_snapshot_progress('モデル取得'):
                    hub.snapshot_download('repo', revision='fixed', token='test-token')
            self.assertIs(hub.snapshot_download, snapshot_download)
        self.assertEqual(calls, [('repo', 'fixed', 'test-token', object)])

    def test_older_hub_forwards_http_and_xet_byte_counts(self):
        hub, file_module = ModuleType('huggingface_hub'), ModuleType('huggingface_hub.file_download')
        hub.__path__ = []
        hub.file_download = file_module
        def snapshot_download(repo_id, *, tqdm_class=None):
            return repo_id
        hub.snapshot_download = snapshot_download
        def original_context(*, desc, log_level, total, initial, name, _tqdm_bar=None):
            return ('upstream', name)
        file_module._get_progress_bar_context = original_context
        class Progress:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        modules = {'huggingface_hub': hub, 'huggingface_hub.file_download': file_module}
        with patch.dict(sys.modules, modules), patch('setup_progress.progress_tqdm', return_value=Progress):
            with hf_snapshot_progress('モデル取得'):
                bar = file_module._get_progress_bar_context(desc='model.bin', log_level=0,
                    total=1000, initial=200, name='huggingface_hub.xet_get', _tqdm_bar=None)
                self.assertEqual(bar.kwargs['total'], 1000)
                self.assertEqual(bar.kwargs['initial'], 200)
                self.assertEqual(bar.kwargs['unit'], 'B')
                self.assertEqual(file_module._get_progress_bar_context(desc='x', log_level=0,
                    total=1000, initial=0, name='other', _tqdm_bar=None), ('upstream', 'other'))
            self.assertIs(file_module._get_progress_bar_context, original_context)

    def test_unsupported_hf_version_preserves_exceptions_and_call(self):
        hub = ModuleType('huggingface_hub')
        hub.snapshot_download = lambda repo_id: repo_id
        with patch.dict(sys.modules, {'huggingface_hub': hub}), \
             patch('setup_progress.progress_tqdm', return_value=object):
            with self.assertRaisesRegex(ValueError, 'original error'):
                with hf_snapshot_progress('モデル取得'):
                    raise ValueError('original error')

    def test_event_survives_nested_setup_pipe(self):
        from setup_process import run_logged
        output = io.StringIO()
        record = PREFIX + json.dumps({'stage': 'model', 'fraction': 0.5, 'detail': 'bytes'})
        command = [sys.executable, '-u', '-c', 'import sys; print(sys.argv[1], flush=True)', record]
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output):
            result = run_logged(command, Path(directory), idle_timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(parse_setup_line(output.getvalue()).fraction, .5)

    def test_inference_download_uses_progress_context_for_both_engines(self):
        from engines import segment_resemblyzer, pyannote_backend
        hub, fake = ModuleType('huggingface_hub'), ModuleType('faster_whisper')
        calls = []
        def snapshot_download(repo_id, *, revision=None, tqdm_class=None):
            calls.append((repo_id, revision, tqdm_class))
            return '/cached/model'
        hub.snapshot_download = snapshot_download
        class StopHere(Exception):
            pass
        def whisper_model(name, *, device, compute_type, revision=None):
            hub.snapshot_download(name, revision=revision)
            raise StopHere()
        fake.WhisperModel = whisper_model
        modules = {'huggingface_hub': hub, 'faster_whisper': fake}
        with patch.dict(sys.modules, modules), patch('setup_progress.progress_tqdm', return_value=object):
            for engine in (segment_resemblyzer, pyannote_backend):
                with patch.object(engine, 'initialize_cuda_libraries'), self.assertRaises(StopHere):
                    engine.transcribe_whisper_words(Path('sound.wav'), 'turbo', 'cpu', None)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(entry[2] is object for entry in calls))
        self.assertIs(hub.snapshot_download, snapshot_download)


if __name__ == '__main__':
    unittest.main()
