"""Security contracts for update metadata, transport, verification and launch."""
import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_updates as updates


VERSION = '2.1.0'
NAME = f'Unified_Transcriber_Setup_v{VERSION}.exe'
PAYLOAD = b'MZ synthetic setup; never executed'
BUNDLE = b'{"synthetic":"test fixture, not a valid attestation"}'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
CHECKSUMS = f'{DIGEST}  {NAME}\n'.encode('ascii')


def asset(name, data):
    return dict(name=name, state='uploaded', size=len(data),
                digest='sha256:'+hashlib.sha256(data).hexdigest(),
                browser_download_url=f'{updates.RELEASE_BASE}download/v{VERSION}/{name}')


def release():
    return dict(tag_name='v'+VERSION, draft=False, prerelease=False,
                html_url=f'{updates.RELEASE_BASE}tag/v{VERSION}',
                assets=[asset(NAME, PAYLOAD), asset('SHA256SUMS.txt', CHECKSUMS),
                        asset(NAME+'.sigstore.json', BUNDLE)])


class Response(io.BytesIO):
    def __init__(self, data, url, *, headers=None, status=200):
        super().__init__(data)
        self.url, self.status = url, status
        self.headers = headers if headers is not None else {'Content-Length': str(len(data))}

    def geturl(self):
        return self.url


class Transport:
    def __init__(self, data=None):
        self.data = data or {NAME: PAYLOAD, 'SHA256SUMS.txt': CHECKSUMS,
                             NAME+'.sigstore.json': BUNDLE}
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return Response(self.data[request.full_url.rsplit('/', 1)[-1]], request.full_url)


class PublicRepositoryIdentity(unittest.TestCase):
    def test_build_and_updater_use_the_same_public_repository_and_workflow(self):
        path = Path(__file__).resolve().parents[1]/'tools/build_windows.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'REPOSITORY'
                               for target in node.targets)]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(ast.literal_eval(assignments[0].value), updates.REPOSITORY)
        self.assertEqual(updates.REPOSITORY, 'Ptcr8-tool/Unified_Transcriber_Windows')
        self.assertEqual(updates.SIGNER_WORKFLOW,
                         updates.REPOSITORY+'/.github/workflows/windows-release.yml')

    def test_wrong_repository_identity_never_executes_the_verifier(self):
        identity = json.dumps(dict(id=123, full_name='other/repository', private=False)).encode()
        info = updates.UpdateInfo(VERSION, 'v'+VERSION, '', NAME, '', DIGEST, 1,
                                  '', DIGEST, 1, '', DIGEST, 1)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(updates, '_trust_manifest', return_value=(Path(directory), {'repository_id':123})), \
             patch.object(updates, '_read_response', return_value=identity), \
             patch.object(updates.subprocess, 'run') as run, \
             self.assertRaises(updates.UpdateSecurityError):
            updates.verify_github_provenance(Path(directory)/NAME, Path(directory)/'bundle.json', info)
        run.assert_not_called()


class Scheduling(unittest.TestCase):
    def test_period_first_every_never_and_timezones(self):
        now = datetime(2000, 1, 15, tzinfo=timezone.utc)
        self.assertTrue(updates.is_check_due('days', 7, now=now))
        self.assertTrue(updates.is_check_due('every', 7, now.isoformat(), now=now))
        self.assertFalse(updates.is_check_due('never', 7, now=now))
        self.assertFalse(updates.is_check_due('days', 7, (now-timedelta(days=6)).isoformat(), now=now))
        self.assertTrue(updates.is_check_due('days', 7, (now-timedelta(days=7)).isoformat(), now=now))
        self.assertTrue(updates.is_check_due('days', 7, (now+timedelta(days=1)).isoformat(), now=now))
        for malformed in ('bad timestamp', '2000-01-01', None):
            self.assertTrue(updates.is_check_due('days', 7, malformed, now=now))

    def test_invalid_preferences_rejected(self):
        for mode, days in (('arbitrary', 7), ('days', 0), ('days', 366), ('days', True)):
            with self.assertRaises(ValueError):
                updates.is_check_due(mode, days)


