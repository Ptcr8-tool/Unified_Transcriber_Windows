"""v10: TXT/audio merging, creation metadata, token safety and GUI glue."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config, choose_output, output_name, load_config, save_config
from media_tools import (parse_creation_time, find_creation_tag, probe_metadata,
    metadata_header, merge_text_files, concat_audio, media_binary, audio_codec_args, MediaMetadata)
from unified_gui import ConcatDialog, UnifiedApp
import test_v25_studio_ui as fixtures


class TxtMergeTest(unittest.TestCase):
    def test_multi_encoding_order_and_headings(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            a=root/'会議①.txt'; b=root/'発言 2.txt'; out=root/'統合.txt'
            a.write_text('こんにちは\n',encoding='utf-8-sig')
            b.write_bytes('二つめ'.encode('utf-16'))
            merge_text_files([a,b],out)
            result=out.read_text(encoding='utf-8')
            self.assertLess(result.index('こんにちは'),result.index('二つめ'))
            self.assertIn(a.name,result)
            self.assertIn(b.name,result)
            self.assertEqual(a.read_text(encoding='utf-8-sig'),'こんにちは\n')
            with self.assertRaises(FileExistsError):
                merge_text_files([a,b],out)
            self.assertEqual(out.read_text(encoding='utf-8'),result)

    def test_cp932_and_no_headings(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td); a=d/'a.txt'; b=d/'b.txt'
            a.write_bytes('日本語'.encode('cp932'))
            b.write_text('Bravo',encoding='utf-8')
            out=d/'out.txt'
            merge_text_files([b,a],out,headings=False)
            self.assertEqual(out.read_text(encoding='utf-8'), 'Bravo\n\n日本語\n')

    def test_failure_does_not_create_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td); a=d/'1.txt';b=d/'2.txt';out=d/'joined.txt'
            a.write_text('okay',encoding='utf-8')
            b.write_bytes(b'\x00\x81\x00\x81')
            # Invalid input can raise, but must never write an incomplete output.
            try:
                merge_text_files([a,b],out)
            except (UnicodeError,ValueError):
                self.assertFalse(out.exists())


class MediaDateTest(unittest.TestCase):
    def test_tag_priority_and_timezone(self):
        d,label = find_creation_tag({'creation_time':'2000-01-21T00:00:00Z',
                                     'com.apple.quicktime.creationdate':'2000-01-20T13:22:05+09:00'})
        self.assertEqual(label,'com.apple.quicktime.creationdate')
        self.assertEqual(d.hour,13)
        self.assertEqual(d.tzinfo.utcoffset(d),timezone.utc.utcoffset(d) if False else
                         datetime.fromisoformat('2000-01-20T13:22:05+09:00').utcoffset())

    def test_missing_creation_stays_missing(self):
        self.assertIsNone(parse_creation_time(''))
        self.assertIsNone(parse_creation_time('test'))
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'missing.bin';p.write_bytes(b'xx')
            with patch('media_tools.media_binary', return_value=None):
                m=probe_metadata(p)
                if os.name!='nt':
                    self.assertIsNone(m.created)

    def test_filename_has_creation_when_known(self):
        cfg=Config(filename_media_creation=True,filename_timestamp=True)
        name=output_name(Path('record.m4a'),cfg,
                transcribed_at=datetime(2000,1,21,15),
                media_created_at=datetime(2000,1,20,10,11,12))
        self.assertIn('recorded-20000120_101112',name)
        name_unknown=output_name(Path('record.m4a'),cfg,transcribed_at=datetime(2000,1,21,15))
        self.assertNotIn('recorded-',name_unknown)

    def test_metadata_header_includes_components(self):
        one=MediaMetadata(Path('source A.m4a'),datetime(2000,9,20,12,0),'メディアタグ: creation_time')
        header=metadata_header(MediaMetadata(Path('merged.mp3')),
             transcribed_at=datetime(2000,1,21,8,0),profile='faster',model='turbo',device='cuda',
             components=[one,MediaMetadata(Path('source B.mp4'))])
        self.assertIn('source A.m4a',header)
        self.assertIn('2000-09-20 12:00:00',header)
        self.assertIn('取得不可',header)
        self.assertIn('cuda',header)


class TokenStorageTest(unittest.TestCase):
    def test_default_no_token_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'settings.json';save_config(Config(),path)
            self.assertNotIn('hf_token_json',json.loads(path.read_text(encoding='utf-8')))

    def test_explicit_opt_in_saves_and_can_clear(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'settings.json'
            c=Config(hf_token_json='hf_an_explicit_example')
            save_config(c,path)
            self.assertEqual(load_config(path).hf_token_json,'hf_an_explicit_example')
            c.hf_token_json='';save_config(c,path)
            self.assertNotIn('hf_token_json',json.loads(path.read_text(encoding='utf-8')))

    def test_register_rejects_invalid(self):
        from hf_auth import register_hf_token
        with self.assertRaises(ValueError):
            register_hf_token('')
        with self.assertRaises(ValueError):
            register_hf_token('aaa\nbbb')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe absent')
class RealAudioConcatTest(unittest.TestCase):
    def test_wav_m4a_video_to_mp3_and_metadata(self):
        with tempfile.TemporaryDirectory(prefix='日本語 media_') as d:
            root=Path(d)
            inputs=[root/'one.wav',root/'two.m4a',root/'動画 3.mp4']
            for i,p in enumerate(inputs):
                base=[shutil.which('ffmpeg'),'-hide_banner','-loglevel','error','-y',
                    '-f','lavfi','-i',f'sine=frequency={330+i*100}:duration=0.28']
                if p.suffix=='.mp4':
                    base+=['-f','lavfi','-i','color=size=16x16:rate=10:duration=0.28',
                           '-map','1:v','-map','0:a','-c:v','mpeg4']
                else:
                    base+=['-ac','1']
                base+=['-metadata','creation_time=2000-01-20T10:20:30Z',str(p)]
                subprocess.run(base,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            meta=probe_metadata(inputs[1])
            self.assertIsNotNone(meta.created)
            target=root/'音声 結合.mp3'
            result=concat_audio(inputs,target,ffmpeg=shutil.which('ffmpeg'),fmt='mp3',bitrate='128k')
            self.assertEqual(target,result)
            self.assertGreater(target.stat().st_size,1000)
            info=probe_metadata(target)
            self.assertGreater(info.duration_seconds,0.55)
            self.assertLess(info.duration_seconds,1.5)
            with self.assertRaises(FileExistsError):
                concat_audio(inputs,target,ffmpeg=shutil.which('ffmpeg'))

    def test_wav_output_bitrate_not_requested(self):
        self.assertIn('pcm_s16le',audio_codec_args('wav','192k',2,44100))
        self.assertNotIn('-b:a',audio_codec_args('wav','192k',2,44100))
        self.assertIn('aac',audio_codec_args('m4a','192k',1,48000))


class MergeWorkerTest(unittest.TestCase):
    def test_auto_transcribe_chain(self):
        app=MagicMock()
        app.stop_event=MagicMock()
        app.stop_event.is_set.return_value=False
        app.events=queue.Queue()
        app._resolve_device.return_value='cuda'
        app._run_one.return_value=('完了','okay')
        cfg=Config(filename_media_creation=True)
        sources=[Path('one.m4a'),Path('two.m4a')]
        with patch('unified_gui.concat_audio') as combine, \
             patch('unified_gui.probe_metadata',return_value=MediaMetadata(sources[0])):
            UnifiedApp._audio_merge_worker(app,sources,Path('combined.mp3'),
                'mp3','192k',1,48000,True,cfg,Path('/test/python'),'ffmpeg','')
        self.assertTrue(combine.called)
        self.assertTrue(app._run_one.called)
        self.assertEqual(app.events.get_nowait()[0],'log')
        events=[]
        while not app.events.empty():events.append(app.events.get_nowait())
        self.assertEqual(events[-1][0],'done')
        self.assertIn('完了',events[-1][1])

    def test_no_auto_transcribe(self):
        app=MagicMock()
        app.stop_event.is_set.return_value=False
        app.events=queue.Queue()
        with patch('unified_gui.concat_audio'), \
             patch('unified_gui.probe_metadata',return_value=MediaMetadata(Path('source.m4a'))):
            UnifiedApp._audio_merge_worker(app,[Path('one.m4a'),Path('two.m4a')],
                Path('combined.mp3'),'mp3','192k',1,48000,False,Config(),None,'ffmpeg','')
        app._run_one.assert_not_called()


class ModalSmokeTest(fixtures.StudioGUIFixture):
    def test_gui_modals_construct(self):
        audio=ConcatDialog(self.app,'audio')
        self.assertTrue(audio.auto_transcribe.get())
        self.assertEqual(audio.fmt.get(),'mp3')
        audio.win.destroy()
        text=ConcatDialog(self.app,'text')
        self.assertTrue(text.headings.get())
        text.win.destroy()
        from unified_gui import SettingsDialog
        dialog=SettingsDialog(self.app)
        self.assertTrue(dialog.output_flags['txt_metadata_header'].get())
        dialog.win.destroy()

if __name__=='__main__':unittest.main()

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
class OtherCodecIntegrationTest(unittest.TestCase):
    def test_m4a_wav_flac_outputs(self):
        with tempfile.TemporaryDirectory(prefix='日本語 codecs_') as td:
            root=Path(td)
            sources=[root/'part_1.wav',root/'part_2.wav']
            for index,source in enumerate(sources):
                subprocess.run([shutil.which('ffmpeg'),'-loglevel','error','-y',
                    '-f','lavfi','-i',f'sine=frequency={500+index*50}:duration=0.1',str(source)],
                    check=True,capture_output=True)
            for fmt in ('m4a','wav','flac'):
                result=root/f'output.{fmt}'
                concat_audio(sources,result,ffmpeg=shutil.which('ffmpeg'),fmt=fmt,
                             channels=2,sample_rate=44100)
                self.assertGreater(result.stat().st_size,200)
                self.assertGreater(probe_metadata(result).duration_seconds,0.15)

    def test_cancel_before_start_protects_destination(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            a=root/'x.wav';b=root/'y.wav'
            a.write_bytes(b'not a media file');b.write_bytes(b'not a media file')
            cancelled=threading.Event();cancelled.set()
            out=root/'out.mp3'
            with self.assertRaises(InterruptedError):
                concat_audio([a,b],out,ffmpeg=shutil.which('ffmpeg'),cancel=cancelled)
            self.assertFalse(out.exists())
