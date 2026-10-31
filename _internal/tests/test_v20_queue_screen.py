"""v20 regressions: queue collision policies and desktop capture command contracts."""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Config, choose_output
from queue_policies import rename_destination
from screen_capture import unique_destination
from unified_gui import UnifiedApp


class QueuePolicyTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root=Path(self.dir.name)
        self.source=self.root/'clip.wav';self.source.write_bytes(b'input')
        self.dest=self.root/'clip.txt'
        self.app=UnifiedApp.__new__(UnifiedApp)
        self.app.events=queue.Queue()
        self.app.interactive_collisions=True
        self.app._request_overwrite=MagicMock(return_value='rename')
        self.app.process_lock=threading.Lock()
        self.app.stop_event=threading.Event()
        self.app.active_process=None
        self.cfg=Config(profile='faster-res',txt_metadata_header=False,output_mode='txt')

    def run_one(self,policy):
        src_script=("import pathlib,sys;"
                    "pathlib.Path(sys.argv[1]).write_text('[00:00:00] SPEAKER_00: TEST\\n',encoding='utf-8')")
        with patch('unified_gui.build_command',side_effect=lambda src,out,cfg,py,device:
             [sys.executable,'-c',src_script,str(out)]):
            return self.app._run_one(self.source,self.dest,self.cfg,Path(sys.executable),'','cpu',
                                     collision_policy=policy)

    def test_skip_has_no_call_even_if_no_output(self):
        result,_=self.run_one('skip')
        self.assertEqual(result,'スキップ')
        self.assertFalse(self.dest.exists())

    def test_auto_skip_all_routes_to_next_without_prompt(self):
        self.dest.write_text('OLD',encoding='utf-8')
        self.app.collision_batch_choice='skip'
        self.app._request_overwrite=__import__('types').MethodType(UnifiedApp._request_overwrite,self.app)
        result,_=self.run_one('ask')
        self.assertEqual(result,'スキップ')
        self.assertEqual(self.dest.read_text(encoding='utf-8'),'OLD')
        self.assertEqual(UnifiedApp._request_overwrite(self.app,self.source,[self.dest]),'skip')

    def test_explicit_overwrite(self):
        self.dest.write_text('OLD',encoding='utf-8')
        state,_=self.run_one('overwrite')
        self.assertEqual(state,'完了')
        self.assertIn('TEST',self.dest.read_text(encoding='utf-8'))
        self.app._request_overwrite.assert_not_called()

    def test_rename_preserves_existing(self):
        self.dest.write_text('OLD',encoding='utf-8')
        state,_=self.run_one('rename')
        self.assertEqual(state,'完了')
        self.assertEqual(self.dest.read_text(encoding='utf-8'),'OLD')
        named=list(self.root.glob('clip_リネーム保存_*.txt'))
        self.assertEqual(len(named),1)
        self.assertIn('TEST',named[0].read_text(encoding='utf-8'))

    def test_ask_rename_and_skip(self):
        self.dest.write_text('OLD',encoding='utf-8')
        state,_=self.run_one('ask')
        self.assertEqual(state,'完了')
        self.assertTrue(list(self.root.glob('clip_リネーム保存_*.txt')))
        self.app._request_overwrite.return_value='skip'
        state,_=self.run_one('ask')
        self.assertEqual(state,'スキップ')

    def test_changed_output_name_is_not_skipped(self):
        self.dest.write_text('OLD',encoding='utf-8')
        cfg=Config(profile='faster-res',filename_model=True)
        new=choose_output(self.source,cfg).name
        self.assertNotEqual(new,self.dest.name)
        destination=self.root/new
        self.assertFalse(destination.exists())
        with patch('unified_gui.build_command',side_effect=lambda src,out,cfg,py,device: [sys.executable,'-c',
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('NEW',encoding='utf-8')",
                str(out)]):
            state,_=self.app._run_one(self.source,destination,self.cfg,Path(sys.executable),'','cpu')
        self.assertEqual(state,'完了')
        self.assertEqual(self.dest.read_text(encoding='utf-8'),'OLD')
        self.assertEqual(destination.read_text(encoding='utf-8'),'NEW')

    def test_rename_checks_both_extensions(self):
        self.dest.with_suffix('.csv').write_text('old',encoding='utf-8')
        result=rename_destination(self.dest,'both',datetime(2000,1,22,14,0))
        self.assertNotIn(result.name,('clip.txt','clip.csv'))
        result.write_text('new',encoding='utf-8')
        result2=rename_destination(self.dest,'both',datetime(2000,1,22,14,0))
        self.assertNotEqual(result,result2)
