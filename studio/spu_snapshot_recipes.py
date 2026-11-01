# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Lift SPU code that a game builds in local store at run time.

Some SPU programs (MotorStorm: Pacific Rift's menu video path, for one) jump
into code they assemble or unpack in local store while running. No game file
contains that code, so it can't be lifted ahead of time and runs on the slow
SPU interpreter. The runtime (runtime/spu/rr_spu_snapshot.c) saves each such
code window once, with the entry points it saw, to <exe dir>/spu_snapshots/.

At build time this module collects those snapshots for the project, keeps a
copy in the project folder (so build cleanup never loses them), lifts each one
at its local-store address and registers the functions with a code-range
guard: the runtime uses a lifted function only while the bytes of the lifted
code still hash to what was lifted. Data sharing the window (which changes
every run) is ignored; code the game changes later safely falls back.
Snapshots that hold the same code are registered once.

Snapshots are the user's own game-derived data; they stay in the user's
project folder and are never part of a Studio release.
"""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

WINDOW = 0x8000
MAX_SNAPSHOTS = 48
_NAME = re.compile(r"^(-?\d+)_([0-9A-F]{5})_([0-9A-F]{16})\.bin$")
_WORD = re.compile(r'/\* TODO spu: \.word (0x[0-9A-Fa-f]+) \*/ spu_unsupported\((0x[0-9A-Fa-f]+), "\.word"\);')
_HALT = ('ctx->stop_code = SPU_HOST_FAILURE_UNSUPPORTED; '
         'ctx->status = SPU_STATUS_STOPPED_BY_HALT; spu_halt(ctx); return;')


def fnv64(data):
    h = 1469598103934665603
    for b in data:
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def _sources(project_dir, fixes_root, stage=None):
    """Every spu_snapshots folder a test run of this project could have written."""
    roots = [Path(project_dir) / "spu-snapshots"]
    roots += sorted(Path(project_dir).glob("attempts/*/build/player/spu_snapshots"))
    if fixes_root:
        roots += sorted(Path(fixes_root).glob("*/workspace/a/build/player/spu_snapshots"))
    real_projects, real_repairs = _real_roots(stage) if stage else (None, None)
    if real_projects is not None:
        roots += [real_projects / "spu-snapshots"]
        roots += sorted(real_projects.glob("attempts/*/build/player/spu_snapshots"))
        roots += sorted(real_repairs.glob("*/workspace/a/build/player/spu_snapshots"))
    seen = []
    for r in roots:
        if r.is_dir() and r.resolve() not in [x.resolve() for x in seen]:
            seen.append(r)
    return seen


def collect(project_dir, fixes_root=None, stage=None):
    """Copy every valid snapshot into <project>/spu-snapshots and return them."""
    real_projects, _ = _real_roots(stage) if stage else (None, None)
    store = (real_projects if real_projects is not None and real_projects.is_dir() else Path(project_dir)) / "spu-snapshots"
    found = {}
    for root in _sources(project_dir, fixes_root, stage):
        for binf in root.glob("*.bin"):
            m = _NAME.match(binf.name)
            meta = binf.with_suffix(".json")
            if not m or not meta.is_file():
                continue
            try:
                raw = binf.read_bytes(); info = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            image, base, digest = int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16)
            if len(raw) != WINDOW or fnv64(raw) != digest or base % 4 or base + WINDOW > 0x40000:
                continue
            entries = sorted({int(e) for e in info.get("entries", []) if base <= int(e) < base + WINDOW and int(e) % 4 == 0})
            key = binf.name
            prev = found.get(key)
            if prev:
                prev["entries"] = sorted(set(prev["entries"]) | set(entries)); continue
            found[key] = {"image": image, "base": base, "hash": digest, "entries": entries, "raw": raw}
    if found:
        store.mkdir(parents=True, exist_ok=True)
        for name, s in found.items():
            (store / name).write_bytes(s["raw"])
            (store / name).with_suffix(".json").write_text(json.dumps(
                {"schema": 1, "image_id": s["image"], "base": s["base"], "size": WINDOW,
                 "hash": f"{s['hash']:016X}", "entries": s["entries"]}), encoding="utf-8")
    return sorted(found.values(), key=lambda s: (s["image"], s["base"], s["hash"]))[:MAX_SNAPSHOTS]


def _lift(snap, out, prefix, tools):
    sys.path.insert(0, str(tools))
    try:
        from wrap_spu_elf import wrap
    finally:
        sys.path.remove(str(tools))
    out.mkdir(parents=True, exist_ok=True)
    base = snap["base"]
    entries = snap["entries"] or [base]
    (out / "snap.elf").write_bytes(wrap(snap["raw"], base=base, entry=entries[0]))
    cmd = [sys.executable, str(tools / "spu_lifter.py"), "--auto-functions", str(out / "snap.elf"),
           "--base", hex(base), "--symbol-prefix", prefix, "-o", str(out), "--report", str(out / "lift.json"),
           "--extra-funcs", ",".join(hex(e) for e in entries)]
    result = subprocess.run(cmd, cwd=str(tools.parent), capture_output=True, text=True)
    (out / "lift.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        return None
    report = json.loads((out / "lift.json").read_text(encoding="utf-8"))
    if set(report.get("unsupported", {})) - {".word"}:
        return None
    starts = {s for s, _ in report["functions"]}
    if not set(entries) <= starts:
        return None
    source = out / "spu_recomp.c"
    code = source.read_text(encoding="utf-8").replace('"../../runtime/spu/spu_helpers.h"', '"spu_helpers.h"')
    code, words = _WORD.subn(lambda m: '{ spu_unsupported(%s, ".word data executed (snapshot)"); %s }'
                             % (m.group(2), _HALT), code)
    if "/* TODO spu:" in code:
        return None
    # Branches out of the window, or into data: a halting stub keeps the build
    # linkable; at run time the guard and interpreter cover anything real.
    orphans = sorted((set(report["call_targets"]) | set(report["branch_targets"])) - starts)
    orphans = [t for t in orphans if f"void {prefix}spu_func_{t:08X}(spu_context* ctx) {{" not in code]
    code = code.replace('#include "spu_helpers.h"',
                        '#include "spu_helpers.h"\n#include "spu_host_failure.h"\nextern void spu_halt(spu_context*);', 1)
    header = out / "spu_recomp.h"
    htext = header.read_text(encoding="utf-8").replace('"../../runtime/spu/', '"')
    if orphans:
        base, end = snap["base"], snap["base"] + WINDOW
        def stub(t):
            if base <= t < end:   # inside the window but not code: data decoded as a branch
                return (f'void {prefix}spu_func_{t:08X}(spu_context* ctx) {{ '
                        f'spu_unsupported(0x{t:X}, "branch into data (snapshot)"); {_HALT} }}\n')
            # Real code elsewhere in local store (the program's own lifted image, or
            # the interpreter): hand it to the runtime dispatcher, which runs the
            # target and returns here, the same as a direct call.
            return f'void {prefix}spu_func_{t:08X}(spu_context* ctx) {{ ctx->pc = 0x{t:X}u; spu_indirect_branch(ctx); }}\n'
        code += "\n/* branch targets the snapshot doesn't contain */\n" + "".join(stub(t) for t in orphans)
        decls = "".join(f"void {prefix}spu_func_{t:08X}(spu_context* ctx);\n" for t in orphans)
        cut = htext.rfind("#endif")
        htext = htext[:cut] + decls + htext[cut:] if cut >= 0 else htext + decls
    source.write_text(code, encoding="utf-8")
    header.write_text(htext, encoding="utf-8")
    return {"functions": sorted(starts), "ranges": sorted((int(a), int(b)) for a, b in report["functions"]),
            "data_words_halting": words, "orphan_stubs": len(orphans)}


def _real_roots(stage):
    """Inside a fix-build worker the Studio's project folders are an isolated
    copy, so also look in the real output folders the stage lives under:
    .../Repairs/<id>/<job>/workspace/a/port -> Repairs/<id> and Projects/<id>."""
    parts = Path(stage).resolve().parts
    for i, name in enumerate(parts):
        if name.lower() == "repairs" and i + 1 < len(parts):
            repairs = Path(*parts[:i + 2]); projects = Path(*parts[:i]) / "Projects" / parts[i + 1]
            return projects, repairs
        if name.lower() == "projects" and i + 1 < len(parts):
            projects = Path(*parts[:i + 2]); repairs = Path(*parts[:i]) / "Repairs" / parts[i + 1]
            return projects, repairs
    return None, None


def apply(stage, project_dir, fixes_root, backend, emit=print):
    """Lift this project's runtime SPU snapshots into the build. Returns a
    summary dict, or None when the project has no snapshots."""
    stage, backend = Path(stage), Path(backend)
    snaps = collect(project_dir, fixes_root, stage)
    if not snaps:
        return None
    cmake = stage / "CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    if '"${SPU_DIR}/*/spu_recomp.c"' not in text:
        return {"applied": False, "reason": "generated CMake contract mismatch"}
    tools = backend / "tools"
    sets, done, skipped, seen_code = [], [], [], set()
    for s in snaps:
        prefix = f"rrsnap_{s['image'] & 0xFFFFFFFF:08X}_{s['base']:05X}_{s['hash']:016X}_"
        info = _lift(s, stage / "spu" / prefix[:-1], prefix, tools)
        if info is None:
            skipped.append(prefix[:-1]); continue
        raw, base, ranges = s["raw"], s["base"], info["ranges"]
        code = b"".join(raw[a - base:b - base] for a, b in ranges)
        key = (s["image"], base, tuple(ranges), fnv64(code))
        if key in seen_code:          # same code as an earlier snapshot, different data
            skipped.append(prefix[:-1] + " (same code)"); continue
        seen_code.add(key)
        fns = [(a, b, fnv64(raw[a - base:b - base])) for a, b in ranges if a in set(info["functions"])]
        sets.append((prefix, s, ranges, key[3], fns))
        done.append({"image": s["image"], "base": hex(base), "entries": [hex(e) for e in s["entries"]],
                     "functions": len(fns), "code_hash": f"{key[3]:016X}",
                     "data_words_halting": info["data_words_halting"]})
    if not sets:
        return {"applied": False, "reason": "no snapshot lifted", "skipped": skipped}
    body = ['/* Generated at build time by RetroRemakes Studio (spu_snapshot_recipes.py):',
            ' * SPU code the game built in local store at run time, lifted from snapshots',
            ' * taken on this machine. Each function is used only while the lifted code',
            ' * bytes still match (code-range guard); data around it is ignored. */',
            '#include "spu_context.h"', '#include "rr_spu_snapshot.h"']
    body += sorted({f"extern void {p}spu_func_{a:08X}(spu_context*);" for p, _s, _r, _h, fns in sets for a, _b, _fh in fns})
    for i, (p, s, ranges, ch, fns) in enumerate(sets):
        body.append(f"static const uint32_t rr_snap_ranges_{i}[] = {{" +
                    ", ".join(f"0x{a:05X}u, 0x{b:05X}u" for a, b in ranges) + "};")
    body.append("__attribute__((constructor)) static void rr_spu_snapshots_register(void)\n{")
    body.append("    int set;")
    for i, (p, s, ranges, ch, fns) in enumerate(sets):
        body.append(f"    set = rr_spu_snapshot_register_set({s['image']}, 0x{s['base']:05X}u, rr_snap_ranges_{i}, "
                    f"{len(ranges)}u, 0x{ch:016X}ull);")
        for a, b, fh in fns:
            body.append(f"    rr_spu_snapshot_register_fn(set, 0x{a:05X}u, 0x{b:05X}u, 0x{fh:016X}ull, {p}spu_func_{a:08X});")
    body.append("}")
    nfuncs = sum(len(x[4]) for x in sets)
    reg = stage / "spu" / "rr_spu_snapshots_register.c"
    reg.write_text("\n".join(body) + "\n", encoding="utf-8")
    line = 'target_sources(${PROJECT_NAME} PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/spu/rr_spu_snapshots_register.c")'
    if line not in text:
        cmake.write_text(text + "\n# Runtime-built SPU code lifted from local snapshots.\n" + line + "\n", encoding="utf-8")
    emit(f"Lifted {len(done)} runtime SPU code snapshot(s) ({nfuncs} functions, code-range guarded).")
    return {"applied": True, "snapshots": done, "skipped": skipped, "generated_from": "local runtime snapshots"}
