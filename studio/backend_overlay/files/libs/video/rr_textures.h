/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* rr_textures.h -- HD texture dump and replacement for the D3D12 backend.
 *
 * Included once by rsx_d3d12_backend.c after s_d3d is defined.
 *
 *   RR_TEX_DUMP=1     save every new guest texture once, as DDS, to
 *                     <exe dir>/textures/dump/<title id>/<hash>_<w>x<h>_<fmt>.dds
 *   RR_TEX_REPLACE=0  ignore an installed pack. Default: on when
 *                     <exe dir>/textures/load/<title id>/ holds any .dds.
 *
 * A texture is identified by a 64-bit hash of its guest source bytes plus its
 * format, size and mip count. Replacement DDS files are matched by the 16 hex
 * digits that start their file name, so any size or mip count works: shaders
 * sample with normalised UVs. Dump writes and pack loads run on worker threads;
 * until a replacement has loaded the original texture is used. With neither
 * option active nothing here runs and output is unchanged.
 */
#ifndef RR_TEXTURES_H
#define RR_TEXTURES_H

/* ---------------------------------------------------------------- hash */
static u64 rr_tex_hash(const u8* p, u32 n, u32 w, u32 h, u32 fmt, u32 mips)
{
    const u64 P1 = 0x9E3779B185EBCA87ull, P2 = 0xC2B2AE3D27D4EB4Full,
              P3 = 0x165667B19E3779F9ull;
    u64 a = P1 ^ ((u64)w << 32 | h), b = P2 ^ ((u64)fmt << 32 | mips),
        c = P3 ^ n, d = P1 + P2;
    u32 i = 0;
    for (; i + 32 <= n; i += 32) {
        u64 v[4]; memcpy(v, p + i, 32);
        a = (a ^ (v[0] * P2)); a = (a << 31 | a >> 33) * P1;
        b = (b ^ (v[1] * P2)); b = (b << 31 | b >> 33) * P1;
        c = (c ^ (v[2] * P2)); c = (c << 31 | c >> 33) * P1;
        d = (d ^ (v[3] * P2)); d = (d << 31 | d >> 33) * P1;
    }
    u64 hsh = (a << 1 | a >> 63) + (b << 7 | b >> 57) + (c << 12 | c >> 52) + (d << 18 | d >> 46);
    for (; i < n; i++) { hsh ^= p[i] * P3; hsh = (hsh << 11 | hsh >> 53) * P1; }
    hsh ^= hsh >> 33; hsh *= P2; hsh ^= hsh >> 29; hsh *= P3; hsh ^= hsh >> 32;
    return hsh ? hsh : 1;
}

/* ---------------------------------------------------------------- jobs */
typedef struct rr_tex_job { struct rr_tex_job* next; int kind; void* arg; } rr_tex_job;
typedef struct {
    CRITICAL_SECTION cs; HANDLE ev; rr_tex_job* head; rr_tex_job* tail; HANDLE thr; int pending;
} rr_tex_queue;

static void rr_tex_run_job(rr_tex_job* j);
static DWORD WINAPI rr_tex_worker(LPVOID q_)
{
    rr_tex_queue* q = (rr_tex_queue*)q_;
    for (;;) {
        WaitForSingleObject(q->ev, INFINITE);
        for (;;) {
            EnterCriticalSection(&q->cs);
            rr_tex_job* j = q->head;
            if (j) { q->head = j->next; if (!q->head) q->tail = NULL; q->pending--; }
            LeaveCriticalSection(&q->cs);
            if (!j) break;
            rr_tex_run_job(j);
            free(j);
        }
    }
    return 0;
}
static void rr_tex_queue_init(rr_tex_queue* q)
{
    InitializeCriticalSection(&q->cs);
    q->ev = CreateEventA(NULL, FALSE, FALSE, NULL);
    q->thr = CreateThread(NULL, 0, rr_tex_worker, q, 0, NULL);
    if (q->thr) SetThreadPriority(q->thr, THREAD_PRIORITY_BELOW_NORMAL);
}
static int rr_tex_push(rr_tex_queue* q, int kind, void* arg, int limit)
{
    rr_tex_job* j = (rr_tex_job*)malloc(sizeof *j);
    if (!j) return 0;
    j->next = NULL; j->kind = kind; j->arg = arg;
    EnterCriticalSection(&q->cs);
    if (limit && q->pending >= limit) { LeaveCriticalSection(&q->cs); free(j); return 0; }
    if (q->tail) q->tail->next = j; else q->head = j;
    q->tail = j; q->pending++;
    LeaveCriticalSection(&q->cs);
    SetEvent(q->ev);
    return 1;
}

