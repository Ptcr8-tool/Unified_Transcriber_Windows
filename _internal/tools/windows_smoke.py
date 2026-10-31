"""Windows runner checks for the actual NSIS installer and native launcher.

All installation/uninstallation targets are created below a fresh temporary
directory. The runner account's HKCU entry and Start Menu shortcut use the
temporary installation's unique ID and are removed in a finally block. No
audio devices are used. The minimal CPU dependency environment loads its small
bundled speaker checkpoint; the large ASR model is deliberately not downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import time
import zipfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from installation import install_package, inspect_installation, uninstall_installation
import installation
from portable_runtime import runtime_environment
from secure_media import (
    VERSION as MEDIA_VERSION, DOWNLOAD_URL as MEDIA_DOWNLOAD_URL,
    ARCHIVE_SHA256 as MEDIA_ARCHIVE_SHA256, BINARY_SHA256 as MEDIA_BINARY_SHA256,
    verified_media_binary,
)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def child_environment(target=None):
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    for key in tuple(env):
        if (key.upper().startswith(('GITHUB_', 'GH_', 'ACTIONS_', 'PIP_'))
                or key.upper() in ('PYTHONHOME', 'PYTHONPATH', 'HF_TOKEN')):
            env.pop(key, None)
    if target is not None:
        env = runtime_environment(target, env)
    return env


def no_host_python_environment(target: Path) -> dict[str, str]:
    """Expose only application runtimes and Windows' own OS utilities on PATH.

    The CI runner happens to have many Python installations. Removing all of
    their directories from these probes prevents a missing bundled component
    from silently borrowing the runner's interpreter, pip or native libraries.
    """
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    folders = [target / 'Runtime', target / 'MLRuntime', system]
    return {'PATH': os.pathsep.join(str(folder) for folder in folders)}


def run(args, timeout=180, target=None, extra_env=None):
    environment = child_environment(target)
    if extra_env:
        environment.update(extra_env)
    try:
        return subprocess.run([str(arg) for arg in args], env=environment, check=True,
            timeout=timeout, capture_output=True, text=True, encoding='utf-8', errors='replace')
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Windows integration command failed:\n' +
            (exc.stdout or '')[-12000:] + '\n' + (exc.stderr or '')[-12000:]) from exc


def check_nsis_probe(setup: Path, arguments: list[str | Path], result_path: Path,
                     *, extra_env: dict[str, str] | None = None) -> dict:
    """Bind a real installer diagnostic to its directly spawned NSIS process.

    The Python wizard lives in a private NSIS extraction directory. Its own
    interpreter path is therefore not the installed maintenance executable.
    Verify the origin and owning process reported by the checked bootstrap,
    rather than silently accepting a successful Python-only diagnostic.
    """
    result_path.unlink(missing_ok=True)
    environment = child_environment()
    if extra_env:
        environment.update(extra_env)
    process = subprocess.Popen([str(setup), *map(str, arguments)], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding='utf-8', errors='replace')
    try:
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError('The actual NSIS installer diagnostic timed out.') from None
    if process.returncode != 0:
        raise RuntimeError('The actual NSIS installer diagnostic failed:\n' +
                           stdout[-12000:] + '\n' + stderr[-12000:])
    if not result_path.is_file() or result_path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError('The actual NSIS installer did not produce a bounded diagnostic report.')
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if (not isinstance(result, dict) or result.get('ok') is not True
            or result.get('packaging') != 'nsis'
            or result.get('setup_parent_verified') is not True
            or result.get('setup_owner_pid') != process.pid):
        raise RuntimeError('The installer diagnostic is not bound to the actual NSIS parent: ' +
                           json.dumps(result, ensure_ascii=False)[:12000])
    origin = Path(result.get('setup_executable', ''))
    if not origin.is_file() or not origin.samefile(setup):
        raise RuntimeError('The installer bootstrap accepted a different executable as its origin.')
    resource_root = Path(result.get('setup_resource_root', ''))
    if (not resource_root.is_absolute() or resource_root == setup.parent
            or resource_root.name.lower().startswith('_mei')):
        raise RuntimeError('The installer must use its own NSIS resources, without a PyInstaller runtime.')
    return result


def check_nsis_installer(setup: Path, sandbox: Path, report: dict,
                         runtime_identity: dict) -> None:
    from nsis_builder import inspect_bootstrap
    print('[Windows smoke] Actual NSIS GUI, icons, verified origin and native runtime', flush=True)
    report['installer_pe'] = inspect_bootstrap(setup)
    run([setup, '--version'], extra_env=no_host_python_environment(sandbox))
    result_path = sandbox/'installer-self-test.json'
    hostile = sandbox/'Untrusted interpreter and Tcl 日本語'
    hostile.mkdir()
    (hostile/'sitecustomize.py').write_text('raise RuntimeError("Untrusted Python startup executed")\n', encoding='utf-8')
    (hostile/'init.tcl').write_text('error "Untrusted Tcl startup executed"\n', encoding='utf-8')
    (hostile/'tk.tcl').write_text('error "Untrusted Tk startup executed"\n', encoding='utf-8')
    result = check_nsis_probe(setup,
        ['--self-test-gui', '--self-test-report', result_path], result_path,
        extra_env={**no_host_python_environment(sandbox),
            'PYTHONHOME': str(hostile), 'PYTHONPATH': str(hostile),
            'TCL_LIBRARY': str(hostile), 'TK_LIBRARY': str(hostile), 'TCLLIBPATH': str(hostile),
            'UNIFIED_SETUP_ORIGIN': str(hostile/'untrusted.exe'),
            'UNIFIED_SETUP_PARENT_PID': '1',
            'UNIFIED_SETUP_COMMAND_LINE': 'untrusted.exe --unexpected-bootstrap-spoof'})
    assert result['gui_ok'] is True and result['icons_ok'] is True and result['controls_ok'] is True
    assert result['python_version'] == runtime_identity['version']
    assert result['tcl_version'] == runtime_identity['tk']
    assert result['tk_version'] == runtime_identity['tk']
    assert result['selected_profile'] == 'faster-res'
    assert result['selected_model'] == 'large-v3-turbo'
    assert result['cuda_probe_started'] is True
    assert Path(result['output_root']).is_absolute()
    assert result['setup_parent_window_count'] >= 1, 'The real NSIS parent window was not inspected'
    assert result['setup_parent_visible_window_count'] == 0, 'The NSIS progress window remained visible under the wizard'
    report['installer_runtime'] = {name: result[name] for name in
        ('python_version', 'tcl_version', 'tk_version', 'packaging', 'setup_parent_verified')}
    report['installer_setup_defaults'] = {name: result[name] for name in
        ('selected_profile', 'selected_model', 'cuda_probe_started', 'output_root')}
    report['checks'].append('The actual NSIS executable validates its payload, verifies its direct parent/origin and creates the real wizard/icons with the build-pinned Python/Tcl/Tk runtime')
    report['checks'].append('The real setup wizard recommends profile ②, initially selects large-v3-turbo, starts the CUDA assessment and exposes an absolute shared output folder')
    report['checks'].append('The real NSIS extraction window is hidden while its child setup wizard is open')
    report['checks'].append('Inherited Python/Tcl startup paths and spoofed bootstrap environment values cannot replace the NSIS origin or GUI runtime')

    # NSIS script strings are traditionally bounded. The bootstrap receives the
    # complete original command line through a dedicated environment value,
    # then parses it with Windows' argument parser. This target is diagnostic
    # data only; no directory with this deliberately long name is ever created.
    long_target = 'C:\\Sample 日本語 & audio\\' + 'x' * 3000
    long_result_path = sandbox/'installer-long-arguments.json'
    long_result = check_nsis_probe(setup,
        ['--self-test', '--target', long_target, '--self-test-report', long_result_path],
        long_result_path)
    assert long_result.get('requested_target') == long_target, 'The NSIS bootstrap truncated or changed the original arguments'
    report['nsis_argument_roundtrip'] = {'target_characters': len(long_target),
                                        'spaces_unicode_ampersand': True}
    report['checks'].append('NSIS preserves a command-line target exceeding 3,000 characters, including spaces, Japanese text and ampersands, without creating that diagnostic target')


def check_legacy_layout_upgrade(payload: Path, setup: Path, version: str,
                                sandbox: Path, report: dict) -> None:
    """Migrate a synthetic older managed layout; this is not an old binary.

    Reuse reviewed program bytes, changing only version metadata in a disposable
    fixture. No old downloaded executable is run, and no large AI environment
    has to be installed twice to prove preservation of user-owned paths.
    """
    old_version = '0.9.0'
    if installation.version_tuple(version) <= installation.version_tuple(old_version):
        raise RuntimeError('The migration smoke requires a release newer than the synthetic fixture.')
    print('[Windows smoke] Synthetic older managed-layout fixture and retained user data', flush=True)
    legacy_payload = sandbox/'legacy-fixture-layout.zip'
    with zipfile.ZipFile(payload) as source, zipfile.ZipFile(legacy_payload, 'w') as output:
        for original in source.infolist():
            info = zipfile.ZipInfo(original.filename, original.date_time)
            info.external_attr = original.external_attr
            info.compress_type = zipfile.ZIP_STORED
            if original.filename == 'VERSION':
                output.writestr(info, old_version+'\n')
            elif original.filename == '_internal/core.py':
                code = source.read(original)
                before = f"APP_VERSION = '{version}'".encode('ascii')
                assert code.count(before) == 1, 'Expected exactly one application version assignment'
                output.writestr(info, code.replace(before, f"APP_VERSION = '{old_version}'".encode('ascii')))
            else:
                with source.open(original) as incoming, output.open(info, 'w') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024*1024)
    target = sandbox/'Legacy Installation 日本語'
    old_install_id = None
    try:
        install_package(legacy_payload, target, old_version,
            expected_sha256=sha256(legacy_payload),
            extra_files={'UnifiedTranscriberSetup.exe': setup})
        old_install_id = inspect_installation(target).install_id
        markers = {
            'Data/settings.json': b'{"theme_mode":"dark","future_setting":{"preserved":true}}\n',
            'Models/sample-model.bin': b'model preserved\x00\x01',
            '_internal/environments/faster-res/fixture.bin': b'environment preserved',
            'Recordings/sample-recording.wav': b'recording preserved',
            'Transcriptions/sample-transcript.txt': '文字起こし保持\n'.encode('utf-8'),
        }
        for name, value in markers.items():
            path = target/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        # A new source code field must not cause the installer to reserialize
        # or reject settings created by another version.
        result = install_package(payload, target, version, expected_sha256=sha256(payload),
            extra_files={'UnifiedTranscriberSetup.exe': setup})
        assert not result.warnings, result.warnings
        assert result.mode == 'upgrade'
        assert inspect_installation(target).install_id == old_install_id
        assert all((target/name).read_bytes() == value for name, value in markers.items())
        assert (target/'VERSION').read_text(encoding='utf-8').strip() == version
        receipt = json.loads((target/installation.RECEIPT_NAME).read_text(encoding='utf-8'))
        assert receipt['managed']['UnifiedTranscriberSetup.exe'] == sha256(setup)
        assert sha256(installation.get_verified_maintenance_path(target)) == sha256(setup)
        report['legacy_layout_migration'] = {'from_version': old_version, 'to_version': version,
            'fixture_only': True, 'old_published_executable_exercised': False,
            'preserved_paths': list(markers), 'install_id_preserved': True,
            'maintenance_matches_new_distribution': True}
        report['checks'].append('A synthetic older managed-layout fixture upgrades to the new release while preserving settings (including unknown fields), models, environments, recordings, transcripts and install identity; the maintenance copy matches the new NSIS distribution')
    finally:
        if target.exists() and (target/installation.RECEIPT_NAME).is_file():
            uninstall_installation(target, remove_data=True)
        legacy_payload.unlink(missing_ok=True)


MEDIA_FFMPEG_TESTS = (
    'test_v16_recording_formats_and_paths.RecordingCodecTests.test_end_to_end_encoding_ffmpeg',
    'test_v21_screen_autotranscribe.StreamInspectionTests.test_real_audio_and_silent_video',
    'test_v21_screen_autotranscribe.StreamInspectionTests.test_invalid_media_raises_clear_error',
    'test_v23_standard_capture.ExternalCaptureTests.test_actual_ffmpeg_mux_preserves_video_and_originals',
    'test_v23_standard_capture.ExternalCaptureTests.test_failed_mux_keeps_original_files_and_no_output',
    'test_v23_standard_capture.EndToEndOsCaptureTest.test_real_video_with_audio_to_txt_csv_without_reencoding',
    'test_v27_audio.AudioChannelsTests.test_lossless_encoding_keeps_lr_order_and_isolation',
    'test_v30_changes.ArchiveTests.test_audio_commits_and_archives_before_followup_transcription',
)
MEDIA_FFPROBE_TESTS = (
    'test_merge_v10.RealAudioConcatTest.test_wav_m4a_video_to_mp3_and_metadata',
    'test_merge_v10.RealAudioConcatTest.test_wav_output_bitrate_not_requested',
    'test_merge_v10.OtherCodecIntegrationTest.test_m4a_wav_flac_outputs',
    'test_merge_v10.OtherCodecIntegrationTest.test_cancel_before_start_protects_destination',
)


def checked_media_tools(target: Path) -> dict[str, Path]:
    """Require the installed, pinned FFmpeg/FFprobe pair before execution."""
    tools = {}
    for name in ('ffmpeg', 'ffprobe'):
        verified = verified_media_binary(target, name)
        if verified is None:
            raise RuntimeError('Native media follow-up requires the verified FFmpeg/FFprobe pair')
        source = Path(verified)
        if source != target/'_internal/environments/media-tools'/f'{name}.exe':
            raise RuntimeError('Native media tools must be the verified installed pair')
        if sha256(source) != MEDIA_BINARY_SHA256[f'{name}.exe']:
            raise RuntimeError(f'Installed {name} differs from its pinned SHA256')
        tools[name] = source
    return tools


def validate_media_results(result: dict) -> None:
    """Only the complete fixed inventory, with no skips, is native coverage."""
    inventory = MEDIA_FFMPEG_TESTS + MEDIA_FFPROBE_TESTS
    expected = set(inventory)
    successes = result.get('successes', [])
    if (len(inventory) != 12 or len(expected) != 12
            or result.get('tests_run') != len(expected)
            or result.get('failures') or result.get('errors') or result.get('skipped')
            or len(successes) != len(expected) or set(successes) != expected):
        raise RuntimeError('Native media follow-up must pass all twelve FFmpeg/FFprobe tests without skips')


def check_native_media(target: Path, sandbox: Path, report: dict) -> None:
    """Exercise all codecs using only the newly installed, verified media pair."""
    print('[Windows smoke] Native FFmpeg/FFprobe media checks (12 required)', flush=True)
    sources = checked_media_tools(target)
    with tempfile.TemporaryDirectory(prefix='Media checks 日本語 ', dir=sandbox) as temporary:
        workspace = Path(temporary).resolve()
        binary_dir = workspace/'trusted tools'
        binary_dir.mkdir()
        tools = {}
        for name, source in sources.items():
            binary = binary_dir/f'{name}.exe'
            shutil.copyfile(source, binary)
            digest = sha256(binary)
            if digest != MEDIA_BINARY_SHA256[binary.name] or digest != sha256(source):
                raise RuntimeError(f'{name} smoke copy differs from the verified installed binary')
            tools[name] = dict(sha256=digest, pinned_sha256=MEDIA_BINARY_SHA256[binary.name],
                               source=str(source.relative_to(target)))
        fixtures = workspace/'Media fixtures 日本語'
        fixtures.mkdir()
        overrides = {'PATH': str(binary_dir)+os.pathsep+os.environ.get('PATH',''),
                     'TEMP': str(fixtures), 'TMP': str(fixtures), 'TMPDIR': str(fixtures)}
        for name in tools:
            version = run([binary_dir/f'{name}.exe', '-version'], extra_env=overrides).stdout.splitlines()[0]
            if not re.match(rf'^{name} version {re.escape(MEDIA_VERSION)}(?:[ -]|$)', version):
                raise RuntimeError(f'{name} native version does not match pinned {MEDIA_VERSION}: {version}')
            tools[name]['version'] = version
        # Keep the original build venv's NumPy/SciPy stack while preloading
        # installed media discovery before test modules add repository paths.
        # Decorators and direct tool calls still see verified copies on PATH.
        program = '''import hashlib,json,shutil,sys,tempfile,unittest
from pathlib import Path
code=Path(sys.argv[1]); installed_code=Path(sys.argv[6])
sys.path[:0]=[str(installed_code),str(code/'tests')]
tempfile.tempdir=sys.argv[2]
names=json.loads(sys.argv[3]); trusted=Path(sys.argv[4]); hashes=json.loads(sys.argv[5])
resolved={name:shutil.which(name) for name in ('ffmpeg','ffprobe')}
for name,path in resolved.items():
    expected=trusted/(name+'.exe')
    if path is None or Path(path).resolve()!=expected.resolve():
        raise RuntimeError(name+' must resolve to the trusted smoke copy')
    if hashlib.sha256(expected.read_bytes()).hexdigest()!=hashes[name+'.exe']:
        raise RuntimeError(name+' smoke copy does not match its pinned SHA256')
import media_tools
if Path(media_tools.__file__).resolve().parent!=installed_code.resolve():
    raise RuntimeError('Native media discovery must load from the installed application')
installed_media_paths={}
for name in ('ffmpeg','ffprobe'):
    path=media_tools.media_binary(name)
    expected=installed_code/'environments/media-tools'/(name+'.exe')
    if path is None or Path(path).resolve()!=expected.resolve():
        raise RuntimeError(name+' discovery must return the verified installed binary')
    installed_media_paths[name]=path
class Results(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); self.successes=[]
    def addSuccess(self,test):
        super().addSuccess(test); self.successes.append(test.id())
suite=unittest.defaultTestLoader.loadTestsFromNames(names)
result=unittest.TextTestRunner(verbosity=2,resultclass=Results).run(suite)
print('MEDIA_SMOKE_JSON='+json.dumps(dict(tests_run=result.testsRun,
    successes=result.successes,skipped=[dict(test=test.id(),reason=reason) for test,reason in result.skipped],
    failures=[test.id() for test,_ in result.failures],errors=[test.id() for test,_ in result.errors],
    ffprobe_available=bool(resolved['ffprobe']),trusted_media_paths=resolved,
    installed_media_module=str(Path(media_tools.__file__).resolve()),
    installed_media_paths=installed_media_paths)))
'''
        checked = run([sys.executable, '-I', '-X', 'utf8', '-c', program, CODE, fixtures,
                       json.dumps(MEDIA_FFMPEG_TESTS+MEDIA_FFPROBE_TESTS), binary_dir,
                       json.dumps(dict(MEDIA_BINARY_SHA256)), target/'_internal'],
                      timeout=300, extra_env=overrides)
        rows = [line.removeprefix('MEDIA_SMOKE_JSON=') for line in checked.stdout.splitlines()
                if line.startswith('MEDIA_SMOKE_JSON=')]
        if len(rows) != 1:
            raise RuntimeError('Native media follow-up did not produce its result')
        result = json.loads(rows[0])
        result.update(pinned_media_version=MEDIA_VERSION, media_source_url=MEDIA_DOWNLOAD_URL,
                      media_archive_sha256=MEDIA_ARCHIVE_SHA256, media_tools=tools,
                      ffmpeg_version=tools['ffmpeg']['version'], ffmpeg_sha256=tools['ffmpeg']['sha256'],
                      ffmpeg_source=tools['ffmpeg']['source'],
                      ffprobe_version=tools['ffprobe']['version'], ffprobe_sha256=tools['ffprobe']['sha256'],
                      ffprobe_source=tools['ffprobe']['source'],
                      tests_required=12, tests_passed=len(result['successes']), tests_skipped=len(result['skipped']),
                      required_ffmpeg_passed=len(set(result['successes']) & set(MEDIA_FFMPEG_TESTS)),
                      required_ffprobe_passed=len(set(result['successes']) & set(MEDIA_FFPROBE_TESTS)),
                      fixture_parent_contains_spaces_and_unicode=True)
        report['native_media_followup'] = result
        try:
            validate_media_results(result)
        except RuntimeError as exc:
            raise RuntimeError(str(exc)+'\n'+checked.stderr[-12000:]) from exc
        report['checks'].append('All twelve real FFmpeg/FFprobe codec/mux/archive/channel tests pass without skips in paths containing spaces and Unicode, using the verified pinned media pair')


def check_nsis_maintenance(target: Path, sandbox: Path, report: dict) -> None:
    """Prove the owning NSIS process is waited on, without uninstalling data."""
    print('[Windows smoke] Installed NSIS maintenance relaunch', flush=True)
    result_path = sandbox/'nsis-maintenance-relaunch.json'
    receipt = (target/installation.RECEIPT_NAME).read_bytes()
    core_digest = sha256(target/'_internal/core.py')
    process = subprocess.Popen([str(target/'UnifiedTranscriberSetup.exe'),
        '--self-test-relaunch', '--target', str(target), '--self-test-report', str(result_path)],
        env=child_environment(target), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert process.wait(timeout=180) == 0, 'Installed setup did not hand off to its outside maintenance helper'
    deadline = time.monotonic()+120
    while not result_path.is_file() and time.monotonic()<deadline:
        time.sleep(.2)
    assert result_path.is_file(), 'Outside maintenance helper did not finish its nondestructive self-test'
    result = json.loads(result_path.read_text(encoding='utf-8'))
    assert result['ok'] is True, 'Outside maintenance self-test: '+json.dumps(result,ensure_ascii=False)
    assert result['packaging'] == 'nsis' and result['setup_parent_verified'] is True
    assert result['copied_outside'] is True
    assert result['installed_setup_copy_verified'] is True
    assert result['waited_setup_pid'] == process.pid, 'Maintenance helper must wait for the owning NSIS process'
    assert result['setup_owner_pid'] != process.pid, 'Outside maintenance must own a new NSIS extraction and process'
    assert not Path(result['setup_executable']).resolve().is_relative_to(target.resolve())
    assert (target/installation.RECEIPT_NAME).read_bytes() == receipt
    assert sha256(target/'_internal/core.py') == core_digest
    report['checks'].append('Installed NSIS maintenance safely relaunches outside its folder, verifies the copied executable, uses independent resources and waits for the original owning NSIS process; installation remains intact')


def check_output_locations(target: Path, sandbox: Path, report: dict) -> None:
    """Use the actual installed preferences and Windows Documents resolver.

    The default Documents path is only resolved. Generated test files stay in
    the disposable sandbox; the runner's actual Documents folder is untouched.
    """
    print('[Windows smoke] Documents outputs and setup-selected shared folder', flush=True)
    selected = sandbox / 'Generated outputs 日本語'
    program = '''import json,sys
from pathlib import Path
target=Path(sys.argv[1]); selected=Path(sys.argv[2])
sys.path.insert(0,str(target/'_internal'))
import output_locations as locations
from core import Config,default_directory,load_config
settings=target/'Data/settings.json'
assert not settings.exists(),'Output default must be tested before preferences exist'
default_root=locations.documents_directory()/'UnifiedTranscriber'
existed_before=default_root.exists()
assert locations.output_root(target)==default_root
assert Config().profile=='faster-res'
for kind,name in locations.OUTPUT_FOLDERS.items():
    assert locations.default_directory(kind,app_root=target)==default_root/name
    assert default_directory(kind)==default_root/name
assert default_root.exists()==existed_before,'Path resolution must not create user folders'
settings.parent.mkdir(parents=True,exist_ok=True)
settings.write_text(json.dumps({'theme_mode':'dark','future_setting':{'retained':True}}),encoding='utf-8')
locations.set_output_root(selected,app_root=target)
preferences=json.loads(settings.read_text(encoding='utf-8'))
assert preferences['theme_mode']=='dark' and preferences['future_setting']=={'retained':True}
assert load_config().output_root==str(selected)
assert locations.output_root(target)==selected
for kind,name in locations.OUTPUT_FOLDERS.items():
    assert default_directory(kind)==selected/name
assert not selected.exists(),'Changing a preference must not create outputs early'
print('OUTPUT_LOCATIONS_JSON='+json.dumps({'documents_default':str(default_root),
    'selected_output_root':str(selected),'output_kinds':list(locations.OUTPUT_FOLDERS),
    'profile':Config().profile,'unknown_preferences_retained':True,
    'path_resolution_did_not_create_folders':True}))
'''
    result = run([target/'Runtime/python.exe', '-I', '-X', 'utf8', '-c', program, target, selected],
                 target=target, extra_env=no_host_python_environment(target))
    row = next(line.removeprefix('OUTPUT_LOCATIONS_JSON=') for line in result.stdout.splitlines()
               if line.startswith('OUTPUT_LOCATIONS_JSON='))
    report['output_locations'] = json.loads(row)
    # A fresh interpreter must see the choice before importing GUI or optional
    # dependencies, since startup logs are opened before the main window.
    startup = '''import json,sys
from pathlib import Path
target=Path(sys.argv[1]); selected=Path(sys.argv[2])
sys.path.insert(0,str(target/'_internal'))
from diagnostic_logs import log_directory
from output_locations import output_root
assert output_root(target)==selected
folder=log_directory()
assert folder==selected/'Logs'
marker=folder/'sample-startup.log'
marker.write_text('sample startup log',encoding='utf-8')
assert marker.read_text(encoding='utf-8')=='sample startup log'
print(json.dumps({'startup_log_uses_selected_root':True}))
'''
    result = run([target/'Runtime/python.exe', '-I', '-X', 'utf8', '-c', startup, target, selected],
                 target=target, extra_env=no_host_python_environment(target))
    report['output_locations'].update(json.loads(result.stdout.strip()))
    report['checks'].append('With host Python absent from PATH, the installed app defaults every generated output category to Windows Documents/UnifiedTranscriber, preserves unknown settings when setup selects another root, and a fresh startup writes its log there')


def check_setup_output(target: Path, report: dict) -> None:
    """Exercise real nested CREATE_NO_WINDOW stdout, stderr and error tails."""
    print('[Windows smoke] Nested setup output, UTF-8 and diagnostic token masking', flush=True)
    sample_token = 'hf_' + 'SampleToken0123456789' * 2
    program = '''import json,subprocess,sys
from pathlib import Path
target=Path(sys.argv[1])
sys.path.insert(0,str(target/'_internal'))
from setup_process import setup_interpreter,run_logged
assert sys.flags.utf8_mode==1
python=setup_interpreter(target)
child='import os,sys; assert sys.flags.utf8_mode==1; print("nested stdout 日本語",flush=True); print("nested stderr 録音",file=sys.stderr,flush=True); print(os.environ["HF_TOKEN"],flush=True); raise SystemExit(7)'
result=run_logged([python,'-I','-X','utf8','-c',child],target,check=False)
assert result.returncode==7
assert 'nested stdout 日本語' in result.stdout and 'nested stderr 録音' in result.stdout
assert '[HF_TOKEN非表示]' in result.stdout
try:
    run_logged([python,'-I','-X','utf8','-c',child],target)
except subprocess.CalledProcessError as exc:
    assert exc.returncode==7 and 'nested stderr 録音' in exc.stdout
else:
    raise AssertionError('Nested setup failure was swallowed')
print('SETUP_OUTPUT_JSON='+json.dumps({'stdout_and_stderr':True,'utf8':True,
    'error_status_retained':True,'masked_tail':True,'interpreter':python}))
'''
    result = run([target/'Runtime/python.exe', '-I', '-X', 'utf8', '-c', program, target], target=target,
                 extra_env={**no_host_python_environment(target), 'HF_TOKEN': sample_token})
    assert sample_token not in result.stdout and sample_token not in result.stderr
    row = next(line.removeprefix('SETUP_OUTPUT_JSON=') for line in result.stdout.splitlines()
               if line.startswith('SETUP_OUTPUT_JSON='))
    report['setup_subprocess_output'] = json.loads(row)
    report['checks'].append('Explicit nested setup pipes preserve stdout/stderr, Japanese diagnostics and nonzero status while masking the in-memory sample token; no global Python directory is on PATH')


def check_optional_environments(target: Path, report: dict) -> None:
    """Install into the disposable installation, never into the shipped payload."""
    print('[Windows smoke] Minimal CPU AI environment setup and checkpoint load', flush=True)
    python = target/'MLRuntime/python.exe'
    if not python.is_file():
        raise RuntimeError('The modern optional environment runtime was not bundled.')
    distribution = json.loads((target/'_internal/distribution-info.json').read_text(encoding='utf-8'))
    expected_python = distribution['ml_python']['version']
    # A real old venv ensures an update cannot accidentally keep using the GUI
    # runtime merely because a Python executable already exists in this folder.
    legacy = target/'_internal/environments/faster-res'
    legacy.parent.mkdir(parents=True, exist_ok=True)
    run([target/'Runtime/python.exe', '-I', '-X', 'utf8', '-m', 'venv', legacy],
        timeout=180, target=target, extra_env=no_host_python_environment(target))
    legacy_probe = run([legacy/'Scripts/python.exe', '-I', '-X', 'utf8', '-c',
        'import sys; print(sys.version.split()[0])'],
        target=target, extra_env=no_host_python_environment(target)).stdout.strip()
    assert legacy_probe != expected_python, 'The migration fixture must use a genuinely older runtime'
    settings = target/'Data/settings.json'
    original_settings = settings.read_bytes()
    retained = target/'Models/sample-retained-model.bin'
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(b'Existing cached model fixture remains unchanged')
    run([python, target/'_internal/setup_backend.py', '--profile', 'faster-res',
         '--hardware', 'cpu'], timeout=1200, target=target,
        extra_env=no_host_python_environment(target))
    engine = target/'_internal/environments/faster-res/Scripts/python.exe'
    probe = run([engine, '-c',
        'import json,sys,torch,torchaudio,faster_whisper,numpy as np; '
        'from pathlib import Path; '
        'from resemblyzer import VoiceEncoder; '
        'from importlib.metadata import version; '
        'from sklearn.cluster import AgglomerativeClustering; '
        'target=Path(sys.argv[1]); sys.path.insert(0,str(target/"_internal")); '
        'from setup_backend import torch_baseline,torchaudio_baseline; '
        'assert sys.version.split()[0]==sys.argv[2]; '
        'assert Path(sys.base_prefix).resolve()==(target/"MLRuntime").resolve(); '
        'assert torch.__version__.split("+")[0]==torch_baseline("faster-res"); '
        'assert torchaudio.__version__.split("+")[0]==torchaudio_baseline("faster-res"); '
        'assert torch.version.cuda is None; '
        'encoder=VoiceEncoder(device="cpu"); '
        'assert next(encoder.parameters()).device.type=="cpu"; '
        'wave=(0.1*np.sin(2*np.pi*220*np.arange(32000)/16000)).astype(np.float32); '
        'embedding=encoder.embed_utterance(wave); '
        'assert embedding.shape==(256,) and np.isfinite(embedding).all(); '
        'assert abs(float(np.linalg.norm(embedding))-1.0)<0.01; '
        'labels=AgglomerativeClustering(n_clusters=2).fit_predict(np.array([[0,0],[0,0.1],[2,2],[2,2.1]])); '
        'assert len(set(labels))==2; '
        'result={name:version(name) for name in '
        '("torch","torchaudio","faster-whisper","ctranslate2","Resemblyzer",'
        '"numpy","scipy","scikit-learn","librosa")}; '
        'result.update(python=sys.version.split()[0],base_prefix=sys.base_prefix,'
        'speaker_embedding_inference=True,clustering=True); '
        'print("SMOKE_JSON="+json.dumps(result))', target, expected_python],
        timeout=180, target=target, extra_env=no_host_python_environment(target))
    versions = next(line.removeprefix('SMOKE_JSON=') for line in probe.stdout.splitlines()
                    if line.startswith('SMOKE_JSON='))
    report['minimal_cpu_dependencies'] = json.loads(versions)
    assert settings.read_bytes() == original_settings
    assert retained.read_bytes() == b'Existing cached model fixture remains unchanged'
    report['modern_environment_migration'] = {'legacy_python': legacy_probe,
        'selected_python': expected_python, 'settings_and_cached_model_retained': True}
    report['checks'].append('With global Python absent from PATH, profile ② migrates an actual legacy venv to the bundled modern interpreter, imports the selected ASR/scientific stack, computes a finite normalized speaker embedding and clusters samples; settings/cache fixtures are retained and no large ASR model is downloaded')
    print('[Windows smoke] Recording environment and verified media tools setup', flush=True)
    run([python, target/'_internal/setup_live_capture.py'], timeout=600, target=target,
        extra_env=no_host_python_environment(target))
    recorder = target/'_internal/environments/live-capture/Scripts/python.exe'
    probe = run([recorder, '-c',
        'import json,sys,numpy,sounddevice,pyaudiowpatch; '
        'from pathlib import Path; '
        'from importlib.metadata import version; '
        'assert sys.version.split()[0]==sys.argv[2]; '
        'assert Path(sys.base_prefix).resolve()==(Path(sys.argv[1])/"MLRuntime").resolve(); '
        'result={name:version(name) for name in ("numpy","sounddevice","PyAudioWPatch")}; '
        'result.update(python=sys.version.split()[0],base_prefix=sys.base_prefix); '
        'print(json.dumps(result))', target, expected_python], target=target,
        extra_env=no_host_python_environment(target))
    report['recording_dependencies'] = json.loads(probe.stdout.strip())
    report['checks'].append('Recording environment installs and imports native PortAudio/WASAPI dependencies without opening audio hardware')


def check_cuda_packages(target: Path, sandbox: Path, report: dict) -> None:
    """Load real CUDA wheels/DLLs without claiming unavailable GPU coverage.

    This is a separate test environment, so CPU profile selection, user data
    and the completed recording environment are unchanged. Wheels are not
    cached: the CUDA download is large and is needed only for this CI gate.
    """
    available = shutil.disk_usage(sandbox).free
    print('[Windows smoke] CUDA 12.6 package/ABI/DLL checks; '
          f'{available / 2**30:.1f} GiB free; GPU inference is not exercised', flush=True)
    if available < 8 * 2**30:
        raise RuntimeError('The required CUDA wheel gate needs at least 8 GiB of disposable '
                           'disk space. CUDA compatibility has not been validated.')
    versions = report['minimal_cpu_dependencies']
    program = """import ctypes,json,os,sys
