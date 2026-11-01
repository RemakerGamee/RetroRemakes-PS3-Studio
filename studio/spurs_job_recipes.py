# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Build-time lifting of SPURS job binaries embedded in the player's EBOOT.

Some engines (Devil May Cry HD Collection, and others built on the SDK's
"jobbin2" job format) keep their SPURS job programs as raw binaries inside the
EBOOT instead of SPU ELFs. SPU discovery only finds ELFs, so those jobs were
never lifted; at run time every one of them is a "[spurs-job] dispatch MISS",
the job never runs, and the PPU waits forever on the event flag or job guard
that the job should have set.

Each such binary starts with the job CRT, which carries the marker
"JOBCRT Ver<NN>" at offset 0x30 followed by the CRT's image size. At build time
this module finds those binaries in the user's own EBOOT, lifts each one at LS 0
(where spurs_job.c loads it) and registers it under the fingerprint the runtime
computes. The runtime hashes exactly the descriptor's sizeBinary bytes, which
is not stored in the binary, so every 16-byte size from the CRT's image size up
to the next binary is registered (a size that is never used never matches).

Nothing from the game ships: the code is generated from the player's EBOOT
into that player's own build folder.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

MARKER = re.compile(rb"JOBCRT Ver\d\d")
MAX_EXTRA = 0x400          # sizes registered beyond the CRT image size
LS_LIMIT = 0x3C000         # leave room for stack, buffers and the descriptor


def _loads(data):
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return []
    phoff = struct.unpack(">Q", data[0x20:0x28])[0]
    count = struct.unpack(">H", data[0x38:0x3A])[0]
    out = []
    for i in range(count):
        p_type, _f, off, vaddr, _pa, filesz, _ms, _al = struct.unpack(">IIQQQQQQ", data[phoff + i * 56:phoff + i * 56 + 56])
        if p_type == 1 and filesz:
            out.append((off, vaddr, filesz))
    return out


def find_jobs(data):
    """[(file_offset, crt_image_size, max_size)] for every embedded job binary."""
    loads = _loads(data)
    starts = []
    for m in MARKER.finditer(data):
        start = m.start() - 0x30
        if start < 0 or start % 16:
            continue
        size = struct.unpack(">I", data[m.start() + 12:m.start() + 16])[0]
        if not 0x40 < size <= LS_LIMIT:
            continue
        if not any(off <= start and start + size <= off + fs for off, _v, fs in loads):
            continue
        starts.append((start, size))
    jobs = []
    for k, (start, size) in enumerate(starts):
        limit = starts[k + 1][0] if k + 1 < len(starts) else start + size + MAX_EXTRA
        seg_end = next(off + fs for off, _v, fs in loads if off <= start < off + fs)
        top = min(limit, start + size + MAX_EXTRA, seg_end, start + LS_LIMIT)
        jobs.append((start, size, top - start))
    return jobs


def fingerprints(raw, first, last):
    """FNV-1a 64 of raw[:n] for every n in first..last step 16 (one pass)."""
    h, out = 1469598103934665603, {}
    for i, byte in enumerate(raw[:last]):
        h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        n = i + 1
        if n >= first and n % 16 == 0:
            out[n] = h
    return out


_WORD = re.compile(r'/\* TODO spu: \.word (0x[0-9A-Fa-f]+) \*/ spu_unsupported\((0x[0-9A-Fa-f]+), "\.word"\);')
_HALT = ('ctx->stop_code = SPU_HOST_FAILURE_UNSUPPORTED; '
         'ctx->status = SPU_STATUS_STOPPED_BY_HALT; spu_halt(ctx); return;')


