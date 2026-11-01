# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Batch try and retry queue: let the Studio cycle through games unattended.

Batch try launches each built game (its selected build) for a fixed time
through the Studio's own validated launch path, takes screenshots, asks the
window to close, stops the process tree after a grace period, and classifies
what happened from the game's own log. Results go to
workspace/batch-try/<stamp>/results.json and REPORT.md (problems grouped,
largest group first); workspace/batch-try/latest.json points at the newest.

Remote control (the Studio checks every 30 s while it is open):
  workspace/batch-try/request.json
      {"schema": 1, "games": "all", "seconds": 120}
      {"schema": 1, "games": ["<project id>", "<exact game name>"], "seconds": 90,
       "screenshots": [20, 80]}
  workspace/batch-try/skip.json
      {"schema": 1, "skip": ["<project id or exact name>"], "reason": "..."}
  workspace/auto-repair/retry-request.json
      {"schema": 1, "games": ["<project id>"], "mode": "fixes" | "retry" | "anyway"}
      run when the build queue is idle: "fixes" = Apply fixes, "retry" = Retry
      blocked games (keep sources), "anyway" = Build blocked games anyway.

Requests can only name library games, durations and modes. Invalid requests
are moved aside as *.rejected-<time>.json. Save data is listed (names, sizes,
times) before and after each game and changes are reported; nothing is
copied, restored or deleted.
"""
from __future__ import annotations
import collections
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

DEFAULT_SECONDS = 120
MAX_SECONDS = 900
GRACE_SECONDS = 8
POLL_MS = 30000

_PERF = re.compile(r"\[RR-PERF\] presented_fps=([0-9.]+)")
_PERF_OLD = re.compile(r"\[PERF\] ([0-9.]+) batches/s")
_NID = re.compile(r"unresolved NID 0x([0-9A-Fa-f]{8})")
_SIGNALS = {
    "spu_halt": re.compile(r"\[spu-halt\]|RETURNED rc=-\d"),
    "pm_not_lifted": re.compile(r"PM NOT LIFTED"),
    "data_word": re.compile(r"\.word data executed"),
    "null_call": re.compile(r"\[null-call\]|bctr to NULL"),
    "exit_dialog": re.compile(r"ContentErrorDialog|OpenErrorCode"),
    "unlifted_spu": re.compile(r"branched into unlifted LS"),
    "out_of_memory": re.compile(r"out of memory", re.I),
}
_WATCH_LAST = re.compile(r"\[WATCHDOG\] (\d+s sample); last HLE call = 0x[0-9A-F]+ \(([^)]*)\)")
_WATCH_IN = re.compile(r"\[WATCHDOG\]\s+guest-tid (\d+)\s+in (\S+)")


def _now_stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path); return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def _reject(path, reason):
    target = path.with_name(f"{path.stem}.rejected-{_now_stamp()}.json")
    try:
        path.rename(target)
        _log(path.parent, f"rejected {path.name}: {reason}")
    except OSError:
        pass


def _log(folder, line):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    with (folder / "control.log").open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


def _nid_names(studio=None):
    """NID -> function name, computed from studio/ps3_function_names.txt (names
    only) plus the backend's own NID database."""
    import hashlib, struct
    suffix = bytes([0x67, 0x59, 0x65, 0x99, 0x04, 0x25, 0x04, 0x90, 0x56, 0x64, 0x27, 0x49, 0x94, 0x89, 0x74, 0x1A])
    names = {}
    try:
        lines = (Path(__file__).with_name("ps3_function_names.txt")).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for name in lines:
        name = name.strip()
        if name and not name.startswith("#"):
            nid = struct.unpack("<I", hashlib.sha1(name.encode() + suffix).digest()[:4])[0]
            names[f"{nid:08X}"] = name
    return names


# ---------------------------------------------------------------- requests

