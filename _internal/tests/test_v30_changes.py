"""Archive publication safety, native wheel input and whole-task cancellation."""
from pathlib import Path
import gc,io,json,queue,shutil,subprocess,sys,tempfile,threading,time,unittest,wave
from unittest.mock import MagicMock,patch
import tkinter as tk
from tkinter import ttk
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Config
from merge_archive import archive_merge_sources,source_stamps
from media_tools import merge_text_files,run_ffmpeg
from transcript_tools import merge_transcript_csv,convert_transcript
from process_control import ProcessRegistry,run_cancellable,owned_process
from unified_gui import UnifiedApp,ConcatDialog,modal
from live_config import LiveOptions
from gamebar_recording import GameBarOptions
from studio_ui import scrollable_body
import test_v25_studio_ui as fixtures


def bare():
 a=UnifiedApp.__new__(UnifiedApp);a.events=queue.Queue();a.stop_event=threading.Event()
 a.live_cancel=threading.Event();a.gamebar_stop=threading.Event();a.process_lock=threading.Lock()
 a.live_stopping=False;a.live_chunk_active=False;a.active_process=None;a.quit_when_idle=False
 a.config=Config(device='cpu');a._run_one=MagicMock(return_value=('完了','result'))
 a._resolve_device=MagicMock(return_value='cpu');a._recognize_live_chunk=MagicMock()
 a._transcribe_completed_screen=MagicMock(return_value=('完了','result'))
 return a

class ArchiveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)
  self.inputs=[]
  for name in ('one','two'):
   folder=self.folder/name;folder.mkdir();path=folder/'会議.txt'
   path.write_text('[00:00:01] SPEAKER_00: '+name+'\n');self.inputs.append(path)
  self.output=self.folder/'joined.txt'
 def test_worker_archives_in_each_original_parent_after_success(self):
  a=bare();a._text_merge_worker(self.inputs,self.output,True,True)
  self.assertIn('one',self.output.read_text());self.assertIn('two',self.output.read_text())
  self.assertTrue(all((p.parent/'_old'/p.name).is_file() and not p.exists() for p in self.inputs))
  self.assertIn('元ファイル2件',list(a.events.queue)[-1][1])
 def test_off_retains_originals(self):
  bare()._text_merge_worker(self.inputs,self.output,False)
  self.assertTrue(all(p.exists() for p in self.inputs));self.assertFalse(list(self.folder.rglob('_old')))
 def test_collision_and_duplicate_input_are_safe(self):
  merge_text_files(self.inputs,self.output);old=self.inputs[0].parent/'_old';old.mkdir()
  (old/'会議.txt').write_text('previous')
  result=archive_merge_sources([self.inputs[0],self.inputs[0]],self.output)
  self.assertEqual(len(result.moved),1);self.assertEqual(result.moved[0][1].name,'会議 (1).txt')
  self.assertEqual((old/'会議.txt').read_text(),'previous')
 def test_missing_or_empty_output_never_moves_inputs(self):
  with self.assertRaises(ValueError):archive_merge_sources(self.inputs,self.output)
  self.output.touch()
  with self.assertRaises(ValueError):archive_merge_sources(self.inputs,self.output)
  self.assertTrue(all(p.exists() for p in self.inputs))
 def test_output_as_source_is_protected(self):
  with self.assertRaises(ValueError):archive_merge_sources(self.inputs,self.inputs[0])
  self.assertTrue(all(p.exists() for p in self.inputs))
 def test_modified_source_stays_in_place(self):
  stamps=source_stamps(self.inputs);merge_text_files(self.inputs,self.output)
  self.inputs[0].write_text('changed')
  result=archive_merge_sources(self.inputs,self.output,stamps=stamps)
  self.assertEqual((len(result.moved),len(result.errors)),(1,1));self.assertEqual(self.inputs[0].read_text(),'changed')
 def test_move_failure_retains_sources_and_removes_reservations(self):
  merge_text_files(self.inputs,self.output)
  with patch('merge_archive.os.replace',side_effect=PermissionError('locked')):result=archive_merge_sources(self.inputs,self.output)
  self.assertEqual(len(result.errors),2);self.assertTrue(all(p.exists() for p in self.inputs))
  self.assertEqual(list(self.folder.rglob('_old/*')),[])
 def test_cancel_stops_remaining_moves(self):
  merge_text_files(self.inputs,self.output);stop=threading.Event()
  result=archive_merge_sources(self.inputs,self.output,cancel=stop,log=lambda _:stop.set())
  self.assertEqual(len(result.moved),1);self.assertTrue(self.inputs[1].exists());self.assertTrue(self.output.exists())
 def test_failed_merge_preserves_existing_output_and_sources(self):
  self.output.write_text('existing');a=bare();a._text_merge_worker(self.inputs,self.output,True,True)
  self.assertEqual(self.output.read_text(),'existing');self.assertTrue(all(p.exists() for p in self.inputs))
  self.assertIn('失敗',list(a.events.queue)[-1][1]);self.assertFalse(list(self.folder.rglob('_old')))
 def test_cancelled_workers_report_done_without_archiving(self):
  for kind in ('text','csv'):
   a=bare();a.stop_event.set()
   if kind=='text':a._text_merge_worker(self.inputs,self.output,True,True)
   else:a._csv_merge_worker(self.inputs,self.output.with_suffix('.csv'),True)
   self.assertEqual(list(a.events.queue)[-1][0],'done');self.assertIn('中止',list(a.events.queue)[-1][1])
  self.assertTrue(all(p.exists() for p in self.inputs))
 def test_csv_archiving_retains_recognized_text(self):
  target=self.output.with_suffix('.csv');bare()._csv_merge_worker(self.inputs,target,True)
  self.assertIn('one',target.read_text(encoding='utf-8-sig'));self.assertIn('two',target.read_text(encoding='utf-8-sig'))
  self.assertTrue(all((p.parent/'_old'/p.name).exists() for p in self.inputs))
 @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
 def test_audio_commits_and_archives_before_followup_transcription(self):
  inputs=[]
  for index in range(2):
   path=self.folder/f'{index}.wav'
   with wave.open(str(path),'wb') as audio:
    audio.setparams((1,2,16000,0,'NONE','not compressed'));audio.writeframes(b'\x10\x00'*1600)
   inputs.append(path)
  target=self.folder/'joined.wav';a=bare()
  def recognize(source,*args,**kwargs):
   self.assertEqual(source,target);self.assertTrue(all((p.parent/'_old'/p.name).exists() for p in inputs))
   with wave.open(str(source),'rb') as audio:self.assertGreaterEqual(audio.getnframes(),3200)
   return '完了','result'
  a._run_one.side_effect=recognize
  a._audio_merge_worker(inputs,target,'wav','192k',1,16000,True,Config(device='cpu',output_dir=str(self.folder)),Path(sys.executable),shutil.which('ffmpeg'),'',True)
  a._run_one.assert_called_once();self.assertIn('完了',list(a.events.queue)[-1][1])

