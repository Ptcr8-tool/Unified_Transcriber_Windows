"""Portable installation transactions and optional per-user Windows registration.

This module never launches a shell, downloads code, or follows links while
installing. The caller authenticates a downloaded package before calling it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Mapping
import uuid
import zipfile


APP_ID = 'UnifiedTranscriberWindows'
RECEIPT_NAME = 'installation.json'
PORTABLE_MARKER = 'portable.json'
RECEIPT_FORMAT = 'unified-transcriber-installation-v1'
MAX_FILES = 50000
MAX_UNPACKED_BYTES = 2 * 1024 ** 3
MAX_FILE_BYTES = 768 * 1024 ** 2
MAX_ARCHIVE_BYTES = 1024 ** 3
REQUIRED_PACKAGE_FILES = frozenset((
    'VERSION', 'UnifiedTranscriber.exe', 'Runtime/python.exe',
    'Runtime/pythonw.exe', '_internal/core.py',
))
_VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_DEVICES = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z', re.I)
_PROTECTED_ROOTS = frozenset(x.casefold() for x in (
    'Data', 'Models', 'Transcriptions', 'Recordings', 'Merged', 'SpeakerEdits',
    'Logs', 'logs', '_old', '.cache', '.huggingface', '.git',
))
_PROTECTED_INTERNAL = frozenset(x.casefold() for x in (
    'environments', 'gui_dependencies', 'log', 'logs',
))
_PRIVATE_NAMES = frozenset((
    'settings.json', 'hf_token.txt', 'stored_tokens', RECEIPT_NAME,
    PORTABLE_MARKER,
))


class InstallationError(ValueError):
    """The target or package is unsafe or does not match this application."""


class MaintenanceChoiceRequired(InstallationError):
    """An equal/newer installation needs an explicit maintenance selection."""


@dataclass(frozen=True)
class InstallationInfo:
    target: Path
    state: str
    version: str | None
    actions: tuple[str, ...]
    managed_paths: tuple[str, ...] = ()
    install_id: str | None = None
    registered_program: bool = False
    create_shortcut: bool = False


@dataclass(frozen=True)
class InstallResult:
    target: Path
    version: str
    mode: str
    managed_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallResult:
    target: Path
    removed_folder: bool
    data_preserved: bool
    warnings: tuple[str, ...] = ()


def version_tuple(value: str) -> tuple[int, int, int]:
    """Compare stable release numbers numerically; reject ambiguous versions."""
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match or len(value) > 40:
        raise InstallationError('バージョン番号は X.Y.Z 形式で指定してください。')
    return tuple(int(part) for part in match.groups())


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == 'nt':
        try:
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except FileNotFoundError:
            return False
    return False


def _target_path(target: Path) -> Path:
    original = Path(target).expanduser().absolute()
    # Reject reparse points before canonicalization: resolve() must never make
    # a junction supplied as an installation target appear to be a safe folder.
    for part in (original, *original.parents):
        if _is_link(part):
            raise InstallationError('リンク・ジャンクションを含むインストール先は使用できません。')
    # Windows may spell the same folder with an 8.3 alias. Receipts, executable
    # identity checks and the per-target mutex must all use the same spelling.
    target = original.resolve()
    for part in (original, *original.parents, target, *target.parents):
        if _is_link(part):
            raise InstallationError('リンク・ジャンクションを含むインストール先は使用できません。')
    if target.parent == target or target == Path.home().resolve():
        raise InstallationError('ドライブ直下やユーザーフォルダー自体は指定できません。')
    if target.exists() and not target.is_dir():
        raise InstallationError('インストール先はフォルダーを指定してください。')
    return target


def _safe_name(name: str, *, allow_private: bool = False) -> str:
    if not isinstance(name, str) or not name or len(name) > 700 or '\\' in name or '\x00' in name:
        raise InstallationError('パッケージ内のパスが不正です。')
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(part in ('.', '..') for part in path.parts):
        raise InstallationError('パッケージにフォルダー外へのパスが含まれています。')
    for part in path.parts:
        if (len(part) > 200 or any(ord(c) < 32 for c in part)
                or any(c in part for c in ':<>"|?*') or part[-1:] in (' ', '.')
                or _DEVICES.fullmatch(part)):
            raise InstallationError('Windowsで安全に扱えないファイル名が含まれています。')
    if not allow_private:
        folded = tuple(part.casefold() for part in path.parts)
        if (folded[0] in _PROTECTED_ROOTS
                or (folded[0] == '_internal' and len(folded) > 1 and folded[1] in _PROTECTED_INTERNAL)
                or any(part in _PRIVATE_NAMES or part.startswith('.env') for part in folded)
                or any(part.startswith('transcriber-settings') and part.endswith('.json') for part in folded)):
            raise InstallationError('設定・モデル・録音など利用者のデータを配布物で置き換えることはできません。')
    return name


def _read_json(path: Path) -> dict:
    if _is_link(path) or path.stat().st_size > 8 * 1024 ** 2:
        raise InstallationError('インストール情報が不正です。')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InstallationError('インストール情報を読み込めません。') from exc
    if not isinstance(data, dict):
        raise InstallationError('インストール情報が不正です。')
    return data


def _read_receipt(target: Path) -> dict | None:
    path = target / RECEIPT_NAME
    if not path.exists():
        return None
    data = _read_json(path)
    if data.get('app_id') != APP_ID or data.get('format') != RECEIPT_FORMAT:
        raise InstallationError('このアプリのインストール情報ではありません。')
    version_tuple(data.get('version', ''))
    if not isinstance(data.get('install_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', data['install_id']):
        raise InstallationError('インストール識別子が不正です。')
    managed = data.get('managed')
    if not isinstance(managed, dict) or len(managed) > MAX_FILES:
        raise InstallationError('管理対象ファイルの情報が不正です。')
    folded = set()
    for name, digest in managed.items():
        _safe_name(name)
        if name.casefold() in folded or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise InstallationError('管理対象ファイルの情報が不正です。')
        folded.add(name.casefold())
    for name in managed:
        if any(parent.as_posix().casefold() in folded for parent in PurePosixPath(name).parents if parent.as_posix() != '.'):
            raise InstallationError('管理対象のファイルとフォルダーが重複しています。')
    return data


def inspect_installation(target: Path, candidate_version: str | None = None) -> InstallationInfo:
    target = _target_path(target)
    if candidate_version is not None:
        version_tuple(candidate_version)
    receipt = _read_receipt(target) if target.exists() else None
    if receipt:
        current = receipt['version']
        actions = ('upgrade', 'cancel') if candidate_version and version_tuple(candidate_version) > version_tuple(current) else ('repair', 'uninstall', 'cancel')
        return InstallationInfo(target, 'installed', current, actions,
            tuple(receipt['managed']), receipt['install_id'],
            bool(receipt.get('registered_program')), bool(receipt.get('create_shortcut')))
    if not target.exists() or not any(target.iterdir()):
        return InstallationInfo(target, 'absent', None, ('install', 'cancel'))
    # The previous source distribution has no installation receipt.
    legacy = target / 'VERSION'
    if legacy.is_file() and not _is_link(legacy) and legacy.stat().st_size < 80:
        try:
            current = legacy.read_text(encoding='utf-8-sig').strip()
            version_tuple(current)
        except (UnicodeError, InstallationError):
            pass
        else:
            if (target / '_internal/core.py').is_file() and ((target / 'start_gui.pyw').is_file() or (target / 'UnifiedTranscriber.exe').is_file()):
                actions = ('upgrade', 'cancel') if candidate_version and version_tuple(candidate_version) > version_tuple(current) else ('repair', 'cancel')
                return InstallationInfo(target, 'legacy', current, actions)
    return InstallationInfo(target, 'unknown', None, ('cancel',))


def get_verified_maintenance_path(target: Path) -> Path:
    """Return only this installation's recorded, unchanged maintenance exe."""
    target = _target_path(target)
    receipt = _read_receipt(target)
    name = 'UnifiedTranscriberSetup.exe'
    if not receipt or name not in receipt['managed']:
        raise InstallationError('修復用セットアップがありません。同じか新しいバージョンをダウンロードしてください。')
    executable = _check_destination(target, name)
    if (not executable.is_file() or executable.stat().st_size > MAX_ARCHIVE_BYTES
            or _file_digest(executable) != receipt['managed'][name]):
        raise InstallationError('修復用セットアップの検証に失敗しました。同じか新しいバージョンをダウンロードしてください。')
    return executable


