"""Startup, launch propagation and notification-area lifecycle contracts."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_launcher
import launch_runtime
import unified_gui
import windows_background as background
from core import Config
import test_v25_studio_ui as fixtures


class BackgroundTests(unittest.TestCase):
    def test_startup_command_quotes_install_path_and_does_not_invoke_shell(self):
        command = background.startup_command(Path('C:/Program Files/Sample App'))
        self.assertIn('UnifiedTranscriber.exe', command)
        self.assertTrue(command.startswith('"'))
        self.assertTrue(command.endswith(' --background'))
        self.assertNotIn('cmd.exe', command.lower())

    def test_launcher_passes_background_only_to_fixed_isolated_runtime(self):
        with patch.object(app_launcher, 'application_root', return_value=Path('/sample')), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(app_launcher, 'reset_dll_search'), \
             patch.object(app_launcher.subprocess, 'Popen') as popen:
            self.assertEqual(app_launcher.main(['--background']), 0)
        args = popen.call_args.args[0]
        self.assertEqual(args[-1], '--background')
        self.assertEqual(args[1:4], ['-I', '-X', 'utf8'])
        self.assertFalse(popen.call_args.kwargs['shell'])

    def test_isolated_runtime_forwards_background_to_gui_and_restores_argv(self):
        original = sys.argv
        seen = []
        with patch.object(sys, 'path', sys.path.copy()), \
             patch.object(launch_runtime.runpy, 'run_path', side_effect=lambda *a, **k:seen.append(sys.argv[:])):
            launch_runtime.main(['--background'])
        self.assertEqual(seen[0][1:], ['--background'])
        self.assertIs(sys.argv, original)

    def test_old_configuration_defaults_to_normal_close_and_minimize(self):
        cfg = Config()
        cfg.validate()
        self.assertFalse(cfg.tray_close)
        self.assertFalse(cfg.tray_minimize)

    def test_close_to_tray_does_not_stop_active_processing(self):
        app = SimpleNamespace(config=replace(Config(), tray_close=True), hide_to_tray=MagicMock(return_value=True),
                              request_exit=MagicMock())
        unified_gui.UnifiedApp.close(app)
        app.hide_to_tray.assert_called_once()
        app.request_exit.assert_not_called()

    def test_failed_tray_registration_keeps_window_open(self):
        app = SimpleNamespace(config=replace(Config(), tray_close=True), hide_to_tray=MagicMock(return_value=False),
                              request_exit=MagicMock())
        unified_gui.UnifiedApp.close(app)
        app.request_exit.assert_not_called()

    def test_tray_exit_explicitly_runs_normal_confirmation_path(self):
        app = SimpleNamespace(restore_from_tray=MagicMock(), request_exit=MagicMock(), _handle_shortcut=MagicMock())
        unified_gui.UnifiedApp._tray_action(app, 'exit')
        app.restore_from_tray.assert_called_once()
        app.request_exit.assert_called_once()
        unified_gui.UnifiedApp._tray_action(app, 'live')
        app._handle_shortcut.assert_called_once_with('live')

    def test_repeat_launch_also_raises_a_window_that_is_already_visible(self):
        root = MagicMock()
        root.state.return_value = 'normal'
        app = SimpleNamespace(in_tray=False, root=root, config=Config(onboarding_seen=True))
        unified_gui.UnifiedApp.restore_from_tray(app)
        root.deiconify.assert_not_called()
        root.lift.assert_called_once()
        root.focus_force.assert_called_once()

    def test_windows_tray_unavailable_fails_without_starting_a_thread(self):
        tray = background.WindowsTray(MagicMock(), Path('/sample.exe'))
        with patch.object(background.sys, 'platform', 'linux'):
            self.assertFalse(tray.start())
        self.assertIsNone(tray._thread)
        self.assertTrue(tray.error)

    def test_registry_only_removes_its_own_installation(self):
        values = {}
        class Key:
            def __enter__(self):return self
            def __exit__(self, *_):pass
        def query(_key, name):
            if name not in values:raise FileNotFoundError(name)
            return values[name]
        def delete(_key, name):values.pop(name)
        registry = SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=2, KEY_WRITE=4, REG_SZ=1,
            OpenKey=lambda *_:Key(), CreateKeyEx=lambda *_:Key(), QueryValueEx=query,
            SetValueEx=lambda _key, name, _reserved, kind, value:values.update({name:(value, kind)}),
            DeleteValue=delete)
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary)/'installed'
            installed.mkdir(); (installed/'UnifiedTranscriber.exe').touch()
            other = Path(temporary)/'other'
            with patch.dict(sys.modules, {'winreg':registry}), patch.object(background.sys, 'platform', 'win32'):
                background.set_startup(installed, True)
                self.assertTrue(background.startup_enabled(installed))
                self.assertFalse(background.startup_enabled(other))
                background.remove_startup_if_matches(other)
                self.assertTrue(background.startup_enabled(installed))
                with self.assertRaises(OSError):background.set_startup(other, False)
                background.remove_startup_if_matches(installed)
                self.assertFalse(background.startup_enabled(installed))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows notification area')
    def test_real_windows_tray_can_start_and_close(self):
        tray = background.WindowsTray(lambda _action:None, Path(sys.executable))
        try:
            ready = tray.start()
            if not ready and '通知領域にアイコンを登録' in tray.error:
                self.skipTest('Windows Explorer notification area unavailable on this runner')
            self.assertTrue(ready, tray.error)
            self.assertTrue(tray._thread.is_alive())
        finally:
            tray.close()
        self.assertFalse(tray._thread.is_alive())


class BackgroundGuiTests(fixtures.StudioGUIFixture):
    def setUp(self):
        super().setUp()
        self.app.config = replace(self.app.config, onboarding_seen=True)
        self.app.tray.start = MagicMock(return_value=True)

    def tearDown(self):
        self.app.config = replace(self.app.config, tray_close=False, tray_minimize=False)
        super().tearDown()

    def test_tray_hide_restore_and_close_preserve_active_work(self):
        self.app.config = replace(self.app.config, tray_close=True)
        self.app.close()
        self.root.update()
        self.assertTrue(self.app.in_tray)
        self.assertEqual(self.root.state(), 'withdrawn')
        self.app.restore_from_tray()
        self.root.update()
        self.assertFalse(self.app.in_tray)
        self.assertEqual(self.root.state(), 'normal')

    def test_shortcut_can_start_previous_live_settings_while_hidden(self):
        capture = self.folder/'python.exe'; capture.touch()
        self.app.config = replace(self.app.config, capture_setup_complete=True, skip_live_settings=True)
        self.app.hide_to_tray()
        options = object()
        with patch.object(self.app, 'live_capture_python', return_value=capture), \
             patch.object(unified_gui, 'saved_options', return_value=options), \
             patch.object(self.app, 'start_live') as start:
            self.app._handle_shortcut('live')
        start.assert_called_once_with(options)
        self.assertTrue(self.app.in_tray)

    def test_shortcut_restores_window_when_first_setup_is_needed(self):
        self.app.config = replace(self.app.config, capture_setup_complete=False)
        self.app.hide_to_tray()
        with patch.object(self.app, 'open_capture_setup') as setup:
            self.app._handle_shortcut('live')
        setup.assert_called_once_with('live')
        self.assertFalse(self.app.in_tray)
        self.assertEqual(self.root.state(), 'normal')

    def test_gamebar_shortcut_always_restores_window_for_target_selection(self):
        capture = self.folder/'python.exe'; capture.touch()
        self.app.config = replace(self.app.config, capture_setup_complete=True, skip_gamebar_settings=True)
        self.app.hide_to_tray()
        with patch.object(self.app, 'live_capture_python', return_value=capture), \
             patch.object(unified_gui, 'saved_options', return_value=object()), \
             patch('window_picker.WindowPickerDialog') as picker:
            self.app._handle_shortcut('gamebar')
        picker.assert_called_once()
        self.assertFalse(self.app.in_tray)
        self.assertEqual(self.root.state(), 'normal')


if __name__ == '__main__':
    unittest.main()
