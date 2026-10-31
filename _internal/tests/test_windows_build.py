"""Security contracts for the source-to-installer packaging boundary."""
import ast
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import subprocess
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / 'tools/build_windows.py'
SPEC = importlib.util.spec_from_file_location('windows_build_tools', SOURCE)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)
RUNTIME_SPEC = importlib.util.spec_from_file_location('cpython_build_tools', SOURCE.with_name('build_cpython.py'))
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime)
SMOKE_SPEC = importlib.util.spec_from_file_location('windows_smoke_tools', SOURCE.with_name('windows_smoke.py'))
smoke = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(smoke)


class BuildContracts(unittest.TestCase):
    def test_verifier_build_requires_upstream_license_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / 'payload'
            payload.mkdir()
            def fetch_without_notice(_entry, archive):
                archive.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr('gh_fixture/bin/gh.exe', b'fixture')
            with patch.object(build, 'fetch_pinned', side_effect=fetch_without_notice), \
                    patch.object(build, 'run') as runner:
                with self.assertRaisesRegex(ValueError, 'license notice'):
                    build.prepare_verifier(payload, root / 'work', 123)
                runner.assert_not_called()

    def test_payload_preserves_unicode_names_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); payload=root/'payload';payload.mkdir()
            (payload/'_internal/assets/icons').mkdir(parents=True)
            (payload/'_internal/assets/icons/sample.png').write_bytes(b'icon pixels')
            (payload/'音声の説明.txt').write_text('sample description',encoding='utf-8')
            archive=root/'payload.zip'
            manifest=build.make_payload(payload,archive)
            with zipfile.ZipFile(archive) as source:
                self.assertEqual(set(source.namelist()),set(manifest))
                for name in source.namelist():
                    import hashlib
                    self.assertEqual(hashlib.sha256(source.read(name)).hexdigest(),manifest[name])
                    self.assertEqual(source.getinfo(name).date_time,(2000,1,1,0,0,0))
            self.assertIn('_internal/assets/icons/sample.png',manifest)

    def test_payload_rejects_symlink_instead_of_following_external_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);payload=root/'payload';payload.mkdir()
            outside=root/'private.txt';outside.write_text('not for distribution')
            try:
                (payload/'linked.txt').symlink_to(outside)
            except OSError:
                # Windows without symlink privilege still exercises the guard.
                (payload/'linked.txt').write_bytes(b'ordinary fixture')
                with patch.object(Path,'is_symlink',return_value=True), self.assertRaises(ValueError):
                    build.make_payload(payload,root/'payload.zip')
            else:
                with self.assertRaises(ValueError):
                    build.make_payload(payload,root/'payload.zip')

    def test_openssl_runtime_validation_uses_fix_and_patch_fields(self):
        identity={'version':'3.11.18','openssl_version_info':[3,5,0,9,0],
            'openssl_version_number':'0x30500090','sqlite':'3.53.4','tk':'8.6.18','zlib':'1.3.2'}
        build.validate_runtime_versions(identity)
        for name,value in (('openssl_version_info',[3,5,0,8,0]),
                           ('version','3.11.9'),('sqlite','3.45.1'),('tk','8.6.15')):
            with self.subTest(name=name),self.assertRaisesRegex(RuntimeError,name+' mismatch'):
                build.validate_runtime_versions(dict(identity,**{name:value}))

    def test_tcl_build_manifest_uses_only_pinned_commit_without_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            component=Path(temporary)
            (component/'win').mkdir()
            (component/'win/gitmanifest.in').write_bytes(b'git-')
            commit='515167c897a7c8066abfbd71e8fe7bd8bf3569de'
            runtime.write_component_manifest(component,commit)
            self.assertEqual((component/'manifest.uuid').read_bytes(),b'git-'+commit.encode()+b'\n')
            self.assertFalse((component/'.git').exists())
            with self.assertRaises(ValueError):
                runtime.write_component_manifest(component,'main\nextra')

    def test_runtime_sources_extract_only_ordinary_bounded_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); archive=root/'source.zip'
            with zipfile.ZipFile(archive,'w') as output:
                output.writestr('upstream/Lib/sample.py','sample = True\n')
            runtime.extract_source(archive,root/'good')
            self.assertEqual((root/'good/Lib/sample.py').read_text(),'sample = True\n')

    def test_runtime_sources_reject_escape_and_windows_case_collisions(self):
        cases=[['upstream/../escape'],['other/escape'],['upstream/C:/escape'],
               ['upstream/A.py','upstream/a.py']]
        for names in cases:
            with self.subTest(names=names),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);archive=root/'source.zip'
                with zipfile.ZipFile(archive,'w') as output:
                    output.writestr('upstream/safe.txt','safe')
                    for name in names:output.writestr(name,'sample')
                with self.assertRaises(ValueError):
                    runtime.extract_source(archive,root/'target')
                self.assertFalse((root/'escape').exists())

    def test_runtime_source_tar_rejects_links_and_oversized_members(self):
        for kind in ('link','oversized'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);archive=root/'source.tgz'
                with tarfile.open(archive,'w:gz') as output:
                    entry=tarfile.TarInfo('upstream/sample')
                    if kind=='link':
                        entry.type=tarfile.SYMTYPE;entry.linkname='../../escape'
                    else:entry.size=40_000_001
                    # A large size declaration is rejected before reading its
                    # absent body, so no large fixture is required.
                    output.addfile(entry)
                with self.assertRaises(ValueError):
                    runtime.extract_source(archive,root/'target',tar=True)

    def test_captured_command_failure_exposes_bounded_diagnostics(self):
        failure=subprocess.CalledProcessError(1,['powershell.exe'],output='x'*50000,
                                             stderr='SignatureStatus=UnknownError')
        with patch.object(build.subprocess,'run',side_effect=failure),self.assertRaises(RuntimeError) as caught:
            build.run(['powershell.exe','-NoProfile'],capture=True)
        message=str(caught.exception)
        self.assertIn('powershell.exe exited with status 1',message)
        self.assertIn('SignatureStatus=UnknownError',message)
        self.assertLess(len(message),25000)

    def test_crt_discovery_supports_vs2026_major_number_and_official_sdk_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            current=root/'Program Files'
            x86=root/'Program Files (x86)'
            def crt(base,product,version,family):
                path=base/f'Microsoft Visual Studio/{product}/Enterprise/VC/Redist/MSVC/{version}/x64/Microsoft.VC{family}.CRT'
                path.mkdir(parents=True)
                for name in build.CRT_REQUIRED:
                    (path/name).write_bytes(b'sample')
                return path
            old=crt(current,'2022','14.44.35112','143')
            new=crt(current,'18','14.50.35717','145')
            env={'ProgramW6432':str(current),'ProgramFiles':str(current),'ProgramFiles(x86)':str(x86)}
            with patch.dict(os.environ,env),patch('builtins.print'):
                self.assertEqual(build.find_visual_cpp_runtime(),new)
            for file in (*old.glob('*.dll'),*new.glob('*.dll')):
                file.unlink()
            alias=crt(x86,'18','v145','145')
            with patch.dict(os.environ,env),patch('builtins.print'):
                self.assertEqual(build.find_visual_cpp_runtime(),alias)

    def test_crt_discovery_failure_describes_checked_sdk_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            incomplete=root/'Microsoft Visual Studio/18/Enterprise/VC/Redist/MSVC/v145/x64/Microsoft.VC145.CRT'
            incomplete.mkdir(parents=True)
            env={name:str(root) for name in ('ProgramW6432','ProgramFiles','ProgramFiles(x86)')}
            with patch.dict(os.environ,env),self.assertRaisesRegex(RuntimeError,'product directories 18') as caught:
                build.find_visual_cpp_runtime()
            self.assertIn('SDK directories v145',str(caught.exception))
            self.assertIn('missing',str(caught.exception))

    def test_dependency_url_cannot_escape_https_allowlist(self):
        build.check_url('https://github.com/cli/cli/releases/download/v1/a.zip')
        build.check_url('https://release-assets.githubusercontent.com/sample?signature=sample')
        for url in ('http://github.com/sample', 'file:///tmp/sample',
                    'https://github.com.evil.invalid/sample', 'https://user:secret@github.com/sample',
                    'https://github.com:444/sample', 'https://evil.invalid/sample'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                build.check_url(url)

    def test_payload_is_reproducible_and_contains_file_digests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'payload'; source.mkdir()
            (source/'VERSION').write_bytes(b'2.1.0\n')
            (source/'sample.txt').write_bytes('サンプル'.encode())
            a, b = root/'one.zip', root/'two.zip'
            manifest = build.make_payload(source, a)
            build.make_payload(source, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(manifest['sample.txt'], build.digest(source/'sample.txt'))
            with zipfile.ZipFile(a) as archive:
                self.assertEqual(archive.getinfo('VERSION').date_time, (2000,1,1,0,0,0))
                self.assertEqual(archive.read('VERSION'), b'2.1.0\n')

    def test_source_boundary_omits_git_workflows_and_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); repo=root/'repo'; repo.mkdir(); target=root/'dist';target.mkdir()
            for name in ('README.md','.github/workflows/sample.yml','_internal/tests/test_sample.py'):
                path=repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('sample')
            listed=b'README.md\0.github/workflows/sample.yml\0_internal/tests/test_sample.py\0'
            with patch.object(build,'REPO',repo),patch.object(build.subprocess,'check_output',return_value=listed):
                self.assertEqual(build.copy_sources(target),['README.md'])
            self.assertEqual(sorted(str(p.relative_to(target)) for p in target.rglob('*')),['README.md'])

    def test_tracked_private_data_is_rejected_even_if_mistakenly_added_to_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir();target=root/'dist';target.mkdir()
            path=repo/'Data/settings.json';path.parent.mkdir();path.write_text('{}')
            with patch.object(build,'REPO',repo),patch.object(build.subprocess,'check_output',return_value=b'Data/settings.json\0'):
                with self.assertRaisesRegex(ValueError,'user/runtime data'):
                    build.copy_sources(target)

    def test_build_subprocess_environment_does_not_inherit_tokens_or_proxy(self):
        with patch.dict(os.environ,{'HF_TOKEN':'sample','GH_TOKEN':'sample','GITHUB_TOKEN':'sample',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'sample','ACTIONS_RUNTIME_TOKEN':'sample',
            'HTTPS_PROXY':'https://sample.invalid','PYTHONPATH':'sample','PIP_INDEX_URL':'sample'}):
            env=build.clean_environment()
        for name in ('HF_TOKEN','GH_TOKEN','GITHUB_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                     'ACTIONS_RUNTIME_TOKEN','HTTPS_PROXY','PYTHONPATH','PIP_INDEX_URL'):
            self.assertNotIn(name,env)
        self.assertEqual(env['GH_PROMPT_DISABLED'],'1')

    def test_work_directory_cannot_replace_repo_or_parent(self):
        for path in (build.REPO,build.REPO.parent):
            with self.subTest(path=path),self.assertRaises(ValueError):
                build.work_folder(str(path))


class NativeMediaContracts(unittest.TestCase):
    def result(self):
        return dict(tests_run=12,
                    successes=list(smoke.MEDIA_FFMPEG_TESTS + smoke.MEDIA_FFPROBE_TESTS),
                    skipped=[], failures=[], errors=[])

    def media_pair(self, target):
        tools={}
        for name in ('ffmpeg','ffprobe'):
            binary=target/'_internal/environments/media-tools'/f'{name}.exe'
            binary.parent.mkdir(parents=True,exist_ok=True)
            binary.write_bytes(b'MZ native fixture '+name.encode())
            tools[name]=binary
        hashes={path.name:smoke.sha256(path) for path in tools.values()}
        return tools,hashes

    def test_media_coverage_requires_all_twelve_successes_without_skips(self):
        smoke.validate_media_results(self.result())
        for name in smoke.MEDIA_FFMPEG_TESTS+smoke.MEDIA_FFPROBE_TESTS:
            with self.subTest(name=name):
                result=self.result();result['successes'].remove(name)
                result['skipped'].append(dict(test=name,reason='media tool absent'))
                with self.assertRaisesRegex(RuntimeError,'all twelve.*without skips'):
                    smoke.validate_media_results(result)

    def test_duplicate_missing_unexpected_failed_or_errored_cases_are_rejected(self):
        for kind in ('duplicate','missing','unexpected','failure','error','count','skip'):
            with self.subTest(kind=kind):
                result=self.result()
                if kind=='duplicate':result['successes'].append(result['successes'][0])
                elif kind=='missing':result['successes'].pop()
                elif kind=='unexpected':result['successes'][-1]='unexpected.Test.test_case'
                elif kind=='failure':result['failures'].append(result['successes'][0])
                elif kind=='error':result['errors'].append(result['successes'][0])
                elif kind=='count':result['tests_run']=11
                else:result['skipped'].append(dict(test=result['successes'][0],reason='absent'))
                with self.assertRaises(RuntimeError):smoke.validate_media_results(result)

    def test_changed_inventory_cannot_silently_lower_or_duplicate_coverage(self):
        for inventory in (smoke.MEDIA_FFPROBE_TESTS[:-1],
                          smoke.MEDIA_FFPROBE_TESTS[:-1]+(smoke.MEDIA_FFMPEG_TESTS[0],)):
            with self.subTest(inventory=inventory),patch.object(smoke,'MEDIA_FFPROBE_TESTS',inventory):
                with self.assertRaises(RuntimeError):smoke.validate_media_results(self.result())

    def test_smoke_requires_both_verified_installed_tools_and_pinned_hashes(self):
        with tempfile.TemporaryDirectory(prefix='Media fixtures 日本語 ') as temporary:
            target=Path(temporary).resolve()/'Sample Installation'
            tools,hashes=self.media_pair(target)
            with patch.object(smoke,'verified_media_binary',side_effect=lambda base,name:str(tools[name])) as verified, \
                    patch.object(smoke,'MEDIA_BINARY_SHA256',hashes):
                self.assertEqual(smoke.checked_media_tools(target),tools)
                self.assertEqual([call.args for call in verified.call_args_list],
                                 [(target,'ffmpeg'),(target,'ffprobe')])
                tools['ffprobe'].write_bytes(b'MZ tampered')
                with self.assertRaisesRegex(RuntimeError,'pinned SHA256'):
                    smoke.checked_media_tools(target)

    def test_missing_or_outside_verified_pair_fails_before_any_media_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary).resolve()/'Sample Installation'
            tools,hashes=self.media_pair(target)
            outside=Path(temporary).resolve()/'ffprobe.exe';outside.write_bytes(b'MZ unrelated')
            for probe in (None,str(outside)):
                def verified(base,name):
                    return str(tools[name]) if name=='ffmpeg' else probe
                with self.subTest(probe=probe),patch.object(smoke,'verified_media_binary',side_effect=verified), \
                        patch.object(smoke,'MEDIA_BINARY_SHA256',hashes),patch.object(smoke,'run') as run:
                    with self.assertRaisesRegex(RuntimeError,'verified.*pair'):
                        smoke.check_native_media(target,Path(temporary),{'checks':[]})
                    run.assert_not_called()

    def test_native_media_copies_both_tools_and_runs_original_build_python_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox=Path(temporary).resolve();target=sandbox/'Sample Installation'
            sources,hashes=self.media_pair(target);report={'checks':[]};calls=[]
            def run(args,**kwargs):
                calls.append((args,kwargs))
                if args[1:]==['-version']:
                    name=Path(args[0]).stem
                    return SimpleNamespace(stdout=f'{name} version {smoke.MEDIA_VERSION}-essentials_build\n',stderr='')
                self.assertEqual(args[:5],[smoke.sys.executable,'-I','-X','utf8','-c'])
                code_root, fixture_path, suite_json, trusted_path, pins_json, installed_code = args[6:]
                self.assertEqual(code_root,smoke.CODE)
                self.assertEqual(tuple(smoke.json.loads(suite_json)),smoke.MEDIA_FFMPEG_TESTS+smoke.MEDIA_FFPROBE_TESTS)
                fixtures=Path(fixture_path);trusted=Path(trusted_path);env=kwargs['extra_env']
                self.assertIn(' ',str(fixtures));self.assertIn('日本語',str(fixtures))
                self.assertEqual(env['PATH'].split(os.pathsep)[0],str(trusted))
                self.assertEqual({env[name] for name in ('TEMP','TMP','TMPDIR')},{str(fixtures)})
                self.assertEqual(smoke.json.loads(pins_json),hashes)
                self.assertEqual(installed_code,target/'_internal')
                for name,source in sources.items():
                    self.assertEqual((trusted/f'{name}.exe').read_bytes(),source.read_bytes())
                result=self.result();result['ffprobe_available']=True
                return SimpleNamespace(stdout='MEDIA_SMOKE_JSON='+smoke.json.dumps(result)+'\n',stderr='')
            with patch.object(smoke,'checked_media_tools',return_value=sources), \
                    patch.object(smoke,'MEDIA_BINARY_SHA256',hashes),patch.object(smoke,'run',side_effect=run):
                smoke.check_native_media(target,sandbox,report)
            self.assertEqual(len(calls),3)
            self.assertEqual(calls[-1][1]['timeout'],300)
            result=report['native_media_followup']
            self.assertEqual((result['tests_required'],result['tests_passed'],result['tests_skipped']),(12,12,0))
            self.assertEqual((result['required_ffmpeg_passed'],result['required_ffprobe_passed']),(8,4))
            self.assertEqual(result['pinned_media_version'],smoke.MEDIA_VERSION)
            self.assertEqual(result['media_source_url'],smoke.MEDIA_DOWNLOAD_URL)
            self.assertEqual(result['media_archive_sha256'],smoke.MEDIA_ARCHIVE_SHA256)
            for name in ('ffmpeg','ffprobe'):
                self.assertEqual(result['media_tools'][name]['pinned_sha256'],hashes[f'{name}.exe'])
                self.assertEqual(result[f'{name}_sha256'],hashes[f'{name}.exe'])
                self.assertEqual(result[f'{name}_source'],str(sources[name].relative_to(target)))

    def test_old_native_binary_version_is_rejected_before_test_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox=Path(temporary).resolve();target=sandbox/'Sample Installation'
            sources,hashes=self.media_pair(target)
            with patch.object(smoke,'checked_media_tools',return_value=sources), \
                    patch.object(smoke,'MEDIA_BINARY_SHA256',hashes), \
                    patch.object(smoke,'run',return_value=SimpleNamespace(stdout='ffmpeg version 7.1\n',stderr='')) as run:
                with self.assertRaisesRegex(RuntimeError,'does not match pinned'):
                    smoke.check_native_media(target,sandbox,{'checks':[]})
                self.assertEqual(run.call_count,1)


if __name__=='__main__':
    unittest.main()
