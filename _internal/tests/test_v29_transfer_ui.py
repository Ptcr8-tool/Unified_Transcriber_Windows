"""Real Tk checks for optional Game Bar target, settings transfer and destinations."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from tkinter import ttk
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
import unified_gui as gui
import test_v25_studio_ui as fixtures
from capture_preferences import saved_options
from settings_transfer import export_settings,write_export
from settings_ui import SettingsExportDialog


def button(win,label):
    return next(w for w in fixtures.descendants(win) if isinstance(w,ttk.Button) and w.cget('text')==label)


class TransferGUI(fixtures.StudioGUIFixture):
    def test_gamebar_can_save_without_any_window_and_start_requests_target(self):
        with patch('screen_capture.windows',return_value=[]):
            dialog=gui.GameBarRecordingDialog(self.app);self.root.update()
        dialog.folder.set(str(self.folder));dialog.skip_settings.set(True)
        with patch.object(gui.messagebox,'showinfo'),patch.object(gui,'show_error') as error:
            dialog._start(save_only=True)
        error.assert_not_called()
        options=saved_options(self.app.config,'gamebar')
        self.assertIsNone(options.target_hwnd)
        self.assertNotIn('target_hwnd',self.app.config.capture_preferences['gamebar'])
        with patch('window_picker.windows',return_value=[('This session',101)]), \
             patch('window_picker.load_thumbnail',side_effect=OSError('No image in test')), \
             patch.object(self.app,'start_gamebar_recording',return_value=True) as start:
            dialog._start();self.root.update();start.assert_not_called()
            next(w for w in fixtures.descendants(dialog.win) if isinstance(w,ttk.Radiobutton) and w.cget('text')=='この画面').invoke()
            button(dialog.win,'選択して録画を開始').invoke();self.root.update()
            start.assert_called_once_with(replace(options,target_title='This session',target_hwnd=101))
        self.assertNotIn('target_title',self.app.config.capture_preferences['gamebar'])

    def test_default_and_input_shortcuts_can_be_switched_and_saved(self):
        a=self.app;dialog=gui.SettingsDialog(a);self.root.update()
        button(dialog.win,'入力と同じ場所').invoke()
        self.assertTrue(dialog.same_input.get())
        button(dialog.win,'デフォルト').invoke()
        self.assertFalse(dialog.same_input.get());dialog._save()
        self.assertEqual(a.config.output_dir,'');self.assertFalse(a.config.output_same_as_input)
        path=self.add_samples(1)[0];a.tree.selection_set(str(path));a.edit_file_outputs();self.root.update()
        win=next(w for w in self.root.winfo_children() if isinstance(w,gui.tk.Toplevel))
        button(win,'デフォルト').invoke();button(win,'この設定を適用').invoke()
        self.assertEqual(a.job_options[str(path)][1],str(core.default_directory('transcriptions')))

    def test_default_shortcuts_use_the_correct_app_folder_for_each_output(self):
        a=self.app
        for factory,attribute,kind in ((gui.LiveCaptureDialog,'folder','recordings'),
                (gui.GameBarRecordingDialog,'folder','recordings'),(gui.StandardRecordingDialog,'dest','recordings')):
            with self.subTest(factory=factory),patch('screen_capture.windows',return_value=[]):
                dialog=factory(a);self.root.update();getattr(dialog,attribute).set('/custom')
                button(dialog.win,'デフォルト').invoke()
                self.assertEqual(getattr(dialog,attribute).get(),str(core.default_directory(kind)))
                dialog.win.destroy()
        for kind in ('text','audio','csv'):
            dialog=gui.ConcatDialog(a,kind);self.root.update()
            button(dialog.win,'デフォルト').invoke()
            self.assertEqual(dialog.destination_mode.get(),'merged')
            self.assertEqual(dialog._folder(),core.default_directory('merged'))
            dialog.win.destroy()
        dialog=gui.TranscriptConvertDialog(a);self.root.update()
        button(dialog.win,'デフォルト').invoke();self.assertEqual(dialog.destination.get(),'merged');dialog.win.destroy()
        source=self.folder/'sample.txt';source.write_text('[00:00:01] SPEAKER_00: 発言\n')
        dialog=a.open_speaker_editor(source);self.root.update()
        dialog.output.set(str(self.folder/'custom.txt'));button(dialog.win,'デフォルト').invoke()
        self.assertEqual(Path(dialog.output.get()),core.default_directory('speaker_edits')/'custom.txt')
        dialog.win.destroy()

    def test_export_opt_in_controls_token_and_import_updates_current_gui_preserving_queue(self):
        a=self.app;paths=self.add_samples(1)
        a.hf_token='test-secret'
        for include in (False,True):
            dialog=SettingsExportDialog(a);self.root.update()
            target=self.folder/'portable.json';dialog.path.set(str(target));dialog.include_token.set(include)
            with patch.object(gui.messagebox,'showinfo'):dialog.save()
            values=json.loads(target.read_text())['settings']
            self.assertEqual('hf_token_json' in values,include)
        config=replace(a.config,profile='faster-res',model_faster='small',theme_mode='dark',auto_open_speaker=True,
                       shortcut_keys={'live':'ctrl+shift+f8','gamebar':'','stop':''})
        write_export(target,export_settings(config))
        with patch.object(gui.messagebox,'showinfo'):
            self.assertIsNotNone(a.import_preferences(target))
        self.assertEqual(a.config.profile,'faster-res');self.assertEqual(a.config.model_faster,'small')
        self.assertEqual(a.ui.theme_var.get(),'dark');self.assertTrue(a.ui.auto_speaker_var.get())
        self.assertEqual(a.tree.get_children(),(str(paths[0]),))
        self.assertEqual(a.config.shortcut_keys['live'],'ctrl+shift+f8')

    def test_import_write_failure_keeps_live_preferences_and_theme(self):
        a=self.app;before=a.config
        target=self.folder/'settings.json';write_export(target,export_settings(replace(a.config,theme_mode='dark')))
        with patch('core.save_config',side_effect=OSError('Cannot write')),patch.object(gui,'show_error') as error:
            self.assertIsNone(a.import_preferences(target))
        self.assertIs(a.config,before);self.assertEqual(a.ui.theme_var.get(),before.theme_mode)
        error.assert_called_once()


if __name__=='__main__':unittest.main()
