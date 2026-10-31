"""Finish footage captured by Windows Game Bar/Snipping Tool or other recorders.

Never screen-grabs or re-encodes video. If separate audio is specified, a
lossless video stream copy and AAC audio remux is performed, with an optional
explicit sync offset. Originals are never modified.
"""
from __future__ import annotations

from process_control import check_cancel, run_cancellable

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

VIDEO_SUFFIXES = frozenset({'.mp4','.mkv','.mov','.avi','.webm'})
AUDIO_SUFFIXES = frozenset({'.wav','.m4a','.mp3','.flac','.aac','.ogg','.wma'})


def _hidden_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def video_duration(video:Path, ffmpeg:str, *, cancel=None, registry=None)->float|None:
    """Read MP4 duration from an ffmpeg header; no ffprobe dependency."""
    p=run_cancellable([str(ffmpeg),'-hide_banner','-i',str(video)],capture_output=True,
        text=True,encoding='utf-8',errors='replace',timeout=25,cancel=cancel,registry=registry,creationflags=_hidden_flags())
    data=p.stderr+'\n'+p.stdout
    m=re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',data)
    if not m:return None
    return int(m.group(1))*3600+int(m.group(2))*60+float(m.group(3))


@dataclass(frozen=True)
class ExternalOptions:
    video:Path
    audio:Path|None=None
    offset:float=0.0  # + = delay added track; - = trim beginning of added track
    mode:str='replace'  # replace, mix
    destination:Path|None=None
    def validate(self):
        if not self.video.is_file() or self.video.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError('Windows標準録画の動画を選択してください（MP4など）。')
        if self.audio is not None:
            if not self.audio.is_file() or self.audio.suffix.lower() not in AUDIO_SUFFIXES:
                raise ValueError('追加する音声ファイル（WAV/M4A/MP3など）を選択してください。')
            if self.mode not in ('replace','mix'):
                raise ValueError('音声の合成方法が不正です。')
            if not -3600 <= self.offset <= 3600:
                raise ValueError('音声ずれの指定は±3600秒以内にしてください。')
            if self.video.resolve()==self.audio.resolve():
                raise ValueError('動画と音声が同じファイルです。')
            if self.destination is None or self.destination.suffix.lower()!='.mp4':
                raise ValueError('合成したMP4の保存先を指定してください。')
            if self.destination.resolve() in (self.video.resolve(),self.audio.resolve()):
                raise ValueError('原本と同じ名前での上書きはできません。')


def mux_command(ffmpeg:str, opts:ExternalOptions, staging:Path, *, duration:float|None, video_has_audio:bool)->list[str]:
    """Audio sync alignment: +N adds initial silence, -N trims N seconds."""
    opts.validate()
    assert opts.audio is not None
    audio_chain='[1:a]'
    filters=[]
    if opts.offset < 0:
        filters.append(f'atrim=start={-opts.offset:.3f}')
        filters.append('asetpts=PTS-STARTPTS')
    elif opts.offset > 0:
        filters.append(f'adelay={int(round(opts.offset*1000))}:all=1')
    # Never extend output past video duration; video is always preserved.
    if duration and duration > 0:
        filters.append(f'atrim=duration={duration:.3f}')
    filters.append('aresample=async=1:first_pts=0')
    af='[1:a]'+','.join(filters)+'[external]'
    if opts.mode=='mix' and video_has_audio:
        af+=';[0:a:0][external]amix=inputs=2:duration=longest:dropout_transition=0[mixed]'
        audio_map='[mixed]'
    else:
        audio_map='[external]'
    return [str(ffmpeg),'-hide_banner','-loglevel','error','-nostdin','-n',
            '-i',str(opts.video),'-i',str(opts.audio),'-filter_complex',af,
            '-map','0:v:0','-map',audio_map,'-c:v','copy','-c:a','aac','-b:a','192k',
            '-movflags','+faststart',str(staging)]


def finalize_external(opts:ExternalOptions, ffmpeg:str, *, has_audio:Callable,
        stop=None,log:Callable[[str],None]=lambda _text:None,registry=None)->Path:
    """Return original when no external audio; else atomically publish a muxed copy."""
    check_cancel(stop)
    opts.validate()
    if opts.audio is None:
        log('[標準録画] 動画に別音声は付けず、原本をそのまま利用します。')
        return opts.video
    assert opts.destination is not None
    opts.destination.parent.mkdir(parents=True,exist_ok=True)
    if opts.destination.exists():
        raise FileExistsError('保存先MP4が既に存在します。別名を指定してください: '+str(opts.destination))
    duration=video_duration(opts.video,ffmpeg,cancel=stop,registry=registry)
    embedded=has_audio(opts.video,ffmpeg) if opts.mode=='mix' else False
    if opts.mode=='mix' and not embedded:
        log('[標準録画] 元動画に音声がないため、追加音声だけを使用します。')
    if duration is None:
        log('[標準録画] 動画の収録時間を取得できず、音声の終端調整ができません。')
    with tempfile.TemporaryDirectory(prefix='.import_work_',dir=str(opts.destination.parent)) as td:
        staged=Path(td)/'ready.mp4'
        cmd=mux_command(ffmpeg,opts,staged,duration=duration,video_has_audio=embedded)
        log(f'[標準録画] 動画を再圧縮せず音声を同期: {opts.offset:+.3f}秒 / {opts.mode}')
        with subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                encoding='utf-8',errors='replace',creationflags=_hidden_flags()) as proc:
            while True:
                if stop is not None and stop.is_set():
                    proc.kill();proc.communicate()
                    raise InterruptedError('同期・合成を中止しました。動画・音声の原本はそのままです。')
                try:
                    stdout,stderr=proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if proc.returncode or not staged.exists() or staged.stat().st_size < 100:
                raise RuntimeError('音声合成に失敗: '+stderr[-1800:])
        check_cancel(stop)
        # The destination may have appeared while ffmpeg was running. Windows
        # rename fails if it exists; POSIX needs a hard link for no-clobber.
        # Both paths publish atomically without overwriting someone else's file.
        if os.name == 'nt':
            os.rename(staged,opts.destination)
        else:
            os.link(staged,opts.destination)
            staged.unlink()
    log(f'[標準録画] 合成済みMP4: {opts.destination}')
    return opts.destination

