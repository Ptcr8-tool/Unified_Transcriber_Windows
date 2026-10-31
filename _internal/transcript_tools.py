"""Safe, schema-aware transcript exports, CSV joins, and speaker labels.

Speaker changes are restricted to the recognized label field, NEVER replace text
inside spoken content. The original input is never modified; outputs are staged.
"""
from __future__ import annotations

from process_control import check_cancel

import csv
import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Mapping, Sequence
from media_tools import decode_text

# Timestamps emitted by all five bundled engines, with optional milliseconds.
TRANSCRIPT_LINE = re.compile(
    r'^\[(?P<time>\d{1,5}:[0-5]\d:[0-5]\d(?:[.,]\d{1,3})?)\]'
    r'(?P<gap>\s+)(?P<speaker>[^:\r\n]+)(?P<colon>:[ \t]*)(?P<text>[^\r\n]*)$'
)
# CSV column names are intentionally stable across models and source files.
CSV_COLUMNS = ('timestamp', 'speaker', 'text', 'source_file',
               'transcribed_at', 'media_created_at', 'model', 'asr', 'device')
# Round-trip conversion CSV includes physical TXT lines as dedicated records.
# Ordinary ASR CSV export remains the stable, compact 9-column schema.
ROUNDTRIP_VERSION = 'transcriber-txt-roundtrip-v1'
SPARSE_ROUNDTRIP_VERSION = 'transcriber-txt-roundtrip-v2'
METADATA_COLUMNS = CSV_COLUMNS[3:]
METADATA_REFERENCE = 'metadata_ref'
ROUNDTRIP_FIELDS = ('record_type', 'line_index', 'raw_line', 'line_ending', 'original_timestamp',
                    'original_speaker', 'original_text', 'original_metadata',
                    'source_section', 'roundtrip_version')
ROUNDTRIP_COLUMNS = (*CSV_COLUMNS, *ROUNDTRIP_FIELDS)
SECTION_LINE = re.compile(r'^=====\s*\d+\.\s*(.+?)\s*=====$')
META_KEYS = {'入力ファイル': 'source_file', '文字起こし日時': 'transcribed_at', 'メディア作成日時': 'media_created_at',
             'モデル': 'model', '音声認識方式': 'asr', '実行デバイス': 'device',
             '元ファイル': 'source_file', 'エスケープ形式': 'escape_version'}
SPEAKER_ALIASES = ('speaker', '話者', 'speaker_id')
TEXT_ALIASES = ('text', '本文', 'utterance', 'transcript')
TIMESTAMP_ALIASES = ('timestamp', 'time', 'start', 'start_time', '開始時刻', '時刻')


def _atomic_text(path: Path, value: str, *, overwrite: bool = False,
                 encoding: str = 'utf-8-sig', cancel=None) -> Path:
    check_cancel(cancel)
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f'既存ファイルは上書きしません: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        stage.write_text(value, encoding=encoding, newline='')
        if path.exists() and not overwrite:
            raise FileExistsError(f'既存ファイルは上書きしません: {path}')
        check_cancel(cancel)
        os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)
    return path


def _split_metadata(contents: str) -> tuple[dict[str, str], str]:
    """Parse known app header without consuming any transcript content."""
    contents = contents.lstrip('\ufeff')
    marker = '--- 文字起こし本文 ---'
    if contents.startswith('# 文字起こし情報') and marker in contents:
        header, body = contents.split(marker, 1)
        metadata: dict[str, str] = {}
        for line in header.splitlines():
            if ': ' in line:
                key, value = line.split(': ', 1)
                if key in META_KEYS:
                    metadata[META_KEYS[key]] = value.strip()
        return metadata, body.lstrip('\r\n')
    return {}, contents


def _pick_column(columns: Sequence[str], choices: Sequence[str]) -> str | None:
    return next((col for name in choices for col in columns
                 if col.strip().lower() == name.lower()), None)


