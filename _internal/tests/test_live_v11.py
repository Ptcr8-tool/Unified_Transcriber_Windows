"""Regression tests for continuous WAV capture and concurrent chunk handoff."""
import json
import os
import sys
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from live_capture import RecordingWriter, capture, list_devices
from live_config import LiveOptions, collision_safe_path
from live_transcript import shift_timestamp, shifted_lines, append_provisional


class CaptureWriterTests(unittest.TestCase):
    def test_unbroken_wav_and_exact_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            output=root/'日本語 録音.wav'
            allchunks=[]
            with RecordingWriter(output,sample_rate=1000,channels=1,
                                 chunk_dir=root/'chunks',chunk_seconds=2) as recorder:
                for count in (700,300,1900,1200):
                    allchunks.extend(recorder.feed(np.ones((count,1),dtype=np.float32)*.5))
                allchunks.extend(recorder.finish())
            self.assertEqual(len(allchunks),3)
            self.assertEqual([round(x[1],1) for x in allchunks],[0,2,4])
            self.assertEqual([x[0] for x in allchunks],[0,1,2])
            self.assertEqual([wave.open(str(x[2])).getnframes() for x in allchunks],[2000,2000,100])
            with wave.open(str(output),'rb') as saved:
                self.assertEqual(saved.getnframes(),4100)
                self.assertEqual(saved.getnchannels(),1)
                content=saved.readframes(saved.getnframes())
            self.assertEqual(content,b''.join(wave.open(str(x[2])).readframes(99999) for x in allchunks))

    def test_stereo_conversion_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            wav=root/'capture.wav'
            with RecordingWriter(wav,sample_rate=1000,channels=2,chunk_dir=None) as writer:
                self.assertEqual(writer.feed(np.zeros((10,1),dtype='float32')),[])
            with wave.open(str(wav),'rb') as saved:
                self.assertEqual(saved.getnchannels(),2)
                self.assertEqual(saved.getnframes(),10)
            with self.assertRaises(FileExistsError):
                with RecordingWriter(wav,sample_rate=1000,channels=2,chunk_dir=None):pass

    def test_invalid_pcm_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with RecordingWriter(Path(td)/'a.wav',sample_rate=100,channels=2,
                                 chunk_dir=None) as writer:
                with self.assertRaises(ValueError): writer.feed_pcm(b'123')

    def test_recorder_fake_device_can_stop_without_killing(self):
        class FakeRecorder:
            count=0
            def __init__(self,stop):self.stop=stop
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def record(self,numframes):
                self.count+=1
                if self.count==4:self.stop.touch()
                return np.ones((numframes,1),dtype=np.float32)*.25
        class FakeMic:
            name='Test Mic'
            def __init__(self,stop):self.stop=stop
            def recorder(self,*,samplerate,blocksize):return FakeRecorder(self.stop)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); stop=root/'stop'; wav=root/'out.wav'; chunks=root/'chunks'
            result=capture(output=wav,chunks=chunks,stop_file=stop,sample_rate=1000,
                           chunk_seconds=.4,device=FakeMic(stop))
            self.assertEqual(result,0)
            with wave.open(str(wav),'rb') as handle:
                self.assertEqual(handle.getnframes(),1024)
            self.assertEqual(len(list(chunks.glob('*.wav'))),3)


class LiveOutputTests(unittest.TestCase):
    def test_time_offsets_and_full_text_protection(self):
        self.assertEqual(shift_timestamp('00:00:02.500',20),'00:00:22.500')
        self.assertEqual(shift_timestamp('00:59:59.999',1.001),'01:00:01.000')
        self.assertEqual(shift_timestamp('00:00:59',61),'00:02:00')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); original=root/'part.txt'; result=root/'live.txt'
            original.write_text('[00:00:01] SPEAKER_00: 私はSPEAKER_00と言いました。\n',encoding='utf-8-sig')
            self.assertEqual(append_provisional(original,result,offset=40,index=2,first=True),1)
            content=result.read_text(encoding='utf-8-sig')
            self.assertIn('[00:00:41] SPEAKER_00: 私はSPEAKER_00と言いました。',content)
            self.assertIn('暫定',content)
            append_provisional(original,result,offset=60,index=3)
            self.assertEqual(result.read_text(encoding='utf-8-sig').count('SPEAKER_00と言いました'),2)

    def test_modes_default_and_collisions(self):
        for mode,expected_audio,expected_asr in [('record',True,False),
                  ('transcribe',False,True),('both',True,True)]:
            opts=LiveOptions(mode=mode)
            opts.validate()
            self.assertEqual(opts.keep_audio,expected_audio)
            self.assertEqual(opts.needs_transcription,expected_asr)
        with self.assertRaises(ValueError):LiveOptions(chunk_seconds=19).validate()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            when=datetime(2000,1,21,10,20,30)
            a=collision_safe_path(root,when)
            a.write_bytes(b'x')
            b=collision_safe_path(root,when)
            self.assertEqual(b.stem,a.stem+'_1')

    def test_device_list_distinguishes_loopback(self):
        class Fake:
            def __init__(self,id,name,loop):
                self.id=id;self.name=name;self.isloopback=loop
        class FakeSC:
            def all_microphones(self,include_loopback=False):
                self.include=include_loopback
                return [Fake('M','Microphone',False),Fake('L','Speakers',True)]
        fake=FakeSC()
        with patch('live_capture.sys', SimpleNamespace(platform='linux')), \
                patch.dict(sys.modules,{'soundcard':fake}):
            result=list_devices()
        self.assertEqual([x['source'] for x in result],['mic','loopback'])
        self.assertTrue(fake.include)

    def test_windows_device_list_uses_independent_microphone_and_loopback_backends(self):
        class Loopbacks:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def get_loopback_device_info_generator(self):
                return [{'index':7,'name':'PC playback'}]
        microphone=SimpleNamespace(query_devices=lambda:[
            {'name':'Output only','max_input_channels':0},
            {'name':'Microphone','max_input_channels':1}])
        loopback=SimpleNamespace(PyAudio=Loopbacks)
        with patch('live_capture.sys',SimpleNamespace(platform='win32')), \
                patch.dict(sys.modules,{'sounddevice':microphone,'pyaudiowpatch':loopback}):
            result=list_devices()
        self.assertEqual(result,[{'id':'pa:1','name':'Microphone','source':'mic'},
                                 {'id':'wp:7','name':'PC playback','source':'loopback'}])


if __name__=='__main__':unittest.main()
