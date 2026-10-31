"""Metadata inspection and conservative oldest-first ordering for merge dialogs.

Embedded recorded/creation tags outrank filesystem birth timestamps. File mtime
is an explicit last resort and is NEVER presented as the recording date.
"""
from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from media_tools import decode_text, probe_metadata, parse_creation_time


@dataclass(frozen=True)
class MergeFileInfo:
    path: Path
    recorded: datetime | None
    file_created: datetime | None
    modified: datetime | None
    sort_time: datetime | None
    sort_source: str


def _date_from_text(path: Path) -> datetime | None:
    try:
        raw = decode_text(path)
        if path.suffix.lower() == '.csv':
            rows = csv.DictReader(io.StringIO(raw, newline=''))
            for row in rows:
                for key in ('media_created_at', '録音日時', 'メディア作成日時'):
                    if row.get(key):
                        return parse_creation_time(row[key])
                break
        else:
            if raw.lstrip('\ufeff').startswith('# 文字起こし情報'):
                for line in raw[:16000].splitlines():
                    if line.strip() == '--- 文字起こし本文 ---':
                        break
                    for key in ('メディア作成日時:', '録音日時:'):
                        if line.startswith(key):
                            parsed = parse_creation_time(line[len(key):].strip())
                            if parsed:
                                return parsed
    except (OSError, UnicodeError, ValueError, csv.Error):
        pass
    return None


def _filesystem_datetime(timestamp: float) -> datetime | None:
    try:
        # A naive local date near the epoch can fall into 1969 and fail when
        # Windows converts it back through mktime. Start with aware UTC.
        return datetime.fromtimestamp(timestamp, timezone.utc).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def inspect_merge_file(path: Path, *, engine_python: Path | None = None) -> MergeFileInfo:
    path = Path(path)
    recorded: datetime | None = None
    source = ''
    if path.suffix.lower() in ('.txt', '.csv'):
        recorded = _date_from_text(path)
        if recorded:
            source = 'TXT/CSV内のメタデータ'
    else:
        metadata = probe_metadata(path, engine_python)
        # Filesystem creation from probe_metadata is NOT an embedded recording tag.
        if metadata.created and metadata.source.startswith('メディアタグ'):
            recorded = metadata.created
            source = metadata.source

    stat = path.stat()
    modified = _filesystem_datetime(stat.st_mtime)
    created = None
    if os.name == 'nt':
        created = _filesystem_datetime(stat.st_ctime)
    elif getattr(stat, 'st_birthtime', None) is not None:
        created = _filesystem_datetime(stat.st_birthtime)

    if recorded:
        sort_time = recorded
    elif created:
        sort_time, source = created, 'ファイル作成日時（録音日時とは限りません）'
    else:
        sort_time, source = modified, '最終更新日時（録音日時ではありません）'
    return MergeFileInfo(path, recorded, created, modified, sort_time, source)


def oldest_first(paths: list[Path], info: dict[Path, MergeFileInfo]) -> list[Path]:
    def key(path: Path):
        record = info.get(path)
        if record and record.sort_time:
            dt=record.sort_time
            return (dt.timestamp(), str(path).casefold())
        try:
            return (path.stat().st_mtime, str(path).casefold())
        except OSError:
            return (float('inf'), str(path).casefold())
    return sorted(paths, key=key)


def show_datetime(value: datetime | None) -> str:
    return value.astimezone().strftime('%Y-%m-%d %H:%M:%S') if value else '取得不可'
