"""Output spacing, backward-compatible settings, and adjacent speaker edits."""
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from engines import openai_resemblyzer, pyannote_backend, segment_resemblyzer
from engines.worker_io import transcript_separator
from live_transcript import append_provisional
from settings_transfer import export_settings, import_settings
from transcript_tools import replace_speaker_labels, export_transcript_csv, transcript_rows


class TranscriptSpacing(unittest.TestCase):
    def test_old_settings_default_to_compact_and_new_setting_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'profile': 'faster-res', 'language': 'en'}))
            cfg = core.load_config(path)
            self.assertEqual(cfg.txt_blank_lines, 0)
            self.assertEqual(cfg.language, 'en')
            cfg.txt_blank_lines = 2
            core.save_config(cfg, path)
            self.assertEqual(core.load_config(path).txt_blank_lines, 2)
            imported, _missing = import_settings(export_settings(cfg), core.Config())
            self.assertEqual(imported.txt_blank_lines, 2)

    def test_invalid_blank_line_settings_are_rejected(self):
        for value in (-1, 11, 1.5, True, '1', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.Config(txt_blank_lines=value).validate()
            with self.subTest(separator=value), self.assertRaises(ValueError):
                transcript_separator(value)
        for value in (0, 1, 2, 10):
            core.Config(txt_blank_lines=value).validate()
            self.assertEqual(transcript_separator(value), '\n' * (value + 1))

    def test_every_profile_receives_selected_spacing(self):
        for profile in core.PROFILES:
            for value in (0, 1, 3):
                with self.subTest(profile=profile.id, value=value):
                    cfg = core.Config(profile=profile.id, txt_blank_lines=value)
                    args = core.build_command(Path('sample.wav'), Path('sample.txt'), cfg, Path(sys.executable), 'cpu')
                    self.assertEqual(args[args.index('--blank-lines') + 1], str(value))
                    if profile.asr == 'openai':
                        parsed = openai_resemblyzer.build_parser().parse_args(args[3:])
                    else:
                        engine = pyannote_backend if profile.diarizer == 'pyannote' else segment_resemblyzer
                        with patch.object(sys, 'argv', args[2:]):
                            parsed = engine.parse_args()
                    self.assertEqual(parsed.blank_lines, value)

    def test_renderers_default_to_single_newline_and_allow_spacing(self):
        for engine in (segment_resemblyzer, pyannote_backend):
            items = [engine.TranscriptItem(0, 1, 'SPEAKER_00', '最初の発話'),
                     engine.TranscriptItem(2, 3, 'SPEAKER_01', '次の発話')]
            compact = engine.render_transcript(items)
            self.assertNotIn('\n\n', compact)
            first, second = compact.split('\n')
            for value in (0, 1, 3, 10):
                self.assertEqual(engine.render_transcript(items, value), first + '\n' * (value + 1) + second)
            self.assertEqual(engine.render_transcript([], 1), '')
            self.assertEqual(engine.render_transcript(items[:1], 2), first)

    def test_openai_transcribe_spacing_and_existing_file_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / '音声.wav'
            source.write_bytes(b'RIFF')
            lines = ['[00:00:00] SPEAKER_00: 最初', '[00:00:02] SPEAKER_01: 次']
            for value in (0, 1, 2):
                dest = Path(folder) / f'result-{value}.txt'
                with patch.object(openai_resemblyzer, 'create_transcript', return_value=lines):
                    openai_resemblyzer.transcribe(source, dest, 'turbo', 'ja', 6, .25, None, blank_lines=value)
                self.assertEqual(dest.read_text(encoding='utf-8-sig'), ('\n' * (value + 1)).join(lines) + '\n')
                with self.assertRaises(FileExistsError):
                    openai_resemblyzer.transcribe(source, dest, 'turbo', 'ja', 6, .25, None, blank_lines=value)

    def test_live_chunks_and_empty_chunks_have_only_selected_spacing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            chunk = root / 'chunk.txt'
            empty = root / 'empty.txt'
            empty.write_text('', encoding='utf-8')
            chunk.write_text('[00:00:01] SPEAKER_00: 一つ目\n\n[00:00:02] SPEAKER_01: 二つ目\n', encoding='utf-8')
            for value in (0, 1, 2):
                dest = root / f'live-{value}.txt'
                append_provisional(empty, dest, offset=0, index=0, first=True, blank_lines=value)
                append_provisional(chunk, dest, offset=0, index=1, blank_lines=value)
                before = dest.read_bytes()
                append_provisional(empty, dest, offset=5, index=2, blank_lines=value)
                self.assertEqual(dest.read_bytes(), before)
                append_provisional(chunk, dest, offset=10, index=3, blank_lines=value)
                body = dest.read_text(encoding='utf-8-sig').split('\n\n', 1)[1]
                expected = ['[00:00:01] SPEAKER_00: 一つ目', '[00:00:02] SPEAKER_01: 二つ目',
                            '[00:00:11] SPEAKER_00: 一つ目', '[00:00:12] SPEAKER_01: 二つ目']
                self.assertEqual(body, ('\n' * (value + 1)).join(expected) + '\n')

    def test_csv_rows_and_speaker_edits_preserve_text_and_spacing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for value in (0, 1, 2):
                source = root / f'source-{value}.txt'
                original = ('\n' * (value + 1)).join(('[00:00:01] SPEAKER_00: 原文', '[00:00:02] SPEAKER_01: 次')) + '\n'
                source.write_text(original, encoding='utf-8-sig')
                edited = source.with_stem(source.stem + '_話者名設定')
                replace_speaker_labels(source, edited, {'SPEAKER_00': '話者A'})
                self.assertEqual(edited.read_text(encoding='utf-8-sig'), original.replace('SPEAKER_00:', '話者A:'))
                self.assertEqual(source.read_text(encoding='utf-8-sig'), original)
                csv_path = source.with_suffix('.csv')
                export_transcript_csv(source, csv_path)
                self.assertEqual(len(transcript_rows(csv_path)), 2)
                with self.assertRaises((FileExistsError, ValueError)):
                    replace_speaker_labels(source, edited, {'SPEAKER_00': '別名'})

    def test_startup_no_longer_creates_speaker_edits_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(core, 'output_root', return_value=root):
                core.ensure_output_directories()
            self.assertNotIn('speaker_edits', core.OUTPUT_FOLDERS)
            self.assertFalse((root / 'SpeakerEdits').exists())
            self.assertTrue((root / 'Transcriptions').is_dir())


# Real Tk widgets: avoid relying only on source-string or mocked-control tests.
import unified_gui as gui
from test_v25_studio_ui import StudioGUIFixture


class OutputPreferencesGUI(StudioGUIFixture):
    def test_spacing_control_saves_and_reopens(self):
        dialog = gui.SettingsDialog(self.app)
        self.root.update()
        self.assertEqual(dialog.txt_blank_lines.get(), '0')
        dialog.txt_blank_lines.set('2')
        with patch.object(gui, 'show_error') as error:
            dialog._save()
        error.assert_not_called()
        self.assertEqual(self.app.config.txt_blank_lines, 2)
        reopened = gui.SettingsDialog(self.app)
        self.root.update()
        self.assertEqual(reopened.txt_blank_lines.get(), '2')
        reopened.win.destroy()

    def test_invalid_spacing_does_not_save(self):
        for value in ('-1', '11', 'abc'):
            dialog = gui.SettingsDialog(self.app)
            self.root.update()
            dialog.txt_blank_lines.set(value)
            with patch.object(gui, 'show_error') as error:
                dialog._save()
            error.assert_called_once()
            self.assertEqual(self.app.config.txt_blank_lines, 0)
            dialog.win.destroy()

    def test_speaker_edit_defaults_and_browse_follow_each_input(self):
        root = self.folder / '元のフォルダー'
        root.mkdir()
        txt = root / '会議.txt'
        txt.write_text('[00:00:01] SPEAKER_00: 発言\n', encoding='utf-8')
        csv_path = root / '表.csv'
        export_transcript_csv(txt, csv_path)
        for source in (txt, csv_path):
            original = source.read_bytes()
            dialog = self.app.open_speaker_editor(source)
            self.root.update()
            expected = source.with_name(source.stem + '_話者名設定' + source.suffix)
            self.assertEqual(Path(dialog.output.get()), expected)
            with patch.object(gui.filedialog, 'asksaveasfilename', return_value='') as browse:
                dialog._output_browse()
            self.assertEqual(Path(browse.call_args.kwargs['initialdir']), source.parent)
            dialog.output.set(str(self.folder / ('custom' + source.suffix)))
            dialog._default_location()
            self.assertEqual(Path(dialog.output.get()), source.parent / ('custom' + source.suffix))
            dialog.rename_vars['SPEAKER_00'].set('話者A')
            with patch.object(gui.messagebox, 'showinfo'), patch.object(gui, 'show_error') as error:
                dialog._save()
            error.assert_not_called()
            self.assertTrue((source.parent / ('custom' + source.suffix)).is_file())
            self.assertEqual(source.read_bytes(), original)