def _csv_reader(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    content = decode_text(path).lstrip('\ufeff')
    handle = io.StringIO(content, newline='')
    reader = csv.DictReader(handle)
    columns = reader.fieldnames or []
    if not columns:
        raise ValueError(f'CSVのヘッダーがありません: {path.name}')
    if len(set(columns)) != len(columns):
        raise ValueError(f'CSVヘッダーの列名が重複しています: {path.name}')
    rows = []
    for row in reader:
        if None in row:
            raise ValueError(f'CSVの列数が一致しません: {path.name}')
        rows.append({col: (row[col] if row[col] is not None else '') for col in columns})
    return columns, rows


def _physical_lines(content: str):
    """Yield physical Unicode lines and their actual EOL (preserve mixed CRLF)."""
    for line in content.splitlines(keepends=True):
        eol = ('\r\n' if line.endswith('\r\n') else
               '\n' if line.endswith('\n') else
               '\r' if line.endswith('\r') else '')
        yield line[:-len(eol)] if eol else line, eol


def _sparse_metadata(records, *, document=False, reference=False):
    """Write file metadata once, or once per source/context in a merged file."""
    previous = None
    references = {}
    for index, record in enumerate(records):
        row = dict(record)
        context = tuple(row.get(key, '') for key in METADATA_COLUMNS)
        changed = context != previous
        if reference:
            changed = context not in references
            references.setdefault(context, str(len(references)+1))
            row[METADATA_REFERENCE] = references[context]
        if not changed:
            for key in METADATA_COLUMNS:
                row[key] = ''
        if document:
            row['original_metadata'] = (json.dumps(dict(zip(METADATA_COLUMNS, context)),
                ensure_ascii=False, sort_keys=True) if changed else '')
            row['roundtrip_version'] = SPARSE_ROUNDTRIP_VERSION if index == 0 else ''
        previous = context
        yield row


def _expanded_metadata(records, *, document=False):
    """Read sparse v26 CSVs and older CSVs that repeated metadata on every row."""
    rows = [dict(row) for row in records]
    sparse_document = document and any(row.get('roundtrip_version') == SPARSE_ROUNDTRIP_VERSION
                                        for row in rows)
    if sparse_document:
        # Excel may reorder the document. Inheritance follows original line order.
        try:
            rows.sort(key=lambda row: int(row['line_index']))
        except (ValueError, KeyError):
            raise ValueError('情報保持CSVの行順序の番号が不正です。') from None
    context = {key: '' for key in METADATA_COLUMNS}
    contexts = {tuple(row.get(key, '') for key in METADATA_COLUMNS) for row in rows
                if any(row.get(key, '') for key in METADATA_COLUMNS)}
    if not document and len(contexts) == 1:
        # A single-file CSV may be sorted in Excel: its metadata is file-wide.
        context = dict(zip(METADATA_COLUMNS, next(iter(contexts))))
    references = {}
    linked = not document and any(row.get(METADATA_REFERENCE) for row in rows)
    if linked:
        for row in rows:
            if any(row.get(key, '') for key in METADATA_COLUMNS):
                ref = row.get(METADATA_REFERENCE, '')
                metadata = {key: row.get(key, '') for key in METADATA_COLUMNS}
                if not ref or (ref in references and references[ref] != metadata):
                    raise ValueError('CSVのメタデータ参照が欠落・重複しています。')
                references[ref] = metadata
    original = ''
    version = ''
    for row in rows:
        if linked:
            ref = row.get(METADATA_REFERENCE, '')
            if ref not in references:
                raise ValueError('CSVのメタデータ参照先が見つかりません。')
            row.update(references[ref])
        explicit = any(row.get(key, '') for key in METADATA_COLUMNS)
        if explicit:
            context = {key: row.get(key, '') for key in METADATA_COLUMNS}
        elif not document or sparse_document:
            row.update(context)
        if sparse_document:
            if row.get('original_metadata'):
                original = row['original_metadata']
            else:
                row['original_metadata'] = original
            if row.get('roundtrip_version'):
                version = row['roundtrip_version']
            else:
                row['roundtrip_version'] = version
        yield row


def _parse_document_txt(path: Path) -> list[dict[str, str]]:
    """Interpret structural lines separately, without losing one physical line."""
    contents = decode_text(path)
    meta, _ = _split_metadata(contents)
    rows = []
    section = ''
    found = 0
    for lineno, (line, ending) in enumerate(_physical_lines(contents), 1):
        printable = line.lstrip('\ufeff')
        heading = SECTION_LINE.fullmatch(printable)
        if heading:
            section = heading.group(1)
        match = TRANSCRIPT_LINE.fullmatch(printable)
        row = {k: '' for k in ROUNDTRIP_COLUMNS}
        row.update({key: value for key, value in meta.items() if key in CSV_COLUMNS})
        row['line_index'] = str(lineno)
        row['record_type'] = 'utterance' if match else ('section' if heading else 'structure')
        row['raw_line'] = line
        row['line_ending'] = ending
        row['source_section'] = section
        row['roundtrip_version'] = ROUNDTRIP_VERSION
        row['source_file'] = meta.get('source_file') or section or path.name
        if match:
            found += 1
            row['timestamp'] = match['time']
            row['speaker'] = match['speaker']
            row['text'] = (decode_escaped_text(match['text'])
                           if meta.get('escape_version') == 'backslash-v1' else match['text'])
            row['original_timestamp'] = row['timestamp']
            row['original_speaker'] = row['speaker']
            row['original_text'] = row['text']
            row['original_metadata'] = json.dumps(
                {k: row[k] for k in CSV_COLUMNS if k not in ('timestamp','speaker','text')},
                ensure_ascii=False, sort_keys=True)
        rows.append(row)
    if not found:
        raise ValueError(f'時刻と話者を含む発話行が見つかりません: {path.name}。'
                         'プレーンテキストは構造化CSVへ変換できません。')
    return rows


def transcript_rows(path: Path) -> list[dict[str, str]]:
    """Read structured rows, skipping lossless CSV's header/comment records."""
    path = Path(path)
    if path.suffix.lower() == '.csv':
        columns, records = _csv_reader(path)
        time_col = _pick_column(columns, TIMESTAMP_ALIASES)
        speaker_col = _pick_column(columns, SPEAKER_ALIASES)
        text_col = _pick_column(columns, TEXT_ALIASES)
        if speaker_col is None or text_col is None:
            raise ValueError(f'話者列・本文列のあるCSVを指定してください: {path.name}')
        roundtrip = 'roundtrip_version' in columns
        result=[]
        for rec in _expanded_metadata(records, document=roundtrip):
            if roundtrip and rec.get('record_type') != 'utterance':
                continue
            row = {key: '' for key in CSV_COLUMNS}
            row.update({key: rec[key] for key in CSV_COLUMNS if key in rec})
            row['timestamp'] = rec.get(time_col, '') if time_col else ''
            row['speaker'] = rec[speaker_col]
            row['text'] = rec[text_col]
            row['source_file'] = rec.get('source_file', '') or path.name
            result.append(row)
        return result
    if path.suffix.lower() != '.txt':
        raise ValueError('TXTまたはCSVを指定してください。')
    lines = _parse_document_txt(path)
    # A flattened CSV merge must never discard free-form transcript content.
    inside_header = False
    for row in lines:
        if row['record_type'] != 'structure':
            continue
        part = row['raw_line'].lstrip('\ufeff').strip()
        if part == '# 文字起こし情報':
            inside_header = True
            continue
        if part == '--- 文字起こし本文 ---':
            inside_header = False
            continue
        if part and not inside_header and not part.startswith('#'):
            raise ValueError(f'{path.name} に発話以外の本文があります: {part[:50]}。'
                             '通常CSVへの変換では失われるため処理を中止しました。'
                             '情報保持付きの「TXT⇔CSV変換」を使用してください。')
    return [{key: row[key] for key in CSV_COLUMNS} for row in lines
            if row['record_type'] == 'utterance']


def export_roundtrip_csv(source: Path, destination: Path, *, overwrite: bool = False, cancel=None) -> Path:
    """CSV with lossless document layout records, plus editable speaker/text fields."""
    check_cancel(cancel)
    rows = _parse_document_txt(Path(source))
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=ROUNDTRIP_COLUMNS, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(_sparse_metadata(rows, document=True))
    return _atomic_text(Path(destination), buffer.getvalue(), overwrite=overwrite,cancel=cancel)


def restore_roundtrip_txt(source: Path, destination: Path, *, overwrite: bool = False, cancel=None) -> Path:
    """Restore source structure; apply only explicit changes to utterance fields."""
    check_cancel(cancel)
    columns, rows = _csv_reader(Path(source))
    missing = set(ROUNDTRIP_COLUMNS) - set(columns)
    if missing:
        raise ValueError(f'情報保持CSVに必須の列がありません: {", ".join(sorted(missing))}')
    extras = [k for k in columns if k not in ROUNDTRIP_COLUMNS]
    if any(rec.get(k, '') for k in extras for rec in rows):
        raise ValueError('TXTでは表現できない独自CSV列があります: ' + '、'.join(extras))
    if not rows:
        raise ValueError('変換するデータがありません。')
    try:
        positions = [int(row['line_index']) for row in rows]
    except ValueError:
        raise ValueError('行順序の番号が不正です。') from None
    if sorted(positions) != list(range(1, len(rows)+1)):
        raise ValueError('行順序の番号が欠落・重複しています。元のTXT構造を復元できません。')
    # Spreadsheet sorting should never scramble preamble / document sections.
    rows = [row for _, row in sorted(zip(positions,rows),key=lambda pair: pair[0])]
    rows = list(_expanded_metadata(rows, document=True))
    escaped_format = any(row['raw_line'].strip() == 'エスケープ形式: backslash-v1' for row in rows)
    output = []
    for idx, row in enumerate(rows, 1):
        check_cancel(cancel)
        if row['roundtrip_version'] not in (ROUNDTRIP_VERSION, SPARSE_ROUNDTRIP_VERSION):
            raise ValueError(f'{idx}行目の往復変換バージョンが不正です。')
        kind = row['record_type']
        line = row['raw_line']
        eol = row['line_ending']
        if eol not in ('', '\r', '\n', '\r\n'):
            raise ValueError(f'{idx}行目の改行コードが不正です。')
        if kind == 'utterance':
            match = TRANSCRIPT_LINE.fullmatch(line.lstrip('\ufeff'))
            if not match:
                raise ValueError(f'{idx}行目の元発話情報が破損しています。')
            try:
                original_meta = json.loads(row['original_metadata'])
            except (ValueError, TypeError):
                raise ValueError(f'{idx}行目のメタデータが破損しています。') from None
            for k, v in original_meta.items():
                if row.get(k, '') != v:
                    raise ValueError(f'{idx}行目の{k}列が変更されています。'
                                     'メタデータは構造行のraw_lineで変更してください。')
            original_values = (row['original_timestamp'], row['original_speaker'], row['original_text'])
            if (match['time'], match['speaker']) != original_values[:2]:
                raise ValueError(f'{idx}行目の元時刻・話者情報が不一致です。')
            if (row['timestamp'],row['speaker'],row['text']) != original_values:
                stamp, speaker, text = row['timestamp'], row['speaker'], row['text']
                if not re.fullmatch(r'\d{1,5}:[0-5]\d:[0-5]\d(?:[.,]\d{1,3})?',stamp):
                    raise ValueError(f'{idx}行目の時刻はHH:MM:SS形式にしてください。')
                if not speaker or any(c in speaker for c in ':\r\n'):
                    raise ValueError(f'{idx}行目の話者名に使用できない文字があります。')
                if escaped_format:
                    text = encode_escaped_text(text)
                elif any(c in text for c in '\r\n'):
                    raise ValueError(f'{idx}行目の本文に改行があります。'
                                     '一つの発話行として復元できないため変換を中止します。')
                # Preserve original BOM and delimiter spacing, not spoken content.
                prefix = line[:len(line)-len(line.lstrip('\ufeff'))]
                line = f'[{stamp}]{match["gap"]}{speaker}{match["colon"]}{text}'
                line = prefix + line
        elif kind not in ('structure','section'):
            raise ValueError(f'{idx}行目のレコード種別が不正です: {kind}')
        if '\n' in line or '\r' in line:
            raise ValueError(f'{idx}行目の元行に改行が混入しています。')
        output.append(line + eol)
    return _atomic_text(Path(destination), ''.join(output), overwrite=overwrite,cancel=cancel)


def encode_escaped_text(value: str) -> str:
    """One physical TXT line per utterance, including multiline CSV fields."""
    return value.replace('\\', '\\\\').replace('\r', '\\r').replace('\n', '\\n')


def decode_escaped_text(value: str) -> str:
    return re.sub(r'\\(\\|n|r)', lambda m: {'n': '\n', 'r': '\r', '\\': '\\'}[m[1]], value)


def convert_transcript_csv_to_txt(source: Path, destination: Path, *, overwrite: bool = False, cancel=None) -> Path:
    """Export structured app CSV as timestamped TXT, protecting nonrepresentable data.

    The TXT header keeps common source and metadata fields; newline and backslash
    characters are escaped with an explicit version marker. CSVs containing
    nonempty custom columns or per-row metadata are rejected rather than silently
    dropping data. The source remains intact.
    """
    check_cancel(cancel)
    source, destination = Path(source), Path(destination)
    if source.suffix.lower() != '.csv' or destination.suffix.lower() != '.txt':
        raise ValueError('変換元はCSV、変換先はTXTを指定してください。')
    if source.resolve() == destination.resolve():
        raise ValueError('入力と出力を同じファイルにできません。')
    columns, raw = _csv_reader(source)
    if 'roundtrip_version' in columns:
        return restore_roundtrip_txt(source, destination, overwrite=overwrite,cancel=cancel)
    if not raw:
        raise ValueError('CSVに発話データがありません。')
    aliases = (*SPEAKER_ALIASES, *TEXT_ALIASES, *TIMESTAMP_ALIASES)
    permitted = set(CSV_COLUMNS) | set(aliases) | {METADATA_REFERENCE}
    extra = [name for name in columns if name not in permitted and any(row.get(name, '') for row in raw)]
    if extra:
        raise ValueError('TXT形式では保持できない追加列があります: ' + '、'.join(extra))
    rows = transcript_rows(source)
    meta_keys = ('transcribed_at', 'media_created_at', 'model', 'asr', 'device', 'source_file')
    meta = {}
    for key in meta_keys:
        check_cancel(cancel)
        values = {row.get(key, '') for row in rows}
        if len(values) > 1:
            raise ValueError(f'行ごとに異なる {key} はTXTヘッダーに保持できません。元CSVを分割してください。')
        meta[key] = next(iter(values), '')
    header = ['# 文字起こし情報']
    labels = {'transcribed_at':'文字起こし日時', 'media_created_at':'メディア作成日時',
              'model':'モデル', 'asr':'音声認識方式', 'device':'実行デバイス',
              'source_file':'元ファイル'}
    for key, title in labels.items():
        check_cancel(cancel)
        if meta[key]:
            if '\n' in meta[key] or '\r' in meta[key]:
                raise ValueError(f'{key} のメタデータに改行があります。TXTへ変換できません。')
            header.append(f'{title}: {meta[key]}')
    header += ['エスケープ形式: backslash-v1', '--- 文字起こし本文 ---']
    utterances=[]
    pattern = re.compile(r'\d{1,5}:[0-5]\d:[0-5]\d(?:[.,]\d{1,3})?\Z')
    for idx, row in enumerate(rows, 1):
        check_cancel(cancel)
        stamp, speaker, text = row['timestamp'], row['speaker'], row['text']
        if not pattern.fullmatch(stamp) or not speaker or any(c in speaker for c in '\r\n:'):
            raise ValueError(f'{idx}行目の時刻または話者がTXT形式と互換ではありません。')
        utterances.append(f'[{stamp}] {speaker}: {encode_escaped_text(text)}')
    return _atomic_text(destination, '\n'.join(header + utterances) + '\n', overwrite=overwrite,cancel=cancel)


def convert_transcript(source: Path, destination: Path, *, overwrite: bool = False, cancel=None) -> Path:
    """Convert between the app's timestamped TXT and canonical structured CSV."""
    check_cancel(cancel)
    source, destination = Path(source), Path(destination)
    if source.suffix.lower() == '.txt' and destination.suffix.lower() == '.csv':
        return export_roundtrip_csv(source, destination, overwrite=overwrite,cancel=cancel)
    if source.suffix.lower() == '.csv' and destination.suffix.lower() == '.txt':
        return convert_transcript_csv_to_txt(source, destination, overwrite=overwrite,cancel=cancel)
    raise ValueError('対応する変換はTXT→CSV、CSV→TXTのみです。')


def serialize_csv(rows: Sequence[Mapping[str, str]]) -> str:
    output = io.StringIO(newline='')
    reference = len({tuple(row.get(key, '') for key in METADATA_COLUMNS) for row in rows}) > 1
    columns = (*CSV_COLUMNS, METADATA_REFERENCE) if reference else CSV_COLUMNS
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator='\r\n')
    writer.writeheader()
    for row in _sparse_metadata(rows, reference=reference):
        writer.writerow({key: row.get(key, '') for key in columns})
    return output.getvalue()


