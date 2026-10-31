"""Structured progress for Whisper/Qwen and conservative parsing of engine logs.
Percent always refers to the *current stage*, never total job completion.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass

PREFIX = '@@TRANSCRIBER@@'

@dataclass(frozen=True)
class EngineUpdate:
    stage: str
    fraction: float | None = None
    snippet: str = ''
    detail: str = ''


def parse_engine_line(line: str) -> EngineUpdate | None:
    text = line.strip()
    # Models can be fetched as part of normal inference, not just setup.
    # Lazy import avoids a module cycle: setup_progress uses EngineUpdate.
    if text.startswith('@@SETUP_PROGRESS@@'):
        from setup_progress import parse_setup_line
        return parse_setup_line(text)
    if text.startswith(PREFIX):
        try:
            raw = json.loads(text[len(PREFIX):])
            if not isinstance(raw, dict) or raw.get('stage') not in ('asr','diarization'):
                return None
            name = '音声認識' if raw['stage']=='asr' else '話者分離'
            value = None
            if raw.get('total') is not None:
                count = float(raw['total'])
                if count > 0:
                    value = min(1.0,max(0.0,float(raw.get('completed',0))/count))
            detail = str(raw.get('detail',''))[:95]
            snippet = str(raw.get('text',''))[:2000]
            return EngineUpdate(name,value,snippet,detail)
        except (ValueError,TypeError,OverflowError):
            return None
    m = re.search(r'Resemblyzerセグメント処理中:\s*(\d+)\s*/\s*(\d+)',text)
    if m and int(m[2]):
        return EngineUpdate('話者分離',min(1.,int(m[1])/int(m[2])),detail=f'{m[1]}/{m[2]} 区間')
    m = re.search(r'Qwen音声認識・タイムスタンプ付与中:\s*(\d+)\s*/\s*(\d+)',text)
    if m:
        # Log is emitted before inference; current chunk has NOT finished.
        return EngineUpdate('音声認識',max(0,int(m[1])-1)/int(m[2]) if int(m[2]) else None,
                            detail=f'{m[1]}/{m[2]} チャンクを処理中')
    if 'モデルを読み込み中' in text or 'Whisperモデル' in text or 'Forced Aligner' in text:
        return EngineUpdate('モデル読み込み')
    if '音声を準備中' in text or '音声を抽出中' in text:
        return EngineUpdate('音声準備')
    if '音声認識を実行中' in text or 'Whisperで文字起こし中' in text:
        return EngineUpdate('音声認識')
    if '話者分離中' in text or '話者特徴量を抽出中' in text:
        return EngineUpdate('話者分離')
    if text.startswith('完了:') or '文字起こし結果を保存しました:' in text:
        return EngineUpdate('ファイル保存')
    return None
