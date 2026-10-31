"""Regression checks for relocated dialogs and failure diagnostics."""
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui_file_dialogs
import unified_gui


class FileDialogSeparationTests(unittest.TestCase):
    def test_public_classes_keep_shared_entry_point_services(self):
        for public, implementation in (
            (unified_gui.WatchDialog, gui_file_dialogs.WatchDialog),
            (unified_gui.ConcatDialog, gui_file_dialogs.ConcatDialog),
            (unified_gui.TranscriptConvertDialog, gui_file_dialogs.TranscriptConvertDialog),
        ):
            self.assertTrue(issubclass(public, implementation))
            instance = object.__new__(public)
            with patch.object(unified_gui, 'MERGE_SCAN_ENTRY_LIMIT', 37):
                self.assertEqual(instance._ui()['MERGE_SCAN_ENTRY_LIMIT'], 37)

    def test_merge_failure_records_traceback_and_still_completes(self):
        app = unified_gui.UnifiedApp.__new__(unified_gui.UnifiedApp)
        app.events = queue.Queue()
        app.stop_event = threading.Event()
        with patch.object(unified_gui, 'merge_text_files', side_effect=RuntimeError('test failure')):
            app._text_merge_worker([Path('one.txt'), Path('two.txt')], Path('out.txt'), False)
        events = list(app.events.queue)
        log = next(value for kind, value in events if kind == 'errorlog')
        self.assertIn('Traceback (most recent call last)', log)
        self.assertIn('RuntimeError: test failure', log)
        self.assertEqual(events[-1][0], 'done')
        self.assertIn('失敗', events[-1][1])

    def test_convert_avoids_symlink_cycles_and_preserves_existing_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / 'sub'
            child.mkdir()
            result = child / 'new.csv'
            result.write_text('text', encoding='utf-8')
            ignored = child / 'ignore.wav'
            ignored.write_text('data', encoding='utf-8')
            try:
                (child / 'back').symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('directory symlinks unavailable')
            previous = root / 'existing.txt'
            previous.write_text('existing', encoding='utf-8')
            dialog = object.__new__(unified_gui.TranscriptConvertDialog)
            dialog.paths = [previous.resolve()]
            dialog._render = Mock()
            dialog._accept([root, result])
            self.assertEqual(dialog.paths, [previous.resolve(), result.resolve()])
            dialog._render.assert_called_once()

    def test_convert_limit_requires_explicit_partial_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(4):
                (root / f'{index}.txt').write_text('data', encoding='utf-8')
            original = Path('already-selected.txt')
            dialog = object.__new__(unified_gui.TranscriptConvertDialog)
            dialog.paths = [original]
            dialog.win = object()
            dialog._render = Mock()
            with patch.object(unified_gui, 'MERGE_SCAN_ENTRY_LIMIT', 3), \
                 patch('gui_file_dialogs.messagebox.askyesno', return_value=False) as confirm:
                dialog._accept([root])
            self.assertEqual(dialog.paths, [original])
            self.assertIn('検出した 2 件を追加しますか？', confirm.call_args.args[1])
            dialog._render.assert_not_called()
            with patch.object(unified_gui, 'MERGE_SCAN_ENTRY_LIMIT', 3), \
                 patch('gui_file_dialogs.messagebox.askyesno', return_value=True):
                dialog._accept([root])
            self.assertEqual(dialog.paths[0], original)
            self.assertEqual(len(dialog.paths), 3)
            self.assertEqual(dialog.paths[1:], sorted(dialog.paths[1:]))
            self.assertTrue(all(path.parent == root.resolve() for path in dialog.paths[1:]))
            dialog._render.assert_called_once()


if __name__ == '__main__':
    unittest.main()
