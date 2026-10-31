"""Native launcher integration probes using the actual installed executable.

Only a fresh fixture below the smoke-test sandbox is changed. Its application
module is a probe; its launcher, isolated entry and Python runtime are the real
installed files. The installed application itself is never patched by probes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess

from native_launcher import verify_native_launcher


PROBE = '''import ctypes, json, os, sys
from ctypes import wintypes
from pathlib import Path
def main():
    inherited = False
    raw = os.environ.get('UNIFIED_NATIVE_LEAK_HANDLE')
    if raw:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD)
        kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        path = ctypes.create_unicode_buffer(32768)
        count = kernel.GetFinalPathNameByHandleW(int(raw),path,len(path),0)
        inherited = bool(count and path.value.endswith(os.environ['UNIFIED_NATIVE_LEAK_FILE']))
    result = {'argv':sys.argv[1:], 'cwd':str(Path.cwd()), 'python':sys.executable,
              'isolated':sys.flags.isolated,'leaked_handle':inherited,
              'unsafe_env':[key for key in os.environ if key.upper() in (
                  'PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','PIP_INDEX_URL',
                  'TCL_LIBRARY','TK_LIBRARY','_PYI_ARCHIVE_FILE','_MEIPASS2')]}
    Path(os.environ['UNIFIED_NATIVE_PROBE_REPORT']).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 37
'''


def check_native_launcher(target: Path, sandbox: Path) -> dict:
    if os.name != 'nt':
        raise RuntimeError('The native launcher smoke probes require Windows.')
    executable = Path(target) / 'UnifiedTranscriber.exe'
    identity = verify_native_launcher(executable)
    fixture = Path(sandbox) / 'Native launcher 日本語 & paths'
    fixture.mkdir()
    shutil.copyfile(executable, fixture / executable.name)
    shutil.copytree(Path(target) / 'Runtime', fixture / 'Runtime')
    code = fixture / '_internal'
    code.mkdir()
    for name in ('native_app_entry.py', 'portable_runtime.py'):
        shutil.copyfile(Path(target) / '_internal' / name, code / name)
    (code / 'app_launcher.py').write_text(PROBE, encoding='utf-8')
    hostile = fixture / 'untrusted current directory'
    hostile.mkdir()
    for name in ('app_launcher.py', 'portable_runtime.py', 'sitecustomize.py'):
        (hostile / name).write_text('raise RuntimeError("Untrusted module executed")\n', encoding='utf-8')
    report = fixture / 'native report 日本語.json'
    marker = fixture / 'unrelated-inheritable-handle.txt'
    marker.write_text('This unrelated file handle must never reach Python.', encoding='utf-8')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Security(ctypes.Structure):
        _fields_ = [('nLength',wintypes.DWORD),('lpSecurityDescriptor',wintypes.LPVOID),
                    ('bInheritHandle',wintypes.BOOL)]
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                                  ctypes.POINTER(Security),wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    security = Security(ctypes.sizeof(Security),None,True)
    handle = kernel.CreateFileW(str(marker),0x80000000,3,ctypes.byref(security),3,0x80,None)
    if handle in (None,ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(),'Cannot create the inherited-handle isolation probe.')
    environment = dict(os.environ)
    environment.update(PYTHONPATH=str(hostile),PYTHONHOME=str(hostile),
        PYTHONSTARTUP=str(hostile/'sitecustomize.py'),PIP_INDEX_URL='http://untrusted.invalid',
        TCL_LIBRARY=str(hostile),TK_LIBRARY=str(hostile),
        _PYI_ARCHIVE_FILE=str(hostile/'old.exe'),_MEIPASS2=str(hostile),
        UNIFIED_NATIVE_PROBE_REPORT=str(report),UNIFIED_NATIVE_LEAK_HANDLE=str(handle),
        UNIFIED_NATIVE_LEAK_FILE=marker.name)
    arguments = ['--native-probe','space 日本語 & file','','trailing\\',
                 'double"quote',r'backslash\"quote',r'\\server\folder\\']
    try:
        # The probe intentionally passes an unrelated inheritable handle to the
        # launcher. Its restricted handle list must not pass that handle on.
        process = subprocess.run([str(fixture/executable.name),*arguments],cwd=hostile,
            env=environment,close_fds=False,capture_output=True,text=True,encoding='utf-8',
            errors='strict',timeout=60)
        if process.returncode != 37:
            raise RuntimeError('Native diagnostic exit code changed: '+process.stderr)
        result = json.loads(report.read_text(encoding='utf-8'))
        if result != json.loads(process.stdout):
            raise RuntimeError('Native diagnostic stdout differs from the child report.')
        if (result['argv'] != arguments or result['isolated'] != 1
                or result['unsafe_env'] or result['leaked_handle']
                or Path(result['cwd']).resolve() != fixture.resolve()
                or Path(result['python']).resolve() != (fixture/'Runtime/python.exe').resolve()):
            raise RuntimeError('Native launcher argument, interpreter, environment or handle isolation failed: '+repr(result))
        # A normal GUI launch uses pythonw and does not require a console or
        # carry any inherited handles. The owned fixture writes its own proof.
        report.unlink()
        normal = subprocess.run([str(fixture/executable.name)],cwd=hostile,env=environment,
            close_fds=False,capture_output=True,timeout=60)
        normal_result = json.loads(report.read_text(encoding='utf-8'))
        if (normal.returncode != 37 or normal_result['argv']
                or normal_result['leaked_handle'] or normal_result['unsafe_env']
                or Path(normal_result['python']).resolve() != (fixture/'Runtime/pythonw.exe').resolve()):
            raise RuntimeError('Normal native GUI startup did not use the isolated pythonw runtime.')
        report.unlink()
        background = subprocess.run([str(fixture/executable.name), '--background'],cwd=hostile,
            env=environment,close_fds=False,capture_output=True,timeout=60)
        background_result = json.loads(report.read_text(encoding='utf-8'))
        if (background.returncode != 37 or background_result['argv'] != ['--background']
                or background_result['leaked_handle'] or background_result['unsafe_env']
                or Path(background_result['python']).resolve() != (fixture/'Runtime/pythonw.exe').resolve()):
            raise RuntimeError('Background startup did not use the isolated pythonw runtime.')
        # Diagnostic errors must fail promptly rather than opening a blocking
        # dialog or searching PATH for another installed interpreter.
        interpreter = fixture/'Runtime/python.exe'
        missing = fixture/'Runtime/temporarily-absent-python.exe'
        interpreter.rename(missing)
        try:
            absent = subprocess.run([str(fixture/executable.name),'--version'],cwd=hostile,
                env=environment,capture_output=True,text=True,encoding='utf-8',timeout=15)
            if absent.returncode != 1 or '修復' not in absent.stderr:
                raise RuntimeError('Missing bundled Python did not fail with a repair diagnostic.')
        finally:
            missing.rename(interpreter)
    finally:
        kernel.CloseHandle(handle)
    return {**identity, 'argument_roundtrip':True,'isolated_interpreter':True,
            'hostile_current_directory':True,'environment_cleanup':True,
            'unrelated_handle_not_inherited':True,'diagnostic_stdout_and_exit_code':True,
            'normal_gui_pythonw':True,'background_gui_pythonw':True,'missing_runtime_fails_closed':True}
