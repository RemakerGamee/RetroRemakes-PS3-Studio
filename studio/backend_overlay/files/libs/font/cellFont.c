/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * ps3recomp - cellFont HLE implementation
 *
 * Font system with optional stb_truetype.h backend for actual TTF rendering.
 * Without stb_truetype, returns valid (but empty) glyph data so games
 * don't crash.
 */

#include "cellFont.h"
extern int cellfs_translate_path(const char*,char*,size_t);
extern uint32_t ppu_vm_size;
#include "../../runtime/ppu/ppu_memory.h"   /* GUEST_PTR, vm_read/vm_write: guest EA -> host */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdatomic.h>

/* Try to include stb_truetype */
#if !defined(PS3RECOMP_FONT_NO_STB) && __has_include("stb_truetype.h")
  #define FONT_HAS_STB 1
  #ifndef STB_TRUETYPE_IMPLEMENTATION
    #define STB_TRUETYPE_IMPLEMENTATION
  #endif
  #include "stb_truetype.h"
#elif !defined(PS3RECOMP_FONT_NO_STB) && defined(PS3RECOMP_HAS_STB_TRUETYPE)
  #define FONT_HAS_STB 1
  #include "stb_truetype.h"
#else
  #define FONT_HAS_STB 0
#endif

/* ---------------------------------------------------------------------------
 * Internal state
 * -----------------------------------------------------------------------*/

typedef struct {
    int     in_use;
    u32     type;
    float   scale_x;
    float   scale_y;
    float   effect_weight;
    float   effect_slant;
    u32     renderer;       /* bound renderer index, or 0xFFFFFFFF */
    /* Font data */
    u8*     font_data;      /* TTF file data (owned if loaded from file) */
    u32     font_data_size;
    int     font_data_owned;
#if FONT_HAS_STB
    stbtt_fontinfo stb_info;
    int     stb_valid;
#endif
} FontSlot;

typedef struct {
    int in_use;
} RendererSlot;

static int          s_font_initialized = 0;
static u32          s_open_mode = CELL_FONT_OPEN_MODE_DEFAULT;
static FontSlot     s_fonts[CELL_FONT_MAX_OPENED];
static RendererSlot s_renderers[CELL_FONT_MAX_RENDERERS];

/* ---------------------------------------------------------------------------
 * Helpers
 * -----------------------------------------------------------------------*/

static FontSlot* font_alloc_slot(void)
{
    for (int i = 0; i < CELL_FONT_MAX_OPENED; i++) {
        if (!s_fonts[i].in_use) {
            memset(&s_fonts[i], 0, sizeof(FontSlot));
            s_fonts[i].in_use = 1;
            s_fonts[i].scale_x = 1.0f;
            s_fonts[i].scale_y = 1.0f;
            s_fonts[i].renderer = 0xFFFFFFFF;
            return &s_fonts[i];
        }
    }
    return NULL;
}


/* ---------------------------------------------------------------------------
 * Guest struct marshalling
 *
 * The structs in cellFont.h are HOST layout and are not what the title has in
 * memory: CellFontRenderSurface and CellFontGlyphImage lead with an 8-byte host
 * pointer where the guest has 4, and every u32/float field is big-endian on the
 * guest side. So a pointer translation alone is not enough -- casting a guest EA
 * to CellFontGlyphMetrics* and storing floats through it writes little-endian
 * values at the wrong offsets, which is how a title reads a glyph advance of
 * 4.6e-41 and lays every string out on top of itself.
 *
 * The render/metrics bodies keep working on host structs; these move them in and
 * out. Offsets are the guest ABI, listed explicitly so they cannot drift with a
 * host compiler's padding.
 * -----------------------------------------------------------------------*/
enum {
    FONT_O_HANDLE = 0,  FONT_O_TYPE = 4,  FONT_O_SCALEX = 8, FONT_O_SCALEY = 12,
    SURF_O_BUFFER = 0,  SURF_O_WIDTHBYTE = 4, SURF_O_PIXELSIZE = 8,
    SURF_O_WIDTH  = 12, SURF_O_HEIGHT = 16,
    SURF_O_SC_X0 = 20, SURF_O_SC_Y0 = 24, SURF_O_SC_X1 = 28, SURF_O_SC_Y1 = 32,
    IMG_O_BUFFER  = 0,  IMG_O_WIDTHBYTE = 4,  IMG_O_PIXELSIZE = 8,
    IMG_O_WIDTH   = 12, IMG_O_HEIGHT = 16,
    LAY_O_BASELINEY = 0, LAY_O_LINEHEIGHT = 4, LAY_O_EFFECTHEIGHT = 8
};

#define FONT_EA(p) ((u32)(uintptr_t)(p))

