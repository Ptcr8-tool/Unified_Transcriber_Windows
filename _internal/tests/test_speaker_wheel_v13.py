"""Regression tests for speaker preview, drop handling and modal wheel routing."""
from __future__ import annotations

import csv
import gc
import sys
import tempfile
import threading
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript_tools import speaker_utterances, replace_speaker_labels
from wheel_support import (wheel_direction, wheel_canvas_for, scroll_canvas_for_event,
                           install_wheel_routing)
from unified_gui import SpeakerEditDialog
import test_v25_studio_ui as fixtures


class SpeakerPreviewDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_txt_preview_only_speaker_lines_and_preserves_original_text(self):
        path = self.dir / '講演.txt'
        source = '# 文字起こし情報\nモデル: SPEAKER_00\n--- 文字起こし本文 ---\n\n' \
            '[00:00:01.234] SPEAKER_00: SPEAKER_01 と発言した\n' \
            '[00:00:03] SPEAKER_01: SPEAKER_00: と書いてある\n' \
            'メモ: SPEAKER_77 これは発話ではない\n' \
            '[00:00:08] SPEAKER_00: もう一回\n'
        path.write_text(source, encoding='utf-8')
        content = speaker_utterances(path)
        self.assertEqual(list(content), ['SPEAKER_00', 'SPEAKER_01'])
        self.assertEqual(content['SPEAKER_00'], [('00:00:01.234', 'SPEAKER_01 と発言した'),
                                                 ('00:00:08', 'もう一回')])
        self.assertEqual(content['SPEAKER_01'][0][1], 'SPEAKER_00: と書いてある')
        self.assertEqual(path.read_text(encoding='utf-8'), source)
        dest = self.dir / '名前.txt'
        replace_speaker_labels(path, dest, {'SPEAKER_00': '山田', 'SPEAKER_01': '田中'})
        self.assertEqual(speaker_utterances(dest)['山田'][0][1], 'SPEAKER_01 と発言した')
        self.assertIn('モデル: SPEAKER_00', dest.read_text(encoding='utf-8-sig'))

    def test_csv_preview_japanese_columns_and_multiline_body(self):
        path = self.dir / '発話.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['時刻', '話者', '本文', 'other'])
            writer.writeheader()
            writer.writerow({'時刻': '00:00:01', '話者': 'SPEAKER_00',
                             '本文': 'SPEAKER_00\n2行目', 'other': 'X'})
        content = speaker_utterances(path)
        self.assertEqual(content['SPEAKER_00'], [('00:00:01', 'SPEAKER_00\n2行目')])
        self.assertEqual(speaker_utterances(path), content)
        dest = self.dir / 'new.csv'
        replace_speaker_labels(path, dest, {'SPEAKER_00': '山田'})
        self.assertEqual(speaker_utterances(dest)['山田'][0][1], 'SPEAKER_00\n2行目')

    def test_csv_with_speaker_but_no_text_column_still_can_be_renamed(self):
        path = self.dir / 'no_text.csv'
        path.write_text('speaker,note\nSPEAKER_00,other\n', encoding='utf-8')
        self.assertEqual(speaker_utterances(path), {'SPEAKER_00': [('', '')]})

    def test_unsupported_type(self):
        path = self.dir / 'x.mp3'; path.touch()
        with self.assertRaises(ValueError):
            speaker_utterances(path)


class WheelRouterTests(unittest.TestCase):
    def test_wheel_normalization(self):
        self.assertEqual(wheel_direction(SimpleNamespace(delta=-120)), 3)
        self.assertEqual(wheel_direction(SimpleNamespace(delta=120)), -3)
        self.assertEqual(wheel_direction(SimpleNamespace(delta=-5)), 3)
        self.assertEqual(wheel_direction(SimpleNamespace(num=4, delta=0)), -3)
        self.assertEqual(wheel_direction(SimpleNamespace(num=5, delta=0)), 3)


