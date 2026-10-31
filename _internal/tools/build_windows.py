"""Build the Windows setup executable on a clean Windows x64 runner.

The user downloads one executable. It carries the app, a relocatable full
CPython runtime, an offline drag/drop wheel, and the pinned update verifier.
The application uses a native Win32 launcher and the setup uses standard NSIS.
CI test dependencies are installed separately after the runtime snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

REPO = Path(__file__).resolve().parents[2]
CODE = REPO / '_internal'
SOURCES = Path(__file__).with_name('windows-build-sources.json')
PRODUCT = 'UnifiedTranscriber'
REPOSITORY = 'Ptcr8-tool/Unified_Transcriber_Windows'
NETWORK_HOSTS = frozenset({'github.com', 'release-assets.githubusercontent.com',
    'objects.githubusercontent.com', 'objects-origin.githubusercontent.com',
    'files.pythonhosted.org', 'codeload.github.com', 'www.python.org',
    'www.nuget.org', 'globalcdn.nuget.org'})


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def clean_environment() -> dict[str, str]:
    """Do not pass CI credentials or an ambient Python/pip config to payload tools."""
    env = dict(os.environ)
    for key in tuple(env):
        upper = key.upper()
        if (upper in {'GH_TOKEN', 'GITHUB_TOKEN', 'HF_TOKEN', 'GH_ENTERPRISE_TOKEN',
                      'GITHUB_ENTERPRISE_TOKEN', 'GH_DEBUG', 'PYTHONHOME', 'PYTHONPATH'}
                or upper.endswith('_PROXY') or upper.startswith(('PIP_', 'ACTIONS_', 'GITHUB_', 'GH_'))):
            env.pop(key, None)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
               GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
    return env


def run(args, *, env=None, timeout=300, capture=False):
    try:
        return subprocess.run([str(arg) for arg in args], check=True,
            env=clean_environment() if env is None else env, timeout=timeout,
            capture_output=capture, text=capture, encoding='utf-8' if capture else None,
            errors='replace' if capture else None)
    except subprocess.CalledProcessError as exc:
        if not capture:
            raise
        raise RuntimeError(f'{Path(str(args[0])).name} exited with status {exc.returncode}.\n'
            + 'stdout:\n' + (exc.stdout or '')[-12000:]
            + '\nstderr:\n' + (exc.stderr or '')[-12000:]) from exc


def require_windows() -> None:
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('Build on Windows x64; PyInstaller does not cross-compile.')
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError('Build with the latest supported CPython 3.11 x64 runtime.')


def work_folder(raw: str) -> Path:
    path = Path(raw).resolve()
    if path == REPO or path in REPO.parents:
        raise ValueError('The build work directory must not replace the source tree.')
    path.mkdir(parents=True, exist_ok=True)
    return path


CRT_REQUIRED = frozenset({'concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll',
    'msvcp140_2.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'})


def find_visual_cpp_runtime() -> Path:
    """Discover VS year/major-version layouts only inside official SDK roots."""
    defaults = {'ProgramW6432':r'C:\Program Files', 'ProgramFiles':r'C:\Program Files',
                'ProgramFiles(x86)':r'C:\Program Files (x86)'}
    roots = list(dict.fromkeys(Path(os.environ.get(name, default))/'Microsoft Visual Studio'
                              for name, default in defaults.items()))
    candidates = []
    diagnostics = []
    for base in roots:
        if not base.is_dir():
            diagnostics.append(str(base) + ': not installed')
            continue
        products = sorted(path for path in base.iterdir()
                          if path.is_dir() and re.fullmatch(r'\d{2,4}', path.name))
        diagnostics.append(str(base) + ': product directories ' + ', '.join(p.name for p in products))
        for product in products:
            for edition in ('Enterprise', 'Professional', 'Community', 'BuildTools'):
                redist = product/edition/'VC/Redist/MSVC'
                if not redist.is_dir():
                    continue
                versions = sorted(redist.iterdir())
                diagnostics.append(str(redist) + ': SDK directories ' + ', '.join(p.name for p in versions[:20]))
                for version in versions:
                    # VS2026 uses product major "18"; some SDKs expose only
                    # the official v143/v145 alias instead of a numeric folder.
                    if re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', version.name):
                        rank = tuple(map(int, version.name.split('.')))
                    elif re.fullmatch(r'v14\d', version.name):
                        rank = (0, int(version.name[1:]))
                    else:
                        continue
                    for crt in (version/'x64').glob('Microsoft.VC*.CRT'):
                        if (not crt.is_dir() or crt.is_symlink()
                                or not crt.resolve().is_relative_to(base.resolve())):
                            continue
                        names = {path.name.lower() for path in crt.glob('*.dll') if path.is_file()}
                        missing = sorted(CRT_REQUIRED - names)
                        if missing:
                            diagnostics.append(str(crt) + ': missing ' + ', '.join(missing))
                            continue
                        candidates.append((rank, crt))
    if not candidates:
        raise RuntimeError('An official Visual Studio x64 CRT redistributable directory is required.\n'
                           + '\n'.join(diagnostics[:30]))
    source = max(candidates, key=lambda entry: entry[0])[1]
    print('Visual Studio x64 CRT source:', source)
    return source


def bundle_visual_cpp_runtime(target: Path) -> dict:
    """Use Microsoft-signed x64 redistributables from the installed VS SDK.

    Never harvest DLLs from System32 or execute a downloaded redist installer.
    App-local files are serviced with this application's runtime updates.
    """
    source = find_visual_cpp_runtime()
    files = sorted(source.glob('*.dll'))
    for path in files:
        if path.is_symlink() or not path.is_file() or path.resolve().parent != source.resolve():
            raise ValueError('Visual Studio redist DLL must be an ordinary file.')
        with path.open('rb') as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:2] != b'MZ':
                raise ValueError('Invalid CRT executable header.')
            handle.seek(struct.unpack_from('<I', header, 60)[0])
            if handle.read(6) != b'PE\0\0d\x86':
                raise ValueError('The CRT redistributable must be an x64 PE binary.')
    # This is a fixed build-only script. Paths are JSON data in an environment
    # variable, never interpolated into PowerShell source or a shell command.
    script = r'''
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
    $paths = ConvertFrom-Json -InputObject $env:UNIFIED_CRT_FILES
    $versions = @()
    foreach ($path in $paths) {
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') {
            throw ('A Visual Studio runtime DLL does not have a valid Microsoft signature. Path={0}; Status={1}; Message={2}; Subject={3}' -f $path,$signature.Status,$signature.StatusMessage,$signature.SignerCertificate.Subject)
        }
        $versions += [PSCustomObject]@{name=[IO.Path]::GetFileName($path); version=[Diagnostics.FileVersionInfo]::GetVersionInfo($path).FileVersion}
    }
    ConvertTo-Json -InputObject @($versions) -Compress
    '''
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = clean_environment()
    # The workflow uses pwsh 7; its module search path can select incompatible
    # modules in Windows PowerShell 5. Load only that engine's own built-ins.
    for key in tuple(env):
        if key.upper() in ('PSMODULEPATH', 'PSMODULEANALYSISCACHEPATH'):
            env.pop(key)
    env['UNIFIED_CRT_FILES'] = json.dumps([str(path) for path in files])
    verified = run([shell, '-NoProfile', '-NonInteractive', '-Command', script],
                   env=env, timeout=180, capture=True)
    versions = {item['name']:item['version'] for item in json.loads(verified.stdout.strip())}
    inventory = []
    for path in files:
        destination = target/path.name
        shutil.copyfile(path, destination)
        inventory.append({'name':path.name, 'version':versions[path.name], 'sha256':digest(destination)})
    notice = ('Microsoft Visual C++ Runtime (x64)\nCopyright Microsoft Corporation.\n'
        'Redistributable files from the licensed Visual Studio installation on the build runner.\n'
        'Microsoft Software License Terms and the Visual Studio REDIST list apply.\n'
        'https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution\n'
        'https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution\n'
        'https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files\n'
        'App-local copies are updated together with this application; no system-wide installation is performed.\n')
    (target/'MICROSOFT_CRT_NOTICE.txt').write_text(notice, encoding='utf-8')
    return {'source':'Visual Studio VC/Redist/MSVC/' + source.parent.parent.name + '/x64/' + source.name,
            'signature_verification':'Windows Authenticode: Valid; Microsoft Corporation', 'files':inventory}


def validate_runtime_versions(identity: dict) -> None:
    """Compare the loaded libraries, including OpenSSL's five-part tuple."""
    expected = {'version':'3.11.18', 'openssl_version_info':[3,5,0,9,0],
                'openssl_version_number':'0x30500090', 'sqlite':'3.53.4',
                'tk':'8.6.18', 'zlib':'1.3.2'}
    for name,value in expected.items():
        if identity.get(name) != value:
            raise RuntimeError(f'Bundled runtime {name} mismatch: expected {value!r}, '
                               f'loaded {identity.get(name)!r}.')


