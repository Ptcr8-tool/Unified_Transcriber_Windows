"""Windows SoundCard import failure regression; backends must not import it."""
from __future__ import annotations
import contextlib
import io
import json
import sys
import types
import time
import tempfile
import wave
import unittest
from unittest.mock import patch
import numpy as np
from live_capture import list_devices, get_device, probe_device, _WASAPILoopbackInput


class FakeSDStream:
    def __init__(self, **kw): self.kw=kw; self.active=False
    def start(self): self.active=True
    def stop(self): self.active=False
    def close(self): pass
    def read(self, count):
        time.sleep(min(0.10,count/48000))
        return np.ones((count,1),dtype=np.float32)*0.1, False


class FakePaStream:
    def __init__(self, **kw): self.kw=kw
    def read(self, n, exception_on_overflow=False):
        time.sleep(min(0.10,n/48000))
        return np.full((n,2), 3276, dtype='<i2').tobytes()
    def stop_stream(self):pass
    def close(self):pass


class FakePA:
    paInt16=8
    def __init__(self):self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.terminate()
    def terminate(self):self.closed=True
    def get_default_wasapi_loopback(self): return {'name':'Speakers Loopback','index':7,'maxInputChannels':2,'isLoopbackDevice':True, 'defaultSampleRate':48000}
    def get_device_info_by_index(self,idx):
        if idx != 7:raise IndexError(idx)
        return self.get_default_wasapi_loopback()
    def get_loopback_device_info_generator(self):
        yield self.get_default_wasapi_loopback()
    def open(self,**kw):return FakePaStream(**kw)


class TestWindowsBackend(unittest.TestCase):
    def setUp(self):
        self.streams=[]
        sd=types.SimpleNamespace(query_devices=lambda:[{'name':'Realtek Microphone','max_input_channels':2,'default_samplerate':48000},
                                                         {'name':'Speakers','max_input_channels':0,'default_samplerate':48000}],
                        default=types.SimpleNamespace(device=[0,1]),
                        InputStream=lambda **kw: FakeSDStream(**kw))
        pawp=types.SimpleNamespace(PyAudio=FakePA,paInt16=8)
        self.patchers=[patch('live_capture.sys.platform','win32'),patch.dict(sys.modules,{'sounddevice':sd,'pyaudiowpatch':pawp}),patch('live_capture.windows_audio_com',return_value=contextlib.nullcontext())]
        for p in self.patchers: p.start()
    def tearDown(self):
        for p in reversed(self.patchers):p.stop()

    def test_list_mic_and_loopback_without_importing_soundcard(self):
        with patch.dict(sys.modules,{'soundcard':None}):
            devices=list_devices()
        self.assertEqual([item['id'] for item in devices],['pa:0','wp:7'])
        self.assertEqual([item['source'] for item in devices],['mic','loopback'])

    def test_selected_and_default_mic_probed_without_soundcard(self):
        with patch.dict(sys.modules,{'soundcard':None}):
            mic=get_device('mic','pa:0')
            measured=probe_device(source='mic', device_id='pa:0',frames=1200)
            default=probe_device(source='mic',frames=1200)
        self.assertEqual(mic.name,'Realtek Microphone')
        self.assertEqual(measured['frames'],1200)
        self.assertEqual(default['device'],'Realtek Microphone')

    def test_loopback_default_probe_works_without_soundcard(self):
        with patch.dict(sys.modules,{'soundcard':None}):
            loop=get_device('loopback','')
            measured=probe_device(source='loopback',device_id='wp:7',frames=2500)
        self.assertIsInstance(loop,_WASAPILoopbackInput)
        self.assertEqual(measured['sample_rate'],48000)
        self.assertEqual(measured['frames'],2500)
        self.assertGreater(measured['peak'],0)

    def test_stale_device_ids_rejected(self):
        with self.assertRaisesRegex(ValueError,'旧形式'):
            get_device('mic','not-an-id')
        with self.assertRaisesRegex(ValueError,'旧形式'):
            get_device('loopback','not-an-id')

    def test_missing_loopback_dep_still_exposes_microphones(self):
        with patch.dict(sys.modules, {'pyaudiowpatch': None}), patch('live_capture.emit') as emit:
            found=list_devices()
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['source'],'mic')
        self.assertTrue(emit.called)

    def test_mix_capture_uses_both_alternative_backends(self):
        from pathlib import Path
        from live_capture import capture_mix
        import threading
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); stop=root/'stop.flag'
            timer=threading.Timer(.90, lambda: stop.touch())
            def recording_event(event, **_):
                if event == 'started': timer.start()
            original_mic_read=FakeSDStream.read
            original_loop_read=FakePaStream.read
            delivered=set()
            def jittered_read(name, original, stream, *args, **kwargs):
                # Real endpoint callbacks need not fill the first 8 ms. Retain
                # that startup gap and verify both sources later in the WAV.
                if name not in delivered:
                    delivered.add(name)
                    time.sleep(.03)
                return original(stream,*args,**kwargs)
            try:
                with patch.dict(sys.modules,{'soundcard':None}), \
                     patch('live_capture.emit',side_effect=recording_event), \
                     patch.object(FakeSDStream,'read',autospec=True,
                         side_effect=lambda *a,**kw:jittered_read('mic',original_mic_read,*a,**kw)) as mic_read, \
                     patch.object(FakePaStream,'read',autospec=True,
                         side_effect=lambda *a,**kw:jittered_read('loopback',original_loop_read,*a,**kw)) as loop_read:
                    result=capture_mix(output=root/'mixed.wav',chunks=None,stop_file=stop,
                                       mic_device_id='pa:0',loopback_device_id='wp:7')
                    self.assertGreater(mic_read.call_count,0)
                    self.assertGreater(loop_read.call_count,0)
            finally:
                timer.cancel()
                if timer.ident is not None: timer.join(timeout=1)
            self.assertEqual(result,0)
            with wave.open(str(root/'mixed.wav'),'rb') as w:
                self.assertEqual(w.getnchannels(),2)
                self.assertGreater(w.getnframes(),12000)
                samples=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2')
                # Each fake input contributes about 3276 PCM units: this
                # threshold proves their sum, rather than either input alone.
                self.assertGreater(int(samples.max()),6000)

    def test_mix_open_failure_never_leaves_empty_wav(self):
        from pathlib import Path
        from live_capture import capture_mix
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with patch.dict(sys.modules,{'pyaudiowpatch':None}), patch('live_capture.emit'):
                with self.assertRaisesRegex(RuntimeError,'初期化できませんでした'):
                    capture_mix(output=root/'should_not_exist.wav',chunks=None,stop_file=root/'stop.flag')
            self.assertFalse((root/'should_not_exist.wav').exists())

    def test_import_soundcard_failure_via_cli_does_not_abort_listing(self):
        from live_capture import main
        output=io.StringIO()
        with patch.dict(sys.modules, {'soundcard': None}), contextlib.redirect_stdout(output):
            code=main(['--list-devices'])
        self.assertEqual(code,0)
        obj=json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(obj['event'],'devices')

if __name__ == '__main__': unittest.main()
