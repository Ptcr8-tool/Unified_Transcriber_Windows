"""Cancel only application-owned subprocesses, even when stdout is silent."""
from contextlib import contextmanager
import os
import subprocess
import threading
import time


def _windows_taskkill_path():
    """Resolve taskkill from the OS system directory, never from PATH."""
    import ctypes
    from pathlib import Path
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel32.GetSystemDirectoryW
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    fn.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    size = fn(buffer, len(buffer))
    if not size or size >= len(buffer):
        raise OSError(ctypes.get_last_error(), 'Windows system directory unavailable')
    return str(Path(buffer.value) / 'taskkill.exe')


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('処理を中止しました。')


def kill_owned_process(process):
    if process.poll() is not None:return
    try:
        if os.name=='nt':
            subprocess.run([_windows_taskkill_path(),'/PID',str(process.pid),'/T','/F'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if process.poll() is None:process.kill()
        else:
            process.terminate()
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:process.kill()
    except (OSError,subprocess.SubprocessError):
        try:process.kill()
        except OSError:pass


class ProcessRegistry:
    def __init__(self,cancel):
        self.cancel=cancel;self.lock=threading.Lock();self.children={}

    def add(self,process,*,recording=False):
        with self.lock:self.children[id(process)]=(process,recording)
        if self.cancel.is_set():
            threading.Thread(target=self._finish,args=(process,recording),daemon=True).start()
        return process

    def remove(self,process):
        with self.lock:self.children.pop(id(process),None)

    def active(self):
        with self.lock:return [item for item in self.children.values() if item[0].poll() is None]

    @contextmanager
    def process(self,command,*,recording=False,**kwargs):
        with subprocess.Popen(command,**kwargs) as process:
            self.add(process,recording=recording)
            try:yield process
            finally:self.remove(process)

    @staticmethod
    def _finish(process,recording):
        if recording:
            try:process.wait(timeout=10);return
            except subprocess.TimeoutExpired:pass
        kill_owned_process(process)

    def stop_all(self):
        self.cancel.set()
        for process,recording in self.active():
            threading.Thread(target=self._finish,args=(process,recording),daemon=True).start()


@contextmanager
def owned_process(owner,command,*,recording=False,**kwargs):
    registry=getattr(owner,'processes',None)
    if isinstance(registry,ProcessRegistry):
        with registry.process(command,recording=recording,**kwargs) as process:yield process
    else:
        with subprocess.Popen(command,**kwargs) as process:yield process


def run_cancellable(command,*,cancel=None,registry=None,timeout=None,**kwargs):
    registry=registry if isinstance(registry,ProcessRegistry) else None
    if cancel is None and registry is None:return subprocess.run(command,timeout=timeout,**kwargs)
    check_cancel(cancel)
    if kwargs.pop('capture_output',False):kwargs.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    started=time.monotonic()
    context=registry.process(command,**kwargs) if registry else subprocess.Popen(command,**kwargs)
    with context as process:
        try:
            while True:
                check_cancel(cancel)
                remaining=None if timeout is None else timeout-(time.monotonic()-started)
                if remaining is not None and remaining<=0:raise subprocess.TimeoutExpired(command,timeout)
                try:
                    stdout,stderr=process.communicate(timeout=min(.2,remaining) if remaining is not None else .2)
                    check_cancel(cancel)
                    return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
                except subprocess.TimeoutExpired:
                    if timeout is not None and time.monotonic()-started>=timeout:raise
        except BaseException:
            kill_owned_process(process);process.communicate();raise

