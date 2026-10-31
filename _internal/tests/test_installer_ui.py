"""Installation preferences, maintenance handoff and real Tk wizard behavior."""
from contextlib import ExitStack, redirect_stdout
import ctypes
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch, call
import weakref
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer_ui as gui
from installation import APP_ID, get_verified_maintenance_path, install_package, InstallationError


def sample_package(folder, version):
    package = folder / ('sample-' + version + '.zip')
    with zipfile.ZipFile(package, 'w') as archive:
        for name in ('UnifiedTranscriber.exe', 'Runtime/python.exe', 'Runtime/pythonw.exe', '_internal/core.py'):
            archive.writestr(name, b'sample code')
        archive.writestr('VERSION', version + '\n')
    return package


class InstallerPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.target = self.folder / 'Sample App'
        self.target.mkdir()
        self.environment = patch.dict(os.environ, {'APPDATA': str(self.folder / 'previous-user')})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_existing_unknown_or_damaged_settings_bytes_are_not_rewritten(self):
        path = self.target / 'Data/settings.json'
        path.parent.mkdir()
        for payload in (b'{"newer_custom_field": {"keep": true}, "theme_mode": "dark"}\r\n',
                        b'partially damaged settings\x00'):
            path.write_bytes(payload)
            gui.seed_preferences(self.target, minimal=True, hardware='cuda', recording_ready=True)
            self.assertEqual(path.read_bytes(), payload)

    def test_legacy_settings_are_copied_without_losing_unknown_fields_or_formatting(self):
        legacy = Path(os.environ['APPDATA']) / 'UnifiedTranscriberV12/settings.json'
        legacy.parent.mkdir(parents=True)
        payload = b'\xef\xbb\xbf{ "future_field": [1,2], "theme_mode":"dark" }\r\n'
        legacy.write_bytes(payload)
        gui.seed_preferences(self.target, minimal=True, hardware='cuda', recording_ready=True)
        self.assertEqual((self.target / 'Data/settings.json').read_bytes(), payload)
        self.assertEqual(legacy.read_bytes(), payload)

    def test_new_minimal_preferences_are_seeded_only_after_a_successful_setup(self):
        gui.seed_preferences(self.target, minimal=True, hardware='cpu', recording_ready=True)
        cfg = json.loads((self.target / 'Data/settings.json').read_text())
        self.assertEqual(cfg['profile'], 'faster-res')
        self.assertEqual(cfg['model_faster'], 'large-v3-turbo')
        self.assertEqual(cfg['device'], 'cpu')
        self.assertTrue(cfg['capture_setup_complete'])
        self.assertTrue(cfg['onboarding_seen'])
        self.assertNotIn('hf_token_json', cfg)

    def test_successful_setup_marks_welcome_complete_and_preserves_other_fields(self):
        gui.seed_preferences(self.target, minimal=False, hardware='auto', recording_ready=False)
        path = self.target / 'Data/settings.json'
        values = json.loads(path.read_text(encoding='utf-8'))
        values['future_option'] = {'preserve': True}
        path.write_text(json.dumps(values), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        gui.update_setup_preferences(self.target, minimal=True, hardware='cpu',
            recording_ready=False, model='large-v3-turbo', expected_sha256=digest,
            initial_preferences=False)
        updated = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(updated['onboarding_seen'])
        self.assertEqual(updated['future_option'], {'preserve': True})
        self.assertEqual(updated['device'], 'auto')

    def test_existing_other_profile_or_model_does_not_skip_welcome(self):
        for overrides in ({'profile': 'qwen-res'}, {'model_faster': 'small'}):
            with self.subTest(overrides=overrides):
                path = self.target / 'Data/settings.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                data = {'onboarding_seen': False, **overrides}
                path.write_text(json.dumps(data), encoding='utf-8')
                gui.update_setup_preferences(self.target, minimal=True, hardware='cpu',
                    recording_ready=False, model='large-v3-turbo',
                    expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    initial_preferences=False)
                self.assertFalse(json.loads(path.read_text())['onboarding_seen'])

    def test_recording_only_setup_does_not_skip_model_welcome(self):
        gui.seed_preferences(self.target, minimal=False, hardware='cpu', recording_ready=False)
        path = self.target / 'Data/settings.json'
        gui.update_setup_preferences(self.target, minimal=False, hardware='cpu',
            recording_ready=True, model='large-v3-turbo',
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        values = json.loads(path.read_text(encoding='utf-8'))
        self.assertFalse(values['onboarding_seen'])
        self.assertTrue(values['capture_setup_complete'])

    def test_existing_mismatched_hardware_does_not_skip_welcome(self):
        path = self.target / 'Data/settings.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'profile': 'faster-res', 'model_faster': 'large-v3-turbo',
                                    'device': 'cuda', 'onboarding_seen': False}), encoding='utf-8')
        gui.update_setup_preferences(self.target, minimal=True, hardware='cpu',
            recording_ready=False, model='large-v3-turbo',
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            initial_preferences=False)
        settings = json.loads(path.read_text(encoding='utf-8'))
        self.assertFalse(settings['onboarding_seen'])
        self.assertEqual(settings['device'], 'cuda')

    def test_concurrent_settings_change_is_not_overwritten(self):
        gui.seed_preferences(self.target, minimal=False, hardware='cpu', recording_ready=False)
        path = self.target / 'Data/settings.json'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.write_text('{"onboarding_seen": false, "future": "keep"}', encoding='utf-8')
        previous = path.read_bytes()
        gui.update_setup_preferences(self.target, minimal=True, hardware='cpu',
            recording_ready=True, model='large-v3-turbo', expected_sha256=digest)
        self.assertEqual(path.read_bytes(), previous)

    def test_settings_destination_created_concurrently_is_kept(self):
        path = self.target / 'Data/settings.json'
        path.parent.mkdir()
        path.write_bytes(b'concurrent settings')
        gui._write_new_settings(path, b'replacement')
        self.assertEqual(path.read_bytes(), b'concurrent settings')

    def test_report_option_alone_runs_read_only_installer_self_test(self):
        package = sample_package(self.folder, gui.APP_VERSION)
        report = self.folder / 'report.json'
        with patch.object(gui, 'resources', return_value=(package, hashlib.sha256(package.read_bytes()).hexdigest())), redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-report', str(report)]), 0)
        self.assertTrue(json.loads(report.read_text())['ok'])
        self.assertFalse((self.target / 'Data').exists())

    def test_normal_payload_self_test_never_requires_a_display(self):
        package=sample_package(self.folder,gui.APP_VERSION)
        with patch.object(gui,'resources',return_value=(package,hashlib.sha256(package.read_bytes()).hexdigest())), \
             patch.object(gui,'gui_self_test') as gui_test,redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test']),0)
        gui_test.assert_not_called()

    def test_gui_self_test_reports_diagnostic_failure_instead_of_starting_installation(self):
        package=sample_package(self.folder,gui.APP_VERSION)
        report=self.folder/'gui-report.json'
        with patch.object(gui,'resources',return_value=(package,hashlib.sha256(package.read_bytes()).hexdigest())), \
             patch.object(gui,'gui_self_test',side_effect=tk.TclError('sample missing Tcl library')), \
             patch.object(gui,'install_package') as install,redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-gui','--self-test-report',str(report)]),1)
        self.assertFalse(json.loads(report.read_text())['ok'])
        install.assert_not_called()

    def test_gui_diagnostic_rejects_maintenance_and_autostart_flags(self):
        for flags in (['--update'],['--uninstall'],['--self-test-relaunch'],['--wait-pid','123'],['--wait-setup-pid','123']):
            with self.subTest(flags=flags),patch.object(gui,'gui_self_test') as gui_test, \
                 patch.object(gui,'relaunch_outside') as relaunch,redirect_stdout(io.StringIO()):
                self.assertEqual(gui.main(['--self-test-gui',*flags]),1)
                gui_test.assert_not_called()
                relaunch.assert_not_called()

    def test_self_test_report_write_error_returns_failure_without_crashing(self):
        package=sample_package(self.folder,gui.APP_VERSION)
        report=self.folder/'missing-directory/report.json'
        with patch.object(gui,'resources',return_value=(package,hashlib.sha256(package.read_bytes()).hexdigest())),redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-report',str(report)]),1)

    def test_bundled_resource_metadata_requires_an_object(self):
        (self.folder / 'BUILD_INFO.json').write_text('[]')
        with patch.object(sys, '_MEIPASS', str(self.folder), create=True), self.assertRaises(InstallationError):
            gui.resources()

    def test_current_or_invalid_wait_pid_is_not_accepted(self):
        for pid in (0, -1, os.getpid(), True, '123'):
            with self.subTest(pid=pid), self.assertRaises(InstallationError):
                gui.wait_for_process(pid, self.target)
        gui.wait_for_process(None, self.target)

    def test_frozen_self_relaunch_resets_dll_search_and_closes_hash_files(self):
        executable = self.target / 'UnifiedTranscriberSetup.exe'
        executable.write_bytes(b'MZ sample maintenance')
        copied_folder = self.folder / 'outside-copy'
        copied_folder.mkdir()
        order = []
        proxy_os = SimpleNamespace(name='nt', getpid=os.getpid)
        with patch.object(gui, 'os', proxy_os), patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(executable)), \
             patch.object(gui.tempfile, 'mkdtemp', return_value=str(copied_folder)), \
             patch.object(gui, 'maintenance_owner_pid', return_value=12345), \
             patch.object(gui, 'reset_dll_search_path', side_effect=lambda: order.append('reset')), \
             patch.object(gui.subprocess, 'Popen', side_effect=lambda *a, **k: order.append(('launch',a,k))) as launch:
            self.assertTrue(gui.relaunch_outside(self.target, ['--uninstall']))
        self.assertEqual(order[0], 'reset')
        command = launch.call_args.args[0]
        self.assertEqual(command[-2:], ['--wait-setup-pid', '12345'])
        self.assertFalse(launch.call_args.kwargs['shell'])
        self.assertEqual(launch.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        copied = Path(command[0])
        self.assertEqual(copied.read_bytes(), executable.read_bytes())
        copied.unlink()  # succeeds on Windows only if hashing handles were closed

    def test_verified_maintenance_uses_fixed_receipt_path_and_rejects_modified_exe(self):
        maintenance = self.folder / 'maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        target = self.folder / 'Installed App'
        install_package(sample_package(self.folder, gui.APP_VERSION), target, gui.APP_VERSION,
            extra_files={'UnifiedTranscriberSetup.exe': maintenance})
        self.assertEqual(get_verified_maintenance_path(target), target / 'UnifiedTranscriberSetup.exe')
        (target / 'UnifiedTranscriberSetup.exe').write_bytes(b'modified executable')
        with self.assertRaises(InstallationError):
            get_verified_maintenance_path(target)

    def test_windows_process_wait_uses_full_width_handle_and_fixed_app_paths(self):
        handle=0x123456789
        kernel=SimpleNamespace(OpenProcess=MagicMock(return_value=handle),
            QueryFullProcessImageNameW=MagicMock(),WaitForSingleObject=MagicMock(side_effect=[0x102,0,0x102]),CloseHandle=MagicMock())
        image=[self.target/'Runtime/pythonw.exe']
        def query(_handle,_flags,buffer,_size):
            buffer.value=str(image[0])
            return True
        kernel.QueryFullProcessImageNameW.side_effect=query
        proxy_os=SimpleNamespace(name='nt',getpid=os.getpid)
        with patch.object(gui,'os',proxy_os),patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            gui.wait_for_process(123,self.target)
            self.assertEqual(kernel.OpenProcess.restype,ctypes.wintypes.HANDLE)
            self.assertEqual(kernel.WaitForSingleObject.call_args_list,[call(handle,0),call(handle,120000)])
            kernel.CloseHandle.assert_called_once_with(handle)
            image[0]=self.target/'_internal/unrelated.exe'
            with self.assertRaises(InstallationError):
                gui.wait_for_process(123,self.target)
        self.assertEqual(kernel.CloseHandle.call_count,2)

    def test_terminated_process_handle_does_not_require_an_image_query(self):
        kernel=SimpleNamespace(OpenProcess=MagicMock(return_value=1234),
            QueryFullProcessImageNameW=MagicMock(),WaitForSingleObject=MagicMock(return_value=0),
            CloseHandle=MagicMock())
        with patch.object(gui,'os',SimpleNamespace(name='nt',getpid=os.getpid)), \
             patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            gui.wait_for_process(123,self.target)
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(1234)

    def test_exit_during_image_query_is_accepted_only_after_a_signaled_wait(self):
        kernel=SimpleNamespace(OpenProcess=MagicMock(return_value=1234),
            QueryFullProcessImageNameW=MagicMock(return_value=False),
            WaitForSingleObject=MagicMock(side_effect=[0x102,0]),CloseHandle=MagicMock())
        with patch.object(gui,'os',SimpleNamespace(name='nt',getpid=os.getpid)), \
             patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            gui.wait_for_process(123,self.target)
            kernel.WaitForSingleObject.side_effect=[0x102,0x102]
            with self.assertRaises(InstallationError):
                gui.wait_for_process(123,self.target)
            kernel.WaitForSingleObject.side_effect=[0xFFFFFFFF]
            with self.assertRaises(InstallationError):
                gui.wait_for_process(123,self.target)
        self.assertEqual(kernel.CloseHandle.call_count,3)

    def test_onefile_owner_is_verified_parent_but_onedir_uses_its_own_pid(self):
        current=self.target/'UnifiedTranscriberSetup.exe'
        handle=0x123456789
        kernel=SimpleNamespace(OpenProcess=MagicMock(return_value=handle),
            QueryFullProcessImageNameW=MagicMock(),CloseHandle=MagicMock())
        image=[current]
        query_succeeds=[True]
        def query(_handle,_flags,buffer,_size):
            buffer.value=str(image[0])
            return query_succeeds[0]
        kernel.QueryFullProcessImageNameW.side_effect=query
        proxy_os=SimpleNamespace(getpid=lambda:100,getppid=lambda:200)
        with patch.object(gui,'os',proxy_os),patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            self.assertEqual(gui.maintenance_owner_pid(current),200)
            image[0]=self.folder/'other-parent.exe'
            self.assertEqual(gui.maintenance_owner_pid(current),100)
            query_succeeds[0]=False
            with self.assertRaises(InstallationError):
                gui.maintenance_owner_pid(current)
            kernel.OpenProcess.return_value=None
            with self.assertRaises(InstallationError):
                gui.maintenance_owner_pid(current)
        self.assertEqual(kernel.CloseHandle.call_count,3)
        self.assertEqual(kernel.OpenProcess.restype,ctypes.wintypes.HANDLE)

    def test_relocated_frozen_self_test_waits_for_previous_bootloader_before_validation(self):
        maintenance=self.folder/'sample-maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        target=self.folder/'Installed App'
        package=sample_package(self.folder,gui.APP_VERSION)
        digest=hashlib.sha256(package.read_bytes()).hexdigest()
        install_package(package,target,gui.APP_VERSION,extra_files={'UnifiedTranscriberSetup.exe':maintenance})
        copied=self.folder/'temporary-copy.exe'
        copied.write_bytes(maintenance.read_bytes())
        report=self.folder/'relaunch-report.json'
        order=[]
        with patch.object(sys,'executable',str(copied)), \
             patch.object(gui,'wait_for_process',side_effect=lambda *a:order.append(('wait',a))), \
             patch.object(gui,'resources',side_effect=lambda:(order.append(('validate',None)) or (package,digest))), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test','--target',str(target),'--wait-setup-pid','200',
                '--self-test-report',str(report)]),0)
        result=json.loads(report.read_text())
        self.assertEqual(order[0],('wait',(200,target)))
        self.assertTrue(result['copied_outside'])
        self.assertTrue(result['installed_setup_copy_verified'])
        self.assertEqual(result['waited_setup_pid'],200)

    def test_relaunch_self_test_only_runs_the_verified_installed_copy_and_never_removes_it(self):
        maintenance=self.folder/'sample-maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        target=self.folder/'Installed App'
        package=sample_package(self.folder,gui.APP_VERSION)
        install_package(package,target,gui.APP_VERSION,extra_files={'UnifiedTranscriberSetup.exe':maintenance})
        report=self.folder/'relaunch-report.json'
        proxy_os=SimpleNamespace(name='nt')
        with patch.object(gui,'os',proxy_os),patch.object(sys,'frozen',True,create=True), \
             patch.object(sys,'executable',str(target/'UnifiedTranscriberSetup.exe')), \
             patch.object(gui,'relaunch_outside',return_value=True) as relaunch, \
             patch.object(gui,'uninstall_installation') as uninstall,redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-relaunch','--target',str(target),'--self-test-report',str(report)]),0)
        relaunch.assert_called_once_with(target,['--self-test','--self-test-report',str(report)])
        uninstall.assert_not_called()
        self.assertEqual((target/'UnifiedTranscriberSetup.exe').read_bytes(),maintenance.read_bytes())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 8.3 path identity')
    def test_relaunch_self_test_accepts_the_same_installation_through_a_short_path(self):
        from ctypes import wintypes
        maintenance = self.folder/'sample-maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        target = self.folder/'Installed App'
        install_package(sample_package(self.folder, gui.APP_VERSION), target, gui.APP_VERSION,
                        extra_files={'UnifiedTranscriberSetup.exe': maintenance})
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        kernel.GetShortPathNameW.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetShortPathNameW(str(target), buffer, len(buffer))
        self.assertGreater(length, 0)
        self.assertLess(length, len(buffer))
        alias = Path(buffer.value)
        if alias == target:
            self.skipTest('This volume does not provide an alternate 8.3 name')
        report = self.folder/'relaunch-short-path.json'
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(target/'UnifiedTranscriberSetup.exe')), \
             patch.object(gui, 'relaunch_outside', return_value=True) as relaunch, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-relaunch', '--target', str(alias),
                                       '--self-test-report', str(report)]), 0)
        relaunch.assert_called_once_with(target, ['--self-test', '--self-test-report', str(report)])

    def test_relaunch_self_test_refuses_a_report_inside_installed_files(self):
        maintenance=self.folder/'sample-maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        target=self.folder/'Installed App'
        install_package(sample_package(self.folder,gui.APP_VERSION),target,gui.APP_VERSION,
            extra_files={'UnifiedTranscriberSetup.exe':maintenance})
        report=target/'should-not-exist.json'
        proxy_os=SimpleNamespace(name='nt')
        with patch.object(gui,'os',proxy_os),patch.object(sys,'frozen',True,create=True), \
             patch.object(sys,'executable',str(target/'UnifiedTranscriberSetup.exe')), \
             patch.object(gui,'relaunch_outside') as relaunch,redirect_stdout(io.StringIO()):
            self.assertEqual(gui.main(['--self-test-relaunch','--target',str(target),'--self-test-report',str(report)]),1)
        self.assertFalse(report.exists())
        relaunch.assert_not_called()


