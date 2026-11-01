/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/*
 * rrSysLibcBridge.c -- sys_libc / sys_libstdcxx / sys_libm imports of
 * game-shipped PRX modules.
 *
 * Game EBOOTs link their C/C++ runtime statically, but modules a game ships
 * (USRDIR .sprx files, see runtime/prx/rr_game_prx.c) import it from the
 * firmware's sys_libc / sys_libstdcxx / sys_libm. This bridge implements the
 * entries those modules use, on guest memory. It is registered only by builds
 * that contain such a module (the generated registration file calls
 * rr_sys_libc_register), so other builds are unaffected.
 *
 * The C functions follow the C standard; the Dinkumware C++ runtime internals
 * are reduced to what static initialisation and container code needs:
 * mutexes are real (host recursive locks keyed by the object's address),
 * locale/iostream bookkeeping is accepted without state (no facets are cached,
 * so use_facet creates its own), and the throw helpers log and return.
 * Written from the C standard and the public NID names; no third-party code.
 */
#include "../../runtime/ppu/ppu_context.h"
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <pthread.h>
#endif

extern uint8_t* vm_base;
void ps3_hle_register_ctx(uint32_t nid, const char* name, void (*fn)(ppu_context*));
int32_t rr_vm_commit_bridge(uint32_t addr, uint32_t size);

#define G(ea) ((char*)(vm_base + (uint32_t)(ea)))
#define A(n)  ((uint32_t)c->gpr[3 + (n)])
static void ret(ppu_context* c, uint64_t v) { c->gpr[3] = v; }
static void wr32(uint32_t ea, uint32_t v)
{
    uint8_t* p = vm_base + ea;
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v;
}
static uint64_t rd64(uint32_t ea)
{
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) v = (v << 8) | vm_base[ea + i];
    return v;
}

/* ---- C library ------------------------------------------------------- */
static void h_memset(ppu_context* c)  { memset(G(A(0)), (int)A(1), A(2)); ret(c, A(0)); }
static void h_memcpy(ppu_context* c)  { memmove(G(A(0)), G(A(1)), A(2)); ret(c, A(0)); }
static void h_memmove(ppu_context* c) { memmove(G(A(0)), G(A(1)), A(2)); ret(c, A(0)); }
static void h_memcmp(ppu_context* c)  { ret(c, (uint64_t)(int64_t)memcmp(G(A(0)), G(A(1)), A(2))); }
static void h_strlen(ppu_context* c)  { ret(c, strlen(G(A(0)))); }
static void h_strcmp(ppu_context* c)  { ret(c, (uint64_t)(int64_t)strcmp(G(A(0)), G(A(1)))); }
static void h_strcspn(ppu_context* c) { ret(c, strcspn(G(A(0)), G(A(1)))); }
static void h_memchr(ppu_context* c)
{
    char* p = (char*)memchr(G(A(0)), (int)(A(1) & 0xFF), A(2));
    ret(c, p ? (uint64_t)(uint32_t)(p - (char*)vm_base) : 0);
}
static void h_strtol(ppu_context* c)
{
    char* end = NULL;
    long v = strtol(G(A(0)), &end, (int)A(2));
    if (A(1)) wr32(A(1), (uint32_t)(end - (char*)vm_base));
    ret(c, (uint64_t)(int64_t)(int32_t)v);   /* PS3 long is 32-bit */
}
static void h_Stoul(ppu_context* c)
{
    char* end = NULL;
    unsigned long v = strtoul(G(A(0)), &end, (int)A(2));
    if (A(1)) wr32(A(1), (uint32_t)(end - (char*)vm_base));
    ret(c, (uint32_t)v);
}
static void h_Stod(ppu_context* c)   /* double _Stod(const char*, char**, long pten) */
{
    char* end = NULL;
    double v = strtod(G(A(0)), &end);
    if (A(1)) wr32(A(1), (uint32_t)(end - (char*)vm_base));
    if ((int32_t)A(2)) v *= pow(10.0, (double)(int32_t)A(2));
    c->fpr[1] = v;
}
static void h_FSin(ppu_context* c)   /* float _FSin(float x, unsigned qoff): 0 sin, odd cos */
{
    double x = c->fpr[1];
    c->fpr[1] = (float)((A(0) & 1) ? cos(x) : sin(x));
}

/* sprintf(buf, fmt, ...): variadic integers come from the GPR slots after the
 * two fixed arguments, then the caller's parameter save area; doubles from
 * f1.. in order (the ABI also reserves their GPR slot). */
