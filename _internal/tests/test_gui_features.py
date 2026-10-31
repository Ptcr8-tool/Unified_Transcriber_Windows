"""Regression checks for Windows file drops, output names and UTF-8 diagnostics."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Config, choose_output, output_name, safe_filename_token
from unified_gui import UnifiedApp, parse_drop_paths, utf8_child_env


class OutputNamingTests(unittest.TestCase):
    def setUp(self):
        self.when = datetime(2000, 1, 1, 9, 0, 0)
        self.source = Path('サンプル録音 01.m4a')

    def test_default_preserves_existing_filename(self):
        self.assertEqual(output_name(self.source, Config(), transcribed_at=self.when), 'サンプル録音 01.txt')

    def test_timestamp_only(self):
        self.assertEqual(output_name(self.source, Config(filename_timestamp=True), transcribed_at=self.when),
                         'サンプル録音 01_20000101_090000.txt')

    def test_all_flags_faster_utterance(self):
        cfg = Config(profile='faster-res',filename_timestamp=True,filename_model=True,
                     filename_backend=True,filename_diarization=True)
        self.assertEqual(output_name(self.source,cfg,transcribed_at=self.when),
                         'サンプル録音 01_20000101_090000_large-v3-turbo_faster-whisper_resemblyzer-utterance.txt')

    def test_fixed_diarization_and_model(self):
        cfg=Config(profile='openai-res',diarization_unit='fixed',filename_model=True,filename_diarization=True)
        self.assertEqual(output_name(self.source,cfg,transcribed_at=self.when),
                         'サンプル録音 01_turbo_resemblyzer-fixed.txt')

    def test_qwen_model_id_sanitized(self):
        cfg=Config(profile='qwen-pya',filename_model=True,filename_backend=True,filename_diarization=True)
        result=output_name(self.source,cfg,transcribed_at=self.when)
        self.assertIn('Qwen3-ASR-1.7B-JA',result)
        self.assertIn('pyannote',result)
        self.assertNotIn('neosophie/',result)
        self.assertNotIn('\\',result)

    def test_illegal_windows_characters_cannot_escape_destination(self):
        cfg=Config(profile='faster-res',model_faster='c:/x/y:foo*?<bar>|"baz',filename_model=True)
        dest=choose_output(self.source,cfg,watch_processed=Path('processed'),transcribed_at=self.when)
        self.assertEqual(dest.parent,Path('processed'))
        self.assertFalse(any(c in dest.name for c in '<>:"/\\|?*'))

    def test_explicit_output_folder_and_monitor_processed(self):
        cfg=Config(output_dir='myout', filename_timestamp=True)
        self.assertEqual(choose_output(self.source,cfg,transcribed_at=self.when),
                         Path('myout')/'サンプル録音 01_20000101_090000.txt')
        self.assertEqual(choose_output(self.source,cfg,watch_processed=Path('done'),transcribed_at=self.when),
                         Path('done')/'サンプル録音 01_20000101_090000.txt')

    def test_old_configuration_with_flags_absent_remains_loadable(self):
        from core import load_config
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'settings.json'
            path.write_text('{"profile":"faster-res", "overwrite":false}',encoding='utf-8')
            cfg=load_config(path)
            self.assertFalse(cfg.filename_timestamp)
            self.assertFalse(cfg.filename_model)
            self.assertEqual(output_name(self.source,cfg), 'サンプル録音 01.txt')

    def test_filename_boolean_validation(self):
        with self.assertRaises(ValueError):
            Config(filename_model='True').validate()

    def test_watch_processed_pair_accounts_for_suffix(self):
        cfg=Config(profile='faster-res',filename_timestamp=True,filename_model=True)
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            first_media,first_text=UnifiedApp._unique_processed_pair(folder,self.source,cfg,self.when)
            self.assertEqual(first_media.name,'サンプル録音 01.m4a')
            self.assertIn('large-v3-turbo',first_text.name)
            first_text.write_text('present',encoding='utf-8')
            new_media,new_text=UnifiedApp._unique_processed_pair(folder,self.source,cfg,self.when)
            self.assertNotEqual(new_media,first_media)
            self.assertNotEqual(new_text,first_text)
            self.assertNotEqual(new_media,new_text)


class DropAndEncodingTests(unittest.TestCase):
    def test_tcl_path_list_handles_unicode_spaces_and_multiple(self):
        interp=tk.Tcl()
        data=r'{C:\Users\SampleUser\サンプル録音 01.m4a} {D:\Audio\test 2.mp3}'
        paths=parse_drop_paths(data,interp.splitlist)
        self.assertEqual(len(paths),2)
        self.assertEqual(str(paths[0]),r'C:\Users\SampleUser\サンプル録音 01.m4a')
        self.assertEqual(str(paths[1]),r'D:\Audio\test 2.mp3')


    def test_disabled_dnd_shows_repair_instead_of_restart_loop(self):
        from unittest.mock import MagicMock, patch
        import unified_gui
        app = MagicMock()
        app.dnd_startup_error = None
        with patch.object(unified_gui, 'TkinterDnD', None), patch.object(unified_gui, 'DND_FILES', None):
            UnifiedApp._enable_drag_drop(app, MagicMock())
        label = app.drop_hint.configure.call_args.kwargs['text']
        self.assertIn('修復', label)
        self.assertNotIn('再実行', label)

    def test_enabled_dnd_registers_actual_file_list(self):
        from unittest.mock import MagicMock, patch
        import unified_gui
        app=MagicMock()
        app.dnd_startup_error=None
        treeframe=MagicMock()
        with patch.object(unified_gui, 'TkinterDnD', MagicMock()), \
             patch.object(unified_gui, 'DND_FILES', 'DND_Files'), \
             patch.object(unified_gui, 'sys', SimpleNamespace(platform='win32')), \
             patch('ctypes.windll',SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda:False)),create=True):
            UnifiedApp._enable_drag_drop(app, treeframe)
        treeframe.drop_target_register.assert_called_once_with('DND_Files')
        app.tree.drop_target_register.assert_called_once_with('DND_Files')
        app.drop_hint.drop_target_register.assert_called_once_with('DND_Files')
        self.assertIn('D&D有効',app.drop_hint.configure.call_args.kwargs['text'])
        app.dnd_repair_btn.pack_forget.assert_called_once()

    def test_elevated_windows_dnd_keeps_registered_targets_and_explains_uipi(self):
        import unified_gui
        app=MagicMock();app.dnd_startup_error=None;frame=MagicMock()
        with patch.object(unified_gui,'TkinterDnD',MagicMock()), \
             patch.object(unified_gui,'DND_FILES','DND_Files'), \
             patch.object(unified_gui,'sys',SimpleNamespace(platform='win32')), \
             patch('ctypes.windll',SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda:True)),create=True):
            UnifiedApp._enable_drag_drop(app,frame)
        frame.drop_target_register.assert_called_once_with('DND_Files')
        self.assertIn('通常権限',app.drop_hint.configure.call_args.kwargs['text'])
        app.dnd_repair_btn.pack_forget.assert_called_once()

    def test_tkdnd_root_fails_shows_reason_not_restart(self):
        app=MagicMock()
        app.dnd_startup_error='unable to load tkdnd'
        UnifiedApp._enable_drag_drop(app,MagicMock())
        self.assertIn('初期化失敗',app.drop_hint.configure.call_args.kwargs['text'])

    def test_utf8_env_preserves_system_env(self):
        env=utf8_child_env()
        self.assertEqual(env['PYTHONIOENCODING'],'utf-8')
        self.assertEqual(env['PYTHONUTF8'],'1')
        self.assertEqual(env['PYTHONUNBUFFERED'],'1')
        self.assertEqual(env.get('PATH'),os.environ.get('PATH'))


if __name__=='__main__':unittest.main()
