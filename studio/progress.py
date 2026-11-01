# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Measured step progress and stage-based overall progress; never an ETA."""
import re

BUILD_STEPS=[('inspect','Inspect game input'),('prepare','Prepare Fulton sources and project'),
 ('ppu-load','Read executable and imports'),('ppu-lift','Translate PPU functions'),
 ('ppu-finalize','Resolve remaining entries and write PPU sources'),
 ('spu-discover-lift','Discover and translate SPU workloads'),
 ('cmake-configure','Configure native build'),('cmake-build','Compile native executable'),
 ('verify','Verify executable and save build report')]

PORT_STEPS={'check':'ppu-load','load':'ppu-load','ppu-lift':'ppu-lift','spu-lift':'spu-discover-lift',
 'configure':'cmake-configure','compile':'cmake-build','stamp':'verify'}

def steps_for(action):
    if action=='operation':return [('operation','Current operation')]
    if action=='analyse':return BUILD_STEPS[:1]
    if action=='lift':return BUILD_STEPS[:6]
    if action=='rebuild':return [BUILD_STEPS[i] for i in (0,6,7,8)]
    if action=='setup':return [('clone','Download backend'),('checkout','Select pinned backend'),('python-env','Prepare Python environment'),('dependencies','Install Python dependencies')]
    return BUILD_STEPS[:]

class ProgressState:
    def __init__(self, action='build', project='', index=0, total=1):
        self.steps=steps_for(action);self.project=project;self.index=index;self.total=max(1,total)
        self.position=0;self.percent=0.0;self.measured=False;self.status='running';self.detail='Waiting for tool output'
        self.completed=set()
    @property
    def key(self):return self.steps[self.position][0]
    def event(self,key,event,percent=None,line='',detail=''):
        keys=[k for k,_ in self.steps]
        if key=='port-firstrun':
            # Upstream port pipeline (studio/upstream_ports.py): map its PROGRESS lines onto the build steps.
            if event!='output':return
            match=re.match(r'PROGRESS step=\d+/\d+ name=(\S+) done=(\d+) total=(\d+)',line.strip())
            if not match or PORT_STEPS.get(match[1]) not in keys:return
            key=PORT_STEPS[match[1]];total=int(match[3])
            for earlier in keys[:keys.index(key)]:self.completed.add(earlier)
            event,percent,detail='percent',(100*int(match[2])/total if total else 0.),f'{match[1]}: {match[2]} / {match[3]}'
        if key not in keys:return
        position=keys.index(key)
        if event=='start':
            self.position=position;self.percent=0.;self.measured=False;self.status='running'
            self.detail=detail or 'Tool has not reported an intermediate percentage'
        elif event=='output':
            if key=='ppu-lift':
                match=re.search(r'lifted\s+(\d+)\s*/\s*(\d+)\s+functions',line)
                if match and int(match[2]):
                    self.position=position;self.percent=min(100.,100*int(match[1])/int(match[2]));self.measured=True
                    self.detail=f'{match[1]} / {match[2]} functions'
                    if self.percent>=100 and 'ppu-finalize' in keys:
                        self.completed.add(key);self.position=keys.index('ppu-finalize')
                        self.percent=0.;self.measured=False;self.detail='Main function pass complete; finalization has no intermediate counts'
            elif key=='cmake-build':
                match=re.search(r'\[\s*(\d+)\s*/\s*(\d+)\]',line)
                percent_match=re.search(r'\[\s*(\d+)%\]',line)
                if match and int(match[2]):
                    self.position=position;self.percent=min(100.,100*int(match[1])/int(match[2]));self.measured=True
                    self.detail=f'{match[1]} / {match[2]} build actions'
                elif percent_match:
                    self.position=position;self.percent=min(100.,float(percent_match[1]));self.measured=True;self.detail='Compiler-reported progress'
        elif event=='done':
            self.completed.add(key)
            if key=='ppu-lift' and 'ppu-finalize' in keys:
                self.completed.add('ppu-finalize');position=keys.index('ppu-finalize')
            self.position=position;self.percent=100.;self.measured=True;self.detail=detail or 'Step completed'
        elif event in ('failed','cancelled'):
            self.status=event;self.detail=detail or event.title()
        elif event=='percent':
            self.position=position;self.percent=max(0.,min(100.,float(percent)));self.measured=True;self.detail=detail
    def snapshot(self):
        current=self.key
        units=len(self.completed)+(0 if current in self.completed else self.percent/100)
        overall=100*(self.index+units/len(self.steps))/self.total
        note='' if self.measured else ' · no intermediate counts from tool'
        step=f'Step {self.position+1}/{len(self.steps)} · {self.steps[self.position][1]} · {self.percent:.1f}%{note}'
        return dict(step=step,step_percent=self.percent,overall_percent=min(100.,overall),
                    overall=f'Overall {min(100.,overall):.1f}% · stage-based · game {self.index+1}/{self.total}'+(f' · {self.project}' if self.project else ''),
                    detail=self.detail,status=self.status,key=current)

class ProgressEmitter:
    def __init__(self, log, update):
        self.log=log;self.update=update;self.state=ProgressState();self.previous=None
    def __call__(self,text):self.log(str(text))
    def begin(self,project='',index=0,total=1,action='build'):
        self.state=ProgressState(action,project,index,total);self.previous=self.state.snapshot();self.update(self.previous)
    def progress(self,key,event,**kwargs):
        self.state.event(key,event,**kwargs)
        snapshot=self.state.snapshot()
        if snapshot!=self.previous:self.previous=snapshot;self.update(snapshot)

def report(emit,key,event,**kwargs):
    callback=getattr(emit,'progress',None)
    if callback:callback(key,event,**kwargs)
