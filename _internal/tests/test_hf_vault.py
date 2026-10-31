"""Credential migration must never discard the only recoverable token."""
import sys
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch, Mock

from core import Config
import hf_auth
from settings_ui import save_import_with_credential
import unified_gui


class CredentialMigrationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(hf_token_json='hf-old-token')
        self.platform = patch.object(sys, 'platform', 'win32')
        self.env = patch.dict('os.environ', {'HF_TOKEN': ''})
        self.guard = patch.object(hf_auth, 'credential_guard', side_effect=lambda *args, **kwargs: nullcontext())
        self.platform.start(); self.env.start(); self.guard.start()
        self.addCleanup(self.platform.stop); self.addCleanup(self.env.stop); self.addCleanup(self.guard.stop)

    def test_migrates_only_after_verified_read_and_persistence(self):
        persist = Mock()
        with patch.object(hf_auth, 'read_credential', side_effect=['', '', 'hf-old-token']), \
             patch.object(hf_auth, 'write_credential') as write:
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, persist)
        self.assertEqual(token, 'hf-old-token'); self.assertEqual(cfg.hf_token_json, '')
        self.assertEqual(warning, ''); write.assert_called_once_with('hf-old-token')
        self.assertEqual(persist.call_args.args[0].hf_token_json, '')

    def test_failed_write_keeps_original(self):
        persist = Mock()
        with patch.object(hf_auth, 'read_credential', return_value=''), \
             patch.object(hf_auth, 'write_credential', side_effect=OSError('unavailable')), \
             patch.object(hf_auth, 'delete_credential'):
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, persist)
        self.assertEqual(token, 'hf-old-token'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning); persist.assert_not_called()

    def test_failed_read_back_keeps_original(self):
        persist = Mock()
        with patch.object(hf_auth, 'read_credential', side_effect=['', '', 'wrong']), \
             patch.object(hf_auth, 'write_credential'), patch.object(hf_auth, 'delete_credential'):
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, persist)
        self.assertEqual(token, 'hf-old-token'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning); persist.assert_not_called()

    def test_failed_config_write_keeps_original(self):
        with patch.object(hf_auth, 'read_credential', return_value='hf-old-token'):
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, Mock(side_effect=OSError()))
        self.assertEqual(token, 'hf-old-token'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning)

    def test_lock_failure_preserves_legacy_and_does_not_read_or_migrate(self):
        persist = Mock()
        with patch.object(hf_auth, 'credential_guard', side_effect=OSError('locked')), \
             patch.object(hf_auth, 'read_credential') as read:
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, persist)
        self.assertEqual(token, 'hf-old-token'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning); read.assert_not_called(); persist.assert_not_called()

    def test_conflicting_vault_entry_is_not_overwritten(self):
        persist = Mock()
        with patch.object(hf_auth, 'read_credential', return_value='hf-other'), \
             patch.object(hf_auth, 'write_credential') as write:
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, persist)
        self.assertEqual(token, 'hf-other'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning); write.assert_not_called(); persist.assert_not_called()

    def test_vault_precedes_environment_and_environment_is_not_mutated(self):
        with patch.dict('os.environ', {'HF_TOKEN': 'hf-environment'}), \
             patch.object(hf_auth, 'read_credential', return_value='hf-vault'):
            token, cfg, warning = hf_auth.resolve_hf_token(replace(self.config, hf_token_json=''), Mock())
            self.assertEqual(token, 'hf-vault')
            self.assertEqual(__import__('os').environ['HF_TOKEN'], 'hf-environment')
            self.assertFalse(warning)

    def test_legacy_and_environment_conflict_preserves_old_priority(self):
        persist = Mock()
        with patch.dict('os.environ', {'HF_TOKEN': 'hf-environment'}), \
             patch.object(hf_auth, 'read_credential', return_value=''), \
             patch.object(hf_auth, 'write_credential') as write:
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, persist)
        self.assertEqual(token, 'hf-environment'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning); write.assert_not_called(); persist.assert_not_called()

    def test_validation_rejects_invalid_secrets(self):
        for value in ('', 'abc\nxyz', 'abc\x00xyz', 'x' * 1281):
            with self.assertRaises(ValueError): hf_auth._token_bytes(value)

    def test_import_writes_vault_and_not_plaintext_settings(self):
        persist = Mock()
        with patch.object(hf_auth, 'read_credential', side_effect=['hf-previous', 'hf-import']), \
             patch.object(hf_auth, 'write_credential') as write:
            cfg = save_import_with_credential(self.config, 'hf-import', persist)
        write.assert_called_once_with('hf-import')
        self.assertEqual(cfg.hf_token_json, '')
        self.assertEqual(persist.call_args.args[0].hf_token_json, '')

    def test_failed_import_restores_previous_credential(self):
        with patch.object(hf_auth, 'read_credential', side_effect=['hf-previous', 'hf-import']), \
             patch.object(hf_auth, 'write_credential') as write:
            with self.assertRaises(OSError):
                save_import_with_credential(self.config, 'hf-import', Mock(side_effect=OSError()))
        self.assertEqual([call.args[0] for call in write.call_args_list], ['hf-import', 'hf-previous'])

    def test_failed_import_removes_new_entry_if_no_previous_entry(self):
        with patch.object(hf_auth, 'read_credential', side_effect=['', 'hf-import']), \
             patch.object(hf_auth, 'write_credential'), patch.object(hf_auth, 'delete_credential') as delete:
            with self.assertRaises(OSError):
                save_import_with_credential(self.config, 'hf-import', Mock(side_effect=OSError()))
        delete.assert_called_once()

    def test_failed_explicit_vault_write_restores_previous_value(self):
        with patch.object(hf_auth, 'read_credential', side_effect=['hf-previous', 'wrong']), \
             patch.object(hf_auth, 'write_credential') as write:
            with self.assertRaises(OSError): hf_auth.write_verified_credential('hf-new')
        self.assertEqual([call.args[0] for call in write.call_args_list], ['hf-new', 'hf-previous'])

    def test_automatic_migration_does_not_overwrite_newly_created_credential(self):
        with patch.object(hf_auth, 'read_credential', side_effect=['', 'hf-race']), \
             patch.object(hf_auth, 'write_credential') as write:
            token, cfg, warning = hf_auth.resolve_hf_token(self.config, Mock())
        self.assertEqual(token, 'hf-old-token'); self.assertEqual(cfg, self.config)
        self.assertTrue(warning); write.assert_not_called()


