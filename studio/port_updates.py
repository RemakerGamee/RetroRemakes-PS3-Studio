# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Online updates for individual ports.

A port update never carries game-derived content.  It carries only
game-independent files (runtime/backend fix sources, recipes made of offsets
and hashes, launcher scripts, configuration and documents).  The player's
copy of the port rebuilds itself from the player's own game files when the
update says requires_rebuild.

Layout of a built port folder:
    port.json          identity, version, update and support settings
    PortTools.ps1      update / report-a-problem helper (from port_runtime/)
    Launch.bat, Check-Updates.bat, Report-Problem.bat
    update/            overlay files delivered by updates (runtime, recipes, ...)
    .port-backups/     previous versions kept for rollback

Manifest (updates/ports/<port_id>.json on the site):
    {"schema":1,"kind":"retroremakes-port-update","port_id":"...","title_ids":["BCUS..."],
     "version":"1.0.3","url":"https://.../file.zip","size":N,"sha256":"...",
     "min_installer":"1.0.0","requires_rebuild":true,"notes":"..."}
"""
import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import release_package as rp

SITE = "https://retroremakes.invalid"  # RetroRemakes site address for port updates and support
MANIFEST_KIND = "retroremakes-port-update"
MAX_PACKAGE = 64 * 1024 * 1024
PORT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# Top folders an update package may contain.  Nothing else is accepted.
ALLOWED_TOP = {"runtime", "recipes", "patches", "launcher", "config", "docs", "update-info.json"}
ALLOWED_EXT = {".c", ".cpp", ".h", ".hpp", ".py", ".json", ".txt", ".md", ".ini", ".cmake",
               ".ps1", ".bat", ".yml", ".yaml", ".hlsl", ".csv"}
RUNTIME = Path(__file__).resolve().parent / "port_runtime"


def vtuple(v):
    if not isinstance(v, str) or not VERSION.match(v):
        raise ValueError("version must look like 1.2.3")
    return tuple(int(x) for x in v.split("."))


def default_support_url(port_id):
    return f"{SITE}/support?port={port_id}"


def manifest_url(port_id):
    return f"{SITE}/updates/ports/{port_id}.json"


# ------------------------------------------------------------- port.json

def write_port_json(folder, port_id, version, title, title_ids, rebuild_command=None, support_url=None,
                    installer_version="1.0.0"):
    """Written by the Studio next to the port exe at build/export time."""
    if not PORT_ID.match(port_id):
        raise ValueError("port id: lowercase letters, digits and '-' only")
    vtuple(version)
    data = {"schema": 1, "port_id": port_id, "title": title, "title_ids": list(title_ids), "version": version,
            "installer_version": installer_version, "update_url": manifest_url(port_id),
            "check_updates_on_start": True, "support_url": support_url or default_support_url(port_id),
            "rebuild_command": rebuild_command or "", "log_files": ["native-play.log", "logs/*.log"],
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    (folder / "port.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def install_port_tools(folder):
    """Copy the update/support helper scripts into a port folder."""
    folder = Path(folder)
    for f in RUNTIME.iterdir():
        if f.is_file() and f.suffix in (".ps1", ".bat", ".md"):
            shutil.copy2(f, folder / f.name)
    return sorted(f.name for f in RUNTIME.iterdir() if f.is_file())


# ------------------------------------------------------------- checks

def audit_bytes(name, data):
    """Same rules as the Studio release check, applied to one update file."""
    problems = []
    low = name.lower()
    if PurePosixPath(low).suffix in rp.BINARY_EXT:
        problems.append(f"{name}: binary/game file type")
    for magic, kind in rp.SIGNATURES.items():
        if data.startswith(magic):
            problems.append(f"{name}: looks like a {kind}")
    if rp.BYTE_TABLE.search(data):
        problems.append(f"{name}: large embedded byte table (possible game data)")
    for pattern in rp.PERSONAL:
        if pattern.search(data):
            problems.append(f"{name}: personal path or address")
    scrubbed = data.replace(rp.KNOWN_HOST, b"")
    for m in set(x.group(0).decode().lower() for x in rp.TOOL_NAMES.finditer(scrubbed)):
        problems.append(f"{name}: names a development tool ({m})")
    return problems


def _safe_member(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError(f"unsafe path in package: {name}")
    if p.parts[0] not in ALLOWED_TOP:
        raise ValueError(f"{name}: only {sorted(ALLOWED_TOP)} may be updated")
    if name.endswith("/"):
        return p
    if p.suffix.lower() not in ALLOWED_EXT:
        raise ValueError(f"{name}: file type not allowed in a port update")
    return p


def validate_manifest(data, port_id=None):
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("kind") != MANIFEST_KIND:
        raise ValueError("not a port update manifest")
    if not PORT_ID.match(str(data.get("port_id", ""))) or (port_id and data["port_id"] != port_id):
        raise ValueError("manifest is for a different port")
    vtuple(data.get("version")); vtuple(data.get("min_installer", "1.0.0"))
    if not re.fullmatch(r"[0-9a-f]{64}", str(data.get("sha256", "")).lower()):
        raise ValueError("manifest has no valid sha256")
    if not isinstance(data.get("size"), int) or not 1 <= data["size"] <= MAX_PACKAGE:
        raise ValueError("invalid package size")
    if not str(data.get("url", "")).startswith("https://"):
        raise ValueError("package URL must be https")
    if not isinstance(data.get("title_ids", []), list):
        raise ValueError("title_ids must be a list")
    return data


def validate_package(path, manifest):
    blob = Path(path).read_bytes()
    if len(blob) != manifest["size"]:
        raise ValueError("package size does not match the manifest")
    if hashlib.sha256(blob).hexdigest() != manifest["sha256"].lower():
        raise ValueError("package checksum does not match the manifest")
    problems = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            _safe_member(info.filename)
            if not info.is_dir():
                problems += audit_bytes(info.filename, z.read(info))
    if problems:
        raise ValueError("package refused: " + "; ".join(problems[:10]))
    return True


# ------------------------------------------------------------- applying (also mirrored in PortTools.ps1)

def apply_update(port_folder, package, manifest):
    """Back up then apply.  Rolls back on any error.  Returns the backup folder."""
    port_folder = Path(port_folder)
    info = json.loads((port_folder / "port.json").read_text(encoding="utf-8"))
    validate_manifest(manifest, info["port_id"])
    if vtuple(manifest["version"]) <= vtuple(info["version"]):
        raise ValueError("already up to date")
    if vtuple(info.get("installer_version", "1.0.0")) < vtuple(manifest.get("min_installer", "1.0.0")):
        raise ValueError("this port's installer is too old for the update; reinstall the port")
    validate_package(package, manifest)
    backup = port_folder / ".port-backups" / f"{info['version']}-{time.strftime('%Y%m%d-%H%M%S')}"
    target = port_folder / "update"
    backup.mkdir(parents=True)
    if target.exists():
        shutil.copytree(target, backup / "update")
    shutil.copy2(port_folder / "port.json", backup / "port.json")
    try:
        with zipfile.ZipFile(package) as z:
            for m in z.infolist():
                if m.is_dir():
                    continue
                dest = target / _safe_member(m.filename)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(m))
        info["version"] = manifest["version"]; info["pending_rebuild"] = bool(manifest.get("requires_rebuild"))
        info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (port_folder / "port.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    except Exception:
        rollback(port_folder, backup); raise
    return backup


def rollback(port_folder, backup):
    port_folder = Path(port_folder); backup = Path(backup)
    target = port_folder / "update"
    if target.exists():
        target.rename(port_folder / f".update-failed-{time.strftime('%Y%m%d-%H%M%S')}")
    if (backup / "update").exists():
        shutil.copytree(backup / "update", target)
    shutil.copy2(backup / "port.json", port_folder / "port.json")


# ------------------------------------------------------------- publishing (Studio side)

def build_update(port_id, version, source_dir, out_dir, title_ids=(), notes="", requires_rebuild=True,
                 min_installer="1.0.0", base_url=None):
    """Zip a fix set into an update package and write its manifest into out_dir.

    source_dir holds only the allowed top folders (runtime/, recipes/, ...).
    Nothing is uploaded: the person uploads out_dir/<files> to the site.
    """
    if not PORT_ID.match(port_id):
        raise ValueError("bad port id")
    vtuple(version)
    source_dir = Path(source_dir); out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in source_dir.rglob("*") if f.is_file() and not rp.SKIP_NAME.search(f.name)
                   and "__pycache__" not in f.parts)
    problems = []
    for f in files:
        rel = f.relative_to(source_dir).as_posix()
        try:
            _safe_member(rel)
        except ValueError as e:
            problems.append(str(e)); continue
        problems += audit_bytes(rel, f.read_bytes())
    if not files:
        problems.append("nothing to package")
    if problems:
        raise ValueError("update refused: " + "; ".join(problems[:20]))
    name = f"{port_id}-{version}.zip"
    zpath = out_dir / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            zi = zipfile.ZipInfo(f.relative_to(source_dir).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, f.read_bytes())
        z.writestr("update-info.json", json.dumps({"port_id": port_id, "version": version, "notes": notes}, indent=2))
    blob = zpath.read_bytes()
    base = (base_url or f"{SITE}/downloads/ports/{port_id}").rstrip("/")
    manifest = {"schema": 1, "kind": MANIFEST_KIND, "port_id": port_id, "title_ids": list(title_ids),
                "version": version, "url": f"{base}/{name}", "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(), "min_installer": min_installer,
                "requires_rebuild": bool(requires_rebuild), "notes": notes}
    validate_manifest(manifest, port_id); validate_package(zpath, manifest)
    (out_dir / f"{port_id}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "UPLOAD.txt").write_text(
        f"Upload {name} to /downloads/ports/{port_id}/ and {port_id}.json to /updates/ports/ on the site,\n"
        "in the same site version. Then open the manifest URL and check size and sha256.\n", encoding="utf-8")
    return manifest


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Build a port update package (nothing is uploaded).")
    ap.add_argument("port_id"); ap.add_argument("version"); ap.add_argument("source_dir"); ap.add_argument("out_dir")
    ap.add_argument("--title-id", action="append", default=[]); ap.add_argument("--notes", default="")
    ap.add_argument("--no-rebuild", action="store_true")
    a = ap.parse_args(argv)
    m = build_update(a.port_id, a.version, a.source_dir, a.out_dir, a.title_id, a.notes, not a.no_rebuild)
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