static void h_sprintf(ppu_context* c)
{
    char* out = G(A(0));
    const char* f = G(A(1));
    int slot = 2, fslot = 1, n = 0;
    uint32_t save = (uint32_t)c->gpr[1] + 48;   /* parameter save area */
#define NEXT_I() (slot < 8 ? c->gpr[3 + slot++] : rd64(save + 8u * (uint32_t)(slot++)))
    while (*f && n < 4096) {
        if (*f != '%') { out[n++] = *f++; continue; }
        char spec[32]; int k = 0; spec[k++] = *f++;
        while (*f && strchr("-+ #0123456789.", *f) && k < 20) spec[k++] = *f++;
        int lmod = 0;
        while (*f == 'l' || *f == 'h' || *f == 'z' || *f == 'j' || *f == 't') { if (*f == 'l') lmod++; f++; }
        char conv = *f ? *f++ : 0;
        char buf[512];
        spec[k] = 0;
        switch (conv) {
        case 'd': case 'i': {
            int64_t v = (int64_t)NEXT_I(); if (lmod < 2) v = (int32_t)v;
            strcat(spec, "lld"); snprintf(buf, sizeof buf, spec, (long long)v); break; }
        case 'u': case 'x': case 'X': case 'o': {
            uint64_t v = NEXT_I(); if (lmod < 2) v = (uint32_t)v;
            size_t L = strlen(spec); spec[L] = 'l'; spec[L + 1] = 'l'; spec[L + 2] = conv; spec[L + 3] = 0;
            snprintf(buf, sizeof buf, spec, (unsigned long long)v); break; }
        case 'c': { int v = (int)NEXT_I(); strcat(spec, "c"); snprintf(buf, sizeof buf, spec, v); break; }
        case 's': { uint32_t ea = (uint32_t)NEXT_I(); strcat(spec, "s");
                    snprintf(buf, sizeof buf, spec, ea ? G(ea) : "(null)"); break; }
        case 'p': { uint32_t v = (uint32_t)NEXT_I(); snprintf(buf, sizeof buf, "0x%x", v); break; }
        case 'f': case 'F': case 'e': case 'E': case 'g': case 'G': {
            double v = fslot <= 13 ? c->fpr[fslot++] : 0.0; slot++;
            size_t L = strlen(spec); spec[L] = conv; spec[L + 1] = 0;
            snprintf(buf, sizeof buf, spec, v); break; }
        case '%': strcpy(buf, "%"); break;
        default:  buf[0] = 0; break;
        }
        for (char* b = buf; *b && n < 4096; b++) out[n++] = *b;
    }
#undef NEXT_I
    out[n] = 0;
    ret(c, (uint64_t)n);
}

/* Scratch guest memory for localeconv()/mbstate: top of the game-module range. */
#define RR_LIBC_SCRATCH 0x3FFF0000u
static int scratch_ready(void)
{
    static int ready = 0;
    if (!ready) {
        if (rr_vm_commit_bridge(RR_LIBC_SCRATCH, 0x10000) != 0) return 0;
        memset(vm_base + RR_LIBC_SCRATCH, 0, 0x10000);
        /* lconv: ten char* (decimal_point, thousands_sep, grouping, int_curr_symbol,
         * currency_symbol, mon_decimal_point, mon_thousands_sep, mon_grouping,
         * positive_sign, negative_sign) then CHAR_MAX bytes. */
        uint32_t strings = RR_LIBC_SCRATCH + 0x100;
        G(strings)[0] = '.'; G(strings)[1] = 0;            /* "." at strings, "" at strings+1 */
        for (int i = 0; i < 10; i++) wr32(RR_LIBC_SCRATCH + 4u * (uint32_t)i, i == 0 ? strings : strings + 1);
        memset(G(RR_LIBC_SCRATCH + 40), 127, 14);
        ready = 1;
    }
    return 1;
}
static void h_localeconv(ppu_context* c)  { ret(c, scratch_ready() ? RR_LIBC_SCRATCH : 0); }
static void h_Getpmbstate(ppu_context* c) { ret(c, scratch_ready() ? RR_LIBC_SCRATCH + 0x200 : 0); }
static void h_Getpwcstate(ppu_context* c) { ret(c, scratch_ready() ? RR_LIBC_SCRATCH + 0x210 : 0); }

