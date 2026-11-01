/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * cellFont ABI bridge.
 *
 * Several cellFont entry points take the font library as their first
 * argument and return the font/renderer through a later pointer
 * (cellFontOpenFontFile(library, path, subNum, uniqueId, font)), and the
 * scale/render calls take floats in f1/f2. The plain handlers in cellFont.c
 * use a shorter signature, so these context handlers read every argument
 * from the right register, validate guest ranges, and forward. Registered
 * by ppu_hle_init() before the generated table, so they take precedence.
 */
#include "cellFont.h"
#include "../../runtime/ppu/ppu_context.h"
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

extern uint8_t* vm_base;
extern uint32_t ppu_vm_size;
uint32_t vm_read32(uint64_t addr);
void vm_write32(uint64_t addr, uint32_t value);
void ps3_hle_register_ctx(uint32_t nid, const char* name, void (*fn)(ppu_context*));
int64_t sys_memory_allocate(ppu_context* ctx);
int64_t sys_memory_free(ppu_context* ctx);
s32 rrFontSetupRenderScalePoint(CellFont* font, float w, float h);
void rrFontSetTransferBuffer(u32 ea, u32 size);

#define FONT_ERR_PARAM   0x80540002
#define FONT_ERR_ALLOC   0x80540007
#define MAX_LIBRARIES    8

static uint32_t s_libraries[MAX_LIBRARIES];

/* ppu_vm_size == 0 means the whole 4 GB guest space is mapped (Windows): the
 * runtime's own convention ("0 = unchecked"). Treating 0 as "nothing valid"
 * made every bridged call fail with INVALID_PARAMETER on Windows, so no font
 * library, font or renderer ever existed and every glyph came back 0x0. */
static uint64_t vm_limit(void) { return ppu_vm_size ? (uint64_t)ppu_vm_size : 0x100000000ull; }
static int in_guest(uint64_t addr, uint64_t size)
{
    return addr >= 0x10000 && size <= vm_limit() && addr <= vm_limit() - size;
}
/* Rate-limited trace: the first 40 calls of each handler, with arguments and result. */
static int trace_ok(int* n) { return (*n)++ < 40; }
#define TRACE(name, fmt, ...) do { static int n_; if (trace_ok(&n_)) \
    fprintf(stderr, "[cellFont] " name "(" fmt ") -> 0x%08X\n", __VA_ARGS__); } while (0)
static void ret(ppu_context* c, int32_t value) { c->gpr[3] = (uint64_t)(int64_t)value; }
static CellFont* arg_font(ppu_context* c) { return (CellFont*)(uintptr_t)(uint32_t)c->gpr[3]; }
static int known_library(uint32_t ea)
{
    for (int i = 0; i < MAX_LIBRARIES; i++) if (ea && s_libraries[i] == ea) return 1;
    return 0;
}

/* cellFontInitLibraryFreeType[WithRevision](revisionFlags, config, CellFontLibrary** lib) */
static void init_library_impl(ppu_context* c, uint32_t config, uint32_t out)
{
    if (!in_guest(out, 4)) { ret(c, FONT_ERR_PARAM); return; }
    vm_write32(out, 0);
    if (!in_guest(config, 4)) { ret(c, FONT_ERR_PARAM); return; }
    int slot = -1;
    for (int i = 0; i < MAX_LIBRARIES; i++) if (!s_libraries[i]) { slot = i; break; }
    if (slot < 0) { ret(c, FONT_ERR_ALLOC); return; }
    ppu_context tmp = *c;
    tmp.gpr[3] = 0x10000; tmp.gpr[4] = 0x200; tmp.gpr[5] = out;   /* 64 KB page */
    if (sys_memory_allocate(&tmp)) { ret(c, FONT_ERR_ALLOC); return; }
    uint32_t ea = vm_read32(out);
    if (!in_guest(ea, 12)) { vm_write32(out, 0); ret(c, FONT_ERR_ALLOC); return; }
    s_libraries[slot] = ea;
    vm_write32(ea, 1); vm_write32(ea + 4, 1); vm_write32(ea + 8, 0);   /* type, version, SystemClosed */
    /* The rest of the library's 64 KB page is its glyph transfer buffer. */
    rrFontSetTransferBuffer(ea + 0x100, 0x10000 - 0x100);
    ret(c, 0);
}
static void init_library_rev(ppu_context* c) { init_library_impl(c, (uint32_t)c->gpr[4], (uint32_t)c->gpr[5]); }
static void init_library(ppu_context* c) { init_library_impl(c, (uint32_t)c->gpr[3], (uint32_t)c->gpr[4]); }

