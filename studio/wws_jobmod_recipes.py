# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Build-time lifting of WWS SPU job code modules embedded in the EBOOT.

Games on the WWS job manager (MotorStorm: Pacific Rift, inFAMOUS 1/2,
Killzone 2, ...) link their SPU job code modules straight into the EBOOT.
Each module streams into the job code buffer at LS 0x4000 and is recognised
at run time by its first 16 bytes (spu_overlay_register_sig), so lifting them
replaces the slow SPU interpreter for job code.

Module layout (same as the SDATA-archive modules the backend's
tools/extract_jobmods.py handles):
    +0x00  4 x ila   (content signature)
    +0x10  entryOffset
    +0x14  imageSize
    +0x20  0xC0DEC0DE
    +0x28  load base (0x4000)

The EBOOT scan follows the approach of erikomis/infamous2-recomp
(tools/extract_eboot_jobmods.py, MIT License, Copyright (c) 2026 Erik Martins).
Lifting uses the backend's tools/lift_jobmods.py. Nothing game-derived ships
with the Studio: modules are read from the user's own EBOOT at build time and
everything generated stays in that user's build directory.
"""
from __future__ import annotations
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

MAGIC = b"\xC0\xDE\xC0\xDE"
LOAD_BASE = 0x4000
REGISTER_NAME = "rr_wws_jobmods_register.c"
SYMBOL_PREFIX = "rr_wws"


def find_modules(data):
    """[(file_offset, raw_bytes, entry_offset)] for every embedded job module."""
    starts, i = [], 0
    while True:
        i = data.find(MAGIC, i + 1)
        if i < 0:
            break
        s = i - 0x20
        if i % 4 or s < 0 or s + 0x30 > len(data):
            continue
        if not all((w >> 24) in (0x42, 0x43) for w in struct.unpack_from(">4I", data, s)):
            continue
        if struct.unpack_from(">I", data, s + 0x28)[0] != LOAD_BASE:
            continue
        starts.append(s)
    out = []
    for n, s in enumerate(starts):
        entry, size = struct.unpack_from(">II", data, s + 0x10)
        end = s + size
        if n + 1 < len(starts):
            end = min(end, starts[n + 1])
        raw = data[s:min(end, len(data))]
        if len(raw) >= 0x40 and entry < len(raw):
            out.append((s, raw, entry))
    return out


def _fix_includes(folder):
    for name in ("spu_recomp.c", "spu_recomp.h"):
        f = folder / name
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            new = text.replace('"../../runtime/spu/', '"')
            if new != text:
                f.write_text(new, encoding="utf-8")


def apply(stage, elf, backend, project=None, emit=print):
    """Extract, lift and register every WWS job module in the user's EBOOT.
    Returns None when the EBOOT has none (nothing is touched)."""
    stage, backend = Path(stage), Path(backend)
    data = Path(elf).read_bytes()
    modules = find_modules(data)
    if not modules:
        return None
    tool = backend / "tools" / "lift_jobmods.py"
    if not tool.is_file():
        raise RuntimeError("backend has no tools/lift_jobmods.py")
    spu = stage / "spu"
    raw_dir = spu / "_wws_jobmods"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("jobmod_*.bin"):
        old.unlink()
    for _, raw, entry in modules:
        (raw_dir / f"jobmod_{hashlib.sha1(raw).hexdigest()[:12]}_e{entry:x}.bin").write_bytes(raw)
    register = spu / REGISTER_NAME
    result = subprocess.run([sys.executable, str(tool), "--jobmods", str(raw_dir), "--out", str(spu),
                             "--register", str(register), "--symbol-prefix", SYMBOL_PREFIX],
                            cwd=str(backend), capture_output=True, text=True)
    (spu / "wws_jobmods_lift.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode or not register.is_file():
        raise RuntimeError(f"job-module lifter failed; see {spu / 'wws_jobmods_lift.log'}")
    manifest = json.loads((spu / "jobmods_manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        _fix_includes(spu / item["name"])
    # The lifted folders are picked up by the CMake glob "${SPU_DIR}/*/spu_recomp.c";
    # only the registration file needs adding.
    cmake = stage / "CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    line = f'target_sources(${{PROJECT_NAME}} PRIVATE "${{CMAKE_CURRENT_SOURCE_DIR}}/spu/{REGISTER_NAME}")'
    if line not in text:
        cmake.write_text(text + "\n# WWS SPU job modules (generated from the user's EBOOT).\n" + line + "\n",
                         encoding="utf-8")
    lifted = [m for m in manifest]
    return {"schema": 1, "applied": True, "found": len(modules), "lifted": len(lifted),
            "data_words": sum(m.get("unsupported", 0) for m in lifted),
            "modules": [{"name": m["name"], "entry": m["entry"], "functions": m.get("funcs"),
                         "unsupported": m.get("unsupported", 0)} for m in lifted],
            "generated_from": "user EBOOT at build time"}
