# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import copy
import json
import queue
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import studio
from studio import game_tools
import test_gui_independent_workers as gui_fixture
from test_gui_independent_workers import Var

class CompanionTests(TestCase):
    def test_refresh_reads_without_rewriting_or_recovering_active_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'studio.json'
            state=dict(projects=[dict(id='p',name='MotorStorm',status='building',attempt='live')],settings={})
            path.write_text(json.dumps(state),encoding='utf-8')
            before=path.read_bytes()
            instance=SimpleNamespace(state_path=path,state={})
            result=game_tools.refresh_snapshot(instance)
            self.assertEqual(result[0]['status'],'building')
            self.assertEqual(result[0]['attempt'],'live')
            self.assertEqual(path.read_bytes(),before)
            result[0]['name']='Detached local snapshot'
            self.assertEqual(json.loads(path.read_text())['projects'][0]['name'],'MotorStorm')

    def test_companion_initializes_without_normal_studio_startup_or_queue_writes(self):
        gui_fixture.IndependentGuiTests.make_app(self) # installs fully mocked Tk/widget services
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'studio.json'
            state=dict(projects=[dict(id='p',name='MotorStorm',status='building')],settings={})
            path.write_text(json.dumps(state),encoding='utf-8')
            before=path.read_bytes()
            instance=SimpleNamespace(home=Path(temp),state_path=path,state=copy.deepcopy(state),list_test_sessions=lambda:[])
            with patch.object(game_tools.Studio,'read_only_snapshot',return_value=instance) as factory:
                app=game_tools.GameTools(temp)
            factory.assert_called_once_with(temp)
            self.assertEqual(path.read_bytes(),before)
            self.assertIn('MotorStorm',app.game_var.get())
            for name in ('manual_status','fix_status','test_session_var','game_var','overview'):
                self.assertIsInstance(getattr(app,name),Var)
            self.assertFalse(hasattr(instance,'save'))
            self.assertNotIn('promote',game_tools.GameTools.poll.__code__.co_names)

    def test_ready_fix_registers_sidecar_without_promotion_or_shared_queue_save(self):
        calls=[];module=types.ModuleType('studio.game_fixes')
        module.register_test_candidate=lambda instance,result:calls.append(result)
        module.promote=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('No companion promotion'))
        result=dict(status='ready',candidate={'attempt':'isolated'})
        events=queue.Queue();events.put(('fix_ready',result))
        instance=SimpleNamespace(events=events,fix_busy=True,fix_status=Var(),studio=SimpleNamespace(),after=lambda *args:None,poll=lambda:None)
        with patch.dict(sys.modules,{'studio.game_fixes':module}),patch.object(studio,'game_fixes',module,create=True):
            game_tools.GameTools.poll(instance)
        self.assertEqual(calls,[result])
        self.assertFalse(instance.fix_busy)
        self.assertIn('queue is unchanged',instance.fix_status.get())