class CredentialGuardTests(unittest.TestCase):
    def test_guard_is_reentrant_and_serializes_threads(self):
        attempted, acquired = threading.Event(), threading.Event()
        def worker():
            attempted.set()
            with hf_auth.credential_guard():
                acquired.set()
        with hf_auth.credential_guard():
            with hf_auth.credential_guard():
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(acquired.wait(0.1))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(acquired.is_set())


class CredentialUserInterfaceTests(unittest.TestCase):
    def app(self, legacy=''):
        app = Mock()
        app.config = Config(hf_token_json=legacy)
        app.hf_token = 'previous'
        app.diagnostics.token = 'previous'
        return app

    def test_successful_secure_save_removes_legacy_plaintext(self):
        app = self.app('old-secret')
        with patch.object(unified_gui, 'write_verified_credential') as save, \
             patch.object(unified_gui, 'save_config') as persist, \
             patch.object(unified_gui.messagebox, 'showinfo'):
            result = unified_gui.save_secure_token(app, Mock(get=Mock(return_value='new-secret')), Mock())
        self.assertTrue(result); save.assert_called_once_with('new-secret')
        self.assertEqual(persist.call_args.args[0].hf_token_json, '')
        self.assertEqual(app.config.hf_token_json, '')
        self.assertEqual(app.hf_token, 'new-secret')

    def test_secure_save_reports_if_plaintext_removal_fails(self):
        app = self.app('old-secret')
        with patch.object(unified_gui, 'write_verified_credential'), \
             patch.object(unified_gui, 'save_config', side_effect=OSError()), \
             patch.object(unified_gui.messagebox, 'showwarning') as warning:
            result = unified_gui.save_secure_token(app, Mock(get=Mock(return_value='new-secret')), Mock())
        self.assertFalse(result); self.assertEqual(app.config.hf_token_json, 'old-secret')
        self.assertEqual(app.hf_token, 'new-secret'); warning.assert_called_once()

    def test_delete_preserves_shared_environment_token(self):
        app = self.app()
        var = Mock()
        with patch.dict('os.environ', {'HF_TOKEN': 'environment-secret'}), \
             patch.object(unified_gui.messagebox, 'askyesno', return_value=True), \
             patch.object(unified_gui.messagebox, 'showinfo'), \
             patch.object(unified_gui, 'delete_credential') as delete:
            unified_gui.remove_secure_token(app, var, Mock())
            self.assertEqual(__import__('os').environ['HF_TOKEN'], 'environment-secret')
        delete.assert_called_once(); var.set.assert_called_once_with('environment-secret')
        self.assertEqual(app.hf_token, 'environment-secret')


if __name__ == '__main__': unittest.main()
