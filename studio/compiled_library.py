# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Explicit, background discovery of completed output folders; no queue mutations."""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.RLock()
_SKIP = {".git", "__pycache__", "native-data", "native-da", "tools", "node_modules", "history"}

_PREF_LOCK = threading.RLock()

def hidden_project_ids(studio):
    try:
        path = studio.home / "compiled-library-preferences.json"
        if path.stat().st_size > 65536:
            return set()
        data = json.loads(path.read_text(encoding="utf-8"))
        values = data.get("hidden_project_ids", []) if isinstance(data, dict) else []
        return {value for value in values if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value)} if isinstance(values, list) else set()
    except (OSError, ValueError, TypeError):
        return set()

def set_hidden_projects(studio, ids, hidden=True):
    """Persist list visibility separately from queue, builds, saves and scan index."""
    if getattr(studio, "read_only", False):
        raise RuntimeError("A read-only Studio snapshot cannot change library visibility")
    with _PREF_LOCK:
        values = hidden_project_ids(studio)
        clean = {str(value) for value in ids if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(value))}
        if hidden:
            values.update(clean)
        elif clean:
            values.difference_update(clean)
        else:
            values.clear()
        path = studio.home / "compiled-library-preferences.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(dict(schema=1, hidden_project_ids=sorted(values)), indent=2), encoding="utf-8")
        os.replace(temp, path)
        return values

def output_owner(studio, attempt):
    """A Studio-owned output belongs to its project, even if ELF bytes match another row."""
    for category in ("projects", "game-fixes"):
        try:
            relative = attempt.resolve().relative_to((studio.home/category).resolve())
            if len(relative.parts) > 1:
                return relative.parts[0]
        except ValueError:
            pass
    return None

def index_path(studio):
    return studio.home / "compiled-library.json"

