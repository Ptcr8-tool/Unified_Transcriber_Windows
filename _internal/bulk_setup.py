"""Batch setup entrypoint. Usage:
  Runtime/python.exe -X utf8 _internal/bulk_setup.py --preset recommended-cuda
  Runtime/python.exe -X utf8 _internal/bulk_setup.py --profile faster-res:cuda --profile qwen-res:cpu \\
      --model faster:large-v3-turbo --model qwen:neosophie/Qwen3-ASR-1.7B-JA

Continues after individual failures and prints a final summary. Return nonzero if
anything failed. All subprocess windows are hidden on Windows when called from GUI.
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path
from bulk_catalog import BulkPlan, preset, planned_summary
from core import BASE, CODE_DIR, BY_ID
from setup_process import run_logged, setup_interpreter

def parse_pair(value: str) -> tuple[str, str]:
    if ':' not in value:
        raise argparse.ArgumentTypeError('profile:hardware または backend:model の形式で指定してください')
    left, right = value.split(':', 1)
    if not left or not right:
        raise argparse.ArgumentTypeError('空の指定はできません')
    return left, right


def command_sequence(plan: BulkPlan, *, with_models=True, with_audio=False) -> list[tuple[str, list[str], str | None]]:
    """Deterministic plan. Shared model caches: each model is prefetched once."""
    plan.validate()
    steps: list[tuple[str, list[str], str | None]] = []
    python = setup_interpreter(BASE)
    for key, hardware in plan.profiles:
        steps.append((f'環境: {BY_ID[key].name} [{hardware}]',
                      [python, '-X', 'utf8', '-u', str(CODE_DIR / 'setup_backend.py'),
                       '--profile', key, '--hardware', hardware], key))
    if with_models:
        for backend, model in plan.models:
            host = next(key for key, _ in plan.profiles if BY_ID[key].asr == backend)
            steps.append((f'モデル: {model}',
                         ['-X','utf8','-u', str(CODE_DIR / 'preload_model.py'),
                          '--backend',backend,'--model',model], host))
        if plan.pyannote and any(BY_ID[p].diarizer=='pyannote' for p, _ in plan.profiles):
            host = next(p for p, _ in plan.profiles if BY_ID[p].diarizer == 'pyannote')
            steps.append(('話者分離モデル: pyannote/speaker-diarization-3.1',
                          ['-X','utf8','-u',str(CODE_DIR / 'preload_model.py'),
                           '--backend','pyannote'],host))
    if with_audio:
        steps.append(('音声収録環境（PortAudio/WASAPI/FFmpeg）',
                      [python, '-X', 'utf8', '-u', str(CODE_DIR / 'setup_live_capture.py')],
                      'live-capture'))
    return steps


def run_plan(plan: BulkPlan, *, with_models=True, with_audio=False, dry_run=False) -> int:
    from core import environment_folder
    tasks = command_sequence(plan, with_models=with_models, with_audio=with_audio)
    print('=== 一括セットアップ ===', flush=True)
    print(planned_summary(plan), flush=True)
    status: dict[str, bool] = {}
    failures: list[str] = []
    for index, (title, command, key) in enumerate(tasks, 1):
        print(f'\n[{index}/{len(tasks)}] {title}', flush=True)
        is_environment = command[0] != '-X'
        if not is_environment and not status.get(key, False):
            # The same ASR family may exist in two independent profiles. If
            # its first profile failed, use another successfully set-up host.
            backend = command[command.index('--backend') + 1]
            candidates = (profile for profile, _ in plan.profiles
                          if (BY_ID[profile].diarizer == 'pyannote' if backend == 'pyannote'
                              else BY_ID[profile].asr == backend))
            key = next((profile for profile in candidates if status.get(profile)), None)
            if key is None:
                failures.append(title)
                print('SKIP: 対象の仮想環境の構築が失敗したためモデル取得をスキップ', flush=True)
                continue
            print(f'代替環境を使用: {BY_ID[key].name}', flush=True)
        python = command[0] if is_environment else str(environment_folder(key) /
                                       ('Scripts/python.exe' if os.name=='nt' else 'bin/python'))
        full_command = command if is_environment else [python, *command]
        if dry_run:
            if is_environment:
                status[key] = True
            print('DRY-RUN: ' + title, flush=True)
            continue
        if not Path(python).is_file():
            failures.append(title)
            print(f'ERROR: Pythonが存在しません: {python}', flush=True)
            if is_environment:
                status[key] = False
            continue
        # Do not echo tokens or use shells; HF_TOKEN is inherited in-memory.
        try:
            # The selected setup script has its own 30-minute inactivity
            # watchdog and must be allowed to stop/clean up its child first.
            proc = run_logged(full_command, BASE, check=False, idle_timeout=60 * 60)
        except (OSError, RuntimeError) as exc:
            failures.append(title)
            if is_environment:
                status[key] = False
            print(f'ERROR: {title}: 起動できませんでした: {exc}', flush=True)
            continue
        ok = proc.returncode == 0
        if is_environment:
            status[key] = ok
        if not ok:
            failures.append(title)
        print(f'{"OK" if ok else "ERROR"}: {title} (終了コード {proc.returncode})', flush=True)
    print(f'\n=== 完了: 成功 {len(tasks)-len(failures)}/{len(tasks)} 件 ===', flush=True)
    if failures:
        print('失敗/スキップした項目:\n' + '\n'.join(' - '+f for f in failures),flush=True)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description='Windows transcription multi-profile setup')
    p.add_argument('--preset', choices=('recommended-cuda','recommended-cpu','all-cuda','all-cpu'))
    p.add_argument('--profile', action='append', type=parse_pair, default=[])
    p.add_argument('--model', action='append', type=parse_pair, default=[])
    p.add_argument('--skip-models', action='store_true')
    p.add_argument('--no-pyannote-model', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--with-audio',action='store_true',help='録音環境も導入')
    a = p.parse_args()
    try:
        if a.preset and (a.profile or a.model):
            p.error('--preset と --profile/--model は併用できません。')
        if a.preset:
            plan = preset(a.preset)
        elif a.profile:
            plan = BulkPlan(tuple(a.profile), tuple(a.model), not a.no_pyannote_model)
        else:
            plan = preset('recommended-cuda')
        if a.no_pyannote_model:
            plan = BulkPlan(plan.profiles, plan.models, False)
        plan.validate()
        return run_plan(plan, with_models=not a.skip_models, with_audio=a.with_audio, dry_run=a.dry_run)
    except (ValueError, RuntimeError, OSError) as exc:
        print('設定エラー: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
