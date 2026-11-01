/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* rr_shader_cache.h - persistent on-disk cache of compiled guest shaders.
 *
 * Guest vertex/fragment programs are translated to HLSL and compiled with
 * D3DCompile at first use, which costs tens of milliseconds per shader and is
 * the main source of load-time hitches. The compiled bytecode is stored
 * beside the executable in cache\shaders\ and reused on later runs.
 *
 * Each file holds a header (magic, version, target, source length and a
 * 64-bit hash of the exact HLSL text) followed by the bytecode. Any mismatch
 * or short file is ignored and the shader is compiled normally, so a damaged
 * cache can never change rendering. RR_SHADER_CACHE=0 turns it off.
 */
#ifndef RR_SHADER_CACHE_H
#define RR_SHADER_CACHE_H

#define RR_SC_MAGIC   0x43535252u   /* "RRSC" */
#define RR_SC_VERSION 1u

typedef struct { unsigned magic, version, target_hash, src_len;
                 unsigned long long src_hash; unsigned blob_size, pad; } rr_sc_header;

static unsigned long long rr_sc_fnv64(const void* p, size_t n, unsigned long long h)
{
    const unsigned char* b = (const unsigned char*)p;
    while (n--) { h ^= *b++; h *= 0x100000001B3ull; }
    return h;
}

static int    rr_sc_state = -1;              /* -1 unknown, 0 off, 1 on */
static char   rr_sc_dir[MAX_PATH];
static unsigned rr_sc_hits = 0, rr_sc_misses = 0;
static double rr_sc_compile_ms = 0.0;

static int rr_sc_enabled(void)
{
    if (rr_sc_state >= 0) return rr_sc_state;
    const char* e = getenv("RR_SHADER_CACHE");
    rr_sc_state = 0;
    if (e && e[0] == '0') return 0;
    char exe[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, exe, MAX_PATH);
    if (!n || n >= MAX_PATH) return 0;
    char* slash = strrchr(exe, '\\'); if (!slash) return 0;
    *slash = 0;
    snprintf(rr_sc_dir, sizeof rr_sc_dir, "%s\\cache", exe);   CreateDirectoryA(rr_sc_dir, NULL);
    snprintf(rr_sc_dir, sizeof rr_sc_dir, "%s\\cache\\shaders", exe); CreateDirectoryA(rr_sc_dir, NULL);
    rr_sc_state = 1;
    return 1;
}

/* Drop-in replacement for D3DCompile(src, len, name, NULL, NULL, entry, target, 0, 0, out, err). */
static HRESULT rr_cached_compile(const char* src, size_t len, const char* name,
                                 const char* entry, const char* target,
                                 ID3DBlob** out, ID3DBlob** err)
{
    *out = NULL; if (err) *err = NULL;
    unsigned long long h = rr_sc_fnv64(src, len, 1469598103934665603ull);
    h = rr_sc_fnv64(entry, strlen(entry), h);
    unsigned th = (unsigned)rr_sc_fnv64(target, strlen(target), 1469598103934665603ull);
    char path[MAX_PATH + 64] = {0};
    if (rr_sc_enabled()) {
        snprintf(path, sizeof path, "%s\\%016llX_%08X.dxbc", rr_sc_dir, h, th);
        FILE* f = fopen(path, "rb");
        if (f) {
            rr_sc_header hd; int ok = fread(&hd, sizeof hd, 1, f) == 1 && hd.magic == RR_SC_MAGIC &&
                hd.version == RR_SC_VERSION && hd.target_hash == th && hd.src_len == (unsigned)len &&
                hd.src_hash == h && hd.blob_size > 0 && hd.blob_size < (64u << 20);
            if (ok && SUCCEEDED(D3DCreateBlob(hd.blob_size, out)) && *out) {
                if (fread((*out)->lpVtbl->GetBufferPointer(*out), 1, hd.blob_size, f) == hd.blob_size) {
                    fclose(f); rr_sc_hits++; return S_OK;
                }
                (*out)->lpVtbl->Release(*out); *out = NULL;
            }
            fclose(f);
        }
    }
    LARGE_INTEGER t0, t1, fq; QueryPerformanceCounter(&t0);
    HRESULT hr = D3DCompile(src, len, name, NULL, NULL, entry, target, 0, 0, out, err);
    QueryPerformanceCounter(&t1); QueryPerformanceFrequency(&fq);
    rr_sc_compile_ms += (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)fq.QuadPart;
    rr_sc_misses++;
    if (SUCCEEDED(hr) && *out && path[0]) {
        char tmp[MAX_PATH + 80];
        snprintf(tmp, sizeof tmp, "%s.%lu.tmp", path, (unsigned long)GetCurrentThreadId());
        FILE* f = fopen(tmp, "wb");
        if (f) {
            rr_sc_header hd = { RR_SC_MAGIC, RR_SC_VERSION, th, (unsigned)len, h,
                                (unsigned)(*out)->lpVtbl->GetBufferSize(*out), 0 };
            int ok = fwrite(&hd, sizeof hd, 1, f) == 1 &&
                     fwrite((*out)->lpVtbl->GetBufferPointer(*out), 1, hd.blob_size, f) == hd.blob_size;
            ok = (fclose(f) == 0) && ok;
            if (!ok || !MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING)) DeleteFileA(tmp);
        }
    }
    return hr;
}

/* One summary line per second when shaders were requested, so logs show what load time went to. */
static void rr_sc_report(void)
{
    static unsigned last_h = 0, last_m = 0; static double last_ms = 0.0;
    if (rr_sc_hits == last_h && rr_sc_misses == last_m) return;
    printf("[RR-TIME] shaders: %u compiled (%.0f ms), %u from disk cache\n",
           rr_sc_misses - last_m, rr_sc_compile_ms - last_ms, rr_sc_hits - last_h);
    last_h = rr_sc_hits; last_m = rr_sc_misses; last_ms = rr_sc_compile_ms;
}
#endif
