# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from studio.core import Studio, Cancelled
from studio import game_fixes as fixes
from studio import private_native_adapter as adapter

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def tree(path):return {str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}
def exe_bytes():
    return b'MZ'+bytes(58)+struct.pack('<I',64)+b'PE\0\0'+bytes(64) if os.name=='nt' else b'\x7fELF'+bytes(128)

def build_output(worker,p,proof):
    attempt=Path(p['attempt']);exe=attempt/'build/player'/('MyGameRecomp.exe' if os.name=='nt' else 'MyGameRecomp')
    exe.parent.mkdir(parents=True);exe.write_bytes(exe_bytes())
    fixes._atomic(attempt/'private-native-adapter.json',proof)
    receipt=worker.home/'verification/native-runtime/fixture.json'
    fixes._atomic(receipt,{'input_sha256':proof['input_sha256'],'executable':str(exe.resolve()),
        'executable_sha256':digest(exe),'attempt':str(attempt.resolve()),'private':True,
        'diagnostic_env':{'SPU_INTERP_UNLIFTED':'1','SPU_INTERP_BUDGET':'1000000'}})
    p.update(native_exe=str(exe),native_runtime_receipt=str(receipt),status='compiled; untested')
    manifest={'status':'compiled; gameplay unverified','input_sha256':proof['input_sha256'],'private_native_adapter':proof}
    fixes._atomic(attempt/'build-report.json',manifest);worker.save();return manifest

class GameFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.home=self.root/'primary';self.home.mkdir();self.game=self.root/'game';self.game.mkdir()
        self.elf=self.game/'EBOOT.elf';self.elf.write_bytes(b'fixture decrypted ELF')
        self.input_hash=digest(self.elf);self.backend=self.root/'backend';self.backend.mkdir()
        self.source=self.home/'projects/title/attempts/completed';(self.source/'port/recompiled').mkdir(parents=True)
        (self.source/'loader').mkdir();(self.source/'port/CMakeLists.txt').write_text('fixture cmake')
        (self.source/'port/recompiled/ppu_recomp.h').write_text('fixture header')
        (self.source/'port/recompiled/game.cpp').write_text('fixture source')
        (self.source/'loader/game.functions.json').write_text('[]')
        (self.source/'build').mkdir();(self.source/'build/CMakeCache.txt').write_text('DO NOT COPY')
        self.old_exe=self.source/'old.exe';self.old_exe.write_bytes(exe_bytes())
        fixes._atomic(self.source/'build-report.json',{'status':'compiled; gameplay unverified','input_sha256':self.input_hash})
        self.p={'id':'title','name':'Fixture','source':str(self.game),'elf':str(self.elf),'attempt':str(self.source),
                'native_exe':str(self.old_exe),'status':'compiled; untested'}
        self.studio=object.__new__(Studio);self.studio.home=self.home;self.studio.state_path=self.home/'studio.json'
        self.studio.state={'projects':[self.p],'settings':{'backend':str(self.backend),'jobs':2},'version':'fixture'}
        self.studio.save();self.state_bytes=self.studio.state_path.read_bytes();self.source_before=tree(self.source)
        manifest=json.loads((adapter.PACKAGE_ROOT/'manifest.json').read_text());manifest['backend_contract']={}
        self.hash_patch=patch.object(adapter,'INPUT_SHA256',self.input_hash);self.hash_patch.start()
        self.verify_patch=patch.object(adapter,'_verified',return_value=json.dumps(manifest).encode());self.verify_patch.start()
        self.build_patch=patch.object(fixes._WorkerStudio,'build',self.fake_build);self.build_patch.start()
        self.build_calls=[]
    def tearDown(self):
        self.build_patch.stop();self.verify_patch.stop();self.hash_patch.stop();self.tmp.cleanup()
    def fake_build(self,p,emit=print,cancel=None,reuse=False):
        # Bound-method fixture is provided explicitly by run_with_build below.
        raise AssertionError('Use bound build fixture')
    def plan(self,candidate=None):return fixes.preview(self.studio,self.p,candidate)
    def run_job(self,plan=None,behavior=None,cancel=None):
        owner=self
        def fake(worker,p,emit=print,cancel=None,reuse=False):
            owner.build_calls.append((worker,p,reuse,cancel))
            assert worker.home!=owner.home and worker.state_path!=owner.studio.state_path
            assert reuse and not (Path(p['attempt'])/'build').exists()
            worker.save()
            if behavior:return behavior(worker,p,cancel)
            proof={'applied':True,'input_sha256':owner.input_hash,'manifest_sha256':adapter.MANIFEST_SHA256,'private':True,'adapter_id':adapter.ADAPTER_ID}
            return build_output(worker,p,proof)
        with patch.object(fixes._WorkerStudio,'build',fake):
            return fixes.run(self.studio,plan or self.plan(),cancel=cancel,emit=lambda _:None)
    def assert_originals(self):
        self.assertEqual(self.studio.state_path.read_bytes(),self.state_bytes)
        self.assertEqual(tree(self.source),self.source_before)

    def test_unknown_game_has_no_fix_or_effect(self):
        # A game whose last fix build already has the current runtime fixes has nothing to apply.
        with patch.object(adapter,'INPUT_SHA256','other'), patch.object(fixes,'_applied_fingerprints',lambda s:{self.p['id']:fixes.current_fix_fingerprint()}):
            plan=self.plan();self.assertFalse(plan['available']);result=self.run_job(plan)
        self.assertEqual(result['status'],'unavailable');self.assertFalse((self.home/'game-fixes').exists());self.assert_originals()

    def test_isolated_build_returns_ready_and_preserves_sources_state_and_data(self):
        saved=self.home/'projects/title/native-data/save.bin';saved.parent.mkdir(parents=True);saved.write_bytes(b'save')
        profile=self.home/'controller-profiles/default.ini';profile.parent.mkdir();profile.write_text('profile')
        result=self.run_job();self.assertEqual(result['status'],'ready',result)
        self.assertEqual(len(self.build_calls),1);self.assertFalse(result['registered']);self.assert_originals()
        self.assertEqual(saved.read_bytes(),b'save');self.assertEqual(profile.read_text(),'profile')
        attempt=Path(result['candidate']['attempt']);self.assertFalse((attempt/'build/CMakeCache.txt').exists())
        self.assertEqual(self.build_calls[0][0].backend_python(),self.studio.backend_python())

    def test_cancel_before_start_does_not_create_worker(self):
        cancel=threading.Event();cancel.set();result=self.run_job(cancel=cancel)
        self.assertEqual(result['status'],'cancelled');self.assertFalse(self.build_calls);self.assert_originals()

    def test_cancel_during_build_is_separate_and_preserves_candidate(self):
        cancel=threading.Event();queue_cancel=threading.Event()
        def stop(worker,p,event):event.set();raise Cancelled('own worker cancelled')
        result=self.run_job(behavior=stop,cancel=cancel);self.assertEqual(result['status'],'cancelled')
        self.assertFalse(queue_cancel.is_set());self.assert_originals()
        self.assertFalse((self.home/'game-fixes/title/.fix-worker.lock').exists())

    def test_existing_fix_owner_is_not_stopped_or_replaced(self):
        lock=self.home/'game-fixes/title/.fix-worker.lock';lock.parent.mkdir(parents=True);lock.write_text('existing owner')
        result=self.run_job();self.assertEqual(result['status'],'busy');self.assertEqual(lock.read_text(),'existing owner')
        self.assertFalse(self.build_calls);self.assert_originals()

    def test_failed_or_missing_executable_never_becomes_ready(self):
        result=self.run_job(behavior=lambda *args:{'status':'compiled; gameplay unverified'})
        self.assertEqual(result['status'],'failed');self.assertFalse(fixes.list_ready_candidates(self.studio,self.p));self.assert_originals()

    def test_busy_queue_registers_independent_candidate_without_promotion(self):
        result=self.run_job();self.p['status']='building';before=copy.deepcopy(self.p)
        promoted=fixes.promote(self.studio,result,queue_busy=True)
        self.assertFalse(promoted['promoted']);self.assertEqual(self.p,before);self.assert_originals()
        candidates=fixes.list_ready_candidates(self.studio,self.p);self.assertEqual(len(candidates),1)
        self.assertTrue(Path(candidates[0]['native_runtime_receipt']).is_relative_to(self.home/'verification'))

    def test_companion_never_changes_primary_state(self):
        result=self.run_job();self.studio._read_only_state=True
        outcome=fixes.promote(self.studio,result);self.assertFalse(outcome['promoted']);self.assert_originals()

    def test_idle_promotion_preserves_history_and_receipt_is_trusted(self):
        result=self.run_job();outcome=fixes.promote(self.studio,result)
        self.assertTrue(outcome['promoted']);self.assertEqual(self.p['native_exe'],result['candidate']['native_exe'])
        self.assertTrue(any(x['native_exe']==str(self.old_exe) for x in self.p['native_candidate_history']))
        self.assertTrue(Path(self.p['native_runtime_receipt']).is_relative_to(self.home/'verification'))
        self.assertEqual(tree(self.source),self.source_before)

    def test_newer_selection_and_changed_input_refuse_promotion(self):
        result=self.run_job();self.p['native_exe']='newer candidate'
        outcome=fixes.promote(self.studio,result);self.assertFalse(outcome['promoted']);self.assertEqual(self.p['native_exe'],'newer candidate')
        self.p['native_exe']=str(self.old_exe);self.elf.write_bytes(b'changed input')
        outcome=fixes.promote(self.studio,result);self.assertFalse(outcome['promoted']);self.assertEqual(self.p['native_exe'],str(self.old_exe))

    def test_tampered_executable_is_never_registered_or_promoted(self):
        result=self.run_job();Path(result['candidate']['native_exe']).write_bytes(b'tampered')
        with self.assertRaises(fixes.GameFixError):fixes.promote(self.studio,result)
        self.assertFalse(fixes.list_ready_candidates(self.studio,self.p));self.assert_originals()

    def test_active_source_attempt_is_never_copied(self):
        self.p['status']='building';self.assertFalse(self.plan({'attempt':str(self.source),'completed':False})['available'])
        active=self.home/'projects/title/attempts/active';active.mkdir();fixes._atomic(active/'build-report.json',{'status':'building','input_sha256':self.input_hash})
        self.assertFalse(self.plan({'attempt':str(active),'completed':True})['available']);self.assert_originals()

    def test_source_link_is_rejected_before_build(self):
        link=self.source/'port/link'
        try:link.symlink_to(self.source/'loader',target_is_directory=True)
        except OSError:self.skipTest('symlink creation unavailable')
        result=self.run_job();self.assertEqual(result['status'],'failed');self.assertFalse(self.build_calls)


    def test_wrong_runtime_receipt_never_becomes_ready(self):
        def wrong(worker,p,event):
            proof={'applied':True,'input_sha256':self.input_hash,'manifest_sha256':adapter.MANIFEST_SHA256,'private':True,'adapter_id':adapter.ADAPTER_ID}
            result=build_output(worker,p,proof);receipt=Path(p['native_runtime_receipt']);data=json.loads(receipt.read_text())
            data['diagnostic_env']={};fixes._atomic(receipt,data);return result
        result=self.run_job(behavior=wrong);self.assertEqual(result['status'],'failed');self.assert_originals()

    @unittest.skipUnless(Path(os.environ.get('RR_TEST_MOTORSTORM_ELF','__missing__')).is_file(),
                         'private actual owner ELF unavailable')
    def test_real_adapter_stages_exact_policy_into_temp_worker_only(self):
        # Real original input/package/contract validation; compilation is mocked.
        self.hash_patch.stop();self.verify_patch.stop()
        root=Path(__file__).resolve().parents[1]
        elf=Path(os.environ.get('RR_TEST_MOTORSTORM_ELF','__missing__'))
        backend=root/'workspace/tools/ps3recomp';self.p.update(source=str(elf.parent),elf=str(elf))
        self.studio.state['settings']['backend']=str(backend)
        fixes._atomic(self.source/'build-report.json',{'status':'compiled; gameplay unverified','input_sha256':adapter.INPUT_SHA256})
        (self.source/'port/CMakeLists.txt').write_bytes((backend/'templates/project/CMakeLists.txt').read_bytes())
        self.source_before=tree(self.source)
        plan=self.plan();self.assertTrue(plan['available'],plan)
        def real_stage(worker,p,event):
            attempt=Path(p['attempt']);proof=adapter.apply(attempt/'port',elf,backend,p)
            return build_output(worker,p,proof)
        with patch('studio.core.subprocess.Popen',side_effect=AssertionError('No process launch allowed in this test')):
            result=self.run_job(plan,behavior=real_stage)
        self.assertEqual(result['status'],'ready',result)
        port=Path(result['candidate']['attempt'])/'port';proof=result['adapter_receipt']
        self.assertEqual(proof['raw_fingerprint'],'093FC7EE6C9F7E84')
        self.assertEqual(digest(port/'spu/motorstorm_wws_policy/code.bin'),adapter.RAW_SHA256)
        self.assertEqual((port/'CMakeLists.txt').read_text().count(adapter.CMAKE_LINE),1)
        self.assertFalse(list(port.rglob('*multistream_register*')))
        self.assert_originals()


    def test_other_game_queue_ownership_also_blocks_selected_promotion(self):
        result=self.run_job();self.studio.state['projects'].append({'id':'other','status':'building'})
        outcome=fixes.promote(self.studio,result,queue_busy=False)
        self.assertFalse(outcome['promoted']);self.assertEqual(self.p['native_exe'],str(self.old_exe))
        self.assertEqual(self.studio.state_path.read_bytes(),self.state_bytes)

    def test_input_changed_during_compile_retains_old_candidate(self):
        def changed(worker,p,event):
            proof={'applied':True,'input_sha256':self.input_hash,'manifest_sha256':adapter.MANIFEST_SHA256,'private':True,'adapter_id':adapter.ADAPTER_ID}
            result=build_output(worker,p,proof);self.elf.write_bytes(b'replaced during compile');return result
        result=self.run_job(behavior=changed);self.assertEqual(result['status'],'failed');self.assert_originals()

    def test_cli_preview_never_constructs_or_saves_primary_studio(self):
        import contextlib,io
        with patch.object(Studio,'__init__',side_effect=AssertionError('must not initialize primary Studio')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fixes.main(['--workspace',str(self.home),'--project','title']),0)
        self.assert_originals();self.assertFalse(self.build_calls)

if __name__=='__main__':unittest.main()
