"""Bounded STOP, independent channels, post-record ASR and hotkey delivery."""
import ctypes
import io
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import MagicMock, patch

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import live_capture
from live_config import LiveOptions,is_silent_pcm
from live_mix import AlignedAudioMixer
from media_tools import encode_live_recording
from gamebar_recording import _send_key_sequence,send_hotkey
from app_hotkeys import normalize_hotkey,validate_shortcuts,tk_shortcut,WindowsHotkeys
from capture_preferences import saved_options,serialize_options,match_window
from core import Config,save_config,load_config
import unified_gui as gui


class AudioChannelsTests(unittest.TestCase):
    def test_pc_and_mic_stay_on_separate_channels_with_gains_and_missing_input(self):
        mixer=AlignedAudioMixer(1000,mic_gain=.5,loopback_gain=2,channel_mode='split_lr')
        mixer.push('mic',0,np.full((10,1),.4))
        mixer.push('loopback',2,np.tile([.1,.3],(6,1)))
        audio,missing=mixer.pull(10)
        np.testing.assert_allclose(audio[:,1],.2)
        np.testing.assert_allclose(audio[2:8,0],.4)
        np.testing.assert_array_equal(audio[[0,1,8,9],0],0)
        self.assertEqual(missing,{'mic':0,'loopback':4})

    def test_split_limiter_does_not_change_the_other_source(self):
        mixer=AlignedAudioMixer(1000,2,1,channel_mode='split_lr')
        mixer.push('mic',0,np.ones((20,1)))
        mixer.push('loopback',0,np.full((20,2),.15))
        audio,_=mixer.pull(20)
        np.testing.assert_allclose(audio[:,0],.15)
        np.testing.assert_allclose(audio[:,1],.98)
        with self.assertRaises(ValueError):LiveOptions(channel_mode='split_lr').validate()
        LiveOptions(source='mix',channel_mode='split_lr').validate()

    def test_lossless_encoding_keeps_lr_order_and_isolation(self):
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'capture.wav'
            data=np.zeros((4800,2),dtype='<i2');data[:2400,0]=6000;data[2400:,1]=-9000
            with wave.open(str(source),'wb') as stream:
                stream.setparams((2,2,48000,0,'NONE','not compressed'));stream.writeframes(data.tobytes())
            flac=root/'separate.flac'
            encode_live_recording(source,flac,ffmpeg=ffmpeg,fmt='flac')
            result=subprocess.run([ffmpeg,'-v','error','-i',str(flac),'-f','s16le','-acodec','pcm_s16le','-'],capture_output=True,check=True)
            np.testing.assert_array_equal(np.frombuffer(result.stdout,dtype='<i2').reshape(-1,2),data)


class SilentStopTests(unittest.TestCase):
    def run_blocked(self,mix):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);stop=root/'stop';release=threading.Event()
            class Device:
                name='Silent endpoint'
                def recorder(self,**_):return self
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def record(self,**_):
                    release.wait(5)
                    return np.zeros((0,2))
            def emit(kind,**_):
                if kind=='started':threading.Timer(.12,stop.touch).start()
            began=time.monotonic()
            try:
                with patch('live_capture.emit',side_effect=emit):
                    if mix:
                        code=live_capture.capture_mix(output=root/'saved.wav',chunks=None,stop_file=stop,
                            sample_rate=8000,devices={'mic':Device(),'loopback':Device()},channel_mode='split_lr')
                    else:
                        code=live_capture.capture(output=root/'saved.wav',chunks=None,stop_file=stop,
                            sample_rate=8000,source='loopback',device=Device())
                self.assertEqual(code,0)
                self.assertLess(time.monotonic()-began,2.6)
                with wave.open(str(root/'saved.wav'),'rb') as stream:
                    self.assertGreater(stream.getnframes(),300)
                    self.assertLess(stream.getnframes(),4000)
                self.assertTrue(is_silent_pcm(root/'saved.wav'))
            finally:release.set()

    def test_mix_stops_once_even_if_both_reads_block(self):self.run_blocked(True)
    def test_single_stops_once_even_if_silent_read_blocks(self):self.run_blocked(False)