class SpeakerGuiTests(fixtures.StudioGUIFixture):
    def setUp(self):
        super().setUp()
        self.dir = self.folder
        self.dialog = SpeakerEditDialog(self.app)

    def tearDown(self):
        dialog = self.__dict__.pop('dialog', None)
        try:
            if dialog is not None and dialog.win.winfo_exists():
                dialog.win.destroy()
        finally:
            # Release the controller before the shared fixture performs its
            # owner-thread GC; it contains variables from the destroyed window.
            del dialog
            super().tearDown()

    def test_speaker_fixture_releases_tk_objects_before_worker_gc(self):
        source = self.dir/'speaker.txt'
        source.write_text('[00:00:00] SPEAKER_00: sample\n', encoding='utf-8')
        self.dialog.input.set(str(source))
        self.dialog._scan()
        self.root.update()
        finalized = []
        owner = threading.get_ident()
        resources = {'root': self.root, 'app': self.app, 'dialog': self.dialog,
                     'input': self.dialog.input,
                     'speaker': self.dialog.rename_vars['SPEAKER_00']}
        references = {name: weakref.ref(value, lambda _ref, name=name:
            finalized.append((name, threading.get_ident()))) for name, value in resources.items()}
        del resources
        self.tearDown()
        self.assertTrue(all(reference() is None for reference in references.values()),
                        [name for name, reference in references.items() if reference() is not None])
        self.assertEqual({thread for _, thread in finalized}, {owner})
        worker = threading.Thread(target=gc.collect, name='speaker-fixture-gc-probe')
        worker.start()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive(), 'Destroyed speaker controls reached worker-thread GC')

    def test_accordion_lazily_displays_timestamp_and_original_utterance(self):
        file = self.dir / '発話ログ.txt'
        file.write_text('[00:00:00] SPEAKER_00: 私はSPEAKER_01です\n'
                        '[00:00:03] SPEAKER_01: そうです\n'
                        '[00:00:05] SPEAKER_00: さらに話す\n', encoding='utf-8')
        self.dialog.input.set(str(file)); self.dialog._scan(); self.root.update()
        self.assertEqual(len(self.dialog.preview_panels), 2)
        self.assertFalse(self.dialog.open_previews)
        self.assertEqual(len(self.dialog.preview_panels['SPEAKER_00'].winfo_children()), 0)
        self.dialog._toggle_speaker('SPEAKER_00'); self.root.update()
        self.assertIn('SPEAKER_00', self.dialog.open_previews)
        text = self.dialog.preview_panels['SPEAKER_00'].winfo_children()[0].winfo_children()[0]
        self.assertIn('[00:00:00] 私はSPEAKER_01です', text.get('1.0', 'end'))
        self.assertEqual(text['state'], 'disabled')
        self.dialog._toggle_speaker('SPEAKER_00')
        self.assertNotIn('SPEAKER_00', self.dialog.open_previews)
        self.assertEqual(len(self.dialog.preview_panels['SPEAKER_00'].winfo_children()), 2)
        self.dialog._expand_all()
        self.assertEqual(len(self.dialog.open_previews), 2)
        self.dialog._collapse_all()
        self.assertEqual(len(self.dialog.open_previews), 0)

    def test_drop_one_file_with_spaces_and_japanese(self):
        file = self.dir / 'テスト 録音.csv'
        file.write_text('speaker,text\nSPEAKER_03,hello\n', encoding='utf-8')
        data = '{' + str(file) + '}'
        ev = SimpleNamespace(data=data, widget=self.dialog.input_entry)
        self.assertEqual(self.dialog._drop_input(ev), 'copy')
        self.assertEqual(Path(self.dialog.input.get()), file)
        self.assertIn('SPEAKER_03', self.dialog.rename_vars)

    def test_drop_multiple_files_rejected_without_changing_source(self):
        original = self.dir / 'in.txt'; original.write_text('[00:00:00] SPEAKER_00: hi', encoding='utf-8')
        self.dialog.input.set(str(original)); self.dialog._scan()
        before = self.dialog.input.get()
        with patch('unified_gui.show_error') as error:
            self.dialog._drop_input(SimpleNamespace(data='{foo.txt} {bar.csv}',
                                                    widget=self.dialog.input_entry))
        self.assertTrue(error.called)
        self.assertEqual(self.dialog.input.get(), before)

    def test_requires_rescan_after_editing_filename(self):
        file = self.dir / 'in.txt'; file.write_text('[00:00:01] SPEAKER_00: hi', encoding='utf-8')
        self.dialog.input.set(str(file)); self.dialog._scan()
        self.dialog.input.set(str(self.dir / 'changed.txt'))
        with patch('unified_gui.show_error') as error:
            self.dialog._save()
        self.assertTrue(error.called)

    def test_wheel_scrolls_canvas_from_descendant_label_and_entry(self):
        install_wheel_routing(self.root)
        file = self.dir / 'in.txt'
        lines = [f'[00:{i//60:02d}:{i%60:02d}] SPEAKER_{i%16:02d}: 話 {i}\n'
                 for i in range(150)]
        file.write_text(''.join(lines), encoding='utf-8')
        self.dialog.input.set(str(file)); self.dialog._scan()
        self.root.update()
        initial = self.dialog.canvas.yview()
        self.assertLess(initial[0], initial[1])
        self.assertLess(initial[1], 0.99)
        label = self.dialog.mapping_frame.winfo_children()[0]
        self.assertIs(wheel_canvas_for(label), self.dialog.canvas)
        label.event_generate('<MouseWheel>', delta=-120)
        self.root.update()
        self.assertGreater(self.dialog.canvas.yview()[0], initial[0])
        # A preview page routes the wheel to the enclosing form.
        self.dialog._toggle_speaker('SPEAKER_00'); self.root.update()
        text = self.dialog.preview_panels['SPEAKER_00'].winfo_children()[0].winfo_children()[0]
        self.assertIs(wheel_canvas_for(text), self.dialog.canvas)


if __name__ == '__main__':
    unittest.main()
