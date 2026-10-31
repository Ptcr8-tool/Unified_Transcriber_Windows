"""Install TkinterDnD2 from a pinned PyPI wheel without invoking pip.

Purpose: Windows pip configurations can enforce --user even with --isolated,
which conflicts with --target. No pip invocation or global Python writes occur.
The downloaded wheel's SHA-256 is verified before loading native binaries.
"""
from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from core import APP_VERSION

BASE = Path(__file__).resolve().parent
TARGET = BASE / 'gui_dependencies'
PKG = 'tkinterdnd2'
VERSION = '0.6.4'
WHEEL_NAME = f'tkinterdnd2-{VERSION}-py3-none-any.whl'
WHEEL_URL = ('https://files.pythonhosted.org/packages/73/a6/'
             '72313c930c923406bd55fd7d389f6fcab2064ccd76be3590ef9d584cf2cf/'
             f'{WHEEL_NAME}')
WHEEL_SHA256 = '4b11e0ac6febd8609faf154851d782eca4068bfa68a88ca3d3dded8294367f80'
MAX_SIZE = 6_000_000
LOCAL_WHEEL = BASE / WHEEL_NAME  # optional wheel for offline setup
SOURCE_WHEEL = '.verified-source.whl'
MAX_UNCOMPRESSED = 25_000_000


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & 0x400)


def _wheel_files(raw: bytes) -> dict[str, bytes]:
    """Read bounded, regular package members from a hash-verified wheel."""
    import io
    result = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
        for info in wheel.infolist():
            if not info.filename.startswith(PKG + '/') or info.is_dir():
                continue
            name = info.filename
            parts = name.split('/')
            mode = (info.external_attr >> 16) & 0o170000
            if (any(p in ('', '.', '..') for p in parts) or '\\' in name or
                    ':' in name or mode not in (0, 0o100000) or
                    len(parts) < 2 or parts[-1] == SOURCE_WHEEL or name in result):
                raise ValueError('Unsafe wheel entry: ' + name)
            total += info.file_size
            if info.file_size > MAX_UNCOMPRESSED or total > MAX_UNCOMPRESSED:
                raise ValueError('D&D wheel expands beyond the size limit')
            data = wheel.read(info)
            if len(data) != info.file_size:
                raise ValueError('D&D wheel entry size mismatch')
            result[name] = data
    if PKG + '/__init__.py' not in result or not any(n.startswith(PKG + '/tkdnd/') for n in result):
        raise ValueError('Wheel is incomplete: missing python code or tkdnd binaries')
    return result


def verify_installed(target: Path = TARGET, sha256: str = WHEEL_SHA256) -> bool:
    """Require all on-disk package members to match the pinned source wheel.

    This checks integrity and version, not whether the pinned wheel is benign.
    Extra files (including bytecode caches) and filesystem links fail closed.
    """
    try:
        target = Path(target)
        pkgdir = target / PKG
        marker = pkgdir / SOURCE_WHEEL
        if any(_is_link_or_reparse(path) for path in (target, pkgdir, marker)):
            return False
        if marker.stat().st_size > MAX_SIZE:
            return False
        raw = marker.read_bytes()
        if not raw or len(raw) > MAX_SIZE or hashlib.sha256(raw).hexdigest() != sha256:
            return False
        expected = _wheel_files(raw)
        found = set()
        for base, dirs, files in os.walk(pkgdir, followlinks=False):
            root = Path(base)
            if any(_is_link_or_reparse(root / d) for d in dirs):
                return False
            for filename in files:
                path = root / filename
                if _is_link_or_reparse(path) or not path.is_file():
                    return False
                name = (Path(PKG) / path.relative_to(pkgdir)).as_posix()
                if path == marker:
                    continue
                if name not in expected:
                    return False
                if path.stat().st_size != len(expected[name]):
                    return False
                if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(expected[name]).digest():
                    return False
                found.add(name)
        return found == set(expected)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def available(target: Path = TARGET, sha256: str = WHEEL_SHA256) -> bool:
    target = Path(target)
    if not verify_installed(target, sha256):
        return False
    # Do not accidentally reuse a module imported from a different directory.
    package_root = (target / PKG).resolve()
    for name, loaded in tuple(sys.modules.items()):
        if name != PKG and not name.startswith(PKG + '.'):
            continue
        loaded_file = getattr(loaded, '__file__', None)
        if not loaded_file:
            return False
        try:
            resolved = Path(loaded_file).resolve()
            resolved.relative_to(package_root)
        except (OSError, ValueError):
            return False
        if name == PKG and resolved != package_root / '__init__.py':
            return False
    # A stale occurrence further down sys.path must not allow a different,
    # unverified package earlier on the path to shadow the verified one.
    target_path = str(target)
    while target_path in sys.path:
        sys.path.remove(target_path)
    sys.path.insert(0, target_path)
    importlib.invalidate_caches()
    previous = sys.dont_write_bytecode
    try:
        # Bytecode from writable directories is not covered by the wheel hash.
        sys.dont_write_bytecode = True
        from tkinterdnd2 import TkinterDnD, DND_FILES
        return bool(TkinterDnD and DND_FILES)
    except (ImportError, RuntimeError, OSError):
        return False
    finally:
        sys.dont_write_bytecode = previous


