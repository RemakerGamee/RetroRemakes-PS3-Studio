# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
from __future__ import annotations

import json
import copy
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import VERSION
from .core import ROOT, Studio, Cancelled


def scan_summary(result):
    return (f"Games scan: {result.get('files_found',0)} ELF/EBOOT files, {result.get('found',0)} unique games/inputs; "
            f"{len(result['added'])} added, {len(result['duplicates'])} associated copies/existing games, "
            f"{len(result['errors'])} errors")


def input_inventory_rows(files):
    rows=[]
    for item in files:
        readiness=('Error' if item.get('error') else 'PPU ELF ready' if item.get('native_input') else
                   'Encrypted SELF' if item.get('format')=='SELF' else 'Other ELF / unsupported input')
        details=item.get('error') or ('Identical content: '+item['duplicate_of'] if item.get('duplicate_of') else item.get('duplicate_reason',''))
        rows.append((item.get('path',item.get('elf','')),item.get('format','unknown'),readiness,
                     item.get('disposition','associated'),details))
    return sorted(rows,key=lambda row:row[0].casefold())


def queue_item_label(item):
    verb={'build':'Building','rebuild':'Rebuilding','lift':'Generating sources for','analyse':'Inspecting'}.get(item['action'],'Processing')
    state=item.get('state','running')
    if state!='running':verb={'done':'Completed','failed':'Blocked','cancelled':'Cancelled'}.get(state,verb)
    return f"{verb} {item['index']+1}/{item['total']}: {item['name']}"


def queue_completion_label(item):
    state='cancelled' if item.get('cancelled') else 'finished'
    text=f"Queue {state}: {item['attempted']}/{item['total']} attempted"
    if item.get('failures'):text+=f" \u00b7 {item['failures']} blocked"
    return text


def manual_session_label(session):
    name=session.get('candidate_label') or session.get('project_name') or session['project_id']
    kind='Native test' if session.get('kind')=='native' else 'RPCS3'
    return f"{kind}: {name} - {session['status']} - PID {session['pid']} [{session['session_id'][:8]}]"


from contextlib import nullcontext as _nullcontext


def _decrypt_feature():
    """The private-build EBOOT decrypter, or None in a public build."""
    try:
        from .private_features import self_decrypt
        return self_decrypt
    except ImportError:
        return None

