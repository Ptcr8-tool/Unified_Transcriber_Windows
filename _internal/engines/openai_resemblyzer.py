from __future__ import annotations
try:
    from .caption_segments import split_timed_items
    from .media_runtime import checked_ffmpeg
    from .worker_io import extract_pcm_audio, write_transcript_atomic
    from .asr_hints import options_for as asr_hint_options
except ImportError:  # standalone Python invocation from the engines directory
    from caption_segments import split_timed_items
    from media_runtime import checked_ffmpeg
    from worker_io import extract_pcm_audio, write_transcript_atomic
    from asr_hints import options_for as asr_hint_options

import argparse
import errno
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from setup_progress import emit_setup_progress, progress_tqdm


SCRIPT_DIR = Path(__file__).resolve().parent
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".wma"}


@dataclass
class TranscriptEntry:
    start: float
    end: float
    speaker: str
    text: str


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def extract_wav(media_path: Path, wav_path: Path) -> None:
    """確認済みのFFmpegで、入力から16kHz/monoのWAVを作る。"""
    ffmpeg = checked_ffmpeg()
    extract_pcm_audio(ffmpeg, media_path, wav_path)


def embed_transcript_segments(segments, audio, sample_rate: int, device: str):
    """Whisperの発話区間ごとにResemblyzerの声の特徴ベクトルを作る。"""
    import librosa
    import numpy as np
    from resemblyzer import VoiceEncoder

    encoder = VoiceEncoder(device=device, verbose=False)
    embeddings: list[np.ndarray | None] = []
    usable_indices: list[int] = []

    for index, segment in enumerate(segments):
        start = max(0, int(float(segment["start"]) * sample_rate))
        end = min(len(audio), int(float(segment["end"]) * sample_rate))
        if end <= start or (end - start) / sample_rate < 1.4:
            embeddings.append(None)
            continue

        voice = np.asarray(audio[start:end], dtype=np.float32)
        # 発話区間の前後の無音を軽く除き、極端に小さい音量を補正する。
        voice, _ = librosa.effects.trim(voice, top_db=30)
        if len(voice) / sample_rate < 1.2:
            embeddings.append(None)
            continue
        rms = float(np.sqrt(np.mean(np.square(voice))))
        if rms < 1e-6:
            embeddings.append(None)
            continue
        voice = np.clip(voice * (0.08 / rms), -1.0, 1.0).astype(np.float32)
        try:
            embeddings.append(encoder.embed_utterance(voice))
            usable_indices.append(index)
        except Exception as exc:
            print(f"話者特徴量を取得できない区間をスキップします ({index}): {exc}", file=sys.stderr)
            embeddings.append(None)

    return embeddings, usable_indices


def cluster_embeddings(embeddings, max_speakers: int, threshold: float, speakers: int | None):
    """Resemblyzer特徴量をコサイン距離でクラスタリングする。"""
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    count = len(embeddings)
    if count == 0:
        return []
    if count == 1:
        return [0]

    vectors = np.asarray(embeddings, dtype=np.float32)
    if speakers is not None:
        cluster_count = max(1, min(speakers, count))
        model = AgglomerativeClustering(n_clusters=cluster_count, metric="cosine", linkage="average")
        return model.fit_predict(vectors).tolist()

    model = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=threshold,
    )
    labels = model.fit_predict(vectors)
    if len(set(labels.tolist())) > max_speakers:
        model = AgglomerativeClustering(
            n_clusters=min(max_speakers, count), metric="cosine", linkage="average"
        )
        labels = model.fit_predict(vectors)
    return labels.tolist()


