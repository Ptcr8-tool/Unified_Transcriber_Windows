"""The managed ML runtime cannot trust an unverified archive or host Python."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import modern_runtime as runtime
import modern_native as native


class ModernRuntimeTests(unittest.TestCase):
    def archive(self, folder, *, extra=(), missing=None, author='Python Software Foundation'):
        path = folder / 'python.nupkg'
        binary = bytearray(256)
        binary[:2] = b'MZ'
        struct.pack_into('<I', binary, 60, 64)
        binary[64:70] = b'PE\0\0d\x86'
        with zipfile.ZipFile(path, 'w') as output:
            for name in sorted(runtime.REQUIRED):
                if name != missing:
                    output.writestr('tools/' + name, binary if name.endswith(('.exe', '.dll')) else b'sample')
            output.writestr('python.nuspec',
                '<package xmlns="http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd">'
                '<metadata><id>python</id><version>3.14.10</version><authors>' + author +
                '</authors></metadata></package>')
            for name in extra:
                member = zipfile.ZipInfo(name)
                # Preserve the malformed bytes in both ZIP headers. On
                # Windows ZipInfo(name) would otherwise normalize backslashes
                # before this fixture ever reaches the archive validator.
                member.filename = name
                output.writestr(member, 'extra')
        return path, dict(size=path.stat().st_size, sha256=runtime.sha256(path))

    def test_pinned_complete_psf_archive_extracts_without_global_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            archive, entry = self.archive(folder, extra=('other/document.txt',))
            runtime.unpack_runtime(archive, folder / 'MLRuntime', entry)
            self.assertTrue((folder / 'MLRuntime/Lib/venv/__init__.py').is_file())
            self.assertFalse((folder / 'MLRuntime/other').exists())
            with self.assertRaisesRegex(ValueError, 'fresh'):
                runtime.unpack_runtime(archive, folder / 'MLRuntime', entry)

    def test_archive_digest_or_size_failure_does_not_write_or_execute(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            archive, entry = self.archive(folder)
            for value in (dict(entry, size=entry['size'] + 1), dict(entry, sha256='0' * 64)):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'identity'):
                    runtime.unpack_runtime(archive, folder / 'MLRuntime', value)
                self.assertFalse((folder / 'MLRuntime').exists())

    def test_all_archive_paths_validated_before_any_write(self):
        for extra in ('tools/../escape', 'tools/C:/escape', 'tools/Lib\\escape',
                      'tools/Lib/venv/__INIT__.py', 'tools/folder. /file', '/absolute',
                      'tools/python314._pth', 'tools/Lib/file\0hidden.py'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                archive, entry = self.archive(folder, extra=(extra,))
                self.assertEqual(archive.read_bytes().count(extra.encode('utf-8')), 2,
                                 'The malformed name must survive in both raw ZIP headers.')
                with zipfile.ZipFile(archive) as source:
                    self.assertIn(extra, [member.orig_filename for member in source.infolist()])
                with self.assertRaises(ValueError):
                    runtime.unpack_runtime(archive, folder / 'MLRuntime', entry)
                self.assertFalse((folder / 'MLRuntime').exists())

    def test_normalized_windows_backslash_rejected_using_original_header_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            name = 'tools/Lib\\escape'
            archive, _entry = self.archive(folder, extra=(name,))
            # Exercise Windows' ZipInfo separator normalization on every test
            # host while preserving a real archive with the malformed name.
            with patch.object(zipfile.os, 'sep', '\\'), patch.object(zipfile.os, 'altsep', '/'):
                with zipfile.ZipFile(archive) as source:
                    member = source.infolist()[-1]
                    self.assertEqual(member.orig_filename, name)
                    self.assertEqual(member.filename, 'tools/Lib/escape')
                    with self.assertRaisesRegex(ValueError, 'Normalized'):
                        runtime.runtime_members(source)

    def test_missing_runtime_or_incorrect_publisher_metadata_rejected(self):
        for options in ({'missing': 'Lib/venv/__init__.py'}, {'author': 'untrusted'}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                archive, entry = self.archive(folder, **options)
                with self.assertRaises(ValueError):
                    runtime.unpack_runtime(archive, folder / 'MLRuntime', entry)
                self.assertFalse((folder / 'MLRuntime').exists())

    def test_archive_link_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            archive, entry = self.archive(folder)
            member = zipfile.ZipInfo('tools/unsafe')
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, 'a') as output:
                output.writestr(member, '../../outside')
            entry = dict(size=archive.stat().st_size, sha256=runtime.sha256(archive))
            with self.assertRaisesRegex(ValueError, 'Links'):
                runtime.unpack_runtime(archive, folder / 'MLRuntime', entry)
            self.assertFalse((folder / 'MLRuntime').exists())

    def test_publisher_check_uses_fixed_os_shell_and_literal_path_data(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
        runtime.verify_publisher(Path('sample & data'), run=run,
            clean_env=lambda: {'PSModulePath': 'untrusted', 'PATH': 'untrusted'})
        command, kwargs = calls[0]
        self.assertIn('System32', str(command[0]))
        self.assertEqual(command[1:4], ['-NoProfile', '-NonInteractive', '-Command'])
        self.assertNotIn('sample & data', command[-1])
        self.assertIn('Python Software Foundation', command[-1])
        self.assertNotIn('PSModulePath', kwargs['env'])
        self.assertEqual(len(json.loads(kwargs['env']['UNIFIED_PSF_BINARIES'])), 3)

    def test_managed_probe_uses_os_only_path_and_checks_latest_libraries_and_venv(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            def run(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(stdout=json.dumps(dict(version='3.14.10', prefix=str(folder), executable='sample')))
            identity = runtime.probe_runtime(folder, folder, run=run, clean_env=lambda: {
                'PATH': 'untrusted', 'PYTHONHOME': 'untrusted', 'VIRTUAL_ENV': 'untrusted',
                'PIP_EXTRA_INDEX_URL': 'untrusted'})
        self.assertEqual(len(calls), 4)
        for command, kwargs in calls:
            self.assertNotIn('untrusted', kwargs['env'].get('PATH', ''))
            self.assertFalse(any(name.startswith('PIP_') for name in kwargs['env']))
            self.assertNotIn('VIRTUAL_ENV', kwargs['env'])
            self.assertEqual(command[1:3], ['-I', '-B'])
        self.assertIn('5.8.4', calls[0][0][-1])
        self.assertIn('3.53.4', calls[0][0][-1])
        self.assertIn('(3,5,0,9,0)', calls[0][0][-1])
        self.assertIn('zlib.ZLIBNG_VERSION=="2.2.4"', calls[0][0][-1])
        self.assertEqual(identity['version'], '3.14.10')


class ModernNativeTests(unittest.TestCase):
    def project_fixture(self, folder):
        source = folder / 'python'
        (source / 'PCbuild').mkdir(parents=True)
        (source / 'Modules').mkdir()
        (source / 'Modules/_lzmamodule.c').write_text('/* trusted source fixture */')
        xz = folder / 'xz'
        names = [f'src/liblzma/common/source{i}.c' for i in range(65)]
        names += ['src/liblzma/common/common.c', 'src/liblzma/check/crc32_table.c', 'src/liblzma/check/crc64_table.c']
        for name in names + ['src/liblzma/simple/arm64.c']:
            target = xz / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('/* trusted source fixture */')
        project = source / 'PCbuild/liblzma.vcxproj'
        project.write_text('<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
            '<ItemGroup>' + ''.join('<ClCompile Include="$(lzmaDir)' + name.replace('/', '\\') + '"/>'
                for name in names) + '</ItemGroup></Project>')
        return source, xz, project

    def test_native_project_reuses_current_source_list_with_needed_xz_adaptations(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source, xz, project = self.project_fixture(folder)
            names = {file.name for file in native.source_files(project, xz)}
            self.assertNotIn('crc32_table.c', names)
            self.assertNotIn('crc64_table.c', names)
            self.assertIn('arm64.c', names)
            self.assertIn('common.c', names)
            output = native.write_project(folder, source, xz, folder / 'MLRuntime', 'v145')
            text = output.read_text()
            self.assertIn('python314.lib', text)
            self.assertNotIn('python311.lib', text)
            self.assertIn('/EXPORT:lzma_version_string', text)
            self.assertIn('MultiThreadedDLL', text)
            self.assertIn('NDEBUG;Py_NO_LINK_LIB=1;', text)
            self.assertNotIn('ProjectReference', text)
            self.assertNotIn('LocalAppDataPlatform', text)

    def test_native_source_list_fails_closed_on_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, xz, project = self.project_fixture(Path(temporary))
            (xz / 'src/liblzma/common/source0.c').unlink()
            with self.assertRaisesRegex(ValueError, 'pinned XZ'):
                native.source_files(project, xz)


if __name__ == '__main__':
    unittest.main()
