"""Trusted entry for the ordinary Windows launcher and isolated bundled Python."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> int:
    code = Path(__file__).resolve().parent
    # -I excludes the current directory, PYTHONPATH, user-site and script folder.
    # Only this application's fixed source folder is deliberately restored.
    sys.path.insert(0, str(code))
    from portable_runtime import runtime_environment
    environment = runtime_environment(code.parent)
    os.environ.clear()
    os.environ.update(environment)
    from app_launcher import main as launch
    return launch()


if __name__ == '__main__':
    raise SystemExit(main())
