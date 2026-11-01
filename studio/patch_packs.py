# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Patch packs: community fix sets for one game, applied at build time.

A patch pack is a folder with a pack.json and patch files. When a game matches
the pack (by title ID or exact EBOOT hash), the Studio:

  1. makes a separate copy of the ps3recomp backend at the revision the pack
     asks for (the shared backend is never changed),
  2. applies the pack's backend patches in file-name order (0001-..., 0002-...),
  3. builds the game against that copy, applies any project patches/files to
     the generated port, and sets the pack's environment variables when the
     native build is tested.

The prepared backend is cached per pack content, so later builds reuse it.
See PATCH-PACKS.md for the format.
"""
from __future__ import annotations
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

SCHEMA = 1
PACK_FILE = "pack.json"
ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}\Z")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
TITLE_ID = re.compile(r"[A-Z]{4}\d{5}\Z")
MAX_FILES = 5000
MAX_BYTES = 512 * 1024 * 1024


class PackError(ValueError):
    pass


class Cancelled(RuntimeError):
    pass


def packs_dir(home):
    return Path(home) / "patch-packs"


# ------------------------------------------------------------------ reading

def _inside(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _glob(folder, patterns):
    if isinstance(patterns, str):
        patterns = [patterns]
    found = []
    for pattern in patterns or []:
        pure = PurePosixPath(str(pattern))
        if pure.is_absolute() or ".." in pure.parts:
            raise PackError(f"Unsafe path in pack.json: {pattern}")
        for path in sorted(folder.glob(str(pure)), key=lambda x: x.name):
            if path.is_file() and _inside(folder, path) and path not in found:
                found.append(path)
    return found


def load(folder):
    """Read and check a pack folder. Returns the pack dict with resolved paths."""
    folder = Path(folder).resolve()
    try:
        data = json.loads((folder / PACK_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PackError(f"No {PACK_FILE} in {folder}")
    except ValueError as error:
        raise PackError(f"{PACK_FILE} is not valid JSON: {error}")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise PackError(f"{PACK_FILE} must have \"schema\": {SCHEMA}")
    if not ID.match(str(data.get("id", ""))):
        raise PackError("pack id must be 2-64 lowercase letters, digits or '-'")
    if not str(data.get("name", "")).strip():
        raise PackError("pack needs a name")
    games = data.get("games") or {}
    titles = [str(t).upper() for t in games.get("title_ids", [])]
    hashes = [str(h).lower() for h in games.get("elf_sha256", [])]
    if not titles and not hashes:
        raise PackError("pack must list games.title_ids or games.elf_sha256")
    for t in titles:
        if not TITLE_ID.match(t):
            raise PackError(f"Not a PS3 title ID: {t} (example: BCUS98234)")
    for h in hashes:
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            raise PackError(f"Not a SHA-256: {h}")
    backend = data.get("backend") or {}
    project = data.get("project") or {}
    strip = int(backend.get("strip", 1))
    if not 0 <= strip <= 5:
        raise PackError("backend.strip must be 0-5")
    revision = str(backend.get("revision", "")).strip()
    if revision and not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", revision):
        raise PackError("backend.revision must be a commit, tag or branch name")
    repository = str(backend.get("repository", "")).strip()
    if repository and not repository.startswith("https://"):
        raise PackError("backend.repository must be an https:// address")
    env = data.get("env") or {}
    for key, value in env.items():
        if not ENV_NAME.match(str(key)) or not isinstance(value, (str, int, float)):
            raise PackError(f"Bad environment setting: {key}")
    pack = {
        "folder": folder, "raw": data, "id": data["id"], "name": str(data["name"]).strip(),
        "version": str(data.get("version", "")), "author": str(data.get("author", "")),
        "license": str(data.get("license", "")), "homepage": str(data.get("homepage", "")),
        "description": str(data.get("description", "")),
        "title_ids": titles, "elf_sha256": hashes,
        "revision": revision, "repository": repository, "strip": strip,
        "studio_fixes": bool(backend.get("studio_fixes", not revision)),
        "backend_patches": _glob(folder, backend.get("patches", [])),
        "project_patches": _glob(folder, project.get("patches", [])),
        "project_files": None, "env": {str(k): str(v) for k, v in env.items()},
    }
    files = project.get("files")
    if files:
        pure = PurePosixPath(str(files))
        target = folder / str(pure)
        if pure.is_absolute() or ".." in pure.parts or not target.is_dir():
            raise PackError(f"project.files must be a folder inside the pack: {files}")
        pack["project_files"] = target
    if not (pack["backend_patches"] or pack["project_patches"] or pack["project_files"] or pack["env"]):
        raise PackError("pack has nothing to apply: add backend.patches, project.patches, project.files or env")
    return pack


def installed(home):
    """Every valid pack, plus a list of (folder, problem) for broken ones."""
    good, bad = [], []
    base = packs_dir(home)
    if base.is_dir():
        for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
            try:
                good.append(load(folder))
            except PackError as error:
                bad.append((folder, str(error)))
    return good, bad


def find(home, pack_id):
    for pack in installed(home)[0]:
        if pack["id"] == pack_id:
            return pack
    return None


def summary(pack):
    if not pack:
        return None
    return {"id": pack["id"], "name": pack["name"], "version": pack["version"], "author": pack["author"],
            "backend_revision": pack["revision"] or "Studio default", "backend_patches": len(pack["backend_patches"]),
            "project_patches": len(pack["project_patches"]), "content_key": content_key(pack)}


def content_key(pack):
    digest = hashlib.sha256()
    digest.update(json.dumps(pack["raw"], sort_keys=True).encode())
    for path in pack["backend_patches"]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


# ------------------------------------------------------------------ matching

def matches(pack, report):
    meta = report.get("metadata") or {}
    title = str(meta.get("TITLE_ID", "")).upper()
    return report.get("sha256", "").lower() in pack["elf_sha256"] or (title and title in pack["title_ids"])


def for_project(studio, p, report):
    """The pack this game should build with, or None. Records the choice on the project."""
    choice = p.get("patch_pack_choice", "auto")
    pack = None
    if choice == "none":
        pack = None
    elif choice and choice != "auto":
        pack = find(studio.home, choice)
        if not pack:
            raise PackError(f"The patch pack chosen for this game ({choice}) is not installed")
    else:
        candidates = [x for x in installed(studio.home)[0] if matches(x, report)]
        pack = candidates[0] if candidates else None
    if pack:
        p["patch_pack"] = pack["id"]
    else:
        p.pop("patch_pack", None)
    return pack


def launch_env(studio, p):
    pack = find(studio.home, p["patch_pack"]) if p.get("patch_pack") else None
    return dict(pack["env"]) if pack else {}


# ------------------------------------------------------------------ installing

def _check_tree(folder):
    count = size = 0
    for path in Path(folder).rglob("*"):
        if path.is_symlink():
            raise PackError(f"Packs may not contain links: {path.name}")
        if path.is_file():
            count += 1
            size += path.stat().st_size
            if count > MAX_FILES or size > MAX_BYTES:
                raise PackError("Pack is too large")


def install(home, source, replace=False):
    """Copy a pack folder or ZIP into the workspace. Returns the installed pack."""
    source = Path(source)
    with tempfile.TemporaryDirectory() as temp:
        if source.is_file():
            root = Path(temp) / "x"
            with zipfile.ZipFile(source) as z:
                for info in z.infolist():
                    name = PurePosixPath(info.filename)
                    if name.is_absolute() or ".." in name.parts or "\\" in info.filename or ":" in info.filename:
                        raise PackError(f"Unsafe path in ZIP: {info.filename}")
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise PackError("ZIP contains a link")
                if len(z.infolist()) > MAX_FILES or sum(i.file_size for i in z.infolist()) > MAX_BYTES:
                    raise PackError("Pack is too large")
                z.extractall(root)
            hits = [p.parent for p in root.rglob(PACK_FILE)]
            if not hits:
                raise PackError(f"No {PACK_FILE} in this ZIP")
            folder = min(hits, key=lambda p: len(p.parts))
        else:
            folder = source
        _check_tree(folder)
        pack = load(folder)
        target = packs_dir(home) / pack["id"]
        if target.exists() and not replace:
            raise PackError(f"A pack with id '{pack['id']}' is already installed")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy beside the old pack, then swap, so a build never sees a half-written pack.
        fresh = target.with_name(f".{pack['id']}.new")
        old = target.with_name(f".{pack['id']}.old")
        for leftover in (fresh, old):
            if leftover.exists():
                shutil.rmtree(leftover)
        shutil.copytree(folder, fresh, ignore=shutil.ignore_patterns(".git", "__pycache__", ".community.json"))
        if target.exists():
            target.rename(old)
        fresh.rename(target)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
    return load(target)


def create_from_folder(home, patch_folder, name, title_ids, revision="", author="", homepage="",
                       license="", studio_fixes=None, pattern="*.patch", replace=False):
    """Make a pack from a plain folder of numbered patches (e.g. a port's patches/ folder)."""
    patch_folder = Path(patch_folder)
    patches = sorted((p for p in patch_folder.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, pattern)),
                     key=lambda x: x.name) if patch_folder.is_dir() else []
    if not patches:
        raise PackError(f"No {pattern} files in {patch_folder}")
    pack_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "patch-pack"
    if len(pack_id) < 2:
        pack_id += "-pack"
    titles = [t.strip().upper() for t in re.split(r"[,\s]+", title_ids or "") if t.strip()]
    data = {"schema": SCHEMA, "id": pack_id, "name": name.strip(), "version": time.strftime("%Y.%m.%d"),
            "author": author, "homepage": homepage, "license": license,
            "games": {"title_ids": titles},
            "backend": {"revision": revision.strip(), "patches": ["patches/*.patch"], "strip": 1,
                        "studio_fixes": (not revision.strip()) if studio_fixes is None else bool(studio_fixes)},
            "env": {}}
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp) / pack_id
        (folder / "patches").mkdir(parents=True)
        for patch in patches:
            shutil.copy2(patch, folder / "patches" / (patch.stem + ".patch"))
        (folder / PACK_FILE).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        load(folder)
        return install(home, folder, replace=replace)


