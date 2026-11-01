# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from studio.core import Studio, BACKEND_REV, sha256
from studio import fulton

class FultonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.studio=Studio(self.root/'workspace')
    def tearDown(self):self.tmp.cleanup()
    def test_console_filter_and_credential_exclusion(self):
        result=fulton.normalize({'userGames':[dict(title='Game',system='ps3',repo='Owner/Game',token='secret'),dict(title='Other',system='ps2',repo='Owner/Other')]},'local')
        self.assertEqual(len(result),1);self.assertNotIn('token',result[0])
    def test_offline_local_library_and_invalid_repo(self):
        path=self.root/'community.json';path.write_text(json.dumps({'items':[dict(name='Game',system='ps3',repo='Owner/Game'),dict(name='Bad',system='ps3',repo='../bad')]}))
        before=path.read_bytes()
        with patch.object(fulton,'local_paths',return_value=[path]),patch.object(fulton,'urlopen',side_effect=OSError('offline')):
            entries=fulton.sync(self.studio,True,lambda x:None)
        self.assertEqual([e['repo'] for e in entries],['Owner/Game']);self.assertEqual(path.read_bytes(),before)
    def test_missing_firmware_and_reload(self):
        folder=self.root/'firmware';folder.mkdir();p=folder/'PS3UPDAT.PUP';p.write_bytes(b'SCEUF\x00\x00\x00'+b'test')
        self.assertEqual(self.studio.detect_firmware(folder),str(p));self.studio.save()
        self.assertEqual(Studio(self.studio.home).state['settings']['firmware'],str(p))
        p.unlink();self.assertEqual(self.studio.detect_firmware(),'');self.assertNotIn('firmware',self.studio.state['settings'])
    def test_exact_metadata_and_hash_rejection(self):
        source=self.root/'source';source.mkdir();loader=self.root/'loader';loader.mkdir()
        for name in ('functions','imports'):(source/(name+'.json')).write_text('[{"entry":1}]')
        manifest=dict(schema=1,console='ps3',boot_sha256='a'*64,backend_revision=BACKEND_REV)
        for name in ('functions','imports'):manifest[name]=dict(path=name+'.json',sha256=sha256(source/(name+'.json')))
        (source/'rr-ps3-base.json').write_text(json.dumps(manifest))
        plan=dict(sources=[dict(repo='Owner/Game',commit='b'*40,path=str(source))],warnings=[],applied=[])
        with patch.object(fulton.subprocess,'check_output',return_value=BACKEND_REV):
            fulton.apply_loader_inputs(plan,{'sha256':'a'*64},self.root,loader,Path('EBOOT.ELF'))
        self.assertEqual(len(plan['applied']),1);before=(loader/'EBOOT.functions.json').read_bytes()
        (source/'functions.json').write_text('[]');plan['applied']=[]
        with patch.object(fulton.subprocess,'check_output',return_value=BACKEND_REV):
            fulton.apply_loader_inputs(plan,{'sha256':'a'*64},self.root,loader,Path('EBOOT.ELF'))
        self.assertEqual(plan['applied'],[]);self.assertEqual((loader/'EBOOT.functions.json').read_bytes(),before)
    def test_unsafe_adapter_path(self):
        for path in ('../escape.json','C:/escape.json','a\\b.json'):
            with self.assertRaises(ValueError):fulton.safe_file(self.root,path)
    def test_pull_continues_after_failure(self):
        entries=[dict(repo='Owner/Bad'),dict(repo='Owner/Good')]
        with patch.object(fulton,'pull',side_effect=[RuntimeError('failed'),dict(repo='Owner/Good',commit='b'*40)]):
            results=fulton.pull_many(self.studio,entries,lambda x:None)
        self.assertEqual(len(results),2);self.assertIn('error',results[0]);self.assertIn('commit',results[1])

    def test_prepare_entire_library_without_a_game_selection(self):
        entries=[dict(name='First',repo='Owner/First'),dict(name='Second',repo='Owner/Second')]
        output=[dict(repo='Owner/First',path='cache'),dict(repo='Owner/Second',error='not found')]
        with patch.object(fulton,'sync',return_value=entries),patch.object(fulton,'pull_many',return_value=output) as pull:
            report=fulton.prepare_library(self.studio,emit=lambda x:None)
        self.assertEqual(pull.call_args.args[1],entries)
        self.assertEqual((report['projects'],report['available'],report['unavailable']),(2,1,1))
        self.assertTrue((fulton.folder(self.studio)/'library-readiness.json').exists())

    def test_preserves_library_requirements_without_importing_settings(self):
        entry=dict(title='Game',system='ps3',repo='Owner/Game',rom={'note':'Use your US disc'},settings={'token':'secret'})
        result=fulton.normalize([entry],'local')[0]
        self.assertEqual(result['requirements'],'Use your US disc');self.assertNotIn('settings',result)

    def test_source_cancel_does_not_touch_build_cancel(self):
        import threading
        source_cancel=threading.Event();build_cancel=threading.Event();source_cancel.set()
        with self.assertRaises(Exception) as caught:
            fulton.pull_many(self.studio,[dict(repo='Owner/Game')],cancel=source_cancel)
        self.assertIsInstance(caught.exception,fulton.Cancelled)
        self.assertFalse(build_cancel.is_set())

    def test_cross_process_source_lock_can_be_reused(self):
        with fulton.source_lock(self.studio):pass
        with fulton.source_lock(self.studio):pass
