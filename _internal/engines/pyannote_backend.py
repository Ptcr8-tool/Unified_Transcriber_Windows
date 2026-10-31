from __future__ import annotations
try:
    from .caption_segments import split_timed_items
    from .media_runtime import checked_ffmpeg
    from .cuda_runtime import initialize_cuda_libraries
    from .worker_io import extract_pcm_audio, read_pcm_for_whisper, write_transcript_atomic
    from .asr_hints import options_for as asr_hint_options
except ImportError:  # standalone Python invocation from the engines directory
    from caption_segments import split_timed_items
    from media_runtime import checked_ffmpeg
    from cuda_runtime import initialize_cuda_libraries
    from worker_io import extract_pcm_audio, read_pcm_for_whisper, write_transcript_atomic
    from asr_hints import options_for as asr_hint_options

import argparse
import gc
import os
import json
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from model_defaults import QWEN_MODEL as DEFAULT_QWEN_MODEL_ID
from model_defaults import QWEN_ALIGNER as DEFAULT_QWEN_ALIGNER_ID
from model_defaults import faster_model_args
from setup_progress import emit_setup_progress, hf_snapshot_progress


DIARIZATION_MODEL_ID = "pyannote/speaker-diarization-3.1"


@dataclass
class SpeakerTurn:
    start: float
    end: float
    speaker: str


@dataclass
class TranscriptItem:
    start: float
    end: float
    speaker: str
    text: str


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def check_ffmpeg() -> str:
    return checked_ffmpeg()


def extract_audio(ffmpeg: str, source: Path, destination: Path) -> None:
    extract_pcm_audio(ffmpeg, source, destination)


def load_diarization_pipeline(token: str, device: str):
    try:
        import torch
        from pyannote.audio import Pipeline
        from pyannote.audio.core.task import Problem, Resolution, Specifications
    except ImportError as exc:
        raise RuntimeError(
            "必要なライブラリがありません。GUIの「一括セットアップ…」で選択した方式を導入してください。"
        ) from exc

    # PyTorch 2.6+ defaults to weights_only=True. Allowlist these metadata/task
    # classes used by the trusted pyannote checkpoint. Do not disable safe loading.
    torch.serialization.add_safe_globals(
        [
            torch.torch_version.TorchVersion,
            Specifications,
            Problem,
            Resolution,
        ]
    )

    try:
        emit_setup_progress('話者分離モデルを準備中')
        with hf_snapshot_progress('話者分離モデルを取得中'):
            pipeline = Pipeline.from_pretrained(
                DIARIZATION_MODEL_ID,
                use_auth_token=token,
            )
        emit_setup_progress('話者分離を準備中', detail='モデルの読み込み完了')
    except Exception as exc:
        raise RuntimeError(
            "pyannoteのモデルを読み込めませんでした。\n"
            "Hugging Faceで pyannote/speaker-diarization-3.1 の利用条件に同意し、"
            "読み取り権限のあるHF_TOKENを設定しているか確認してください。\n"
            f"詳細: {exc}"
        ) from exc

    if device == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDAが利用できません。NVIDIAドライバーとCUDA対応PyTorchを確認するか、--device cpuを指定してください。"
            )
        pipeline.to(torch.device("cuda"))
    return pipeline


def release_cuda_memory() -> None:
    """Release model memory between diarization and ASR on small GPUs."""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def get_speaker_turns(pipeline: Any, audio_path: Path, max_speakers: int) -> list[SpeakerTurn]:
    try:
        result = pipeline(str(audio_path), max_speakers=max_speakers)
    except TypeError:
        # pyannote API differences across versions
        result = pipeline(str(audio_path))

    annotation = getattr(result, "speaker_diarization", result)
    turns: list[SpeakerTurn] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append(SpeakerTurn(float(turn.start), float(turn.end), str(speaker)))
    return sorted(turns, key=lambda item: item.start)


def identify_speaker(start: float, end: float, turns: list[SpeakerTurn]) -> str:
    if end <= start:
        end = start + 0.01

    best_speaker = "UNKNOWN"
    best_overlap = 0.0
    midpoint = (start + end) / 2.0

    for turn in turns:
        overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker
        elif turn.start <= midpoint <= turn.end and best_overlap == 0.0:
            best_speaker = turn.speaker

    if best_overlap > 0.0:
        return best_speaker

    # Fill only very small gaps near diarization boundaries.
    nearest = min(
        turns,
        key=lambda turn: min(abs(midpoint - turn.start), abs(midpoint - turn.end)),
        default=None,
    )
    if nearest is not None:
        distance = min(abs(midpoint - nearest.start), abs(midpoint - nearest.end))
        if distance <= 0.35:
            return nearest.speaker
    return "UNKNOWN"



