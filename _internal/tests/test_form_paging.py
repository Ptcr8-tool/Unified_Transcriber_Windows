"""Pages preserve full data and selection without a second scroll region."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_file_dialogs import FormListPages
from unified_gui import _preview_pages
import unified_gui
import test_v25_studio_ui as fixtures
from wheel_support import wheel_canvas_for
from tkinter import ttk


class FakeList:
    def __init__(self):
        self.items = []
        self.selection = set()
    def curselection(self):return tuple(sorted(self.selection))
    def delete(self, *_):self.items.clear();self.selection.clear()
    def selection_set(self, index):self.selection.add(index)
    def configure(self, **_):pass


class FormPagingTests(unittest.TestCase):
    def test_selection_across_pages_and_same_filename_in_different_directories(self):
        paths = [Path(str(i))/'sample.txt' for i in range(45)]
        widget = FakeList()
        page = FormListPages.__new__(FormListPages)
        page.widget, page._paths, page.tree = widget, lambda:paths, False
        page.page, page.visible, page.selected = 0, {}, set()
        page.previous = MagicMock();page.next = MagicMock();page.label = MagicMock()
        page.render_item = lambda index, path, key:widget.items.append(path)
        page.refresh()
        widget.selection_set(1)
        page.go(1)
        widget.selection_set(1)
        page.go(1)
        widget.selection_set(4)
        self.assertEqual(page.selected_indices(), [1, 21, 44])
        page.go(-2)
        self.assertEqual(widget.curselection(), (1,))
        paths.pop(21)
        page.refresh()
        self.assertEqual(page.selected_indices(), [1, 43])

    def test_preview_pages_cover_every_character_without_truncation(self):
        source = ('日本語' * 310 + '\nline\n' * 40 + '最後')
        pages = _preview_pages(source)
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(pages), source)
        self.assertEqual(_preview_pages(''), [''])


class FormPagingGuiTests(fixtures.StudioGUIFixture):
    def test_merge_selection_and_remove_across_pages(self):
        dialog = unified_gui.ConcatDialog(self.app, 'text')
        dialog.paths = [self.folder/f'file-{i}.txt' for i in range(45)]
        dialog._render(); self.root.update()
        self.assertIsNotNone(wheel_canvas_for(dialog.listbox))
        self.assertFalse(any(isinstance(w, ttk.Scrollbar) and w.cget('orient') == 'vertical'
                             for w in dialog.listbox.master.winfo_children()))
        dialog.listbox.selection_set('1')
        dialog.pages.go(1)
        dialog.listbox.selection_set('21')
        self.assertEqual(dialog._selected_indices(), [1, 21])
        dialog._remove()
        self.assertEqual(len(dialog.paths), 43)
        self.assertNotIn(self.folder/'file-1.txt', dialog.paths)
        self.assertNotIn(self.folder/'file-21.txt', dialog.paths)
        dialog.win.destroy()

    def test_conversion_selection_and_remove_across_pages(self):
        dialog = unified_gui.TranscriptConvertDialog(self.app)
        dialog.paths = [self.folder/f'file-{i}.txt' for i in range(45)]
        dialog._render(); self.root.update()
        self.assertIsNotNone(wheel_canvas_for(dialog.listbox))
        self.assertFalse(any(isinstance(w, ttk.Scrollbar) for w in dialog.listbox.master.winfo_children()))
        dialog.listbox.selection_set(1)
        dialog.pages.go(1)
        dialog.listbox.selection_set(1)
        self.assertEqual(dialog.pages.selected_indices(), [1, 21])
        dialog._remove()
        self.assertEqual(len(dialog.paths), 43)
        self.assertNotIn(self.folder/'file-1.txt', dialog.paths)
        self.assertNotIn(self.folder/'file-21.txt', dialog.paths)
        dialog.win.destroy()


if __name__ == '__main__':
    unittest.main()