/* CellFontGlyphMetrics is eight consecutive floats in guest order. */
static void font_metrics_store(CellFontGlyphMetrics* guest_ea,
                               const CellFontGlyphMetrics* h)
{
    u32 ea = FONT_EA(guest_ea);
    if (!ea) return;
    vm_write_f32(ea +  0, h->width);
    vm_write_f32(ea +  4, h->height);
    vm_write_f32(ea +  8, h->h_bearing_x);
    vm_write_f32(ea + 12, h->h_bearing_y);
    vm_write_f32(ea + 16, h->h_advance);
    vm_write_f32(ea + 20, h->v_bearing_x);
    vm_write_f32(ea + 24, h->v_bearing_y);
    vm_write_f32(ea + 28, h->v_advance);
}

/* Only explicit SetScissor calls enable scissor reads. Legacy users of the
 * original five-field host helper keep their previous full-surface behavior.
 * Membership is host-only bookkeeping; the public rectangle stays in the
 * actual 36-byte big-endian guest structure. No guest fields are repurposed. */
typedef struct FontClippedSurface { u32 ea; struct FontClippedSurface* next; } FontClippedSurface;
static FontClippedSurface* s_clipped_surfaces;
static atomic_flag s_surface_lock = ATOMIC_FLAG_INIT;
static atomic_int s_surface_tracking_failed;
static void font_surface_lock(void) { while (atomic_flag_test_and_set_explicit(&s_surface_lock, memory_order_acquire)) {} }
static void font_surface_unlock(void) { atomic_flag_clear_explicit(&s_surface_lock, memory_order_release); }
static int font_surface_clipped(u32 ea)
{
    int found = 0;
    font_surface_lock();
    for (FontClippedSurface* p = s_clipped_surfaces; p; p = p->next)
        if (p->ea == ea) { found = 1; break; }
    font_surface_unlock();
    return found;
}
static void font_surface_track(u32 ea, int enabled)
{
    font_surface_lock();
    FontClippedSurface** pp = &s_clipped_surfaces;
    while (*pp && (*pp)->ea != ea) pp = &(*pp)->next;
    if (!enabled && *pp) { FontClippedSurface* old = *pp; *pp = old->next; free(old); }
    else if (enabled && !*pp) {
        FontClippedSurface* p = (FontClippedSurface*)malloc(sizeof(*p));
        if (p) { p->ea = ea; p->next = NULL; *pp = p; }
        else atomic_store(&s_surface_tracking_failed, 1);
    }
    font_surface_unlock();
}
static void font_surface_clear_tracking(void)
{
    font_surface_lock();
    while (s_clipped_surfaces) { FontClippedSurface* old = s_clipped_surfaces; s_clipped_surfaces = old->next; free(old); }
    atomic_store(&s_surface_tracking_failed, 0);
    font_surface_unlock();
}

void cellFontRenderSurfaceInit(CellFontRenderSurface* surface, void* buffer,
                                s32 stride, s32 pixelSize, s32 width, s32 height)
{
    u32 ea = FONT_EA(surface);
    if (!ea) return;
    u32 w = width > 0 ? (u32)width : 0, h = height > 0 ? (u32)height : 0;
    vm_write32(ea + SURF_O_BUFFER, FONT_EA(buffer));
    vm_write32(ea + SURF_O_WIDTHBYTE, (u32)stride);
    vm_write32(ea + SURF_O_PIXELSIZE, (u32)pixelSize);
    vm_write32(ea + SURF_O_WIDTH, w); vm_write32(ea + SURF_O_HEIGHT, h);
    vm_write32(ea + SURF_O_SC_X0, 0); vm_write32(ea + SURF_O_SC_Y0, 0);
    vm_write32(ea + SURF_O_SC_X1, w); vm_write32(ea + SURF_O_SC_Y1, h);
    font_surface_track(ea, 0); /* reinitializing the same stack address resets prior scissor */
    { static int n_; if (n_++ < 40) fprintf(stderr, "[cellFont] RenderSurfaceInit(surf=%08X buf=%08X widthByte=%d pixel=%d %dx%d)\n", ea, FONT_EA(buffer), stride, pixelSize, width, height); }
}

