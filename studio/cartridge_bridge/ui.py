# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Optional, console-scoped Cartridge panel. No network calls during startup."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import uuid
import webbrowser
from . import (UPSTREAM, catalog, repo_url, state_path, load_state, save_state,
               inspect, convert_n64, launch_command)


class CartridgePanel(ttk.Frame):
    def __init__(self, notebook, console, host=None):
        super().__init__(notebook, padding=12)
        notebook.add(self, text='Fulton library')
        self.console, self.host = console, host
        self.path = state_path(console)
        self.settings = load_state(self.path)
        installed = Path(os.environ.get('ProgramFiles',r'C:\Program Files'))/'Fulton/Fulton.exe'
        if not self.settings.get('launcher') and installed.is_file(): self.settings['launcher']=str(installed)
        self.jobs = queue.Queue()
        self.working = False
        self.process = None
        self.query = tk.StringVar(master=self)
        ttk.Label(self, text='Cartridge · existing ports & local builds', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
        ttk.Label(self, text='Browse the bundled catalog for this console, inspect game files, or open the full Cartridge launcher.\nCatalog availability does not restrict Studio build attempts. Local builds remain unverified.', wraplength=850).pack(anchor='w', pady=6)
        row = ttk.Frame(self); row.pack(fill='x')
        for label, fn in [('Get Fulton', lambda: webbrowser.open('https://github.com/TriwindBryan/fulton/releases/latest')),
                          ('Choose launcher', self.choose_launcher), ('Open Fulton', self.open_launcher),
                          ('Full community catalog', lambda: webbrowser.open(UPSTREAM+'#the-catalog'))]:
            ttk.Button(row, text=label, command=lambda f=fn: self.safe(f)).pack(side='left', padx=(0, 6))
        entry = ttk.Entry(self, textvariable=self.query); entry.pack(fill='x', pady=8)
        self.query.trace_add('write', lambda *_: self.refresh())
        wrap = ttk.Frame(self); wrap.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(wrap, columns=('title','port','kind'), show='headings', height=6)
        for key, width in [('title',300),('port',300),('kind',200)]:
            self.tree.heading(key, text=key.title()); self.tree.column(key, width=width)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll=ttk.Scrollbar(wrap, command=self.tree.yview);scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        row = ttk.Frame(self); row.pack(fill='x', pady=6)
        for label, fn in [('Open port project', self.open_project), ('Find ports by title', self.search_title),
                          ('Inspect game file', lambda: self.pick_inspect(False)), ('Inspect game folder', lambda: self.pick_inspect(True))]:
            ttk.Button(row, text=label, command=lambda f=fn: self.safe(f)).pack(side='left', padx=(0, 6))
        if console == 'n64':
            ttk.Button(row, text='Save .z64 copy', command=lambda: self.safe(self.convert)).pack(side='left')
        ttk.Label(self, text='Your local native builds · selecting or running a file does not approve gameplay').pack(anchor='w', pady=(6,2))
        self.builds = tk.Listbox(self, height=3, exportselection=False)
        self.builds.pack(fill='x')
        row = ttk.Frame(self); row.pack(fill='x', pady=5)
        for label, fn in [('Add local executable', self.add_build), ('Launch unverified build', self.launch_build),
                          ('Stop this build', self.stop_build), ('Remove reference', self.remove_build)]:
            ttk.Button(row, text=label, command=lambda f=fn: self.safe(f)).pack(side='left', padx=(0,6))
        self.output = tk.Text(self, height=7, wrap='word', state='disabled')
        self.output.pack(fill='both', expand=True)
        self.refresh(); self.refresh_builds()
        self.show('Bundled upstream catalog; no automatic downloads. Choose a port project for its current releases.\nMulti-console inspection uses Node.js LTS when installed; N64 inspection/conversion needs only Python.\nFor Cartridge: add the port in its catalog, then use “Add your copy” to select your locally built executable. This panel does not edit Cartridge’s private settings.')
        self.after(150, self.poll)
        self.bind('<Destroy>', self.on_destroy, add='+')

    def on_destroy(self, event):
        if event.widget == self:
            self.stop_build()

    def safe(self, fn):
        try: return fn()
        except Exception as e: messagebox.showerror('Cartridge', str(e), parent=self)

    def show(self, text):
        self.output.configure(state='normal'); self.output.delete('1.0','end')
        self.output.insert('end', text); self.output.configure(state='disabled')

    def save(self): save_state(self.path, self.settings)

    def pick(self, purpose, folder=False, save=False, **kwargs):
        initial = Path(self.settings.get('folders',{}).get(purpose, str(Path.home())))
        while not initial.is_dir() and initial.parent != initial: initial=initial.parent
        method = filedialog.askdirectory if folder else filedialog.asksaveasfilename if save else filedialog.askopenfilename
        value=method(parent=self, initialdir=str(initial), **kwargs)
        if value:
            self.settings.setdefault('folders',{})[purpose] = value if folder else str(Path(value).parent)
            self.save()
        return value

    def refresh(self):
        self.entries = catalog(self.console, self.query.get())
        self.tree.delete(*self.tree.get_children())
        for i,g in enumerate(self.entries):
            self.tree.insert('', 'end', iid=str(i), values=(g['title'],g.get('port',''),g.get('kind','')))
        if not self.entries:
            self.tree.insert('', 'end', iid='empty', values=('No entries. Is Cartridge downloaded? See REQUIREMENTS.md.','Use Find ports by title or full Cartridge.',''))

    def selected_game(self):
        ids=self.tree.selection()
        if not ids or ids[0]=='empty': raise ValueError('Select a port from the catalog.')
        return self.entries[int(ids[0])]

    def open_project(self): webbrowser.open(repo_url(self.selected_game()))

    def search_title(self):
        from urllib.parse import quote
        title=self.query.get().strip() or simpledialog.askstring('Find existing ports','Game title:',parent=self)
        if title:
            webbrowser.open('https://github.com/search?type=repositories&q='+quote(title+' (decomp OR recomp OR native port)'))

    def choose_launcher(self):
        value=self.pick('launcher', title='Select installed Cartridge executable')
        if value:
            self.settings['launcher']=value; self.save(); self.show('Launcher saved: '+value)

    def open_launcher(self):
        value=self.settings.get('launcher')
        if not value: raise ValueError('Get Fulton, install it, then Choose launcher once.')
        args,cwd=launch_command(value)
        subprocess.Popen(args,cwd=cwd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.show('Opened Cartridge. Its install/update/save management runs in its own window.')

    def background(self, fn):
        if self.working: raise ValueError('An inspection is running; wait for it to finish.')
        self.working=True; self.show('Inspecting…')
        def work():
            try: self.jobs.put(('result', fn()))
            except Exception as e: self.jobs.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def pick_inspect(self, folder):
        if self.working: raise ValueError('Wait for the current inspection.')
        value=self.pick('input-folder' if folder else 'input-file', folder=folder, title='Select game input (read only)')
        if value: self.background(lambda:inspect(value,self.console))

    def convert(self):
        if self.working: raise ValueError('Wait for the current inspection.')
        source=self.pick('input-file',title='Select N64 ROM to convert',filetypes=[('N64 ROM','*.n64 *.v64 *.z64')])
        if not source:return
        destination=self.pick('converted-rom',save=True,title='Save a new .z64 copy',defaultextension='.z64',filetypes=[('N64 ROM','*.z64')])
        if destination:self.background(lambda:convert_n64(source,destination))

    def refresh_builds(self):
        self.builds.delete(0,'end')
        for value in self.settings.get('builds',[]):self.builds.insert('end',value)

    def add_build(self):
        value=self.pick('local-build',title='Select a native game executable')
        if not value:return
        # Validate the host executable shape without launching or changing it.
        launch_command(value)
        builds=self.settings.setdefault('builds',[])
        if value not in builds:builds.append(value)
        self.save();self.refresh_builds()
        self.show('Saved local executable reference. No game files were copied. Use Launch unverified build to test, or Cartridge → Add your copy for a catalog port.')

    def selected_build(self):
        ids=self.builds.curselection()
        if not ids:raise ValueError('Select a local executable.')
        return self.settings['builds'][ids[0]]

    def remove_build(self):
        value=self.selected_build()
        self.settings['builds'].remove(value);self.save();self.refresh_builds()

    def launch_build(self):
        if self.process and self.process.poll() is None:raise ValueError('A local build is already running in this panel.')
        if self.host and getattr(self.host,'busy',False):raise ValueError('Wait for the current Studio operation to finish.')
        value=self.selected_build();args,cwd=launch_command(value)
        logs=self.path.parent/'runs';logs.mkdir(parents=True,exist_ok=True)
        self.receipt=logs/(uuid.uuid4().hex+'.json')
        self.run_record={'executable':value,'status':'launched','gameplay_verified':False,'time_limit_seconds':None}
        log=self.receipt.with_suffix('.log')
        with log.open('wb') as output:
            self.process=subprocess.Popen(args,cwd=cwd,stdout=output,stderr=subprocess.STDOUT,
                start_new_session=os.name!='nt',creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
        self.run_record.update(pid=self.process.pid,log=str(log))
        save_state(self.receipt,self.run_record)
        self.show('Launched unverified native build without a time limit.\nLog: '+str(log))

    def stop_build(self):
        if self.process and self.process.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(self.process.pid),'/T','/F'],capture_output=True,timeout=15)
            else:
                try:os.killpg(self.process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            self.run_record['stop_requested']=True

    def poll(self):
        try:
            while True:
                kind,value=self.jobs.get_nowait();self.working=False
                self.show(json.dumps(value,indent=2,ensure_ascii=False) if kind=='result' else 'Inspection failed: '+value)
        except queue.Empty:pass
        if self.process and self.process.poll() is not None:
            self.run_record.update(status='exited',exit_code=self.process.returncode)
            save_state(self.receipt,self.run_record)
            self.show('Local build exited with code '+str(self.process.returncode)+'. Gameplay remains unverified.\nReceipt: '+str(self.receipt))
            self.process=None
        self.after(200,self.poll)