class App(tk.Tk):
    def __init__(self, home=None):
        super().__init__()
        self.title(f"RetroRemakes · PS3 Port Studio {VERSION} · Public Edition")
        self.geometry(f"{min(1180, self.winfo_screenwidth()-50)}x{min(800, self.winfo_screenheight()-80)}")
        self.minsize(960, 620)
        self.configure(bg="#edf6fc")
        self.studio = Studio(home)
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.busy = False
        self.worker = None
        self.manual_launch_busy=False;self.manual_launch_worker=None;self.manual_stop_worker=None
        self.test_sessions={};self.test_session_labels={}
        self.fix_busy=False;self.fix_worker=None;self.fix_cancel=threading.Event()
        self.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#edf6fc")
        style.configure("TLabel", background="#edf6fc", foreground="#173a56", font=("Segoe UI", 10))
        style.configure("TButton", padding=(12, 7), font=("Segoe UI", 10))
        style.configure("Accent.TButton", background="#9ed9fa", foreground="#10314a")
        style.configure("Treeview", rowheight=32, font=("Segoe UI", 10), fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#b2e1fc")], foreground=[("selected", "#10314a")])
        top = ttk.Frame(self, padding=(24, 18))
        top.pack(fill="x")
        ttk.Label(top, text="RETROREMAKES", font=("Segoe UI", 11, "bold"), foreground="#2178aa").pack(anchor="w")
        ttk.Label(top, text="PS3 Port Studio", font=("Segoe UI", 27, "bold")).pack(anchor="w")
        ttk.Label(top, text="Inspect → lift PPU + SPU → compile → test and refine").pack(anchor="w", pady=(6, 0))
        self.status = tk.StringVar(value="Experimental native porting workbench · no game tested yet")
        ttk.Label(self, textvariable=self.status, padding=(24, 8), foreground="#286c91").pack(fill="x")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self.projects_tab()
        self.compiled_tab()
        self.tools_tab()
        self.settings_tab()
        from .controllers import ControllerPanel
        controller_tab=self.frame("Controllers")
        self.controllers=ControllerPanel(controller_tab,self.studio.home)
        self.controllers.pack(fill="both",expand=True)
        self.updates_tab()
        self.research_tab()
        self.fulton_tab()
        from .fix_assistant_ui import FixAssistantPanel
        self.fix_assistant=FixAssistantPanel(self.frame("Fix assistant"),self)
        self.fix_assistant.pack(fill="both",expand=True)
        from .cartridge_bridge.ui import CartridgePanel
        self.cartridge_panel=CartridgePanel(self.tabs,'ps3',self)
        self.refresh()
        self.after(500, self.start_fulton_sync)
        self.after(120, self.poll)
        self.after(750,self.watch_manual_sessions)
        self.after(1000,self.watch_repair_progress)
        self.batch_try=None
        from .batch_queue import POLL_MS
        self.after(POLL_MS,self.poll_queue_requests)
        self.auto_loop=None
        self.bind("<F5>", lambda e: self.selected_job("analyse"))
        self.bind("<F6>", lambda e: self.selected_job("build"))
        self.bind("<Escape>", lambda e: self.stop())
        if self.studio.state['settings'].get('check_updates_on_start', False):
            self.after(1500, self.check_updates)

    def frame(self, name):
        frame = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(frame, text=name)
        return frame

    def button(self, parent, text, command, accent=False):
        b = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        b.pack(side="left", padx=(0, 6), pady=3)
        return b

    def projects_tab(self):
        f = self.frame("Game queue & build")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.button(row, "+ Game folder", self.add_folder, True)
        self.button(row, "Add games folder", self.scan_games_folder)
        self.button(row, "Rescan all game folders", self.rescan_all_game_folders)
        self.button(row, "Remove a games folder", self.remove_games_folder)
        if _decrypt_feature():
            self.button(row, "Decrypt all games", self.decrypt_all_games)
        self.button(row, "Found ELF / EBOOT files", self.show_found_inputs)
        self.button(row, "+ ELF / EBOOT", self.add_file)
        self.button(row, "Set decrypted ELF", self.set_elf)
        self.button(row, "Game patches...", self.game_patches_dialog)
        self.button(row, "Update games", self.update_games)
        self.button(row, "Update to latest", self.update_games_latest)
        self.button(row, "Display settings...", self.display_settings_dialog)
        self.button(row, "Remove from queue", self.remove)
        cleanup = ttk.Frame(f); cleanup.pack(fill="x")
        self.button(cleanup, "Batch import ELF files", self.batch_import_elfs, True)
        self.button(cleanup, "Remove all compiled games from queue", self.remove_compiled)
        tree_wrap = ttk.Frame(f)
        tree_wrap.pack(fill="x", pady=12)
        self.tree = ttk.Treeview(tree_wrap, columns=("game", "status"), show="headings", selectmode="extended", height=3)
        self.tree.heading("game", text="Game / project")
        self.tree.heading("status", text="Latest result")
        self.tree.column("game", width=540)
        self.tree.column("status", width=300)
        self.tree.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.button(row, "Inspect · F5", lambda: self.selected_job("analyse"))
        self.button(row, "Generate sources", lambda: self.selected_job("lift"))
        self.button(row, "Attempt native build · F6", lambda: self.selected_job("build"), True)
        self.button(row, "Build entire queue", self.build_queue)
        self.button(row, "Stop build", self.stop)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.button(row, "Test native build", lambda: self.launch(True), True)
        self.button(row, "Test in RPCS3", lambda: self.launch(False))
        self.button(row, "Open project", self.open_project)
        self.button(row, "Mod workspace", self.open_mods)
        self.button(row, "Export diagnostics", self.export)
        row=ttk.Frame(f);row.pack(fill='x')
        self.button(row,'Stop test',self.stop_test)
        self.button(row,'Open test log',self.open_test_log)
        self.button(row,'Apply fixes to games',self.apply_game_fixes)
        self.button(row,'Apply fixes to selected game',self.apply_fixes_selected)
        self.button(row,'Batch try built games',self.batch_try_all)
        self.button(row,'Stop batch try',self.stop_batch_try)
        self.button(row,'Auto loop: on/off',self.toggle_auto_loop)
        self.button(row,'Stop fixes',self.stop_fixes)
        self.test_session_var=tk.StringVar(value='')
        self.test_session_picker=ttk.Combobox(row,textvariable=self.test_session_var,state='readonly',width=42)
        self.test_session_picker.pack(side='left',fill='x',expand=True,padx=6)
        self.test_session_picker.bind('<<ComboboxSelected>>',lambda event:self.manual_status.set(self.test_session_var.get()))
        self.manual_status=tk.StringVar(value='Manual test: idle - independent of the build queue')
        ttk.Label(f,textvariable=self.manual_status,wraplength=1050).pack(anchor='w')
        self.fix_status=tk.StringVar(value='Fix preparation: idle - creates a separate candidate')
        ttk.Label(f,textvariable=self.fix_status,wraplength=1050).pack(anchor='w')
        self.add_repair_progress(f)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.button(row, "Rebuild existing attempt (keep fixes)", lambda: self.selected_job("rebuild"))
        self.button(row, "Retry blocked games (keep sources)", self.retry_blocked)
        self.button(row, "Build blocked games anyway", self.build_blocked_anyway)
        ttk.Label(row, text="Edit generated code / stubs in the project folder, then rebuild here.").pack(side="left", padx=6)
        ttk.Label(f, text="A compiled executable still needs gameplay validation. RPCS3 is a separate reference test.",
                  wraplength=900).pack(anchor="w", pady=(10, 6))
        self.current_queue_item=tk.StringVar(value='Queue: idle')
        ttk.Label(f,textvariable=self.current_queue_item,font=('Segoe UI',11,'bold'),wraplength=1050).pack(anchor='w',pady=(0,6))
        self.overall_progress_text=tk.StringVar(value='Overall 0.0% · ready')
        self.step_progress_text=tk.StringVar(value='Current step: waiting for a job · 0.0%')
        self.progress_detail=tk.StringVar(value='Overall percentage counts build stages; it is not a time estimate.')
        ttk.Label(f,textvariable=self.overall_progress_text).pack(anchor='w')
        self.overall_progress=ttk.Progressbar(f,mode='determinate',maximum=100)
        self.overall_progress.pack(fill='x',pady=(0,4))
        ttk.Label(f,textvariable=self.step_progress_text).pack(anchor='w')
        ttk.Label(f,textvariable=self.progress_detail,wraplength=1050).pack(anchor='w')
        self.progress = ttk.Progressbar(f, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 6))
        self.log = tk.Text(f, bg="#102a40", fg="#d9f0ff", insertbackground="white", font=("Consolas", 10),
                           wrap="word", relief="flat", height=12, padx=12, pady=12)
        self.log.pack(fill="both", expand=True)
        self.log.insert("end", "Ready. Add a game folder or decrypted EBOOT.ELF.\nSet up the backend under Settings, then inspect and attempt a build.\nBuild output and measured progress update live; complete logs remain in the project folder.\n")
        self.log.configure(state="disabled")

    def add_repair_progress(self,parent):
        # Top pair: "Apply fixes to games" (whole library). Bottom pair: selected game.
        if 'repair_all_text' not in self.__dict__:
            self.repair_all_text=tk.StringVar(value='Apply fixes to games: idle')
            self.repair_all_overall_text=tk.StringVar(value='All games overall: waiting')
            self.repair_all_step_bars=[];self.repair_all_overall_bars=[]
        ttk.Label(parent,textvariable=self.repair_all_text,wraplength=1050).pack(anchor='w')
        bar=ttk.Progressbar(parent,mode='determinate',maximum=100);bar.pack(fill='x',pady=(0,3));self.repair_all_step_bars.append(bar)
        ttk.Label(parent,textvariable=self.repair_all_overall_text,wraplength=1050).pack(anchor='w')
        bar=ttk.Progressbar(parent,mode='determinate',maximum=100);bar.pack(fill='x',pady=(0,6));self.repair_all_overall_bars.append(bar)
        if 'repair_progress_text' not in self.__dict__:
            self.repair_progress_text=tk.StringVar(value='Selected game fix: idle (separate from game window)')
            self.repair_overall_text=tk.StringVar(value='Selected game overall: waiting')
            self.repair_detail=tk.StringVar(value='Percentages measure source bytes and build stages, not remaining time.')
            self.repair_step_bars=[];self.repair_overall_bars=[]
        ttk.Label(parent,textvariable=self.repair_progress_text,wraplength=1050).pack(anchor='w')
        bar=ttk.Progressbar(parent,mode='determinate',maximum=100);bar.pack(fill='x',pady=(0,3));self.repair_step_bars.append(bar)
        ttk.Label(parent,textvariable=self.repair_overall_text,wraplength=1050).pack(anchor='w')
        bar=ttk.Progressbar(parent,mode='determinate',maximum=100);bar.pack(fill='x',pady=(0,3));self.repair_overall_bars.append(bar)
        ttk.Label(parent,textvariable=self.repair_detail,wraplength=1050).pack(anchor='w')

    def show_repair_progress(self,value,target='selected'):
        percent=value.get('step_percent');overall=value.get('overall_percent')
        name=value.get('project_name',value.get('project_id','Game'))
        if target=='all':
            done,total=getattr(self,'_all_fix_count',(0,0))
            self.repair_all_text.set(f"Apply fixes to games: {name} | {value.get('stage','Waiting')}"+
                                     (f" | {percent:.1f}%" if percent is not None else ''))
            whole=((done+(overall or 0)/100)/total*100) if total else (overall or 0)
            self.repair_all_overall_text.set(f'All games overall: game {min(done+1,total)}/{total} | {whole:.1f}%')
            for bar in self.repair_all_step_bars:
                bar.stop()
                if percent is None and value.get('status')=='running':bar.configure(mode='indeterminate');bar.start(12)
                else:bar.configure(mode='determinate');bar['value']=percent or 0
            for bar in self.repair_all_overall_bars:bar['value']=whole
            return
        self.repair_progress_text.set(f"Selected game fix: {name} | {value.get('stage','Waiting')}"+
                                      (f" | {percent:.1f}%" if percent is not None else ' | percentage not reported'))
        self.repair_overall_text.set('Selected game overall: '+(f'{overall:.1f}%' if overall is not None else 'not reported')+
                                    ' | phase-based (source preparation + native build), not time')
        self.repair_detail.set(value.get('detail',''))
        for bar in self.repair_step_bars:
            bar.stop()
            if percent is None and value.get('status')=='running':bar.configure(mode='indeterminate');bar.start(12)
            else:bar.configure(mode='determinate');bar['value']=percent or 0
        for bar in self.repair_overall_bars:bar['value']=overall or 0

    def watch_repair_progress(self):
        live=self.fix_busy or getattr(self,'single_fix_busy',False)
        if not live and not getattr(self,'repair_watch_busy',False):
            self.repair_watch_busy=True
            ids=[project['id'] for project in self.studio.state.get('projects',[])]
            def work():
                try:
                    from .game_fixes import read_progress
                    rows=read_progress(self.studio.home,ids)
                    active=[row for row in rows if row.get('status') in ('running','unknown')]
                    for row in (active or rows[:1]):
                        self.events.put(('repair_watch',row))
                    if not (active or rows):self.events.put(('repair_watch',None))
                except Exception:
                    self.events.put(('repair_watch',None))
            threading.Thread(target=work,daemon=True).start()
        self.after(1000,self.watch_repair_progress)

    def compiled_tab(self):
        self.compiled_frame = self.frame("Compiled / built games")
        f = self.compiled_frame
        ttk.Label(f, text="Finished builds available for testing. Compilation does not confirm playable gameplay.").pack(anchor="w", pady=8)
        row = ttk.Frame(f); row.pack(fill="x")
        self.button(row, "Refresh built games", self.refresh_compiled_async, True)
        self.button(row, "Select output folder", self.select_compiled_output_folder, True)
        from .compiled_library import selected_folder
        self.compiled_output_folder = tk.StringVar(value=selected_folder(self.studio) or "Studio output folders (automatic)")
        ttk.Label(f, textvariable=self.compiled_output_folder, wraplength=1050).pack(anchor="w")
        self.compiled_status = tk.StringVar(value="Ready to check finished builds.")
        ttk.Label(row, textvariable=self.compiled_status).pack(side="left", padx=8)
        self.compiled_tree = ttk.Treeview(f, columns=("game", "result", "queue"), show="headings", selectmode="extended", height=12)
        for key, title in (("game", "Game"), ("result", "Build result"), ("queue", "Build queue")):
            self.compiled_tree.heading(key, text=title)
        self.compiled_tree.pack(fill="both", expand=True)
        row = ttk.Frame(f); row.pack(fill="x")
        self.button(row, "Test native build", lambda: self.launch(True), True)
        self.button(row, "Test in RPCS3", lambda: self.launch(False))
        self.button(row, "Open project", self.open_project)
        self.button(row, "Add selected back to queue", self.restore_compiled)
        row = ttk.Frame(f); row.pack(fill="x")
        self.button(row, "Remove selected from compiled list", self.hide_compiled_selection)
        self.button(row, "Show removed games", self.show_removed_compiled)
        row = ttk.Frame(f); row.pack(fill="x")
        self.button(row, "Apply fixes to games", self.apply_game_fixes)
        self.button(row, "Apply fixes to selected game", self.apply_fixes_selected)
        self.button(row, "Export port folder", self.export_port_folder)
        self.button(row, "Clean up old builds", self.clean_up_old_builds)
        self.button(row, "Display settings...", self.display_settings_dialog)
        self.button(row, "Stop test", self.stop_test)
        self.button(row, "Open test log", self.open_test_log)
        ttk.Label(f, textvariable=self.manual_status, wraplength=1050).pack(anchor="w")
        ttk.Label(f, textvariable=self.fix_status, wraplength=1050).pack(anchor="w")
        self.add_repair_progress(f)
        self.restore_cached_compiled_rows()

    def hide_compiled_selection(self):
        ids = list(self.compiled_tree.selection())
        if not ids:
            self.compiled_status.set("Select one or more games in the compiled list first.")
            return
        from .compiled_library import set_hidden_projects, hidden_project_ids
        set_hidden_projects(self.studio, ids, hidden=True)
        self._compiled_hidden_ids = set(hidden_project_ids(self.studio))
        for ident in ids:
            if self.compiled_tree.exists(ident):
                self.compiled_tree.delete(ident)
        self.compiled_status.set(f"Removed {len(ids)} game(s) from this list. Use Show removed games to restore them. Files and queue retained.")
        # A scan already in progress is also filtered at its UI commit below.
        self.refresh_compiled_async()

    def show_removed_compiled(self):
        from .compiled_library import set_hidden_projects
        set_hidden_projects(self.studio, [], hidden=False)
        self._compiled_hidden_ids = set()
        self.refresh_compiled_async()

    def remove_compiled(self):
        from .compiled_queue_cleanup import remove_compiled_async
        remove_compiled_async(self)

    def restore_compiled(self):
        if self.busy:
            self.status.set("Wait for the active build operation to finish before changing the queue.")
            return
        for project in self.selected():
            project["queue_removed"] = False
        self.studio.save()
        self.refresh()
        self.status.set("Selected games added back to the build queue.")

    def tools_tab(self):
        f = self.frame("Tools & sources")
        ttk.Label(f, text="Integrated backend + curated specialist tools", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(f, text="Catalog entries describe a role, not automatic integration. Select an entry for its limits.").pack(anchor="w", pady=8)
        self.catalog = json.loads((ROOT / "tool-catalog.json").read_text(encoding="utf-8"))["tools"]
        self.query = tk.StringVar()
        entry = ttk.Entry(f, textvariable=self.query)
        entry.pack(fill="x", pady=(0, 8))
        self.query.trace_add("write", lambda *a: self.filter_tools())
        self.tooltree = ttk.Treeview(f, columns=("name", "role", "integration"), show="headings", height=10)
        for c, t, w in (("name", "Tool", 240), ("role", "Category", 200), ("integration", "Studio support", 340)):
            self.tooltree.heading(c, text=t)
            self.tooltree.column(c, width=w)
        self.tooltree.pack(fill="both", expand=True)
        self.tooltree.bind("<<TreeviewSelect>>", self.describe_tool)
        self.tool_description = tk.Text(f, height=7, wrap="word", font=("Segoe UI", 10), bg="white", relief="flat", padx=12, pady=10)
        self.tool_description.pack(fill="x", pady=8)
        self.tool_description.configure(state="disabled")
        self.button(f, "Open official source / download", self.open_tool, True)
        self.filter_tools()

    def filter_tools(self):
        self.tooltree.delete(*self.tooltree.get_children())
        q = self.query.get().lower()
        for i, t in enumerate(self.catalog):
            if q in json.dumps(t).lower():
                self.tooltree.insert("", "end", iid=str(i), values=(t["name"], t["category"], t["support"]))

    def describe_tool(self, event=None):
        s = self.tooltree.selection()
        if s:
            t = self.catalog[int(s[0])]
            self.tool_description.configure(state="normal")
            self.tool_description.delete("1.0", "end")
            self.tool_description.insert("end", f"{t['name']}\n{t['purpose']}\nLimit: {t['limits']}\n{t['url']}")
            self.tool_description.configure(state="disabled")

    def open_tool(self):
        s = self.tooltree.selection()
        if s:
            webbrowser.open(self.catalog[int(s[0])]["url"])

    def settings_tab(self):
        f = self.frame("Settings & setup")
        ttk.Label(f, text="Set up once, keep every project", font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(0, 12))
        self.backend = tk.StringVar(value=self.studio.state["settings"]["backend"])
        self.rpcs3 = tk.StringVar(value=self.studio.state["settings"].get("rpcs3", ""))
        self.jobs = tk.IntVar(value=self.studio.state["settings"].get("jobs", 2))
        for label, var, directory in (("ps3recomp checkout", self.backend, True), ("RPCS3 executable (your own install: decrypting, firmware and reference tests)", self.rpcs3, False)):
            ttk.Label(f, text=label).pack(anchor="w")
            row = ttk.Frame(f)
            row.pack(fill="x", pady=(4, 12))
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.button(row, "Browse", lambda v=var, d=directory: self.browse_setting(v, d))
        self.firmware_folder = tk.StringVar(value=self.studio.state["settings"].get("firmware_folder", ""))
        self.firmware_status = tk.StringVar(value=self.firmware_description())
        ttk.Label(f, text="PS3 firmware folder (detected automatically at startup)").pack(anchor="w")
        row = ttk.Frame(f); row.pack(fill="x", pady=(4, 6))
        ttk.Entry(row, textvariable=self.firmware_folder).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.button(row, "Browse", self.browse_firmware)
        self.button(row, "Detect firmware", self.detect_firmware)
        ttk.Label(f, textvariable=self.firmware_status, wraplength=850).pack(anchor="w", pady=(0, 8))
        ttk.Label(f, text="Compiler jobs (2 uses less memory for large generated files)").pack(anchor="w")
        ttk.Spinbox(f, from_=1, to=32, textvariable=self.jobs, width=8).pack(anchor="w", pady=8)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        self.button(row, "Save settings", self.save_settings, True)
        self.button(row, "Install backend + Python dependencies", self.install)
        self.button(row, "Check environment", self.doctor)
        ttk.Label(f, text="Windows: Python 3.11+, Git, CMake, Ninja, Visual Studio C++ Build Tools with Clang tools. "
                  "See REQUIREMENTS.md for everything you need to install. START-STUDIO.bat loads the Visual Studio build environment when available. Setup downloads a pinned ps3recomp checkout "
                  "and installs its Python requirements into an isolated environment. Existing checkouts are preserved.",
                  wraplength=850).pack(anchor="w", pady=12)
        ttk.Label(f, text=f"Workspace: {self.studio.home}", wraplength=850).pack(anchor="w", pady=6)
        ttk.Label(f, text="Studio updates are available in the Online updates tab and apply on restart. "
                  "Backend updates use a separate checkout so game builds retain their recorded tool revision.", wraplength=850).pack(anchor="w", pady=6)
        self.button(f, "Backend releases", lambda: webbrowser.open("https://github.com/sp00nznet/ps3recomp/releases"))

    def fulton_tab(self):
        from . import fulton
        f = self.frame("Fulton sources")
        ttk.Label(f, text="Fulton · automatic PS3 sources", font=("Segoe UI",17,"bold")).pack(anchor="w")
        ttk.Label(f, text="Uses the PS3 source projects in your own installed Fulton library. Pull the ones you want, or turn on automatic preparation below. "
                  "Sources without an exact-input adapter remain available for development; fetching is not a working port.", wraplength=850).pack(anchor="w", pady=8)
        self.fulton_auto = tk.BooleanVar(value=self.studio.state['settings'].get('fulton_auto',False))
        ttk.Checkbutton(f, text="Automatically prepare the entire Fulton PS3 source library at startup", variable=self.fulton_auto,
                        command=self.save_fulton_choice).pack(anchor="w")
        self.fulton_cancel=threading.Event()
        self.fulton_worker=None
        self.fulton_status = tk.StringVar(value="Reading installed Fulton library…")
        ttk.Label(f,textvariable=self.fulton_status,wraplength=850).pack(anchor="w",pady=8)
        self.fulton_tree = ttk.Treeview(f,columns=('name','repo','state'),show='headings',height=10)
        self.fulton_tree.heading('name',text='PS3 game');self.fulton_tree.heading('repo',text='Source repository')
        self.fulton_tree.column('name',width=300);self.fulton_tree.column('repo',width=400)
        self.fulton_tree.heading('state',text='Source status');self.fulton_tree.column('state',width=180)
        self.fulton_tree.pack(fill='both',expand=True)
        self.fulton_requirements=tk.StringVar(value='Select a source project to see its Fulton requirements.')
        ttk.Label(f,textvariable=self.fulton_requirements,wraplength=850).pack(anchor='w',pady=6)
        self.fulton_tree.bind('<<TreeviewSelect>>',self.show_fulton_requirements)
        row=ttk.Frame(f);row.pack(fill='x',pady=8)
        self.button(row,'Sync now',lambda:self.start_fulton_sync(True))
        self.button(row,'Pull selected sources',lambda:self.pull_fulton(False))
        self.button(row,'Pull all PS3 sources',lambda:self.pull_fulton(True))
        self.button(row,'Stop source preparation',self.stop_fulton)
        self.show_fulton_entries(fulton.catalog(self.studio))

    def show_fulton_entries(self, entries):
        self.fulton_entries=entries
        self.fulton_tree.delete(*self.fulton_tree.get_children())
        from . import fulton
        for i,e in enumerate(entries):self.fulton_tree.insert('', 'end',iid=str(i),values=(e['name'],e['repo'],fulton.readiness(self.studio,e)))
        self.fulton_status.set(f"{len(entries)} PS3 source projects · library read automatically · cache: {self.studio.home / 'ps3-fulton'}")

    def show_fulton_requirements(self, event=None):
        selected=self.fulton_tree.selection()
        if selected:
            entry=self.fulton_entries[int(selected[0])]
            self.fulton_requirements.set(entry.get('requirements') or 'Fulton has no additional requirements recorded for this source project.')

    def save_fulton_choice(self):
        if self.busy:
            self.fulton_auto.set(self.studio.state['settings'].get('fulton_auto',False))
            raise RuntimeError('Wait for the current operation before changing source settings')
        self.studio.state['settings']['fulton_auto']=bool(self.fulton_auto.get());self.studio.save()
        if self.fulton_auto.get():self.start_fulton_sync()
        else:self.stop_fulton()

    def start_fulton_sync(self, force=False, entries=None):
        if getattr(self,'fulton_syncing',False):return
        if entries is None and not force and not self.fulton_auto.get():return
        self.fulton_syncing=True;self.fulton_cancel.clear()
        self.fulton_status.set('Preparing your Fulton PS3 library…')
        def emit(message):
            self.events.put(('log',str(message)))
            if str(message).startswith('Fulton '):self.events.put(('fulton_progress',str(message)))
        def work():
            from . import fulton
            try:
                result=fulton.prepare_library(self.studio,force,emit,self.fulton_cancel,entries)
                self.events.put(('fulton_ready',result))
            except Cancelled:self.events.put(('fulton_stopped',None))
            except Exception as error:self.events.put(('fulton_error',str(error)))
        self.fulton_worker=threading.Thread(target=work,daemon=True)
        self.fulton_worker.start()

    def stop_fulton(self):
        if getattr(self,'fulton_syncing',False):
            self.fulton_cancel.set()
            self.fulton_status.set('Stopping source preparation; cached projects are retained…')

    def pull_fulton(self, all_sources=False):
        entries=self.fulton_entries if all_sources else [self.fulton_entries[int(i)] for i in self.fulton_tree.selection()]
        if not entries:raise ValueError('Select a Fulton PS3 source project first')
        self.start_fulton_sync(True,entries)

    def updates_tab(self):
        from .updater import CHANNEL_URL
        f = self.frame('Online updates')
        ttk.Label(f, text='Keep PS3 Studio up to date', font=('Segoe UI',17,'bold')).pack(anchor='w',pady=(0,12))
        ttk.Label(f, text=f'Installed version: {VERSION}').pack(anchor='w')
        self.update_status = tk.StringVar(value='Ready to check the RetroRemakes update channel')
        ttk.Label(f, textvariable=self.update_status, wraplength=850).pack(anchor='w',pady=12)
        self.auto_updates = tk.BooleanVar(value=self.studio.state['settings'].get('check_updates_on_start',False))
        ttk.Checkbutton(f, text='Check for updates when Studio opens',variable=self.auto_updates,command=self.save_update_preference).pack(anchor='w',pady=8)
        row=ttk.Frame(f);row.pack(fill='x',pady=12)
        self.check_update_button=self.button(row,'Check for updates',self.check_updates,True)
        self.download_update_button=self.button(row,'Download update',self.download_update)
        self.download_update_button.configure(state='disabled')
        self.restart_update_button=self.button(row,'Restart to apply',self.restart_for_update)
        self.restart_update_button.configure(state='normal' if (ROOT/'.studio-updates/pending.json').exists() else 'disabled')
        ttk.Label(f,text='Updates download in the background and are verified before installation. Restart after your current work finishes. '
                  'Projects, settings, installed tools, saves and game files remain in their existing workspace. '
                  'Previous application files are backed up before replacement.',wraplength=850).pack(anchor='w',pady=12)
        ttk.Label(f,text=CHANNEL_URL or 'No update channel is set for this release yet.',wraplength=850).pack(anchor='w',pady=8)
        self.update_busy=False
        self.available_update=None

    def save_update_preference(self):
        if self.busy:
            self.auto_updates.set(self.studio.state['settings'].get('check_updates_on_start',False))
            return
        self.studio.state['settings']['check_updates_on_start']=bool(self.auto_updates.get())
        self.studio.save()

    def check_updates(self):
        if self.update_busy or self.busy:
            return
        from .updater import check
        self.update_busy=True
        self.check_update_button.configure(state='disabled')
        self.update_status.set('Checking for updates…')
        def work():
            try:
                data,available=check()
                self.events.put(('update_checked',(data,available)))
            except Exception as exc:
                self.events.put(('update_error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def download_update(self):
        if self.busy or self.update_busy or not self.available_update:
            return
        from .updater import stage
        self.update_busy=True
        self.check_update_button.configure(state='disabled')
        self.download_update_button.configure(state='disabled')
        self.update_status.set('Downloading and verifying the update…')
        release=dict(self.available_update)
        def work():
            try:
                stage(ROOT,release)
                self.events.put(('update_staged',release['version']))
            except Exception as exc:
                self.events.put(('update_error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def restart_for_update(self):
        if getattr(self,"fulton_syncing",False):raise RuntimeError("Finish or stop Fulton source preparation before restarting")
        if self.busy or self.update_busy:
            raise RuntimeError('Wait for the current operation to finish before restarting')
        if not (ROOT/'.studio-updates/pending.json').is_file():
            raise RuntimeError('Download an update before restarting to apply it')
        self.studio.save()
        subprocess.Popen([os.sys.executable,str(ROOT/'bootstrap.py'),'--home',str(self.studio.home)],cwd=ROOT)
        self.destroy()

    def research_tab(self):
        f = self.frame("Research & limits")
        text = tk.Text(f, wrap="word", bg="white", fg="#173a56", relief="flat", font=("Segoe UI", 11), padx=20, pady=20)
        text.pack(fill="both", expand=True)
        text.insert("end", (ROOT / "RESEARCH.md").read_text(encoding="utf-8"))
        text.configure(state="disabled")

    def browse_setting(self, var, directory):
        value = filedialog.askdirectory() if directory else filedialog.askopenfilename()
        if value:
            var.set(value)

    def firmware_description(self):
        path = self.studio.state["settings"].get("firmware", "")
        return (f"Detected: {path} · local update file; RPCS3 installation is separate."
                if path else "No valid PS3UPDAT.PUP found. Choose its existing folder; no download is needed.")

    def browse_firmware(self):
        folder = filedialog.askdirectory(initialdir=self.firmware_folder.get() or str(Path.home()))
        if folder:
            self.firmware_folder.set(folder)
            self.detect_firmware()

    def detect_firmware(self):
        if self.busy:
            raise RuntimeError("Wait until the active job ends before changing settings")
        self.studio.detect_firmware(self.firmware_folder.get() or None)
        self.studio.save()
        self.firmware_folder.set(self.studio.state["settings"].get("firmware_folder", ""))
        self.firmware_status.set(self.firmware_description())

    def save_settings(self):
        if self.busy:
            raise RuntimeError("Wait until the active job ends before changing settings")
        jobs = self.jobs.get()
        if not 1 <= jobs <= 32:
            raise ValueError("Compiler jobs must be 1–32")
        self.studio.state["settings"].update(backend=self.backend.get(), rpcs3=self.rpcs3.get(), jobs=jobs)
        self.detect_firmware()
        self.status.set("Settings saved")

    def selected(self):
        tree = self.compiled_tree if self.tabs.select() == str(self.compiled_frame) else self.tree
        ids = tree.selection()
        if not ids:
            raise ValueError("Select a game first")
        return [p for p in self.studio.state["projects"] if p["id"] in ids]

    def add_folder(self):
        self.scan_games_folder()

    def batch_import_elfs(self):
        if self.busy:
            messagebox.showinfo('Job running', 'Wait for the active operation before importing ELF files')
            return
        folder = filedialog.askdirectory(title='Batch import all ELF files from this folder and every subfolder',
            initialdir=self.studio.state['settings'].get('elf_import_folder') or self.studio.state['settings'].get('games_folder') or str(Path.home()))
        if not folder:
            return
        def work(emit):
            result = self.studio.import_elf_tree(folder, emit, self.cancel)
            self.events.put(('elf_import', result))
        self.start_job(work)

    def scan_games_folder(self):
        if self.busy:
            messagebox.showinfo('Job running','Wait for the active operation before importing games');return
        folder=filedialog.askdirectory(title='Select a PS3 games library or one extracted game folder',
            initialdir=self.studio.state['settings'].get('games_folder') or str(Path.home()))
        if not folder:return
        def work(emit):
            tool=_decrypt_feature()
            if tool:   # private build: decrypt new EBOOT.BIN files first
                try:tool.decrypt_tree(folder,self.studio.home,self.studio.state['settings'],emit,self.cancel)
                except Exception as error:emit('Decrypt step skipped: '+str(error))
            result=self.studio.scan_games_folder(folder,emit,self.cancel)
            self.events.put(('folder_scan',result))
        self.start_job(work)

    def rescan_all_game_folders(self):
        from .core import games_folders
        if self.busy:
            messagebox.showinfo('Job running','Wait for the active operation before scanning');return
        folders=games_folders(self.studio.state['settings'])
        if not folders:
            self.scan_games_folder();return
        def work(emit):
            tool=_decrypt_feature()
            for folder in folders:
                if self.cancel.is_set():break
                emit('Scanning '+folder)
                if tool:
                    try:tool.decrypt_tree(folder,self.studio.home,self.studio.state['settings'],emit,self.cancel)
                    except Exception as error:emit('Decrypt step skipped: '+str(error))
                result=self.studio.scan_games_folder(folder,emit,self.cancel)
                self.events.put(('folder_scan',result))
        self.start_job(work)

    def remove_games_folder(self):
        settings=self.studio.state['settings']
        folders=[settings.get('games_folder')]+list(settings.get('games_folders') or [])
        folders=[f for i,f in enumerate(folders) if f and f not in folders[:i]]
        if not folders:
            messagebox.showinfo('Games folders','No games folders have been added yet.');return
        window=tk.Toplevel(self);window.title('Games folders');window.geometry('700x300')
        ttk.Label(window,text='Removing a folder only stops scanning it. Games and builds are kept.').pack(anchor='w',padx=8,pady=6)
        box=tk.Listbox(window,selectmode='extended');box.pack(fill='both',expand=True,padx=8)
        for f in folders:box.insert('end',f)
        def remove():
            drop={box.get(i) for i in box.curselection()}
            keep=[f for f in folders if f not in drop]
            settings['games_folders']=keep
            settings['games_folder']=keep[0] if keep else ''
            self.studio.save();window.destroy()
            self.status.set(f'Games folders: {len(keep)} kept.')
        ttk.Button(window,text='Remove selected',command=remove).pack(pady=6)

    def decrypt_all_games(self):
        """Decrypt every EBOOT.BIN in the games folder that has no EBOOT.elf yet."""
        tool=_decrypt_feature()
        if not tool:return
        if self.busy:
            messagebox.showinfo('Job running','Wait for the active operation before decrypting');return
        from .core import games_folders
        folders=games_folders(self.studio.state['settings'])
        if not folders:
            folder=filedialog.askdirectory(title='Select your PS3 games folder',initialdir=str(Path.home()))
            if not folder:return
            folders=[folder]
        def work(emit):
            for folder in folders:
                if self.cancel.is_set():break
                tool.decrypt_tree(folder,self.studio.home,self.studio.state['settings'],emit,self.cancel)
        self.start_job(work)

    def show_found_inputs(self):
        # This inventory window opens only when the player explicitly requests it.
        latest=getattr(self,'latest_folder_scan',None)
        if latest is None:
            reports=sorted((self.studio.home/'game-folder-imports').glob('*.json'),reverse=True)
            if reports:
                try:latest=json.loads(reports[0].read_text())
                except (ValueError,OSError):pass
        selected=[p for p in self.studio.state['projects'] if p['id'] in self.tree.selection()]
        window=tk.Toplevel(self);window.title('Found ELF / EBOOT files');window.geometry('1120x560')
        frame=ttk.Frame(window,padding=12);frame.pack(fill='both',expand=True)
        row=ttk.Frame(frame);row.pack(fill='x')
        ttk.Label(row,text='Show:').pack(side='left')
        scope=tk.StringVar(value='Selected game' if selected else 'Latest scan')
        choice=ttk.Combobox(row,textvariable=scope,values=('Selected game','Latest scan'),state='readonly',width=20)
        choice.pack(side='left',padx=8)
        summary=tk.StringVar();ttk.Label(row,textvariable=summary).pack(side='left')
        wrap=ttk.Frame(frame);wrap.pack(fill='both',expand=True,pady=10)
        tree=ttk.Treeview(wrap,columns=('path','format','ready','status'),show='headings',selectmode='browse')
        for column,title,width in (('path','Full file path',660),('format','Format',75),('ready','Readiness',150),('status','Discovery',100)):
            tree.heading(column,text=title);tree.column(column,width=width,stretch=False)
        tree.grid(row=0,column=0,sticky='nsew')
        y=ttk.Scrollbar(wrap,orient='vertical',command=tree.yview);y.grid(row=0,column=1,sticky='ns')
        x=ttk.Scrollbar(wrap,orient='horizontal',command=tree.xview);x.grid(row=1,column=0,sticky='ew')
        tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        wrap.rowconfigure(0,weight=1);wrap.columnconfigure(0,weight=1)
        detail=tk.StringVar(value='Select a file to see its complete path and duplicate/error details.')
        ttk.Label(frame,textvariable=detail,wraplength=1060).pack(fill='x')
        ttk.Label(frame,text='Discovery does not decrypt files or prove a game is playable.').pack(anchor='w',pady=(8,0))
        rows={}
        def populate(event=None):
            tree.delete(*tree.get_children());rows.clear()
            files=([item for p in selected for item in p.get('discovered_inputs',[])] if scope.get()=='Selected game'
                   else (latest or {}).get('files',[]))
            for number,item in enumerate(input_inventory_rows(files)):
                key=str(number);rows[key]=item;tree.insert('', 'end',iid=key,values=item[:4])
            summary.set(f'{len(files)} files' if files else 'Scan games folder to collect this inventory.')
        def show_detail(event=None):
            ids=tree.selection()
            if ids:
                item=rows[ids[0]];detail.set(item[0]+('\n'+item[4] if item[4] else ''))
        choice.bind('<<ComboboxSelected>>',populate);tree.bind('<<TreeviewSelect>>',show_detail)
        populate()

    def add_file(self):
        p = filedialog.askopenfilename(title="Select EBOOT.ELF or EBOOT.BIN")
        if p:
            self.add(p)

    def add(self, p):
        if self.busy:
            messagebox.showinfo("Job running", "Wait for the active operation to finish")
            return
        self.studio.add(p)
        self.refresh()

    def set_elf(self):
        if self.busy:
            return
        p = self.selected()[0]
        value = filedialog.askopenfilename(title="Select decrypted PPU EBOOT.ELF")
        if value:
            p["elf"], p["native_exe"], p["status"] = value, "", "queued"
            self.studio.save()
            self.refresh()

    def game_patches_dialog(self):
        """Pick community patches (player-supplied patch.yml) to apply to a copy of the ELF."""
        from . import game_patches
        from .core import choose_elf, read_json
        try:
            p = self.selected()[0]
        except ValueError as e:
            messagebox.showinfo("Game patches", str(e))
            return
        settings = self.studio.state["settings"]
        try:
            elf = choose_elf(p["source"], p.get("elf", ""))
            metadata = read_json(p["last_analysis"], {}).get("metadata", {}) if p.get("last_analysis") else {}
            key, patches, errors, files = game_patches.available_for_project(self.studio.home, settings, elf, metadata)
        except Exception as e:
            messagebox.showerror("Game patches", f"Could not read patches for this game:\n{e}")
            return
        window = tk.Toplevel(self)
        window.title("Game patches - " + p.get("name", ""))
        window.geometry("820x520")
        ttk.Label(window, text=f"Executable: {key}", wraplength=780).pack(anchor="w", padx=10, pady=(10, 2))
        source = "; ".join(Path(f["path"]).name for f in files) or "none found"
        ttk.Label(window, text=f"Patch files: {source}. Patches are applied to a copy of the ELF in the build attempt "
                  "folder; your original file is never changed. Configurable values use the file's defaults.",
                  wraplength=780).pack(anchor="w", padx=10, pady=2)
        errors = [e for e in errors if "unknown patch type" not in e]   # emulator-only patch types are listed greyed out instead
        if errors:
            ttk.Label(window, text=f"{len(errors)} problem(s) while reading patch files; first: {errors[0]}",
                      wraplength=780, foreground="#b45309").pack(anchor="w", padx=10, pady=2)
        body = ttk.Frame(window)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        canvas = tk.Canvas(body, highlightthickness=0)
        bar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        enabled = set(p.get("patches") or [])
        choices = {}
        if not patches:
            ttk.Label(inner, text="No patches in the supplied patch files match this executable.").pack(anchor="w")
        for info in patches:
            if info["name"] in choices:
                continue
            label = info["name"]
            extra = [x for x in (info["group"] and "group: " + info["group"], info["author"] and "by " + info["author"],
                                 info["patch_version"] and "v" + info["patch_version"]) if x]
            if extra:
                label += "  (" + ", ".join(extra) + ")"
            if not info["applicable"]:
                label += "  [cannot apply to a file: " + (", ".join(info["unsupported"]) or "invalid entries") + "]"
            var = tk.BooleanVar(value=info["name"] in enabled and info["applicable"])
            ttk.Checkbutton(inner, text=label, variable=var,
                            state="normal" if info["applicable"] else "disabled").pack(anchor="w", pady=1)
            choices[info["name"]] = var
        row = ttk.Frame(window)
        row.pack(fill="x", padx=10, pady=(0, 10))

        def save():
            if self.busy:
                messagebox.showinfo("Job running", "Wait for the active operation to finish", parent=window)
                return
            p["patches"] = [name for name, var in choices.items() if var.get()]
            self.studio.save()
            self.status.set(f"{len(p['patches'])} game patch(es) enabled for {p.get('name', 'game')}")
            window.destroy()

        def choose_file():
            value = filedialog.askopenfilename(parent=window, title="Select a patch.yml",
                                               filetypes=[("Patch files", "*.yml *.yaml"), ("All files", "*.*")])
            if value:
                settings["patch_file"] = value
                self.studio.save()
                window.destroy()
                self.game_patches_dialog()

        self.button(row, "Save", save, True)
        self.button(row, "Choose patch file...", choose_file)
        self.button(row, "Clear all", lambda: [v.set(False) for v in choices.values()])
        self.button(row, "Cancel", window.destroy)

    def remove(self):
        if self.busy:
            return
        ids = {p["id"] for p in self.selected()}
        self.studio.state["projects"] = [p for p in self.studio.state["projects"] if p["id"] not in ids]
        self.studio.save()
        self.refresh()
        self.status.set("Removed from queue. Project files retained.")

    def refresh(self):
        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for p in sorted(self.studio.state["projects"], key=lambda p: p.get("name", "").casefold()):   # A-Z
            if not p.get("queue_removed"):
                self.tree.insert("", "end", iid=p["id"], values=(p["name"], p["status"]))
        self.tree.selection_set([i for i in selection if self.tree.exists(i)])
        # Update the compiled list's queue column right away from current
        # state; the full background rescan can take minutes.
        try:
            removed = {p["id"]: bool(p.get("queue_removed")) for p in self.studio.state["projects"]}
            for item_id in self.compiled_tree.get_children():
                if item_id in removed:
                    values = list(self.compiled_tree.item(item_id, "values"))
                    if len(values) >= 3:
                        values[2] = "Removed from queue" if removed[item_id] else "In queue"
                        self.compiled_tree.item(item_id, values=values)
                else:
                    self.compiled_tree.delete(item_id)
        except Exception as error:
            self.events.put(("log", f"Compiled list quick update failed: {error}"))
        self.refresh_compiled_async()

    def select_compiled_output_folder(self):
        from .compiled_library import selected_folder
        folder = filedialog.askdirectory(title="Select folder containing completed build outputs",
                                         initialdir=selected_folder(self.studio) or str(self.studio.home))
        if folder:
            self.compiled_output_folder.set(folder)
            self._compiled_requested_folder = folder
            self.refresh_compiled_async()

    def restore_cached_compiled_rows(self):
        # This bounded display snapshot contains names/counts only. Actual
        # executable and input validation remains in the background/launch path.
        from .compiled_library import read_cached_rows
        try:
            known = {project['id'] for project in self.studio.state['projects']}
            rows = [row for row in read_cached_rows(self.studio) if row[0] in known]
            if rows:
                self._update_compiled_rows(rows, final=False)
                self.compiled_status.set("Saved built games; checking for changes...")
        except Exception as error:
            self.events.put(("log", f"Saved built-game list could not be restored: {error}"))

    def _update_compiled_rows(self, rows, final=False):
        """Commit only validated results; interim updates never end the scan."""
        from .compiled_library import hidden_project_ids
        try:
            hidden = set(hidden_project_ids(self.studio))
            self._compiled_hidden_ids = hidden
        except Exception as error:
            hidden = self.__dict__.get('_compiled_hidden_ids', set())
            self.compiled_status.set(f"Compiled-list visibility could not be read: {error}")
        selection = self.compiled_tree.selection()
        if final:
            self.compiled_tree.delete(*self.compiled_tree.get_children())
        else:
            # Keep the previous library visible until the authoritative final
            # pass, while respecting removals made during this worker's scan.
            for project_id in self.compiled_tree.get_children():
                if project_id in hidden:
                    self.compiled_tree.delete(project_id)
        for project_id, name, count, removed in sorted(rows, key=lambda r: str(r[1]).casefold()):   # A-Z
            if project_id in hidden:
                continue
            values = (name, f"{count} finished build(s); gameplay unverified",
                      "Removed from queue" if removed else "In queue")
            if not final and self.compiled_tree.exists(project_id):
                self.compiled_tree.item(project_id, values=values)
            else:
                self.compiled_tree.insert("", "end", iid=project_id, values=values)
        ordered = sorted(self.compiled_tree.get_children(), key=lambda i: str(self.compiled_tree.item(i, "values")[0]).casefold())
        for position, item_id in enumerate(ordered):
            self.compiled_tree.move(item_id, "", position)
        self.compiled_tree.selection_set([i for i in selection if self.compiled_tree.exists(i)])

    def refresh_compiled_async(self):
        # Candidate validation reads and hashes game files; never block Tk startup.
        if getattr(self, "compiled_scan_busy", False):
            self.compiled_scan_pending = True
            self.compiled_status.set("Checking finished builds; another refresh is queued.")
            return
        self.compiled_status.set("Saved built games; checking for changes in the background..."
                                 if self.compiled_tree.get_children() else
                                 "Checking finished builds in the background...")
        self.compiled_scan_busy = True
        self.compiled_scan_pending = False
        projects = copy.deepcopy(self.studio.state["projects"])
        folder = getattr(self, "_compiled_requested_folder", None)
        self._compiled_requested_folder = None
        def work():
            rows = []
            scan_error = None
            try:
                from .compiled_library import scan_outputs
                discovery = scan_outputs(self.studio, folder, lambda text: self.events.put(("compiled_status", text)))
                if discovery.get("scan_errors"):
                    self.events.put(("log", f"Output folder scan: {discovery['scan_errors']} report/folder check(s) failed."))
            except Exception as error:
                scan_error = str(error)
                self.events.put(("log", f"Output folder scan failed: {error}"))
                self.events.put(("compiled_status", f"Output folder scan failed: {error}"))
            errors = 0
            try:
                from .compiled_library import hidden_project_ids
                hidden = set(hidden_project_ids(self.studio))
            except Exception as error:
                hidden = self.__dict__.get('_compiled_hidden_ids', set())
                errors += 1
                self.events.put(("log", f"Compiled-list visibility could not be read: {error}"))
            total = len(projects)
            for checked, project in enumerate(projects, 1):
                self.events.put(("compiled_status", f"Checking game {checked}/{total}: {project.get('name', 'game')} | {len(rows)} finished game(s) found"))
                if project["id"] in hidden:
                    continue
                try:
                    candidates = self.studio.list_test_candidates(project)
                    if candidates:
                        rows.append((project["id"], project["name"], len(candidates), bool(project.get("queue_removed"))))
                        # Show this independently validated game immediately;
                        # unrelated remaining inputs must not hide the library.
                        self.events.put(("compiled_rows_progress", list(rows)))
                except Exception as error:
                    errors += 1
                    self.events.put(("log", f"Build library check for {project.get('name', 'game')}: {error}"))
            if not scan_error and not errors:
                try:
                    from .compiled_library import write_cached_rows
                    write_cached_rows(self.studio, rows)
                except Exception as error:
                    self.events.put(("log", f"Built-game list could not be saved for next startup: {error}"))
            summary = f"Refreshed: {len(rows)} game(s), {sum(row[2] for row in rows)} finished build(s)."
            if scan_error:
                summary += f" Output folder scan failed: {scan_error}"
            if errors:
                summary += f" {errors} game check(s) failed; see the log."
            self.events.put(("compiled_status", summary))
            self.events.put(("compiled_rows", rows))
        threading.Thread(target=work, daemon=True).start()

    def start_job(self, work, action="operation"):
        if self.busy:
            messagebox.showinfo("Job running", "Wait or use Stop before starting another operation")
            return
        self.busy = True
        self.current_queue_item.set('Queue: no game operation active')
        self.cancel.clear()
        self.progress.start(12)
        self.status.set("Working · detailed output is saved in the project logs")
        def run():
            try:
                from .progress import ProgressEmitter
                emit=ProgressEmitter(lambda x:self.events.put(('log',x)),lambda x:self.events.put(('progress',x)))
                self.current_progress=emit
                emit.begin(action=action)
                work(emit)
            except Exception as e:
                self.events.put(("log", str(e)))
                if hasattr(self,'current_progress'):
                    state=self.current_progress.state
                    self.current_progress.progress(state.key,'cancelled' if isinstance(e,Cancelled) else 'failed',detail=str(e))
                self.events.put(("status", "Cancelled" if isinstance(e, Cancelled) else "Blocked · see log for next step"))
            else:
                if self.current_progress.state.key=='operation':self.current_progress.progress('operation','done')
                self.events.put(("status", "Operation finished · review the result in the queue and log"))
            finally:
                self.events.put(("done", None))
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def selected_job(self, action):
        self.run_projects(self.selected(), action)

    def run_projects(self, projects, action):
        # The position, total and displayed names belong to this operation's
        # snapshot; later selection/name edits must not change the active label.
        items=tuple((p,p['name']) for p in projects)
        def work(emit):
            failures=0;attempted=0;cancelled=False
            work_items=items
            try:
                if action in ("retry", "queue"):
                    # Candidate validation may hash files; keep it off the UI thread.
                    # "queue" = Build entire queue: every game without a finished build,
                    # blocked ones included; finished builds are kept for testing.
                    if action == "queue":
                        # Quick check (no hashing): skip games whose finished executable exists.
                        emit(f"Build entire queue: checking {len(items)} game(s) for finished builds...")
                        work_items=tuple((p,name) for p,name in items
                                         if not (p.get("native_exe") and Path(p["native_exe"]).is_file()))
                        emit(f"{len(work_items)} game(s) to build, A-Z; {len(items)-len(work_items)} already built and kept.")
                    else:
                        work_items=tuple((p,name) for p,name in items if not self.studio.list_test_candidates(p))
                    if not work_items:
                        emit("Every game in the queue already has a finished build." if action=="queue" else "No blocked games need retrying; completed builds were retained.")
                for project_index,(p,name) in enumerate(work_items):
                    if self.cancel.is_set():raise Cancelled("Queue cancelled")
                    item=dict(name=name,index=project_index,total=len(work_items),action=action,state='running')
                    self.events.put(('queue_item',item))
                    emit.begin(name,project_index,len(work_items),action)
                    attempted+=1
                    emit(f"\n=== {name} ===")
                    try:
                        if action == "analyse":
                            report = self.studio.analyse(p, emit, self.cancel)
                            emit(json.dumps(report["binary"], indent=2))
                        else:
                            reuse = action in ("rebuild", "retry") or (action == "queue" and bool(p.get("attempt")))
                            self.studio.build(p, emit, self.cancel, stop_after="lift" if action == "lift" else "build", reuse=reuse)
                    except Cancelled:
                        self.events.put(('queue_item',dict(item,state='cancelled')))
                        raise
                    except Exception as e:
                        emit.progress(emit.state.key,'failed',detail=str(e))
                        emit(str(e));failures+=1
                        self.events.put(('queue_item',dict(item,state='failed')))
                    else:
                        self.events.put(('queue_item',dict(item,state='done')))
                        if action in ("build", "rebuild", "retry", "queue"):
                            # Keep only this newest build of the game.
                            try:
                                from .build_cleanup import prune_project
                                pruned=prune_project(self.studio,p)
                                if pruned.get('removed'):emit(f"Removed {len(pruned['removed'])} older build(s) of {name}; only the newest is kept.")
                            except Exception as error:emit('Old-build cleanup skipped: '+str(error))
                    self.events.put(("refresh", None))
                if failures:
                    raise RuntimeError(f"{failures} project(s) blocked. Other queue items were still attempted.")
            except Cancelled:
                cancelled=True;raise
            finally:
                self.events.put(('queue_complete',dict(attempted=attempted,total=len(work_items),failures=failures,cancelled=cancelled)))
        self.start_job(work)

    def retry_blocked(self):
        if self.busy:
            self.status.set("Wait for the active build queue to finish before retrying blocked games.")
            return
        projects = [p for p in self.studio.state["projects"]
                    if not p.get("queue_removed") and p.get("status") in ("blocked", "failed", "error")]
        if not projects:
            self.status.set("No blocked games are waiting for a retry.")
            return
        self.run_projects(projects, "retry")

    def build_blocked_anyway(self):
        """Mark every game blocked at SPU discovery as build-anyway, then retry.
        Their SPU programs then accept data words inside code sections; any
        that the game actually executes stops that SPU program with a logged
        '.word data executed' line instead of running wrong code."""
        if self.busy:
            self.status.set("Wait for the active build queue to finish first.")
            return
        # Every blocked game: SPU discovery (build anyway), compiler crashes (retried at
        # lower optimisation) and file-lock failures (retried) all rebuild here, A-Z.
        projects = sorted((p for p in self.studio.state["projects"]
                           if not p.get("queue_removed") and p.get("status") in ("blocked", "failed", "error", "analysis blocked")),
                          key=lambda p: p.get("name", "").casefold())
        if not projects:
            self.status.set("No games are blocked at SPU discovery.")
            return
        with self.studio.state_lock if hasattr(self.studio, "state_lock") else _nullcontext():
            for p in projects:
                p["spu_build_anyway"] = True
            self.studio.save()
        self.status.set(f"Building {len(projects)} blocked game(s) anyway: " + ", ".join(p["name"] for p in projects))
        self.run_projects(projects, "retry")

    def build_queue(self):
        projects = sorted((p for p in self.studio.state["projects"] if not p.get("queue_removed")), key=lambda p: p.get("name", "").casefold())   # A-Z
        if not projects:
            raise ValueError("Add a game first")
        with self.studio.state_lock if hasattr(self.studio, "state_lock") else _nullcontext():
            for p in projects:
                if p.get("status") in ("blocked", "failed", "error", "analysis blocked"):
                    p["spu_build_anyway"] = True
            self.studio.save()
        self.run_projects(projects, "queue")

    def install(self):
        if self.busy:
            return
        self.save_settings()
        self.start_job(lambda emit: self.studio.setup_backend(emit, self.cancel),action="setup")

    def doctor(self):
        if self.busy:
            return
        self.save_settings()
        self.tabs.select(0)
        self.start_job(lambda emit: emit(json.dumps(self.studio.doctor(), indent=2)))

    def stop(self):
        if self.busy:
            self.cancel.set()
            self.status.set("Stopping · waiting for the current subprocess to exit")

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'elf_import':
                self.refresh()
                message = f"ELF import: {len(value['added'])} queued; {len(value['duplicates'])} already in library; {len(value['errors'])} errors. Use Build entire queue to start."
                self.status.set(message)
                self.log.configure(state='normal');self.log.insert('end',message+'\nReport: '+value['report']+'\n');self.log.configure(state='disabled')
            elif kind == 'folder_scan':
                self.latest_folder_scan=value
                self.refresh()
                count=len(value['added']);duplicates=len(value['duplicates']);needed=len(value['needs_decrypted_elf'])
                self.status.set(scan_summary(value))
                self.log.configure(state='normal');self.log.insert('end',scan_summary(value)+'; use Found ELF / EBOOT files to view every input. No builds started.\n');self.log.configure(state='disabled')
            elif kind == 'manual_launched':
                self.manual_launch_busy=False
                self.refresh_test_sessions(preferred_pid=value['pid'])
            elif kind in ('manual_error','manual_stop_error'):
                if kind=='manual_error':self.manual_launch_busy=False
                self.manual_status.set(value)
            elif kind == 'manual_stopped':
                self.refresh_test_sessions();self.manual_status.set(value)
            elif kind == 'repair_watch':
                self.repair_watch_busy=False
                if value:self.show_repair_progress(value)
            elif kind == 'repair_progress':
                self.show_repair_progress(value)
            elif kind == 'repair_progress_all':
                self.show_repair_progress(value,'all')
            elif kind == 'repair_progress_selected':
                self.show_repair_progress(value,'selected')
            elif kind == 'fix_status':
                self.fix_status.set(value)
            elif kind == 'fix_error':
                self.fix_busy=False;self.fix_status.set('Fix preparation: '+value)
            elif kind == 'fix_ready_one':
                from . import game_fixes
                try:game_fixes.promote(self.studio,value,queue_busy=self.busy)
                except Exception as error:self.fix_status.set('Fix build kept, but not selected: '+str(error))
                self.prune_builds_async([value.get('project_id')])
            elif kind == 'batch_status':
                self.fix_status.set(value)
            elif kind == 'games_update_status':
                self.status.set(value)
            elif kind == 'games_update_elf':
                pid,elf=value
                lock=self.studio.state_lock if hasattr(self.studio,'state_lock') else _nullcontext()
                with lock:
                    for p in self.studio.state['projects']:
                        if p['id']==pid:
                            # Same as "Set decrypted ELF": the updated game needs a new build.
                            p['elf'],p['native_exe'],p['status']=elf,'','queued'
                    self.studio.save()
                self.refresh()
            elif kind == 'games_update_done':
                self.games_update_busy=False
                self.status.set(value.splitlines()[0] if value else 'Update games finished.')
                if value and ('\n' in value or 'installed' in value or 'failed' in value):
                    messagebox.showinfo('Update games',value)
            elif kind == 'fix_status_all':
                self.repair_all_text.set('Apply fixes to games: '+value)
            elif kind == 'all_fix_bars_done':
                self.repair_all_text.set(value)
                for bar in self.repair_all_step_bars+self.repair_all_overall_bars:
                    bar.stop();bar.configure(mode='determinate');bar['value']=100
            elif kind == 'single_fix_done':
                self.single_fix_busy=False;self._single_fix_pid=None;self.fix_status.set(value)
                try:self.refresh()
                except Exception:pass
            elif kind == 'fix_all_done':
                self.fix_busy=False;self.fix_status.set(value)
                try:self.refresh()
                except Exception:pass
                # Then build every game that has no finished build yet (blocked ones
                # included, A-Z), so one click covers the whole library.
                if not self.busy and getattr(self,'_fix_whole_library',False):
                    try:
                        self.status.set('Fixes applied; now building games that have no finished build yet.')
                        self.build_queue()
                    except Exception as error:
                        self.status.set('Could not start building unbuilt games: '+str(error))
            elif kind == 'fix_ready':
                self.fix_busy=False
                if value.get('status')=='ready':
                    from . import game_fixes
                    try:
                        promotion=game_fixes.promote(self.studio,value,queue_busy=self.busy)
                        self.fix_status.set('Fix candidate ready. '+('Selected for this game.' if promotion['promoted'] else promotion.get('reason','Current queue selection preserved.')))
                    except Exception as error:self.fix_status.set('Fix candidate retained; selection unchanged: '+str(error))
                    self.prune_builds_async([value.get('project_id')])
                else:self.fix_status.set('Fix preparation: '+value.get('status','failed'))
            elif kind == 'queue_item':
                self.current_queue_item.set(queue_item_label(value))
            elif kind == 'queue_complete':
                self.current_queue_item.set(queue_completion_label(value))
            elif kind == 'progress':
                self.overall_progress_text.set(value['overall'])
                self.overall_progress['value']=value['overall_percent']
                self.step_progress_text.set(value['step'])
                self.progress_detail.set(value['detail'])
                self.progress.stop()
                if value['status']=='running' and 'no intermediate counts' in value['step']:
                    self.progress.configure(mode='indeterminate');self.progress.start(12)
                else:
                    self.progress.configure(mode='determinate',maximum=100);self.progress['value']=value['step_percent']
            elif kind == 'fulton_progress':
                self.fulton_status.set(value)
            elif kind in ('fulton_ready','fulton_stopped'):
                from . import fulton
                self.fulton_syncing=False;self.show_fulton_entries(fulton.catalog(self.studio))
                if kind=='fulton_ready':
                    self.fulton_status.set(f"Library prepared: {value['available']}/{value['projects']} sources cached; {value['unavailable']} unavailable. Game compatibility still requires validation.")
                else:self.fulton_status.set('Source preparation stopped; downloaded sources and logs retained.')
            elif kind == 'fulton_catalog':
                self.fulton_syncing=False;self.show_fulton_entries(value)
            elif kind == 'fulton_error':
                self.fulton_syncing=False;self.fulton_status.set('Fulton sync: '+value+'; local entries remain available')
            elif kind == "log":
                self.log.configure(state="normal")
                self.log.insert("end", value + "\n")
                # Bound UI memory; complete records stay in project log files.
                if int(self.log.index("end-1c").split(".")[0]) > 2500:
                    self.log.delete("1.0", "1000.0")
                self.log.see("end")
                self.log.configure(state="disabled")
            elif kind in ('update_checked','update_staged','update_error'):
                self.update_busy=False
                self.check_update_button.configure(state='normal')
                if kind=='update_checked':
                    release,available=value
                    self.available_update=release if available else None
                    self.download_update_button.configure(state='normal' if available else 'disabled')
                    self.update_status.set(('Version '+release['version']+' is available. '+release.get('notes','')) if available else 'PS3 Studio is up to date ('+VERSION+').')
                elif kind=='update_staged':
                    self.restart_update_button.configure(state='normal')
                    self.update_status.set('Version '+value+' is verified and ready. Restart to apply it.')
                else:
                    self.update_status.set('Update could not complete: '+value)
                    self.download_update_button.configure(state='normal' if self.available_update else 'disabled')
            elif kind == "status":
                self.status.set(value)
            elif kind == "refresh":
                self.refresh()
            elif kind == "compiled_status":
                self.compiled_status.set(value)
            elif kind == "compiled_rows_progress":
                self._update_compiled_rows(value, final=False)
            elif kind == "compiled_rows":
                self._update_compiled_rows(value, final=True)
                self.compiled_scan_busy = False
                if self.compiled_scan_pending:
                    self.refresh_compiled_async()
            elif kind == "done":
                self.busy = False
                self.progress.stop()
                self.refresh()
        self.after(120, self.poll)

    def choose_completed_candidate(self, project, on_choose, title='Choose completed build', button_text='Use selected build'):
        candidates=self.studio.list_test_candidates(project)
        if not candidates:
            raise ValueError('No completed build is available for this game yet. The build queue can continue.')
        if len(candidates)==1 and project.get('status') not in ('building','analysing'):
            on_choose(candidates[0]);return
        window=tk.Toplevel(self);window.title(title);window.geometry('800x210')
        frame=ttk.Frame(window,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=project['name'],font=('Segoe UI',12,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Choose a finished build. The active build and queue selection are preserved.').pack(anchor='w',pady=8)
        labels=[f"{i+1}. {c['label']}" for i,c in enumerate(candidates)]
        selected=tk.StringVar(value=labels[0])
        chooser=ttk.Combobox(frame,textvariable=selected,values=labels,state='readonly',width=100)
        chooser.pack(fill='x')
        def accept():
            candidate=candidates[labels.index(selected.get())]
            window.destroy();on_choose(candidate)
        self.button(frame,button_text,accept,True)
        self.button(frame,'Cancel',window.destroy)

    def launch(self, native):
        project=copy.deepcopy(self.selected()[0])
        if self.manual_launch_busy:
            self.manual_status.set('A manual test is already starting.');return
        if native:
            self.choose_completed_candidate(project,lambda candidate:self.start_manual_test(project,True,candidate),
                                            title='Test a completed native build',button_text='Test selected build')
        else:self.start_manual_test(project,False)

    def start_manual_test(self, project, native, candidate=None):
        if self.manual_launch_busy:return
        self.manual_launch_busy=True
        self.manual_status.set('Starting '+('native test' if native else 'RPCS3 reference')+': '+project['name'])
        def run():
            try:
                pid=self.studio.launch(project,native,candidate_id=candidate['id'] if candidate else None)
                self.events.put(('manual_launched',dict(pid=pid,project_name=project['name'])))
            except Exception as error:self.events.put(('manual_error',str(error)))
        self.manual_launch_worker=threading.Thread(target=run,daemon=True)
        self.manual_launch_worker.start()

    def refresh_test_sessions(self, preferred_pid=None):
        sessions=self.studio.list_test_sessions()
        previous=self.test_session_labels.get(self.test_session_var.get())
        self.test_sessions={s['session_id']:s for s in sessions}
        labels={manual_session_label(s):s['session_id'] for s in sessions}
        self.test_session_labels=labels
        self.test_session_picker.configure(values=list(labels))
        target=next((s['session_id'] for s in sessions if s['pid']==preferred_pid),None) or previous
        if target not in self.test_sessions:
            target=next((s['session_id'] for s in sessions if s['status']=='running'),None)
        label=next((label for label,sid in labels.items() if sid==target),'')
        self.test_session_var.set(label)
        if label:self.manual_status.set(label)

    def watch_manual_sessions(self):
        try:self.refresh_test_sessions()
        except (OSError,ValueError) as error:self.manual_status.set('Test status unavailable: '+str(error))
        self.after(750,self.watch_manual_sessions)

    def stop_test(self):
        session_id=self.test_session_labels.get(self.test_session_var.get())
        if not session_id:
            self.manual_status.set('Choose a running manual test first.');return
        self.manual_status.set('Stopping selected manual test; builds continue.')
        def run():
            try:
                stopped=self.studio.stop_test_session(session_id)
                self.events.put(('manual_stopped','Selected manual test stopped.' if stopped else 'This test is finished or is not owned by this Studio session.'))
            except Exception as error:self.events.put(('manual_stop_error',str(error)))
        self.manual_stop_worker=threading.Thread(target=run,daemon=True)
        self.manual_stop_worker.start()

    def open_test_log(self):
        session_id=self.test_session_labels.get(self.test_session_var.get())
        if not session_id:
            self.manual_status.set('Choose a manual test to open its log.');return
        session=self.test_sessions[session_id]
        self.open_path(session['log_path'])

    def batch_try_all(self, projects=None, seconds=None, screenshots=None):
        """Launch every built game (or the given ones) for a fixed time and
        write workspace/batch-try/<stamp>/REPORT.md. Runs beside the build queue."""
        from .batch_queue import BatchTry, DEFAULT_SECONDS
        if self.batch_try and self.batch_try.active():
            self.fix_status.set('Batch try is already running.');return
        if projects is None:
            projects=[p for p in self.studio.state['projects'] if not p.get('queue_removed')]
        seconds=seconds or DEFAULT_SECONDS
        shots=screenshots if screenshots is not None else [max(5,seconds//3),max(10,seconds-10)]
        def notify(text):self.events.put(('batch_status',text))
        self.batch_try=BatchTry(self.studio,projects,seconds,shots,notify)
        self.batch_try.start()
        self.fix_status.set(f'Batch try started: {len(projects)} game(s), {seconds}s each.')

    def stop_batch_try(self):
        if self.batch_try and self.batch_try.active():
            self.batch_try.cancel();self.fix_status.set('Stopping batch try after the current game.')

    def poll_queue_requests(self):
        """Pick up queued requests (see studio/batch_queue.py) every 30 s."""
        from .batch_queue import POLL_MS, take_batch_request, take_retry_request
        try:
            if not (self.batch_try and self.batch_try.active()):
                req=take_batch_request(self.studio.home,self.studio)
                if req:
                    projects,seconds,shots=req;self.batch_try_all(projects,seconds,shots)
            if not self.busy and not self.fix_busy:
                req=take_retry_request(self.studio.home,self.studio)
                if req:
                    projects,mode=req
                    if mode=='fixes':self.apply_game_fixes(projects)
                    else:
                        if mode=='anyway':
                            lock=self.studio.state_lock if hasattr(self.studio,'state_lock') else _nullcontext()
                            with lock:
                                for p in projects:p['spu_build_anyway']=True
                                self.studio.save()
                        self.run_projects(projects,'retry')
            from .auto_loop import AutoLoop, GuiHost
            if self.auto_loop is None:self.auto_loop=AutoLoop(ROOT,self.studio.home)
            state=self.auto_loop.tick(GuiHost(self))
            if state!='off':self.fix_status.set('Auto loop: '+state)
        except Exception as error:
            self.fix_status.set('Queue request failed: '+str(error))
        finally:
            self.after(POLL_MS,self.poll_queue_requests)

    def toggle_auto_loop(self):
        from .auto_loop import AutoLoop
        if self.auto_loop is None:self.auto_loop=AutoLoop(ROOT,self.studio.home)
        on=not self.auto_loop.enabled();self.auto_loop.set_enabled(on)
        self.fix_status.set('Auto loop '+('on: building and testing every game, one at a time.' if on else 'off after the current step.'))

    # ------------------------------------------------ updates, port export, display settings
    def update_games(self):
        """Bring the selected games (or, with none selected, every game) to the version
        their project or patches need: disc update first, else the official download
        (private build), then decrypt the new EBOOT and point the project at it."""
        if getattr(self,'games_update_busy',False):
            self.status.set('Game updates are already running.');return
        try:projects=self.selected()
        except ValueError:projects=[p for p in self.studio.state['projects'] if not p.get('queue_removed')]
        self.games_update_busy=True
        self.status.set(f'Checking versions for {len(projects)} game(s)...')
        settings=copy.deepcopy(self.studio.state['settings']);home=self.studio.home
        def run():
            from .port_export import update_game_version
            lines=[]
            for i,p in enumerate(projects,1):
                name=p.get('name','?')
                def emit(m,_n=name,_i=i):self.events.put(('games_update_status',f'Update games {_i}/{len(projects)} {_n}: {m}'))
                try:r=update_game_version(p,settings,home,emit)
                except Exception as error:r={'status':'failed','reason':str(error)}
                if r.get('elf'):self.events.put(('games_update_elf',(p['id'],r['elf'])))
                if r.get('status') not in ('up to date','skipped') or len(projects)<=5:
                    lines.append(f"{name}: {r.get('status')} {r.get('version') or r.get('current') or ''} {r.get('reason') or r.get('note') or ''}".strip())
            self.events.put(('games_update_done','\n'.join(lines) or 'Every checked game is already at the version it needs.'))
        threading.Thread(target=run,daemon=True).start()

    def update_games_latest(self):
        """Newest official update for the selected games (or every game). Games whose
        port depends on their current version (job-system recipes, pins) are kept."""
        if getattr(self,'games_update_busy',False):
            self.status.set('Game updates are already running.');return
        try:
            from .private_features import update_download  # noqa: F401  (private build only)
        except ImportError:
            messagebox.showinfo('Update to latest','Downloading official updates is not part of the Public Edition. Install game updates in your own RPCS3.');return
        settings=self.studio.state['settings']
        if not settings.get('update_server_cert_sha256'):
            from . import game_versions as gv
            try:fp=gv.server_cert_fingerprint()
            except Exception as error:
                messagebox.showerror('Update to latest',f"Could not reach Sony's update server: {error}");return
            ok=messagebox.askyesno('Approve update server',
                "Sony's update server uses Sony's own certificate, which Windows doesn't know.\n\n"
                f"Certificate SHA-256:\n{fp}\n\nTrust this server for downloading official game updates? "
                "(Asked once; only this exact certificate is accepted.)")
            if not ok:
                self.status.set('Update to latest cancelled: update server not approved.');return
            settings['update_server_cert_sha256']=fp;self.studio.save()
        try:projects=self.selected()
        except ValueError:projects=list(self.studio.state['projects'])
        self.games_update_busy=True
        self.status.set(f'Checking official updates for {len(projects)} game(s)...')
        settings_copy=copy.deepcopy(settings);home=self.studio.home
        def run():
            from .port_export import update_game_version
            updated=[];kept=[];failed=[];current=0
            for i,p in enumerate(projects,1):
                name=p.get('name','?')
                def emit(m,_n=name,_i=i):self.events.put(('games_update_status',f'Update to latest {_i}/{len(projects)} {_n}: {m}'))
                try:r=update_game_version(p,settings_copy,home,emit,mode='latest')
                except Exception as error:r={'status':'failed','reason':str(error)}
                st=r.get('status')
                if r.get('elf'):self.events.put(('games_update_elf',(p['id'],r['elf'])))
                if st=='installed':updated.append(f"{name} -> {r.get('version') or 'latest'}")
                elif st=='kept':kept.append(f"{name}: {r.get('reason')}")
                elif st in ('up to date','skipped','no target'):current+=1
                else:failed.append(f"{name}: {r.get('reason') or r.get('error') or st}")
            lines=[f'Updated: {len(updated)}   Kept on purpose: {len(kept)}   Already current or not applicable: {current}   Failed: {len(failed)}']
            if updated:lines+=['','Updated (each needs a fresh Attempt native build):']+updated
            if kept:lines+=['','Kept:']+kept
            if failed:lines+=['','Failed:']+failed[:30]
            self.events.put(('games_update_done','\n'.join(lines)))
        threading.Thread(target=run,daemon=True).start()

    def export_port_folder(self):
        try:project=copy.deepcopy(self.selected()[0])
        except ValueError as error:
            self.status.set(str(error));return
        def chosen(candidate):
            exe=candidate.get('native_exe')
            dest=Path(self.studio.home/'projects').resolve().parent/'Ports'
            def run():
                from .port_export import export_port
                try:
                    folder=export_port(exe,dest,project.get('name','game'),[],
                                       emit=lambda m:self.events.put(('games_update_status',m)))
                    self.events.put(('games_update_done',f'Port folder ready: {folder}'))
                except Exception as error:self.events.put(('games_update_done','Export failed: '+str(error)))
            threading.Thread(target=run,daemon=True).start()
        try:self.choose_completed_candidate(project,chosen,title='Export a finished build as a port folder',button_text='Export this build')
        except ValueError as error:self.status.set(str(error))

    def display_settings_dialog(self):
        from .port_export import DISPLAY_OPTIONS, NOT_YET, read_display_ini, write_display_ini
        try:project=copy.deepcopy(self.selected()[0])
        except ValueError as error:
            self.status.set(str(error));return
        def chosen(candidate):
            exe=Path(candidate.get('native_exe') or '')
            if not exe.is_file():
                self.status.set('That build has no executable.');return
            ini=exe.parent/'display.ini';current=read_display_ini(ini)
            window=tk.Toplevel(self);window.title('Display settings - '+project.get('name',''))
            frame=ttk.Frame(window,padding=14);frame.pack(fill='both',expand=True)
            ttk.Label(frame,text=str(ini),wraplength=640).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,8))
            fields={}
            for r,(key,label,choices,default) in enumerate(DISPLAY_OPTIONS,1):
                ttk.Label(frame,text=label).grid(row=r,column=0,sticky='w',padx=(0,10),pady=2)
                var=tk.StringVar(value=current.get(key,default))
                ttk.Combobox(frame,textvariable=var,values=list(choices),width=16).grid(row=r,column=1,sticky='w')
                fields[key]=var
            r=len(DISPLAY_OPTIONS)+1
            for key,text in NOT_YET.items():
                ttk.Label(frame,text=text,foreground='#666').grid(row=r,column=0,columnspan=2,sticky='w');r+=1
            def save():
                values={k:v.get().strip() for k,v in fields.items()}
                values={k:v for k,v in values.items() if v and v!=dict((o[0],o[3]) for o in DISPLAY_OPTIONS).get(k)}
                try:write_display_ini(ini,values)
                except (OSError,ValueError) as error:
                    messagebox.showerror('Display settings',str(error));return
                self.status.set('Display settings saved: '+str(ini));window.destroy()
            buttons=ttk.Frame(frame);buttons.grid(row=r,column=0,columnspan=2,sticky='w',pady=(10,0))
            self.button(buttons,'Save',save,True);self.button(buttons,'Cancel',window.destroy)
        try:self.choose_completed_candidate(project,chosen,title='Display settings for which build?',button_text='Edit this build')
        except ValueError as error:self.status.set(str(error))

    def prune_builds_async(self, project_ids):
        """After a successful fix build: delete that game's older builds in the background."""
        ids=[i for i in project_ids if i]
        if not ids:return
        def work():
            from .build_cleanup import prune_project
            for pid in ids:
                p=next((q for q in self.studio.state.get('projects',[]) if q.get('id')==pid),None)
                if p is None:continue
                try:
                    result=prune_project(self.studio,p)
                    if result.get('removed'):
                        self.events.put(('fix_status',f"Kept the newest build of {p.get('name')}; removed {len(result['removed'])} older one(s)."))
                except Exception as error:
                    self.events.put(('fix_status','Old-build cleanup skipped: '+str(error)))
            self.events.put(('refresh',None))
        threading.Thread(target=work,daemon=True).start()

    def clean_up_old_builds(self):
        """One-time sweep: every game keeps only its newest finished build."""
        if not messagebox.askyesno('Clean up old builds',
                'Delete every older build of every game, keeping only the newest build of each?\n\n'
                'Game files, save data and the sources used for future fixes are kept. '
                'Games that are building, being fixed or being tested are skipped.'):
            return
        self.compiled_status.set('Cleaning up old builds...')
        def work():
            from .build_cleanup import prune_all
            try:
                totals=prune_all(self.studio,lambda m:self.events.put(('fix_status',m)))
                msg=f"Cleanup done: removed {totals['removed']} old build(s) from {totals['games']} game(s)."
                if totals['failed']:msg+=f" {totals['failed']} could not be fully removed (see workspace/cleanup.log)."
            except Exception as error:
                msg='Cleanup failed: '+str(error)
            self.events.put(('fix_status',msg));self.events.put(('refresh',None))
        threading.Thread(target=work,daemon=True).start()

    def apply_fixes_selected(self, projects=None):
        """Apply every available fix to just the selected game(s) in this tab.
        Runs on its own worker, so it can run beside "Apply fixes to games"
        (each game has its own lock, so the two never build the same game)."""
        if projects is None:
            try:
                projects=self.selected()
            except ValueError as error:
                self.fix_status.set(str(error));return
        if getattr(self,'single_fix_busy',False):
            self.fix_status.set('A selected-game fix is already running; wait for it or use Stop fixes.');return
        self.single_fix_busy=True
        self.single_fix_cancel=threading.Event()
        names=', '.join(p.get('name','?') for p in projects)
        self.fix_status.set('Selected game fix started: '+names)
        def run():
            done=[];failed=[]
            try:
                from . import game_fixes
                for p in projects:
                    if self.single_fix_cancel.is_set():break
                    name=p.get('name','?')
                    def emit(message,_name=name):self.events.put(('fix_status',f'[selected] {_name}: {message}'))
                    emit.repair_progress=lambda value:self.events.put(('repair_progress_selected',value))
                    self._single_fix_pid=p.get('id')
                    try:plan=game_fixes.preview(self.studio,p,force=True)
                    except Exception as error:plan={'available':False,'reason':str(error)}
                    if not plan.get('available'):
                        failed.append(f"{name} ({plan.get('reason','no build to fix')})");continue
                    emit('building fixes ('+'; '.join(plan.get('fixes',[]))+')')
                    try:result=game_fixes.run(self.studio,plan,cancel=self.single_fix_cancel,emit=emit)
                    except Exception as error:result={'status':'failed','reason':str(error)}
                    if result.get('status')=='ready':
                        self.events.put(('fix_ready_one',result));done.append(name)
                    else:failed.append(f"{name} ({result.get('reason') or result.get('status')})")
                msg=f"Selected game fix finished. Fixed: {', '.join(done) or 'none'}."
                if failed:msg+=f" Not fixed: {'; '.join(failed)}."
                self.events.put(('single_fix_done',msg))
            except Exception as error:self.events.put(('single_fix_done','Selected game fix failed: '+str(error)))
        self.single_fix_worker=threading.Thread(target=run,daemon=True)
        self.single_fix_worker.start()

    def apply_game_fixes(self, projects=None):
        """Apply every available fix to every game that needs it, one game at a
        time, quickest first; each finished game switches to its fixed build.
        With an explicit project list (a queued request) only those games are
        built; games with hand-made repairs still keep their selected build."""
        if self.fix_busy:
            self.fix_status.set('Fix preparation is already running.');return
        self.fix_busy=True;self.fix_cancel.clear()
        self._fix_whole_library = projects is None   # only a full run goes on to build unbuilt games
        self.fix_status.set('Checking every game for available fixes...' if projects is None else
                            'Applying fixes to: '+', '.join(p.get('name','?') for p in projects))
        def run():
            done=[];failed=[]
            try:
                from . import game_fixes
                def emit(message):self.events.put(('fix_status_all',str(message)))
                emit.repair_progress=lambda value:self.events.put(('repair_progress_all',value))
                if projects is None:
                    plans=game_fixes.plan_all(self.studio,emit,self.fix_cancel)
                else:
                    plans=[]
                    for p in projects:
                        try:plan=game_fixes.preview(self.studio,p,force=True)
                        except Exception as error:plan={'available':False,'reason':str(error)}
                        if plan.get('available'):plans.append(plan)
                        else:emit(f"{p.get('name','?')}: {plan.get('reason','no build to fix')}")
                if not plans:
                    self.events.put(('fix_all_done','No games need fixes right now.'));return
                names=', '.join(pl['project'].get('name','?') for pl in plans)
                emit(f'{len(plans)} game(s) to fix, quickest first: {names}')
                for i,plan in enumerate(plans,1):
                    if self.fix_cancel.is_set():break
                    name=plan['project'].get('name','?')
                    self._all_fix_pid=plan.get('project_id');self._all_fix_count=(i-1,len(plans))
                    def emit_game(message,_i=i,_n=len(plans),_name=name):
                        self.events.put(('fix_status_all',f'[{_i}/{_n}] {_name}: {message}'))
                    emit_game.repair_progress=emit.repair_progress
                    emit_game('building fixes ('+'; '.join(plan.get('fixes',[]))+')')
                    try:result=game_fixes.run(self.studio,plan,cancel=self.fix_cancel,emit=emit_game)
                    except Exception as error:result={'status':'failed','reason':str(error)}
                    if result.get('status')=='ready':
                        self.events.put(('fix_ready_one',result));done.append(name)
                    else:failed.append(f"{name} ({result.get('reason') or result.get('status')})")
                self._all_fix_pid=None
                self.events.put(('all_fix_bars_done',f"Apply fixes to games: finished {len(done)} of {len(plans)}"))
                msg=f"Fixes finished. Fixed: {', '.join(done) or 'none'}."
                if failed:msg+=f" Failed: {'; '.join(failed)}."
                if self.fix_cancel.is_set():msg='Fixes stopped. '+msg
                self.events.put(('fix_all_done',msg))
            except Exception as error:self.events.put(('fix_error',str(error)))
        self.fix_worker=threading.Thread(target=run,daemon=True)
        self.fix_worker.start()

    def start_game_fixes(self, project, candidate):
        if self.fix_busy:return
        self.fix_busy=True;self.fix_cancel.clear()
        self.fix_status.set('Checking available fixes: '+project['name'])
        def run():
            try:
                from . import game_fixes
                preview=game_fixes.preview(self.studio,project,candidate)
                if not preview['available']:
                    self.events.put(('fix_error',preview['reason']));return
                def emit(message):self.events.put(('fix_status',str(message)))
                emit.repair_progress=lambda value:self.events.put(('repair_progress',value))
                result=game_fixes.run(self.studio,preview,cancel=self.fix_cancel,emit=emit)
                self.events.put(('fix_ready',result))
            except Exception as error:self.events.put(('fix_error',str(error)))
        self.fix_worker=threading.Thread(target=run,daemon=True)
        self.fix_worker.start()

    def stop_fixes(self):
        if getattr(self,'single_fix_busy',False):
            self.single_fix_cancel.set()
        if self.fix_busy:
            self.fix_cancel.set()
            self.fix_status.set('Stopping fix preparation; build queue and manual tests continue.')

    def open_path(self, path):
        path = str(Path(path).resolve())
        if os.name == "nt":
            os.startfile(path)
        elif os.sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_project(self):
        self.open_path(self.studio.project_dir(self.selected()[0]))

    def open_mods(self):
        p = self.selected()[0]
        self.open_path(self.studio.project_dir(p) / "mods")
        self.status.set("Mod workspace only. Asset conversion and runtime loading depend on the game's port.")

    def export(self):
        if self.busy:
            raise RuntimeError("Wait for the current job before exporting")
        p = self.selected()[0]
        destination = filedialog.asksaveasfilename(title="Export logs and diagnostics", defaultextension=".zip",
                      initialfile=f"PS3-{p['id']}-diagnostics.zip", filetypes=[("ZIP archive", "*.zip")])
        if destination:
            self.studio.export(p, destination)
            self.status.set("Diagnostic ZIP saved · review local paths and logs before sharing")

    def report_callback_exception(self, exc, val, tb):
        messagebox.showerror("PS3 Port Studio", str(val))

    def close(self):
        self.stop_fulton()
        self.stop_fixes()
        if self.busy or (self.fix_worker and self.fix_worker.is_alive()) or (self.manual_launch_worker and self.manual_launch_worker.is_alive()) or (self.fulton_worker and self.fulton_worker.is_alive()):
            self.stop()
            self.status.set("Stopping current job before closing…")
            self.after(200, self.wait_close)
        else:
            self.destroy()

    def wait_close(self):
        if ((self.worker and self.worker.is_alive()) or
                (self.fulton_worker and self.fulton_worker.is_alive()) or
                (self.fix_worker and self.fix_worker.is_alive()) or
                (self.manual_launch_worker and self.manual_launch_worker.is_alive()) or
                (self.manual_stop_worker and self.manual_stop_worker.is_alive())):
            self.after(200, self.wait_close)
        else:
            self.destroy()