@contextmanager
def _installation_lock(target: Path):
    digest = hashlib.sha256(os.fsencode(os.path.normcase(str(target)))).hexdigest()
    if os.name == 'nt':
        # A named kernel mutex is released even if setup crashes. It leaves no
        # external lock file behind after deleting the portable app directory.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, 'Local\\UnifiedTranscriber.Install.' + digest)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'セットアップの排他制御を開始できません。')
        owned = False
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in (0, 0x80):  # acquired or previous owner terminated
                raise InstallationError('別のセットアップが実行中です。終了してから再試行してください。')
            owned = True
            yield
        finally:
            if owned:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        # OS-managed advisory locks also recover automatically on test hosts.
        # Keep the tiny file in the host temporary area to avoid inode races
        # caused by unlinking a lock another process may already have opened.
        import fcntl
        cache = Path(tempfile.gettempdir()) / ('unified-transcriber-locks-' + str(os.getuid()))
        cache.mkdir(mode=0o700, exist_ok=True)
        if _is_link(cache) or cache.stat().st_uid != os.getuid():
            raise InstallationError('セットアップの一時保存先が安全ではありません。')
        descriptor = os.open(cache / (digest + '.lock'),
            os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'a+b') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallationError('別のセットアップが実行中です。終了してから再試行してください。') from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_FILES:
        raise InstallationError('配布パッケージのファイル数が不正です。')
    # Also accept the root folder used by GitHub source archives for this app.
    prefix = 'Unified_Transcriber_Windows/'
    wrapped = all(info.filename.startswith(prefix) for info in entries)
    result, folded, total = {}, set(), 0
    for info in entries:
        if info.orig_filename != info.filename:
            raise InstallationError('配布物に省略された不正なファイル名が含まれています。')
        original = info.filename[len(prefix):] if wrapped else info.filename
        if not original and info.is_dir():
            continue
        name = _safe_name(original[:-1] if info.is_dir() else original)
        if name.casefold() in folded:
            raise InstallationError('配布パッケージに重複したパスがあります。')
        folded.add(name.casefold())
        mode = info.external_attr >> 16
        if (info.flag_bits & 1 or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) and not stat.S_ISREG(mode) and not stat.S_ISDIR(mode))):
            raise InstallationError('リンク・特殊ファイル・暗号化ファイルは展開できません。')
        if info.is_dir():
            continue
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise InstallationError('配布ファイルのサイズが上限を超えています。')
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise InstallationError('配布パッケージの展開サイズが上限を超えています。')
        result[name] = info
    if not REQUIRED_PACKAGE_FILES <= result.keys():
        raise InstallationError('配布パッケージに必要なアプリ・Pythonのファイルがありません。')
    files = {name.casefold() for name in result}
    for name in result:
        if any(parent.as_posix().casefold() in files for parent in PurePosixPath(name).parents if parent.as_posix() != '.'):
            raise InstallationError('配布物のファイルとフォルダーが重複しています。')
    return result