def remove(home, pack_id):
    target = packs_dir(home) / pack_id
    if not ID.match(pack_id) or not target.is_dir():
        raise PackError("That pack is not installed")
    shutil.rmtree(target)


# ------------------------------------------------------------------ building

def _git(args, cwd=None, log=None, timeout=1800):
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if log:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(f"$ git {' '.join(map(str, args))}\n{run.stdout}{run.stderr}\n")
    return run


def _patch_program():
    found = shutil.which("patch")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramW6432", "")):
        candidate = Path(base or "") / "Git" / "usr" / "bin" / "patch.exe"
        if base and candidate.is_file():
            return str(candidate)
    return None


def apply_patch(tree, patch, strip=1, log=None):
    """git apply, falling back to patch(1) (shipped with Git for Windows) for fuzzy hunks."""
    first = _git(["apply", "--whitespace=nowarn", f"-p{strip}", str(patch)], cwd=tree, log=log)
    if first.returncode == 0:
        return "git apply"
    program = _patch_program()
    if program:
        dry = subprocess.run([program, f"-p{strip}", "-N", "--batch", "--dry-run", "-i", str(patch)],
                             cwd=tree, capture_output=True, text=True)
        if dry.returncode == 0:
            real = subprocess.run([program, f"-p{strip}", "-N", "--batch", "-i", str(patch)],
                                  cwd=tree, capture_output=True, text=True)
            if log:
                with open(log, "a", encoding="utf-8") as stream:
                    stream.write(f"$ patch -p{strip} -i {patch.name}\n{real.stdout}{real.stderr}\n")
            if real.returncode == 0:
                return "patch (fuzzy)"
    detail = (first.stderr or first.stdout).strip().splitlines()
    raise PackError(f"Patch {patch.name} does not apply: " + (" ".join(detail[:3]) or "unknown error"))


