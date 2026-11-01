/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* See rr_spu_snapshot.h. Written for RetroRemakes; no third-party code. */
#include "rr_spu_snapshot.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#include <direct.h>
#define RR_MKDIR(p) _mkdir(p)
#else
#include <sys/stat.h>
#include <unistd.h>
#define RR_MKDIR(p) mkdir(p, 0755)
#endif

#define LS_SIZE 0x40000u
#define MAX_SNAPS 48
#define MAX_ENTRIES 64
#define MAX_REGS 8192

uint64_t rr_spu_snap_hash(const uint8_t* p, uint32_t n)
{
    uint64_t h = 1469598103934665603ull;               /* FNV-1a 64 */
    for (uint32_t i = 0; i < n; i++) { h ^= p[i]; h *= 1099511628211ull; }
    return h;
}

uint32_t rr_spu_snap_window_base(uint32_t lsa)
{
    uint32_t base = (lsa & ~0x3FFFu);                   /* 16 KB aligned, 32 KB window */
    if (base + RR_SNAP_WINDOW > LS_SIZE) base = LS_SIZE - RR_SNAP_WINDOW;
    return base;
}

static int covered_by_lift(const spu_context* ctx, uint32_t lsa);

/* ---------------------------------------------------------------- capture */
typedef struct { int image; uint32_t base; uint64_t hash; uint32_t n; uint32_t entry[MAX_ENTRIES]; } snap_t;
static snap_t s_snaps[MAX_SNAPS];
static int s_nsnaps, s_enabled = -1;
static char s_dir[1024];
#ifdef _WIN32
static CRITICAL_SECTION s_lock; static volatile LONG s_lock_init;
static void lock(void){ if (InterlockedCompareExchange(&s_lock_init,1,0)==0){InitializeCriticalSection(&s_lock);s_lock_init=2;} while(s_lock_init!=2)Sleep(0); EnterCriticalSection(&s_lock);}
static void unlock(void){ LeaveCriticalSection(&s_lock);}
#else
#include <pthread.h>
static pthread_mutex_t s_lock = PTHREAD_MUTEX_INITIALIZER;
static void lock(void){ pthread_mutex_lock(&s_lock);}
static void unlock(void){ pthread_mutex_unlock(&s_lock);}
#endif

static int snap_enabled(void)
{
    if (s_enabled >= 0) return s_enabled;
    const char* e = getenv("RR_SPU_SNAPSHOT");
    s_enabled = !(e && e[0] == '0');
    const char* d = getenv("RR_SPU_SNAPSHOT_DIR");
    if (d && *d) snprintf(s_dir, sizeof s_dir, "%s", d);
    else {
#ifdef _WIN32
        char exe[1024]; DWORD n = GetModuleFileNameA(NULL, exe, sizeof exe);
        if (n && n < sizeof exe) { char* s = strrchr(exe, '\\'); if (s) *s = 0; snprintf(s_dir, sizeof s_dir, "%s\\spu_snapshots", exe); }
#else
        char exe[1024]; ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
        if (n > 0) { exe[n] = 0; char* s = strrchr(exe, '/'); if (s) *s = 0; snprintf(s_dir, sizeof s_dir, "%s/spu_snapshots", exe); }
#endif
        if (!s_dir[0]) snprintf(s_dir, sizeof s_dir, "spu_snapshots");
    }
    return s_enabled;
}

static void write_json(const snap_t* s)
{
    char path[1100];
    snprintf(path, sizeof path, "%s/%d_%05X_%016llX.json", s_dir, s->image, s->base, (unsigned long long)s->hash);
    FILE* f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "{\"schema\":1,\"image_id\":%d,\"base\":%u,\"size\":%u,\"hash\":\"%016llX\",\"entries\":[",
            s->image, s->base, RR_SNAP_WINDOW, (unsigned long long)s->hash);
    for (uint32_t i = 0; i < s->n; i++) fprintf(f, "%s%u", i ? "," : "", s->entry[i]);
    fprintf(f, "]}\n");
    fclose(f);
}

void rr_spu_snapshot_capture(const spu_context* ctx, uint32_t lsa)
{
    if (!ctx || !ctx->ls || !snap_enabled() || !s_enabled) return;
    lsa &= (LS_SIZE - 1) & ~3u;
    uint32_t base = rr_spu_snap_window_base(lsa);
    const uint8_t* win = ctx->ls + base;
    /* cheap pre-check before the full hash: same image/window/entry seen? */
    lock();
    for (int i = 0; i < s_nsnaps; i++) {
        snap_t* s = &s_snaps[i];
        if (s->image == ctx->image_id && s->base == base) {
            for (uint32_t k = 0; k < s->n; k++) if (s->entry[k] == lsa) { unlock(); return; }
        }
    }
    unlock();
    if (covered_by_lift(ctx, lsa)) return;              /* same code, only data differs */
    uint64_t h = rr_spu_snap_hash(win, RR_SNAP_WINDOW);
    lock();
    snap_t* s = NULL;
    for (int i = 0; i < s_nsnaps; i++)
        if (s_snaps[i].image == ctx->image_id && s_snaps[i].base == base && s_snaps[i].hash == h) s = &s_snaps[i];
    if (!s) {
        if (s_nsnaps >= MAX_SNAPS) { unlock(); return; }
        s = &s_snaps[s_nsnaps++];
        memset(s, 0, sizeof *s); s->image = ctx->image_id; s->base = base; s->hash = h;
        RR_MKDIR(s_dir);
        char path[1100];
        snprintf(path, sizeof path, "%s/%d_%05X_%016llX.bin", s_dir, s->image, base, (unsigned long long)h);
        FILE* f = fopen(path, "wb");
        if (f) { fwrite(win, 1, RR_SNAP_WINDOW, f); fclose(f); }
        fprintf(stderr, "[RR-SNAP] captured SPU code img=%d LS 0x%05X+0x%X hash %016llX (entry 0x%05X)\n",
                s->image, base, RR_SNAP_WINDOW, (unsigned long long)h, lsa);
    }
    int known = 0;
    for (uint32_t k = 0; k < s->n; k++) if (s->entry[k] == lsa) known = 1;
    if (!known && s->n < MAX_ENTRIES) { s->entry[s->n++] = lsa; write_json(s); }
    unlock();
}

