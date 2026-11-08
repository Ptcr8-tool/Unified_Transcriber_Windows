"""Use Windows certificate trust for application-owned model downloads."""
from __future__ import annotations

from sys import platform


def configure_model_tls() -> None:
    if platform != 'win32':
        return
    try:
        import truststore
    except ImportError as exc:
        raise RuntimeError(
            'モデル取得用の証明書ライブラリがありません。'
            '「一括セットアップ…」で利用する認識方式を再セットアップしてください。'
        ) from exc
    # This runs in the application's model worker before any network request.
    # Requests/urllib3 and HTTPX keep certificate and hostname verification while
    # using the system roots, including roots managed by Windows administrators.
    truststore.inject_into_ssl()
