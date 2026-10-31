"""Windows Credential Manager smoke test using only a unique dummy credential.

No third-party packages, real tokens, GUI libraries, or permanent target names.
Run on an isolated Windows runner: python -I _internal/tools/windows_credential_smoke.py
"""
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import uuid

if sys.platform != 'win32':
    raise SystemExit('Windows is required.')
module_path = Path(__file__).resolve().parent.parent / 'hf_auth.py'
spec = importlib.util.spec_from_file_location('hf_auth_smoke', module_path)
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)

TEST_PREFIX = 'UnifiedTranscriber/Tests/'
SECRET_A = 'hf_dummy_sample_A_not_a_token'
SECRET_B = 'hf_dummy_sample_B_not_a_token'

if len(sys.argv) == 2 and sys.argv[1] == '--assert-lock-held':
    try:
        with auth.credential_guard(timeout_ms=1000):
            raise SystemExit('Another process entered the held credential transaction.')
    except OSError as exc:
        if '別のアプリが資格情報を操作中' not in str(exc):
            raise
    raise SystemExit(0)

if len(sys.argv) == 3 and sys.argv[1] == '--read-back':
    target = sys.argv[2]
    if not re.fullmatch(re.escape(TEST_PREFIX) + r'[0-9a-f]{32}', target):
        raise SystemExit('Invalid test target.')
    auth._TARGET = target
    if auth.read_credential() != SECRET_B:
        raise SystemExit('Cross-process read failed.')
    raise SystemExit(0)

if len(sys.argv) != 1:
    raise SystemExit('Invalid arguments.')
auth._TARGET = TEST_PREFIX + uuid.uuid4().hex
with auth.credential_guard():
    # Proves nested acquisition and another application's exclusion, without
    # reading or writing any existing Windows credential.
    with auth.credential_guard():
        subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--assert-lock-held'],
                       check=True, timeout=30)
if auth.read_credential():
    raise SystemExit('Unique test target already existed; no modification was made.')
created = False
try:
    created = True  # Attempt cleanup even if the first write fails partway through.
    auth.write_credential(SECRET_A)
    assert auth.read_credential() == SECRET_A, 'Initial read failed.'
    auth.write_credential(SECRET_B)
    assert auth.read_credential() == SECRET_B, 'Replacement read failed.'
    subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--read-back', auth._TARGET],
                   check=True, timeout=30)
    for invalid in ('', 'line\nbreak', '\x00'):
        try:
            auth.write_credential(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid credential was accepted.')
    assert auth.read_credential() == SECRET_B, 'Invalid input changed credential.'
    auth.delete_credential()
    assert auth.read_credential() == '', 'Deletion failed.'
    auth.delete_credential()  # deleting a missing entry is idempotent
    print('Credential Manager: mutex, write, replace, cross-process read, invalid input, delete: OK')
finally:
    if created:
        auth.delete_credential()
        if auth.read_credential():
            raise RuntimeError('Test credential cleanup failed.')
