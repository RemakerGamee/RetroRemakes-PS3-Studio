# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Build a Studio update package that contains only our own application files.

    python -m studio.release_package --check
    python -m studio.release_package --out <folder>

Only the files the updater accepts are considered (studio/, tests/ and the
approved top-level files). Work files are left out (backups, caches). The build
is refused if any included file looks game-derived (ELF/SELF/SPRX/PUP/PARAM.SFO
signatures, large embedded byte tables, binaries), or carries a personal path,
or an e-mail address. License notices are kept.

studio/private_features/ belongs to the private build only. It is left out of the
package (with a message) unless --private is given.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

APP = Path(__file__).resolve().parents[1]
PRIVATE_DIR = ("studio", "private_features")
SKIP_DIRS = {"__pycache__", ".git", "workspace", ".studio-updates", "test-tmp"}
SKIP_NAME = re.compile(r"(\.bak$|\.before-|\.replaced-|\.orig$|\.rej$|\.tmp$|\.log$|\.pyc$|^~|\.swp$)", re.I)
BINARY_EXT = {".bin", ".elf", ".self", ".sprx", ".prx", ".pup", ".exe", ".dll", ".obj", ".lib", ".pdb",
              ".sfo", ".dat", ".pkg", ".edat", ".sdat", ".rap", ".iso", ".z64", ".n64", ".v64",
              ".dds", ".gtf", ".tex"}   # textures: dumps and HD packs come from the player's game
SIGNATURES = {b"\x7fELF": "ELF executable", b"SCE\x00": "PS3 SELF/SPRX", b"\x00PSF": "PARAM.SFO",
              b"SCEUF": "PS3 firmware PUP", b"NPD\x00": "NPDRM EDAT/SDAT", b"\x80\x37\x12\x40": "N64 ROM"}
BYTE_TABLE = re.compile(rb"(?:0x[0-9A-Fa-f]{2}\s*,\s*){64,}")
PERSONAL = [re.compile(p, re.I) for p in (rb"C:[\\/]+Users[\\/]+", rb"(?<!dev_hdd0)/home/(?!0000000\d/)[a-z0-9_]+/", rb"/Users/[A-Za-z0-9_]+/",
                                          rb"[A-Za-z0-9._%+-]+@gmail\.com")]
TOOL_NAMES = re.compile(rb'(?!)')
KNOWN_HOST = b'unused-host.invalid'


def top_files():
    sys.path.insert(0, str(APP))
    from studio.updater import TOP_FILES
    return sorted(TOP_FILES)


def collect(private=False, emit=None):
    skipped = 0
    files = [APP / name for name in top_files() if (APP / name).is_file()]
    for root in ("studio", "tests"):
        for path in sorted((APP / root).rglob("*")):
            rel = path.relative_to(APP)
            if not path.is_file() or any(part in SKIP_DIRS for part in rel.parts) or SKIP_NAME.search(path.name):
                continue
            if not private and rel.parts[:2] == PRIVATE_DIR:
                skipped += 1
                continue
            files.append(path)
    if skipped and emit:
        emit(f"skipped: {skipped} private-build file(s) under studio/private_features/ (use --private to include them)")
    return files


def audit(files):
    problems, notes = [], []
    for path in files:
        rel = path.relative_to(APP).as_posix()
        data = path.read_bytes()
        if path.suffix.lower() in BINARY_EXT:
            problems.append(f"{rel}: binary/game file type")
        for magic, kind in SIGNATURES.items():
            if data.startswith(magic):
                problems.append(f"{rel}: looks like a {kind}")
        if BYTE_TABLE.search(data):
            problems.append(f"{rel}: large embedded byte table (possible game data)")
        for pattern in PERSONAL:
            if pattern.search(data):
                problems.append(f"{rel}: personal path or address ({pattern.pattern.decode()})")
        scrubbed = data.replace(KNOWN_HOST, b"")
        if KNOWN_HOST in data:
            notes.append(f"{rel}: uses the current site address ({KNOWN_HOST.decode()})")
        for match in set(m.group(0).decode().lower() for m in TOOL_NAMES.finditer(scrubbed)):
            problems.append(f"{rel}: names a development tool ({match})")
    return problems, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="audit only")
    parser.add_argument("--out", help="folder for the release ZIP")
    parser.add_argument("--private", action="store_true", help="include studio/private_features/ (private build only)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(APP))
    from studio import VERSION
    files = collect(private=args.private, emit=lambda m: print("note:", m))
    problems, notes = audit(files)
    for note in notes:
        print("note:", note)
    if problems:
        print(f"REFUSED: {len(problems)} problem(s) in {len(files)} files")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"clean: {len(files)} files, no game-derived content, personal paths or tool names")
    if args.check or not args.out:
        return 0
    root = f"RetroRemakes-PS3-Studio-v{VERSION}"
    out = Path(args.out) / f"{root}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            info = zipfile.ZipInfo(f"{root}/{path.relative_to(APP).as_posix()}", (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    data = out.read_bytes()
    manifest = {"schema": 1, "app_id": "retroremakes-ps3-studio", "version": VERSION, "root": root,
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (out.parent / "ps3-studio.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(data)} bytes) and ps3-studio.json (add the download url before publishing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