class CaptureTests(unittest.TestCase):
 def test_live_all_stop_cancels_backlog_final_asr_and_encoding_keeps_wav(self):
  for live in (False,True):
   with self.subTest(live=live),tempfile.TemporaryDirectory() as td:
    folder=Path(td);a=bare();a.live_active=True
    class Capture:
     def __init__(self,cmd,**kw):
      output=Path(cmd[cmd.index('--output')+1])
      with wave.open(str(output),'wb') as audio:
       audio.setparams((1,2,48000,0,'NONE','not compressed'));audio.writeframes(b'\x10\x00'*480)
      self.stdout=self.lines()
     def lines(self):
      yield json.dumps({'event':'started'})+'\n'
      a.stop_event.set();a.live_cancel.set();a.live_stopping=True
      if live:yield json.dumps(dict(event='chunk',index=0,offset=0,path=str(folder/'chunk.wav')))+'\n'
      yield json.dumps(dict(event='finished',seconds=.01))+'\n'
     def __enter__(self):return self
     def __exit__(self,*_):pass
     def wait(self,**_):return 0
    with patch('unified_gui.subprocess.Popen',Capture),patch('unified_gui.encode_live_recording') as encode:
     a._live_worker(LiveOptions(mode='both' if live else 'record',post_transcribe=True,folder=folder,recording_format='mp3'),Path(sys.executable),Path(sys.executable),Config(output_dir=str(folder)),'')
    a._run_one.assert_not_called();a._recognize_live_chunk.assert_not_called();encode.assert_not_called()
    self.assertEqual(len(list(folder.glob('*.wav'))),1);self.assertFalse(a.live_active)
 def test_gamebar_all_stop_dispatches_stop_once_skips_asr(self):
  from test_v24_gamebar import FakeSoundProcess
  with tempfile.TemporaryDirectory() as td:
   folder=Path(td);a=bare();calls=[]
   def key(*args,**kwargs):
    calls.append(args)
    if len(calls)==1:a.gamebar_stop.set();a.stop_event.set();a.live_cancel.set()
   with patch('unified_gui.CODE_DIR',folder),patch('unified_gui.subprocess.Popen',FakeSoundProcess),patch('gamebar_recording.send_hotkey',side_effect=key),patch('gamebar_recording.snapshot_mp4',return_value={}),patch('gamebar_recording.find_completed_capture',return_value=None):
    a._gamebar_worker(GameBarOptions(destination=folder,captures=folder,target_hwnd=123,auto_transcribe=True,audio_format='mp3'),a.config,Path(sys.executable),Path(sys.executable),None,'')
   self.assertEqual(len(calls),2);a._transcribe_completed_screen.assert_not_called();self.assertEqual(len(list(folder.glob('*.wav'))),1)
 def test_gamebar_exception_after_start_still_stops_in_cleanup(self):
  from test_v24_gamebar import FakeSoundProcess
  with tempfile.TemporaryDirectory() as td:
   folder=Path(td);a=bare();a.gamebar_stop=MagicMock();a.gamebar_stop.is_set.return_value=False
   a.gamebar_stop.wait.side_effect=RuntimeError('interrupted wait')
   with patch('unified_gui.CODE_DIR',folder),patch('unified_gui.subprocess.Popen',FakeSoundProcess),patch('gamebar_recording.send_hotkey') as key,patch('gamebar_recording.snapshot_mp4',return_value={}):
    a._gamebar_worker(GameBarOptions(destination=folder,captures=folder,target_hwnd=123),a.config,Path(sys.executable),Path(sys.executable),None,'')
   self.assertEqual(key.call_count,2);a._transcribe_completed_screen.assert_not_called()

