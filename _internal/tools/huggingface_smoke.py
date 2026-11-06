"""Native Windows checks for legacy setup and small public Hub downloads."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch
import warnings
import wave


CODE = Path(__file__).resolve().parents[1]
PROFILES = ('faster-pya', 'qwen-pya')
RESULT_PREFIX = 'HF_SMOKE_JSON='


class DeniedSymlinkOS:
    """Delegate OS calls while denying the Hub module's symlink requests."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def __getattr__(self, name):
        return getattr(os, name)

    def symlink(self, source, destination):
        self.calls.append(Path(destination).name)
        if Path(destination).name == 'dummy_file_dst':
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError('The cache capability fixture timed out.')
        error = OSError('The symlink privilege fixture denied this operation.')
        error.winerror = 1314
        raise error


def check_cache_denial(*, guarded: bool, new_blob: bool) -> dict:
    """Exercise Hub 0.36's real concurrent capability probe and copy fallback."""
    import huggingface_hub.file_download as download
    from hf_cache import hf_cache_probes

    denied = DeniedSymlinkOS()
    original_probe = download.are_symlinks_supported
    original_symlink = os.symlink
    content = b'small cache fixture\n'
    with tempfile.TemporaryDirectory(prefix='unified-hf-cache-') as temporary:
        repository = Path(temporary) / 'models--fixture--small'
        source = repository / 'blobs' / 'fixture'
        destination = repository / 'snapshots' / ('a' * 40) / '.gitattributes'
        source.parent.mkdir(parents=True)
        destination.parent.mkdir(parents=True)
        source.write_bytes(content)
        download._are_symlinks_supported_in_dir.clear()
        guard = hf_cache_probes() if guarded else nullcontext()
        with patch.object(download, 'os', denied), warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with guard, ThreadPoolExecutor(max_workers=2) as workers:
                probe = workers.submit(download.are_symlinks_supported, repository)
                try:
                    assert denied.started.wait(5), 'The capability probe did not start.'
                    creation = workers.submit(download._create_symlink,
                                              str(source), str(destination), new_blob)
                    if guarded:
                        try:
                            creation.result(timeout=.1)
                        except FutureTimeout:
                            pass
                        else:
                            raise AssertionError('The second worker bypassed the probe lock.')
                    else:
                        try:
                            creation.result(timeout=5)
                        except OSError as error:
                            assert error.winerror == 1314
                        else:
                            raise AssertionError('The upstream race fixture did not reproduce.')
                finally:
                    denied.release.set()
                assert probe.result(timeout=5) is False
                if guarded:
                    creation.result(timeout=5)
                    assert destination.read_bytes() == content
                    assert not destination.is_symlink()
                    assert source.exists() is (not new_blob)
                    assert denied.calls == ['dummy_file_dst']
        assert download.are_symlinks_supported is original_probe
        assert os.symlink is original_symlink
    return {'guarded': guarded, 'new_blob': new_blob,
            'result': ('move' if new_blob else 'copy') if guarded else 'WinError1314',
            'probe_restored': True, 'global_os_symlink_unchanged': True}


def install_profile_worker(target: Path, profile: str) -> None:
    from core import BASE, environment_folder
    from setup_backend import install_profile
    from setup_environment import environment_build
    from setup_process import setup_interpreter

    assert BASE.resolve() == target
    host = setup_interpreter(target, profile=profile)
    assert Path(host).resolve() == target / 'Runtime' / 'python.exe'
    with environment_build(environment_folder(profile), target, host,
                           supported=(3, 11)) as python:
        install_profile(profile, 'cpu', python)


