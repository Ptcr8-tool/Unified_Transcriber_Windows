from __future__ import annotations

import io
import errno
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engines import openai_resemblyzer as engine


class WatchRecoveryTests(unittest.TestCase):
    def run_once(self, root: Path, fail_move: str):
        # The watcher resolves its paths before processing.  On Windows this
        # also expands TEMP's 8.3 form (e.g. RUNNER~1) to its long name; use
        # the same canonical root for the injected failure and log checks.
        root = root.resolve()
        incoming = root / 'incoming'
        incoming.mkdir()
        source = incoming / 'recording.wav'
        source.write_bytes(b'audio')
        # A prior interrupted result must not be removed when processing starts.
        previous = incoming / 'recording.transcribing.txt'
        previous.write_text('previous result', encoding='utf-8')
        args = SimpleNamespace(input_dir=incoming, processed_dir=root / 'processed',
                               failed_dir=root / 'failed', poll_seconds=1,
                               model='turbo', language='ja', max_speakers=6,
                               threshold=.25, speakers=None, device='cpu',
                               diarization_unit='utterance', segment_length=20,
                               caption_max_chars=None, caption_max_seconds=5)
        move = engine.move_file_no_overwrite

        def transcribe(_source, destination, *args, **kwargs):
            destination.write_text('completed transcript', encoding='utf-8')

        def fail_selected_move(src, dst):
            if (fail_move == 'media' and Path(src) == source and Path(dst).parent == root / 'processed' or
                    fail_move == 'transcript' and Path(src).name.startswith('.transcribing-')):
                raise PermissionError('destination is locked')
            return move(src, dst)

        errors = io.StringIO()
        with (patch.object(engine, 'wait_until_stable', return_value=True),
              patch.object(engine, 'transcribe', side_effect=transcribe),
              patch.object(engine, 'move_file_no_overwrite', side_effect=fail_selected_move),
              patch.object(engine.time, 'sleep', side_effect=KeyboardInterrupt),
              redirect_stdout(io.StringIO()), redirect_stderr(errors)):
            with self.assertRaises(KeyboardInterrupt):
                engine.watch_folder(args)

        self.assertEqual(previous.read_text(encoding='utf-8'), 'previous result')
        saved = list(incoming.glob('.transcribing-*.txt'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(encoding='utf-8'), 'completed transcript')
        self.assertIn('destination is locked', errors.getvalue())
        self.assertIn(str(saved[0]), errors.getvalue())
        log = (root / 'failed' / 'recording.wav.error.log').read_text(encoding='utf-8')
        self.assertIn('destination is locked', log)
        self.assertIn(str(saved[0]), log)
        return root, log

    def test_completed_text_is_retained_if_audio_move_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.run_once(Path(directory), 'media')
            self.assertEqual((root / 'failed' / 'recording.wav').read_bytes(), b'audio')

    def test_completed_text_and_media_location_are_retained_if_text_move_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root, log = self.run_once(Path(directory), 'transcript')
            moved = root / 'processed' / 'recording.wav'
            self.assertEqual(moved.read_bytes(), b'audio')
            self.assertIn(str(moved), log)

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.write_bytes(b'original source')
            target.write_bytes(b'preexisting target')
            with self.assertRaises(FileExistsError):
                engine.move_file_no_overwrite(source, target)
            self.assertEqual(source.read_bytes(), b'original source')
            self.assertEqual(target.read_bytes(), b'preexisting target')

    def test_previous_error_log_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            failed_media = Path(directory) / 'recording.wav'
            previous = Path(directory) / 'recording.wav.error.log'
            previous.write_text('old failure', encoding='utf-8')
            new_log = engine.write_watch_error_log(failed_media, 'new failure')
            self.assertEqual(previous.read_text(encoding='utf-8'), 'old failure')
            self.assertEqual(new_log.name, 'recording.wav.error.1.log')
            self.assertEqual(new_log.read_text(encoding='utf-8'), 'new failure')

    @unittest.skipIf(engine.os.name == 'nt', 'POSIX cross-device link simulation')
    def test_cross_volume_copy_is_published_before_source_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.write_bytes(b'complete source')
            os.utime(source, (946684800, 946684800))
            link = engine.os.link
            def cross_device(src, dst):
                if Path(src) == source:
                    raise OSError(errno.EXDEV, 'different volume')
                self.assertEqual(source.read_bytes(), b'complete source')
                return link(src, dst)
            with patch.object(engine.os, 'link', side_effect=cross_device):
                engine.move_file_no_overwrite(source, target)
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b'complete source')
            self.assertEqual(int(target.stat().st_mtime), 946684800)

    @unittest.skipIf(engine.os.name == 'nt', 'POSIX cross-device link simulation')
    def test_copy_failure_keeps_source_and_cleans_partial_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.write_bytes(b'complete source')
            with (patch.object(engine.os, 'link', side_effect=OSError(errno.EXDEV, 'different volume')),
                  patch.object(shutil, 'copyfileobj', side_effect=OSError('disk full'))):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    engine.move_file_no_overwrite(source, target)
            self.assertEqual(source.read_bytes(), b'complete source')
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [source])

    @unittest.skipIf(engine.os.name == 'nt', 'POSIX cross-device link simulation')
    def test_cross_volume_concurrent_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.write_bytes(b'complete source')
            link = engine.os.link
            def concurrent_create(src, dst):
                if Path(src) == source:
                    raise OSError(errno.EXDEV, 'different volume')
                target.write_bytes(b'concurrent file')
                return link(src, dst)
            with patch.object(engine.os, 'link', side_effect=concurrent_create):
                with self.assertRaises(FileExistsError):
                    engine.move_file_no_overwrite(source, target)
            self.assertEqual(source.read_bytes(), b'complete source')
            self.assertEqual(target.read_bytes(), b'concurrent file')
            self.assertEqual(set(Path(directory).iterdir()), {source, target})

    @unittest.skipUnless(engine.os.name == 'nt', 'Windows cross-volume rename simulation')
    def test_windows_cross_volume_concurrent_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.write_bytes(b'complete source')
            rename = engine.os.rename
            def concurrent_create(src, dst):
                if Path(src) == source:
                    raise OSError(errno.EXDEV, 'different volume')
                target.write_bytes(b'concurrent file')
                return rename(src, dst)
            with patch.object(engine.os, 'rename', side_effect=concurrent_create):
                with self.assertRaises(FileExistsError):
                    engine.move_file_no_overwrite(source, target)
            self.assertEqual(source.read_bytes(), b'complete source')
            self.assertEqual(target.read_bytes(), b'concurrent file')
            self.assertEqual(set(Path(directory).iterdir()), {source, target})


if __name__ == '__main__':
    unittest.main()