def emit_gui_progress(stage: str, completed: float, total: float, text: str = '', detail: str = '') -> None:
    """Only structured single-line output is consumed by the GUI; CLI ignores it."""
    print('@@TRANSCRIBER@@' + json.dumps({'stage': stage, 'completed': completed,
          'total': total, 'text': text, 'detail': detail}, ensure_ascii=False), flush=True)
    try:
        from .engine_load import pace_if_needed
    except ImportError:
        from engine_load import pace_if_needed
    pace_if_needed(stage)

def transcribe_whisper_words(
    audio_path: Path, model_size: str, device: str, compute_type: str | None
) -> list[TranscriptItem]:
    initialize_cuda_libraries(device)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisperがありません。GUIの「一括セットアップ…」で選択した方式を導入してください。"
        ) from exc

    actual_compute_type = compute_type or ("float16" if device == "cuda" else "int8")
    print(f"faster-whisperモデルを読み込み中: {model_size} ({device}, {actual_compute_type})")
    emit_setup_progress('モデルを準備中', detail='faster-whisper')
    with hf_snapshot_progress('モデル取得 · faster-whisper'):
        model_id, model_options = faster_model_args(model_size, WhisperModel)
        model = WhisperModel(model_id, device=device, compute_type=actual_compute_type, **model_options)
    emit_setup_progress('音声認識を準備中', detail='モデルの読み込み完了')
    segments, info = model.transcribe(
        read_pcm_for_whisper(audio_path),
        language="ja",
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        **asr_hint_options('faster', model=model),
    )

    words: list[TranscriptItem] = []
    fallback_segments: list[TranscriptItem] = []
    print(f"音声認識を実行中 (検出言語: {info.language})...")
    for segment in segments:
        segment_text = (segment.text or "").strip()
        emit_gui_progress('asr', float(segment.end), float(getattr(info, 'duration', 0) or 0),
                          segment_text, f'{float(segment.end):.1f}秒まで認識')
        fallback_segments.append(
            TranscriptItem(float(segment.start), float(segment.end), "UNKNOWN", segment_text)
        )
        if segment.words:
            for word in segment.words:
                text = getattr(word, "word", "") or ""
                if text.strip():
                    words.append(
                        TranscriptItem(
                            float(word.start),
                            float(word.end),
                            "UNKNOWN",
                            text,
                        )
                    )

    return words if words else fallback_segments