/* ------------------------------------------------------- registry + lookup */
#define MAX_SETS 64
typedef struct { int image; uint32_t base; const uint32_t* ranges; uint32_t n; uint64_t code_hash; } set_t;
static set_t s_sets[MAX_SETS];
static int s_nsets;
/* set < 0: legacy whole-window entry (size/hash cover the window). */
typedef struct { int image, set; uint32_t base, size, lsa, end; uint64_t hash; rr_snap_fn fn; } reg_t;
static reg_t s_regs[MAX_REGS];
static int s_nregs;

void rr_spu_snapshot_register(int image_id, uint32_t base, uint32_t size, uint64_t hash,
                              uint32_t lsa, rr_snap_fn fn)
{
    if (s_nregs < MAX_REGS && fn) s_regs[s_nregs++] = (reg_t){image_id, -1, base, size, lsa, 0, hash, fn};
}

int rr_spu_snapshot_register_set(int image_id, uint32_t base, const uint32_t* ranges,
                                 uint32_t nranges, uint64_t code_hash)
{
    if (s_nsets >= MAX_SETS || !ranges || !nranges) return -1;
    for (uint32_t i = 0; i < nranges; i++)
        if (ranges[2*i] >= ranges[2*i+1] || ranges[2*i+1] > LS_SIZE) return -1;
    s_sets[s_nsets] = (set_t){image_id, base, ranges, nranges, code_hash};
    return s_nsets++;
}

void rr_spu_snapshot_register_fn(int set, uint32_t lsa, uint32_t end, uint64_t fn_hash, rr_snap_fn fn)
{
    if (set < 0 || set >= s_nsets || !fn || lsa >= end || end > LS_SIZE || s_nregs >= MAX_REGS) return;
    const set_t* st = &s_sets[set];
    s_regs[s_nregs++] = (reg_t){st->image, set, st->base, 0, lsa, end, fn_hash, fn};
}

static uint64_t set_hash(const uint8_t* ls, const set_t* st)
{
    uint64_t h = 1469598103934665603ull;
    for (uint32_t i = 0; i < st->n; i++)
        for (uint32_t a = st->ranges[2*i]; a < st->ranges[2*i+1]; a++) { h ^= ls[a]; h *= 1099511628211ull; }
    return h;
}

/* Guard cache, per thread. Every use hashes the target function's own bytes
 * (a change to the code being entered is seen at once). The whole set's code
 * (the sibling functions lifted code calls directly) is re-hashed when the
 * cache slot is new or every 512 uses. */
#define GUARD_SLOTS 64
typedef struct { const spu_context* ctx; const reg_t* r; int ok; unsigned uses; } guard_t;
static SPU_THREAD_LOCAL guard_t s_guard[GUARD_SLOTS];

static int guard_ok(const spu_context* ctx, const reg_t* r)
{
    unsigned slot = (unsigned)(((uintptr_t)ctx >> 6) ^ r->lsa ^ (uint32_t)r->hash) % GUARD_SLOTS;
    guard_t* g = &s_guard[slot];
    if (r->set < 0) {                                   /* legacy: whole window */
        if (g->ctx == ctx && g->r == r && ++g->uses < 512) return g->ok;
        int ok = rr_spu_snap_hash(ctx->ls + r->base, r->size) == r->hash;
        g->ctx = ctx; g->r = r; g->ok = ok; g->uses = 0;
        return ok;
    }
    if (rr_spu_snap_hash(ctx->ls + r->lsa, r->end - r->lsa) != r->hash) {
        g->ctx = ctx; g->r = r; g->ok = 0; g->uses = 0;
        return 0;
    }
    if (g->ctx == ctx && g->r == r && g->ok && ++g->uses < 512) return 1;
    const set_t* st = &s_sets[r->set];
    int ok = set_hash(ctx->ls, st) == st->code_hash;
    g->ctx = ctx; g->r = r; g->ok = ok; g->uses = 0;
    return ok;
}

/* 1 when lsa lies inside lifted code whose bytes still match: capturing the
 * window again would only record different data around the same code. */
static int covered_by_lift(const spu_context* ctx, uint32_t lsa)
{
    for (int i = 0; i < s_nsets; i++) {
        const set_t* st = &s_sets[i];
        if (st->image != ctx->image_id) continue;
        for (uint32_t k = 0; k < st->n; k++)
            if (lsa >= st->ranges[2*k] && lsa < st->ranges[2*k+1]) {
                if (set_hash(ctx->ls, st) == st->code_hash) return 1;
                break;                                  /* other code version: try the next set */
            }
    }
    return 0;
}

rr_snap_fn rr_spu_snapshot_lookup(const spu_context* ctx, uint32_t lsa)
{
    if (!ctx || !ctx->ls || !s_nregs) return NULL;
    lsa &= (LS_SIZE - 1);
    for (int i = 0; i < s_nregs; i++) {
        const reg_t* r = &s_regs[i];
        if (r->lsa != lsa || r->image != ctx->image_id) continue;
        if (r->set < 0 && r->base + r->size > LS_SIZE) continue;
        if (guard_ok(ctx, r)) return r->fn;
    }
    return NULL;
}