static void end_library(ppu_context* c)
{
    uint32_t ea = (uint32_t)c->gpr[3];
    for (int i = 0; i < MAX_LIBRARIES; i++) {
        if (ea && s_libraries[i] == ea) {
            ppu_context tmp = *c; tmp.gpr[3] = ea;
            int64_t rc = sys_memory_free(&tmp);
            if (!rc) {
                s_libraries[i] = 0;
                /* hand the transfer buffer to another live library, or drop it */
                uint32_t other = 0;
                for (int j = 0; j < MAX_LIBRARIES; j++) if (s_libraries[j]) { other = s_libraries[j]; break; }
                rrFontSetTransferBuffer(other ? other + 0x100 : 0, other ? 0x10000 - 0x100 : 0);
            }
            ret(c, (int32_t)rc); return;
        }
    }
    ret(c, FONT_ERR_PARAM);
}

/* cellFontOpenFontFile(library, fontPath, subNum, uniqueId, CellFont* font) */
static void open_font_file(ppu_context* c)
{
    uint32_t path = (uint32_t)c->gpr[4], out = (uint32_t)c->gpr[7];
    if (!known_library((uint32_t)c->gpr[3]) || !in_guest(out, 16) || !in_guest(path, 1)) { ret(c, FONT_ERR_PARAM); return; }
    uint32_t n = 0;
    while (n < 1024 && in_guest((uint64_t)path + n, 1) && vm_base[path + n]) ++n;
    if (n == 1024 || !in_guest((uint64_t)path + n, 1)) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, cellFontOpenFontFile((CellFont*)(uintptr_t)out, (const char*)(uintptr_t)path,
                                (u32)c->gpr[5], (s32)c->gpr[6]));
}

/* cellFontCreateRenderer(library, config, CellFontRenderer* renderer) */
static void create_renderer(ppu_context* c)
{
    if (!known_library((uint32_t)c->gpr[3]) || !in_guest(c->gpr[4], 20) || !in_guest(c->gpr[5], 256)) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, cellFontCreateRenderer((u32)c->gpr[3], (CellFontRenderer*)(uintptr_t)(uint32_t)c->gpr[5]));
}
/* cellFontBindRenderer(font, CellFontRenderer* renderer): the renderer is a pointer */
static void bind_renderer(ppu_context* c)
{
    if (!in_guest(c->gpr[3], 16) || !in_guest(c->gpr[4], 256)) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, cellFontBindRenderer(arg_font(c), vm_read32(c->gpr[4])));
}
static void destroy_renderer(ppu_context* c)
{
    if (!in_guest(c->gpr[3], 256)) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, cellFontDestroyRenderer(vm_read32(c->gpr[3])));
}

static int valid_scale(double w, double h)
{
    return isfinite(w) && isfinite(h) && w > 0 && h > 0 && w <= 4096 && h <= 4096;
}
static void scale_pixel(ppu_context* c)
{
    if (!in_guest(c->gpr[3], 16) || !valid_scale(c->fpr[1], c->fpr[2])) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, cellFontSetScalePixel(arg_font(c), (float)c->fpr[1], (float)c->fpr[2]));
}
static void scale_point(ppu_context* c)
{
    if (!in_guest(c->gpr[3], 16) || !valid_scale(c->fpr[1], c->fpr[2])) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, rrFontSetupRenderScalePoint(arg_font(c), (float)c->fpr[1], (float)c->fpr[2]));
}
/* cellFontRenderCharGlyphImage(font, code, surface, f1 x, f2 y, metrics r8, transInfo r9) */
static void render_glyph(ppu_context* c)
{
    if (!in_guest(c->gpr[3], 16) || !in_guest(c->gpr[5], 36) ||
        (c->gpr[8] && !in_guest(c->gpr[8], 32)) || (c->gpr[9] && !in_guest(c->gpr[9], 24)) ||
        !isfinite(c->fpr[1]) || !isfinite(c->fpr[2]) ||
        fabs(c->fpr[1]) > 1048576 || fabs(c->fpr[2]) > 1048576) { ret(c, FONT_ERR_PARAM); return; }
    ret(c, cellFontRenderCharGlyphImage(arg_font(c), (u32)c->gpr[4],
            (CellFontRenderSurface*)(uintptr_t)(uint32_t)c->gpr[5], (float)c->fpr[1], (float)c->fpr[2],
            (CellFontGlyphMetrics*)(uintptr_t)(uint32_t)c->gpr[8], (CellFontGlyphImage*)(uintptr_t)(uint32_t)c->gpr[9]));
}