from pathlib import Path
target=Path(sys.argv[1]); environment=Path(sys.argv[2]); pins=json.loads(sys.argv[3])
sys.path.insert(0,str(target/'_internal'))
from setup_backend import torch_baseline,torchaudio_baseline
from setup_process import run_logged
runtime=target/'MLRuntime/python.exe'
run_logged([str(runtime),'-I','-X','utf8','-m','venv',str(environment)],target)
python=environment/'Scripts/python.exe'
pip=[str(python),'-m','pip','--isolated','--disable-pip-version-check','--require-virtualenv',
    'install','--no-cache-dir','--only-binary=:all:','--constraint',str(target/'_internal/constraints_security.txt')]
run_logged([*pip,'--index-url','https://download.pytorch.org/whl/cu126',
    'torch=='+torch_baseline('faster-res'),'torchaudio=='+torchaudio_baseline('faster-res')],target)
run_logged([*pip,'--index-url','https://pypi.org/simple',
    'ctranslate2=='+pins['ctranslate2'],'numpy=='+pins['numpy']],target)
probe=r'''import json,os,sys
from pathlib import Path
from importlib.metadata import version
target=Path(sys.argv[1]); sys.path.insert(0,str(target/'_internal'))
from setup_backend import torch_baseline,torchaudio_baseline
from engines.cuda_runtime import initialize_cuda_libraries,REQUIRED_DLLS
assert 'torch' not in sys.modules
os.environ['CUDA_PATH']=str(target/'Untrusted external toolkit')
initialization=initialize_cuda_libraries()
assert initialization['status']=='ready'
assert 'CUDA_PATH' not in os.environ
assert set(REQUIRED_DLLS).issubset(initialization['loaded'])
assert initialization['handle_count']==len(initialization['loaded'])
library=Path(sys.prefix)/'Lib/site-packages/torch/lib'
assert Path(initialization['torch_lib']).resolve()==library.resolve()
for name,origin in initialization['loaded'].items():
    assert Path(origin).resolve()==(library/name).resolve()
assert initialize_cuda_libraries()['handle_count']==initialization['handle_count']
import torch,torchaudio,numpy,ctranslate2
assert torch.__version__.split('+')[0]==torch_baseline('faster-res')
assert torchaudio.__version__.split('+')[0]==torchaudio_baseline('faster-res')
assert torch.version.cuda=='12.6'
assert Path(sys.base_prefix).resolve()==(target/'MLRuntime').resolve()
assert Path(torch.__file__).resolve().parent/'lib'==library.resolve()
cpu_types=sorted(ctranslate2.get_supported_compute_types('cpu'))
assert cpu_types
result={name:version(name) for name in ('torch','torchaudio','ctranslate2','numpy')}
result.update(python=sys.version.split()[0],built_cuda=torch.version.cuda,
    gpu_available=torch.cuda.is_available(),cuda_initialization=initialization,
    ctranslate2_native_import=True,ctranslate2_cpu_compute_types=cpu_types,
    hardware_validation_performed=False,large_model_downloaded=False)
print('CUDA_PACKAGE_JSON='+json.dumps(result))
'''
run_logged([str(python),'-I','-X','utf8','-c',probe,str(target)],target)
"""
    with tempfile.TemporaryDirectory(prefix='Cuda package checks 日本語 ', dir=sandbox) as temporary:
        environment = Path(temporary)/'environment'
        result = run([target/'MLRuntime/python.exe', '-I', '-X', 'utf8', '-c', program, target,
                      environment, json.dumps(versions)], timeout=1800, target=target,
                     extra_env=no_host_python_environment(target))
        rows = [line.removeprefix('CUDA_PACKAGE_JSON=') for line in result.stdout.splitlines()
                if line.startswith('CUDA_PACKAGE_JSON=')]
        if len(rows) != 1:
            raise RuntimeError('The CUDA package/ABI/DLL gate did not produce exactly one result.')
        report['cuda_package_compatibility'] = json.loads(rows[0])
    report['checks'].append('A separate bundled-modern-Python venv installs official CUDA 12.6 Torch/TorchAudio and matching CTranslate2 wheels; the product CUDA initializer loads its own CUDA/cuBLAS and any included optional cuDNN DLLs before CTranslate2, removes external toolkit lookup and retains handles without duplication. Host Python is absent from PATH; no GPU computation or ASR inference is claimed')


def directory_junction(link: Path, destination: Path) -> None:
    """Create a runner-local NTFS junction using Win32, never a command shell."""
    import ctypes
    from ctypes import wintypes
    link.mkdir()
    target = str(destination.resolve())
    substitute = ('\\??\\' + target).encode('utf-16-le')
    printed = target.encode('utf-16-le')
    paths = substitute + b'\0\0' + printed + b'\0\0'
    data = struct.pack('<IHHHHHH', 0xA0000003, 8+len(paths), 0,
                       0, len(substitute), len(substitute)+2, len(printed)) + paths
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(link), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), 'Could not open the temporary junction directory')
    try:
        returned = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data)
        if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(data), None, 0,
                                      ctypes.byref(returned), None):
            raise OSError(ctypes.get_last_error(), 'Could not create the temporary NTFS junction')
    finally:
        kernel.CloseHandle(handle)


def verify_registration(target: Path, receipt: dict, version: str) -> Path:
    import winreg
    key_name = installation._registration_key(receipt['install_id'])
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
        assert winreg.QueryValueEx(key, 'DisplayVersion')[0] == version
        assert Path(winreg.QueryValueEx(key, 'InstallLocation')[0]) == target
        expected = subprocess.list2cmdline([str(target/'UnifiedTranscriberSetup.exe'),
                                            '--target', str(target), '--uninstall'])
        assert winreg.QueryValueEx(key, 'UninstallString')[0] == expected
        assert Path(winreg.QueryValueEx(key, 'DisplayIcon')[0]) == target/'UnifiedTranscriber.exe'
    shortcut = installation._shortcut_path(receipt['install_id'])
    assert shortcut.is_file() and shortcut.stat().st_size > 0
    return shortcut


def assert_unregistered(receipt: dict, shortcut: Path) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, installation._registration_key(receipt['install_id'])):
            raise AssertionError('The temporary uninstall registration remains after uninstall')
    except FileNotFoundError:
        pass
    assert not shortcut.exists(), 'The temporary Start Menu shortcut remains after uninstall'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--dist', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This integration check requires Windows.')
    info = json.loads((args.work/'BUILD_INFO.json').read_text(encoding='utf-8'))
    runtime_identity = json.loads((args.work/'runtime-info.json').read_text(encoding='utf-8'))
    version = info['version']
    setup = (args.dist/f'Unified_Transcriber_Setup_v{version}.exe').resolve()
    payload = (args.work/'payload.zip').resolve()
    assert sha256(payload) == info['payload_sha256']
    report = {'version': version, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='unified-installer-smoke-') as temporary:
        sandbox = Path(temporary).resolve()
        check_nsis_installer(setup, sandbox, report, runtime_identity)
        check_legacy_layout_upgrade(payload, setup, version, sandbox, report)
        target = sandbox/'Sample Installation'
        receipt = None
        try:
            print('[Windows smoke] Installation and Windows registration', flush=True)
            result = install_package(payload, target, version, expected_sha256=info['payload_sha256'],
                extra_files={'UnifiedTranscriberSetup.exe': setup},
                register_program=True, create_shortcut=True)
            receipt = json.loads((target/installation.RECEIPT_NAME).read_text(encoding='utf-8'))
            assert not result.warnings, result.warnings
            installed = inspect_installation(target)
            assert installed.version == version
            report['checks'].append('Installation succeeds in a folder containing spaces')
            shortcut = verify_registration(target, receipt, version)
            report['checks'].append('Native HKCU uninstall entry and COM Start Menu shortcut point to this install')
            print('[Windows smoke] Relocated runtime and installed native launcher', flush=True)
            python = target/'Runtime/python.exe'
            result = run([python, '-I', '-X', 'utf8', '-c',
                'import sys,json,tkinter,ssl,ensurepip,venv,ctypes; from pathlib import Path; '
                'ctypes.WinDLL(str(Path(sys.executable).parent/"msvcp140.dll")); '
                'r=tkinter.Tk(); r.update(); r.destroy(); '
                'print(json.dumps({"prefix":sys.prefix,"version":sys.version.split()[0]}))'],
                target=target, extra_env=no_host_python_environment(target))
            assert Path(json.loads(result.stdout.strip())['prefix']).resolve() == (target/'Runtime').resolve()
            report['checks'].append('Bundled Python, TLS, Tk, venv and app-local Microsoft C++ runtime relocate without system Python')
            app_report = sandbox/'app-self-test.json'
            reported_version = run([target/'UnifiedTranscriber.exe', '--version'],
                target=target, extra_env=no_host_python_environment(target))
            assert reported_version.stdout.strip() == version, 'The native launcher must preserve --version stdout'
            run([target/'UnifiedTranscriber.exe', '--self-test', '--self-test-report', app_report],
                target=target, extra_env=no_host_python_environment(target))
            assert app_report.is_file()
            assert json.loads(app_report.read_text(encoding='utf-8'))['ok'] is True
            from native_launcher_smoke import check_native_launcher
            report['native_launcher'] = check_native_launcher(target, sandbox)
            report['checks'].append('The ordinary x64 GUI launcher has ASLR/NX/CFG, no PyInstaller archive, preserves diagnostic stdout/exit codes and uses only the isolated bundled runtime; native probes cover quoting, Unicode/spaces, hostile startup paths, noninherited handles and missing-runtime repair diagnostics')
            check_output_locations(target, sandbox, report)
            check_setup_output(target, report)
            check_nsis_maintenance(target, sandbox, report)
            check_optional_environments(target, report)
            check_cuda_packages(target, sandbox, report)
            check_native_media(target, sandbox, report)
            settings = target/'Data/settings.json'
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text('{"profile":"faster-res","theme_mode":"dark"}', encoding='utf-8')
            markers = [settings, target/'Models/sample-model.txt',
                       target/'_internal/environments/faster-res/sample-environment.txt',
                       target/'Recordings/sample-recording.txt']
            for path in markers[1:]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('sample retained data', encoding='utf-8')
            before = {str(path):path.read_bytes() for path in markers}
            (target/'_internal/core.py').write_text('# simulated damaged program file\n', encoding='utf-8')
            print('[Windows smoke] Repair and retained user data', flush=True)
            repaired = install_package(payload, target, version, mode='repair',
                expected_sha256=info['payload_sha256'], extra_files={'UnifiedTranscriberSetup.exe': setup})
            assert not repaired.warnings, repaired.warnings
            assert all(path.read_bytes() == before[str(path)] for path in markers)
            assert b'APP_VERSION' in (target/'_internal/core.py').read_bytes()
            verify_registration(target, receipt, version)
            report['checks'].append('Repair restores program files and preserves settings/models/environments/recordings and registration')
            external = sandbox/'External Recording'
            external.mkdir()
            (external/'sample.txt').write_text('external data', encoding='utf-8')
            directory_junction(target/'linked-external', external)
            assert (target/'linked-external/sample.txt').read_text() == 'external data'
            print('[Windows smoke] Uninstall, registration cleanup and external junction protection', flush=True)
            removed = uninstall_installation(target, remove_data=True)
            assert not removed.warnings, removed.warnings
            assert not target.exists()
            assert (external/'sample.txt').read_text() == 'external data'
            selected_log = Path(report['output_locations']['selected_output_root'])/'Logs/sample-startup.log'
            assert selected_log.read_text(encoding='utf-8') == 'sample startup log'
            assert_unregistered(receipt, shortcut)
            report['checks'].append('Uninstall removes its folder/HKCU/shortcut while retaining the separately selected output root and without following an NTFS junction into external data')
        finally:
            # Keep the runner clean even when a preceding assertion fails.
            if receipt is None and (target/installation.RECEIPT_NAME).is_file():
                receipt = json.loads((target/installation.RECEIPT_NAME).read_text(encoding='utf-8'))
            if receipt is not None:
                installation.unregister_windows(target, receipt)
    (args.dist/'WINDOWS_SMOKE.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