/* ---------------------------------------------------------------- state */
enum { RR_HD_INDEXED = 0, RR_HD_LOADING, RR_HD_CPU, RR_HD_GPU, RR_HD_FAILED };
typedef struct {
    u64 hash;
    char* path;
    volatile LONG state;
    u8* data; size_t size;          /* mip chain, tightly packed (RR_HD_CPU) */
    DXGI_FORMAT fmt; u32 w, h, mips;
    ID3D12Resource* res; ID3D12Resource* up; u64 up_frame;
} rr_hd_entry;

static struct {
    int init, dump, replace;
    char dump_dir[MAX_PATH], load_dir[MAX_PATH];
    rr_tex_queue dumpq, loadq;
    u64* dumped; u32 dumped_cap, dumped_n;      /* open-addressed hash set */
    rr_hd_entry* hd; u32 hd_cap, hd_n;          /* open-addressed by hash */
    u32 n_dumped, n_replaced;
} s_rrt;

static u32 rr_slot_of(u64 h, u32 cap) { return (u32)(h ^ (h >> 29)) & (cap - 1); }

static int rr_dumped_add(u64 h)   /* 1 if new */
{
    if ((s_rrt.dumped_n + 1) * 2 > s_rrt.dumped_cap) {
        u32 nc = s_rrt.dumped_cap ? s_rrt.dumped_cap * 2 : 4096;
        u64* nt = (u64*)calloc(nc, sizeof(u64));
        if (!nt) return 0;
        for (u32 i = 0; i < s_rrt.dumped_cap; i++) if (s_rrt.dumped[i]) {
            u32 k = rr_slot_of(s_rrt.dumped[i], nc);
            while (nt[k]) k = (k + 1) & (nc - 1);
            nt[k] = s_rrt.dumped[i];
        }
        free(s_rrt.dumped); s_rrt.dumped = nt; s_rrt.dumped_cap = nc;
    }
    u32 k = rr_slot_of(h, s_rrt.dumped_cap);
    while (s_rrt.dumped[k]) { if (s_rrt.dumped[k] == h) return 0; k = (k + 1) & (s_rrt.dumped_cap - 1); }
    s_rrt.dumped[k] = h; s_rrt.dumped_n++;
    return 1;
}

static rr_hd_entry* rr_hd_find(u64 h)
{
    if (!s_rrt.hd_cap) return NULL;
    u32 k = rr_slot_of(h, s_rrt.hd_cap);
    while (s_rrt.hd[k].hash) { if (s_rrt.hd[k].hash == h) return &s_rrt.hd[k]; k = (k + 1) & (s_rrt.hd_cap - 1); }
    return NULL;
}
static void rr_hd_add(u64 h, const char* path)
{
    if ((s_rrt.hd_n + 1) * 2 > s_rrt.hd_cap) {
        u32 nc = s_rrt.hd_cap ? s_rrt.hd_cap * 2 : 1024;
        rr_hd_entry* nt = (rr_hd_entry*)calloc(nc, sizeof *nt);
        if (!nt) return;
        for (u32 i = 0; i < s_rrt.hd_cap; i++) if (s_rrt.hd[i].hash) {
            u32 k = rr_slot_of(s_rrt.hd[i].hash, nc);
            while (nt[k].hash) k = (k + 1) & (nc - 1);
            nt[k] = s_rrt.hd[i];
        }
        free(s_rrt.hd); s_rrt.hd = nt; s_rrt.hd_cap = nc;
    }
    if (rr_hd_find(h)) return;                 /* first file for a hash wins */
    u32 k = rr_slot_of(h, s_rrt.hd_cap);
    while (s_rrt.hd[k].hash) k = (k + 1) & (s_rrt.hd_cap - 1);
    s_rrt.hd[k].hash = h; s_rrt.hd[k].path = _strdup(path); s_rrt.hd_n++;
}

