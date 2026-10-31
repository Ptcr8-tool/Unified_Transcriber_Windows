"""v22 screen-recorder preview regressions (no Windows hardware required)."""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from window_preview import fit_preview, bgra_to_ppm, capture_preview


class PreviewImageTests(unittest.TestCase):
    def test_aspect_ratios_and_limits(self):
        self.assertEqual(fit_preview(1920,1080,400,200),(356,200))
        self.assertEqual(fit_preview(1080,1920,400,200),(112,200))
        self.assertEqual(fit_preview(100,100,400,200),(100,100))
        with self.assertRaises(ValueError):fit_preview(0,300)

    def test_bgra_rgb_conversion_and_ppm_header(self):
        ppm=bgra_to_ppm(bytes([7,9,11,255, 16,20,30,0]),2,1)
        self.assertEqual(ppm,b'P6\n2 1\n255\n'+bytes([11,9,7,30,20,16]))
        with self.assertRaises(ValueError):bgra_to_ppm(b'bad',1,1)

    @unittest.skipIf(os.name=='nt','Windows hardware smoke test not in CI')
    def test_linux_raises_clean_error(self):
        with self.assertRaisesRegex(OSError,'Windows'):
            capture_preview('desktop')