class GuiTests(fixtures.StudioGUIFixture):
 def controls(self):
  win=modal(self.root,'ホイール操作',660);win.geometry('660x400');body,_=scrollable_body(win)
  first=ttk.Combobox(body,values=('first','second','third'));first.set('second');first.pack()
  second=ttk.Combobox(body,values=('first','second','third'),state='readonly');second.set('second');second.pack()
  spin=ttk.Spinbox(body,from_=0,to=10);spin.set('5');spin.pack()
  for i in range(35):ttk.Label(body,text=f'項目{i}').pack()
  self.root.update();canvas=next(w for w in fixtures.descendants(win) if isinstance(w,tk.Canvas))
  return win,canvas,first,second,spin
 def click(self,w):
  # event_generate does not activate a native Windows toplevel as a physical
  # click would. Activate only the window; native widget bindings set focus.
  w.winfo_toplevel().focus_force();self.root.update()
  for event in ('<ButtonPress-1>','<ButtonRelease-1>'):w.event_generate(event,x=15,y=max(5,w.winfo_height()//2))
  self.root.update()
 def wheel(self,w,delta=-120):w.event_generate('<MouseWheel>',delta=delta);self.root.update()
 def pump(self,condition):
  end=time.monotonic()+4
  while not condition() and time.monotonic()<end:self.root.update();time.sleep(.01)
  self.assertTrue(condition())
 def test_hover_and_keyboard_focus_are_not_enough_to_change_values(self):
  win,canvas,first,second,spin=self.controls()
  for w in (first,second,spin):
   before=w.get();w.focus_force();self.root.update();self.wheel(w);w.event_generate('<Button-5>');self.root.update();self.assertEqual(w.get(),before)
  self.assertGreater(canvas.yview()[0],0)
 def test_click_arms_one_control_without_scrolling_form(self):
  win,canvas,first,second,spin=self.controls();self.click(first);position=canvas.yview();self.wheel(first)
  self.assertEqual(first.get(),'third');self.assertEqual(canvas.yview(),position)
  self.wheel(second);self.assertEqual(second.get(),'second')
 def test_focus_loss_disarms_and_spinbox_can_be_clicked_to_enable(self):
  win,canvas,first,second,spin=self.controls();self.click(first)
  second.focus_force();self.root.update();first.focus_force();self.root.update();self.wheel(first)
  self.assertEqual(first.get(),'second');self.click(spin);self.wheel(spin,120);self.assertEqual(spin.get(),'6')
 def test_native_popup_selection_and_safe_remapping(self):
  win,canvas,first,combo,spin=self.controls();self.click(combo);popup=str(combo)+'.popdown.f.l'
  combo.tk.call('event','generate',popup,'<KeyPress-Down>');combo.tk.call('event','generate',popup,'<KeyPress-Return>');self.root.update()
  self.assertEqual(combo.get(),'third');self.wheel(combo,120);self.assertEqual(combo.get(),'second')
  combo.pack_forget();self.root.update();combo.pack();first.focus_force();self.root.update()
  self.wheel(combo);self.assertEqual(combo.get(),'second')
 def test_keyboard_leaving_native_popup_disarms_until_another_click(self):
  win,canvas,first,combo,spin=self.controls();self.click(combo)
  popup=str(combo)+'.popdown.f.l'
  combo.tk.call('event','generate',popup,'<KeyPress-Tab>');self.root.update()
  self.assertIsNone(self.root._click_wheel_guard['armed'])
  combo.focus_force();self.root.update();before=combo.get();self.wheel(combo)
  self.assertEqual(combo.get(),before)
 def test_native_text_scroll_remains_independent(self):
  win,canvas,first,second,spin=self.controls();text=tk.Text(first.master,height=2);text.pack(before=first)
  text.insert('end','\n'.join(str(i) for i in range(100)));self.root.update();position=canvas.yview();self.wheel(text)
  self.assertGreater(text.yview()[0],0);self.assertEqual(canvas.yview(),position)
 def test_top_stop_flags_and_idle_state(self):
  a=self.app;b=a.ui.stop_all_button;self.assertEqual(str(b.cget('state')),'disabled');a._set_busy(True)
  a.pending_speaker_edits.append(self.folder/'result.txt');a.pending_capture_target='gamebar'
  b.invoke();b.invoke();self.assertTrue(a.stop_event.is_set());self.assertTrue(a.live_cancel.is_set());self.assertTrue(a.gamebar_stop.is_set())
  self.assertFalse(a.pending_speaker_edits);self.assertIsNone(a.pending_capture_target);self.assertTrue(a.busy)
  a.events.put(('done','中止'));a._poll();self.assertFalse(a.busy);self.assertIn('すべての処理を停止しました',a.status.get())
  a._begin_work();a._set_busy(True);self.assertEqual(str(b.cget('state')),'normal')
 def test_top_kills_silent_asr_and_never_starts_next_file(self):
  a=self.app;paths=self.add_samples(2);calls=[];a.config.device='cpu';a.config.txt_metadata_header=False
  def command(*args):calls.append(args[0]);return [sys.executable,'-c','import time;time.sleep(30)']
  with patch.object(a,'_require_environment',return_value=Path(sys.executable)),patch('unified_gui.build_command',side_effect=command):
   a.start_batch();self.addCleanup(lambda:[a._kill(p) for p,_ in a.processes.active()]);self.pump(lambda:bool(a.processes.active()))
   a.ui.stop_all_button.invoke();self.pump(lambda:not a.busy)
  self.assertEqual(calls,[paths[0]]);self.assertFalse(a.processes.active());self.assertEqual(a.tree.item(str(paths[0]),'values')[2],'中止')
 def test_merge_dialogs_forward_archive_toggle(self):
  inputs=[]
  for i in range(2):p=self.folder/f'{i}.txt';p.write_text('sample');inputs.append(p)
  for kind,name in (('text','start_text_merge'),('csv','start_csv_merge'),('audio','start_audio_merge')):
   with patch.object(ConcatDialog,'_inspect_file_async'):
    d=ConcatDialog(self.app,kind);d.paths=inputs;d._refresh_output();self.assertFalse(d.move_originals.get());d.move_originals.set(True)
    with patch.object(self.app,name) as start:d._start()
    self.assertTrue(start.call_args.kwargs['move_originals']);d.win.destroy();self.root.update()

class ProcessTests(unittest.TestCase):
 def test_silent_process_can_be_cancelled(self):
  stop=threading.Event();timer=threading.Timer(.2,stop.set);timer.start();self.addCleanup(timer.cancel);started=time.monotonic()
  with self.assertRaises(InterruptedError):run_ffmpeg([sys.executable,'-c','import time;time.sleep(30)'],cancel=stop)
  self.assertLess(time.monotonic()-started,3)
 def test_timeout_reaps_child(self):
  with self.assertRaises(subprocess.TimeoutExpired):run_cancellable([sys.executable,'-c','import time;time.sleep(30)'],cancel=threading.Event(),timeout=.2,capture_output=True)
 def test_only_registered_children_are_killed(self):
  registry=ProcessRegistry(threading.Event())
  with subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']) as other:
   try:
    with registry.process([sys.executable,'-c','import time;time.sleep(30)']) as owned:
     registry.stop_all();owned.wait(timeout=3);self.assertIsNone(other.poll())
   finally:other.kill();other.wait(timeout=3)
  self.assertFalse(registry.active())
 def test_late_spawn_is_caught_after_stop(self):
  registry=ProcessRegistry(threading.Event());registry.stop_all()
  with registry.process([sys.executable,'-c','import time;time.sleep(30)']) as process:process.wait(timeout=3)
 def test_cancelled_conversion_does_not_overwrite(self):
  with tempfile.TemporaryDirectory() as td:
   folder=Path(td);source=folder/'in.txt';source.write_text('[00:00:01] SPEAKER_00: hello\n');target=folder/'out.csv';target.write_text('existing')
   stop=threading.Event();stop.set()
   with self.assertRaises(InterruptedError):convert_transcript(source,target,overwrite=True,cancel=stop)
   self.assertEqual(target.read_text(),'existing')

if __name__=='__main__':unittest.main()