/* ---- locks ----------------------------------------------------------- */
#define RR_LOCKS 512
typedef struct { uint32_t key; int used;
#ifdef _WIN32
    CRITICAL_SECTION cs;
#else
    pthread_mutex_t mtx;
#endif
} rr_lock;
static rr_lock s_locks[RR_LOCKS];
#ifdef _WIN32
static INIT_ONCE s_once = INIT_ONCE_STATIC_INIT;
static CRITICAL_SECTION s_table;
static BOOL CALLBACK init_table(PINIT_ONCE o, PVOID p, PVOID* x) { (void)o; (void)p; (void)x; InitializeCriticalSection(&s_table); return TRUE; }
#define TABLE_LOCK()   (InitOnceExecuteOnce(&s_once, init_table, NULL, NULL), EnterCriticalSection(&s_table))
#define TABLE_UNLOCK() LeaveCriticalSection(&s_table)
#else
static pthread_mutex_t s_table = PTHREAD_MUTEX_INITIALIZER;
#define TABLE_LOCK()   pthread_mutex_lock(&s_table)
#define TABLE_UNLOCK() pthread_mutex_unlock(&s_table)
#endif
static rr_lock* lock_for(uint32_t key)
{
    TABLE_LOCK();
    uint32_t i = (key * 2654435761u) % RR_LOCKS;
    rr_lock* found = NULL;
    for (int probe = 0; probe < RR_LOCKS; probe++, i = (i + 1) % RR_LOCKS) {
        if (s_locks[i].used && s_locks[i].key == key) { found = &s_locks[i]; break; }
        if (!s_locks[i].used) {
            s_locks[i].used = 1; s_locks[i].key = key;
#ifdef _WIN32
            InitializeCriticalSection(&s_locks[i].cs);
#else
            pthread_mutexattr_t a; pthread_mutexattr_init(&a);
            pthread_mutexattr_settype(&a, PTHREAD_MUTEX_RECURSIVE);
            pthread_mutex_init(&s_locks[i].mtx, &a);
#endif
            found = &s_locks[i]; break;
        }
    }
    TABLE_UNLOCK();
    return found;
}
static void do_lock(uint32_t key, int lock)
{
    rr_lock* l = lock_for(key);
    if (!l) return;
#ifdef _WIN32
    if (lock) EnterCriticalSection(&l->cs); else LeaveCriticalSection(&l->cs);
#else
    if (lock) pthread_mutex_lock(&l->mtx); else pthread_mutex_unlock(&l->mtx);
#endif
}
static void h_Locksyslock(ppu_context* c)   { do_lock(0xFFFF0000u | (A(0) & 0xFF), 1); ret(c, 0); }
static void h_Unlocksyslock(ppu_context* c) { do_lock(0xFFFF0000u | (A(0) & 0xFF), 0); ret(c, 0); }
static void h_MutexCtor(ppu_context* c)     { lock_for(A(0)); ret(c, A(0)); }
static void h_MutexDtor(ppu_context* c)     { ret(c, A(0)); }
static void h_MutexLock(ppu_context* c)     { do_lock(A(0), 1); ret(c, 0); }
static void h_MutexUnlock(ppu_context* c)   { do_lock(A(0), 0); ret(c, 0); }

/* ---- Dinkumware C++ runtime internals -------------------------------- */
static void h_this(ppu_context* c)          { ret(c, A(0)); }   /* ctor/dtor/init: return this */
static void h_zero(ppu_context* c)          { ret(c, 0); }      /* _Getfacet: no cached facet */
static void h_throw(ppu_context* c)
{
    fprintf(stderr, "[sys_libstdcxx] C++ exception raised in a game module (this=0x%08X); continuing\n", A(0));
    ret(c, 0);
}

void rr_sys_libc_register(void)
{
    static int done = 0;
    if (done) return;
    done = 1;
    static const struct { uint32_t nid; const char* name; void (*fn)(ppu_context*); } t[] = {
        {0x5909E3C4u, "memset", h_memset},   {0x831D70A5u, "memcpy", h_memcpy},
        {0x5B162B7Fu, "memmove", h_memmove}, {0xC3E14CBEu, "memcmp", h_memcmp},
        {0x2F45D39Cu, "strlen", h_strlen},   {0x3D85D6F8u, "strcmp", h_strcmp},
        {0xCBAC7AD7u, "memchr", h_memchr},   {0xF7908E27u, "strcspn", h_strcspn},
        {0xD14ECE90u, "strtol", h_strtol},   {0x867275D7u, "_Stoul", h_Stoul},
        {0xD1D69CB8u, "_Stod", h_Stod},      {0xB5E28191u, "_FSin", h_FSin},
        {0x273B9711u, "sprintf", h_sprintf}, {0x1098A99Du, "localeconv", h_localeconv},
        {0x53EB43A1u, "_Getpmbstate", h_Getpmbstate}, {0x59C1BB1Fu, "_Getpwcstate", h_Getpwcstate},
        {0x7028DEA9u, "_Locksyslock", h_Locksyslock}, {0x34E7C97Eu, "_Unlocksyslock", h_Unlocksyslock},
        {0x75824DE0u, "std::_Mutex::_Mutex", h_MutexCtor},
        {0x1C3F1C4Fu, "std::_Mutex::~_Mutex", h_MutexDtor},
        {0x577C2695u, "std::_Mutex::_Lock", h_MutexLock},
        {0xD356AEFDu, "std::_Mutex::_Unlock", h_MutexUnlock},
        {0x605131D5u, "std::_Locinfo::_Locinfo", h_this},
        {0x5119680Bu, "std::_Locinfo::~_Locinfo", h_this},
        {0x660882E8u, "std::locale::locale", h_this},
        {0x0091A3FDu, "std::locale::_Getfacet", h_zero},
        {0xE2B2AC5Au, "std::locale::facet::_Register", h_this},
        {0x3697BBD3u, "std::ios_base::_Init", h_this},
        {0x4952490Eu, "std::ios_base::clear", h_this},
        {0xD8B23008u, "std::ios_base::~ios_base", h_this},
        {0x1A00F889u, "std::exception::~exception", h_this},
        {0x2D8BE7E8u, "std::exception::_Raise", h_throw},
        {0x9CB73EE0u, "std::_Throw", h_throw},
        {0x683CA70Au, "std::_String_base::_Xlen", h_throw},
        {0xC7931798u, "std::_String_base::_Xran", h_throw},
    };
    for (size_t i = 0; i < sizeof t / sizeof t[0]; i++)
        ps3_hle_register_ctx(t[i].nid, t[i].name, t[i].fn);
}