static u32 font_surface_bound(int64_t value, u32 extent)
{
    return value < 0 ? 0 : value > (int64_t)extent ? extent : (u32)value;
}
void cellFontRenderSurfaceSetScissor(CellFontRenderSurface* surface, s32 x, s32 y,
                                     u32 width, u32 height)
{
    u32 ea = FONT_EA(surface);
    if (!ea) return;
    u32 w = vm_read32(ea + SURF_O_WIDTH), h = vm_read32(ea + SURF_O_HEIGHT);
    u32 x0 = font_surface_bound(x, w), y0 = font_surface_bound(y, h);
    /* Promote before addition: negative origins and UINT32_MAX extents must
     * intersect the surface, not wrap in 32-bit arithmetic. */
    u32 x1 = font_surface_bound((int64_t)x + width, w);
    u32 y1 = font_surface_bound((int64_t)y + height, h);
    if (x1 < x0) x1 = x0;
    if (y1 < y0) y1 = y0;
    vm_write32(ea + SURF_O_SC_X0, x0); vm_write32(ea + SURF_O_SC_Y0, y0);
    vm_write32(ea + SURF_O_SC_X1, x1); vm_write32(ea + SURF_O_SC_Y1, y1);
    font_surface_track(ea, 1);
    { static int n_; if (n_++ < 40) fprintf(stderr, "[cellFont] RenderSurfaceSetScissor(surf=%08X %d,%d %ux%u) -> %u,%u..%u,%u\n", ea, x, y, width, height, x0, y0, x1, y1); }
}

static void font_surface_load(CellFontRenderSurface* h,
                              const CellFontRenderSurface* guest_ea)
{
    u32 ea = FONT_EA(guest_ea);
    memset(h, 0, sizeof(*h));
    if (!ea) return;
    u32 buf = vm_read32(ea + SURF_O_BUFFER);
    h->buffer        = buf ? (u8*)(vm_base + buf) : NULL;
    h->widthByte     = (s32)vm_read32(ea + SURF_O_WIDTHBYTE);
    h->pixelSizeByte = (s32)vm_read32(ea + SURF_O_PIXELSIZE);
    h->width         = (s32)vm_read32(ea + SURF_O_WIDTH);
    h->height        = (s32)vm_read32(ea + SURF_O_HEIGHT);
    h->sc_x0 = h->sc_y0 = 0;
    h->sc_x1 = h->width > 0 ? (u32)h->width : 0;
    h->sc_y1 = h->height > 0 ? (u32)h->height : 0;
    if (font_surface_clipped(ea)) {
        h->sc_x0 = vm_read32(ea + SURF_O_SC_X0); h->sc_y0 = vm_read32(ea + SURF_O_SC_Y0);
        h->sc_x1 = vm_read32(ea + SURF_O_SC_X1); h->sc_y1 = vm_read32(ea + SURF_O_SC_Y1);
    }
}

/* buffer is left as the title set it -- we render into the surface, never
 * hand back a buffer of our own. */
static void font_image_store(CellFontGlyphImage* guest_ea,
                             u32 pixels, u32 stride, u32 width, u32 height,
                             u32 surface, u32 surface_stride)
{
    u32 ea=FONT_EA(guest_ea); if(!ea)return;
    vm_write32(ea,pixels);vm_write32(ea+4,stride);
    vm_write32(ea+8,width);vm_write32(ea+12,height);
    vm_write32(ea+16,surface);vm_write32(ea+20,surface_stride);
}

static void font_store(CellFont* guest_ea, u32 handle, u32 type,
                       float sx, float sy)
{
    u32 ea = FONT_EA(guest_ea);
    if (!ea) return;
    vm_write32(ea + FONT_O_HANDLE, handle);
    vm_write32(ea + FONT_O_TYPE,   type);
    vm_write_f32(ea + FONT_O_SCALEX, sx);
    vm_write_f32(ea + FONT_O_SCALEY, sy);
}

static FontSlot* font_get_slot(CellFont* font)
{
    /* font is a guest EA; the handle is the big-endian word at +0. */
    u32 ea = FONT_EA(font);
    if (!ea)
        return NULL;
    u32 handle = vm_read32(ea + FONT_O_HANDLE);
    if (handle >= CELL_FONT_MAX_OPENED)
        return NULL;
    FontSlot* slot = &s_fonts[handle];
    if (!slot->in_use)
        return NULL;
    return slot;
}

#if FONT_HAS_STB
static float font_get_stb_scale(FontSlot* slot)
{
    if (!slot->stb_valid) return 0.0f;
    return stbtt_ScaleForPixelHeight(&slot->stb_info, slot->scale_y);
}
#endif

/* ---------------------------------------------------------------------------
 * API implementations
 * -----------------------------------------------------------------------*/

s32 cellFontInit(const CellFontConfig* config)
{
    (void)config;
    printf("[cellFont] Init()\n");

    if (s_font_initialized)
        return CELL_FONT_ERROR_ALREADY_INITIALIZED;

    memset(s_fonts, 0, sizeof(s_fonts));
    memset(s_renderers, 0, sizeof(s_renderers));
    s_font_initialized = 1;
    return CELL_OK;
}

/* cellFontInitializeWithRevision(revisionFlags, config): the entry point titles built with a newer SDK use instead of
 * cellFontInit (NID 0xF03DCC29). Without it every later cellFont call returns CELL_FONT_ERROR_UNINITIALIZED, which
 * a title that ignores the result never notices. */
