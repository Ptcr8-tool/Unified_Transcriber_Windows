"""Pure-Python configuration and command orchestration for the unified GUI.

All ML work remains in the three original, independently installed engines.
No imported ML dependencies are needed to launch the GUI.
"""
from __future__ import annotations

import json
import math
import ntpath
import os
import re
import sys
import tempfile
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path, PureWindowsPath

# Application code and environments are stored inside the installation folder.
BASE = Path(__file__).resolve().parent.parent
CODE_DIR = Path(__file__).resolve().parent
APP_VERSION = '1.0.0'

from portable_runtime import (MAX_CONFIG_BYTES, configure_portable_environment,
                              is_portable)
from output_locations import (OUTPUT_FOLDERS, default_output_root,
                              output_root, validate_output_root)
from model_defaults import QWEN_MODEL, QWEN_ALIGNER
configure_portable_environment(BASE)

def default_directory(kind: str = 'transcriptions', config: Config | None = None) -> Path:
    """Default output below Documents, or the user's selected common folder."""
    root = ((Path(validate_output_root(config.output_root)) if config.output_root.strip()
             else default_output_root()) if config is not None
            else output_root(BASE, config_path=app_config_path()))
    return root / OUTPUT_FOLDERS[kind]

def ensure_output_directories() -> None:
    for kind in OUTPUT_FOLDERS:
        default_directory(kind).mkdir(parents=True, exist_ok=True)


def environment_folder(profile: str) -> Path:
    """Keep all engine environments inside the private application folder."""
    return CODE_DIR / 'environments' / profile
SUPPORTED_EXTENSIONS = frozenset({
    '.mp4', '.mov', '.mkv', '.avi', '.mp3', '.wav', '.m4a', '.flac', '.aac', '.wma'
})

@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    engine: str
    asr: str
    diarizer: str
    description: str

PROFILES = (
    Profile('openai-res', '① Whisper (OpenAI) + Resemblyzer',
            'openai_resemblyzer.py', 'openai', 'resemblyzer',
            'Resemblyzer：発話単位／固定長を切替可能。Hugging Face認証不要。'),
    Profile('faster-res', '② faster-whisper + Resemblyzer（推奨）',
            'segment_resemblyzer.py', 'faster', 'resemblyzer',
            '高速Whisper＋Resemblyzer。発話単位／固定長を選択できます。HF不要。'),
    Profile('faster-pya', '③ faster-whisper + pyannote',
            'pyannote_backend.py', 'faster', 'pyannote',
            '単語タイムスタンプ＋詳細な話者区間推定。HFトークンと利用条件同意が必要。'),
    Profile('qwen-res', '④ Qwen3-ASR-JA + Resemblyzer',
            'segment_resemblyzer.py', 'qwen', 'resemblyzer',
            '日本語Qwen＋ForcedAligner＋Resemblyzer。発話単位／固定長を選択。HF不要。'),
    Profile('qwen-pya', '⑤ Qwen3-ASR-JA + pyannote',
            'pyannote_backend.py', 'qwen', 'pyannote',
            '日本語QwenとForcedAligner＋pyannote話者推定。HFトークンが必要です。'),
)
BY_ID = {p.id: p for p in PROFILES}


def legacy_config_path() -> Path:
    appdata = os.environ.get('APPDATA')
    return (Path(appdata) if appdata else Path.home() / '.config') / 'UnifiedTranscriberV12' / 'settings.json'


def app_config_path() -> Path:
    return BASE / 'Data' / 'settings.json' if is_portable(BASE) else legacy_config_path()


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validated_python_override(value: str) -> str:
    if not isinstance(value, str) or '\0' in value or len(value) > 32768:
        raise ValueError('Python環境には実行ファイルを指定してください。')
    value = value.strip()
    windows_path = os.name == 'nt' or bool(ntpath.splitdrive(value)[0]) or '\\' in value
    if value and windows_path and PureWindowsPath(value).suffix.casefold() != '.exe':
        # .bat/.cmd can invoke cmd.exe implicitly even with shell=False.
        raise ValueError('WindowsのPython環境にはpython.exeを指定してください。')
    return value


