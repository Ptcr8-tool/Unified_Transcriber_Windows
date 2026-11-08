"""Legacy pyannote setup must resolve without upgrading its NumPy ABI."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_backend


class PyannoteSetupTests(unittest.TestCase):
    def commands_for(self, profile):
        with patch.object(setup_backend, 'ensure_pip'), \
             patch.object(setup_backend, 'repair_compression_dependencies'), \
             patch.object(setup_backend, 'torch_ready', return_value=True), \
             patch.object(setup_backend, 'run') as run:
            setup_backend.install_profile(profile, 'cpu', Path('python.exe'))
        return run.call_args_list

    def test_both_pyannote_profiles_authenticate_source_bootstrap_before_install(self):
        for profile in ('faster-pya', 'qwen-pya'):
            with self.subTest(profile=profile):
                calls = self.commands_for(profile)
                builds = [c for c in calls if any('requirements-pyannote-build.lock' in a for a in c.args)]
                sources = [c for c in calls if any('requirements-pyannote-source.lock' in a for a in c.args)]
                self.assertEqual(len(builds), 1)
                self.assertEqual(len(sources), 1)
                for call in (builds[0], sources[0]):
                    self.assertIn('--require-hashes', call.args)
                    self.assertIn('--no-deps', call.args)
                    self.assertIn('--force-reinstall', call.args)
                self.assertIn('--no-build-isolation', sources[0].args)
                self.assertEqual(sources[0].kwargs['source_packages'], ('docopt', 'antlr4-python3-runtime'))
                install = next(c for c in calls if 'pyannote.audio==3.3.2' in c.args)
                self.assertLess(calls.index(builds[0]), calls.index(sources[0]))
                self.assertLess(calls.index(sources[0]), calls.index(install))
                self.assertIn(str(setup_backend.CODE_DIR / 'constraints_pyannote.txt'), install.args)
                self.assertIn('numpy==1.26.4', install.args)
                self.assertIn('huggingface-hub==0.36.0', install.args)
                self.assertTrue(any('from pyannote.audio import Pipeline' in a for c in calls for a in c.args))

    def test_source_exception_leaves_other_packages_wheel_only(self):
        with patch.object(setup_backend, 'run_logged') as run, \
             patch.object(setup_backend, 'is_portable', return_value=False):
            setup_backend.run('python.exe', '-m', 'pip', 'install', '--require-hashes',
                '--no-deps', '--no-build-isolation', '-r',
                str(setup_backend.CODE_DIR / 'requirements-pyannote-source.lock'),
                source_packages=('docopt', 'antlr4-python3-runtime'))
            setup_backend.run('python.exe', '-m', 'pip', 'install', 'pyannote.audio==3.3.2')
        source, ordinary = [call.args[0] for call in run.call_args_list]
        self.assertIn('--only-binary=:all:', source)
        self.assertGreater(source.index('--no-binary=docopt,antlr4-python3-runtime'),
                           source.index('--only-binary=:all:'))
        self.assertIn('--only-binary=:all:', ordinary)
        self.assertFalse(any(a.startswith('--no-binary') for a in ordinary))

    def test_every_huggingface_profile_installs_system_trust_support(self):
        for profile in ('faster-res', 'faster-pya', 'qwen-res', 'qwen-pya'):
            with self.subTest(profile=profile):
                self.assertTrue(any('truststore==0.10.5' in c.args for c in self.commands_for(profile)))

    def test_bootstrap_locks_pin_each_archive_and_build_wheel(self):
        for filename, expected in (
                ('requirements-pyannote-build.lock', {'setuptools==84.2.1'}),
                ('requirements-pyannote-source.lock', {'docopt==0.6.2', 'antlr4-python3-runtime==4.9.3'})):
            with self.subTest(filename=filename):
                lines = [line for line in (setup_backend.CODE_DIR / filename).read_text().splitlines()
                         if line and not line.startswith('#')]
                self.assertEqual({line.split()[0] for line in lines}, expected)
                for line in lines:
                    self.assertRegex(line, r'^[A-Za-z0-9_.-]+==[0-9.]+ --hash=sha256:[a-f0-9]{64}$')


if __name__ == '__main__':
    unittest.main()
