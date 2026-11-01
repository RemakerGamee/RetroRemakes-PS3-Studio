/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * ps3recomp - sceNp HLE implementation
 *
 * Provides fake PSN identity so games can proceed through NP checks.
 * The username defaults to "PS3Player" but is configurable.
 */

#include "sceNp.h"
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "../../runtime/ppu/ppu_memory.h"   /* vm_write*: guest EA -> host, byte-swapped */
#include "np_psnr.h"
#include "../system/cellSysutil.h"
#include "../filesystem/edat.h"


/* ---------------------------------------------------------------------------
 * Internal state
 * -----------------------------------------------------------------------*/

static int  s_np_initialized = 0;
static int  s_score_initialized = 0;
static char s_fake_username[SCE_NP_ONLINEID_MAX_LENGTH + 1] = "PS3Player";

/* ---------------------------------------------------------------------------
 * Configuration
 * -----------------------------------------------------------------------*/

void sceNpSetFakeUsername(const char* username)
{
    if (username) {
        strncpy(s_fake_username, GUEST_PTR(username, const char*),
            SCE_NP_ONLINEID_MAX_LENGTH);
        s_fake_username[SCE_NP_ONLINEID_MAX_LENGTH] = '\0';
    }
}

const char* np_fake_username(void)
{
    return s_fake_username;
}

/* Build a fake NP ID from the current username (PS3_NP_ONLINE_ID wins, so
 * two instances on one machine can be two players). */
static void np_build_fake_id(SceNpId* npId)
{
    memset(npId, 0, sizeof(SceNpId));
    strncpy(npId->handle.data, np_psnr_online_id(), SCE_NP_ONLINEID_MAX_LENGTH);
    npId->handle.term = '\0';
}

/* ---------------------------------------------------------------------------
 * API implementations
 * -----------------------------------------------------------------------*/

s32 sceNpInit(u32 poolSize, void* poolPtr)
{
    (void)poolSize;
    (void)poolPtr;

    printf("[sceNp] Init(poolSize=%u, username=\"%s\")\n",
           poolSize, np_psnr_online_id());

    if (s_np_initialized)
        return SCE_NP_ERROR_ALREADY_INITIALIZED;

    s_np_initialized = 1;
    return CELL_OK;
}

/* Whether sceNpInit has run; sceNpBasic follows it (see sceNpBasic.c). */
int np_is_initialized(void) { return s_np_initialized; }

s32 sceNpTerm(void)
{
    printf("[sceNp] Term()\n");

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    s_np_initialized = 0;
    return CELL_OK;
}

s32 sceNpGetNpId(s32 userId, SceNpId* npId)
{
    (void)userId;

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!npId)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    /* A guest address, like every sibling getter's: writing through it
     * untranslated hit a host address and crashed Simpsons Arcade the first
     * time it asked for its NP ID online. */
    npId = GUEST_PTR(npId, SceNpId*);
    np_build_fake_id(npId);
    printf("[sceNp] GetNpId(user=%d) -> \"%s\"\n", userId, np_psnr_online_id());
    return CELL_OK;
}

s32 sceNpGetOnlineId(s32 userId, SceNpOnlineId* onlineId)
{
    onlineId = GUEST_PTR(onlineId, SceNpOnlineId*);
    (void)userId;

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!onlineId)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    memset(onlineId, 0, sizeof(SceNpOnlineId));
    strncpy(onlineId->data, np_psnr_online_id(), SCE_NP_ONLINEID_MAX_LENGTH);
    onlineId->term = '\0';

    printf("[sceNp] GetOnlineId(user=%d) -> \"%s\"\n", userId, np_psnr_online_id());
    return CELL_OK;
}

s32 sceNpGetOnlineName(s32 userId, SceNpOnlineName* onlineName)
{
    onlineName = GUEST_PTR(onlineName, SceNpOnlineName*);
    (void)userId;

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!onlineName)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    memset(onlineName, 0, sizeof(SceNpOnlineName));
    strncpy(onlineName->data, np_psnr_online_id(),
            SCE_NP_ONLINENAME_MAX_LENGTH - 1);

    printf("[sceNp] GetOnlineName(user=%d) -> \"%s\"\n",
           userId, np_psnr_online_id());
    return CELL_OK;
}