s32 cellFontInitializeWithRevision(u64 revisionFlags, const CellFontConfig* config)
{
    (void)revisionFlags;
    return cellFontInit(config);
}

s32 cellFontEnd(void)
{
    printf("[cellFont] End()\n");

    if (!s_font_initialized)
        return CELL_FONT_ERROR_UNINITIALIZED;

    for (int i = 0; i < CELL_FONT_MAX_OPENED; i++) {
        if (s_fonts[i].in_use && s_fonts[i].font_data_owned && s_fonts[i].font_data)
            free(s_fonts[i].font_data);
        s_fonts[i].in_use = 0;
    }

    font_surface_clear_tracking();
    s_font_initialized = 0;
    return CELL_OK;
}

s32 cellFontOpenFontFile(CellFont* font, const char* fontPath, u32 subNum, s32 uniqueId)
{
    (void)subNum;
    (void)uniqueId;
    fontPath = GUEST_PTR(fontPath, const char*);
    printf("[cellFont] OpenFontFile(%s)\n", fontPath ? fontPath : "(null)");

    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    if (!font || !fontPath)  return CELL_FONT_ERROR_INVALID_PARAMETER;

    FontSlot* slot = font_alloc_slot();
    if (!slot) return CELL_FONT_ERROR_ALLOCATION_FAILED;

    /* Load TTF file */
    char host_path[1024];
    if (cellfs_translate_path(fontPath, host_path, sizeof(host_path)) != 0) {
        slot->in_use = 0; return CELL_FONT_ERROR_OPEN_FAILED;
    }
    FILE* f = fopen(host_path, "rb");
    if (!f) {
        slot->in_use = 0;
        printf("[cellFont] Cannot open font file: %s\n", fontPath);
        return CELL_FONT_ERROR_OPEN_FAILED;
    }

    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); slot->in_use=0; return CELL_FONT_ERROR_OPEN_FAILED; }
    long size = ftell(f);
    if (size <= 0 || size > 32*1024*1024 || fseek(f, 0, SEEK_SET) != 0) { fclose(f);slot->in_use=0;return CELL_FONT_ERROR_OPEN_FAILED; }

    slot->font_data = (u8*)malloc((size_t)size);
    if (!slot->font_data) {
        fclose(f);
        slot->in_use = 0;
        return CELL_FONT_ERROR_ALLOCATION_FAILED;
    }

    slot->font_data_size = (u32)fread(slot->font_data, 1, (size_t)size, f);
    slot->font_data_owned = 1;
    fclose(f);
    if (slot->font_data_size != (u32)size) { free(slot->font_data); memset(slot,0,sizeof(*slot)); return CELL_FONT_ERROR_OPEN_FAILED; }

#if FONT_HAS_STB
    if (stbtt_InitFont(&slot->stb_info, slot->font_data,
                       stbtt_GetFontOffsetForIndex(slot->font_data, 0))) {
        slot->stb_valid = 1;
    } else {
        printf("[cellFont] stbtt_InitFont failed for: %s\n", fontPath);
        free(slot->font_data); memset(slot,0,sizeof(*slot)); return CELL_FONT_ERROR_OPEN_FAILED;
    }
#endif

    font_store(font, (u32)(slot - s_fonts), 0, 1.0f, 1.0f);

    return CELL_OK;
}

s32 cellFontOpenFontMemory(CellFont* font, const void* data, u32 dataSize, u32 subNum, s32 uniqueId)
{
    (void)subNum;
    (void)uniqueId;
    printf("[cellFont] OpenFontMemory(size=%u)\n", dataSize);

    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    if (!font || !data || dataSize == 0) return CELL_FONT_ERROR_INVALID_PARAMETER;

    FontSlot* slot = font_alloc_slot();
    if (!slot) return CELL_FONT_ERROR_ALLOCATION_FAILED;

    slot->font_data = GUEST_PTR(data, u8*); /* borrowed guest buffer */
    slot->font_data_size = dataSize;
    slot->font_data_owned = 0;

#if FONT_HAS_STB
    if (stbtt_InitFont(&slot->stb_info, slot->font_data,
                       stbtt_GetFontOffsetForIndex(slot->font_data, 0))) {
        slot->stb_valid = 1;
    } else {
        slot->stb_valid = 0;
    }
#endif

    font_store(font, (u32)(slot - s_fonts), 0, 1.0f, 1.0f);

    return CELL_OK;
}