static int rr_parse_hash(const char* name, u64* out)
{
    u64 v = 0;
    for (int i = 0; i < 16; i++) {
        char c = name[i]; int d;
        if (c >= '0' && c <= '9') d = c - '0';
        else if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
        else return 0;
        v = v << 4 | (u64)d;
    }
    if (name[16] && name[16] != '_' && name[16] != '.') return 0;
    *out = v; return v != 0;
}

/* Recursively index <dir>; mode 0 = pack (.dds into hd table), 1 = dump (hashes already saved). */
static void rr_scan(const char* dir, int mode, int depth)
{
    char pat[MAX_PATH]; WIN32_FIND_DATAA fd;
    if (depth > 8 || snprintf(pat, sizeof pat, "%s\\*", dir) >= (int)sizeof pat) return;
    HANDLE f = FindFirstFileA(pat, &fd);
    if (f == INVALID_HANDLE_VALUE) return;
    do {
        if (fd.cFileName[0] == '.') continue;
        char full[MAX_PATH];
        if (snprintf(full, sizeof full, "%s\\%s", dir, fd.cFileName) >= (int)sizeof full) continue;
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) { rr_scan(full, mode, depth + 1); continue; }
        size_t L = strlen(fd.cFileName); u64 h;
        if (L < 5 || _stricmp(fd.cFileName + L - 4, ".dds") || !rr_parse_hash(fd.cFileName, &h)) continue;
        if (mode == 0) rr_hd_add(h, full); else rr_dumped_add(h);
    } while (FindNextFileA(f, &fd));
    FindClose(f);
}

static void rr_mkdirs(const char* path)
{
    char tmp[MAX_PATH]; size_t n = strlen(path);
    if (n >= sizeof tmp) return;
    memcpy(tmp, path, n + 1);
    for (size_t i = 3; i < n; i++) if (tmp[i] == '\\' || tmp[i] == '/') { char c = tmp[i]; tmp[i] = 0; CreateDirectoryA(tmp, NULL); tmp[i] = c; }
    CreateDirectoryA(tmp, NULL);
}