s32 sceNpGetUserProfile(s32 userId, SceNpUserInfo* userInfo)
{
    userInfo = GUEST_PTR(userInfo, SceNpUserInfo*);
    (void)userId;

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!userInfo)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    memset(userInfo, 0, sizeof(SceNpUserInfo));
    np_build_fake_id(&userInfo->npId);
    strncpy(userInfo->onlineName.data, np_psnr_online_id(),
            SCE_NP_ONLINENAME_MAX_LENGTH - 1);
    /* Leave avatar URL empty */

    printf("[sceNp] GetUserProfile(user=%d) -> \"%s\"\n",
           userId, np_psnr_online_id());
    return CELL_OK;
}

s32 sceNpGetAccountRegion(s32 userId, u32* region)
{
    (void)userId;

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!region)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    /* Region: US (SCEA) = 0x5553 ('US') */
    vm_write32((u32)(uintptr_t)region, (u32)0x5553);

    printf("[sceNp] GetAccountRegion(user=%d) -> US\n", userId);
    return CELL_OK;
}

s32 sceNpGetAccountAge(s32 userId, s32* age)
{
    (void)userId;

    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!age)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    vm_write32((u32)(uintptr_t)age, 25); /* default adult age */
    printf("[sceNp] GetAccountAge(user=%d) -> 25\n", userId);
    return CELL_OK;
}

s32 sceNpGetMyLanguages(SceNpMyLanguages* langs)
{
    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    if (!langs)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    u32 langs_ea = (u32)(uintptr_t)langs;
    vm_write32(langs_ea + 0, SCE_NP_LANG_ENGLISH);
    vm_write32(langs_ea + 4, 0);
    vm_write32(langs_ea + 8, 0);

    printf("[sceNp] GetMyLanguages() -> English\n");
    return CELL_OK;
}

/* ---------------------------------------------------------------------------
 * NP Manager (sign-in state)
 *
 * The toolkit runs with a fake local profile but no live PSN connection. So the
 * manager reports OFFLINE — games gate their online flows on GetStatus and skip
 * them cleanly — while the identity getters still hand back the fake account,
 * matching how NP behaves on a real signed-in-but-disconnected console.
 * Imported by most online-capable titles.
 * -----------------------------------------------------------------------*/

static SceNpManagerCallback s_npmgr_cb = NULL;
static void*                s_npmgr_cb_arg = NULL;

s32 sceNpManagerGetStatus(s32* status)
{
    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!status)
        return SCE_NP_ERROR_INVALID_ARGUMENT;
    /* `status` is a GUEST address -- the HLE ABI adapter passes pointer
     * parameters straight through as guest values, so dereferencing one
     * writes to whatever host address shares that number. Same trap as
     * cellGcmSys had. Tokyo Jungle calls this during its online init. */
    /* Signed in when there is a psnr server to be signed in to. */
    s32 st = np_psnr_enabled() ? SCE_NP_MANAGER_STATUS_ONLINE : SCE_NP_MANAGER_STATUS_OFFLINE;
    vm_write32((uint32_t)(uintptr_t)status, (uint32_t)st);
    { static s32 last = -2;
      if (st != last) printf("[sceNp] ManagerGetStatus() -> %s\n", st < 0 ? "OFFLINE" : "ONLINE");
      last = st; }
    return CELL_OK;
}

s32 sceNpManagerRegisterCallback(SceNpManagerCallback callback, void* arg)
{
    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;
    s_npmgr_cb     = callback;
    s_npmgr_cb_arg = arg;
    printf("[sceNp] ManagerRegisterCallback()\n");
    /* Online: tell the title it is signed in, as the console does once the
     * manager reaches ONLINE. Delivered on its next cellSysutilCheckCallback. */
    if (np_psnr_enabled() && callback) {
        const u64 args[8] = { (u64)(u32)SCE_NP_MANAGER_STATUS_ONLINE, 0,
                              (u64)(u32)(uintptr_t)arg, 0, 0, 0, 0, 0 };
        cellSysutilQueueGuestCallbackArgs((u32)(uintptr_t)callback, args);
    }
    return CELL_OK;
}

s32 sceNpManagerUnregisterCallback(void)
{
    s_npmgr_cb     = NULL;
    s_npmgr_cb_arg = NULL;
    return CELL_OK;
}

/* Identity getters: reuse the fake-profile implementations (offline-with-account). */
s32 sceNpManagerGetNpId(SceNpId* npId)               { return sceNpGetNpId(0, npId); }
s32 sceNpManagerGetOnlineId(SceNpOnlineId* onlineId) { return sceNpGetOnlineId(0, onlineId); }
s32 sceNpManagerGetOnlineName(SceNpOnlineName* name) { return sceNpGetOnlineName(0, name); }
s32 sceNpManagerGetAccountAge(s32* age)              { return sceNpGetAccountAge(0, age); }

