# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Helpers behind three Studio buttons (no Tk here, so they can be tested):

* display.ini read/write for "Display settings..."
* export_port() for "Export port folder"
* update_game_version() for "Update games"
"""
from __future__ import annotations
import hashlib

import json
import re
import shutil
import time
from pathlib import Path

# (key, label, choices, default). Choices None = free number.
DISPLAY_OPTIONS = (
    ("RR_FPS_LIMIT", "Frame-rate limit (0 = off)", ("0", "30", "60", "120", "144", "165", "240"), "0"),
    ("RR_VSYNC", "VSync", ("1", "0"), "1"),
    ("RR_OUTPUT", "Window / output size", ("", "1280x720", "1920x1080", "2560x1440", "3440x1440", "3840x2160"), ""),
    ("RR_ASPECT", "Aspect", ("keep", "pillarbox", "stretch"), "keep"),
    ("RR_FULLSCREEN", "Borderless fullscreen", ("0", "1"), "0"),
    ("RR_ANISO", "Anisotropic filtering", ("1", "2", "4", "8", "16"), "1"),
    ("RR_AA", "Anti-aliasing", ("off", "fxaa"), "off"),
    ("RR_SHARPEN", "Sharpening (0-100)", ("0", "20", "30", "40", "50", "70", "100"), "0"),
    ("RR_DITHER", "Dithering", ("0", "1"), "0"),
)
NOT_YET = {"RR_RES_SCALE": "Internal resolution scale: not yet available"}
_KEY = re.compile(r"^\s*(RR_[A-Z0-9_]+)\s*=\s*(.*?)\s*$")


def read_display_ini(path):
    values = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0]
            m = _KEY.match(line)
            if m:
                values[m.group(1)] = m.group(2)
    except OSError:
        pass
    return values


def display_ini_text(values):
    lines = ["# Display settings for this port (see DISPLAY-OPTIONS in the Studio docs).",
             "# Empty or missing = the game's own behaviour."]
    for key, label, _choices, _default in DISPLAY_OPTIONS:
        value = str(values.get(key, "")).strip()
        if not re.fullmatch(r"[0-9A-Za-z_.x-]*", value):
            raise ValueError(f"{key}: invalid value {value!r}")
        lines.append(f"# {label}")
        lines.append(f"{key}={value}" if value else f"# {key}=")
    return "\n".join(lines) + "\n"


def write_display_ini(path, values):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(display_ini_text(values), encoding="utf-8")
    tmp.replace(path)
    return path


def safe_folder_name(title):
    name = re.sub(r"[^\w .()&'-]+", "", str(title)).strip(" .")
    return (name or "game")[:80]


def port_id_for(title):
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return (slug or "ps3-port")[:60]


def export_port(exe, dest_root, title, title_ids=(), version="1.0.0", emit=print):
    """Copy a finished build (exe + everything beside it) into dest_root/<title>.

    An existing folder is renamed aside first (never overwritten). Adds display.ini
    (if the build had none), port.json and the Launch / Check-Updates / Report-Problem
    tools. Returns the port folder."""
    from . import port_updates
    exe = Path(exe)
    if not exe.is_file():
        raise ValueError("the selected build has no executable")
    dest = Path(dest_root) / safe_folder_name(title)
    if dest.exists():
        aside = dest.with_name(dest.name + ".previous-" + time.strftime("%Y%m%d-%H%M%S"))
        dest.rename(aside)
        emit(f"Previous port folder kept as {aside.name}")
    skip = {".pdb", ".ilk", ".obj", ".lib", ".exp"}
    shutil.copytree(exe.parent, dest, ignore=lambda d, names: [n for n in names if Path(n).suffix.lower() in skip])
    if not (dest / "display.ini").is_file():
        write_display_ini(dest / "display.ini", {})
    port_updates.write_port_json(dest, port_id_for(title), version, str(title), list(title_ids))
    port_updates.install_port_tools(dest)
    emit(f"Port folder ready: {dest}")
    return dest


def pinned_reason(project, home, settings=None):
    """Why this game must stay on its current version, or ''.
    Ports built on per-version recipes (WWS policy modules, upstream routes,
    an explicit pin) break if the EBOOT changes."""
    if project.get("pin_version"):
        return f"pinned to {project['pin_version']}"
    try:
        from .core import choose_elf
        from . import wws_policy_recipes as recipes
        elf = Path(choose_elf(project.get("source", ""), project.get("elf", "")))
        data = elf.read_bytes()
        recipe = recipes.recipe_for(hashlib.sha256(data).hexdigest(), data)
        if recipe:
            return f"{recipe['title']} job-system recipe is made for this version"
    except Exception:
        pass
    return ""


def update_game_version(project, settings, home, emit=print, allow_download=True, mode="target"):
    """Bring one project's game to its target (or latest) version and return the
    decrypted updated EBOOT path, or a dict explaining why nothing changed."""
    from . import game_versions as gv
    source = Path(project.get("source", ""))
    root = next((p for p in [source, *source.parents] if (p / "PS3_GAME" / "PARAM.SFO").is_file()
                 or (p / "PARAM.SFO").is_file() and (p / "USRDIR").is_dir()), None)
    if root is None:
        return {"status": "skipped", "reason": "game folder with PARAM.SFO not found"}
    info = gv.game_info(root, settings)
    if not info:
        return {"status": "skipped", "reason": "PARAM.SFO has no title id"}
    tid, current = info["title_id"], info.get("current") or info.get("app_ver")
    target = gv.resolve_target(tid, current, home, settings)
    result = None
    if mode == "latest" and not target:
        pin = pinned_reason(project, home, settings)
        if pin:
            return {"status": "kept", "title_id": tid, "current": current,
                    "reason": f"kept at {current} (port depends on this version: {pin})"}
        if not allow_download:
            return {"status": "skipped", "title_id": tid, "reason": "downloads disabled"}
        try:
            from .private_features import update_download
        except ImportError:
            return {"status": "skipped", "title_id": tid, "reason": "update download is not part of the Public Edition"}
        result = update_download.update_game(info, settings, home, to="latest", emit=emit)
        if result.get("status") != "installed":
            return {**result, "title_id": tid}
        target = {"version": "latest"}
    if result is None and target and gv.version_key(target["version"]) <= gv.version_key(current or "0"):
        return {"status": "up to date", "title_id": tid, "current": current}
    on_disc = gv.disc_updates(info, target["version"]) if (target and result is None) else []
    if result is not None:
        pass  # latest update already installed above
    elif not target:
        # "Update games" only brings games to the version their port needs;
        # "Update to latest" (mode="latest") handles everything else.
        return {"status": "up to date", "title_id": tid, "current": current,
                "note": "no specific version needed for this port"}
    elif on_disc:
        emit(f"{tid}: installing the {target['version']} update from the disc")
        result = gv.install_pkgs([on_disc[0]["path"]], settings, home, emit, title_id=tid)
    elif allow_download:
        try:
            from .private_features import update_download
        except ImportError:
            return {"status": "skipped", "title_id": tid, "reason": "update download is not part of the Public Edition"}
        result = update_download.update_game(info, settings, home, to="target" if target else "latest", emit=emit)
        if result.get("status") != "installed":
            return {**result, "title_id": tid}
    else:
        return {"status": "skipped", "title_id": tid, "reason": "no update on the disc"}
    eboot = Path(result.get("eboot") or "")
    if not eboot.is_file():
        return {**result, "status": "installed", "note": "update has no EBOOT.BIN; the game EBOOT is unchanged"}
    try:
        from .private_features import self_decrypt
    except ImportError:
        return {**result, "status": "installed", "note": "decrypt the updated EBOOT.BIN, then use Set decrypted ELF"}
    elf = eboot.with_name("EBOOT.elf")
    if not elf.exists():
        keys = self_decrypt.load_keys(home)
        self_decrypt.decrypt_file(eboot, elf, keys, self_decrypt.rap_folders(home, settings))
    emit(f"{tid}: updated EBOOT decrypted to {elf}")
    return {**result, "status": "installed", "elf": str(elf), "title_id": tid}
