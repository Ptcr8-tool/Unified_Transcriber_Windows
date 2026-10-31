"""Service the native components in the official CPython ML runtime.

The PSF package still includes SQLite3.50 and XZ5.2. This project reuses the
authenticated source-built SQLite C ABI DLL and rebuilds CPython's own _lzma
extension against the same pinned XZ5.8.4 used by the GUI runtime. No old
Python extension is reused across CPython ABIs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from build_cpython import extract_source
from native_launcher import find_visual_studio, toolset_for


def source_files(project: Path, xz: Path) -> list[Path]:
    """Use the official CPython source list with its reviewed XZ5.8 changes."""
    root = ET.parse(project).getroot()
    names = [node.attrib['Include'] for node in root.findall(
        './/{http://schemas.microsoft.com/developer/msbuild/2003}ClCompile') if 'Include' in node.attrib]
    files = []
    for name in names:
        if not name.startswith('$(lzmaDir)'):
            raise ValueError('The official liblzma project contains an unexpected source path.')
        relative = Path(name[len('$(lzmaDir)'):].replace('\\', '/'))
        if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.c':
            raise ValueError('The official liblzma source list is unsafe.')
        # Removed by current upstream XZ; table definitions now live in the
        # fast/check implementation. ARM64 BCJ is enabled by the pinnedconfig.
        if relative.name in ('crc32_table.c', 'crc64_table.c'):
            continue
        files.append(xz / relative)
    if not 60 <= len(files) <= 100:
        raise ValueError('The official liblzma source list changed unexpectedly.')
    files.append(xz / 'src/liblzma/simple/arm64.c')
    if len(set(files)) != len(files) or any(not file.is_file() for file in files):
        raise ValueError('The pinned XZ source files do not match the reviewed CPython project.')
    return files


def write_project(folder: Path, python_source: Path, xz: Path, runtime: Path, toolset: str) -> Path:
    if not re.fullmatch(r'v14\d', toolset):
        raise ValueError('An installed official Visual C++ toolset is required.')
    files = source_files(python_source / 'PCbuild/liblzma.vcxproj', xz)
    files.append(python_source / 'Modules/_lzmamodule.c')
    directories = [xz / 'windows', *[xz / ('src/liblzma/' + suffix) for suffix in
        ('common', 'api', 'check', 'delta', 'lz', 'lzma', 'rangecoder', 'simple')],
        xz / 'src/common', python_source / 'Include', python_source / 'Include/internal',
        python_source / 'Include/internal/mimalloc', python_source / 'PC']
    includes = ';'.join(escape(str(path.resolve())) for path in directories)
    items = '\n'.join('<ClCompile Include="' + escape(str(path.resolve())) + '">'
        '<ObjectFileName>$(IntDir)' + str(index) + '.obj</ObjectFileName></ClCompile>'
        for index, path in enumerate(files))
    out = escape(str(folder.resolve()) + os.sep)
    python_library = escape(str((runtime / 'libs/python314.lib').resolve()))
    project = folder / 'modern_lzma.vcxproj'
    project.write_text(f'''<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations"><ProjectConfiguration Include="Release|x64">
    <Configuration>Release</Configuration><Platform>x64</Platform>
  </ProjectConfiguration></ItemGroup>
  <PropertyGroup Label="Globals"><ProjectGuid>{{0F3AFD95-E750-43D0-9E3C-AC73F52FF234}}</ProjectGuid>
    <RootNamespace>UnifiedModernLzma</RootNamespace><WindowsTargetPlatformVersion>10.0</WindowsTargetPlatformVersion>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.Default.props" />
  <PropertyGroup Label="Configuration"><ConfigurationType>DynamicLibrary</ConfigurationType>
    <UseDebugLibraries>false</UseDebugLibraries><PlatformToolset>{toolset}</PlatformToolset>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props" />
  <PropertyGroup><OutDir>{out}</OutDir><IntDir>{out}obj\\</IntDir>
    <TargetName>_lzma</TargetName><TargetExt>.pyd</TargetExt><LinkIncremental>false</LinkIncremental>
  </PropertyGroup>
  <ItemDefinitionGroup><ClCompile>
    <AdditionalIncludeDirectories>{includes};%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>
    <PreprocessorDefinitions>WIN32;NDEBUG;Py_NO_LINK_LIB=1;HAVE_CONFIG_H;_LIB;LZMA_API_STATIC;_FILE_OFFSET_BITS=64;_CRT_SECURE_NO_DEPRECATE;_CRT_NONSTDC_NO_DEPRECATE;%(PreprocessorDefinitions)</PreprocessorDefinitions>
    <DisableSpecificWarnings>4244;4267;4996;%(DisableSpecificWarnings)</DisableSpecificWarnings>
    <WarningLevel>Level3</WarningLevel><Optimization>MaxSpeed</Optimization>
    <RuntimeLibrary>MultiThreadedDLL</RuntimeLibrary><PrecompiledHeader>NotUsing</PrecompiledHeader>
    <CompileAs>CompileAsC</CompileAs><LanguageStandard_C>stdc11</LanguageStandard_C>
    <BufferSecurityCheck>true</BufferSecurityCheck><ControlFlowGuard>Guard</ControlFlowGuard>
    <AdditionalOptions>/utf-8 %(AdditionalOptions)</AdditionalOptions>
  </ClCompile><Link>
    <AdditionalDependencies>{python_library};kernel32.lib;advapi32.lib;shell32.lib;ole32.lib;oleaut32.lib;%(AdditionalDependencies)</AdditionalDependencies>
    <GenerateDebugInformation>false</GenerateDebugInformation><RandomizedBaseAddress>true</RandomizedBaseAddress>
    <DataExecutionPrevention>true</DataExecutionPrevention><HighEntropyVA>true</HighEntropyVA><ControlFlowGuard>Guard</ControlFlowGuard>
    <AdditionalOptions>/CETCOMPAT /EXPORT:lzma_version_string %(AdditionalOptions)</AdditionalOptions>
  </Link></ItemDefinitionGroup>
  <ItemGroup>{items}</ItemGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />
</Project>
''', encoding='utf-8')
    return project


def service_native(runtime: Path, payload: Path, work: Path, lock: dict, *, fetch, run, clean_env) -> dict:
    from modern_runtime import sha256
    trusted = json.loads(Path(__file__).with_name('cpython-windows-sources.json').read_text(encoding='utf-8'))
    dependencies = {entry['name']: entry for entry in trusted['dependencies']}
    folder = work / 'modern-native'
    folder.mkdir()
    sources = {}
    for name, entry, tar in (('python', lock['extension_source'], True),
                             ('xz', dependencies['xz'], True),
                             ('xz-config', dependencies['xz-build-config'], False)):
        archive = work / 'downloads' / ('modern-' + name + ('.tgz' if tar else '.zip'))
        fetch(entry, archive)
        target = folder / name
        extract_source(archive, target, tar=tar)
        sources[name] = target
    (sources['xz'] / 'windows').mkdir(exist_ok=True)
    shutil.copyfile(sources['xz-config'] / 'windows/config.h', sources['xz'] / 'windows/config.h')
    studio = find_visual_studio()
    project = write_project(folder, sources['python'], sources['xz'], runtime, toolset_for(studio))
    run([studio / 'MSBuild/Current/Bin/MSBuild.exe', project, '/t:Build', '/nologo', '/v:minimal', '/m:2',
         '/p:Configuration=Release', '/p:Platform=x64', '/p:PreferredToolArchitecture=x64'],
        env=clean_env(), timeout=300)
    built = folder / '_lzma.pyd'
    if not built.is_file():
        raise RuntimeError('The authenticated modern XZ extension did not build.')
    shutil.copyfile(built, runtime / 'DLLs/_lzma.pyd')
    # SQLite's public C ABI is stable across these supported releases. Its
    # DLL is independent of Python's extension ABI; _sqlite3.pyd remains3.14.
    sqlite = payload / 'Runtime/DLLs/sqlite3.dll'
    if not sqlite.is_file():
        raise RuntimeError('The authenticated source-built SQLite DLL is unavailable.')
    shutil.copyfile(sqlite, runtime / 'DLLs/sqlite3.dll')
    (runtime / 'NATIVE_COMPONENTS_NOTICE.txt').write_text(
        'CPython3.14.10 _lzma is rebuilt from PSF source against XZ5.8.4.\n'
        'SQLite3.53.4 is the C ABI DLL built from the same authenticated source as the GUI runtime.\n'
        'No CPython3.11 extension is reused in this runtime.\n'
        'XZ license: https://tukaani.org/xz/\n'
        'SQLite license (public domain): https://sqlite.org/copyright.html\n', encoding='utf-8')
    shutil.copyfile(sources['xz'] / 'COPYING', runtime / 'XZ_COPYING.txt')
    shutil.copyfile(sources['xz'] / 'COPYING.0BSD', runtime / 'XZ_LICENSE_0BSD.txt')
    return {'xz': {'version': dependencies['xz']['version'], 'source': dependencies['xz'],
                   'configuration_source': dependencies['xz-build-config'],
                   'python_source': lock['extension_source'], 'extension_sha256': sha256(runtime / 'DLLs/_lzma.pyd')},
            'sqlite': {'version': '3.53.4', 'source': dependencies['sqlite'],
                       'dll_sha256': sha256(runtime / 'DLLs/sqlite3.dll')}}
