"""Functional checks for setup, appearance, per-input destinations and completion flow."""
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unified_gui as gui
import studio_ui
import test_v25_studio_ui as fixtures
from core import choose_output
from studio_workflow import CaptureSetupDialog, WelcomeDialog
from transcript_tools import export_transcript_csv
from ui_controls import ToggleSwitch


class WorkflowTests(fixtures.StudioGUIFixture):
    def transcript(self, name='result'):
        path = self.folder/(name+'.txt')
        path.write_text('[00:00:01] SPEAKER_00: 発言です。\n[00:00:03] SPEAKER_01: はい。\n', encoding='utf-8')
        return path

    def test_first_run_prompt_can_open_setup_and_remembers_dismissal(self):
        self.app.offer_initial_setup();self.root.update()
        win = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        self.assertIn('セットアップ', win.title())
        later = next(w for w in fixtures.descendants(win) if isinstance(w, ttk.Button) and w.cget('text') == '後で')
        later.invoke()
        self.assertTrue(self.app.config.onboarding_seen)
        self.app.offer_initial_setup()
        self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))
        self.app.config.onboarding_seen = False
        welcome = WelcomeDialog(self.app)
        with patch.object(gui, 'BulkSetupDialog') as setup:
            welcome.start()
        setup.assert_called_once_with(self.app)

    def test_capture_shortcuts_require_success_once_and_can_be_reopened(self):
        a = self.app
        with patch.object(gui, 'CaptureSetupDialog') as setup, patch.object(a.ui, '_dialog') as dialog, \
             patch.object(a, 'live_capture_python', return_value=Path(sys.executable)):
            a.open_capture('live')
            setup.assert_called_once_with(a, 'live');dialog.assert_not_called()
            a.config.capture_setup_complete = True
            a.open_capture('live');a.open_capture('gamebar')
            self.assertEqual(dialog.call_args_list[0].args, ('live',))
            self.assertEqual(dialog.call_args_list[1].args, ('gamebar',))
            a.open_capture_setup()
            self.assertEqual(setup.call_args.args, (a, None))
        with patch.object(gui, 'CaptureSetupDialog') as setup, \
             patch.object(a, 'live_capture_python', return_value=self.folder/'missing.exe'):
            a.open_capture('gamebar')
            setup.assert_called_once_with(a, 'gamebar')

    def test_setup_completion_opens_requested_capture_and_failure_does_not(self):
        a = self.app
        a.pending_capture_target = 'live'
        a._set_busy(True)
        with patch.object(a, 'open_capture') as open_capture:
            a.events.put(('capture-ready',));a.events.put(('done', '録音環境のセットアップ完了'))
            a._poll();self.root.update()
            self.assertTrue(a.config.capture_setup_complete)
            open_capture.assert_called_once_with('live',force_settings=True)
            open_capture.reset_mock()
            a.config.capture_setup_complete = False;a.capture_verified = False
            a.pending_capture_target = 'gamebar';a._set_busy(True)
            a.events.put(('done', '録音環境のセットアップ失敗'))
            a._poll();self.root.update()
            open_capture.assert_not_called()
            self.assertFalse(a.config.capture_setup_complete)
            self.assertIsNone(a.pending_capture_target)

    def test_capture_setup_cancel_is_not_marked_complete(self):
        dialog = CaptureSetupDialog(self.app, 'gamebar');self.root.update()
        dialog.win.destroy()
        self.assertFalse(self.app.config.capture_setup_complete)
        dialog = CaptureSetupDialog(self.app, 'gamebar');self.root.update()
        with patch.object(self.app, 'start_capture_setup') as start:
            dialog.start()
        start.assert_called_once()
        self.assertEqual(self.app.pending_capture_target, 'gamebar')
        self.assertFalse(self.app.capture_verified)

    def test_capture_worker_emits_verified_only_on_uncanceled_success(self):
        class Process:
            def __init__(self, rc):self.rc=rc;self.stdout=io.StringIO('check\n')
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def wait(self, timeout=None):return self.rc
            def poll(self):return self.rc
        for rc, canceled, expected in ((0, False, True), (1, False, False), (0, True, False)):
            with self.subTest(rc=rc,canceled=canceled):
                self.app.stop_event.set() if canceled else self.app.stop_event.clear()
                with patch.object(gui.subprocess, 'Popen', return_value=Process(rc)), \
                     patch.object(self.app, '_kill'):
                    self.app._capture_setup_worker()
                events=[]
                while not self.app.events.empty():events.append(self.app.events.get_nowait())
                self.assertEqual(any(e[0]=='capture-ready' for e in events), expected)
        self.app.stop_event.clear()

    def test_top_buttons_call_capture_entrypoint_and_busy_disables_them(self):
        with patch.object(self.app, 'open_capture') as open_capture:
            self.app.ui.live_shortcut.invoke();self.app.ui.gamebar_shortcut.invoke()
            self.assertEqual([call.args for call in open_capture.call_args_list], [('live',),('gamebar',)])
            self.app._set_busy(True)
            self.app.ui.live_shortcut.invoke();self.app.ui.gamebar_shortcut.invoke()
            self.assertEqual(open_capture.call_count, 2)

    def test_manual_and_system_themes_preserve_queue_and_preview(self):
        paths = self.add_samples()
        a = self.app;view = a.ui
        a.recognized_box.configure(state='normal');a.recognized_box.insert('end', 'この内容を保持');a.recognized_box.configure(state='disabled')
        a.tree.selection_set(str(paths[0]));original_tree = a.tree
        view.theme_toggle.buttons['dark'].invoke()
        self.assertEqual(a.config.theme_mode, 'dark')
        self.assertEqual(a.logbox.cget('background'), studio_ui.DARK['surface'])
        self.assertIs(a.tree, original_tree)
        self.assertEqual(a.tree.selection(), (str(paths[0]),))
        self.assertIn('この内容', a.recognized_box.get('1.0','end'))
        with patch.object(studio_ui, 'system_is_dark', return_value=False):
            view.theme_toggle.buttons['system'].invoke()
        self.assertEqual(a.config.theme_mode, 'system')
        self.assertEqual(view.theme_var.get(),'system')
        self.assertEqual(a.logbox.cget('background'), studio_ui.LIGHT['surface'])
        with patch.object(studio_ui, 'system_is_dark', return_value=True):
            view._poll_system_theme()
        self.assertEqual(a.logbox.cget('background'), studio_ui.DARK['surface'])
        self.assertEqual(view.theme_var.get(),'system')

    def test_all_checkbox_controls_are_switches_and_keep_boolean_semantics(self):
        dialog = gui.BulkSetupDialog(self.app);self.root.update()
        checks=[w for w in fixtures.descendants(dialog.win) if isinstance(w, ttk.Checkbutton)]
        self.assertTrue(checks)
        self.assertTrue(all(isinstance(w,ToggleSwitch) for w in checks))
        before=dialog.with_audio.get()
        check=next(w for w in checks if '録音環境も' in str(w.cget('text')))
        check.invoke();self.assertNotEqual(dialog.with_audio.get(), before)
        check.configure(state='disabled');check.invoke()
        self.assertNotEqual(dialog.with_audio.get(), before)
        dialog.win.destroy()

    def test_accordion_commands_and_column_switch_keep_state_on_reopen(self):
        view=self.app.ui
        view.selection_button.toggle();self.root.update()
        self.assertTrue(view.selection_button.opened)
        view.selection_button.group_buttons['表示'].invoke();self.root.update()
        switches=[w for w in fixtures.descendants(view.queue_drawer) if isinstance(w,ToggleSwitch)]
        self.assertEqual(len(switches),1)
        switches[0].invoke()
        self.assertTrue(view.detailed_columns.get())
        self.assertEqual(tuple(self.app.tree['displaycolumns']),view.COLUMNS)
        view.close_drawers();view.selection_button.toggle();self.root.update()
        self.assertEqual(view.selection_button.active_group,'表示')
        self.assertTrue(view.detailed_columns.get())
        self.app._set_busy(True)
        self.assertFalse(view.selection_button.opened)

    def test_accordion_opens_inline_for_mouse_and_keyboard(self):
        button=self.app.ui.selection_button
        button.event_generate('<ButtonPress-1>', x=10, y=10)
        button.event_generate('<ButtonRelease-1>', x=10, y=10)
        self.root.update()
        self.assertTrue(button.opened)
        self.assertTrue(self.app.ui.queue_drawer.winfo_ismapped())
        menu=button.nametowidget(str(button.cget('menu')))
        self.assertFalse(menu.winfo_ismapped())
        button.focus_force();button.event_generate('<space>');self.root.update()
        self.assertFalse(button.opened)

    def test_bulk_setup_respects_recording_environment_toggle(self):
        from bulk_catalog import preset
        plan=preset('recommended-cpu')
        with patch.object(gui.threading,'Thread') as thread:
            self.app.start_bulk_setup(plan,with_audio=False)
        self.assertFalse(thread.call_args.kwargs['args'][2])
        thread.return_value.start.assert_called_once()

    def test_compact_accordion_scrolls_and_keeps_queue_visible(self):
        self.add_samples()
        self.root.geometry('900x640');self.root.update()
        view=self.app.ui;view.selection_button.toggle()
        self.root.after(180,self.root.quit);self.root.mainloop()
        self.assertGreaterEqual(self.app.tree.winfo_height(),60)
        canvas=next(w for w in view.queue_drawer.winfo_children() if isinstance(w,tk.Canvas))
        self.assertLess(canvas.winfo_height(),canvas.bbox('all')[3])
        commands=[w for w in fixtures.descendants(canvas) if isinstance(w,ttk.Button) and w.cget('style')=='Drawer.TButton']
        self.assertTrue(all(w.winfo_width()>300 for w in commands))
        view.selection_button.group_buttons['表示'].invoke();self.root.update()
        canvas.yview_moveto(1);self.root.update()
        switch=next(w for w in fixtures.descendants(canvas) if isinstance(w,ToggleSwitch))
        self.assertGreaterEqual(switch.winfo_rooty(),canvas.winfo_rooty())
        self.assertLessEqual(switch.winfo_rooty()+switch.winfo_height(),
                             canvas.winfo_rooty()+canvas.winfo_height())

    def test_default_input_folder_shortcut_preserves_other_preferences(self):
        a=self.app
        a.config.theme_mode='dark';a.config.capture_setup_complete=True
        a.config.onboarding_seen=True;a.config.auto_open_speaker=True
        dialog=gui.SettingsDialog(a);self.root.update()
        button=next(w for w in fixtures.descendants(dialog.win) if isinstance(w,ttk.Button) and w.cget('text')=='入力と同じ場所')
        button.invoke();dialog._save()
        self.assertTrue(a.config.output_same_as_input)
        self.assertTrue(a.config.capture_setup_complete)
        self.assertTrue(a.config.onboarding_seen)
        self.assertTrue(a.config.auto_open_speaker)
        self.assertEqual(a.config.theme_mode,'dark')
        source=self.folder/'別フォルダー'/'録音.wav'
        self.assertEqual(choose_output(source,a.config).parent,source.parent)

    def test_selected_inputs_in_different_folders_get_their_own_destination(self):
        a=self.app
        paths=[]
        for name in ('A','B'):
            folder=self.folder/name;folder.mkdir();p=folder/'source.wav';p.write_bytes(b'sample');paths.append(p)
        a._add_paths(paths);a.ui.select_all();a.edit_file_outputs();self.root.update()
        win=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel))
        widgets=list(fixtures.descendants(win))
        next(w for w in widgets if isinstance(w,ttk.Button) and w.cget('text')=='入力と同じ場所').invoke()
        next(w for w in widgets if isinstance(w,ttk.Button) and w.cget('text')=='この設定を適用').invoke()
        for path in paths:self.assertEqual(a.job_options[str(path)][1],str(path.parent))
        self.assertFalse(a.config.output_same_as_input)

    def test_import_and_speaker_destinations_have_input_folder_shortcuts(self):
        video=self.add_samples(1)[0]
        dialog=gui.StandardRecordingDialog(self.app);dialog.video.set(str(video));dialog.same_input_destination()
        self.assertEqual(Path(dialog.dest.get()),video.parent);dialog.win.destroy()
        source=self.transcript()
        dialog=gui.SpeakerEditDialog(self.app,source=source);self.root.update()
        name=Path(dialog.output.get()).name;dialog._same_input_location()
        self.assertEqual(Path(dialog.output.get()),source.parent/name)
        self.assertNotEqual(Path(dialog.output.get()),source)
        dialog.win.destroy()

    def test_completed_result_uses_actual_saved_name_for_navigation(self):
        path=self.add_samples(1)[0];result=self.transcript('renamed_2')
        a=self.app
        a.events.put(('transcript-saved',str(path),[str(result)],'完了'));a._poll();self.root.update()
        self.assertEqual(a.job_outputs[str(path)],[result])
        a.tree.selection_set(str(path))
        with patch.object(gui,'open_folder') as folder:
            box=a.tree.bbox(str(path),'destination')
            a._on_tree_double_click(SimpleNamespace(x=box[0]+6,y=box[1]+6))
            folder.assert_called_once_with(result)
            a.open_selected_source();self.assertEqual(folder.call_args.args,(path,))
        with patch.object(gui,'ResultDialog') as dialog:
            box=a.tree.bbox(str(path),'file')
            a._on_tree_double_click(SimpleNamespace(x=box[0]+6,y=box[1]+6))
            dialog.assert_called_once_with(a,str(path))
        with patch.object(a,'open_speaker_editor') as editor:
            a.edit_selected_speakers();editor.assert_called_once_with(result)
        a.clear_files();self.assertEqual(a.job_outputs,{})

    def test_auto_speaker_edit_waits_for_batch_then_opens_each_result_in_order(self):
        a=self.app;a.config.auto_open_speaker=True
        paths=self.add_samples();outputs=[self.transcript('first'),self.transcript('second')]
        a._set_busy(True)
        for source,result in zip(paths,outputs):
            a.events.put(('transcript-saved',str(source),[str(result)],'完了'))
        a._poll();self.root.update()
        self.assertIsNone(a.speaker_window)
        a.events.put(('done','完了'));a._poll();self.root.update()
        self.assertIsNotNone(a.speaker_window)
        first=a.speaker_window
        entries=[w for w in fixtures.descendants(first) if isinstance(w,ttk.Entry)]
        self.assertEqual(entries[0].get(),str(outputs[0]))
        first.destroy();self.root.update()
        second=a.speaker_window
        self.assertIsNotNone(second)
        entries=[w for w in fixtures.descendants(second) if isinstance(w,ttk.Entry)]
        self.assertEqual(entries[0].get(),str(outputs[1]))
        second.destroy();self.root.update()
        self.assertIsNone(a.speaker_window)
        self.assertEqual(a.pending_speaker_edits,[])
        # Recording-only working WAVs must not become persistent queue entries.
        temporary_source=self.folder/'temporary-recording.wav';temporary_source.write_bytes(b'RIFF')
        a._set_busy(True)
        a.events.put(('transcript-saved',str(temporary_source),[str(outputs[0])],'完了'))
        a._poll()
        self.assertNotIn(str(temporary_source),a.job_sources)
        self.assertEqual(a.pending_speaker_edits,[outputs[0]])

    def test_auto_speaker_edit_off_and_csv_only_manual_edit(self):
        path=self.add_samples(1)[0];txt=self.transcript();csv_path=self.folder/'result.csv'
        export_transcript_csv(txt,csv_path)
        a=self.app
        a.events.put(('transcript-saved',str(path),[str(csv_path)],'完了'));a.events.put(('done','完了'))
        a._poll();self.root.update()
        self.assertIsNone(a.speaker_window)
        self.assertEqual(a.pending_speaker_edits,[])
        editor=a.open_speaker_editor(csv_path);self.root.update()
        self.assertEqual(set(editor.rename_vars),{'SPEAKER_00','SPEAKER_01'})
        self.assertEqual(editor.input.get(),str(csv_path))
        editor.win.destroy()


if __name__=='__main__':
    unittest.main()