def format_transcript(segments, embeddings, usable_indices, raw_labels, language: str,
                      caption_max_chars=None, caption_max_seconds=5.0) -> list[str]:
    # クラスタ番号を、音声中に最初に登場した順でSPEAKER_00...に変換。
    label_for_segment: dict[int, int] = {}
    for index, cluster in zip(usable_indices, raw_labels):
        label_for_segment[index] = int(cluster)
    first_seen: dict[int, float] = {}
    for index, cluster in label_for_segment.items():
        first_seen[cluster] = min(float(segments[index]["start"]), first_seen.get(cluster, float(segments[index]["start"])))
    cluster_order = sorted(first_seen, key=lambda cluster: first_seen[cluster])
    speaker_name = {cluster: f"SPEAKER_{position:02d}" for position, cluster in enumerate(cluster_order)}

    entries: list[TranscriptEntry] = []
    valid_segments = [
        (index, float(segments[index]["start"]), float(segments[index]["end"]), speaker_name[cluster])
        for index, cluster in label_for_segment.items()
    ]

    for index, segment in enumerate(segments):
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment["start"])
        end = float(segment["end"])
        speaker = speaker_name.get(label_for_segment.get(index, -1), "UNKNOWN")
        if speaker == "UNKNOWN" and valid_segments:
            midpoint = (start + end) / 2
            nearest = min(
                valid_segments,
                key=lambda item: 0.0 if item[1] <= midpoint <= item[2]
                else min(abs(midpoint - item[1]), abs(midpoint - item[2])),
            )
            distance = 0.0 if nearest[1] <= midpoint <= nearest[2] else min(abs(midpoint - nearest[1]), abs(midpoint - nearest[2]))
            # ごく短い相づちは、近くの特徴量取得済み発話に限り同じ話者として扱う。
            if distance <= 5.0:
                speaker = nearest[3]
        entries.append(TranscriptEntry(start, end, speaker, text))

    if not entries:
        return []

    if caption_max_chars is not None:
        caption_entries = split_timed_items(entries, max_chars=caption_max_chars, max_seconds=caption_max_seconds)
        return [f'[{format_timestamp(item.start)}] {item.speaker}: {item.text.strip()}'
                for item in caption_entries if item.text.strip()]

    # 同じ話者の連続するWhisper区間をまとめる。
    merged: list[TranscriptEntry] = []
    current = entries[0]
    for entry in entries[1:]:
        if entry.speaker == current.speaker and entry.start - current.end <= 1.5:
            if language.lower().startswith("ja"):
                combined = current.text + entry.text
            else:
                combined = current.text.rstrip() + " " + entry.text.lstrip()
            current = TranscriptEntry(current.start, max(current.end, entry.end), current.speaker, combined)
        else:
            merged.append(current)
            current = entry
    merged.append(current)

    lines = []
    for entry in merged:
        text = re.sub(r"\s+([,.;:!?。、「」？！])", r"\1", entry.text).strip()
        if text:
            lines.append(f"[{format_timestamp(entry.start)}] {entry.speaker}: {text}")
    return lines


def fixed_windows(audio, sample_rate: int, seconds: float) -> list[dict]:
    """Fixed acoustic windows (not Whisper utterances), with exact end times."""
    if seconds <= 1:
        raise ValueError('区間長は1秒より長くしてください。')
    step = max(1, int(sample_rate * seconds))
    return [
        {'start': pos / sample_rate, 'end': min(pos + step, len(audio)) / sample_rate}
        for pos in range(0, len(audio), step)
        if min(step, len(audio) - pos) > sample_rate
    ]


def labels_for_transcript_segments(segments, windows, usable_indices, labels):
    """Map acoustic-window labels to ASR segments by maximum temporal overlap."""
    turns = [
        (float(windows[i]['start']), float(windows[i]['end']), int(label))
        for i, label in zip(usable_indices, labels)
    ]
    indices, result = [], []
    for index, segment in enumerate(segments):
        start, end = float(segment['start']), float(segment['end'])
        if end <= start:
            continue
        best = max(turns, key=lambda t: max(0., min(end, t[1])-max(start, t[0])), default=None)
        if best is not None and max(0., min(end, best[1])-max(start, best[0])) > 0:
            indices.append(index)
            result.append(best[2])
    return indices, result