def export_transcript_csv(source: Path, destination: Path, *, overwrite: bool = False,
                          source_name: str | None = None) -> Path:
    """Convert one app-generated transcript to an Excel-compatible UTF-8 BOM CSV."""
    rows = transcript_rows(source)
    if source_name is not None:
        for row in rows:
            row['source_file'] = source_name
    return _atomic_text(destination, serialize_csv(rows), overwrite=overwrite)


def merge_transcript_csv(sources: Sequence[Path], destination: Path, *, overwrite: bool = False, cancel=None) -> Path:
    """Merge TXT/CSV transcriptions, preserving extra columns from source CSVs.

    Canonical columns come first. Non-standard CSV columns are appended to a
    union schema; no source field silently disappears.
    """
    check_cancel(cancel)
    if len(sources) < 2:
        raise ValueError('結合するTXT/CSVを2件以上選択してください。')
    dest = Path(destination)
    paths = [Path(p) for p in sources]
    if dest.suffix.lower() != '.csv':
        raise ValueError('CSV結合結果は.csvで保存してください。')
    if any(p.resolve() == dest.resolve() for p in paths):
        raise ValueError('入力ファイルと出力ファイルが同一です。')
    records: list[dict[str, str]] = []
    extras: list[str] = []
    for path in paths:
        check_cancel(cancel)
        if path.suffix.lower() == '.txt':
            records.extend(transcript_rows(path))
        elif path.suffix.lower() == '.csv':
            columns, raw = _csv_reader(path)
            timestamp_col = _pick_column(columns, TIMESTAMP_ALIASES)
            speaker_col = _pick_column(columns, SPEAKER_ALIASES)
            text_col = _pick_column(columns, TEXT_ALIASES)
            if speaker_col is None or text_col is None:
                raise ValueError(f'話者列・本文列が必要です: {path.name}')
            for column in columns:
                if column == METADATA_REFERENCE:
                    continue
                if column in ROUNDTRIP_FIELDS and column != 'source_section':
                    continue
                if column not in CSV_COLUMNS and column not in extras:
                    extras.append(column)
            for item in _expanded_metadata(raw, document='roundtrip_version' in columns):
                check_cancel(cancel)
                if 'roundtrip_version' in columns and item.get('record_type') != 'utterance':
                    continue
                normalized = {key: item.get(key, '') for key in CSV_COLUMNS}
                normalized.update({key:item[key] for key in extras if key in item})
                normalized['timestamp'] = item.get(timestamp_col, '') if timestamp_col else ''
                normalized['speaker'] = item[speaker_col]
                normalized['text'] = item[text_col]
                normalized['source_file'] = item.get('source_file', '') or path.name
                records.append(normalized)
        else:
            raise ValueError(f'CSV結合にはTXTまたはCSVだけを指定してください: {path.name}')
    buffer = io.StringIO(newline='')
    reference = len({tuple(row.get(key, '') for key in METADATA_COLUMNS) for row in records}) > 1
    columns = (*CSV_COLUMNS, *extras, *((METADATA_REFERENCE,) if reference else ()))
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(_sparse_metadata(records, reference=reference))
    return _atomic_text(dest, buffer.getvalue(), overwrite=overwrite,cancel=cancel)


