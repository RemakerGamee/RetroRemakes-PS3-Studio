/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * ps3recomp - cellUsbd HLE implementation
 *
 * Stub. Init/term work, no USB devices are enumerated.
 * Games that require specific USB peripherals will need
 * proper device emulation.
 */

#include "cellUsbd.h"
#include <stdio.h>
#include <string.h>
#include "../../runtime/ppu/ppu_memory.h"   /* GUEST_PTR, vm_read/vm_write: guest EA -> host */

/* Internal state */

static int s_initialized = 0;

#define MAX_LDD_OPS 8
static const CellUsbdLddOps* s_ldd_ops[MAX_LDD_OPS];
static int s_ldd_count = 0;

/* API */

s32 cellUsbdInit(void)
{
    printf("[cellUsbd] Init()\n");
    if (s_initialized)
        return (s32)CELL_USBD_ERROR_ALREADY_INITIALIZED;
    s_ldd_count = 0;
    memset(s_ldd_ops, 0, sizeof(s_ldd_ops));
    s_initialized = 1;
    return CELL_OK;
}

s32 cellUsbdEnd(void)
{
    printf("[cellUsbd] End()\n");
    s_initialized = 0;
    return CELL_OK;
}

s32 cellUsbdRegisterLdd(const CellUsbdLddOps* ops)
{
    /* CellUsbdLddOps leads with a char* -- a 4-byte EA to the guest. */
    u32 name_ea = ops ? vm_read32((u32)(uintptr_t)ops) : 0;
    printf("[cellUsbd] RegisterLdd(%s)\n",
           name_ea ? (const char*)(vm_base + name_ea) : (ops ? "?" : "null"));
    if (!s_initialized) return (s32)CELL_USBD_ERROR_NOT_INITIALIZED;
    if (!ops) return (s32)CELL_USBD_ERROR_INVALID_ARGUMENT;
    if (s_ldd_count >= MAX_LDD_OPS) return (s32)CELL_USBD_ERROR_INVALID_ARGUMENT;
    s_ldd_ops[s_ldd_count++] = ops;
    return CELL_OK;
}

s32 cellUsbdUnregisterLdd(const CellUsbdLddOps* ops)
{
    if (!s_initialized) return (s32)CELL_USBD_ERROR_NOT_INITIALIZED;
    for (int i = 0; i < s_ldd_count; i++) {
        if (s_ldd_ops[i] == ops) {
            for (int j = i; j < s_ldd_count - 1; j++)
                s_ldd_ops[j] = s_ldd_ops[j + 1];
            s_ldd_count--;
            return CELL_OK;
        }
    }
    return CELL_OK;
}

s32 cellUsbdGetDeviceList(CellUsbdDeviceInfo* list, u32 maxDevices,
                            u32* numDevices)
{
    (void)list; (void)maxDevices;
    if (!s_initialized) return (s32)CELL_USBD_ERROR_NOT_INITIALIZED;
    if (!numDevices) return (s32)CELL_USBD_ERROR_INVALID_ARGUMENT;
    vm_write32((u32)(uintptr_t)numDevices, 0); /* no devices */
    return CELL_OK;
}

s32 cellUsbdOpenPipe(CellUsbdDeviceId deviceId, u32 endpoint,
                       CellUsbdPipeId* pipeId)
{
    (void)deviceId; (void)endpoint; (void)pipeId;
    return (s32)CELL_USBD_ERROR_NO_DEVICE;
}

s32 cellUsbdClosePipe(CellUsbdPipeId pipeId)
{
    (void)pipeId;
    return (s32)CELL_USBD_ERROR_PIPE_NOT_FOUND;
}

s32 cellUsbdBulkTransfer(CellUsbdPipeId pipeId, void* data,
                           u32 length, u32* transferred)
{
    (void)pipeId; (void)data; (void)length; (void)transferred;
    return (s32)CELL_USBD_ERROR_PIPE_NOT_FOUND;
}

s32 cellUsbdControlTransfer(CellUsbdPipeId pipeId, u8 requestType,
                              u8 request, u16 value, u16 index,
                              void* data, u32 length, u32* transferred)
{
    (void)pipeId; (void)requestType; (void)request;
    (void)value; (void)index; (void)data; (void)length; (void)transferred;
    return (s32)CELL_USBD_ERROR_PIPE_NOT_FOUND;
}

/* cellUsbdRegisterExtraLdd2 -- register a logical device driver for a vendor
 * id and a product-id range (SDK signature; RPCS3 leaves it argument-less and
 * returns CELL_OK). The driver is recorded like cellUsbdRegisterLdd; no USB
 * devices are ever enumerated here, so its probe/attach callbacks never run,
 * which is what a title sees on a console with the peripheral unplugged. */
s32 cellUsbdRegisterExtraLdd2(const CellUsbdLddOps* lddops, u16 vendorId,
                              u16 productIdMin, u16 productIdMax)
{
    u32 name_ea = lddops ? vm_read32((u32)(uintptr_t)lddops) : 0;
    printf("[cellUsbd] RegisterExtraLdd2(%s, vid=0x%04x, pid=0x%04x-0x%04x)\n",
           name_ea ? (const char*)(vm_base + name_ea) : (lddops ? "?" : "null"),
           vendorId, productIdMin, productIdMax);
    if (!s_initialized) return (s32)CELL_USBD_ERROR_NOT_INITIALIZED;
    if (!lddops) return (s32)CELL_USBD_ERROR_INVALID_ARGUMENT;
    if (s_ldd_count >= MAX_LDD_OPS) return (s32)CELL_USBD_ERROR_INVALID_ARGUMENT;
    s_ldd_ops[s_ldd_count++] = lddops;
    return CELL_OK;
}
