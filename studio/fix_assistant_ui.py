# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Fix assistant tab: diagnose, then fix it yourself or ask an AI service."""
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from . import fix_assistant as fa


class FixAssistantPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.studio = app, app.studio
        self.info, self.reply = None, ""
        ttk.Label(self, text="Fix assistant", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(self, text="Pick a game to see what went wrong. Then fix it yourself, or ask an AI service you set up. "
                  "Nothing is sent anywhere unless you choose Ask AI and press Send.", wraplength=900).pack(anchor="w", pady=(4, 8))
        row = ttk.Frame(self); row.pack(fill="x")
        ttk.Label(row, text="Game").pack(side="left", padx=(0, 6))
        self.game = ttk.Combobox(row, state="readonly", width=60)
        self.game.pack(side="left", padx=(0, 6))
        app.button(row, "Refresh list", self.reload_games)
        app.button(row, "Diagnose", self.diagnose, True)
        self.mode = tk.StringVar(value="manual")
        row = ttk.Frame(self); row.pack(fill="x", pady=6)
        ttk.Radiobutton(row, text="I'll fix it myself", value="manual", variable=self.mode, command=self.show_mode).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(row, text="Ask AI for a fix", value="ai", variable=self.mode, command=self.show_mode).pack(side="left")
        self.diag = scrolledtext.ScrolledText(self, height=11, wrap="word", font=("Consolas", 9))
        self.diag.pack(fill="both", expand=True, pady=(0, 6))
        self.manual = ttk.Frame(self)
        row = ttk.Frame(self.manual); row.pack(fill="x")
        app.button(row, "Open attempt folder", lambda: self.open_info("attempt"))
        app.button(row, "Open build log", lambda: self.open_info("build_log_path"))
        app.button(row, "Open test log", lambda: self.open_info("test_log_path"))
        app.button(row, "Copy diagnosis", self.copy)
        app.button(row, "Rebuild with my changes", self.rebuild, True)
        self.ai = ttk.Frame(self)
        grid = ttk.Frame(self.ai); grid.pack(fill="x")
        settings = fa.ai_settings(self.studio)
        self.url, self.model, self.key = (tk.StringVar(value=settings[k]) for k in ("url", "model", "key"))
        for i, (label, var, show) in enumerate((("Service address", self.url, ""), ("Model", self.model, ""), ("API key (blank for local servers)", self.key, "*"))):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(grid, textvariable=var, show=show, width=70).grid(row=i, column=1, sticky="we", pady=2)
        grid.columnconfigure(1, weight=1)
        ttk.Label(self.ai, text="Your note to the AI (optional)").pack(anchor="w", pady=(6, 0))
        self.question = tk.Text(self.ai, height=3, wrap="word"); self.question.pack(fill="x")
        row = ttk.Frame(self.ai); row.pack(fill="x", pady=4)
        app.button(row, "Save AI settings", self.save_ai)
        app.button(row, "Preview request", self.preview)
        self.send_button = app.button(row, "Send", self.send, True)
        app.button(row, "Apply suggested patch", self.apply)
        app.button(row, "Undo last patch", self.undo)
        app.button(row, "Rebuild with this fix", self.rebuild)
        self.show_mode()
        self.reload_games()

    # ---------------------------------------------------------------- helpers
    def project(self):
        index = self.game.current()
        if index < 0:
            raise ValueError("Pick a game first.")
        return self.projects[index]

    def say(self, text):
        self.diag.delete("1.0", "end"); self.diag.insert("1.0", text)

    def guard(self, fn):
        try:
            return fn()
        except Exception as error:
            messagebox.showerror("Fix assistant", str(error))

    def reload_games(self):
        self.projects = sorted(self.studio.state["projects"], key=lambda p: p.get("name", "").casefold())
        self.game["values"] = [f"{p.get('name','')}  [{p.get('status','')}]" for p in self.projects]

    def show_mode(self):
        (self.ai if self.mode.get() == "manual" else self.manual).pack_forget()
        (self.manual if self.mode.get() == "manual" else self.ai).pack(fill="x")
        if self.info and self.mode.get() == "manual":
            self.say(fa.diagnosis_text(self.info) + "\n\n" + fa.manual_guide(self.info))

    # ---------------------------------------------------------------- actions
    def diagnose(self):
        def go():
            self.info = fa.gather(self.studio, self.project())
            text = fa.diagnosis_text(self.info)
            self.say(text + ("\n\n" + fa.manual_guide(self.info) if self.mode.get() == "manual" else ""))
        self.guard(go)

    def need_info(self):
        if not self.info:
            raise ValueError("Press Diagnose first.")
        return self.info

    def open_info(self, key):
        def go():
            value = self.need_info().get(key)
            if not value:
                raise ValueError("Nothing to open yet for this game.")
            self.app.open_path(value)
        self.guard(go)

    def copy(self):
        self.clipboard_clear(); self.clipboard_append(self.diag.get("1.0", "end"))

    def rebuild(self):
        self.guard(lambda: self.app.run_projects([self.project()], "rebuild"))

    def save_ai(self):
        fa.save_ai_settings(self.studio, self.url.get(), self.model.get(), self.key.get())
        self.app.status.set("AI settings saved in your workspace.")

    def messages(self):
        return fa.build_request(self.need_info(), self.question.get("1.0", "end"))

    def preview(self):
        self.guard(lambda: self.say("\n\n".join(f"[{m['role']}]\n{m['content']}" for m in self.messages())))

    def send(self):
        def go():
            messages = self.messages()
            self.save_ai()
            settings = fa.ai_settings(self.studio)
            self.send_button.configure(state="disabled")
            self.say("Waiting for the AI service...")
            def work():
                try:
                    text = fa.ask(settings, messages)
                except Exception as error:
                    text = f"AI request failed: {error}"
                self.after(0, lambda: self.got_reply(text))
            threading.Thread(target=work, daemon=True).start()
        self.guard(go)

    def got_reply(self, text):
        self.send_button.configure(state="normal")
        self.reply = text
        patch = fa.extract_patch(text)
        self.say(text + ("\n\n[A patch was suggested. Review it, then press Apply suggested patch.]" if patch else ""))

    def apply(self):
        def go():
            info = self.need_info()
            patch = fa.extract_patch(self.reply)
            if not patch:
                raise ValueError("The last AI reply has no patch to apply.")
            files = fa.patch_files(patch)
            if not messagebox.askyesno("Apply patch", "Change these files in the build attempt?\n\n" + "\n".join(files) +
                                       "\n\nBackups are kept so you can undo."):
                return
            fix_id = fa.apply_patch(info["attempt"], patch)
            messagebox.showinfo("Fix assistant", f"Applied (fix {fix_id}). Press Rebuild with this fix, then test.")
        self.guard(go)

    def undo(self):
        def go():
            info = self.need_info()
            fixes = fa.list_fixes(info["attempt"]) if info.get("attempt") else []
            if not fixes:
                raise ValueError("No applied fixes to undo for this attempt.")
            fa.undo(info["attempt"], fixes[-1])
            messagebox.showinfo("Fix assistant", f"Undid fix {fixes[-1]}.")
        self.guard(go)