def speakers_in_transcript(path: Path) -> list[str]:
    """Speaker IDs in first-appearance order; labels only, not words in the body."""
    path=Path(path)
    if path.suffix.lower() == '.csv':
        columns, records = _csv_reader(path)
        speaker_col = _pick_column(columns, SPEAKER_ALIASES)
        if speaker_col is None:
            raise ValueError('CSVに話者列がありません。')
        return list(dict.fromkeys(row[speaker_col] for row in records if row[speaker_col]))
    if path.suffix.lower() != '.txt':
        raise ValueError('TXTまたはCSVを指定してください。')
    _, body = _split_metadata(decode_text(path))
    return list(dict.fromkeys(m['speaker'] for line in body.splitlines()
                              if (m := TRANSCRIPT_LINE.fullmatch(line.lstrip('\ufeff')))))


def speaker_utterances(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Preview only structured speaker/text fields; never edit input or body.

    CSV: use speaker/time/text column aliases, preserving multiline dialogue.
    TXT: read only timestamp-prefixed speaker lines; ignore preamble and other
    non-utterance lines exactly as speakers_in_transcript() does.
    """
    path = Path(path)
    if path.suffix.lower() == '.csv':
        columns, records = _csv_reader(path)
        speaker_col = _pick_column(columns, SPEAKER_ALIASES)
        if speaker_col is None:
            raise ValueError('CSVに話者列がありません。')
        time_col = _pick_column(columns, TIMESTAMP_ALIASES)
        text_col = _pick_column(columns, TEXT_ALIASES)
        output: dict[str, list[tuple[str, str]]] = {}
        for row in records:
            speaker = row[speaker_col]
            if speaker:
                output.setdefault(speaker, []).append(
                    (row.get(time_col, '') if time_col else '',
                     row.get(text_col, '') if text_col else ''))
        return output
    if path.suffix.lower() == '.txt':
        _, body = _split_metadata(decode_text(path))
        output: dict[str, list[tuple[str, str]]] = {}
        for line in body.splitlines():
            match = TRANSCRIPT_LINE.fullmatch(line)
            if match:
                output.setdefault(match['speaker'], []).append((match['time'], match['text']))
        return output
    raise ValueError('TXTまたはCSVを指定してください。')


def _validate_map(names: Mapping[str, str]) -> dict[str, str]:
    result={}
    for original, new in names.items():
        old=str(original).strip()
        renamed=str(new).strip()
        if not old or not renamed or old == renamed:
            continue
        if any(ch in renamed for ch in '\r\n:') or len(renamed)>80:
            raise ValueError('話者名には改行・半角コロンを含めず、80文字以内にしてください。')
        result[old]=renamed
    return result


def replace_speaker_labels(source: Path, destination: Path, mapping: Mapping[str,str],
                           *, overwrite: bool = False) -> tuple[Path, int]:
    """Rewrite only the speaker field in TXT/CSV. Never replace spoken content."""
    source=Path(source)
    destination=Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('元ファイルを上書きしないでください。別名で保存します。')
    if source.suffix.lower()!=destination.suffix.lower():
        raise ValueError('元ファイルと出力ファイルの形式を一致させてください。')
    names=_validate_map(mapping)
    if not names:
        raise ValueError('変更する話者名がありません。')
    count=0
    if source.suffix.lower()=='.csv':
        columns, rows = _csv_reader(source)
        speaker_col=_pick_column(columns, SPEAKER_ALIASES)
        if speaker_col is None:
            raise ValueError('CSVに話者列がありません。')
        for row in rows:
            old=row[speaker_col]
            if old in names:
                row[speaker_col]=names[old]
                count+=1
        output=io.StringIO(newline='')
        writer=csv.DictWriter(output,fieldnames=columns,lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(rows)
        content=output.getvalue()
    elif source.suffix.lower()=='.txt':
        contents=decode_text(source)
        meta, _body = _split_metadata(contents)
        # Maintain preamble byte-for-byte at Unicode codepoint level. Rename
        # only timestamp/label lines following our known metadata delimiter.
        marker='--- 文字起こし本文 ---'
        preamble, separator, body=(contents.split(marker,1)[0], marker, contents.split(marker,1)[1]) \
            if contents.lstrip('\ufeff').startswith('# 文字起こし情報') and marker in contents \
            else ('','',contents)
        def rewrite(line: str) -> str:
            nonlocal count
            offset=len(line)-len(line.lstrip('\ufeff'))
            match=TRANSCRIPT_LINE.fullmatch(line[offset:])
            if match and match['speaker'] in names:
                count+=1
                a,b=match.span('speaker')
                return line[:offset+a]+names[match['speaker']]+line[offset+b:]
            return line
        content=preamble+separator+''.join(rewrite(line.rstrip('\r\n'))+line[len(line.rstrip('\r\n')):]
                              for line in body.splitlines(keepends=True))
    else:
        raise ValueError('TXTまたはCSVを指定してください。')
    if count == 0:
        raise ValueError('変更対象の話者ラベルが見つかりませんでした。')
    return _atomic_text(destination, content, overwrite=overwrite), count