def _match_games(studio, wanted):
    projects = [p for p in studio.state.get("projects", []) if not p.get("queue_removed")]
    if wanted == "all":
        return projects
    if not isinstance(wanted, list) or not wanted or len(wanted) > 300:
        raise ValueError("games must be 'all' or a non-empty list")
    chosen, missing = [], []
    for item in wanted:
        if not isinstance(item, str):
            raise ValueError("game entries must be strings")
        match = next((p for p in studio.state.get("projects", []) if p.get("id") == item or p.get("name") == item), None)
        (chosen.append(match) if match else missing.append(item))
    if missing:
        raise ValueError("unknown games: " + ", ".join(missing[:10]))
    return chosen


def take_batch_request(home, studio):
    """Claim workspace/batch-try/request.json once. Returns (projects, seconds, shots) or None."""
    folder = Path(home) / "batch-try"; path = folder / "request.json"
    if not path.is_file():
        return None
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
        if req.get("schema") != 1:
            raise ValueError("schema must be 1")
        seconds = int(req.get("seconds", DEFAULT_SECONDS))
        if not 10 <= seconds <= MAX_SECONDS:
            raise ValueError(f"seconds must be 10..{MAX_SECONDS}")
        shots = sorted({int(s) for s in req.get("screenshots", [max(5, seconds // 3), max(10, seconds - 10)])
                        if 1 <= int(s) < seconds})[:8]
        projects = _match_games(studio, req.get("games", "all"))
    except (OSError, ValueError, TypeError) as exc:
        _reject(path, str(exc)); return None
    path.unlink(missing_ok=True)
    _log(folder, f"accepted batch try: {len(projects)} game(s), {seconds}s each")
    return projects, seconds, shots


def take_retry_request(home, studio):
    """Claim workspace/auto-repair/retry-request.json once. Returns (projects, mode) or None."""
    folder = Path(home) / "auto-repair"; path = folder / "retry-request.json"
    if not path.is_file():
        return None
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
        if req.get("schema") != 1:
            raise ValueError("schema must be 1")
        mode = req.get("mode", "fixes")
        if mode not in ("fixes", "retry", "anyway"):
            raise ValueError("mode must be fixes, retry or anyway")
        projects = _match_games(studio, req.get("games"))
    except (OSError, ValueError, TypeError) as exc:
        _reject(path, str(exc)); return None
    path.unlink(missing_ok=True)
    _log(folder, f"accepted retry ({mode}): " + ", ".join(p.get("name", "?") for p in projects))
    return projects, mode


# ---------------------------------------------------------------- one game

def _skip_list(folder):
    try:
        data = json.loads((Path(folder) / "skip.json").read_text(encoding="utf-8"))
        return set(data.get("skip", [])) if data.get("schema") == 1 else set()
    except (OSError, ValueError, AttributeError):
        return set()


def _save_listing(root):
    out = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if len(out) > 5000:
            break
        try:
            if path.is_file():
                st = path.stat(); out[str(path.relative_to(root))] = (st.st_size, int(st.st_mtime))
        except OSError:
            continue
    return out


def _alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], capture_output=True, text=True).stdout
        return str(int(pid)) in out
    try:
        os.kill(int(pid), 0); return True
    except OSError:
        return False


def _stop(pid, grace):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T"], capture_output=True)   # asks windows to close
        deadline = time.time() + grace
        while time.time() < deadline and _alive(pid):
            time.sleep(0.5)
        if _alive(pid):
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True)
            return "forced"
        return "closed"
    try:
        os.kill(int(pid), 15)
    except OSError:
        pass
    return "signalled"


def _screenshot(target):
    if os.name != "nt":
        return False
    script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
              "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
              "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
              "$g=[System.Drawing.Graphics]::FromImage($i);"
              "$g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size);"
              f"$i.Save('{str(target)}',[System.Drawing.Imaging.ImageFormat]::Png)")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and Path(target).is_file()