s32 cellFontOpenFontset(CellFontLibrary lib, CellFontType* fontType, CellFont* font)
{
    (void)lib;
    u32 want_type = fontType ? vm_read32(FONT_EA(fontType)) : 0;
    printf("[cellFont] OpenFontset(type=0x%X)\n", want_type);

    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    if (!font) return CELL_FONT_ERROR_INVALID_PARAMETER;

    /* System fonts are not available in recomp. Allocate a slot anyway
     * so the game can proceed with empty glyphs. */
    FontSlot* slot = font_alloc_slot();
    if (!slot) return CELL_FONT_ERROR_ALLOCATION_FAILED;

    slot->type = want_type;

    font_store(font, (u32)(slot - s_fonts), slot->type, 1.0f, 1.0f);

    return CELL_OK;
}

s32 cellFontCloseFont(CellFont* font)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;

    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;

    if (slot->font_data_owned && slot->font_data)
        free(slot->font_data);

    slot->in_use = 0;
    return CELL_OK;
}

s32 cellFontSetFontOpenMode(u32 openMode)
{
    s_open_mode = openMode;
    return CELL_OK;
}

s32 cellFontCreateRenderer(CellFontLibrary lib, CellFontRenderer* renderer)
{
    (void)lib;
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    if (!renderer) return CELL_FONT_ERROR_INVALID_PARAMETER;

    for (u32 i = 0; i < CELL_FONT_MAX_RENDERERS; i++) {
        if (!s_renderers[i].in_use) {
            s_renderers[i].in_use = 1;
            vm_write32(FONT_EA(renderer), i);
            return CELL_OK;
        }
    }

    return CELL_FONT_ERROR_ALLOCATION_FAILED;
}

s32 cellFontDestroyRenderer(CellFontRenderer renderer)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    if (renderer >= CELL_FONT_MAX_RENDERERS) return CELL_FONT_ERROR_INVALID_PARAMETER;

    s_renderers[renderer].in_use = 0;
    return CELL_OK;
}

s32 cellFontBindRenderer(CellFont* font, CellFontRenderer renderer)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;

    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;
    if (renderer >= CELL_FONT_MAX_RENDERERS || !s_renderers[renderer].in_use)
        return CELL_FONT_ERROR_INVALID_PARAMETER;

    if (slot->renderer != 0xFFFFFFFF)
        return CELL_FONT_ERROR_RENDERER_ALREADY_BIND;

    slot->renderer = renderer;
    return CELL_OK;
}

s32 cellFontUnbindRenderer(CellFont* font)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;

    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;

    slot->renderer = 0xFFFFFFFF;
    return CELL_OK;
}

s32 cellFontSetScalePixel(CellFont* font, float w, float h)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;

    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;

    slot->scale_x = w;
    slot->scale_y = h;
    vm_write_f32(FONT_EA(font) + FONT_O_SCALEX, w);
    vm_write_f32(FONT_EA(font) + FONT_O_SCALEY, h);

    return CELL_OK;
}

s32 cellFontSetEffectWeight(CellFont* font, float weight)
{
    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;
    slot->effect_weight = weight;
    return CELL_OK;
}

s32 cellFontSetEffectSlant(CellFont* font, float slant)
{
    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;
    slot->effect_slant = slant;
    return CELL_OK;
}

s32 cellFontGetHorizontalLayout(CellFont* font, CellFontHorizontalLayout* layout)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    if (!layout) return CELL_FONT_ERROR_INVALID_PARAMETER;

    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;

#if FONT_HAS_STB
    if (slot->stb_valid) {
        float scale = font_get_stb_scale(slot);
        int ascent, descent, lineGap;
        stbtt_GetFontVMetrics(&slot->stb_info, &ascent, &descent, &lineGap);
        vm_write_f32(FONT_EA(layout) + LAY_O_BASELINEY, (float)ascent * scale);
        vm_write_f32(FONT_EA(layout) + LAY_O_LINEHEIGHT,
                     (float)(ascent - descent + lineGap) * scale);
        vm_write_f32(FONT_EA(layout) + LAY_O_EFFECTHEIGHT,
                     (float)(ascent - descent) * scale);
        return CELL_OK;
    }
#endif

    /* Fallback: reasonable defaults based on scale */
    vm_write_f32(FONT_EA(layout) + LAY_O_BASELINEY,    slot->scale_y * 0.8f);
    vm_write_f32(FONT_EA(layout) + LAY_O_LINEHEIGHT,   slot->scale_y * 1.2f);
    vm_write_f32(FONT_EA(layout) + LAY_O_EFFECTHEIGHT, slot->scale_y);
    return CELL_OK;
}

/* Fills a HOST metrics struct. cellFontGetRenderCharGlyphMetrics is the
 * marshalling wrapper; cellFontRenderCharGlyphImage wants the host copy
 * anyway and would otherwise write the guest struct then read it back. */
