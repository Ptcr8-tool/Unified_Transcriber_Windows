"""Portable storage, hostile configuration and real silent-child cancellation."""
from contextlib import nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from core import Config, load_config, save_config
import portable_runtime as portable
import setup_backend


def marker(root):
    (root / 'portable.json').write_text(json.dumps({
        'format': 'unified-transcriber-portable', 'version': 1,
        'app_id': 'UnifiedTranscriberWindows', 'settings_directory': 'Data',
        'install_id': 'a' * 32}), encoding='utf-8')


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Sample App'
        self.root.mkdir()

    def test_marker_requires_installer_identity_and_fixed_data_directory(self):
        marker(self.root)
        self.assertTrue(portable.is_portable(self.root))
        path = self.root / 'portable.json'
        valid = json.loads(path.read_text())
        for key, value in [('app_id', 'other'), ('version', True),
                           ('settings_directory', '../outside'), ('install_id', 'not-an-id')]:
            with self.subTest(key=key):
                path.write_text(json.dumps({**valid, key: value}))
                self.assertFalse(portable.is_portable(self.root))
        path.write_text(' ' * 5000)
        self.assertFalse(portable.is_portable(self.root))

    def test_runtime_environment_removes_frozen_interpreter_injection_and_keeps_token_in_memory(self):
        marker(self.root)
        supplied = {'PATH': 'device-tools', 'PYTHONPATH': 'untrusted',
                    'pythonhome': 'untrusted', 'PYTHONSTARTUP': 'evil.py',
                    'PIP_INDEX_URL': 'http://untrusted.invalid', 'PIP_TRUSTED_HOST': 'evil.invalid',
                    'HF_TOKEN': 'token-in-memory', 'TCL_LIBRARY': 'old-tcl',
                    'TK_LIBRARY': 'old-tk', '_PYI_ARCHIVE_FILE': 'old.exe', '_MEIPASS2': 'old',
                    'HF_HOME': 'external-model-folder'}
        original = dict(supplied)
        env = portable.runtime_environment(self.root, supplied)
        self.assertEqual(supplied, original)
        for key in ('PYTHONPATH', 'pythonhome', 'PYTHONSTARTUP', 'PIP_INDEX_URL',
                    'PIP_TRUSTED_HOST', 'TCL_LIBRARY', 'TK_LIBRARY', '_PYI_ARCHIVE_FILE', '_MEIPASS2'):
            self.assertNotIn(key, env)
        self.assertEqual(env['HF_TOKEN'], 'token-in-memory')
        self.assertEqual(env['PATH'], supplied['PATH'])
        self.assertEqual(Path(env['HF_HOME']), self.root.resolve() / 'Models' / 'huggingface')
        self.assertEqual(Path(env['TORCH_HOME']), self.root.resolve() / 'Models' / 'torch')
        self.assertEqual(env['PIP_CONFIG_FILE'], os.devnull)
        self.assertFalse((self.root / 'Data').exists(), 'Building an environment must not write credentials')

    def test_source_checkout_keeps_user_model_cache_and_legacy_config_path(self):
        env = portable.runtime_environment(self.root, {'HF_HOME': 'existing-cache'})
        self.assertEqual(env['HF_HOME'], 'existing-cache')
        with patch.object(core, 'BASE', self.root), patch.dict(os.environ, {'APPDATA': str(self.root / 'legacy')}):
            self.assertEqual(core.app_config_path(), self.root / 'legacy' / 'UnifiedTranscriberV12' / 'settings.json')

    def test_windows_runtime_path_is_fixed_first_and_deduplicated_without_cwd(self):
        marker(self.root)
        runtime = self.root / 'Runtime'
        runtime.mkdir()
        # A module proxy leaves pathlib's actual OS unchanged on Linux CI.
        windows = SimpleNamespace(name='nt', environ={}, devnull=os.devnull)
        previous = ';'.join(['C:\\Windows\\System32', str(runtime).upper(), '.', '',
                             str(runtime).replace('/', '\\'), 'C:\\SampleTools'])
        with patch.object(portable, 'os', windows):
            env = portable.runtime_environment(self.root, {'Path': previous})
        self.assertEqual(env['Path'].split(';'), [str(runtime.resolve()), r'C:\Windows\System32', r'C:\SampleTools'])
        self.assertNotIn('PATH', env, 'Do not create two differently cased Windows PATH keys')
        runtime.rmdir()
        with patch.object(portable, 'os', windows):
            unchanged = portable.runtime_environment(self.root, {'PATH': previous})
        self.assertEqual(unchanged['PATH'], previous)

    def test_valid_legacy_settings_are_copied_once_and_original_is_untouched(self):
        marker(self.root)
        old = self.root / 'previous' / 'settings.json'
        old.parent.mkdir()
        old.write_text(json.dumps({'profile': 'faster-res', 'model_faster': 'small',
                                  'theme_mode': 'dark', 'hf_token_json': 'explicit-saved-token'}))
        original = old.read_bytes()
        with patch.object(core, 'BASE', self.root), patch.object(core, 'legacy_config_path', return_value=old):
            cfg = load_config()
            self.assertEqual((cfg.profile, cfg.model_faster, cfg.theme_mode), ('faster-res', 'small', 'dark'))
            self.assertEqual(cfg.hf_token_json, 'explicit-saved-token')
            local = self.root / 'Data' / 'settings.json'
            self.assertTrue(local.is_file())
            self.assertEqual(old.read_bytes(), original)
            cfg.model_faster = 'medium'
            save_config(cfg)
            old.write_text('{"profile":"qwen-res"}')
            self.assertEqual(load_config().model_faster, 'medium')
            self.assertEqual(core.app_config_path(), local)

    def test_atomic_config_write_failure_does_not_change_existing_file(self):
        target = self.root / 'settings.json'
        save_config(Config(model_faster='small'), target)
        before = target.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('simulated replacement failure')):
            with self.assertRaises(OSError):
                save_config(Config(model_faster='large-v3'), target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_setup_plan_contains_only_selected_features_and_fixed_model(self):
        self.assertEqual(portable.setup_commands(self.root, recording=False, minimal=False), [])
        recording = portable.setup_commands(self.root, recording=True, minimal=False)
        self.assertEqual(Path(recording[0][-1]).name, 'setup_live_capture.py')
        minimum = portable.setup_commands(self.root, recording=True, minimal=True, hardware='cuda')
        command = minimum[0]
        self.assertEqual(command[command.index('--profile')+1], 'faster-res:cuda')
        self.assertEqual(command[command.index('--model')+1], 'faster:large-v3-turbo')
        self.assertIn('--no-pyannote-model', command)
        self.assertIn('--with-audio', command)
        for model in ('small', 'medium', 'large-v3', 'distil-large-v3'):
            selected = portable.setup_commands(self.root, recording=False, minimal=True, model=model)[0]
            self.assertEqual(selected[selected.index('--model')+1], 'faster:' + model)
        for model in ('../outside', '--untrusted-argument', '', None, []):
            with self.subTest(model=model), self.assertRaises(ValueError):
                portable.setup_commands(self.root, recording=False, minimal=True, model=model)
        with self.assertRaises(ValueError):
            portable.setup_commands(self.root, recording=True, minimal=True, hardware='cpu & run')

    def prepare_fake_install(self):
        marker(self.root)
        (self.root / 'Runtime').mkdir()
        (self.root / 'Runtime' / 'python.exe').write_bytes(b'placeholder; not executed by tests')

    def test_real_silent_setup_child_is_cancelled_without_second_press(self):
        self.prepare_fake_install()
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(portable, 'setup_commands', return_value=[
                    [sys.executable, '-c', 'import time; time.sleep(30)']]):
                with self.assertRaises(InterruptedError):
                    portable.setup_selected(self.root, cancel=cancel, timeout=10)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 5)

    def test_setup_error_preserves_data_and_redacts_token_from_output(self):
        self.prepare_fake_install()
        (self.root / 'Data').mkdir()
        data = self.root / 'Data' / 'settings.json'
        data.write_text('{"model_faster":"small"}')
        progress = []
        code = 'print("failure with token-in-memory", flush=True); raise SystemExit(3)'
        with patch.dict(os.environ, {'HF_TOKEN': 'token-in-memory'}), \
                patch.object(portable, 'setup_commands', return_value=[[sys.executable, '-c', code]]):
            with self.assertRaises(RuntimeError):
                portable.setup_selected(self.root, progress=progress.append)
        self.assertNotIn('token-in-memory', ''.join(progress))
        self.assertIn('[HF_TOKEN非表示]', ''.join(progress))
        self.assertEqual(data.read_text(), '{"model_faster":"small"}')