/* Parental controls: an unrestricted adult account. Unimplemented, these
 * returned OK without writing, and the title read whatever was in its stack. */
s32 sceNpManagerGetContentRatingFlag(s32* isRestricted, s32* age)
{
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (isRestricted) vm_write32((u32)(uintptr_t)isRestricted, 0);
    if (age) vm_write32((u32)(uintptr_t)age, 25);
    return CELL_OK;
}

s32 sceNpManagerGetChatRestrictionFlag(s32* isRestricted)
{
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (isRestricted) vm_write32((u32)(uintptr_t)isRestricted, 0);
    return CELL_OK;
}

/* Score setup is local even while the NP manager is offline. Games may
 * initialize it before starting the worker that decides whether to use PSN. */
s32 sceNpScoreInit(void)
{
    if (s_score_initialized)
        return SCE_NP_COMMUNITY_ERROR_ALREADY_INITIALIZED;
    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;
    s_score_initialized = 1;
    return CELL_OK;
}

s32 sceNpScoreTerm(void)
{
    if (!s_score_initialized)
        return SCE_NP_COMMUNITY_ERROR_NOT_INITIALIZED;
    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;
    s_score_initialized = 0;
    return CELL_OK;
}

/* ---------------------------------------------------------------------------
 * NPDRM
 *
 * On hardware sceNpDrmIsAvailable[2](klicensee, path) checks the licence for an
 * NPDRM file and leaves the klicensee with the kernel, so later cellFsOpen calls
 * on EDATs return plaintext. Free (license type 3) and disc-bound game data use a
 * title-specific klicensee that only the game knows; record it for the EDAT
 * reader (libs/filesystem/edat.c), which verifies it against each file's
 * dev_hash before using it. The two calls differ only in licence-check details
 * that do not apply here.
 * -----------------------------------------------------------------------*/

static s32 np_drm_is_available(const u8* k_licensee, const char* drm_path)
{
    const u8* k = GUEST_PTR(k_licensee, const u8*);
    const char* path = GUEST_PTR(drm_path, const char*);
    /* A NULL klicensee is legal for content that needs none; nothing to record. */
    if (k) edat_add_klicensee(k);
    fprintf(stderr, "[sceNp] DRM availability check: %s\n", path ? path : "(null)");
    return CELL_OK;
}

s32 sceNpDrmIsAvailable(const u8* k_licensee, const char* drm_path)
{
    return np_drm_is_available(k_licensee, drm_path);
}

s32 sceNpDrmIsAvailable2(const u8* k_licensee, const char* drm_path)
{
    return np_drm_is_available(k_licensee, drm_path);
}

/* sceNpManagerGetCachedInfo -- the NP identity cached for a local user.
 * Same checks and order as RPCS3 (sceNp.cpp): NOT_INITIALIZED, null param ->
 * INVALID_ARGUMENT, a user other than the current one (0) or the single local
 * user (1, /dev_hdd0/home/00000001) -> CELL_ENOENT. Every member is a byte
 * array, so the guest layout matches the host struct byte for byte except
 * the leading big-endian size word. */
typedef struct SceNpManagerCacheParam_hle {
    u32             size;          /* big-endian in guest memory */
    SceNpOnlineId   onlineId;
    SceNpId         npId;
    SceNpOnlineName onlineName;
    SceNpAvatarUrl  avatarUrl;
} SceNpManagerCacheParam_hle;

s32 sceNpManagerGetCachedInfo(u32 userId, void* param)
{
    if (!s_np_initialized)
        return SCE_NP_ERROR_NOT_INITIALIZED;

    u32 param_ea = (u32)(uintptr_t)param;
    if (!param_ea)
        return SCE_NP_ERROR_INVALID_ARGUMENT;

    if (userId != 0 && userId != 1)
        return CELL_ENOENT;

    SceNpManagerCacheParam_hle* p = GUEST_PTR(param, SceNpManagerCacheParam_hle*);
    memset(p, 0, sizeof(*p));
    strncpy(p->onlineId.data, np_psnr_online_id(), SCE_NP_ONLINEID_MAX_LENGTH);
    np_build_fake_id(&p->npId);
    strncpy(p->onlineName.data, np_psnr_online_id(), SCE_NP_ONLINENAME_MAX_LENGTH - 1);
    /* avatar URL left empty, as in sceNpGetUserProfile */
    vm_write32(param_ea, (u32)sizeof(SceNpManagerCacheParam_hle));

    printf("[sceNp] ManagerGetCachedInfo(user=%u) -> \"%s\"\n", userId, np_psnr_online_id());
    return CELL_OK;
}

