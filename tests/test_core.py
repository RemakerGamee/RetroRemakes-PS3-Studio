# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json, os, struct, sys, tempfile, threading, time, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from studio.core import Studio, Runner, Cancelled, inspect_binary, parse_sfo, sha256, write_json


def make_elf(path, machine=21, opd=True):
    b = bytearray(0x3100)
    b[:16] = b'\x7fELF\x02\x02\x01' + bytes(9)
    struct.pack_into('>HHIQQQIHHHHHH', b, 16, 2, machine, 1, 0x20000, 64, 0x3000, 0, 64, 56, 2, 64, 4, 3)
    struct.pack_into('>IIQQQQQQ', b, 64, 1, 5, 0x1000, 0x10000, 0x10000, 8, 8, 0x1000)
    struct.pack_into('>IIQQQQQQ', b, 120, 1, 6, 0x2000, 0x20000, 0x20000, 8, 8, 0x1000)
    b[0x1000:0x1008] = bytes.fromhex('386000004e800020')
    b[0x2000:0x2008] = struct.pack('>II', 0x10000, 0x28000)
    names = b'\0.text\0.opd\0.shstrtab\0'
    b[0x2800:0x2800+len(names)] = names
    for off, vals in [(0x3040,(1,1,6,0x10000,0x1000,8,0,0,4,0)),(0x3080,(7,1,3,0x20000 if opd else 0x21000,0x2000,8,0,0,4,0)),(0x30c0,(12,3,0,0,0x2800,len(names),0,0,1,0))]:
        struct.pack_into('>IIQQQQIIQQ',b,off,*vals)
    Path(path).write_bytes(b)
    return Path(path)

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.game=self.base/'unknown game [not on any list]';self.game.mkdir()
        self.elf=make_elf(self.game/'EBOOT.ELF');self.studio=Studio(self.base/'workspace')
    def tearDown(self): self.tmp.cleanup()
    def test_unknown_title_can_be_queued_and_analysed(self):
        p=self.studio.add(self.game);r=self.studio.analyse(p,lambda x:None)
        self.assertTrue(r['binary']['native_input']);self.assertEqual(r['sha256'],sha256(self.elf))
        self.assertEqual(p['status'],'ready for attempt')
        self.assertEqual(Studio(self.studio.home).state['projects'][0]['id'],p['id'])
    def test_self_not_misreported_as_elf(self):
        self.elf.write_bytes(b'SCE\0'+bytes(64));p=self.studio.add(self.game)
        self.assertFalse(self.studio.analyse(p,lambda x:None)['binary']['native_input'])
        self.assertEqual(p['status'],'needs decrypted ELF')
    def test_truncated_elf_rejected(self):
        self.elf.write_bytes(b'\x7fELF\x02\x02\x01')
        with self.assertRaises(ValueError):inspect_binary(self.elf)
    def test_invalid_segment_bounds(self):
        b=bytearray(self.elf.read_bytes());struct.pack_into('>Q',b,72,0x99999);self.elf.write_bytes(b)
        with self.assertRaisesRegex(ValueError,'bounds'):inspect_binary(self.elf)
    def test_other_architecture_rejected(self):
        make_elf(self.elf,machine=62);self.assertFalse(inspect_binary(self.elf)['native_input'])
    def test_sfo_metadata_and_invalid_bounds(self):
        path=self.game/'PARAM.SFO';value=b'Unknown PS3 Title\0';key=b'TITLE\0';b=bytearray(36+len(key)+len(value))
        struct.pack_into('<4sIIII',b,0,b'\0PSF',0x101,36,36+len(key),1)
        struct.pack_into('<HHIII',b,20,0,0x204,len(value),len(value),0)
        b[36:36+len(key)]=key;b[36+len(key):]=value;path.write_bytes(b)
        self.assertEqual(parse_sfo(path)['TITLE'],'Unknown PS3 Title')
        struct.pack_into('<I',b,24,999999);path.write_bytes(b)
        with self.assertRaises(ValueError):parse_sfo(path)
    def test_input_workspace_overlap_rejected(self):
        with self.assertRaisesRegex(ValueError,'outside'):Studio(self.game/'workspace').add(self.game)
    def test_failed_process_records_exit_and_output(self):
        r=Runner(self.base/'logs',lambda x:None)
        with self.assertRaisesRegex(RuntimeError,'exit 7'):r.run('fail',[sys.executable,'-c','print("missing symbol");raise SystemExit(7)'])
        self.assertEqual(json.loads((self.base/'logs/commands.json').read_text())[0]['exit_code'],7)
        self.assertIn('missing symbol',(self.base/'logs/01-fail.log').read_text())
    def test_arguments_not_interpreted_by_shell(self):
        text='spaces & $(echo no-shell) `echo no-shell`'
        out=Runner(self.base/'logs',lambda x:None).run('literal',[sys.executable,'-c','import sys;print(sys.argv[1])',text])
        self.assertEqual(out.strip(),text)
    def test_cancellation_terminates_process(self):
        ev=threading.Event();r=Runner(self.base/'logs',lambda x:None,ev);t=threading.Timer(.3,ev.set);t.start();start=time.monotonic()
        with self.assertRaises(Cancelled):r.run('cancel',[sys.executable,'-c','import time;time.sleep(60)'])
        t.join();self.assertLess(time.monotonic()-start,6);self.assertEqual(r.records[0]['status'],'cancelled')
    def test_export_excludes_assets_and_code(self):
        p=self.studio.add(self.game);self.studio.analyse(p,lambda x:None);a=self.studio.project_dir(p)/'attempts/one';p['attempt']=str(a)
        write_json(a/'build-report.json',{'status':'blocked'});(a/'port').mkdir();(a/'port/game.cpp').write_text('private code')
        dest=self.base/'diagnostics.zip';self.studio.export(p,dest)
        with zipfile.ZipFile(dest) as z:
            self.assertIn('analysis.json',z.namelist());self.assertFalse(any(n.endswith(('.ELF','.cpp')) for n in z.namelist()))
    def test_rebuild_keeps_manual_fixes(self):
        p=self.studio.add(self.game);a=self.studio.project_dir(p)/'attempts/edited';p['attempt']=str(a)
        (a/'port/recompiled').mkdir(parents=True);(a/'loader').mkdir();marker=a/'port/recompiled/ppu_recomp.h';marker.write_text('// manual fix')
        (a/'port/recompiled/ppu_recomp_0.cpp').write_text('// custom fix')
        write_json(a/'loader/EBOOT.functions.json',[{'start':'0x10000','end':'0x10008'}]);write_json(a/'loader/EBOOT.imports.json',[])
        write_json(a/'build-report.json',{'input_sha256':sha256(self.elf)});backend=self.base/'backend';backend.mkdir()
        self.studio.state['settings']['backend']=str(backend);calls=[]
        def run(r,label,argv,**kw):calls.append(label);raise RuntimeError('intentional configure failure')
        with patch.object(self.studio,'validate_backend'),patch.object(self.studio,'provenance',return_value={}),patch('studio.core.shutil.which',return_value='/tool'),patch.object(Runner,'run',run):
            with self.assertRaisesRegex(RuntimeError,'intentional configure'):self.studio.build(p,lambda x:None,reuse=True)
        self.assertEqual(calls,['cmake-configure']);self.assertEqual(marker.read_text(),'// manual fix');self.assertTrue(list((a/'history').glob('*.json')))
    def test_native_test_records_failure_output(self):
        if os.name=='nt':self.skipTest('POSIX executable fixture')
        p=self.studio.add(self.game);a=self.studio.project_dir(p)/'attempts/native';exe=a/'build/MyGameRecomp';exe.parent.mkdir(parents=True)
        exe.write_text('#!/bin/sh\necho native-test-output\nexit 3\n');exe.chmod(0o755);p['native_exe']=str(exe);p['attempt']=str(a)
        write_json(a/'build-report.json',{'status':'compiled; gameplay unverified','input_sha256':sha256(self.elf)});self.studio.launch(p);rp=self.studio.project_dir(p)/'native-test.json'
        for _ in range(50):
            report=json.loads(rp.read_text())
            if report['status']!='running':break
            time.sleep(.02)
        self.assertEqual(report['exit_code'],3);self.assertEqual(report['playability'],'unverified')
        self.assertIn('native-test-output',self.studio.test_log_path(p).read_text())

if __name__=='__main__':unittest.main()
