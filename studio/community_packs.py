# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Community patch packs: browse the shared index, install with a checksum, share your own.

The index is a JSON file in the community packs repository on GitHub. Each entry
points at a pack ZIP (usually a release download on the author's own GitHub)
plus its SHA-256, so the Studio installs exactly what was reviewed.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from . import VERSION
from . import patch_packs as pp

PACKS_REPO = "https://github.com/RemakerGamee/PS3-Studio-Packs"
INDEX_URL = "https://raw.githubusercontent.com/RemakerGamee/PS3-Studio-Packs/main/index.json"
CONTRIBUTE_URL = PACKS_REPO + "/blob/main/CONTRIBUTING.md"
MARKER = ".community.json"
SYNC_EVERY = 6 * 3600
MAX_INDEX = 4 * 1024 * 1024
MAX_PACK = 256 * 1024 * 1024
_HEADERS = {"User-Agent": "RetroRemakes-PS3-Studio/" + VERSION, "Cache-Control": "no-cache"}


def _https(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise pp.PackError(f"Community packs must use https:// addresses: {url}")
    return url


def _download(url, limit):
    request = urllib.request.Request(_https(url), headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        _https(response.geturl())
        data = response.read(limit + 1)
    if len(data) > limit:
        raise pp.PackError("Download is larger than allowed")
    return data


def check_entry(entry):
    if not isinstance(entry, dict):
        raise pp.PackError("Index entry is not an object")
    for key in ("id", "name", "version", "author", "url", "sha256"):
        if not str(entry.get(key, "")).strip():
            raise pp.PackError(f"Index entry is missing '{key}'")
    if not pp.ID.match(entry["id"]):
        raise pp.PackError(f"Bad pack id: {entry['id']}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", entry["sha256"]):
        raise pp.PackError(f"Bad sha256 for {entry['id']}")
    _https(entry["url"])
    titles = entry.get("title_ids", [])
    if not isinstance(titles, list) or any(not pp.TITLE_ID.match(str(t).upper()) for t in titles):
        raise pp.PackError(f"Bad title_ids for {entry['id']}")
    return entry


def fetch_index(url=INDEX_URL):
    """The community list. Bad entries are skipped and reported."""
    data = json.loads(_download(url, MAX_INDEX).decode("utf-8"))
    entries = data.get("packs") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise pp.PackError("The community index is not in the expected format")
    good, bad = [], []
    for entry in entries:
        try:
            good.append(check_entry(entry))
        except pp.PackError as error:
            bad.append(str(error))
    return good, bad


def install_entry(home, entry, replace=False):
    """Download a listed pack, check its SHA-256 and that its pack.json agrees, then install it."""
    check_entry(entry)
    data = _download(entry["url"], MAX_PACK)
    if hashlib.sha256(data).hexdigest().lower() != entry["sha256"].lower():
        raise pp.PackError(f"{entry['name']}: the download does not match the checksum in the community index")
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "pack.zip"
        archive.write_bytes(data)
        pack = pp.install(home, archive, replace=replace)
    if pack["id"] != entry["id"]:
        pp.remove(home, pack["id"])
        raise pp.PackError(f"The downloaded pack is '{pack['id']}', but the index lists '{entry['id']}'")
    record = {k: entry.get(k) for k in ("id", "version", "sha256", "url", "author")}
    record["installed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (pack["folder"] / MARKER).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return pack


def matches_games(entry, projects_metadata):
    titles = {str(t).upper() for t in entry.get("title_ids", [])}
    return sorted({name for name, title in projects_metadata if title and title.upper() in titles})


# ------------------------------------------------------------------ sharing

def export_zip(pack, dest_folder):
    """Zip an installed pack for sharing. Returns (zip path, index entry)."""
    dest_folder = Path(dest_folder)
    dest_folder.mkdir(parents=True, exist_ok=True)
    name = f"{pack['id']}-{pack['version'] or 'pack'}.zip"
    path = dest_folder / re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pack["folder"].rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and ".git" not in f.parts:
                info = zipfile.ZipInfo(f"{pack['id']}/{f.relative_to(pack['folder']).as_posix()}", (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, f.read_bytes())
    entry = {"id": pack["id"], "name": pack["name"], "version": pack["version"], "author": pack["author"],
             "homepage": pack["homepage"], "license": pack["license"], "description": pack["description"],
             "title_ids": pack["title_ids"],
             "url": "https://github.com/<your-account>/<your-repo>/releases/download/<tag>/" + path.name,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return path, entry


# ------------------------------------------------------------------ library

def library_dir(home):
    return Path(home) / "community"


def cached_index(home):
    try:
        data = json.loads((library_dir(home) / "index-cache.json").read_text(encoding="utf-8"))
        return data.get("packs", []), data.get("fetched", "")
    except (OSError, ValueError):
        return [], ""


def community_record(home, pack_id):
    """The community download record of an installed pack, or None for packs the player added."""
    try:
        return json.loads((pp.packs_dir(home) / pack_id / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def my_games(studio):
    """(name, title ID) for every game in the Studio's library."""
    games = []
    for project in list(studio.state.get("projects", [])):
        title = ""
        try:
            analysis = json.loads(Path(project.get("last_analysis", "")).read_text(encoding="utf-8"))
            title = (analysis.get("metadata") or {}).get("TITLE_ID", "")
        except (OSError, ValueError):
            pass
        games.append((project.get("name", ""), title))
    return games


def status(home, entry):
    pack = pp.find(home, entry["id"])
    if not pack:
        return "not installed"
    record = community_record(home, entry["id"])
    if record is None:
        return "your own pack (same id)"
    return "installed" if str(record.get("version")) == str(entry["version"]) else "update available"


def library(studio):
    """Rows for the library view from the last synced list."""
    entries, fetched = cached_index(studio.home)
    games = my_games(studio)
    rows = [dict(e, status=status(studio.home, e), your_games=matches_games(e, games)) for e in entries]
    return rows, fetched


def due(studio):
    last = studio.state["settings"].get("community_last_sync", 0)
    return time.time() - float(last or 0) >= SYNC_EVERY


def sync(studio, auto_install=True, scope="mine", emit=print, url=INDEX_URL):
    """Pull the community list; install new packs for your games (or all) and update community packs."""
    good, bad = fetch_index(url)
    folder = library_dir(studio.home)
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / "index-cache.tmp"
    temp.write_text(json.dumps({"fetched": time.strftime("%Y-%m-%d %H:%M"), "packs": good}, indent=2), encoding="utf-8")
    temp.replace(folder / "index-cache.json")
    result = {"listed": len(good), "invalid": len(bad), "installed": [], "updated": [], "errors": []}
    if auto_install:
        games = my_games(studio)
        for entry in good:
            state = status(studio.home, entry)
            wanted = scope == "all" or matches_games(entry, games)
            try:
                if state == "update available":
                    install_entry(studio.home, entry, replace=True)
                    result["updated"].append(entry["name"])
                elif state == "not installed" and wanted:
                    install_entry(studio.home, entry)
                    result["installed"].append(entry["name"])
            except Exception as error:
                result["errors"].append(f"{entry['name']}: {error}")
    studio.state["settings"]["community_last_sync"] = time.time()
    return result


def describe(result):
    parts = [f"{result['listed']} pack(s) in the community library"]
    if result["installed"]:
        parts.append("installed " + ", ".join(result["installed"]))
    if result["updated"]:
        parts.append("updated " + ", ".join(result["updated"]))
    if result["errors"]:
        parts.append(f"{len(result['errors'])} could not be installed")
    return "Community library: " + "; ".join(parts) + "."
