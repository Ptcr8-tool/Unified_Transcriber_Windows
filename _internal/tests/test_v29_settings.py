"""Portable settings across Windows users, install paths and incomplete installations."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Config,save_config,load_config
from capture_preferences import saved_options
from settings_transfer import export_settings,import_settings,write_export,FORMAT

OLD=r'C:\Users\SampleUserA\Documents\Unified_Transcriber_Windows'
NEW=r'C:\Users\SampleUserB\movie\Unified_Transcriber_Windows'
OLD_HOME=r'C:\Users\SampleUserA'
NEW_HOME=r'C:\Users\SampleUserB'


class PortableSettings(unittest.TestCase):
    def source(self):
        return Config(profile='faster-res',model_faster='small',output_mode='both',theme_mode='dark',
            output_dir=OLD+r'\Transcriptions\会議',live_recording_dir=OLD+r'\Recordings',
            gamebar_captures_dir=OLD_HOME+r'\Videos\Captures',hf_token_json='private-token',
            python_overrides={'faster-res':OLD+r'\_internal\environments\faster-res\Scripts\python.exe'},
            capture_preferences={'live':{'folder':OLD+r'\Recordings','mode':'record','post_transcribe':True,
                'device_id':'old-device'},'gamebar':{'destination':OLD+r'\Recordings',
                'captures':OLD_HOME+r'\Videos\Captures','realtime_transcription':True,
                'target_hwnd':999,'target_title':'Old window','mic_device_id':'old-mic'}})

    def test_requested_windows_install_example_rebases_all_paths_and_preserves_choices(self):
        source=self.source()
        data=export_settings(source,app_root=OLD,user_home=OLD_HOME)
        self.assertEqual(data['settings']['output_dir'],{'base':'app','path':'Transcriptions/会議'})
        self.assertNotIn('hf_token_json',data['settings'])
        self.assertNotIn('target_hwnd',data['settings']['capture_preferences']['gamebar'])
        current=Config(hf_token_json='destination-token',capture_setup_complete=False)
        restored,missing=import_settings(data,current,app_root=NEW,user_home=NEW_HOME,path_exists=lambda _:True)
        self.assertEqual(restored.output_dir,NEW+r'\Transcriptions\会議')
        self.assertEqual(restored.live_recording_dir,NEW+r'\Recordings')
        self.assertEqual(restored.gamebar_captures_dir,NEW_HOME+r'\Videos\Captures')
        self.assertEqual(restored.python_overrides['faster-res'],NEW+r'\_internal\environments\faster-res\Scripts\python.exe')
        self.assertEqual((restored.profile,restored.model_faster,restored.output_mode,restored.theme_mode),
                         ('faster-res','small','both','dark'))
        self.assertEqual(restored.hf_token_json,'destination-token');self.assertFalse(missing)
        gamebar=saved_options(restored,'gamebar')
        self.assertTrue(gamebar.realtime_transcription)
        self.assertEqual(str(gamebar.destination),NEW+r'\Recordings')
        self.assertEqual(gamebar.mic_device_id,'');self.assertIsNone(gamebar.target_hwnd)
        self.assertEqual(saved_options(restored,'live').device_id,'')
        self.assertEqual(source.capture_preferences['gamebar']['target_hwnd'],999,'Source config was mutated')

    def test_home_paths_external_drives_and_similar_folder_names_are_not_confused(self):
        source=self.source()
        source.output_dir=OLD_HOME+r'\Documents\Transcripts'
        source.live_recording_dir=r'D:\Archive\Audio'
        source.gamebar_captures_dir=OLD+r'_other\Captures'
        data=export_settings(source,app_root=OLD,user_home=OLD_HOME)
        restored,_=import_settings(data,app_root=NEW,user_home=NEW_HOME,path_exists=lambda _:True)
        self.assertEqual(restored.output_dir,NEW_HOME+r'\Documents\Transcripts')
        self.assertEqual(restored.live_recording_dir,r'D:\Archive\Audio')
        self.assertEqual(restored.gamebar_captures_dir,NEW_HOME+r'\Documents\Unified_Transcriber_Windows_other\Captures')

    def test_missing_python_assignments_do_not_block_new_environment_setup(self):
        data=export_settings(self.source(),app_root=OLD,user_home=OLD_HOME)
        restored,missing=import_settings(data,Config(capture_setup_complete=False,onboarding_seen=False),
            app_root=NEW,user_home=NEW_HOME,path_exists=lambda _:False)
        self.assertEqual(missing,['faster-res']);self.assertEqual(restored.python_overrides,{})
        self.assertFalse(restored.capture_setup_complete);self.assertFalse(restored.onboarding_seen)

    def test_empty_default_and_same_input_are_preserved(self):
        for same in (False,True):
            data=export_settings(Config(output_dir='',output_same_as_input=same),app_root=OLD,user_home=OLD_HOME)
            restored,_=import_settings(data,app_root=NEW,user_home=NEW_HOME)
            self.assertEqual(restored.output_dir,'');self.assertEqual(restored.output_same_as_input,same)

    def test_auth_token_is_exported_only_when_selected(self):
        data=export_settings(self.source(),app_root=OLD,user_home=OLD_HOME,include_token=True)
        self.assertEqual(data['settings']['hf_token_json'],'private-token')
        restored,_=import_settings(data,app_root=NEW,user_home=NEW_HOME)
        self.assertEqual(restored.hf_token_json,'private-token')

    def test_legacy_plain_json_moves_app_and_home_paths_and_discards_window(self):
        legacy={'output_dir':OLD+r'\Transcriptions','live_recording_dir':OLD_HOME+r'\Music\Recorded',
            'capture_preferences':{'gamebar':{'target_hwnd':123,'target_title':'Gone'}}}
        restored,_=import_settings(legacy,app_root=NEW,user_home=NEW_HOME)
        self.assertEqual(restored.output_dir,NEW+r'\Transcriptions')
        self.assertEqual(restored.live_recording_dir,NEW_HOME+r'\Music\Recorded')
        self.assertIsNone(saved_options(restored,'gamebar').target_hwnd)

    def test_invalid_data_and_traversal_are_rejected_before_current_settings_change(self):
        original=self.source();before=deepcopy(original)
        valid=export_settings(original,app_root=OLD,user_home=OLD_HOME)
        cases=[{'format':FORMAT,'version':90,'settings':{}},
            {'profile':123},{'max_speakers':True},{'theme_mode':'unknown'},
            {'capture_preferences':{'gamebar':{'chunk_seconds':1}}},
            {'capture_preferences':{'gamebar':{'auto_transcribe':'false'}}},
            {'capture_preferences':{'live':{'final_pass':'true'}}}]
        for path in ('../elsewhere','/absolute','C:/injected'):
            data=deepcopy(valid);data['settings']['output_dir']={'base':'app','path':path};cases.append(data)
        for data in cases:
            with self.subTest(data=data),self.assertRaises((ValueError,TypeError)):
                import_settings(data,original,app_root=NEW,user_home=NEW_HOME)
        self.assertEqual(original,before)

    def test_atomic_export_json_roundtrip_and_regular_saves_drop_window_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'settings.json';source=Config(capture_preferences={'gamebar':{'target_hwnd':1,'target_title':'old'}})
            data=export_settings(source);write_export(path,data)
            self.assertEqual(json.loads(path.read_text()),data)
            self.assertFalse(list(Path(td).glob('*.tmp')))
            save_config(source,path)
            self.assertNotIn('target_hwnd',json.loads(path.read_text())['capture_preferences']['gamebar'])
            self.assertIsNone(saved_options(load_config(path),'gamebar').target_hwnd)


if __name__=='__main__':unittest.main()
