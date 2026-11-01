# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""RetroRemakes fixes to the pinned ps3recomp backend, shipped as source.

The Studio clones ps3recomp at BACKEND_REV; our runtime and tool fixes live in
backend_overlay/files (our own MIT code on top of MIT ps3recomp -- no game code
or data). Each file is written only when the checkout's copy is exactly the
pinned upstream file (compared without line-ending differences) or a missing
new file. A file someone has changed locally is never overwritten.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "backend_overlay"


def _lf(data):
    return data.replace(b"\r\n", b"\n")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _head(backend):
    head = backend / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value.startswith("ref:"):
        ref = backend / ".git" / value.split(" ", 1)[1].strip()
        try:
            return ref.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return value


def apply(backend, emit=print):
    """Bring a pinned checkout up to the shipped fixes. Returns a summary."""
    backend = Path(backend)
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    summary = {"applied": [], "current": [], "kept_local": [], "skipped": None}
    rev = _head(backend)
    if rev is not None and rev != manifest["backend_rev"]:
        summary["skipped"] = f"backend is at {rev[:12]}, fixes target {manifest['backend_rev'][:12]}"
        emit("RetroRemakes backend fixes not applied: " + summary["skipped"])
        return summary
    for entry in manifest["files"]:
        rel = entry["path"]
        source = ROOT / "files" / rel
        target = backend / rel
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            raise ValueError(f"unsafe overlay path {rel}")
        new = source.read_bytes()
        if _sha(new) != entry["sha256"]:
            raise ValueError(f"overlay file changed after packaging: {rel}")
        if target.exists():
            current = target.read_bytes()
            if _sha(_lf(current)) == entry["sha256"]:
                summary["current"].append(rel)
                continue
            if entry["base_sha256"] is None or _sha(_lf(current)) != entry["base_sha256"]:
                summary["kept_local"].append(rel)
                continue
            crlf = b"\r\n" in current
        else:
            if entry["base_sha256"] is not None:
                summary["kept_local"].append(rel)   # upstream file missing: leave it
                continue
            crlf = False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new.replace(b"\n", b"\r\n") if crlf else new)
        summary["applied"].append(rel)
    if summary["applied"]:
        emit(f"Applied {len(summary['applied'])} RetroRemakes backend fix file(s).")
    if summary["kept_local"]:
        emit("Kept locally modified backend file(s) unchanged: " + ", ".join(summary["kept_local"]))
    return summary
