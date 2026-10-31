import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript_tools import (transcript_rows, export_transcript_csv,
    merge_transcript_csv, speakers_in_transcript, replace_speaker_labels)
from core import Config, save_config, load_config

class SpeakerFieldSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_txt_replaces_only_timestamp_label_not_body_or_metadata(self):
        source=self.root/'話者 収録.txt'
        source.write_text('# 文字起こし情報\nモデル: SPEAKER_00\n'
            '話者分離備考: SPEAKER_00: あくまで文字列\n\n--- 文字起こし本文 ---\n\n'
            '[00:00:01] SPEAKER_00: SPEAKER_00: と文字列が発言内にある\n\n'
            '[00:01:09] SPEAKER_01: SPEAKER_00が登場\n'
            '[00:01:12] SPEAKER_00: 自分はSPEAKER_01ではない\n',encoding='utf-8')
        dest=self.root/'名前付き.txt'
        output,count=replace_speaker_labels(source,dest,{'SPEAKER_00':'田中', 'SPEAKER_01':'佐藤'})
        self.assertEqual(output,dest)
        self.assertEqual(count,3)
        result=dest.read_text(encoding='utf-8-sig')
        self.assertIn('モデル: SPEAKER_00',result)
        self.assertIn('話者分離備考: SPEAKER_00',result)
        self.assertIn('[00:00:01] 田中: SPEAKER_00: と文字列が発言内にある',result)
        self.assertIn('[00:01:09] 佐藤: SPEAKER_00が登場',result)
        self.assertIn('[00:01:12] 田中: 自分はSPEAKER_01ではない',result)
        self.assertEqual(source.read_text(encoding='utf-8').count('SPEAKER_00:'),4)
        with self.assertRaises(FileExistsError):replace_speaker_labels(source,dest,{'SPEAKER_00':'誰か'})

    def test_txt_line_without_label_remains_untouched(self):
        source=self.root/'old.txt'
        source.write_text('SPEAKER_00: 行頭ですが本文です\n'
                          '[00:00:05.456] SPEAKER_00: 文字SPEAKER_00です\n'
                          '途中 SPEAKER_00: は発言です\n',encoding='utf-8')
        out,_=replace_speaker_labels(source,self.root/'new.txt',{'SPEAKER_00':'木村'})
        content=out.read_text(encoding='utf-8-sig')
        self.assertIn('SPEAKER_00: 行頭ですが本文です',content)
        self.assertIn('途中 SPEAKER_00: は発言です',content)
        self.assertIn('[00:00:05.456] 木村: 文字SPEAKER_00です',content)

    def test_csv_speaker_column_only_multiline_text_preserved(self):
        source=self.root/'start.csv'
        with source.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['timestamp','speaker','text','unknown_column'])
            writer.writeheader();writer.writerow({'timestamp':'00:00:01','speaker':'SPEAKER_00',
                'text':'SPEAKER_00 と話しました\nSPEAKER_00: 本文','unknown_column':'SPEAKER_00'})
        dest=self.root/'end.csv'
        replace_speaker_labels(source,dest,{'SPEAKER_00':'斎藤'})
        with dest.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual(rows[0]['speaker'],'斎藤')
        self.assertEqual(rows[0]['text'],'SPEAKER_00 と話しました\nSPEAKER_00: 本文')
        self.assertEqual(rows[0]['unknown_column'],'SPEAKER_00')
        self.assertEqual(speakers_in_transcript(dest),['斎藤'])

    def test_bad_name_rejected_and_existing_original_safe(self):
        source=self.root/'raw.txt'
        source.write_text('[00:00:01] SPEAKER_00: ああ',encoding='utf-8')
        for name in ['a:b','aaa\nbbb']:
            with self.assertRaises(ValueError):
                replace_speaker_labels(source,self.root/'out.txt',{'SPEAKER_00':name})
        self.assertFalse((self.root/'out.txt').exists())
        with self.assertRaises(ValueError):
            replace_speaker_labels(source,source,{'SPEAKER_00':'Saito'})

class CSVOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_csv_headers_and_multiline_preserved(self):
        src=self.root/'a.txt'
        src.write_text('# 文字起こし情報\n文字起こし日時: 2000-01-21 10:00:00\n'
             'メディア作成日時: 2000-01-20 12:00:00\nモデル: large-v3-turbo\n'
             '音声認識方式: faster-whisper\n実行デバイス: cuda\n\n--- 文字起こし本文 ---\n'
             '\n[00:00:01] SPEAKER_00: 話者=SPEAKER_01, 引用は"こんにちは"\n'
             '\n[00:00:03] SPEAKER_01: はい\n',encoding='utf-8-sig')
        out=self.root/'a.csv'
        export_transcript_csv(src,out)
        with out.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['text'],'話者=SPEAKER_01, 引用は"こんにちは"')
        self.assertEqual(rows[0]['model'],'large-v3-turbo')
        self.assertEqual(rows[1]['speaker'],'SPEAKER_01')
        self.assertEqual(rows[1]['media_created_at'],'')
        self.assertEqual(transcript_rows(out)[1]['media_created_at'],'2000-01-20 12:00:00')
        self.assertEqual(rows[0]['source_file'],'a.txt')

    def test_mixed_merge_preserves_source_and_order(self):
        a=self.root/'a.txt'; a.write_text('[00:00:00] SPEAKER_00: A',encoding='utf-8')
        b=self.root/'b.csv'
        with b.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(['timestamp','speaker','text','quality']);w.writerow(['00:00:02','田中','B','high'])
        dest=self.root/'merged.csv'
        merge_transcript_csv([b,a],dest)
        with dest.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual([r['text'] for r in rows],['B','A'])
        self.assertEqual([r['source_file'] for r in rows],['b.csv','a.txt'])
        self.assertEqual(rows[0]['quality'],'high')
        self.assertEqual(rows[1]['quality'],'')
        with self.assertRaises(FileExistsError):merge_transcript_csv([a,b],dest)

    def test_strict_error_without_silent_data_loss(self):
        a=self.root/'a.txt';a.write_text('[00:00:01] SPEAKER_00: OK\n失われてはいけない文章\n',encoding='utf-8')
        b=self.root/'b.txt';b.write_text('[00:00:01] SPEAKER_01: OK\n',encoding='utf-8')
        dest=self.root/'dest.csv'
        with self.assertRaises(ValueError):merge_transcript_csv([a,b],dest)
        self.assertFalse(dest.exists())

    def test_config_mode_roundtrip_validation(self):
        path=self.root/'settings.json'
        for mode in ('txt','csv','both'):
            cfg=Config(output_mode=mode);save_config(cfg,path)
            self.assertEqual(load_config(path).output_mode,mode)
        with self.assertRaises(ValueError):Config(output_mode='xls').validate()

if __name__=='__main__':unittest.main()

class PipelineCSVTests(unittest.TestCase):
    def test_output_modes_and_existing_file_safety(self):
        import queue
        import subprocess
        from unittest.mock import patch
        from unified_gui import UnifiedApp
        from media_tools import MediaMetadata
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            source=d/'録音.m4a';source.write_bytes(b'audio sample')
            dest=d/'文字起こし.txt'
            # The fake engine writes into the exact stage file specified by --output.
            script=("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("
                    "'[00:00:03] SPEAKER_00: 本文にSPEAKER_00が登場\\n', encoding='utf-8')")
            fake_app=MagicMock()
            fake_app.process_lock=MagicMock()
            fake_app.events=queue.Queue()
            fake_app.stop_event.is_set.return_value=False
            for mode in ('txt','csv','both'):
                dest.unlink(missing_ok=True)
                dest.with_suffix('.csv').unlink(missing_ok=True)
                cfg=Config(output_mode=mode,txt_metadata_header=True)
                def make_command(_source, stage, _cfg, _python, _device):
                    return [sys.executable,'-c',script,str(stage)]
                with patch('unified_gui.build_command',side_effect=make_command),\
                     patch('unified_gui.probe_metadata',return_value=MediaMetadata(source)):
                    result,info=UnifiedApp._run_one(fake_app,source,dest,cfg,Path(sys.executable),'','cpu')
                self.assertEqual(result,'完了',info)
                self.assertEqual(dest.exists(),mode in ('txt','both'))
                self.assertEqual(dest.with_suffix('.csv').exists(),mode in ('csv','both'))
                if mode!='txt':
                    with dest.with_suffix('.csv').open(encoding='utf-8-sig',newline='') as f:
                        rows=list(csv.DictReader(f))
                    self.assertEqual(rows[0]['text'],'本文にSPEAKER_00が登場')
                    self.assertEqual(rows[0]['source_file'],source.name)
                    # existing CSV should be protected when not overwrite
                    with patch('unified_gui.build_command',side_effect=make_command):
                        state,msg=UnifiedApp._run_one(fake_app,source,dest,cfg,
                                                      Path(sys.executable),'','cpu')
                    self.assertEqual(state,'スキップ')

    def test_failed_conversion_keeps_original_files(self):
        import queue
        from unittest.mock import patch
        from unified_gui import UnifiedApp
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'audio.m4a';source.write_bytes(b'fake')
            dest=root/'result.txt'
            script=("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("
                    "'invalid transcript with no labels',encoding='utf-8')")
            fake_app=MagicMock()
            fake_app.process_lock=MagicMock()
            fake_app.events=queue.Queue()
            fake_app.stop_event.is_set.return_value=False
            with patch('unified_gui.build_command',
                       side_effect=lambda x,stage,y,z,q:[sys.executable,'-c',script,str(stage)]):
                state,msg=UnifiedApp._run_one(fake_app,source,dest,
                    Config(output_mode='both'),Path(sys.executable),'','cpu')
            # v19: ASR text must survive CSV conversion failure.
            self.assertEqual(state,'一部完了')
            self.assertTrue(dest.exists())
            self.assertIn('invalid transcript', dest.read_text(encoding='utf-8-sig'))
            self.assertFalse(dest.with_suffix('.csv').exists())
            self.assertTrue(source.exists())
