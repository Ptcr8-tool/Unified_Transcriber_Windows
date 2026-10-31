"""Expose this engine's own PyTorch CUDA DLLs to CTranslate2 on Windows.

CTranslate2 dynamically calls LoadLibraryA for cuBLAS and some releases inspect
CUDA_PATH. Register only the active application's virtual-environment library
directory, preload absolute names, and retain loader handles for lazy loads.
The search policy is process-local: the GUI and system PATH are untouched.
"""
from __future__ import annotations

import ctypes
import importlib
import importlib.machinery
import os
from pathlib import Path
import stat
import sys
import threading

LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100
LOAD_LIBRARY_SEARCH_APPLICATION_DIR = 0x00000200
LOAD_LIBRARY_SEARCH_USER_DIRS = 0x00000400
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
SECURE_DEFAULT_DIRS = (LOAD_LIBRARY_SEARCH_APPLICATION_DIR |
                       LOAD_LIBRARY_SEARCH_USER_DIRS | LOAD_LIBRARY_SEARCH_SYSTEM32)
SECURE_ABSOLUTE_LOAD = LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | SECURE_DEFAULT_DIRS
REQUIRED_DLLS = ('cudart64_12.dll', 'cublasLt64_12.dll', 'cublas64_12.dll')
_LOCK = threading.RLock()
_STATE: dict | None = None
_DIRECTORY_HANDLES: list = []
_LIBRARY_HANDLES: list = []


def _ordinary(path: Path, *, directory: bool) -> None:
    """Reject links/reparse ancestors before resolving the trusted path."""
    if not path.is_absolute():
        raise RuntimeError('CUDAライブラリのパスが絶対パスではありません。')
    for ancestor in reversed(path.parents):
        info = ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise RuntimeError('CUDA環境の親フォルダーにリンク／再解析ポイントがあります。')
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise RuntimeError('CUDA環境には通常のフォルダー／ファイルが必要です。')


def _torch_location() -> tuple[Path, Path, Path]:
    prefix = Path(sys.prefix)
    if prefix == Path(sys.base_prefix):
        raise RuntimeError('CUDAはアプリの専用実行環境から使用してください。一括セットアップでCUDA環境を導入できます。')
    _ordinary(prefix, directory=True)
    site = prefix / 'Lib/site-packages'
    package = site / 'torch'
    library = package / 'lib'
    for directory in (site, package, library):
        _ordinary(directory, directory=True)
    _ordinary(package / '__init__.py', directory=False)
    spec = importlib.machinery.PathFinder.find_spec('torch', [str(site)])
    if (spec is None or not spec.origin
            or Path(spec.origin).resolve() != (package / '__init__.py').resolve()):
        raise RuntimeError('現在の仮想環境内のPyTorchを確認できません。CUDA環境を再セットアップしてください。')
    return site, package, library


def _kernel32():
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True,
                           winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
    kernel.SetDefaultDllDirectories.argtypes = [ctypes.c_ulong]
    kernel.SetDefaultDllDirectories.restype = ctypes.c_int
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
    kernel.GetModuleFileNameW.restype = ctypes.c_ulong
    return kernel


def _module_path(kernel, handle: int) -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise RuntimeError('CUDA DLLの実際の読み込み元を検証できません。')
    return Path(buffer.value).resolve()


def _load_torch(site: Path, package: Path):
    existing = sys.modules.get('torch')
    if existing is not None:
        origin = getattr(existing, '__file__', None)
        if not origin or Path(origin).resolve() != (package / '__init__.py').resolve():
            raise RuntimeError('別の場所のPyTorchが読み込まれています。アプリを再起動してください。')
        return existing
    # Import from the verified package location rather than a launch CWD or
    # user site. PyTorch's own native initialization then uses its own libdir.
    sys.path.insert(0, str(site))
    try:
        torch = importlib.import_module('torch')
    finally:
        sys.path.remove(str(site))
    origin = getattr(torch, '__file__', None)
    if not origin or Path(origin).resolve() != (package / '__init__.py').resolve():
        raise RuntimeError('専用実行環境とPyTorchの読み込み元が一致しません。')
    return torch


