import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Config, build_command, env_python, PROFILES
from engines.caption_segments import split_timed_items
from engines.segment_resemblyzer import TranscriptItem, merge_items
from merge_metadata import inspect_merge_file, oldest_first
from media_tools import MediaMetadata

class MergeMetadataTests(unittest.TestCase):
    def test_txt_metadata_precedes_modified_time(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'newer.txt';b=Path(d)/'older.txt'
            a.write_text('# 文字起こし情報\nメディア作成日時: 2000-01-20 12:00:00\n--- 文字起こし本文 ---\n[0:00:01] SPEAKER_00: 新\n',encoding='utf-8')
            b.write_text('# 文字起こし情報\nメディア作成日時: 2000-01-18 12:00:00\n--- 文字起こし本文 ---\n[0:00:01] SPEAKER_00: 旧\n',encoding='utf-8')
            os.utime(a,(1,1));os.utime(b,(2,2))
            infos={p:inspect_merge_file(p) for p in (a,b)}
            self.assertIn('TXT/CSV',infos[a].sort_source)
            self.assertEqual(oldest_first([a,b],infos),[b,a])

    def test_csv_metadata_is_used_for_order(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'音声.csv'
            with file.open('w',encoding='utf-8-sig',newline='') as out:
                writer=csv.DictWriter(out,fieldnames=['timestamp','speaker','text','media_created_at'])
                writer.writeheader(); writer.writerow({'timestamp':'0:00:01','speaker':'SPEAKER_00','text':'あ','media_created_at':'2000-09-01T12:04:00Z'})
            self.assertEqual(inspect_merge_file(file).recorded.year,2000)

    def test_audio_tags_are_source_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'recording.m4a';file.write_bytes(b'not-media')
            metadata=MediaMetadata(file,datetime(2000,1,1),'メディアタグ: creation_time')
            with patch('merge_metadata.probe_metadata',return_value=metadata):
                info=inspect_merge_file(file)
            self.assertEqual(info.recorded.year,2000)
            self.assertTrue(info.sort_source.startswith('メディアタグ'))

    def test_missing_metadata_explicit_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'non_header.txt';file.write_text('hello',encoding='utf-8')
            info=inspect_merge_file(file)
            self.assertIsNone(info.recorded)
            self.assertTrue(info.sort_source.startswith('最終更新日時') or
                            info.sort_source.startswith('ファイル作成日時'))

    def test_unrepresentable_filesystem_date_does_not_hide_recorded_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'recorded.txt'
            file.write_text('# 文字起こし情報\nメディア作成日時: 2000-01-20 12:00:00\n'
                            '--- 文字起こし本文 ---\n[0:00:01] SPEAKER_00: sample\n',encoding='utf-8')
            invalid=SimpleNamespace(st_mtime=float('inf'),st_ctime=float('inf'),st_birthtime=float('inf'))
            with patch('merge_metadata.Path.stat',return_value=invalid):
                info=inspect_merge_file(file)
            self.assertEqual(info.recorded.year,2000)
            self.assertIsNone(info.modified)
            self.assertIsNone(info.file_created)
            self.assertEqual(info.sort_time,info.recorded)

class CaptionTests(unittest.TestCase):
    def test_split_keeps_exact_utterance_and_real_timing(self):
        words=[TranscriptItem(0.,1.,'UNKNOWN','これは'),TranscriptItem(1.,2.,'UNKNOWN','長い'),
               TranscriptItem(2.,3.,'UNKNOWN','ニュース'),TranscriptItem(3.,4.,'UNKNOWN','です。')]
        out=split_timed_items(words,max_chars=8,max_seconds=2)
        self.assertGreater(len(out),1)
        self.assertEqual(''.join(x.text for x in out),''.join(x.text for x in words))
        self.assertTrue(all(len(x.text)<=8 and x.end-x.start<=2.01 for x in out))
        self.assertEqual(out[0].start,0.)

    def test_diarized_engine_caption_mode_works(self):
        words=[TranscriptItem(i*.5,(i+1)*.5,'UNKNOWN',letter) for i,letter in enumerate('あいうえおかきくけこ')]
        with patch('engines.segment_resemblyzer.identify_speaker',return_value='CLUSTER_00'):
            from engines.segment_resemblyzer import SpeakerTurn
            result=merge_items(words,[SpeakerTurn(0,5,'CLUSTER_00')],1.2,
                               caption_max_chars=8,caption_max_seconds=2)
        self.assertGreater(len(result),1)
        self.assertEqual(''.join(x.text for x in result),'あいうえおかきくけこ')
        self.assertEqual({x.speaker for x in result},{'SPEAKER_00'})

    def test_cli_commands_all_5_engines_receive_caption_args(self):
        for profile in PROFILES:
            with self.subTest(profile=profile.id):
                cfg=Config(profile=profile.id,caption_split=True,caption_max_chars=26,caption_max_seconds=4.)
                cmd=build_command(Path('input.wav'),Path('out.txt'),cfg,Path('python'), 'cpu')
                self.assertEqual(cmd[cmd.index('--caption-max-chars')+1],'26')
                self.assertEqual(cmd[cmd.index('--caption-max-seconds')+1],'4.0')

    def test_setting_validation(self):
        with self.assertRaises(ValueError):Config(caption_max_chars=1).validate()
        with self.assertRaises(ValueError):Config(caption_max_seconds=0).validate()

if __name__=='__main__':unittest.main()