def analyse_log(path, nid_names=None):
    """Classify a native run from its log text."""
    nid_names = nid_names or {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"classification": "no-log", "signature": "no log written"}
    fps = [float(x) for x in _PERF.findall(text)] or [float(x) for x in _PERF_OLD.findall(text)]
    counts = {k: len(r.findall(text)) for k, r in _SIGNALS.items()}
    nids = collections.Counter(n.upper() for n in _NID.findall(text))
    watch = _WATCH_LAST.findall(text)
    inside = sorted({f"tid{t}:{n}" for t, n in _WATCH_IN.findall(text)})
    last_lines = [l for l in text.splitlines() if l.strip()][-15:]
    draws = bool(re.search(r"draw_arrays|DRAW_ARRAYS|draw_indexed", text))
    all_lines = text.splitlines()
    last_perf = max((i for i, l in enumerate(all_lines) if "[RR-PERF]" in l or "[PERF]" in l), default=-1)
    trailing = len(all_lines) - 1 - last_perf if last_perf >= 0 else 0
    # Frame reports arrive about once a second; a long silent tail means frames stopped.
    stalled = last_perf >= 0 and trailing > 400 and last_perf < 0.85 * len(all_lines)
    if counts["exit_dialog"]:
        kind = "game-quit-error-dialog"
    elif fps and fps[-1] >= 1 and not stalled:
        kind = "running"
    elif fps:
        kind = "frozen-after-frames"
    elif draws:
        kind = "drawing-but-no-present"
    else:
        kind = "black-screen"
    first_nid = next(iter(nids), None)
    if kind == "running":
        sig = "running"
    elif counts["pm_not_lifted"]:
        sig = "SPU job manager not compiled (PM NOT LIFTED)"
    elif counts["data_word"]:
        sig = "SPU program ran a data word (build-anyway halt)"
    elif counts["out_of_memory"]:
        sig = "out of memory"
    elif counts["spu_halt"]:
        sig = "SPU program halted"
    elif first_nid:
        sig = "missing function " + nid_names.get(first_nid, "0x" + first_nid)
    elif counts["null_call"]:
        sig = "called a NULL function pointer"
    elif watch:
        sig = "stalled in " + watch[-1][1]
    else:
        sig = "no frames, no error in log"
    return {"classification": kind, "signature": sig, "fps_samples": fps[-6:], "fps_last": fps[-1] if fps else 0.0,
            "signals": {k: v for k, v in counts.items() if v},
            "unresolved_functions": {nid_names.get(n, "0x" + n): c for n, c in nids.most_common(12)},
            "stall_report": {"last_hle": [w[1] for w in watch][-2:], "inside_hle": inside[:20]},
            "log_tail": last_lines}


