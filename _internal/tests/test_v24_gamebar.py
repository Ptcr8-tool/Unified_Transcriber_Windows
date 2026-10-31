"""v24: no screen capture, separate audio + Game Bar lifecycle, setup integration."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gamebar_recording import (parse_hotkey,GameBarOptions,snapshot_mp4,
                               find_completed_capture,captures_directory)
from bulk_setup import command_sequence
from bulk_catalog import preset
from unified_gui import UnifiedApp
from core import Config

class KeyTests(unittest.TestCase):
    def test_hotkeys(self):
        self.assertEqual(parse_hotkey('win+alt+r'), (0x5B,0x12,ord('R')))
        self.assertEqual(parse_hotkey('ctrl + shift + f9'), (0x11,0x10,0x78))
        for key in ('a','win','alt+r+r','win+r+f9','foo+alt+r','alt+alt+r'):
            with self.subTest(key=key),self.assertRaises(ValueError):parse_hotkey(key)

    def test_options(self):
        with tempfile.TemporaryDirectory() as d:
            opts=GameBarOptions(captures=Path(d),destination=Path(d),target_hwnd=42)
            opts.validate()
            with self.assertRaises(ValueError):GameBarOptions(source='invalid').validate()

    def test_no_ffmpeg_screen_capture_remaining(self):
        from screen_capture import windows,has_audio_stream
        import screen_capture
        self.assertFalse(hasattr(screen_capture,'record'))
        self.assertFalse(hasattr(screen_capture,'capture_command'))
        self.assertNotIn('gdigrab',Path(screen_capture.__file__).read_text())

    def test_pick_only_new_mp4(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);old=directory/'old.mp4';old.write_bytes(b'o'*3000)
            before=snapshot_mp4(directory)
            marker=time.time()
            new=directory/'new.mp4';new.write_bytes(b'n'*3000)
            self.assertEqual(find_completed_capture(directory,before,marker,timeout=2,sleep=lambda _:None),new.resolve())
            self.assertEqual(old.read_bytes(),b'o'*3000)

    def test_none_on_missing(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            self.assertIsNone(find_completed_capture(folder,{},time.time(),timeout=0))

    def test_multiple_new_videos_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);marker=time.time()
            for name in ('1.mp4','2.mp4'):(folder/name).write_bytes(b'x'*3000)
            with self.assertRaisesRegex(RuntimeError,'複数'):
                find_completed_capture(folder,{},marker,timeout=2,sleep=lambda _:None)

    def test_audio_step_added_and_disabled(self):
        plan=preset('recommended-cpu')
        normal=command_sequence(plan)
        added=command_sequence(plan,with_audio=True)
        self.assertEqual(len(added),len(normal)+1)
        self.assertEqual(added[-1][2],'live-capture')
        self.assertIn('setup_live_capture.py',' '.join(added[-1][1]))

class FakeSoundProcess:
    def __init__(self, cmd, **kwargs):
        self.cmd=cmd
        self.wav=Path(cmd[cmd.index('--output')+1]);self.wav.parent.mkdir(parents=True,exist_ok=True)
        self.wav.write_bytes(b'RIFF'+b'0'*200)
        self.file=Path(cmd[cmd.index('--stop-file')+1])
        self.stdout=io.StringIO(json.dumps({'event':'started','path':str(self.wav)})+'\n')
        self.returncode=None
    def poll(self):return self.returncode
    def wait(self,timeout=None):
        self.returncode=0
        return 0
    def kill(self):self.returncode=1

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.app=UnifiedApp.__new__(UnifiedApp)
        import queue
        self.app.events=queue.Queue()
        self.app.gamebar_active=True
        self.app.gamebar_stop=threading.Event()
        self.app.stop_event=threading.Event()
        self.app.quit_when_idle=False
        self.app.config=Config(profile='faster-res')
        self.app._transcribe_completed_screen=MagicMock(return_value=('完了','recognized.txt'))
        self.options=GameBarOptions(target_hwnd=123,source='mic',captures=self.folder,
                                    destination=self.folder,auto_transcribe=True,create_synced_video=False)

    def run_worker(self,video_found=False, transcriber=True):
        shortcuts=[]
        def fake_shortcut(key,*,hwnd=None):
            shortcuts.append((key,hwnd))
            if len(shortcuts)==1:
                self.app.gamebar_stop.set()
        video=self.folder/'gamebar.mp4';video.write_bytes(b'mp4'*500) if video_found else None
        with patch('unified_gui.subprocess.Popen',FakeSoundProcess),\
             patch('gamebar_recording.send_hotkey',side_effect=fake_shortcut),\
             patch('gamebar_recording.snapshot_mp4',return_value={}),\
             patch('gamebar_recording.find_completed_capture',return_value=video),\
             patch('unified_gui.unique_destination',side_effect=lambda value:value):
            python=Path(sys.executable) if transcriber else self.folder/'missing.exe'
            self.app._gamebar_worker(self.options,self.app.config,python,python,None,'')
        return shortcuts

    def test_stop_shortcut_twice_and_asr_wav_only(self):
        shortcuts=self.run_worker(video_found=False)
        self.assertEqual(len(shortcuts),2)
        self.assertEqual(shortcuts[0][0],'win+alt+r')
        self.app._transcribe_completed_screen.assert_called_once()
        audio=self.app._transcribe_completed_screen.call_args.args[0]
        self.assertEqual(audio.suffix,'.wav')
        self.assertTrue(audio.is_file())
        self.assertFalse(self.app.gamebar_active)
        self.assertIn('done',[item[0] for item in list(self.app.events.queue)])

    def test_video_found_transcribes_audio_and_preserves_video(self):
        self.run_worker(video_found=True)
        self.assertEqual(self.app._transcribe_completed_screen.call_count,1)
        self.assertTrue((self.folder/'gamebar.mp4').exists())

    def test_missing_transcriber_preserves_audio(self):
        self.run_worker(transcriber=False)
        self.app._transcribe_completed_screen.assert_not_called()
        self.assertEqual(len(list(self.folder.glob('標準録画_音声*.wav'))),1)

if __name__=='__main__':unittest.main()
