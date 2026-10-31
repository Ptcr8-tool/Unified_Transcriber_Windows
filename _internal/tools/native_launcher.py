"""Build the ordinary x64 application launcher with the installed Microsoft SDK.

No downloaded compiler, executable packer or embedded Python is used. MSBuild
invokes the installed Visual C++ compiler, resource compiler and manifest tool.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import struct
from xml.sax.saxutils import escape

TOOLS = Path(__file__).resolve().parent


def find_visual_studio() -> Path:
    defaults = {'ProgramW6432': r'C:\Program Files',
                'ProgramFiles': r'C:\Program Files',
                'ProgramFiles(x86)': r'C:\Program Files (x86)'}
    candidates = []
    for root in dict.fromkeys(Path(os.environ.get(key, value)) / 'Microsoft Visual Studio'
                              for key, value in defaults.items()):
        if not root.is_dir():
            continue
        for product in root.iterdir():
            if not product.is_dir() or not re.fullmatch(r'\d{2,4}', product.name):
                continue
            for edition in ('Enterprise', 'Professional', 'Community', 'BuildTools'):
                candidate = product / edition
                if (candidate / 'MSBuild/Current/Bin/MSBuild.exe').is_file():
                    # VS2026 changed its installation directory from a year
                    # to a product major (18). Compare the actual installed
                    # compiler versions rather than ranking 2022 above 18.
                    versions = [tuple(map(int, path.name.split('.')))
                                for path in (candidate/'VC/Tools/MSVC').glob('*')
                                if path.is_dir() and re.fullmatch(r'\d+(?:\.\d+){2,3}', path.name)]
                    if versions:
                        candidates.append((max(versions), candidate))
    if not candidates:
        raise RuntimeError('The official Visual Studio C++ build tools are required.')
    return max(candidates, key=lambda pair: pair[0])[1]


def toolset_for(studio: Path) -> str:
    available = {path.name for path in (studio / 'MSBuild/Microsoft/VC').glob(
        '*/Platforms/x64/PlatformToolsets/v14*') if path.is_dir()}
    if 'v143' in available:
        return 'v143'
    versions = sorted(name for name in available if re.fullmatch(r'v14\d', name))
    if not versions:
        raise RuntimeError('The official Visual C++ x64 v14x platform toolset is required.')
    return versions[-1]


def write_project(folder: Path, version: str, icon: Path, toolset: str) -> Path:
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('A stable application version is required.')
    numbers = tuple(map(int, version.split('.')))
    if any(number > 65535 for number in numbers):
        raise ValueError('Native version components must fit the Windows version resource.')
    if not re.fullmatch(r'v14\d', toolset):
        raise ValueError('An installed Visual C++ v14x toolset is required.')
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ('c', 'rc', 'manifest'):
        shutil.copyfile(TOOLS / ('native_launcher.' + suffix), folder / ('native_launcher.' + suffix))
    shutil.copyfile(icon, folder / 'native_launcher.ico')
    (folder / 'native_launcher_version.h').write_text(
        '#define UNIFIED_VERSION_TUPLE ' + ','.join(map(str, (*numbers, 0))) + '\n'
        '#define UNIFIED_VERSION_STRING "' + version + '"\n', encoding='ascii')
    project = folder / 'native_launcher.vcxproj'
    # Inputs below are builder-owned. XML escaping keeps an ampersand in the
    # staging directory a path character rather than project markup.
    outdir = escape(str(folder.resolve()) + os.sep)
    text = f'''<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations"><ProjectConfiguration Include="Release|x64">
    <Configuration>Release</Configuration><Platform>x64</Platform>
  </ProjectConfiguration></ItemGroup>
  <PropertyGroup Label="Globals">
    <ProjectGuid>{{BDA8B872-7E97-43D7-9BCC-331923030F4C}}</ProjectGuid>
    <RootNamespace>UnifiedTranscriberLauncher</RootNamespace>
    <WindowsTargetPlatformVersion>10.0</WindowsTargetPlatformVersion>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.Default.props" />
  <PropertyGroup Label="Configuration">
    <ConfigurationType>Application</ConfigurationType><UseDebugLibraries>false</UseDebugLibraries>
    <PlatformToolset>{toolset}</PlatformToolset><CharacterSet>Unicode</CharacterSet>
    <WholeProgramOptimization>true</WholeProgramOptimization>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props" />
  <PropertyGroup><OutDir>{outdir}</OutDir><IntDir>{outdir}obj\\</IntDir>
    <TargetName>UnifiedTranscriber</TargetName><TargetExt>.exe</TargetExt>
    <LinkIncremental>false</LinkIncremental><GenerateManifest>true</GenerateManifest>
  </PropertyGroup>
  <ItemDefinitionGroup>
    <ClCompile><WarningLevel>Level4</WarningLevel><TreatWarningAsError>true</TreatWarningAsError>
      <Optimization>MaxSpeed</Optimization><RuntimeLibrary>MultiThreaded</RuntimeLibrary>
      <BufferSecurityCheck>true</BufferSecurityCheck><ControlFlowGuard>Guard</ControlFlowGuard>
      <PrecompiledHeader>NotUsing</PrecompiledHeader><CompileAs>CompileAsC</CompileAs>
      <LanguageStandard_C>stdc11</LanguageStandard_C>
      <AdditionalOptions>/utf-8 %(AdditionalOptions)</AdditionalOptions>
    </ClCompile>
    <ResourceCompile><Culture>0x0409</Culture><AdditionalOptions>/c65001 %(AdditionalOptions)</AdditionalOptions></ResourceCompile>
    <Link><SubSystem>Windows</SubSystem><GenerateDebugInformation>false</GenerateDebugInformation>
      <RandomizedBaseAddress>true</RandomizedBaseAddress><DataExecutionPrevention>true</DataExecutionPrevention>
      <HighEntropyVA>true</HighEntropyVA><ControlFlowGuard>Guard</ControlFlowGuard>
      <AdditionalDependencies>kernel32.lib;user32.lib;shell32.lib;%(AdditionalDependencies)</AdditionalDependencies>
      <AdditionalOptions>/CETCOMPAT %(AdditionalOptions)</AdditionalOptions>
    </Link>
    <Manifest><AdditionalManifestFiles>native_launcher.manifest;%(AdditionalManifestFiles)</AdditionalManifestFiles></Manifest>
  </ItemDefinitionGroup>
  <ItemGroup><ClCompile Include="native_launcher.c"/><ResourceCompile Include="native_launcher.rc"/></ItemGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />
</Project>
'''
    project.write_text(text, encoding='utf-8')
    return project


def verify_native_launcher(path: Path) -> dict:
    """Reject the wrong architecture, subsystem or missing loader protections."""
    data = path.read_bytes()
    if len(data) < 512 or data[:2] != b'MZ':
        raise RuntimeError('Native launcher is not a Windows PE executable.')
    offset = struct.unpack_from('<I', data, 60)[0]
    if offset + 24 + 112 > len(data) or data[offset:offset + 4] != b'PE\0\0':
        raise RuntimeError('Native launcher has an invalid PE header.')
    machine = struct.unpack_from('<H', data, offset + 4)[0]
    optional = offset + 24
    magic = struct.unpack_from('<H', data, optional)[0]
    subsystem, dll_characteristics = struct.unpack_from('<HH', data, optional + 68)
    required = 0x20 | 0x40 | 0x100 | 0x4000  # High entropy ASLR, ASLR, NX, CFG.
    if (machine != 0x8664 or magic != 0x20b or subsystem != 2
            or dll_characteristics & required != required):
        raise RuntimeError('Native launcher must be an x64 GUI PE with ASLR, NX and CFG.')
    if b'MEI\x0c\x0b\x0a\x0b\x0e' in data:
        raise RuntimeError('The ordinary launcher must not contain a PyInstaller archive.')
    return {'architecture': 'x64', 'subsystem': 'Windows GUI', 'aslr': True,
            'nx': True, 'cfg': True, 'self_extracting': False, 'size': len(data)}


def build_native_launcher(work: Path, output: Path, version: str, *, run,
                          clean_env, visual_studio: Path | None = None) -> Path:
    if os.name != 'nt':
        raise RuntimeError('Build the native launcher on Windows x64.')
    studio = Path(visual_studio) if visual_studio is not None else find_visual_studio()
    compiler = studio / 'MSBuild/Current/Bin/MSBuild.exe'
    if not compiler.is_file():
        raise RuntimeError('The official Visual Studio MSBuild executable is required.')
    folder = Path(work) / 'native-launcher-build'
    project = write_project(folder, version, Path(work) / 'studio.ico', toolset_for(studio))
    run([compiler, project, '/t:Build', '/nologo', '/v:minimal', '/m:2',
         '/p:Configuration=Release', '/p:Platform=x64', '/p:PreferredToolArchitecture=x64'],
        env=clean_env(), timeout=180)
    built = folder / 'UnifiedTranscriber.exe'
    verify_native_launcher(built)
    Path(output).mkdir(parents=True, exist_ok=True)
    target = Path(output) / 'UnifiedTranscriber.exe'
    shutil.copyfile(built, target)
    return target
