# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Game-shipped PRX modules (.sprx/.prx in USRDIR), recompiled into the build.

Some games load modules of their own at run time with sys_prx_load_module
(Just Cause 2: PathEngine.sprx). A static recompile cannot load new code at
run time, so this build step, run on the player's own game files:

  1. finds the modules the EBOOT names (their file name appears in it),
  2. decrypts each SELF-wrapped module (private feature, player's keys),
  3. relocates it to a fixed guest base with the backend's tools/lift_prx.py,
  4. lifts it with tools/ppu_lifter.py (own symbol prefix; the module's own
     imports become HLE calls via --hle-stubs),
  5. writes a registration file (runtime/prx/rr_game_prx.c picks it up) and
     adds the sources plus a copy of the relocated image to the CMake build.

At run time sys_prx_load_module/start_module then hand back and start the
recompiled module, and the game's imports resolve to its exports. Nothing is
generated for games without such modules, and no game code ships with the
Studio: everything is produced inside the user's own build folder.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

BASE = 0x3C000000      # above the raw-SPU windows, inside the dispatcher's PRX range
SLOT = 0x01000000      # 16 MB per module
MAX_MODULES = 4
START_NID, STOP_NID = 0xBC9A0086, 0xAB779874


class GamePrxError(RuntimeError):
    pass


def _slug(name):
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "module"
    return ("prx_" + s) if s[0].isdigit() else s


def find_modules(game_root, eboot_bytes):
    """Module files in USRDIR (recursively) whose file name the EBOOT mentions."""
    usrdir = Path(game_root) / "PS3_GAME" / "USRDIR"
    if not usrdir.is_dir():
        return []
    low = eboot_bytes.lower()
    found = []
    for path in sorted(usrdir.rglob("*")):
        if path.suffix.lower() not in (".sprx", ".prx") or not path.is_file():
            continue
        stem = path.stem.lower().encode()
        if path.name.lower().encode() in low or (stem + b".sprx") in low or (stem + b".prx") in low:
            found.append(path)
    return found[:MAX_MODULES]


def _decrypted(path, out_dir, home, settings):
    data = path.read_bytes()
    if data[:4] == b"\x7fELF":
        return path
    if data[:4] != b"SCE\0":
        raise GamePrxError(f"{path.name}: not a PRX or SELF")
    try:
        from .private_features import self_decrypt
    except ImportError as exc:
        raise GamePrxError(f"{path.name} is encrypted and the decrypter is not available in this build") from exc
    keys = self_decrypt.load_keys(home)
    out = Path(out_dir) / (path.stem + ".prx")
    self_decrypt.decrypt_file(path, out, keys, self_decrypt.rap_folders(home, settings))
    return out


def apply(port, game_root, eboot, backend, home, settings=None, python=None, emit=print):
    """Returns a manifest dict, or None when the game has no modules of its own."""
    port, backend = Path(port), Path(backend)
    python = python or sys.executable
    modules = find_modules(game_root, Path(eboot).read_bytes())
    if not modules:
        return None
    tools = backend / "tools"
    cmake = port / "CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    if not re.search(r"add_executable\s*\(\s*\$\{PROJECT_NAME\}", text, re.I):
        raise GamePrxError("generated CMake contract mismatch")
    results, lines = [], []
    for index, module in enumerate(modules):
        base = BASE + index * SLOT
        slug = _slug(module.stem)
        out = port / "prx" / slug
        out.mkdir(parents=True, exist_ok=True)
        prx = _decrypted(module, out, home, settings)
        r = subprocess.run([python, str(tools / "lift_prx.py"), str(prx), "--base", hex(base), "-o", str(out)],
                           capture_output=True, text=True)
        (out / "lift_prx.log").write_text(r.stdout + r.stderr, encoding="utf-8")
        if r.returncode:
            raise GamePrxError(f"{module.name}: relocation failed; see {out / 'lift_prx.log'}")
        stem = prx.stem
        exports = json.loads((out / f"{stem}_exports.json").read_text(encoding="utf-8"))
        imports = json.loads((out / f"{stem}_imports.json").read_text(encoding="utf-8"))
        image = out / f"{stem}_image.bin"
        size = image.stat().st_size
        if size > SLOT:
            raise GamePrxError(f"{module.name}: image is {size} bytes, larger than its 16 MB slot")
        stubs = [{"library": lib, "nid": nid, "stub": slot} for lib, ents in imports.items() for nid, slot in ents.items()]
        (out / "hle_stubs.json").write_text(json.dumps(stubs), encoding="utf-8")
        cmd = [python, str(tools / "ppu_lifter.py"), str(image), "--raw", "--base", hex(base),
               "--functions", str(out / f"{stem}_functions.json"), "--toc", exports["toc"],
               "--symbol-prefix", slug + "_", "--hle-stubs", str(out / "hle_stubs.json"),
               "--header-name", f"{slug}_recomp.h", "--source-name", f"{slug}_recomp.c", "-o", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        (out / "lift.log").write_text(r.stdout + r.stderr, encoding="utf-8")
        if r.returncode:
            raise GamePrxError(f"{module.name}: PPU lift failed; see {out / 'lift.log'}")
        syslib = exports["exports"].get("!syslib", {})
        start = int(syslib.get(f"0x{START_NID:08X}", "0"), 16)
        stop = int(syslib.get(f"0x{STOP_NID:08X}", "0"), 16)
        rows = [(int(nid, 16), int(opd, 16) - base) for lib, ents in exports["exports"].items()
                if lib != "!syslib" for nid, opd in ents.items()]
        exp_c = ",\n    ".join(f"{{0x{n:08X}u, 0x{v:08X}u}}" for n, v in rows) or "{0u, 0u}"
        register = out / f"{slug}_register.cpp"
        register.write_text(f'''/* Generated at build time by RetroRemakes Studio (game_prx.py) from the
 * user's own {module.name}: registers the recompiled module with the runtime. */
#include <stdint.h>
#include "prx_loader.h"
extern "C" {{
extern const prx_func_entry {slug}_function_table[];
extern const uint64_t {slug}_function_table_count;
void rr_game_prx_add(const char*, const char*, uint32_t, uint32_t, const prx_func_entry*, uint64_t,
                     const prx_export*, uint32_t, uint32_t, uint32_t);
extern void (*rr_game_prx_registrar)(uint64_t, void (*)(void*));
extern uint64_t (*rr_game_prx_caller)(uint32_t, uint64_t, uint64_t, uint64_t, uint64_t,
                                      uint64_t, uint64_t, uint64_t, uint64_t);
void ppu_register_function(uint64_t, void (*)(void*));
uint64_t ppu_guest_call(uint32_t, uint64_t, uint64_t, uint64_t, uint64_t,
                        uint64_t, uint64_t, uint64_t, uint64_t);
void rr_sys_libc_register(void);
}}
static const prx_export {slug}_exports[] = {{
    {exp_c}
}};
namespace {{
struct Register_{slug} {{
    Register_{slug}() {{
        rr_game_prx_registrar = ppu_register_function;
        rr_game_prx_caller = ppu_guest_call;
        rr_sys_libc_register();   /* the module's own sys_libc / sys_libstdcxx imports */
        rr_game_prx_add("{module.name.lower()}", "{stem}", 0x{base:08X}u, {size}u,
                        {slug}_function_table, {slug}_function_table_count,
                        {slug}_exports, {len(rows)}u, 0x{start:08X}u, 0x{stop:08X}u);
    }}
}} s_register_{slug};
}}
''', encoding="utf-8")
        rel = f"prx/{slug}"
        lines.append(f'file(GLOB {slug.upper()}_SOURCES "${{CMAKE_CURRENT_SOURCE_DIR}}/{rel}/*.cpp")')
        lines.append(f'target_sources(${{PROJECT_NAME}} PRIVATE ${{{slug.upper()}_SOURCES}})')
        lines.append(f'target_include_directories(${{PROJECT_NAME}} PRIVATE "${{CMAKE_CURRENT_SOURCE_DIR}}/{rel}" "${{PS3RECOMP_DIR}}/runtime/prx")')
        lines.append(f'add_custom_command(TARGET ${{PROJECT_NAME}} POST_BUILD '
                     f'COMMAND ${{CMAKE_COMMAND}} -E make_directory "$<TARGET_FILE_DIR:${{PROJECT_NAME}}>/prx" '
                     f'COMMAND ${{CMAKE_COMMAND}} -E copy_if_different "${{CMAKE_CURRENT_SOURCE_DIR}}/{rel}/{image.name}" '
                     f'"$<TARGET_FILE_DIR:${{PROJECT_NAME}}>/prx/{image.name}")')
        results.append({"module": module.name, "name": exports.get("module"), "base": f"0x{base:08X}",
                        "image_bytes": size, "exports": len(rows), "imports": len(stubs),
                        "module_start": f"0x{start:08X}"})
        emit(f"Recompiled game module {module.name} at 0x{base:08X} ({len(rows)} export(s), {len(stubs)} import(s)).")
    marker = "# Game PRX modules (generated from the user's own game files)."
    if marker not in text:
        cmake.write_text(text + "\n" + marker + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return {"schema": 1, "applied": True, "modules": results, "generated_from": "user game files at build time"}
