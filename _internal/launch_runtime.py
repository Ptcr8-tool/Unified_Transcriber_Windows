"""Trusted script directory for the bundled Python's isolated launch."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import runpy
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='文字起こし統合スタジオの同梱Python起動')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args(argv)
    code = Path(__file__).resolve().parent
    # This path is the verified application's own source folder. Isolated mode
    # excludes arbitrary current-directory/PYTHONPATH modules before this point.
    sys.path.insert(0, str(code))
    if args.self_test:
        import tkinter as tk
        from core import APP_VERSION, Config, app_config_path
        from portable_runtime import is_portable
        Config().validate()
        window = tk.Tk()
        try:
            window.withdraw()
            window.update()
        finally:
            window.destroy()
        print(json.dumps({'version':APP_VERSION, 'tk':tk.TkVersion,
            'portable':is_portable(code.parent),
            'settings_local':app_config_path().parent == code.parent/'Data'}))
    else:
        previous_argv = sys.argv
        try:
            sys.argv = [str(code / 'start_gui.pyw')] + (['--background'] if args.background else [])
            runpy.run_path(str(code / 'start_gui.pyw'), run_name='__main__')
        finally:
            sys.argv = previous_argv
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
