"""Portable checks for native-build metadata and isolated entry behavior."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

CODE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_launcher_build', CODE/'tools/native_launcher.py')
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class NativeLauncherTests(unittest.TestCase):
    def test_project_resource_and_manifest_are_bounded_and_well_formed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'Source 日本語 & path'
            icon = Path(temporary)/'source.ico'; icon.write_bytes(b'icon')
            project = native.write_project(root,'2.1.2',icon,'v143')
            document = ET.fromstring(project.read_text(encoding='utf-8'))
            ns = {'m':'http://schemas.microsoft.com/developer/msbuild/2003'}
            self.assertEqual(document.find('.//m:PlatformToolset',ns).text,'v143')
            self.assertEqual(document.find('.//m:RuntimeLibrary',ns).text,'MultiThreaded')
            self.assertEqual(document.find('.//m:SubSystem',ns).text,'Windows')
            self.assertEqual(document.find('.//m:ControlFlowGuard',ns).text,'Guard')
            manifest = ET.fromstring((root/'native_launcher.manifest').read_text(encoding='utf-8'))
            request = manifest.find('.//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel')
            self.assertEqual(request.attrib,{'level':'asInvoker','uiAccess':'false'})
            self.assertIn('2,1,2,0',(root/'native_launcher_version.h').read_text())
            for version,toolset in [('main','v143'),('2.1.65536','v143'),('2.1.2','v143;evil')]:
                with self.subTest(version=version,toolset=toolset),self.assertRaises(ValueError):
                    native.write_project(root,version,icon,toolset)

    def test_native_pe_validation_rejects_console_architecture_and_packed_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)/'launcher.exe'
            data = bytearray(1024); data[:2]=b'MZ'; struct.pack_into('<I',data,60,128)
            data[128:132]=b'PE\0\0'; struct.pack_into('<H',data,132,0x8664)
            struct.pack_into('<H',data,152,0x20b)
            struct.pack_into('<HH',data,152+68,2,0x20|0x40|0x100|0x4000)
            target.write_bytes(data)
            self.assertFalse(native.verify_native_launcher(target)['self_extracting'])
            cases=[]
            console=bytearray(data); struct.pack_into('<H',console,152+68,3); cases.append(console)
            x86=bytearray(data); struct.pack_into('<H',x86,132,0x14c); cases.append(x86)
            no_cfg=bytearray(data); struct.pack_into('<H',no_cfg,152+70,0x160); cases.append(no_cfg)
            cases.append(data+b'MEI\x0c\x0b\x0a\x0b\x0e')
            cases.append(bytearray(b'not an executable'))
            for invalid in cases:
                target.write_bytes(invalid)
                with self.assertRaises(RuntimeError): native.verify_native_launcher(target)

    def test_isolated_entry_ignores_untrusted_modules_and_cleans_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); code=root/'App 日本語 & path'/'_internal'; code.mkdir(parents=True)
            hostile=root/'untrusted'; hostile.mkdir()
            for name in ('native_app_entry.py','portable_runtime.py'):
                shutil.copyfile(CODE/name,code/name)
            (code/'app_launcher.py').write_text(
                'import json,os,sys\ndef main():\n'
                ' print(json.dumps({"args":sys.argv[1:],"isolated":sys.flags.isolated,'
                '"bad":[k for k in os.environ if k in ("PYTHONPATH","PYTHONHOME","PIP_INDEX_URL","TCL_LIBRARY","_PYI_ARCHIVE_FILE")]}))\n'
                ' return 37\n',encoding='utf-8')
            for name in ('portable_runtime.py','app_launcher.py','sitecustomize.py'):
                (hostile/name).write_text('raise RuntimeError("external module executed")\n')
            env=dict(os.environ,PYTHONPATH=str(hostile),PYTHONHOME=str(hostile),
                PIP_INDEX_URL='http://untrusted.invalid',TCL_LIBRARY=str(hostile),_PYI_ARCHIVE_FILE='old.exe')
            args=['space 日本語 & file','','double"quote','trailing\\']
            process=subprocess.run([sys.executable,'-I','-X','utf8',str(code/'native_app_entry.py'),*args],
                cwd=hostile,env=env,capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(process.returncode,37,process.stderr)
            self.assertEqual(json.loads(process.stdout),{'args':args,'isolated':1,'bad':[]})


if __name__ == '__main__':
    unittest.main()
