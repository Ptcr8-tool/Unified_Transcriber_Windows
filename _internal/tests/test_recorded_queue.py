"""Recording completion is represented in the ordinary queue exactly once."""
import io
import json
import queue
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config
from external_recording import ExternalOptions
from live_config import LiveOptions
from unified_gui import UnifiedApp


class Tree:
    def __init__(self): self.rows={}; self.order=[]; self.selected=()
    def exists(self, key): return key in self.rows
    def insert(self, _parent, position, iid, values):
        self.rows[iid]=tuple(values)
        self.order.insert(len(self.order) if position=='end' else position, iid)
    def item(self, key, option=None, **kw):
        if 'values' in kw:self.rows[key]=tuple(kw['values'])
        return self.rows[key] if option=='values' else {'values':self.rows[key]}
    def index(self,key): return self.order.index(key)
    def delete(self,key): self.order.remove(key);self.rows.pop(key)
    def selection(self): return self.selected
    def selection_set(self,key): self.selected=(key,)


def bare():
    app=UnifiedApp.__new__(UnifiedApp)
    app.config=Config()
    app.tree=Tree();app.status=MagicMock();app.job_sources={};app.job_outputs={}
    app.job_options={};app.job_policies={};app.queue_policy_lock=threading.Lock()
    app.active_job_key=None;app.ui=None;app.log=MagicMock()
    app.events=queue.Queue();app.stop_event=threading.Event();app.quit_when_idle=False
    return app


