"""Build CPython from authenticated upstream source; host Python is build-only.

PCbuild's MSBuild project and PC/layout are the official Windows build/layout
entry points. We prefetch every external at its immutable commit and digest,
instead of invoking get_externals.bat's mutable-tag downloads. No installer,
registry edits, root-store changes or alternative Python vendor is involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import zipfile

LOCK = Path(__file__).with_name('cpython-windows-sources.json')


def write_component_manifest(component: Path, commit: str) -> None:
    """Supply NMake's git metadata from the authenticated source pin.

    Codeload archives deliberately contain no .git directory. This matches
    upstream's `copy gitmanifest.in manifest.uuid; git rev-parse HEAD >> ...`
    output without creating a repository or asking git for ambient metadata.
    """
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('The Tcl/Tk build manifest requires an exact source commit.')
    template = (component/'win/gitmanifest.in').read_bytes()
    if template != b'git-':
        raise ValueError('The upstream Tcl/Tk build manifest template changed.')
    (component/'manifest.uuid').write_bytes(template + commit.encode('ascii') + b'\n')


def archive_path(name: str, prefix: str) -> Path:
    path = PurePosixPath(name)
    if (not name.startswith(prefix + '/') or path.is_absolute() or path.as_posix() != name
            or '\\' in name or any(part in ('.', '..') or ':' in part for part in path.parts)):
        raise ValueError('Unsafe upstream source archive member: ' + name)
    return Path(*path.parts[1:])


def extract_source(archive: Path, target: Path, *, tar: bool = False) -> None:
    """Write ordinary files only, with bounded members and expanded content."""
    if target.exists():
        raise ValueError('A fresh source directory is required.')
    target.mkdir(parents=True)
    seen = set()
    total = 0
    with (tarfile.open(archive, 'r:gz') if tar else zipfile.ZipFile(archive)) as source:
        entries = iter(source) if tar else iter(source.infolist())
        prefix = None
        for count,entry in enumerate(entries,1):
            if count > 20000:
                raise ValueError('Too many upstream source archive members.')
            name = entry.name if tar else entry.filename
            if prefix is None:
                prefix = name.split('/')[0]
            if (entry.isdir() if tar else entry.is_dir()):
                continue
            if ((tar and not entry.isfile()) or
                    (not tar and stat.S_ISLNK(entry.external_attr >> 16))):
                raise ValueError('Source links and special files are forbidden.')
            relative = archive_path(name, prefix)
            folded = relative.as_posix().casefold()
            if folded in seen:
                raise ValueError('Duplicate or Windows case-conflicting source path.')
            seen.add(folded)
            size = entry.size if tar else entry.file_size
            total += size
            if size > 40_000_000 or total > 600_000_000:
                raise ValueError('Upstream source archive exceeded its expanded limit.')
            destination = target/relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            reader = source.extractfile(entry) if tar else source.open(entry)
            with reader, destination.open('xb') as output:
                shutil.copyfileobj(reader, output, length=1024*1024)
            if destination.stat().st_size != size:
                raise ValueError('Incomplete upstream archive member.')


def prepare_sources(work: Path, fetch) -> tuple[Path, dict]:
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    download = work/'downloads/cpython'
    entry = lock['cpython']
    archive = download/'CPython.tgz'
    fetch(entry, archive)
    source = work/'cpython-source'
    extract_source(archive, source, tar=True)
    patchlevel = (source/'Include/patchlevel.h').read_text(encoding='utf-8')
    if f'#define PY_VERSION              "{entry["version"]}"' not in patchlevel:
        raise ValueError('Authenticated source has an unexpected CPython version.')
    externals = source/'externals'
    for dependency in lock['dependencies']:
        if dependency.get('commit') and not re.fullmatch('[0-9a-f]{40}', dependency['commit']):
            raise ValueError('Every CPython external must pin an immutable commit.')
        is_tar = dependency.get('format') == 'tar.gz'
        archive = download/(dependency['name']+('.tgz' if is_tar else '.zip'))
        fetch(dependency, archive)
        component = externals/dependency.get('directory',dependency['name'])
        extract_source(archive, component, tar=is_tar)
        if dependency['name'] in ('tcl','tk'):
            write_component_manifest(component, dependency['commit'])
    # Upstream CPython's small MSVC configuration is build support only. The
    # vulnerable 5.8.1 C source is never compiled: use authenticated XZ5.8.4.
    (externals/'xz/windows').mkdir(exist_ok=True)
    shutil.copyfile(externals/'xz-build-config/windows/config.h', externals/'xz/windows/config.h')
    # Adopt the current upstream liblzma source-list changes for XZ 5.8.
    # These are project-only adaptations; no CPython runtime source is changed.
    project = source/'PCbuild/liblzma.vcxproj'
    text = project.read_text(encoding='utf-8-sig')
    text = text.replace('windows/vs2019', 'windows').replace('windows\\vs2019', 'windows')
    text = '\n'.join(line for line in text.splitlines()
                     if not any(name in line for name in ('crc32_table.c','crc64_table.c','crc_macros.h')))+'\n'
    old = '<ClCompile Include="$(lzmaDir)src\\liblzma\\simple\\arm.c" />'
    if text.count(old) != 1:
        raise ValueError('Upstream liblzma project changed; review the XZ adaptation.')
    text = text.replace(old, old+'\n    <ClCompile Include="$(lzmaDir)src\\liblzma\\simple\\arm64.c" />')
    project.write_text(text, encoding='utf-8')
    # Avoid bootstrap pip24/setuptools79 in newly created user environments.
    bundled = source/'Lib/ensurepip/_bundled'
    for wheel in bundled.glob('*.whl'):
        wheel.unlink()
    ensurepip = source/'Lib/ensurepip/__init__.py'
    text = ensurepip.read_text(encoding='utf-8')
    for wheel in lock['bootstrap_wheels']:
        fetch(wheel, bundled/wheel['filename'])
        constant = '_' + wheel['name'].upper() + '_VERSION'
        text, count = re.subn(rf'{constant} = "[0-9.]+"',
                             f'{constant} = "{wheel["version"]}"', text)
        if count != 1:
            raise ValueError('Upstream ensurepip constants changed; review bootstrap update.')
    ensurepip.write_text(text, encoding='utf-8')
    return source, lock


def build_source_runtime(work: Path, target: Path, crt_source: Path, *, fetch, run, clean_env) -> dict:
    source, lock = prepare_sources(work, fetch)
    # This follows the MSBuild invocation in official PCbuild/build.bat, with
    # explicit global properties and no get_externals/network fallback.
    studio = crt_source.parents[5]
    msbuild = studio/'MSBuild/Current/Bin/MSBuild.exe'
    if not msbuild.is_file():
        raise RuntimeError('The Visual Studio SDK lacks its official MSBuild executable.')
    toolsets = list((studio/'MSBuild/Microsoft/VC').glob('*/Platforms/x64/PlatformToolsets/v14*'))
    available = {path.name for path in toolsets if path.is_dir()}
    toolset = 'v143' if 'v143' in available else max(available, default='')
    if not toolset:
        raise RuntimeError('No installed Visual C++ v14x x64 toolset was found.')
    external = source/'externals'
    properties = {'Configuration':'Release', 'Platform':'x64', 'PlatformToolset':toolset,
        'PreferredToolArchitecture':'x64', 'BuildForRelease':'true', 'KillPython':'false',
        'IncludeExternals':'true', 'IncludeSSL':'true', 'IncludeTkinter':'true',
        'IncludeCTypes':'true', 'IncludeTests':'false', 'IncludeTest':'false',
        'IncludeUwp':'false', 'PythonForBuild':os.fspath(Path(os.sys.executable)),
        'ExternalsDir':str(external)+os.sep, 'bz2Dir':str(external/'bzip2')+os.sep,
        'sqlite3Dir':str(external/'sqlite-3.53.4.0')+os.sep, 'lzmaDir':str(external/'xz')+os.sep,
        'zlibDir':str(external/'zlib')+os.sep, 'libffiDir':str(external/'libffi')+os.sep,
        'opensslOutDir':str(external/'openssl/amd64')+os.sep,
        'TclVersion':'8.6.18.0', 'TkVersion':'8.6.18.0',
        'tclDir':str(external/'tcl')+os.sep, 'tkDir':str(external/'tk')+os.sep,
        'tcltkDir':str(external/'tcltk/amd64')+os.sep}
    environment = clean_env()
    environment['PYTHON'] = os.sys.executable
    environment['PYTHON_FOR_BUILD'] = os.sys.executable
    compiler = run([msbuild, '-version', '-nologo'], capture=True, env=environment).stdout.strip()
    # Match upstream prepare_tcltk.bat: a Win32 Tcl shell supplies native
    # generators, then build the x64 Tcl/Tk pair used by the application.
    # All source and zlib inputs are already authenticated; no external fetch
    # helper or system installation is called by these project invocations.
    for platform,project_name in (('Win32','tcl'),('x64','tcl'),('x64','tk')):
        tcl_properties = dict(properties, Platform=platform,
            tcltkDir=str(external/('tcltk/win32' if platform=='Win32' else 'tcltk/amd64'))+os.sep)
        run([msbuild, source/f'PCbuild/{project_name}.vcxproj', '/t:Build', '/nologo', '/v:minimal',
             *['/p:'+name+'='+value for name,value in tcl_properties.items()]],
            env=environment, timeout=600)
        dll = Path(tcl_properties['tcltkDir'])/'bin'/('tcl86t.dll' if project_name=='tcl' else 'tk86t.dll')
        if not dll.is_file():
            raise RuntimeError(f'Official Tcl/Tk {platform} source build did not produce {dll.name}.')
    shell = external/'tcltk/amd64/bin/tclsh86t.exe'
    if not shell.is_file() or not (external/'tcltk/amd64/bin/zlib1.dll').is_file():
        raise RuntimeError('The Tcl source build lacks its native shell or current zlib dependency.')
    run([msbuild, source/'PCbuild/pcbuild.proj', '/t:Build', '/m:2', '/nologo', '/v:minimal',
         '/clp:Summary', *['/p:'+name+'='+value for name,value in properties.items()]],
        env=environment, timeout=2100)
    binary = source/'PCbuild/amd64/python.exe'
    # Tcl8.6.18 links its current official zlib1.3.2 DLL. The old3.11
    # _tkinter project copies only Tcl/Tk DLLs, so include this dependency.
    shutil.copyfile(external/'tcltk/amd64/bin/zlib1.dll', binary.parent/'zlib1.dll')
    version = run([binary, '-I', '-c', 'import sys; print(sys.version.split()[0])'],
                  capture=True, env=environment).stdout.strip()
    if version != lock['cpython']['version']:
        raise RuntimeError('Source-built CPython version does not match its pinned release.')
    # MemoryBIO + upstream fixtures exercise the compromised-CA rejection with
    # this exact OpenSSL build. No Internet server, audio or private files.
    run([binary, '-m', 'unittest',
         'test.test_ssl.SSLObjectTests.test_distrusted_spki'], env=environment, timeout=120)
    run([binary, source/'PC/layout', '--source', source, '--build', binary.parent,
         '--copy', target, '--temp', work/'cpython-layout', '--arch', 'amd64',
         '--include-stable', '--include-distutils', '--include-tcltk', '--include-venv',
         '--include-dev'], env=environment, timeout=300)
    # Preserve the complete PSF/third-party notice from the authenticated
    # CPython source even when the layout tool omits it.
    shutil.copyfile(source/'LICENSE', target/'LICENSE.txt')
    return {'source':lock, 'msbuild_version':compiler, 'platform_toolset':toolset,
            'security_regression':'test.test_ssl.SSLObjectTests.test_distrusted_spki passed',
            'adaptations':['XZ5.8 project source list from upstream',
                           'Tcl/Tk build manifests generated from authenticated source commits',
                           'ensurepip bundles pinned pip/setuptools security updates'],
            'build_properties':{name:('<HOST_PYTHON>' if name=='PythonForBuild'
                else value.replace(str(source),'<BUILD_ROOT>/cpython-source'))
                for name,value in properties.items()}}
