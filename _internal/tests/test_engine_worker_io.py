from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from engines import worker_io


class WorkerIOTests(unittest.TestCase):
    def test_output_replace_failure_preserves_existing_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            destination = Path(d) / 'report.txt'
            destination.write_bytes(b'old result')
            with patch.object(worker_io.os, 'replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    worker_io.write_transcript_atomic(destination, 'new result')
            self.assertEqual(destination.read_bytes(), b'old result')
            self.assertEqual(list(Path(d).iterdir()), [destination])

    def test_no_overwrite_is_atomic_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            destination = Path(d) / 'report.txt'
            destination.write_bytes(b'old result')
            with self.assertRaises(FileExistsError):
                worker_io.write_transcript_atomic(destination, 'new result', overwrite=False)
            self.assertEqual(destination.read_bytes(), b'old result')
            self.assertEqual(list(Path(d).iterdir()), [destination])

    def test_stalled_child_is_terminated_even_when_heartbeat_continues(self):
        program = ('import sys,time\n'
                   'while True:\n'
                   ' print("out_time_us=1", flush=True);time.sleep(.08)\n')
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            return real_popen([sys.executable, '-u', '-c', program], **kwargs)
        with tempfile.TemporaryDirectory() as d, patch.object(worker_io.subprocess, 'Popen', side_effect=launch):
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, '進まない'):
                worker_io.extract_pcm_audio('ffmpeg', Path(d)/'in', Path(d)/'out', stall_seconds=0.7)
            self.assertLess(time.monotonic() - start, 8)

    def test_advancing_child_can_run_longer_than_stall_interval(self):
        program = ('import time\n'
                   'for i in range(10):\n'
                   ' print("out_time_us="+str(i), flush=True);time.sleep(.15)\n')
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            return real_popen([sys.executable, '-u', '-c', program], **kwargs)
        with tempfile.TemporaryDirectory() as d, patch.object(worker_io.subprocess, 'Popen', side_effect=launch):
            start = time.monotonic()
            worker_io.extract_pcm_audio('ffmpeg', Path(d)/'in', Path(d)/'out', stall_seconds=1.0)
            self.assertGreater(time.monotonic() - start, 1.0)

    def test_failed_child_surfaces_bounded_error_details(self):
        program = ('import sys\n'
                   'sys.stderr.write("x" * 50000 + " decoder failed")\n'
                   'sys.exit(4)\n')
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            return real_popen([sys.executable, '-u', '-c', program], **kwargs)
        with tempfile.TemporaryDirectory() as d, patch.object(worker_io.subprocess, 'Popen', side_effect=launch):
            with self.assertRaises(RuntimeError) as result:
                worker_io.extract_pcm_audio('ffmpeg', Path(d)/'in', Path(d)/'out', stall_seconds=5)
        self.assertIn('decoder failed', str(result.exception))
        self.assertIn('終了コード 4', str(result.exception))
        self.assertLess(len(str(result.exception)), 17000)

    def test_all_diarization_errors_are_not_reported_as_success(self):
        import numpy as np
        from engines import segment_resemblyzer as engine
        def fail(_):
            raise RuntimeError('encoder is broken')
        fake = types.SimpleNamespace(
            VoiceEncoder=lambda **_: types.SimpleNamespace(embed_utterance=fail),
            preprocess_wav=lambda data, source_sr: data,
        )
        audio = np.ones(engine.SAMPLE_RATE * 4, dtype=np.float32)
        chunks = [(0., 2., audio[:engine.SAMPLE_RATE*2]),
                  (2., 4., audio[engine.SAMPLE_RATE*2:])]
        log = StringIO()
        counts = {}
        with redirect_stdout(log), patch.dict(sys.modules, {'resemblyzer': fake}), \
             patch.object(engine, 'load_pcm_wav', return_value=audio), \
             patch.object(engine, 'split_audio_for_resemblyzer', return_value=chunks), \
             patch.object(engine, 'release_cuda_memory'):
            self.assertEqual(engine.get_resemblyzer_speaker_turns(Path('dummy.wav'), 3, 'cpu', 2., warning_counts=counts), [])
        self.assertIn('@@TRANSCRIBER_DIARIZATION_WARNING@@2', log.getvalue())
        self.assertIn('失敗 2件', log.getvalue())
        self.assertEqual(counts, {'failed': 2})

    def test_silent_or_short_segments_still_allow_unknown_speakers(self):
        import numpy as np
        from engines import segment_resemblyzer as engine
        fake = types.SimpleNamespace(
            VoiceEncoder=lambda **_: types.SimpleNamespace(),
            preprocess_wav=lambda data, source_sr: np.array([], dtype=np.float32),
        )
        log = StringIO()
        with redirect_stdout(log), patch.dict(sys.modules, {'resemblyzer': fake}), \
             patch.object(engine, 'load_pcm_wav', return_value=np.ones(2)), \
             patch.object(engine, 'split_audio_for_resemblyzer', return_value=[(0., 2., np.ones(2))]), \
             patch.object(engine, 'release_cuda_memory'):
            self.assertEqual(engine.get_resemblyzer_speaker_turns(Path('dummy.wav'), 3, 'cpu', 2.), [])
        self.assertNotIn('@@TRANSCRIBER_DIARIZATION_WARNING@@', log.getvalue())

    def test_partial_diarization_failure_retains_successful_speaker_and_reports_count(self):
        import numpy as np
        from engines import segment_resemblyzer as engine
        calls = []
        def embed(_):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('failed')
            return np.array([0.3, 0.4], dtype=np.float32)
        fake = types.SimpleNamespace(
            VoiceEncoder=lambda **_: types.SimpleNamespace(embed_utterance=embed),
            preprocess_wav=lambda data, source_sr: data,
        )
        audio = np.ones(engine.SAMPLE_RATE * 4, dtype=np.float32)
        chunks = [(0., 2., audio[:engine.SAMPLE_RATE*2]),
                  (2., 4., audio[engine.SAMPLE_RATE*2:])]
        log = StringIO()
        with redirect_stdout(log), patch.dict(sys.modules, {'resemblyzer': fake}), \
             patch.object(engine, 'load_pcm_wav', return_value=audio), \
             patch.object(engine, 'split_audio_for_resemblyzer', return_value=chunks), \
             patch.object(engine, 'compute_cluster_labels', return_value=[0]), \
             patch.object(engine, 'release_cuda_memory'):
            turns = engine.get_resemblyzer_speaker_turns(Path('dummy.wav'), 3, 'cpu', 2.)
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0].start, turns[0].end), (2., 4.))
        self.assertIn('@@TRANSCRIBER_DIARIZATION_WARNING@@1', log.getvalue())

    def test_main_preserves_asr_text_and_reports_partial_diarization(self):
        from engines import segment_resemblyzer as engine
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)/'source.wav'
            destination = Path(d)/'result.txt'
            source.write_bytes(b'fixture')
            args = types.SimpleNamespace(input=source, output=destination, max_speakers=3,
                speakers=None, threshold=.25, segment_length=2., merge_gap=1.2,
                qwen_max_new_tokens=4096, asr_backend='whisper', model='small', device='cpu',
                compute_type=None, diarization_unit='fixed', caption_max_chars=None,
                caption_max_seconds=5.)
            def diarize(*_args, **kwargs):
                kwargs['warning_counts']['failed'] = 2
                print('@@TRANSCRIBER_DIARIZATION_WARNING@@2')
                return []
            log = StringIO()
            with redirect_stdout(log), patch.object(engine, 'parse_args', return_value=args), \
                 patch.object(engine, 'check_ffmpeg', return_value='ffmpeg'), \
                 patch.object(engine, 'extract_audio'), \
                 patch.object(engine, 'transcribe_whisper_words',
                              return_value=[engine.TranscriptItem(0., 1., '', '文字起こし本文')]), \
                 patch.object(engine, 'release_cuda_memory'), \
                 patch.object(engine, 'get_resemblyzer_speaker_turns', side_effect=diarize):
                self.assertEqual(engine.main(), 0)
            self.assertIn('文字起こし本文', destination.read_text(encoding='utf-8-sig'))
            self.assertIn('UNKNOWN', destination.read_text(encoding='utf-8-sig'))
            self.assertIn('一部完了:', log.getvalue())
            self.assertIn('取得失敗 2件', log.getvalue())


if __name__ == '__main__':
    unittest.main()
