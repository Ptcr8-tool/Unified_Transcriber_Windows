"""Fixed Python entry invoked by the standard NSIS setup executable."""
from __future__ import annotations

from pathlib import Path
import sys


def main(argv=None) -> int:
    # Python -I deliberately excludes even the script directory. Add exactly
    # this staged code directory, never the caller's current directory.
    # Keep the original spelling until initialize_context has checked every
    # ancestor for reparse points; resolving first could hide a junction.
    code = Path(__file__).absolute().parent
    sys.path.insert(0, str(code))
    try:
        from setup_context import initialize_context
        context = initialize_context(code.parent)
        # This import must remain after parent/runtime validation and Tcl
        # environment isolation. It imports tkinter and installation helpers.
        from installer_ui import main as run_installer
        return run_installer(context.arguments if argv is None else argv)
    except (OSError, ValueError, RuntimeError) as exc:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, str(exc), 'セットアップを開始できませんでした', 0x10)
        elif sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
