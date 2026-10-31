"""v16 regression: source engine, recording formats, safe final commit, default dirs."""
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import sys

from core import BASE, Config, default_directory, choose_output
from output_locations import default_output_root
from live_config import LiveOptions, unique_media_path
from media_tools import recording_codec_args, encode_live_recording
from engines import segment_resemblyzer


class DefaultOutputs(unittest.TestCase):
    def test_subfolders_under_documents_output_root(self):
        for kind, folder in (('recordings', 'Recordings'), ('transcriptions', 'Transcriptions'),
                             ('merged', 'Merged'), ('speaker_edits', 'SpeakerEdits')):
            self.assertEqual(default_directory(kind, Config()), default_output_root() / folder)
        self.assertEqual(choose_output(Path('sample.m4a'), Config()),
                         default_output_root() / 'Transcriptions' / 'sample.txt')

    def test_user_selected_output_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=Config(output_dir=td)
            self.assertEqual(choose_output(Path('sample.wav'), cfg), Path(td)/'sample.txt')

    def test_unique_media_destination(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'recording.mp3'
            self.assertEqual(unique_media_path(path),path)
            path.write_bytes(b'x')
            self.assertEqual(unique_media_path(path),path.with_name('recording_1.mp3'))


class RecordingCodecTests(unittest.TestCase):
    def test_valid_options_and_format_checks(self):
        for fmt in ('wav','mp3','m4a','flac'):
            LiveOptions(recording_format=fmt, recording_bitrate='192k',
                        recording_channels=1,recording_pcm_bits=24).validate()
        for kwargs in ({'recording_format':'bad'}, {'recording_pcm_bits':32},
                       {'recording_bitrate':'fake'}, {'recording_sample_rate':96000},
                       {'recording_channels':3}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                LiveOptions(**kwargs).validate()
        self.assertIn('libmp3lame',recording_codec_args('mp3','192k',16,2,48000))
        self.assertIn('aac',recording_codec_args('m4a','192k',16,1,44100))
        self.assertIn('pcm_s24le',recording_codec_args('wav','192k',24,2,44100))

    def _make_wav(self,path):
        with wave.open(str(path),'wb') as handle:
            handle.setnchannels(2)
            handle.setframerate(48000)
            handle.setsampwidth(2)
            handle.writeframes(b'\x00\x00'*2*4800)

    def test_end_to_end_encoding_ffmpeg(self):
        exe=shutil.which('ffmpeg')
        if not exe:
            self.skipTest('FFmpeg unavailable in CI; use installed ffmpeg in Windows venv')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            original=root/'capture.wav'
            self._make_wav(original)
            for fmt,bits in (('mp3',16),('m4a',16),('flac',16),('flac',24),('wav',24)):
                with self.subTest(fmt=fmt,bits=bits):
                    target=root/f'encoded_{bits}.{fmt}'
                    result=encode_live_recording(original,target,ffmpeg=exe,fmt=fmt,
                        bitrate='192k',pcm_bits=bits,channels=2,sample_rate=44100)
                    self.assertEqual(result,target)
                    self.assertTrue(target.is_file())
                    self.assertGreater(target.stat().st_size,50)
                    self.assertTrue(original.is_file())

    def test_failure_preserves_wav_and_does_not_write_destination(self):
        with tempfile.TemporaryDirectory() as td:
            original=Path(td)/'record.wav'
            target=Path(td)/'encoded.mp3'
            self._make_wav(original)
            def failing_run(args):
                output=Path(args[-1]);output.write_bytes(b'corrupt')
                return subprocess.CompletedProcess(args,1,'','broken encoder')
            with self.assertRaisesRegex(RuntimeError,'broken encoder'):
                encode_live_recording(original,target,ffmpeg='not-called',run=failing_run)
            self.assertTrue(original.exists())
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(td).glob('.*.mp3')),[])

    def test_no_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            original=Path(td)/'record.wav';target=Path(td)/'record.mp3'
            self._make_wav(original);target.write_bytes(b'PREEXISTING')
            with self.assertRaises(FileExistsError):
                encode_live_recording(original,target,ffmpeg='not-called')
            self.assertEqual(target.read_bytes(),b'PREEXISTING')


class RuntimeRegressionTests(unittest.TestCase):
    def test_faster_qwen_shared_engine_routes_to_ffmpeg_extractor(self):
        # The v15.2 NameError must not reappear in the common extractor route.
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'in.wav';output=Path(td)/'out.wav'
            with patch.object(segment_resemblyzer,'extract_pcm_audio') as run:
                segment_resemblyzer.extract_audio('ffmpeg',source,output)
                self.assertEqual(run.call_count,1)
                self.assertEqual(run.call_args.args, ('ffmpeg',source,output))


if __name__=='__main__':unittest.main()
