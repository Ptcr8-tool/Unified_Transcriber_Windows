"""Build a conventional, pinned NSIS bootstrap for the existing setup wizard.

The bootstrap only unpacks its own bundled runtime and starts a fixed Python
entry point. It never runs a shell, downloads application code, or implements
installation policy. Installation receipts, transaction/rollback protection and
preservation of user data remain the maintenance wizard's responsibility.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

NSIS_VERSION = '3.13'
NSIS_URL = 'https://downloads.sourceforge.net/project/nsis/NSIS%203/3.13/nsis-3.13.zip'
NSIS_SHA256 = 'ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8'
NSIS_BYTES = 2_363_925
NSIS_IDENTITY = {'name': 'NSIS', 'version': NSIS_VERSION,
                 'archive_sha256': NSIS_SHA256, 'compression': 'zlib',
                 'bootstrap': 'bundled Python; setup wizard only'}
_ROOT = 'nsis-' + NSIS_VERSION
_SCRIPT = Path(__file__).with_name('installer_bootstrap.nsi')
_RESERVED = frozenset(('CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)),
                       *(f'LPT{i}' for i in range(10))))


def digest(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ordinary(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Build inputs cannot be links or reparse points: ' + str(path))
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Build input is not an ordinary ' + ('directory: ' if directory else 'file: ') + str(path))


def _allowed_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.fragment
            or parsed.port not in (None, 443)
            or not (host == 'downloads.sourceforge.net' or host.endswith('.dl.sourceforge.net'))
            or urllib.parse.unquote(parsed.path) != '/project/nsis/NSIS 3/3.13/nsis-3.13.zip'):
        raise ValueError('NSIS download must use the pinned official SourceForge project archive.')


class _PinnedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, request, response, code, message, headers, new_url):
        _allowed_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def download_nsis(target: Path) -> None:
    """Verify exact bytes before any compiler or plugin can be executed."""
    if target.exists():
        ordinary(target)
        if target.stat().st_size == NSIS_BYTES and digest(target) == NSIS_SHA256:
            return
        raise ValueError('Cached NSIS archive differs from the pinned release.')
    target.parent.mkdir(parents=True, exist_ok=True)
    _allowed_url(NSIS_URL)
    descriptor, name = tempfile.mkstemp(prefix='nsis-download-', suffix='.partial', dir=target.parent)
    partial = Path(name)
    deadline = time.monotonic() + 120
    try:
        request = urllib.request.Request(NSIS_URL, headers={
            'User-Agent': 'UnifiedTranscriberBuild/1', 'Accept-Encoding': 'identity'})
        opener = urllib.request.build_opener(_PinnedRedirects())
        with os.fdopen(descriptor, 'wb') as output, opener.open(request, timeout=30) as response:
            _allowed_url(response.url)
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('NSIS server returned an unsupported response.')
            advertised = response.headers.get('Content-Length')
            if advertised is not None and advertised != str(NSIS_BYTES):
                raise ValueError('NSIS server returned an unexpected archive size.')
            total = 0
            while block := response.read(128 * 1024):
                total += len(block)
                if total > NSIS_BYTES or time.monotonic() > deadline:
                    raise ValueError('NSIS download exceeded its size/time bound.')
                output.write(block)
        if (time.monotonic() > deadline or partial.stat().st_size != NSIS_BYTES
                or digest(partial) != NSIS_SHA256):
            raise ValueError('NSIS archive failed its pinned SHA-256 verification.')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def safe_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Validate every name and type, even entries not subsequently executed."""
    result = []
    seen = set()
    total = 0
    for item in archive.infolist():
        name = item.filename
        if (name != item.orig_filename or '\\' in name or '\x00' in name
                or ':' in name or name.startswith('/') or name.endswith('//') or item.flag_bits & 1):
            raise ValueError('Invalid NSIS ZIP member name or encryption.')
        parts = name.rstrip('/').split('/')
        if (not parts or parts[0] != _ROOT or any(not part or part in ('.', '..')
                or part.endswith((' ', '.')) or part.split('.', 1)[0].upper() in _RESERVED
                or any(ord(char) < 32 or char in '<>"|?*' for char in part) for part in parts)):
            raise ValueError('Unsafe NSIS ZIP member path.')
        folded = '/'.join(parts).casefold()
        if folded in seen:
            raise ValueError('Case-conflicting or duplicate NSIS ZIP member.')
        seen.add(folded)
        mode = item.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFDIR, stat.S_IFREG):
            raise ValueError('NSIS ZIP cannot contain symlinks or special files.')
        total += item.file_size
        if (len(result) >= 2_000 or item.file_size > 16 * 1024 * 1024
                or total > 32 * 1024 * 1024):
            raise ValueError('NSIS ZIP exceeds its expansion limit.')
        result.append((item, PurePosixPath(*parts)))
    files = {path.as_posix() for item, path in result if not item.is_dir()}
    required = {_ROOT + '/Bin/makensis.exe', _ROOT + '/Include/LogicLib.nsh',
                _ROOT + '/Plugins/x86-unicode/System.dll', _ROOT + '/COPYING'}
    if not required <= files:
        raise ValueError('NSIS ZIP is missing required compiler resources.')
    # A file cannot also be the ancestor of another member.
    folded_files = {path.casefold() for path in files}
    for _item, path in result:
        if any(parent.as_posix().casefold() in folded_files for parent in path.parents if parent.parts):
            raise ValueError('NSIS ZIP contains a file/directory path collision.')
    return result


