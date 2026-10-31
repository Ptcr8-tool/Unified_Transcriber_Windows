"""Persistent, user-accessible session and error logs for the Windows GUI.

Never log process environments, authentication options, or HF tokens.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime
import threading
import uuid

from output_locations import default_directory


def log_directory() -> Path:
    """Use the selected output root, defaulting to Documents/UnifiedTranscriber."""
    folder = default_directory('logs')
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class SessionLogs:
    def __init__(self, token: str = ''):
        self._lock = threading.Lock()
        self.token = token
        suffix = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        self.folder = log_directory()
        self.session_path = self.folder / f'session_{suffix}.log'
        self.error_path = self.folder / f'errors_{suffix}.log'
        self.error_path.touch(exist_ok=True)
        self.write('ログ開始。日時はこのPCのローカル時刻です。')

    def refresh_location(self) -> bool:
        """Start new logs after a preference change; keep earlier logs intact."""
        folder = log_directory()
        with self._lock:
            if folder == self.folder:
                return False
            suffix = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
            session_path = folder / f'session_{suffix}.log'
            error_path = folder / f'errors_{suffix}.log'
            # Prepare the destination before changing the active log paths.
            with session_path.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] '
                             '出力先の変更によりログを開始しました。以前のログは元の場所に保持しています。\n')
            error_path.touch(exist_ok=False)
            self.folder, self.session_path, self.error_path = folder, session_path, error_path
        return True

    def redact(self, value: str) -> str:
        text = str(value)
        if self.token:
            text = text.replace(self.token, '[HF_TOKEN_REDACTED]')
        return text

    def _append(self, path: Path, text: str):
        with self._lock, path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {self.redact(text).rstrip()}\n')

    def write(self, value: str):
        self._append(self.session_path, value)

    def error(self, value: str):
        self._append(self.error_path, value)
        self.write('[ERROR] ' + str(value))