def emit_gui_progress(stage: str, completed: float, total: float, text: str = '', detail: str = '') -> None:
    """Only structured single-line output is consumed by the GUI; CLI ignores it."""
    print('@@TRANSCRIBER@@' + json.dumps({'stage': stage, 'completed': completed,
          'total': total, 'text': text, 'detail': detail}, ensure_ascii=False), flush=True)
    try:
        from .engine_load import pace_if_needed
    except ImportError:
        from engine_load import pace_if_needed
    pace_if_needed(stage)

def create_transcript(media_path: Path, model_size: str, language: str,
                      max_speakers: int, threshold: float, speakers: int | None,
                      device_choice: str = "auto",
                      diarization_unit: str = "utterance", segment_length: float = 20.,
                      caption_max_chars=None, caption_max_seconds=5.0) -> list[str]:
    import librosa
    import torch
    import whisper

    cuda_available = torch.cuda.is_available()
    if device_choice == "cuda" and not cuda_available:
        raise RuntimeError("CUDA/GPUが利用できません。GPU版PyTorchとNVIDIAドライバーを確認してください。")
    if device_choice not in {"auto", "cpu", "cuda"}:
        raise ValueError("device_choice は auto / cpu / cuda のいずれかにしてください。")
    device = "cuda" if device_choice == "cuda" or (device_choice == "auto" and cuda_available) else "cpu"
    print(f"デバイス: {device} / Whisperモデル: {model_size}")
    if device == "cuda":
        vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        print(f"GPU: {torch.cuda.get_device_name(0)} / VRAM: {vram_gb:.1f} GB")
        if model_size.lower().startswith("large") and vram_gb < 10:
            print("警告: largeモデルはVRAM不足になる可能性があります。8GB GPUではmediumを推奨します。", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="speaker_transcriber_") as temp_dir:
        wav_path = Path(temp_dir) / "audio.wav"
        print("音声を抽出中...")
        extract_wav(media_path, wav_path)
        audio, sample_rate = librosa.load(str(wav_path), sr=16000, mono=True)

        print("Whisperで文字起こし中...")
        emit_setup_progress('モデルを準備中', detail='OpenAI Whisper')
        original_tqdm = getattr(whisper, 'tqdm', None)
        bar = progress_tqdm('モデル取得 · OpenAI Whisper')
        try:
            if original_tqdm is not None and bar is not None:
                whisper.tqdm = bar
            asr_model = whisper.load_model(model_size, device=device)
        finally:
            if original_tqdm is not None:
                whisper.tqdm = original_tqdm
        emit_setup_progress('音声認識を準備中', detail='モデルの読み込み完了')
        language_arg = None if language.lower() == "auto" else language
        result = asr_model.transcribe(
            audio,
            language=language_arg,
            fp16=(device == "cuda"),
            verbose=False,
            condition_on_previous_text=True,
            **asr_hint_options('openai', model=asr_model, whisper_module=whisper, language=language_arg),
        )
        segments_for_preview = result.get('segments', [])
        duration_for_preview = max((float(x.get('end', 0)) for x in segments_for_preview), default=0.0)
        for entry in segments_for_preview:
            emit_gui_progress('asr',float(entry.get('end',0)),duration_for_preview,
                              str(entry.get('text','')).strip(),
                              f'{float(entry.get("end",0)):.1f}秒まで認識')
        # 話者特徴量抽出の前にWhisperモデルを解放し、GPUメモリのピークを下げる。
        del asr_model
        if device == "cuda":
            torch.cuda.empty_cache()
        segments = result.get("segments", [])
        if not segments:
            raise RuntimeError("音声から文字起こし結果を取得できませんでした。")

        if diarization_unit == 'fixed':
            feature_segments = fixed_windows(audio, sample_rate, segment_length)
            print(f'Resemblyzer固定長話者分離: {segment_length:g}秒、{len(feature_segments)}区間')
        elif diarization_unit == 'utterance':
            feature_segments = segments
            print('Resemblyzer発話単位話者分離: Whisper発話区間')
        else:
            raise ValueError('話者分離単位はutterance / fixedのみです。')
        embeddings, usable_indices = embed_transcript_segments(feature_segments, audio, sample_rate, device)
        if not usable_indices:
            print('話者特徴量が取得できず、話者ラベルはUNKNOWNになります。')
            labels = []
        else:
            vectors = [embeddings[index] for index in usable_indices]
            labels = cluster_embeddings(vectors, max_speakers, threshold, speakers)
            print(f'推定した話者数: {len(set(labels))}')
        detected_language = result.get('language') or language
        if diarization_unit == 'fixed':
            mapped_indices, mapped_labels = labels_for_transcript_segments(
                segments, feature_segments, usable_indices, labels)
            return format_transcript(segments, embeddings, mapped_indices, mapped_labels, detected_language,
                                     caption_max_chars, caption_max_seconds)
        return format_transcript(segments, embeddings, usable_indices, labels, detected_language,
                                 caption_max_chars, caption_max_seconds)