class HostileConfigTests(unittest.TestCase):
    def test_bad_types_and_nonfinite_values_are_rejected_before_attribute_access(self):
        cases = [{'model_openai': 1}, {'language': []}, {'hf_token_json': {}},
                 {'python_overrides': {'faster-res': []}}, {'max_speakers': True},
                 {'threshold': float('nan')}, {'segment_length': float('inf')},
                 {'threshold': 10**500}, {'output_dir': 'bad\0folder'},
                 {'capture_preferences': {'gamebar': {'shortcut': 1}}},
                 {'capture_preferences': {'live': {'mic_gain': '1'}}},
                 {'capture_preferences': {'live': {'folder': {}}}},
                 {'update_check_days': False}, {'update_check_mode': 'once-per-minute'}]
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    Config(**values).validate()

    def test_windows_python_override_cannot_be_a_batch_command(self):
        for name in (r'C:\Sample\python.cmd', r'C:\Sample\python.bat',
                     r'\\server\share\python.ps1', r'C:\Sample\python.exe '):
            cfg = Config(python_overrides={'faster-res': name})
            with self.subTest(name=name):
                # Leading/trailing spaces are harmless trimming, so only the
                # executable basename's actual suffix determines acceptance.
                if name.endswith('.exe '):
                    self.assertEqual(core.env_python('faster-res', cfg).suffix, '.exe')
                    continue
                with self.assertRaises(ValueError):
                    cfg.validate()
                with self.assertRaises(ValueError):
                    core.env_python('faster-res', cfg)
        native = Config(python_overrides={'faster-res': 'python'})
        with patch.object(core.os, 'name', 'nt'):
            with self.assertRaises(ValueError):
                native.validate()
        native_path = 'C:/Sample/python.exe' if os.name == 'nt' else '/sample/bin/python'
        self.assertEqual(core.env_python('faster-res', Config(
            python_overrides={'faster-res': native_path})), Path(native_path))

    def test_corrupt_large_and_deep_settings_do_not_crash_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            for raw in ('{"model_openai":1}', '{"language":[]}', '{"hf_token_json":{}}',
                        '[' * 5000 + '0' + ']' * 5000, ' ' * (portable.MAX_CONFIG_BYTES + 1)):
                with self.subTest(size=len(raw)):
                    path.write_text(raw)
                    self.assertEqual(load_config(path), Config())

    def test_optional_update_schedule_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            cfg = Config(update_check_mode='every', update_check_days=30,
                         update_last_checked='2000-01-01T09:00:00+00:00')
            save_config(cfg, path)
            self.assertEqual(load_config(path), cfg)