def fetch_wheel(url: str = WHEEL_URL) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': f'UnifiedTranscriber/{APP_VERSION} (tkinterdnd2 bootstrap)'})
    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read(MAX_SIZE + 1)
    if not raw or len(raw) > MAX_SIZE:
        raise ValueError('D&D wheel download is missing or unexpectedly large')
    return raw


def install_wheel(raw: bytes, target: Path = TARGET, sha256: str = WHEEL_SHA256) -> None:
    if not raw or len(raw) > MAX_SIZE or hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('D&D download checksum mismatch; package was NOT installed')
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(target):
        raise ValueError('D&D target directory is a link or reparse point')
    staging = Path(tempfile.mkdtemp(prefix='.dnd-staging-', dir=str(target)))
    old_dir = None
    dst = target / PKG
    try:
        files = _wheel_files(raw)
        for name, data in files.items():
            output = staging.joinpath(*name.split('/'))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        pkgdir = staging / PKG
        # This copy is checked against the compile-time wheel hash on each launch.
        (pkgdir / SOURCE_WHEEL).write_bytes(raw)
        if dst.exists():
            old_dir = target / ('.dnd-backup-' + os.urandom(8).hex())
            dst.rename(old_dir)
        try:
            pkgdir.rename(dst)
        except BaseException:
            if old_dir is not None and old_dir.exists():
                old_dir.rename(dst)
                old_dir = None
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # Keep the previous package in its private backup directory. A later
        # import or native Tk initialization may still fail; retaining the
        # original allows recovery without ever executing the unverified copy.


def ensure_drag_drop(log=sys.stdout, target: Path = TARGET, downloader=None,
                     sha256: str = WHEEL_SHA256, force: bool = False) -> bool:
    if not force and available(target, sha256):
        print('[GUI] Explorer drag/drop package available.', file=log, flush=True)
        return True
    print('[GUI] Installing Explorer drag/drop from a verified wheel (without pip)...', file=log, flush=True)
    try:
        if downloader is not None:
            raw = downloader()
        elif LOCAL_WHEEL.is_file():
            print('[GUI] Using manually provided wheel from application directory.', file=log, flush=True)
            if LOCAL_WHEEL.stat().st_size > MAX_SIZE:
                raise ValueError('D&D wheel is unexpectedly large')
            raw = LOCAL_WHEEL.read_bytes()
        else:
            raw = fetch_wheel()
        install_wheel(raw, target, sha256)
        if available(target, sha256):
            print('[GUI] Explorer drag/drop installed and imported.', file=log, flush=True)
            return True
        print('[GUI] Installed package cannot be imported. Check your Python/Tk installation.', file=log, flush=True)
    except Exception as exc:
        print(f'[GUI] Explorer drag/drop installation failed: {type(exc).__name__}: {exc}', file=log, flush=True)
        print('[GUI] File picker remains available. Internet access and write permission may be needed.', file=log, flush=True)
    return False


def test_native(log=sys.stdout) -> bool:
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        root.withdraw()
        root.destroy()
        print('[GUI] Windows TkDND native library loaded successfully.', file=log, flush=True)
        return True
    except Exception as exc:
        print(f'[GUI] TkDND native initialization failed: {type(exc).__name__}: {exc}', file=log, flush=True)
        return False


def main() -> int:
    ok = ensure_drag_drop(force="--repair" in sys.argv)
    if not ok:
        return 1
    if '--install-and-check' in sys.argv:
        return 0 if test_native() else 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