@dataclass
class Config:
    profile: str = 'faster-res'
    model_openai: str = 'turbo'
    model_faster: str = 'large-v3-turbo'
    qwen_model: str = QWEN_MODEL
    qwen_aligner: str = QWEN_ALIGNER
    qwen_max_tokens: int = 4096
    language: str = 'ja'
    device: str = 'auto'    # auto resolves CUDA capability inside selected environment
    compute_type: str = 'auto'
    max_speakers: int = 6
    diarization_unit: str = 'utterance'  # Resemblyzer: 'utterance' or 'fixed'
    speaker_mode: str = 'auto'  # Resemblyzer count: auto threshold or fixed speakers
    speakers: int = 2
    threshold: float = 0.25
    segment_length: float = 20.0
    merge_gap: float = 1.2
    output_root: str = ''  # empty selects Documents/UnifiedTranscriber
    output_dir: str = ''
    output_same_as_input: bool = False
    auto_open_speaker: bool = False
    theme_mode: str = 'system'
    onboarding_seen: bool = False
    capture_setup_complete: bool = False
    tray_minimize: bool = False
    tray_close: bool = False
    skip_live_settings: bool = False
    skip_gamebar_settings: bool = False
    capture_preferences: dict[str, dict] = field(default_factory=dict)
    # Proper names are supplied as recognition hints only; no output replacement.
    hint_enabled: bool = False
    hint_groups: dict[str, list[dict[str, str]]] = field(default_factory=lambda: {'共通': []})
    hint_selected_groups: list[str] = field(default_factory=list)
    shortcut_keys: dict[str, str] = field(default_factory=lambda: {
        'live': '', 'gamebar': '', 'stop': ''})
    live_recording_dir: str = ''
    live_recording_format: str = 'wav'
    live_recording_bitrate: str = '192k'
    live_recording_pcm_bits: int = 16
    live_recording_sample_rate: int = 48000
    live_recording_channels: int = 2
    live_channel_mode: str = 'mixed'
    live_post_transcribe: bool = False
    # Optional output filename suffixes (old configurations remain compatible).
    filename_timestamp: bool = False
    filename_model: bool = False
    filename_backend: bool = False
    filename_diarization: bool = False
    filename_media_creation: bool = False
    txt_metadata_header: bool = True
    caption_split: bool = False  # shorten utterances for auto-subtitle editing
    caption_max_chars: int = 28
    caption_max_seconds: float = 5.0
    gamebar_shortcut: str = 'win+alt+r'
    gamebar_audio_source: str = 'mix'
    gamebar_captures_dir: str = ''
    output_mode: str = 'txt'  # txt / csv / both
    cpu_limit_percent: int | None = None  # Windows Job Object percent; None=unlimited
    gpu_soft_percent: int | None = None  # approximate inter-segment pacing only
    hf_token_json: str = ''  # Explicit opt-in ONLY; plaintext, discouraged.
    overwrite: bool = False
    # hf_token_json is persisted ONLY upon explicit opt-in; default is empty.
    python_overrides: dict[str, str] = field(default_factory=dict)
    update_check_mode: str = 'days'
    update_check_days: int = 7
    update_last_checked: str = ''

    def validate(self) -> None:
        # Validate types before string/numeric operations. JSON booleans are
        # integers in Python, but must never count as numeric setting values.
        for item in fields(self):
            default = item.default if item.default is not MISSING else item.default_factory()
            value = getattr(self, item.name)
            valid = (value is None or type(value) is int) if default is None else (
                type(value) is bool if type(default) is bool else
                type(value) is int if type(default) is int else
                _finite_number(value) if type(default) is float else
                isinstance(value, type(default)))
            if not valid or (isinstance(value, str) and ('\0' in value or len(value) > 32768)):
                raise ValueError(f'設定項目「{item.name}」の値を確認してください。')
        from term_hints import validate_hint_settings
        validate_hint_settings(self.hint_enabled, self.hint_groups, self.hint_selected_groups)
        if any(key not in BY_ID or not isinstance(value, str) or '\0' in value
               for key, value in self.python_overrides.items()):
            raise ValueError('Python環境の割り当てを確認してください。')
        for value in self.python_overrides.values():
            _validated_python_override(value)
        if self.update_check_mode not in ('days', 'every', 'never') or not 1 <= self.update_check_days <= 365:
            raise ValueError('更新確認は毎回・1〜365日ごと・なしから選択してください。')
        if len(self.update_last_checked) > 64:
            raise ValueError('更新確認日時を確認してください。')
        if self.theme_mode not in ('system', 'light', 'dark'):
            raise ValueError('テーマは system / light / dark を指定してください。')
        for key in ('output_same_as_input', 'auto_open_speaker', 'onboarding_seen', 'capture_setup_complete',
                    'live_post_transcribe', 'skip_live_settings', 'skip_gamebar_settings',
                    'tray_minimize', 'tray_close'):
            if not isinstance(getattr(self, key), bool):
                raise ValueError('画面・保存先の設定を確認してください。')
        if not isinstance(self.capture_preferences, dict) or any(
                key not in ('live','gamebar') or not isinstance(value,dict)
                for key,value in self.capture_preferences.items()):
            raise ValueError('録音・録画の前回設定を確認してください。')
        from gamebar_recording import GameBarOptions
        from live_config import LiveOptions
        for kind, values in self.capture_preferences.items():
            factory = LiveOptions if kind == 'live' else GameBarOptions
            defaults = factory()
            accepted = {item.name for item in fields(defaults)}
            converted = {}
            for key, value in values.items():
                if key not in accepted:
                    raise ValueError('録音・録画の前回設定を確認してください。')
                expected = getattr(defaults, key)
                valid = ((value is None or type(value) is int) if expected is None else
                         isinstance(value, str) if isinstance(expected, Path) else
                         _finite_number(value) if type(expected) is float else
                         type(value) is type(expected))
                if not valid or (isinstance(value, str) and ('\0' in value or len(value) > 32768)):
                    raise ValueError('録音・録画の前回設定を確認してください。')
                if kind == 'gamebar' and key in ('target_hwnd', 'target_title'):
                    continue  # A recording target is selected afresh on every start.
                converted[key] = Path(value) if isinstance(expected, Path) else value
            factory(**converted).validate()
        if any(not isinstance(value, str) for value in self.shortcut_keys.values()):
            raise ValueError('ショートカット設定を確認してください。')
        from app_hotkeys import validate_shortcuts
        validate_shortcuts(self.shortcut_keys, self.gamebar_shortcut)
        if self.profile not in BY_ID:
            raise ValueError('未対応の文字起こし方式が選択されています。')
        if self.device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('デバイスは auto / cpu / cuda を指定してください。')
        if self.compute_type not in ('auto', 'int8', 'float16', 'float32', 'int8_float16'):
            raise ValueError('計算精度を確認してください。')
        if self.diarization_unit not in ('utterance', 'fixed'):
            raise ValueError('話者分離の区間は utterance / fixed を指定してください。')
        if self.speaker_mode not in ('auto', 'fixed'):
            raise ValueError('話者数設定を確認してください。')
        if not (1 <= self.max_speakers <= 50 and 1 <= self.speakers <= 50):
            raise ValueError('話者数は1〜50で指定してください。')
        if not (0 < self.threshold <= 2):
            raise ValueError('分離しきい値は0より大きく2以下にしてください。')
        if not (1 < self.segment_length <= 600):
            raise ValueError('固定長話者分離の区間は1秒超〜600秒です。')
        if not (0 <= self.merge_gap <= 60):
            raise ValueError('発話結合の間隔は0〜60秒です。')
        if not (1 <= self.qwen_max_tokens <= 65536):
            raise ValueError('Qwen出力トークン上限は1〜65536です。')
        if not self.model_openai.strip() or not self.model_faster.strip():
            raise ValueError('Whisperモデルを入力してください。')
        if not self.qwen_model.strip() or not self.qwen_aligner.strip():
            raise ValueError('QwenモデルとForced Alignerモデルを入力してください。')
        if not self.language.strip():
            raise ValueError('言語コードを入力してください。')
        for key in ('filename_timestamp', 'filename_model', 'filename_backend', 'filename_diarization', 'filename_media_creation', 'txt_metadata_header'):
            if not isinstance(getattr(self, key), bool):
                raise ValueError('出力ファイル名のオプション設定を確認してください。')
        if not isinstance(self.caption_split, bool) or not 8 <= self.caption_max_chars <= 120 or not 1 <= self.caption_max_seconds <= 30:
            raise ValueError('字幕向け分割は最大8〜120文字、1〜30秒で設定してください。')
        for pct in (self.cpu_limit_percent, self.gpu_soft_percent):
            if pct is not None and (not isinstance(pct,int) or isinstance(pct,bool) or not 10 <= pct <= 100):
                raise ValueError('負荷制限は未指定または10〜100%で指定してください。')
        from gamebar_recording import parse_hotkey
        parse_hotkey(self.gamebar_shortcut)
        if self.gamebar_audio_source not in ('mic','loopback','mix'):
            raise ValueError('Game Bar録音の入力設定を確認してください。')
        if self.output_mode not in ('txt', 'csv', 'both'):
            raise ValueError('出力形式はTXT / CSV / 両方から選択してください。')
        if self.live_recording_format not in ('wav','mp3','m4a','flac'):
            raise ValueError('録音形式が不正です。')
        if self.live_recording_bitrate not in ('64k','96k','128k','160k','192k','256k','320k'):
            raise ValueError('録音ビットレートが不正です。')
        if self.live_recording_pcm_bits not in (16,24):
            raise ValueError('録音のPCMビット深度が不正です。')
        if self.live_recording_sample_rate not in (44100,48000) or self.live_recording_channels not in (1,2):
            raise ValueError('録音出力パラメーターが不正です。')
        if self.live_channel_mode not in ('mixed', 'split_lr') or (
                self.live_channel_mode == 'split_lr' and self.live_recording_channels != 2):
            raise ValueError('録音チャンネルの割り当てが不正です。')
        if self.live_recording_dir and Path(self.live_recording_dir).is_file():
            raise ValueError('録音保存先にはフォルダーを指定してください。')
        if self.output_dir and Path(self.output_dir).is_file():
            raise ValueError('出力フォルダーにファイルを指定できません。')
        validate_output_root(self.output_root)