def snapshot_runtime(work: Path) -> None:
    require_windows()
    target = work / 'Runtime'
    if target.exists():
        raise ValueError('Use a new work directory for each runtime snapshot.')
    # Fail fast on native CRT signature checks before the long source build.
    preflight = work/'crt-preflight'
    preflight.mkdir()
    crt = bundle_visual_cpp_runtime(preflight)
    from build_cpython import build_source_runtime
    source_build = build_source_runtime(work, target, find_visual_cpp_runtime(),
        fetch=fetch_pinned, run=run, clean_env=clean_environment)
    for file in preflight.iterdir():
        shutil.copyfile(file, target/file.name)
    probe_runtime(work, crt=crt, source_build=source_build)


def probe_runtime(work: Path, *, crt: dict, source_build: dict) -> None:
    """Check fresh and byte-verified cached runtimes with the same probes."""
    target = work/'Runtime'
    for required in ('python.exe', 'pythonw.exe', 'Lib/venv/__init__.py',
                     'Lib/ensurepip/__init__.py', 'Lib/tkinter/__init__.py',
                     'tcl/tcl8.6/init.tcl', 'tcl/tk8.6/tk.tcl', 'DLLs/zlib1.dll'):
        if not (target / required).is_file():
            raise RuntimeError(f'The runtime snapshot is missing {required}')
    environment = clean_environment()
    environment.update(TCL_LIBRARY=str(target/'tcl/tcl8.6'), TK_LIBRARY=str(target/'tcl/tk8.6'))
    result = run([target / 'python.exe', '-I', '-B', '-c',
        'import sys,ssl,tkinter,venv,ensurepip,json,sqlite3,lzma,bz2,zlib,pyexpat,ctypes; '
        'identity={"version":sys.version.split()[0],"prefix":sys.prefix,'
        '"openssl":ssl.OPENSSL_VERSION,"openssl_version_info":list(ssl.OPENSSL_VERSION_INFO),'
        '"openssl_version_number":hex(ssl.OPENSSL_VERSION_NUMBER),'
        '"sqlite":sqlite3.sqlite_version,"zlib":zlib.ZLIB_RUNTIME_VERSION,'
        '"expat":pyexpat.EXPAT_VERSION,"pip":ensurepip.version()}; '
        'print("Loaded runtime identity: "+json.dumps(identity),file=sys.stderr,flush=True); '
        'identity["tk"]=tkinter.Tcl().eval("info patchlevel"); '
        'callback=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_int)(lambda value:value+1); '
        'assert callback(4)==5,"libffi native callback failed"; '
        'assert lzma.decompress(lzma.compress(b"sample"))==b"sample","XZ roundtrip failed"; '
        'assert bz2.decompress(bz2.compress(b"sample"))==b"sample","bzip2 roundtrip failed"; '
        'ctx=ssl.create_default_context(); assert ctx.check_hostname,"TLS hostname verification disabled"; '
        'print(json.dumps(identity))'], capture=True, env=environment)
    identity = json.loads(result.stdout.strip())
    print('Loaded runtime identity:', json.dumps(identity))
    validate_runtime_versions(identity)
    identity['visual_cpp_runtime'] = crt
    identity['source_build'] = source_build
    if Path(identity.pop('prefix')).resolve() != target.resolve():
        raise RuntimeError('Copied Python runtime did not relocate to the install folder.')
    with tempfile.TemporaryDirectory(prefix='runtime-venv-', dir=work) as temporary:
        venv = Path(temporary) / 'probe'
        run([target / 'python.exe', '-I', '-B', '-m', 'venv', str(venv)], timeout=180, env=environment)
        run([venv / 'Scripts/python.exe', '-I', '-B', '-m', 'pip', '--version'], capture=True, env=environment)
    write_json(work / 'runtime-info.json', identity)
    print('Clean runtime copied and relocation/venv support verified:', identity['version'])


