# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""PS3 Studio controller profiles and live SDL2/XInput setup. No drivers."""
import configparser
import ctypes as C
import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk

ROOT=Path(__file__).resolve().parent.parent
ACTIONS=['Select','L3','R3','Start','D-pad Up','D-pad Right','D-pad Down','D-pad Left','L2','R2','L1','R1','Triangle','Circle','Cross','Square']
TYPES={'PS4 DualShock 4':'ds4','Other / PS4-style SDL controller':'sdl','Xbox / XInput':'xinput','Keyboard':'keyboard','Automatic':'auto'}
SDL_BUTTONS={0:14,1:13,2:15,3:12,4:0,6:3,7:1,8:2,9:10,10:11,11:4,12:6,13:7,14:5}

def profile_path(home):return Path(home)/'controller-profiles/default.ini'
def defaults():return dict(type='ds4',device='0',deadzone='7849',trigger_threshold='30',invert_left_y='0',invert_right_y='0',rumble='1',**{'map_'+str(i):str(i) for i in range(16)})
def load_profile(home, on_warning=None):
    values=defaults();parser=configparser.ConfigParser()
    try:
        parser.read(profile_path(home),encoding='utf-8')
        if parser.has_section('controller'):values.update(dict(parser['controller']))
        return validate(values)
    except (OSError,ValueError,TypeError,configparser.Error):
        if on_warning:on_warning('The saved controller profile is invalid. Defaults are shown; the original file is preserved until you Save.')
        return defaults()
def validate(values):
    result=defaults();result.update(values)
    if result['type'] not in TYPES.values():raise ValueError('Unsupported controller type')
    for key,lo,hi in [('device',0,3),('deadzone',0,20000),('trigger_threshold',0,254),('invert_left_y',0,1),('invert_right_y',0,1),('rumble',0,1)]+[('map_'+str(i),-1,15) for i in range(16)]:
        n=int(result[key])
        if not lo<=n<=hi:raise ValueError('Invalid controller setting: '+key)
        result[key]=str(n)
    return result