def validate_package(package: Path, version: str, expected_sha256: str | None = None) -> tuple[str, ...]:
    version_tuple(version)
    package = Path(package)
    if _is_link(package) or not package.is_file() or package.stat().st_size > MAX_ARCHIVE_BYTES:
        raise InstallationError('配布パッケージが見つからないか、サイズが不正です。')
    if expected_sha256 is not None:
        if not _DIGEST.fullmatch(expected_sha256) or _file_digest(package) != expected_sha256:
            raise InstallationError('配布パッケージのSHA-256が一致しません。')
    try:
        with zipfile.ZipFile(package) as archive:
            members = _archive_members(archive)
            if members['VERSION'].file_size > 80 or archive.read(members['VERSION']).decode('utf-8-sig').strip() != version:
                raise InstallationError('配布パッケージのバージョンが一致しません。')
            return tuple(members)
    except (zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
        raise InstallationError('配布パッケージを読み込めません。') from exc


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _copy_bounded(source, destination, limit: int) -> None:
    total = 0
    while block := source.read(min(1024 * 1024, limit - total + 1)):
        total += len(block)
        if total > limit:
            raise InstallationError('配布パッケージが大きすぎます。')
        destination.write(block)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _check_destination(target: Path, name: str) -> Path:
    result = target / name
    for path in (result, *result.parents):
        if path == target.parent:
            break
        if _is_link(path):
            raise InstallationError('更新先にリンク・ジャンクションがあります。')
    if result.exists() and not result.is_file():
        raise InstallationError('更新するファイルと同名のフォルダーがあります。')
    return result


def install_package(package: Path, target: Path, version: str, *,
                    expected_sha256: str | None = None, mode: str = 'auto',
                    register_program: bool | None = None, create_shortcut: bool | None = None,
                    extra_files: Mapping[str, Path] | None = None) -> InstallResult:
    """Install a trusted local ZIP; upgrades preserve every unmanaged path.

    mode='auto' only installs or upgrades. Equal/newer versions require an
    explicit 'repair' choice, and older packages can never repair newer code.
    extra_files is for the trusted running setup executable, copied separately
    to avoid embedding an installer inside itself.
    """
    target = _target_path(target)
    version_tuple(version)
    if mode not in ('auto', 'install', 'upgrade', 'repair'):
        raise InstallationError('セットアップの操作が不正です。')
    validate_package(package, version, expected_sha256)
    with _installation_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        current = inspect_installation(target, version)
        if current.state == 'unknown':
            raise InstallationError('別のファイルがあるフォルダーにはインストールできません。専用フォルダーを選んでください。')
        if current.version and version_tuple(version) < version_tuple(current.version):
            raise MaintenanceChoiceRequired('インストール済みより古い配布物では修復・更新できません。同じか新しいセットアップを使用してください。')
        if mode == 'auto':
            if current.version and version_tuple(version) == version_tuple(current.version):
                raise MaintenanceChoiceRequired('同じバージョンが入っています。修復・アンインストール・キャンセルを選んでください。')
            mode = 'upgrade' if current.version else 'install'
        if mode == 'install' and current.version:
            raise MaintenanceChoiceRequired('既存のアプリがあります。更新または修復を選んでください。')
        if mode == 'upgrade' and current.version and version_tuple(version) <= version_tuple(current.version):
            raise MaintenanceChoiceRequired('更新する新しいバージョンがありません。修復またはキャンセルを選んでください。')
        old = _read_receipt(target) if current.state == 'installed' else None
        install_id = current.install_id or uuid.uuid4().hex
        register_program = current.registered_program if register_program is None else bool(register_program)
        create_shortcut = current.create_shortcut if create_shortcut is None else bool(create_shortcut)
        warnings = []
        stage = Path(tempfile.mkdtemp(prefix='.unified-stage-', dir=target.parent))
        backup = Path(tempfile.mkdtemp(prefix='.unified-rollback-', dir=target.parent))
        installed, moved = [], []
        preserve_backup = False
        created_target = not target.exists()
        try:
            managed = {}
            # Authenticate the exact private snapshot that is extracted. A caller
            # changing the original download between verification and use cannot
            # substitute bytes after a successful digest check.
            with tempfile.TemporaryFile(dir=target.parent) as snapshot:
                with Path(package).open('rb') as source:
                    _copy_bounded(source, snapshot, MAX_ARCHIVE_BYTES)
                snapshot.seek(0)
                digest = hashlib.file_digest(snapshot, 'sha256').hexdigest()
                if expected_sha256 is not None and digest != expected_sha256:
                    raise InstallationError('配布パッケージのSHA-256が一致しません。')
                snapshot.seek(0)
                archive = zipfile.ZipFile(snapshot)
                try:
                    members = _archive_members(archive)
                    if members['VERSION'].file_size > 80 or archive.read(members['VERSION']).decode('utf-8-sig').strip() != version:
                        raise InstallationError('配布パッケージのバージョンが一致しません。')
                    for name, info in members.items():
                        path = stage / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as source, path.open('xb') as destination:
                            shutil.copyfileobj(source, destination, 1024 * 1024)
                        if path.stat().st_size != info.file_size:
                            raise InstallationError('展開したファイルのサイズが一致しません。')
                        managed[name] = _file_digest(path)
                finally:
                    archive.close()
            for name, source in (extra_files or {}).items():
                name = _safe_name(name)
                source = Path(source)
                if name != 'UnifiedTranscriberSetup.exe' or name in managed or _is_link(source) or not source.is_file():
                    raise InstallationError('追加するセットアップファイルが不正です。')
                if source.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise InstallationError('セットアップファイルが大きすぎます。')
                shutil.copyfile(source, stage / name)
                managed[name] = _file_digest(stage / name)
            receipt = dict(format=RECEIPT_FORMAT, app_id=APP_ID, install_id=install_id,
                version=version, managed=managed, registered_program=register_program,
                create_shortcut=create_shortcut)
            _write_json(stage / RECEIPT_NAME, receipt)
            _write_json(stage / PORTABLE_MARKER, dict(format='unified-transcriber-portable',
                version=1, app_id=APP_ID, install_id=install_id, settings_directory='Data'))
            names = set(managed) | {RECEIPT_NAME, PORTABLE_MARKER}
            obsolete = set(old['managed']) - set(managed) if old else set()
            # Validate every destination before changing the first file.
            for name in names | obsolete:
                _check_destination(target, name)
            target.mkdir(parents=True, exist_ok=True)
            for name in sorted(names | obsolete):
                destination = _check_destination(target, name)
                if destination.is_file():
                    saved = backup / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, saved)
                    moved.append(name)
                if name in names:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage / name, destination)
                    installed.append(name)
        except BaseException as original:
            recovery_errors = []
            for name in reversed(installed):
                try:
                    (target / name).unlink(missing_ok=True)
                except OSError as exc:
                    # A backup replacement below may still restore this path.
                    if name not in moved:
                        recovery_errors.append((name, str(exc)))
            for name in reversed(moved):
                try:
                    destination = _check_destination(target, name)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup / name, destination)
                except (OSError, InstallationError) as exc:
                    recovery_errors.append((name, str(exc)))
            if created_target and target.exists():
                _remove_empty_dirs(target)
            if recovery_errors:
                preserve_backup = True
                try:
                    _write_json(backup / 'RECOVERY.json', dict(app_id=APP_ID,
                        target=str(target), failures=recovery_errors))
                except OSError:
                    pass  # Preserve the backup even when the disk is full.
                raise InstallationError('更新を完了できず、一部の旧ファイルの復元もできませんでした。'
                    '復旧用ファイルを残しました: ' + str(backup)) from original
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if not preserve_backup:
                shutil.rmtree(backup, ignore_errors=True)
        # Registration is optional and does not roll back a working application.
        try:
            if register_program or create_shortcut:
                register_windows(target, receipt, register_program=register_program,
                                 create_shortcut=create_shortcut)
            elif old and (old.get('registered_program') or old.get('create_shortcut')):
                unregister_windows(target, old)
        except OSError as exc:
            warnings.append('アプリは保存しましたが、Windowsへの任意の登録を完了できませんでした: ' + str(exc))
        return InstallResult(target, version, mode, len(managed), tuple(warnings))


