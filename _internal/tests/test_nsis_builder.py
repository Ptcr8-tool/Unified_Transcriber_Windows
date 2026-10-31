from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import nsis_builder as builder


def zip_data(extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as output:
        for name in ('Bin/makensis.exe', 'Include/LogicLib.nsh',
                     'Plugins/x86-unicode/System.dll', 'COPYING'):
            output.writestr('nsis-3.13/' + name, b'test')
        for name, value in extra:
            output.writestr(name, value)
    stream.seek(0)
    return zipfile.ZipFile(stream)


class PinnedNsisTests(unittest.TestCase):
    def test_download_urls_cannot_escape_official_project(self):
        for url in (builder.NSIS_URL,
                    'https://altushost-swe.dl.sourceforge.net/project/nsis/NSIS%203/3.13/nsis-3.13.zip?viasf=1'):
            builder._allowed_url(url)
        for url in ('http://downloads.sourceforge.net/project/nsis/NSIS%203/3.13/nsis-3.13.zip',
                    'https://downloads.sourceforge.net.evil.test/project/nsis/NSIS%203/3.13/nsis-3.13.zip',
                    'https://evil.test/x.dl.sourceforge.net/project/nsis/NSIS%203/3.13/nsis-3.13.zip',
                    'https://downloads.sourceforge.net:444/project/nsis/NSIS%203/3.13/nsis-3.13.zip',
                    'https://user@downloads.sourceforge.net/project/nsis/NSIS%203/3.13/nsis-3.13.zip',
                    'https://downloads.sourceforge.net/project/nsis/NSIS%203/3.11/nsis-3.11.zip'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                builder._allowed_url(url)

    def test_changed_cached_compiler_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'compiler.zip'
            path.write_bytes(b'untrusted')
            with self.assertRaisesRegex(ValueError, 'pinned'):
                builder.download_nsis(path)
            with self.assertRaisesRegex(ValueError, 'pinned'):
                builder.extract_nsis(path, Path(directory) / 'extract')
            self.assertFalse((Path(directory) / 'extract').exists())

    def test_notice_is_copied_from_verified_archive_and_modified_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'compiler.zip'
            with zip_data() as source:
                archive.write_bytes(source.fp.getvalue())
            with mock.patch.object(builder, 'NSIS_BYTES', archive.stat().st_size), \
                    mock.patch.object(builder, 'NSIS_SHA256', builder.digest(archive)):
                notice = root / 'installed/_internal/NSIS-LICENSE.txt'
                builder.copy_nsis_license(archive, notice)
                self.assertEqual(notice.read_bytes(), b'test')
                archive.write_bytes(b'not a trusted archive')
                with self.assertRaisesRegex(ValueError, 'pinned'):
                    builder.copy_nsis_license(archive, root / 'other.txt')
                self.assertFalse((root / 'other.txt').exists())

    def test_only_complete_regular_tool_archive_is_accepted(self):
        with zip_data() as source:
            self.assertEqual(len(builder.safe_members(source)), 4)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as output:
            output.writestr('nsis-3.13/Bin/makensis.exe', b'only one')
        with zipfile.ZipFile(stream) as source, self.assertRaisesRegex(ValueError, 'missing'):
            builder.safe_members(source)

    def test_malicious_zip_names_and_case_collisions_are_rejected(self):
        for name in ('nsis-3.13/../outside', '/absolute', 'nsis-3.13/name:stream',
                     'nsis-3.13/CON.txt', 'nsis-3.13/folder./file',
                     'nsis-3.13/folder /file', 'nsis-3.13//file',
                     'nsis-3.13/folder//', 'nsis-3.13/invalid?file',
                     'nsis-3.13/\\outside', 'nsis-3.13/bin/MAKENSIS.EXE',
                     'nsis-3.13/COPYING/child'):
            with self.subTest(name=name), zip_data([(name, b'bad')]) as source:
                with self.assertRaises(ValueError):
                    builder.safe_members(source)

    def test_zip_links_and_expansion_limit_are_rejected(self):
        with zip_data() as source:
            item = source.infolist()[0]
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                builder.safe_members(source)
        with zip_data() as source:
            source.infolist()[0].file_size = 32 * 1024 * 1024
            with self.assertRaisesRegex(ValueError, 'expansion'):
                builder.safe_members(source)


class BootstrapStagingTests(unittest.TestCase):
    def prepare(self, root):
        runtime = root / 'Runtime'
        runtime.mkdir()
        (runtime / 'pythonw.exe').write_bytes(b'MZ test runtime')
        code = root / '_internal'
        (code / 'assets/icons').mkdir(parents=True)
        (code / 'assets/icons/copy.png').write_bytes(b'icon')
        (code / 'installer_bootstrap.py').write_text('print("setup")')
        (code / 'dynamic_config.py').write_text('setting=True')
        (code / 'pinned-tools.json').write_text('{}')
        (code / 'environments').mkdir()
        (code / 'environments/private.txt').write_text('must not copy')
        (code / 'update-verifier').mkdir()
        (code / 'update-verifier/gh.exe').write_bytes(b'not needed by wizard')
        (code / 'tests').mkdir()
        (code / 'tests/local.py').write_text('not distributed')
        archive = root / 'payload.zip'
        archive.write_bytes(b'payload')
        info = root / 'BUILD_INFO.json'
        info.write_text(json.dumps({'version': '2.1.2', 'payload_size': archive.stat().st_size,
                                    'payload_sha256': builder.digest(archive)}))
        return dict(runtime=runtime, archive=archive, info=info, code=code,
                    target=root / 'bootstrap', version='2.1.2')

    def test_dynamic_modules_are_preserved_but_private_environments_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.prepare(Path(directory))
            target = builder.stage_bootstrap(**args)
            names = {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}
            self.assertEqual(names, {'Runtime/pythonw.exe', '_internal/installer_bootstrap.py',
                '_internal/dynamic_config.py', '_internal/pinned-tools.json',
                '_internal/assets/icons/copy.png', 'payload.zip', 'BUILD_INFO.json'})

    def test_mismatched_payload_fails_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.prepare(Path(directory))
            args['archive'].write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'do not agree'):
                builder.stage_bootstrap(**args)
            self.assertFalse(args['target'].exists())

    def test_existing_staging_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.prepare(Path(directory))
            args['target'].mkdir()
            (args['target'] / 'keep').write_text('keep')
            with self.assertRaisesRegex(ValueError, 'new bootstrap'):
                builder.stage_bootstrap(**args)
            self.assertEqual((args['target'] / 'keep').read_text(), 'keep')

    def test_build_path_preprocessor_escaping(self):
        self.assertIn('$TEMP', builder.nsis_literal(Path('build-$TEMP')))
        self.assertNotIn('$$TEMP', builder.nsis_literal(Path('build-$TEMP')))
        for name in ('unsafe\n!include other.nsi', 'unsafe"path', '${OTHER}', '$%USER%'):
            with self.assertRaises(ValueError):
                builder.nsis_literal(Path(name))


class ContainerChecks(unittest.TestCase):
    def test_real_output_gate_rejects_console_and_pyinstaller_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'setup.exe'
            image = bytearray(4096)
            image[:2] = b'MZ'
            struct.pack_into('<I', image, 60, 128)
            image[128:132] = b'PE\0\0'
            struct.pack_into('<H', image, 132, 0x014c)
            struct.pack_into('<H', image, 152, 0x010b)
            struct.pack_into('<H', image, 152 + 68, 2)
            image[2048:2060] = b'NullsoftInst'
            path.write_bytes(image)
            self.assertEqual(builder.inspect_bootstrap(path)['format'], 'NSIS')
            image[-16:-8] = b'MEI\x0c\x0b\x0a\x0b\x0e'
            path.write_bytes(image)
            with self.assertRaises(ValueError):
                builder.inspect_bootstrap(path)
            image[-16:-8] = bytes(8)
            struct.pack_into('<H', image, 152 + 68, 3)
            path.write_bytes(image)
            with self.assertRaises(ValueError):
                builder.inspect_bootstrap(path)


if __name__ == '__main__':
    unittest.main()