class PostRecordingTests(unittest.TestCase):
    def run_worker(self,post,silent=False,realtime=False,teardown_timeout=False):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            app=gui.UnifiedApp.__new__(gui.UnifiedApp)
            app.events=queue.Queue();app.process_lock=threading.Lock()
            app.live_cancel=threading.Event();app.live_stopping=realtime
            app.live_active=True;app.capture_process=None
            closed=[];commands=[]
            class Process:
                def __init__(self,cmd,**_):
                    self.waits=0
                    commands.append(cmd)
                    output=Path(cmd[cmd.index('--output')+1])
                    with wave.open(str(output),'wb') as stream:
                        stream.setparams((2,2,48000,0,'NONE','not compressed'))
                        stream.writeframes((np.zeros((480,2),dtype='<i2') if silent else np.full((480,2),1000,dtype='<i2')).tobytes())
                    events=[]
                    if '--chunks' in cmd:
                        chunk=Path(cmd[cmd.index('--chunks')+1])/'segment_000000.wav'
                        events.append({'event':'chunk','index':0,'offset':0,'path':str(chunk)})
                    events.append({'event':'finished','seconds':.01})
                    self.stdout=io.StringIO(''.join(json.dumps(event)+'\n' for event in events))
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def wait(self,**_):
                    self.waits+=1
                    if teardown_timeout and self.waits==1:
                        raise subprocess.TimeoutExpired('capture',3)
                    closed.append(True);return 0
                def kill(self):pass
            def device(*_):
                if not realtime:self.assertTrue(closed,'ASR started before the recording closed')
                return 'cpu'
            app._resolve_device=MagicMock(side_effect=device)
            app._recognize_live_chunk=MagicMock()
            app._run_one=MagicMock(return_value=('完了','result.txt'))
            options=LiveOptions(mode='both' if realtime else 'record',post_transcribe=post,
                                folder=folder,source='mix',channel_mode='split_lr')
            with patch('unified_gui.subprocess.Popen',Process):
                app._live_worker(options,Path(sys.executable),Path(sys.executable),Config(output_dir=str(folder)), '')
            self.assertEqual('--chunks' in commands[0],realtime)
            self.assertEqual(commands[0][commands[0].index('--channel-mode')+1],'split_lr')
            app._recognize_live_chunk.assert_not_called()
            self.assertEqual(app._run_one.call_count,1 if (post or realtime) and not silent else 0)
            self.assertEqual(sum(item[0]=='new-file' for item in app.events.queue),app._run_one.call_count)
            self.assertTrue(list(folder.glob('*.wav')))
            self.assertFalse(list(folder.glob('*.live.txt')))
            self.assertFalse(app.live_active)
            self.assertIn('done',[item[0] for item in list(app.events.queue)])

    def test_record_only_post_transcription_runs_after_wav_close(self):self.run_worker(True)
    def test_record_only_default_never_runs_asr(self):self.run_worker(False)
    def test_exact_silence_skips_final_model_loading_and_keeps_audio(self):self.run_worker(True,True)
    def test_stop_skips_pending_partial_recognition_and_runs_final_once(self):self.run_worker(False,realtime=True)
    def test_device_teardown_timeout_after_wav_commit_does_not_block_final_asr(self):self.run_worker(True,teardown_timeout=True)


