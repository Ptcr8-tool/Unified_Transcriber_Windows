"""Actual Tk checks for complete previews, copying, and mandatory target selection."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unified_gui as gui
import test_v25_studio_ui as fixtures
from engine_progress import EngineUpdate
from gamebar_recording import GameBarOptions
from live_config import LiveOptions
from capture_preferences import saved_options


class LivePreviewGUI(fixtures.StudioGUIFixture):
    def test_live_text_accumulates_past_150_lines_copies_all_then_final_resets_once(self):
        a=self.app;a._set_busy(True)
        for index in range(180):
            a.events.put(('engine-progress',EngineUpdate('ライブ認識',snippet=f'発言{index:03d}')))
        a._poll();self.root.update()
        shown=a.recognized_box.get('1.0','end-1c')
        self.assertTrue(shown.startswith('発言000\n'))
        self.assertTrue(shown.endswith('発言179\n'))
        self.assertEqual(len(shown.splitlines()),180)
        a.copy_preview_btn.invoke();self.root.update()
        self.assertEqual(self.root.clipboard_get(),shown)
        a.events.put(('new-file','録音全体.wav',1,1))
        a.events.put(('engine-progress',EngineUpdate('音声認識',snippet='先頭からの最終版')))
        a.events.put(('engine-progress',EngineUpdate('音声認識',snippet='最終版の続き')))
        a._poll()
        self.assertEqual(a.recognized_box.get('1.0','end-1c'),'先頭からの最終版\n最終版の続き\n')
        a.events.put(('new-file','次のファイル.wav',1,1));a._poll()
        self.assertEqual(a.recognized_box.get('1.0','end-1c'),'')

    def test_empty_copy_does_not_replace_existing_clipboard(self):
        self.root.clipboard_clear();self.root.clipboard_append('既存クリップボード')
        self.app.copy_preview_btn.invoke()
        self.assertEqual(self.root.clipboard_get(),'既存クリップボード')

    def test_new_live_session_clears_previous_preview_once(self):
        a=self.app
        a.recognized_box.configure(state='normal');a.recognized_box.insert('end','前の認識');a.recognized_box.configure(state='disabled')
        with patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
             patch.object(a,'_require_environment',return_value=Path(sys.executable)), \
             patch('unified_gui.threading.Thread') as thread:
            a.start_live(LiveOptions(folder=self.folder))
        thread.return_value.start.assert_called_once()
        self.assertEqual(a.recognized_box.get('1.0','end-1c'),'')
        a.events.put(('engine-progress',EngineUpdate('ライブ認識',snippet='新しいライブ')));a._poll()
        self.assertEqual(a.recognized_box.get('1.0','end-1c'),'新しいライブ\n')

    def test_gamebar_picker_cancel_does_not_start_and_selection_saves_new_target(self):
        a=self.app;a.config.capture_setup_complete=True
        options=GameBarOptions(target_title='Meeting',target_hwnd=42,destination=self.folder,
                              realtime_transcription=True,chunk_seconds=10)
        a.save_capture_preferences('gamebar',options,True)
        with patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
             patch('screen_capture.windows',return_value=[('Meeting',42),('New window',99)]), \
             patch('window_picker.windows',return_value=[('Meeting',42),('New window',99)]), \
             patch('window_picker.load_thumbnail',side_effect=OSError('No image in test')), \
             patch.object(a,'start_gamebar_recording') as start:
            picker=a.open_capture('gamebar');self.root.update()
            self.assertIsNone(picker.selected)
            start.assert_not_called();picker.close();self.root.update()
            start.assert_not_called()
            picker=a.open_capture('gamebar');self.root.update()
            picker.select(99);picker.accept();self.root.update()
            updated=replace(options,target_title='New window',target_hwnd=99)
            start.assert_called_once_with(updated)
            self.assertEqual(saved_options(a.config,'gamebar'),replace(updated,target_title='',target_hwnd=None))

    def test_recording_without_live_asr_keeps_previous_preview_until_final_starts(self):
        a=self.app
        a.recognized_box.configure(state='normal');a.recognized_box.insert('end','前の認識');a.recognized_box.configure(state='disabled')
        with patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
             patch('unified_gui.threading.Thread'):
            a.start_live(LiveOptions(folder=self.folder,mode='record'))
        self.assertEqual(a.recognized_box.get('1.0','end-1c'),'前の認識')

    def test_gamebar_live_and_final_settings_are_independent_and_restored(self):
        with patch('screen_capture.windows',return_value=[('Meeting',42)]):
            dialog=gui.GameBarRecordingDialog(self.app);self.root.update()
            self.assertEqual(str(dialog.interval_combo.cget('state')),'disabled')
            dialog.realtime.set(True);dialog._refresh_live_controls()
            self.assertEqual(str(dialog.interval_combo.cget('state')),'readonly')
            dialog.interval.set('10');dialog.auto.set(False)
            dialog.target.set(next(iter(dialog.windows)));dialog.skip_settings.set(True)
            with patch.object(gui.messagebox,'showinfo'):dialog._start(save_only=True)
            options=saved_options(self.app.config,'gamebar')
            self.assertTrue(options.realtime_transcription);self.assertFalse(options.auto_transcribe)
            self.assertEqual(options.chunk_seconds,10)
            dialog.win.destroy()
            restored=gui.GameBarRecordingDialog(self.app);self.root.update()
            self.assertTrue(restored.realtime.get());self.assertFalse(restored.auto.get())
            self.assertEqual(restored.interval.get(),'10')
            self.assertEqual(str(restored.interval_combo.cget('state')),'readonly')
            restored.win.destroy()

    def test_gamebar_live_requires_asr_environment_before_recording(self):
        a=self.app
        with patch('unified_gui.sys.platform','win32'), \
             patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
             patch.object(a,'_require_environment',return_value=None) as require, \
             patch('unified_gui.threading.Thread') as thread:
            self.assertFalse(a.start_gamebar_recording(GameBarOptions(destination=self.folder,realtime_transcription=True)))
        require.assert_called_once();thread.assert_not_called();self.assertFalse(a.busy)

    def test_gamebar_live_start_clears_old_text_and_stop_is_idempotent(self):
        a=self.app
        a.recognized_box.configure(state='normal');a.recognized_box.insert('end','前の認識');a.recognized_box.configure(state='disabled')
        with patch('unified_gui.sys.platform','win32'), \
             patch.object(a,'live_capture_python',return_value=Path(sys.executable)), \
             patch.object(a,'_require_environment',return_value=Path(sys.executable)), \
             patch('unified_gui.media_binary',return_value=None), \
             patch('unified_gui.threading.Thread') as thread:
            self.assertTrue(a.start_gamebar_recording(GameBarOptions(destination=self.folder,realtime_transcription=True)))
            self.assertEqual(a.recognized_box.get('1.0','end-1c'),'')
            a.live_chunk_active=True;a.active_process=object()
            a.stop();a.stop()
            self.assertTrue(a.gamebar_stop.is_set());self.assertFalse(a.stop_event.is_set())
            self.assertEqual(thread.return_value.start.call_count,2,'One worker and one partial-ASR cancellation')
            self.assertEqual(str(a.stop_btn.cget('state')),'disabled')


if __name__=='__main__':unittest.main()
