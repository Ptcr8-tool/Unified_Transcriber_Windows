"""Exercise real Tk controls after the v25 menu and workspace reorganization."""
from contextlib import ExitStack
import gc
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config
from engine_progress import EngineUpdate
import unified_gui
from unified_gui import UnifiedApp


def descendants(parent):
    for child in parent.winfo_children():
        yield child
        yield from descendants(child)


@unittest.skipUnless(os.environ.get('DISPLAY') or sys.platform == 'win32',
                     'GUI checks require a display')
class StudioGUIFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Windows may return an 8.3 TEMP alias. The queue intentionally resolves
        # inputs, so use the same canonical location for fixture paths and IDs.
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.cfg = Config(output_dir=str(self.folder/'transcripts'))
        self.stack.enter_context(patch.object(unified_gui, 'load_config', return_value=self.cfg))
        self.stack.enter_context(patch.object(unified_gui, 'save_config'))
        self.stack.enter_context(patch('core.save_config'))
        self.stack.enter_context(patch('studio_workflow.save_config'))
        self.stack.enter_context(patch('hotkey_ui.save_config'))
        self.stack.enter_context(patch.object(unified_gui, 'ensure_output_directories'))
        self.stack.enter_context(patch.object(unified_gui, 'CODE_DIR', self.folder))
        # Functional GUI fixtures represent an already-provisioned environment.
        # The readiness regressions explicitly override this when testing repair.
        self.stack.enter_context(patch.object(UnifiedApp, 'media_tools_ready', return_value=True))
        diagnostic = SimpleNamespace(session_path=self.folder/'session.log',
            error_path=self.folder/'errors.log', folder=self.folder,
            write=MagicMock(), error=MagicMock())
        self.stack.enter_context(patch.object(unified_gui, 'SessionLogs', return_value=diagnostic))
        self.root = tk.Tk()
        self.callback_errors = []
        errors = self.callback_errors
        self.root.report_callback_exception = lambda *exc: errors.append(exc)
        self.app = UnifiedApp(self.root)
        self.root.update()

    def tearDown(self):
        try:
            self._close_gui()
        finally:
            # Mock call histories can retain bound methods and widgets. Release
            # them before collecting destroyed Tk objects on their owner thread.
            self.stack.close()
            self.__dict__.pop('app', None)
            self.__dict__.pop('root', None)
            gc.collect()
        self.assertEqual(self.callback_errors, [], 'Tk callback failed')

    def _close_gui(self):
        root = getattr(self, 'root', None)
        if root is None:
            return
        for job in root.tk.call('after', 'info'):
            root.tk.call('after', 'cancel', job)
        self.app.busy = False
        self.app.close()
        root.__dict__.pop('report_callback_exception', None)

    def add_samples(self, count=2):
        paths = []
        for i in range(count):
            path = self.folder/f'日本語の録音 {i+1}.wav'
            path.write_bytes(b'RIFF sample')
            paths.append(path)
        self.app._add_paths(paths)
        self.root.update()
        return paths

    def finish_dialog(self, win, previous_jobs):
        for job in set(self.root.tk.call('after', 'info'))-previous_jobs:
            self.root.tk.call('after', 'cancel', job)
        if win.winfo_exists():
            win.destroy()
        self.root.update()

