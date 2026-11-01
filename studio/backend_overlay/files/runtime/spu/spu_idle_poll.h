/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Optional owner-verified idle DMA classification. NULL preserves watchdog. */
#ifndef SPU_IDLE_POLL_H
#define SPU_IDLE_POLL_H
#include "spu_context.h"
typedef int (*spu_idle_dma_fn)(const spu_context*,uint32_t,uint64_t,uint32_t,uint32_t,uint32_t,const uint8_t*);
#ifdef __cplusplus
extern "C" {
#endif
extern spu_idle_dma_fn g_spu_idle_dma;
#ifdef __cplusplus
}
#endif
#endif
