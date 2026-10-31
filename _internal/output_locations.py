"""User output locations, available before GUI or optional packages are imported.

Only preferences live beside the application. Logs and generated user files use
the user's Documents folder by default, including redirected Windows folders.
Resolving a path never creates directories or changes settings.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile

from portable_runtime import MAX_CONFIG_BYTES, is_portable


APP_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FOLDERS = {
    'recordings': 'Recordings',
    'transcriptions': 'Transcriptions',
    'merged': 'Merged',
    'speaker_edits': 'SpeakerEdits',
    'logs': 'Logs',
    'exports': 'Exports',
}


def _windows_documents() -> Path | None:
    """Resolve FOLDERID_Documents through the OS, without assuming its spelling."""
    import ctypes
    from ctypes import wintypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    guid = GUID.from_buffer_copy(uuid.UUID('fdd39ad0-238f-46af-adb4-6c85480369c7').bytes_le)
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    ole32 = ctypes.WinDLL('ole32', use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                           wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    allocated = ctypes.c_void_p()
    try:
        status = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(allocated))
        if status == 0 and allocated.value:
            value = ctypes.wstring_at(allocated.value)
            if value:
                return Path(value)
    finally:
        if allocated.value:
            ole32.CoTaskMemFree(allocated)
    return None


def documents_directory() -> Path:
    if os.name == 'nt':
        try:
            folder = _windows_documents()
            if folder is not None:
                return folder
        except (OSError, ValueError, AttributeError):
            pass
        # Retain Windows folder redirection even if the shell API is unavailable.
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                value, _kind = winreg.QueryValueEx(key, 'Personal')
            if isinstance(value, str) and value:
                expanded = os.path.expandvars(value)
                if Path(expanded).is_absolute():
                    return Path(expanded)
        except (OSError, ValueError, ImportError):
            pass
    return Path.home() / 'Documents'


def default_output_root() -> Path:
    return documents_directory() / 'UnifiedTranscriber'


def settings_path(app_root: Path | None = None) -> Path:
    root = Path(app_root) if app_root is not None else APP_ROOT
    if is_portable(root):
        return root / 'Data' / 'settings.json'
    appdata = os.environ.get('APPDATA')
    return (Path(appdata) if appdata else Path.home() / '.config') / 'UnifiedTranscriberV12' / 'settings.json'


def validate_output_root(value: str) -> str:
    if not isinstance(value, str) or '\0' in value or len(value) > 32768:
        raise ValueError('共通の出力先にはフォルダーの絶対パスを指定してください。')
    value = value.strip()
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute() and not PureWindowsPath(value).is_absolute():
            raise ValueError('共通の出力先にはフォルダーの絶対パスを指定してください。')
        if path.is_file():
            raise ValueError('共通の出力先にはファイルではなくフォルダーを指定してください。')
        value = str(path)
    return value


def _read_settings(path: Path) -> dict:
    with path.open('rb') as stream:
        payload = stream.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise ValueError('設定ファイルが大きすぎます。')
    data = json.loads(payload.decode('utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('設定ファイルの内容を確認してください。')
    return data


def output_root(app_root: Path | None = None, *, config_path: Path | None = None) -> Path:
    path = Path(config_path) if config_path is not None else settings_path(app_root)
    try:
        value = validate_output_root(_read_settings(path).get('output_root', ''))
        if value:
            return Path(value)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return default_output_root()


def default_directory(kind: str = 'transcriptions', *, app_root: Path | None = None,
                      config_path: Path | None = None) -> Path:
    return output_root(app_root, config_path=config_path) / OUTPUT_FOLDERS[kind]


def set_output_root(value: str | Path, *, app_root: Path | None = None,
                    config_path: Path | None = None) -> None:
    """Change one preference atomically; preserve existing and unknown fields.

    An invalid existing configuration is left untouched so repair never erases
    settings the user may want to recover. An empty value selects Documents.
    """
    selected = validate_output_root(str(value))
    path = Path(config_path) if config_path is not None else settings_path(app_root)
    for item in (path, path.parent):
        if item.is_symlink() or (os.name == 'nt' and item.exists()
                and item.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('設定の保存先がリンク・ジャンクションのため変更しませんでした。')
    data = _read_settings(path) if path.exists() else {}
    data['output_root'] = selected
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