def load_config(path: Path | None = None) -> Config:
    default_path = path is None
    path = Path(path) if path is not None else app_config_path()
    migrate = default_path and is_portable(BASE) and not path.exists()
    migration_note = BASE / 'Data' / 'legacy-import.json'
    source = legacy_config_path() if migrate and not migration_note.exists() else path
    try:
        if source.stat().st_size > MAX_CONFIG_BYTES:
            return Config()
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            return Config()
        accepted = {f.name for f in fields(Config)}
        data = {k: v for k, v in data.items() if k in accepted}
        if not isinstance(data.get('python_overrides', {}), dict):
            data.pop('python_overrides', None)
        cfg = Config(**data)
        cfg.validate()
        legacy_default=Path.home() / 'Documents' / 'Transcriptions'
        if cfg.output_dir and Path(cfg.output_dir).expanduser() == legacy_default:
            cfg.output_dir=''  # old default, not an intentional custom output folder
        if cfg.live_recording_dir and Path(cfg.live_recording_dir).expanduser() == BASE / 'Recordings':
            cfg.live_recording_dir = ''  # Follow the current common root for the old default only.
        if migrate and source != path:
            # Only copy a validated configuration. Never change or remove the
            # previous installation's settings; interrupted migration is retried.
            try:
                save_config(cfg, path)
                migration_note.write_text('{"imported":true}\n', encoding='utf-8')
            except OSError:
                # An unwritable new folder must not discard a valid legacy
                # configuration already loaded into memory.
                pass
        return cfg
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, json.JSONDecodeError):
        return Config()


