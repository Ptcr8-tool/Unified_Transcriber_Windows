"""Regression tests for bundled synthetic transcript formats."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from transcript_tools import convert_transcript, transcript_rows, speakers_in_transcript, replace_speaker_labels, export_transcript_csv, merge_transcript_csv

FIXTURE_ROOT=Path(__file__).resolve().parent/'fixtures'
SAMPLES = ['sample_merged_20000101_090000.live.txt', 'sample_merged_20000101_090000.txt', 'sample_recording_20000101_093000.live.txt', 'sample_meeting_20000101_100000.txt']

class RealDocuments(unittest.TestCase):
    def test_all_sample_formats_lossless_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for filename in SAMPLES:
                with self.subTest(filename=filename):
                    source=FIXTURE_ROOT/filename
                    if not source.exists():self.skipTest('Bundled sample fixture unavailable')
                    csv_path=Path(tmp)/(filename+'.csv')
                    recovered=Path(tmp)/(filename+'.recovered.txt')
                    convert_transcript(source,csv_path)
                    records=transcript_rows(csv_path)
                    self.assertGreater(len(records),0)
                    self.assertTrue(all(r['speaker'] for r in records))
                    with csv_path.open(encoding='utf-8-sig',newline='') as h:
                        flat=list(csv.DictReader(h))
                    self.assertIn('structure',{r['record_type'] for r in flat})
                    convert_transcript(csv_path,recovered)
                    self.assertEqual(source.read_text(encoding='utf-8-sig'),
                                     recovered.read_text(encoding='utf-8-sig'))
    def test_merged_source_sections_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=FIXTURE_ROOT/SAMPLES[0]
            if not source.exists():self.skipTest('Bundled sample fixture unavailable')
            dst=Path(tmp)/'a.csv'
            convert_transcript(source,dst)
            with dst.open(encoding='utf-8-sig',newline='') as h:
                rec=list(csv.DictReader(h))
            section_rows=[r for r in rec if r['record_type']=='section']
            self.assertEqual(len(section_rows),2)
            speech=[r for r in rec if r['record_type']=='utterance']
            self.assertEqual(len({r['source_section'] for r in speech}),2)

    def test_speaker_edit_changes_only_speaker_not_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'in.txt'
            source.write_text('# ライブ文字起こし（暫定）\n\n[00:00:01] SPEAKER_00: SPEAKER_00 が言いました\n',encoding='utf-8')
            csv_path=Path(tmp)/'out.csv';convert_transcript(source,csv_path)
            with csv_path.open(encoding='utf-8-sig',newline='') as h:
                rows=list(csv.DictReader(h));headers=rows[0].keys()
            idx=next(i for i,row in enumerate(rows) if row['record_type']=='utterance')
            rows[idx]['speaker']='田中'
            with csv_path.open('w',encoding='utf-8-sig',newline='') as h:
                w=csv.DictWriter(h,fieldnames=headers);w.writeheader();w.writerows(rows)
            dst=Path(tmp)/'converted.txt';convert_transcript(csv_path,dst)
            self.assertIn('[00:00:01] 田中: SPEAKER_00 が言いました',dst.read_text(encoding='utf-8-sig'))

    def test_metadata_and_structure_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=FIXTURE_ROOT/SAMPLES[1]
            if not source.exists():
                # Keep this contract runnable in clean distributions, which use
                # only synthetic sample transcripts.
                source=Path(tmp)/'portable_metadata.txt'
                source.write_text('# 文字起こし情報\n結合元ファイル数: 2\n'
                    '入力ファイル: sample.wav\n--- 文字起こし本文 ---\n\n'
                    '[00:00:01] SPEAKER_00: 最初の発言です。\n'
                    '[00:00:04] SPEAKER_01: 次の発言です。\n', encoding='utf-8')
            csv_path=Path(tmp)/'a.csv';convert_transcript(source,csv_path)
            out=Path(tmp)/'a.txt';convert_transcript(csv_path,out)
            text=out.read_text(encoding='utf-8-sig')
            self.assertIn('結合元ファイル数: 2',text)
            self.assertIn('--- 文字起こし本文 ---',text)
            self.assertIn('入力ファイル:',text)
            # Compact export is still only 9 columns and can see all speeches.
            compact=Path(tmp)/'compact.csv';export_transcript_csv(source,compact)
            self.assertEqual(len(transcript_rows(compact)),len(transcript_rows(csv_path)))

    def test_legacy_csv_to_txt_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'a.csv';src.write_text('timestamp,speaker,text\n0:00:01,A,ok\n',encoding='utf-8')
            dest=Path(tmp)/'b.txt';convert_transcript(src,dest)
            self.assertEqual(transcript_rows(dest)[0]['text'],'ok')

    def test_unrecognized_custom_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'in.txt';source.write_text('[00:00:01] A: x\n')
            csv_path=Path(tmp)/'out.csv';convert_transcript(source,csv_path)
            with csv_path.open(encoding='utf-8-sig',newline='') as h:
                rows=list(csv.DictReader(h));headers=list(rows[0])
            with csv_path.open('w',encoding='utf-8-sig',newline='') as h:
                w=csv.DictWriter(h,fieldnames=headers+['custom']);w.writeheader()
                for r in rows:w.writerow({**r,'custom':'not_empty'})
            with self.assertRaisesRegex(ValueError,'独自CSV列'):
                convert_transcript(csv_path,Path(tmp)/'back.txt')

if __name__=='__main__':unittest.main()

class SpecialCases(unittest.TestCase):
    def test_backslash_format_edit_can_contain_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            src=base/'a.csv'; src.write_text('timestamp,speaker,text\n0:00:01,A,hello\n')
            txt=base/'a.txt';convert_transcript(src,txt)
            converted=base/'editable.csv';convert_transcript(txt,converted)
            with converted.open(encoding='utf-8-sig',newline='') as f:
                rows=list(csv.DictReader(f));keys=list(rows[0])
            spoken=[r for r in rows if r['record_type']=='utterance'][0]
            spoken['text']='改行\nSPEAKER_00 と \\ 文字'
            with converted.open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)
            back=base/'back.txt';convert_transcript(converted,back)
            self.assertIn(r'改行\nSPEAKER_00 と \\ 文字',back.read_text(encoding='utf-8-sig'))
            self.assertEqual(transcript_rows(back)[0]['text'],spoken['text'])

    def test_legacy_header_bom_speaker_name_only(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);src=base/'a.txt'
            src.write_text('# 文字起こし情報\n--- 文字起こし本文 ---\n\n\ufeff[0:00:05] SPEAKER_00: 本文中の SPEAKER_00\n',encoding='utf-8')
            back=base/'renamed.txt'
            _,count=replace_speaker_labels(src,back,{'SPEAKER_00':'田中'})
            self.assertEqual(count,1)
            self.assertIn('田中: 本文中の SPEAKER_00',back.read_text(encoding='utf-8-sig'))

    def test_flat_merge_refuses_freeform_but_roundtrip_keeps_it(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);a=base/'a.txt';b=base/'b.txt'
            a.write_text('[00:00:01] A: speech\n自由文は保持される\n',encoding='utf-8')
            b.write_text('[00:00:02] B: speech\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'発話以外の本文'):
                merge_transcript_csv([a,b],base/'flat.csv')
            csv_path=base/'roundtrip.csv';convert_transcript(a,csv_path)
            recovered=base/'recovered.txt';convert_transcript(csv_path,recovered)
            self.assertEqual(recovered.read_text(encoding='utf-8-sig'),a.read_text(encoding='utf-8-sig'))

class ReorderedCsv(unittest.TestCase):
    def test_excel_sorted_csv_restores_original_order(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);src=base/'src.txt'
            src.write_text('# 文字起こし情報\nモデル: turbo\n--- 文字起こし本文 ---\n\n'
                           '[00:00:01] A: こんにちは\n[00:00:03] B: はい\n',encoding='utf-8')
            dst=base/'out.csv';convert_transcript(src,dst)
            with dst.open(encoding='utf-8-sig',newline='') as f:
                raw=list(csv.DictReader(f));cols=list(raw[0])
            with dst.open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(raw[::-1])
            recover=base/'recovered.txt';convert_transcript(dst,recover)
            self.assertEqual(recover.read_text(encoding='utf-8-sig'),src.read_text(encoding='utf-8-sig'))