def transcribe(media_path: Path, output_path: Path, model_size: str, language: str,
               max_speakers: int, threshold: float, speakers: int | None,
               overwrite: bool = False, device_choice: str = "auto",
               diarization_unit: str = "utterance", segment_length: float = 20.,
               caption_max_chars=None, caption_max_seconds=5.0) -> None:
    if not media_path.is_file():
        raise FileNotFoundError(f"入力ファイルが見つかりません: {media_path}")
    if media_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"未対応の形式です: {media_path.suffix} / 対応形式: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"出力ファイルが既にあります（上書きしません）: {output_path}。上書きする場合は --overwrite を指定してください。")
    if max_speakers < 1 or (speakers is not None and speakers < 1):
        raise ValueError("話者数は1以上にしてください。")
    if threshold <= 0:
        raise ValueError("--threshold は0より大きい値にしてください。")

    lines = create_transcript(media_path, model_size, language, max_speakers, threshold, speakers, device_choice, diarization_unit, segment_length,
                              caption_max_chars, caption_max_seconds)
    if not lines:
        raise RuntimeError("出力できる文字起こし行がありませんでした。")

    write_transcript_atomic(output_path, "\n".join(lines) + "\n", overwrite=overwrite)
    print(f"文字起こし結果を保存しました: {output_path}")


def wait_until_stable(path: Path, checks: int = 3, interval: float = 3.0) -> bool:
    """コピー中のファイルを処理しないよう、サイズが連続して変わらないことを確認する。"""
    last_size = -1
    stable_count = 0
    for _ in range(max(1, checks * 10)):
        try:
            size = path.stat().st_size
        except (FileNotFoundError, PermissionError):
            size = -1
        if size > 0 and size == last_size:
            stable_count += 1
            if stable_count >= checks:
                return True
        else:
            stable_count = 0
        last_size = size
        time.sleep(interval)
    return False


