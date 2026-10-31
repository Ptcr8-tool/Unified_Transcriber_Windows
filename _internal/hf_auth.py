"""User-scoped Hugging Face credentials and legacy environment registration."""
from __future__ import annotations
from dataclasses import replace
from contextlib import contextmanager
from functools import wraps
import hashlib
import os
import sys
import threading

_TARGET = 'UnifiedTranscriber/HuggingFace'
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2  # Persistent for this Windows user, not shared with others.
_ERROR_NOT_FOUND = 1168
_MAX_BLOB = 2560
_THREAD_LOCK = threading.RLock()


@contextmanager
def credential_guard(timeout_ms: int = 15000):
    """Serialize this application's credential transactions for one Windows user.

    The named mutex is advisory: software that does not use it can still change
    a Windows credential concurrently. An abandoned mutex is safe to acquire;
    transactions retain their existing verification and rollback checks.
    """
    with _THREAD_LOCK:
        if sys.platform != 'win32':
            yield
            return
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        advapi.GetUserNameW.restype = wintypes.BOOL
        username = ctypes.create_unicode_buffer(257)
        size = wintypes.DWORD(len(username))
        if not advapi.GetUserNameW(username, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), '資格情報の利用者を確認できません。')
        # The name is stable across sessions, contains no token or plain user name,
        # and cannot collide with a different application target by accident.
        suffix = hashlib.sha256(username.value.casefold().encode('utf-8')).hexdigest()
        name = 'Global\\UnifiedTranscriber.HFToken.' + suffix
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), '資格情報の排他制御を開始できません。')
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, timeout_ms)
            if result not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED
                if result == 0x102:
                    raise OSError('別のアプリが資格情報を操作中です。しばらくして再試行してください。')
                raise OSError(ctypes.get_last_error(), '資格情報の排他制御に失敗しました。')
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def _guarded(function):
    @wraps(function)
    def call(*args, **kwargs):
        with credential_guard():
            return function(*args, **kwargs)
    return call


def _token_bytes(token: str) -> bytes:
    token = token.strip()
    if not token or any(char in token for char in '\r\n\x00'):
        raise ValueError('有効なトークンを指定してください。')
    data = token.encode('utf-16-le')
    if len(data) > _MAX_BLOB:
        raise ValueError('トークンが長すぎます。')
    return data


