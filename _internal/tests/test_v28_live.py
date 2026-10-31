"""Live text routing and Game Bar's recording/chunk/final-ASR lifecycle."""
from dataclasses import replace
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unified_gui as gui
from core import Config
from engine_progress import EngineUpdate, PREFIX
from gamebar_recording import GameBarOptions
from capture_preferences import saved_options, serialize_options


def wav(path, *, silent=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as out:
        out.setparams((2,2,48000,0,'NONE','not compressed'))
        out.writeframes((b'\0\0' if silent else b'\xe8\x03')*960)


def worker_app():
    app=gui.UnifiedApp.__new__(gui.UnifiedApp)
    app.events=queue.Queue();app.process_lock=threading.Lock()
    app.live_cancel=threading.Event();app.stop_event=threading.Event()
    app.gamebar_stop=threading.Event()
    app.live_stopping=False;app.live_chunk_active=False;app.live_finalize_on_stop=False
    app.gamebar_active=True;app.quit_when_idle=False
    app.active_process=None
    return app


class LiveTextTests(unittest.TestCase):
    def test_streamed_text_is_routed_during_inference_without_duplicate_saved_text(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);app=worker_app();preview=folder/'capture.live.txt'
            cfg=Config(profile='faster-res');events_before_finish=[]
            class Process:
                def __init__(self,args,**_):
                    Path(args[-1]).write_text('[00:00:01] SPEAKER_00: 保存した発言\n',encoding='utf-8')
                    self.stdout=self.lines()
                def lines(self):
                    yield PREFIX+json.dumps(dict(stage='asr',completed=1,total=2,text='ライブの発言'))+'\n'
                    events_before_finish.append(any(item[0]=='engine-progress' and
                        item[1].snippet=='ライブの発言' for item in app.events.queue))
                    yield PREFIX+json.dumps(dict(stage='diarization',completed=1,total=1))+'\n'
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def wait(self):return 0
                def poll(self):return 0
            with patch('unified_gui.build_command',side_effect=lambda s,t,*_:['asr',str(t)]), \
                 patch('unified_gui.subprocess.Popen',Process):
                for index in range(2):
                    segment=folder/f'chunk{index}.wav';wav(segment)
                    app._recognize_live_chunk(segment,index,index*20,preview,cfg,Path(sys.executable),'','cpu',folder)
            text_events=[item[1].snippet for item in app.events.queue if item[0]=='engine-progress' and item[1].snippet]
            self.assertEqual(events_before_finish,[True,True])
            self.assertEqual(text_events,['ライブの発言','ライブの発言'])
            self.assertFalse(any(item[0]=='new-file' for item in app.events.queue))
            self.assertIn('[00:00:21]',preview.read_text(encoding='utf-8-sig'))
            self.assertEqual(preview.read_text(encoding='utf-8-sig').count('保存した発言'),2)
            self.assertFalse(any(PREFIX in item[1] for item in app.events.queue if item[0]=='log'))

    def test_engine_without_text_events_displays_committed_txt_with_session_offset(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);app=worker_app();segment=folder/'chunk.wav';wav(segment)
            class Process:
                def __init__(self,args,**_):
                    Path(args[-1]).write_text('[00:00:01] SPEAKER_00: テキストのみ\n',encoding='utf-8')
                    self.stdout=io.StringIO('完了\n')
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def wait(self):return 0
                def poll(self):return 0
            with patch('unified_gui.build_command',side_effect=lambda s,t,*_:['asr',str(t)]), \
                 patch('unified_gui.subprocess.Popen',Process):
                app._recognize_live_chunk(segment,1,20,folder/'live.txt',Config(),Path(sys.executable),'','cpu',folder)
            updates=[item[1] for item in app.events.queue if item[0]=='engine-progress']
            self.assertEqual(len(updates),1)
            self.assertIn('[00:00:21] SPEAKER_00: テキストのみ',updates[0].snippet)

    def test_silent_chunk_does_not_load_engine_or_add_text(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);segment=folder/'silent.wav';wav(segment,silent=True);app=worker_app()
            with patch('unified_gui.subprocess.Popen') as process:
                app._recognize_live_chunk(segment,0,0,folder/'live.txt',Config(),Path(sys.executable),'','cpu',folder)
            process.assert_not_called()
            self.assertFalse(any(item[0]=='engine-progress' for item in app.events.queue))

    def test_gamebar_live_options_restore_old_settings_and_validate_interval(self):
        cfg=Config(capture_preferences={'gamebar':{'target_title':'Old','target_hwnd':11}})
        self.assertFalse(saved_options(cfg,'gamebar').realtime_transcription)
        options=GameBarOptions(realtime_transcription=True,chunk_seconds=10)
        cfg.capture_preferences={'gamebar':serialize_options(options)}
        self.assertEqual(saved_options(cfg,'gamebar'),options)
        with self.assertRaises(ValueError):replace(options,chunk_seconds=1).validate()
        with self.assertRaises(ValueError):replace(options,realtime_transcription='yes').validate()


class GameBarLiveTests(unittest.TestCase):
    def simulate(self, *, live=True, final=True, silent=False, teardown_timeout=False,shortcut_error=False):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);app=worker_app()
            app.live_finalize_on_stop=live and final
            app.config=Config(profile='faster-res',output_dir=str(folder/'text'),
                              output_same_as_input=False,filename_media_creation=False)
            app._resolve_device=MagicMock(return_value='cpu')
            chunk_done=[threading.Event(),threading.Event()]
            commands=[];closed=[];shortcuts=[];capture=[];final_events=[]
            class ASR:
                def __init__(self,args,**_):
                    self.index=int(Path(args[1]).stem.rsplit('_',1)[1])
                    self.stdout=io.StringIO(PREFIX+json.dumps(dict(stage='asr',text=f'速報{self.index}'))+'\n')
                    Path(args[-1]).write_text(f'[00:00:01] SPEAKER_00: 速報{self.index}\n',encoding='utf-8')
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def poll(self):return 0
                def wait(self):chunk_done[self.index].set();return 0
            class Audio:
                def __init__(self,args,**_):
                    commands.append(args);self.args=args;self.returncode=None;self.waits=0
                    self.output=Path(args[args.index('--output')+1]);wav(self.output,silent=silent)
                    capture.append(self);self.stdout=self.lines()
                def lines(self):
                    yield json.dumps({'event':'started'})+'\n'
                    if '--chunks' in self.args and not silent:
                        directory=Path(self.args[self.args.index('--chunks')+1])
                        for index in range(2):
                            segment=directory/f'segment_{index:06d}.wav';wav(segment)
                            yield json.dumps(dict(event='chunk',index=index,offset=index*20,path=str(segment)))+'\n'
                            if not chunk_done[index].wait(3):raise AssertionError('Live ASR did not run during recording')
                    yield json.dumps(dict(event='finished',seconds=40))+'\n'
                def poll(self):return self.returncode
                def wait(self,timeout=None):
                    self.waits+=1
                    if teardown_timeout and self.waits==1:
                        raise subprocess.TimeoutExpired('audio',timeout)
                    closed.append(True);self.returncode=0;return 0
                def kill(self):self.returncode=-9
            def process(args,**kwargs):
                return ASR(args,**kwargs) if args[0]=='asr' else Audio(args,**kwargs)
            def final_run(source,*_,**__):
                self.assertTrue(closed,'Final ASR began before WAV was closed')
                self.assertFalse(app.gamebar_active)
                final_events.extend(list(app.events.queue))
                app.events.put(('engine-progress',EngineUpdate('音声認識',snippet='最終版の全文')))
                return '完了','final.txt'
            app._run_one=MagicMock(side_effect=final_run)
            def trigger(*args,**kwargs):
                shortcuts.append((args,kwargs))
                if shortcut_error:raise OSError('Game Bar unavailable')
            options=GameBarOptions(target_hwnd=123,source='mix',destination=folder,
                captures=folder,auto_transcribe=final,realtime_transcription=live,
                chunk_seconds=10,create_synced_video=False)
            with patch('unified_gui.CODE_DIR',folder), \
                 patch('unified_gui.build_command',side_effect=lambda s,t,*_:['asr',str(s),str(t)]), \
                 patch('unified_gui.subprocess.Popen',side_effect=process), \
                 patch('gamebar_recording.snapshot_mp4',return_value={}), \
                 patch('gamebar_recording.find_completed_capture',return_value=None), \
                 patch('gamebar_recording.send_hotkey',side_effect=trigger):
                app._gamebar_worker(options,app.config,Path(sys.executable),Path(sys.executable),None,'')
            events=list(app.events.queue)
            errors=[item for item in events if item[0]=='errorlog']
            self.assertEqual(len(errors),int(shortcut_error),errors)
            self.assertEqual(len(shortcuts),1 if shortcut_error else 2)
            self.assertTrue(capture[0].output.is_file())
            self.assertEqual('--chunks' in commands[0],live)
            self.assertFalse(list(folder.glob('.gamebar_live_*')))
            self.assertFalse(app.gamebar_active)
            self.assertFalse(app.live_chunk_active)
            self.assertFalse(app.live_stopping)
            self.assertEqual(app._run_one.call_count,int(final and not silent))
            texts=[item[1].snippet for item in events if item[0]=='engine-progress' and item[1].snippet]
            expected=(['速報0','速報1'] if live and not silent else [])+(['最終版の全文'] if final and not silent else [])
            self.assertEqual(texts,expected)
            previews=list((folder/'text').glob('*.live.txt')) if (folder/'text').exists() else []
            self.assertEqual(len(previews),int(live and not silent))
            if previews:
                self.assertIn('[00:00:21] SPEAKER_00: 速報1',previews[0].read_text(encoding='utf-8-sig'))
            if final and not silent:
                resets=[item for item in final_events if item[0]=='new-file']
                self.assertEqual(len(resets),1)
                self.assertEqual(final_events[-3][0],'new-file')
            else:self.assertFalse(any(item[0]=='new-file' for item in events))

    def test_live_and_final_share_recording_and_reset_only_before_final(self):self.simulate()
    def test_live_only_finishes_all_chunks_and_keeps_provisional_text(self):self.simulate(final=False)
    def test_final_only_keeps_previous_recording_behavior(self):self.simulate(live=False)
    def test_silence_completes_without_text(self):self.simulate(silent=True)
    def test_committed_wav_with_stuck_teardown_still_reaches_final(self):self.simulate(teardown_timeout=True)
    def test_gamebar_trigger_failure_still_records_and_transcribes_live_audio(self):self.simulate(shortcut_error=True)

    def test_stop_with_final_enabled_interrupts_active_partial_and_skips_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);app=worker_app();app.live_finalize_on_stop=True
            app.config=Config(profile='faster-res',output_dir=str(folder))
            app._resolve_device=MagicMock(return_value='cpu')
            app._transcribe_completed_screen=MagicMock(return_value=('完了','final.txt'))
            active=threading.Event();released=threading.Event();triggered=threading.Event();seen=[]
            def recognize(_path,index,*_):
                seen.append(index);app.live_chunk_active=True
                app.active_process=MagicMock();active.set()
                self.assertTrue(released.wait(3),'Stop did not interrupt the active partial')
                app.live_chunk_active=False;app.active_process=None
            app._recognize_live_chunk=recognize
            app._kill=MagicMock(side_effect=lambda _:released.set())
            class Audio:
                def __init__(self,args,**_):
                    output=Path(args[args.index('--output')+1]);wav(output)
                    self.returncode=None;self.stdout=self.lines()
                def lines(self):
                    yield json.dumps({'event':'started'})+'\n'
                    yield json.dumps(dict(event='chunk',index=0,offset=0,path=str(folder/'0.wav')))+'\n'
                    if not active.wait(3):raise AssertionError('Partial ASR did not start')
                    if not triggered.wait(3):raise AssertionError('Recording did not start')
                    yield json.dumps(dict(event='chunk',index=1,offset=20,path=str(folder/'1.wav')))+'\n'
                    app.gamebar_stop.set()
                    yield json.dumps({'event':'finished'})+'\n'
                def poll(self):return self.returncode
                def wait(self,timeout=None):self.returncode=0;return 0
            with patch('unified_gui.CODE_DIR',folder),patch('unified_gui.subprocess.Popen',Audio), \
                 patch('gamebar_recording.send_hotkey',side_effect=lambda *_,**__:triggered.set()), \
                 patch('gamebar_recording.snapshot_mp4',return_value={}), \
                 patch('gamebar_recording.find_completed_capture',return_value=None):
                app._gamebar_worker(GameBarOptions(destination=folder,captures=folder,
                    realtime_transcription=True,create_synced_video=False),app.config,
                    Path(sys.executable),Path(sys.executable),None,'')
            self.assertEqual(seen,[0]);app._kill.assert_called_once()
            app._transcribe_completed_screen.assert_called_once()
            self.assertTrue(list(folder.glob('*.wav')))


if __name__=='__main__':unittest.main()
