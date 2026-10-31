"""Ensure setup and both inference paths select the same reviewed snapshot."""
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_defaults import FASTER_TURBO_REPO, FASTER_TURBO_REVISION, faster_model_identity
from engines import segment_resemblyzer, pyannote_backend


class PinnedFasterModelTests(unittest.TestCase):
    def test_aliases_share_snapshot_other_models_unchanged(self):
        expected = (FASTER_TURBO_REPO, {'revision': FASTER_TURBO_REVISION})
        for name in ('turbo', 'large-v3-turbo'):
            self.assertEqual(faster_model_identity(name), expected)
        self.assertEqual(faster_model_identity('small'), ('small', {}))
        self.assertEqual(faster_model_identity('/local/model'), ('/local/model', {}))

    def test_both_engines_pass_the_pinned_snapshot(self):
        calls = []
        class StopAfterConstruction(Exception):
            pass
        def model(name, *, device, compute_type, revision=None):
            calls.append(((name,), {'device': device, 'compute_type': compute_type,
                                    'revision': revision}))
            raise StopAfterConstruction()
        fake = ModuleType('faster_whisper')
        fake.WhisperModel = model
        with patch.dict(sys.modules, {'faster_whisper': fake}):
            for engine in (segment_resemblyzer, pyannote_backend):
                with patch.object(engine, 'initialize_cuda_libraries'), self.assertRaises(StopAfterConstruction):
                    engine.transcribe_whisper_words(Path('input.wav'), 'turbo', 'cpu', None)
        self.assertEqual(calls, [((FASTER_TURBO_REPO,), {
            'device': 'cpu', 'compute_type': 'int8', 'revision': FASTER_TURBO_REVISION,
        })] * 2)

    def test_older_engines_get_the_pinned_snapshot_directory(self):
        calls, snapshots = [], []
        class StopAfterConstruction(Exception):
            pass
        def old_model(name, device, compute_type):
            calls.append((name, device, compute_type))
            raise StopAfterConstruction()
        fake, hub = ModuleType('faster_whisper'), ModuleType('huggingface_hub')
        fake.WhisperModel = old_model
        hub.snapshot_download = lambda **kwargs: snapshots.append(kwargs) or '/cache/pinned-snapshot'
        with patch.dict(sys.modules, {'faster_whisper': fake, 'huggingface_hub': hub}):
            for engine in (segment_resemblyzer, pyannote_backend):
                with patch.object(engine, 'initialize_cuda_libraries'), self.assertRaises(StopAfterConstruction):
                    engine.transcribe_whisper_words(Path('input.wav'), 'turbo', 'cpu', None)
        self.assertEqual(calls, [('/cache/pinned-snapshot', 'cpu', 'int8')] * 2)
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(call['revision'] == FASTER_TURBO_REVISION and
                            call['repo_id'] == FASTER_TURBO_REPO for call in snapshots))

    def test_older_preload_downloads_snapshot_without_calling_old_downloader(self):
        import preload_model
        fake, utils, hub = ModuleType('faster_whisper'), ModuleType('faster_whisper.utils'), ModuleType('huggingface_hub')
        def old_download(name):
            self.fail('Pinned snapshot should be downloaded via Hugging Face')
        utils.download_model = old_download
        snapshots = []
        hub.snapshot_download = lambda **kwargs: snapshots.append(kwargs) or '/cache/pinned-snapshot'
        with patch.dict(sys.modules, {'faster_whisper': fake, 'faster_whisper.utils': utils, 'huggingface_hub': hub}):
            preload_model.preload('faster', 'large-v3-turbo')
        self.assertEqual(snapshots[0]['revision'], FASTER_TURBO_REVISION)
        self.assertEqual(snapshots[0]['repo_id'], FASTER_TURBO_REPO)

    def test_older_model_with_forwarded_kwargs_uses_snapshot(self):
        calls, snapshots = [], []
        class StopAfterConstruction(Exception):
            pass
        def old_model(name, device, compute_type, **model_kwargs):
            calls.append((name, device, compute_type, model_kwargs))
            raise StopAfterConstruction()
        fake, hub = ModuleType('faster_whisper'), ModuleType('huggingface_hub')
        fake.WhisperModel = old_model
        hub.snapshot_download = lambda **kwargs: snapshots.append(kwargs) or '/cache/pinned-snapshot'
        with patch.dict(sys.modules, {'faster_whisper': fake, 'huggingface_hub': hub}):
            for engine in (segment_resemblyzer, pyannote_backend):
                with patch.object(engine, 'initialize_cuda_libraries'), self.assertRaises(StopAfterConstruction):
                    engine.transcribe_whisper_words(Path('input.wav'), 'turbo', 'cpu', None)
        self.assertEqual(calls, [('/cache/pinned-snapshot', 'cpu', 'int8', {})] * 2)
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(call['revision'] == FASTER_TURBO_REVISION for call in snapshots))


if __name__ == '__main__':
    unittest.main()