class Metadata(unittest.TestCase):
    def test_strict_versions_numerically_compare_without_downgrade(self):
        self.assertEqual(updates.parse_version('2.10.0'), (2, 10, 0))
        for value in ('2.01.0', 'v2.1.0', '2.1.0-rc1', '2.1.0+build', '2.1', '-1.0.0',
                      '2.1.0\n', '2.1.0;calc', '1000000.0.0', None):
            with self.assertRaises(updates.UpdateSecurityError):
                updates.parse_version(value)
        self.assertIsNone(updates.parse_release(release(), VERSION))
        self.assertIsNone(updates.parse_release(release(), '2.10.0'))
        info = updates.parse_release(release(), '2.0.0')
        self.assertEqual((info.version, info.setup_sha256, info.setup_size), (VERSION, DIGEST, len(PAYLOAD)))
        self.assertTrue(info.has_provenance)

    def test_ambiguous_assets_release_flags_urls_and_digests_are_rejected(self):
        mutations = [lambda r:r.update(draft=True), lambda r:r.update(prerelease=True),
                     lambda r:r.update(draft=0), lambda r:r.update(tag_name='v2.1.0;calc'),
                     lambda r:r.update(html_url='https://github.com/unrelated/repo/releases/tag/v2.1.0'),
                     lambda r:r['assets'].append(r['assets'][0]),
                     lambda r:r['assets'][0].update(size=True),
                     lambda r:r['assets'][0].update(size=updates.INSTALLER_LIMIT+1),
                     lambda r:r['assets'][0].update(digest='sha256:invalid'),
                     lambda r:r['assets'][0].update(browser_download_url='file:///tmp/setup.exe'),
                     lambda r:r['assets'][0].update(browser_download_url='https://github.com.evil.test/setup.exe'),
                     lambda r:r['assets'][0].update(browser_download_url=r['assets'][0]['browser_download_url']+'?token=x'),
                     lambda r:r['assets'][0].update(name='../'+NAME)]
        for mutate in mutations:
            data = release()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(updates.UpdateSecurityError):
                updates.parse_release(data, '2.0.0')

    def test_public_check_sends_no_credentials_and_private_404_is_unavailable(self):
        opener = Mock()
        opener.open.return_value = Response(json.dumps(release()).encode(), updates.RELEASE_API)
        self.assertIsNotNone(updates.check_latest('2.0.0', opener=opener))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, updates.RELEASE_API)
        self.assertNotIn('Authorization', dict(request.header_items()))
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], updates.REQUEST_TIMEOUT)
        opener.open.side_effect = HTTPError(updates.RELEASE_API, 404, 'not found', {}, None)
        with self.assertRaises(updates.UpdateUnavailable):
            updates.check_latest('2.0.0', opener=opener)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(updates.UpdateSecurityError):
            updates._load_json(b'{"draft":false,"draft":true}')

    def test_tls_failure_is_identified_without_echoing_private_error_text(self):
        opener = Mock()
        opener.open.side_effect = updates.URLError(updates.ssl.SSLCertVerificationError(
            1, 'sample-private-proxy-credential'))
        with self.assertRaisesRegex(updates.UpdateUnavailable, 'TLS証明書') as error:
            updates.check_latest('2.0.0', opener=opener)
        self.assertNotIn('sample-private-proxy-credential', str(error.exception))
        self.assertIn('手動', str(error.exception))

    def test_timeout_is_distinct_from_private_or_unpublished_release(self):
        opener = Mock()
        opener.open.side_effect = updates.URLError(TimeoutError('sample private host'))
        with self.assertRaisesRegex(updates.UpdateUnavailable, 'タイムアウト') as error:
            updates.check_latest('2.0.0', opener=opener)
        self.assertNotIn('sample private host', str(error.exception))


