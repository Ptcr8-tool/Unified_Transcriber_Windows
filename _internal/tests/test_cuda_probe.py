"""Pre-install GPU advisory requires neither torch nor a machine GPU."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cuda_probe


class CudaProbeTests(unittest.TestCase):
    def test_modern_driver_and_gpu_are_likely_not_verified(self):
        result = cuda_probe._parse_query('NVIDIA RTX 4060, 570.01, 8192, 8.9\n')
        self.assertEqual(result.state, 'likely')
        self.assertEqual(result.recommended_hardware, 'cuda')
        self.assertEqual(result.memory_mib, 8192)
        self.assertIn('実際', result.details)

    def test_old_driver_and_old_gpu_default_to_cpu(self):
        for output in ('NVIDIA RTX, 552.22, 4096, 8.6\n',
                       'NVIDIA GTX, 570.01, 4096, 6.1\n',
                       'NVIDIA GPU, 570.01, N/A, N/A\n',
                       'NVIDIA RTX 5090, 580.01, 32768, 12.0\n'):
            with self.subTest(output=output):
                result = cuda_probe._parse_query(output)
                self.assertEqual(result.state, 'unknown')
                self.assertEqual(result.recommended_hardware, 'cpu')

    def test_multiple_devices_choose_supported_one(self):
        result = cuda_probe._parse_query('GTX, 570.01, 4096, 6.1\nRTX, 570.01, 8192, 8.9\n')
        self.assertEqual(result.gpu_name, 'RTX')
        self.assertEqual(result.recommended_hardware, 'cuda')

    def test_reject_invalid_records(self):
        for output in ('', 'GPU, 570.01, 8192', 'x, 570.bad, 1, 8.0',
                       'a\0b, 570.01, 1, 8.0', 'x'*32769):
            with self.subTest(output=output), self.assertRaises(ValueError):
                cuda_probe._parse_query(output)

    def test_unavailable_never_searches_path_or_imports_torch(self):
        with patch.object(cuda_probe, '_system_nvidia_smi', return_value=None), \
             patch.object(cuda_probe.subprocess, 'run') as execute:
            result = cuda_probe.probe_cuda()
        execute.assert_not_called()
        self.assertEqual(result.recommended_hardware, 'cpu')

    def test_driver_query_is_fixed_bounded_and_shell_free(self):
        tool = Path('/trusted/windows/System32/nvidia-smi.exe')
        with patch.object(cuda_probe, '_system_nvidia_smi', return_value=tool), \
             patch.object(cuda_probe.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout='NVIDIA RTX, 570.01, 8192, 8.9\n')) as execute:
            self.assertEqual(cuda_probe.probe_cuda().state, 'likely')
        self.assertEqual(execute.call_args.args[0][0], str(tool))
        self.assertFalse(execute.call_args.kwargs['shell'])
        self.assertEqual(execute.call_args.kwargs['timeout'], 4)
        self.assertEqual(execute.call_args.kwargs['cwd'], str(tool.parent))

    def test_timeout_and_invalid_result_are_unknown(self):
        for value in (subprocess.TimeoutExpired(['nvidia-smi'], 4),
                      OSError('driver busy')):
            with self.subTest(value=value), \
                 patch.object(cuda_probe, '_system_nvidia_smi', return_value=Path('/trusted/nvidia-smi.exe')), \
                 patch.object(cuda_probe.subprocess, 'run', side_effect=value):
                result = cuda_probe.probe_cuda()
                self.assertEqual(result.state, 'unknown')
                self.assertEqual(result.recommended_hardware, 'cpu')


if __name__ == '__main__':
    unittest.main()
