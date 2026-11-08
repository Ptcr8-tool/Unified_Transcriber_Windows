"""Windows model downloads use the native trust store without bypassing TLS."""
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
import hf_transport


class ModelTrustTests(unittest.TestCase):
    def test_windows_configures_native_certificate_trust(self):
        truststore = ModuleType('truststore')
        truststore.inject_into_ssl = Mock()
        with patch.object(hf_transport, 'platform', 'win32'), \
             patch.dict(sys.modules, {'truststore': truststore}):
            hf_transport.configure_model_tls()
        truststore.inject_into_ssl.assert_called_once_with()

    def test_other_platforms_keep_their_existing_transport(self):
        truststore = ModuleType('truststore')
        truststore.inject_into_ssl = Mock()
        with patch.object(hf_transport, 'platform', 'linux'), \
             patch.dict(sys.modules, {'truststore': truststore}):
            hf_transport.configure_model_tls()
        truststore.inject_into_ssl.assert_not_called()

    def test_missing_dependency_points_to_profile_setup(self):
        with patch.object(hf_transport, 'platform', 'win32'), \
             patch.dict(sys.modules, {'truststore': None}):
            with self.assertRaisesRegex(RuntimeError, '認識方式を再セットアップ'):
                hf_transport.configure_model_tls()

    def test_native_trust_initialization_errors_propagate(self):
        truststore = ModuleType('truststore')
        truststore.inject_into_ssl = Mock(side_effect=RuntimeError('native trust failed'))
        with patch.object(hf_transport, 'platform', 'win32'), \
             patch.dict(sys.modules, {'truststore': truststore}):
            with self.assertRaisesRegex(RuntimeError, 'native trust failed'):
                hf_transport.configure_model_tls()

    @unittest.skipUnless(importlib.util.find_spec('truststore'), 'truststore is required')
    def test_real_context_requires_valid_certificate_and_hostname(self):
        # A separate process isolates the application's global SSL initialization.
        script = '''import ssl, sys, truststore
sys.path.insert(0, sys.argv[1])
import hf_transport
hf_transport.platform = 'win32'
hf_transport.configure_model_tls()
context = ssl.create_default_context()
assert isinstance(context, truststore.SSLContext)
assert context.verify_mode == ssl.CERT_REQUIRED
assert context.check_hostname
'''
        result = subprocess.run([sys.executable, '-c', script, str(CODE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
