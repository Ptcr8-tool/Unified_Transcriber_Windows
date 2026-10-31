"""Regression coverage for the reported v6 Windows D&D breakage."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
import dnd_support


def fake_wheel(extra=()):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('tkinterdnd2/__init__.py', 'DND_FILES="DND_Files"\nclass TkinterDnD: pass\n')
        z.writestr('tkinterdnd2/tkdnd/win64/pkgIndex.tcl','package ifneeded tkdnd 2.9.5 {}\n')
        for key in extra: z.writestr(key,'not trusted')
    return data.getvalue()


class DnDSetupRegression(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop('tkinterdnd2',None)

    def test_offline_mock_wheel_install_and_import_without_pip(self):
        wheel=fake_wheel()
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'gui_dependencies'
            log=io.StringIO()
            with patch.dict(os.environ, {'PIP_USER':'1', 'PIP_TARGET':'other', 'PIP_PREFIX':'elsewhere'}), \
                 patch('subprocess.run', side_effect=AssertionError('pip must not run')):
                self.assertTrue(dnd_support.ensure_drag_drop(log,target,downloader=lambda:wheel,sha256=hashlib.sha256(wheel).hexdigest()))
            self.assertTrue((target/'tkinterdnd2'/'tkdnd'/'win64'/'pkgIndex.tcl').is_file())
            self.assertIn('without pip',log.getvalue())
            if str(target) in sys.path: sys.path.remove(str(target))

    def test_already_installed_no_download_or_pip(self):
        wheel=fake_wheel()
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'gui_dependencies'
            dnd_support.install_wheel(wheel,target,hashlib.sha256(wheel).hexdigest())
            with patch.object(dnd_support,'fetch_wheel',side_effect=AssertionError('not needed')):
                self.assertTrue(dnd_support.ensure_drag_drop(io.StringIO(),target,sha256=hashlib.sha256(wheel).hexdigest()))
            if str(target) in sys.path: sys.path.remove(str(target))

    def test_repair_replaces_existing_package_even_if_it_imports(self):
        wheel=fake_wheel()
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'gui_dependencies'
            dnd_support.install_wheel(wheel,target,hashlib.sha256(wheel).hexdigest())
            old=target/'tkinterdnd2'/'obsolete.txt'
            old.write_text('stale',encoding='utf-8')
            result=dnd_support.ensure_drag_drop(io.StringIO(),target,
                downloader=lambda:wheel,sha256=hashlib.sha256(wheel).hexdigest(),force=True)
            self.assertTrue(result)
            self.assertFalse(old.exists())
            if str(target) in sys.path: sys.path.remove(str(target))

    def test_checksum_failure_keeps_previous_files(self):
        wheel=fake_wheel()
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'gui_dependencies'
            (target/'tkinterdnd2').mkdir(parents=True)
            original=target/'tkinterdnd2'/'original.txt'
            original.write_text('preserve')
            with self.assertRaisesRegex(ValueError,'checksum'):
                dnd_support.install_wheel(wheel,target,sha256='0'*64)
            self.assertEqual(original.read_text(),'preserve')

    def test_old_or_tampered_package_is_replaced_before_import(self):
        wheel = fake_wheel()
        digest = hashlib.sha256(wheel).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/'gui_dependencies'
            package = target/'tkinterdnd2'
            package.mkdir(parents=True)
            sentinel = Path(d)/'ran.txt'
            (package/'__init__.py').write_text(
                f'open({str(sentinel)!r}, "w").write("executed")', encoding='utf-8')
            self.assertFalse(dnd_support.verify_installed(target, digest))
            self.assertTrue(dnd_support.ensure_drag_drop(io.StringIO(), target,
                downloader=lambda: wheel, sha256=digest))
            self.assertFalse(sentinel.exists())
            self.assertTrue(dnd_support.verify_installed(target, digest))
            if str(target) in sys.path: sys.path.remove(str(target))

    def test_modified_extra_or_stale_contents_fail_verification(self):
        wheel = fake_wheel()
        digest = hashlib.sha256(wheel).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/'gui_dependencies'
            dnd_support.install_wheel(wheel, target, digest)
            self.assertTrue(dnd_support.verify_installed(target, digest))
            self.assertFalse(dnd_support.verify_installed(target, '0'*64))
            extra = target/'tkinterdnd2'/'evil.py'
            extra.write_text('unexpected', encoding='utf-8')
            self.assertFalse(dnd_support.verify_installed(target, digest))
            extra.unlink()
            path = target/'tkinterdnd2'/'__init__.py'
            path.write_text('tampered', encoding='utf-8')
            self.assertFalse(dnd_support.verify_installed(target, digest))

    def test_failed_import_retains_old_package_in_private_backup(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as wheel:
            wheel.writestr('tkinterdnd2/__init__.py', 'raise RuntimeError("simulated")')
            wheel.writestr('tkinterdnd2/tkdnd/win64/pkgIndex.tcl', 'test')
        raw = data.getvalue()
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/'gui_dependencies'
            package = target/'tkinterdnd2'
            package.mkdir(parents=True)
            (package/'old-data.txt').write_text('preserved', encoding='utf-8')
            self.assertFalse(dnd_support.ensure_drag_drop(io.StringIO(), target,
                downloader=lambda: raw, sha256=hashlib.sha256(raw).hexdigest()))
            backups = list(target.glob('.dnd-backup-*/old-data.txt'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding='utf-8'), 'preserved')
            if str(target) in sys.path: sys.path.remove(str(target))

    def test_zip_duplicate_package_member_is_rejected(self):
        wheel = fake_wheel(['tkinterdnd2/__init__.py'])
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                dnd_support.install_wheel(wheel, Path(d), hashlib.sha256(wheel).hexdigest())

    def test_direct_gui_import_does_not_use_unverified_preloaded_package(self):
        script = '''
import sys, types
sys.path.insert(0, sys.argv[1])
import dnd_support
dnd_support.available = lambda *args, **kwargs: False
fake = types.ModuleType("tkinterdnd2")
def forbidden(name):
    raise AssertionError("unverified module accessed")
fake.__getattr__ = forbidden
sys.modules["tkinterdnd2"] = fake
import unified_gui
assert unified_gui.DND_FILES is None
assert unified_gui.TkinterDnD is None
'''
        result = subprocess.run([sys.executable, '-I', '-c', script, str(APP)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verified_package_takes_precedence_over_earlier_sys_path(self):
        wheel = fake_wheel()
        digest = hashlib.sha256(wheel).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            target = base/'trusted'
            dnd_support.install_wheel(wheel, target, digest)
            hostile = base/'other'
            (hostile/'tkinterdnd2').mkdir(parents=True)
            sentinel = base/'ran.txt'
            (hostile/'tkinterdnd2'/'__init__.py').write_text(
                f'open({str(sentinel)!r}, "w").write("executed")', encoding='utf-8')
            old_path = sys.path[:]
            try:
                sys.path[:0] = [str(hostile), str(target)]
                self.assertTrue(dnd_support.available(target, digest))
                self.assertEqual(sys.path[0], str(target))
                self.assertFalse(sentinel.exists())
            finally:
                sys.path[:] = old_path

    def test_reject_zip_traversal(self):
        wheel=fake_wheel(['tkinterdnd2/../../danger.txt'])
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'gui_dependencies'
            with self.assertRaisesRegex(ValueError,'Unsafe'):
                dnd_support.install_wheel(wheel,target,sha256=hashlib.sha256(wheel).hexdigest())
            self.assertFalse((Path(d)/'danger.txt').exists())

    def test_manual_wheel_path_when_network_unavailable(self):
        wheel=fake_wheel()
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            path=base/'tkinterdnd2-0.6.4-py3-none-any.whl'
            path.write_bytes(wheel)
            target=base/'gui_dependencies'
            with patch.object(dnd_support,'LOCAL_WHEEL',path), \
                 patch.object(dnd_support,'fetch_wheel',side_effect=AssertionError('no networking')):
                self.assertTrue(dnd_support.ensure_drag_drop(io.StringIO(),target,sha256=hashlib.sha256(wheel).hexdigest()))
            if str(target) in sys.path: sys.path.remove(str(target))

    def test_no_network_fallback_does_not_crash_gui(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'gui_dependencies'
            log=io.StringIO()
            with patch.object(dnd_support,'fetch_wheel',side_effect=OSError('offline')):
                self.assertFalse(dnd_support.ensure_drag_drop(log,target))
            self.assertIn('offline',log.getvalue())

    def test_batch_scripts_no_pip_and_no_broken_ampersand(self):
        helper=(APP/'tools'/'ensure_gui_dependencies.bat').read_text(encoding='utf-8')
        repair=(APP/'tools'/'repair_dnd.bat').read_text(encoding='utf-8')
        self.assertNotIn('pip --isolated',helper)
        self.assertIn('dnd_support.py',helper)
        self.assertIn('goto failed',repair)
        self.assertIn('--repair',repair)
        self.assertNotIn('echo [GUI] D&D',helper)
        self.assertNotIn('echo [GUI] D&D',repair)

if __name__ == '__main__': unittest.main()
