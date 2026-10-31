"""The setup bootstrap must use an authenticated, fixed pip distribution."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_backend


class SetupPipLockTests(unittest.TestCase):
    def test_bootstrap_enforces_hash_even_if_pip_is_installed(self):
        commands = []
        with patch.object(setup_backend, 'ensure_pip'), \
             patch.object(setup_backend, 'repair_compression_dependencies'), \
             patch.object(setup_backend, 'torch_ready', return_value=True), \
             patch.object(setup_backend, 'run', side_effect=lambda *args: commands.append(args)):
            setup_backend.install_profile('faster-res', 'cpu', Path('python.exe'))
        bootstrap = next(c for c in commands if '--require-hashes' in c)
        self.assertIn('--force-reinstall', bootstrap)
        self.assertIn('--no-deps', bootstrap)
        lock = Path(bootstrap[bootstrap.index('-r') + 1])
        lines = [line for line in lock.read_text(encoding='utf-8').splitlines()
                 if line and not line.startswith('#')]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^pip==[0-9.]+ --hash=sha256:[a-f0-9]{64}$')


if __name__ == '__main__':
    unittest.main()
