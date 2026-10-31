"""Small standalone recording environment; no torch / model downloads needed."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from core import BASE, CODE_DIR
from portable_runtime import is_portable, runtime_environment
from setup_backend import ensure_pip, repair_compression_dependencies, run_probe
from secure_media import ensure_media_tools
from setup_process import run_logged, setup_interpreter
from setup_environment import environment_build

ROOT = CODE_DIR / 'environments' / 'live-capture'
PYTHON = ROOT / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def setup() -> None:
    host_python = setup_interpreter(BASE, profile='live-capture')
    print(f'[録音環境] セットアップ用Python: {host_python}', flush=True)
    # A ready audio environment can still contain the old imageio FFmpeg.
    # Repair the shared, verified pair independently of pip/import readiness.
    if os.name == 'nt':
        ensure_media_tools(BASE, log=lambda line: print(line, flush=True))
    with environment_build(ROOT, BASE, host_python, supported=(3, 14)) as python:
        install_recording(python)
    print('録音環境のセットアップが完了しました。', flush=True)


def install_recording(python: Path) -> None:
    ensure_pip(python)
    repair_compression_dependencies(python)
    imports = 'import numpy, sounddevice, pyaudiowpatch'
    if os.name != 'nt':
        imports += ', imageio_ffmpeg'
    versions = ('; from importlib.metadata import version; '
                'assert version("numpy")=="2.5.4"; '
                'assert version("sounddevice")=="0.5.6"; '
                'assert version("PyAudioWPatch")=="0.2.12.9"')
    check = run_probe([str(python), '-c', imports + versions], purpose='録音ライブラリの確認',
                           capture_output=True, creationflags=FLAGS,
                           env=runtime_environment(BASE), shell=False)
    if check.returncode:
        print('PortAudio(sounddevice) / PyAudioWPatch(WASAPI loopback) / NumPyをインストール中（音声認識モデルは不要）。', flush=True)
        command=[str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check',
                 '--require-virtualenv', 'install', '--upgrade', '--only-binary=:all:',
                 '--index-url', 'https://pypi.org/simple',
                 '--constraint', str(CODE_DIR / 'constraints_security.txt'),
                 'sounddevice==0.5.6', 'PyAudioWPatch==0.2.12.9',
                 'numpy==2.5.4']
        if os.name != 'nt':
            command.append('imageio-ffmpeg>=0.5,<1')
        if is_portable(BASE):
            command += ['--cache-dir', str(BASE / 'Data' / 'pip-cache')]
        run_logged(command, BASE)
    run_logged([str(python), '-c', imports + versions + '; print("録音環境: PortAudio / PyAudioWPatch / NumPy OK")'], BASE)


if __name__ == '__main__':
    try:
        setup()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('録音環境のセットアップに失敗:', exc, file=sys.stderr)
        raise SystemExit(1)
