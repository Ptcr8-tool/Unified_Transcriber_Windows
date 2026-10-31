"""Keep Japanese diagnostic text consistent with the surrounding UI."""
from tkinter import font as tkfont
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unified_gui
import studio_ui
from test_v25_studio_ui import StudioGUIFixture


class JapaneseLogFontTests(StudioGUIFixture):
    def _font_family(self, widget):
        return tkfont.Font(root=self.root, font=widget.cget('font')).actual('family')

    def test_main_and_detached_logs_use_preview_font_and_keep_mixed_text(self):
        text = '[診断] 日本語テスト / C:\\Example\\sample.wav / 進捗 80%\n'
        self.app.log(text)
        self.app.open_detached_log()
        self.root.update()
        self.assertEqual(self._font_family(self.app.logbox),
                         self._font_family(self.app.recognized_box))
        self.assertEqual(self._font_family(self.app.detached_logbox),
                         self._font_family(self.app.recognized_box))
        self.assertIn(text, self.app.logbox.get('1.0', 'end'))
        self.assertIn(text, self.app.detached_logbox.get('1.0', 'end'))
        for dark, palette in ((False, studio_ui.LIGHT), (True, studio_ui.DARK)):
            self.app.ui._apply_appearance(dark)
            self.root.update()
            for widget in (self.app.logbox, self.app.detached_logbox):
                self.assertEqual(self._font_family(widget), self._font_family(self.app.recognized_box))
                self.assertEqual(widget.cget('background'), palette['surface'])
                self.assertIn(text, widget.get('1.0', 'end'))
        self.app.detached_logbox.winfo_toplevel().destroy()
        self.app.detached_logbox = None

    def test_cuda_diagnostic_uses_preview_font(self):
        previous_jobs = set(self.root.tk.call('after', 'info'))
        dialog = unified_gui.CudaTestDialog(self.app)
        try:
            self.root.update()
            self.assertEqual(self._font_family(dialog.output),
                             self._font_family(self.app.recognized_box))
            dialog._append('診断ログ: CUDA 10%\n')
            self.assertIn('診断ログ: CUDA 10%', dialog.output.get('1.0', 'end'))
            for dark, palette in ((False, studio_ui.LIGHT), (True, studio_ui.DARK)):
                self.app.ui._apply_appearance(dark)
                self.root.update()
                self.assertEqual(self._font_family(dialog.output),
                                 self._font_family(self.app.recognized_box))
                self.assertEqual(dialog.output.cget('background'), palette['surface'])
                self.assertIn('診断ログ: CUDA 10%', dialog.output.get('1.0', 'end'))
        finally:
            self.finish_dialog(dialog.win, previous_jobs)
