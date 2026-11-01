# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Keep only the newest finished build of each game.

After a fix build or native build succeeds, older builds of that game are
deleted so the test picker offers exactly one. Never deleted:
  * the newest build (its whole fix-job folder when it is a fix build),
  * the source attempt future fix builds are made from (the project's own
    attempt and the source recorded by the newest fix build),
  * game files and save data (native-data) -- only attempt folders,
    fix-job folders and hand-made repair sidecars are ever removed,
  * anything while that game is building, being fixed or being tested.
Every path is validated as Studio-owned (registered output junctions only)
before removal, and each removal is logged to workspace/cleanup.log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")


def _log(studio, line):
    try:
        with open(Path(studio.home) / "cleanup.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _owned(studio, path):
    from .output_location import validate_owned_path
    return validate_owned_path(studio, path)


def _rmtree(path):
    def retry(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=retry)
    return not os.path.lexists(path)


def _busy_reason(studio, p):
    if p.get("status") in ("building", "analysing"):
        return "a build is running"
    try:
        sessions = studio.list_test_sessions(p.get("id"))
    except Exception:
        sessions = []
    if any(s.get("status") in ("running", "stopping") for s in sessions):
        return "a test is running"
    lock = Path(studio.home) / "game-fixes" / str(p.get("id")) / ".fix-worker.lock"
    if lock.exists():
        try:
            from .game_fixes import _pid_alive
            pid = int(_read(lock).get("pid", 0))
            if pid and (pid == os.getpid() or _pid_alive(pid)):
                return "a fix build is running"
        except (ValueError, TypeError, ImportError):
            return "a fix build lock is present"
    return None


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def plan_project(studio, p, candidates):
    """What would be kept and removed for one game (no changes made)."""
    ident = str(p.get("id", ""))
    if not _ID.fullmatch(ident):
        return {"skip": "unusual project id", "remove": [], "keep": []}
    builds = [c for c in candidates if c.get("exe") and c.get("attempt")]
    if not builds:
        return {"skip": "no finished build", "remove": [], "keep": []}
    newest = max(builds, key=lambda c: _mtime(c["exe"]))
    fix_root = (Path(studio.home) / "game-fixes" / ident)
    keep = {Path(newest["attempt"]).resolve()}
    newest_attempt = Path(newest["attempt"]).resolve()
    # A fix build lives in game-fixes/<id>/<job>/...; keep its whole job folder.
    kept_job = None
    try:
        fixes_canon = _owned(studio, fix_root) if fix_root.exists() else None
    except (OSError, ValueError):
        fixes_canon = None
    if fixes_canon is not None and newest_attempt.is_relative_to(fixes_canon):
        kept_job = fixes_canon / newest_attempt.relative_to(fixes_canon).parts[0]
        keep.add(kept_job)
        source = _read(kept_job / "result.json").get("source_attempt")
        if source:
            keep.add(Path(source).resolve())
    if p.get("attempt"):
        keep.add(Path(p["attempt"]).resolve())
    remove = []
    # 1. Older fix-build jobs.
    if fixes_canon is not None:
        for job in sorted(fixes_canon.iterdir()):
            if job.is_dir() and job != kept_job and not any(k == job or k.is_relative_to(job) for k in keep):
                remove.append(("fix build", job))
    # 2. Older attempt folders (never native-data or anything else in the project).
    try:
        attempts = _owned(studio, studio.project_dir(p) / "attempts")
    except (OSError, ValueError):
        attempts = None
    if attempts is not None and attempts.is_dir():
        for attempt in sorted(attempts.iterdir()):
            if attempt.is_dir() and not any(k == attempt or k.is_relative_to(attempt) or attempt.is_relative_to(k) for k in keep):
                remove.append(("old build", attempt))
    # 3. Hand-made repair sidecars (their attempts are covered by step 2).
    sidecars = Path(studio.home) / "native-repairs" / ident
    if sidecars.is_dir():
        for item in sorted(sidecars.glob("*.json")):
            target = _read(item).get("attempt")
            if target and any(Path(target).resolve() == k for k in keep):
                continue
            remove.append(("repair record", item))
    return {"skip": None, "keep": sorted(str(k) for k in keep), "newest": newest.get("label"),
            "remove": [(kind, str(path)) for kind, path in remove]}


def prune_project(studio, p, candidates=None, dry_run=False):
    """Delete every build of this game except the newest. Returns a summary."""
    reason = _busy_reason(studio, p)
    if reason:
        _log(studio, f"skip {p.get('name')} [{p.get('id')}]: {reason}")
        return {"project_id": p.get("id"), "skipped": reason, "removed": [], "failed": []}
    if candidates is None:
        candidates = studio.all_test_candidates(p)
    plan = plan_project(studio, p, candidates)
    if plan["skip"]:
        return {"project_id": p.get("id"), "skipped": plan["skip"], "removed": [], "failed": []}
    removed, failed = [], []
    if not dry_run and plan["remove"]:
        # Keep runtime SPU code snapshots that old test runs captured: copy them
        # into the project folder before their build folders are removed.
        try:
            from .spu_snapshot_recipes import collect
            collect(studio.project_dir(p), Path(studio.home) / "game-fixes" / str(p.get("id", "")))
        except Exception as error:
            _log(studio, f"could not keep SPU snapshots for {p.get('name')}: {error}")
    for kind, path in plan["remove"]:
        path = Path(path)
        if dry_run:
            removed.append(str(path)); continue
        try:
            canonical = path.resolve() if kind == "repair record" else _owned(studio, path)
            ok = canonical.unlink() is None if kind == "repair record" else _rmtree(canonical)
        except (OSError, ValueError) as error:
            ok = False; _log(studio, f"could not remove {path}: {error}")
        (removed if ok else failed).append(str(path))
        _log(studio, ("removed " if ok else "partly removed ") + f"{kind} of {p.get('name')} [{p.get('id')}]: {path}")
    if not dry_run and removed:
        # Forget history entries whose build no longer exists.
        lock = getattr(studio, "state_lock", None)
        def drop():
            live = next((q for q in studio.state.get("projects", []) if q.get("id") == p.get("id")), None)
            if live and live.get("native_candidate_history"):
                live["native_candidate_history"] = [h for h in live["native_candidate_history"]
                                                    if h.get("native_exe") and Path(h["native_exe"]).is_file()]
            studio.save()
        if lock is not None:
            with lock:
                drop()
        else:
            drop()
    return {"project_id": p.get("id"), "skipped": None, "kept": plan.get("newest"),
            "removed": removed, "failed": failed}


def prune_all(studio, emit=print, dry_run=False):
    totals = {"games": 0, "removed": 0, "failed": 0}
    for p in list(studio.state.get("projects", [])):
        try:
            result = prune_project(studio, p, dry_run=dry_run)
        except Exception as error:  # one game never stops the sweep
            _log(studio, f"error cleaning {p.get('name')}: {error}"); continue
        if result["removed"] or result["failed"]:
            totals["games"] += 1
            totals["removed"] += len(result["removed"]); totals["failed"] += len(result["failed"])
            emit(f"{p.get('name')}: removed {len(result['removed'])} old build(s)")
    return totals