static void rr_textures_init(void)
{
    if (s_rrt.init) return;
    s_rrt.init = 1;
    extern const char* cellGame_get_title_id(void);
    const char* tid = cellGame_get_title_id();
    char title[64]; size_t ti = 0;
    for (const char* c = tid ? tid : "UNKNOWN"; *c && ti + 1 < sizeof title; c++)
        title[ti++] = ((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') || (*c >= '0' && *c <= '9')) ? *c : '_';
    title[ti] = 0;
    char exe[MAX_PATH]; DWORD n = GetModuleFileNameA(NULL, exe, MAX_PATH);
    if (!n || n >= MAX_PATH) return;
    char* slash = strrchr(exe, '\\'); if (slash) *slash = 0;
    snprintf(s_rrt.dump_dir, sizeof s_rrt.dump_dir, "%s\\textures\\dump\\%s", exe, title);
    snprintf(s_rrt.load_dir, sizeof s_rrt.load_dir, "%s\\textures\\load\\%s", exe, title);

    const char* e = getenv("RR_TEX_DUMP");
    s_rrt.dump = e && atoi(e) > 0;
    e = getenv("RR_TEX_REPLACE");
    int want_replace = e ? atoi(e) > 0 : 1;
    if (want_replace) rr_scan(s_rrt.load_dir, 0, 0);
    s_rrt.replace = want_replace && s_rrt.hd_n > 0;
    if (s_rrt.dump) { rr_mkdirs(s_rrt.dump_dir); rr_scan(s_rrt.dump_dir, 1, 0); rr_tex_queue_init(&s_rrt.dumpq); }
    if (s_rrt.replace) rr_tex_queue_init(&s_rrt.loadq);
    if (s_rrt.dump || s_rrt.replace)
        printf("[RR-TEX] title %s: dump %s (%u already saved), replace %s (%u HD textures)\n",
               title, s_rrt.dump ? "on" : "off", s_rrt.dumped_n,
               s_rrt.replace ? "on" : "off", s_rrt.hd_n);
}
static int rr_textures_active(void) { rr_textures_init(); return s_rrt.dump || s_rrt.replace; }

/* ---------------------------------------------------------------- DDS */
typedef struct {
    u32 magic, size, flags, height, width, pitch, depth, mips, reserved[11];
    u32 pf_size, pf_flags, fourcc, rgb_bits, rmask, gmask, bmask, amask;
    u32 caps, caps2, caps3, caps4, reserved2;
} rr_dds_header;
typedef struct { u32 dxgi, dim, misc, array, misc2; } rr_dds_dx10;

static int rr_fmt_info(DXGI_FORMAT f, u32* block, u32* bytes)   /* block 4 = BC */
{
    switch (f) {
    case DXGI_FORMAT_BC1_UNORM: *block = 4; *bytes = 8; return 1;
    case DXGI_FORMAT_BC2_UNORM: case DXGI_FORMAT_BC3_UNORM: case DXGI_FORMAT_BC7_UNORM:
        *block = 4; *bytes = 16; return 1;
    case DXGI_FORMAT_R8G8B8A8_UNORM: case DXGI_FORMAT_B8G8R8A8_UNORM: *block = 1; *bytes = 4; return 1;
    case DXGI_FORMAT_R8G8_UNORM: case DXGI_FORMAT_R16_UNORM: *block = 1; *bytes = 2; return 1;
    case DXGI_FORMAT_R8_UNORM: *block = 1; *bytes = 1; return 1;
    case DXGI_FORMAT_R16G16_UNORM: case DXGI_FORMAT_R16G16_FLOAT: case DXGI_FORMAT_R32_FLOAT:
        *block = 1; *bytes = 4; return 1;
    case DXGI_FORMAT_R16G16B16A16_FLOAT: *block = 1; *bytes = 8; return 1;
    case DXGI_FORMAT_R32G32B32A32_FLOAT: *block = 1; *bytes = 16; return 1;
    default: return 0;
    }
}
static size_t rr_level_size(DXGI_FORMAT f, u32 w, u32 h)
{
    u32 blk, by; if (!rr_fmt_info(f, &blk, &by)) return 0;
    if (blk == 4) return (size_t)((w + 3) / 4 ? (w + 3) / 4 : 1) * ((h + 3) / 4 ? (h + 3) / 4 : 1) * by;
    return (size_t)w * h * by;
}
static DXGI_FORMAT rr_unorm(u32 f)
{
    switch (f) {
    case 72: return DXGI_FORMAT_BC1_UNORM; case 75: return DXGI_FORMAT_BC2_UNORM;
    case 78: return DXGI_FORMAT_BC3_UNORM; case 99: return DXGI_FORMAT_BC7_UNORM;
    case 29: return DXGI_FORMAT_R8G8B8A8_UNORM; case 91: return DXGI_FORMAT_B8G8R8A8_UNORM;
    default: return (DXGI_FORMAT)f;
    }
}

static void rr_load_dds(rr_hd_entry* e)
{
    FILE* fp = fopen(e->path, "rb");
    if (!fp) goto fail;
    rr_dds_header hd;
    if (fread(&hd, sizeof hd, 1, fp) != 1 || hd.magic != 0x20534444u || hd.size != 124) goto fail_close;
    DXGI_FORMAT f = DXGI_FORMAT_UNKNOWN;
    if (hd.pf_flags & 4) {                               /* FourCC */
        if (hd.fourcc == 0x30315844u) {                  /* "DX10" */
            rr_dds_dx10 x; if (fread(&x, sizeof x, 1, fp) != 1 || x.dim != 3 || x.array > 1) goto fail_close;
            f = rr_unorm(x.dxgi);
        } else if (hd.fourcc == 0x31545844u) f = DXGI_FORMAT_BC1_UNORM;     /* DXT1 */
        else if (hd.fourcc == 0x33545844u) f = DXGI_FORMAT_BC2_UNORM;       /* DXT3 */
        else if (hd.fourcc == 0x35545844u) f = DXGI_FORMAT_BC3_UNORM;       /* DXT5 */
    } else if ((hd.pf_flags & 0x40) && hd.rgb_bits == 32) {
        f = hd.rmask == 0x000000FFu ? DXGI_FORMAT_R8G8B8A8_UNORM : DXGI_FORMAT_B8G8R8A8_UNORM;
    }
    if (hd.caps2 & 0x200) goto fail_close;               /* cube maps: not replaced */
    u32 blk, by;
    if (!rr_fmt_info(f, &blk, &by) || !hd.width || !hd.height || hd.width > 16384 || hd.height > 16384) goto fail_close;
    u32 mips = (hd.flags & 0x20000) && hd.mips ? hd.mips : 1;
    size_t total = 0;
    for (u32 l = 0, w = hd.width, h = hd.height; l < mips; l++) {
        total += rr_level_size(f, w, h);
        w = w > 1 ? w >> 1 : 1; h = h > 1 ? h >> 1 : 1;
    }
    u8* data = (u8*)malloc(total);
    if (!data) goto fail_close;
    if (fread(data, 1, total, fp) != total) { free(data); goto fail_close; }
    fclose(fp);
    e->data = data; e->size = total; e->fmt = f; e->w = hd.width; e->h = hd.height; e->mips = mips;
    InterlockedExchange(&e->state, RR_HD_CPU);
    return;
fail_close:
    fclose(fp);
fail:
    fprintf(stderr, "[RR-TEX] could not load %s (unsupported or damaged DDS)\n", e->path);
    InterlockedExchange(&e->state, RR_HD_FAILED);
}

/* ---------------------------------------------------------------- dump */
typedef struct { u64 hash; u32 w, h, fmt_guest; DXGI_FORMAT dx; u32 row, rows; u8* pix; } rr_dump_job;

static void rr_write_dump(rr_dump_job* d)
{
    char path[MAX_PATH];
    if (snprintf(path, sizeof path, "%s\\%016llX_%ux%u_%02X.dds", s_rrt.dump_dir,
                 (unsigned long long)d->hash, d->w, d->h, d->fmt_guest) >= (int)sizeof path) return;
    char tmp[MAX_PATH + 8]; snprintf(tmp, sizeof tmp, "%s.part", path);
    FILE* fp = fopen(tmp, "wb");
    if (!fp) return;
    rr_dds_header hd; memset(&hd, 0, sizeof hd);
    u32 blk = 1, by = 4; rr_fmt_info(d->dx, &blk, &by);
    hd.magic = 0x20534444u; hd.size = 124;
    hd.flags = 0x1 | 0x2 | 0x4 | 0x1000 | (blk == 4 ? 0x80000 : 0x8);
    hd.height = d->h; hd.width = d->w; hd.pitch = d->row; hd.mips = 1;
    hd.pf_size = 32; hd.pf_flags = 4; hd.fourcc = 0x30315844u; hd.caps = 0x1000;
    rr_dds_dx10 x = { (u32)d->dx, 3, 0, 1, 0 };
    int ok = fwrite(&hd, sizeof hd, 1, fp) == 1 && fwrite(&x, sizeof x, 1, fp) == 1
          && fwrite(d->pix, (size_t)d->row * d->rows, 1, fp) == 1;
    ok = (fclose(fp) == 0) && ok;
    if (ok && MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING)) InterlockedIncrement((volatile LONG*)&s_rrt.n_dumped);
    else DeleteFileA(tmp);
}

static void rr_tex_run_job(rr_tex_job* j)
{
    if (j->kind == 0) { rr_dump_job* d = (rr_dump_job*)j->arg; rr_write_dump(d); free(d->pix); free(d); }
    else rr_load_dds((rr_hd_entry*)j->arg);
}

/* Called with the decoded upload rows (pitch-aligned) still mapped. */
static void rr_tex_dump(u64 hash, const u8* mapped, u32 pitch, u32 w, u32 h, u32 fmt, DXGI_FORMAT dx)
{
    if (!s_rrt.dump || w < 16 || h < 16 || !rr_dumped_add(hash)) return;
    u32 blk, by; if (!rr_fmt_info(dx, &blk, &by)) return;
    u32 row = blk == 4 ? ((w + 3) / 4) * by : w * by;
    u32 rows = blk == 4 ? (h + 3) / 4 : h;
    rr_dump_job* d = (rr_dump_job*)malloc(sizeof *d);
    if (!d) return;
    d->pix = (u8*)malloc((size_t)row * rows);
    if (!d->pix) { free(d); return; }
    for (u32 r = 0; r < rows; r++) memcpy(d->pix + (size_t)r * row, mapped + (size_t)r * pitch, row);
    d->hash = hash; d->w = w; d->h = h; d->fmt_guest = fmt & 0xFF; d->dx = dx; d->row = row; d->rows = rows;
    if (!rr_tex_push(&s_rrt.dumpq, 0, d, 4096)) { free(d->pix); free(d); }   /* never stall */
}

/* ---------------------------------------------------------------- replace */
/* Returns the HD resource for hash once it is on the GPU, else NULL (and starts
 * loading it). Must run on the render thread with s_d3d.cmd_list open. */
static ID3D12Resource* rr_tex_replacement(u64 hash, DXGI_FORMAT* fmt, u32* mips)
{
    if (!s_rrt.replace) return NULL;
    rr_hd_entry* e = rr_hd_find(hash);
    if (!e) return NULL;
    LONG st = e->state;
    if (st == RR_HD_GPU) {
        if (e->up && (u64)s_d3d.frame_count > e->up_frame + 8) { e->up->lpVtbl->Release(e->up); e->up = NULL; }
        *fmt = e->fmt; *mips = e->mips; return e->res;
    }
    if (st == RR_HD_INDEXED) {
        if (InterlockedCompareExchange(&e->state, RR_HD_LOADING, RR_HD_INDEXED) == RR_HD_INDEXED
            && !rr_tex_push(&s_rrt.loadq, 1, e, 0))
            InterlockedExchange(&e->state, RR_HD_INDEXED);
        return NULL;
    }
    if (st != RR_HD_CPU) return NULL;
    /* CPU data ready: create and upload on this thread. */
    D3D12_HEAP_PROPERTIES hp = {0}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {0};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = e->w; td.Height = e->h; td.DepthOrArraySize = 1; td.MipLevels = (UINT16)e->mips;
    td.Format = e->fmt; td.SampleDesc.Count = 1; td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp[16]; UINT nrows[16]; UINT64 rowsz[16], total = 0;
    if (e->mips > 16) goto fail;
    s_d3d.device->lpVtbl->GetCopyableFootprints(s_d3d.device, &td, 0, e->mips, 0, fp, nrows, rowsz, &total);
    if (FAILED(s_d3d.device->lpVtbl->CreateCommittedResource(s_d3d.device, &hp, D3D12_HEAP_FLAG_NONE, &td,
            D3D12_RESOURCE_STATE_COPY_DEST, NULL, &IID_ID3D12Resource, (void**)&e->res))) goto fail;
    D3D12_HEAP_PROPERTIES hu = {0}; hu.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC bd = {0};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; bd.Width = total; bd.Height = 1;
    bd.DepthOrArraySize = 1; bd.MipLevels = 1; bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (FAILED(s_d3d.device->lpVtbl->CreateCommittedResource(s_d3d.device, &hu, D3D12_HEAP_FLAG_NONE, &bd,
            D3D12_RESOURCE_STATE_GENERIC_READ, NULL, &IID_ID3D12Resource, (void**)&e->up))) goto fail;
    u8* map = NULL; D3D12_RANGE nr = {0, 0};
    if (FAILED(e->up->lpVtbl->Map(e->up, 0, &nr, (void**)&map)) || !map) goto fail;
    { size_t src = 0;
      for (u32 l = 0; l < e->mips; l++) {
          for (UINT r = 0; r < nrows[l]; r++)
              memcpy(map + fp[l].Offset + (size_t)r * fp[l].Footprint.RowPitch, e->data + src + (size_t)r * rowsz[l], (size_t)rowsz[l]);
          src += (size_t)rowsz[l] * nrows[l];
      } }
    e->up->lpVtbl->Unmap(e->up, 0, NULL);
    for (u32 l = 0; l < e->mips; l++) {
        D3D12_TEXTURE_COPY_LOCATION dst = {0}, s = {0};
        dst.pResource = e->res; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = l;
        s.pResource = e->up; s.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; s.PlacedFootprint = fp[l];
        s_d3d.cmd_list->lpVtbl->CopyTextureRegion(s_d3d.cmd_list, &dst, 0, 0, 0, &s, NULL);
    }
    { D3D12_RESOURCE_BARRIER b = {0};
      b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; b.Transition.pResource = e->res;
      b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
      b.Transition.StateAfter = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
      b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
      s_d3d.cmd_list->lpVtbl->ResourceBarrier(s_d3d.cmd_list, 1, &b); }
    free(e->data); e->data = NULL;
    e->up_frame = (u64)s_d3d.frame_count;
    InterlockedExchange(&e->state, RR_HD_GPU);
    if (++s_rrt.n_replaced <= 8 || (s_rrt.n_replaced % 100) == 0)
        printf("[RR-TEX] HD texture %016llX loaded (%ux%u, %u mips) - %u replaced so far\n",
               (unsigned long long)hash, e->w, e->h, e->mips, s_rrt.n_replaced);
    *fmt = e->fmt; *mips = e->mips;
    return NULL;   /* copies are recorded this frame; switch on the next bind */
fail:
    if (e->res) { e->res->lpVtbl->Release(e->res); e->res = NULL; }
    if (e->up) { e->up->lpVtbl->Release(e->up); e->up = NULL; }
    free(e->data); e->data = NULL;
    InterlockedExchange(&e->state, RR_HD_FAILED);
    fprintf(stderr, "[RR-TEX] could not create HD texture %016llX on the GPU\n", (unsigned long long)hash);
    return NULL;
}

/* Write a view of an HD resource into heap slot heap_slot. */
static void rr_tex_write_view(u32 heap_slot, ID3D12Resource* res, DXGI_FORMAT fmt, u32 mips, UINT mapping)
{
    D3D12_SHADER_RESOURCE_VIEW_DESC sv = {0};
    sv.Format = fmt; sv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sv.Shader4ComponentMapping = mapping; sv.Texture2D.MipLevels = mips;
    D3D12_CPU_DESCRIPTOR_HANDLE sh;
    s_d3d.srv_heap->lpVtbl->GetCPUDescriptorHandleForHeapStart(s_d3d.srv_heap, &sh);
    sh.ptr += (u64)heap_slot * s_d3d.srv_inc;
    s_d3d.device->lpVtbl->CreateShaderResourceView(s_d3d.device, res, &sv, sh);
}

#endif /* RR_TEXTURES_H */
