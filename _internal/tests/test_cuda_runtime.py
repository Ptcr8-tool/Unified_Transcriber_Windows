"""CUDA DLL routing must use the selected venv and exclude launch-directory DLLs."""
from contextlib import ExitStack
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from engines import cuda_runtime as cuda


class CudaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.prefix = Path(self.temporary.name) / 'environment'
        self.site = self.prefix / 'Lib/site-packages'
        self.package = self.site / 'torch'
        self.library = self.package / 'lib'
        self.library.mkdir(parents=True)
        (self.package / '__init__.py').write_text('# trusted package fixture\n')
        for name in cuda.REQUIRED_DLLS:
            (self.library / name).write_bytes(b'trusted fixture')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cuda.sys, 'prefix', str(self.prefix)))
        self.stack.enter_context(patch.object(cuda.sys, 'base_prefix', str(self.prefix / 'base')))
        self.stack.enter_context(patch.object(cuda.sys, 'platform', 'win32'))
        self.stack.enter_context(patch.object(cuda, '_STATE', None))
        self.stack.enter_context(patch.object(cuda, '_DIRECTORY_HANDLES', []))
        self.stack.enter_context(patch.object(cuda, '_LIBRARY_HANDLES', []))

    def native_fixture(self):
        kernel = MagicMock()
        kernel.SetDefaultDllDirectories.return_value = 1
        kernel.GetModuleHandleW.return_value = 0
        handles = {}
        seen = []
        def load(path, *, winmode):
            self.assertTrue(Path(path).is_absolute())
            self.assertEqual(winmode, cuda.SECURE_ABSOLUTE_LOAD)
            seen.append(path)
            handle = SimpleNamespace(_handle=len(handles) + 1)
            handles[handle._handle] = Path(path).resolve()
            return handle
        self.stack.enter_context(patch.object(cuda, '_kernel32', return_value=kernel))
        add = self.stack.enter_context(patch.object(cuda.os, 'add_dll_directory', create=True))
        self.stack.enter_context(patch.object(cuda.ctypes, 'WinDLL', side_effect=load, create=True))
        self.stack.enter_context(patch.object(cuda, '_module_path', side_effect=lambda _kernel, handle: handles[handle]))
        torch = SimpleNamespace(__version__='2.14.1+cu126', version=SimpleNamespace(cuda='12.6'))
        imported = self.stack.enter_context(patch.object(cuda, '_load_torch', return_value=torch))
        return kernel, add, imported, seen

    def test_cpu_and_non_windows_do_not_import_torch_or_modify_loader(self):
        with patch.object(cuda, '_torch_location') as location:
            self.assertEqual(cuda.initialize_cuda_libraries('cpu')['status'], 'skipped')
            with patch.object(cuda.sys, 'platform', 'linux'):
                self.assertEqual(cuda.initialize_cuda_libraries()['status'], 'skipped')
            location.assert_not_called()

    def test_only_this_venv_torch_location_is_accepted_without_import(self):
        with patch.object(cuda.importlib, 'import_module') as importer:
            site, package, library = cuda._torch_location()
            self.assertEqual((site, package, library), (self.site, self.package, self.library))
            importer.assert_not_called()
        with patch.object(cuda.sys, 'base_prefix', str(self.prefix)):
            with self.assertRaisesRegex(RuntimeError, '専用実行環境'):
                cuda._torch_location()

    def test_missing_cuda_dll_fails_before_torch_import_and_loader_changes(self):
        (self.library / cuda.REQUIRED_DLLS[0]).unlink()
        with patch.object(cuda, '_kernel32') as kernel, patch.object(cuda, '_load_torch') as imported:
            with self.assertRaisesRegex(RuntimeError, 'DLLが不足'):
                cuda.initialize_cuda_libraries()
            kernel.assert_not_called()
            imported.assert_not_called()

    def test_cuda_symlink_rejected_before_dll_loading(self):
        target = self.library / cuda.REQUIRED_DLLS[0]
        outside = self.prefix / 'outside.dll'
        outside.write_bytes(b'foreign')
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with patch.object(cuda, '_kernel32') as kernel:
            with self.assertRaisesRegex(RuntimeError, 'DLLが不足'):
                cuda.initialize_cuda_libraries()
            kernel.assert_not_called()

    def test_secure_absolute_load_retains_handles_is_idempotent_and_preserves_path(self):
        kernel, add, imported, seen = self.native_fixture()
        with patch.dict(os.environ, {'PATH': 'keep-user-path', 'CUDA_PATH': 'untrusted-toolkit'}):
            first = cuda.initialize_cuda_libraries()
            self.assertEqual(os.environ['PATH'], 'keep-user-path')
            self.assertNotIn('CUDA_PATH', os.environ)
            second = cuda.initialize_cuda_libraries()
        self.assertEqual(first, second)
        self.assertEqual(first['handle_count'], 3)
        self.assertEqual(list(first['loaded']), list(cuda.REQUIRED_DLLS))
        self.assertFalse(first['cuDNN_present'])
        self.assertEqual(len(seen), 3)
        kernel.SetDefaultDllDirectories.assert_called_once_with(cuda.SECURE_DEFAULT_DIRS)
        add.assert_called_once_with(str(self.library))
        imported.assert_called_once()
        self.assertEqual(len(cuda._DIRECTORY_HANDLES), 1)
        self.assertEqual(len(cuda._LIBRARY_HANDLES), 3)

    def test_existing_foreign_cuda_library_rejected_before_torch_import(self):
        kernel, add, imported, seen = self.native_fixture()
        kernel.GetModuleHandleW.return_value = 99
        with patch.object(cuda, '_module_path', return_value=self.prefix / 'foreign.dll'):
            with self.assertRaisesRegex(RuntimeError, '別の場所のCUDA DLL'):
                cuda.initialize_cuda_libraries()
        imported.assert_not_called()
        add.return_value.close.assert_called_once_with()
        self.assertEqual(cuda._DIRECTORY_HANDLES, [])
        self.assertEqual(seen, [])

    def test_wrong_loaded_library_origin_rejected(self):
        kernel, add, imported, seen = self.native_fixture()
        with patch.object(cuda, '_module_path', return_value=self.prefix / 'foreign.dll'):
            with self.assertRaisesRegex(RuntimeError, '読み込み元'):
                cuda.initialize_cuda_libraries()
        self.assertEqual(len(seen), 1)

    def test_existing_foreign_torch_module_not_used(self):
        foreign = SimpleNamespace(__file__=str(self.prefix / 'foreign/__init__.py'))
        with patch.dict(sys.modules, {'torch': foreign}):
            with self.assertRaisesRegex(RuntimeError, '別の場所のPyTorch'):
                cuda._load_torch(self.site, self.package)

    def test_optional_wheel_owned_cudnn_components_are_loaded_and_retained(self):
        (self.library / 'cudnn64_9.dll').write_bytes(b'trusted cuDNN fixture')
        (self.library / 'cudnn_ops64_9.dll').write_bytes(b'trusted component fixture')
        kernel, add, imported, seen = self.native_fixture()
        result = cuda.initialize_cuda_libraries()
        self.assertTrue(result['cuDNN_present'])
        self.assertEqual(result['handle_count'], 5)
        self.assertTrue(all(Path(path).parent == self.library for path in seen))


if __name__ == '__main__':
    unittest.main()
