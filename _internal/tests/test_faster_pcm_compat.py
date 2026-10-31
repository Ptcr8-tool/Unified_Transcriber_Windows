"""Regression tests for faster-whisper/PyAV incompatibility and WAV handling."""
import io
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines import segment_resemblyzer, pyannote_backend, worker_io
import setup_backend


def write_wav(path, samples, *, channels=1, sample_width=2, rate=16000):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(rate)
        output.writeframes(samples)


class FasterPcmTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is unavailable')
    def test_stereo_recording_uses_existing_downmix_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            stereo = Path(directory) / 'stereo.wav'
            mono = Path(directory) / 'mono.wav'
            frames = np.empty((48000, 2), dtype='<i2')
            frames[:, 0], frames[:, 1] = 10000, -5000
            write_wav(stereo, frames.tobytes(), channels=2, rate=48000)
            worker_io.extract_pcm_audio(shutil.which('ffmpeg'), stereo, mono)
            audio = worker_io.read_pcm_for_whisper(mono)
            self.assertEqual(audio.shape, (16000,))
            # FFmpeg's existing stereo-to-mono conversion averages L and R.
            self.assertAlmostEqual(float(audio[8000]), 2500 / 32768, delta=0.001)

    def test_pcm_scaling_and_multiple_chunks_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'long.wav'
            values = np.resize(np.array([-32768, -1, 0, 1, 32767], dtype='<i2'), 300001)
            write_wav(path, values.tobytes())
            result = worker_io.read_pcm_for_whisper(path)
            self.assertEqual(result.dtype, np.float32)
            self.assertEqual(len(result), len(values))
            np.testing.assert_array_equal(result, values.astype(np.float32) / 32768.0)

    def test_wrong_format_or_corrupted_wav_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.wav'
            for values in ({'channels': 2}, {'sample_width': 1}, {'rate': 44100}):
                write_wav(path, b'\0' * 100, **values)
                with self.subTest(values=values), self.assertRaisesRegex(RuntimeError, '形式'):
                    worker_io.read_pcm_for_whisper(path)
            write_wav(path, b'\0' * 200)
            content = path.read_bytes()
            path.write_bytes(content[:-40])
            with self.assertRaisesRegex(RuntimeError, '途切れ'):
                worker_io.read_pcm_for_whisper(path)
            path.write_bytes(b'broken')
            with self.assertRaisesRegex(RuntimeError, '読み込めません'):
                worker_io.read_pcm_for_whisper(path)

    def test_all_faster_engines_pass_prepared_samples_directly(self):
        calls = []
        class Model:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(self, audio, **kwargs):
                calls.append((audio, kwargs))
                return [], SimpleNamespace(language='ja', duration=len(audio) / 16000)
        fake = ModuleType('faster_whisper')
        fake.WhisperModel = Model
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.wav'
            values = np.array([-32768, -100, 0, 100, 32767], dtype='<i2')
            write_wav(path, values.tobytes())
            with patch.dict(sys.modules, {'faster_whisper': fake}):
                for engine in (segment_resemblyzer, pyannote_backend):
                    with patch.object(engine, 'initialize_cuda_libraries'):
                        self.assertEqual(engine.transcribe_whisper_words(path, 'small', 'cpu', None), [])
        self.assertEqual(len(calls), 2)
        for audio, kwargs in calls:
            np.testing.assert_array_equal(audio, values.astype(np.float32) / 32768.0)
            self.assertTrue(kwargs['vad_filter'])
            self.assertEqual(kwargs['language'], 'ja')

    def test_setup_selects_compatible_pyav_and_checks_decode(self):
        calls = []
        with patch.object(setup_backend, 'run', side_effect=lambda *args: calls.append(args)), \
             patch.object(setup_backend, 'ensure_pip'), \
             patch.object(setup_backend, 'repair_compression_dependencies'), \
             patch.object(setup_backend, 'torch_ready', return_value=True):
            setup_backend.install_profile('faster-res', 'cpu', Path('python'))
        installs = [c for c in calls if 'faster-whisper==1.2.1' in c]
        self.assertEqual(len(installs), 1)
        self.assertIn('av==18.1.0', installs[0])
        self.assertTrue(any('from faster_whisper.audio import decode_audio' in arg
                            for c in calls for arg in c))

    def test_both_faster_engines_pass_vocabulary_to_model(self):
        calls = []
        class Model:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(self, audio, **kwargs):
                calls.append(kwargs)
                return [], SimpleNamespace(language='ja', duration=0.1)
        fake = ModuleType('faster_whisper')
        fake.WhisperModel = Model
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.wav'
            write_wav(path, b'\0\0' * 1600)
            with patch.dict(sys.modules, {'faster_whisper': fake}), \
                 patch.dict('os.environ', {'TRANSCRIBER_ASR_HINTS': 'サンプル社、テスト会議'}):
                for engine in (segment_resemblyzer, pyannote_backend):
                    with patch.object(engine, 'initialize_cuda_libraries'):
                        engine.transcribe_whisper_words(path, 'small', 'cpu', None)
        self.assertEqual([c['hotwords'] for c in calls], ['サンプル社、テスト会議'] * 2)


if __name__ == '__main__':
    unittest.main()
