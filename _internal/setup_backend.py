"""Application-owned setup: Python 3.14 for ②, Python 3.11 for legacy profiles.
The installed application selects its bundled interpreter, without global Python.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from core import BASE, CODE_DIR, BY_ID, environment_folder
from portable_runtime import is_portable, runtime_environment
from secure_media import ensure_media_tools
from setup_process import run_logged, setup_interpreter
from setup_environment import environment_build


NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
PROBE_TIMEOUT = 60


def run_probe(command, *, purpose: str, **kwargs):
    """Bound short dependency/version probes even if a native import hangs."""
    try:
        return subprocess.run(command, timeout=PROBE_TIMEOUT, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'{purpose}が{PROBE_TIMEOUT}秒以内に完了しませんでした。') from exc


def run(*args: str) -> None:
    command = list(args)
    if len(command) > 3 and command[1:3] == ['-m', 'pip'] and 'install' in command:
        # Never inherit a user's extra index, trusted-host or pip.ini. The
        # optional OpenAI Whisper package may require an official PyPI sdist;
        # other profiles use wheels rather than executing source build hooks.
        if '--index-url' not in command:
            command += ['--index-url', 'https://pypi.org/simple']
        command += ['--constraint', str(CODE_DIR / 'constraints_security.txt')]
        if not any(value.startswith('openai-whisper') for value in command):
            command.append('--only-binary=:all:')
        if is_portable(BASE):
            command += ['--cache-dir', str(BASE / 'Data' / 'pip-cache')]
    print('> ' + ' '.join(command), flush=True)
    run_logged(command, BASE)


def ensure_pip(python: Path) -> None:
    """Repair interrupted venv creation rather than failing with 'No module named pip'."""
    status = run_probe([str(python), '-m', 'pip', '--version'], purpose='pipの確認',
                            capture_output=True, text=True, check=False, creationflags=NO_WINDOW,
                            env=runtime_environment(BASE), shell=False)
    if status.returncode != 0:
        print('仮想環境にpipがありません。ensurepipで修復します。', flush=True)
        try:
            run(str(python), '-m', 'ensurepip', '--upgrade')
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('pipの修復に失敗。該当environmentsフォルダーを削除し、再セットアップしてください。') from exc
        verify = run_probe([str(python), '-m', 'pip', '--version'], purpose='pipの復元確認',
                                capture_output=True, text=True, check=False, creationflags=NO_WINDOW,
                                env=runtime_environment(BASE), shell=False)
        if verify.returncode != 0:
            raise RuntimeError('pipを復元できませんでした。該当方式の仮想環境を再作成してください。')


def repair_compression_dependencies(python: Path) -> None:
    """Repair known vulnerable optional decoders without importing native code.

    Constraints alone do not upgrade an already installed, otherwise unused
    transitive package. Read only its package metadata, then repair affected
    versions in this application's selected virtual environment.
    """
    code = '''import json
from importlib import metadata
from pip._vendor.packaging.version import Version, InvalidVersion
updates = []
for name, floor in (('Brotli', '1.2.1'), ('brotlicffi', '1.2.1.0')):
    try:
        installed = metadata.version(name)
    except metadata.PackageNotFoundError:
        continue
    try:
        affected = Version(installed) < Version(floor)
    except InvalidVersion:
        affected = True
    if affected:
        updates.append(name + '>=' + floor)
print(json.dumps(updates))
'''
    try:
        result = subprocess.run([str(python), '-c', code], capture_output=True, text=True,
            encoding='utf-8', errors='strict', timeout=30, creationflags=NO_WINDOW,
            env=runtime_environment(BASE), shell=False)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise RuntimeError('追加環境の安全性を確認できません。環境セットアップを修復してください。') from exc
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise RuntimeError('追加環境の安全性を確認できません。環境セットアップを修復してください。')
    try:
        updates = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('追加環境のパッケージ情報を確認できません。') from exc
    allowed = {'Brotli>=1.2.1', 'brotlicffi>=1.2.1.0'}
    if (not isinstance(updates, list) or len(updates) > 2
            or any(not isinstance(value, str) or value not in allowed for value in updates)
            or len(set(updates)) != len(updates)):
        raise RuntimeError('追加環境のパッケージ情報を確認できません。')
    if updates:
        print('既存の追加環境にある圧縮ライブラリのセキュリティ更新を適用します。', flush=True)
        run(str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check',
            '--require-virtualenv', 'install', '--upgrade', *updates)


def torch_baseline(profile: str | None = None) -> str:
    """Use the patched pair for the supported minimum profile.

    The pyannote/Qwen legacy stacks have independent constraints and must not
    be silently moved across torchaudio's removed APIs. Their migration needs
    separate inference compatibility testing before changing those profiles.
    """
    if profile is not None and profile not in BY_ID:
        raise ValueError('未対応の実行方式です。')
    return '2.14.1' if profile == 'faster-res' else '2.8.0'


def torchaudio_baseline(profile: str | None = None) -> str:
    # TorchAudio 2.11 uses the stable Torch ABI and supports Torch >= 2.11.
    torch_baseline(profile)  # Validate the profile consistently.
    return '2.11.0' if profile == 'faster-res' else '2.8.0'


def torch_ready(python: Path, hardware: str, profile: str | None = None) -> bool:
    """Reuse only a matching, hardware-appropriate torch/torchaudio pair."""
    version = torch_baseline(profile)
    audio_version = torchaudio_baseline(profile)
    code = ("import torch, torchaudio; "
            f"assert torch.__version__.split('+')[0] == '{version}'; "
            f"assert torchaudio.__version__.split('+')[0] == '{audio_version}'; "
            f"assert {'torch.version.cuda is not None' if hardware == 'cuda' else 'torch.version.cuda is None'}")
    result = run_probe([str(python), '-c', code], purpose='PyTorchの確認',
                            capture_output=True, text=True, check=False, creationflags=NO_WINDOW,
                            env=runtime_environment(BASE), shell=False)
    return result.returncode == 0


def setup(profile: str, hardware: str) -> None:
    if profile not in BY_ID or hardware not in ('cpu', 'cuda'):
        raise ValueError('Unknown profile / hardware')
    host_python = setup_interpreter(BASE, profile=profile)
    print(f'[実行環境] セットアップ用Python: {host_python}', flush=True)
    # Media tools are shared by every profile. Always verify/repair them,
    # including when an existing virtual environment needs no ML upgrades.
    if os.name == 'nt':
        ensure_media_tools(BASE, log=lambda line: print(line, flush=True))
    folder = environment_folder(profile)
    supported = (3, 14) if profile == 'faster-res' else (3, 11)
    with environment_build(folder, BASE, host_python, supported=supported) as python:
        install_profile(profile, hardware, python)
    print('\nセットアップ完了。GUIからこの方式を実行できます。', flush=True)


def install_profile(profile: str, hardware: str, python: Path) -> None:
    ensure_pip(python)
    repair_compression_dependencies(python)
    pip = (str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', '--require-virtualenv')
    # Bootstrap pip from one pinned, hash-checked wheel rather than executing
    # whatever version happens to be latest when setup is run.  Reinstall even
    # when the version matches: an existing installation's files were not
    # authenticated by pip's normal "already satisfied" path.
    run(*pip, 'install', '--upgrade', '--force-reinstall', '--require-hashes',
        '--no-deps', '-r', str(CODE_DIR / 'requirements-setup-pip.lock'))

    torch_version = torch_baseline(profile)
    audio_version = torchaudio_baseline(profile)
    torch_index = f'https://download.pytorch.org/whl/{"cpu" if hardware == "cpu" else "cu126"}'
    if torch_ready(python, hardware, profile):
        print(f'PyTorch {torch_version} は既に対応版がインストールされています。再ダウンロードを省略。', flush=True)
    else:
        run(*pip, 'install', '--upgrade',
            f'torch=={torch_version}', f'torchaudio=={audio_version}', '--index-url', torch_index)

    p = BY_ID[profile]
    common = ['numpy==2.5.4'] if profile == 'faster-res' else ['numpy==1.26.4']
    if os.name != 'nt':
        common.append('imageio-ffmpeg')
    if p.asr == 'openai':
        # Explicit webrtcvad wheel avoids compiling the original VAD on Windows.
        run(*pip, 'install', *common, 'openai-whisper', 'librosa',
            'scikit-learn==1.5.2', 'scipy', 'resampy', 'soundfile', 'webrtcvad-wheels')
        run(*pip, 'install', '--no-deps', 'resemblyzer==0.1.4')
    elif p.diarizer == 'resemblyzer':
        # faster-whisper 1.2.1 calls av.open(metadata_errors=...), removed in
        # PyAV 19. Explicitly select a compatible wheel even in reused venvs.
        extra = (['faster-whisper==1.2.1', 'av==18.1.0', 'ctranslate2==4.8.4', 'scikit-learn==1.9.1',
                  'librosa==1.0.0', 'scipy==1.18.1', 'resampy', 'soundfile', 'webrtcvad-wheels']
                 if profile == 'faster-res' else
                 ['faster-whisper', 'scikit-learn==1.5.2', 'librosa', 'scipy',
                  'resampy', 'soundfile', 'webrtcvad-wheels'])
        if p.asr == 'qwen':
            extra.extend(['qwen-asr==0.0.6', 'huggingface-hub==0.36.0'])
            run(*pip, 'install', '--constraint', str(CODE_DIR / 'constraints_qwen_res.txt'), *common, *extra)
        else:
            run(*pip, 'install', *common, *extra)
        run(*pip, 'install', '--no-deps', 'resemblyzer==0.1.4')
    else:
        extra = ['faster-whisper', 'pyannote.audio==3.3.2', 'huggingface-hub==0.36.0']
        if p.asr == 'qwen':
            extra.append('qwen-asr==0.0.6')
            run(*pip, 'install', '--constraint', str(CODE_DIR / 'constraints_qwen_pya.txt'), *common, *extra)
        else:
            run(*pip, 'install', *common, *extra)

    run(str(python), '-c',
        'import torch; print("PyTorch", torch.__version__, "CUDA", torch.cuda.is_available()); '
        'print("GPU", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none")')
    if profile == 'faster-res':
        # Check the entire modern import stack and its included speaker
        # checkpoint before replacing an older Python environment. This does
        # not download or load the selected, much larger ASR model.
        run(str(python), '-c',
            'import torch,torchaudio,numpy,scipy,sklearn,librosa,faster_whisper,ctranslate2; '
            'from resemblyzer import VoiceEncoder; '
            'encoder=VoiceEncoder(device="cpu"); '
            'assert next(encoder.parameters()).device.type=="cpu"; '
            'print("方式②: 依存ライブラリ・話者モデルの読み込み OK")')
        # Import checks alone miss removed PyAV arguments. Exercise the actual
        # faster-whisper decoder with a generated WAV; no model or network needed.
        run(str(python), '-c',
            'import io,wave; from faster_whisper.audio import decode_audio; '
            'b=io.BytesIO(); w=wave.open(b,"wb"); w.setnchannels(1); '
            'w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\\0\\0"*1600); '
            'w.close(); b.seek(0); a=decode_audio(b,sampling_rate=16000); '
            'assert len(a)==1600, "音声デコードの長さが一致しません"; '
            'print("方式②: 音声デコード OK")')
    if hardware == 'cuda':
        run(str(python), '-c',
            'import torch; assert torch.cuda.is_available(), '
            '"GPUが認識されません。NVIDIAドライバーを更新してください。"')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', required=True, choices=tuple(BY_ID))
    parser.add_argument('--hardware', required=True, choices=('cpu', 'cuda'))
    args = parser.parse_args()
    try:
        setup(args.profile, args.hardware)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'セットアップ失敗: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
