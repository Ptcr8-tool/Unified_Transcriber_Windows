"""Actual Tk setup choices, asynchronous advisory, and common output setting."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unified_gui as gui
from cuda_probe import CudaAvailability
import test_v25_studio_ui as fixtures


LIKELY = CudaAvailability('likely', 'GPU候補', '環境導入後の診断が必要', 'cuda')
UNKNOWN = CudaAvailability('unknown', '未確認', '手動選択できます', 'cpu')


class SetupPreferencesTests(fixtures.StudioGUIFixture):
    def wait_for_probe(self, dialog):
        deadline = time.monotonic()+2
        while dialog.cuda_status is None and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIsNotNone(dialog.cuda_status)
        self.root.update()

    def test_before_probe_only_profile_two_is_selected_and_ui_is_responsive(self):
        ready=threading.Event()
        self.addCleanup(ready.set)
        def probe():
            ready.wait(2)
            return UNKNOWN
        with patch.object(gui, 'probe_cuda', side_effect=probe):
            dialog=gui.BulkSetupDialog(self.app)
            self.root.update()
            self.assertEqual(dialog._plan().profiles, (('faster-res','cpu'),))
            self.assertEqual(dialog._plan().models, (('faster','large-v3-turbo'),))
            self.assertFalse(dialog.with_pyannote.get())
            self.assertEqual(dialog.setup_tabs.tab(dialog.models_tab,'state'),'disabled')
            heartbeat=[]
            self.root.after(0,lambda:heartbeat.append(True))
            self.root.update()
            self.assertEqual(heartbeat,[True])
            dialog.profile_devices['faster-res'].set('cuda')
            dialog.hardware_touched.add('faster-res')
            ready.set()
            self.wait_for_probe(dialog)
            self.assertEqual(dialog._plan().profiles,(('faster-res','cuda'),))
            self.assertEqual(dialog.setup_tabs.tab(dialog.models_tab,'state'),'normal')
            dialog.win.destroy()

    def test_likely_gpu_recommends_cuda_without_changing_model_selection(self):
        with patch.object(gui,'probe_cuda',return_value=LIKELY):
            dialog=gui.BulkSetupDialog(self.app)
            self.wait_for_probe(dialog)
            self.assertIn('GPU候補', dialog.cuda_frame.cuda_status_variable.get())
            variable_name = str(dialog.cuda_frame.cuda_status_variable)
            self.root.update()
            self.assertIn('GPU候補', dialog.win.getvar(variable_name))
            self.assertEqual(dialog._plan().profiles,(('faster-res','cuda'),))
            self.assertEqual(dialog._plan().models,(('faster','large-v3-turbo'),))
            dialog.model_flags[('faster','small')].set(True)
            dialog.model_flags[('faster','large-v3-turbo')].set(False)
            dialog._cuda_ready(UNKNOWN)
            self.assertEqual(dialog._plan().models,(('faster','small'),))
            dialog.win.destroy()

    def test_model_tab_has_no_nested_scrollbar_and_all_options_fit_the_form(self):
        import tkinter as tk
        from wheel_support import wheel_canvas_for
        with patch.object(gui, 'probe_cuda', return_value=UNKNOWN):
            dialog = gui.BulkSetupDialog(self.app)
            self.wait_for_probe(dialog)
            dialog.setup_tabs.select(dialog.models_tab)
            self.root.update()
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            children = list(descendants(dialog.models_tab))
            self.assertFalse(any(isinstance(w, (tk.Canvas, tk.Scrollbar, gui.ttk.Scrollbar)) for w in children))
            self.assertGreaterEqual(dialog.models_tab.winfo_height(), dialog.models_tab.winfo_reqheight() - 2)
            canvas = wheel_canvas_for(dialog.models_tab)
            self.assertIsNotNone(canvas)
            self.assertLess(canvas.yview()[1], 1)
            canvas.yview_moveto(1)
            self.root.update()
            self.assertAlmostEqual(canvas.yview()[1], 1, places=3)
            self.assertTrue(dialog.model_flags[('faster','large-v3-turbo')].get())
            dialog.win.destroy()

    def test_explicit_preset_keeps_user_hardware_when_advisory_arrives(self):
        with patch.object(gui,'probe_cuda',return_value=LIKELY):
            dialog=gui.BulkSetupDialog(self.app)
            dialog._load_preset('all-cpu')
            self.wait_for_probe(dialog)
            self.assertEqual({hardware for _,hardware in dialog._plan().profiles},{'cpu'})
            dialog.win.destroy()

    def test_bulk_saves_common_output_before_starting(self):
        destination=self.folder/'output documents'
        with patch.object(gui,'probe_cuda',return_value=UNKNOWN):
            dialog=gui.BulkSetupDialog(self.app)
            self.wait_for_probe(dialog)
            dialog.output_root.set(str(destination))
            self.app.diagnostics.refresh_location=MagicMock()
            with patch.object(self.app,'start_bulk_setup') as start:
                dialog._start()
            self.assertEqual(self.app.config.output_root,str(destination))
            self.app.diagnostics.refresh_location.assert_called_once()
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0].profiles,(('faster-res','cpu'),))

    def test_settings_root_persists_without_replacing_custom_transcript_path(self):
        destination=self.folder/'my outputs'
        original=self.app.config.output_dir
        dialog=gui.SettingsDialog(self.app)
        dialog.output_root.set(str(destination))
        self.app.diagnostics.refresh_location=MagicMock()
        dialog._save()
        self.assertEqual(self.app.config.output_root,str(destination))
        self.assertEqual(self.app.config.output_dir,original)
        self.app.diagnostics.refresh_location.assert_called_once()

    def test_default_documents_remains_portable_preference(self):
        with patch.object(gui,'default_output_root',return_value=self.folder/'Documents'/'UnifiedTranscriber'):
            gui.save_setup_output_root(self.app,str(self.folder/'Documents'/'UnifiedTranscriber'))
        self.assertEqual(self.app.config.output_root,'')

    def test_watch_failure_log_uses_common_log_folder_and_redacts_token(self):
        folder=self.folder/'watch'
        folder.mkdir()
        (folder/'failed').mkdir()
        (folder/'processed').mkdir()
        source=folder/'sample.wav'
        source.write_bytes(b'audio')
        logs=self.folder/'documents'/'Logs'
        logs.mkdir(parents=True)
        cycles=[]
        def pause(_seconds):
            cycles.append(True)
            if len(cycles)>5 or not source.exists():
                self.app.stop_event.set()
        with patch.object(self.app,'_resolve_device',return_value='cpu'), \
             patch.object(self.app,'_run_one',return_value=('失敗','private-token is hidden')), \
             patch.object(gui,'log_directory',return_value=logs), \
             patch.object(self.app.stop_event,'wait',side_effect=pause):
            self.app._watch_worker(folder,self.app.config,Path(sys.executable),'private-token')
        self.assertTrue((folder/'failed'/'sample.wav').is_file())
        self.assertFalse(list((folder/'failed').glob('*.log')))
        files=list(logs.glob('watch_*.error.log'))
        self.assertEqual(len(files),1)
        self.assertNotIn('private-token',files[0].read_text(encoding='utf-8'))
        self.assertIn('HF_TOKEN',files[0].read_text(encoding='utf-8'))


class OutputPreferenceTransactionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        from core import Config
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name)
        old=self.folder/'previous'
        self.previous=Config(output_root=str(old))
        self.logs=SimpleNamespace(folder=old/'Logs',session_path=old/'Logs/session.log',
            error_path=old/'Logs/errors.log',refresh_location=MagicMock())
        self.app=SimpleNamespace(config=self.previous,diagnostics=self.logs)

    def test_denied_temporary_write_never_saves_or_switches_logs(self):
        from dataclasses import replace
        selected=replace(self.previous,output_root=str(self.folder/'denied'))
        with patch.object(gui.tempfile,'NamedTemporaryFile',side_effect=PermissionError('write denied')), \
             patch.object(gui,'save_config') as persist:
            with self.assertRaises(PermissionError):
                gui.save_preferences_with_output(self.app,selected)
        persist.assert_not_called()
        self.logs.refresh_location.assert_not_called()
        self.assertIs(self.app.config,self.previous)
        self.assertEqual(self.logs.folder,self.folder/'previous'/'Logs')

    def test_directory_failure_never_changes_preferences(self):
        from dataclasses import replace
        occupied=self.folder/'file-instead-of-folder'
        occupied.write_text('keep',encoding='utf-8')
        selected=replace(self.previous,output_root=str(occupied))
        with patch.object(gui,'save_config') as persist:
            with self.assertRaises((ValueError,OSError)):
                gui.save_preferences_with_output(self.app,selected)
        persist.assert_not_called()
        self.assertEqual(occupied.read_text(encoding='utf-8'),'keep')
        self.assertIs(self.app.config,self.previous)

    def test_log_race_restores_persisted_preferences_and_previous_log_paths(self):
        from dataclasses import replace
        import core
        settings=self.folder/'settings.json'
        core.save_config(self.previous,settings)
        original=settings.read_bytes()
        selected=replace(self.previous,output_root=str(self.folder/'selected'))
        def failed_refresh():
            self.logs.folder=self.folder/'selected'/'Logs'
            raise PermissionError('folder changed after preflight')
        self.logs.refresh_location.side_effect=failed_refresh
        with self.assertRaisesRegex(OSError,'以前の設定'):
            gui.save_preferences_with_output(self.app,selected,
                persist=lambda cfg:core.save_config(cfg,settings))
        self.assertEqual(settings.read_bytes(),original)
        self.assertIs(self.app.config,self.previous)
        self.assertEqual(self.logs.folder,self.folder/'previous'/'Logs')
        self.assertFalse(list((self.folder/'selected').rglob('.transcriber-write-check-*')))

    def test_import_denied_destination_reports_error_without_saving_config(self):
        from dataclasses import replace
        from types import SimpleNamespace
        import settings_ui
        selected=replace(self.previous,output_root=str(self.folder/'denied'))
        source=self.folder/'import.json'
        source.write_text('{}',encoding='utf-8')
        self.app.busy=False
        self.app.root=None
        self.app.ui=SimpleNamespace(close_drawers=MagicMock())
        with patch.object(settings_ui,'import_settings',return_value=(selected,[])), \
             patch.object(gui,'preflight_output_root',side_effect=PermissionError('write denied')), \
             patch.object(settings_ui.core,'save_config') as persist, \
             patch.object(gui,'show_error') as error:
            result=settings_ui.load_settings_file(self.app,source)
        self.assertIsNone(result)
        self.assertIs(self.app.config,self.previous)
        persist.assert_not_called()
        self.logs.refresh_location.assert_not_called()
        error.assert_called_once()
