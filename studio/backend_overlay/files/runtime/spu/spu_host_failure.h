/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Host execution failures are outside the architectural 14-bit STOP space.
 * They must never be mistaken for a guest STOP/service request. */
#ifndef SPU_HOST_FAILURE_H
#define SPU_HOST_FAILURE_H
#include <stdint.h>
#define SPU_HOST_FAILURE_UNSUPPORTED UINT32_C(0xFFFFFFFF)
#define SPU_HOST_FAILURE_BUDGET UINT32_C(0xFFFFFFFE)
#define SPU_HOST_FAILURE_WATCHDOG UINT32_C(0xFFFFFFFD)
static inline int32_t spu_host_failure_rc(uint32_t code) {
    return code == SPU_HOST_FAILURE_UNSUPPORTED ? -1 :
           code == SPU_HOST_FAILURE_BUDGET ? -2 :
           code == SPU_HOST_FAILURE_WATCHDOG ? -3 : 0;
}
#endif
