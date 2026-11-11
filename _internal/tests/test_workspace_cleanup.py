"""A hidden successful workspace may be cleared; saved and unfinished work may not."""
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace_cleanup as cleanup
from engine_progress import EngineUpdate
import test_v25_studio_ui as fixtures


class WorkspaceCleanupTests(fixtures.StudioGUIFixture):
    def setUp(self):
        super().setUp()
        self.app.config = replace(self.app.config, tray_close=True, onboarding_seen=True,
                                  auto_open_speaker=False)
        self.app.tray.start = MagicMock(return_value=True)

    def tearDown(self):
        if hasattr(self, 'app'):
            self.app.config = replace(self.app.config, tray_close=False, tray_minimize=False)
        super().tearDown()

    def row(self, status='完了'):
        index = len(self.app.job_sources)
        source = self.folder / f'capture-{index}.wav'
        source.write_bytes(b'RIFF saved audio')
        self.app._add_paths([source])
        key = str(source)
        values = list(self.app.tree.item(key, 'values'))
        values[2] = status
        self.app.tree.item(key, values=values)
        output = self.folder / f'capture-{index}.txt'
        output.write_text('Saved transcription.\n', encoding='utf-8')
        self.app.job_outputs[key] = [output]
        return key, source, output

    def show_preview(self, text):
        # Real engine updates are deliberately ignored while idle. Reproduce
        # the actual in-flight display before testing a later close boundary.
        previous = self.app.busy
        self.app.busy = True
        try:
            update = EngineUpdate('文字起こし', snippet=text)
            cleanup.observe_event(self.app, ('engine-progress', update))
            self.app._show_progress(update)
        finally:
            self.app.busy = previous
        self.assertIn(text, self.app.recognized_box.get('1.0', 'end-1c'))

    def preview(self, key, output, status='完了'):
        self.app._reset_recognition_preview()
        self.show_preview('Visible transcription')
        cleanup.observe_event(self.app, ('transcript-saved', key, [str(output)], status))

    def reopen(self):
        self.app.close()
        self.assertEqual(self.root.state(), 'withdrawn')
        self.app.restore_from_tray()
        self.root.update()
        self.assertEqual(self.root.state(), 'normal')

    def test_x_close_reopen_removes_successful_ui_but_keeps_all_saved_bytes(self):
        key, source, output = self.row()
        original = {path: path.read_bytes() for path in (source, output)}
        self.preview(key, output)
        settings = self.app.config
        self.reopen()
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(self.app.job_sources, {})
        self.assertEqual(self.app.job_outputs, {})
        self.assertEqual(self.app.job_options, {})
        self.assertEqual(self.app.job_policies, {})
        self.assertEqual(self.app.recognized_box.get('1.0', 'end-1c'), '')
        self.assertIs(self.app.config, settings)
        self.assertTrue(all(path.read_bytes() == data for path, data in original.items()))

    def test_mixed_queue_removes_only_exact_success_not_failed_partial_pending_or_skipped(self):
        good, _, _ = self.row()
        retained = []
        for status in ('待機中', '失敗', '中止', '処理中', '一部完了', '一部完了（話者分離1件失敗）', 'スキップ'):
            retained.append(self.row(status))
        key, _, output = retained[-2]
        self.preview(key, output, '一部完了')
        text = self.app.recognized_box.get('1.0', 'end-1c')
        self.reopen()
        self.assertFalse(self.app.tree.exists(good))
        self.assertEqual(set(self.app.tree.get_children()), {r[0] for r in retained})
        self.assertEqual(self.app.recognized_box.get('1.0', 'end-1c'), text)

    def test_successful_record_only_and_live_result_rows_are_retired(self):
        rows = [self.row(status) for status in ('録音済み', '録画済み', 'ライブ完了')]
        self.preview(rows[-1][0], rows[-1][2], 'ライブ完了')
        self.reopen()
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertTrue(all(source.exists() and output.exists() for _, source, output in rows))

    def test_minimize_restore_or_relaunch_while_visible_keeps_workspace(self):
        key, _, output = self.row()
        self.preview(key, output)
        self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.app.hide_to_tray()
        self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.assertIn('Visible', self.app.recognized_box.get('1.0', 'end-1c'))

    def test_reopen_during_processing_does_not_clear_now_or_later_automatically(self):
        key, _, output = self.row()
        self.preview(key, output)
        self.app.busy = True
        self.reopen()
        self.assertTrue(self.app.tree.exists(key))
        self.app.busy = False
        self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.reopen()
        self.assertFalse(self.app.tree.exists(key))

    def test_close_while_busy_can_clean_after_it_finishes_while_hidden(self):
        key, _, output = self.row('処理中')
        self.app.busy = True
        self.app.close()
        self.assertTrue(self.app.tree.exists(key))
        values = list(self.app.tree.item(key, 'values')); values[2] = '完了'
        self.app.tree.item(key, values=values)
        self.preview(key, output)
        self.app.busy = False
        self.app.restore_from_tray()
        self.assertFalse(self.app.tree.exists(key))

    def test_pending_final_events_callbacks_and_owned_process_guard_against_races(self):
        key, _, output = self.row()
        self.preview(key, output)
        self.app.close()
        self.app.events.put(('job', key, '失敗'))
        self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.app.events.get_nowait()
        self.app.close()
        with patch.object(self.app.processes, 'active', return_value=[object()]):
            self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.app.close()
        with patch.object(self.app, 'worker', SimpleNamespace(is_alive=lambda: True)):
            self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.app.close()
        self.app.ui_callbacks.put((self.root, lambda: None))
        self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))
        self.app.ui_callbacks.get_nowait()

    def test_saved_transcript_preview_without_persistent_audio_can_be_cleared(self):
        output = self.folder / 'transcribe-only.txt'
        output.write_text('saved', encoding='utf-8')
        self.preview(str(self.folder / 'temporary.wav'), output)
        self.reopen()
        self.assertEqual(self.app.recognized_box.get('1.0', 'end-1c'), '')
        self.assertEqual(output.read_text(encoding='utf-8'), 'saved')

    def test_uncommitted_or_failed_preview_is_not_discarded(self):
        self.app._reset_recognition_preview()
        self.show_preview('Uncommitted text')
        self.reopen()
        self.assertIn('Uncommitted', self.app.recognized_box.get('1.0', 'end-1c'))

    def test_older_successful_rows_never_certify_a_new_unsaved_preview(self):
        key, _, output = self.row()
        self.preview(key, output)
        self.app._reset_recognition_preview()
        self.show_preview('Uncommitted newer text')
        self.reopen()
        self.assertFalse(self.app.tree.exists(key))
        self.assertIn('Uncommitted newer', self.app.recognized_box.get('1.0', 'end-1c'))

    def test_successful_latest_preview_clears_without_dropping_previous_failed_rows(self):
        failed, _, _ = self.row('失敗')
        good, _, output = self.row()
        self.preview(good, output)
        self.reopen()
        self.assertTrue(self.app.tree.exists(failed))
        self.assertFalse(self.app.tree.exists(good))
        self.assertEqual(self.app.recognized_box.get('1.0', 'end-1c'), '')

    def test_conversion_failure_after_asr_success_protects_recording_and_preview(self):
        key, _, output = self.row()
        cleanup.observe_event(self.app, ('capture-queue', Path(key), '処理中'))
        self.preview(key, output)
        cleanup.observe_event(self.app, ('errorlog', 'PCM WAV変換失敗'))
        self.reopen()
        self.assertTrue(self.app.tree.exists(key))
        self.assertIn('Visible', self.app.recognized_box.get('1.0', 'end-1c'))

    def test_capture_move_preserves_failure_guard(self):
        key, _, output = self.row()
        cleanup.observe_event(self.app, ('capture-queue', Path(key), '処理中'))
        self.preview(key, output)
        cleanup.observe_event(self.app, ('errorlog', 'finalization error'))
        replacement = self.folder / 'converted.wav'
        replacement.write_bytes(b'RIFF converted')
        self.app._move_recorded_queue_item(Path(key), replacement)
        cleanup.observe_event(self.app, ('capture-moved', Path(key), replacement))
        self.reopen()
        self.assertTrue(self.app.tree.exists(str(replacement)))

    def test_pending_speaker_edits_and_modal_dialog_keep_result_available(self):
        key, _, output = self.row()
        self.preview(key, output)
        self.app.pending_speaker_edits.append(output)
        self.reopen()
        self.assertTrue(self.app.tree.exists(key))
        self.app.pending_speaker_edits.clear()
        self.app.close()
        with patch.object(self.root, 'grab_current', return_value=object()):
            self.app.restore_from_tray()
        self.assertTrue(self.app.tree.exists(key))

    def test_unsuccessful_tray_hide_never_requests_cleanup_or_exits(self):
        key, _, output = self.row()
        self.preview(key, output)
        with patch.object(self.app, 'hide_to_tray', return_value=False), \
             patch.object(self.app, 'request_exit') as exit_app:
            self.app.close()
        self.assertFalse(getattr(self.app, '_clear_completed_after_close', False))
        self.assertTrue(self.app.tree.exists(key))
        exit_app.assert_not_called()

    def test_real_poll_observes_commit_and_later_error_before_reopening(self):
        key, _, output = self.row()
        self.preview(key, output)
        self.app.events.put(('capture-queue', Path(key), '処理中'))
        self.app.events.put(('transcript-saved', key, [str(output)], '完了'))
        self.app.events.put(('errorlog', 'conversion failed'))
        self.app._poll()
        self.reopen()
        self.assertTrue(self.app.tree.exists(key))


if __name__ == '__main__':
    unittest.main()
