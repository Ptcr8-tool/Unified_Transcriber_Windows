"""v17 regression: file collision decisions, merge names, safe conversion, UI progress."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine_progress import PREFIX, EngineUpdate, parse_engine_line
from transcript_tools import convert_transcript, transcript_rows, serialize_csv
from core import Config
from unified_gui import UnifiedApp


class StageProgressTests(unittest.TestCase):
    def test_real_segment_progress(self):
        update = parse_engine_line('Resemblyzerセグメント処理中: 1/447\n')
        self.assertEqual(update.stage, '話者分離')
        self.assertAlmostEqual(update.fraction, 1/447)
        self.assertIn('1/447', update.detail)

    def test_qwen_before_chunk_not_completed(self):
        update = parse_engine_line('Qwen音声認識・タイムスタンプ付与中: 2/10')
        self.assertAlmostEqual(update.fraction, 0.1)

    def test_live_japanese_text_and_audio_progress(self):
        import json
        line=PREFIX+json.dumps({'stage':'asr', 'completed':25, 'total':100,
                                'text':'日本語の発話 SPEAKER_00 を変更しない', 'detail':'25秒まで認識'},ensure_ascii=False)
        update=parse_engine_line(line)
        self.assertEqual(update.stage,'音声認識')
        self.assertEqual(update.fraction,.25)
        self.assertIn('SPEAKER_00',update.snippet)

    def test_missing_total_is_spinner(self):
        self.assertIsNone(parse_engine_line('Whisperで文字起こし中...').fraction)
        self.assertIsNone(parse_engine_line(PREFIX+'{"stage":"asr","completed":1,"total":0}').fraction)

    def test_invalid_machine_events_ignored(self):
        self.assertIsNone(parse_engine_line(PREFIX+'{bad-json'))
        self.assertIsNone(parse_engine_line(PREFIX+'{"stage":"unknown"}'))


class ConversionV17Tests(unittest.TestCase):
    def test_csv_roundtrip_handles_escaping_and_japanese(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)
            src=directory/'話者.csv'
            rows=[{'timestamp':'0:00:01','speaker':'SPEAKER_00','text':'本文のSPEAKER_00: はそのまま\\改行\n次の行',
                   'source_file':'Original 74.m4a','model':'large-v3-turbo'},
                  {'timestamp':'0:00:04','speaker':'SPEAKER_01','text':'こんにちは\r続き','source_file':'Original 74.m4a','model':'large-v3-turbo'}]
            src.write_text(serialize_csv(rows),encoding='utf-8-sig',newline='')
            txt=directory/'結合.txt'; csv_dst=directory/'復元.csv'
            convert_transcript(src,txt)
            self.assertIn('エスケープ形式: backslash-v1',txt.read_text(encoding='utf-8-sig'))
            convert_transcript(txt,csv_dst)
            recovered=transcript_rows(csv_dst)
            self.assertEqual([x['text'] for x in recovered],[x['text'] for x in rows])
            self.assertEqual([x['speaker'] for x in recovered],[x['speaker'] for x in rows])
            self.assertEqual([x['source_file'] for x in recovered],[x['source_file'] for x in rows])

    def test_txt_to_csv_source_protected_and_collision(self):
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/'src.txt'
            src.write_text('[0:00:01] SPEAKER_00: hello SPEAKER_00\n',encoding='utf-8-sig')
            dst=src.with_suffix('.csv')
            convert_transcript(src,dst)
            self.assertEqual(transcript_rows(dst)[0]['text'],'hello SPEAKER_00')
            with self.assertRaises(FileExistsError):convert_transcript(src,dst)
            self.assertTrue(src.is_file())
            self.assertEqual(transcript_rows(dst)[0]['speaker'],'SPEAKER_00')
            convert_transcript(src,dst,overwrite=True)

    def test_csv_custom_fields_refused(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path=Path(d)/'data.csv'
            csv_path.write_text('timestamp,speaker,text,private_custom\n0:00:01,SPEAKER_00,ok,secret\n',encoding='utf-8-sig')
            dst=csv_path.with_suffix('.txt')
            with self.assertRaisesRegex(ValueError,'追加列'):
                convert_transcript(csv_path,dst)
            self.assertFalse(dst.exists())

    def test_csv_different_metadata_per_row_refused(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path=Path(d)/'data.csv'
            csv_path.write_text('timestamp,speaker,text,model\n0:00:01,SPEAKER_00,ok,small\n0:00:02,SPEAKER_01,yes,turbo\n',encoding='utf-8-sig')
            with self.assertRaisesRegex(ValueError,'model'):
                convert_transcript(csv_path,csv_path.with_suffix('.txt'))


class DecisionAndNamingTests(unittest.TestCase):
    def test_skip_and_cancel_never_launch_engine(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)
            source=directory/'a.wav';source.write_bytes(b'audio')
            destination=directory/'a.txt';destination.write_text('prior',encoding='utf-8')
            app=UnifiedApp.__new__(UnifiedApp)
            app.interactive_collisions=True
            app._request_overwrite=MagicMock(return_value='skip')
            result=app._run_one(source,destination,Config(profile='faster-res'),Path('dummy-python'),'','cpu')
            self.assertEqual(result[0],'スキップ')
            self.assertEqual(destination.read_text(encoding='utf-8'),'prior')
            app._request_overwrite.return_value='cancel'
            result=app._run_one(source,destination,Config(profile='faster-res'),Path('dummy-python'),'','cpu')
            self.assertEqual(result[0],'中止')
            self.assertEqual(app._request_overwrite.call_count,2)

    def test_merge_first_folder_and_filename(self):
        from unified_gui import ConcatDialog
        dialog=ConcatDialog.__new__(ConcatDialog)
        dialog.kind='text'
        dialog.paths=[Path('/tmp/test_abc/一つ目.txt'),Path('/tmp/test_xyz/二つ目.txt'),Path('/tmp/test_xx/三つ目.txt')]
        class Var:
            def __init__(self,s):self.s=s
            def get(self):return self.s
        dialog.destination_mode=Var('first')
        dialog.custom_folder=Var('/tmp/custom')
        self.assertEqual(dialog._folder(),Path('/tmp/test_abc'))
        self.assertEqual(dialog._automatic_name(),'結合3ファイル_一つ目.txt')
        dialog.destination_mode=Var('custom')
        self.assertEqual(dialog._folder(),Path('/tmp/custom'))


if __name__=='__main__':unittest.main()

class PublishTransactionTests(unittest.TestCase):
    def test_two_outputs_restore_both_after_partial_failure(self):
        from output_commit import publish_outputs
        import os
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            txt=base/'result.txt'; csv_file=base/'result.csv'
            txt.write_text('old txt');csv_file.write_text('old csv')
            stage_txt=base/'staged.txt';stage_csv=base/'staged.csv'
            stage_txt.write_text('new txt');stage_csv.write_text('new csv')
            original=os.replace
            raised=[False]
            def simulated(a,b):
                if Path(b)==csv_file and not raised[0]:
                    raised[0]=True
                    raise PermissionError('simulated Windows file lock')
                return original(a,b)
            with patch('output_commit.os.replace',side_effect=simulated):
                with self.assertRaises(PermissionError):
                    publish_outputs({txt:stage_txt,csv_file:stage_csv},overwrite=True)
            self.assertEqual(txt.read_text(),'old txt')
            self.assertEqual(csv_file.read_text(),'old csv')

    def test_existing_prevents_unconfirmed_replacement(self):
        from output_commit import publish_outputs
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            target=base/'result.csv'; target.write_text('old')
            stage=base/'temp.csv'; stage.write_text('new')
            with self.assertRaises(FileExistsError):publish_outputs({target:stage})
            self.assertEqual(target.read_text(),'old')
