"""Optional per-ASR-process CPU cap and GPU-paced inference control.

CPU: Windows Job Object CPU rate hard cap (percentage of total logical CPUs).
GPU: portable *best-effort* pause after ASR progress events; this is not
an NVIDIA utilization limit and cannot affect an in-flight kernel.
Never apply these limits to the live audio-capture process.
"""
from __future__ import annotations
import ctypes
import json
import os
import threading
from pathlib import Path


class WindowsCpuLimit:
    def __init__(self):
        self.job = None
        self.pid = None
        self._lock = threading.RLock()
        self._kernel = None

    def attach(self, pid: int, percent: int | None) -> str:
        if os.name != 'nt':
            return 'Windows以外ではCPU Job Objectは無効です。'
        with self._lock:
            self.close()
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            kernel.SetInformationJobObject.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            job = kernel.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), 'CreateJobObjectW failed')
            process = kernel.OpenProcess(0x0100 | 0x0001 | 0x1000, False, pid)
            if not process:
                kernel.CloseHandle(job)
                raise OSError(ctypes.get_last_error(), 'OpenProcess failed')
            try:
                if not kernel.AssignProcessToJobObject(job, process):
                    raise OSError(ctypes.get_last_error(), 'AssignProcessToJobObject failed')
            except Exception:
                kernel.CloseHandle(job)
                raise
            finally:
                kernel.CloseHandle(process)
            self.job, self.pid, self._kernel = job, pid, kernel
            try:
                if percent is not None:
                    self.update(percent)
            except Exception:
                self.close()
                raise
            return ('CPU上限なし（実行中変更可能）' if percent is None else f'CPU上限 {percent}%（Windows Job Object）')

    def update(self, percent: int | None) -> str:
        with self._lock:
            if not self.job:
                return '実行中のCPU制限対象はありません。'
            class CpuRate(ctypes.Structure):
                _fields_ = [('ControlFlags', ctypes.c_uint32), ('CpuRate', ctypes.c_uint32)]
            # JOB_OBJECT_CPU_RATE_CONTROL_ENABLE=0x1, HARD_CAP=0x4
            # Zero flags lifts the cap while leaving the process in the job.
            info = CpuRate(0 if percent is None else 0x5, 0 if percent is None else percent * 100)
            if not self._kernel.SetInformationJobObject(self.job, 15, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), 'SetInformationJobObject failed')
            return 'CPU制限なし' if percent is None else f'CPU上限を{percent}%に変更'

    def close(self):
        with self._lock:
            if self.job and self._kernel:
                self._kernel.CloseHandle(self.job)
            self.job = None
            self.pid = None
            self._kernel = None


def write_gpu_pacing(path: Path, percent: int | None) -> None:
    """Atomic update for child process polling between ASR chunks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'gpu_soft_percent': percent}, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)
