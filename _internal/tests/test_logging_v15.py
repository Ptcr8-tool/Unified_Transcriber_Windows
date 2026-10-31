"""Failure diagnosis and mic failover regression tests."""
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from diagnostic_logs import SessionLogs
from live_capture import open_recorder_with_fallback, _portaudio_device


class BrokenSoundCard:
    name = 'Realtek(R) Audio'
    def recorder(self, samplerate, blocksize):
        raise RuntimeError('Error 0x100000001')


class FakeStream:
    def __init__(self, **kw):
        self.settings=kw
        self.started=False
        self.closed=False
    def start(self):self.started=True
    def stop(self):self.started=False
    def close(self):self.closed=True
    def read(self, count):return np.ones((count,1),dtype=np.float32)*.2, False


class TestDiagnosis(unittest.TestCase):
    def test_session_error_logs_are_persistent_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, patch('diagnostic_logs.log_directory', return_value=Path(tmp)):
            logs=SessionLogs('hf_supersecret')
            logs.write('認識処理が動作')
            logs.error('RuntimeError: hf_supersecret')
            self.assertTrue(logs.session_path.exists())
            self.assertTrue(logs.error_path.exists())
            self.assertIn('認識処理が動作',logs.session_path.read_text(encoding='utf8'))
            self.assertNotIn('hf_supersecret',logs.error_path.read_text(encoding='utf8'))
            self.assertIn('[HF_TOKEN_REDACTED]',logs.error_path.read_text(encoding='utf8'))

    def test_portaudio_fallback_when_soundcard_fails(self):
        fake = types.SimpleNamespace(query_devices=lambda:[{'name':'Realtek(R) Audio',
                    'max_input_channels':2,'default_samplerate':48000}],
                InputStream=lambda **kwargs: FakeStream(**kwargs),
                default=types.SimpleNamespace(device=[0,0]))
        with patch.dict(sys.modules,{'sounddevice':fake}), patch('live_capture.sys.platform','win32'), patch('live_capture.emit') as emit:
            with ExitStack() as stack:
                rec,rate,frames=open_recorder_with_fallback(stack,BrokenSoundCard(),48000,source='mic')
                self.assertEqual(rate,48000)
                self.assertEqual(rec.record(123).shape,(123,1))
                self.assertTrue(any(x.args[0]=='warning' for x in emit.call_args_list))

    def test_portaudio_can_use_default_input_for_default_mic(self):
        fake = types.SimpleNamespace(query_devices=lambda:[{'name':'USB microphone',
                    'max_input_channels':1,'default_samplerate':44100}],
                default=types.SimpleNamespace(device=[0,0]))
        with patch.dict(sys.modules,{'sounddevice':fake}):
            i,d=_portaudio_device('Different SoundCard default name',allow_default=True)
        self.assertEqual(i,0)

    def test_explicit_mismatched_mic_never_silently_switches_input(self):
        fake = types.SimpleNamespace(query_devices=lambda:[{'name':'USB microphone',
                    'max_input_channels':1,'default_samplerate':44100}],
                default=types.SimpleNamespace(device=[0,0]))
        with patch.dict(sys.modules,{'sounddevice':fake}):
            with self.assertRaisesRegex(RuntimeError,'同じマイクを特定'):
                _portaudio_device('Wrong mic name',allow_default=False)

    def test_loopback_does_not_use_portaudio(self):
        fake = types.SimpleNamespace(query_devices=lambda:[],InputStream=lambda **kw:1)
        with patch.dict(sys.modules,{'sounddevice':fake}), patch('live_capture.sys.platform','win32'):
            with ExitStack() as stack:
                with self.assertRaisesRegex(RuntimeError,'0x100000001'):
                    open_recorder_with_fallback(stack,BrokenSoundCard(),48000,source='loopback')

if __name__=='__main__':unittest.main()