class ShortcutTests(unittest.TestCase):
    def test_modifier_hold_then_action_and_reverse_release(self):
        sequence=[];now=[0.0]
        def sleep(seconds):sequence.append(('wait',seconds));now[0]+=seconds
        triggered=_send_key_sequence((0x5b,0x12,ord('R')),
            lambda key,up:sequence.append((key,up,now[0])),sleep=sleep,clock=lambda:now[0])
        keys=[entry for entry in sequence if entry[0]!='wait']
        self.assertEqual([(key,up) for key,up,_ in keys],
            [(0x5b,False),(0x12,False),(ord('R'),False),(ord('R'),True),(0x12,True),(0x5b,True)])
        self.assertGreaterEqual(keys[2][2]-keys[1][2],.20)
        self.assertAlmostEqual(triggered,keys[2][2])

    def test_failed_key_release_still_releases_remaining_modifiers(self):
        events=[]
        def key_event(key,up):
            events.append((key,up))
            if key==ord('R') and not up:raise OSError('input blocked')
        with self.assertRaises(OSError):_send_key_sequence((0x5b,0x12,ord('R')),key_event,sleep=lambda _:None)
        self.assertEqual(events[-2:],[(0x12,True),(0x5b,True)])

    def test_actual_keys_and_conflicting_assignments(self):
        held=set()
        self.assertIsNone(tk_shortcut(SimpleNamespace(keysym='Control_L',state=0),held))
        self.assertEqual(tk_shortcut(SimpleNamespace(keysym='F8',state=5),held),'ctrl+shift+f8')
        self.assertEqual(normalize_hotkey(' Shift + Ctrl + F8 '),'ctrl+shift+f8')
        self.assertEqual(tk_shortcut(SimpleNamespace(keysym='F8',state=0),set()),'f8')
        self.assertIsNone(tk_shortcut(SimpleNamespace(keysym='a',state=0),set()))
        validate_shortcuts({'live':'f8','gamebar':'f9','stop':'f10'})
        with self.assertRaises(ValueError):validate_shortcuts({'live':'ctrl+f8','stop':'ctrl+f8'})
        with self.assertRaises(ValueError):validate_shortcuts({'gamebar':'win+alt+r'})

    def test_sendinput_uses_pointer_sized_struct_and_physical_scan_codes(self):
        api=SimpleNamespace(**{name:MagicMock() for name in ('IsWindow','IsIconic','SetForegroundWindow',
            'GetForegroundWindow','GetAsyncKeyState','MapVirtualKeyW','SendInput')})
        api.IsWindow.return_value=1;api.IsIconic.return_value=0
        api.GetForegroundWindow.return_value=42;api.GetAsyncKeyState.return_value=0
        api.MapVirtualKeyW.side_effect=lambda key,_:{0x5b:0xe05b,0x12:0x38,ord('R'):0x13}[key]
        events=[]
        def send(_count,pointer,size):
            info=pointer._obj
            self.assertEqual(size,40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)
            events.append((info.union.ki.wVk,info.union.ki.wScan,info.union.ki.dwFlags));return 1
        api.SendInput.side_effect=send
        with patch('gamebar_recording.os.name','nt'),patch('ctypes.WinDLL',return_value=api,create=True),patch('gamebar_recording.time.sleep'):
            send_hotkey('win+alt+r',hwnd=42)
        self.assertEqual(events[0],(0,0x5b,0x9))
        self.assertEqual(events[2],(0,0x13,0x8))
        self.assertEqual(events[-1],(0,0x5b,0xb))

    def test_windows_hotkeys_register_dispatch_suspend_and_unregister(self):
        api=SimpleNamespace(**{name:MagicMock() for name in ('RegisterHotKey','UnregisterHotKey','PeekMessageW')})
        api.RegisterHotKey.return_value=1
        emitted=[];received=threading.Event();sent=[]
        def peek(pointer,_window,_low,_high,remove):
            if remove and not sent:
                pointer._obj.message=0x0312;pointer._obj.wParam=1;sent.append(True);return 1
            return 0
        api.PeekMessageW.side_effect=peek
        def callback(action):emitted.append(action);received.set()
        with patch('app_hotkeys.sys.platform','win32'),patch('ctypes.WinDLL',return_value=api,create=True):
            manager=WindowsHotkeys({'live':'ctrl+shift+f8','stop':'ctrl+shift+f10'},callback,lambda _:None)
            try:
                self.assertTrue(received.wait(1))
                self.assertEqual(emitted,['live'])
                self.assertEqual(api.RegisterHotKey.call_args_list[0].args,(None,1,0x4006,0x77))
                manager.suspend(True)
                self.assertEqual(api.UnregisterHotKey.call_count,2)
                calls=api.RegisterHotKey.call_count
                manager.update({'live':'ctrl+alt+f8'})
                self.assertEqual(api.RegisterHotKey.call_count,calls)
                manager.suspend(False)
                self.assertEqual(api.RegisterHotKey.call_count,calls+1)
            finally:
                manager.close();manager.thread.join(timeout=1)
            self.assertFalse(manager.thread.is_alive())


class PreferenceTests(unittest.TestCase):
    def test_old_settings_and_new_preferences_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'config.json'
            target.write_text(json.dumps({'gamebar_shortcut':'ctrl+shift+f9'}))
            cfg=load_config(target)
            self.assertEqual(cfg.gamebar_shortcut,'ctrl+shift+f9')
            self.assertFalse(cfg.skip_live_settings)
            opts=LiveOptions(mode='record',post_transcribe=True,source='mix',channel_mode='split_lr',folder=Path(td))
            cfg.capture_preferences={'live':serialize_options(opts)};cfg.skip_live_settings=True
            cfg.shortcut_keys={'live':'ctrl+alt+f8','stop':'ctrl+alt+f10'}
            save_config(cfg,target);restored=load_config(target)
            self.assertEqual(saved_options(restored,'live'),opts)
            self.assertTrue(restored.skip_live_settings)

    def test_reused_window_handle_cannot_record_the_wrong_app(self):
        from gamebar_recording import GameBarOptions
        opts=GameBarOptions(target_hwnd=12,target_title='Meeting')
        self.assertIsNone(match_window(opts,[('Unrelated',12)]))
        self.assertEqual(match_window(opts,[('Meeting',99)]),99)
        self.assertIsNone(match_window(opts,[('Meeting',99),('Meeting',98)]))


if __name__=='__main__':unittest.main()
