"""Batch setup validation: offline tests, no PyTorch or models downloaded."""
import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import BY_ID, QWEN_MODEL
from bulk_catalog import BulkPlan, DEFAULT_MODELS, MODEL_CHOICES, preset, planned_summary
import bulk_setup
import preload_model


class PlanTests(unittest.TestCase):
    def test_presets_all_cpu_gpu(self):
        for label, hardware in [('all-cuda', 'cuda'), ('all-cpu','cpu')]:
            plan = preset(label)
            self.assertEqual(len(plan.profiles), 5)
            self.assertEqual({device for _, device in plan.profiles}, {hardware})
            self.assertEqual(len(set(plan.models)), len(plan.models))
            self.assertEqual({m for k,m in plan.models if k == 'openai'}, {'turbo'})

    def test_recommended_installs_only_two_profiles(self):
        plan = preset('recommended-cuda')
        self.assertEqual(plan.profiles, (('faster-res','cuda'),('qwen-res','cuda')))
        self.assertEqual(len(plan.models),2)

    def test_reject_duplicate_profile(self):
        with self.assertRaisesRegex(ValueError,'重複'):
            BulkPlan((('faster-res','cpu'),('faster-res','cuda')),()).validate()

    def test_reject_unsupported_model(self):
        with self.assertRaisesRegex(ValueError,'未対応'):
            BulkPlan((('faster-res','cuda'),),(('faster','something-unknown'),)).validate()

    def test_reject_models_for_unselected_backend(self):
        with self.assertRaises(ValueError):
            BulkPlan((('openai-res','cpu'),),(('qwen',QWEN_MODEL),)).validate()

    def test_reject_invalid_device(self):
        with self.assertRaises(ValueError):
            BulkPlan((('faster-res','tpu'),),()).validate()

    def test_allow_empty_models_for_env_only(self):
        BulkPlan((('qwen-res','cpu'),),()).validate()

    def test_expected_planned_summary(self):
        self.assertIn('5件',planned_summary(preset('all-cpu')))
        self.assertIn('pyannote',planned_summary(preset('all-cpu')))

    def test_models_prefetched_once_per_backend_not_per_profile(self):
        plan=preset('all-cuda')
        tasks=bulk_setup.command_sequence(plan)
        self.assertEqual(len(tasks),5+3+1)
        qwen=[x for x in tasks if x[0].startswith('モデル: neosophie')]
        self.assertEqual(len(qwen),1)
        self.assertEqual(qwen[0][2],'qwen-res')

    def test_per_profile_hardware_commands(self):
        plan=BulkPlan((('faster-res','cuda'),('qwen-pya','cpu')), (('faster','small'),('qwen',QWEN_MODEL)))
        tasks=bulk_setup.command_sequence(plan)
        self.assertEqual([x[1][-1] for x in tasks[:2]], ['cuda','cpu'])

    def test_parse_colon_split_once(self):
        self.assertEqual(bulk_setup.parse_pair('a:b:c'),('a','b:c'))
        with self.assertRaises(Exception):bulk_setup.parse_pair('invalid')

    def test_cli_explicit_dry_run_is_offline(self):
        argv=['bulk_setup.py','--profile','faster-res:cuda','--profile','qwen-res:cpu',
              '--model','faster:large-v3-turbo','--model',f'qwen:{QWEN_MODEL}','--dry-run']
        with patch.object(sys,'argv',argv), contextlib.redirect_stdout(io.StringIO()) as output:
            rc=bulk_setup.main()
        self.assertEqual(rc,0)
        self.assertIn('DRY-RUN',output.getvalue())

    def test_failure_of_one_engine_skips_its_download_and_continues(self):
        plan=BulkPlan((('faster-res','cpu'),('qwen-res','cpu')),
                      (('faster','large-v3-turbo'),('qwen',QWEN_MODEL)))
        with tempfile.TemporaryDirectory() as d:
            env_root=Path(d)
            for p in ('faster-res','qwen-res'):
                py=env_root/p/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
                py.parent.mkdir(parents=True)
                py.touch()
            outputs=[SimpleNamespace(returncode=i) for i in (1,0,0)]
            with patch('core.environment_folder',side_effect=lambda key:env_root/key), \
                 patch('bulk_setup.run_logged',side_effect=outputs) as sub, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                rc=bulk_setup.run_plan(plan)
            self.assertEqual(rc,1)
            self.assertEqual(sub.call_count,3)
            self.assertIn('SKIP',output.getvalue())
            self.assertIn('Qwen',output.getvalue())