static s32 font_metrics_host(CellFont* font, u32 code,
                             CellFontGlyphMetrics* metrics)
{
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;

    FontSlot* slot = font_get_slot(font);
    if (!slot) return CELL_FONT_ERROR_INVALID_PARAMETER;

    memset(metrics, 0, sizeof(CellFontGlyphMetrics));

#if FONT_HAS_STB
    if (slot->stb_valid) {
        float scale = font_get_stb_scale(slot);
        int glyph = stbtt_FindGlyphIndex(&slot->stb_info, (int)code);
        if (glyph == 0) {
            /* Glyph not found -- return space-like metrics */
            metrics->h_advance = slot->scale_x * 0.5f;
            return CELL_OK;
        }

        int advanceWidth, leftSideBearing;
        stbtt_GetGlyphHMetrics(&slot->stb_info, glyph, &advanceWidth, &leftSideBearing);

        int x0, y0, x1, y1;
        stbtt_GetGlyphBitmapBox(&slot->stb_info, glyph, scale, scale, &x0, &y0, &x1, &y1);

        metrics->width      = (float)(x1 - x0);
        metrics->height     = (float)(y1 - y0);
        metrics->h_bearing_x = (float)leftSideBearing * scale;
        metrics->h_bearing_y = (float)(-y0);
        metrics->h_advance  = (float)advanceWidth * scale;
        metrics->v_advance  = metrics->height;

        return CELL_OK;
    }
#endif

    /* Fallback: monospace-like metrics */
    metrics->width       = slot->scale_x * 0.6f;
    metrics->height      = slot->scale_y * 0.8f;
    metrics->h_bearing_x = 0.0f;
    metrics->h_bearing_y = slot->scale_y * 0.7f;
    metrics->h_advance   = slot->scale_x * 0.6f;
    metrics->v_advance   = slot->scale_y;

    return CELL_OK;
}

/* Outline metrics use font scale, not the integer-rounded render bitmap box.
 * Horizontal queries have no vertical layout request; retain zero vertical
 * fields, as in the documented public CellFont horizontal-query shape. This
 * does not implement cellFontGetCharGlyphMetricsVertical or synthetic effects.
 * New queries refuse the legacy approximate-metrics fallback: a loaded stb
 * provider and a supported glyph are required before reporting success. */
static s32 font_query_slot(CellFont* font, FontSlot** slot)
{
    if (!font) return CELL_FONT_ERROR_INVALID_PARAMETER;
    if (!s_font_initialized) return CELL_FONT_ERROR_UNINITIALIZED;
    *slot = font_get_slot(font);
    if (!*slot) return CELL_FONT_ERROR_INVALID_PARAMETER;
    if (!isfinite((*slot)->scale_x) || !isfinite((*slot)->scale_y) ||
        (*slot)->scale_x <= 0 || (*slot)->scale_y <= 0)
        return CELL_FONT_ERROR_INVALID_PARAMETER;
    if ((*slot)->effect_weight != 0 || (*slot)->effect_slant != 0)
        return CELL_FONT_ERROR_NO_SUPPORT_FUNCTION;
#if FONT_HAS_STB
    if ((*slot)->stb_valid) return CELL_OK;
#endif
    return CELL_FONT_ERROR_NO_SUPPORT_FUNCTION;
}

static int font_query_unicode(u32 code)
{
    return code <= 0x10FFFFu && !(code >= 0xD800u && code <= 0xDFFFu);
}

s32 cellFontGetCharGlyphMetrics(CellFont* font, u32 code,
                                CellFontGlyphMetrics* metrics)
{
    if (!metrics) return CELL_FONT_ERROR_INVALID_PARAMETER;
    FontSlot* slot;
    s32 rc = font_query_slot(font, &slot);
    if (rc != (s32)CELL_OK) return rc;
    if (!font_query_unicode(code)) return CELL_FONT_ERROR_NO_SUPPORT_CODE;
#if FONT_HAS_STB
    int glyph = stbtt_FindGlyphIndex(&slot->stb_info, (int)code);
    if (!glyph) return CELL_FONT_ERROR_NO_SUPPORT_GLYPH;
    int advance, bearing, x0 = 0, y0 = 0, x1 = 0, y1 = 0;
    stbtt_GetGlyphHMetrics(&slot->stb_info, glyph, &advance, &bearing);
    stbtt_GetGlyphBox(&slot->stb_info, glyph, &x0, &y0, &x1, &y1);
    float sx = stbtt_ScaleForPixelHeight(&slot->stb_info, slot->scale_x);
    float sy = stbtt_ScaleForPixelHeight(&slot->stb_info, slot->scale_y);
    CellFontGlyphMetrics h = {0};
    h.width = (float)(x1 - x0) * sx;
    h.height = (float)(y1 - y0) * sy;
    h.h_bearing_x = (float)bearing * sx;
    h.h_bearing_y = (float)y1 * sy;
    h.h_advance = (float)advance * sx;
    font_metrics_store(metrics, &h);
    return CELL_OK;
#else
    return CELL_FONT_ERROR_NO_SUPPORT_FUNCTION;
#endif
}

