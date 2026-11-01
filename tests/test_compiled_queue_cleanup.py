# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import sys,threading,queue,copy,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from studio import compiled_queue_cleanup as fix

class CleanupTests(unittest.TestCase):
    def test_valid_complete_only_active_and_removed_skipped(self):
        projects=[dict(id=x,status='building' if x=='active' else 'compiled',queue_removed=x=='hidden') for x in ['good','invalid','partial','active','hidden','error']]
        def candidates(p):
            if p['id']=='error':raise OSError('unreadable')
            return [dict(valid=p['id']!='invalid',completed=p['id']!='partial')]
        reader=SimpleNamespace(list_test_candidates=Mock(side_effect=candidates))
        ids,errors=fix.collect_completed(reader,projects)
        self.assertEqual(ids,['good']);self.assertEqual(errors,[('error','unreadable')]);self.assertEqual(reader.list_test_candidates.call_count,5)

    def test_already_removed_compiled_input_removes_alias_but_not_same_name(self):
        projects=[dict(id='built',source='same.elf',queue_removed=True),dict(id='alias',name='EBOOT',source='same.elf'),dict(id='other',name='EBOOT',source='different.elf')]
        reader=SimpleNamespace(list_test_candidates=lambda p:[dict(valid=True,completed=True)] if p['id']=='built' else [])
        with patch.object(fix,'choose_elf',side_effect=lambda source,elf:Path(source)):
            ids,errors=fix.collect_completed(reader,projects)
        self.assertEqual(ids,['alias']);self.assertEqual(errors,[])

    def test_commit_current_state_preserves_new_fields_and_builds(self):
        done=dict(id='done',native_exe='kept.exe',status='compiled',new_field='concurrent change')
        active=dict(id='active',status='building');other=dict(id='other',status='queued')
        studio=SimpleNamespace(state={'projects':[done,active,other]},state_lock=threading.RLock(),save=Mock())
        self.assertEqual(fix.apply_removals(studio,['done','active','deleted']),1)
        self.assertTrue(done['queue_removed']);self.assertEqual(done['native_exe'],'kept.exe');self.assertEqual(done['new_field'],'concurrent change')
        self.assertNotIn('queue_removed',active);self.assertNotIn('queue_removed',other);studio.save.assert_called_once()

    def test_save_failure_rolls_back_only_cleanup_flags(self):
        projects=[dict(id='a'),dict(id='b',queue_removed=False)]
        studio=SimpleNamespace(state={'projects':projects},save=Mock(side_effect=OSError('locked')))
        before=copy.deepcopy(projects)
        with self.assertRaises(OSError):fix.apply_removals(studio,['a','b'])
        self.assertEqual(projects,before)

    def test_live_build_not_blocked_and_ui_only_on_owner_thread(self):
        owner=threading.get_ident();entered=threading.Event();release=threading.Event();scheduled=[]
        projects=[dict(id='done',status='compiled',native_exe='retained'),dict(id='active',status='building')]
        studio=SimpleNamespace(home=Path('unused'),state={'projects':projects,'settings':{}},save=Mock(),state_lock=threading.RLock())
        def check(p):
            self.assertNotEqual(owner,threading.get_ident());entered.set();release.wait(3)
            return [dict(valid=True,completed=True)]
        reader=SimpleNamespace(state={},list_test_candidates=check)
        def ui(value):self.assertEqual(owner,threading.get_ident())
        app=SimpleNamespace(studio=studio,busy=True,compiled_status=SimpleNamespace(set=ui),status=SimpleNamespace(set=ui),refresh=Mock(),events=queue.Queue(),after=lambda delay,fn:scheduled.append(fn),cancel=threading.Event(),worker=object())
        owned=app.worker
        with patch.object(fix.Studio,'read_only_snapshot',return_value=reader):
            start=time.monotonic();fix.remove_compiled_async(app);self.assertLess(time.monotonic()-start,.5)
            self.assertTrue(entered.wait(2));studio.save.assert_not_called()
            fix.remove_compiled_async(app) # Double click doesn't start another worker.
            release.set();app._compiled_queue_cleanup_worker.join(3)
            self.assertFalse(app._compiled_queue_cleanup_worker.is_alive())
            for fn in list(scheduled):fn()
        self.assertTrue(projects[0]['queue_removed']);self.assertNotIn('queue_removed',projects[1]);self.assertTrue(app.busy);self.assertIs(app.worker,owned);self.assertFalse(app.cancel.is_set());self.assertFalse(app._compiled_queue_cleanup_busy)
        app.refresh.assert_called_once();studio.save.assert_called_once()

if __name__=='__main__':unittest.main()
