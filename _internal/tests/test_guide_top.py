"""The illustrated guide is reachable from the top of the studio."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_ui
from test_v25_studio_ui import StudioGUIFixture


class GuideOpenTests(unittest.TestCase):
    def test_opens_bundled_guide_as_file_url(self):
        with patch.object(studio_ui.webbrowser, 'open', return_value=True) as browser:
            self.assertTrue(studio_ui.open_illustrated_guide(None))
        guide = Path(studio_ui.__file__).resolve().parent / 'docs' / 'GUIDE.html'
        browser.assert_called_once_with(guide.as_uri())

    def test_missing_guide_reports_error(self):
        with patch.object(Path, 'is_file', return_value=False), \
             patch.object(studio_ui.webbrowser, 'open') as browser, \
             patch.object(studio_ui.messagebox, 'showerror') as error:
            self.assertFalse(studio_ui.open_illustrated_guide(None))
        browser.assert_not_called()
        error.assert_called_once()

    def test_browser_failure_reports_error(self):
        with patch.object(studio_ui.webbrowser, 'open', return_value=False), \
             patch.object(studio_ui.messagebox, 'showerror') as error:
            self.assertFalse(studio_ui.open_illustrated_guide(None))
        error.assert_called_once()


class GuideButtonTests(StudioGUIFixture):
    def test_top_button_opens_guide_while_busy(self):
        self.app.busy = True
        with patch.object(studio_ui, 'open_illustrated_guide', return_value=True) as opened:
            self.app.ui.guide_button.invoke()
        opened.assert_called_once_with(self.root)
        self.assertNotIn(self.app.ui.guide_button, self.app.ui.idle_controls)


if __name__ == '__main__':
    unittest.main()
