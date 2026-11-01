# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""User-opened game tools alongside an already running Studio build queue.

The queue JSON is read only. Manual-test receipts and isolated fix sidecars are
owned by their respective services; this window never promotes into queue state.
"""
from __future__ import annotations

import argparse
import copy
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .core import Studio
from .gui import App


def refresh_snapshot(studio):
    value=json.loads(studio.state_path.read_text(encoding='utf-8'))
    if not isinstance(value.get('projects'),list) or not isinstance(value.get('settings'),dict):
        raise ValueError('Studio queue snapshot is incomplete; try Refresh games again.')
    studio.state=copy.deepcopy(value)
    return studio.state['projects']


class GameTools(tk.Tk):
    button=App.button
    choose_completed_candidate=App.choose_completed_candidate
    launch=App.launch
    start_manual_test=App.start_manual_test
    refresh_test_sessions=App.refresh_test_sessions
    watch_manual_sessions=App.watch_manual_sessions
    stop_test=App.stop_test
    open_test_log=App.open_test_log
    open_path=App.open_path
    apply_game_fixes=App.apply_game_fixes
    start_game_fixes=App.start_game_fixes
    stop_fixes=App.stop_fixes
    add_repair_progress=App.add_repair_progress
    show_repair_progress=App.show_repair_progress
    watch_repair_progress=App.watch_repair_progress

    def __init__(self,home=None):
        super().__init__()
        self.title('PS3 Game tools - build queue stays running')
        self.geometry('1100x600');self.minsize(850,550)
        self.studio=Studio.read_only_snapshot(home)
        self.events=queue.Queue()
        self.manual_launch_busy=False;self.manual_launch_worker=None;self.manual_stop_worker=None
        self.test_sessions={};self.test_session_labels={}
        self.fix_busy=False;self.fix_worker=None;self.fix_cancel=threading.Event()
        self.game_labels={};self.game_var=tk.StringVar(value='')
        self.manual_status=tk.StringVar(value='Manual test: idle')
        self.fix_status=tk.StringVar(value='Fix preparation: idle')
        self.test_session_var=tk.StringVar(value='')
        self.overview=tk.StringVar(value='Reading the existing Studio queue; builds remain under the original window.')
        frame=ttk.Frame(self,padding=18);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='Game tools',font=('Segoe UI',20,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Use this window while Studio continues building. Queue files and active builds are preserved.',
                  wraplength=1000).pack(anchor='w',pady=8)
        row=ttk.Frame(frame);row.pack(fill='x')
        self.game_picker=ttk.Combobox(row,textvariable=self.game_var,state='readonly',width=100)
        self.game_picker.pack(side='left',fill='x',expand=True)
        self.button(row,'Refresh games',self.refresh_games)
        ttk.Label(frame,textvariable=self.overview,wraplength=1000).pack(anchor='w',pady=6)
        row=ttk.Frame(frame);row.pack(fill='x')
        self.button(row,'Test native build',lambda:self.launch(True))
        self.button(row,'Test in RPCS3',lambda:self.launch(False))
        self.button(row,'Apply fixes to games',self.apply_game_fixes)
        self.button(row,'Stop fixes',self.stop_fixes)
        ttk.Label(frame,textvariable=self.fix_status,wraplength=1000).pack(anchor='w',pady=8)
        self.add_repair_progress(frame)
        row=ttk.Frame(frame);row.pack(fill='x')
        self.test_session_picker=ttk.Combobox(row,textvariable=self.test_session_var,state='readonly',width=100)
        self.test_session_picker.pack(side='left',fill='x',expand=True)
        self.button(row,'Stop test',self.stop_test)
        self.button(row,'Open test log',self.open_test_log)
        self.test_session_picker.bind('<<ComboboxSelected>>',lambda event:self.manual_status.set(self.test_session_var.get()))
        ttk.Label(frame,textvariable=self.manual_status,wraplength=1000).pack(anchor='w',pady=8)
        ttk.Label(frame,text='Fixes produce a separate completed candidate. This companion never changes the queue-selected build.',
                  wraplength=1000).pack(anchor='w')
        self.refresh_games()
        self.protocol('WM_DELETE_WINDOW',self.close_tools)
        self.after(120,self.poll)
        self.after(750,self.watch_manual_sessions)
        self.after(1000,self.watch_repair_progress)

    def refresh_games(self):
        selected_id=self.game_labels.get(self.game_var.get())
        try:projects=refresh_snapshot(self.studio)
        except (OSError,ValueError) as error:
            self.overview.set('Queue read failed: '+str(error));return
        self.game_labels={f"{p['name']} - {p.get('status','queued')} [{p['id'][:8]}]":p['id'] for p in projects}
        self.game_picker.configure(values=list(self.game_labels))
        label=next((label for label,pid in self.game_labels.items() if pid==selected_id),'')
        # Initial selection is a convenience in this separate window only.
        if not selected_id and self.game_labels:label=next(iter(self.game_labels))
        self.game_var.set(label)
        active=[p['name'] for p in projects if p.get('status') in ('building','analysing')]
        self.overview.set(('Studio working on: '+', '.join(active)) if active else 'No active build is reported in the saved queue snapshot.')

    def selected(self):
        selected_id=self.game_labels.get(self.game_var.get())
        if not selected_id:raise ValueError('Choose a game first.')
        refresh_snapshot(self.studio)
        project=next((p for p in self.studio.state['projects'] if p['id']==selected_id),None)
        if project is None:raise ValueError('This game is no longer in the queue. Refresh games and choose again.')
        return [copy.deepcopy(project)]

    def poll(self):
        while True:
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='manual_launched':
                self.manual_launch_busy=False;self.refresh_test_sessions(preferred_pid=value['pid'])
            elif kind in ('manual_error','manual_stop_error'):
                if kind=='manual_error':self.manual_launch_busy=False
                self.manual_status.set(value)
            elif kind=='manual_stopped':
                self.refresh_test_sessions();self.manual_status.set(value)
            elif kind=='repair_watch':
                self.repair_watch_busy=False
                if value:self.show_repair_progress(value)
            elif kind=='repair_progress':self.show_repair_progress(value)
            elif kind=='fix_status':self.fix_status.set(value)
            elif kind=='fix_error':
                self.fix_busy=False;self.fix_status.set('Fix preparation: '+value)
            elif kind=='fix_ready_one':
                try:
                    from .game_fixes import register_test_candidate
                    register_test_candidate(self.studio,value)
                except Exception as error:self.fix_status.set('Fix build kept, registration pending: '+str(error))
            elif kind=='fix_all_done':
                self.fix_busy=False;self.fix_status.set(value+' Use Test native build to pick the fixed builds.')
            elif kind=='fix_ready':
                self.fix_busy=False
                if value.get('status')=='ready':
                    try:
                        from .game_fixes import register_test_candidate
                        register_test_candidate(self.studio,value)
                        self.fix_status.set('Fix candidate ready. Use Test native build to choose it; the Studio queue is unchanged.')
                    except Exception as error:self.fix_status.set('Fix candidate retained, registration pending: '+str(error))
                else:self.fix_status.set('Fix preparation: '+value.get('status','failed'))
        self.after(120,self.poll)

    def close_tools(self):
        self.stop_fixes()
        workers=(self.fix_worker,self.manual_launch_worker,self.manual_stop_worker)
        if any(worker and worker.is_alive() for worker in workers):
            self.fix_status.set('Waiting for this window\'s worker to finish. The Studio queue continues.')
            self.after(200,self.close_tools)
        else:self.destroy()

    def report_callback_exception(self,exc,value,tb):
        messagebox.showerror('PS3 Game tools',str(value))


def main():
    parser=argparse.ArgumentParser(description='Independent PS3 game tests and isolated fixes')
    parser.add_argument('--home',help='Existing Studio workspace')
    args=parser.parse_args()
    GameTools(args.home).mainloop()


if __name__=='__main__':main()
