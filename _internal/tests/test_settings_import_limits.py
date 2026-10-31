"""Malformed external settings cannot mutate the running application."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
from settings_ui import load_settings_file


class ExternalSettingsLimits(unittest.TestCase):
    def test_large_invalid_encoding_and_deep_json_are_rejected_without_saving(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'sample-settings.json'
            app=SimpleNamespace(busy=False,root=object(),ui=SimpleNamespace(close_drawers=MagicMock()),config=core.Config(model_faster='small'))
            original=app.config
            for payload in (b' '*(core.MAX_CONFIG_BYTES+1),b'\xff\xfeinvalid',b'['*5000+b'0'+b']'*5000):
                source.write_bytes(payload)
                with self.subTest(size=len(payload)),patch('core.save_config') as save,patch('unified_gui.show_error') as error:
                    self.assertIsNone(load_settings_file(app,source))
                    error.assert_called_once()
                    save.assert_not_called()
                    self.assertIs(app.config,original)


if __name__=='__main__':unittest.main()
