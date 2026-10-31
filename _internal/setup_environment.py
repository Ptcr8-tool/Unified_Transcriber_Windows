"""Migrate an application's optional venv without discarding a working copy."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from portable_runtime import runtime_environment
from setup_process import run_logged


def _ordinary(path: Path, *, directory: bool = True):
    value = path.lstat()
    if ((not stat.S_ISDIR(value.st_mode) if directory else not stat.S_ISREG(value.st_mode))
            or stat.S_ISLNK(value.st_mode)
            or getattr(value, 'st_file_attributes', 0) & 0x400):
        raise RuntimeError('追加環境の保存先にリンクや特殊ファイルは使用できません。')
    return value


def _ancestors(path: Path):
    for parent in (path, *path.parents):
        _ordinary(parent)


def _info(python: Path, target: Path):
    code = ('import json,sys; print(json.dumps({"version":list(sys.version_info[:2]),'
            '"base":sys.base_prefix}))')
    try:
        result = subprocess.run([str(python), '-I', '-c', code], capture_output=True,
            text=True, encoding='utf-8', errors='strict', timeout=30, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            env=runtime_environment(target))
        if result.returncode != 0 or len(result.stdout) > 32768:
            return None
        info = json.loads(result.stdout)
        if (not isinstance(info, dict) or set(info) != {'version', 'base'}
                or not isinstance(info['version'], list) or len(info['version']) != 2
                or any(type(value) is not int for value in info['version'])
                or not isinstance(info['base'], str) or not Path(info['base']).is_absolute()):
            return None
        return info
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None


def _matches(python: Path, target: Path, expected: dict) -> bool:
    actual = _info(python, target)
    return (actual is not None and actual['version'] == expected['version']
            and Path(actual['base']).resolve() == Path(expected['base']).resolve())


def _python(folder: Path) -> Path:
    return folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def _move(source: Path, destination: Path) -> None:
    identity = _ordinary(source)
    started = time.monotonic()
    while True:
        _ancestors(source.parent)
        _ancestors(destination.parent)
        current = _ordinary(source)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError('追加環境の保存先が変更されました。')
        if destination.exists() or destination.is_symlink():
            raise RuntimeError('追加環境の移動先に予期しないフォルダーがあります。')
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or time.monotonic() - started >= 12:
                raise
            time.sleep(.1)


@contextmanager
def _lock(parent: Path, name: str):
    path = parent / ('.' + name + '-setup.lock')
    if path.exists() or path.is_symlink():
        _ordinary(path, directory=False)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        before, opened = _ordinary(path, directory=False), os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError('追加環境のロックファイルが変更されました。')
        if opened.st_size == 0:
            os.write(descriptor, b'\0')
        if os.name == 'nt':
            import msvcrt
            def acquire():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            def release():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        started = time.monotonic()
        while True:
            try:
                acquire()
                locked = True
                break
            except OSError:
                if time.monotonic() - started > 120:
                    raise TimeoutError('同じ追加環境のセットアップが実行中です。完了後に再実行してください。')
                time.sleep(.1)
        yield
    finally:
        if locked:
            release()
        os.close(descriptor)


@contextmanager
def environment_build(folder: Path, target: Path, host: str, *, supported: tuple[int, int]):
    """Reuse a matching venv or build a replacement beside the old environment.

    Consumers must finish their dependency/import checks before yielding back.
    Settings, shared model caches, recordings and transcripts are outside the
    replaced venv. Failed builds leave the original venv at its original path.
    """
    folder, target = Path(folder).absolute(), Path(target).absolute()
    parent = target / '_internal' / 'environments'
    if folder.parent != parent or folder.name.startswith('.'):
        raise ValueError('追加環境の保存先がアプリの環境フォルダーではありません。')
    _ancestors(target)
    for path in (target / '_internal', parent):
        path.mkdir(exist_ok=True)
        _ancestors(path)
    with _lock(parent, folder.name):
        if folder.exists() or folder.is_symlink():
            _ordinary(folder)
        expected = _info(Path(host), target)
        if expected is None or expected['version'] != list(supported):
            raise RuntimeError(f'追加環境用のPython {supported[0]}.{supported[1]}を確認できません。'
                               'セットアップexeの「修復」を実行してください。')
        current = _python(folder)
        if current.is_file() and _matches(current, target, expected):
            yield current
            return
        print(f'追加環境をPython {supported[0]}.{supported[1]}で構築します。'
              '既存の環境は新環境の確認が終わるまで保持します。', flush=True)
        stage = Path(tempfile.mkdtemp(prefix='.' + folder.name + '-stage-', dir=parent))
        backup = parent / ('.' + folder.name + '-backup-' + uuid.uuid4().hex)
        moved = False
        try:
            run_logged([host, '-m', 'venv', str(stage)], target)
            python = _python(stage)
            if not _matches(python, target, expected):
                raise RuntimeError('作成した追加環境のPythonを確認できません。')
            yield python
            if folder.exists():
                _move(folder, backup)
                moved = True
            try:
                _move(stage, folder)
                if not _matches(_python(folder), target, expected):
                    raise RuntimeError('移動後の追加環境を確認できません。')
            except BaseException as error:
                if folder.exists():
                    _move(folder, stage)
                if moved:
                    try:
                        _move(backup, folder)
                        moved = False
                    except BaseException as rollback:
                        raise RuntimeError('追加環境の復元に失敗しました。旧環境は削除せず保持しました: '
                                           + str(backup)) from rollback
                raise error
            if moved:
                try:
                    shutil.rmtree(backup)
                    moved = False
                except OSError:
                    print('新環境の構築は完了しました。使用中の旧環境は保持しました: ' + str(backup), flush=True)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
