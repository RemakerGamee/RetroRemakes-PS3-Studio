# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Read-only progress viewer for an existing attempt; never starts/stops a build."""
import argparse
import codecs
import json
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk
from .progress import ProgressState

class AttemptProgress:
    def __init__(self,attempt,title='MotorStorm'):
        self.attempt=Path(attempt);self.state=ProgressState(project=title)
        self.offsets={};self.decoders={};self.pending={};self.active=None
    def update(self):
        folder=self.attempt/'logs'
        logs=sorted(folder.glob('[0-9]*-*.log'))
        if logs:
            for key in ('inspect','prepare'):self.state.completed.add(key)
        records=[]
        try:records=json.loads((folder/'commands.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        passed={r['stage'] for r in records if r.get('status')=='passed'}
        for log in logs:
            key=re.sub(r'^\d+-','',log.stem)
            if log in self.offsets and key!=self.active:continue
            if self.active!=key:
                if self.active is not None and self.active in passed:self.state.event(self.active,'done')
                self.state.event(key,'start');self.active=key
            offset=self.offsets.get(log,0)
            if log.stat().st_size<offset:offset=0;self.pending.pop(log,None);self.decoders.pop(log,None)
            decoder=self.decoders.setdefault(log,codecs.getincrementaldecoder('utf-8')('replace'))
            with log.open('rb') as stream:
                stream.seek(offset)
                # Parsing in chunks keeps even warning-heavy compiler logs bounded.
                while True:
                    data=stream.read(65536)
                    if not data:break
                    text=self.pending.get(log,'')+decoder.decode(data)
                    lines=re.split(r'[\r\n]+',text);self.pending[log]=lines.pop()
                    for line in lines:self.state.event(key,'output',line=line)
                self.offsets[log]=stream.tell()
            if key in passed:self.state.event(key,'done')
        try:report=json.loads((self.attempt/'build-report.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):report={}
        status=report.get('status','building')
        if status=='compiled; gameplay unverified':
            self.state.event('verify','done');self.state.status='completed'
        elif status in ('blocked','cancelled'):
            self.state.event(self.state.key,'cancelled' if status=='cancelled' else 'failed',detail=report.get('error',status))
        return self.state.snapshot()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--attempt',required=True);parser.add_argument('--title',default='MotorStorm: Pacific Rift');args=parser.parse_args()
    reader=AttemptProgress(args.attempt,args.title)
    root=tk.Tk();root.title(args.title+' - live build progress');root.geometry('1000x280')
    frame=ttk.Frame(root,padding=16);frame.pack(fill='both',expand=True)
    overall=tk.StringVar();step=tk.StringVar();detail=tk.StringVar();status=tk.StringVar()
    ttk.Label(frame,textvariable=overall,font=('Segoe UI',12,'bold'),wraplength=950).pack(anchor='w')
    overall_bar=ttk.Progressbar(frame,maximum=100);overall_bar.pack(fill='x',pady=8)
    ttk.Label(frame,textvariable=step,font=('Segoe UI',11),wraplength=950).pack(anchor='w')
    step_bar=ttk.Progressbar(frame,maximum=100);step_bar.pack(fill='x',pady=8)
    ttk.Label(frame,textvariable=detail,wraplength=950).pack(anchor='w')
    ttk.Label(frame,textvariable=status,wraplength=950).pack(anchor='w',pady=8)
    ttk.Label(frame,text='Read-only viewer. Closing this window leaves the Studio build running. Overall progress counts stages, not elapsed time.').pack(anchor='w')
    def poll():
        try:
            snap=reader.update();overall.set(snap['overall']);step.set(snap['step']);detail.set(snap['detail'])
            overall_bar['value']=snap['overall_percent'];step_bar['value']=snap['step_percent']
            status.set('Build '+snap['status']+' · saved logs are checked every second')
        except (OSError,ValueError) as error:status.set('Waiting for readable build logs: '+str(error))
        root.after(1000,poll)
    poll();root.mainloop()

if __name__=='__main__':main()