class StudioRegressionTests(StudioGUIFixture):
    def test_closed_settings_and_recording_dialogs_release_their_tk_variables(self):
        owner = threading.get_ident()
        finalized = []
        for factory, attribute in ((unified_gui.SettingsDialog, 'output_mode'),
                                   (unified_gui.LiveCaptureDialog, 'source'),
                                   (unified_gui.GameBarRecordingDialog, 'source')):
            with self.subTest(dialog=factory.__name__):
                dialog = factory(self.app)
                controller = weakref.ref(dialog, lambda _ref:
                    finalized.append(threading.get_ident()))
                variable = weakref.ref(getattr(dialog, attribute), lambda _ref:
                    finalized.append(threading.get_ident()))
                dialog.win.destroy()
                del dialog
                # Closing a dialog while recording must release its variables
                # immediately; the next worker allocation may trigger GC.
                self.assertIsNone(controller())
                self.assertIsNone(variable())
        self.assertEqual(finalized, [owner] * 6)

    def test_closed_gui_is_collected_before_an_audio_worker_can_run_gc(self):
        owner = threading.get_ident()
        finalized = []
        resources = {'root': self.root, 'app': self.app, 'view': self.app.ui,
                     'variable': self.app.ui.queue_summary, 'image': self.root._studio_icon}
        references = {name: weakref.ref(value, lambda _ref, name=name:
            finalized.append((name, threading.get_ident()))) for name, value in resources.items()}
        del resources
        # Retain the TestCase itself, just as unittest does between tests, while
        # exercising its real teardown rather than dropping the whole fixture.
        self.tearDown()
        self.assertTrue(all(reference() is None for reference in references.values()),
                        [name for name, reference in references.items() if reference() is not None])
        self.assertEqual({thread for _, thread in finalized}, {owner})
        errors = []
        def record_error(error):
            errors.append((threading.get_ident(), str(error.exc_value)))
        with patch.object(sys, 'unraisablehook', record_error):
            worker = threading.Thread(target=gc.collect, name='audio-gc-lifecycle-probe')
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), 'Destroyed Tk objects reached worker-thread GC')
        self.assertEqual(errors, [])

    def test_every_relocated_tool_opens_its_original_dialog(self):
        self.app.config.capture_setup_complete = True
        self.stack.enter_context(patch.object(self.app, 'live_capture_python', return_value=Path(sys.executable)))
        view = self.app.ui
        cases = (
            ('live', 'live', ()), ('gamebar', 'gamebar', ()), ('import', 'standard', ()),
            ('text_merge', 'concat', ('text',)), ('audio_merge', 'concat', ('audio',)),
            ('csv_merge', 'concat', ('csv',)), ('convert', 'convert', ()),
            ('speaker', 'speaker', ()), ('bulk', 'bulk', ()), ('setup', 'setup', ()),
            ('cuda', 'cuda', ()), ('watch', 'watch', ()),
        )
        for action, key, args in cases:
            with self.subTest(action=action):
                factory = MagicMock()
                with patch.dict(view.dialogs, {key: factory}):
                    view.actions[action].invoke()
                factory.assert_called_once_with(self.app, *args)
        factory = MagicMock()
        with patch.dict(view.dialogs, {'settings': factory}):
            self.app.settings_btn.invoke()
        factory.assert_called_once_with(self.app)

    def test_busy_blocks_mutating_tools_but_preserves_queue_policy_and_logs(self):
        a = self.app
        a._set_busy(True, '検証中')
        self.assertEqual(str(a.start_btn.cget('state')), 'disabled')
        self.assertEqual(str(a.stop_btn.cget('state')), 'normal')
        self.assertEqual(str(a.ui.profile_box.cget('state')), 'disabled')
        for control in a.ui.idle_controls:
            self.assertEqual(str(control.cget('state')), 'disabled')
        for key in ('file_policy', 'limits', 'logs', 'export_logs', 'help'):
            self.assertEqual(str(a.ui.actions[key].cget('state')), 'normal')
        factory = MagicMock()
        with patch.dict(a.ui.dialogs, {'live': factory}):
            a.live_btn.invoke()
            a.ui._dialog('live')
        factory.assert_not_called()
        a._set_busy(False)
        self.assertEqual(str(a.ui.profile_box.cget('state')), 'readonly')
        for control in a.ui.idle_controls:
            self.assertEqual(str(control.cget('state')), 'normal')

    def test_add_select_remove_clear_preserves_input_files_and_output_defaults(self):
        a = self.app
        paths = self.add_samples()
        a._add_paths(paths)  # duplicates stay excluded
        a._add_paths([path.parent/'..'/path.parent.name/path.name for path in paths])
        self.assertEqual(len(a.job_sources), 2)
        self.assertEqual(a.tree.get_children(), tuple(str(path) for path in paths))
        self.assertFalse(a.ui.empty_hint.winfo_ismapped())
        a.ui.select_all()
        self.assertEqual(len(a.tree.selection()), 2)
        self.assertIn('2 件選択', a.ui.queue_summary.get())
        for key in a.job_sources:
            self.assertEqual(a.job_options[key], ('txt', self.cfg.output_dir))
        a.tree.selection_set(str(paths[0]))
        a.remove_selected()
        self.assertEqual(len(a.job_sources), 1)
        a.clear_files()
        self.root.update()
        self.assertEqual(a.job_options, {})
        self.assertEqual(a.job_policies, {})
        self.assertTrue(a.ui.empty_hint.winfo_ismapped())
        self.assertTrue(all(path.is_file() for path in paths))

    def test_per_file_output_dialog_does_not_change_default_settings(self):
        paths = self.add_samples()
        self.app.ui.select_all()
        previous = set(self.root.tk.call('after', 'info'))
        self.app.edit_file_outputs()
        self.root.update()
        win = next(x for x in self.root.winfo_children() if isinstance(x, tk.Toplevel))
        widgets = list(descendants(win))
        next(x for x in widgets if isinstance(x, ttk.Combobox)).set('both')
        entry = next(x for x in widgets if isinstance(x, ttk.Entry) and not isinstance(x, ttk.Combobox))
        entry.delete(0, 'end');entry.insert(0, str(self.folder/'chosen'))
        next(x for x in widgets if isinstance(x, ttk.Button) and x.cget('text') == 'この設定を適用').invoke()
        for path in paths:
            self.assertEqual(self.app.job_options[str(path)], ('both', str(self.folder/'chosen')))
        self.assertEqual(self.cfg.output_mode, 'txt')
        self.assertEqual(self.cfg.output_dir, str(self.folder/'transcripts'))
        for job in set(self.root.tk.call('after', 'info'))-previous:
            self.root.tk.call('after', 'cancel', job)

    def test_next_queue_policy_is_editable_while_current_job_is_protected(self):
        paths = self.add_samples()
        a = self.app
        a.active_job_key = str(paths[0])
        a._set_busy(True)
        a.tree.selection_set(str(paths[1]))
        previous = set(self.root.tk.call('after', 'info'))
        a.edit_queue_policies()
        self.root.update()
        win = next(x for x in self.root.winfo_children() if isinstance(x, tk.Toplevel))
        widgets = list(descendants(win))
        next(x for x in widgets if isinstance(x, ttk.Radiobutton) and x.cget('value') == 'rename').invoke()
        next(x for x in widgets if isinstance(x, ttk.Button) and x.cget('text') == 'キューへ適用').invoke()
        self.assertEqual(a.job_policies[str(paths[1])], 'rename')
        self.assertEqual(a.job_policies[str(paths[0])], 'ask')
        a.tree.selection_set(str(paths[0]))
        with patch.object(unified_gui, 'show_error') as error:
            a.edit_queue_policies()
        error.assert_called_once()
        for job in set(self.root.tk.call('after', 'info'))-previous:
            self.root.tk.call('after', 'cancel', job)

    def test_double_click_resolves_data_column_after_display_columns_change(self):
        path = self.add_samples(1)[0]
        a = self.app
        key = str(path)
        with patch.object(a, 'edit_file_outputs') as outputs, patch.object(a, 'edit_queue_policies') as policies:
            box = a.tree.bbox(key, 'format')
            a._on_tree_double_click(SimpleNamespace(x=box[0]+5, y=box[1]+5))
            outputs.assert_called_once()
            policies.assert_not_called()
            a.ui.detailed_columns.set(True);a.ui.toggle_columns();self.root.update()
            box = a.tree.bbox(key, 'policy')
            a._on_tree_double_click(SimpleNamespace(x=box[0]+5, y=box[1]+5))
            policies.assert_called_once()
        self.assertEqual(tuple(a.tree['displaycolumns']), a.ui.COLUMNS)
        self.assertTrue(a.tree.cget('xscrollcommand'))

    def test_progress_queue_results_and_detached_log_stay_synchronized(self):
        path = self.add_samples(1)[0]
        a = self.app
        a._set_busy(True)
        a._show_progress(EngineUpdate('音声認識', .5, '発言内容のプレビュー', '30秒まで認識'))
        self.assertEqual(float(a.progress['value']), 50)
        self.assertIn('発言内容', a.recognized_box.get('1.0', 'end'))
        a.events.put(('job', str(path), '一部完了'))
        a._poll()
        self.assertIn('partial', a.tree.item(str(path), 'tags'))
        a.open_detached_log()
        a.log('追加の処理ログ\n')
        self.assertIn('追加の処理ログ', a.detached_logbox.get('1.0', 'end'))
        self.assertIn('追加の処理ログ', a.logbox.get('1.0', 'end'))
        a._set_busy(False)
        self.assertEqual(a.progress_label.get(), '')
        self.assertFalse(a.progress.winfo_ismapped())

    def test_main_controls_and_both_logs_remain_visible_at_minimum_window_size(self):
        path = self.add_samples(1)[0]
        self.app._set_busy(True, '処理中')
        self.root.geometry('790x550')
        self.root.update()
        a = self.app
        for widget in (a.start_btn, a.stop_btn, a.settings_btn, a.ui.add_button,
                       a.ui.selection_button, a.logbox, a.recognized_box):
            with self.subTest(widget=str(widget)):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreater(widget.winfo_width(), 25)
                self.assertGreaterEqual(widget.winfo_rootx(), self.root.winfo_rootx())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),
                                     self.root.winfo_rootx()+self.root.winfo_width())
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),
                                     self.root.winfo_rooty()+self.root.winfo_height())
        self.assertGreater(a.logbox.winfo_height(), 40)
        self.assertGreater(a.recognized_box.winfo_height(), 40)
        row = a.tree.bbox(str(path))
        self.assertTrue(row)
        self.assertLessEqual(row[1]+row[3], a.tree.winfo_height(),
                             'At least the first queue row must remain fully visible')

    def test_recording_stop_button_still_calls_existing_stop_handler(self):
        a = self.app
        a._set_busy(True)
        a.gamebar_active = True
        a.stop_btn.invoke()
        self.assertTrue(a.gamebar_stop.is_set())
        self.assertEqual(str(a.stop_btn.cget('state')), 'disabled')
        a.gamebar_active = False
        a.live_active = True
        a.live_stop_file = self.folder/'stop-request'
        a.stop_btn.configure(state='normal')
        a.stop_btn.invoke()
        self.assertTrue(a.live_stop_file.exists())
        self.assertFalse(a.live_cancel.is_set())
        a.stop_btn.invoke()
        self.assertFalse(a.live_cancel.is_set())
        a.cancel_live_transcription()
        self.assertTrue(a.live_cancel.is_set())
        a.live_active = False
        a.stop_btn.configure(state='normal')
        a.stop_btn.invoke()
        self.assertTrue(a.stop_event.is_set())

    def test_all_real_dialogs_build_and_close_without_starting_a_worker(self):
        cases = (
            (unified_gui.SettingsDialog, ()), (unified_gui.SetupDialog, ()),
            (unified_gui.BulkSetupDialog, ()), (unified_gui.CudaTestDialog, ()),
            (unified_gui.ConcatDialog, ('text',)), (unified_gui.ConcatDialog, ('audio',)),
            (unified_gui.ConcatDialog, ('csv',)), (unified_gui.TranscriptConvertDialog, ()),
            (unified_gui.SpeakerEditDialog, ()), (unified_gui.LiveCaptureDialog, ()),
            (unified_gui.GameBarRecordingDialog, ()), (unified_gui.StandardRecordingDialog, ()),
            (unified_gui.WatchDialog, ()),
        )
        for factory, args in cases:
            with self.subTest(dialog=factory.__name__, args=args):
                before = set(self.root.tk.call('after', 'info'))
                dialog = factory(self.app, *args)
                self.root.update()
                self.assertTrue(dialog.win.winfo_ismapped())
                self.assertLessEqual(dialog.win.winfo_height(), self.root.winfo_screenheight()-80)
                self.assertIsNone(self.app.worker)
                for button in descendants(dialog.win):
                    if not isinstance(button, ttk.Button):
                        continue
                    if str(button.cget('text')) not in (
                        '閉じる', 'キャンセル', '設定を保存', '環境をセットアップ',
                        '選択項目を一括セットアップ', 'CUDAをテスト', '結合を開始',
                        '変換を開始', '話者名を置換して保存', '● 録音を開始', '● 録画を開始',
                    ):
                        continue
                    self.assertTrue(button.winfo_ismapped(), f'Hidden action: {button.cget("text")}')
                    self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                        dialog.win.winfo_rooty()+dialog.win.winfo_height(),
                        f'Action outside dialog: {button.cget("text")}')
                self.finish_dialog(dialog.win, before)

    def test_shortcuts_are_bound_and_help_can_open_during_processing(self):
        for sequence in ('<Control-o>', '<Control-Return>', '<F1>'):
            self.assertTrue(self.root.bind(sequence))
        self.assertTrue(self.app.tree.bind('<Control-a>'))
        self.assertTrue(self.app.tree.bind('<Delete>'))
        before = set(self.root.tk.call('after', 'info'))
        self.app._set_busy(True)
        self.app.ui.actions['help'].invoke()
        self.root.update()
        win = next(x for x in self.root.winfo_children() if isinstance(x, tk.Toplevel))
        text = next(x for x in descendants(win) if isinstance(x, tk.Text))
        self.assertIn('Ctrl+Enter', text.get('1.0', 'end'))
        self.assertIn('フォルダー監視', self.app.ui.actions['watch'].menu.entrycget(
            self.app.ui.actions['watch'].index, 'label'))
        self.finish_dialog(win, before)

    def test_closing_dialog_cancels_its_timers_and_keeps_application_timers(self):
        dialog = unified_gui.CudaTestDialog(self.app)
        self.root.update()
        # Application timers may legitimately run and acquire a new ID while
        # the dialog is opening. Classify ownership immediately before close.
        before = set(self.root.tk.call('after', 'info'))
        owned = set(dialog.win._tclCommands or ())
        created = {job for job in before if self.root.tk.splitlist(
            self.root.tk.call('after','info',job)[0])[0] in owned}
        application=before-created
        self.assertTrue(created)
        self.assertTrue(application)
        dialog._close()
        remaining = set(self.root.tk.call('after', 'info'))
        self.assertTrue(application <= remaining)
        self.assertFalse(created & remaining)


if __name__ == '__main__':
    unittest.main()