def initialize_cuda_libraries(device: str = 'cuda') -> dict:
    """Prepare Windows CUDA DLL loading; no GPU or model inference is claimed.

    CPU/Linux perform no PyTorch import and no loader/environment changes.
    Successful state and directory/DLL objects remain alive for this engine's
    lifetime, including CTranslate2's deferred loads during transcription.
    """
    if device != 'cuda' or sys.platform != 'win32':
        return {'status': 'skipped', 'device': device, 'loaded': {}, 'handle_count': 0}
    global _STATE
    with _LOCK:
        site, package, library = _torch_location()
        if _STATE is not None:
            if _STATE['torch_lib'] != str(library.resolve()):
                raise RuntimeError('実行中にCUDA実行環境を変更できません。アプリを再起動してください。')
            return dict(_STATE, loaded=dict(_STATE['loaded']))
        paths = {name: library / name for name in REQUIRED_DLLS}
        # CUDA12 is provided by the selected CUDA PyTorch wheel. The current
        # CTranslate2 pure CUDA convolutions make cuDNN optional; load its
        # wheel-owned components when present for older supported engines too.
        for file in sorted(library.glob('cudnn*64_9.dll')):
            paths[file.name] = file
        try:
            for path in paths.values():
                _ordinary(path, directory=False)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError('CUDA版PyTorchのcuBLAS/CUDA DLLが不足しています。CUDA環境を再セットアップしてください。') from exc
        kernel = _kernel32()
        if not kernel.SetDefaultDllDirectories(SECURE_DEFAULT_DIRS):
            raise RuntimeError('CUDA DLLの安全な検索先を設定できません。')
        # CUDA_PATH is consumed lazily by CTranslate2's cublas_stub.cc, where
        # it otherwise calls SetDllDirectoryA on an external toolkit folder.
        # Only this independent engine process is affected, never the user
        # environment, registry, or system-wide PATH.
        for name in tuple(os.environ):
            if name.upper() == 'CUDA_PATH':
                os.environ.pop(name, None)
        directory = os.add_dll_directory(str(library))
        loaded = {}
        try:
            # Reject a conflicting already-loaded library before importing
            # PyTorch or handing a module-name load to CTranslate2.
            for name, path in paths.items():
                existing = kernel.GetModuleHandleW(name)
                if existing and _module_path(kernel, existing) != path.resolve():
                    raise RuntimeError('別の場所のCUDA DLLが読み込まれています: ' + name)
            torch = _load_torch(site, package)
            if not getattr(getattr(torch, 'version', None), 'cuda', None):
                raise RuntimeError('CPU版PyTorchです。CUDAを選んで実行環境を再セットアップしてください。')
            for name, path in paths.items():
                handle = ctypes.WinDLL(str(path), winmode=SECURE_ABSOLUTE_LOAD)
                _LIBRARY_HANDLES.append(handle)
                origin = _module_path(kernel, handle._handle)
                if origin != path.resolve():
                    raise RuntimeError('CUDA DLLの読み込み元が専用環境と一致しません: ' + name)
                loaded[name] = str(origin)
        except BaseException as exc:
            # A failed attempt must not leave a process-wide DLL search path.
            directory.close()
            if isinstance(exc, (ImportError, OSError, RuntimeError)):
                raise RuntimeError('CUDAライブラリの初期化に失敗しました。CUDA環境を修復してください。\n詳細: ' + str(exc)) from exc
            raise
        _DIRECTORY_HANDLES.append(directory)
        _STATE = {'status': 'ready', 'device': 'cuda', 'torch_lib': str(library.resolve()),
                  'torch_version': str(torch.__version__), 'loaded': loaded,
                  'handle_count': len(_LIBRARY_HANDLES), 'cuDNN_present': any(name.startswith('cudnn') for name in loaded)}
        return dict(_STATE, loaded=dict(loaded))
