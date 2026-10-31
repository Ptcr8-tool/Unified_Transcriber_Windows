"""v19.1: embedded detailed logs stay visible alongside recognition preview."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine_progress import EngineUpdate
from unified_gui import human_readable_engine_progress
import test_v25_studio_ui as fixtures


class LogFormatTests(unittest.TestCase):
    def test_structured_asr_is_human_readable(self):
        update=EngineUpdate('音声認識', .375, 'こんにちは SPEAKER_00\n二行目', '15秒まで認識')
        line=human_readable_engine_progress(update)
        self.assertIn('[進捗] 音声認識 38%', line)
        self.assertIn('こんにちは SPEAKER_00 二行目',line)
        self.assertNotIn('@@TRANSCRIBER@@',line)

    def test_uncertain_stage_has_no_invented_percentage(self):
        self.assertEqual(human_readable_engine_progress(EngineUpdate('モデル読み込み')),
                         '[進捗] モデル読み込み')


class LogGUIRegressionTests(fixtures.StudioGUIFixture):
    def test_log_is_visible_and_detached_updates_live(self):
        app=self.app
        app.log('[モデル] CUDAで読み込み完了\n')
        app._set_busy(True)
        app._show_progress(EngineUpdate('音声認識',0.25,'これが発言です','5秒まで認識'))
        self.root.update()
        self.assertGreater(app.logbox.winfo_height(),45,
                           'Detailed log was squashed beneath the recognition preview')
        self.assertGreater(app.recognized_box.winfo_height(),45)
        self.assertIn('CUDAで読み込み完了',app.logbox.get('1.0','end'))
        self.assertIn('これが発言',app.recognized_box.get('1.0','end'))
        app.open_detached_log()
        app.log('[進捗] 話者分離 2/5\n')
        self.assertIn('話者分離 2/5',app.detached_logbox.get('1.0','end'))
        self.assertIn('話者分離 2/5',app.logbox.get('1.0','end'))
        # Closing the detached window does not disable the embedded log.
        win=app.detached_logbox.winfo_toplevel()
        win.destroy()
        app.detached_logbox=None
        app.log('[完了] OK\n')
        self.assertIn('[完了] OK',app.logbox.get('1.0','end'))

if __name__=='__main__':unittest.main()