/* ===========================================================================
 * Derived from RPCS3 (https://github.com/RPCS3/rpcs3), GPL-2.0-only
 * Offline answers for NP calls that up to 41 games import but that were not
 * registered. The unresolved path answered CELL_OK with untouched buffers, so
 * a game could believe it had a PSN ticket, a region or a friend list. These
 * return what RPCS3 returns for a console that is not signed in. With the
 * PSN-replacement server enabled (np_psnr_enabled) they keep the previous
 * behaviour (CELL_OK) so online mode does not regress.
 * ======================================================================== */
#define RR_NP_BASIC_ERROR_NOT_INITIALIZED       0x8002a662u
#define RR_NP_BASIC_ERROR_INVALID_ARGUMENT      0x8002a665u
#define RR_NP_BASIC_ERROR_INVALID_DATA_ID       0x8002a673u
#define RR_NP_FRIENDLIST_ERROR_NOT_INITIALIZED  0x8002ab21u
#define RR_NP_FRIENDLIST_ERROR_INVALID_ARGUMENT 0x8002ab28u
#define RR_NP_FRIENDLIST_ERROR_CANCEL           0x8002ab25u
#define RR_NP_PROFILE_ERROR_NOT_INITIALIZED     0x8002ab41u
#define RR_NP_PROFILE_ERROR_INVALID_ARGUMENT    0x8002ab48u
#define RR_NP_PROFILE_ERROR_CANCEL              0x8002ab45u
#define RR_NP_COMMUNITY_ERROR_INSUFFICIENT_ARG  0x8002a1a1u
#define RR_NP_COMMUNITY_ERROR_INVALID_ONLINE_ID 0x8002a1a4u
#define RR_NP_CUSTOM_MENU_ERROR_NOT_INITIALIZED 0x80023b02u
#define RR_NP_CUSTOM_MENU_ERROR_INVALID_ARG     0x80023b09u
#define RR_NP_SIGNALING_ERROR_NOT_INITIALIZED   0x8002a801u
#define RR_NP_AUTH_EINVALID_ARGUMENT            0x8002a015u
#define RR_NP_OFFLINE (!np_psnr_enabled())

s32 sceNpManagerGetTicket(u32 buffer_ea, u32 size_ea)
{
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!size_ea) return SCE_NP_ERROR_INVALID_ARGUMENT;
    if (RR_NP_OFFLINE) { vm_write32(size_ea, 0); return SCE_NP_ERROR_INVALID_STATE; }  /* no ticket */
    (void)buffer_ea; return CELL_OK;
}

s32 sceNpManagerGetTicketParam(s32 paramId, u32 param_ea)
{
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!param_ea || paramId < 0 || paramId > 13) return SCE_NP_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_INVALID_STATE : CELL_OK;   /* ticket empty */
}

s32 sceNpManagerGetAccountRegion(u32 countryCode_ea, u32 language_ea)
{
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!countryCode_ea || !language_ea) return SCE_NP_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_OFFLINE : CELL_OK;
}

s32 sceNpManagerRequestTicket2(u32 npId_ea, u32 version_ea, u32 serviceId_ea, u32 cookie_ea, u32 cookieSize)
{
    (void)version_ea; (void)cookie_ea;
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!npId_ea || !serviceId_ea || cookieSize > 1024) return RR_NP_AUTH_EINVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_OFFLINE : CELL_OK;
}

s32 sceNpManagerGetEntitlementById(u32 entId_ea, u32 ent_ea)
{
    (void)ent_ea;
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!entId_ea) return SCE_NP_ERROR_INVALID_ARGUMENT;
    return SCE_NP_ERROR_ID_NOT_FOUND;   /* RPCS3: always not found */
}

/* GUI flows (message, friend request, friend list, profile): no overlay exists,
 * so report that the player cancelled instead of returning success and never
 * firing the completion callback (which hangs games that wait for it). */
s32 sceNpBasicSendMessageGui(u32 msg_ea, u32 containerId)
{
    (void)containerId;
    if (!s_np_initialized) return RR_NP_BASIC_ERROR_NOT_INITIALIZED;
    if (!msg_ea) return RR_NP_BASIC_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_OFFLINE : CELL_OK;
}

s32 sceNpBasicAddFriend(u32 contact_ea, u32 body_ea, u32 containerId)
{
    (void)body_ea; (void)containerId;
    if (!s_np_initialized) return RR_NP_BASIC_ERROR_NOT_INITIALIZED;
    if (!contact_ea) return RR_NP_BASIC_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_OFFLINE : CELL_OK;
}

