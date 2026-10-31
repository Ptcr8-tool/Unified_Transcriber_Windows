"""Real nested setup output and transactional venv migration, without ML downloads."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_environment
from setup_environment import environment_build
import setup_process
from setup_process import run_logged, setup_interpreter


class SetupProcessTests(unittest.TestCase):
    def test_portable_setup_never_falls_back_to_global_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / 'portable.json').write_text(json.dumps({
                'format': 'unified-transcriber-portable', 'version': 1,
                'app_id': 'UnifiedTranscriberWindows', 'settings_directory': 'Data',
                'install_id': 'a' * 32}))
            with self.assertRaisesRegex(RuntimeError, '同梱Python'):
                setup_interpreter(base, profile='faster-res')
            for name in ('Runtime', 'MLRuntime'):
                (base / name).mkdir()
                (base / name / 'python.exe').touch()
            self.assertEqual(setup_interpreter(base), str(base / 'Runtime/python.exe'))
            for profile in ('faster-res', 'live-capture'):
                self.assertEqual(setup_interpreter(base, profile=profile), str(base / 'MLRuntime/python.exe'))

    def test_source_pythonw_uses_only_its_console_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            console = base / 'python.exe'
            console.touch()
            windows = SimpleNamespace(name='nt')
            with patch.object(setup_process, 'os', windows), \
                    patch.object(sys, 'executable', str(base / 'pythonw.exe')):
                self.assertEqual(setup_interpreter(base), str(console))
                console.unlink()
                with self.assertRaisesRegex(RuntimeError, 'python.exe'):
                    setup_interpreter(base)

    def test_actual_nested_output_merges_stderr_and_masks_secret(self):
        code_root = Path(__file__).resolve().parents[1]
        grandchild = 'import sys; print("日本語 stdout"); print("token-for-test",file=sys.stderr); raise SystemExit(7)'
        child = ('import sys; sys.path.insert(0,' + repr(str(code_root)) + '); '
                 'from pathlib import Path; from setup_process import run_logged; '
                 'result=run_logged([sys.executable,"-c",' + repr(grandchild) + '],Path.cwd(),check=False); '
                 'raise SystemExit(result.returncode)')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'HF_TOKEN': 'token-for-test', 'PYTHONPATH': 'untrusted'}), \
                redirect_stdout(io.StringIO()) as output:
            result = run_logged([sys.executable, '-c', child], Path(temporary), check=False)
        self.assertEqual(result.returncode, 7)
        self.assertIn('日本語 stdout', output.getvalue())
        self.assertIn('[HF_TOKEN非表示]', output.getvalue())
        self.assertNotIn('token-for-test', output.getvalue())
        self.assertNotIn('token-for-test', result.stdout)

    def test_nonzero_result_retains_failure_text(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                run_logged([sys.executable, '-c', 'print("dependency failure"); raise SystemExit(4)'], Path(temporary))
        self.assertEqual(caught.exception.returncode, 4)
        self.assertIn('dependency failure', caught.exception.output)

    def test_token_split_across_bounded_output_chunks_is_still_masked(self):
        token = 'secret-at-chunk-boundary'
        code = 'print("x"*65530 + ' + repr(token) + ' + " end")'
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'HF_TOKEN': token}), redirect_stdout(io.StringIO()) as output:
            result = run_logged([sys.executable, '-c', code], Path(temporary))
        self.assertNotIn(token, output.getvalue())
        self.assertIn('[HF_TOKEN非表示]', output.getvalue())
        self.assertNotIn(token, result.stdout)

    def test_silent_child_and_closed_output_are_stopped(self):
        for code in ('import time; time.sleep(10)',
                     'import os,time; os.close(1); os.close(2); time.sleep(10)'):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                started = time.monotonic()
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(TimeoutError, '出力'):
                    run_logged([sys.executable, '-c', code], Path(temporary), idle_timeout=.3)
                self.assertLess(time.monotonic() - started, 5)

    def test_partial_utf8_output_without_newline_counts_as_progress(self):
        code = ('import time,sys\nfor x in range(10):\n'
                ' sys.stdout.write("日");sys.stdout.flush();time.sleep(.15)')
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()) as output:
            result = run_logged([sys.executable, '-c', code], Path(temporary), idle_timeout=1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output.getvalue(), '日' * 10)

    def test_slow_secret_fragments_are_masked_before_printing(self):
        token = 'secret-token-for-test'
        code = ('import time,sys\nsys.stdout.write("secret-");sys.stdout.flush();'
                'time.sleep(.05);sys.stdout.write("token-for-test");sys.stdout.flush()')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'HF_TOKEN': token}), redirect_stdout(io.StringIO()) as output:
            result = run_logged([sys.executable, '-c', code], Path(temporary), idle_timeout=2)
        self.assertNotIn(token, output.getvalue())
        self.assertIn('[HF_TOKEN非表示]', output.getvalue())
        self.assertNotIn(token, result.stdout)

    def test_short_dependency_probe_times_out_with_actionable_error(self):
        import setup_backend
        with patch.object(setup_backend.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired(['python'], 60)) as probe:
            with self.assertRaisesRegex(RuntimeError, '録音ライブラリの確認.*60秒'):
                setup_backend.run_probe(['python', '-c', 'pass'], purpose='録音ライブラリの確認')
        self.assertEqual(probe.call_args.kwargs['timeout'], 60)


class EnvironmentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.folder = self.base / '_internal/environments/faster-res'
        self.folder.mkdir(parents=True)
        (self.folder / 'old-user-dependency').write_text('keep until ready')
        self.info = {'version': [3, 14], 'base': str(self.base / 'MLRuntime')}

    def context(self):
        return environment_build(self.folder, self.base, 'owned-python.exe', supported=(3, 14))

    def test_failed_dependency_install_keeps_old_environment_and_other_data(self):
        other = self.base / 'settings.json'
        other.write_text('unchanged')
        with patch.object(setup_environment, '_info', return_value=self.info), \
                patch.object(setup_environment, '_matches', return_value=True), \
                patch.object(setup_environment, 'run_logged'), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'install failure'):
                with self.context() as python:
                    self.assertIn('-stage-', str(python))
                    self.assertTrue((self.folder / 'old-user-dependency').is_file())
                    raise RuntimeError('install failure')
        self.assertTrue((self.folder / 'old-user-dependency').is_file())
        self.assertEqual(other.read_text(), 'unchanged')
        self.assertEqual(list(self.folder.parent.glob('.faster-res-stage-*')), [])

    def test_ready_stage_replaces_old_environment_only_at_completion(self):
        with patch.object(setup_environment, '_info', return_value=self.info), \
                patch.object(setup_environment, '_matches', return_value=True), \
                patch.object(setup_environment, 'run_logged'), redirect_stdout(io.StringIO()):
            with self.context() as python:
                stage = python.parents[1]
                (stage / 'new-dependencies').write_text('verified')
                self.assertTrue((self.folder / 'old-user-dependency').is_file())
        self.assertEqual((self.folder / 'new-dependencies').read_text(), 'verified')
        self.assertFalse((self.folder / 'old-user-dependency').exists())
        self.assertEqual(list(self.folder.parent.glob('.faster-res-backup-*')), [])

    def test_post_promotion_probe_failure_restores_old_environment(self):
        with patch.object(setup_environment, '_info', return_value=self.info), \
                patch.object(setup_environment, '_matches', side_effect=[True, False]), \
                patch.object(setup_environment, 'run_logged'), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '移動後'):
                with self.context():
                    pass
        self.assertTrue((self.folder / 'old-user-dependency').is_file())

    def test_failed_rollback_preserves_old_environment_backup(self):
        original_move = setup_environment._move
        def fail_restore(source, destination):
            if '-backup-' in source.name:
                raise PermissionError('locked old environment')
            return original_move(source, destination)
        with patch.object(setup_environment, '_info', return_value=self.info), \
                patch.object(setup_environment, '_matches', side_effect=[True, False]), \
                patch.object(setup_environment, '_move', side_effect=fail_restore), \
                patch.object(setup_environment, 'run_logged'), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '旧環境は削除せず保持'):
                with self.context():
                    pass
        backups = list(self.folder.parent.glob('.faster-res-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'old-user-dependency').is_file())

    def test_links_are_rejected_before_any_python_is_run(self):
        linked = self.folder.parent / 'live-capture'
        try:
            linked.symlink_to(self.folder, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks not permitted on this host')
        with patch.object(setup_environment, '_info') as probe:
            with self.assertRaisesRegex(RuntimeError, 'リンク'):
                with environment_build(linked, self.base, 'owned-python.exe', supported=(3, 14)):
                    pass
        probe.assert_not_called()

    def test_real_venv_relocates_and_uses_same_owned_base(self):
        # A tiny stdlib-only venv confirms the actual Python relocation contract.
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            base = Path(temporary)
            folder = base / '_internal/environments/faster-res'
            with environment_build(folder, base, sys.executable, supported=sys.version_info[:2]) as python:
                self.assertTrue(python.is_file())
                self.assertIn('-stage-', str(python))
            final = setup_environment._python(folder)
            result = subprocess.run([str(final), '-c', 'import sys; print(sys.base_prefix)'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(Path(result.stdout.strip()).resolve(), Path(sys.base_prefix).resolve())


if __name__ == '__main__':
    unittest.main()