class BatchTry:
    """Run on its own thread; never holds the build queue."""

    def __init__(self, studio, projects, seconds=DEFAULT_SECONDS, screenshots=None, notify=None):
        self.studio = studio
        self.projects = list(projects)
        self.seconds = seconds
        self.screenshots = screenshots or []
        self.notify = notify or (lambda text: None)
        self.base = Path(studio.home) / "batch-try"
        self.folder = self.base / _now_stamp()
        self.cancel_event = threading.Event()
        self.thread = None
        self.names = _nid_names()
        self.results = {"schema": 1, "kind": "ps3-batch-try", "started_at": time.time(), "finished_at": None,
                        "seconds_per_game": seconds, "status": "running", "folder": str(self.folder), "games": []}

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="ps3-batch-try")
        self.thread.start(); return self.thread

    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def cancel(self):
        self.cancel_event.set()

    def _save(self):
        _write_json(self.folder / "results.json", self.results)
        _write_json(self.base / "latest.json", {"schema": 1, "results": str(self.folder / "results.json"),
                                                "report": str(self.folder / "REPORT.md"),
                                                "status": self.results["status"], "updated_at": time.time()})

    def run(self):
        self.folder.mkdir(parents=True, exist_ok=True); self._save()
        try:
            for index, p in enumerate(self.projects, 1):
                if self.cancel_event.is_set():
                    break
                name = p.get("name", p.get("id"))
                if p.get("id") in _skip_list(self.base) or name in _skip_list(self.base):
                    self.results["games"].append({"project_id": p.get("id"), "name": name,
                                                  "classification": "skipped", "signature": "listed in skip.json"})
                    self._save(); continue
                self.notify(f"Batch try {index}/{len(self.projects)}: {name}")
                try:
                    entry = self.try_one(p, index)
                except Exception as exc:   # one game never stops the batch
                    entry = {"project_id": p.get("id"), "name": name, "classification": "error",
                             "signature": "could not launch: " + str(exc)[:200]}
                self.results["games"].append(entry); self._save()
            self.results["status"] = "cancelled" if self.cancel_event.is_set() else "finished"
        finally:
            self.results["finished_at"] = time.time(); self._save()
            (self.folder / "REPORT.md").write_text(report_markdown(self.results), encoding="utf-8")
            self.notify("Batch try " + self.results["status"] + f": {len(self.results['games'])} game(s); see REPORT.md")

    def _frozen(self, p):
        """Stop a test early once the game's own freeze report has been written
        (frames stopped for 8 s); a short grace lets the report finish."""
        now = time.time()
        if now - getattr(self, "_last_freeze_check", 0) < 3:
            return False
        self._last_freeze_check = now
        try:
            log = json.loads((self.studio.project_dir(p) / "native-test.json").read_text(encoding="utf-8")).get("log_path")
            with open(log, "rb") as fh:
                fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 200000))
                if b"frames stopped" in fh.read():
                    time.sleep(3); self._froze = True; return True
        except (OSError, ValueError, TypeError):
            pass
        return False

    def try_one(self, p, index):
        self._froze = False
        name = p.get("name", p.get("id"))
        candidates = self.studio.list_test_candidates(p)
        if not candidates:
            return {"project_id": p.get("id"), "name": name, "classification": "not-built", "signature": "no finished build"}
        candidate = candidates[0]
        data_root = self.studio.project_dir(p) / "native-data"
        before = _save_listing(data_root)
        pid = self.studio.launch(p, True, candidate["id"])
        started = time.time(); shots = []
        for at in self.screenshots + [self.seconds]:
            while time.time() - started < at and _alive(pid) and not self.cancel_event.is_set():
                time.sleep(0.5)
                if self._frozen(p):
                    break
            if getattr(self, "_froze", False):
                break
            if not _alive(pid) or self.cancel_event.is_set():
                break
            if at in self.screenshots:
                target = self.folder / f"{index:03d}-{p.get('id')}-{at:03d}s.png"
                if _screenshot(target):
                    shots.append(target.name)
        exited = not _alive(pid)
        ran = round(time.time() - started, 1)
        stop = "already exited" if exited else _stop(pid, GRACE_SECONDS)
        report = json.loads((self.studio.project_dir(p) / "native-test.json").read_text(encoding="utf-8"))
        after = _save_listing(data_root)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        entry = {"project_id": p.get("id"), "name": name, "candidate": candidate.get("label"),
                 "ran_seconds": ran, "exited_on_its_own": exited, "stop": stop,
                 "log": report.get("log_path"), "screenshots": shots, "save_data_changed": changed[:50]}
        entry.update(analyse_log(report.get("log_path", ""), self.names))
        if exited and entry["classification"] == "running":
            entry["classification"] = "exited-after-frames"
        elif exited:
            entry["classification"] = "exited-or-crashed"
        return entry


def report_markdown(results):
    games = results.get("games", [])
    groups = collections.defaultdict(list)
    for g in games:
        groups[g.get("signature", "?")].append(g)
    lines = [f"# PS3 batch try {time.strftime('%Y-%m-%d %H:%M', time.localtime(results.get('started_at', 0)))}",
             "", f"{len(games)} game(s), {results.get('seconds_per_game')} s each, status {results.get('status')}.", ""]
    counts = collections.Counter(g.get("classification") for g in games)
    lines += ["| result | games |", "|---|---|"] + [f"| {k} | {v} |" for k, v in counts.most_common()] + [""]
    lines.append("## Problems, largest group first")
    for sig, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines += ["", f"### {sig} ({len(members)})"]
        for g in members:
            fps = f", last fps {g.get('fps_last', 0):.1f}" if g.get("fps_last") else ""
            lines.append(f"- {g.get('name')} [{g.get('project_id')}]: {g.get('classification')}{fps}")
    return "\n".join(lines) + "\n"
