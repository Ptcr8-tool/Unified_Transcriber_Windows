"""v21 screen capture → finalized MP4 → ordinary transcription regressions."""
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screen_capture import has_audio_stream
from core import Config, choose_output
from unified_gui import UnifiedApp


class StreamInspectionTests(unittest.TestCase):
    def test_real_audio_and_silent_video(self):
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            for sound in (True,False):
                movie=base/('sound.mp4' if sound else 'silent.mp4')
                cmd=[ffmpeg,'-hide_banner','-loglevel','error','-y',
                     '-f','lavfi','-i','color=c=blue:s=160x120:r=5:d=1']
                if sound:
                    cmd+=['-f','lavfi','-i','sine=frequency=440:duration=1']
                cmd+=['-t','1','-c:v','mpeg4']
                if sound:
                    cmd+=['-c:a','aac','-shortest']
                cmd+=[str(movie)]
                subprocess.run(cmd,check=True,capture_output=True)
                self.assertEqual(has_audio_stream(movie,ffmpeg),sound)

    def test_invalid_media_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as td:
            movie=Path(td)/'corrupt.mp4'
            movie.write_bytes(b'bad video')
            ffmpeg=shutil.which('ffmpeg')
            if not ffmpeg:self.skipTest('FFmpeg unavailable')
            with self.assertRaisesRegex(RuntimeError,'読み取れません'):
                has_audio_stream(movie,ffmpeg)