def needs_own_backend(pack):
    return bool(pack["backend_patches"] or pack["revision"])


def backend_folder(studio, pack):
    return studio.home / "tools" / "patch-pack-backends" / f"{pack['id']}-{content_key(pack)}"


def prepare_backend(studio, pack, emit=print, cancel=None):
    """A ps3recomp checkout with this pack's patches applied, or None when the pack
    only changes the generated project. Cached; the shared backend is untouched."""
    from . import core
    if not needs_own_backend(pack):
        return None
    dest = backend_folder(studio, pack)
    marker = dest / ".rr-patch-pack.json"
    if marker.is_file():
        emit(f"Patch pack backend ready (cached): {dest.name}")
        return dest
    work = dest.with_name(dest.name + ".partial")
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    log = work.parent / f"{dest.name}.log"
    log.write_text(f"Preparing patch pack {pack['name']} ({pack['id']})\n", encoding="utf-8")
    shared = Path(studio.state["settings"]["backend"]).expanduser().resolve()
    repository = pack["repository"] or core.BACKEND_URL
    revision = pack["revision"] or core.BACKEND_REV
    source = str(shared) if (shared / ".git").exists() else repository
    emit(f"Patch pack: copying the backend for {pack['name']}...")
    if _git(["clone", "--no-checkout", source, str(work)], log=log).returncode:
        raise PackError(f"Could not copy the backend. See {log}")
    have = _git(["rev-parse", "--verify", "--quiet", revision + "^{commit}"], cwd=work, log=log).returncode == 0
    if not have:
        emit(f"Patch pack: downloading backend revision {revision}...")
        if _git(["fetch", "--tags", repository, revision], cwd=work, log=log).returncode:
            raise PackError(f"Backend revision {revision} was not found at {repository}. See {log}")
        revision = "FETCH_HEAD" if _git(["rev-parse", "--verify", "--quiet", revision + "^{commit}"],
                                        cwd=work, log=log).returncode else revision
    if _git(["checkout", "--detach", revision], cwd=work, log=log).returncode:
        raise PackError(f"Could not check out backend revision {revision}. See {log}")
    if pack["studio_fixes"]:
        from .backend_overlay import apply as apply_overlay
        apply_overlay(work, emit)
    total = len(pack["backend_patches"])
    for number, patch in enumerate(pack["backend_patches"], 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled("Patch pack preparation cancelled")
        how = apply_patch(work, patch, pack["strip"], log)
        if number == total or number % 10 == 0:
            emit(f"Patch pack: applied {number}/{total} patches ({patch.name}, {how})")
    record = {"pack": summary(pack), "revision": _git(["rev-parse", "HEAD"], cwd=work).stdout.strip(),
              "patches": [p.name for p in pack["backend_patches"]], "prepared": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (work / ".rr-patch-pack.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    work.rename(dest)
    emit(f"Patch pack backend ready: {total} patch(es) applied")
    return dest


def apply_project(pack, port, emit=print):
    """Copy the pack's project files over the generated port, then apply its project patches."""
    port = Path(port)
    if pack.get("project_files"):
        for source in sorted(pack["project_files"].rglob("*")):
            if source.is_file():
                target = port / source.relative_to(pack["project_files"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        emit(f"Patch pack: copied project files from {pack['project_files'].name}/")
    for patch in pack["project_patches"]:
        apply_patch(port, patch, 1)
    if pack["project_patches"]:
        emit(f"Patch pack: applied {len(pack['project_patches'])} project patch(es)")