def _remove_empty_dirs(root: Path) -> None:
    for folder in sorted((p for p in root.rglob('*') if p.is_dir() and not _is_link(p)),
                         key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def uninstall_installation(target: Path, *, remove_data: bool = False) -> UninstallResult:
    """Remove a recognized install only; never access custom output directories.

    Set remove_data=True only after confirming removal of the whole selected
    installation folder, including recordings and models it contains. The
    maintenance executable must be running from outside that folder on Windows.
    """
    target = _target_path(target)
    with _installation_lock(target):
        receipt = _read_receipt(target)
        if not receipt:
            raise InstallationError('このセットアップで導入したフォルダーではありません。')
        warnings = []
        if remove_data:
            # Keep the receipt until every other item has been removed. If a
            # locked file prevents removal, a subsequent attempt can still
            # recognize this installation. Never traverse top-level links.
            receipt_path = target / RECEIPT_NAME
            receipt_bytes = receipt_path.read_bytes()
            for child in target.iterdir():
                if (child.name.casefold() == RECEIPT_NAME.casefold()
                        if os.name == 'nt' else child.name == RECEIPT_NAME):
                    continue
                if _is_link(child):
                    if os.name == 'nt' and child.is_dir():
                        child.rmdir()
                    else:
                        child.unlink()
                elif child.is_dir():
                    # rmtree does not follow child symlinks or Windows junctions.
                    shutil.rmtree(child)
                else:
                    child.unlink()
            receipt_path.unlink()
            try:
                target.rmdir()
            except OSError as exc:
                # A concurrently created file or an open directory handle may
                # prevent the last rmdir after the receipt was removed.
                try:
                    if _is_link(target) or not target.is_dir():
                        raise OSError('インストール先が変更されました。')
                    with receipt_path.open('xb') as stream:
                        stream.write(receipt_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as restore_exc:
                    raise InstallationError('削除に失敗し、インストール情報の復元もできませんでした: '
                                            + str(restore_exc)) from exc
                raise
        else:
            for name in receipt['managed']:
                path = _check_destination(target, name)
                path.unlink(missing_ok=True)
            (target / RECEIPT_NAME).unlink()
            # The data-only marker allows the GUI to explain what remains.
            _remove_empty_dirs(target)
        try:
            unregister_windows(target, receipt)
        except OSError as exc:
            warnings.append('Windowsの任意の登録を削除できませんでした: ' + str(exc))
        try:
            from windows_background import remove_startup_if_matches
            remove_startup_if_matches(target)
        except OSError as exc:
            warnings.append('Windowsの自動起動を解除できませんでした: ' + str(exc))
        return UninstallResult(target, not target.exists(), not remove_data, tuple(warnings))


def _registration_key(install_id: str) -> str:
    if not isinstance(install_id, str) or not re.fullmatch(r'[0-9a-f]{32}', install_id):
        raise InstallationError('インストール識別子が不正です。')
    return r'Software\Microsoft\Windows\CurrentVersion\Uninstall\UnifiedTranscriber.' + install_id


def _shortcut_path(install_id: str) -> Path:
    _registration_key(install_id)
    import ctypes
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [('data', ctypes.c_ubyte * 16)]
    guid = GUID.from_buffer_copy(uuid.UUID('A77F5D77-2E2B-44C3-A6A2-ABA601054A51').bytes_le)
    pointer = ctypes.c_wchar_p()
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    hr = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    if hr < 0:
        raise OSError('スタートメニューの保存先を取得できません。')
    try:
        directory = Path(pointer.value) / ('Unified Transcriber ' + install_id[:8])
    finally:
        ole = ctypes.OleDLL('ole32')
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree(ctypes.cast(pointer, ctypes.c_void_p))
    return directory / '文字起こし統合スタジオ.lnk'


def _create_shortcut(executable: Path, shortcut: Path, working_directory: Path) -> None:
    """Use IShellLinkW directly; no script interpreter or shell command."""
    import ctypes
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [('data', ctypes.c_ubyte * 16)]
    def guid(value):
        return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
    ole = ctypes.OleDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    # RPC_E_CHANGED_MODE: the GUI thread already has another apartment type.
    if initialized < 0 and initialized != -2147417850:
        raise OSError('Windowsのショートカット機能を初期化できません。')
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    def call(pointer, index, arguments, *values):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(table[index])
        hr = method(pointer, *values)
        if hr < 0:
            raise OSError('ショートカットを保存できません。')
    try:
        clsid = guid('00021401-0000-0000-C000-000000000046')
        iid = guid('000214F9-0000-0000-C000-000000000046')
        ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole.CoCreateInstance.restype = ctypes.c_long
        if ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link)) < 0:
            raise OSError('Windowsのショートカット機能を開けません。')
        call(link, 20, [wintypes.LPCWSTR], str(executable))
        call(link, 9, [wintypes.LPCWSTR], str(working_directory))
        call(link, 7, [wintypes.LPCWSTR], '文字起こし統合スタジオ')
        persist_iid = guid('0000010b-0000-0000-C000-000000000046')
        call(link, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(persist_iid), ctypes.byref(persist))
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        call(persist, 6, [wintypes.LPCWSTR, wintypes.BOOL], str(shortcut), True)
    finally:
        for pointer in (persist, link):
            if pointer.value:
                table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(table[2])(pointer)
        if initialized >= 0:
            ole.CoUninitialize()


