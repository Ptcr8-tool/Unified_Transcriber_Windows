"""Run with: python -m unittest discover -s tests -v"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import (BY_ID, Config, PROFILES, build_command, engine_path,
                  choose_output, env_python, load_config, save_config, redact)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.audio=self.base/'日本語 スペース.wav'
        self.audio.write_bytes(b'dummy')
        self.out=self.base/'結果 保存.txt'
        self.python=self.base/'python.exe'

    def test_five_profiles_are_distinct(self):
        self.assertEqual(len(PROFILES),5)
        self.assertEqual(len(BY_ID),5)
        self.assertTrue(all(engine_path(p.id).is_file() for p in PROFILES))

    def test_default_is_no_overwrite(self):
        self.assertFalse(Config().overwrite)
        self.assertEqual(Config().profile,'faster-res')

    def test_command_preserves_spaces_without_shell(self):
        cfg=Config(profile='openai-res')
        args=build_command(self.audio,self.out,cfg,self.python,'cpu')
        self.assertEqual(args[0],str(self.python))
        self.assertEqual(args[3],str(self.audio))
        self.assertEqual(args[5],str(self.out))
        self.assertIn('--overwrite',args)
        self.assertIn('--threshold',args)

    def test_legacy_fixed_speakers(self):
        cfg=Config(speaker_mode='fixed',speakers=3)
        args=build_command(self.audio,self.out,cfg,self.python,'cuda')
        self.assertEqual(args[args.index('--speakers')+1],'3')
        self.assertEqual(args[args.index('--device')+1],'cuda')

    def test_res_fixed_segments(self):
        cfg=Config(profile='faster-res',segment_length=11.0)
        args=build_command(self.audio,self.out,cfg,self.python,'cpu')
        self.assertEqual(args[args.index('--segment-length')+1],'11.0')
        self.assertNotIn('--speakers',args)
        self.assertNotIn('--qwen-model',args)

    def test_pyannote_does_not_pass_token_as_arg(self):
        cfg=Config(profile='faster-pya',merge_gap=0.3)
        args=build_command(self.audio,self.out,cfg,self.python,'cpu')
        self.assertNotIn('--segment-length',args)
        self.assertIn('--merge-gap',args)
        self.assertNotIn('HF_TOKEN',' '.join(args))

    def test_qwen_res_command(self):
        cfg=Config(profile='qwen-res')
        args=build_command(self.audio,self.out,cfg,self.python,'cuda')
        self.assertEqual(args[args.index('--asr-backend')+1],'qwen')
        self.assertIn('--qwen-aligner',args)
        self.assertIn('--segment-length',args)

    def test_qwen_pya_command(self):
        cfg=Config(profile='qwen-pya')
        args=build_command(self.audio,self.out,cfg,self.python,'cuda')
        self.assertEqual(args[args.index('--asr-backend')+1],'qwen')
        self.assertNotIn('--segment-length',args)

    def test_output_folder_and_unicode(self):
        cfg=Config(output_dir=str(self.base/'output dir'))
        self.assertEqual(choose_output(self.audio,cfg),self.base/'output dir'/'日本語 スペース.txt')
        self.assertEqual(choose_output(self.audio,cfg,watch_processed=self.base/'processed'),
                         self.base/'processed'/'日本語 スペース.txt')

    def test_config_does_not_save_credentials(self):
        cfg=Config(profile='qwen-pya')
        target=self.base/'nested'/'settings.json'
        save_config(cfg,target)
        serialized=target.read_text(encoding='utf-8')
        self.assertNotIn('HF_TOKEN',serialized)
        self.assertNotIn('hf_token',serialized)
        self.assertEqual(load_config(target).profile,'qwen-pya')

    def test_unknown_config_items_ignored(self):
        target=self.base/'settings.json'
        target.write_text(json.dumps({'profile':'qwen-res','old_app_value':9,
                                      'python_overrides':{'qwen-res':'C:/venv/python.exe'}}))
        self.assertEqual(load_config(target).profile,'qwen-res')

    def test_new_preferences_persist_and_old_settings_receive_defaults(self):
        target=self.base/'settings.json'
        target.write_text(json.dumps({'profile':'faster-res','model_faster':'small'}))
        cfg=load_config(target)
        self.assertEqual(cfg.model_faster,'small')
        self.assertEqual(cfg.theme_mode,'system')
        self.assertFalse(cfg.auto_open_speaker)
        cfg.theme_mode='dark';cfg.auto_open_speaker=True;cfg.output_same_as_input=True
        cfg.onboarding_seen=True;cfg.capture_setup_complete=True
        save_config(cfg,target)
        self.assertEqual(load_config(target),cfg)

    def test_input_adjacent_destination_preserves_watch_folder_priority(self):
        cfg=Config(output_dir=str(self.base/'elsewhere'),output_same_as_input=True)
        self.assertEqual(choose_output(self.audio,cfg).parent,self.audio.parent)
        self.assertEqual(choose_output(self.audio,cfg,watch_processed=self.base/'processed').parent,
                         self.base/'processed')

    def test_invalid_configuration_rejected(self):
        for c in (Config(max_speakers=0),Config(threshold=-2),Config(segment_length=0),
                  Config(merge_gap=-1),Config(profile='unrecognized')):
            with self.subTest(c=c):
                with self.assertRaises(ValueError):c.validate()

    def test_token_redaction(self):
        self.assertEqual(redact('failed hf-secret!', 'hf-secret'),'failed [HF_TOKEN非表示]!')

    def test_environment_overrides(self):
        cfg=Config(python_overrides={'qwen-res':'C:/legacy/.venv-qwen/Scripts/python.exe'})
        self.assertEqual(env_python('qwen-res',cfg), Path('C:/legacy/.venv-qwen/Scripts/python.exe'))


if __name__=='__main__':unittest.main()
