"""Pinned, application-owned FFmpeg for Windows (standard library only).

The code pins, not the writable installation receipt, authenticate executables.
Only the fixed pair and upstream license/readme are copied from a verified ZIP.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile

from process_control import check_cancel, run_cancellable


VERSION = '9.0.2'
DOWNLOAD_URL = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip'
ARCHIVE_SHA256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
BINARY_SHA256 = {
    'ffmpeg.exe': '3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec',
    'ffprobe.exe': 'f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52',
}
NOTICE_SHA256 = {
    'LICENSE': '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903',
    'README.txt': '0342de6bb39dd421cbec2c00d89ad274b640d75ad3f2cab09ffd647ce2d8f949',
}
MAX_ARCHIVE_BYTES = 140 * 1024 ** 2
MAX_FILE_BYTES = 128 * 1024 ** 2
MAX_UNPACKED_BYTES = 600 * 1024 ** 2
MAX_MEMBERS = 1000
DOWNLOAD_TIMEOUT = 300
LOCK_TIMEOUT = 360
REPLACE_TIMEOUT = 12
REPLACE_INTERVAL = .1
_REPARSE = 0x400
_RESERVED = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z', re.I)


class MediaToolsError(ValueError):
    """The media tools could not be safely verified or installed."""


def _ordinary(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & _REPARSE):
        raise MediaToolsError('FFmpeg の保存先にリンクや特殊ファイルは使用できません。')
    return info


def _base_path(base: Path) -> Path:
    base = Path(base).expanduser().absolute()
    # Check before resolve(), so a junction cannot disappear through normalization.
    for path in (base, *base.parents):
        _ordinary(path, directory=True)
    return base.resolve()


def _digest(path: Path, *, cancel=None, maximum: int = MAX_FILE_BYTES) -> str:
    if _ordinary(path).st_size > maximum:
        raise MediaToolsError('FFmpeg ファイルのサイズが上限を超えています。')
    value = hashlib.sha256()
    total = 0
    with path.open('rb') as stream:
        while True:
            check_cancel(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise MediaToolsError('FFmpeg ファイルのサイズが上限を超えています。')
            value.update(block)
    return value.hexdigest()


def _verified_pair(folder: Path, *, cancel=None) -> bool:
    try:
        _ordinary(folder, directory=True)
        for name, expected in BINARY_SHA256.items():
            if _digest(folder / name, cancel=cancel) != expected:
                return False
        return True
    except (OSError, MediaToolsError):
        return False


def verified_media_binary(base: Path, binary: str) -> str | None:
    """Return only a pinned executable, requiring the complete verified pair.

    No executable is launched while locating tools, and local receipts are never
    a trust anchor. Modified executables therefore cannot bless their own hashes.
    """
    if binary not in ('ffmpeg', 'ffprobe'):
        return None
    try:
        root = _base_path(base)
        _ordinary(root / '_internal', directory=True)
        _ordinary(root / '_internal' / 'environments', directory=True)
        folder = root / '_internal' / 'environments' / 'media-tools'
        if _verified_pair(folder):
            return str(folder / (binary + '.exe'))
    except (OSError, MediaToolsError):
        pass
    return None


class _PinnedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if newurl != DOWNLOAD_URL:
            raise MediaToolsError('FFmpeg の配布 URL が固定された取得先から変更されました。')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _download_archive(destination: Path, *, cancel=None) -> None:
    request = urllib.request.Request(DOWNLOAD_URL, headers={
        'Accept-Encoding': 'identity', 'User-Agent': 'UnifiedTranscriber/secure-media',
    })
    opener = urllib.request.build_opener(_PinnedRedirect())
    started = time.monotonic()
    value = hashlib.sha256()
    total = 0
    check_cancel(cancel)
    with opener.open(request, timeout=15) as response, destination.open('xb') as stream:
        if response.geturl() != DOWNLOAD_URL or response.status != 200:
            raise MediaToolsError('FFmpeg の配布 URL または HTTP 応答が不正です。')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise MediaToolsError('圧縮転送された FFmpeg は受け付けません。')
        length = response.headers.get('Content-Length')
        if length is not None:
            if not re.fullmatch(r'[0-9]{1,12}', length) or not 0 < int(length) <= MAX_ARCHIVE_BYTES:
                raise MediaToolsError('FFmpeg のダウンロードサイズが上限を超えています。')
        while True:
            check_cancel(cancel)
            if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                raise TimeoutError('FFmpeg のダウンロードがタイムアウトしました。')
            # read1 returns after one socket read; read(n) can wait indefinitely
            # for n bytes while a slow server keeps resetting the socket timeout.
            block = response.read1(64 * 1024)
            if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                raise TimeoutError('FFmpeg のダウンロードがタイムアウトしました。')
            if not block:
                break
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise MediaToolsError('FFmpeg のダウンロードサイズが上限を超えています。')
            value.update(block)
            stream.write(block)
        check_cancel(cancel)
        if length is not None and total != int(length):
            raise MediaToolsError('FFmpeg のダウンロードが完了していません。')
    if value.hexdigest() != ARCHIVE_SHA256:
        raise MediaToolsError('FFmpeg アーカイブの SHA256 が固定値と一致しません。')


def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    plain = name[:-1] if info.is_dir() else name
    path = PurePosixPath(plain)
    if (not plain or info.orig_filename != name or '\\' in name or '\x00' in name
            or path.is_absolute() or path.as_posix() != plain
            or any(part in ('.', '..') for part in path.parts)):
        raise MediaToolsError('FFmpeg ZIP 内に不正なパスが含まれています。')
    for part in path.parts:
        if (len(part) > 200 or any(ord(ch) < 32 for ch in part)
                or any(ch in ':<>"|?*' for ch in part) or part[-1:] in (' ', '.')
                or _RESERVED.fullmatch(part)):
            raise MediaToolsError('FFmpeg ZIP 内に Windows で安全に扱えないパスがあります。')
    mode = stat.S_IFMT(info.external_attr >> 16)
    if (mode not in (0, stat.S_IFREG, stat.S_IFDIR)
            or (mode == stat.S_IFDIR) != info.is_dir() and mode != 0
            or info.external_attr & _REPARSE or info.flag_bits & 1
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
        raise MediaToolsError('FFmpeg ZIP 内にリンク・特殊属性・未対応の圧縮形式があります。')
    return name


def _extract_archive(archive: Path, destination: Path, *, cancel=None) -> None:
    # This is deliberately repeated at the extraction boundary. Never inspect or
    # execute a downloaded archive based solely on a caller's claim of verification.
    if _digest(archive, cancel=cancel, maximum=MAX_ARCHIVE_BYTES) != ARCHIVE_SHA256:
        raise MediaToolsError('FFmpeg アーカイブの SHA256 が固定値と一致しません。')
    prefix = f'ffmpeg-{VERSION}-essentials_build/'
    members = {prefix + 'bin/' + name: (name, digest) for name, digest in BINARY_SHA256.items()}
    members.update({prefix + name: (name, digest) for name, digest in NOTICE_SHA256.items()})
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > MAX_MEMBERS:
            raise MediaToolsError('FFmpeg ZIP 内のファイル数が上限を超えています。')
        seen, selected, total = set(), {}, 0
        for info in entries:
            check_cancel(cancel)
            name = _member_name(info)
            if name.casefold() in seen:
                raise MediaToolsError('FFmpeg ZIP 内に重複パスがあります。')
            seen.add(name.casefold())
            total += info.file_size
            if info.file_size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES:
                raise MediaToolsError('FFmpeg ZIP の展開サイズが上限を超えています。')
            if name in members:
                if info.is_dir():
                    raise MediaToolsError('FFmpeg の固定ファイルがフォルダーになっています。')
                selected[name] = info
        if set(selected) != set(members):
            raise MediaToolsError('FFmpeg ZIP に必要な固定ファイルがありません。')
        for name, info in selected.items():
            filename, expected = members[name]
            value, count = hashlib.sha256(), 0
            with zipped.open(info) as source, (destination / filename).open('xb') as output:
                while True:
                    check_cancel(cancel)
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > min(info.file_size, MAX_FILE_BYTES):
                        raise MediaToolsError('FFmpeg ZIP の展開サイズが不正です。')
                    value.update(block)
                    output.write(block)
            if count != info.file_size or value.hexdigest() != expected:
                raise MediaToolsError('FFmpeg の固定ファイルの SHA256 が一致しません。')


@contextmanager
def _install_lock(parent: Path, *, cancel=None):
    path = parent / '.media-tools.lock'
    if path.exists() or path.is_symlink():
        _ordinary(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        before, opened = _ordinary(path), os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise MediaToolsError('FFmpeg ロックファイルが変更されました。')
        if opened.st_size == 0:
            os.write(descriptor, b'\0')
        if os.name == 'nt':
            import msvcrt
            def lock():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            def unlock():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def lock():
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def unlock():
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        started = time.monotonic()
        while True:
            check_cancel(cancel)
            try:
                lock()
                locked = True
                break
            except OSError:
                if time.monotonic() - started >= LOCK_TIMEOUT:
                    raise TimeoutError('別の FFmpeg セットアップの完了を待機中にタイムアウトしました。')
                time.sleep(.1)
        yield
    finally:
        if locked:
            unlock()
        os.close(descriptor)


def _probe_version(folder: Path, *, cancel=None) -> None:
    if not _verified_pair(folder, cancel=cancel):
        raise MediaToolsError('実行前の FFmpeg SHA256 検証に失敗しました。')
    for binary in ('ffmpeg', 'ffprobe'):
        check_cancel(cancel)
        result = run_cancellable([str(folder / (binary + '.exe')), '-version'],
            cancel=cancel, timeout=20, capture_output=True, text=True, encoding='utf-8',
            errors='replace', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        first = result.stdout.splitlines()[0] if result.stdout else ''
        if result.returncode or not first.startswith(f'{binary} version {VERSION}-essentials_build-www.gyan.dev'):
            raise MediaToolsError('FFmpeg の固定バージョンを確認できませんでした。')


def _replace_directory(source: Path, destination: Path, *, cancel=None, log=print,
                       verify=None) -> None:
    """Rename an owned directory, allowing bounded Windows sharing-lock delays.

    Windows can retain a deny-delete handle briefly after an executable exits
    (or while another process inspects it). Do not change ACLs, attributes or
    protection settings. A real permission failure still stops after the bound.
    Every attempt rechecks ancestors, the original directory identity and the
    absence of a destination; retries must not bless a substituted path.
    """
    identity = _ordinary(source, directory=True)
    started, waiting = time.monotonic(), False
    while True:
        check_cancel(cancel)
        _base_path(source.parent)
        _base_path(destination.parent)
        current = _ordinary(source, directory=True)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise MediaToolsError('FFmpeg の一時保存フォルダーが変更されました。')
        try:
            destination.lstat()
        except FileNotFoundError:
            pass
        else:
            raise MediaToolsError('FFmpeg の移動先に予期しないファイルまたはフォルダーがあります。')
        if verify is not None:
            verify()
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            # Only the Windows access/sharing/lock conditions are retryable.
            # Disk-full, cross-device and all other errors must remain immediate.
            if getattr(exc, 'winerror', None) not in (5, 32, 33):
                raise
            remaining = REPLACE_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                raise MediaToolsError(
                    f'FFmpeg の保存先を更新できません (Windows エラー {exc.winerror})。'
                    f'使用中の録音・変換を終了し、保存先の書き込み権限を確認して再実行してください。'
                    f'保存先: {destination}') from exc
            if not waiting:
                log('FFmpeg のファイルが使用中か、一時的に移動を拒否されています。'
                    f'最大 {REPLACE_TIMEOUT:g} 秒待って再試行します。')
                waiting = True
            time.sleep(min(REPLACE_INTERVAL, remaining))


def ensure_media_tools(base: Path, *, cancel=None, log=print) -> Path:
    """Install the pinned pair transactionally; preserve existing tools on failure."""
    check_cancel(cancel)
    root = _base_path(base)
    internal = root / '_internal'
    internal.mkdir(exist_ok=True)
    _ordinary(internal, directory=True)
    parent = internal / 'environments'
    parent.mkdir(exist_ok=True)
    _ordinary(parent, directory=True)
    target = parent / 'media-tools'
    with _install_lock(parent, cancel=cancel):
        if _verified_pair(target, cancel=cancel):
            return target
        if target.exists() or target.is_symlink():
            _ordinary(target, directory=True)
        log(f'検証済み FFmpeg / FFprobe {VERSION} を取得します。')
        with tempfile.TemporaryDirectory(prefix='.media-tools-stage-', dir=parent,
                                         ignore_cleanup_errors=True) as temporary:
            work = Path(temporary)
            payload = work / 'payload'
            payload.mkdir()
            archive = work / 'download.zip'
            _download_archive(archive, cancel=cancel)
            _extract_archive(archive, payload, cancel=cancel)
            _probe_version(payload, cancel=cancel)
            receipt = {'schema': 1, 'version': VERSION, 'url': DOWNLOAD_URL,
                'archive_sha256': ARCHIVE_SHA256, 'binaries': BINARY_SHA256, 'notices': NOTICE_SHA256}
            (payload / 'verified-media.json').write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            check_cancel(cancel)
            backup = parent / ('.media-tools-backup-' + uuid.uuid4().hex)
            moved = False
            try:
                if target.exists() or target.is_symlink():
                    _ordinary(target, directory=True)
                    _replace_directory(target, backup, cancel=cancel, log=log)
                    moved = True
                def verify_payload():
                    if not _verified_pair(payload, cancel=cancel):
                        raise MediaToolsError('移動前の FFmpeg SHA256 検証に失敗しました。')
                _replace_directory(payload, target, cancel=cancel, log=log,
                                   verify=verify_payload)
            except BaseException:
                if moved:
                    try:
                        # A cancelled install must still restore the old tools.
                        _replace_directory(backup, target, log=log)
                    except BaseException as recovery:
                        # The backup is outside TemporaryDirectory: never erase
                        # the user's existing tools if restoration is blocked.
                        raise MediaToolsError(
                            'FFmpeg の更新と元の状態への復元に失敗しました。'
                            f'以前のファイルは次のフォルダーに保持しました: {backup}') from recovery
                raise
            if moved:
                try:
                    shutil.rmtree(backup)
                except OSError:
                    # Publication has committed. Locked old executables must not
                    # make a valid new installation look like a failed upgrade.
                    log(f'旧 FFmpeg の一時保存先を削除できませんでした: {backup}')
        log(f'FFmpeg / FFprobe {VERSION} の SHA256 とバージョンを確認しました。')
        return target