s32 sceNpBasicRecvMessageCustom(u32 mainType, u32 recvOptions, u32 containerId)
{
    (void)recvOptions; (void)containerId;
    if (!s_np_initialized) return RR_NP_BASIC_ERROR_NOT_INITIALIZED;
    if (mainType != 3 && mainType != 4) return 0x8002a671u;   /* NOT_SUPPORTED */
    return RR_NP_OFFLINE ? SCE_NP_ERROR_OFFLINE : CELL_OK;
}

s32 sceNpBasicRecvMessageAttachmentLoad(u32 id, u32 buffer_ea, u32 size_ea)
{
    (void)id;
    if (!s_np_initialized) return RR_NP_BASIC_ERROR_NOT_INITIALIZED;
    if (!buffer_ea || !size_ea) return RR_NP_BASIC_ERROR_INVALID_ARGUMENT;
    return RR_NP_BASIC_ERROR_INVALID_DATA_ID;   /* no selected message offline */
}

s32 sceNpBasicGetBlockListEntry(u32 index, u32 npid_ea)
{
    (void)index;
    if (!s_np_initialized) return RR_NP_BASIC_ERROR_NOT_INITIALIZED;
    if (!npid_ea) return RR_NP_BASIC_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_ID_NOT_FOUND : CELL_OK;
}

s32 sceNpBasicSetPresenceDetails2(u32 pres_ea, u32 options)
{
    (void)options;
    if (!s_np_initialized) return RR_NP_BASIC_ERROR_NOT_INITIALIZED;
    if (!pres_ea) return RR_NP_BASIC_ERROR_INVALID_ARGUMENT;
    return CELL_OK;   /* presence is accepted (and ignored) offline as well */
}

s32 sceNpFriendlist(u32 handler_ea, u32 userArg_ea, u32 containerId)
{
    (void)userArg_ea; (void)containerId;
    if (!s_np_initialized) return RR_NP_FRIENDLIST_ERROR_NOT_INITIALIZED;
    if (!handler_ea) return RR_NP_FRIENDLIST_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? RR_NP_FRIENDLIST_ERROR_CANCEL : CELL_OK;
}

s32 sceNpProfileCallGui(u32 npid_ea, u32 handler_ea, u32 userArg_ea, u64 options)
{
    (void)npid_ea; (void)userArg_ea; (void)options;
    if (!s_np_initialized) return RR_NP_PROFILE_ERROR_NOT_INITIALIZED;
    if (!handler_ea) return RR_NP_PROFILE_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? RR_NP_PROFILE_ERROR_CANCEL : CELL_OK;
}

s32 sceNpLookupNpIdAsync(s32 transId, u32 onlineId_ea, u32 npId_ea, s32 prio, u32 option_ea)
{
    (void)transId; (void)prio;
    if (!onlineId_ea || !npId_ea) return RR_NP_COMMUNITY_ERROR_INSUFFICIENT_ARG;
    if (option_ea) return 0x8002a1a3u;   /* COMMUNITY_ERROR_INVALID_ARGUMENT */
    return RR_NP_OFFLINE ? RR_NP_COMMUNITY_ERROR_INVALID_ONLINE_ID : CELL_OK;
}

s32 sceNpSignalingAddExtendedHandler(u32 ctx_id, u32 handler_ea, u32 arg_ea)
{
    (void)ctx_id; (void)handler_ea; (void)arg_ea;
    if (!s_np_initialized) return RR_NP_SIGNALING_ERROR_NOT_INITIALIZED;
    return CELL_OK;
}

s32 sceNpCustomMenuRegisterActions(u32 menu_ea, u32 handler_ea, u32 userArg_ea, u64 options)
{
    (void)userArg_ea; (void)options;
    if (!s_np_initialized) return RR_NP_CUSTOM_MENU_ERROR_NOT_INITIALIZED;
    if (!menu_ea || !handler_ea) return RR_NP_CUSTOM_MENU_ERROR_INVALID_ARG;
    return CELL_OK;   /* menu entries are never shown; registering succeeds */
}
s32 sceNpManagerGetNetworkTime(u32 tick_ea)
{
    if (!s_np_initialized) return SCE_NP_ERROR_NOT_INITIALIZED;
    if (!tick_ea) return SCE_NP_ERROR_INVALID_ARGUMENT;
    return RR_NP_OFFLINE ? SCE_NP_ERROR_OFFLINE : CELL_OK;
}
/* ======================== end RPCS3-derived block ======================== */

