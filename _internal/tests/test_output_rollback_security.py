"""Preserve originals when both publishing and filesystem rollback fail."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from output_commit import publish_outputs


class RecoveryBackupTests(unittest.TestCase):
    def test_rollback_failure_retains_original_and_reports_recovery_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first, second = folder/'first.txt', folder/'second.csv'
            first_stage, second_stage = folder/'first.stage', folder/'second.stage'
            for path, value in ((first, 'original text'), (second, 'original csv'),
                                (first_stage, 'new text'), (second_stage, 'new csv')):
                path.write_text(value, encoding='utf-8')
            replace = os.replace

            def fail_publish_and_restore(source, destination):
                if Path(source) == second_stage or Path(source).suffix == '.backup':
                    raise OSError('simulated locked destination')
                return replace(source, destination)

            with patch('output_commit.os.replace', side_effect=fail_publish_and_restore):
                with self.assertRaises(RuntimeError) as result:
                    publish_outputs({first:first_stage, second:second_stage}, overwrite=True)
            recovery = list(folder.glob('.first.txt.*.backup'))
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].read_text(encoding='utf-8'), 'original text')
            self.assertIn(str(recovery[0]), str(result.exception))
            self.assertEqual(second.read_text(encoding='utf-8'), 'original csv')

    def test_successful_rollback_restores_originals_and_removes_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first, second = folder/'first.txt', folder/'second.csv'
            first_stage, second_stage = folder/'first.stage', folder/'second.stage'
            for path, value in ((first, 'original text'), (second, 'original csv'),
                                (first_stage, 'new text'), (second_stage, 'new csv')):
                path.write_text(value, encoding='utf-8')
            replace = os.replace

            def fail_second_publish(source, destination):
                if Path(source) == second_stage:
                    raise OSError('simulated publish failure')
                return replace(source, destination)

            with patch('output_commit.os.replace', side_effect=fail_second_publish):
                with self.assertRaises(OSError):
                    publish_outputs({first:first_stage, second:second_stage}, overwrite=True)
            self.assertEqual(first.read_text(encoding='utf-8'), 'original text')
            self.assertEqual(second.read_text(encoding='utf-8'), 'original csv')
            self.assertEqual(list(folder.glob('*.backup')), [])


if __name__ == '__main__':
    unittest.main()
