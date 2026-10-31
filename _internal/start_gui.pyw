"""Console-free startup, including optional Explorer drag/drop bootstrap.

When pythonw.exe launches a .pyw file, there is no usable sys.stderr/stdout.
Never silently discard failures: keep a startup log and show a Windows error box.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys
import traceback

from diagnostic_logs import log_directory
from output_locations import default_directory

CODE_DIR = Path(__file__).resolve().parent
BASE = CODE_DIR.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))


def startup_log_path() -> Path:
    """Use the same user-selected diagnostics folder as the running GUI."""
    return log_directory() / 'gui_startup.log'


def ensure_drag_drop(log) -> bool:
    """Verified package installation without pip or conflicting user settings."""
    from dnd_support import ensure_drag_drop as install_dnd
    return install_dnd(log)


def show_launch_error(log_path: Path, message: str) -> None:
    text = (f'GUIを起動できませんでした。\n\n{message}\n\n'
            f'起動ログ: {log_path}\n\n'
            '詳細は _internal\\tools の診断用バッチを参照してください。')
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, '文字起こし統合スタジオ - 起動エラー', 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.__stderr__ if sys.__stderr__ is not None else sys.stdout)


def main() -> int:
    try:
        log_path = startup_log_path()
    except OSError as exc:
        show_launch_error(default_directory('logs') / 'gui_startup.log',
                          f'ログフォルダーを作成できません: {exc}\n'
                          'ドキュメント内、またはセットアップで選択した出力先への書き込み権限を確認してください。')
        return 1
    previous_out, previous_err = sys.stdout, sys.stderr
    try:
        with log_path.open('w', encoding='utf-8', buffering=1) as log:
            sys.stdout = log
            sys.stderr = log
            try:
                print(f'Startup: {BASE}', file=log)
                print(f'Python: {sys.executable}', file=log)
                try:
                    ready = ensure_drag_drop(log)
                    print(f'D&D bootstrap status: {"ready" if ready else "unavailable"}', file=log)
                except Exception:
                    print('Optional drag/drop setup failed; continuing with file selection.', file=log)
                    traceback.print_exc(file=log)
                runpy.run_path(str(CODE_DIR / 'unified_gui.py'), run_name='__main__')
                return 0
            except Exception as exc:
                traceback.print_exc(file=log)
                show_launch_error(log_path, str(exc))
                return 1
            finally:
                sys.stdout = previous_out
                sys.stderr = previous_err
    except OSError as exc:
        show_launch_error(log_path, str(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())