/* Traced wrappers: log the guest arguments and the result of each bridged call. */
#define TRACED(fn, label) static void t_##fn(ppu_context* c) { \
    uint64_t a3 = c->gpr[3], a4 = c->gpr[4], a5 = c->gpr[5], a6 = c->gpr[6], a7 = c->gpr[7]; \
    double f1 = c->fpr[1], f2 = c->fpr[2]; fn(c); \
    TRACE(label, "r3=%08X r4=%08X r5=%08X r6=%08X r7=%08X f1=%g f2=%g", (uint32_t)a3, (uint32_t)a4, \
          (uint32_t)a5, (uint32_t)a6, (uint32_t)a7, f1, f2, (uint32_t)c->gpr[3]); }
TRACED(init_library_rev, "InitLibraryFreeTypeWithRevision")
TRACED(init_library, "InitLibraryFreeType")
TRACED(end_library, "EndLibrary")
TRACED(open_font_file, "OpenFontFile")
TRACED(create_renderer, "CreateRenderer")
TRACED(bind_renderer, "BindRenderer")
TRACED(destroy_renderer, "DestroyRenderer")
TRACED(scale_pixel, "SetScalePixel")
TRACED(scale_point, "SetupRenderScalePoint")
static void t_render_glyph(ppu_context* c)
{
    uint32_t font = (uint32_t)c->gpr[3], code = (uint32_t)c->gpr[4], ti = (uint32_t)c->gpr[9], surf = (uint32_t)c->gpr[5];
    double x = c->fpr[1], y = c->fpr[2];
    render_glyph(c);
    static int n_;
    if (trace_ok(&n_)) {
        uint32_t w = ti && in_guest(ti, 24) ? vm_read32(ti + 8) : 0, h = ti && in_guest(ti, 24) ? vm_read32(ti + 12) : 0;
        uint32_t img = ti && in_guest(ti, 24) ? vm_read32(ti) : 0;
        fprintf(stderr, "[cellFont] RenderCharGlyphImage(font=%08X code=U+%04X surf=%08X x=%g y=%g) -> 0x%08X image=%08X %ux%u\n",
                font, code, surf, x, y, (uint32_t)c->gpr[3], img, w, h);
    }
}

void rr_font_bridge_register(void)
{
    ps3_hle_register_ctx(0x7A0A83C4, "cellFontInitLibraryFreeTypeWithRevision", t_init_library_rev);
    ps3_hle_register_ctx(0x40D40544, "cellFontEndLibrary", t_end_library);
    ps3_hle_register_ctx(0x0A7306A4, "cellFontOpenFontFile", t_open_font_file);
    ps3_hle_register_ctx(0x042E74E3, "cellFontCreateRenderer", t_create_renderer);
    ps3_hle_register_ctx(0x66A23100, "cellFontBindRenderer", t_bind_renderer);
    ps3_hle_register_ctx(0x21EBB248, "cellFontDestroyRenderer", t_destroy_renderer);
    ps3_hle_register_ctx(0x297F0E93, "cellFontSetScalePixel", t_scale_pixel);
    ps3_hle_register_ctx(0x4D19C631, "cellFontSetupRenderScalePoint", t_scale_point);
    ps3_hle_register_ctx(0x88BE4799, "cellFontRenderCharGlyphImage", t_render_glyph);
    ps3_hle_register_ctx(0xF9677A46, "cellFontInitLibraryFreeType", t_init_library);
}
