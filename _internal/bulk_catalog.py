"""Single source of truth for batch setup presets and model choices.

This module deliberately imports no optional/ML packages: the GUI can import it
before any of the 5 environments is installed.
"""
from __future__ import annotations
from dataclasses import dataclass
from core import BY_ID, QWEN_MODEL

MODEL_CHOICES = {
    'openai': ('turbo', 'small', 'medium', 'large-v3'),
    'faster': ('large-v3-turbo', 'small', 'medium', 'large-v3', 'distil-large-v3'),
    'qwen': (QWEN_MODEL, 'Qwen/Qwen3-ASR-0.6B', 'Qwen/Qwen3-ASR-1.7B'),
}
DEFAULT_MODELS = {'openai': ('turbo',), 'faster': ('large-v3-turbo',), 'qwen': (QWEN_MODEL,)}
MODEL_LABELS = {
    'turbo': 'turbo（高速・高精度）',
    'large-v3-turbo': 'large-v3-turbo（高速・高精度）',
    'small': 'small（軽量）',
    'medium': 'medium（中型）',
    'large-v3': 'large-v3（高精度・大容量）',
    'distil-large-v3': 'distil-large-v3（高速蒸留）',
    QWEN_MODEL: 'Qwen3-ASR-1.7B-JA（日本語特化）',
    'Qwen/Qwen3-ASR-0.6B': 'Qwen3-ASR-0.6B（軽量）',
    'Qwen/Qwen3-ASR-1.7B': 'Qwen3-ASR-1.7B（標準）',
}

@dataclass(frozen=True)
class BulkPlan:
    # Sequence allows device per profile; order determines execution order.
    profiles: tuple[tuple[str, str], ...]
    models: tuple[tuple[str, str], ...]
    pyannote: bool = True

    def validate(self) -> None:
        if not self.profiles:
            raise ValueError('最低1つの実行方式を選んでください。')
        ids = [key for key, _ in self.profiles]
        if len(set(ids)) != len(ids):
            raise ValueError('同じ実行方式が重複しています。')
        for key, hardware in self.profiles:
            if key not in BY_ID or hardware not in ('cpu', 'cuda'):
                raise ValueError(f'実行方式またはCPU/GPUの指定が不正です: {key}/{hardware}')
        if len(set(self.models)) != len(self.models):
            raise ValueError('モデル指定が重複しています。')
        for backend, model in self.models:
            if backend not in MODEL_CHOICES or model not in MODEL_CHOICES[backend]:
                raise ValueError(f'未対応のモデルです: {backend}/{model}')
            if not any(BY_ID[p].asr == backend for p, _ in self.profiles):
                raise ValueError(f'モデル {model} の音声認識方式が選択されていません。')


def preset(name: str) -> BulkPlan:
    if name not in ('recommended-cuda', 'recommended-cpu', 'all-cuda', 'all-cpu'):
        raise ValueError(f'不明なプリセット: {name}')
    hardware = 'cuda' if name.endswith('cuda') else 'cpu'
    ids = ('faster-res', 'qwen-res') if name.startswith('recommended') else tuple(BY_ID)
    backends = {BY_ID[key].asr for key in ids}
    plan = BulkPlan(tuple((key, hardware) for key in ids),
                    tuple((backend, model) for backend in ('openai', 'faster', 'qwen')
                          if backend in backends for model in DEFAULT_MODELS[backend]))
    plan.validate()
    return plan


def planned_summary(plan: BulkPlan) -> str:
    plan.validate()
    return (f'実行環境 {len(plan.profiles)}件 / 音声認識モデル {len(plan.models)}件'
            + (' / pyannoteモデル 1組' if plan.pyannote and any(
                BY_ID[p].diarizer == 'pyannote' for p, _ in plan.profiles) else '')
            + '\nモデルは同じキャッシュに保存され、同一モデルの再取得を極力避けます。'
              '環境ごとのライブラリ容量は別途必要です。')
