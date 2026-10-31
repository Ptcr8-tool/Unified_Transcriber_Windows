"""Download a selected model into the *same default caches* used by the engines.

Runs inside the selected engine's own virtualenv. No GPU allocation is necessary.
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

from bulk_catalog import MODEL_CHOICES
from core import QWEN_ALIGNER
from model_defaults import faster_model_args
from setup_progress import emit_setup_progress, hf_snapshot_progress, progress_tqdm


def preload(backend: str, model: str = '', *, token: str = '') -> None:
    if backend == 'pyannote':
        if model:
            raise ValueError('pyannoteはモデルの個別指定を受け付けません。')
        if not token:
            raise ValueError('pyannoteにはHF_TOKENとモデル利用条件への同意が必要です。')
        # Same security allowlist and pipeline entrypoint as actual transcription.
        from engines.pyannote_backend import load_diarization_pipeline
        print('pyannote: speaker-diarization-3.1 と依存モデルを取得中…', flush=True)
        emit_setup_progress('話者分離モデルを準備中', detail='ダウンロード・読み込み')
        with hf_snapshot_progress('話者分離モデルを取得中'):
            pipe = load_diarization_pipeline(token, 'cpu')
        if pipe is None:
            raise RuntimeError('pyannoteのパイプラインを読み込めませんでした。')
        print('pyannote: モデル取得・パイプライン読込完了', flush=True)
        return
    if backend not in MODEL_CHOICES or model not in MODEL_CHOICES[backend]:
        raise ValueError('許可されていないモデルの組み合わせです。')
    if backend == 'openai':
        import whisper
        # Official Whisper cache path used by whisper.load_model(name).
        if model not in whisper.available_models():
            raise ValueError('導入済みWhisperではこのモデルをサポートしません。')
        print(f'OpenAI Whisper: {model} を標準キャッシュへ取得中…', flush=True)
        emit_setup_progress('モデルを取得中', detail=f'OpenAI Whisper · {model}')
        url = whisper._MODELS[model]
        cache_dir = Path(os.getenv('XDG_CACHE_HOME') or (Path.home() / '.cache')) / 'whisper'
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Prefer the exact same downloader used by actual whisper.load_model.
        original_tqdm = getattr(whisper, 'tqdm', None)
        bar = progress_tqdm(f'モデル取得 · {model}')
        try:
            if original_tqdm is not None and bar is not None:
                whisper.tqdm = bar
            whisper._download(url, str(cache_dir), False)
        finally:
            if original_tqdm is not None:
                whisper.tqdm = original_tqdm
    elif backend == 'faster':
        print(f'faster-whisper: {model} を標準キャッシュへ取得中…', flush=True)
        emit_setup_progress('モデルを取得中', detail=f'faster-whisper · {model}')
        with hf_snapshot_progress(f'モデル取得 · {model}'):
            from faster_whisper.utils import download_model
            model_id, options = faster_model_args(model, download_model)
            if options or model_id == model:
                download_model(model_id, **options)
    elif backend == 'qwen':
        print(f'Qwen: {model} をHF共有キャッシュへ取得中…', flush=True)
        emit_setup_progress('モデルを取得中', detail=f'Qwen · {model}')
        with hf_snapshot_progress('Qwenモデルを取得中'):
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=model, token=token or None)
        print(f'Qwen ForcedAligner: {QWEN_ALIGNER} を取得中…', flush=True)
        emit_setup_progress('モデルを取得中', detail='Qwen ForcedAligner')
        with hf_snapshot_progress('Qwenタイムスタンプモデルを取得中'):
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=QWEN_ALIGNER, token=token or None)
    emit_setup_progress('モデルの取得完了', detail='次の処理を確認中')
    print('モデルの事前ダウンロード完了', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Model prefetch for transcription engines')
    parser.add_argument('--backend', required=True, choices=('openai', 'faster', 'qwen', 'pyannote'))
    parser.add_argument('--model', default='')
    a = parser.parse_args()
    try:
        preload(a.backend, a.model, token=os.environ.get('HF_TOKEN', ''))
        return 0
    except Exception as exc:
        print('事前ダウンロード失敗: ' + str(exc).replace(os.environ.get('HF_TOKEN', '') or '\0', '[TOKEN]'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