s32 cellFontGetKerning(CellFont* font, u32 previousCode, u32 code,
                       CellFontKerning* kerning)
{
    if (!kerning) return CELL_FONT_ERROR_INVALID_PARAMETER;
    FontSlot* slot;
    s32 rc = font_query_slot(font, &slot);
    if (rc != (s32)CELL_OK) return rc;
    if (!font_query_unicode(previousCode) || !font_query_unicode(code))
        return CELL_FONT_ERROR_NO_SUPPORT_CODE;
#if FONT_HAS_STB
    float x = 0;
    if (previousCode && code) {
        int previousGlyph = stbtt_FindGlyphIndex(&slot->stb_info, (int)previousCode);
        int glyph = stbtt_FindGlyphIndex(&slot->stb_info, (int)code);
        if (!previousGlyph || !glyph) return CELL_FONT_ERROR_NO_SUPPORT_GLYPH;
        float scale = stbtt_ScaleForPixelHeight(&slot->stb_info, slot->scale_x);
        x = (float)stbtt_GetGlyphKernAdvance(&slot->stb_info, previousGlyph, glyph) * scale;
    }
    vm_write_f32(FONT_EA(kerning), x);
    vm_write_f32(FONT_EA(kerning) + 4, 0.0f); /* horizontal kern/GPOS pair adjustment */
    return CELL_OK;
#else
    return CELL_FONT_ERROR_NO_SUPPORT_FUNCTION;
#endif
}

s32 cellFontGetRenderCharGlyphMetrics(CellFont* font, u32 code,
                                       CellFontGlyphMetrics* metrics)
{
    if (!metrics) return CELL_FONT_ERROR_INVALID_PARAMETER;
    CellFontGlyphMetrics h;
    s32 rc = font_metrics_host(font, code, &h);
    if (rc == (s32)CELL_OK) font_metrics_store(metrics, &h);
    return rc;
}

s32 cellFontGetRenderCharGlyphMetricsVertical(CellFont* font, u32 code,
                                               CellFontGlyphMetrics* metrics)
{
    /* Vertical metrics: use horizontal metrics as base, swap axes */
    if (!metrics) return CELL_FONT_ERROR_INVALID_PARAMETER;
    CellFontGlyphMetrics h;
    s32 rc = font_metrics_host(font, code, &h);
    if (rc != (s32)CELL_OK) return rc;

    /* Swap horizontal/vertical bearings for vertical layout */
    float tmp;
    tmp = h.h_bearing_x; h.h_bearing_x = h.v_bearing_x; h.v_bearing_x = tmp;
    tmp = h.h_bearing_y; h.h_bearing_y = h.v_bearing_y; h.v_bearing_y = tmp;
    tmp = h.h_advance;   h.h_advance   = h.v_advance;   h.v_advance   = tmp;

    font_metrics_store(metrics, &h);
    return CELL_OK;
}

/* Library-owned glyph transfer buffer (guest memory). When a title passes a
 * render surface with a NULL buffer, the real libfont renders the glyph into
 * its own buffer and returns it through CellFontImageTransInfo (Image, width,
 * height, stride); the title then copies it into its own glyph atlas.
 * MotorStorm: Pacific Rift does this for its menu text. */
static u32 s_xfer_ea, s_xfer_size;
void rrFontSetTransferBuffer(u32 ea, u32 size) { s_xfer_ea = ea; s_xfer_size = size; }

