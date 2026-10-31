"""Small frozen entry point; execute only the bundled, isolated interpreter."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from core import APP_VERSION
from portable_runtime import reset_dll_search_path, runtime_environment


def application_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent


def reset_dll_search() -> None:
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        reset_dll_search_path()


def _write_report(path: Path | None, report: dict) -> bool:
    if path is None:
        return True
    try:
        path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        return True
    except OSError as exc:
        print('自己検査の結果ファイルを保存できませんでした: ' + str(exc), file=sys.stderr)
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='文字起こし統合スタジオ')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--self-test-report', type=Path)
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args(argv)
    args.self_test = args.self_test or args.self_test_report is not None
    if args.version:
        print(APP_VERSION)
        return 0
    root = application_root()
    python = root / 'Runtime' / ('python.exe' if args.self_test else 'pythonw.exe')
    entry = root / '_internal' / 'launch_runtime.py'
    try:
        if not python.is_file() or not entry.is_file():
            raise RuntimeError('実行ファイルが不足しています。セットアップexeの「修復」を実行してください。')
        reset_dll_search()
        command = [str(python), '-I', '-X', 'utf8', str(entry)]
        if args.self_test:
            command.append('--self-test')
        elif args.background:
            command.append('--background')
        env = runtime_environment(root)
        if args.self_test:
            result = subprocess.run(command, cwd=str(root), env=env, shell=False,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
            report = {'version': APP_VERSION, 'ok': result.returncode == 0,
                      'runtime': result.stdout.strip(), 'errors': result.stderr.strip()}
            saved = _write_report(args.self_test_report, report)
            print(json.dumps(report, ensure_ascii=False))
            return result.returncode if saved else 1
        subprocess.Popen(command, cwd=str(root), env=env, shell=False,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        _write_report(args.self_test_report, {'version':APP_VERSION,'ok':False,'errors':str(exc)})
        if os.name == 'nt' and not args.self_test:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), '文字起こし統合スタジオ', 0x10)
        else:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
