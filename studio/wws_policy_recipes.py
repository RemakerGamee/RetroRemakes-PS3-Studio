# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Build-time recipes for raw WWS job-manager policy modules.

Some titles (Killzone 2, MotorStorm: Pacific Rift) run every SPURS job
workload through one raw SPU policy module embedded in the EBOOT. SPU
discovery only finds SPU ELFs, so without this the runtime refuses those
workloads ("PM NOT LIFTED") and the title shows a black screen.

A recipe contains NO game code or data: only where the module sits in the
user's own EBOOT, hashes to recognise it, and lifting options. At build time
the module is read from the user's EBOOT, lifted with the backend's
spu_lifter, and registered with code written here. Everything generated goes
into that user's own build directory.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

RECIPES = (
    {"id": "killzone2-wws-policy", "title": "Killzone 2",
     "input_sha256": "39a28358efd91fd0e914944392a3807b0141e4c5b6ba22e4964640c9c43b9799",
     "offset": 0xD7B000, "size": 11904,
     "raw_sha256": "ac60a58849cc0bee5a2ab77c4b99bc2a375de23f36c8c0150bd33bce0ddad991",
     "fnv1a64": "DDD59DC528B4CED6", "ls_base": 0xA00, "extra_entries": [0xA2C],
     "prefix": "kz2_wws", "image_id": 0x2C60A588, "functions": 199, "data_words": 4,
     "poll": {"ls": 0x2520, "pc": 0x25F4,
              "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "motorstorm-pr-wws-policy", "title": "MotorStorm: Pacific Rift",
     "input_sha256": "bbb7fe2545e54847483439f59cda54e907db23a111d7afe9a1b655624886b287",
     "offset": 0xFECC80, "size": 11520,
     "raw_sha256": "145395f1434fbdbb7a9596795bf0cc439b691e25d624cee9d87c1de6aeddfd0d",
     "fnv1a64": "093FC7EE6C9F7E84", "ls_base": 0xA00, "extra_entries": [0xA2C],
     "prefix": "motorstorm_wws", "image_id": 0x145395F1, "functions": 188, "data_words": 0,
     "poll": {"ls": 0x24D0, "pc": 0x25A4,
              "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "infamous-wws-policy", "title": "inFAMOUS",
     "input_sha256": "4f2db3fb7c76fbb4f80cbc8445cf0ea1570a664996a7787e690efcc1a39befb4",
     "offset": 0x68C880, "size": 11648,
     "raw_sha256": "2fedf78bc38dd708d5e950c220baec9e987db677cf776614fc4bdf5e289d1b56",
     "fnv1a64": "6FFFB30E41EE17BC", "ls_base": 0xA00, "extra_entries": [0xA2C],
     "prefix": "infamous_wws", "image_id": 0x2FEDF78B, "functions": 188, "data_words": 0,
     "poll": {"ls": 0x24D0, "pc": 0x25A4,
              "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "infamous2-wws-policy", "title": "inFAMOUS 2",
     "input_sha256": "bd4569d249e901449f2e0f6e3a289387af48cb02f637ba8ef950c13c5eb8c2a0",
     "offset": 0x877080, "size": 11904,
     "raw_sha256": "3a1930a2bc834787b6d4e74d9cfc1d78d87d42d07d2bd56ea3a1090ac72f237d",
     "fnv1a64": "B3523C4B17650EF8", "ls_base": 0xA00, "extra_entries": [0xA2C],
     "prefix": "infamous2_wws", "image_id": 0x3A1930A2, "functions": 195, "data_words": 4,
     "poll": {"ls": 0x2440, "pc": 0x2514,
              "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "gt5-wws-policy", "title": "Gran Turismo 5", "input_sha256": "bbbc31048dd608f8e959d9a84205b9fd7001b480b707af08c9d1f8c0816672bc", "offset": "0x176f200", "size": 12032, "raw_sha256": "2974ca2ed283d98b16e10d9b1db9fc56ac958b87108686dc6f5f6e6ec2d55176", "fnv1a64": "75BFE68AE96D7577", "ls_base": "0xA00", "extra_entries": ["0xA2C"], "prefix": "gt5_wws", "image_id": "0x2974ca2e", "functions": 195, "data_words": 4, "poll": {"ls": "0x2460", "pc": "0x2534", "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "lbp-wws-policy", "title": "LittleBigPlanet - Game of the Year Edition", "input_sha256": "b04488f2aff6913eefb6a36e7f5d49f85bdd687f783a51fa0726f2c9f0fe2d80", "offset": "0x8f8a80", "size": 12416, "raw_sha256": "a6ae35980b0cda7bf13e5ee26ca7f5a82bede6a85adbe8e7dea3050dd33f7a4f", "fnv1a64": "4B9DB22BE4E172EB", "ls_base": "0xA00", "extra_entries": ["0xA2C"], "prefix": "lbp_wws", "image_id": "0x26ae3598", "functions": 194, "data_words": 0, "poll": {"ls": "0x24d0", "pc": "0x25a4", "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "resistance2-wws-policy", "title": "Resistance 2", "input_sha256": "408eb55fa3f00fc43823517df28044451d9192e2023219c98ebb6f95e41e89f5", "offset": "0xce9680", "size": 12544, "raw_sha256": "b9936c8edc7df451b6da768a6314a7354202a567d193539b2214848766ccd2ad", "fnv1a64": "924CAF0A30D9F98F", "ls_base": "0xA00", "extra_entries": ["0xA2C"], "prefix": "resistance2_wws", "image_id": "0x39936c8e", "functions": 236, "data_words": 0, "poll": {"ls": "0x2750", "pc": "0x2824", "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "resistance3-wws-policy", "title": "Resistance 3", "input_sha256": "6d8d1dabe589bbaa8c2fd65bae0026bf11fe24dd89d3a1de429c3f164879df67", "offset": "0xea9e00", "size": 12544, "raw_sha256": "a13fde9c910e411243073e6aedaffce51a736ef4109ceff3926ebab3f986f951", "fnv1a64": "EA3A104FE30430AA", "ls_base": "0xA00", "extra_entries": ["0xA2C"], "prefix": "resistance3_wws", "image_id": "0x213fde9c", "functions": 234, "data_words": 0, "poll": {"ls": "0x2768", "pc": "0x283c", "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "twistedmetal-wws-policy", "title": "Twisted Metal", "input_sha256": "269642a56f3c70be74df41049876887d07aedd4171ce18a481a05067b1b8564e", "offset": "0xebe580", "size": 11904, "raw_sha256": "3a1930a2bc834787b6d4e74d9cfc1d78d87d42d07d2bd56ea3a1090ac72f237d", "fnv1a64": "B3523C4B17650EF8", "ls_base": "0xA00", "extra_entries": ["0xA2C"], "prefix": "twistedmetal_wws", "image_id": "0x3a1930a2", "functions": 195, "data_words": 4, "poll": {"ls": "0x2440", "pc": "0x2514", "sha256": "54c3d930ee7a6a47d6c7c77382817fc610485c9278ec6f503a0d41f1757e5139"}},
    {"id": "warhawk-wws-policy", "title": "Warhawk", "input_sha256": "8513e1357bf620ce133a5ea06aaea38cfca28675e1561ebc899243ed2f68d496", "offset": "0x781980", "size": 11136, "raw_sha256": "01894e565deaf941f9b9cf26f4676101996e03f0a22146b3a59e2cbf108ce832", "fnv1a64": "231CD5AA1CB29C4D", "ls_base": "0xA00", "extra_entries": ["0xA2C"], "prefix": "warhawk_wws", "image_id": "0x1894e56", "functions": 188, "data_words": 0, "poll": None},
)
def _ints(recipe):
    """Accept hex strings in appended entries ("0x176F200") as integers."""
    num = lambda v: int(v, 0) if isinstance(v, str) else v
    for key in ("offset", "size", "ls_base", "image_id", "functions", "data_words"):
        recipe[key] = num(recipe[key])
    recipe["extra_entries"] = [num(v) for v in recipe["extra_entries"]]
    if recipe.get("poll"):   # older WWS versions have a different idle loop: no idle classifier
        recipe["poll"]["ls"], recipe["poll"]["pc"] = num(recipe["poll"]["ls"]), num(recipe["poll"]["pc"])
    return recipe


RECIPES = tuple(_ints(dict(r, poll=dict(r["poll"]) if r.get("poll") else None)) for r in RECIPES)
POLL_SIZE = 256
DIAGNOSTIC_ENV = {"SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": "1000000"}


class RecipeError(RuntimeError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _fnv(data):
    value = 1469598103934665603
    for byte in data:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016X}"


def recipe_for(input_sha256, data=None):
    """Match by the whole EBOOT, or (for patched copies of it) by the policy module itself."""
    found = next((r for r in RECIPES if r["input_sha256"] == input_sha256), None)
    if found is None and data is not None:
        found = next((r for r in RECIPES if _sha(data[r["offset"]:r["offset"] + r["size"]]) == r["raw_sha256"]), None)
    return found


def register_name(recipe):
    return f"{recipe['prefix']}_policy_register.c"


_WORD = re.compile(r'/\* TODO spu: \.word (0x[0-9A-Fa-f]+) \*/ spu_unsupported\((0x[0-9A-Fa-f]+), "\.word"\);')


def _register_tail(recipe):
    p, image = recipe["prefix"], recipe["image_id"]
    return f'''extern void {p}_spu_func_00000A00(spu_context*);
__attribute__((constructor)) static void rr_{p}_policy_register(void)
{{
    spu_begin_image(0x{image:08X});
    {p}_spu_recomp_register();
    spu_workload_register_img(0x{recipe["fnv1a64"]}ULL, {p}_spu_func_00000A00, 0x{image:08X},
                              "{recipe["title"]} Wws_Job raw policy");
}}
'''


def _register_source(recipe, poll):
    p, image = recipe["prefix"], recipe["image_id"]
    if poll is None:
        return f'''/* Generated at build time by RetroRemakes Studio (wws_policy_recipes.py)
 * for {recipe["title"]}: registers the raw WWS policy module lifted from the
 * user's own EBOOT (older WWS version: no idle-poll classifier). */
#include "spu_context.h"
#include "spu_workload.h"
extern void spu_begin_image(int image_id);
extern void {p}_spu_recomp_register(void);
''' + _register_tail(recipe)
    poll_bytes = ", ".join(f"0x{b:02X}" for b in poll)
    pc, ls = recipe["poll"]["pc"], recipe["poll"]["ls"]
    return f'''/* Generated at build time by RetroRemakes Studio (wws_policy_recipes.py)
 * for {recipe["title"]}: registers the raw WWS policy module lifted from the
 * user's own EBOOT. The poll bytes below were read from that EBOOT. */
#include "spu_context.h"
#include "spu_workload.h"
#include "spu_idle_poll.h"
#include <string.h>
extern void spu_begin_image(int image_id);
extern void {p}_spu_recomp_register(void);
extern void {p}_spu_func_00000A00(spu_context*);
static const uint8_t {p}_poll_code[] = {{{poll_bytes}}};
/* WWS empty-command poll: classify only an all-zero command fetch by the
 * exact poll routine as idle; any posted command takes the normal path. */
static int {p}_idle_dma(const spu_context* ctx, uint32_t lsa, uint64_t ea,
    uint32_t size, uint32_t cmd, uint32_t tag, const uint8_t* payload)
{{
    if (ctx->image_id != 0x{image:08X} || ctx->pc != 0x{pc:X} || lsa != 0xD00 ||
        size != 128 || cmd != 0x40 || tag != 8 || ctx->gpr[60]._u32[0] != 0 ||
        ctx->gpr[70]._u32[0] != (uint32_t)ea ||
        memcmp(ctx->ls + 0x{ls:X}, {p}_poll_code, sizeof {p}_poll_code)) return 0;
    for (unsigned i = 0; i < 128; i++) if (payload[i] || ctx->ls[0xD00 + i]) return 0;
    return 1;
}}
__attribute__((constructor)) static void rr_{p}_policy_register(void)
{{
    g_spu_idle_dma = {p}_idle_dma;
    spu_begin_image(0x{image:08X});
    {p}_spu_recomp_register();
    spu_workload_register_img(0x{recipe["fnv1a64"]}ULL, {p}_spu_func_00000A00, 0x{image:08X},
                              "{recipe["title"]} Wws_Job raw policy");
}}
'''


def apply(stage, elf, backend, project):
    """Generate and register the policy module for a matching input.
    Returns None when the input has no recipe (nothing is touched)."""
    stage, backend = Path(stage), Path(backend)
    try:
        data = Path(elf).read_bytes()
    except OSError:
        return None
    input_hash = _sha(data)
    recipe = recipe_for(input_hash, data)
    if recipe is None:
        return None
    raw = data[recipe["offset"]:recipe["offset"] + recipe["size"]]
    if len(raw) != recipe["size"] or _sha(raw) != recipe["raw_sha256"] or _fnv(raw) != recipe["fnv1a64"]:
        raise RecipeError(f"{recipe['title']}: policy module in this EBOOT does not match the recipe")
    poll = None
    if recipe.get("poll"):
        start = recipe["poll"]["ls"] - recipe["ls_base"]
        poll = raw[start:start + POLL_SIZE]
        if _sha(poll) != recipe["poll"]["sha256"]:
            raise RecipeError(f"{recipe['title']}: idle-poll routine does not match the recipe")
    cmake = stage / "CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    if '"${SPU_DIR}/*/spu_recomp.c"' not in text or not re.search(r"add_executable\s*\(\s*\$\{PROJECT_NAME\}", text, re.I):
        raise RecipeError("generated CMake contract mismatch")
    out = stage / "spu" / recipe["prefix"]
    out.mkdir(parents=True, exist_ok=True)
    tools = backend / "tools"
    sys.path.insert(0, str(tools))
    try:
        from wrap_spu_elf import wrap
    finally:
        sys.path.remove(str(tools))
    (out / "policy.elf").write_bytes(wrap(raw, base=recipe["ls_base"], entry=recipe["ls_base"]))
    cmd = [sys.executable, str(tools / "spu_lifter.py"), "--auto-functions", str(out / "policy.elf"),
           "--base", hex(recipe["ls_base"]), "--symbol-prefix", recipe["prefix"] + "_",
           "-o", str(out), "--report", str(out / "lift.json")]
    if recipe["extra_entries"]:
        cmd += ["--extra-funcs", ",".join(hex(a) for a in recipe["extra_entries"])]
    result = subprocess.run(cmd, cwd=str(backend), capture_output=True, text=True)
    (out / "lift.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RecipeError(f"{recipe['title']}: SPU lifter failed; see {out / 'lift.log'}")
    report = json.loads((out / "lift.json").read_text(encoding="utf-8"))
    words = report.get("unsupported", {}).get(".word", 0)
    if len(report["functions"]) != recipe["functions"] or set(report.get("unsupported", {})) - {".word"} \
            or words != recipe["data_words"] or not any(f[0] == 0xA2C for f in report["functions"]):
        raise RecipeError(f"{recipe['title']}: lift coverage differs from the verified recipe")
    source = out / "spu_recomp.c"
    code = source.read_text(encoding="utf-8").replace('"../../runtime/spu/spu_helpers.h"', '"spu_helpers.h"')
    code, count = _WORD.subn(lambda m: ('{ spu_unsupported(%s, ".word data executed"); '
                                        'ctx->stop_code = SPU_HOST_FAILURE_UNSUPPORTED; '
                                        'ctx->status = SPU_STATUS_STOPPED_BY_HALT; spu_halt(ctx); return; }'
                                        % m.group(2)), code)
    if count != recipe["data_words"]:
        raise RecipeError(f"{recipe['title']}: expected {recipe['data_words']} data words, found {count}")
    if count:
        code = code.replace('#include "spu_helpers.h"',
                            '#include "spu_helpers.h"\n#include "spu_host_failure.h"\nextern void spu_halt(spu_context*);', 1)
    if "/* TODO spu:" in code:
        raise RecipeError(f"{recipe['title']}: unresolved instruction in lifted policy")
    source.write_text(code, encoding="utf-8")
    header = out / "spu_recomp.h"
    header.write_text(header.read_text(encoding="utf-8").replace('"../../runtime/spu/', '"'), encoding="utf-8")
    (stage / "spu" / register_name(recipe)).write_text(_register_source(recipe, poll), encoding="utf-8")
    line = f'target_sources(${{PROJECT_NAME}} PRIVATE "${{CMAKE_CURRENT_SOURCE_DIR}}/spu/{register_name(recipe)}")'
    if line not in text:
        cmake.write_text(text + f"\n# {recipe['title']} WWS policy module (generated from the user's EBOOT).\n" + line + "\n",
                         encoding="utf-8")
    # A game-patched build lifts from a patched COPY of the EBOOT; the caller binds
    # receipts to the player's original EBOOT, so report that hash when it is the
    # one this recipe was made for (the module bytes were verified above either way).
    bound_hash = input_hash
    if input_hash != recipe["input_sha256"] and isinstance(project, dict) and project.get("source"):
        try:
            from .core import choose_elf
            original = choose_elf(project["source"], project.get("elf", ""))
            if _sha(Path(original).read_bytes()) == recipe["input_sha256"]:
                bound_hash = recipe["input_sha256"]
        except (OSError, ValueError, RuntimeError):
            pass
    return {"schema": 1, "applied": True, "adapter_id": recipe["id"], "version": 2,
            "project_id": project.get("id", "") if isinstance(project, dict) else str(project),
            "input_sha256": bound_hash, "lifted_input_sha256": input_hash, "raw_fingerprint": recipe["fnv1a64"],
            "generated_from": "user EBOOT at build time", "functions": len(report["functions"]),
            "data_words_halting": count, "stage": str(stage.resolve()), "private": True,
            "mode": "hybrid-SPU", "playability": "unverified", "diagnostic_env": dict(DIAGNOSTIC_ENV)}
