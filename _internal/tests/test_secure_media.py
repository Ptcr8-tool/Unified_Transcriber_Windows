"""Security boundaries for the Windows application-owned media tool pair."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_tools
import secure_media as media


def digest(value):
    return hashlib.sha256(value).hexdigest()


class SecureMediaTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / '_internal/environments/media-tools'
        self.binaries = {'ffmpeg.exe': b'MZ-pinned-ffmpeg', 'ffprobe.exe': b'MZ-pinned-ffprobe'}
        self.notices = {'LICENSE': b'GPL fixture', 'README.txt': b'source fixture'}
        self.prefix = f'ffmpeg-{media.VERSION}-essentials_build/'

    def archive(self, *, extra=(), missing=(), altered=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
            for name, data in self.binaries.items():
                if name not in missing:
                    zipped.writestr(self.prefix + 'bin/' + name, (altered or {}).get(name, data))
            for name, data in self.notices.items():
                zipped.writestr(self.prefix + name, data)
            # Safe unused members must never be extracted or executed.
            zipped.writestr(self.prefix + 'bin/ffplay.exe', b'MZ-unused')
            for name, data in extra:
                if isinstance(name, str):
                    # ZipInfo normalizes native Windows separators on creation.
                    # Preserve the attack spelling in the actual ZIP bytes.
                    info = zipfile.ZipInfo(name)
                    info.filename = info.orig_filename = name
                else:
                    info = name
                zipped.writestr(info, data)
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as zipped:
            for name, _ in extra:
                expected = name if isinstance(name, str) else name.orig_filename
                self.assertIn(expected, [info.orig_filename for info in zipped.infolist()])
        return output.getvalue()

    @contextmanager
    def pins(self, archive=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(media, 'BINARY_SHA256', {name: digest(data) for name, data in self.binaries.items()}))
            stack.enter_context(patch.object(media, 'NOTICE_SHA256', {name: digest(data) for name, data in self.notices.items()}))
            if archive is not None:
                stack.enter_context(patch.object(media, 'ARCHIVE_SHA256', digest(archive)))
            yield

    def write_pair(self):
        self.target.mkdir(parents=True)
        for name, data in self.binaries.items():
            (self.target / name).write_bytes(data)

    def existing_old(self):
        self.target.mkdir(parents=True)
        (self.target / 'ffmpeg.exe').write_bytes(b'old-working-version')
        (self.target / 'user-preserved.txt').write_bytes(b'old-content')

    def assert_old_preserved(self):
        self.assertEqual((self.target / 'ffmpeg.exe').read_bytes(), b'old-working-version')
        self.assertEqual((self.target / 'user-preserved.txt').read_bytes(), b'old-content')
        self.assertFalse(list(self.target.parent.glob('.media-tools-stage-*')))
        self.assertFalse(list(self.target.parent.glob('.media-tools-backup-*')))

    def install(self, archive, *, cancel=None, output_version=None):
        def download(path, **kwargs):
            path.write_bytes(archive)
        def run(command, **kwargs):
            binary = Path(command[0]).stem
            return subprocess.CompletedProcess(command, 0,
                f'{binary} version {output_version or media.VERSION}-essentials_build-www.gyan.dev Copyright\n', '')
        return (patch.object(media, '_download_archive', side_effect=download),
                patch.object(media, 'run_cancellable', side_effect=run))

    def test_lookup_requires_both_code_pinned_hashes_ignores_receipt(self):
        self.write_pair()
        with self.pins():
            self.assertEqual(media.verified_media_binary(self.root, 'ffmpeg'), str(self.target / 'ffmpeg.exe'))
            (self.target / 'ffprobe.exe').write_bytes(b'changed')
            (self.target / 'verified-media.json').write_text(json.dumps({'binaries': {
                'ffmpeg.exe': digest(self.binaries['ffmpeg.exe']), 'ffprobe.exe': digest(b'changed')}}))
            self.assertIsNone(media.verified_media_binary(self.root, 'ffmpeg'))
            self.assertIsNone(media.verified_media_binary(self.root, '../ffmpeg'))

    def test_lookup_rejects_symlinks_and_symlink_parent(self):
        self.write_pair()
        alternate = self.root / 'alternate'
        alternate.write_bytes(self.binaries['ffmpeg.exe'])
        (self.target / 'ffmpeg.exe').unlink()
        try:
            (self.target / 'ffmpeg.exe').symlink_to(alternate)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks require Windows developer mode or privileges.')
        with self.pins():
            self.assertIsNone(media.verified_media_binary(self.root, 'ffmpeg'))

    def test_lookup_and_install_reject_linked_environment_parent(self):
        internal = self.root / '_internal'
        internal.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (internal / 'environments').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks require Windows developer mode or privileges.')
        with self.pins(), patch.object(media, '_download_archive') as download:
            self.assertIsNone(media.verified_media_binary(self.root, 'ffmpeg'))
            with self.assertRaises(media.MediaToolsError):
                media.ensure_media_tools(self.root, log=lambda _: None)
            download.assert_not_called()
        self.assertFalse(list(outside.iterdir()))

    def test_install_extracts_only_fixed_members_checks_both_versions_and_is_idempotent(self):
        archive = self.archive()
        download, runner = self.install(archive)
        with self.pins(archive), download as fetched, runner as executed:
            self.assertEqual(media.ensure_media_tools(self.root, log=lambda _: None), self.target)
            self.assertEqual({path.name for path in self.target.iterdir()}, {
                'ffmpeg.exe', 'ffprobe.exe', 'LICENSE', 'README.txt', 'verified-media.json'})
            self.assertEqual(executed.call_count, 2)
            self.assertEqual([Path(call.args[0][0]).name for call in executed.call_args_list], ['ffmpeg.exe', 'ffprobe.exe'])
            self.assertTrue(all(call.args[0][1:] == ['-version'] for call in executed.call_args_list))
            self.assertEqual(media.ensure_media_tools(self.root, log=lambda _: None), self.target)
            self.assertEqual(fetched.call_count, 1)
            self.assertEqual(executed.call_count, 2)

    def test_bad_archive_hash_never_executes_or_replaces_previous_installation(self):
        self.existing_old()
        good = self.archive()
        download, runner = self.install(good + b'changed')
        with self.pins(good), download, runner as executed:
            with self.assertRaises(media.MediaToolsError):
                media.ensure_media_tools(self.root, log=lambda _: None)
            executed.assert_not_called()
        self.assert_old_preserved()

    def test_bad_binary_hash_never_executes_or_replaces_previous_installation(self):
        self.existing_old()
        archive = self.archive(altered={'ffprobe.exe': b'untrusted'})
        download, runner = self.install(archive)
        with self.pins(archive), download, runner as executed:
            with self.assertRaises(media.MediaToolsError):
                media.ensure_media_tools(self.root, log=lambda _: None)
            executed.assert_not_called()
        self.assert_old_preserved()

    def test_wrong_version_keeps_previous_installation(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive, output_version='7.1')
        with self.pins(archive), download, runner:
            with self.assertRaises(media.MediaToolsError):
                media.ensure_media_tools(self.root, log=lambda _: None)
        self.assert_old_preserved()

    def test_cancel_during_download_keeps_previous_installation(self):
        self.existing_old()
        archive, cancel = self.archive(), threading.Event()
        def download(path, **kwargs):
            path.write_bytes(archive)
            cancel.set()
        with self.pins(archive), patch.object(media, '_download_archive', side_effect=download), patch.object(media, 'run_cancellable') as executed:
            with self.assertRaises(InterruptedError):
                media.ensure_media_tools(self.root, cancel=cancel, log=lambda _: None)
            executed.assert_not_called()
        self.assert_old_preserved()

    def test_failed_atomic_publish_rolls_back_previous_directory(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        replace = media.os.replace
        def replace_except_publish(source, destination):
            if Path(source).name == 'payload':
                raise OSError('injected publish failure')
            return replace(source, destination)
        with self.pins(archive), download, runner, patch.object(media.os, 'replace', side_effect=replace_except_publish):
            with self.assertRaises(OSError):
                media.ensure_media_tools(self.root, log=lambda _: None)
        self.assert_old_preserved()

    @staticmethod
    def windows_error(code=5):
        error = PermissionError('injected Windows access/sharing denial')
        error.winerror = code
        return error

    def test_transient_windows_publish_denial_retries_and_rechecks_hashes(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        replace, attempts, logs = media.os.replace, [], []
        def replace_with_delay(source, destination):
            if Path(source).name == 'payload':
                attempts.append(Path(source))
                if len(attempts) <= 2:
                    raise self.windows_error(32 if len(attempts) == 1 else 5)
            return replace(source, destination)
        with self.pins(archive), download, runner, \
                patch.object(media.os, 'replace', side_effect=replace_with_delay), \
                patch.object(media.time, 'sleep'), \
                patch.object(media, '_verified_pair', wraps=media._verified_pair) as verify:
            self.assertEqual(media.ensure_media_tools(self.root, log=logs.append), self.target)
            payload_checks = [call for call in verify.call_args_list
                              if Path(call.args[0]).name == 'payload']
            self.assertGreaterEqual(len(payload_checks), 4)  # probe + every move attempt
            self.assertIsNotNone(media.verified_media_binary(self.root, 'ffmpeg'))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len([message for message in logs if '再試行' in message]), 1)
        self.assertFalse(list(self.target.parent.glob('.media-tools-backup-*')))

    def test_persistent_windows_denial_is_bounded_and_restores_old_tools(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        replace = media.os.replace
        def deny_publish(source, destination):
            if Path(source).name == 'payload':
                raise self.windows_error()
            return replace(source, destination)
        with self.pins(archive), download, runner, \
                patch.object(media.os, 'replace', side_effect=deny_publish), \
                patch.object(media, 'REPLACE_TIMEOUT', 0):
            with self.assertRaisesRegex(media.MediaToolsError, 'Windows エラー 5'):
                media.ensure_media_tools(self.root, log=lambda _: None)
        self.assert_old_preserved()

    def test_cancel_during_publish_retry_restores_old_tools_without_honoring_cancel(self):
        self.existing_old()
        archive, cancel = self.archive(), threading.Event()
        download, runner = self.install(archive)
        replace = media.os.replace
        def deny_and_cancel(source, destination):
            if Path(source).name == 'payload':
                cancel.set()
                raise self.windows_error(33)
            return replace(source, destination)
        with self.pins(archive), download, runner, \
                patch.object(media.os, 'replace', side_effect=deny_and_cancel), \
                patch.object(media.time, 'sleep'):
            with self.assertRaises(InterruptedError):
                media.ensure_media_tools(self.root, cancel=cancel, log=lambda _: None)
        self.assert_old_preserved()

    def test_changed_payload_during_retry_is_rejected_and_old_tools_restored(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        replace, attempts = media.os.replace, []
        def change_payload(source, destination):
            if Path(source).name == 'payload':
                attempts.append(source)
                (Path(source) / 'ffmpeg.exe').write_bytes(b'substituted binary')
                raise self.windows_error()
            return replace(source, destination)
        with self.pins(archive), download, runner, \
                patch.object(media.os, 'replace', side_effect=change_payload), \
                patch.object(media.time, 'sleep'):
            with self.assertRaisesRegex(media.MediaToolsError, 'SHA256'):
                media.ensure_media_tools(self.root, log=lambda _: None)
        self.assertEqual(len(attempts), 1)
        self.assert_old_preserved()

    def test_failed_rollback_keeps_backup_and_reports_recovery_directory(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        replace = media.os.replace
        def deny_publish_and_restore(source, destination):
            if Path(source).name == 'payload' or Path(source).name.startswith('.media-tools-backup-'):
                raise self.windows_error()
            return replace(source, destination)
        with self.pins(archive), download, runner, \
                patch.object(media.os, 'replace', side_effect=deny_publish_and_restore), \
                patch.object(media, 'REPLACE_TIMEOUT', 0):
            with self.assertRaises(media.MediaToolsError) as failure:
                media.ensure_media_tools(self.root, log=lambda _: None)
        backups = list(self.target.parent.glob('.media-tools-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), str(failure.exception))
        self.assertEqual((backups[0] / 'user-preserved.txt').read_bytes(), b'old-content')
        self.assertFalse(list(self.target.parent.glob('.media-tools-stage-*')))

    def test_retry_rejects_replaced_source_directory_and_unexpected_destination(self):
        for substitution in ('source', 'destination'):
            with self.subTest(substitution=substitution), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                source, destination = root / 'source', root / 'destination'
                source.mkdir()
                def change_paths(*args):
                    if substitution == 'source':
                        # Retain the original so the filesystem cannot reuse its inode.
                        source.rename(root / 'retained')
                        source.mkdir()
                    else:
                        destination.mkdir()
                    raise self.windows_error()
                with patch.object(media.os, 'replace', side_effect=change_paths) as replace, \
                        patch.object(media.time, 'sleep'):
                    with self.assertRaises(media.MediaToolsError):
                        media._replace_directory(source, destination, log=lambda _: None)
                replace.assert_called_once()

    def test_non_windows_or_unrelated_rename_errors_are_not_retried(self):
        for error in (PermissionError('POSIX permission denied'), OSError('disk full'), self.windows_error(17)):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                source = root / 'source'
                source.mkdir()
                with patch.object(media.os, 'replace', side_effect=error) as replace, \
                        patch.object(media.time, 'sleep') as sleep:
                    with self.assertRaises(OSError):
                        media._replace_directory(source, root / 'target', log=lambda _: None)
                replace.assert_called_once()
                sleep.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Requires native Windows sharing semantics.')
    def test_native_windows_directory_handle_without_delete_share_retries_then_installs(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        release, handles, logs = threading.Event(), [], []
        probe = media._probe_version
        def lock_probed_payload(folder, **kwargs):
            probe(folder, **kwargs)
            # An ordinary directory handle with read/write sharing but no
            # FILE_SHARE_DELETE causes a real Windows rename denial. Request
            # GENERIC_READ: a metadata-only (desiredAccess=0) handle can bypass
            # the filesystem's read/write/delete share accounting.
            handle = create(str(folder), 0x80000000, 1 | 2, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            # Establish the native sharing-lock precondition explicitly. This
            # direct rename is never mocked and never releases the handle;
            # the production retry callback below is the only release signal.
            try:
                os.replace(folder, folder.with_name('locked-payload-probe'))
            except OSError as exc:
                self.assertIn(getattr(exc, 'winerror', None), (5, 32, 33))
            else:
                self.fail('The native directory handle did not block os.replace.')
        def log(message):
            logs.append(message)
            if '再試行' in message:
                release.set()
        def unlock():
            if release.wait(10) and handles:
                kernel.CloseHandle(handles.pop())
        thread = threading.Thread(target=unlock)
        thread.start()
        try:
            with self.pins(archive), download, runner, \
                    patch.object(media, '_probe_version', side_effect=lock_probed_payload):
                self.assertEqual(media.ensure_media_tools(self.root, log=log), self.target)
                self.assertIsNotNone(media.verified_media_binary(self.root, 'ffmpeg'))
            self.assertTrue(any('再試行' in message for message in logs))
            self.assertFalse(list(self.target.parent.glob('.media-tools-backup-*')))
        finally:
            release.set()
            thread.join(12)
            for handle in handles:
                kernel.CloseHandle(handle)

    def test_cancel_immediately_before_commit_keeps_old_tools(self):
        self.existing_old()
        archive, cancel = self.archive(), threading.Event()
        download, runner = self.install(archive)
        with self.pins(archive), download, runner, \
                patch.object(media, '_probe_version', side_effect=lambda *args, **kwargs: cancel.set()):
            with self.assertRaises(InterruptedError):
                media.ensure_media_tools(self.root, cancel=cancel, log=lambda _: None)
        self.assert_old_preserved()

    def test_locked_old_backup_cleanup_does_not_fail_committed_installation(self):
        self.existing_old()
        archive = self.archive()
        download, runner = self.install(archive)
        rmtree = shutil.rmtree
        def fail_backup(path, *args, **kwargs):
            if Path(path).name.startswith('.media-tools-backup-'):
                raise PermissionError('old executable is temporarily locked')
            return rmtree(path, *args, **kwargs)
        logs = []
        with self.pins(archive), download, runner, patch.object(media.shutil, 'rmtree', side_effect=fail_backup):
            self.assertEqual(media.ensure_media_tools(self.root, log=logs.append), self.target)
            self.assertIsNotNone(media.verified_media_binary(self.root, 'ffmpeg'))
        self.assertTrue(any('一時保存先' in message for message in logs))

    def test_cross_process_lock_is_bounded_and_cancel_is_prompt(self):
        self.target.parent.mkdir(parents=True)
        code = ('import sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); import secure_media as media; '
                'media.LOCK_TIMEOUT=.1\n'
                'try:\n with media._install_lock(Path(sys.argv[2])): raise AssertionError("lock bypass")\n'
                'except TimeoutError: print("blocked")\n')
        with media._install_lock(self.target.parent):
            result = subprocess.run([sys.executable, '-c', code, str(Path(media.__file__).parent),
                str(self.target.parent)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'blocked')
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(InterruptedError), media._install_lock(self.target.parent, cancel=cancel):
                self.fail('a cancelled lock must never be acquired')

    def test_archive_rejects_traversal_ads_absolute_case_duplicates_and_links_before_execution(self):
        link = zipfile.ZipInfo('safe-link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        reparse = zipfile.ZipInfo('reparse')
        reparse.external_attr = 0x400
        variants = ['../escape', '/absolute', 'C:/escape', 'dir\\escape',
                    'dir/../escape', 'file:alternate', 'dir/NUL.txt', 'trailing. ',
                    self.prefix + 'bin/FFMPEG.EXE', link, reparse]
        for unsafe in variants:
            with self.subTest(path=str(unsafe)):
                archive = self.archive(extra=[(unsafe, b'evil')])
                download, runner = self.install(archive)
                with self.pins(archive), download, runner as executed:
                    with self.assertRaises(media.MediaToolsError):
                        media.ensure_media_tools(self.root, log=lambda _: None)
                    executed.assert_not_called()
                self.assertFalse(self.target.exists())
                self.assertFalse((self.root.parent / 'escape').exists())

    def test_archive_missing_binary_and_oversize_total_are_rejected(self):
        for missing, maximum in [(('ffprobe.exe',), media.MAX_UNPACKED_BYTES), ((), 1)]:
            with self.subTest(missing=missing, maximum=maximum):
                archive = self.archive(missing=missing)
                download, runner = self.install(archive)
                with self.pins(archive), download, runner as executed, patch.object(media, 'MAX_UNPACKED_BYTES', maximum):
                    with self.assertRaises(media.MediaToolsError):
                        media.ensure_media_tools(self.root, log=lambda _: None)
                    executed.assert_not_called()

    def test_source_manifest_mirrors_embedded_trust_anchor(self):
        source = Path(media.__file__).with_name('ffmpeg-windows-sources.json')
        value = json.loads(source.read_text(encoding='utf-8'))
        self.assertEqual(value['version'], media.VERSION)
        self.assertEqual(value['url'], media.DOWNLOAD_URL)
        self.assertEqual(value['sha256'], media.ARCHIVE_SHA256)
        self.assertEqual(value['binaries'], media.BINARY_SHA256)
        self.assertEqual(value['notices'], media.NOTICE_SHA256)

    def test_windows_media_resolution_cannot_fall_back_to_path_or_imageio(self):
        # Patch only the module's os reference, avoiding pathlib's global OS dispatch.
        with patch.object(media_tools, 'os', SimpleNamespace(name='nt')), \
                patch.object(media, 'verified_media_binary', return_value=None) as verified, \
                patch.object(media_tools.shutil, 'which') as which:
            self.assertIsNone(media_tools.media_binary('ffmpeg', self.root / 'Scripts/python.exe'))
            verified.assert_called_once()
            which.assert_not_called()


class DownloadBoundaryTest(unittest.TestCase):
    def response(self, data, *, headers=None, url=None, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = url or media.DOWNLOAD_URL
        response.status = status
        response.headers = headers or {}
        response.read1.side_effect = [data, b'']
        return response

    def test_download_requests_identity_checks_exact_url_and_archive_hash(self):
        data = b'trusted archive bytes'
        response = self.response(data, headers={'Content-Length': str(len(data))})
        with tempfile.TemporaryDirectory() as td, patch.object(media, 'ARCHIVE_SHA256', digest(data)), \
                patch.object(media.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            destination = Path(td) / 'download.zip'
            media._download_archive(destination)
            self.assertEqual(destination.read_bytes(), data)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Accept-encoding'), 'identity')
            self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 15)

    def test_download_rejects_wrong_url_encoding_declared_size_stream_size_and_hash(self):
        data = b'wrong bytes'
        variants = [({'url': 'https://evil.test/archive.zip'}, media.MAX_ARCHIVE_BYTES),
            ({'headers': {'Content-Encoding': 'gzip'}}, media.MAX_ARCHIVE_BYTES),
            ({'headers': {'Content-Length': str(media.MAX_ARCHIVE_BYTES + 1)}}, media.MAX_ARCHIVE_BYTES),
            ({'headers': {'Content-Length': '1'}}, media.MAX_ARCHIVE_BYTES),
            ({}, 1), ({}, media.MAX_ARCHIVE_BYTES)]
        for kwargs, maximum in variants:
            with self.subTest(response=kwargs, maximum=maximum), tempfile.TemporaryDirectory() as td, \
                    patch.object(media, 'MAX_ARCHIVE_BYTES', maximum), \
                    patch.object(media.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = self.response(data, **kwargs)
                with self.assertRaises(media.MediaToolsError):
                    media._download_archive(Path(td) / 'download.zip')

    def test_redirect_handler_rejects_unpinned_and_non_https_locations(self):
        handler = media._PinnedRedirect()
        for url in ('http://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip',
                    'https://evil.test/archive.zip', media.DOWNLOAD_URL + '?changed=1'):
            with self.subTest(url=url), self.assertRaises(media.MediaToolsError):
                handler.redirect_request(None, None, 302, '', {}, url)

    def test_slow_download_total_timeout_is_checked_after_each_socket_read(self):
        response = self.response(b'drip')
        with tempfile.TemporaryDirectory() as td, patch.object(media, 'DOWNLOAD_TIMEOUT', 1), \
                patch.object(media.time, 'monotonic', side_effect=[0, .5, 2]), \
                patch.object(media.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            with self.assertRaises(TimeoutError):
                media._download_archive(Path(td) / 'download.zip')
            response.read1.assert_called_once_with(64 * 1024)


if __name__ == '__main__':
    unittest.main()
