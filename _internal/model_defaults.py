"""Model identifiers shared by standalone ASR engines and configuration."""

QWEN_MODEL = 'neosophie/Qwen3-ASR-1.7B-JA'
QWEN_ALIGNER = 'Qwen/Qwen3-ForcedAligner-0.6B'

# This is the repository used by faster-whisper 1.2.1 for both turbo aliases.
# The full Hugging Face commit fixes the recommended model independently of
# future changes to the repository's mutable main branch. The historical
# repository name preserves the shared cache used by older installations.
FASTER_TURBO_REPO = 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'
FASTER_TURBO_REVISION = '0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf'


def faster_model_identity(name: str) -> tuple[str, dict[str, str]]:
    if name in ('turbo', 'large-v3-turbo'):
        return FASTER_TURBO_REPO, {'revision': FASTER_TURBO_REVISION}
    return name, {}


def faster_model_args(name: str, api) -> tuple[str, dict[str, str]]:
    """Use a pinned local snapshot when an older faster-whisper lacks revision.

    Older 1.0/1.1 APIs accept a model directory but not the revision keyword.
    Hugging Face's snapshot API supports full commit IDs on those versions.
    Do not fall back to a floating revision if retrieval fails.
    """
    import inspect
    model_id, options = faster_model_identity(name)
    if not options:
        return model_id, options
    parameters = inspect.signature(api).parameters.values()
    # Some older WhisperModel constructors accept **model_kwargs but forward
    # those kwargs to CTranslate2, which does not accept a revision argument.
    # A variadic parameter is therefore not evidence of revision support.
    accepts_revision = any(p.name == 'revision' and p.kind in
                           (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY)
                           for p in parameters)
    if accepts_revision:
        return model_id, options
    from huggingface_hub import snapshot_download
    path = snapshot_download(repo_id=model_id, revision=FASTER_TURBO_REVISION,
        allow_patterns=['config.json', 'preprocessor_config.json', 'model.bin',
                        'tokenizer.json', 'vocabulary.*'])
    return path, {}
