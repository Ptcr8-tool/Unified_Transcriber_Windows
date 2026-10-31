"""Old ready environments must offer repair before starting media operations."""
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unified_gui as gui
import test_v25_studio_ui as fixtures
from live_config import LiveOptions
from gamebar_recording import GameBarOptions


class MediaReadinessGUI(fixtures.StudioGUIFixture):
    def interpreter(self):
        path=self.folder/'sample-python.exe'
        path.write_bytes(b'MZ sample interpreter')
        return path

    def test_old_capture_environment_reopens_setup_even_when_settings_are_skipped(self):
        self.app.config=replace(self.app.config,capture_setup_complete=True,
                                skip_live_settings=True,skip_gamebar_settings=True)
        with patch.object(self.app,'live_capture_python',return_value=self.interpreter()), \
             patch.object(self.app,'media_tools_ready',return_value=False), \
             patch.object(self.app,'open_capture_setup') as setup, \
             patch.object(self.app.ui,'_dialog') as settings:
            self.app.open_capture('live')
            self.app.open_capture('gamebar')
        self.assertEqual([item.args for item in setup.call_args_list],[('live',),('gamebar',)])
        settings.assert_not_called()

    def test_ready_ai_environment_requests_media_repair_before_starting_asr(self):
        cfg=replace(self.app.config,profile='faster-res')
        with patch.object(gui,'env_python',return_value=self.interpreter()), \
             patch.object(self.app,'media_tools_ready',return_value=False), \
             patch.object(gui,'show_error') as error:
            self.assertIsNone(self.app._require_environment(cfg))
        self.assertIn('FFmpeg / FFprobe',error.call_args.args[1])
        self.assertIn('モデルは保持',error.call_args.args[1])

    def test_live_does_not_create_recordings_or_start_workers_before_repair(self):
        destination=self.folder/'new-recordings'
        with patch.object(self.app,'live_capture_python',return_value=self.interpreter()), \
             patch.object(self.app,'media_tools_ready',return_value=False), \
             patch.object(gui,'show_error') as error,patch.object(gui.threading,'Thread') as worker:
            self.app.start_live(LiveOptions(mode='record',folder=destination))
        error.assert_called_once()
        worker.assert_not_called()
        self.assertFalse(destination.exists())

    def test_gamebar_does_not_start_capture_before_media_repair(self):
        destination=self.folder/'new-gamebar'
        options=GameBarOptions(source='mic',captures=self.folder,destination=destination,
                               target_hwnd=42,auto_transcribe=False)
        with patch.object(self.app,'live_capture_python',return_value=self.interpreter()), \
             patch.object(self.app,'media_tools_ready',return_value=False), \
             patch.object(gui,'show_error') as error,patch.object(gui.threading,'Thread') as worker:
            self.assertFalse(self.app.start_gamebar_recording(options))
        error.assert_called_once()
        worker.assert_not_called()
        self.assertFalse(destination.exists())
