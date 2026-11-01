# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Read-only validation for explicitly registered output-root junctions.

Only the two Studio workspace aliases named by a completed local migration may
be junctions. Arbitrary source links and nested junctions remain forbidden.
"""
import json
import os
from pathlib import Path


def _is_reparse(path):
    value = path.lstat()
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _is_junction(path):
    return getattr(path, "is_junction", lambda: False)()


def _existing_ancestors(path):
    for parent in (path, *path.parents):
        # lexists catches dangling links too; .exists() deliberately would not.
        if os.path.lexists(parent):
            yield parent


def _registered_aliases(studio):
    home = Path(studio.home).absolute()
    config = home / "output-location.json"
    try:
        if config.is_symlink() or config.stat().st_size > 16384:
            return {}
        data = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1 or data.get("status") != "ready":
            return {}
        output = Path(data["output_root"])
        projects, repairs = Path(data["project_root"]), Path(data["repair_root"])
        if not all(path.is_absolute() for path in (output, projects, repairs)):
            return {}
        output, projects, repairs = (path.absolute() for path in (output, projects, repairs))
        if projects != output / "Projects" or repairs != output / "Repairs":
            return {}
        if output == home or output.is_relative_to(home) or home.is_relative_to(output):
            return {}
        return {home / "projects": projects, home / "game-fixes": repairs}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def validate_owned_path(studio, path):
    """Reject links except exact, configured migration-root directory junctions.

    Both the lexical access path and the canonical destination are checked.
    Resolving first alone would conceal an unregistered nested link.
    """
    raw = Path(path).absolute()
    aliases = _registered_aliases(studio)
    for parent in _existing_ancestors(raw):
        if not _is_reparse(parent):
            continue
        target = aliases.get(parent)
        if (target is None or not _is_junction(parent)
                or not parent.is_dir() or parent.resolve() != target
                or not target.is_dir()):
            raise ValueError(f"Unregistered link/junction in owned output path: {parent}")
        # A target that is itself another junction must never be hidden by
        # Path.resolve(), including any reparse point in its ancestry.
        for destination in _existing_ancestors(target):
            if _is_reparse(destination):
                raise ValueError(f"Linked migration destination is not allowed: {destination}")
    canonical = raw.resolve()
    for parent in _existing_ancestors(canonical):
        if _is_reparse(parent):
            raise ValueError(f"Link/junction in resolved output path: {parent}")
    return canonical