s32 cellFontRenderCharGlyphImage(CellFont* font, u32 code,
    CellFontRenderSurface* surface,float x,float y,
    CellFontGlyphMetrics* metrics,CellFontGlyphImage* image)
{
    if(image)font_image_store(image,0,0,0,0,0,0);
    FontSlot* slot=font_get_slot(font);
    if(!s_font_initialized)return CELL_FONT_ERROR_UNINITIALIZED;
    if(!slot||!surface||!isfinite(x)||!isfinite(y))return CELL_FONT_ERROR_INVALID_PARAMETER;
    if(slot->renderer==0xffffffff || !s_renderers[slot->renderer].in_use)return (s32)0x80540021;
    CellFontGlyphMetrics hm; s32 rc=font_metrics_host(font,code,&hm);
    if(rc)return rc; if(metrics)font_metrics_store(metrics,&hm);
    CellFontRenderSurface hs;font_surface_load(&hs,surface);
    u32 surf_ea=vm_read32(FONT_EA(surface));
#if FONT_HAS_STB
    if(!surf_ea){
        /* NULL surface buffer: render into the library transfer buffer. */
        if(!slot->stb_valid)return CELL_FONT_ERROR_OPEN_FAILED;
        if(!s_xfer_ea||!s_xfer_size)return CELL_FONT_ERROR_INVALID_PARAMETER;
        int g=stbtt_FindGlyphIndex(&slot->stb_info,(int)code);
        if(!g)return CELL_FONT_ERROR_NO_SUPPORT_GLYPH;
        float tsy=font_get_stb_scale(slot);
        float tsx=tsy*(slot->scale_x/slot->scale_y);
        int tw=0,th=0,tx=0,ty=0;
        u8* tb=stbtt_GetGlyphBitmap(&slot->stb_info,tsx,tsy,g,&tw,&th,&tx,&ty);
        if(!tb||tw<=0||th<=0){if(tb)stbtt_FreeBitmap(tb,NULL);return CELL_OK;} /* blank glyph (space) */
        if((uint64_t)tw*(uint64_t)th>s_xfer_size){stbtt_FreeBitmap(tb,NULL);return CELL_FONT_ERROR_ALLOCATION_FAILED;}
        memcpy(vm_base+s_xfer_ea,tb,(size_t)tw*(size_t)th);
        stbtt_FreeBitmap(tb,NULL);
        if(image)font_image_store(image,s_xfer_ea,(u32)tw,(u32)tw,(u32)th,s_xfer_ea,(u32)tw);
        return CELL_OK;
    }
#endif
    if(hs.pixelSizeByte!=1)return (s32)0x80540040;
    if(!surf_ea || hs.width<=0 || hs.height<=0 || hs.widthByte<hs.width ||
       (uint64_t)surf_ea+(uint64_t)hs.widthByte*(u32)hs.height>(ppu_vm_size?(uint64_t)ppu_vm_size:0x100000000ull))return CELL_FONT_ERROR_INVALID_PARAMETER; /* 0 = whole 4 GB mapped (Windows) */
#if FONT_HAS_STB
    if(!slot->stb_valid)return CELL_FONT_ERROR_OPEN_FAILED;
    int glyph=stbtt_FindGlyphIndex(&slot->stb_info,(int)code);
    if(!glyph)return CELL_FONT_ERROR_NO_SUPPORT_GLYPH;
    float sy=font_get_stb_scale(slot);
    float sx=sy*(slot->scale_x/slot->scale_y);
    int bw=0,bh=0,bx=0,by=0;
    u8* bitmap=stbtt_GetGlyphBitmap(&slot->stb_info,sx,sy,glyph,&bw,&bh,&bx,&by);
    if(!bitmap){if(!bw||!bh)return CELL_OK;return CELL_FONT_ERROR_ALLOCATION_FAILED;}
    int dx=(int)x+bx,dy=(int)y+by;
    int x0=dx<0?0:dx,y0=dy<0?0:dy;
    int x1=dx+bw>hs.width?hs.width:dx+bw,y1=dy+bh>hs.height?hs.height:dy+bh;
    if(x0<(int)hs.sc_x0)x0=(int)hs.sc_x0;if(y0<(int)hs.sc_y0)y0=(int)hs.sc_y0;
    if(x1>(int)hs.sc_x1)x1=(int)hs.sc_x1;if(y1>(int)hs.sc_y1)y1=(int)hs.sc_y1;
    if(x1>x0 && y1>y0){
        for(int iy=y0;iy<y1;iy++)for(int ix=x0;ix<x1;ix++)
            hs.buffer[(size_t)iy*hs.widthByte+ix]=bitmap[(iy-dy)*bw+(ix-dx)];
        if(image)font_image_store(image,surf_ea+(u32)y0*(u32)hs.widthByte+(u32)x0,
            (u32)hs.widthByte,(u32)(x1-x0),(u32)(y1-y0),surf_ea,(u32)hs.widthByte);
    }
    stbtt_FreeBitmap(bitmap,NULL);return CELL_OK;
#else
    return CELL_FONT_ERROR_NO_SUPPORT_FUNCTION;
#endif
}

/* This private bridge uses default 72 DPI; the title never changes resolution.
 * SetScalePixel and SetupRenderScalePoint receive their real floating args. */
s32 rrFontSetupRenderScalePoint(CellFont* font,float w,float h)
{
    FontSlot* slot=font_get_slot(font);
    if(!slot)return CELL_FONT_ERROR_INVALID_PARAMETER;
    if(slot->renderer==0xffffffff)return (s32)0x80540021;
    return cellFontSetScalePixel(font,w,h);
}

s32 cellFontSetupRenderScalePixel(CellFont* font, float w, float h)
{
    return cellFontSetScalePixel(font, w, h);
}
