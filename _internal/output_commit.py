"""Commit transcript outputs with rollback if any output replacement fails.

This ensures an existing TXT is not left replaced if publishing its companion
CSV fails. New output paths are removed during rollback, old paths restored.
"""
from __future__ import annotations
import os
import shutil
import uuid
from pathlib import Path
from collections.abc import Mapping


def publish_outputs(staged: Mapping[Path, Path], *, overwrite: bool = False) -> None:
    pending = {Path(dest):Path(src) for dest,src in staged.items()}
    for dst, src in pending.items():
        if not src.is_file():
            raise FileNotFoundError(f'出力準備ファイルがありません: {src}')
        if dst.exists() and not overwrite:
            raise FileExistsError(f'既存出力を保護しました: {dst}')
    backups: dict[Path,Path] = {}
    replaced: list[Path] = []
    preserve_backups = False
    try:
        # Backup all existing files before modifying any destination.
        for dst in pending:
            if dst.is_file():
                backup = dst.with_name(f'.{dst.name}.{uuid.uuid4().hex}.backup')
                try:shutil.copy2(dst, backup)
                except Exception:
                    backup.unlink(missing_ok=True)
                    raise
                backups[dst]=backup
        for dst,src in pending.items():
            # Race protection when no confirmation was given.
            if dst.exists() and not overwrite:
                raise FileExistsError(f'書き込み直前に既存出力を検出しました: {dst}')
            os.replace(src,dst)
            replaced.append(dst)
    except Exception:
        rollback_errors=[]
        for dst in reversed(replaced):
            try:
                if dst in backups:
                    os.replace(backups[dst], dst)
                else:
                    dst.unlink(missing_ok=True)
            except OSError as exc:
                rollback_errors.append(f'{dst}: {exc}')
        if rollback_errors:
            # These copies may now be the only remaining originals. Never
            # delete them when the filesystem also prevented restoration.
            preserve_backups = True
            recovery = [str(path) for path in backups.values() if path.exists()]
            message = '出力保存中に失敗し、復旧も一部失敗しました。詳細: '+' / '.join(rollback_errors)
            if recovery:
                message += '\n復旧用の元ファイルを保持しました: '+' / '.join(recovery)
            raise RuntimeError(message)
        raise
    finally:
        if not preserve_backups:
            for backup in backups.values():
                backup.unlink(missing_ok=True)


def publish_transcription(stage_txt: Path, *, txt: Path | None, csv_path: Path | None,
                          exporter, source_name: str, overwrite: bool = False
                          ) -> tuple[str, list[Path], str | None]:
    """Publish completed ASR as soon as possible, then attempt CSV separately.

    TXT survives CSV failures. A CSV-only job that cannot create a CSV preserves
    the completed recognition as an explicitly named *recovery* TXT, without
    touching any existing TXT. `exporter` is the ordinary 9-column CSV writer.
    """
    stage_txt = Path(stage_txt)
    txt = Path(txt) if txt is not None else None
    csv_path = Path(csv_path) if csv_path is not None else None
    if txt is None and csv_path is None:
        raise ValueError('保存する出力形式が指定されていません。')
    saved: list[Path] = []
    if txt is not None:
        publish_outputs({txt: stage_txt}, overwrite=overwrite)
        saved.append(txt)
    if csv_path is None:
        return '完了', saved, None

    csv_stage = csv_path.with_name(f'.{csv_path.stem}.{uuid.uuid4().hex[:12]}.processing.csv')
    try:
        exporter(txt if txt is not None else stage_txt, csv_stage, source_name=source_name)
        publish_outputs({csv_path: csv_stage}, overwrite=overwrite)
        saved.append(csv_path)
        return '完了', saved, None
    except Exception as exc:
        # CSV writers may raise csv.Error, RuntimeError, or a decoder error.
        # Once TXT is committed, none of them may cause its deletion.
        if txt is not None:
            return '一部完了', saved, f'CSV生成または保存に失敗しました（TXTは保存済み）: {exc}'
        # Do not overwrite an unrelated TXT just because CSV output was requested.
        recovery = csv_path.with_name(csv_path.stem + '_CSV変換失敗_復旧.txt')
        for suffix in range(2, 10000):
            if not recovery.exists():
                break
            recovery = csv_path.with_name(csv_path.stem + f'_CSV変換失敗_復旧_{suffix}.txt')
        else:
            raise RuntimeError('CSV変換失敗時の復旧TXTに使用できる名前がありません。') from exc
        publish_outputs({recovery: stage_txt}, overwrite=False)
        saved.append(recovery)
        return '一部完了', saved, f'CSV生成または保存に失敗しました（復旧用TXTを保存）: {exc}'
    finally:
        csv_stage.unlink(missing_ok=True)
