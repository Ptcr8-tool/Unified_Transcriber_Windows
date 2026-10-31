"""Media metadata and text/audio joining. Uses FFmpeg, no heavy ML imports."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence
from process_control import check_cancel, run_cancellable

MEDIA_CREATION_TAGS = ('com.apple.quicktime.creationdate', 'creation_time',
                       'date', 'encoded_date', 'recorded_date')
AUDIO_FORMATS = ('mp3', 'm4a', 'wav', 'flac')
BITRATES = ('64k', '96k', '128k', '160k', '192k', '256k', '320k')


def hidden_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def media_binary(binary: str, engine_python: Path | None = None) -> str | None:
    """Windows uses the verified app-owned pair; retain Unix discovery."""
    if os.name == 'nt':
        from secure_media import verified_media_binary
        return verified_media_binary(Path(__file__).resolve().parent.parent, binary)
    found = shutil.which(binary)
    if found:
        return found
    if binary != 'ffmpeg' or not engine_python:
        return None
    env_root = engine_python.parent.parent
    for pattern in ('Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg*.exe',
                    'lib/python*/site-packages/imageio_ffmpeg/binaries/ffmpeg*'):
        for candidate in env_root.glob(pattern):
            if candidate.is_file():
                return str(candidate)
    return None


def parse_creation_time(raw: str) -> datetime | None:
    """Trust explicit creation metadata only; do not reinterpret modification time."""
    value = str(raw or '').strip()
    if not value:
        return None
    if value[:4].upper() == 'UTC ':
        value = value[4:]
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    for transform in (lambda s: s, lambda s: s.replace(' ', 'T'),
                      lambda s: s.replace(':', '-', 2)):
        try:
            dt = datetime.fromisoformat(transform(value))
            return dt if dt.year >= 1970 else None
        except ValueError:
            continue
    return None


def find_creation_tag(tags: dict) -> tuple[datetime | None, str]:
    lowered = {str(k).lower(): v for k, v in tags.items()}
    for key in MEDIA_CREATION_TAGS:
        value = lowered.get(key)
        if value:
            parsed = parse_creation_time(str(value))
            if parsed:
                return parsed, key
    return None, ''


@dataclass(frozen=True)
class MediaMetadata:
    path: Path
    created: datetime | None = None
    source: str = ''  # 'media tag' or 'filesystem creation', never mtime
    duration_seconds: float | None = None
    codec: str = ''


def probe_metadata(path: Path, engine_python: Path | None = None, *, cancel=None, registry=None) -> MediaMetadata:
    """ffprobe is preferred; ffmpeg's metadata dump is fallback on Windows.

    A missing creation tag is not synthesized from modification timestamp.
    Windows st_ctime is creation time; Unix st_ctime is not.
    """
    check_cancel(cancel)
    created, source, duration, codec = None, '', None, ''
    probe = media_binary('ffprobe', engine_python)
    if probe:
        try:
            result = run_cancellable([
                probe, '-v', 'error', '-show_entries',
                'format=duration:format_tags:stream=codec_name:stream_tags',
                '-of', 'json', str(path)], capture_output=True, text=True,
                encoding='utf-8', errors='replace', cancel=cancel, registry=registry, timeout=40,
                creationflags=hidden_flags())
            if result.returncode == 0:
                data = json.loads(result.stdout)
                entries = [s.get('tags', {}) for s in data.get('streams', [])]
                entries.append(data.get('format', {}).get('tags', {}))
                for tags in entries:
                    created, label = find_creation_tag(tags)
                    if created:
                        source = f'メディアタグ: {label}'
                        break
                if data.get('format', {}).get('duration'):
                    duration = float(data['format']['duration'])
                for stream in data.get('streams', []):
                    if stream.get('codec_name'):
                        codec = str(stream['codec_name'])
                        break
        except InterruptedError:
            raise
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            pass
    if created is None:
        ffmpeg = media_binary('ffmpeg', engine_python)
        if ffmpeg:
            try:
                result = run_cancellable([ffmpeg, '-hide_banner', '-i', str(path)],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=40, cancel=cancel, registry=registry, creationflags=hidden_flags())
                for key in MEDIA_CREATION_TAGS:
                    match = re.search(r'^\s*' + re.escape(key) + r'\s*:\s*(.+?)\s*$',
                                      result.stderr, re.IGNORECASE | re.MULTILINE)
                    if match:
                        created = parse_creation_time(match.group(1))
                        if created:
                            source = f'メディアタグ: {key}'
                            break
            except InterruptedError:
                raise
            except (OSError, subprocess.SubprocessError):
                pass
    if created is None and os.name == 'nt':
        try:
            stat = path.stat()
            created = datetime.fromtimestamp(stat.st_ctime).astimezone()
            source = 'Windowsファイル作成日時（録音日時とは限りません）'
        except OSError:
            pass
    return MediaMetadata(path, created, source, duration, codec)


def format_date(dt: datetime | None) -> str:
    return dt.isoformat(sep=' ', timespec='seconds') if dt else '取得不可'


def metadata_header(metadata: MediaMetadata, *, transcribed_at: datetime,
                    profile: str, model: str, device: str,
                    components: Sequence[MediaMetadata] = ()) -> str:
    fields = [
        '# 文字起こし情報',
        f'入力ファイル: {metadata.path.name}',
        f'文字起こし日時: {format_date(transcribed_at)}',
        f'メディア作成日時: {format_date(metadata.created)}',
        f'作成日時の取得元: {metadata.source or "なし"}',
        f'音声認識方式: {profile}',
        f'モデル: {model}',
        f'実行デバイス: {device}',
    ]
    if metadata.duration_seconds is not None:
        fields.append(f'収録時間: {metadata.duration_seconds:.2f}秒')
    if components:
        fields.append(f'結合元ファイル数: {len(components)}')
        for i, m in enumerate(components, 1):
            fields.append(f'  {i}. {m.path.name} / 作成日時: {format_date(m.created)} / 取得元: {m.source or "なし"}')
    return '\n'.join(fields) + '\n\n--- 文字起こし本文 ---\n\n'


def unique_output(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f'{path.stem}_{i}{path.suffix}')
        if not candidate.exists():
            return candidate
    raise ValueError('出力ファイル名が重複しすぎています。')


def decode_text(path: Path) -> str:
    content = path.read_bytes()
    if content.startswith((b'\xff\xfe', b'\xfe\xff')):
        return content.decode('utf-16')
    if content.startswith(b'\xef\xbb\xbf'):
        return content.decode('utf-8-sig')
    # Japanese Shift-JIS bytes can occasionally form *valid* UTF-8 sequences.
    # Use a cautious fallback only when the UTF-8 result contains PUA or
    # compatibility ideographs (strong signal of mojibake in ordinary text).
    try:
        decoded=content.decode('utf-8')
        suspicious=any(unicodedata.category(ch) in ('Co', 'Cs') or
                       '\uf900' <= ch <= '\ufaff' for ch in decoded)
        if not suspicious:
            return decoded
    except UnicodeDecodeError:
        pass
    try:
        return content.decode('cp932')
    except UnicodeDecodeError:
        raise ValueError(f'文字コードを判定できません: {path.name}') from None


def merge_text_files(sources: Sequence[Path], destination: Path, *, headings: bool = True,
                     overwrite: bool = False, cancel=None) -> Path:
    check_cancel(cancel)
    if len(sources) < 2:
        raise ValueError('テキストを2件以上選択してください。')
    paths = [Path(p).resolve() for p in sources]
    if any(p.suffix.lower() != '.txt' for p in paths):
        raise ValueError('TXT形式のファイルのみ結合できます。')
    dest = Path(destination)
    if any(p == dest.resolve() for p in paths):
        raise ValueError('入力ファイルと出力ファイルの場所が同じです。')
    if dest.exists() and not overwrite:
        raise FileExistsError(f'既存のファイルは上書きしません: {dest}')
    # Validate/decode all inputs before changing destination.
    pieces = []
    for i, path in enumerate(paths, 1):
        check_cancel(cancel)
        text = decode_text(path).rstrip('\r\n')
        pieces.append((f'===== {i}. {path.name} =====\n' if headings else '') + text)
    result = '\n\n'.join(pieces).rstrip('\r\n') + '\n'
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(f'.{dest.stem}.{uuid.uuid4().hex}.tmp')
    try:
        staged.write_text(result, encoding='utf-8')
        if dest.exists() and not overwrite:
            raise FileExistsError(f'既存のファイルは上書きしません: {dest}')
        check_cancel(cancel)
        os.replace(staged, dest)
    finally:
        staged.unlink(missing_ok=True)
    return dest


def audio_codec_args(fmt: str, bitrate: str, channels: int, sample_rate: int) -> list[str]:
    if fmt not in AUDIO_FORMATS:
        raise ValueError('出力形式は mp3 / m4a / wav / flac に対応しています。')
    if fmt != 'wav' and fmt != 'flac' and bitrate not in BITRATES:
        raise ValueError('ビットレートを確認してください。')
    common = ['-ar', str(sample_rate), '-ac', str(channels)]
    if fmt == 'mp3':
        return ['-c:a', 'libmp3lame', '-b:a', bitrate] + common
    if fmt == 'm4a':
        return ['-c:a', 'aac', '-b:a', bitrate] + common
    if fmt == 'flac':
        return ['-c:a', 'flac'] + common
    return ['-c:a', 'pcm_s16le'] + common



def recording_codec_args(fmt: str, bitrate: str, pcm_bits: int, channels: int, sample_rate: int) -> list[str]:
    """Codec choices for a completed live capture; never encode on the capture thread."""
    if fmt not in AUDIO_FORMATS or pcm_bits not in (16, 24):
        raise ValueError('録音形式またはビット深度が不正です。')
    if bitrate not in BITRATES or channels not in (1, 2) or sample_rate not in (44100, 48000):
        raise ValueError('録音ビットレート・チャンネル・サンプルレートが不正です。')
    common = ['-ar', str(sample_rate), '-ac', str(channels)]
    if fmt == 'wav':
        return ['-c:a', 'pcm_s24le' if pcm_bits == 24 else 'pcm_s16le'] + common
    if fmt == 'flac':
        return ['-c:a', 'flac', '-sample_fmt', 's32' if pcm_bits == 24 else 's16',
                '-bits_per_raw_sample', str(pcm_bits)] + common
    if fmt == 'mp3':
        return ['-c:a', 'libmp3lame', '-b:a', bitrate] + common
    return ['-c:a', 'aac', '-b:a', bitrate] + common


def encode_live_recording(source: Path, destination: Path, *, ffmpeg: str,
                          fmt: str = 'mp3', bitrate: str = '192k', pcm_bits: int = 16,
                          channels: int = 2, sample_rate: int = 48000,
                          run=None, cancel=None, registry=None) -> Path:
    """Encode atomically. Leave original uninterrupted PCM WAV untouched on failure."""
    check_cancel(cancel)
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.stat().st_size <= 44:
        raise ValueError('変換元の録音WAVが存在しません。')
    if source.resolve() == destination.resolve() or destination.suffix.lower() != '.' + fmt:
        raise ValueError('録音変換の入出力先または拡張子が不正です。')
    if destination.exists():
        raise FileExistsError(f'録音ファイルを上書きしません: {destination}')
    codec_args = recording_codec_args(fmt, bitrate, pcm_bits, channels, sample_rate)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name('.' + destination.stem + '.' + uuid.uuid4().hex + destination.suffix)
    try:
        command = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                   '-i', str(source), '-vn', *codec_args, str(staged)]
        result = run(command) if run is not None else run_ffmpeg(command,cancel=cancel,registry=registry)
        check_cancel(cancel)
        if result.returncode != 0 or not staged.is_file() or staged.stat().st_size == 0:
            raise RuntimeError('録音の音声変換に失敗しました: ' + (result.stderr or '')[-1500:])
        if destination.exists():
            raise FileExistsError(f'変換先が作成されました: {destination}')
        staged.rename(destination)
        return destination
    finally:
        staged.unlink(missing_ok=True)


def run_ffmpeg(command: list[str], *, cancel=None, registry=None, **kwargs) -> subprocess.CompletedProcess:
    """Run FFmpeg invisibly, allow Stop to interrupt even during one long input."""
    kwargs.pop('capture_output',None);kwargs.pop('text',None)
    kwargs.pop('encoding',None);kwargs.pop('errors',None)
    return run_cancellable(command,cancel=cancel,registry=registry,capture_output=True,
        text=True,encoding='utf-8',errors='replace',creationflags=hidden_flags(),**kwargs)


def concat_audio(sources: Sequence[Path], destination: Path, *, ffmpeg: str,
                 fmt: str = 'mp3', bitrate: str = '192k', channels: int = 1,
                 sample_rate: int = 48000, cancel=None, registry=None,
                 run: Callable | None = None, progress: Callable[[str], None] = lambda _: None) -> Path:
    """Sequentially normalize mixed audio/video inputs then concatenate; never mix tracks.

    Intermediate normalized WAVs have matching PCM parameters. This avoids
    depending on the input container/audio codec and avoids Windows command
    length limits. Output committed only after success.
    """
    if run is None:
        run = lambda command, **kwargs: run_ffmpeg(command, cancel=cancel,registry=registry)
    paths = [Path(p).resolve() for p in sources]
    dest = Path(destination)
    if len(paths) < 2 or any(not p.is_file() for p in paths):
        raise ValueError('実在する音声・動画を2件以上選択してください。')
    if any(p == dest.resolve() for p in paths):
        raise ValueError('出力先と入力ファイルを同じにできません。')
    if channels not in (1, 2) or sample_rate not in (16000, 44100, 48000):
        raise ValueError('チャンネル数かサンプルレートを確認してください。')
    codec_args = audio_codec_args(fmt, bitrate, channels, sample_rate)
    if dest.suffix.lower() != f'.{fmt}':
        raise ValueError('出力ファイルの拡張子が選択形式と一致しません。')
    if dest.exists():
        raise FileExistsError(f'既存の音声ファイルは上書きしません: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='transcriber_concat_') as td:
        work = Path(td)
        pieces = []
        stage = dest.with_name(f'.{dest.stem}.{uuid.uuid4().hex}.processing{dest.suffix}')
        try:
            for i, source in enumerate(paths):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('結合を中止しました。')
                normalized = work / f'part_{i:05d}.wav'
                progress(f'音声を正規化中 {i+1}/{len(paths)}: {source.name}')
                command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-i', str(source), '-map', '0:a:0', '-vn', '-ar', str(sample_rate),
                    '-ac', str(channels), '-c:a', 'pcm_s16le', str(normalized)]
                finished = run(command, capture_output=True, text=True,
                               encoding='utf-8', errors='replace',
                               creationflags=hidden_flags())
                if finished.returncode != 0:
                    raise RuntimeError(f'{source.name}の音声取り出しに失敗: {finished.stderr[-1000:]}')
                pieces.append(normalized)
            if cancel is not None and cancel.is_set():
                raise InterruptedError('結合を中止しました。')
            manifest = work / 'concat.txt'
            # All normalized pieces use an ASCII-only temp path created by tempfile.
            manifest.write_text(''.join('file ' + "'" + p.as_posix().replace("'", "'\\''") + "'\n"
                                        for p in pieces), encoding='utf-8')
            progress('音声を結合・エンコード中…')
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                       '-f', 'concat', '-safe', '0', '-i', str(manifest),
                       '-vn'] + codec_args + [str(stage)]
            finished = run(command, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', creationflags=hidden_flags())
            if finished.returncode != 0:
                raise RuntimeError('音声の結合に失敗: ' + finished.stderr[-1000:])
            if cancel is not None and cancel.is_set():
                raise InterruptedError('結合を中止しました。')
            if not stage.is_file() or stage.stat().st_size == 0:
                raise RuntimeError('出力音声ファイルが生成されませんでした。')
            if dest.exists():
                raise FileExistsError(f'処理中に同名ファイルが作成されました: {dest}')
            os.replace(stage, dest)
            return dest
        finally:
            stage.unlink(missing_ok=True)
