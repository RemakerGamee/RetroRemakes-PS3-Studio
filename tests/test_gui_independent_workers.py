# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import queue
import threading
from studio import gui

class Var:
    def __init__(self,*args,value='',**kwargs):self.value=value
    def set(self,value):self.value=value
    def get(self):return self.value

class ImmediateThread:
    def __init__(self,target,**kwargs):self.target=target
    def start(self):self.target()
    def is_alive(self):return False

def widget(*args,**kwargs):
    value=MagicMock()
    value.selection.return_value=()
    value.get_children.return_value=()
    value.index.return_value='1.0'
    return value

class IndependentGuiTests(TestCase):
    def make_app(self,projects=None):
        stack=ExitStack();self.addCleanup(stack.close)
        def fake_tk_init(app):
            app.tk=MagicMock();app._w='.';app.children={};app._tclCommands=[]
        stack.enter_context(patch.object(gui.tk.Tk,'__init__',fake_tk_init))
        for name in ('title','geometry','minsize','configure','protocol','bind','after'):
            stack.enter_context(patch.object(gui.tk.Tk,name,MagicMock()))
        stack.enter_context(patch.object(gui.tk.Tk,'winfo_screenwidth',return_value=1920))
        stack.enter_context(patch.object(gui.tk.Tk,'winfo_screenheight',return_value=1080))
        stack.enter_context(patch.object(gui.tk,'StringVar',Var))
        stack.enter_context(patch.object(gui.tk,'Text',side_effect=widget))
        for name in ('Style','Frame','Label','Notebook','Button','Treeview','Scrollbar','Progressbar','Combobox'):
            stack.enter_context(patch.object(gui.ttk,name,side_effect=widget))
        for name in ('tools_tab','settings_tab','updates_tab','research_tab','fulton_tab'):
            stack.enter_context(patch.object(gui.App,name,lambda app:None))
        stack.enter_context(patch('studio.controllers.ControllerPanel',side_effect=widget))
        stack.enter_context(patch('studio.cartridge_bridge.ui.CartridgePanel',side_effect=widget))
        studio=SimpleNamespace(home=Path('unused-fixture-home'),
                               state={'projects':projects or [],'settings':{'check_updates_on_start':False}},
                               list_test_sessions=lambda:[], list_test_candidates=lambda p:[])
        stack.enter_context(patch.object(gui,'Studio',return_value=studio))
        return gui.App()

    def test_recursive_elf_import_button_is_created_and_bound(self):
        buttons=[]
        original=gui.App.button
        def capture(app,parent,text,command,accent=False):
            buttons.append((text,command))
            return original(app,parent,text,command,accent)
        with patch.object(gui.App,'button',capture):
            app=self.make_app()
        matches=[command for text,command in buttons if text=='Batch import ELF files']
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0].__self__,app)
        self.assertEqual(matches[0].__func__,gui.App.batch_import_elfs)

    def test_compiled_refresh_button_is_created_bound_and_independent_of_busy_queue(self):
        buttons=[]
        original=gui.App.button
        def capture(app,parent,text,command,accent=False):
            buttons.append((text,command))
            return original(app,parent,text,command,accent)
        with patch.object(gui.App,'button',capture):
            app=self.make_app()
        callback=[command for text,command in buttons if text=='Refresh built games']
        self.assertEqual(len(callback),1)
        self.assertEqual(callback[0].__func__,gui.App.refresh_compiled_async)
        app.busy=True;app.compiled_scan_busy=False
        app.current_queue_item.set('Building another game')
        project=dict(id='p',name='Finished game',status='building',attempt='active-attempt')
        app.studio.state['projects']=[project]
        app.studio.save=MagicMock()
        app.studio.list_test_candidates=MagicMock(return_value=[dict(id='candidate')])
        app.compiled_tree.selection.return_value=('p',)
        app.compiled_tree.exists.return_value=True
        with patch.object(gui.threading,'Thread') as worker:
            callback[0]()
            app.studio.list_test_candidates.assert_not_called()
            self.assertIn('background',app.compiled_status.get())
            worker.call_args.kwargs['target']()
        app.poll()
        self.assertIn('1 game(s), 1 finished build(s)',app.compiled_status.get())
        app.compiled_tree.selection_set.assert_called_with(['p'])
        app.studio.save.assert_not_called()
        self.assertTrue(app.busy)
        self.assertFalse(app.cancel.is_set())
        self.assertEqual(app.current_queue_item.get(),'Building another game')
        self.assertEqual(project['attempt'],'active-attempt')

    def test_compiled_results_appear_before_later_game_finishes_and_scan_remains_busy(self):
        from studio import compiled_library
        import copy
        app=self.make_app();app.busy=True;app.compiled_scan_busy=False
        app.current_queue_item.set('Building another game')
        app.studio.state['projects']=[dict(id='first',name='Finished game'),dict(id='later',name='Later input')]
        before=copy.deepcopy(app.studio.state);app.studio.save=MagicMock()
        tree=app.compiled_tree
        visible={'old':('Previous result', '1 finished build(s)', 'In queue')}
        tree.get_children.side_effect=lambda:tuple(visible)
        tree.exists.side_effect=lambda ident:ident in visible
        tree.selection.return_value=('old',)
        def insert(parent,where,iid,values):visible[iid]=values
        def delete(*ids):
            for ident in ids:visible.pop(ident,None)
        def item(ident,**kwargs):visible[ident]=kwargs['values']
        tree.insert.side_effect=insert;tree.delete.side_effect=delete;tree.item.side_effect=item
        def candidates(project):
            if project['id']=='first':return [dict(id='finished')]
            # This executes while the second validation is still outstanding.
            app.compiled_scan_pending=True
            app.refresh_compiled_async=MagicMock()
            app.poll()
            self.assertIn('first',visible)
            self.assertIn('old',visible) # prior results survive interim updates
            self.assertTrue(app.compiled_scan_busy)
            app.refresh_compiled_async.assert_not_called()
            self.assertIn('Checking game 2/2: Later input',app.compiled_status.get())
            self.assertTrue(app.busy);self.assertFalse(app.cancel.is_set())
            return []
        app.studio.list_test_candidates=candidates
        with patch.object(compiled_library,'hidden_project_ids',return_value=set()), patch.object(compiled_library,'scan_outputs',return_value={}), patch.object(gui.threading,'Thread') as worker:
            app.refresh_compiled_async()
            worker.call_args.kwargs['target']()
            app.poll()
        self.assertEqual(set(visible),{'first'})
        self.assertFalse(app.compiled_scan_busy)
        app.refresh_compiled_async.assert_called_once()
        self.assertEqual(app.studio.state,before);app.studio.save.assert_not_called()
        self.assertEqual(app.current_queue_item.get(),'Building another game')

    def test_partial_compiled_results_respect_new_hidden_rows_without_finishing_scan(self):
        from studio import compiled_library
        app=self.make_app();app.compiled_scan_busy=True;app.compiled_scan_pending=True
        app.refresh_compiled_async=MagicMock()
        app.compiled_tree.get_children.return_value=('hide','old')
        app.compiled_tree.exists.return_value=False
        app.events.put(('compiled_rows_progress',[('hide','Hidden',1,False),('new','New finished',1,False)]))
        with patch.object(compiled_library,'hidden_project_ids',return_value={'hide'}):app.poll()
        app.compiled_tree.delete.assert_called_once_with('hide')
        self.assertEqual([c.kwargs['iid'] for c in app.compiled_tree.insert.call_args_list],['new'])
        self.assertTrue(app.compiled_scan_busy)
        app.refresh_compiled_async.assert_not_called()

    def test_startup_restores_saved_compiled_rows_before_background_refresh(self):
        from studio import compiled_library
        project=dict(id='saved',name='Saved game',status='compiled; untested')
        rows=[('saved','Saved game',2,True),('removed-project','Stale row',1,False)]
        with patch.object(compiled_library,'read_cached_rows',return_value=rows) as load, patch.object(gui.App,'_update_compiled_rows',autospec=True) as display, patch.object(gui.App,'refresh_compiled_async',autospec=True) as refresh:
            app=self.make_app(projects=[project])
        load.assert_called_once_with(app.studio)
        display.assert_called_once_with(app,[rows[0]],final=False)
        refresh.assert_called_once_with(app)
        self.assertEqual(app.compiled_status.get(),'Saved built games; checking for changes...')

    def test_completed_refresh_persists_last_verified_rows_off_ui_thread(self):
        from studio import compiled_library
        app=self.make_app();app.compiled_scan_busy=False
        app.studio.state['projects']=[dict(id='built',name='Built game',queue_removed=True),dict(id='pending',name='Pending game')]
        app.studio.save=MagicMock()
        app.studio.list_test_candidates=lambda project:[dict(id='candidate')] if project['id']=='built' else []
        with patch.object(compiled_library,'scan_outputs',return_value={}), patch.object(compiled_library,'hidden_project_ids',return_value=set()), patch.object(compiled_library,'write_cached_rows') as save_view, patch.object(gui.threading,'Thread') as worker:
            app.refresh_compiled_async()
            save_view.assert_not_called()
            worker.call_args.kwargs['target']()
            save_view.assert_called_once_with(app.studio,[('built','Built game',1,True)])
            app.poll()
        app.studio.save.assert_not_called()

    def test_invalid_startup_display_cache_does_not_break_studio(self):
        from studio import compiled_library
        with patch.object(compiled_library,'read_cached_rows',side_effect=ValueError('Malformed display cache')), patch.object(gui.App,'_update_compiled_rows',autospec=True) as display, patch.object(gui.App,'refresh_compiled_async',autospec=True):
            app=self.make_app(projects=[dict(id='saved',name='Saved game',status='compiled; untested')])
        display.assert_not_called()
        events=[]
        while not app.events.empty():events.append(app.events.get_nowait())
        self.assertTrue(any(kind=='log' and 'could not be restored' in text for kind,text in events))

    def test_startup_saved_rows_keep_hidden_projects_hidden(self):
        from studio import compiled_library
        rows=[('hide','Hidden game',1,False),('keep','Visible game',2,True)]
        with patch.object(compiled_library,'read_cached_rows',return_value=rows), patch.object(compiled_library,'hidden_project_ids',return_value={'hide'}), patch.object(gui.App,'refresh_compiled_async',autospec=True):
            app=self.make_app(projects=[dict(id='hide',name='Hidden game',status='compiled; untested'),dict(id='keep',name='Visible game',status='compiled; untested')])
        # The empty mocked tree defaults exists() truthy, so restoration uses item.
        self.assertEqual([call.args[0] for call in app.compiled_tree.item.call_args_list],['keep'])

    def test_output_folder_button_schedules_scan_while_queue_busy(self):
        from studio import compiled_library
        buttons=[]
        original=gui.App.button
        def capture(app,parent,text,command,accent=False):
            buttons.append((text,command))
            return original(app,parent,text,command,accent)
        with patch.object(gui.App,'button',capture):
            app=self.make_app()
        callback=[command for text,command in buttons if text=='Select output folder']
        self.assertEqual(len(callback),1)
        self.assertEqual(callback[0].__func__,gui.App.select_compiled_output_folder)
        app.busy=True;app.compiled_scan_busy=False
        app.current_queue_item.set('Building another game')
        app.studio.save=MagicMock()
        app.studio.list_test_candidates=MagicMock(return_value=[])
        with patch.object(gui.filedialog,'askdirectory',return_value='Z:/Chosen outputs'), patch.object(gui.threading,'Thread') as worker, patch.object(compiled_library,'scan_outputs',return_value={}) as scan:
            callback[0]()
            scan.assert_not_called()
            self.assertEqual(app.compiled_output_folder.get(),'Z:/Chosen outputs')
            worker.call_args.kwargs['target']()
            self.assertEqual(scan.call_args.args[1],'Z:/Chosen outputs')
        app.poll()
        app.studio.save.assert_not_called()
        self.assertTrue(app.busy)
        self.assertFalse(app.cancel.is_set())
        self.assertEqual(app.current_queue_item.get(),'Building another game')

    def test_retry_blocked_button_filters_queue_and_reuses_sources(self):
        app=self.make_app()
        blocked=dict(id='bad',name='Blocked',status='blocked')
        removed=dict(id='removed',name='Archived',status='blocked',queue_removed=True)
        compiled=dict(id='done',name='Finished',status='compiled; untested')
        app.studio.state['projects']=[blocked,removed,compiled]
        app.run_projects=MagicMock()
        app.retry_blocked()
        app.run_projects.assert_called_once_with([blocked],'retry')
        app.busy=True;app.run_projects.reset_mock()
        app.retry_blocked()
        app.run_projects.assert_not_called()
        self.assertFalse(app.cancel.is_set())

    def test_retry_worker_skips_completed_candidates_continues_failures_and_keeps_sources(self):
        app=self.make_app()
        completed=dict(id='done',name='Prior candidate',status='blocked')
        first=dict(id='first',name='First blocked',status='blocked')
        second=dict(id='second',name='Second blocked',status='blocked')
        app.studio.list_test_candidates=lambda p:[dict(id='retained')] if p['id']=='done' else []
        app.studio.build=MagicMock(side_effect=[RuntimeError('still unsupported'),None])
        emit=MagicMock();emit.state.key='spu'
        app.start_job=lambda work:work(emit)
        with self.assertRaisesRegex(RuntimeError,'1 project'):
            app.run_projects([completed,first,second],'retry')
        self.assertEqual([call.args[0]['id'] for call in app.studio.build.call_args_list],['first','second'])
        self.assertTrue(all(call.kwargs['reuse'] for call in app.studio.build.call_args_list))
        self.assertFalse(app.cancel.is_set())

    def test_compiled_refresh_reports_validation_errors_and_finishes(self):
        app=self.make_app();app.compiled_scan_busy=False
        app.studio.state['projects']=[dict(id='p',name='Bad candidate')]
        app.studio.list_test_candidates=MagicMock(side_effect=ValueError('Invalid build receipt'))
        with patch.object(gui.threading,'Thread',ImmediateThread):
            app.refresh_compiled_async()
        app.poll()
        self.assertIn('1 game check(s) failed',app.compiled_status.get())
        self.assertFalse(app.compiled_scan_busy)

    def test_full_headless_initialization_creates_all_status_variables_and_handlers(self):
        app=self.make_app()
        for name in ('status','current_queue_item','overall_progress_text','step_progress_text',
                     'progress_detail','manual_status','fix_status','test_session_var'):
            self.assertIsInstance(getattr(app,name),Var,name)
        app.events.put(('queue_item',dict(action='build',name='MotorStorm',index=0,total=20)))
        app.events.put(('progress',dict(overall='Overall 10%',overall_percent=10,step='Compiling 30%',
                                       detail='actual compiler output',status='running',step_percent=30)))
        app.events.put(('manual_error','A separate test message'))
        app.poll()
        self.assertEqual(app.current_queue_item.get(),'Building 1/20: MotorStorm')
        self.assertEqual(app.manual_status.get(),'A separate test message')
        self.assertEqual(app.step_progress_text.get(),'Compiling 30%')
        self.assertFalse(app.fix_cancel is app.cancel)

    def test_manual_native_launch_while_build_busy_keeps_queue_identity_and_state(self):
        app=self.make_app();app.busy=True
        app.current_queue_item.set('Building 2/20: Another game')
        project=dict(id='p',name='MotorStorm',status='building',attempt='live-attempt')
        app.selected=lambda:[project]
        candidate=dict(id='finished',label='Previous completed build')
        app.choose_completed_candidate=lambda p,choose,**kwargs:choose(candidate)
        calls=[]
        def launch(snapshot,native,candidate_id=None):
            calls.append((snapshot,native,candidate_id));return 123
        app.studio.launch=launch
        app.studio.list_test_sessions=lambda:[dict(session_id='owned-test',pid=123,status='running',kind='native',
                                                  candidate_label='Previous completed build',project_id='p',log_path='only-test.log')]
        with patch.object(gui.threading,'Thread',ImmediateThread):app.launch(True)
        app.poll()
        self.assertEqual(calls[0][2],'finished')
        self.assertIsNot(calls[0][0],project)
        self.assertEqual(project['attempt'],'live-attempt')
        self.assertTrue(app.busy)
        self.assertFalse(app.cancel.is_set())
        self.assertEqual(app.current_queue_item.get(),'Building 2/20: Another game')
        self.assertIn('Previous completed build',app.manual_status.get())

    def test_stop_test_targets_one_owned_session_and_preserves_build_and_fix_cancel(self):
        app=self.make_app();app.busy=True
        app.test_session_labels={'Chosen test':'owned-a'};app.test_session_var.set('Chosen test')
        calls=[];app.studio.stop_test_session=lambda session_id:calls.append(session_id) or True
        with patch.object(gui.threading,'Thread',ImmediateThread):app.stop_test()
        self.assertEqual(calls,['owned-a'])
        self.assertFalse(app.cancel.is_set())
        self.assertFalse(app.fix_cancel.is_set())
        self.assertTrue(app.busy)

    def test_stop_fixes_uses_only_its_separate_cancellation(self):
        app=self.make_app();app.busy=True;app.fix_busy=True
        app.stop_fixes()
        self.assertTrue(app.fix_cancel.is_set())
        self.assertFalse(app.cancel.is_set())
        self.assertTrue(app.busy)

    def test_stop_build_leaves_manual_tests_and_fix_worker_alone(self):
        app=self.make_app();app.busy=True;app.fix_busy=True
        app.manual_status.set('Native test: running')
        app.stop()
        self.assertTrue(app.cancel.is_set())
        self.assertFalse(app.fix_cancel.is_set())
        self.assertEqual(app.manual_status.get(),'Native test: running')

    def test_fix_worker_and_busy_promotion_never_cancel_or_replace_queue(self):
        import sys,types,studio
        app=self.make_app();app.busy=True
        app.current_queue_item.set('Building 4/20: Queue game')
        source=dict(id='p',name='MotorStorm',attempt='live-attempt')
        candidate=dict(id='prior',attempt='completed-attempt')
        calls=[]
        module=types.ModuleType('studio.game_fixes')
        module.preview=lambda s,p,c:calls.append(('preview',p,c)) or dict(available=True)
        module.run=lambda s,p,cancel=None,emit=None:calls.append(('run',cancel)) or dict(status='ready')
        module.promote=lambda s,result,queue_busy=False:calls.append(('promote',queue_busy)) or dict(promoted=False,reason='Queue active; candidate retained.')
        with patch.dict(sys.modules,{'studio.game_fixes':module}),patch.object(studio,'game_fixes',module,create=True),patch.object(gui.threading,'Thread',ImmediateThread):
            app.start_game_fixes(source,candidate);app.poll()
        self.assertEqual(calls[0],('preview',source,candidate))
        self.assertIs(calls[1][1],app.fix_cancel)
        self.assertEqual(calls[2],('promote',True))
        self.assertFalse(app.fix_busy)
        self.assertFalse(app.cancel.is_set())
        self.assertTrue(app.busy)
        self.assertEqual(source['attempt'],'live-attempt')
        self.assertEqual(app.current_queue_item.get(),'Building 4/20: Queue game')

    def test_compiled_cleanup_retains_projects_and_builds_and_excludes_from_batch(self):
        app=self.make_app()
        completed=dict(id='done', name='Finished', native_exe='retained.exe', status='compiled')
        pending=dict(id='todo', name='Pending', status='queued')
        app.studio.state['projects']=[completed,pending]
        app.studio.list_test_candidates=lambda p:[dict(id='candidate')] if p['id']=='done' else []
        app.studio.save=MagicMock()
        from studio.compiled_queue_cleanup import apply_removals
        apply_removals(app.studio,["done"])
        self.assertEqual(len(app.studio.state['projects']),2)
        self.assertTrue(completed['queue_removed'])
        self.assertEqual(completed['native_exe'],'retained.exe')
        app.run_projects=MagicMock()
        app.build_queue()
        app.run_projects.assert_called_once_with([pending],'build')
        app.selected=lambda:[completed]
        app.restore_compiled()
        self.assertFalse(completed['queue_removed'])

    def test_cleanup_during_active_queue_delegates_to_background_worker(self):
        from studio import compiled_queue_cleanup
        app=self.make_app();app.busy=True
        app.studio.save=MagicMock()
        with patch.object(compiled_queue_cleanup,'remove_compiled_async') as remove:
            app.remove_compiled()
        remove.assert_called_once_with(app)
        app.studio.save.assert_not_called()
        self.assertTrue(app.busy)

    def test_library_validation_is_deferred_off_startup_thread(self):
        app=self.make_app()
        app.compiled_scan_busy=False
        app.studio.state['projects']=[dict(id='p',name='Game')]
        app.studio.list_test_candidates=MagicMock(return_value=[])
        with patch.object(gui.threading,'Thread') as thread:
            app.refresh_compiled_async()
            app.studio.list_test_candidates.assert_not_called()
            thread.return_value.start.assert_called_once()
            thread.call_args.kwargs['target']()
        app.studio.list_test_candidates.assert_called_once()
        app.poll()
        self.assertFalse(app.compiled_scan_busy)

    def test_repair_progress_has_separate_stage_and_overall_and_preserves_queue(self):
        app=self.make_app();app.busy=True
        app.current_queue_item.set('Building another game')
        app.events.put(('repair_progress',dict(project_name='MotorStorm',stage='Checking sources',
            step_percent=25.,overall_percent=12.5,detail='1/4 files; 25/100 bytes',status='running')))
        app.poll()
        self.assertIn('25.0%',app.repair_progress_text.get())
        self.assertIn('12.5%',app.repair_overall_text.get())
        self.assertEqual(app.current_queue_item.get(),'Building another game')
        self.assertFalse(app.cancel.is_set())

    def test_compiled_hide_and_restore_buttons_are_created_and_bound(self):
        buttons=[];original=gui.App.button
        def capture(app,parent,text,command,accent=False):
            buttons.append((text,command));return original(app,parent,text,command,accent)
        with patch.object(gui.App,'button',capture):app=self.make_app()
        for label,method in [('Remove selected from compiled list',gui.App.hide_compiled_selection),
                             ('Show removed games',gui.App.show_removed_compiled)]:
            callbacks=[command for text,command in buttons if text==label]
            self.assertEqual(len(callbacks),1)
            self.assertEqual(callbacks[0].__self__,app);self.assertEqual(callbacks[0].__func__,method)

    def test_hide_selected_compiled_rows_during_build_changes_only_visibility(self):
        from studio import compiled_library
        import copy
        app=self.make_app();app.busy=True
        projects=[dict(id='a',name='A',queue_removed=True),dict(id='b',name='B',status='building')]
        app.studio.state['projects']=projects;before=copy.deepcopy(app.studio.state)
        app.current_queue_item.set('Building B')
        app.studio.save=MagicMock();app.refresh_compiled_async=MagicMock()
        app.compiled_tree.selection.return_value=('a',)
        with patch.object(compiled_library,'set_hidden_projects',create=True) as save_hidden,              patch.object(compiled_library,'hidden_project_ids',return_value={'a'},create=True):
            app.hide_compiled_selection()
        save_hidden.assert_called_once_with(app.studio,['a'],hidden=True)
        app.compiled_tree.delete.assert_called_with('a')
        app.refresh_compiled_async.assert_called_once()
        app.studio.save.assert_not_called();self.assertEqual(app.studio.state,before)
        self.assertTrue(app.busy);self.assertFalse(app.cancel.is_set())
        self.assertEqual(app.current_queue_item.get(),'Building B')

    def test_inflight_compiled_scan_cannot_restore_a_newly_hidden_row(self):
        from studio import compiled_library
        app=self.make_app();app.busy=True;app.compiled_scan_pending=False
        app.studio.save=MagicMock();app.studio.list_test_candidates=MagicMock()
        app.compiled_tree.selection.return_value=('hide','keep')
        app.compiled_tree.exists.side_effect=lambda ident:ident=='keep'
        app.compiled_tree.insert.reset_mock()
        app.events.put(('compiled_rows',[('hide','Hidden game',1,True),('keep','Visible game',2,False)]))
        with patch.object(compiled_library,'hidden_project_ids',return_value={'hide'},create=True):app.poll()
        inserted=[call.kwargs['iid'] for call in app.compiled_tree.insert.call_args_list]
        self.assertEqual(inserted,['keep'])
        app.compiled_tree.selection_set.assert_called_with(['keep'])
        app.studio.list_test_candidates.assert_not_called();app.studio.save.assert_not_called()
        self.assertTrue(app.busy);self.assertFalse(app.cancel.is_set())

    def test_background_refresh_skips_hidden_games_without_mutating_queue(self):
        from studio import compiled_library
        app=self.make_app();app.busy=True;app.compiled_scan_busy=False;app.compiled_scan_pending=False
        app.studio.state['projects']=[dict(id='hide',name='Hidden'),dict(id='keep',name='Kept')]
        app.studio.save=MagicMock();app.studio.list_test_candidates=MagicMock(return_value=[dict(id='done')])
        with patch.object(compiled_library,'hidden_project_ids',return_value={'hide'},create=True),              patch.object(compiled_library,'scan_outputs',return_value={}),              patch.object(gui.threading,'Thread') as worker:
            app.refresh_compiled_async()
            app.studio.list_test_candidates.assert_not_called()
            worker.call_args.kwargs['target']()
            self.assertEqual([call.args[0]['id'] for call in app.studio.list_test_candidates.call_args_list],['keep'])
            app.poll()
        app.studio.save.assert_not_called();self.assertTrue(app.busy);self.assertFalse(app.cancel.is_set())

    def test_show_removed_games_clears_visibility_only_while_building(self):
        from studio import compiled_library
        app=self.make_app();app.busy=True;app._compiled_hidden_ids={'a'}
        app.studio.save=MagicMock();app.refresh_compiled_async=MagicMock()
        with patch.object(compiled_library,'set_hidden_projects',create=True) as clear:app.show_removed_compiled()
        clear.assert_called_once_with(app.studio,[],hidden=False)
        self.assertEqual(app._compiled_hidden_ids,set());app.refresh_compiled_async.assert_called_once()
        app.studio.save.assert_not_called();self.assertTrue(app.busy);self.assertFalse(app.cancel.is_set())