def _lift(raw, out, prefix, tools):
    sys.path.insert(0, str(tools))
    try:
        from wrap_spu_elf import wrap
    finally:
        sys.path.remove(str(tools))
    out.mkdir(parents=True, exist_ok=True)
    (out / "job.elf").write_bytes(wrap(raw, base=0, entry=0))
    cmd = [sys.executable, str(tools / "spu_lifter.py"), "--auto-functions", str(out / "job.elf"),
           "--base", "0x0", "--symbol-prefix", prefix, "-o", str(out), "--report", str(out / "lift.json")]
    result = subprocess.run(cmd, cwd=str(tools.parent), capture_output=True, text=True)
    (out / "lift.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        return None
    report = json.loads((out / "lift.json").read_text(encoding="utf-8"))
    if set(report.get("unsupported", {})) - {".word"}:
        return None
    starts = {s for s, _ in report["functions"]}
    if 0 not in starts:
        return None
    source = out / "spu_recomp.c"
    code = source.read_text(encoding="utf-8").replace('"../../runtime/spu/spu_helpers.h"', '"spu_helpers.h"')
    code, words = _WORD.subn(lambda m: '{ spu_unsupported(%s, ".word data executed (job)"); %s }' % (m.group(2), _HALT), code)
    if "/* TODO spu:" in code:
        return None
    orphans = sorted((set(report["call_targets"]) | set(report["branch_targets"])) - starts)
    orphans = [t for t in orphans if f"void {prefix}spu_func_{t:08X}(spu_context* ctx) {{" not in code]
    code = code.replace('#include "spu_helpers.h"',
                        '#include "spu_helpers.h"\n#include "spu_host_failure.h"\nextern void spu_halt(spu_context*);', 1)
    if orphans:
        code += "\n/* branch targets that decode from data: halt if ever reached */\n" + "".join(
            f'void {prefix}spu_func_{t:08X}(spu_context* ctx) {{ spu_unsupported(0x{t:X}, "branch into data (job)"); {_HALT} }}\n'
            for t in orphans)
        header = out / "spu_recomp.h"
        text = header.read_text(encoding="utf-8")
        decls = "".join(f"void {prefix}spu_func_{t:08X}(spu_context* ctx);\n" for t in orphans)
        cut = text.rfind("#endif")
        header.write_text(text[:cut] + decls + text[cut:] if cut >= 0 else text + decls, encoding="utf-8")
    source.write_text(code, encoding="utf-8")
    header = out / "spu_recomp.h"
    header.write_text(header.read_text(encoding="utf-8").replace('"../../runtime/spu/', '"'), encoding="utf-8")
    return {"functions": len(report["functions"]), "data_words_halting": words, "orphan_stubs": len(orphans)}


def apply(stage, elf, backend, already=()):
    """Lift every embedded SPURS job binary. Returns a summary dict, or None if
    the EBOOT has none. `already` = fingerprints SPU discovery registered."""
    stage, backend = Path(stage), Path(backend)
    try:
        data = Path(elf).read_bytes()
    except OSError:
        return None
    jobs = find_jobs(data)
    if not jobs:
        return None
    cmake = stage / "CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    if '"${SPU_DIR}/*/spu_recomp.c"' not in text:
        return {"applied": False, "reason": "generated CMake contract mismatch"}
    tools = backend / "tools"
    lines, done, skipped = [], [], []
    for start, size, top in jobs:
        raw = data[start:start + top]
        digest = hashlib.sha256(raw[:size]).hexdigest()
        prefix = f"rrjob_{digest[:12]}_"
        image_id = 0x40000000 | (int(digest[:7], 16) & 0x0FFFFFFF)
        info = _lift(raw, stage / "spu" / prefix[:-1], prefix, tools)
        if info is None:
            skipped.append(hex(start)); continue
        fps = {n: fp for n, fp in fingerprints(raw, (size + 15) & ~15, top).items() if fp not in already}
        regs = "".join(f'    spu_workload_register_img(0x{fp:016X}ULL, {prefix}spu_func_00000000, 0x{image_id:08X}, "rrjob {start:#x} size {n}");\n'
                       for n, fp in sorted(fps.items()))
        lines.append((prefix, image_id, regs))
        done.append({"offset": hex(start), "crt_size": size, "sizes_registered": len(fps), **info})
    if not lines:
        return {"applied": False, "reason": "no embedded job lifted", "skipped": skipped}
    reg = stage / "spu" / "rr_spurs_jobs_register.c"
    body = ['/* Generated at build time by RetroRemakes Studio (spurs_job_recipes.py):',
            ' * SPURS job binaries lifted from the user\'s own EBOOT. */',
            '#include "spu_context.h"', '#include "spu_workload.h"',
            'extern void spu_begin_image(int image_id);']
    body += [f"extern void {p}spu_recomp_register(void);\nextern void {p}spu_func_00000000(spu_context*);" for p, _i, _r in lines]
    body.append("__attribute__((constructor)) static void rr_spurs_jobs_register(void)\n{")
    for p, image_id, regs in lines:
        body.append(f"    spu_begin_image(0x{image_id:08X});\n    {p}spu_recomp_register();\n{regs}")
    body.append("    spu_begin_image(0);\n}")
    reg.write_text("\n".join(body) + "\n", encoding="utf-8")
    line = 'target_sources(${PROJECT_NAME} PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/spu/rr_spurs_jobs_register.c")'
    if line not in text:
        cmake.write_text(text + "\n# SPURS job binaries lifted from the user's EBOOT.\n" + line + "\n", encoding="utf-8")
    return {"applied": True, "jobs": done, "skipped": skipped, "generated_from": "user EBOOT at build time"}


if __name__ == "__main__":
    data = Path(sys.argv[1]).read_bytes()
    for start, size, top in find_jobs(data):
        print(hex(start), hex(size), hex(top))
