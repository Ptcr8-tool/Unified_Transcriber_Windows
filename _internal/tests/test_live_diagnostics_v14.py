"""Microphone probe and failed WAV regression tests for v14."""
from __future__ import annotations
import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
import numpy as np
from live_capture import capture, probe_device, main

class DummyRecorder:
    def __init__(self, rate, stop=None):
        self.rate = rate
        self.stop = stop
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def record(self, numframes):
        if self.stop is not None:
            self.stop.touch()
        return np.ones((numframes, 1), dtype=np.float32) * .12

class SampleRateDevice:
    name='Realtek test microphone'
    def __init__(self, stop=None):
        self.stop = stop
        self.attempted=[]
    def recorder(self, *, samplerate, blocksize):
        self.attempted.append(samplerate)
        if samplerate == 48000:
            raise RuntimeError('Unsupported sample rate')
        return DummyRecorder(samplerate, self.stop)

class BadDevice:
    name = 'Busy microphone'
    def recorder(self, *, samplerate, blocksize):
        raise OSError('Device in use')

class DiagnosticsTests(unittest.TestCase):
    def test_probe_falls_back_and_reports_actual_rate(self):
        device=SampleRateDevice()
        with patch('live_capture.emit') as emit:
            result=probe_device(device=device,frames=9600)
        self.assertEqual(result['sample_rate'],44100)
        self.assertEqual(device.attempted,[48000,44100])
        self.assertEqual(result['device'],'Realtek test microphone')
        self.assertGreater(result['peak'],0)
        self.assertTrue(emit.called)

    def test_device_init_failure_does_not_create_empty_wave(self):
        with tempfile.TemporaryDirectory() as td:
            wav=Path(td)/'must_not_exist.wav'
            with self.assertRaisesRegex(RuntimeError,'48000Hz.*44100Hz'):
                capture(output=wav,chunks=None,stop_file=Path(td)/'stop',device=BadDevice())
            self.assertFalse(wav.exists())

    def test_capture_uses_fallback_rate_in_wav_header(self):
        with tempfile.TemporaryDirectory() as td:
            stop=Path(td)/'stop'; wav=Path(td)/'record.wav'
            device=SampleRateDevice(stop)
            with patch('live_capture.emit'):
                self.assertEqual(capture(output=wav,chunks=None,stop_file=stop,device=device),0)
            with wave.open(str(wav),'rb') as recorded:
                self.assertEqual(recorded.getframerate(),44100)
                self.assertGreater(recorded.getnframes(),0)

    def test_cli_probe_error_emits_detailed_event(self):
        output=io.StringIO()
        with patch('live_capture.get_device',return_value=BadDevice()), contextlib.redirect_stdout(output):
            status=main(['--probe-device','--source','mic'])
        self.assertEqual(status,1)
        msg=json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(msg['event'],'error')
        self.assertIn('Device in use', msg['detail'])

if __name__=='__main__':unittest.main()
