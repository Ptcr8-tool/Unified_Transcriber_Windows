"""Public GitHub release checks and authenticated, bounded installer downloads.

No credentials, shell commands, release-note evaluation or arbitrary update URLs.
Automatic installation requires both integrity checks and signed build provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (HTTPSHandler, HTTPRedirectHandler, ProxyHandler,
                            Request, build_opener)

REPOSITORY = 'Ptcr8-tool/Unified_Transcriber_Windows'
RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
REPOSITORY_API = f'https://api.github.com/repos/{REPOSITORY}'
RELEASE_BASE = f'https://github.com/{REPOSITORY}/releases/'
SIGNER_WORKFLOW = f'{REPOSITORY}/.github/workflows/windows-release.yml'
METADATA_LIMIT = 1024 * 1024
CHECKSUM_LIMIT = 64 * 1024
BUNDLE_LIMIT = 4 * 1024 * 1024
INSTALLER_LIMIT = 1024 * 1024 * 1024
REQUEST_TIMEOUT = 15
TRANSFER_TIMEOUT = 600
_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_CDN_HOSTS = frozenset(('release-assets.githubusercontent.com', 'objects.githubusercontent.com'))


class UpdateError(RuntimeError):
    """An update could not be checked, downloaded or verified."""


class UpdateUnavailable(UpdateError):
    """Public release metadata is unavailable (including private repositories)."""


class UpdateSecurityError(UpdateError):
    """Untrusted, ambiguous or damaged update material was rejected."""


def parse_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdateSecurityError('バージョン表記を確認できません。')
    return tuple(int(part) for part in value.split('.'))


def is_check_due(mode: str, days: int, last_checked: str = '', *,
                 now: datetime | None = None) -> bool:
    """Check scheduling; invalid/future timestamps never suppress checks forever."""
    if mode not in ('days', 'every', 'never'):
        raise ValueError('更新確認の間隔が不正です。')
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError('更新確認の間隔は1〜365日で指定してください。')
    if mode == 'never':
        return False
    if mode == 'every' or not last_checked:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        previous = datetime.fromisoformat(last_checked.replace('Z', '+00:00'))
        if previous.tzinfo is None:
            return True
    except (AttributeError, TypeError, ValueError, OverflowError):
        return True
    return previous > current or current - previous >= timedelta(days=days)


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    release_url: str
    setup_name: str
    setup_url: str
    setup_sha256: str
    setup_size: int
    checksums_url: str
    checksums_sha256: str
    checksums_size: int
    bundle_url: str = ''
    bundle_sha256: str = ''
    bundle_size: int = 0

    @property
    def has_provenance(self) -> bool:
        return bool(self.bundle_url)


@dataclass(frozen=True)
class VerifiedUpdate:
    info: UpdateInfo
    path: Path
    bundle_path: Path
    provenance_verified: bool


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateSecurityError('更新情報に重複した項目があります。')
        result[key] = value
    return result


def _load_json(data: bytes):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_json_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise UpdateSecurityError('更新情報の形式が不正です。') from exc


def _url_parts(url: str):
    if (not isinstance(url, str) or not url or len(url) > 8192 or
            any(ord(c) <= 32 or ord(c) >= 127 for c in url) or '\\' in url):
        raise UpdateSecurityError('更新先URLが不正です。')
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or
                parts.netloc != parts.hostname or parts.username or parts.password or
                parts.port not in (None, 443) or parts.fragment):
            raise ValueError('Unsafe URL')
    except ValueError as exc:
        raise UpdateSecurityError('HTTPS以外または不正な更新先を拒否しました。') from exc
    return parts


def _validate_asset_url(url: str, tag: str, filename: str) -> None:
    _url_parts(url)
    expected = f'{RELEASE_BASE}download/{tag}/{filename}'
    if url != expected:
        raise UpdateSecurityError('指定リポジトリ以外の更新ファイルを拒否しました。')


class _SafeRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def __init__(self, original_url: str, *, asset: bool):
        self.original_url, self.asset = original_url, asset

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parts = _url_parts(newurl)
        if not self.asset:
            if newurl != self.original_url:
                raise UpdateSecurityError('更新APIの転送先を拒否しました。')
        elif newurl != self.original_url and parts.hostname not in _CDN_HOSTS:
            raise UpdateSecurityError('更新ファイルの転送先を拒否しました。')
        # Construct a clean GET instead of copying any ambient authentication.
        return Request(newurl, headers={'User-Agent': 'UnifiedTranscriber-Updater',
                                       'Accept': 'application/octet-stream',
                                       'Accept-Encoding': 'identity'})


def _opener(url: str, *, asset: bool):
    return build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()),
                        _SafeRedirects(url, asset=asset))


def _read_response(url: str, limit: int, *, asset: bool, opener=None,
                   destination: Path | None = None, expected_size: int | None = None) -> bytes:
    """Read bounded bytes with TLS verification; never use an ambient proxy/token."""
    _url_parts(url)
    request = Request(url, headers={
        'User-Agent': 'UnifiedTranscriber-Updater',
        'Accept': 'application/octet-stream' if asset else 'application/vnd.github+json',
        'Accept-Encoding': 'identity',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    transport = opener or _opener(url, asset=asset)
    started = time.monotonic()
    try:
        with transport.open(request, timeout=REQUEST_TIMEOUT) as response:
            status = getattr(response, 'status', 200)
            if status != 200:
                raise UpdateUnavailable('更新情報を取得できませんでした。')
            if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise UpdateSecurityError('圧縮された更新レスポンスを拒否しました。')
            final_url = response.geturl()
            final = _url_parts(final_url)
            if final_url != url and (not asset or final.hostname not in _CDN_HOSTS):
                raise UpdateSecurityError('予期しない更新先へ転送されました。')
            length = response.headers.get('Content-Length')
            if length is not None:
                try:
                    parsed_length = int(length)
                except (ValueError, TypeError) as exc:
                    raise UpdateSecurityError('更新ファイルのサイズが不正です。') from exc
                if parsed_length < 0 or parsed_length > limit or (
                        expected_size is not None and parsed_length != expected_size):
                    raise UpdateSecurityError('更新ファイルのサイズが一致しません。')
            total = 0
            chunks = []
            output = destination.open('xb') if destination else None
            try:
                while True:
                    if time.monotonic() - started > TRANSFER_TIMEOUT:
                        raise UpdateError('更新ファイルの受信が時間内に完了しませんでした。')
                    chunk = response.read(min(1024 * 1024, limit - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit or (expected_size is not None and total > expected_size):
                        raise UpdateSecurityError('更新ファイルが許可サイズを超えています。')
                    if output:
                        output.write(chunk)
                    else:
                        chunks.append(chunk)
                if expected_size is not None and total != expected_size:
                    raise UpdateSecurityError('更新ファイルの受信が途中で終了しました。')
                if output:
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if output:
                    output.close()
            return b''.join(chunks)
    except HTTPError as exc:
        if exc.code == 404:
            raise UpdateUnavailable('公開された更新情報がありません。現在は非公開、または未公開の可能性があります。') from exc
        raise UpdateUnavailable('GitHubから更新情報を取得できませんでした。時間をおいて再試行してください。') from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            detail = 'TLS証明書を検証できませんでした。PCの日時・証明書・ネットワーク管理設定を確認してください。'
        elif isinstance(reason, TimeoutError):
            detail = '接続がタイムアウトしました。接続を確認して再試行してください。'
        else:
            # Remote/proxy exception text can contain credentials or private
            # URLs. Record the category, never its arbitrary message.
            detail = 'ネットワークの接続・プロキシ設定を確認してください。'
        raise UpdateUnavailable('更新サーバーに接続できませんでした。'+detail+
            ' 非公開リポジトリでは自動更新を取得できません。GitHubのリリースから手動で更新できます。') from exc


def _asset(assets: list, name: str, tag: str, limit: int, *, required: bool = True):
    matching = [item for item in assets if isinstance(item, dict) and item.get('name') == name]
    if not matching and not required:
        return None
    if len(matching) != 1:
        raise UpdateSecurityError('更新ファイルが不足しているか、重複しています。')
    item = matching[0]
    size, digest, url = item.get('size'), item.get('digest'), item.get('browser_download_url')
    if (item.get('state') != 'uploaded' or type(size) is not int or not 1 <= size <= limit or
            not isinstance(digest, str) or not digest.startswith('sha256:') or
            not _SHA256.fullmatch(digest[7:])):
        raise UpdateSecurityError('更新ファイルの検証情報が不正です。')
    _validate_asset_url(url, tag, name)
    return url, digest[7:], size


def parse_release(data: dict, current_version: str) -> UpdateInfo | None:
    current = parse_version(current_version)
    if (not isinstance(data, dict) or data.get('draft') is not False or
            data.get('prerelease') is not False):
        raise UpdateSecurityError('正式リリース以外の更新情報を拒否しました。')
    tag = data.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise UpdateSecurityError('リリースのバージョンが不正です。')
    version = tag[1:]
    candidate = parse_version(version)
    release_url = data.get('html_url')
    if release_url != f'{RELEASE_BASE}tag/{tag}':
        raise UpdateSecurityError('指定リポジトリ以外のリリースを拒否しました。')
    if candidate <= current:
        return None
    assets = data.get('assets')
    if not isinstance(assets, list) or len(assets) > 100:
        raise UpdateSecurityError('リリースのファイル一覧が不正です。')
    setup_name = f'Unified_Transcriber_Setup_v{version}.exe'
    setup = _asset(assets, setup_name, tag, INSTALLER_LIMIT)
    checksums = _asset(assets, 'SHA256SUMS.txt', tag, CHECKSUM_LIMIT)
    bundle = _asset(assets, setup_name + '.sigstore.json', tag, BUNDLE_LIMIT, required=False)
    return UpdateInfo(version, tag, release_url, setup_name, *setup, *checksums,
                      *(bundle or ('', '', 0)))


def check_latest(current_version: str, *, opener=None) -> UpdateInfo | None:
    """Look up the public stable release without any GitHub authentication."""
    parse_version(current_version)
    return parse_release(_load_json(_read_response(RELEASE_API, METADATA_LIMIT,
                                                   asset=False, opener=opener)), current_version)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_checksum(data: bytes, filename: str) -> str:
    try:
        text = data.decode('ascii')
    except UnicodeError as exc:
        raise UpdateSecurityError('チェックサムの文字コードが不正です。') from exc
    values = {}
    for line in text.splitlines():
        if not line:
            continue
        match = re.fullmatch(r'([0-9a-f]{64}) [ *]([A-Za-z0-9_.-]{1,200})', line)
        if not match or match[2] in values:
            raise UpdateSecurityError('チェックサムの形式が不正、または重複しています。')
        values[match[2]] = match[1]
    if filename not in values:
        raise UpdateSecurityError('セットアップのチェックサムがありません。')
    return values[filename]


def _trust_manifest():
    base = Path(__file__).resolve().parent / 'update-verifier'
    manifest_path = base / 'manifest.json'
    if (manifest_path.is_symlink() or not manifest_path.is_file() or
            manifest_path.stat().st_size > CHECKSUM_LIMIT):
        raise UpdateSecurityError('更新の署名検証ツールがありません。公式セットアップで修復してください。')
    data = _load_json(manifest_path.read_bytes())
    if not isinstance(data, dict):
        raise UpdateSecurityError('署名検証ツールの情報が不正です。')
    for name, key in (('gh.exe', 'gh_sha256'), ('trusted_root.jsonl', 'trusted_root_sha256')):
        path = base / name
        digest = data.get(key)
        if (path.is_symlink() or not path.is_file() or not isinstance(digest, str) or
                not _SHA256.fullmatch(digest) or _sha256(path) != digest):
            raise UpdateSecurityError('署名検証ツールの整合性を確認できません。公式セットアップで修復してください。')
    return base, data


def verify_github_provenance(installer: Path, bundle: Path, info: UpdateInfo) -> None:
    """Verify Sigstore certificate, hosted workflow and tag using a pinned verifier.

    The bundled CLI and trust roots are fixed by the installed application's
    build manifest. No executable is resolved from PATH; no personal gh config,
    token, proxy, git config or plugin is inherited by the verifier process.
    """
    base, manifest = _trust_manifest()
    repository_id = manifest.get('repository_id')
    if type(repository_id) is not int or repository_id <= 0:
        raise UpdateSecurityError('更新元リポジトリの識別情報がありません。')
    identity = _load_json(_read_response(REPOSITORY_API, METADATA_LIMIT, asset=False))
    if (not isinstance(identity, dict) or identity.get('id') != repository_id or
            identity.get('full_name') != REPOSITORY or identity.get('private') is not False):
        raise UpdateSecurityError('更新元リポジトリの同一性を確認できません。')
    with tempfile.TemporaryDirectory(prefix='transcriber-verify-') as private_config:
        allowed = ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LOCALAPPDATA')
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update(GH_CONFIG_DIR=private_config, GH_NO_UPDATE_NOTIFIER='1',
                   GH_NO_EXTENSION_UPDATE_NOTIFIER='1', GH_PROMPT_DISABLED='1',
                   GH_HOST='github.com')
        command = [str(base/'gh.exe'), 'attestation', 'verify', str(installer),
                   '--bundle', str(bundle), '--custom-trusted-root', str(base/'trusted_root.jsonl'),
                   '--repo', REPOSITORY, '--signer-workflow', SIGNER_WORKFLOW,
                   '--source-ref', f'refs/tags/{info.tag}', '--deny-self-hosted-runners',
                   '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com',
                   '--format', 'json']
        if os.name == 'nt':
            from portable_runtime import reset_dll_search_path
            reset_dll_search_path()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        try:
            result = subprocess.run(command, shell=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=90, check=False, env=env, cwd=private_config,
                                    creationflags=flags)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateSecurityError('更新の署名検証を完了できませんでした。') from exc
        if result.returncode or len(result.stdout) > BUNDLE_LIMIT:
            raise UpdateSecurityError('更新の署名または配布元を確認できません。自動更新を中止しました。')
        verified = _load_json(result.stdout)
        if not isinstance(verified, list) or not verified:
            raise UpdateSecurityError('署名検証の結果が空です。')
        # gh enforces identity / workflow / source ref and all cryptographic
        # checks. Also require a witnessed time and the exact artifact digest.
        for item in verified:
            result = item.get('verificationResult', {}) if isinstance(item, dict) else {}
            if not isinstance(result, dict):
                continue
            signature = result.get('signature')
            certificate = signature.get('certificate') if isinstance(signature, dict) else None
            if not isinstance(certificate, dict):
                continue
            if str(certificate.get('sourceRepositoryIdentifier', '')) != str(repository_id):
                continue
            statement = result.get('statement')
            subjects = statement.get('subject') if isinstance(statement, dict) else None
            if not isinstance(subjects, list):
                continue
            if result.get('verifiedTimestamps') and any(
                    isinstance(subject, dict) and isinstance(subject.get('digest'), dict) and
                    subject['digest'].get('sha256') == info.setup_sha256
                    for subject in subjects):
                return
        raise UpdateSecurityError('署名に含まれる更新ファイルの情報が一致しません。')


def download_verified(info: UpdateInfo, directory: Path, *, opener=None,
                      provenance_verifier: Callable[[Path, Path, UpdateInfo], None] | None = None) -> VerifiedUpdate:
    """Return an installer only after hash AND cryptographic provenance checks."""
    # Revalidate the whole immutable object, including objects created by callers.
    parse_version(info.version)
    if info.tag != 'v'+info.version or info.setup_name != f'Unified_Transcriber_Setup_v{info.version}.exe':
        raise UpdateSecurityError('更新情報のバージョンが一致しません。')
    if not info.has_provenance:
        raise UpdateSecurityError('この更新には署名付きビルド証明がありません。自動更新は実行しません。')
    for url, name, digest, size, limit in (
            (info.setup_url, info.setup_name, info.setup_sha256, info.setup_size, INSTALLER_LIMIT),
            (info.checksums_url, 'SHA256SUMS.txt', info.checksums_sha256, info.checksums_size, CHECKSUM_LIMIT),
            (info.bundle_url, info.setup_name+'.sigstore.json', info.bundle_sha256, info.bundle_size, BUNDLE_LIMIT)):
        _validate_asset_url(url, info.tag, name)
        if (not isinstance(digest, str) or not _SHA256.fullmatch(digest) or
                type(size) is not int or not 1 <= size <= limit):
            raise UpdateSecurityError('更新ファイルの検証情報が不正です。')
    directory = Path(directory)
    if directory.is_symlink():
        raise UpdateSecurityError('更新保存先にリンクは使用できません。')
    directory.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='verified-update-', dir=directory))
    installer = work / info.setup_name
    bundle = work / (info.setup_name+'.sigstore.json')
    try:
        checksums = _read_response(info.checksums_url, CHECKSUM_LIMIT, asset=True,
                                   opener=opener, expected_size=info.checksums_size)
        if (hashlib.sha256(checksums).hexdigest() != info.checksums_sha256 or
                _expected_checksum(checksums, info.setup_name) != info.setup_sha256):
            raise UpdateSecurityError('更新のチェックサムが一致しません。')
        _read_response(info.setup_url, INSTALLER_LIMIT, asset=True, opener=opener,
                       destination=installer, expected_size=info.setup_size)
        if _sha256(installer) != info.setup_sha256:
            raise UpdateSecurityError('セットアップファイルが破損しているか、変更されています。')
        with installer.open('rb') as stream:
            if stream.read(2) != b'MZ':
                raise UpdateSecurityError('Windowsセットアップ形式ではありません。')
        _read_response(info.bundle_url, BUNDLE_LIMIT, asset=True, opener=opener,
                       destination=bundle, expected_size=info.bundle_size)
        if _sha256(bundle) != info.bundle_sha256:
            raise UpdateSecurityError('ビルド証明のチェックサムが一致しません。')
        (provenance_verifier or verify_github_provenance)(installer, bundle, info)
        # Detect accidental modification while the verifier was running.
        if _sha256(installer) != info.setup_sha256:
            raise UpdateSecurityError('検証中にセットアップファイルが変更されました。')
        return VerifiedUpdate(info, installer, bundle, True)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def launch_installer(update: VerifiedUpdate, target: Path, *, wait_pid: int | None = None):
    """Launch a verified setup; caller must obtain the user's update choice first."""
    path = Path(update.path)
    if (not update.provenance_verified or path.is_symlink() or not path.is_file() or
            path.name != update.info.setup_name or _sha256(path) != update.info.setup_sha256):
        raise UpdateSecurityError('更新の検証状態を確認できません。')
    target = Path(target).absolute()
    if '\0' in str(target):
        raise UpdateSecurityError('インストール先が不正です。')
    command = [str(path.absolute()), '--target', str(target), '--update']
    if wait_pid is not None:
        if type(wait_pid) is not int or wait_pid <= 0:
            raise ValueError('待機するプロセス番号が不正です。')
        command.extend(('--wait-pid', str(wait_pid)))
    allowed = ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA',
               'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'COMSPEC',
               'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'PATH')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    if os.name == 'nt':
        # A frozen parent must not redirect DLL loading in the new executable.
        from portable_runtime import reset_dll_search_path
        reset_dll_search_path()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    return subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=str(path.parent), creationflags=flags, env=env)
