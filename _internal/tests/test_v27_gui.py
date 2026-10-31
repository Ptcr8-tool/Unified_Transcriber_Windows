"""Real Tk checks for three-way appearance, quick actions and recording settings."""
from dataclasses import replace
import ctypes
from pathlib import Path
import sys
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unified_gui as gui
import test_v25_studio_ui as fixtures
from live_config import LiveOptions
from gamebar_recording import GameBarOptions
from capture_preferences import saved_options
from hotkey_ui import KeyCaptureDialog,ShortcutSettingsDialog
from window_picker import WindowPickerDialog


class RecordingGUI(fixtures.StudioGUIFixture):
    def send_action_key(self,key,identity,action):
        if sys.platform=='win32':
            # Tk synthetic keys cannot create RegisterHotKey's WM_HOTKEY.
            # Deliver the native message only to this test's owned worker.
            from ctypes import wintypes
            api=ctypes.WinDLL('user32',use_last_error=True)
            api.PostThreadMessageW.argtypes=[wintypes.DWORD,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
            api.PostThreadMessageW.restype=wintypes.BOOL
            worker=self.app.hotkeys.thread
            self.assertIsNotNone(worker);self.assertTrue(worker.is_alive())
            self.assertTrue(api.PostThreadMessageW(worker.native_id,0x0312,identity,
                ((0x70+int(key[1:])-1)<<16)|3))
        else:
            self.root.event_generate('<Control-Alt-KeyPress-'+key+'>')
        deadline=time.monotonic()+1
        while not action.called and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.01)

    def test_three_icon_choices_select_one_mode_and_keep_system_selected(self):
        view=self.app.ui
        self.assertEqual(tuple(view.theme_toggle.buttons),('light','system','dark'))
        for mode in ('light','dark','system'):
            with patch('studio_ui.system_is_dark',return_value=True):
                view.theme_toggle.buttons[mode].invoke()
            self.assertEqual(self.app.config.theme_mode,mode)
            self.assertEqual(sum(b.instate(['selected']) for b in view.theme_toggle.buttons.values()),1)
        with patch('studio_ui.system_is_dark',return_value=False):view._poll_system_theme()
        self.assertEqual(view.theme_var.get(),'system')
        self.assertFalse(view.dark)

    def test_first_top_visit_opens_settings_then_saved_top_starts_and_shift_reopens(self):
        a=self.app;a.config.capture_setup_complete=True
        options=LiveOptions(mode='record',folder=self.folder)
        with patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
             patch.object(a.ui,'_dialog') as dialog,patch.object(a,'start_live') as start:
            a.open_capture('live');dialog.assert_called_once_with('live')
            a.save_capture_preferences('live',options,True)
            dialog.reset_mock();a.ui.live_shortcut.invoke()
            start.assert_called_once_with(options);dialog.assert_not_called()
            a.ui.live_shortcut.event_generate('<Shift-ButtonPress-1>',x=10,y=10)
            a.ui.live_shortcut.event_generate('<Shift-ButtonRelease-1>',x=10,y=10)
            self.root.update();dialog.assert_called_once_with('live')
            start.assert_called_once()
            dialog.reset_mock();a.ui.actions['live'].invoke();dialog.assert_called_once_with('live')
            a.config.skip_live_settings=False;dialog.reset_mock()
            a.open_capture('live');dialog.assert_called_once_with('live')

    def test_gamebar_quick_start_always_asks_for_target_even_with_saved_match(self):
        a=self.app;a.config.capture_setup_complete=True
        options=GameBarOptions(target_hwnd=42,target_title='Meeting',destination=self.folder)
        a.save_capture_preferences('gamebar',options,True)
        for candidates,selected in (([('Meeting',99)],None),([('Unrelated',42)],None)):
            with self.subTest(candidates=candidates), \
                 patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
                 patch('screen_capture.windows',return_value=candidates), \
                 patch('window_picker.WindowPickerDialog') as picker, \
                 patch.object(a,'start_gamebar_recording') as start,patch.object(a.ui,'_dialog') as dialog:
                a.open_capture('gamebar')
                picker.assert_called_once();self.assertEqual(picker.call_args.args[1],selected)
                start.assert_not_called();dialog.assert_not_called()
                picker.call_args.args[2]('Chosen',101)
                start.assert_called_once_with(replace(options,target_hwnd=101,target_title='Chosen'))

    def test_record_only_allows_post_transcription_and_restores_saved_settings(self):
        a=self.app;dialog=gui.LiveCaptureDialog(a);self.root.update()
        dialog.mode.set('録音のみ');dialog.folder.set(str(self.folder));self.root.update()
        self.assertEqual(str(dialog.interval_combo.cget('state')),'disabled')
        self.assertEqual(str(dialog.final_box.cget('state')),'normal')
        self.assertFalse(dialog.post_transcribe.get())
        dialog.final_box.invoke();dialog.skip_settings.set(True)
        dialog.recording_channels.set('L：PC音 / R：マイク')
        self.assertEqual(dialog.source.get(),'マイク＋PC再生音（同時録音）')
        dialog._start(save_only=True)
        options=saved_options(a.config,'live')
        self.assertTrue(options.post_transcribe)
        self.assertFalse(options.realtime_transcription)
        self.assertEqual(options.channel_mode,'split_lr')
        self.assertTrue(a.config.skip_live_settings)
        dialog.win.destroy();self.root.update()
        restored=gui.LiveCaptureDialog(a);self.root.update()
        self.assertEqual(restored.mode.get(),'録音のみ')
        self.assertTrue(restored.post_transcribe.get())
        self.assertEqual(restored.recording_channels.get(),'L：PC音 / R：マイク')
        restored.win.destroy()

    def test_actual_key_events_are_recorded_and_parent_grab_is_restored(self):
        parent=gui.LiveCaptureDialog(self.app);self.root.update()
        manager=MagicMock();value=[]
        capture=KeyCaptureDialog(parent.win,'',value.append,manager);self.root.update()
        capture.win.event_generate('<KeyPress-Control_L>')
        capture.win.event_generate('<KeyPress-Shift_L>')
        capture.win.event_generate('<KeyPress-F8>',state=5)
        self.root.update()
        self.assertEqual(capture.value.get(),'ctrl+shift+f8')
        next(w for w in fixtures.descendants(capture.win) if isinstance(w,ttk.Button) and w.cget('text')=='このキーを使う').invoke()
        self.root.update()
        self.assertEqual(value,['ctrl+shift+f8'])
        self.assertIs(self.root.grab_current(),parent.win)
        self.assertEqual([call.args for call in manager.suspend.call_args_list],[(True,),(False,)])
        parent.win.destroy()

    def test_shortcut_settings_save_and_key_event_routes_to_top_action(self):
        dialog=ShortcutSettingsDialog(self.app);self.root.update()
        for action,key in (('live','f8'),('gamebar','f9'),('stop','f10')):
            dialog.variables[action].set('ctrl+alt+'+key)
        next(w for w in fixtures.descendants(dialog.win) if isinstance(w,ttk.Button) and w.cget('text')=='保存').invoke()
        self.root.update();self.root.focus_force();self.root.update()
        with patch.object(self.app,'open_capture') as start:
            self.send_action_key('F8',1,start)
            start.assert_called_once_with('live')
        with patch.object(self.app,'stop') as stop:
            self.send_action_key('F10',3,stop)
            stop.assert_called_once()

    def test_picker_shows_all_thumbnails_and_can_choose_despite_one_failed_image(self):
        parent=gui.GameBarRecordingDialog(self.app);self.root.update()
        applied=[]
        ppm=b'P6\n16 9\n255\n'+bytes([40,120,200])*16*9
        def image(hwnd):
            if hwnd==2:raise OSError('protected window')
            return ppm,1920,1080
        with patch('window_picker.windows',return_value=[('One',1),('Two',2),('Three',3)]), \
             patch('window_picker.load_thumbnail',side_effect=image):
            picker=WindowPickerDialog(parent.win,None,lambda title,hwnd:applied.append((title,hwnd)))
            for future in picker.futures:future.result(timeout=1)
            picker._poll();self.root.update()
            self.assertEqual(len(picker.cards),3)
            self.assertEqual(set(picker.photos),{1,3})
            self.assertIn('取得できません',str(picker.cards[2][1].cget('text')))
            picker.cards[3][1].event_generate('<Button-1>');self.root.update()
            self.assertEqual(picker.selected,3)
            self.assertEqual(applied,[])
            picker.accept();self.root.update()
            self.assertEqual(applied,[('Three',3)])
            self.assertIs(self.root.grab_current(),parent.win)
        parent.win.destroy()

    def test_gamebar_channel_selection_and_key_capture_entry(self):
        with patch('screen_capture.windows',return_value=[('Meeting',42)]):
            dialog=gui.GameBarRecordingDialog(self.app);self.root.update()
        dialog.target.set(next(iter(dialog.windows)))
        dialog.recording_channels.set('L：PC音 / R：マイク')
        dialog.skip_settings.set(True)
        self.assertTrue(dialog.source.get().startswith('mix'))
        with patch.object(gui.messagebox,'showinfo'):
            dialog._start(save_only=True)
        options=saved_options(self.app.config,'gamebar')
        self.assertEqual(options.target_title,'')
        self.assertIsNone(options.target_hwnd)
        self.assertEqual(options.channel_mode,'split_lr')
        self.assertTrue(self.app.config.skip_gamebar_settings)
        self.assertIsInstance(dialog.window_combo,ttk.Entry)
        dialog._edit_trigger();self.root.update()
        child=next(w for w in dialog.win.winfo_children() if isinstance(w,tk.Toplevel))
        child.destroy();dialog.win.destroy()

    def test_stop_is_idempotent_and_asr_cancel_is_explicit(self):
        a=self.app;a._set_busy(True);a.live_active=True
        a.live_stop_file=self.folder/'stop'
        a.stop();a.stop()
        self.assertTrue(a.live_stop_file.exists())
        self.assertFalse(a.live_cancel.is_set())
        self.assertEqual(str(a.stop_btn.cget('state')),'disabled')
        a.cancel_live_transcription();self.assertTrue(a.live_cancel.is_set())
        a.live_active=False;a.events.put(('done','完了'));a._poll();self.root.update()
        self.assertFalse(a.busy)
        self.assertEqual(str(a.start_btn.cget('state')),'normal')


if __name__=='__main__':unittest.main()
