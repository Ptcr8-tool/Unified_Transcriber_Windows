"""Archive merged originals only after output publication; never overwrite."""
from dataclasses import dataclass
import os
from pathlib import Path


def source_stamps(sources):
    result={}
    for source in sources:
        path=Path(source).absolute();info=path.stat()
        result[path]=(info.st_size,info.st_mtime_ns,info.st_ino)
    return result


@dataclass(frozen=True)
class ArchiveResult:
    moved:tuple
    errors:tuple
    def message(self):
        text=f'元ファイル{len(self.moved)}件を各フォルダーの _old へ移動しました。'
        if self.errors:text+=f' {len(self.errors)}件は元の場所に残しました（ログ参照）。'
        return text


def archive_merge_sources(sources,output,*,stamps=None,cancel=None,log=lambda _:None):
    output=Path(output).resolve()
    if not output.is_file() or not output.stat().st_size:
        raise ValueError('結合結果を確認できないため元ファイルは移動しません。')
    paths=list(dict.fromkeys(Path(p).absolute() for p in sources))
    if any(p.resolve()==output for p in paths):raise ValueError('結合結果は移動できません。')
    moved,errors=[],[]
    for path in paths:
        if cancel is not None and cancel.is_set():break
        reserved=None
        try:
            if path.is_symlink():raise ValueError('リンクの元ファイルは移動しません。')
            info=path.stat()
            if stamps is not None and stamps.get(path)!=(info.st_size,info.st_mtime_ns,info.st_ino):
                raise ValueError('結合開始後に変更されたため元の場所に残します。')
            folder=path.parent/'_old'
            if folder.is_symlink():raise ValueError('_old がリンクのため移動しません。')
            folder.mkdir(exist_ok=True);number=0
            while True:
                candidate=folder/(path.name if not number else f'{path.stem} ({number}){path.suffix}')
                try:
                    with candidate.open('xb'):pass
                    reserved=candidate;break
                except FileExistsError:number+=1
            if cancel is not None and cancel.is_set():break
            os.replace(path,reserved);moved.append((path,reserved))
            log(f'[元ファイル移動] {path} → {reserved}');reserved=None
        except (OSError,ValueError) as exc:
            errors.append((path,str(exc)));log(f'[元ファイル移動の注意] {path}: {exc}')
        finally:
            if reserved is not None:
                try:reserved.unlink(missing_ok=True)
                except OSError as exc:log(f'[注意] 移動予約ファイルの削除: {reserved}: {exc}')
    return ArchiveResult(tuple(moved),tuple(errors))
