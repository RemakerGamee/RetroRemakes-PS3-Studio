# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Patch packs tab: install, create and assign community fix sets."""
from __future__ import annotations
import threading
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import webbrowser

from . import patch_packs as pp
from . import community_packs as cp
from .core import ROOT


class PatchPacksPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.studio = app, app.studio
        ttk.Label(self, text="Patch packs", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(self, text="A patch pack is a set of fixes for one game, like a port's folder of numbered patches. "
                  "When a game matches a pack, the Studio applies the pack to its own copy of the recompiler for that game, "
                  "so other games are not affected. Anyone can make one; see PATCH-PACKS.md.",
                  wraplength=900).pack(anchor="w", pady=(4, 8))
        row = ttk.Frame(self); row.pack(fill="x")
        app.button(row, "Add pack (folder)…", lambda: self.add(False), True)
        app.button(row, "Add pack (ZIP)…", lambda: self.add(True))
        app.button(row, "Make pack from a patch folder…", self.make)
        app.button(row, "Remove pack", self.remove)
        app.button(row, "Share selected pack…", self.share)
        app.button(row, "Open packs folder", self.open_folder)
        app.button(row, "How to make a pack", lambda: app.open_path(ROOT / "PATCH-PACKS.md"))
        cols = ("name", "version", "author", "games", "patches", "backend")
        ttk.Label(self, text="Installed packs", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(8, 0))
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=5)
        for col, title, width in zip(cols, ("Pack", "Version", "Author", "Games", "Patches", "Backend revision"),
                                     (260, 80, 120, 170, 80, 160)):
            self.tree.heading(col, text=title); self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=6)
        self.problems = tk.StringVar()
        ttk.Label(self, textvariable=self.problems, foreground="#a33", wraplength=900).pack(anchor="w")
        lib = ttk.LabelFrame(self, text="Community library", padding=8); lib.pack(fill="both", expand=True, pady=6)
        settings = self.studio.state["settings"]
        row = ttk.Frame(lib); row.pack(fill="x")
        self.auto = tk.BooleanVar(value=settings.get("community_auto", True))
        ttk.Checkbutton(row, text="Keep in sync automatically (at start-up and every 6 hours)", variable=self.auto,
                        command=self.save_library_settings).pack(side="left", padx=(0, 12))
        ttk.Label(row, text="Install automatically:").pack(side="left", padx=(0, 4))
        self.scope_labels = {"mine": "Packs for games in my library", "all": "Every community pack", "none": "Nothing (updates only)"}
        self.scope = ttk.Combobox(row, state="readonly", width=30, values=list(self.scope_labels.values()))
        self.scope.set(self.scope_labels.get(settings.get("community_scope", "mine"), self.scope_labels["mine"]))
        self.scope.pack(side="left")
        self.scope.bind("<<ComboboxSelected>>", lambda e: self.save_library_settings())
        lcols = ("name", "version", "author", "games", "yours", "status")
        self.library_tree = ttk.Treeview(lib, columns=lcols, show="headings", height=7)
        for col, title, width in zip(lcols, ("Pack", "Version", "Author", "Title IDs", "Your games", "Status"),
                                     (240, 70, 110, 160, 170, 140)):
            self.library_tree.heading(col, text=title); self.library_tree.column(col, width=width, anchor="w")
        self.library_tree.pack(fill="both", expand=True, pady=4)
        self.library_tree.bind("<<TreeviewSelect>>", lambda e: self.show_entry())
        self.library_info = tk.StringVar(value="Shared by players. Packs are checked against the community list before installing.")
        ttk.Label(lib, textvariable=self.library_info, wraplength=900).pack(anchor="w")
        row = ttk.Frame(lib); row.pack(fill="x", pady=(4, 0))
        self.sync_button = app.button(row, "Sync now", lambda: self.sync(manual=True), True)
        app.button(row, "Install / update selected", self.install_entry)
        app.button(row, "Open author's page", self.open_author)
        app.button(row, "How to add your pack", lambda: webbrowser.open(cp.CONTRIBUTE_URL))
        self.library_rows, self.syncing = [], False
        self.after(4000, self.auto_sync)
        box = ttk.LabelFrame(self, text="Use a pack for one game", padding=8); box.pack(fill="x", pady=6)
        ttk.Label(box, text="Games pick a matching pack automatically. Override it here if needed.").pack(anchor="w")
        row = ttk.Frame(box); row.pack(fill="x", pady=4)
        self.game = ttk.Combobox(row, state="readonly", width=45); self.game.pack(side="left", padx=(0, 6))
        self.choice = ttk.Combobox(row, state="readonly", width=40); self.choice.pack(side="left", padx=(0, 6))
        app.button(row, "Save for this game", self.save_choice, True)
        app.button(row, "Refresh", self.refresh)
        self.game.bind("<<ComboboxSelected>>", lambda e: self.show_choice())
        self.refresh()

    def guard(self, fn):
        try:
            return fn()
        except Exception as error:
            messagebox.showerror("Patch packs", str(error))

    def refresh(self):
        self.packs, bad = pp.installed(self.studio.home)
        self.tree.delete(*self.tree.get_children())
        for i, pack in enumerate(self.packs):
            games = ", ".join(pack["title_ids"]) or f"{len(pack['elf_sha256'])} exact EBOOT(s)"
            patches = len(pack["backend_patches"]) + len(pack["project_patches"])
            self.tree.insert("", "end", iid=str(i), values=(pack["name"], pack["version"], pack["author"], games, patches,
                             pack["revision"] or "Studio default"))
        self.problems.set("\n".join(f"Not loaded: {folder.name}: {error}" for folder, error in bad))
        self.projects = sorted(self.studio.state["projects"], key=lambda p: p.get("name", "").casefold())
        self.game["values"] = [p.get("name", "") for p in self.projects]
        self.options = [("auto", "Automatic (matching pack)"), ("none", "No pack")] + [(p["id"], p["name"]) for p in self.packs]
        self.choice["values"] = [label for _, label in self.options]
        self.show_choice()
        if hasattr(self, "library_tree"):
            self.show_library()

    def show_choice(self):
        index = self.game.current()
        if index < 0:
            self.choice.set(""); return
        current = self.projects[index].get("patch_pack_choice", "auto")
        labels = dict(self.options)
        self.choice.set(labels.get(current, f"{current} (not installed)"))

    def save_choice(self):
        def go():
            gi, ci = self.game.current(), self.choice.current()
            if gi < 0 or ci < 0:
                raise ValueError("Pick a game and a choice first.")
            project = self.projects[gi]
            value = self.options[ci][0]
            if value == "auto":
                project.pop("patch_pack_choice", None)
            else:
                project["patch_pack_choice"] = value
            self.studio.save()
            self.app.status.set(f"{project.get('name','Game')}: patch pack set to {self.options[ci][1]}. Start a new build to use it.")
        self.guard(go)

    def add(self, from_zip):
        path = (filedialog.askopenfilename(title="Choose a patch pack ZIP", filetypes=[("ZIP files", "*.zip")]) if from_zip
                else filedialog.askdirectory(title="Choose the patch pack folder (it contains pack.json)"))
        if not path:
            return
        def go():
            try:
                pack = pp.install(self.studio.home, path)
            except pp.PackError as error:
                if "already installed" not in str(error) or not messagebox.askyesno("Patch packs", f"{error}. Replace it?"):
                    raise
                pack = pp.install(self.studio.home, path, replace=True)
            self.refresh()
            messagebox.showinfo("Patch packs", f"Installed {pack['name']}.")
        self.guard(go)

    def remove(self):
        def go():
            sel = self.tree.selection()
            if not sel:
                raise ValueError("Select a pack first.")
            pack = self.packs[int(sel[0])]
            if messagebox.askyesno("Patch packs", f"Remove {pack['name']}? Games already built with it keep their builds."):
                pp.remove(self.studio.home, pack["id"]); self.refresh()
        self.guard(go)

    def open_folder(self):
        folder = pp.packs_dir(self.studio.home); folder.mkdir(parents=True, exist_ok=True)
        self.app.open_path(folder)

    def make(self):
        folder = filedialog.askdirectory(title="Choose the folder that holds the numbered .patch files")
        if not folder:
            return
        win = tk.Toplevel(self); win.title("Make a patch pack"); win.transient(self.winfo_toplevel())
        frame = ttk.Frame(win, padding=14); frame.pack(fill="both", expand=True)
        fields = [("Pack name", "name", ""), ("Game title IDs (comma separated, e.g. BCUS98234, BCES01007)", "titles", ""),
                  ("ps3recomp revision the patches were made against (commit or tag; blank = Studio default)", "revision", ""),
                  ("Author", "author", ""), ("Homepage", "homepage", ""), ("License", "license", "")]
        values = {}
        for label, key, default in fields:
            ttk.Label(frame, text=label).pack(anchor="w")
            values[key] = tk.StringVar(value=default)
            ttk.Entry(frame, textvariable=values[key], width=70).pack(fill="x", pady=(0, 6))
        fixes = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Apply RetroRemakes backend fixes before these patches "
                        "(only if the patches were made on top of them)", variable=fixes).pack(anchor="w", pady=4)
        status = tk.StringVar(value=f"Patches from: {folder}")
        ttk.Label(frame, textvariable=status, wraplength=600).pack(anchor="w", pady=4)
        def create():
            def work():
                try:
                    pack = pp.create_from_folder(self.studio.home, folder, values["name"].get(), values["titles"].get(),
                                                 values["revision"].get(), values["author"].get(), values["homepage"].get(),
                                                 values["license"].get(), studio_fixes=fixes.get())
                    self.after(0, lambda: (win.destroy(), self.refresh(),
                                           messagebox.showinfo("Patch packs", f"Made {pack['name']} with {len(pack['backend_patches'])} patches.")))
                except Exception as error:
                    message = str(error)
                    self.after(0, lambda: status.set("Could not make the pack: " + message))
            status.set("Copying patches…")
            threading.Thread(target=work, daemon=True).start()
        row = ttk.Frame(frame); row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Make pack", command=create).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left")

    # ------------------------------------------------------------ community
    def selected_pack(self):
        sel = self.tree.selection()
        if not sel:
            raise ValueError("Select a pack in the list first.")
        return self.packs[int(sel[0])]

    def share(self):
        def go():
            pack = self.selected_pack()
            folder = filedialog.askdirectory(title="Where should the shareable ZIP be saved?")
            if not folder:
                return
            path, entry = cp.export_zip(pack, folder)
            win = tk.Toplevel(self); win.title("Share " + pack["name"])
            frame = ttk.Frame(win, padding=14); frame.pack(fill="both", expand=True)
            steps = ("To share this pack with everyone:\n"
                     f"1. Upload {path.name} (saved in {folder}) to a release on your GitHub.\n"
                     "2. Put that file's download address in \"url\" below.\n"
                     "3. Open the community packs repository and add this entry to index.json "
                     "(Contribute > pull request). The full guide is under How to contribute.")
            ttk.Label(frame, text=steps, wraplength=640, justify="left").pack(anchor="w")
            text = scrolledtext.ScrolledText(frame, height=16, width=90, font=("Consolas", 9))
            text.insert("1.0", json.dumps(entry, indent=2)); text.pack(fill="both", expand=True, pady=8)
            row = ttk.Frame(frame); row.pack(fill="x")
            ttk.Button(row, text="Copy entry", command=lambda: (win.clipboard_clear(), win.clipboard_append(text.get("1.0", "end")))).pack(side="left", padx=(0, 6))
            ttk.Button(row, text="Open community repository", command=lambda: webbrowser.open(cp.PACKS_REPO)).pack(side="left", padx=(0, 6))
            ttk.Button(row, text="Open ZIP folder", command=lambda: self.app.open_path(folder)).pack(side="left")
        self.guard(go)

    # ------------------------------------------------------------ community library
    def save_library_settings(self):
        settings = self.studio.state["settings"]
        settings["community_auto"] = bool(self.auto.get())
        settings["community_scope"] = next((k for k, v in self.scope_labels.items() if v == self.scope.get()), "mine")
        self.studio.save()
        if self.auto.get():
            self.sync()

    def show_library(self):
        self.library_rows, fetched = cp.library(self.studio)
        self.library_tree.delete(*self.library_tree.get_children())
        for i, row in enumerate(self.library_rows):
            self.library_tree.insert("", "end", iid=str(i), values=(row["name"], row["version"], row["author"],
                                     ", ".join(row.get("title_ids", [])), ", ".join(row["your_games"]), row["status"]))
        if not self.library_rows:
            self.library_info.set("No community packs yet. Press Sync now." if not fetched else
                                  f"The community library is empty (checked {fetched}). Be the first: share a pack!")

    def chosen_entry(self):
        sel = self.library_tree.selection()
        if not sel:
            raise ValueError("Select a pack in the community library first.")
        return self.library_rows[int(sel[0])]

    def show_entry(self):
        try:
            e = self.chosen_entry()
        except ValueError:
            return
        self.library_info.set(f"{e['name']} {e['version']} by {e['author']}. {e.get('description', '')} "
                              f"License: {e.get('license', '?')}.")

    def open_author(self):
        self.guard(lambda: webbrowser.open(_https_or_raise(self.chosen_entry().get("homepage", ""))))

    def auto_sync(self):
        try:
            if self.studio.state["settings"].get("community_auto", True) and cp.due(self.studio):
                self.sync()
            else:
                self.show_library()
        finally:
            self.after(30 * 60 * 1000, self.auto_sync)   # re-check every 30 min; syncs when 6 h have passed

    def sync(self, manual=False):
        if self.syncing:
            return
        self.syncing = True
        self.sync_button.configure(state="disabled")
        self.library_info.set("Syncing the community library…")
        settings = self.studio.state["settings"]
        scope = settings.get("community_scope", "mine")
        def work():
            try:
                result = cp.sync(self.studio, auto_install=scope != "none", scope=scope)
                self.after(0, lambda: self.synced(cp.describe(result), result["errors"], manual))
            except Exception as error:
                message = str(error)
                self.after(0, lambda: self.synced("Could not reach the community library: " + message, [], manual))
        threading.Thread(target=work, daemon=True).start()

    def synced(self, text, errors, manual):
        self.syncing = False
        self.sync_button.configure(state="normal")
        self.studio.save()
        self.refresh()
        self.library_info.set(text)
        if "installed " in text or "updated " in text or manual:
            self.app.status.set(text)
        if errors and manual:
            messagebox.showwarning("Community library", "\n".join(errors[:10]))

    def install_entry(self):
        def go():
            e = self.chosen_entry()
            if e["status"] == "your own pack (same id)" and not messagebox.askyesno(
                    "Community library", f"You have your own pack named '{e['id']}'. Replace it with the community one?"):
                return
            if e["status"] == "installed":
                raise ValueError("That pack is already installed and up to date.")
            self.library_info.set(f"Downloading {e['name']}…")
            def work():
                try:
                    pack = cp.install_entry(self.studio.home, e, replace=e["status"] != "not installed")
                    self.after(0, lambda: (self.refresh(), self.library_info.set(f"Installed {pack['name']} {pack['version']}.")))
                except Exception as error:
                    message = str(error)
                    self.after(0, lambda: self.library_info.set("Install failed: " + message))
            threading.Thread(target=work, daemon=True).start()
        self.guard(go)

    def title_of(self, project):
        try:
            analysis = json.loads(open(project.get("last_analysis", ""), encoding="utf-8").read())
            return (analysis.get("metadata") or {}).get("TITLE_ID", "")
        except (OSError, ValueError):
            return ""


def _https_or_raise(url):
    if not str(url).startswith("https://"):
        raise ValueError("This pack has no web page listed.")
    return url
