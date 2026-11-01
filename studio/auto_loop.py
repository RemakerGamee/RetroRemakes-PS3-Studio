# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Automatic build -> test -> report loop for every game in the Studio.

One game at a time:
  1. build it with every available fix (or "build anyway" when the last
     build was blocked),
  2. launch it for a timed test run (batch_queue.BatchTry),
  3. record the result in workspace/auto-loop/state.json and NEEDS-FIX.md.

A game passes when it keeps presenting frames for the whole run at or above
the target frame rate with no error signature.  Any other game waits until the
fix fingerprint changes (backend sources, overlay, recipes, game fixes or
workspace/auto-loop/fix-generation.txt), then goes back into the queue.

The GUI calls tick() from its 30 s poll; nothing here blocks.
Turn the loop on/off with workspace/auto-loop/control.json {"enabled": true}.
"""
import hashlib
import json
import time
from pathlib import Path

TARGET_FPS = 120.0          # goal: 120 fps at 1440p (and 60 fps at 4K, tested later)
TEST_DISPLAY = {"RR_OUTPUT": "2560x1440", "RR_VSYNC": "0", "RR_FPS_LIMIT": "0"}
TEST_SECONDS = 120
BUILD_TIMEOUT = 6 * 3600
TEST_TIMEOUT = 30 * 60
WATCHED = ("workspace/tools/ps3recomp/libs", "workspace/tools/ps3recomp/runtime",
           "workspace/tools/ps3recomp/templates", "workspace/tools/ps3recomp/tools")
WATCHED_FILES = ("studio/backend_overlay/manifest.json", "studio/wws_policy_recipes.py", "studio/spurs_job_recipes.py", "studio/wws_jobmod_recipes.py",
                 "studio/game_fixes.py", "studio/game_patches.py")


def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for _ in range(10):
        try:
            tmp.replace(path); return
        except PermissionError:
            time.sleep(0.2)


def fix_fingerprint(root):
    """Changes whenever any fix source changes."""
    root = Path(root); h = hashlib.sha256()
    for rel in WATCHED:
        base = root / rel
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                if f.is_file() and f.suffix in (".c", ".cpp", ".h", ".hpp", ".py", ".txt", ".cmake") and ".bak" not in f.name:
                    st = f.stat(); h.update(f"{f.relative_to(root)}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    for rel in WATCHED_FILES + ("workspace/auto-loop/fix-generation.txt",):
        f = root / rel
        if f.is_file():
            h.update(rel.encode() + hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()[:16]


def judge(entry, target_fps=TARGET_FPS):
    """passed / slow / failed from a BatchTry result entry."""
    cls = entry.get("classification")
    if cls == "running" and not entry.get("exited_on_its_own"):
        fps = entry.get("fps_samples") or [entry.get("fps_last", 0.0)]
        low = min(fps[-3:]) if fps else 0.0
        if low >= target_fps - 2:   # never dips below the target in the last samples
            return "passed"
        # Stable at the game's own 30/60 cap: runs fine, needs a frame-rate patch to go higher.
        return "running-capped" if low >= 28 and max(fps[-3:]) - low < 4 else "slow"
    return "failed"


class AutoLoop:
    def __init__(self, root, home):
        self.root = Path(root); self.folder = Path(home) / "auto-loop"
        self.state_path = self.folder / "state.json"
        self.state = _read(self.state_path, {})
        self.state.setdefault("schema", 1); self.state.setdefault("games", {})
        self.state.setdefault("phase", "idle"); self.state.setdefault("round", 0)

    # -- control
    def enabled(self):
        return bool(_read(self.folder / "control.json", {}).get("enabled", False))

    def target(self):
        """Current pass mark: stage 1 = 60 fps steady at 1440p, final goal 120 (set in control.json)."""
        return float(_read(self.folder / "control.json", {}).get("target_fps", 60))

    def set_enabled(self, on):
        data = _read(self.folder / "control.json", {}); data.update(schema=1, enabled=bool(on), changed_at=time.time())
        _write(self.folder / "control.json", data)

    def save(self):
        self.state["updated_at"] = time.time(); _write(self.state_path, self.state)
        (self.folder / "NEEDS-FIX.md").write_text(self.report(), encoding="utf-8")

    def log(self, text):
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.folder / "loop.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")

    # -- choosing
    def _game(self, p):
        g = self.state["games"].setdefault(p["id"], {"name": p.get("name"), "status": "pending", "attempts": 0})
        g["name"] = p.get("name"); return g

    def next_game(self, projects, fp):
        pri = _read(self.folder / "priority.json", {})
        priority = pri.get("games", [])
        if pri.get("only"):   # focus mode: only the listed games go through the loop
            projects = [p for p in projects if p["id"] in priority or p.get("name") in priority]
        order = {pid: i for i, pid in enumerate(priority)}
        ready = []
        for p in projects:
            g = self._game(p)
            if g["status"] == "passed" and g.get("target") == self.target():
                continue
            if g["status"] in ("failed", "slow", "not-built", "running-capped") and g.get("fingerprint") == fp:
                continue                      # waiting for a new fix
            ready.append(p)
        ready.sort(key=lambda p: (order.get(p["id"], order.get(p.get("name"), 10**6)),
                                  (p.get("name") or "").casefold()))   # priority games first, then A-Z
        return ready[0] if ready else None

    # -- cycle mode: test every built game, priority games first; failures get
    #    the latest fixes and are tested again; then the pass starts over.
    def cycle_seconds(self):
        return int(_read(self.folder / "control.json", {}).get("seconds", 60))

    def _built_ids(self):
        lib = _read(Path(self.folder).parent / "compiled-library.json", {})
        return {r.get("project_id") for r in lib.get("records", []) if r.get("native_exe")}

    def tick_cycle(self, host):
        now = time.time(); c = self.state.setdefault("cycle", {"queue": [], "i": 0, "pass": 0})
        phase, cur = self.state["phase"], self.state.get("current")
        fp = fix_fingerprint(self.root)
        if phase == "building":
            if host.busy() and now - self.state.get("since", now) < BUILD_TIMEOUT:
                return "building"
            self.state["games"].get(cur, {})["tested_fp"] = None   # test the new build next
            self.state.update(phase="idle"); self.save()
            return "built"
        if phase == "testing":
            if host.test_active() and now - self.state.get("since", now) < TEST_TIMEOUT:
                return "testing"
            entry = next((e for e in reversed(host.latest_results().get("games", [])) if e.get("project_id") == cur), None)
            g = self.state["games"].setdefault(cur, {"name": cur, "status": "pending", "attempts": 0})
            g["status"] = "failed" if entry is None else ("not-built" if entry.get("classification") == "not-built" else judge(entry, self.target()))
            g["last"] = {k: (entry or {}).get(k) for k in ("classification", "signature", "fps_last", "log", "stall_report")}
            g["tested_fp"] = fp; g["tested_at"] = now
            try:   # the run captured runtime-built SPU code: the next fix build can lift it
                log = (entry or {}).get("log")
                if log and g["status"] != "passed" and b"[RR-SNAP] captured" in Path(log).read_bytes():
                    g["fixed_fp"] = None
                    self.log(f"pass {c['pass']}: {g.get('name')}: new SPU code snapshots captured; rebuilding to lift them")
            except OSError:
                pass
            self.log(f"pass {c['pass']}: {g.get('name')}: {g['status']} - {g['last'].get('signature')}")
            if g["status"] == "passed" or g.get("fixed_fp") == fp:
                c["i"] += 1                     # nothing new to apply: move on
            self.state.update(phase="idle", current=None); self.save()
            return g["status"]
        if host.busy() or host.test_active():
            return "waiting"
        pri_ids = [i for i in _read(self.folder / "priority.json", {}).get("games", [])]
        def actionable(i):
            g = self.state["games"].get(i)
            if not g or g.get("status") == "pending":
                return True
            if g.get("status") == "passed":
                return g.get("tested_fp") != fp
            return g.get("fixed_fp") != fp or g.get("tested_fp") != fp
        focus_ready = any(actionable(i) for i in pri_ids if host.project(i))
        if c.get("scope") == "others" and focus_ready:
            c["i"] = len(c["queue"])              # new fixes for a focus game: go back to the focus games now
            self.log("new fixes available for a focus game; returning to the focus games")
        if c["i"] >= len(c["queue"]):
            if c.get("pass") and not c.get("active") and c.get("idle_fp") == fp:
                return "waiting-for-fixes"        # last pass changed nothing; wait for new fixes
            c["idle_fp"] = fp if not c.get("active") else None
            c["active"] = False
            pri = [i for i in _read(self.folder / "priority.json", {}).get("games", [])]
            built = self._built_ids()
            projects = {p["id"]: p for p in host.projects()}
            rest = sorted((i for i in built if i in projects and i not in pri),
                          key=lambda i: (projects[i].get("name") or "").casefold())
            if focus_ready or not rest:
                c.update(queue=[i for i in pri if i in projects], i=0, scope="focus")
            else:                                  # focus games are waiting for fixes: use the time on the others
                c.update(queue=rest, i=0, scope="others")
            c["pass"] += 1; c.pop("pass_", None)
            self.log(f"pass {c['pass']} starts ({c.get('scope')}): {len(c['queue'])} game(s)")
            if not c["queue"]:
                self.save(); return "no-built-games"
        pid = c["queue"][c["i"]]; p = host.project(pid)
        if p is None:
            c["i"] += 1; self.save(); return "skip"
        g = self.state["games"].setdefault(pid, {"name": p.get("name"), "status": "pending", "attempts": 0})
        g["name"] = p.get("name")
        if g.get("status") == "passed" and g.get("tested_fp") == fp:
            c["i"] += 1; self.save(); return "skip-passed"
        # Only rebuild a game that has been tested since its last build; otherwise
        # every backend edit during a build would trigger another build before a test.
        if g.get("status") not in ("pending", "passed") and g.get("fixed_fp") != fp and g.get("tested_fp"):
            g["attempts"] += 1; g["fixed_fp"] = fp
            self.state.update(phase="building", current=pid, since=now)
            c["active"] = True
            self.log(f"pass {c['pass']}: applying fixes to {p.get('name')}"); self.save()
            host.start_build(p, "fixes")
            return "building"
        if g.get("tested_fp") == fp and g.get("fixed_fp") == fp and g.get("status") != "pending":
            c["i"] += 1; self.save(); return "skip-unchanged"
        c["active"] = True
        self.state.update(phase="testing", current=pid, since=now)
        self.log(f"pass {c['pass']}: testing {p.get('name')}"); self.save()
        per_game = _read(self.folder / "priority.json", {}).get("seconds", {})
        host.start_test(p, int(per_game.get(pid, self.cycle_seconds())))
        return "testing"

    # -- the step called every poll
    def tick(self, host):
        """host: busy(), start_build(p, mode), start_test(p, seconds), test_active(), latest_results()"""
        if not self.enabled():
            return "off"
        if _read(self.folder / "control.json", {}).get("mode") == "cycle":
            return self.tick_cycle(host)
        now = time.time(); phase = self.state["phase"]; cur = self.state.get("current")
        if phase == "building":
            if host.busy() and now - self.state.get("since", now) < BUILD_TIMEOUT:
                return "building"
            p = host.project(cur)
            if p is None:
                self.state["phase"] = "idle"; self.save(); return "idle"
            host.start_test(p, TEST_SECONDS)
            self.state.update(phase="testing", since=now); self.log(f"testing {p.get('name')}"); self.save()
            return "testing"
        if phase == "testing":
            if host.test_active() and now - self.state.get("since", now) < TEST_TIMEOUT:
                return "testing"
            entry = next((e for e in reversed(host.latest_results().get("games", [])) if e.get("project_id") == cur), None)
            g = self.state["games"].get(cur, {})
            if entry is None:
                g["status"] = "failed"; g["last"] = {"signature": "no test result"}
            else:
                verdict = judge(entry, self.target())
                if entry.get("classification") == "not-built":
                    verdict = "not-built"
                g["status"] = verdict
                g["last"] = {k: entry.get(k) for k in ("classification", "signature", "fps_last", "fps_samples",
                                                        "log", "screenshots", "unresolved_functions", "stall_report")}
            g["fingerprint"] = self.state.get("building_fp"); g["tested_at"] = now; g["target"] = self.target()
            g.setdefault("history", []).append({"at": now, "status": g["status"],
                                                "signature": g.get("last", {}).get("signature")})
            g["history"] = g["history"][-20:]
            self.log(f"result {g.get('name')}: {g['status']} - {g.get('last', {}).get('signature')}")
            self.state.update(phase="idle", current=None); self.save()
            return g["status"]
        # idle
        if host.busy() or host.test_active():
            return "waiting"
        fp = fix_fingerprint(self.root)
        p = self.next_game(host.projects(), fp)
        if p is None:
            if self.state.get("all_done_fp") != fp:
                self.state["all_done_fp"] = fp; self.log("every game is passed or waiting for a fix"); self.save()
            return "waiting-for-fixes"
        g = self._game(p)
        mode = "anyway" if g.get("status") == "not-built" else "fixes"
        trials = _read(self.folder / "backend-trials.json", {}).get("games", {})
        trial = trials.get(p["id"])
        if trial and g.get("attempts", 0) % 2 == 1:
            # alternate: odd attempts try the trial backend as a plain rebuild
            p["backend_override"] = str(self.root / trial); mode = "retry"
        else:
            p.pop("backend_override", None)
        g["backend"] = p.get("backend_override") or "default"
        g["attempts"] += 1; g["status"] = "building"
        self.state.update(phase="building", current=p["id"], since=time.time(), building_fp=fp)
        self.state["round"] += 1
        self.log(f"building {p.get('name')} (attempt {g['attempts']}, {mode})"); self.save()
        host.start_build(p, mode)
        return "building"

    def report(self):
        games = self.state["games"].values()
        counts = {}
        for g in games:
            counts[g["status"]] = counts.get(g["status"], 0) + 1
        lines = ["# PS3 auto loop", "", f"Phase: {self.state['phase']}  current: {self.state.get('current')}  "
                 f"builds started: {self.state['round']}", "",
                 " | ".join(f"{k}: {v}" for k, v in sorted(counts.items())), "", "| game | status | tries | last fps | problem |",
                 "|---|---|---|---|---|"]
        for pid, g in sorted(self.state["games"].items(), key=lambda kv: (kv[1]["status"] == "passed", kv[1].get("name") or "")):
            last = g.get("last", {})
            lines.append(f"| {g.get('name')} [{pid}] | {g['status']} | {g['attempts']} | {last.get('fps_last') or ''} | {last.get('signature') or ''} |")
        return "\n".join(lines) + "\n"


class GuiHost:
    """Adapter from the Tk app to AutoLoop."""
    def __init__(self, app):
        self.app = app

    def busy(self):
        # The loop builds on the selected-game fix worker, so a full
        # "Apply fixes to games" run (fix_busy) doesn't block it.
        return bool(self.app.busy or getattr(self.app, "single_fix_busy", False))

    def projects(self):
        # Built games live on the Compiled tab and are usually "removed from queue";
        # the loop still tests and fixes them.
        return list(self.app.studio.state.get("projects", []))

    def project(self, pid):
        return next((p for p in self.projects() if p.get("id") == pid), None)

    def start_build(self, p, mode):
        if mode in ("anyway", "retry"):
            if mode == "anyway":
                p["spu_build_anyway"] = True
            self.app.studio.save(); self.app.run_projects([p], "retry")
        else:
            self.app.apply_fixes_selected([p])

    def start_test(self, p, seconds):
        import os
        os.environ.update(TEST_DISPLAY)   # every loop test runs at 1440p, uncapped
        self.app.batch_try_all([p], seconds)

    def test_active(self):
        return bool(self.app.batch_try and self.app.batch_try.active())

    def latest_results(self):
        bt = self.app.batch_try
        return bt.results if bt else {}