def read_index(studio):
    try:
        p = index_path(studio)
        if p.stat().st_size > 8 * 1024 * 1024:
            return {}
        value = json.loads(p.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("schema") == 1 else {}
    except (OSError, ValueError, TypeError):
        return {}

def selected_folder(studio):
    return str(read_index(studio).get("selected_folder", ""))

def records_for(studio, project):
    data = read_index(studio)
    items = data.get("records", [])
    if not isinstance(items, list):
        return []
    records = []
    for item in items:
        if not isinstance(item, dict) or item.get("project_id") != project.get("id"):
            continue
        try:
            attempt = Path(item["attempt"]).resolve()
            exe = Path(item["native_exe"]).resolve()
            root = Path(item["scan_root"]).resolve()
            owner = output_owner(studio, attempt)
            if owner is not None and owner != project.get("id"):
                continue
            if not attempt.is_relative_to(root) or not exe.is_relative_to(attempt):
                continue
            # core.list_test_candidates independently validates completion, input
            # identity and executable bytes on every use, including launch.
            records.append({"attempt": str(attempt), "native_exe": str(exe),
                            "native_runtime_receipt": None,
                            "label": "Output folder: " + attempt.name + " / " + exe.parent.name})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return records

def scan_outputs(studio, folder=None, progress=None, persist=True):
    """Scan saved/default roots plus selected root; save a separate library index."""
    if persist and getattr(studio, "read_only", False):
        raise RuntimeError("A read-only Studio snapshot cannot save output-folder preferences")
    with _LOCK:
        previous = read_index(studio)
        chosen = str(Path(folder).resolve()) if folder is not None else str(previous.get("selected_folder", ""))
        if folder is not None and not Path(chosen).is_dir():
            raise ValueError("Select an existing build output folder")
        roots = [studio.home / "projects", studio.home / "game-fixes"]
        if chosen:
            roots.append(Path(chosen))
        # Index reports by the source identity already recorded at build time.
        projects = list(studio.state.get("projects", []))
        by_hash, by_input = {}, {}
        for project in projects:
            # Analysis files are small metadata; actual ELF hashing is left to
            # the existing candidate validator, off the UI thread.
            digest = project.get("input_sha256")
            if not digest:
                try:
                    analysis = json.loads((studio.project_dir(project) / "analysis.json").read_text(encoding="utf-8"))
                    if isinstance(analysis, dict):
                        digest = analysis.get("input_sha256") or analysis.get("sha256")
                except (OSError, ValueError, TypeError):
                    pass
            try:
                from .core import choose_elf
                input_path = str(choose_elf(project["source"], project.get("elf", "")).resolve()).lower()
                by_input.setdefault(input_path, []).append(project["id"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                by_hash.setdefault(digest, []).append(project["id"])
        records, seen, checked, errors = [], set(), 0, 0
        for root in dict.fromkeys(str(r.resolve()) for r in roots if r.is_dir()):
            root = Path(root)
            def walk_error(_error):
                nonlocal errors
                errors += 1
            for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                dirs[:] = [d for d in dirs if d not in _SKIP and not (Path(base)/d).is_symlink()
                           and not getattr(Path(base)/d, "is_junction", lambda: False)()]
                if "build-report.json" not in files:
                    continue
                dirs[:] = [d for d in dirs if d not in {"build", "port", "loader", "logs"}]
                checked += 1
                if progress:
                    progress(f"Checking output reports: {checked}")
                report_file = Path(base)/"build-report.json"
                try:
                    if report_file.is_symlink() or report_file.stat().st_size > 4*1024*1024:
                        continue
                    report = json.loads(report_file.read_text(encoding="utf-8"))
                    if not isinstance(report, dict) or report.get("status") != "compiled; gameplay unverified":
                        continue
                    ids = list(by_hash.get(report.get("input_sha256"), []))
                    if report.get("input"):
                        ids = list(dict.fromkeys(ids + by_input.get(str(Path(report["input"]).resolve()).lower(), [])))
                    if not ids:
                        continue
                    attempt = Path(base).resolve()
                    owner = output_owner(studio, attempt)
                    if owner is not None:
                        ids = [project_id for project_id in ids if project_id == owner]
                    candidates = [attempt/"build/player/MyGameRecomp.exe", attempt/"build/MyGameRecomp.exe",
                                  attempt/"build/player/MyGameRecomp", attempt/"build/MyGameRecomp"]
                    for key in ("native_repair_executable", "native_exe"):
                        if report.get(key):
                            candidates.append(Path(report[key]))
                    for exe in candidates:
                        if exe.is_symlink():
                            continue
                        exe = exe.resolve()
                        if not exe.is_relative_to(attempt) or not exe.is_file():
                            continue
                        for project_id in ids:
                            key = (project_id, str(exe).lower())
                            if key in seen:
                                continue
                            seen.add(key)
                            records.append(dict(project_id=project_id, attempt=str(attempt), native_exe=str(exe), scan_root=str(root)))
                    # Generated sources/objects can contain many thousands of
                    # files; completed attempt metadata is sufficient here.
                    dirs[:] = [d for d in dirs if d not in {"build", "port", "loader", "logs"}]
                except (OSError, ValueError, KeyError, TypeError):
                    errors += 1
        data = dict(schema=1, selected_folder=chosen, records=records, reports_checked=checked, scan_errors=errors)
        if not persist:
            return data
        target = index_path(studio)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return data


def _clean_cached_rows(rows):
    if not isinstance(rows, list):
        return []
    clean, seen = [], set()
    for row in rows[:1000]:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        ident, name, count, removed = row
        if (not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", ident)
                or ident in seen or not isinstance(name, str) or len(name) > 1000
                or type(count) is not int or not 1 <= count <= 10000):
            continue
        seen.add(ident)
        clean.append((ident, name, count, bool(removed)))
    return clean

def read_cached_rows(studio):
    """Small display-only snapshot; launch always revalidates the actual build."""
    try:
        path = studio.home / "compiled-library-view.json"
        if path.is_symlink() or path.stat().st_size > 262144:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != 1
                or data.get("workspace") != str(studio.home.resolve())):
            return []
        return _clean_cached_rows(data.get("rows"))
    except (OSError, ValueError, TypeError):
        return []

def write_cached_rows(studio, rows):
    if getattr(studio, "read_only", False):
        raise RuntimeError("A read-only Studio snapshot cannot save the built-game view")
    data = dict(schema=1, workspace=str(studio.home.resolve()), saved_at=time.time(),
                rows=_clean_cached_rows(rows))
    text = json.dumps(data, indent=2)
    if len(text.encode("utf-8")) > 262144:
        raise ValueError("Built-game view exceeds its bounded display cache")
    path = studio.home / "compiled-library-view.json"
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return data["rows"]
