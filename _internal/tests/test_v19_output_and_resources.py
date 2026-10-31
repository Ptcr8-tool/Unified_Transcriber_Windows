"""v19 regression tests for job specific settings and safe resource limits."""
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Config, choose_output, load_config, save_config
from resource_limits import WindowsCpuLimit, write_gpu_pacing
from unified_gui import UnifiedApp
from engines import engine_load


class PerFileTests(unittest.TestCase):
    def test_job_snapshot_respects_each_output_destination_and_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            first=root/'第一音声.mp3';second=root/'第二音声.wav'
            first.write_bytes(b'one');second.write_bytes(b'two')
            app=UnifiedApp.__new__(UnifiedApp)
            app.events=queue.Queue()
            app.stop_event=__import__('threading').Event()
            app._resolve_device=lambda cfg,py:'cpu'
            seen=[]
            def run_one(source,dest,cfg,*args,**kwargs):
                seen.append((source.name,dest,cfg.output_mode))
                return '完了','OK'
            app._run_one=run_one
            options={str(first):('csv',str(root/'CSV')),str(second):('both',str(root/'TXTとCSV'))}
            app._batch_worker([first,second],Config(profile='faster-res'),Path('python'),'token',options)
            self.assertEqual([r[2] for r in seen],['csv','both'])
            self.assertEqual([r[1].parent for r in seen],[root/'CSV',root/'TXTとCSV'])
            self.assertEqual([r[1].suffix for r in seen],['.txt','.txt'])
            self.assertEqual(app.events.get()[0],'log')

    def test_defaults_are_valid_and_optional(self):
        cfg=Config()
        self.assertEqual((cfg.cpu_limit_percent,cfg.gpu_soft_percent),(None,None))
        cfg.validate()
        for pct in (0,101,'50',True):
            with self.assertRaises(ValueError):Config(cpu_limit_percent=pct).validate()
            with self.assertRaises(ValueError):Config(gpu_soft_percent=pct).validate()

    def test_limits_survive_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'settings.json'
            save_config(Config(cpu_limit_percent=30,gpu_soft_percent=60),p)
            loaded=load_config(p)
            self.assertEqual((loaded.cpu_limit_percent,loaded.gpu_soft_percent),(30,60))

    def test_gpu_runtime_json_updates_and_pacing_is_dynamic(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'gpu.json'
            write_gpu_pacing(path,None)
            with patch.dict(os.environ,{'TRANSCRIBER_RESOURCE_CONTROL':str(path)}):
                engine_load._last_asr_time=None
                engine_load.pace_if_needed('asr')
                write_gpu_pacing(path,50)
                with patch('engines.engine_load.time.monotonic',return_value=engine_load._last_asr_time+0.2),patch('engines.engine_load.time.sleep') as sleeper:
                    engine_load.pace_if_needed('asr')
                    sleeper.assert_called_once()
                    self.assertAlmostEqual(sleeper.call_args.args[0],0.2,places=5)
                write_gpu_pacing(path,None)
                with patch('engines.engine_load.time.sleep') as sleeper:
                    engine_load.pace_if_needed('asr')
                    sleeper.assert_not_called()
        engine_load._last_asr_time=None

    def test_job_object_disabled_outside_windows(self):
        with patch('resource_limits.os.name','posix'):
            self.assertIn('無効',WindowsCpuLimit().attach(321,None))