def extract_nsis(archive: Path, destination: Path) -> Path:
    ordinary(archive)
    if archive.stat().st_size != NSIS_BYTES or digest(archive) != NSIS_SHA256:
        raise ValueError('Only the exact pinned NSIS release may be extracted.')
    if destination.exists():
        raise ValueError('Use a new NSIS compiler staging directory.')
    with zipfile.ZipFile(archive) as source:
        members = safe_members(source)
        destination.mkdir(parents=True)
        try:
            for item, relative in members:
                output = destination.joinpath(*relative.parts)
                if item.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as input_file, output.open('xb') as output_file:
                    shutil.copyfileobj(input_file, output_file)
            compiler = destination / _ROOT / 'Bin/makensis.exe'
            ordinary(compiler)
            return compiler
        except BaseException:
            shutil.rmtree(destination)
            raise


def copy_nsis_license(archive: Path, destination: Path) -> None:
    """Keep the exact upstream notice available after the installer exits."""
    ordinary(archive)
    if archive.stat().st_size != NSIS_BYTES or digest(archive) != NSIS_SHA256:
        raise ValueError('Only the exact pinned NSIS release may provide its license.')
    with zipfile.ZipFile(archive) as source:
        members = safe_members(source)
        entries = [item for item, path in members
                   if not item.is_dir() and path.as_posix() == _ROOT + '/COPYING']
        if len(entries) != 1 or not 0 < entries[0].file_size <= 128 * 1024:
            raise ValueError('Pinned NSIS release lacks one valid license file.')
        content = source.read(entries[0])
        if not content.strip():
            raise ValueError('The NSIS license file is empty.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as output:
        output.write(content)


def copy_tree(source: Path, target: Path) -> None:
    ordinary(source, directory=True)
    target.mkdir(parents=True)
    for path in sorted(source.iterdir()):
        if path.is_dir():
            ordinary(path, directory=True)
            copy_tree(path, target / path.name)
        else:
            ordinary(path)
            shutil.copyfile(path, target / path.name)


def stage_bootstrap(*, runtime: Path, archive: Path, info: Path, code: Path,
                    target: Path, version: str) -> Path:
    """Stage sanitized program inputs; never copy a user's install directory."""
    if target.exists():
        raise ValueError('Use a new bootstrap staging directory.')
    for source in (archive, info, runtime / 'pythonw.exe', code / 'installer_bootstrap.py'):
        ordinary(source)
    ordinary(code, directory=True)
    metadata = json.loads(info.read_text(encoding='utf-8'))
    if (not isinstance(metadata, dict) or metadata.get('version') != version
            or metadata.get('payload_size') != archive.stat().st_size
            or metadata.get('payload_sha256') != digest(archive)):
        raise ValueError('Bootstrap payload and build information do not agree.')
    target.mkdir(parents=True)
    copy_tree(runtime, target / 'Runtime')
    internal = target / '_internal'
    internal.mkdir()
    # The root Python modules form a small dependency closure. Copying them all
    # avoids dynamic GUI/config import omissions while excluding environments,
    # models, logs, tests, update-verifier binaries and developer tools.
    for source in sorted(code.iterdir()):
        if source.suffix.lower() in ('.py', '.json') and source.is_file():
            ordinary(source)
            shutil.copyfile(source, internal / source.name)
    copy_tree(code / 'assets/icons', internal / 'assets/icons')
    shutil.copyfile(archive, target / 'payload.zip')
    shutil.copyfile(info, target / 'BUILD_INFO.json')
    return target


def nsis_literal(path: Path) -> str:
    value = str(path.absolute())
    if (any(char in value for char in ('\x00', '\r', '\n', '"'))
            or '${' in value or '$%' in value):
        raise ValueError('Build paths cannot contain quotes, control characters or preprocessor expressions.')
    # These paths appear only in compile-time commands (File, Icon, OutFile,
    # !include). Unlike runtime NSIS strings, those commands treat $ literally.
    return value


def inspect_bootstrap(path: Path) -> dict:
    """Check the real output's conventional GUI PE and NSIS container header."""
    ordinary(path)
    with path.open('rb') as stream:
        prefix = stream.read(256 * 1024)
        stream.seek(-min(path.stat().st_size, 512), os.SEEK_END)
        tail = stream.read()
    try:
        pe = struct.unpack_from('<I', prefix, 60)[0]
        machine = struct.unpack_from('<H', prefix, pe + 4)[0]
        optional = pe + 24
        magic = struct.unpack_from('<H', prefix, optional)[0]
        subsystem = struct.unpack_from('<H', prefix, optional + 68)[0]
        cert_offset, cert_size = struct.unpack_from('<II', prefix, optional + 96 + 4 * 8)
    except struct.error as exc:
        raise ValueError('NSIS output does not have a complete PE header.') from exc
    pyi = b'MEI\x0c\x0b\x0a\x0b\x0e' in tail
    marker = b'NullsoftInst' in prefix
    if (prefix[:2] != b'MZ' or prefix[pe:pe + 4] != b'PE\0\0' or machine != 0x014c
            or magic != 0x010b or subsystem != 2 or not marker or pyi):
        raise ValueError('Setup must be the standard NSIS GUI bootstrap, without a PyInstaller container.')
    return {'format': 'NSIS', 'pe_machine': '0x014c', 'subsystem': 'windows-gui',
            'security_directory': {'offset': cert_offset, 'size': cert_size},
            'pyinstaller_onefile': pyi, 'standard_nsis_marker': marker}


def build_installer(*, runtime: Path, archive: Path, info: Path, code: Path,
                    work: Path, output: Path, version: str, icon: Path, run=None) -> Path:
    """Return the one executable users download; compile only pinned tooling."""
    if os.name != 'nt':
        raise RuntimeError('Build the NSIS distribution on the Windows x64 build runner.')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('A stable numeric application version is required.')
    ordinary(icon)
    archive_path = work / 'downloads' / ('nsis-' + NSIS_VERSION + '.zip')
    download_nsis(archive_path)
    compiler = extract_nsis(archive_path, work / 'nsis-compiler')
    staging = stage_bootstrap(runtime=runtime, archive=archive, info=info, code=code,
                              target=work / 'bootstrap', version=version)
    shutil.copyfile(compiler.parent.parent / 'COPYING', staging / '_internal/NSIS-LICENSE.txt')
    output.mkdir(parents=True, exist_ok=True)
    executable = output / ('Unified_Transcriber_Setup_v' + version + '.exe')
    if executable.exists():
        raise ValueError('The setup output already exists; use a fresh output directory.')
    driver = work / 'build-nsis.nsi'
    definitions = {'VERSION': version, 'STAGING': nsis_literal(staging),
                   'OUTPUT': nsis_literal(executable), 'APPICON': nsis_literal(icon)}
    driver.write_text(''.join(f'!define {name} "{value}"\n' for name, value in definitions.items())
                      + '!include "' + nsis_literal(_SCRIPT) + '"\n', encoding='utf-8-sig')
    environment = dict(os.environ)
    for key in tuple(environment):
        upper = key.upper()
        if (upper.startswith(('NSIS', 'PYTHON', 'PIP_', 'GH_', 'GITHUB_', 'ACTIONS_', '_PYI_'))
                or upper in ('HF_TOKEN', 'TCL_LIBRARY', 'TK_LIBRARY', 'TCLLIBPATH', '_MEIPASS2')
                or upper.endswith('_PROXY')):
            environment.pop(key)
    environment['NSISDIR'] = str(compiler.parent.parent)
    command = [str(compiler), '/NOCONFIG', '/INPUTCHARSET', 'UTF8', '/V3', str(driver)]
    if run is None:
        subprocess.run(command, env=environment, shell=False, check=True, timeout=900)
    else:
        run(command, env=environment, timeout=900)
    ordinary(executable)
    if executable.stat().st_size < archive.stat().st_size:
        raise RuntimeError('NSIS did not produce the expected standard bootstrap executable.')
    inspect_bootstrap(executable)
    return executable
