"""Optional CLI using exactly the same engine/profile mapping as the Windows GUI."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path
from core import (BY_ID, PROFILES, build_command, choose_output, env_python,
                  load_config, redact, supported_file)


def resolve_device(device: str, python: Path, backend: str = 'faster') -> str:
    if device != 'auto':
        return device
    try:
        result = subprocess.run([str(python), '-u', str(Path(__file__).resolve().parent / 'cuda_diagnostic.py'), '--backend', backend],
                                capture_output=True, text=True, check=False, timeout=40)
        return 'cuda' if result.returncode == 0 else 'cpu'
    except (OSError, subprocess.SubprocessError, IndexError):
        return 'cpu'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Unified Transcriber 5-backend CLI')
    parser.add_argument('files', nargs='*', type=Path)
    parser.add_argument('--profile', choices=tuple(BY_ID))
    parser.add_argument('--device', choices=('auto','cpu','cuda'))
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--diarization-unit', choices=('utterance', 'fixed'),
                        help='Resemblyzerの話者分析区間')
    parser.add_argument('--segment-length', type=float, help='固定長の場合の分析秒数')
    parser.add_argument('--speaker-mode', choices=('auto', 'fixed'), help='Resemblyzerの人数推定')
    parser.add_argument('--speakers', type=int, help='話者数を固定する場合の人数')
    parser.add_argument('--faster-model', help='faster-whisperモデル')
    parser.add_argument('--list-profiles', action='store_true')
    args = parser.parse_args(argv)
    if args.list_profiles:
        for profile in PROFILES:
            print(profile.id, '-', profile.name)
        return 0
    if not args.files:
        parser.error('音声・動画ファイルを指定してください。')
    cfg = load_config()
    if args.profile:cfg.profile=args.profile
    if args.device:cfg.device=args.device
    if args.output_dir:cfg.output_dir=str(args.output_dir)
    if args.overwrite:cfg.overwrite=True
    if args.diarization_unit:cfg.diarization_unit=args.diarization_unit
    if args.segment_length is not None:cfg.segment_length=args.segment_length
    if args.speaker_mode:cfg.speaker_mode=args.speaker_mode
    if args.speakers is not None:cfg.speakers=args.speakers
    if args.faster_model:cfg.model_faster=args.faster_model
    try:
        cfg.validate()
    except ValueError as exc:
        print('設定が不正です: ' + str(exc), file=sys.stderr)
        return 2
    python=env_python(cfg.profile,cfg)
    if not python.is_file():
        print('実行環境がありません。GUIの「環境セットアップ」を実行してください:',python,file=sys.stderr)
        return 2
    token=os.environ.get('HF_TOKEN','')
    if BY_ID[cfg.profile].diarizer=='pyannote' and not token:
        print('pyannote方式には環境変数 HF_TOKEN が必要です。',file=sys.stderr)
        return 2
    device=resolve_device(cfg.device,python,BY_ID[cfg.profile].asr)
    failures=0
    for media in args.files:
        media=media.expanduser().resolve()
        if not supported_file(media):
            print('ファイルが見つからないか、未対応の形式です:',media,file=sys.stderr)
            failures+=1
            continue
        dest=choose_output(media,cfg)
        if dest.exists() and not cfg.overwrite:
            print('スキップ（既存のTXTを保護）:',dest)
            continue
        dest.parent.mkdir(parents=True,exist_ok=True)
        stage=dest.parent/f'.{dest.stem}.{uuid.uuid4().hex[:12]}.processing.txt'
        try:
            command=build_command(media,stage,cfg,python,device)
            from term_hints import apply_hint_environment
            child_env={**os.environ,'PYTHONIOENCODING':'utf-8'}
            apply_hint_environment(child_env,cfg)
            proc=subprocess.run(command,env=child_env,check=False)
            if proc.returncode!=0 or not stage.is_file() or stage.stat().st_size==0:
                print(f'失敗: {media.name} (終了コード {proc.returncode})',file=sys.stderr)
                failures+=1
                continue
            if dest.exists() and not cfg.overwrite:
                print('スキップ（処理中に同名TXTを検出）:',dest)
                continue
            stage.replace(dest)
            print('完了:',dest)
        except (OSError,subprocess.SubprocessError) as exc:
            print('失敗:',redact(str(exc),token),file=sys.stderr)
            failures+=1
        finally:
            stage.unlink(missing_ok=True)
    return 1 if failures else 0


if __name__=='__main__':
    raise SystemExit(main())

