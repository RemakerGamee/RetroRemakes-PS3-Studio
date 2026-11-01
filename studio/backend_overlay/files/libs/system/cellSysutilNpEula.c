/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * ps3recomp - cellSysutilNpEula HLE implementation
 *
 * EULA check: returns "already accepted" so games skip the EULA dialog.
 */

#include "cellSysutilNpEula.h"
#include <stdio.h>

s32 cellSysutilNpEulaCheck(void* cb, void* arg)
{
    (void)cb; (void)arg;
    printf("[cellSysutilNpEula] EulaCheck() — returning accepted\n");
    /* Return 1 = EULA already accepted */
    return 1;
}

/* ---------------------------------------------------------------------------
 * Derived from RPCS3 (https://github.com/RPCS3/rpcs3), GPL-2.0-only
 * (rpcs3/Emu/Cell/Modules/cellSysutilNpEula.cpp).
 *
 * sceNpEulaCheckEulaStatus reports the PSN terms as already accepted through
 * the title's callback on its next sysutil poll. Without it (inFAMOUS 2 imports
 * it) the call resolved to a bare CELL_OK and the callback the title waits on
 * never ran.
 * -------------------------------------------------------------------------*/
#include "cellSysutil.h"

#define SCE_NP_EULA_ALREADY_ACCEPTED            2
#define SCE_NP_EULA_ERROR_INVALID_ARGUMENT      0x8002e501u

s32 sceNpEulaCheckEulaStatus(u32 communicationId, u32 arg2, u64 arg3, u32 cbFunc, u32 cbFuncArg)
{
    (void)arg2; (void)arg3;
    printf("[sceNpEula] CheckEulaStatus(commId=0x%08X, cb=0x%08X) -> already accepted\n",
           communicationId, cbFunc);
    if (!communicationId || !cbFunc)
        return (s32)SCE_NP_EULA_ERROR_INVALID_ARGUMENT;
    /* Callback args as RPCS3 passes them: (status, errorCode, count, arg). */
    u64 args[8] = { SCE_NP_EULA_ALREADY_ACCEPTED, CELL_OK, 1, cbFuncArg, 0, 0, 0, 0 };
    return cellSysutilQueueGuestCallbackArgs(cbFunc, args);
}

s32 sceNpEulaShowCurrentEula(u32 communicationId, u64 arg2, u32 cbFunc, u32 cbFuncArg)
{
    (void)arg2; (void)cbFuncArg;
    printf("[sceNpEula] ShowCurrentEula(commId=0x%08X) -- no dialog\n", communicationId);
    if (!communicationId || !cbFunc)
        return (s32)SCE_NP_EULA_ERROR_INVALID_ARGUMENT;
    return CELL_OK;
}
/* ----------------------------- end RPCS3-derived ------------------------- */