def _credential_api():
    if sys.platform != 'win32':
        raise OSError('Windows 資格情報マネージャーは Windows 専用です。')
    import ctypes
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                    ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                    ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                    ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
                    ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                    ('UserName', wintypes.LPWSTR)]
    api = ctypes.WinDLL('Advapi32', use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return ctypes, Credential, api


@_guarded
def write_credential(token: str) -> None:
    """Save a generic credential for the current user, without a subprocess."""
    data = _token_bytes(token)
    ctypes, Credential, api = _credential_api()
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    credential = Credential()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = _TARGET
    credential.CredentialBlobSize = len(data)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = 'Unified Transcriber'
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(ctypes.get_last_error(), 'Windows 資格情報の保存に失敗しました。')


@_guarded
def read_credential() -> str:
    if sys.platform != 'win32':
        return ''
    ctypes, Credential, api = _credential_api()
    ptr = ctypes.POINTER(Credential)()
    if not api.CredReadW(_TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return ''
        raise OSError(code, 'Windows 資格情報の読み込みに失敗しました。')
    try:
        size = ptr.contents.CredentialBlobSize
        if size > _MAX_BLOB or size % 2 or (size and not ptr.contents.CredentialBlob):
            raise OSError('Windows 資格情報の形式が不正です。')
        try:
            value = ctypes.string_at(ptr.contents.CredentialBlob, size).decode('utf-16-le')
            _token_bytes(value)
        except (UnicodeError, ValueError) as exc:
            raise OSError('Windows 資格情報の形式が不正です。') from exc
        return value
    finally:
        api.CredFree(ptr)


@_guarded
def delete_credential() -> None:
    """Delete only this application's credential, leaving shared HF_TOKEN untouched."""
    ctypes, _, api = _credential_api()
    if not api.CredDeleteW(_TARGET, _CRED_TYPE_GENERIC, 0):
        code = ctypes.get_last_error()
        if code != _ERROR_NOT_FOUND:
            raise OSError(code, 'Windows 資格情報の削除に失敗しました。')


@_guarded
def write_verified_credential(token: str, *, only_if_empty: bool = False) -> None:
    """Replace a credential only if it can be read back; restore it on failure."""
    token = token.strip()
    _token_bytes(token)
    previous = read_credential()
    if previous and only_if_empty:
        raise OSError('既存の資格情報は自動移行で上書きしません。')
    try:
        write_credential(token)
        if read_credential() != token:
            raise OSError('保存した資格情報を確認できません。')
    except (OSError, ValueError):
        try:
            if previous:
                write_credential(previous)
            else:
                delete_credential()
        except (OSError, ValueError):
            raise OSError('資格情報の保存に失敗し、以前の資格情報も復元できませんでした。') from None
        raise


def resolve_hf_token(config, persist):
    """Use the vault first. Migrate a legacy JSON secret only after verification.

    A failed write/read-back or config write retains the old JSON untouched. A
    different existing vault secret is never overwritten by automatic migration.
    Environment variables belong to the user and are never removed or modified.
    """
    try:
        with credential_guard():
            return _resolve_hf_token_locked(config, persist)
    except OSError:
        # If the lock cannot be obtained, keep the application usable and
        # preserve legacy settings; never attempt an unprotected migration.
        return (os.environ.get('HF_TOKEN', '').strip() or config.hf_token_json.strip(),
                config, '資格情報を安全に確認できません。従来の設定は保持しています。')


def _resolve_hf_token_locked(config, persist):
    legacy = config.hf_token_json.strip()
    env = os.environ.get('HF_TOKEN', '').strip()
    try:
        saved = read_credential()
    except OSError:
        return env or legacy, config, 'Windows 資格情報を読み込めません。従来の設定は保持しています。'
    if sys.platform != 'win32':
        return env or legacy, config, ''
    if legacy and env and env != legacy and not saved:
        return env, config, '環境変数と以前の平文トークンが異なるため、自動移行せず従来の設定を維持しました。'
    if legacy and not saved:
        try:
            write_verified_credential(legacy, only_if_empty=True)
            saved = legacy
        except (OSError, ValueError):
            return env or legacy, config, 'トークンを資格情報へ移行できません。従来の設定は保持しています。'
    if legacy and saved == legacy:
        sanitized = replace(config, hf_token_json='')
        try:
            persist(sanitized)
            config = sanitized
        except OSError:
            return saved, config, '平文設定を削除できません。保存先の権限を確認してください。'
    elif legacy and saved != legacy:
        return saved, config, '資格情報と以前の平文トークンが異なるため、以前の設定を保持しました。'
    return saved or env or legacy, config, ''


def register_hf_token(token: str) -> None:
    """Store in HKCU Environment (user scope); do not use setx/CLI arguments.

    Windows environment variables are plain text and are NOT a secret vault.
    Also update this process so a restart is unnecessary for current jobs.
    """
    token = token.strip()
    if not token or any(char in token for char in '\r\n\x00'):
        raise ValueError('有効なトークンを指定してください。')
    if sys.platform != 'win32':
        raise OSError('環境変数への永続登録はWindows専用です。')
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, 'Environment', 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'HF_TOKEN', 0, winreg.REG_SZ, token)
    os.environ['HF_TOKEN'] = token
    # Broadcast user environment update; ignored by older already running apps.
    try:
        import ctypes
        from ctypes import wintypes
        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xffff, 0x001a, 0, 'Environment', 0x0002, 2000, ctypes.byref(result))
    except (OSError, AttributeError):
        pass