def save_profile(home, values):
    values=validate(values);path=profile_path(home);path.parent.mkdir(parents=True,exist_ok=True)
    parser=configparser.ConfigParser();parser['controller']=values
    fd,name=tempfile.mkstemp(prefix='profile-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:parser.write(stream)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return path

class SDLController:
    def __init__(self):
        self.lib=C.CDLL(str(ROOT/'runtime/controller/SDL2.dll'));self.handle=None;self.devices=[]
        api=[('SDL_InitSubSystem',[C.c_uint],C.c_int),('SDL_NumJoysticks',[],C.c_int),('SDL_IsGameController',[C.c_int],C.c_int),('SDL_GameControllerOpen',[C.c_int],C.c_void_p),('SDL_GameControllerClose',[C.c_void_p],None),('SDL_GameControllerGetAttached',[C.c_void_p],C.c_int),('SDL_GameControllerGetVendor',[C.c_void_p],C.c_ushort),('SDL_GameControllerGetProduct',[C.c_void_p],C.c_ushort),('SDL_GameControllerName',[C.c_void_p],C.c_char_p),('SDL_GameControllerGetButton',[C.c_void_p,C.c_int],C.c_ubyte),('SDL_GameControllerGetAxis',[C.c_void_p,C.c_int],C.c_short),('SDL_GameControllerUpdate',[],None),('SDL_PumpEvents',[],None)]
        for name,args,ret in api:fn=getattr(self.lib,name);fn.argtypes=args;fn.restype=ret
        if self.lib.SDL_InitSubSystem(0x2000)!=0:raise RuntimeError('SDL2 could not initialize controller input')
    def detect(self,kind='ds4',index=0):
        self.close();self.devices=[];self.lib.SDL_PumpEvents();count=0;names=[]
        for i in range(self.lib.SDL_NumJoysticks()):
            if not self.lib.SDL_IsGameController(i):continue
            handle=self.lib.SDL_GameControllerOpen(i)
            if not handle:continue
            vendor=self.lib.SDL_GameControllerGetVendor(handle);product=self.lib.SDL_GameControllerGetProduct(handle)
            name=(self.lib.SDL_GameControllerName(handle) or b'Controller').decode('utf-8','replace')
            self.devices.append(dict(name=name,vendor=vendor,product=product))
            match=kind!='ds4' or vendor==0x054C and product in (0x05C4,0x09CC,0x0BA0)
            if match:
                names.append(name)
                if count==index:self.handle=handle;handle=None
                count+=1
            if handle:self.lib.SDL_GameControllerClose(handle)
        return names
    def buttons(self):
        if not self.handle or not self.lib.SDL_GameControllerGetAttached(self.handle):return []
        self.lib.SDL_PumpEvents();self.lib.SDL_GameControllerUpdate();buttons=[]
        for button,action in SDL_BUTTONS.items():
            if self.lib.SDL_GameControllerGetButton(self.handle,button):buttons.append(action)
        for axis,action in ((4,8),(5,9)):
            if self.lib.SDL_GameControllerGetAxis(self.handle,axis)>8000:buttons.append(action)
        return buttons
    def close(self):
        if self.handle:self.lib.SDL_GameControllerClose(self.handle);self.handle=None

class XInputController:
    class Gamepad(C.Structure):
        _fields_=[('buttons',C.c_ushort),('lt',C.c_ubyte),('rt',C.c_ubyte),('lx',C.c_short),('ly',C.c_short),('rx',C.c_short),('ry',C.c_short)]
    class State(C.Structure):pass
    State._fields_=[('packet',C.c_uint),('gamepad',Gamepad)]
    def __init__(self):
        self.lib=C.WinDLL('xinput1_4');self.handle=None
        self.lib.XInputGetState.argtypes=[C.c_uint,C.POINTER(self.State)];self.lib.XInputGetState.restype=C.c_uint
    def detect(self,kind='xinput',index=0):
        names=[];self.handle=None
        for i in range(4):
            state=self.State()
            if self.lib.XInputGetState(i,C.byref(state))==0:
                names.append('XInput slot '+str(i+1))
                if i==index:self.handle=i+1
        return names
    def buttons(self):
        if self.handle is None:return []
        state=self.State()
        if self.lib.XInputGetState(self.handle-1,C.byref(state))!=0:return []
        mapping={0x20:0,0x40:1,0x80:2,0x10:3,1:4,8:5,2:6,4:7,0x100:10,0x200:11,0x8000:12,0x2000:13,0x1000:14,0x4000:15}
        buttons=[action for mask,action in mapping.items() if state.gamepad.buttons&mask]
        if state.gamepad.lt>30:buttons.append(8)
        if state.gamepad.rt>30:buttons.append(9)
        return buttons
    def close(self):self.handle=None

class ControllerPanel(ttk.Frame):
    def __init__(self,parent,home):
        super().__init__(parent,padding=12);self.home=Path(home);self.reader=None;self.listening=None;self.previous=set()
        self.vars={};self.status=tk.StringVar(value='Choose your controller type, then Detect controller.')
        top=ttk.Frame(self);top.pack(fill='x')
        ttk.Label(top,text='Controller type').pack(side='left')
        self.kind=tk.StringVar(value='PS4 DualShock 4');ttk.Combobox(top,textvariable=self.kind,values=list(TYPES),state='readonly',width=31).pack(side='left',padx=8)
        ttk.Label(top,text='Device number').pack(side='left');self.device=tk.IntVar(value=1);ttk.Spinbox(top,from_=1,to=4,textvariable=self.device,width=4).pack(side='left',padx=6)
        ttk.Button(top,text='Detect controller',command=self.detect).pack(side='left',padx=6)
        ttk.Label(self,textvariable=self.status,wraplength=1000).pack(anchor='w',pady=8)
        ttk.Label(self,text='PS3 button → physical controller button. Choose a binding or click Listen and press the button.').pack(anchor='w')
        grid=ttk.Frame(self);grid.pack(fill='x',pady=6)
        for i,action in enumerate(ACTIONS):
            column=(i//8)*3;row=i%8
            ttk.Label(grid,text=action,width=13).grid(row=row,column=column,sticky='w',padx=(0,4),pady=2)
            var=tk.StringVar(value=action);self.vars['map_'+str(i)]=var
            ttk.Combobox(grid,textvariable=var,values=['Unbound']+ACTIONS,state='readonly',width=16).grid(row=row,column=column+1,padx=4)
            ttk.Button(grid,text='Listen',command=lambda n=i:self.listen(n)).grid(row=row,column=column+2,padx=(0,18))
        options=ttk.Frame(self);options.pack(fill='x',pady=6)
        ttk.Label(options,text='Stick deadzone %').pack(side='left');self.deadzone=tk.DoubleVar();ttk.Spinbox(options,from_=0,to=60,increment=1,textvariable=self.deadzone,width=5).pack(side='left',padx=6)
        ttk.Label(options,text='Trigger threshold %').pack(side='left');self.threshold=tk.DoubleVar();ttk.Spinbox(options,from_=0,to=99,increment=1,textvariable=self.threshold,width=5).pack(side='left',padx=6)
        self.invert_left=tk.BooleanVar();self.invert_right=tk.BooleanVar();self.rumble=tk.BooleanVar()
        for label,var in [('Invert left Y',self.invert_left),('Invert right Y',self.invert_right),('Rumble',self.rumble)]:ttk.Checkbutton(options,text=label,variable=var).pack(side='left',padx=6)
        row=ttk.Frame(self);row.pack(fill='x',pady=8)
        ttk.Button(row,text='Save controller profile',command=self.save).pack(side='left',padx=4)
        ttk.Button(row,text='Reset to defaults',command=lambda:self.populate(defaults())).pack(side='left',padx=4)
        ttk.Label(self,text='Keyboard defaults: arrows = D-pad/left stick, Z/X/A/S = Cross/Circle/Square/Triangle, Q/W = L1/R1, 1/2 = L2/R2, Enter = Start, Tab = Select.\nThese settings apply to controller-enabled native builds. RPCS3 has its own controller settings. They do not resolve MotorStorm’s current startup stall.',wraplength=1000).pack(anchor='w')
        self.populate(load_profile(home,self.status.set));self.kind.trace_add('write',self.selection_changed);self.device.trace_add('write',self.selection_changed)
        self.bind('<Destroy>',self.destroyed,add='+');self.after(50,self.poll)
    def selection_changed(self,*args):
        if self.reader:self.reader.close()
        self.reader=None;self.listening=None
        self.status.set('Controller selection changed. Click Detect controller before using Listen.')
    def populate(self,p):
        self.kind.set(next(k for k,v in TYPES.items() if v==p['type']));self.device.set(int(p['device'])+1)
        for i in range(16):self.vars['map_'+str(i)].set(ACTIONS[int(p['map_'+str(i)])] if int(p['map_'+str(i)])>=0 else 'Unbound')
        self.deadzone.set(round(int(p['deadzone'])*100/32767,1));self.threshold.set(round(int(p['trigger_threshold'])*100/255,1))
        self.invert_left.set(p['invert_left_y']=='1');self.invert_right.set(p['invert_right_y']=='1');self.rumble.set(p['rumble']=='1')
    def save(self):
        try:
            p=dict(type=TYPES[self.kind.get()],device=str(self.device.get()-1),deadzone=str(round(self.deadzone.get()*32767/100)),trigger_threshold=str(round(self.threshold.get()*255/100)),invert_left_y=str(int(self.invert_left.get())),invert_right_y=str(int(self.invert_right.get())),rumble=str(int(self.rumble.get())))
            for i in range(16):p['map_'+str(i)]=str(ACTIONS.index(self.vars['map_'+str(i)].get()) if self.vars['map_'+str(i)].get()!='Unbound' else -1)
            path=save_profile(self.home,p);self.status.set('Profile saved. Controller-enabled native builds read this profile: '+str(path))
        except (ValueError,OSError,tk.TclError) as error:self.status.set('Could not save: '+str(error))
    def detect(self):
        try:
            if self.reader:self.reader.close()
            self.listening=None;self.previous=set()
            kind=TYPES[self.kind.get()]
            if kind=='keyboard':
                self.reader=None;self.status.set('Keyboard uses the listed default keys.');return
            if kind in ('xinput','auto'):
                self.reader=XInputController();names=self.reader.detect('xinput',self.device.get()-1)
                if kind=='auto' and not self.reader.handle:
                    self.reader=SDLController();names=self.reader.detect('ds4',self.device.get()-1)
            else:
                self.reader=SDLController();names=self.reader.detect(kind,self.device.get()-1)
            if self.reader.handle:
                if isinstance(self.reader,XInputController):selected='XInput slot '+str(self.device.get())
                else:selected=names[self.device.get()-1]
                self.status.set('Selected: '+selected+' · device '+str(self.device.get())+'. Ready for Listen. Save the profile to apply your bindings.')
            elif names:
                self.status.set('Device '+str(self.device.get())+' is not connected. Available matching devices: '+', '.join(names))
            elif getattr(self.reader,'devices',[]):
                detected=', '.join(d['name']+' ('+format(d['vendor'],'04X')+':'+format(d['product'],'04X')+')' for d in self.reader.devices)
                self.status.set('No Sony DualShock 4 detected. Found: '+detected+'. For a third-party or PS4-style pad, select Other / PS4-style SDL controller, device 1, then Detect controller.')
            else:self.status.set('No matching controller detected. Connect it by USB or Bluetooth, then Detect controller.')
        except Exception as error:self.status.set('Controller detection failed: '+str(error))
    def listen(self,n):
        if not self.reader or not self.reader.handle:self.detect()
        if not self.reader or not self.reader.handle:return
        self.listening=n;self.previous=set(self.reader.buttons());self.status.set('Press a controller button for '+ACTIONS[n])
    def poll(self):
        if self.listening is not None and self.reader:
            pressed=set(self.reader.buttons());new=pressed-self.previous;self.previous=pressed
            if new:
                physical=min(new);self.vars['map_'+str(self.listening)].set(ACTIONS[physical]);self.status.set(ACTIONS[self.listening]+' mapped to '+ACTIONS[physical]+'. Save the profile to keep it.');self.listening=None
        self.after(50,self.poll)
    def destroyed(self,event):
        if event.widget==self and self.reader:self.reader.close()

def main():
    home=Path(os.environ.get('RETROREMAKES_PS3_HOME',ROOT/'workspace'))
    import sys
    root=tk.Tk();root.title('PS3 Studio - Controller setup (updated)' if '--updated' in sys.argv else 'PS3 Studio - Controller setup');root.geometry('1050x580')
    ControllerPanel(root,home).pack(fill='both',expand=True);root.mainloop()
if __name__=='__main__':main()
