"""Regressions for GUI worker delivery and bounded recursive merge input."""
import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import unified_gui as gui
from core import Config


class Window:
    def __init__(self):
        self.exists = True
        self.bindings = []
    def winfo_exists(self):
        return self.exists
    def bind(self, *args, **kwargs):
        self.bindings.append(args[1])
    def title(self, value):
        self.last_title = value
    def geometry(self, value):
        pass


class GuiDispatchTests(unittest.TestCase):
    def test_preview_failure_survives_exception_scope_and_is_delivered_on_ui_thread(self):
        app=types.SimpleNamespace(ui_callbacks=queue.Queue(128), events=queue.Queue(), closing=False)
        dialog=object.__new__(gui.GameBarRecordingDialog)
        dialog.app=app; dialog.win=Window()
        dialog.target=types.SimpleNamespace(get=lambda:'selected')
        dialog.windows={'selected':7}
        viewer=Window(); label=mock.Mock()
        preview=types.ModuleType('window_preview')
        preview.capture_preview=mock.Mock(side_effect=RuntimeError('sample failure'))
        with mock.patch.object(gui.tk,'Toplevel',return_value=viewer), \
             mock.patch.object(gui.ttk,'Label',return_value=label), \
             mock.patch.dict(sys.modules,{'window_preview':preview}):
            dialog._preview()
            widget,callback=app.ui_callbacks.get(timeout=3)
        diagnostics=mock.Mock()
        gui.dispatch_ui_callback(widget,callback,diagnostics)
        label.configure.assert_called_once_with(text='画像を取得できません: sample failure')
        diagnostics.error.assert_not_called()

    def test_preview_image_decode_error_is_visible(self):
        app=types.SimpleNamespace(ui_callbacks=queue.Queue(128),events=queue.Queue(),closing=False)
        dialog=object.__new__(gui.GameBarRecordingDialog)
        dialog.app=app; dialog.win=Window()
        dialog.target=types.SimpleNamespace(get=lambda:'selected'); dialog.windows={'selected':7}
        viewer=Window(); label=mock.Mock()
        preview=types.ModuleType('window_preview')
        preview.capture_preview=mock.Mock(return_value=(b'bad',10,10))
        with mock.patch.object(gui.tk,'Toplevel',return_value=viewer), \
             mock.patch.object(gui.ttk,'Label',return_value=label), \
             mock.patch.dict(sys.modules,{'window_preview':preview}):
            dialog._preview(); widget,callback=app.ui_callbacks.get(timeout=3)
        with mock.patch.object(gui.tk,'PhotoImage',side_effect=gui.tk.TclError('bad image')):
            gui.dispatch_ui_callback(widget,callback,mock.Mock())
        label.configure.assert_called_once_with(text='画像を取得できません: bad image')

    def test_full_queue_waits_without_losing_result_and_cancel_unblocks_worker(self):
        app=types.SimpleNamespace(ui_callbacks=queue.Queue(1),events=queue.Queue(),closing=False)
        app.ui_callbacks.put(('first',lambda:None))
        widget=Window(); callback=mock.Mock()
        worker=threading.Thread(target=gui.queue_ui_callback,args=(app,widget,callback))
        worker.start()
        self.assertEqual(app.ui_callbacks.get(timeout=2)[0],'first')
        worker.join(timeout=2); self.assertFalse(worker.is_alive())
        queued_widget,queued_callback=app.ui_callbacks.get(timeout=2)
        self.assertIs(queued_widget,widget); self.assertIs(queued_callback,callback)
        cancel=threading.Event(); app.ui_callbacks.put(('first',callback))
        worker=threading.Thread(target=gui.queue_ui_callback,args=(app,widget,callback,cancel))
        worker.start(); cancel.set(); worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(app.ui_callbacks.qsize(),1)

    def test_closed_window_is_not_updated_and_unexpected_callback_errors_are_logged(self):
        widget=Window(); widget.exists=False; callback=mock.Mock(); log=mock.Mock()
        gui.dispatch_ui_callback(widget,callback,log); callback.assert_not_called()
        widget.exists=True
        gui.dispatch_ui_callback(widget,mock.Mock(side_effect=ValueError('bad')),log)
        self.assertIn('ValueError: bad',log.error.call_args.args[0])


