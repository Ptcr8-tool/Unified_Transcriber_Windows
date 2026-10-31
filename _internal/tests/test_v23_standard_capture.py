import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from external_recording import ExternalOptions,finalize_external,mux_command,video_duration
from screen_capture import has_audio_stream
from unified_gui import UnifiedApp
from core import Config,choose_output


class ExternalCaptureTests(unittest.TestCase):
    def test_missing_audio_requires_no_output(self):
        with tempfile.TemporaryDirectory() as td:
            video=Path(td)/'input.mp4';video.write_bytes(b'video')
            opts=ExternalOptions(video=video)
            opts.validate()
            self.assertEqual(finalize_external(opts,'unused',has_audio=lambda *_: False),video)
            self.assertEqual(video.read_bytes(),b'video')

    def test_invalid_input_and_original_protection(self):
        with tempfile.TemporaryDirectory() as td:
            video=Path(td)/'video.mp4';audio=Path(td)/'audio.wav'
            video.write_bytes(b'v');audio.write_bytes(b'a')
            with self.assertRaisesRegex(ValueError,'原本'):
                ExternalOptions(video,audio,destination=video).validate()
            with self.assertRaises(ValueError):
                ExternalOptions(video,audio,offset=9000,destination=Path(td)/'out.mp4').validate()

    def test_sync_filter_directions_and_stream_copy(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);v=base/'video.mp4';a=base/'audio.wav'
            v.write_bytes(b'v');a.write_bytes(b'a')
            for offset,expected in [(-2.5,'atrim=start=2.500'),(1.75,'adelay=1750:all=1')]:
                opts=ExternalOptions(v,a,offset,'mix',base/'output.mp4')
                cmd=mux_command('ffmpeg',opts,base/'work.mp4',duration=10,video_has_audio=True)
                self.assertIn('-c:v',cmd);self.assertIn('copy',cmd)
                self.assertIn(expected,' '.join(cmd))
                self.assertIn('amix=inputs=2',' '.join(cmd))
            cmd=mux_command('ffmpeg',ExternalOptions(v,a,destination=base/'output.mp4'),base/'work.mp4',duration=10,video_has_audio=False)
            self.assertNotIn('amix',' '.join(cmd))

    def test_actual_ffmpeg_mux_preserves_video_and_originals(self):
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:self.skipTest('ffmpeg is required')
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);video=base/'original.mp4';audio=base/'separate.wav'
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','color=c=blue:s=320x240:r=10:d=3',
                '-f','lavfi','-i','sine=frequency=330:duration=3',
                '-c:v','mpeg4','-c:a','aac','-shortest',str(video)],check=True,capture_output=True)
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','sine=frequency=660:duration=4',str(audio)],check=True,capture_output=True)
            vbytes=video.read_bytes();abytes=audio.read_bytes()
            for i,(offset,mode) in enumerate([(-0.5,'replace'),(0.35,'mix')]):
                out=base/f'combined{i}.mp4'
                result=finalize_external(ExternalOptions(video,audio,offset,mode,out),ffmpeg,
                                         has_audio=has_audio_stream)
                self.assertEqual(result,out)
                self.assertTrue(has_audio_stream(out,ffmpeg))
                self.assertLess(video_duration(out,ffmpeg),3.5)
            self.assertEqual(video.read_bytes(),vbytes)
            self.assertEqual(audio.read_bytes(),abytes)

    def test_failed_mux_keeps_original_files_and_no_output(self):
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:self.skipTest('ffmpeg is required')
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);video=base/'broken.mp4';audio=base/'broken.wav'
            video.write_bytes(b'bogus');audio.write_bytes(b'bogus')
            output=base/'out.mp4'
            with self.assertRaises((RuntimeError,ValueError)):
                finalize_external(ExternalOptions(video,audio,destination=output),ffmpeg,
                                  has_audio=lambda *_:False)
            self.assertFalse(output.exists())
            self.assertEqual(video.read_bytes(),b'bogus')


class StandardWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.video=Path(self.tmp.name)/'recorded.mp4';self.video.write_bytes(b'original')
        self.app=UnifiedApp.__new__(UnifiedApp)
        self.app.events=queue.Queue()
        self.app.stop_event=threading.Event()
        self.app.quit_when_idle=False
        self.cfg=Config(profile='faster-res',output_mode='txt',output_dir=self.tmp.name)
    def all_events(self):
        rows=[]
        while not self.app.events.empty():rows.append(self.app.events.get_nowait())
        return rows
    def test_just_use_original_without_transcription(self):
        opts=ExternalOptions(self.video)
        with patch('unified_gui.finalize_external',return_value=self.video):
            self.app._standard_import_worker(opts,'ffmpeg',False,self.cfg,None,'')
        self.assertIn('取り込みました',str(self.all_events()))
    def test_runs_transcription_once_using_published_video(self):
        opts=ExternalOptions(self.video)
        with patch('unified_gui.finalize_external',return_value=self.video), \
             patch('unified_gui.has_audio_stream',return_value=True), \
             patch.object(self.app,'_transcribe_completed_screen',return_value=('完了','done')) as asr:
            self.app._standard_import_worker(opts,'ffmpeg',True,self.cfg,Path(sys.executable),'')
        # Windows TEMP may use an 8.3 short name; queue keys are canonical.
        asr.assert_called_once_with(self.video.resolve(),self.cfg,Path(sys.executable),'')
        self.assertIn('文字起こしが完了',str(self.all_events()))
    def test_no_audio_skips_asr(self):
        with patch('unified_gui.finalize_external',return_value=self.video), \
             patch('unified_gui.has_audio_stream',return_value=False), \
             patch.object(self.app,'_transcribe_completed_screen') as asr:
            self.app._standard_import_worker(ExternalOptions(self.video),'ffmpeg',True,self.cfg,Path(sys.executable),'')
        asr.assert_not_called()
        self.assertIn('音声ストリームがない',str(self.all_events()))
    def test_mux_failures_keep_orig_video(self):
        with patch('unified_gui.finalize_external',side_effect=RuntimeError('mux failed')):
            self.app._standard_import_worker(ExternalOptions(self.video),'ffmpeg',True,self.cfg,Path(sys.executable),'')
        self.assertTrue(self.video.exists())
        self.assertIn('mux failed',str(self.all_events()))

if __name__=='__main__':unittest.main()

class EndToEndOsCaptureTest(unittest.TestCase):
    def test_real_video_with_audio_to_txt_csv_without_reencoding(self):
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:self.skipTest('ffmpeg unavailable')
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            video=base/'GameBar Clip.mp4'
            separate=base/'recording.wav'
            combined=base/'synced.mp4'
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y',
               '-f','lavfi','-i','testsrc2=s=160x120:r=10:d=2',
               '-f','lavfi','-i','sine=frequency=440:duration=2',
               '-c:v','mpeg4','-c:a','aac','-shortest',str(video)],check=True,capture_output=True)
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y',
               '-f','lavfi','-i','sine=frequency=440:duration=3',str(separate)],check=True,capture_output=True)
            cfg=Config(profile='faster-res',output_mode='both',txt_metadata_header=False,output_dir=td)
            app=UnifiedApp.__new__(UnifiedApp)
            app.events=queue.Queue();app.stop_event=threading.Event();app.quit_when_idle=False
            app.process_lock=threading.Lock();app.active_process=None
            app.interactive_collisions=False;app.device_cache={};app.config=cfg
            app._resolve_device=MagicMock(return_value='cpu')
            script=("import pathlib,sys;" "pathlib.Path(sys.argv[1]).write_text('[00:00:00] SPEAKER_00: hello Game Bar\\n',encoding='utf-8')")
            opts=ExternalOptions(video,separate,offset=-0.15,mode='mix',destination=combined)
            with patch('unified_gui.build_command',side_effect=lambda src,out,cfg,py,device:
                   [sys.executable,'-c',script,str(out)]):
                app._standard_import_worker(opts,ffmpeg,True,cfg,Path(sys.executable),'')
            self.assertTrue(combined.exists())
            self.assertTrue(video.exists())
            self.assertTrue(separate.exists())
            self.assertTrue(has_audio_stream(combined,ffmpeg))
            self.assertTrue(choose_output(combined,cfg).exists())
            self.assertTrue(choose_output(combined,cfg).with_suffix('.csv').exists())