class CapturedQueueTests(unittest.TestCase):
    def test_queue_only_is_registered_once_and_not_transcribed(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'recording.wav';src.write_bytes(b'audio')
            app=bare();app._run_one=MagicMock()
            app._recorded_queue_item(src,'録音済み')
            app._recorded_queue_item(src,'録音済み')
            self.assertEqual(list(app.job_sources),[str(src.resolve())])
            self.assertEqual(app.tree.item(str(src.resolve()),'values')[2],'録音済み')
            app._run_one.assert_not_called()

    def test_conversion_retargets_row_and_keeps_result_and_policy(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'audio.wav';dst=Path(td)/'audio.mp3';out=Path(td)/'audio.txt'
            src.write_bytes(b'original');app=bare();app._recorded_queue_item(src,'処理中')
            old=str(src.resolve());app.job_outputs[old]=[out];app.job_policies[old]='rename'
            dst.write_bytes(b'encoded');src.unlink()
            app._move_recorded_queue_item(src,dst)
            new=str(dst.resolve());self.assertEqual(list(app.job_sources),[new])
            self.assertEqual(app.job_outputs[new],[out]);self.assertEqual(app.job_policies[new],'rename')
            self.assertEqual(app.tree.item(new,'values')[0],'audio.mp3')

    def test_delayed_queue_event_survives_original_wav_already_removed(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'audio.wav';dst=Path(td)/'audio.flac';dst.write_bytes(b'final')
            app=bare();app._recorded_queue_item(src,'録音済み')
            app._move_recorded_queue_item(src,dst)
            self.assertEqual(list(app.job_sources),[str(dst.resolve())])
            self.assertTrue(app.tree.exists(str(dst.resolve())))

    def test_conversion_collision_merges_results_without_stale_source(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'audio.wav';dst=Path(td)/'audio.mp3'
            src.write_bytes(b'original');dst.write_bytes(b'converted')
            app=bare();app._recorded_queue_item(src,'処理中');app._recorded_queue_item(dst,'録音済み')
            old,new=str(src.resolve()),str(dst.resolve())
            old_result=Path(td)/'old.txt';new_result=Path(td)/'new.txt'
            app.job_outputs[old]=[old_result];app.job_outputs[new]=[new_result]
            app.job_policies[new]='overwrite';src.unlink()
            app._move_recorded_queue_item(src,dst)
            self.assertEqual(list(app.job_sources),[new]);self.assertFalse(app.tree.exists(old))
            self.assertEqual(app.job_outputs[new],[new_result,old_result])
            self.assertEqual(app.job_policies[new],'overwrite')

    def test_import_auto_setting_controls_transcription_after_enqueue(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'capture.mp4';src.write_bytes(b'video')
            for auto in (False, True):
                with self.subTest(auto=auto):
                    app=bare();cfg=Config(profile='faster-res')
                    def recognize(*_args):
                        self.assertTrue(any(e[0]=='capture-queue' for e in app.events.queue))
                        return '完了','saved'
                    app._transcribe_completed_screen=MagicMock(side_effect=recognize)
                    with patch('unified_gui.finalize_external',return_value=src),patch('unified_gui.has_audio_stream',return_value=True):
                        app._standard_import_worker(ExternalOptions(src),'ffmpeg',auto,cfg,Path(sys.executable),'')
                    events=list(app.events.queue)
                    self.assertEqual(sum(e[0]=='capture-queue' for e in events),1)
                    self.assertEqual(app._transcribe_completed_screen.call_count,int(auto))
                    self.assertEqual(next(e[2] for e in events if e[0]=='capture-queue'),'処理中' if auto else '録音済み')

    def test_live_record_only_enqueues_without_final_asr(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);app=bare();app.live_cancel=threading.Event();app.live_stopping=False
            app.live_active=True;app.capture_process=None;app.process_lock=threading.Lock()
            app._run_one=MagicMock();app._recognize_live_chunk=MagicMock()
            class Process:
                def __init__(self,cmd,**_kwargs):
                    output=Path(cmd[cmd.index('--output')+1])
                    with wave.open(str(output),'wb') as stream:
                        stream.setparams((2,2,48000,0,'NONE','not compressed'))
                        stream.writeframes(b'\x01\x00'*960)
                    self.stdout=io.StringIO(json.dumps({'event':'finished','seconds':.01})+'\n')
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def wait(self,**_):return 0
            with patch('unified_gui.subprocess.Popen',Process):
                app._live_worker(LiveOptions(mode='record',post_transcribe=False,folder=root),Path(sys.executable),None,Config(output_dir=str(root)),'')
            events=list(app.events.queue)
            queued=[e for e in events if e[0]=='capture-queue']
            self.assertEqual(len(queued),1);self.assertEqual(queued[0][2],'録音済み')
            self.assertTrue(queued[0][1].is_file());app._run_one.assert_not_called()

    def test_gamebar_registers_audio_once_and_honors_auto_switch(self):
        from test_v24_gamebar import WorkerTests
        for auto in (False, True):
            with self.subTest(auto=auto):
                case=WorkerTests('test_stop_shortcut_twice_and_asr_wav_only')
                case.setUp()
                try:
                    case.options=replace(case.options,auto_transcribe=auto)
                    case.run_worker(video_found=True)
                    events=list(case.app.events.queue)
                    registered=[e for e in events if e[0]=='capture-queue']
                    self.assertEqual(len(registered),1)
                    self.assertEqual(registered[0][1].suffix,'.wav')
                    self.assertEqual(case.app._transcribe_completed_screen.call_count,int(auto))
                    if auto:self.assertIn(('job',str(registered[0][1]),'完了'),events)
                finally:case.doCleanups()

    def test_live_capture_process_error_marks_saved_recording_as_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);app=bare();app.live_cancel=threading.Event();app.live_stopping=False
            app.live_active=True;app.capture_process=None;app.process_lock=threading.Lock()
            app._run_one=MagicMock()
            class Process:
                def __init__(self,cmd,**_kwargs):
                    output=Path(cmd[cmd.index('--output')+1])
                    with wave.open(str(output),'wb') as stream:
                        stream.setparams((2,2,48000,0,'NONE','not compressed'))
                        stream.writeframes(b'\x01\x00'*960)
                    self.stdout=io.StringIO(json.dumps({'event':'finished','seconds':.01})+'\n')
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def wait(self,**_):return 3
            with patch('unified_gui.subprocess.Popen',Process):
                app._live_worker(LiveOptions(mode='record',folder=root),Path(sys.executable),None,Config(output_dir=str(root)),'')
            events=list(app.events.queue);source=next(e[1] for e in events if e[0]=='capture-queue')
            self.assertTrue(source.exists());self.assertIn(('job',str(source),'失敗'),events)
            app._run_one.assert_not_called()

    def test_live_final_transcription_enqueues_once_then_retargets_encoded_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);app=bare();app.live_cancel=threading.Event();app.live_stopping=False
            app.live_active=True;app.capture_process=None;app.process_lock=threading.Lock()
            app._resolve_device=MagicMock(return_value='cpu')
            result=root/'result.txt';result.write_text('recognized',encoding='utf-8')
            def transcribe(source,*_args,**_kwargs):
                self.assertTrue(any(e[0]=='capture-queue' and e[1]==source for e in app.events.queue))
                app.events.put(('transcript-saved',str(source),[str(result)],'完了'))
                return '完了',str(result)
            app._run_one=MagicMock(side_effect=transcribe)
            class Process:
                def __init__(self,cmd,**_kwargs):
                    output=Path(cmd[cmd.index('--output')+1])
                    with wave.open(str(output),'wb') as stream:
                        stream.setparams((2,2,48000,0,'NONE','not compressed'))
                        stream.writeframes(b'\x01\x00'*960)
                    self.stdout=io.StringIO(json.dumps({'event':'finished','seconds':.01})+'\n')
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def wait(self,**_):return 0
            def encode(_src,dst,**_kwargs):
                dst.write_bytes(b'encoded audio');return dst
            with patch('unified_gui.subprocess.Popen',Process),patch('unified_gui.media_binary',return_value='ffmpeg'),patch('unified_gui.encode_live_recording',side_effect=encode):
                app._live_worker(LiveOptions(mode='record',post_transcribe=True,folder=root,recording_format='mp3'),Path(sys.executable),Path(sys.executable),Config(output_dir=str(root)),'')
            events=list(app.events.queue)
            self.assertEqual(sum(e[0]=='capture-queue' for e in events),1)
            app._run_one.assert_called_once()
            queued=next(e[1] for e in events if e[0]=='capture-queue')
            moved=next(e for e in events if e[0]=='capture-moved')
            self.assertEqual(moved[1],queued);self.assertFalse(queued.exists());self.assertTrue(moved[2].is_file())
            for event in events:
                if event[0]=='capture-queue':app._recorded_queue_item(event[1],event[2])
                elif event[0]=='transcript-saved':app.job_outputs[event[1]]=[Path(p) for p in event[2]]
                elif event[0]=='capture-moved':app._move_recorded_queue_item(event[1],event[2])
            self.assertEqual(list(app.job_sources),[str(moved[2].resolve())])
            self.assertEqual(app.job_outputs[str(moved[2].resolve())],[result])


if __name__=='__main__':unittest.main()
