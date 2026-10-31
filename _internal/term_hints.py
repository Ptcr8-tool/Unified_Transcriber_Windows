"""Local proper-name hints. Never replace or rewrite recognized text.

Dictionary settings live in the existing Config so normal settings import/export
and atomic settings persistence include them. A dedicated JSON interchange
format is available for transferring only the dictionary.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

COMMON_GROUP = '共通'
DICTIONARY_FORMAT = 'unified-transcriber-hint-dictionary'
DICTIONARY_VERSION = 1
MAX_GROUPS = 50
MAX_ENTRIES = 1000
MAX_NAME_LENGTH = 80
MAX_TERM_LENGTH = 120
MAX_READING_LENGTH = 120
MAX_HINT_LENGTH = 1000
MAX_DICTIONARY_BYTES = 2 * 1024 * 1024
HINT_ENV_NAME = 'TRANSCRIBER_ASR_HINTS'


def _valid_text(value, maximum, *, empty=False):
    return (isinstance(value, str) and len(value) <= maximum
            and value == value.strip() and (bool(value) or empty)
            and not any(ord(char) < 32 or ord(char) == 127
                        or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def validate_hint_settings(enabled, groups, selected):
    """Validate all inputs before use, including untrusted imported JSON."""
    if type(enabled) is not bool:
        raise ValueError('辞書の有効・無効を確認してください。')
    if not isinstance(groups, dict) or not 1 <= len(groups) <= MAX_GROUPS or COMMON_GROUP not in groups:
        raise ValueError('辞書グループを確認してください。共通グループは必須です。')
    total = 0
    for name, entries in groups.items():
        if not _valid_text(name, MAX_NAME_LENGTH) or not isinstance(entries, list):
            raise ValueError('辞書グループの名前または単語一覧を確認してください。')
        total += len(entries)
        if total > MAX_ENTRIES:
            raise ValueError(f'辞書には最大 {MAX_ENTRIES} 件まで登録できます。')
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'term', 'reading'}:
                raise ValueError('辞書の単語・読み方を確認してください。')
            if not _valid_text(entry['term'], MAX_TERM_LENGTH) or '、' in entry['term']:
                raise ValueError('辞書の単語を確認してください。「、」は単語の区切りに使うため登録できません。')
            if not _valid_text(entry['reading'], MAX_READING_LENGTH, empty=True):
                raise ValueError('辞書の読み方を確認してください。')
    if (not isinstance(selected, list) or len(selected) > MAX_GROUPS
            or any(not isinstance(name, str) or name not in groups for name in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('利用する辞書グループを確認してください。')


def _selected_terms(config):
    validate_hint_settings(config.hint_enabled, config.hint_groups, config.hint_selected_groups)
    if not config.hint_enabled:
        return []
    names = [COMMON_GROUP] + [name for name in config.hint_selected_groups if name != COMMON_GROUP]
    terms = []
    seen = set()
    for name in names:
        for entry in config.hint_groups[name]:
            term = entry['term']
            key = term.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(term)
    return terms


def hint_summary(config):
    """Return the sent hint text and the number included/omitted.

    Prefer complete words over partial words, and consider later shorter words
    after an overly long entry. The model may impose an additional token limit.
    """
    terms = _selected_terms(config)
    included = []
    used = 0
    for term in terms:
        size = len(term) + (1 if included else 0)
        if used + size <= MAX_HINT_LENGTH:
            included.append(term)
            used += size
    return {'text': '、'.join(included), 'included': len(included),
            'omitted': len(terms) - len(included)}


def build_hint_text(config):
    return hint_summary(config)['text']


def apply_hint_environment(env, config):
    """Forward only explicitly selected hints; never inherit stale hints."""
    text = build_hint_text(config)
    if text:
        env[HINT_ENV_NAME] = text
    else:
        env.pop(HINT_ENV_NAME, None)
    return env


def dictionary_data(config):
    validate_hint_settings(config.hint_enabled, config.hint_groups, config.hint_selected_groups)
    return {'format': DICTIONARY_FORMAT, 'version': DICTIONARY_VERSION,
            'hint_enabled': config.hint_enabled,
            'hint_groups': deepcopy(config.hint_groups),
            'hint_selected_groups': list(config.hint_selected_groups)}


def read_dictionary(path):
    path = Path(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_DICTIONARY_BYTES + 1)
        if len(raw) > MAX_DICTIONARY_BYTES:
            raise ValueError('辞書ファイルが大きすぎます。')
        data = json.loads(raw.decode('utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('辞書ファイルを読み込めません。') from exc
    if (not isinstance(data, dict) or data.get('format') != DICTIONARY_FORMAT
            or type(data.get('version')) is not int or data['version'] != DICTIONARY_VERSION):
        raise ValueError('この辞書ファイルの形式・バージョンには対応していません。')
    try:
        enabled, groups, selected = (data['hint_enabled'], data['hint_groups'], data['hint_selected_groups'])
    except KeyError as exc:
        raise ValueError('辞書ファイルの内容が不足しています。') from exc
    validate_hint_settings(enabled, groups, selected)
    return {'hint_enabled': enabled, 'hint_groups': deepcopy(groups),
            'hint_selected_groups': list(selected)}


def write_dictionary(path, config):
    """Write a standalone dictionary, replacing the target only after fsync."""
    data = dictionary_data(config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
