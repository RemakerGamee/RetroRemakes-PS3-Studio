# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
from types import SimpleNamespace
from unittest import TestCase
import queue
import threading
from studio.core import Cancelled
from studio.gui import App, queue_item_label, queue_completion_label

class Emit:
    state=SimpleNamespace(key='compile')
    def begin(self,*args):pass
    def __call__(self,*args):pass
    def progress(self,*args,**kwargs):pass

class QueueIdentityTests(TestCase):
    def app(self,build):
        return SimpleNamespace(events=queue.Queue(),cancel=threading.Event(),
                               studio=SimpleNamespace(build=build))
    def run_work(self,app,projects):
        work=[]
        app.start_job=lambda callback:work.append(callback)
        App.run_projects(app,projects,'build')
        return work[0]
    def events(self,app):
        result=[]
        while not app.events.empty():result.append(app.events.get_nowait())
        return result

    def test_actual_name_position_and_total_format(self):
        self.assertEqual(queue_item_label(dict(action='build',index=0,total=20,name='MotorStorm')),
                         'Building 1/20: MotorStorm')
        self.assertEqual(queue_completion_label(dict(attempted=2,total=5,failures=1,cancelled=True)),
                         'Queue cancelled: 2/5 attempted \u00b7 1 blocked')

    def test_snapshot_names_and_positions_survive_selection_list_changes(self):
        calls=[];app=self.app(lambda p,*args,**kwargs:calls.append(p['id']))
        projects=[dict(id='a',name='MotorStorm'),dict(id='b',name='Juiced 2')]
        work=self.run_work(app,projects)
        projects[0]['name']='Renamed later';projects.pop()
        work(Emit())
        events=self.events(app)
        running=[v for k,v in events if k=='queue_item' and v['state']=='running']
        self.assertEqual([(x['name'],x['index'],x['total']) for x in running],
                         [('MotorStorm',0,2),('Juiced 2',1,2)])
        self.assertEqual(calls,['a','b'])
        self.assertEqual(events[-1],('queue_complete',dict(attempted=2,total=2,failures=0,cancelled=False)))

    def test_blocked_item_continues_and_summary_counts_actual_attempts(self):
        def build(p,*args,**kwargs):
            if p['id']=='a':raise RuntimeError('compile failed')
        app=self.app(build);work=self.run_work(app,[dict(id='a',name='First'),dict(id='b',name='Second')])
        with self.assertRaisesRegex(RuntimeError,'1 project'):work(Emit())
        events=self.events(app)
        self.assertEqual([v['state'] for k,v in events if k=='queue_item'],['running','failed','running','done'])
        self.assertEqual(events[-1][1],dict(attempted=2,total=2,failures=1,cancelled=False))

    def test_cancellation_before_next_item_does_not_claim_unattempted_work(self):
        app=self.app(lambda *args,**kwargs:app.cancel.set())
        work=self.run_work(app,[dict(id='a',name='First'),dict(id='b',name='Second')])
        with self.assertRaises(Cancelled):work(Emit())
        events=self.events(app)
        self.assertEqual(events[-1][1],dict(attempted=1,total=2,failures=0,cancelled=True))
        self.assertEqual(sum(k=='queue_item' and v['state']=='running' for k,v in events),1)

    def test_active_cancellation_has_specific_item_event(self):
        def build(*args,**kwargs):raise Cancelled('Stopped')
        app=self.app(build);work=self.run_work(app,[dict(id='a',name='First')])
        with self.assertRaises(Cancelled):work(Emit())
        events=self.events(app)
        self.assertEqual([v['state'] for k,v in events if k=='queue_item'],['running','cancelled'])
        self.assertEqual(events[-1][1],dict(attempted=1,total=1,failures=0,cancelled=True))
