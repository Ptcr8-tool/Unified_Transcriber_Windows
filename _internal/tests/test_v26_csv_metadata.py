"""Sparse file metadata must shrink exports without losing editing/restore contracts."""
import csv
import io
import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transcript_tools import (CSV_COLUMNS, METADATA_COLUMNS, ROUNDTRIP_COLUMNS,
    _parse_document_txt, convert_transcript, export_transcript_csv, merge_transcript_csv,
    replace_speaker_labels, serialize_csv, speaker_utterances, transcript_rows)


class SparseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def sample(self, name='入力', count=4, model='large-v3-turbo'):
        path = self.root/(name+'.txt')
        path.write_text('# 文字起こし情報\n入力ファイル: '+name+'.wav\n'
            '文字起こし日時: 2000-01-23 09:00:00\nメディア作成日時: 2000-01-22 12:00:00\n'
            'モデル: '+model+'\n音声認識方式: faster-whisper\n実行デバイス: cuda\n'
            '--- 文字起こし本文 ---\n\n'+''.join(
            f'[00:{i//60:02d}:{i%60:02d}] SPEAKER_{i%2:02d}: 発言 {i}、引用は"はい"。\n'
            for i in range(count)), encoding='utf-8')
        return path

    def read(self, path):
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames), list(reader)

    def write(self, path, headers, rows):
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader();writer.writerows(rows)

    def test_metadata_only_on_first_data_row_and_inherited_by_all_speeches(self):
        src = self.sample();dest = self.root/'output.csv'
        export_transcript_csv(src, dest)
        headers, rows = self.read(dest)
        self.assertEqual(tuple(headers), CSV_COLUMNS)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(rows[0][key] for key in METADATA_COLUMNS))
        self.assertTrue(all(not row[key] for row in rows[1:] for key in METADATA_COLUMNS))
        recovered = transcript_rows(dest)
        self.assertEqual(recovered, transcript_rows(src))

    def test_compact_csv_speaker_edit_and_txt_conversion_keep_metadata(self):
        src = self.sample();csv_path = self.root/'a.csv'
        export_transcript_csv(src, csv_path)
        renamed = self.root/'renamed.csv'
        replace_speaker_labels(csv_path, renamed, {'SPEAKER_00':'田中'})
        headers, raw = self.read(renamed)
        self.assertTrue(all(not row[key] for row in raw[1:] for key in METADATA_COLUMNS))
        txt = self.root/'back.txt';convert_transcript(renamed, txt)
        rows = transcript_rows(txt)
        self.assertEqual(rows[2]['speaker'], '田中')
        self.assertEqual(rows[2]['model'], 'large-v3-turbo')
        self.assertEqual(rows[3]['source_file'], '入力.wav')
        self.assertEqual(len(speaker_utterances(renamed)['田中']), 2)

    def test_lossless_v2_restores_layout_after_excel_reordering(self):
        src = self.sample();dest = self.root/'roundtrip.csv'
        convert_transcript(src, dest)
        headers, rows = self.read(dest)
        self.assertTrue(all(not row[key] for row in rows[1:] for key in METADATA_COLUMNS))
        self.assertEqual(sum(bool(row['original_metadata']) for row in rows), 1)
        self.assertEqual(sum(bool(row['roundtrip_version']) for row in rows), 1)
        self.write(dest, headers, list(reversed(rows)))
        self.assertEqual(transcript_rows(dest), transcript_rows(src))
        txt = self.root/'back.txt';convert_transcript(dest, txt)
        self.assertEqual(txt.read_text(encoding='utf-8-sig'), src.read_text(encoding='utf-8-sig'))

    def test_legacy_roundtrip_v1_and_repeated_metadata_still_load(self):
        src = self.sample();dest = self.root/'legacy.csv'
        self.write(dest, ROUNDTRIP_COLUMNS, _parse_document_txt(src))
        txt = self.root/'legacy.txt';convert_transcript(dest, txt)
        self.assertEqual(txt.read_text(encoding='utf-8-sig'), src.read_text(encoding='utf-8-sig'))
        self.write(dest, CSV_COLUMNS, transcript_rows(src))
        self.assertEqual(transcript_rows(dest), transcript_rows(src))

    def test_mixed_source_merge_restarts_metadata_only_at_source_changes(self):
        first = self.sample('前半', model='model-A')
        second = self.sample('後半', model='model-B')
        csv_first = self.root/'first.csv';export_transcript_csv(first, csv_first)
        merged = self.root/'merged.csv';merge_transcript_csv([csv_first, second], merged)
        _, raw = self.read(merged)
        self.assertEqual(len(raw), 8)
        self.assertEqual([i for i,row in enumerate(raw) if row['source_file']], [0, 4])
        rows = transcript_rows(merged)
        self.assertEqual([row['model'] for row in rows], ['model-A']*4+['model-B']*4)
        self.assertEqual([row['source_file'] for row in rows], ['前半.wav']*4+['後半.wav']*4)
        with self.assertRaisesRegex(ValueError, '行ごとに異なる'):
            convert_transcript(merged, self.root/'merged.txt')

    def test_modified_metadata_remains_explicitly_rejected_in_lossless_csv(self):
        src = self.sample();dest = self.root/'data.csv';convert_transcript(src, dest)
        headers, rows = self.read(dest);rows[0]['model'] = 'edited'
        self.write(dest, headers, rows)
        with self.assertRaisesRegex(ValueError, 'model列が変更'):
            convert_transcript(dest, self.root/'back.txt')

    def test_large_file_is_smaller_and_all_rows_survive(self):
        src = self.sample('長い入力ファイル名_'+'収録会議'*12, count=1000)
        rows = transcript_rows(src)
        original = io.StringIO(newline='')
        writer = csv.DictWriter(original, fieldnames=CSV_COLUMNS)
        writer.writeheader();writer.writerows(rows)
        compact = serialize_csv(rows)
        self.assertLess(len(compact.encode('utf-8')), len(original.getvalue().encode('utf-8'))*.5)
        path = self.root/'large.csv';path.write_text(compact, encoding='utf-8-sig')
        self.assertEqual(transcript_rows(path), rows)

    def test_sorted_compact_csv_keeps_file_and_mixed_source_metadata(self):
        first=self.sample('A',model='model-A');second=self.sample('B',model='model-B')
        csv_path=self.root/'single.csv';export_transcript_csv(first,csv_path)
        headers,raw=self.read(csv_path);self.write(csv_path,headers,list(reversed(raw)))
        self.assertEqual(transcript_rows(csv_path),list(reversed(transcript_rows(first))))
        merged=self.root/'merged.csv';merge_transcript_csv([csv_path,second],merged)
        headers,raw=self.read(merged)
        expected=list(reversed(transcript_rows(merged)))
        self.write(merged,headers,list(reversed(raw)))
        self.assertEqual(transcript_rows(merged),expected)
        again=self.root/'again.csv';merge_transcript_csv([merged,first],again)
        self.assertEqual(transcript_rows(again),expected+transcript_rows(first))


if __name__ == '__main__':
    unittest.main()