def save_config(config: Config, path: Path | None = None) -> None:
    config.validate()
    path = path or app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Plaintext HF token is excluded by default. Only explicit opt-in saves it.
    payload = asdict(config)
    for key in ('target_hwnd','target_title'):
        payload.get('capture_preferences',{}).get('gamebar',{}).pop(key,None)
    if not config.hf_token_json:
        payload.pop('hf_token_json', None)
    # Never replace a valid settings file with one that load_config would reject
    # because added user dictionaries made it exceed the documented read cap.
    encoded_payload = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if len(encoded_payload.encode('utf-8')) > MAX_CONFIG_BYTES:
        raise ValueError('設定ファイルが大きすぎます。辞書の単語数などを減らしてください。')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                dir=path.parent, prefix='.'+path.name+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded_payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def env_python(profile: str, config: Config | None = None) -> Path:
    if profile not in BY_ID:
        raise ValueError('Unknown profile')
    override = _validated_python_override(config.python_overrides.get(profile, '') if config else '')
    if override:
        return Path(override).expanduser()
    venv = environment_folder(profile)
    return venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def engine_path(profile: str) -> Path:
    return CODE_DIR / 'engines' / BY_ID[profile].engine


def safe_filename_token(raw: str) -> str:
    """Sanitize a filename suffix token on Windows without affecting Japanese Unicode."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '-', str(raw))
    value = re.sub(r'\s+', '_', value).strip(' ._-')[:72].rstrip(' ._-')
    return value or 'unknown'


def output_name(source: Path, config: Config, *, transcribed_at: datetime | None = None,
                media_created_at: datetime | None = None) -> str:
    """Output filename with independent opt-in, predictable timestamp/model tags."""
    profile = BY_ID[config.profile]
    suffixes = []
    if config.filename_timestamp:
        when = transcribed_at if transcribed_at is not None else datetime.now()
        suffixes.append(when.strftime('%Y%m%d_%H%M%S'))
    if config.filename_media_creation and media_created_at is not None:
        suffixes.append('recorded-' + media_created_at.strftime('%Y%m%d_%H%M%S'))
    if config.filename_model:
        model = (config.model_openai if profile.asr == 'openai' else
                 config.model_faster if profile.asr == 'faster' else config.qwen_model)
        suffixes.append(safe_filename_token(re.split(r'[/\\]', model)[-1]))
    if config.filename_backend:
        suffixes.append(safe_filename_token({'openai':'Whisper', 'faster':'faster-whisper',
                                            'qwen':'Qwen3-ASR'}[profile.asr]))
    if config.filename_diarization:
        suffixes.append(safe_filename_token(profile.diarizer +
                         ('-' + config.diarization_unit if profile.diarizer == 'resemblyzer' else '')))
    return source.stem + (('_' + '_'.join(suffixes)) if suffixes else '') + '.txt'


def choose_output(source: Path, config: Config, *, watch_processed: Path | None = None,
                  transcribed_at: datetime | None = None,
                  media_created_at: datetime | None = None) -> Path:
    if watch_processed is not None:
        folder = watch_processed
    elif config.output_same_as_input:
        folder = source.parent
    elif config.output_dir.strip():
        folder = Path(config.output_dir).expanduser()
    else:
        folder = default_directory('transcriptions', config)
    return folder / output_name(source, config, transcribed_at=transcribed_at,
                                media_created_at=media_created_at)


def build_command(source: Path, destination: Path, config: Config, python: Path, device: str) -> list[str]:
    """Retain every original engine and pass arguments as a list (never through a shell)."""
    config.validate()
    p = BY_ID[config.profile]
    if device not in ('cpu', 'cuda'):
        raise ValueError('Resolved device must be cpu/cuda')
    args = [str(python), '-u', str(engine_path(p.id)), str(source), '--output', str(destination)]
    if p.diarizer != 'pyannote':
        args += ['--diarization-unit', config.diarization_unit,
                 '--segment-length', str(config.segment_length),
                 '--threshold', str(config.threshold)]
        if config.speaker_mode == 'fixed':
            args += ['--speakers', str(config.speakers)]
    if p.asr == 'openai':
        args += ['--model', config.model_openai.strip(), '--language', config.language.strip(),
                 '--device', device, '--max-speakers', str(config.max_speakers)]
        # Intermediate output filename is unique. Engine may overwrite only this temp path.
        args += ['--overwrite']
    else:
        args += ['--asr-backend', 'qwen' if p.asr == 'qwen' else 'whisper',
                 '--device', device, '--max-speakers', str(config.max_speakers),
                 '--merge-gap', str(config.merge_gap)]
        if p.asr == 'faster':
            args += ['--model', config.model_faster.strip()]
            if config.compute_type != 'auto':
                args += ['--compute-type', config.compute_type]
        else:
            args += ['--qwen-model', config.qwen_model.strip(),
                     '--qwen-aligner', config.qwen_aligner.strip(),
                     '--qwen-max-new-tokens', str(config.qwen_max_tokens)]
    if config.caption_split:
        args += ['--caption-max-chars', str(config.caption_max_chars),
                 '--caption-max-seconds', str(config.caption_max_seconds)]
    return args


def redact(text: str, token: str) -> str:
    return text.replace(token, '[HF_TOKEN非表示]') if token else text


def supported_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