class TransportGuards(unittest.TestCase):
    def test_redirects_only_allow_exact_github_asset_and_known_https_cdn(self):
        url = release()['assets'][0]['browser_download_url']
        handler = updates._SafeRedirects(url, asset=True)
        request = Request(url, headers={'Authorization':'must-not-be-forwarded'})
        safe = handler.redirect_request(request, None, 302, '', {},
            'https://release-assets.githubusercontent.com/github-production-release-asset/asset?sig=test')
        self.assertNotIn('Authorization', dict(safe.header_items()))
        for bad in ('http://release-assets.githubusercontent.com/a', 'https://github.com/other/file',
                    'https://evil.test/file', 'https://github.com@evil.test/file',
                    'https://release-assets.githubusercontent.com:444/file',
                    'https://release-assets.githubusercontent.com/\nfile', 'file:///tmp/a'):
            with self.subTest(bad=bad), self.assertRaises(updates.UpdateSecurityError):
                handler.redirect_request(request, None, 302, '', {}, bad)
        with self.assertRaises(updates.UpdateSecurityError):
            updates._SafeRedirects(updates.RELEASE_API, asset=False).redirect_request(
                Request(updates.RELEASE_API), None, 302, '', {}, 'https://api.github.com/repos/other/repo')

    def test_reject_compression_oversized_truncated_or_wrong_final_url(self):
        opener = Mock()
        for data, headers, final in ((b'{}', {'Content-Encoding':'br'}, updates.RELEASE_API),
                (b'{}', {'Content-Length':'100'}, updates.RELEASE_API),
                (b'{}', {'Content-Length':'-1'}, updates.RELEASE_API),
                (b'abcdef', {}, updates.RELEASE_API),
                (b'{}', {}, 'https://evil.test/update')):
            opener.open.return_value = Response(data, final, headers=headers)
            with self.subTest(headers=headers, final=final), self.assertRaises(updates.UpdateSecurityError):
                updates._read_response(updates.RELEASE_API, 5, asset=False, opener=opener)
        opener.open.return_value = Response(b'ab', updates.RELEASE_API, headers={})
        with self.assertRaises(updates.UpdateSecurityError):
            updates._read_response(updates.RELEASE_API, 5, asset=False, opener=opener, expected_size=3)