class MergeTraversalTests(unittest.TestCase):
    def dialog(self):
        dialog=object.__new__(gui.ConcatDialog)
        dialog.kind='text'; dialog.paths=[]; dialog.win=Window()
        dialog.sort_mode=types.SimpleNamespace(get=lambda:'added')
        dialog._inspect_file_async=mock.Mock(); dialog._render=mock.Mock()
        return dialog

    def test_symlink_cycle_is_visited_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); child=root/'child'; child.mkdir()
            file=child/'file.txt'; file.write_text('sample',encoding='utf-8')
            try: (child/'back').symlink_to(root,target_is_directory=True)
            except (OSError,NotImplementedError): self.skipTest('directory symlink unavailable')
            dialog=self.dialog()
            dialog._accept([root])
            self.assertEqual(dialog.paths,[file.resolve()])
            dialog._inspect_file_async.assert_called_once_with(file.resolve())

    def test_limit_requires_confirmation_before_partial_addition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for i in range(4): (root/f'{i}.txt').write_text('x',encoding='utf-8')
            dialog=self.dialog()
            with mock.patch.object(gui,'MERGE_SCAN_ENTRY_LIMIT',3), \
                 mock.patch.object(gui.messagebox,'askyesno',return_value=False) as confirm:
                dialog._accept([root])
            confirm.assert_called_once(); self.assertEqual(dialog.paths,[])
            dialog._inspect_file_async.assert_not_called()
            with mock.patch.object(gui,'MERGE_SCAN_ENTRY_LIMIT',3), \
                 mock.patch.object(gui.messagebox,'askyesno',return_value=True):
                dialog._accept([root])
            self.assertEqual(len(dialog.paths),2)

    def test_metadata_workers_are_bounded_and_stop_after_dialog_closes(self):
        dialog=self.dialog()
        dialog._inspect_file_async=gui.ConcatDialog._inspect_file_async.__get__(dialog)
        dialog.file_info={}; dialog.metadata_pending=set(); dialog.metadata_queue=queue.Queue()
        dialog.metadata_jobs=queue.Queue(); dialog.metadata_lock=threading.Lock()
        dialog.metadata_worker_count=0; dialog.metadata_stop=threading.Event()
        dialog.app=types.SimpleNamespace(live_capture_python=lambda:Path('/does-not-exist'),events=queue.Queue())
        started=threading.Event(); release=threading.Event(); lock=threading.Lock(); active=[]
        def inspect(*args,**kwargs):
            with lock:
                active.append(1)
                if len(active)==4: started.set()
            release.wait(3)
            return 'ok'
        try:
            with mock.patch.object(gui,'inspect_merge_file',side_effect=inspect):
                for i in range(30):dialog._inspect_file_async(Path(f'file-{i}.txt'))
                self.assertTrue(started.wait(3))
                with lock:self.assertEqual(len(active),4)
                self.assertEqual(dialog.metadata_worker_count,4)
                dialog.metadata_stop.set(); release.set()
                for _ in range(4):dialog.metadata_queue.get(timeout=3)
                with lock:self.assertEqual(len(active),4)
        finally:
            release.set()