def create_qwen_chunks(
    audio_path: Path,
    chunk_dir: Path,
    chunk_seconds: float = 240.0,
    overlap_seconds: float = 1.0,
) -> list[tuple[Path, float, float]]:
    """Split the 16-kHz mono WAV into aligner-sized chunks with a short overlap.

    The official ForcedAligner supports speech segments up to five minutes. Four-
    minute chunks leave headroom; the one-second overlap reduces boundary loss.
    Returns (chunk_path, absolute_start_seconds, overlap_seconds).
    """
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float, float]] = []
    with wave.open(str(audio_path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        total_frames = source.getnframes()
        if channels != 1 or sample_width != 2 or sample_rate != 16000:
            raise RuntimeError("内部WAV形式が想定外です。16 kHz・モノラル・16-bit PCMが必要です。")

        chunk_frames = max(1, int(chunk_seconds * sample_rate))
        overlap_frames = max(0, int(overlap_seconds * sample_rate))
        step_frames = max(1, chunk_frames - overlap_frames)
        start_frame = 0
        index = 0
        while start_frame < total_frames:
            frame_count = min(chunk_frames, total_frames - start_frame)
            source.setpos(start_frame)
            frames = source.readframes(frame_count)
            chunk_path = chunk_dir / f"qwen_chunk_{index:04d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(sample_width)
                target.setframerate(sample_rate)
                target.writeframes(frames)

            chunks.append(
                (
                    chunk_path,
                    start_frame / sample_rate,
                    overlap_frames / sample_rate if index > 0 else 0.0,
                )
            )
            if start_frame + frame_count >= total_frames:
                break
            start_frame += step_frames
            index += 1
    return chunks


# Shared compatibility adapter: the official SDK exposes ForcedAlignResult.items.
if __package__:
    from .qwen_compat import (
        timestamp_entries as _extract_timestamp_entries,
        stamp_value as _get_stamp_value,
        qwen_dtype,
        transcript_text,
        pcm_wav_seconds,
        UNALIGNED_SPEAKER,
        UNALIGNED_PREFIX,
    )
else:
    from qwen_compat import (
        timestamp_entries as _extract_timestamp_entries,
        stamp_value as _get_stamp_value,
        qwen_dtype,
        transcript_text,
        pcm_wav_seconds,
        UNALIGNED_SPEAKER,
        UNALIGNED_PREFIX,
    )


def transcribe_qwen_words(
    audio_path: Path,
    model_id: str,
    aligner_id: str,
    device: str,
    chunk_dir: Path,
    max_new_tokens: int,
) -> list[TranscriptItem]:
    try:
        import torch
        from qwen_asr import Qwen3ASRModel
    except ImportError as exc:
        raise RuntimeError(
            "Qwenのライブラリがありません。GUIの「一括セットアップ…」でQwenの環境を導入してください。"
        ) from exc

    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA対応PyTorchからGPUを利用できません。NVIDIAドライバーを更新し、"
            "GUIの「CUDA診断…」でGPUの確認が成功するか確認してください。"
        )

    device_map = "cuda:0" if device == "cuda" else "cpu"
    dtype = qwen_dtype(torch, device)
    print(f"Qwen ASRモデルを読み込み中: {model_id} ({device_map}, {dtype})")
    print(f"タイムスタンプ用Forced Aligner: {aligner_id}")

    try:
        emit_setup_progress('モデルを準備中', detail='Qwen / Forced Aligner')
        with hf_snapshot_progress('モデル取得 · Qwen'):
            model = Qwen3ASRModel.from_pretrained(
                model_id,
                dtype=dtype,
                device_map=device_map,
                max_inference_batch_size=1,
                max_new_tokens=max_new_tokens,
                forced_aligner=aligner_id,
                forced_aligner_kwargs={
                    "dtype": dtype,
                    "device_map": device_map,
                },
            )
        emit_setup_progress('音声認識を準備中', detail='モデルの読み込み完了')
    except Exception as exc:
        message = str(exc)
        if "out of memory" in message.lower() or "cuda" in message.lower() and "memory" in message.lower():
            raise RuntimeError(
                "Qwen/Forced AlignerのGPUメモリが不足しました。ほかのGPUアプリを終了し、"
                "NVIDIAドライバーを更新して再試行してください。改善しない場合は --device cpu を指定してください。\n"
                f"詳細: {exc}"
            ) from exc
        raise RuntimeError(
            "QwenまたはForced Alignerを読み込めませんでした。ネット接続、モデルID、"
            "CUDA対応PyTorchを確認してください。\n"
            f"詳細: {exc}"
        ) from exc

    chunks = create_qwen_chunks(audio_path, chunk_dir)
    if not chunks:
        return []

    items: list[TranscriptItem] = []
    try:
        for index, (chunk_path, offset, overlap) in enumerate(chunks, start=1):
            print(f"Qwen音声認識・タイムスタンプ付与中: {index}/{len(chunks)}")
            results = model.transcribe(
                audio=str(chunk_path),
                language="Japanese",
                return_time_stamps=True,
                **asr_hint_options('qwen'),
            )
            if not results:
                print(f"注意: Qwenチャンク {index} は認識結果が空でした。")
                continue
            result = results[0]
            emit_gui_progress('asr', index, len(chunks), transcript_text(result),
                              f'{index}/{len(chunks)} チャンク完了')
            stamps = _extract_timestamp_entries(result)
            valid_in_chunk = 0
            chunk_duration = pcm_wav_seconds(chunk_path)
            for stamp in stamps:
                start_value = _get_stamp_value(stamp, "start_time")
                end_value = _get_stamp_value(stamp, "end_time")
                text_value = _get_stamp_value(stamp, "text")
                if start_value is None or end_value is None or text_value is None:
                    continue
                word_text = str(text_value).strip()
                if not word_text:
                    continue
                try:
                    start = float(start_value)
                    end = float(end_value)
                except (TypeError, ValueError):
                    continue
                if not (0 <= start <= end <= chunk_duration + 2.0):
                    continue
                if overlap > 0 and start < overlap:
                    continue
                items.append(TranscriptItem(start + offset, end + offset, "UNKNOWN", word_text))
                valid_in_chunk += 1
            # Preserve recognized text if the aligner genuinely gave no valid stamps.
            # Never pretend we know word positions or a speaker for this fallback.
            if valid_in_chunk == 0:
                text_without_timing = transcript_text(result)
                if not text_without_timing:
                    print(f"注意: Qwenチャンク {index} は無音/認識テキストなしのためスキップ。")
                    continue
                if stamps and overlap > 0:
                    print(f"注意: Qwenチャンク {index} の整列語が重複領域内だけでした。")
                    continue
                print(f"警告: Qwenチャンク {index} のタイムスタンプが空または不正です。"
                      "文字起こし本文を概算区間・話者UNKNOWNで保存します。")
                items.append(TranscriptItem(
                    offset + overlap,
                    offset + chunk_duration,
                    UNALIGNED_SPEAKER,
                    UNALIGNED_PREFIX + text_without_timing,
                ))
    except RuntimeError as exc:
        message = str(exc)
        if "out of memory" in message.lower() or ("cuda" in message.lower() and "memory" in message.lower()):
            raise RuntimeError(
                "Qwen処理中にGPUメモリが不足しました。ほかのGPUアプリを終了して再実行してください。"
                "改善しない場合は --device cpu を指定してください。\n"
                f"詳細: {exc}"
            ) from exc
        raise
    except Exception as exc:
        message = str(exc)
        if "out of memory" in message.lower() or ("cuda" in message.lower() and "memory" in message.lower()):
            raise RuntimeError(
                "Qwen処理中にGPUメモリが不足しました。ほかのGPUアプリを終了して再実行してください。"
                "改善しない場合は --device cpu を指定してください。\n"
                f"詳細: {exc}"
            ) from exc
        raise RuntimeError(f"Qwenの音声認識に失敗しました。\n詳細: {exc}") from exc
    finally:
        del model
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()

    return sorted(items, key=lambda item: item.start)


def merge_items(
    items: list[TranscriptItem], turns: list[SpeakerTurn], merge_gap: float,
    caption_max_chars: int | None = None, caption_max_seconds: float = 5.0
) -> list[TranscriptItem]:
    if not items:
        return []

    # Normalize speaker IDs to SPEAKER_00... by first appearance.
    first_seen: dict[str, float] = {}
    for turn in turns:
        first_seen.setdefault(turn.speaker, turn.start)
    speaker_names = {
        speaker: f"SPEAKER_{index:02d}"
        for index, (speaker, _) in enumerate(sorted(first_seen.items(), key=lambda pair: pair[1]))
    }

    labeled: list[TranscriptItem] = []
    for item in sorted(items, key=lambda entry: entry.start):
        source_speaker = identify_speaker(item.start, item.end, turns)
        speaker = ("UNKNOWN" if item.speaker == UNALIGNED_SPEAKER
                   else speaker_names.get(source_speaker, "UNKNOWN"))
        labeled.append(TranscriptItem(item.start, item.end, speaker, item.text))

    if caption_max_chars is not None:
        return split_timed_items(labeled, max_chars=caption_max_chars, max_seconds=caption_max_seconds)

    merged: list[TranscriptItem] = []
    for item in labeled:
        if (
            merged
            and merged[-1].speaker == item.speaker
            and item.start - merged[-1].end <= merge_gap
        ):
            # Keep any spacing provided by the ASR backend.
            merged[-1].text += item.text
            merged[-1].end = max(merged[-1].end, item.end)
        else:
            merged.append(TranscriptItem(item.start, item.end, item.speaker, item.text))

    for item in merged:
        item.text = item.text.strip()
    return [item for item in merged if item.text]


def render_transcript(items: list[TranscriptItem]) -> str:
    return "\n\n".join(
        f"[{format_timestamp(item.start)}] {item.speaker}: {item.text}"
        for item in items
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="音声・動画を話者分離し、タイムスタンプ付きUTF-8 TXTに保存します。"
    )
    parser.add_argument("input", type=Path, help="入力音声／動画ファイル (例: meeting.mp4)")
    parser.add_argument(
        "--asr-backend",
        choices=("whisper", "qwen"),
        default="whisper",
        help="文字起こしエンジン (既定: whisper。Qwen日本語モデルを使う場合はqwen)",
    )
    parser.add_argument(
        "--model",
        default="large-v3-turbo",
        help="faster-whisperモデル: turbo / large-v3-turbo / large-v3 / medium / small (既定: large-v3-turbo)",
    )
    parser.add_argument(
        "--qwen-model",
        default=DEFAULT_QWEN_MODEL_ID,
        help=f"Qwen ASRモデルID (既定: {DEFAULT_QWEN_MODEL_ID})",
    )
    parser.add_argument(
        "--qwen-aligner",
        default=DEFAULT_QWEN_ALIGNER_ID,
        help=f"Qwen Forced AlignerモデルID (既定: {DEFAULT_QWEN_ALIGNER_ID})",
    )
    parser.add_argument(
        "--qwen-max-new-tokens",
        type=int,
        default=4096,
        help="Qwen各音声チャンクの生成上限 (既定: 4096)",
    )
    parser.add_argument(
        "--max-speakers", type=int, default=6, help="想定する最大話者数 (既定: 6)"
    )
    parser.add_argument(
        "--device", choices=("cpu", "cuda"), default="cpu", help="処理デバイス (既定: cpu)"
    )
    parser.add_argument(
        "--compute-type",
        default=None,
        help="faster-whisper計算形式。既定はCPU=int8、CUDA=float16 (Whisperのみ)",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=1.2,
        help="同じ話者の発話を結合する最大間隔・秒 (既定: 1.2)",
    )
    parser.add_argument("--caption-max-chars", type=int, default=None)
    parser.add_argument("--caption-max-seconds", type=float, default=5.0)
    parser.add_argument(
        "--output", type=Path, default=None, help="出力TXTパス (既定: 入力と同じ場所・同じ名前.txt)"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    if not source.is_file():
        print(f"入力ファイルが見つかりません: {source}", file=sys.stderr)
        return 2
    if args.max_speakers < 1:
        print("--max-speakers は1以上を指定してください。", file=sys.stderr)
        return 2
    if args.merge_gap < 0:
        print("--merge-gap は0以上を指定してください。", file=sys.stderr)
        return 2
    if args.qwen_max_new_tokens < 1:
        print("--qwen-max-new-tokens は1以上を指定してください。", file=sys.stderr)
        return 2

    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print(
            "Hugging Faceのアクセストークンが未設定です。\n"
            "PowerShellで $env:HF_TOKEN = 'hf_...' を設定してから実行してください。\n"
            "トークン設定とモデル利用条件への同意手順はREADME_Windows.txtを参照してください。",
            file=sys.stderr,
        )
        return 2

    output = args.output.expanduser().resolve() if args.output else source.with_suffix(".txt")
    if output == source:
        print("出力TXTに入力ファイルと同じパスは指定できません。", file=sys.stderr)
        return 2

    try:
        ffmpeg = check_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="speaker_transcribe_") as temp_dir:
            temp_path = Path(temp_dir)
            wav_path = temp_path / "audio_16k_mono.wav"
            print(f"音声を準備中: {source.name}")
            extract_audio(ffmpeg, source, wav_path)

            pipeline = load_diarization_pipeline(token, args.device)
            print("話者区間を解析中...")
            turns = get_speaker_turns(pipeline, wav_path, args.max_speakers)
            # The diarization pass is complete. Free GPU memory before loading the
            # larger ASR/aligner pair; this matters on 8-GB GPUs such as RTX 4060.
            del pipeline
            release_cuda_memory()

            if args.asr_backend == "qwen":
                items = transcribe_qwen_words(
                    wav_path,
                    args.qwen_model,
                    args.qwen_aligner,
                    args.device,
                    temp_path / "qwen_chunks",
                    args.qwen_max_new_tokens,
                )
            else:
                items = transcribe_whisper_words(
                    wav_path, args.model, args.device, args.compute_type
                )

            final_items = merge_items(items, turns, args.merge_gap,
                        caption_max_chars=args.caption_max_chars, caption_max_seconds=args.caption_max_seconds)
            transcript = render_transcript(final_items)

        write_transcript_atomic(output, transcript + ('\n' if transcript else ''))

        print(f"完了: {output}")
        if not turns:
            print("注意: 話者区間を検出できませんでした。出力の話者名はUNKNOWNになる場合があります。")
        return 0
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