class SetupSupplyChainTests(unittest.TestCase):
    def test_minimum_profile_uses_patched_pair_without_silently_changing_legacy_stacks(self):
        self.assertEqual(setup_backend.torch_baseline('faster-res'), '2.14.1')
        self.assertEqual(setup_backend.torchaudio_baseline('faster-res'), '2.11.0')
        for profile in ('openai-res', 'faster-pya', 'qwen-res', 'qwen-pya'):
            self.assertEqual(setup_backend.torch_baseline(profile), '2.8.0')
            self.assertEqual(setup_backend.torchaudio_baseline(profile), '2.8.0')
        with patch.object(setup_backend.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(setup_backend.torch_ready(Path('python.exe'), 'cpu', 'faster-res'))
        probe = run.call_args.args[0][-1]
        self.assertIn("torch.__version__.split('+')[0] == '2.14.1'", probe)
        self.assertIn("torchaudio.__version__.split('+')[0] == '2.11.0'", probe)
        self.assertIn('torch.version.cuda is None', probe)

    def test_existing_old_minimum_environment_is_upgraded_to_matching_official_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'faster-res'
            python = folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.write_bytes(b'existing interpreter placeholder')
            with patch.object(setup_backend, 'environment_folder', return_value=folder), \
                    patch.object(setup_backend, 'environment_build', return_value=nullcontext(python)), \
                    patch.object(setup_backend.sys, 'version_info', (3, 11)), \
                    patch.object(setup_backend, 'ensure_media_tools'), \
                    patch.object(setup_backend, 'ensure_pip'), \
                    patch.object(setup_backend, 'repair_compression_dependencies'), \
                    patch.object(setup_backend, 'torch_ready', return_value=False) as ready, \
                    patch.object(setup_backend, 'run') as run:
                setup_backend.setup('faster-res', 'cuda')
            ready.assert_called_once_with(python, 'cuda', 'faster-res')
            commands = [call.args for call in run.call_args_list]
            torch_command = next(command for command in commands if 'torch==2.14.1' in command)
            self.assertIn('torchaudio==2.11.0', torch_command)
            self.assertEqual(torch_command[torch_command.index('--index-url')+1],
                             'https://download.pytorch.org/whl/cu126')
            self.assertFalse(any('torch==2.8.0' in command for command in commands))

    def test_pip_install_uses_fixed_index_isolation_wheels_and_security_constraints(self):
        with patch.object(setup_backend, 'run_logged') as run, \
                patch.dict(os.environ, {'PIP_INDEX_URL': 'http://untrusted.invalid', 'PYTHONPATH': 'evil'}):
            setup_backend.run('C:/Sample/python.exe', '-m', 'pip', '--isolated', 'install', 'faster-whisper')
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--index-url')+1], 'https://pypi.org/simple')
        self.assertIn('--only-binary=:all:', command)
        self.assertTrue(command[command.index('--constraint')+1].endswith('constraints_security.txt'))
        self.assertEqual(run.call_args.args[1], core.BASE)

    def test_known_vulnerable_existing_packages_are_repaired_even_if_not_needed_by_profile(self):
        answer = subprocess.CompletedProcess([], 0, '["Brotli>=1.2.1"]', '')
        with patch.object(setup_backend.subprocess, 'run', return_value=answer), \
                patch.object(setup_backend, 'run') as install:
            setup_backend.repair_compression_dependencies(Path('C:/Sample/python.exe'))
        command = install.call_args.args
        self.assertIn('--isolated', command)
        self.assertIn('Brotli>=1.2.1', command)
        self.assertNotIn('brotlicffi>=1.2.1.0', command)

    def test_invalid_metadata_cannot_supply_pip_arguments(self):
        answer = subprocess.CompletedProcess([], 0, '["--index-url=http://evil.invalid"]', '')
        with patch.object(setup_backend.subprocess, 'run', return_value=answer), \
                patch.object(setup_backend, 'run') as install:
            with self.assertRaises(RuntimeError):
                setup_backend.repair_compression_dependencies(Path('C:/Sample/python.exe'))
        install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
