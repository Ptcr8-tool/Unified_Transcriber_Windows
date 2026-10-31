"""Pure-Python timestamp offsets and provisional transcript assembly."""
from __future__ import annotations

from pathlib import Path
from transcript_tools import TRANSCRIPT_LINE
from media_tools import decode_text


def shift_timestamp(value: str, offset: float) -> str:
    v = value.replace(',', '.')
    parts = v.split(':')
    if len(parts) != 3:
        raise ValueError(f'Invalid timestamp: {value}')
    h, m, s = parts
    total = int(h) * 3600 + int(m) * 60 + float(s) + offset
    if total < 0:
        raise ValueError('Negative timestamp')
    if '.' in v:
        milliseconds=round(total * 1000)
        seconds, ms = divmod(milliseconds, 1000)
        return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}.{ms:03d}'
    whole = round(total)
    return f'{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}'


def shifted_lines(source: Path, offset: float) -> list[str]:
    result=[]
    for line in decode_text(source).lstrip('\ufeff').splitlines():
        m = TRANSCRIPT_LINE.fullmatch(line)
        if m is None:
            if line.strip():
                # Never silently drop unparseable recognition text.
                result.append(f'[未解析 / {offset:.1f}s] {line}')
            continue
        shifted=shift_timestamp(m['time'], offset)
        result.append(f'[{shifted}] {m["speaker"]}: {m["text"]}')
    return result


def append_provisional(source: Path, destination: Path, *, offset: float,
                       index: int, first: bool = False) -> int:
    lines=shifted_lines(source, offset)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('a' if not first else 'x', encoding='utf-8-sig' if first else 'utf-8', newline='\n') as out:
        if first:
            out.write('# ライブ文字起こし（暫定）\n')
            out.write('# 区間ごとに推論するため、話者番号は区間をまたいで一致するとは限りません。\n')
            out.write('# 録音停止後の全文再解析で最終版を作れます。\n\n')
        for line in lines:
            out.write(line+'\n')
        out.flush()
    return len(lines)