def register_windows(target: Path, receipt: dict, *, register_program: bool, create_shortcut: bool) -> None:
    if os.name != 'nt':
        return
    import winreg
    executable = target / 'UnifiedTranscriber.exe'
    setup = target / 'UnifiedTranscriberSetup.exe'
    if not executable.is_file() or not setup.is_file():
        raise OSError('アプリまたはセットアップexeが見つかりません。')
    key_name = _registration_key(receipt['install_id'])
    if register_program:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_SET_VALUE) as key:
            values = dict(DisplayName='文字起こし統合スタジオ', DisplayVersion=receipt['version'],
                Publisher='Unified Transcriber', InstallLocation=str(target), DisplayIcon=str(executable),
                UninstallString=subprocess.list2cmdline([str(setup), '--target', str(target), '--uninstall']))
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            for name in ('NoModify', 'NoRepair'):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)
    else:
        _remove_registration(key_name)
    shortcut = _shortcut_path(receipt['install_id'])
    if create_shortcut:
        _create_shortcut(executable, shortcut, target)
    else:
        shortcut.unlink(missing_ok=True)


def _remove_registration(key_name: str) -> None:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
    except FileNotFoundError:
        pass


def unregister_windows(target: Path, receipt: dict) -> None:
    if os.name != 'nt':
        return
    _remove_registration(_registration_key(receipt['install_id']))
    shortcut = _shortcut_path(receipt['install_id'])
    shortcut.unlink(missing_ok=True)
    try:
        shortcut.parent.rmdir()
    except OSError:
        pass