def check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in NETWORK_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Build dependency redirect left its HTTPS origin allowlist.')


class PinnedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        check_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_pinned(entry: dict, target: Path) -> None:
    expected = entry['sha256']
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Dependency SHA-256 must be pinned.')
    if target.is_file() and digest(target) == expected:
        return
    check_url(entry['url'])
    request = urllib.request.Request(entry['url'], headers={
        'User-Agent': 'UnifiedTranscriber-build/2.1', 'Accept-Encoding': 'identity'})
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.partial')
    try:
        with urllib.request.build_opener(PinnedRedirect()).open(request, timeout=60) as response:
            check_url(response.geturl())
            if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise ValueError('Compressed HTTP responses are not accepted for build dependencies.')
            size = 0
            with partial.open('wb') as output:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > entry['max_bytes']:
                        raise ValueError('Build dependency exceeded its size limit.')
                    output.write(block)
        if not size or digest(partial) != expected:
            raise ValueError('Build dependency digest mismatch.')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def copy_sources(target: Path) -> list[str]:
    """Only tracked program files can enter a distribution, never local data."""
    output = subprocess.check_output(['git', 'ls-files', '-z'], cwd=REPO)
    files = output.decode('utf-8').rstrip('\0').split('\0')
    copied = []
    for name in files:
        if not name or name.startswith(('.github/', '_internal/tests/')):
            continue
        if name.startswith('_internal/tools/') and name not in (
                '_internal/tools/repair_dnd.bat', '_internal/tools/start_gui_console.bat',
                '_internal/tools/run_cli.bat', '_internal/tools/ensure_gui_dependencies.bat',
                '_internal/tools/setup_all_cpu.bat', '_internal/tools/setup_all_gpu.bat',
                '_internal/tools/setup_bulk.bat', '_internal/tools/setup_bulk_help.txt',
                '_internal/tools/windows-build-sources.json', '_internal/tools/cpython-windows-sources.json',
                '_internal/tools/modern-python-sources.json'):
            continue
        source = REPO / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Distribution source must be an ordinary tracked file: ' + name)
        folded = tuple(part.casefold() for part in Path(name).parts)
        if (any(part in ('data', 'models', 'recordings', 'transcriptions', 'merged',
                         'speakeredits', 'environments', '__pycache__', 'log', 'logs',
                         '.cache', '.huggingface') for part in folded)
                or any(part in ('settings.json', 'hf_token.txt', 'stored_tokens', 'portable.json',
                                'installation.json') or part.startswith('.env')
                       or (part.startswith('transcriber-settings') and part.endswith('.json'))
                       for part in folded)):
            raise ValueError('Tracked user/runtime data must not enter the payload: ' + name)
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied.append(name)
    return copied