@unittest.skipUnless(os.environ.get('DISPLAY') or sys.platform == 'win32', 'GUI checks require a display')
class InstallerWindowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.target = self.folder / 'Sample App'
        self.package = sample_package(self.folder, gui.APP_VERSION)
        self.digest = hashlib.sha256(self.package.read_bytes()).hexdigest()
        self.stack.enter_context(patch.dict(os.environ, {'APPDATA': str(self.folder / 'previous-user')}))
        self.stack.enter_context(patch('output_locations.default_output_root', return_value=self.folder/'Documents/UnifiedTranscriber'))
        self.stack.enter_context(patch.object(gui, 'default_output_root', return_value=self.folder/'Documents/UnifiedTranscriber'))
        self.stack.enter_context(patch.object(gui, 'probe_cuda', return_value=gui.CudaAvailability(
            'not-detected', 'Sample CPU', 'Sample advisory')))
        self.errors = []
        self.stack.enter_context(patch.object(gui.messagebox, 'showerror', side_effect=lambda *a, **k:self.errors.append(a)))
        self.stack.enter_context(patch.object(gui.messagebox, 'askyesno', return_value=True))
        self.root = tk.Tk()
        self.callback_errors = []
        errors = self.callback_errors
        self.root.report_callback_exception = lambda *a:errors.append(a)
        args = SimpleNamespace(target=self.target, update=False, uninstall=False, wait_pid=None)
        self.window = gui.InstallerWindow(self.root,args,(self.package,self.digest))
        self.window.register.set(False)
        self.window.shortcut.set(False)
        self.root.update()
        self.window.poll()

    def tearDown(self):
        try:
            self._close_gui()
        finally:
            self.stack.close()
            self.__dict__.pop('window', None)
            self.__dict__.pop('root', None)
            gc.collect()
        self.assertEqual(self.callback_errors, [])

    def _close_gui(self):
        if not hasattr(self, 'window'):
            return
        worker = getattr(self.window, 'worker', None)
        if worker is not None:
            self.window.cancel.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        if not self.window.closed:
            self.window.busy = False
            self.window.close()
        self.root.__dict__.pop('report_callback_exception', None)

    def finish_worker(self):
        self.window.worker.join(timeout=5)
        self.assertFalse(self.window.worker.is_alive())
        self.window.poll()
        if not self.window.closed:
            self.root.update()

    def test_footer_remains_accessible_and_form_scrolls_at_640_pixels(self):
        self.root.geometry('850x640')
        self.root.update()
        for button in (self.window.action,self.window.cancel_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
        self.assertLess(self.window.canvas.yview()[1],1)
        self.window.canvas.yview_moveto(1)
        self.root.update()
        self.assertGreater(self.window.canvas.yview()[0],0)

    def test_closed_wizard_releases_tk_objects_before_worker_gc(self):
        owner = threading.get_ident()
        finalized = []
        resources = {'root': self.root, 'window': self.window, 'variable': self.window.mode}
        references = {name: weakref.ref(value, lambda _ref, name=name:
            finalized.append((name, threading.get_ident()))) for name, value in resources.items()}
        del resources
        self.tearDown()
        self.assertTrue(all(reference() is None for reference in references.values()),
                        [name for name, reference in references.items() if reference() is not None])
        self.assertEqual({thread for _, thread in finalized}, {owner})
        errors = []
        with patch.object(sys, 'unraisablehook', lambda error: errors.append(str(error.exc_value))):
            worker = threading.Thread(target=gc.collect, name='installer-gc-lifecycle-probe')
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), 'Destroyed wizard reached worker-thread GC')
        self.assertEqual(errors, [])

    def test_gui_diagnostic_constructs_actual_wizard_and_cleans_its_temporary_target(self):
        inspected=[]
        real_inspect=gui.inspect_installation
        def inspect(target,version):
            inspected.append(target)
            return real_inspect(target,version)
        with patch.object(gui,'inspect_installation',side_effect=inspect), \
             patch.object(gui,'install_package') as install,patch.object(gui,'uninstall_installation') as uninstall, \
             patch.object(gui,'setup_selected') as setup:
            result=gui.gui_self_test()
        self.assertTrue(result['gui_ok'])
        self.assertTrue(result['wizard_initialized'])
        self.assertTrue(result['controls_ok'])
        self.assertTrue(result['icons_ok'])
        self.assertEqual(result['python_version'],'.'.join(str(v) for v in sys.version_info[:3]))
        self.assertEqual(result['tcl_version'],str(self.root.tk.call('info','patchlevel')))
        self.assertEqual(result['tk_version'],str(self.root.tk.call('set','tk_patchLevel')))
        self.assertEqual(len(inspected),1)
        self.assertNotEqual(inspected[0],self.target)
        self.assertFalse(inspected[0].parent.exists())
        install.assert_not_called()
        uninstall.assert_not_called()
        setup.assert_not_called()

    def test_error_restores_deeply_nested_options_and_allows_retry(self):
        with patch.object(gui,'install_package',side_effect=InstallationError('sample failure')):
            self.window.begin()
            self.finish_worker()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.path_entry.instate(('disabled',)))
        self.assertFalse(self.window.browse.instate(('disabled',)))
        controls = []
        def descendants(parent):
            for child in parent.winfo_children():
                yield child
                yield from descendants(child)
        for control in descendants(self.window.options):
            if isinstance(control,(ttk.Checkbutton,ttk.Radiobutton)):
                controls.append(control)
                self.assertFalse(control.instate(('disabled',)))
        self.assertGreaterEqual(len(controls),6)
        self.assertFalse(self.window.action.instate(('disabled',)))
        self.assertEqual(len(self.errors),1)

    def test_failed_optional_environment_keeps_app_and_seeds_safe_defaults(self):
        self.window.minimal.set(True)
        with patch.object(gui,'setup_selected',side_effect=RuntimeError('sample optional failure')):
            self.window.begin()
            self.finish_worker()
        self.assertTrue((self.target/'UnifiedTranscriber.exe').is_file())
        self.assertTrue(self.window.installed)
        self.assertIn('追加環境',self.window.status.get())
        cfg=json.loads((self.target/'Data/settings.json').read_text())
        self.assertEqual(cfg['profile'],'faster-res')  # recommended even before AI setup succeeds
        self.assertFalse(cfg['capture_setup_complete'])
        self.assertFalse(cfg['onboarding_seen'])

    def test_chosen_output_and_model_reach_setup_and_diagnostic_log(self):
        chosen = self.folder / 'Sample Documents' / 'Transcriber'
        self.window.output_location.set(str(chosen))
        self.window.output_modified = True
        self.window.model.set(gui.MODEL_LABELS['small'])
        self.window.minimal.set(True)
        self.window.hardware_modified = True
        self.window.hardware.set('cpu')
        observed = []
        def setup(target, **choices):
            data = json.loads((target/'Data/settings.json').read_text())
            observed.append((data['output_root'], choices['model'], choices['hardware']))
            choices['progress']('sample package diagnostic')
        with patch.object(gui,'setup_selected',side_effect=setup):
            self.window.begin()
            self.finish_worker()
        self.assertEqual(observed, [(str(chosen), 'small', 'cpu')])
        cfg = json.loads((self.target/'Data/settings.json').read_text())
        self.assertEqual(cfg['model_faster'], 'small')
        self.assertTrue(cfg['capture_setup_complete'])
        self.assertTrue(cfg['onboarding_seen'])
        logs = list((chosen/'Logs').glob('setup_*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('sample package diagnostic', logs[0].read_text())

    def test_cuda_advisory_does_not_override_explicit_hardware_choice(self):
        self.window.hardware_modified = True
        self.window.hardware.set('cpu')
        self.window.messages.put(('cuda', SimpleNamespace(summary='Sample GPU', details='advisory only',
                                                        recommended_hardware='cuda')))
        self.window.poll()
        self.assertEqual(self.window.hardware.get(), 'cpu')
        self.assertIn('Sample GPU', self.window.cuda_status.get())

    def test_worker_reads_tk_choices_only_on_the_main_thread(self):
        owner = threading.get_ident()
        for value in (self.window.target,self.window.mode,self.window.recording,self.window.minimal,
                      self.window.hardware,self.window.register,self.window.shortcut):
            original=value.get
            def checked_get(original=original):
                self.assertEqual(threading.get_ident(),owner)
                return original()
            self.stack.enter_context(patch.object(value,'get',side_effect=checked_get))
        with patch.object(gui,'setup_selected',return_value=None):
            self.window.begin()
            self.finish_worker()
        self.assertTrue(self.window.installed)
        self.assertEqual(self.errors,[])

    def test_optional_cancellation_never_interrupts_atomic_uninstall(self):
        self.window.set_busy(True)
        self.window.set_phase('uninstall')
        self.assertTrue(self.window.cancel_button.instate(('disabled',)))
        self.assertNotIn('追加環境',self.window.cancel_button.cget('text'))
        self.window.close()
        self.assertFalse(self.window.cancel.is_set())
        self.assertFalse(self.window.closed)
        self.window.set_phase('optional')
        self.window.close()
        self.assertTrue(self.window.cancel.is_set())
        self.assertFalse(self.window.closed)

    def test_finished_setup_stops_progress_and_next_attempt_resets_it(self):
        self.window.set_busy(True)
        self.assertEqual(str(self.window.progress.cget('mode')), 'indeterminate')
        self.window.messages.put(('complete', '完了', False, []))
        self.window.poll()
        self.assertFalse(self.window.busy)
        self.assertEqual(str(self.window.progress.cget('mode')), 'determinate')
        self.assertEqual(float(self.window.progress.cget('value')), 100.0)
        self.window.set_busy(True)
        self.assertEqual(str(self.window.progress.cget('mode')), 'indeterminate')
        self.assertLess(float(self.window.progress.cget('value')), 100.0)
        self.window.set_busy(False)

    def test_partial_setup_failure_stops_without_showing_success_progress(self):
        self.window.set_busy(True)
        self.window.messages.put(('complete', '追加環境は未完了', False, [], False))
        self.window.poll()
        self.assertFalse(self.window.busy)
        self.assertEqual(str(self.window.progress.cget('mode')), 'determinate')
        self.assertEqual(float(self.window.progress.cget('value')), 0.0)
        self.assertIn('未完了', self.window.status.get())

    def test_auto_launch_requires_enabled_successful_install_or_repair(self):
        cases = ((False, True, True, False, False), (True, True, True, False, True),
                 (True, True, False, False, False), (True, False, True, False, False),
                 (True, True, True, True, False))
        for enabled, installed, success, cancelled, expected in cases:
            with self.subTest(enabled=enabled, installed=installed, success=success, cancelled=cancelled):
                self.window.cancel.clear()
                if cancelled:self.window.cancel.set()
                self.window.launch_after_setup.set(enabled)
                self.window.operation_target = self.target
                self.window.set_busy(True)
                with patch.object(self.window, 'open_app') as launch:
                    self.window.messages.put(('complete', 'テスト完了', installed, [], success))
                    self.window.poll()
                self.assertEqual(launch.call_count, int(expected))

    def test_same_version_maintenance_hides_components_for_uninstall(self):
        install_package(self.package,self.target,gui.APP_VERSION)
        self.window.refresh()
        self.window.mode.set('uninstall')
        self.root.update()
        self.assertFalse(self.window.options.winfo_manager())
        self.window.refresh()  # no pack-before-unmanaged-widget Tk error
        self.window.mode.set('repair')
        self.root.update()
        self.assertTrue(self.window.options.winfo_manager())

    def test_refresh_does_not_undo_registration_selection_before_begin(self):
        install_package(self.package,self.target,gui.APP_VERSION,register_program=True,create_shortcut=True)
        self.window.preference_target=None
        self.window.refresh()
        self.assertTrue(self.window.register.get())
        self.window.register.set(False)
        self.window.shortcut.set(False)
        self.window.refresh()
        self.assertFalse(self.window.register.get())
        self.assertFalse(self.window.shortcut.get())

    def test_newer_version_repair_handoffs_to_verified_installed_maintenance(self):
        maintenance=self.folder/'maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        install_package(sample_package(self.folder,'9.0.0'),self.target,'9.0.0',
            extra_files={'UnifiedTranscriberSetup.exe':maintenance})
        self.window.refresh()
        self.window.mode.set('repair')
        with patch.object(gui.subprocess,'Popen') as launch, patch.object(gui,'reset_dll_search_path') as reset:
            self.window.begin()
            self.finish_worker()
        self.assertTrue(self.window.closed)
        self.assertEqual(launch.call_args.args[0],[str(self.target/'UnifiedTranscriberSetup.exe'),'--target',str(self.target)])
        self.assertFalse(launch.call_args.kwargs['shell'])
        reset.assert_called_once()

    def test_newer_modified_maintenance_is_never_launched(self):
        maintenance=self.folder/'maintenance.exe'
        maintenance.write_bytes(b'MZ sample maintenance')
        install_package(sample_package(self.folder,'9.0.0'),self.target,'9.0.0',
            extra_files={'UnifiedTranscriberSetup.exe':maintenance})
        (self.target/'UnifiedTranscriberSetup.exe').write_bytes(b'modified')
        self.window.refresh()
        self.window.mode.set('repair')
        with patch.object(gui.subprocess,'Popen') as launch:
            self.window.begin()
            self.finish_worker()
        launch.assert_not_called()
        self.assertFalse(self.window.closed)
        self.assertEqual(len(self.errors),1)
        self.assertFalse(self.window.action.instate(('disabled',)))

    def test_update_flag_starts_upgrade_without_rebuilding_optional_environments(self):
        install_package(sample_package(self.folder,'2.0.0'),self.target,'2.0.0')
        settings=self.target/'Data/settings.json'
        settings.parent.mkdir()
        original=b'{"future_custom_field":true,"theme_mode":"dark"}\r\n'
        settings.write_bytes(original)
        child=tk.Toplevel(self.root)
        args=SimpleNamespace(target=self.target,update=True,uninstall=False,wait_pid=None)
        with patch.object(gui,'setup_selected',return_value=None) as setup:
            wizard=gui.InstallerWindow(child,args,(self.package,self.digest))
            try:
                self.root.update()  # executes the automatic upgrade after idle
                wizard.worker.join(timeout=5)
                self.assertFalse(wizard.worker.is_alive())
                wizard.poll()
                self.assertTrue(wizard.installed)
                self.assertFalse(setup.call_args.kwargs['recording'])
                self.assertFalse(setup.call_args.kwargs['minimal'])
                self.assertEqual(settings.read_bytes(),original)
            finally:
                wizard.busy=False
                wizard.close()


if __name__=='__main__':
    unittest.main()
