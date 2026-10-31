"""Subtitle-friendly partitioning of word-timed ASR output.

Never invent punctuation. Preserve original spoken text exactly (modulo whitespace
at chunk boundaries). Prefer original word timing, estimate only when the ASR
provides a single long segment rather than individual words.
"""
from __future__ import annotations


def _chunks(text: str, size: int) -> list[str]:
    """Find natural clause/space boundaries without losing any characters."""
    out=[]
    while len(text)>size:
        # Favor a recent punctuation / whitespace boundary; do not require it.
        lower=max(1,int(size*0.55))
        cut=size
        for idx in range(size,lower-1,-1):
            if text[idx-1] in '。！？!?、，,；; ':
                cut=idx
                break
        out.append(text[:cut]);text=text[cut:]
    if text:out.append(text)
    return out


def split_item(item, *, max_chars: int, max_seconds: float):
    """Return objects of the same type as `item`. Long ASR segments use estimated timing."""
    duration=max(0.,float(item.end)-float(item.start))
    text=str(item.text)
    if len(text)<=max_chars and duration<=max_seconds:
        return [type(item)(item.start,item.end,item.speaker,item.text)]
    import math
    count=max(1,math.ceil(len(text)/max_chars),math.ceil(duration/max_seconds))
    # Break at punctuation preferentially but respect the duration limit too.
    target=max(1,min(max_chars,math.ceil(len(text)/count)))
    pieces=_chunks(text,target)
    result=[]
    for idx,part in enumerate(pieces):
        start=float(item.start)+duration*idx/len(pieces)
        end=float(item.start)+duration*(idx+1)/len(pieces)
        result.append(type(item)(start,end,item.speaker,part))
    return result


def split_timed_items(items, *, max_chars: int = 28, max_seconds: float = 5.) -> list:
    if not (8<=max_chars<=120 and 1.<=max_seconds<=30.):
        raise ValueError('字幕用の最大文字数・最大秒数が範囲外です。')
    result=[]
    for item in items:
        for portion in split_item(item,max_chars=max_chars,max_seconds=max_seconds):
            # Do not combine across speaker boundaries; respect punctuation and timing.
            if result and result[-1].speaker==portion.speaker:
                previous=result[-1]
                duration=float(portion.end)-float(previous.start)
                text=previous.text+portion.text
                if (len(text)<=max_chars and duration<=max_seconds and
                    float(portion.start)-float(previous.end)<=1.2):
                    previous.text=text
                    previous.end=portion.end
                    continue
            result.append(portion)
    return result
