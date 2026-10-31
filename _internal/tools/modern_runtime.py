"""Bundle an authenticated PSF runtime for the current Windows ML wheels.

The GUI and legacy engines retain their independently serviced runtime. This
second runtime is installed as application data, never registered as a global
Python installation. All code arrives in the user's one setup executable.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile
import zipfile
import xml.etree.ElementTree as ET

LOCK = Path(__file__).with_name('modern-python-sources.json')
REQUIRED = frozenset({'python.exe', 'pythonw.exe', 'python314.dll', 'LICENSE.txt',
    'DLLs/_ssl.pyd', 'DLLs/libcrypto-3.dll', 'DLLs/libssl-3.dll',
    'Lib/venv/__init__.py', 'Lib/ensurepip/__init__.py',
    'Lib/ensurepip/_bundled/pip-26.2.2-py3-none-any.whl'})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def runtime_members(source: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    """Validate every archive name/type before writing any runtime member."""
    selected = []
    seen = set()
    total = 0
    entries = source.infolist()
    if len(entries) > 5000:
        raise ValueError('Too many files in the managed Python package.')
    for entry in entries:
        name = entry.filename
        # ZipInfo normalizes Windows separators and truncates NUL bytes.
        # Validate the original central-directory name before trusting that
        # normalized view; otherwise a malformed path can look ordinary.
        if entry.orig_filename != name or '\0' in name:
            raise ValueError('Normalized managed Python archive paths are forbidden.')
        path = PurePosixPath(name.rstrip('/'))
        if (not name or '\\' in name or path.is_absolute()
                or path.as_posix() != name.rstrip('/')
                or any(part in ('.', '..') or ':' in part
                       or part.rstrip(' .') != part for part in path.parts)):
            raise ValueError('Unsafe managed Python package path.')
        key = path.as_posix().casefold()
        if key in seen:
            raise ValueError('Duplicate or case-conflicting managed Python path.')
        seen.add(key)
        mode = entry.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError('Links and special files are forbidden in managed Python.')
        if entry.flag_bits & 1:
            raise ValueError('Encrypted managed Python package member.')
        total += entry.file_size
        if entry.file_size > 30_000_000 or total > 200_000_000:
            raise ValueError('Managed Python expanded size exceeds its limit.')
        if path.parts[0] != 'tools' or len(path.parts) < 2 or entry.is_dir():
            continue
        relative = Path(*path.parts[1:])
        # The PSF NuGet distribution is a complete interpreter, not an
        # embeddable ._pth layout. Retain venv/site support intentionally.
        if relative.suffix.casefold() == '._pth':
            raise ValueError('A restricted embeddable Python cannot create ML environments.')
        selected.append((entry, relative))
    names = {relative.as_posix() for _, relative in selected}
    if not REQUIRED <= names:
        raise ValueError('The managed Python package is missing runtime/venv/SSL files.')
    metadata = source.read('python.nuspec')
    if len(metadata) > 16384:
        raise ValueError('Managed Python metadata is unexpectedly large.')
    root = ET.fromstring(metadata)
    ns = {'n': 'http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd'}
    fields = {name: root.findtext('n:metadata/n:' + name, namespaces=ns)
              for name in ('id', 'version', 'authors')}
    version = json.loads(LOCK.read_text(encoding='utf-8'))['python']['version']
    if fields != {'id': 'python', 'version': version, 'authors': 'Python Software Foundation'}:
        raise ValueError('Managed Python package identity does not match the pinned PSF release.')
    return selected


def unpack_runtime(archive: Path, target: Path, entry: dict) -> None:
    if target.exists():
        raise ValueError('The managed runtime must use a fresh directory.')
    if archive.stat().st_size != entry['size'] or sha256(archive) != entry['sha256']:
        raise ValueError('Managed Python archive identity mismatch.')
    with zipfile.ZipFile(archive) as source:
        selected = runtime_members(source)
        target.mkdir(parents=True)
        for member, relative in selected:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as reader, destination.open('xb') as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            if destination.stat().st_size != member.file_size:
                raise ValueError('Incomplete managed Python package member.')
    for name in ('python.exe', 'pythonw.exe', 'python314.dll'):
        with (target / name).open('rb') as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b'MZ':
                raise ValueError('Managed Python binary is not a PE executable.')
            stream.seek(struct.unpack_from('<I', header, 60)[0])
            if stream.read(6) != b'PE\0\0d\x86':
                raise ValueError('Managed Python must be a Windows x64 runtime.')


def verify_publisher(target: Path, *, run, clean_env) -> None:
    """Check the actual PSF executables before executing the downloaded runtime."""
    script = r'''
    $ErrorActionPreference = 'Stop'
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
    $paths = ConvertFrom-Json -InputObject $env:UNIFIED_PSF_BINARIES
    foreach ($path in $paths) {
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Python Software Foundation(,|$)') {
            throw ('The pinned PSF interpreter signature is invalid: {0}; {1}' -f $path,$signature.Status)
        }
    }
    '''
    environment = clean_env()
    environment.pop('PSModulePath', None)
    environment['UNIFIED_PSF_BINARIES'] = json.dumps([
        str(target / name) for name in ('python.exe', 'pythonw.exe', 'python314.dll')])
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    run([shell, '-NoProfile', '-NonInteractive', '-Command', script], env=environment, timeout=120)


def probe_runtime(target: Path, work: Path, *, run, clean_env) -> dict:
    """Use only bundled Python/CRT with hostile configuration removed."""
    environment = clean_env()
    for key in tuple(environment):
        if key.upper().startswith(('PYTHON', 'PIP_')) or key.upper() in ('VIRTUAL_ENV', 'TCL_LIBRARY', 'TK_LIBRARY'):
            environment.pop(key, None)
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    environment['PATH'] = os.pathsep.join((str(target), str(system / 'System32'), str(system)))
    probe = ('import sys,ssl,venv,ensurepip,json,sqlite3,lzma,bz2,zlib,pyexpat,ctypes; '
        'ctx=ssl.create_default_context(); '
        'assert ctx.check_hostname and ctx.verify_mode==ssl.CERT_REQUIRED; '
        'assert lzma.decompress(lzma.compress(b"sample"))==b"sample"; '
        'assert bz2.decompress(bz2.compress(b"sample"))==b"sample"; '
        'callback=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_int)(lambda n:n+1); assert callback(4)==5; '
        'import _lzma; library=ctypes.CDLL(_lzma.__file__); '
        'library.lzma_version_string.restype=ctypes.c_char_p; '
        'xz=library.lzma_version_string().decode("ascii"); assert xz=="5.8.4"; '
        'assert ssl.OPENSSL_VERSION_INFO== (3,5,0,9,0); '
        'assert sqlite3.sqlite_version=="3.53.4"; assert pyexpat.EXPAT_VERSION=="expat_2.9.0"; '
        'assert zlib.ZLIBNG_VERSION=="2.2.4"; '
        'print(json.dumps({"version":sys.version.split()[0],"prefix":sys.prefix,'
        '"executable":sys.executable,"openssl":ssl.OPENSSL_VERSION,"sqlite":sqlite3.sqlite_version,'
        '"zlib":zlib.ZLIB_RUNTIME_VERSION,"zlib_ng":zlib.ZLIBNG_VERSION,'
        '"expat":pyexpat.EXPAT_VERSION,"xz":xz,"pip":ensurepip.version()}))')
    result = run([target / 'python.exe', '-I', '-B', '-c', probe], capture=True, env=environment)
    identity = json.loads(result.stdout.strip())
    version = json.loads(LOCK.read_text(encoding='utf-8'))['python']['version']
    if identity.get('version') != version or Path(identity.pop('prefix')).resolve() != target.resolve():
        raise RuntimeError('The managed Python interpreter did not report its own version/location.')
    identity.pop('executable', None)
    with tempfile.TemporaryDirectory(prefix='modern-venv-', dir=work) as temporary:
        venv = Path(temporary) / 'probe'
        run([target / 'python.exe', '-I', '-B', '-m', 'venv', str(venv)], env=environment, timeout=180)
        run([venv / 'Scripts/python.exe', '-I', '-B', '-m', 'pip', '--version'], capture=True, env=environment)
        run([venv / 'Scripts/python.exe', '-I', '-B', '-c',
             'import sys,ssl; assert sys.version_info[:2]==(3,14); '
             'assert sys.prefix!=sys.base_prefix; '
             'assert ssl.create_default_context().check_hostname'], env=environment)
    identity['isolated_venv_probe'] = 'passed with bundled interpreter and OS-only PATH'
    return identity


def prepare_runtime(payload: Path, work: Path, *, fetch, run, clean_env, bundle_crt) -> dict:
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    entry = lock['python']
    archive = work / 'downloads/python.3.14.10.nupkg'
    fetch(entry, archive)
    target = payload / 'MLRuntime'
    unpack_runtime(archive, target, entry)
    verify_publisher(target, run=run, clean_env=clean_env)
    crt = bundle_crt(target)
    from modern_native import service_native
    native = service_native(target, payload, work, lock, fetch=fetch, run=run, clean_env=clean_env)
    identity = probe_runtime(target, work, run=run, clean_env=clean_env)
    identity.update(source=lock, visual_cpp_runtime=crt,
        native_components=native,
        signature_verification='Windows Authenticode: Valid; Python Software Foundation',
        profiles=['faster-res', 'live-capture'])
    (target / 'UNIFIED_RUNTIME_IDENTITY.json').write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return identity
