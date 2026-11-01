/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Display options for native builds.
 *
 * Read once from environment variables, then from display.ini beside the
 * executable (KEY=VALUE lines, '#' comments).  Environment wins.  With
 * nothing set every option keeps the original behaviour.
 *
 *   RR_FPS_LIMIT   0 = no limiter (default) | 30 | 60 | 120 | 144 | 165 | 240 ...
 *   RR_VSYNC       1 = wait for the monitor refresh (default) | 0 = off (tearing when supported)
 *   RR_OUTPUT      window size, e.g. 1920x1080, 2560x1440, 3440x1440, 3840x2160
 *   RR_ASPECT      keep (default when RR_OUTPUT set) | stretch | pillarbox (same as keep)
 *   RR_FULLSCREEN  0 (default) | 1 = borderless window covering the monitor
 *   RR_ANISO       1 (default) | 2 | 4 | 8 | 16 anisotropic filtering on game textures
 *   RR_VBLANK_HZ   60 (default) guest vertical-blank rate; see DISPLAY-OPTIONS.md
 *   RR_AA          off (default) | fxaa  edge anti-aliasing on the final image
 *   RR_SHARPEN     0 (default) .. 100   adaptive sharpening on the final image
 *   RR_DITHER      0 (default) | 1      dither the output to hide colour banding
 *   RR_RES_SCALE   read and reported, not applied yet (see DISPLAY-OPTIONS.md).
 */
#ifndef RR_DISPLAY_OPTIONS_H
#define RR_DISPLAY_OPTIONS_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#ifdef _WIN32
#include <windows.h>
#endif

typedef struct rr_display_options {
    int fps_limit;       /* 0 = off */
    int vsync;           /* 1 = on */
    unsigned out_w, out_h;
    int aspect;          /* 0 keep, 1 stretch */
    int fullscreen;
    int aniso;
    int vblank_hz;
    int res_scale;       /* percent; reserved */
    int aa;              /* 0 off, 1 fxaa */
    int dither;          /* 0 off, 1 on */
    int sharpen;         /* 0..100 */
} rr_display_options;

static void rr_display__set(rr_display_options* o, const char* k, const char* v)
{
    if (!k || !v) return;
    if (!strcmp(k, "RR_FPS_LIMIT")) o->fps_limit = atoi(v) < 0 ? 0 : atoi(v);
    else if (!strcmp(k, "RR_VSYNC")) o->vsync = atoi(v) ? 1 : 0;
    else if (!strcmp(k, "RR_OUTPUT")) {
        unsigned w = 0, h = 0;
        if (sscanf(v, "%ux%u", &w, &h) == 2 && w >= 320 && h >= 200 && w <= 7680 && h <= 4320) { o->out_w = w; o->out_h = h; }
    }
    else if (!strcmp(k, "RR_ASPECT")) o->aspect = !strcmp(v, "stretch") ? 1 : 0;
    else if (!strcmp(k, "RR_FULLSCREEN")) o->fullscreen = atoi(v) ? 1 : 0;
    else if (!strcmp(k, "RR_ANISO")) { int a = atoi(v); o->aniso = a >= 16 ? 16 : a >= 8 ? 8 : a >= 4 ? 4 : a >= 2 ? 2 : 1; }
    else if (!strcmp(k, "RR_VBLANK_HZ")) { int h = atoi(v); o->vblank_hz = (h >= 30 && h <= 480) ? h : 60; }
    else if (!strcmp(k, "RR_RES_SCALE")) { int s = atoi(v); o->res_scale = (s >= 50 && s <= 400) ? s : 100; }
    else if (!strcmp(k, "RR_AA")) o->aa = !strcmp(v, "fxaa") ? 1 : 0;
    else if (!strcmp(k, "RR_DITHER")) o->dither = atoi(v) ? 1 : 0;
    else if (!strcmp(k, "RR_SHARPEN")) { int s = atoi(v); o->sharpen = s < 0 ? 0 : s > 100 ? 100 : s; }
}

static const rr_display_options* rr_display_options_get(void)
{
    static rr_display_options o;
    static int loaded = 0;
    static const char* keys[] = { "RR_FPS_LIMIT", "RR_VSYNC", "RR_OUTPUT", "RR_ASPECT", "RR_FULLSCREEN",
                                  "RR_ANISO", "RR_VBLANK_HZ", "RR_RES_SCALE", "RR_AA", "RR_DITHER", "RR_SHARPEN" };
    if (loaded) return &o;
    loaded = 1;
    memset(&o, 0, sizeof o);
    o.vsync = 1; o.aniso = 1; o.vblank_hz = 60; o.res_scale = 100;
#ifdef _WIN32
    {   /* display.ini beside the executable */
        char path[MAX_PATH]; DWORD n = GetModuleFileNameA(NULL, path, MAX_PATH);
        if (n && n < MAX_PATH) {
            char* slash = strrchr(path, '\\');
            if (slash && (size_t)(slash - path) + 13 < MAX_PATH) {
                strcpy(slash + 1, "display.ini");
                FILE* f = fopen(path, "r");
                if (f) {
                    char line[256];
                    while (fgets(line, sizeof line, f)) {
                        char* eq = strchr(line, '=');
                        if (line[0] == '#' || line[0] == ';' || !eq) continue;
                        *eq = 0; char* k = line; char* v = eq + 1;
                        while (*k && isspace((unsigned char)*k)) k++;
                        for (char* e = k + strlen(k); e > k && isspace((unsigned char)e[-1]); ) *--e = 0;
                        while (*v && isspace((unsigned char)*v)) v++;
                        for (char* e = v + strlen(v); e > v && isspace((unsigned char)e[-1]); ) *--e = 0;
                        rr_display__set(&o, k, v);
                    }
                    fclose(f);
                }
            }
        }
    }
#endif
    for (size_t i = 0; i < sizeof keys / sizeof keys[0]; i++)
        rr_display__set(&o, keys[i], getenv(keys[i]));
    return &o;
}

#endif