class PreloadTests(unittest.TestCase):
    def test_invalid_model_does_not_import_ml_libraries(self):
        with self.assertRaisesRegex(ValueError,'許可'):
            preload_model.preload('faster', 'unknown')

    def test_faster_uses_official_download_model(self):
        from types import ModuleType
        pkg=ModuleType('faster_whisper')
        utils=ModuleType('faster_whisper.utils')
        calls=[]
        def download_model(model, revision=None):
            calls.append((model, {'revision': revision}))
        utils.download_model=download_model
        with patch.dict(sys.modules,{'faster_whisper':pkg,'faster_whisper.utils':utils}):
            preload_model.preload('faster','large-v3-turbo')
        from model_defaults import FASTER_TURBO_REPO, FASTER_TURBO_REVISION
        self.assertEqual(calls, [(FASTER_TURBO_REPO, {'revision': FASTER_TURBO_REVISION})])

    def test_qwen_prefetches_asr_and_forced_aligner_once(self):
        from types import ModuleType
        hub=ModuleType('huggingface_hub')
        calls=[]
        hub.snapshot_download=lambda **kw: calls.append(kw)
        with patch.dict(sys.modules,{'huggingface_hub':hub}):
            preload_model.preload('qwen',QWEN_MODEL)
        self.assertEqual([x['repo_id'] for x in calls],[QWEN_MODEL, 'Qwen/Qwen3-ForcedAligner-0.6B'])

    def test_pyannote_requires_token_first(self):
        with self.assertRaisesRegex(ValueError,'HF_TOKEN'):
            preload_model.preload('pyannote')

    def test_openai_uses_standard_model_cache_downloader(self):
        from types import ModuleType
        w=ModuleType('whisper')
        calls=[]
        w.available_models=lambda:['turbo']
        w._MODELS={'turbo':'https://example.invalid/turbo.pt'}
        w._download=lambda *args: calls.append(args)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{'whisper':w}), \
             patch.dict(os.environ,{'XDG_CACHE_HOME':tmp}):
            preload_model.preload('openai','turbo')
        self.assertEqual(calls[0][0],w._MODELS['turbo'])
        self.assertTrue(calls[0][1].endswith('whisper'))


if __name__ == '__main__':unittest.main()

class AlternateHostTests(unittest.TestCase):
    def test_model_fallback_to_second_successful_profile(self):
        plan=BulkPlan((('faster-res','cpu'),('faster-pya','cpu')),
                      (('faster','large-v3-turbo'),),False)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for profile in ('faster-res','faster-pya'):
                python=root/profile/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
                python.parent.mkdir(parents=True)
                python.touch()
            with patch('core.environment_folder',side_effect=lambda key:root/key), \
                 patch('bulk_setup.run_logged',side_effect=[SimpleNamespace(returncode=1),
                                                                SimpleNamespace(returncode=0),
                                                                SimpleNamespace(returncode=0)]) as proc, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                rc=bulk_setup.run_plan(plan)
            self.assertEqual(rc,1)  # environment failure remains reported
            self.assertEqual(proc.call_count,3)  # prefetch still happens
            expected=root/'faster-pya'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            self.assertEqual(proc.call_args.args[0][0],str(expected))
            self.assertIn('代替環境',output.getvalue())

class GuiDispatchTests(unittest.TestCase):
    def test_bulk_worker_transfers_individual_choices_and_token_without_cli_secret(self):
        import io, queue, threading
        from unittest.mock import MagicMock
        import unified_gui
        app=MagicMock()
        app.events=queue.Queue()
        app.process_lock=threading.Lock()
        app.stop_event=threading.Event()
        ctx=MagicMock()
        ctx.__enter__.return_value.stdout=io.StringIO('OK: completed\n')
        ctx.__enter__.return_value.wait.return_value=0
        with patch('unified_gui.subprocess.Popen',return_value=ctx) as popen:
            unified_gui.UnifiedApp._bulk_setup_worker(app,
                BulkPlan((('faster-res','cuda'),('qwen-res','cpu')),
                         (('faster','large-v3-turbo'),('qwen',QWEN_MODEL)),False), 'private_token')
        cmd=popen.call_args.args[0]
        self.assertIn('faster-res:cuda',cmd)
        self.assertIn('qwen-res:cpu',cmd)
        self.assertIn('qwen:'+QWEN_MODEL,cmd)
        self.assertIn('--no-pyannote-model',cmd)
        self.assertNotIn('private_token',str(cmd))
        self.assertEqual(popen.call_args.kwargs['env']['HF_TOKEN'],'private_token')
        self.assertTrue(any(ev[0]=='done' for ev in list(app.events.queue)))
