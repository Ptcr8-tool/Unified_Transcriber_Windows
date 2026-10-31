"""Pass optional vocabulary hints to recognizers without changing their output."""
from __future__ import annotations

import os
import inspect

ENV_KEY = 'TRANSCRIBER_ASR_HINTS'
# Whisper's text context is finite. Reserve capacity for prior output and
# generation; include complete terms only, and never split a Unicode sequence.
WHISPER_HINT_TOKEN_LIMIT = 80


def _whisper_hints(text: str, tokenizer=None) -> tuple[str, bool]:
    terms = [part.strip() for part in text.split('、') if part.strip()]
    selected = []
    limited = False
    for term in terms:
        candidate = '、'.join([*selected, term])
        try:
            if tokenizer is None:
                raise LookupError('tokenizer unavailable')
            count = int(tokenizer(candidate))
        except Exception:
            # Whisper's byte-level BPE cannot produce more tokens than bytes;
            # reserve space for the leading token added by implementations.
            count = len((' ' + candidate).encode('utf-8'))
        if count <= WHISPER_HINT_TOKEN_LIMIT:
            selected.append(term)
        else:
            limited = True
    return '、'.join(selected), limited


def options_for(kind: str, *, model=None, whisper_module=None, language='ja') -> dict:
    """Return supported model kwargs; an empty dictionary preserves old behavior.

    Values travel in the environment of the child process rather than its
    command line. Do not print the vocabulary; it can contain private names.
    """
    text = os.environ.get(ENV_KEY, '').strip()
    if not text:
        return {}
    if len(text) > 1000 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError('認識ヒントが長すぎるか、使用できない文字を含みます。辞書設定を確認してください。')
    if kind == 'qwen':
        return {'context': text}
    if kind not in ('faster', 'openai'):
        raise ValueError('この認識方式は辞書ヒントに対応していません。')
    tokenizer = None
    if kind == 'faster' and getattr(model, 'hf_tokenizer', None) is not None:
        tokenizer = lambda s: len(model.hf_tokenizer.encode(' ' + s, add_special_tokens=False).ids)
    elif kind == 'openai' and whisper_module is not None:
        try:
            wt = whisper_module.tokenizer.get_tokenizer(
                model.is_multilingual, num_languages=model.num_languages,
                language=language or 'ja', task='transcribe')
            tokenizer = lambda s: len(wt.encode(' ' + s))
        except (AttributeError, TypeError, ValueError):
            pass
    limited, omitted = _whisper_hints(text, tokenizer)
    if omitted:
        print('注意: 辞書が認識エンジンのヒント上限を超えたため、一部の単語を今回の認識に使用できません。', flush=True)
    if not limited:
        return {}
    if kind == 'faster':
        return {'hotwords': limited}
    options = {'initial_prompt': limited}
    try:
        if 'carry_initial_prompt' in inspect.signature(model.transcribe).parameters:
            options['carry_initial_prompt'] = True
        else:
            print('注意: 現在の Whisper は最初の区間にのみ辞書ヒントを適用します。', flush=True)
    except (AttributeError, ValueError, TypeError):
        print('注意: この Whisper で長時間の辞書ヒントを維持できるか確認できません。', flush=True)
    return options
