"""Collision policy helpers. New requested filenames are never treated as old names."""
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def rename_destination(destination:Path, output_mode:str, now:datetime|None=None) -> Path:
    """Find a distinct stem for all requested formats without overwriting any file."""
    if output_mode not in ('txt','csv','both'):
        raise ValueError('不明な出力形式です')
    destination=Path(destination)
    stamp=(now or datetime.now()).strftime('%Y%m%d_%H%M%S')
    for index in range(1,10000):
        suffix = f'_リネーム保存_{stamp}' + (f'_{index}' if index>1 else '')
        trial=destination.with_name(destination.stem+suffix+'.txt')
        targets=([trial] if output_mode=='txt' else
                 [trial.with_suffix('.csv')] if output_mode=='csv' else
                 [trial,trial.with_suffix('.csv')])
        if all(not item.exists() for item in targets):
            return trial
    raise FileExistsError('安全なリネーム先を見つけられませんでした')