def write_icon(path: Path) -> None:
    """A small code-native icon matching the studio waveform mark; no image dependencies."""
    pixels = []
    for y in reversed(range(32)):
        for x in range(32):
            white = any(a <= x < a+2 and b <= y < 32-b
                        for a, b in ((7, 12), (13, 6), (19, 9), (25, 14)))
            pixels.append(bytes((255, 255, 255, 255) if white else (235, 99, 37, 255)))
    bitmap = struct.pack('<IIIHHIIIIII', 40, 32, 64, 1, 32, 0, 32*32*4, 0, 0, 0, 0)
    bitmap += b''.join(pixels) + bytes(32*4)
    path.write_bytes(struct.pack('<HHH', 0, 1, 1) +
        struct.pack('<BBBBHHII', 32, 32, 0, 0, 1, 32, len(bitmap), 22) + bitmap)



def prepare_verifier(payload: Path, work: Path, repository_id: int) -> dict:
    entries = json.loads(SOURCES.read_text(encoding='utf-8'))
    gh_entry = entries['github_cli']
    archive = work / 'downloads' / Path(urllib.parse.urlsplit(gh_entry['url']).path).name
    fetch_pinned(gh_entry, archive)
    verifier = payload / '_internal/update-verifier'
    verifier.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        selected = [name for name in source.namelist() if name.endswith('/bin/gh.exe') or name == 'bin/gh.exe']
        if len(selected) != 1:
            raise ValueError('Pinned GitHub CLI archive is missing one unique bin/gh.exe.')
        (verifier / 'gh.exe').write_bytes(source.read(selected[0]))
        licenses = [name for name in source.namelist()
                    if Path(name).name.lower() in ('license', 'license.txt')]
        if not licenses:
            raise ValueError('Pinned GitHub CLI archive lacks its license notice.')
        # The main notice is at the shortest path; fail if there is ambiguity
        # instead of copying an unrelated dependency license by accident.
        shortest = min(len(Path(name).parts) for name in licenses)
        roots = [name for name in licenses if len(Path(name).parts) == shortest]
        if len(roots) != 1 or not 0 < source.getinfo(roots[0]).file_size <= 128 * 1024:
            raise ValueError('Pinned GitHub CLI archive has an ambiguous or empty license notice.')
        (verifier / 'LICENSE.txt').write_bytes(source.read(roots[0]))
    environment = clean_environment()
    environment['GH_CONFIG_DIR'] = str(work / 'empty-gh-config')
    result = run([verifier / 'gh.exe', '--version'], env=environment, capture=True)
    if not result.stdout.startswith('gh version ' + gh_entry['version']):
        raise RuntimeError('Pinned GitHub CLI archive reported a different version.')
    roots = run([verifier / 'gh.exe', 'attestation', 'trusted-root'],
                env=environment, capture=True, timeout=120).stdout
    if not roots.strip() or len(roots) > 2_000_000:
        raise RuntimeError('Could not retrieve validated Sigstore trust roots.')
    for line in roots.splitlines():
        if line.strip():
            json.loads(line)
    (verifier / 'trusted_root.jsonl').write_text(roots, encoding='utf-8')
    manifest = {'schema': 1, 'gh_version': gh_entry['version'],
        'gh_sha256': digest(verifier / 'gh.exe'),
        'trusted_root_sha256': digest(verifier / 'trusted_root.jsonl'),
        'repository': REPOSITORY, 'repository_id': repository_id,
        'signer_workflow': REPOSITORY + '/.github/workflows/windows-release.yml'}
    write_json(verifier / 'manifest.json', manifest)
    # Confirm this CLI can reach cryptographic parsing with no token or login.
    probe = work / 'invalid.sigstore.json'
    probe.write_text('{}', encoding='ascii')
    result = subprocess.run([str(verifier / 'gh.exe'), 'attestation', 'verify',
        str(verifier / 'gh.exe'), '--bundle', str(probe), '--custom-trusted-root',
        str(verifier / 'trusted_root.jsonl'), '--repo', REPOSITORY],
        env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    text = (result.stdout + result.stderr).lower()
    if result.returncode == 0 or any(phrase in text for phrase in
            ('gh auth login', 'set the gh_token', 'authentication required', 'not logged into')):
        raise RuntimeError('Offline update verifier unexpectedly requires GitHub authentication.')
    drag_drop = entries['drag_drop']
    fetch_pinned(drag_drop, payload / '_internal' / drag_drop['filename'])
    return {'github_cli': {'version': gh_entry['version'], 'archive_sha256': gh_entry['sha256']},
            'drag_drop': {'filename': drag_drop['filename'], 'sha256': drag_drop['sha256']}}


def make_payload(folder: Path, archive: Path) -> dict[str, str]:
    manifest = {}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as output:
        for source in sorted(folder.rglob('*')):
            if source.is_dir():
                continue
            if source.is_symlink():
                raise ValueError('Symlinks are not permitted in the install payload.')
            name = source.relative_to(folder).as_posix()
            manifest[name] = digest(source)
            info = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            output.writestr(info, source.read_bytes())
    return manifest


def build(work: Path, output: Path, repository_id: int, source_commit: str) -> Path:
    require_windows()
    version = (REPO / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('VERSION must contain a stable semantic version.')
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit) or repository_id <= 0:
        raise ValueError('A repository ID and exact source commit are required.')
    runtime = work / 'Runtime'
    if not (runtime / 'python.exe').is_file():
        raise ValueError('Run snapshot-runtime before installing build dependencies.')
    payload = work / 'payload'
    if payload.exists():
        raise ValueError('Payload staging directory already exists; use a clean work directory.')
    payload.mkdir()
    from native_launcher import build_native_launcher
    from nsis_builder import (build_installer, copy_nsis_license, download_nsis,
                              NSIS_IDENTITY, NSIS_VERSION)
    copied = copy_sources(payload)
    shutil.copytree(runtime, payload / 'Runtime')
    # The installer compiler is itself distributed as executable code. The
    # bootstrap's temporary notice disappears after exit, so include the
    # exact pinned compiler archive notice in the installed payload as well.
    nsis_archive = work / 'downloads' / ('nsis-' + NSIS_VERSION + '.zip')
    download_nsis(nsis_archive)
    copy_nsis_license(nsis_archive, payload / '_internal/NSIS-LICENSE.txt')
    # The current ML wheel ecosystem supports3.14, while the independently
    # tested legacy engines retain3.11. Both runtimes ship in the one setup;
    # neither depends on a user's installed Python or a global CRT installer.
    from modern_runtime import prepare_runtime
    modern_runtime = prepare_runtime(payload, work, fetch=fetch_pinned, run=run,
        clean_env=clean_environment, bundle_crt=bundle_visual_cpp_runtime)
    dependency_info = prepare_verifier(payload, work, repository_id)
    dependency_info['installer'] = dict(NSIS_IDENTITY)
    dependency_info['modern_runtime'] = modern_runtime
    write_icon(work / 'studio.ico')
    write_json(payload / '_internal/distribution-info.json', {
        'schema': 1, 'version': version, 'source_commit': source_commit,
        'python': json.loads((work / 'runtime-info.json').read_text()),
        'ml_python': modern_runtime,
        'dependencies': dependency_info, 'repository': REPOSITORY,
        'repository_id': repository_id, 'source_file_count': len(copied)})
    output.mkdir(parents=True, exist_ok=True)
    launcher = build_native_launcher(work, payload, version, run=run, clean_env=clean_environment)
    archive = work / 'payload.zip'
    manifest = make_payload(payload, archive)
    write_json(work / 'PAYLOAD_MANIFEST.json', manifest)
    info = {'schema': 1, 'product': PRODUCT, 'version': version,
        'payload_sha256': digest(archive), 'payload_size': archive.stat().st_size,
        'source_commit': source_commit, 'repository': REPOSITORY,
        'repository_id': repository_id, 'payload_files': len(manifest),
        'packaging': {'installer':'NSIS', 'launcher':'native-win32',
                      'launcher_sha256':digest(launcher), 'pyinstaller':False}}
    write_json(work / 'BUILD_INFO.json', info)
    executable = build_installer(runtime=runtime, archive=archive,
        info=work/'BUILD_INFO.json', code=payload/'_internal', work=work, output=output,
        version=version, icon=work/'studio.ico', run=run)
    (output / 'SHA256SUMS.txt').write_text(f'{digest(executable)}  {executable.name}\n', encoding='ascii')
    write_json(output / 'BUILD_INFO.json', info)
    write_json(output / 'DEPENDENCIES.json', {
        'runtime': json.loads((work / 'runtime-info.json').read_text()),
        'modern_runtime': modern_runtime,
        'build': json.loads(run([sys.executable, '-m', 'pip', '--isolated', 'list', '--format=json'], capture=True).stdout),
        'bundled': dependency_info})
    print('Built:', executable)
    print('SHA-256:', digest(executable))
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('snapshot-runtime', 'build'))
    parser.add_argument('--work', required=True)
    parser.add_argument('--output', default='dist')
    parser.add_argument('--repository-id', type=int, default=0)
    parser.add_argument('--source-commit', default='')
    args = parser.parse_args()
    work = work_folder(args.work)
    if args.command == 'snapshot-runtime':
        snapshot_runtime(work)
    else:
        build(work, Path(args.output).resolve(), args.repository_id, args.source_commit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
