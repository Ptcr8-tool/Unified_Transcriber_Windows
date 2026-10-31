"""Advisory NVIDIA driver check before optional AI packages are installed.

This module never imports torch, changes a driver, or searches the current
working directory/PATH for executables. A driver query is evidence of likely
support, not a successful CUDA inference test.
"""
from __future__ import annotations

import csv
import ctypes
from dataclasses import dataclass
import io
import os
from pathlib import Path
import subprocess

# The optional CUDA environment uses the official cu126 PyTorch wheels.
# Conservative Windows CUDA 12.6 GA driver floor, rather than the less strict
# minor-version compatibility floor. Pre-Turing GPUs need manual confirmation.
MIN_WINDOWS_DRIVER = (560, 76)
MIN_RECOMMENDED_CAPABILITY = (7, 5)
MAX_RECOMMENDED_CAPABILITY = (9, 0)


@dataclass(frozen=True)
class CudaAvailability:
    state: str  # likely / not-detected / unknown
    summary: str
    details: str
    recommended_hardware: str = 'cpu'
    gpu_name: str = ''
    driver_version: str = ''
    memory_mib: int | None = None


def _system_nvidia_smi() -> Path | None:
    if os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_system = kernel.GetSystemDirectoryW
    get_system.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    get_system.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_system(buffer, len(buffer))
    if not length or length >= len(buffer):
        return None
    path = Path(buffer.value) / 'nvidia-smi.exe'
    return path if path.is_file() and not path.is_symlink() else None


def _version(value: str) -> tuple[int, ...]:
    pieces = value.strip().split('.')
    if not pieces or any(not part.isdecimal() for part in pieces):
        raise ValueError('invalid NVIDIA version')
    return tuple(int(part) for part in pieces)


def _parse_query(output: str) -> CudaAvailability:
    if len(output) > 32768 or '\0' in output:
        raise ValueError('invalid NVIDIA response')
    rows = list(csv.reader(io.StringIO(output)))
    if not rows or len(rows) > 32:
        raise ValueError('invalid NVIDIA device count')
    detected = []
    for row in rows:
        if len(row) != 4:
            raise ValueError('invalid NVIDIA device record')
        name, driver, memory, capability = (part.strip() for part in row)
        if not name or len(name) > 256 or any(ord(c) < 32 for c in name):
            raise ValueError('invalid NVIDIA device name')
        driver_tuple = _version(driver)
        memory_mib = int(memory) if memory.isdecimal() else None
        try:
            compute = _version(capability)
        except ValueError:
            compute = ()
        likely = (driver_tuple >= MIN_WINDOWS_DRIVER
                  and MIN_RECOMMENDED_CAPABILITY <= compute <= MAX_RECOMMENDED_CAPABILITY)
        detected.append((likely, name, driver, memory_mib, capability))
    likely, name, driver, memory, capability = max(detected, key=lambda item: item[0])
    gpu = f'{name} / ドライバー {driver}'
    if memory is not None:
        gpu += f' / VRAM {memory / 1024:.1f} GB'
    if likely:
        return CudaAvailability('likely', 'CUDAを利用できる可能性があります',
            gpu + '\nGPU構成を候補にしました。環境導入後のCUDA診断で実際の利用可否を確認します。'
            'モデルの必要VRAMも確認してください。', 'cuda', name, driver, memory)
    return CudaAvailability('unknown', 'NVIDIA GPUを検出しました（CUDAの互換性は要確認）',
        gpu + f' / 演算能力 {capability}\nCPU構成を候補にしました。'
        'GPU構成を選ぶ場合は、対応GPU・ドライバーとCUDA診断を確認してください。',
        'cpu', name, driver, memory)


def probe_cuda() -> CudaAvailability:
    """Complete the driver query within four seconds; no optional dependencies."""
    try:
        program = _system_nvidia_smi()
        if program is None:
            return CudaAvailability('not-detected', 'CUDA対応環境を確認できませんでした',
                'CPU構成を候補にしました。NVIDIAドライバー未導入・照会ツールなしの場合も含みます。'
                '対応GPUをお使いの場合はGPU構成へ変更できます。')
        result = subprocess.run(
            [str(program), '--query-gpu=name,driver_version,memory.total,compute_cap',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=4, check=False, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            cwd=str(program.parent))
        if result.returncode != 0:
            raise ValueError('driver query failed')
        return _parse_query(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return CudaAvailability('unknown', 'CUDAの事前確認を完了できませんでした',
            'CPU構成を候補にしました。照会の失敗・タイムアウトはGPU非対応とは限りません。'
            'GPU構成は手動でも選べます。')
