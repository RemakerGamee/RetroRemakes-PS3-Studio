# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Read a player's support zip and match it to what the Studio already knows.

    python -m studio.support_bundles <support.zip> [--json]

Uses the same log classifier as the batch try (batch_queue.analyse_log) and
compares the signature with workspace/auto-loop/state.json and the latest
batch-try results, so a player's report lands on the problem it belongs to.
Bundles are opened read-only; nothing is executed.
"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .batch_queue import analyse_log, _nid_names

MAX_MEMBER = 16 * 1024 * 1024


def read_bundle(path):
    out = {"port": {}, "system": {}, "logs": {}, "notes": ""}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            if info.is_dir() or ".." in name.parts or info.file_size > MAX_MEMBER:
                continue
            text = z.read(info).decode("utf-8", "replace").lstrip("﻿")
            if name.name == "port.json":
                out["port"] = json.loads(text or "{}")
            elif name.name == "system.json":
                out["system"] = json.loads(text or "{}")
            elif name.name == "WHAT-HAPPENED.txt":
                out["notes"] = text.strip()
            elif name.suffix == ".log":
                out["logs"][name.name] = text
    return out


def classify(bundle):
    names = _nid_names(); results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in bundle["logs"].items():
            p = Path(tmp) / name; p.write_text(text, encoding="utf-8")
            r = analyse_log(p, names); r.pop("log_tail", None); results[name] = r
    return results


def known_matches(home, port, signature):
    """Games in the auto loop / latest batch try that show the same signature."""
    home = Path(home); hits = []
    try:
        state = json.loads((home / "auto-loop" / "state.json").read_text(encoding="utf-8"))
        for pid, g in state.get("games", {}).items():
            if (g.get("last") or {}).get("signature") == signature:
                hits.append({"source": "auto-loop", "project_id": pid, "name": g.get("name"), "status": g.get("status")})
    except (OSError, ValueError):
        pass
    try:
        latest = json.loads((home / "batch-try" / "latest.json").read_text(encoding="utf-8"))
        res = json.loads(Path(latest["results"]).read_text(encoding="utf-8"))
        for g in res.get("games", []):
            if g.get("signature") == signature:
                hits.append({"source": "batch-try", "project_id": g.get("project_id"), "name": g.get("name"),
                             "status": g.get("classification")})
    except (OSError, ValueError, KeyError):
        pass
    tids = set(port.get("title_ids") or [])
    for h in hits:
        h["same_title"] = bool(tids and any(t in (h.get("name") or "") for t in tids))
    return hits


def summarise(path, home=None):
    b = read_bundle(path); c = classify(b)
    main = next(iter(c.values()), {"classification": "no-log", "signature": "no log in bundle"})
    return {"port_id": b["port"].get("port_id"), "port_version": b["port"].get("version"),
            "title_ids": b["port"].get("title_ids"), "system": b["system"], "player_notes": b["notes"][:2000],
            "logs": c, "signature": main.get("signature"), "classification": main.get("classification"),
            "known": known_matches(home, b["port"], main.get("signature")) if home else []}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv; argv = [a for a in argv if a != "--json"]
    if not argv:
        print(__doc__); return 2
    from .core import Studio
    try:
        home = Studio().home
    except Exception:
        home = None
    s = summarise(argv[0], home)
    if as_json:
        print(json.dumps(s, indent=2)); return 0
    print(f"Port {s['port_id']} {s['port_version']}  titles {s['title_ids']}")
    gpus = ", ".join(g.get("name", "?") + " " + str(g.get("driver", "")) for g in (s["system"].get("gpus") or []))
    print(f"PC: {s['system'].get('os')} | {s['system'].get('cpu')} | {s['system'].get('ram_gb')} GB | {gpus}")
    print(f"Result: {s['classification']} - {s['signature']}")
    for k in s["known"]:
        print(f"  same problem in {k['source']}: {k['name']} [{k['project_id']}] ({k['status']})")
    if s["player_notes"]:
        print("Player says: " + s["player_notes"][:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
