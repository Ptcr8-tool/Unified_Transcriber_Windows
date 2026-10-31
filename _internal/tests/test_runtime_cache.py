"""Cache integrity failures must stop before any cached code is executed."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS=Path(__file__).resolve().parents[1]/'tools'
sys.path.insert(0,str(TOOLS))
import runtime_cache as cache


class RuntimeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work=Path(self.temporary.name)
        (self.work/'Runtime/Lib').mkdir(parents=True)
        (self.work/'Runtime/python.exe').write_bytes(b'sample authenticated executable')
        (self.work/'Runtime/Lib/sample.py').write_text('sample=True\n')
        locked=json.loads((cache.REPO/'_internal/tools/cpython-windows-sources.json').read_text())
        self.identity={'version':'3.11.18','openssl_version_info':[3,5,0,9,0],
            'openssl_version_number':'0x30500090','sqlite':'3.53.4','tk':'8.6.18','zlib':'1.3.2',
            'source_build':{'source':locked,'security_regression':'test.test_ssl.SSLObjectTests.test_distrusted_spki passed'}}
        (self.work/'runtime-info.json').write_text(json.dumps(self.identity))
        self.environment=patch.dict(os.environ,{'ImageVersion':'20261021.278.1',
            'GITHUB_REPOSITORY_ID':'12345','GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'1234',
            'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
            'PRIVATE_REPOSITORY':'true'})
        self.environment.start();self.addCleanup(self.environment.stop)
        cache.seal(self.work)

    def test_exact_private_cache_bytes_and_identity_pass(self):
        self.assertEqual(cache.verify_bytes(self.work,cache.header()),self.identity)

    def test_changed_missing_and_injected_files_stop_before_execution(self):
        original=(self.work/'Runtime/python.exe').read_bytes()
        for mutation in ('change','remove','inject'):
            with self.subTest(mutation=mutation):
                (self.work/'Runtime/python.exe').write_bytes(original)
                (self.work/'Runtime/extra.py').unlink(missing_ok=True)
                if mutation=='change':(self.work/'Runtime/python.exe').write_bytes(b'altered')
                elif mutation=='remove':(self.work/'Runtime/python.exe').unlink()
                else:(self.work/'Runtime/extra.py').write_text('injected')
                with patch.object(cache.build,'probe_runtime') as probe,self.assertRaises(ValueError):
                    cache.verify(self.work)
                probe.assert_not_called()

    def test_input_digest_mismatch_stops_before_execution(self):
        with patch.dict(os.environ,{'ImageVersion':'20261022.1'}),\
             patch.object(cache.build,'probe_runtime') as probe,self.assertRaises(ValueError):
            cache.verify(self.work)
        probe.assert_not_called()

    def test_old_identity_cannot_be_hidden_by_rehashing_manifest(self):
        self.identity['version']='3.11.9'
        (self.work/'runtime-info.json').write_text(json.dumps(self.identity))
        manifest=json.loads((self.work/cache.MANIFEST).read_text())
        manifest['files']=cache.inventory(self.work)
        (self.work/cache.MANIFEST).write_text(json.dumps(manifest))
        with patch.object(cache.build,'probe_runtime') as probe,self.assertRaises(RuntimeError):
            cache.verify(self.work)
        probe.assert_not_called()

    def test_public_pull_request_and_tag_cache_use_is_rejected(self):
        for changed in ({'PRIVATE_REPOSITORY':'false'},{'GITHUB_EVENT_NAME':'pull_request'},
                        {'GITHUB_REF':'refs/tags/v2.1.0'}):
            with self.subTest(changed=changed),patch.dict(os.environ,changed),\
                 patch.object(cache.build,'probe_runtime') as probe,self.assertRaises(ValueError):
                cache.verify(self.work)
            probe.assert_not_called()

    def test_windows_reparse_point_is_rejected_without_following_it(self):
        info=SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
        with patch.object(Path,'lstat',return_value=info),self.assertRaises(ValueError):
            cache.ordinary(self.work/'Runtime')

    def test_inexact_discard_preserves_unrelated_work_files(self):
        unrelated=self.work/'unrelated.txt';unrelated.write_text('preserve')
        cache.discard(self.work)
        self.assertEqual(unrelated.read_text(),'preserve')
        self.assertFalse((self.work/'Runtime').exists())
        self.assertFalse((self.work/cache.MANIFEST).exists())


if __name__=='__main__':unittest.main()