class DownloadAndLaunch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.info = updates.parse_release(release(), '2.0.0')

    def test_signed_verified_installer_only_then_local_rehash_before_launch(self):
        verifier = Mock()
        transport = Transport()
        verified = updates.download_verified(self.info, self.directory,
                                              opener=transport, provenance_verifier=verifier)
        self.assertEqual(verified.path.read_bytes(), PAYLOAD)
        verifier.assert_called_once_with(verified.path, verified.bundle_path, self.info)
        self.assertEqual(len(transport.requests), 3)
        target = self.directory/'folder with & spaces'
        with patch.object(updates.subprocess, 'Popen') as launch:
            updates.launch_installer(verified, target, wait_pid=123)
            command = launch.call_args.args[0]
            self.assertEqual(command, [str(verified.path.absolute()), '--target', str(target.absolute()),
                                       '--update', '--wait-pid', '123'])
            self.assertIs(launch.call_args.kwargs['shell'], False)
        verified.path.write_bytes(b'MZ tampered')
        with patch.object(updates.subprocess, 'Popen') as launch, self.assertRaises(updates.UpdateSecurityError):
            updates.launch_installer(verified, target)
        launch.assert_not_called()

    def test_missing_provenance_is_not_downgraded_to_checksum_only(self):
        info = replace(self.info, bundle_url='', bundle_sha256='', bundle_size=0)
        transport = Transport()
        with self.assertRaises(updates.UpdateSecurityError):
            updates.download_verified(info, self.directory, opener=transport)
        self.assertEqual(transport.requests, [])

    def test_hash_failure_and_provenance_failure_cleanup_without_launch(self):
        bad_checksum = f'{"0"*64}  {NAME}\n'.encode()
        cases = (({NAME:b'MZ tampered', 'SHA256SUMS.txt':CHECKSUMS, NAME+'.sigstore.json':BUNDLE}, self.info, Mock()),
                 ({NAME:PAYLOAD, 'SHA256SUMS.txt':bad_checksum, NAME+'.sigstore.json':BUNDLE},
                  replace(self.info, checksums_sha256=hashlib.sha256(bad_checksum).hexdigest()), Mock()),
                 (None, self.info, Mock(side_effect=updates.UpdateSecurityError('invalid attestation'))))
        for data, info, verifier in cases:
            with self.subTest(data=data), self.assertRaises(updates.UpdateSecurityError):
                updates.download_verified(info, self.directory, opener=Transport(data), provenance_verifier=verifier)
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_malicious_version_filename_checksum_and_verifier_modification(self):
        info = replace(self.info, setup_name='../../bad.exe')
        with self.assertRaises(updates.UpdateSecurityError):
            updates.download_verified(info, self.directory, opener=Transport())
        for contents in ((CHECKSUMS+CHECKSUMS), b'not checksums', b'\xff'):
            with self.assertRaises(updates.UpdateSecurityError):
                updates._expected_checksum(contents, NAME)
        def corrupt(path, _bundle, _info):
            path.write_bytes(b'MZ replaced during verification')
        with self.assertRaises(updates.UpdateSecurityError):
            updates.download_verified(self.info, self.directory, opener=Transport(), provenance_verifier=corrupt)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_provenance_command_pins_workflow_tag_root_and_ignores_personal_tokens(self):
        root = self.directory/'trusted'
        root.mkdir()
        (root/'gh.exe').write_bytes(b'test verifier')
        result = [{'verificationResult': {'verifiedTimestamps':[{'type':'test'}],
                    'signature':{'certificate':{'sourceRepositoryIdentifier':'123'}},
                    'statement':{'subject':[{'digest':{'sha256':DIGEST}}]}}}]
        checked = subprocess.CompletedProcess([], 0, json.dumps(result).encode(), b'')
        identity = json.dumps(dict(id=123, full_name=updates.REPOSITORY, private=False)).encode()
        with patch.object(updates, '_trust_manifest', return_value=(root, {'repository_id':123})), \
             patch.object(updates, '_read_response', return_value=identity), \
             patch.dict(updates.os.environ, {'GH_TOKEN':'must-not-pass', 'HF_TOKEN':'must-not-pass',
                                             'HTTPS_PROXY':'must-not-pass'}), \
             patch.object(updates.subprocess, 'run', return_value=checked) as run:
            updates.verify_github_provenance(self.directory/NAME, self.directory/'bundle.json', self.info)
            command = run.call_args.args[0]
            self.assertEqual(command[0], str(root/'gh.exe'))
            self.assertIn('--bundle', command)
            self.assertIn('--custom-trusted-root', command)
            self.assertIn(updates.SIGNER_WORKFLOW, command)
            self.assertIn('refs/tags/v'+VERSION, command)
            self.assertIn('--deny-self-hosted-runners', command)
            self.assertIs(run.call_args.kwargs['shell'], False)
            self.assertFalse({'GH_TOKEN','HF_TOKEN','HTTPS_PROXY'} & run.call_args.kwargs['env'].keys())

    def test_old_or_missing_certificate_repository_id_is_rejected(self):
        identity = json.dumps(dict(id=123, full_name=updates.REPOSITORY, private=False)).encode()
        for certificate in ({}, {'sourceRepositoryIdentifier':'999'}):
            result = [{'verificationResult': {'verifiedTimestamps':[{'type':'test'}],
                        'signature':{'certificate':certificate},
                        'statement':{'subject':[{'digest':{'sha256':DIGEST}}]}}}]
            checked = subprocess.CompletedProcess([], 0, json.dumps(result).encode(), b'')
            with patch.object(updates, '_trust_manifest', return_value=(self.directory, {'repository_id':123})), \
                 patch.object(updates, '_read_response', return_value=identity), \
                 patch.object(updates.subprocess, 'run', return_value=checked), \
                 self.subTest(certificate=certificate), self.assertRaises(updates.UpdateSecurityError):
                updates.verify_github_provenance(self.directory/NAME, self.directory/'bundle.json', self.info)


if __name__ == '__main__':
    unittest.main()
