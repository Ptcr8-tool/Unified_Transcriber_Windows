"""Behavior tests for safe editing of the local hint dictionary."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config
from hints_dialog import HintsDialog
from test_v25_studio_ui import StudioGUIFixture


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class HintsDialogSafetyTests(unittest.TestCase):
    def make_dialog(self):
        dialog = HintsDialog.__new__(HintsDialog)
        dialog.app = SimpleNamespace(config=Config(), busy=False, status=MagicMock())
        dialog.win = MagicMock()
        dialog.enabled = Variable(True)
        dialog.groups = {'共通': [{'term': '会議室', 'reading': 'かいぎしつ'}],
                         '部署': [{'term': '開発部', 'reading': ''}]}
        dialog.selected = {'部署'}
        dialog.current_group = '共通'
        dialog.word_form_index = None
        dialog.term = Variable('')
        dialog.reading = Variable('')
        dialog._selected_word_index = lambda: None
        return dialog

    def test_save_swaps_config_and_leaves_original_unchanged(self):
        dialog = self.make_dialog()
        before = dialog.app.config
        with patch('hints_dialog.save_config') as save:
            dialog._save()
        saved = save.call_args.args[0]
        self.assertIs(dialog.app.config, saved)
        self.assertIsNot(saved, before)
        self.assertEqual(before.hint_groups, {'共通': []})
        dialog.groups['共通'][0]['term'] = '後の編集'
        self.assertEqual(saved.hint_groups['共通'][0]['term'], '会議室')
        self.assertEqual(saved.hint_selected_groups, ['部署'])
        self.assertTrue(saved.hint_enabled)

    def test_save_blocks_busy_and_unapplied_input(self):
        dialog = self.make_dialog()
        dialog.app.busy = True
        with patch('hints_dialog.messagebox.showinfo'), patch('hints_dialog.save_config') as save:
            dialog._save()
        save.assert_not_called()
        dialog.app.busy = False
        dialog.term = Variable('追加し忘れ')
        with patch('hints_dialog.messagebox.askyesno', return_value=False), patch('hints_dialog.save_config') as save:
            dialog._save()
        save.assert_not_called()
        dialog.win.destroy.assert_not_called()

    def test_invalid_import_preserves_editing_state(self):
        dialog = self.make_dialog()
        before = copy.deepcopy(dialog.groups)
        with patch('hints_dialog.filedialog.askopenfilename', return_value='bad.json'), \
             patch('hints_dialog.read_dictionary', side_effect=ValueError('invalid')), \
             patch('hints_dialog.messagebox.showerror') as show:
            dialog._import()
        self.assertEqual(dialog.groups, before)
        show.assert_called_once()

    def test_unapplied_form_is_not_discarded_when_navigation_declined(self):
        dialog = self.make_dialog()
        dialog.term = Variable('新しい単語')
        with patch('hints_dialog.messagebox.askyesno', return_value=False):
            self.assertFalse(dialog._confirm_discard_form())
        dialog.word_form_index = 0
        dialog.term = Variable('会議室')
        dialog.reading = Variable('かいぎしつ')
        self.assertTrue(dialog._confirm_discard_form())

    def test_export_requires_pending_entry_to_be_added_first(self):
        dialog = self.make_dialog()
        dialog.term = Variable('追加前の単語')
        with patch('hints_dialog.messagebox.showinfo') as warning, \
             patch('hints_dialog.filedialog.asksaveasfilename') as picker:
            dialog._export()
        warning.assert_called_once()
        picker.assert_not_called()


class HintsDialogWindowTests(StudioGUIFixture):
    def test_top_button_opens_dictionary_and_busy_blocks_it(self):
        view = self.app.ui
        factory = MagicMock()
        with patch.dict(view.dialogs, {'hints': factory}):
            view.hints_button.invoke()
            factory.assert_called_once_with(self.app)
            self.app._set_busy(True, 'test')
            view.hints_button.invoke()
            self.assertEqual(factory.call_count, 1)

    def test_edit_and_save_through_real_widgets(self):
        dialog = HintsDialog(self.app)
        self.root.update()
        dialog.enabled.set(True)
        dialog.term.set('文字起こしスタジオ')
        dialog.reading.set('もじおこし')
        dialog._add_word()
        self.assertEqual(len(dialog.word_tree.get_children()), 1)
        with patch('hints_dialog.simpledialog.askstring', return_value='会議'):
            dialog._add_group()
        dialog.term.set('サンプル株式会社')
        dialog._add_word()
        self.assertEqual(dialog.current_group, '会議')
        with patch('hints_dialog.save_config') as save:
            dialog._save()
        self.root.update()
        save.assert_called_once()
        cfg = self.app.config
        self.assertTrue(cfg.hint_enabled)
        self.assertEqual(cfg.hint_groups['共通'][0]['term'], '文字起こしスタジオ')
        self.assertEqual(cfg.hint_groups['会議'][0]['term'], 'サンプル株式会社')
        self.assertEqual(cfg.hint_selected_groups, ['会議'])


if __name__ == '__main__':
    unittest.main()
