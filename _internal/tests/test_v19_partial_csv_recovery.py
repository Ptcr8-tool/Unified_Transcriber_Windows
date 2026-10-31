"""Regression: a failed CSV export cannot eat a fully recognized TXT."""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from output_commit import publish_transcription
from transcript_tools import export_transcript_csv


class PartialCsvTests(unittest.TestCase):
    @staticmethod
    def recognized(path: Path):
        path.write_text('# 文字起こし情報\n入力ファイル: 録音.mp4\nモデル: large-v3-turbo\n\n'
                        '--- 文字起こし本文 ---\n\n\ufeff[0:00:01] SPEAKER_00: Hello\n'
                        '[0:00:04] SPEAKER_01: SPEAKER_00 は文字列です\n', encoding='utf-8-sig')
        return path

    def test_both_txt_committed_before_csv_and_csv_parse_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            tmp=self.recognized(root/'pending.txt')
            txt=root/'out.txt'; csv_out=root/'out.csv'
            status,saved,error=publish_transcription(tmp,txt=txt,csv_path=csv_out,
                exporter=export_transcript_csv,source_name='録音.mp4')
            self.assertEqual((status,error),('完了',None))
            self.assertEqual(saved,[txt,csv_out])
            self.assertTrue(txt.exists());self.assertTrue(csv_out.exists())
            with csv_out.open(encoding='utf-8-sig',newline='') as h:
                data=list(csv.DictReader(h))
            self.assertEqual(data[1]['text'],'SPEAKER_00 は文字列です')

    def test_both_failed_csv_keeps_new_txt(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); stage=self.recognized(root/'pending.txt')
            txt=root/'out.txt'; csv_out=root/'out.csv'
            def broken_export(source,target,**kw):
                self.assertEqual(source,txt)
                self.assertTrue(source.exists())
                raise ValueError('カスタムCSVエラー')
            status,saved,problem=publish_transcription(stage,txt=txt,csv_path=csv_out,
                exporter=broken_export,source_name='録音.mp4')
            self.assertEqual(status,'一部完了');self.assertEqual(saved,[txt])
            self.assertIn('カスタムCSVエラー',problem)
            self.assertIn('SPEAKER_01',txt.read_text(encoding='utf-8-sig'))
            self.assertFalse(csv_out.exists())

    def test_csv_only_failed_csv_saves_recovery_txt_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);stage=self.recognized(root/'pending.txt')
            out=root/'out.csv';existing=root/'out.txt';existing.write_text('古い文章',encoding='utf-8')
            def broken(*a,**kw):raise ValueError('CSV構文変換失敗')
            status,saved,problem=publish_transcription(stage,txt=None,csv_path=out,
                exporter=broken,source_name='録音.mp4')
            self.assertEqual(status,'一部完了')
            self.assertEqual(existing.read_text(encoding='utf-8'),'古い文章')
            self.assertIn('_CSV変換失敗_復旧',saved[0].stem)
            self.assertIn('Hello',saved[0].read_text(encoding='utf-8-sig'))

    def test_both_overwrite_existing_txt_and_failing_csv_keeps_old_csv(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);stage=self.recognized(root/'pending.txt')
            txt=root/'out.txt';txt.write_text('OLD TXT',encoding='utf-8')
            csv_out=root/'out.csv';csv_out.write_text('OLD CSV',encoding='utf-8')
            def broken(*a,**kw):raise ValueError('convert failed')
            status,saved,error=publish_transcription(stage,txt=txt,csv_path=csv_out,
                exporter=broken,source_name='録音.mp4',overwrite=True)
            self.assertEqual(status,'一部完了')
            self.assertIn('Hello',txt.read_text(encoding='utf-8-sig'))
            self.assertEqual(csv_out.read_text(encoding='utf-8'),'OLD CSV')

    def test_txt_only_works_without_conversion(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);stage=self.recognized(root/'pending.txt')
            txt=root/'out.txt'
            status,saved,err=publish_transcription(stage,txt=txt,csv_path=None,
                exporter=lambda *a,**kw:self.fail('should not be called'),source_name='a')
            self.assertEqual(status,'完了');self.assertEqual(saved,[txt])


if __name__=='__main__':unittest.main()