class DiarizationWarningTests(unittest.TestCase):
    def test_marker_parsing_is_bounded_and_rejects_non_ascii_counts(self):
        prefix=gui.DIARIZATION_WARNING_PREFIX
        self.assertEqual(gui.diarization_warning_count(prefix+'12\n'),12)
        for invalid in ('0','-1','１２','1xyz','9999999999'):
            self.assertIsNone(gui.diarization_warning_count(prefix+invalid))
        self.assertIsNone(gui.diarization_warning_count('other'))

    def test_recognized_text_is_saved_and_partial_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.wav'; source.write_bytes(b'fake')
            dest=root/'result.txt'; script=root/'engine.py'
            script.write_text('import sys\nfrom pathlib import Path\n'
                'Path(sys.argv[1]).write_text("[00:00:00] UNKNOWN: test\\n",encoding="utf-8")\n'
                'print("@@TRANSCRIBER_DIARIZATION_WARNING@@3",flush=True)\n',encoding='utf-8')
            app=object.__new__(gui.UnifiedApp)
            app.events=queue.Queue(); app.stop_event=threading.Event()
            app.active_process=None; app.process_lock=threading.Lock()
            with mock.patch.object(gui,'build_command',side_effect=lambda source,stage,*args:
                                   [sys.executable,'-u',str(script),str(stage)]):
                status,detail=app._run_one(source,dest,Config(),Path(sys.executable),'','cpu')
            self.assertEqual(status,'一部完了')
            self.assertIn('話者分離3件失敗',detail)
            self.assertIn('UNKNOWN: test',dest.read_text(encoding='utf-8-sig'))
            events=list(app.events.queue)
            self.assertTrue(any(event[0]=='transcript-saved' and event[3]=='一部完了' for event in events))
            self.assertTrue(any(event[0]=='errorlog' and '話者分離3件失敗' in event[1] for event in events))
            self.assertEqual(gui.job_status_label(status,detail),'一部完了（話者分離3件失敗）')

    def test_watched_partial_result_does_not_repeat_or_move_original(self):
        class StopAfterEightPolls:
            def __init__(self):self.cycles=0
            def is_set(self):return self.cycles>=8
            def wait(self,seconds):self.cycles+=1
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.wav'; source.write_bytes(b'fake audio')
            app=object.__new__(gui.UnifiedApp)
            app.events=queue.Queue(); app.stop_event=StopAfterEightPolls()
            app._resolve_device=mock.Mock(return_value='cpu')
            app._unique_processed_pair=mock.Mock(return_value=(root/'processed'/'source.wav',root/'processed'/'source.txt'))
            app._run_one=mock.Mock(return_value=('一部完了','source.wav / 話者分離3件失敗。保存した文字起こしの話者ラベルを確認してください。'))
            app._watch_worker(root,Config(),Path(sys.executable),'')
            app._run_one.assert_called_once()
            self.assertTrue(source.exists())
            self.assertTrue(any(event[0]=='job' and '話者分離3件失敗' in event[2] for event in app.events.queue))

    def test_live_chunk_reports_warning_and_keeps_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'chunk.wav'; source.write_bytes(b'fake')
            script=root/'engine.py'
            script.write_text('import sys\nfrom pathlib import Path\n'
                'Path(sys.argv[1]).write_text("[00:00:00] UNKNOWN: test\\n",encoding="utf-8")\n'
                'print("@@TRANSCRIBER_DIARIZATION_WARNING@@2",flush=True)\n',encoding='utf-8')
            app=object.__new__(gui.UnifiedApp)
            app.events=queue.Queue(); app.live_cancel=threading.Event(); app.stop_event=threading.Event()
            app.process_lock=threading.Lock(); app.active_process=None; app.live_stopping=False
            with mock.patch.object(gui,'build_command',side_effect=lambda source,stage,*args:
                                   [sys.executable,'-u',str(script),str(stage)]), \
                 mock.patch.object(gui,'is_silent_pcm',return_value=False), \
                 mock.patch.object(gui,'append_provisional',return_value=1) as append:
                app._recognize_live_chunk(source,0,0,root/'preview.txt',Config(),Path(sys.executable),'','cpu',root)
            append.assert_called_once()
            self.assertTrue(any(event[0]=='log' and '話者分離2件失敗' in event[1] for event in app.events.queue))


if __name__ == '__main__':
    unittest.main()
