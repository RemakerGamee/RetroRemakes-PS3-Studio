/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * rr_game_prx.c -- game-shipped PRX modules recompiled into the build.
 *
 * Some games ship their own modules (.sprx files in USRDIR, e.g. Just Cause 2's
 * PathEngine.sprx) and load them with sys_prx_load_module at run time. A
 * static recompile cannot load new code at run time, so the Studio's build
 * step decrypts each module from the player's own game files, relocates it to
 * a fixed guest base (tools/lift_prx.py), lifts it with ppu_lifter.py
 * (--symbol-prefix, --hle-stubs for the module's own imports) and generates a
 * small registration file that calls rr_game_prx_add() from a static
 * constructor.
 *
 * At run time sys_prx_load_module() asks rr_game_prx_load() first: when the
 * requested file is one of these modules, its relocated image (written beside
 * the executable as prx/<name>_image.bin at build time) is placed in guest
 * memory, every lifted function is registered with the PPU dispatcher and the
 * module's exports are published through prx_loader's NID registry, which the
 * HLE import path consults before any HLE stub. sys_prx_start_module() then
 * runs the module's own module_start.
 *
 * Builds without game modules never call rr_game_prx_add(), so nothing here
 * changes their behaviour.
 */
#include "prx_loader.h"
#include "../memory/vm.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <unistd.h>
#endif

#define RR_GAME_PRX_MAX 16
#define RR_GAME_PRX_ID_BASE 0x23800000u   /* distinct from the HLE firmware ids */

typedef struct {
    const char*           file;       /* lower-case file name, e.g. "pathengine.sprx" */
    const char*           name;       /* module name, e.g. "PathEngine" */
    uint32_t              base;
    uint32_t              image_size;
    const prx_func_entry* funcs;
    uint64_t              func_count;
    const prx_export*     exports;    /* base-0 vaddrs; !syslib entries excluded */
    uint32_t              export_count;
    uint32_t              start_opd;  /* absolute guest OPD of module_start, or 0 */
    uint32_t              stop_opd;
    int                   loaded;
    int                   started;
} rr_game_prx;

static rr_game_prx s_mods[RR_GAME_PRX_MAX];
static int         s_count;

void rr_game_prx_add(const char* file, const char* name, uint32_t base, uint32_t image_size,
                     const prx_func_entry* funcs, uint64_t func_count,
                     const prx_export* exports, uint32_t export_count,
                     uint32_t start_opd, uint32_t stop_opd)
{
    if (s_count >= RR_GAME_PRX_MAX) return;
    rr_game_prx* m = &s_mods[s_count++];
    m->file = file; m->name = name; m->base = base; m->image_size = image_size;
    m->funcs = funcs; m->func_count = func_count;
    m->exports = exports; m->export_count = export_count;
    m->start_opd = start_opd; m->stop_opd = stop_opd;
}

static int same_file(const char* path, const char* file)
{
    const char* slash = strrchr(path, '/');
    const char* base = slash ? slash + 1 : path;
    size_t n = strlen(file);
    if (strlen(base) != n) {
        /* a .sprx request may name the .prx and the other way round */
        const char* dot = strrchr(base, '.');
        const char* fdot = strrchr(file, '.');
        if (!dot || !fdot || (size_t)(dot - base) != (size_t)(fdot - file)) return 0;
        n = (size_t)(fdot - file);
    }
    for (size_t i = 0; i < n; i++) {
        char a = base[i], b = file[i];
        if (a >= 'A' && a <= 'Z') a = (char)(a + 32);
        if (b >= 'A' && b <= 'Z') b = (char)(b + 32);
        if (a != b) return 0;
    }
    return 1;
}

static void exe_dir(char* out, size_t size)
{
    out[0] = 0;
#ifdef _WIN32
    DWORD n = GetModuleFileNameA(NULL, out, (DWORD)size);
    if (!n || n >= size) { out[0] = 0; return; }
#else
    ssize_t n = readlink("/proc/self/exe", out, size - 1);
    if (n <= 0) { out[0] = 0; return; }
    out[n] = 0;
#endif
    char* cut = strrchr(out, '/');
    char* cut2 = strrchr(out, '\\');
    if (cut2 > cut) cut = cut2;
    if (cut) cut[1] = 0; else out[0] = 0;
}

/* Set by the generated registration file (compiled into the game, which links
 * the PPU scaffold), so this runtime file has no link-time dependency on it. */
void     (*rr_game_prx_registrar)(uint64_t addr, void (*fn)(void*)) = 0;
uint64_t (*rr_game_prx_caller)(uint32_t opd, uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3,
                               uint64_t a4, uint64_t a5, uint64_t a6, uint64_t a7) = 0;
static void reg_adapter(uint32_t addr, void (*fn)(void*))
{
    if (rr_game_prx_registrar) rr_game_prx_registrar(addr, fn);
}

/* Returns a module id (> 0) when `path` names a recompiled game module,
 * a negative CELL error when it does but loading failed, 0 otherwise. */
int32_t rr_game_prx_load(const char* path)
{
    if (!path) return 0;
    for (int i = 0; i < s_count; i++) {
        rr_game_prx* m = &s_mods[i];
        if (!same_file(path, m->file)) continue;
        int32_t id = (int32_t)(RR_GAME_PRX_ID_BASE + (uint32_t)i);
        if (m->loaded) return id;   /* loading twice hands back the same module */
        char dir[1024], img_path[1200];
        exe_dir(dir, sizeof dir);
        snprintf(img_path, sizeof img_path, "%sprx/%s_image.bin", dir, m->name);
        uint32_t size = 0;
        uint8_t* image = prx_image_load_file(img_path, &size);
        if (!image || size != m->image_size) {
            fprintf(stderr, "[game-prx] %s: image %s missing or wrong size (%u, expected %u)\n",
                    m->name, img_path, size, m->image_size);
            free(image);
            return (int32_t)0x80011001;   /* CELL_PRX_ERROR_ERROR */
        }
        if (vm_commit(m->base, size) != CELL_OK) {
            fprintf(stderr, "[game-prx] %s: cannot commit guest memory at 0x%08X\n", m->name, m->base);
            free(image);
            return (int32_t)0x80010004;   /* CELL_ENOMEM */
        }
        prx_module pm;
        memset(&pm, 0, sizeof pm);
        pm.name = m->name; pm.base = m->base; pm.image = image; pm.image_size = size;
        pm.funcs = m->funcs; pm.func_count = m->func_count;
        pm.exports = m->exports; pm.export_count = m->export_count;
        prx_load_result r = prx_load_module(&pm, reg_adapter);
        free(image);
        if (!r.ok) return (int32_t)0x80011001;
        m->loaded = 1;
        printf("[game-prx] loaded %s at 0x%08X (%u functions, %u exports) id=0x%08X\n",
               m->name, m->base, r.funcs_registered, m->export_count, (uint32_t)id);
        return id;
    }
    return 0;
}

int rr_game_prx_is_ours(uint32_t id)
{
    return id >= RR_GAME_PRX_ID_BASE && id < RR_GAME_PRX_ID_BASE + (uint32_t)s_count;
}

/* Runs module_start(args, argp); returns its result through *result_out. */
int32_t rr_game_prx_start(uint32_t id, uint32_t args, uint32_t argp, int32_t* result_out)
{
    if (!rr_game_prx_is_ours(id)) return (int32_t)0x80010005;   /* CELL_ESRCH */
    rr_game_prx* m = &s_mods[id - RR_GAME_PRX_ID_BASE];
    if (!m->loaded) return (int32_t)0x80010005;
    if (m->started) return (int32_t)0x80011001;
    m->started = 1;
    int32_t res = 0;
    if (m->start_opd && rr_game_prx_caller) {
        printf("[game-prx] %s: module_start (opd 0x%08X)\n", m->name, m->start_opd);
        res = (int32_t)rr_game_prx_caller(m->start_opd, args, argp, 0, 0, 0, 0, 0, 0);
    }
    if (result_out) *result_out = res;
    return 0;
}

/* For libs/system/rrSysLibcBridge.c (scratch memory for localeconv & co.). */
int32_t rr_vm_commit_bridge(uint32_t addr, uint32_t size)
{
    return vm_commit(addr, size) == CELL_OK ? 0 : -1;
}
