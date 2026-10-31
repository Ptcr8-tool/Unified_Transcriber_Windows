"""CUDA diagnostics for an isolated transcription engine environment.

Run with that environment's Python, not the GUI Python. No model downloads.
Exit: 0 usable for selected backend, 2 unavailable/not ready.
"""
from __future__ import annotations

import argparse
import sys


def diagnose(backend: str) -> int:
    print('=== CUDA 診断 ===', flush=True)
    print('Python:', sys.executable, flush=True)
    from cuda_probe import probe_cuda
    advisory = probe_cuda()
    print('ドライバー事前確認:', advisory.summary, flush=True)
    print(advisory.details, flush=True)
    if backend == 'faster':
        try:
            from engines.cuda_runtime import initialize_cuda_libraries
            libraries = initialize_cuda_libraries()
            if libraries['status'] == 'ready':
                print('CUDA DLL: 専用実行環境から ' + str(libraries['handle_count']) + ' 件を検証して読み込み', flush=True)
        except (ImportError, OSError, RuntimeError) as exc:
            print('NG: faster-whisper用CUDA DLLの初期化に失敗:', str(exc), flush=True)
            return 2

    try:
        import torch
    except Exception as exc:
        print('NG: PyTorchの読み込みに失敗:', repr(exc), flush=True)
        return 2
    print('PyTorch:', torch.__version__, flush=True)
    print('PyTorch搭載CUDA:', torch.version.cuda or 'なし（CPU版の可能性）', flush=True)
    if not torch.cuda.is_available():
        print('NG: torch.cuda.is_available() が False です。', flush=True)
        print('CUDA版PyTorch、NVIDIAドライバー、対応GPUをご確認ください。', flush=True)
        return 2
    try:
        name = torch.cuda.get_device_name(0)
        props = torch.cuda.get_device_properties(0)
        print(f'検出GPU: {name} / VRAM: {props.total_memory / 2**30:.2f} GiB', flush=True)
        capability = torch.cuda.get_device_capability(0)
        print(f'GPU演算能力: {capability[0]}.{capability[1]}', flush=True)
        if backend == 'qwen':
            bf16_native = capability[0] >= 8 and torch.cuda.is_bf16_supported()
            print('Qwen推論精度: ' + ('BF16' if bf16_native else 'FP16') +
                  (' (BF16非対応GPUのためFP16を使用)' if not bf16_native else ''), flush=True)
        # A small real CUDA operation, not just a driver/configuration check.
        a = torch.arange(4096, device='cuda', dtype=torch.float32).reshape(64, 64)
        output = (a @ a.T).sum()
        torch.cuda.synchronize()
        value = float(output.item())
        if not (value > 0):
            raise RuntimeError('計算結果が予期した値ではありません。')
        print('OK: PyTorch CUDAメモリ確保＋行列演算＋GPU同期に成功', flush=True)
        del a, output
        torch.cuda.empty_cache()
    except Exception as exc:
        print('NG: PyTorch CUDAの実演算に失敗:', repr(exc), flush=True)
        return 2

    if backend == 'faster':
        try:
            import ctranslate2
            print('CTranslate2:', ctranslate2.__version__, flush=True)
            types = sorted(ctranslate2.get_supported_compute_types('cuda'))
            print('CTranslate2 CUDA対応形式:', ', '.join(types) or '(なし)', flush=True)
            if not types:
                print('NG: CTranslate2でCUDAが利用できません。', flush=True)
                return 2
            print('OK: CTranslate2のCUDAバックエンドを認識', flush=True)
        except Exception as exc:
            print('NG: faster-whisper用CTranslate2 CUDA診断失敗:', repr(exc), flush=True)
            print('cuBLAS/cuDNNなどの必要DLLとCTranslate2の互換性を確認してください。', flush=True)
            return 2
    else:
        print('この方式の音声認識・話者分離はPyTorchを利用します。', flush=True)

    print('=== 判定: CUDA利用可能（モデル全体の推論成功を保証するものではありません） ===', flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=('openai', 'faster', 'qwen'), required=True)
    args = parser.parse_args()
    return diagnose(args.backend)


if __name__ == '__main__':
    raise SystemExit(main())
