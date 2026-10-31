"""Live two-source mixing, timeline, gains and end-to-end fake-device tests."""
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
from live_config import LiveOptions
from live_capture import capture_mix
from live_mix import AlignedAudioMixer, StreamClock


class AlignedAudioMixerTests(unittest.TestCase):
    def test_sum_mono_mic_stereo_pc_and_preserve_timeline(self):
        mixer=AlignedAudioMixer(1000)
        mixer.push('mic', 0, np.full((1000,1),0.25,dtype=np.float32))
        mixer.push('loopback',100,np.array([[0.1,-0.1]]*800,dtype=np.float32))
        a,missing=mixer.pull(500)
        b,more=mixer.pull(500)
        self.assertEqual(a.shape,(500,2))
        self.assertTrue(np.allclose(a[:100],.25))
        self.assertTrue(np.allclose(a[100:],np.array([.35,.15])))
        self.assertTrue(np.allclose(b[:400],np.array([.35,.15])))
        self.assertTrue(np.allclose(b[400:],.25))
        self.assertEqual(missing['mic'],0)
        self.assertEqual(missing['loopback'],100)
        self.assertEqual(more['loopback'],100)
        self.assertEqual(mixer.cursor,1000)

    def test_block_splitting_does_not_repeat_samples(self):
        mixer=AlignedAudioMixer(500)
        mixer.push('mic',0,np.arange(500,dtype=np.float32).reshape((-1,1))/1000)
        mixer.push('loopback',0,np.zeros((500,2),dtype=np.float32))
        a,_=mixer.pull(240)
        b,_=mixer.pull(260)
        self.assertEqual(len(np.vstack([a,b])),500)
        self.assertTrue(np.allclose(np.vstack([a,b])[:,0],np.arange(500)/1000))
        self.assertEqual(len(mixer.sources['mic'].pending),0)

    def test_late_packets_are_cropped_before_written_cursor(self):
        mixer=AlignedAudioMixer(100)
        mixer.pull(20)
        mixer.push('mic',10,np.full((20,1),.4,dtype=np.float32))
        mixer.push('loopback',20,np.zeros((10,2)))
        out,missing=mixer.pull(10)
        self.assertEqual(missing['mic'],0)
        self.assertTrue(np.allclose(out,.4))

    def test_peak_limiter_and_gains(self):
        mixer=AlignedAudioMixer(100,2,2)
        mixer.push('mic',0,np.ones(20,dtype=np.float32))
        mixer.push('loopback',0,np.ones((20,2),dtype=np.float32))
        audio,_=mixer.pull(20)
        self.assertAlmostEqual(float(np.max(audio)),.98,places=5)
        with self.assertRaises(ValueError):AlignedAudioMixer(48000,-0.01,1)
        with self.assertRaises(ValueError):LiveOptions(source='mix',mic_gain=5).validate()
        LiveOptions(source='mix',mic_gain=0,loopback_gain=2).validate()

    def test_source_clock_detects_device_pause(self):
        timebase=StreamClock(1000,origin=10)
        self.assertEqual(timebase.place(100,10.1),0)
        self.assertEqual(timebase.place(100,10.2),100)
        self.assertEqual(timebase.place(100,11.5),1400)
        self.assertEqual(timebase.discontinuities,1)


class FakeMic:
    def __init__(self,name,amplitude,channels=1,empty=False):
        self.name=name
        self.amplitude=amplitude
        self.channels=channels
        self.empty=empty

    def recorder(self,*,samplerate,blocksize):
        owner=self
        class Recorder:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def record(self,numframes):
                time.sleep(numframes/samplerate)
                if owner.empty:return np.zeros((0,owner.channels),dtype=np.float32)
                return np.full((numframes,owner.channels),owner.amplitude,dtype=np.float32)
        return Recorder()


class ConcurrentCaptureTests(unittest.TestCase):
    def test_slow_writer_bounds_queue_warns_and_stops(self):
        import queue
        import live_capture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stop = root / 'stop.flag'
            devices = {'mic': FakeMic('Mic', .2), 'loopback': FakeMic('PC', .1, 2)}
            emitted = []
            real_queue = queue.Queue
            real_feed = live_capture.RecordingWriter.feed
            def small_queue(*args, **kwargs):
                return real_queue(maxsize=2)
            def slow_feed(writer, data):
                time.sleep(.65)
                return real_feed(writer, data)
            timer = threading.Timer(.9, stop.touch)
            timer.start()
            try:
                with patch.object(queue, 'Queue', side_effect=small_queue), \
                     patch.object(live_capture.RecordingWriter, 'feed', slow_feed), \
                     patch('live_capture.emit', side_effect=lambda kind, **kw: emitted.append((kind, kw))):
                    result = capture_mix(output=root/'mixed.wav', chunks=None, stop_file=stop,
                                         sample_rate=8000, devices=devices)
            finally:
                timer.cancel()
            self.assertEqual(result, 0)
            self.assertTrue(any('保存が追いついていません' in e.get('message', '') for k, e in emitted if k == 'warning'))
            self.assertEqual(sum(k == 'finished' for k, _ in emitted), 1)

    def test_stereo_continuous_wav_and_chunks_from_two_live_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); stop=root/'stop.flag'
            devices={'mic':FakeMic('USB Mic',.20), 'loopback':FakeMic('PC Speakers',.10,2)}
            events=[]
            from live_capture import emit as real_emit
            with patch('live_capture.emit',side_effect=lambda kind,**kw:events.append((kind,kw))):
                timer=threading.Timer(.97,lambda:stop.touch())
                timer.start()
                try:
                    result=capture_mix(output=root/'mixed.wav',chunks=root/'chunks',stop_file=stop,
                                       chunk_seconds=.25,sample_rate=8000,devices=devices)
                finally:timer.cancel()
            self.assertEqual(result,0)
            with wave.open(str(root/'mixed.wav'),'rb') as w:
                self.assertEqual(w.getnchannels(),2)
                self.assertEqual(w.getframerate(),8000)
                frames=w.getnframes()
                values=np.frombuffer(w.readframes(frames),dtype='<i2').reshape((-1,2))/32767
            self.assertGreater(frames,5000)
            self.assertLess(frames,12000)
            self.assertGreater(float(np.mean(values[int(.25*8000):int(.50*8000)])),.20)
            segments=sorted((root/'chunks').glob('segment_*.wav'))
            self.assertGreaterEqual(len(segments),3)
            chunks=b''.join(wave.open(str(path),'rb').readframes(999999) for path in segments)
            self.assertEqual(chunks,(np.clip(values,-1,1)*32767).astype('<i2').tobytes())
            self.assertEqual(len([kind for kind,_ in events if kind=='finished']),1)
            self.assertEqual(len([kind for kind,_ in events if kind=='started']),1)

    def test_silent_loopback_does_not_stop_mic_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); stop=root/'stop.flag'
            timer=threading.Timer(.8,lambda:stop.touch())
            timer.start()
            try:
                with patch('live_capture.emit'):
                    result=capture_mix(output=root/'mixed.wav',chunks=None,stop_file=stop,
                      sample_rate=8000,devices={'mic':FakeMic('Mic',.22),
                                                'loopback':FakeMic('PC silent',0,2,True)})
            finally:timer.cancel()
            self.assertEqual(result,0)
            with wave.open(str(root/'mixed.wav'),'rb') as w:
                values=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2')
            self.assertGreater(np.count_nonzero(values),2000)


if __name__ == '__main__':unittest.main()