def verify_profile_worker(target: Path, profile: str) -> dict:
    from importlib.metadata import version
    import huggingface_hub
    import torch
    import truststore
    from faster_whisper.audio import decode_audio
    from pyannote.audio import Pipeline
    from hf_transport import configure_model_tls
    from setup_progress import hf_snapshot_progress

    assert Path(sys.prefix).resolve() == target / '_internal' / 'environments' / profile
    assert huggingface_hub.__version__ == '0.36.0'
    assert torch.version.cuda is None
    assert callable(Pipeline)
    packages = ['torch', 'torchaudio', 'numpy', 'faster-whisper', 'av',
                'pyannote.audio', 'pyannote.core', 'pyannote.database',
                'pyannote.metrics', 'pyannote.pipeline', 'docopt',
                'antlr4-python3-runtime', 'huggingface-hub', 'truststore']
    if profile == 'qwen-pya':
        from qwen_asr import Qwen3ASRModel
        assert callable(Qwen3ASRModel)
        packages.append('qwen-asr')
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'check'],
                   check=True, timeout=120)
    with io.BytesIO() as sample:
        with wave.open(sample, 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b'\0\0' * 1600)
        sample.seek(0)
        assert len(decode_audio(sample, sampling_rate=16000)) == 1600

    configure_model_tls()
    context = ssl.create_default_context()
    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    cache_cases = [check_cache_denial(guarded=False, new_blob=True),
                   check_cache_denial(guarded=True, new_blob=True),
                   check_cache_denial(guarded=True, new_blob=False)]
    models = target / 'Models'
    models.mkdir(exist_ok=True)
    filenames = ('.gitattributes', 'config.json')
    with tempfile.TemporaryDirectory(prefix='small-hub-', dir=models) as temporary:
        cache = Path(temporary)
        with hf_snapshot_progress('Hugging Face smoke'):
            snapshot = Path(huggingface_hub.snapshot_download(
                repo_id='Qwen/Qwen3-ForcedAligner-0.6B', token=False,
                cache_dir=cache, allow_patterns=list(filenames), max_workers=4))
        files = []
        for name in filenames:
            path = snapshot / name
            assert path.resolve().is_relative_to(cache.resolve())
            assert path.is_file() and 0 < path.stat().st_size < 1024 * 1024
            files.append({'name': name, 'bytes': path.stat().st_size,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        assert isinstance(json.loads((snapshot / 'config.json').read_text()), dict)
        revision = snapshot.name
    return {'profile': profile, 'python': sys.version.split()[0],
            'packages': {name: version(name) for name in packages},
            'imports': ['pyannote.audio.Pipeline'] +
                       (['qwen_asr.Qwen3ASRModel'] if profile == 'qwen-pya' else []),
            'pip_check': True, 'wav_decode': True,
            'tls': {'context': 'truststore.SSLContext', 'verify_mode': 'CERT_REQUIRED',
                    'check_hostname': True},
            'cache_cases': cache_cases,
            'public_download': {'repository': 'Qwen/Qwen3-ForcedAligner-0.6B',
                                'revision': revision, 'files': files}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--dist', type=Path)
    parser.add_argument('--worker', choices=('install', 'verify'), help=argparse.SUPPRESS)
    parser.add_argument('--target', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--profile', choices=PROFILES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This integration check requires native Windows.')
    if args.worker:
        if args.target is None or args.profile is None:
            parser.error('A worker requires --target and --profile.')
        target = args.target.resolve()
        assert sys.version_info[:2] == (3, 11)
        assert Path(sys.base_prefix).resolve() == target / 'Runtime'
        sys.path.insert(0, str(target / '_internal'))
        if args.worker == 'install':
            install_profile_worker(target, args.profile)
        else:
            print(RESULT_PREFIX + json.dumps(verify_profile_worker(target, args.profile)))
        return
    if args.work is None or args.dist is None:
        parser.error('--work and --dist are required.')
    sys.path.insert(0, str(CODE))
    from installation import install_package
    from windows_smoke import no_host_python_environment, run, sha256

    info = json.loads((args.work / 'BUILD_INFO.json').read_text(encoding='utf-8'))
    payload = (args.work / 'payload.zip').resolve()
    assert sha256(payload) == info['payload_sha256']
    report = {'version': info['version'], 'source_commit': info['source_commit'],
              'payload_sha256': info['payload_sha256'], 'status': 'running', 'profiles': []}
    try:
        with tempfile.TemporaryDirectory(prefix='unified-hf-smoke-') as temporary:
            target = Path(temporary).resolve() / 'Application'
            installed = install_package(payload, target, info['version'],
                                        expected_sha256=info['payload_sha256'])
            assert not installed.warnings, installed.warnings
            environment = no_host_python_environment(target)
            environment.update(HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                               HF_HUB_DISABLE_TELEMETRY='1')
            script = Path(__file__).resolve()
            for profile in PROFILES:
                print(f'[Hugging Face smoke] Install {profile} CPU dependencies', flush=True)
                run([target / 'Runtime' / 'python.exe', '-I', '-X', 'utf8', script,
                     '--worker', 'install', '--target', target, '--profile', profile],
                    timeout=1200, target=target, extra_env=environment)
                python = target / '_internal' / 'environments' / profile / 'Scripts' / 'python.exe'
                print(f'[Hugging Face smoke] Verify {profile} imports, TLS and cache', flush=True)
                result = run([python, '-I', '-X', 'utf8', script, '--worker', 'verify',
                              '--target', target, '--profile', profile],
                             timeout=180, target=target, extra_env=environment)
                records = [line.removeprefix(RESULT_PREFIX) for line in result.stdout.splitlines()
                           if line.startswith(RESULT_PREFIX)]
                assert len(records) == 1, 'The profile probe must return one report.'
                report['profiles'].append(json.loads(records[0]))
        report['status'] = 'passed'
    except Exception:
        report['status'] = 'failed'
        raise
    finally:
        args.dist.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report, indent=2) + '\n'
        (args.dist / 'HUGGINGFACE_SMOKE.json').write_text(text, encoding='utf-8')
        print(text, flush=True)


if __name__ == '__main__':
    main()
