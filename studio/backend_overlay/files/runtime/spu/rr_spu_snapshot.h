/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Snapshot-based lifting of SPU code that a title builds in local store at
 * runtime (no file contains it, so it cannot be lifted ahead of time).
 *
 * 1. Capture: when the interpreter is entered at an unlifted LS address, the
 *    surrounding code window is saved once per (image, window, content hash)
 *    to <exe dir>/spu_snapshots/ with the entry points seen.
 * 2. The Studio lifts those snapshots on the next build and the generated
 *    code calls rr_spu_snapshot_register() for each lifted function.
 * 3. Lookup: a registered function is used only while the LS bytes of its
 *    code still hash to what was lifted (code-range guard). Data that shares
 *    the window is ignored; code the title changes falls back to the
 *    interpreter. Older builds registered with a whole-window hash, which is
 *    still accepted.
 * Written for RetroRemakes; no third-party code. */
#ifndef RR_SPU_SNAPSHOT_H
#define RR_SPU_SNAPSHOT_H
#include <stdint.h>
#include "spu_context.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef void (*rr_snap_fn)(spu_context*);
#define RR_SNAP_WINDOW 0x8000u            /* bytes captured around an entry */
uint64_t rr_spu_snap_hash(const uint8_t* p, uint32_t n);
uint32_t rr_spu_snap_window_base(uint32_t lsa);
void rr_spu_snapshot_capture(const spu_context* ctx, uint32_t lsa);
void rr_spu_snapshot_register(int image_id, uint32_t base, uint32_t size, uint64_t hash,
                              uint32_t lsa, rr_snap_fn fn);
/* Code-range registration: one set per lifted snapshot (all its function
 * ranges as [start,end) pairs and an FNV-64 over those bytes in order), then
 * each function with its own range hash. Returns the set id, or -1. */
int rr_spu_snapshot_register_set(int image_id, uint32_t base, const uint32_t* ranges,
                                 uint32_t nranges, uint64_t code_hash);
void rr_spu_snapshot_register_fn(int set, uint32_t lsa, uint32_t end, uint64_t fn_hash, rr_snap_fn fn);
rr_snap_fn rr_spu_snapshot_lookup(const spu_context* ctx, uint32_t lsa);
#ifdef __cplusplus
}
#endif
#endif
