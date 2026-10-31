"""Resolve the application-owned media tools for standalone ASR workers."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


CODE_DIR = Path(__file__).resolve().parents[1]
APP_BASE = CODE_DIR.parent
# Engine scripts also run directly, with only their own directory on sys.path.
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
from secure_media import verified_media_binary


def checked_ffmpeg() -> str:
    """Require the pinned Windows pair; retain source-host support elsewhere."""
    if os.name == 'nt':
        ffmpeg = verified_media_binary(APP_BASE, 'ffmpeg')
        if not ffmpeg:
            raise RuntimeError(
                '安全性を確認済みのFFmpeg / FFprobeがありません。'
                'GUIの「録音環境をセットアップ」または「一括セットアップ」で修復してください。'
            )
        # Some ASR libraries launch FFmpeg themselves. Keep their lookups on
        # the same verified tools rather than the user's PATH or wheel copy.
        directory = str(Path(ffmpeg).parent)
        path_key = next((key for key in os.environ if key.upper() == 'PATH'), 'PATH')
        current = os.environ.get(path_key, '')
        if current.split(';', 1)[0].replace('/', '\\').casefold() != directory.replace('/', '\\').casefold():
            os.environ[path_key] = directory + (';' + current if current else '')
        os.environ['IMAGEIO_FFMPEG_EXE'] = ffmpeg
        return ffmpeg

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError, OSError):
            pass
    if not ffmpeg:
        raise RuntimeError('FFmpegが見つかりません。FFmpegをインストールしてください。')
    return ffmpeg
