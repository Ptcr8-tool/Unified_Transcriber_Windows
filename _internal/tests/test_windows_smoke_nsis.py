"""A successful Python report cannot substitute for the real NSIS process."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


SOURCE = Path(__file__).resolve().parents[1]/'tools/windows_smoke.py'
SPEC = importlib.util.spec_from_file_location('nsis_windows_smoke', SOURCE)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class NSISSmokeProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.setup = self.folder/'SampleSetup.exe'
        self.setup.write_bytes(b'MZsample-distribution')
        self.result_path = self.folder/'probe.json'
        self.report = {'ok': True, 'packaging': 'nsis', 'setup_parent_verified': True,
            'setup_owner_pid': 123, 'setup_executable': str(self.setup),
            'setup_resource_root': str(self.folder/'nsis-private-resources')}

    def probe(self, report):
        def communicate(timeout=None):
            self.result_path.write_text(json.dumps(report), encoding='utf-8')
            return '', ''
        process = SimpleNamespace(pid=123, returncode=0, communicate=communicate)
        with patch.object(smoke.subprocess, 'Popen', return_value=process):
            return smoke.check_nsis_probe(self.setup,
                ['--self-test', '--self-test-report', self.result_path], self.result_path)

    def test_python_success_cannot_hide_a_missing_or_different_nsis_parent(self):
        changes = ({'packaging': 'pyinstaller'}, {'setup_parent_verified': False},
                   {'setup_owner_pid': 456}, {'ok': False})
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                self.probe(dict(self.report, **changed))

    def test_origin_must_be_the_actual_distribution_not_another_ordinary_exe(self):
        other = self.folder/'DifferentSetup.exe'
        other.write_bytes(self.setup.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'different executable'):
            self.probe(dict(self.report, setup_executable=str(other)))

    def test_relative_or_pyinstaller_resource_paths_are_rejected(self):
        for resource_root in ('relative/resources', str(self.folder), str(self.folder/'_MEIsample')):
            with self.subTest(resource_root=resource_root), self.assertRaisesRegex(RuntimeError, 'NSIS resources'):
                self.probe(dict(self.report, setup_resource_root=resource_root))

    def test_verified_origin_and_direct_parent_can_accept_a_finished_nsis_probe(self):
        self.assertEqual(self.probe(self.report), self.report)

    def test_first_release_runs_synthetic_upgrade_and_preserves_data(self):
        payload = self.folder/'payload.zip'
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('VERSION', '1.0.0\n')
            archive.writestr('_internal/core.py', "APP_VERSION = '1.0.0'\n")
            archive.writestr('UnifiedTranscriber.exe', b'app')
            archive.writestr('Runtime/python.exe', b'python')
            archive.writestr('Runtime/pythonw.exe', b'pythonw')
        result = {'checks': []}
        smoke.check_legacy_layout_upgrade(payload, self.setup, '1.0.0', self.folder, result)
        self.assertEqual(result['legacy_layout_migration']['from_version'], '0.9.0')
        self.assertTrue(result['legacy_layout_migration']['fixture_only'])
        self.assertTrue(result['legacy_layout_migration']['install_id_preserved'])
        self.assertTrue(result['legacy_layout_migration']['maintenance_matches_new_distribution'])

    def test_synthetic_upgrade_refuses_non_newer_version(self):
        with self.assertRaisesRegex(RuntimeError, 'newer than the synthetic fixture'):
            smoke.check_legacy_layout_upgrade(self.folder/'unused.zip', self.setup, '0.9.0',
                                              self.folder, {'checks': []})


if __name__ == '__main__':
    unittest.main()