def unique_pair(processed_dir: Path, media_path: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    index = 0
    while True:
        suffix = "" if index == 0 else f"_{stamp}_{index}"
        stem = media_path.stem + suffix
        destination_media = processed_dir / f"{stem}{media_path.suffix.lower()}"
        destination_txt = processed_dir / f"{stem}.txt"
        if not destination_media.exists() and not destination_txt.exists():
            return destination_media, destination_txt
        index += 1


def move_file_no_overwrite(source: Path, destination: Path) -> None:
    """Move a file without overwriting a concurrently-created destination.

    When volumes differ (or hard links are unsupported), copy to a unique file
    in the destination directory first. Publish it with an exclusive link or a
    Windows rename, and only then remove the source. A failed copy or publish
    leaves the original intact. Never fall back to an overwrite-capable rename.
    """
    source, destination = Path(source), Path(destination)
    try:
        if os.name == 'nt':
            os.rename(source, destination)  # Windows rejects an existing target.
            return
        os.link(source, destination)
    except OSError as exc:
        if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST:
            raise
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES,
                             errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
    else:
        source.unlink()
        return

    fd, temporary = tempfile.mkstemp(prefix='.transcriber-move-', suffix='.tmp',
                                      dir=destination.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, staged)
        if os.name == 'nt':
            os.rename(staged, destination)
        else:
            os.link(staged, destination)
        # At this point a complete destination is published. Even if removing
        # the original fails, both copies remain rather than losing data.
        source.unlink()
    finally:
        staged.unlink(missing_ok=True)


def write_watch_error_log(failed_media: Path, text: str) -> Path:
    """Create a new log without replacing an earlier or concurrent result."""
    for index in range(10000):
        suffix = '.error.log' if index == 0 else f'.error.{index}.log'
        destination = failed_media.with_name(failed_media.name + suffix)
        try:
            with destination.open('x', encoding='utf-8') as stream:
                stream.write(text)
        except FileExistsError:
            continue
        return destination
    raise FileExistsError('保存できるエラーログ名が見つかりません。')


def watch_folder(args: argparse.Namespace) -> None:
    input_dir = args.input_dir.resolve()
    processed_dir = args.processed_dir.resolve()
    failed_dir = args.failed_dir.resolve()
    for directory in (input_dir, processed_dir, failed_dir):
        directory.mkdir(parents=True, exist_ok=True)

    print(f"監視中: {input_dir}")
    print(f"成功: {processed_dir} / 失敗: {failed_dir}")
    print("終了するには Ctrl+C を押してください。")
    while True:
        candidates = sorted(
            (p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda p: p.name.lower(),
        )
        if not candidates:
            time.sleep(args.poll_seconds)
            continue

        for media_path in candidates:
            if not media_path.exists():
                continue
            print(f"\nファイルを検出: {media_path.name}（書き込み完了を待機中）")
            if not wait_until_stable(media_path):
                print(f"ファイルサイズが安定しないため次回に再確認します: {media_path.name}")
                continue

            destination_media, destination_txt = unique_pair(processed_dir, media_path)
            temp_output = None
            transcription_finished = False
            media_moved = False
            try:
                # Never remove or overwrite a previous interrupted result.
                fd, temp_name = tempfile.mkstemp(prefix=".transcribing-",
                                                 suffix=".txt", dir=input_dir)
                os.close(fd)
                temp_output = Path(temp_name)
                transcribe(media_path, temp_output, args.model, args.language,
                           args.max_speakers, args.threshold, args.speakers,
                           overwrite=True, device_choice=args.device,
                           diarization_unit=args.diarization_unit, segment_length=args.segment_length,
                   caption_max_chars=args.caption_max_chars, caption_max_seconds=args.caption_max_seconds)
                transcription_finished = True
                move_file_no_overwrite(media_path, destination_media)
                media_moved = True
                move_file_no_overwrite(temp_output, destination_txt)
                print(f"処理済みファイル: {destination_media}")
                print(f"文字起こし: {destination_txt}")
            except KeyboardInterrupt:
                if transcription_finished and temp_output and temp_output.exists():
                    print(f"完了済みの文字起こしを保持しました: {temp_output}", file=sys.stderr)
                elif temp_output and temp_output.exists():
                    temp_output.unlink()
                raise
            except Exception as exc:
                print(f"処理失敗: {media_path.name}: {exc}", file=sys.stderr)
                recovery = []
                if media_moved:
                    recovery.append(f"移動済みの元ファイル: {destination_media}")
                if transcription_finished and temp_output and temp_output.exists():
                    recovery.append(f"保持した文字起こし: {temp_output}")
                for line in recovery:
                    print(line, file=sys.stderr)
                failed_dir.mkdir(parents=True, exist_ok=True)
                failed_media = failed_dir / media_path.name
                if failed_media.exists():
                    failed_media = failed_dir / f"{media_path.stem}_{datetime.now():%Y%m%d_%H%M%S}{media_path.suffix.lower()}"
                try:
                    if media_path.exists():
                        move_file_no_overwrite(media_path, failed_media)
                        print(f"失敗ファイルを移動しました: {failed_media}")
                except OSError as move_error:
                    print(f"失敗ファイルの移動にも失敗しました: {move_error}", file=sys.stderr)
                try:
                    error_log = write_watch_error_log(failed_media,
                        f"処理日時: {datetime.now():%Y-%m-%d %H:%M:%S}\nファイル: {media_path}\nエラー: {exc}\n"
                        + "".join(f"{line}\n" for line in recovery))
                    print(f"エラーログ: {error_log}", file=sys.stderr)
                except OSError as move_error:
                    print(f"エラーログを保存できませんでした: {move_error}", file=sys.stderr)
                finally:
                    if not transcription_finished and temp_output and temp_output.exists():
                        temp_output.unlink()
        time.sleep(args.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Whisper + Resemblyzerで話者分離し、タイムスタンプ付きTXTを出力します（Hugging Face不要）。"
    )
    parser.add_argument("media", nargs="?", help="入力する音声・動画ファイル")
    parser.add_argument("--output", type=Path, help="出力する .txt のパス（省略時は入力と同じ場所）")
    parser.add_argument("--model", default="turbo", help="Whisperモデル: tiny / base / small / medium / large / turbo（既定: turbo）")
    parser.add_argument("--language", default="ja", help="言語コード（既定: ja、言語自動判定は auto）")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto", help="実行デバイス（既定: auto）")
    parser.add_argument("--max-speakers", type=int, default=6, help="自動推定する話者数の上限（既定: 6）")
    parser.add_argument("--speakers", type=int, help="話者数が分かっている場合に固定（例: --speakers 2）")
    parser.add_argument("--threshold", type=float, default=0.25, help="話者クラスタリングの距離しきい値（既定: 0.25）")
    parser.add_argument('--diarization-unit', choices=('utterance', 'fixed'), default='utterance',
                        help='話者特徴量の抽出単位: Whisper発話区間 / 固定秒数')
    parser.add_argument('--segment-length', type=float, default=20., help='固定長話者分離の秒数')
    parser.add_argument("--caption-max-chars", type=int, default=None)
    parser.add_argument("--caption-max-seconds", type=float, default=5.0)
    parser.add_argument("--overwrite", action="store_true", help="既存の出力TXTを上書きする")
    parser.add_argument("--watch", action="store_true", help="inputフォルダーを監視して自動処理する")
    parser.add_argument("--input-dir", type=Path, default=SCRIPT_DIR / "input", help="監視フォルダー")
    parser.add_argument("--processed-dir", type=Path, default=SCRIPT_DIR / "processed", help="成功時の移動先")
    parser.add_argument("--failed-dir", type=Path, default=SCRIPT_DIR / "failed", help="失敗時の移動先")
    parser.add_argument("--poll-seconds", type=int, default=10, help="監視間隔（秒、既定: 10）")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.watch:
        try:
            watch_folder(args)
        except KeyboardInterrupt:
            print("\n監視を終了しました。")
        return 0
    if not args.media:
        parser.error("入力ファイルを指定するか、--watchを指定してください。")

    media_path = Path(args.media).expanduser().resolve()
    output_path = args.output.expanduser().resolve() if args.output else media_path.with_suffix(".txt")
    try:
        transcribe(media_path, output_path, args.model, args.language,
                   args.max_speakers, args.threshold, args.speakers,
                   args.overwrite, device_choice=args.device,
                   diarization_unit=args.diarization_unit, segment_length=args.segment_length,
                   caption_max_chars=args.caption_max_chars, caption_max_seconds=args.caption_max_seconds)
        return 0
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
