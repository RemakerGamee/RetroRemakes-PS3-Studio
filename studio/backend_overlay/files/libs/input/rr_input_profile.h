/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* RetroRemakes local controller-profile extension. Windows host input only.
 * SDL2 API is loaded from the application's own SDL2.dll; no renderer/window ownership changes.
 * Uses the existing ps3recomp PadHostState and retains its PS3 button layout. */
#ifndef RR_PS3_INPUT_PROFILE_H
#define RR_PS3_INPUT_PROFILE_H
#include <limits.h>
typedef struct { char kind[24]; int device,deadzone,threshold,invert_l,invert_r,rumble,map[16]; } RrInput;
static RrInput rr_input;
static SRWLOCK rr_input_lock=SRWLOCK_INIT;
static char rr_profile[MAX_PATH*4];
static ULONGLONG rr_profile_checked;
static int rr_profile_started;
static HMODULE rr_sdl;
static void* rr_pad;
static ULONGLONG rr_detect_after;
static ULONGLONG rr_rumble_sent;
static int (*rr_sdl_init)(unsigned);
static int (*rr_sdl_count)(void);
static int (*rr_sdl_is_pad)(int);
static void* (*rr_sdl_open)(int);
static void (*rr_sdl_close)(void*);
static int (*rr_sdl_attached)(void*);
static unsigned short (*rr_sdl_vendor)(void*);
static unsigned short (*rr_sdl_product)(void*);
static void (*rr_sdl_update)(void);
static void (*rr_sdl_pump)(void);
static unsigned char (*rr_sdl_button)(void*,int);
static short (*rr_sdl_axis)(void*,int);
static int (*rr_sdl_rumble)(void*,unsigned short,unsigned short,unsigned);
static int rr_number(const char* key,int fallback,int lo,int hi) {
    int n=(int)GetPrivateProfileIntA("controller",key,fallback,rr_profile);
    return n>=lo && n<=hi?n:fallback;
}
static void rr_input_load(void) {
    ULONGLONG now=GetTickCount64();
    if(rr_profile_started && now-rr_profile_checked<1000)return;
    rr_profile_checked=now;
    if(!rr_profile_started) {
        const char* configured=getenv("RR_PS3_INPUT_PROFILE");
        if(configured && *configured)snprintf(rr_profile,sizeof rr_profile,"%s",configured);
        else {
            char base[sizeof rr_profile];GetModuleFileNameA(NULL,base,sizeof base);
            char* slash=strrchr(base,'\\');if(slash)*slash=0;
            for(int i=0;i<9;i++) {
                snprintf(rr_profile,sizeof rr_profile,"%s\\controller-profiles\\default.ini",base);
                if(GetFileAttributesA(rr_profile)!=INVALID_FILE_ATTRIBUTES)break;
                slash=strrchr(base,'\\');if(!slash){rr_profile[0]=0;break;}*slash=0;
            }
        }
        rr_profile_started=1;
    }
    char old_kind[24];snprintf(old_kind,sizeof old_kind,"%s",rr_input.kind);int old_device=rr_input.device;
    GetPrivateProfileStringA("controller","type","auto",rr_input.kind,sizeof rr_input.kind,rr_profile);
    rr_input.device=rr_number("device",0,0,3);rr_input.deadzone=rr_number("deadzone",7849,0,20000);
    rr_input.threshold=rr_number("trigger_threshold",30,0,254);
    rr_input.invert_l=rr_number("invert_left_y",0,0,1);rr_input.invert_r=rr_number("invert_right_y",0,0,1);
    rr_input.rumble=rr_number("rumble",1,0,1);
    if(rr_pad && !rr_input.rumble){rr_sdl_rumble(rr_pad,0,0,0);rr_rumble_sent=0;}
    if(rr_pad && (old_device!=rr_input.device || strcmp(old_kind,rr_input.kind))){rr_sdl_rumble(rr_pad,0,0,0);rr_sdl_close(rr_pad);rr_pad=NULL;}
    for(int i=0;i<16;i++){char key[24];snprintf(key,sizeof key,"map_%d",i);rr_input.map[i]=rr_number(key,i,-1,15);}
}
static int rr_sdl_ready(void) {
    static int failed=0;if(rr_sdl)return 1;if(failed)return 0;failed=1;
    char dll[MAX_PATH*4];GetModuleFileNameA(NULL,dll,sizeof dll);char* slash=strrchr(dll,'\\');
    if(!slash)return 0;strcpy(slash+1,"SDL2.dll");
    rr_sdl=LoadLibraryExA(dll,NULL,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if(!rr_sdl)return 0;
#define RR_SDL_LOAD(var,name) do { *(FARPROC*)&var=GetProcAddress(rr_sdl,name); if(!var){FreeLibrary(rr_sdl);rr_sdl=NULL;return 0;} }while(0)
    RR_SDL_LOAD(rr_sdl_init,"SDL_InitSubSystem");RR_SDL_LOAD(rr_sdl_count,"SDL_NumJoysticks");
    RR_SDL_LOAD(rr_sdl_is_pad,"SDL_IsGameController");RR_SDL_LOAD(rr_sdl_open,"SDL_GameControllerOpen");
    RR_SDL_LOAD(rr_sdl_close,"SDL_GameControllerClose");RR_SDL_LOAD(rr_sdl_attached,"SDL_GameControllerGetAttached");
    RR_SDL_LOAD(rr_sdl_vendor,"SDL_GameControllerGetVendor");RR_SDL_LOAD(rr_sdl_product,"SDL_GameControllerGetProduct");
    RR_SDL_LOAD(rr_sdl_update,"SDL_GameControllerUpdate");RR_SDL_LOAD(rr_sdl_pump,"SDL_PumpEvents");
    RR_SDL_LOAD(rr_sdl_button,"SDL_GameControllerGetButton");RR_SDL_LOAD(rr_sdl_axis,"SDL_GameControllerGetAxis");
    RR_SDL_LOAD(rr_sdl_rumble,"SDL_GameControllerRumble");
#undef RR_SDL_LOAD
    if(rr_sdl_init(0x2000)!=0){FreeLibrary(rr_sdl);rr_sdl=NULL;return 0;}
    return 1;
}
static u8 rr_axis(short raw,int invert) {
    int value=raw;float n=0;int dz=rr_input.deadzone;
    if(value>dz)n=(float)(value-dz)/(32767-dz);
    else if(value < -dz)n=(float)(value+dz)/(32768-dz);
    if(invert)n=-n;int result=128+(int)(n*(n<0?128:127));
    return (u8)(result<0?0:result>255?255:result);
}
static void rr_pressure(PadHostState* p) {
    p->press_up=(p->buttons&CELL_PAD_CTRL_UP)?255:0;p->press_down=(p->buttons&CELL_PAD_CTRL_DOWN)?255:0;
    p->press_left=(p->buttons&CELL_PAD_CTRL_LEFT)?255:0;p->press_right=(p->buttons&CELL_PAD_CTRL_RIGHT)?255:0;
    p->press_cross=(p->buttons&CELL_PAD_CTRL_CROSS)?255:0;p->press_circle=(p->buttons&CELL_PAD_CTRL_CIRCLE)?255:0;
    p->press_triangle=(p->buttons&CELL_PAD_CTRL_TRIANGLE)?255:0;p->press_square=(p->buttons&CELL_PAD_CTRL_SQUARE)?255:0;
    p->press_l1=(p->buttons&CELL_PAD_CTRL_L1)?255:0;p->press_r1=(p->buttons&CELL_PAD_CTRL_R1)?255:0;
}
static int rr_poll_sony(PadHostState* p) {
    if(!rr_sdl_ready())return 0;
    rr_sdl_pump();rr_sdl_update();
    if(rr_pad && !rr_sdl_attached(rr_pad)){rr_sdl_close(rr_pad);rr_pad=NULL;}
    if(!rr_pad && GetTickCount64()>=rr_detect_after){
        rr_detect_after=GetTickCount64()+1000;int found=0;
        for(int i=0;i<rr_sdl_count();i++){
            if(!rr_sdl_is_pad(i))continue;void* pad=rr_sdl_open(i);if(!pad)continue;
            unsigned vendor=rr_sdl_vendor(pad),product=rr_sdl_product(pad);
            if((!strcmp(rr_input.kind,"sdl") || (vendor==0x054c && (product==0x05c4||product==0x09cc||product==0x0ba0))) && found++==rr_input.device){rr_pad=pad;break;}
            rr_sdl_close(pad);
        }
    }
    if(!rr_pad)return 0;
    if(rr_input.rumble && (s_rumble[0].lo||s_rumble[0].hi) && GetTickCount64()-rr_rumble_sent>=400){rr_sdl_rumble(rr_pad,s_rumble[0].lo,s_rumble[0].hi,1000);rr_rumble_sent=GetTickCount64();}
    memset(p,0,sizeof *p);p->connected=1;
    static const int pairs[][2]={{0,14},{1,13},{2,15},{3,12},{4,0},{6,3},{7,1},{8,2},{9,10},{10,11},{11,4},{12,6},{13,7},{14,5}};
    for(unsigned i=0;i<sizeof pairs/sizeof pairs[0];i++)if(rr_sdl_button(rr_pad,pairs[i][0]))p->buttons|=(u16)(1u<<pairs[i][1]);
    p->trigger_l2=(u8)(((int)rr_sdl_axis(rr_pad,4)*255)/32767);p->trigger_r2=(u8)(((int)rr_sdl_axis(rr_pad,5)*255)/32767);
    if(p->trigger_l2>rr_input.threshold)p->buttons|=CELL_PAD_CTRL_L2;
    if(p->trigger_r2>rr_input.threshold)p->buttons|=CELL_PAD_CTRL_R2;
    p->analog_lx=rr_axis(rr_sdl_axis(rr_pad,0),0);p->analog_ly=rr_axis(rr_sdl_axis(rr_pad,1),rr_input.invert_l);
    p->analog_rx=rr_axis(rr_sdl_axis(rr_pad,2),0);p->analog_ry=rr_axis(rr_sdl_axis(rr_pad,3),rr_input.invert_r);
    return 1;
}
static void rr_remap(PadHostState* p) {
    u16 original=p->buttons,mapped=0;u8 l2=p->trigger_l2,r2=p->trigger_r2;
    for(int i=0;i<16;i++)if(rr_input.map[i]>=0 && (original&(1u<<rr_input.map[i])))mapped|=(u16)(1u<<i);
    int lm=rr_input.map[8],rm=rr_input.map[9];p->buttons=mapped;
    p->trigger_l2=lm==8?l2:lm==9?r2:(mapped&CELL_PAD_CTRL_L2)?255:0;
    p->trigger_r2=rm==8?l2:rm==9?r2:(mapped&CELL_PAD_CTRL_R2)?255:0;
    rr_pressure(p);
}

static void rr_input_apply_locked(void) {
    rr_input_load();PadHostState* p=&s_host_state[0];
    XINPUT_STATE raw;int has_x=XInputGetState((DWORD)rr_input.device,&raw)==ERROR_SUCCESS;
    if(!strcmp(rr_input.kind,"keyboard")){memset(p,0,sizeof *p);pad_poll_keyboard();rr_pressure(p);return;}
    if(!strcmp(rr_input.kind,"ds4")||!strcmp(rr_input.kind,"sdl")||(!strcmp(rr_input.kind,"auto")&&!has_x)){
        if(!rr_poll_sony(p) && strcmp(rr_input.kind,"auto")){memset(p,0,sizeof *p);p->analog_lx=p->analog_ly=p->analog_rx=p->analog_ry=128;return;}
    }else if(has_x){
        *p=s_host_state[rr_input.device];p->connected=1;
        p->analog_lx=rr_axis(raw.Gamepad.sThumbLX,0);p->analog_ly=rr_axis(raw.Gamepad.sThumbLY,!rr_input.invert_l);
        p->analog_rx=rr_axis(raw.Gamepad.sThumbRX,0);p->analog_ry=rr_axis(raw.Gamepad.sThumbRY,!rr_input.invert_r);
        p->buttons&=~(CELL_PAD_CTRL_L2|CELL_PAD_CTRL_R2);
        if(p->trigger_l2>rr_input.threshold)p->buttons|=CELL_PAD_CTRL_L2;
        if(p->trigger_r2>rr_input.threshold)p->buttons|=CELL_PAD_CTRL_R2;
    }else if(!strcmp(rr_input.kind,"xinput")){memset(p,0,sizeof *p);p->analog_lx=p->analog_ly=p->analog_rx=p->analog_ry=128;return;}
    rr_remap(p);
}

static int rr_input_rumble_locked(u32 port,u16 lo,u16 hi) {
    if(port!=0)return 0;rr_input_load();if(!rr_input.rumble)lo=hi=0;
    if(rr_pad && rr_sdl_attached(rr_pad)){rr_sdl_rumble(rr_pad,lo,hi,1000);rr_rumble_sent=GetTickCount64();return 1;}
    if(!rr_input.rumble || rr_input.device!=0){XINPUT_VIBRATION vib={lo,hi};XInputSetState((DWORD)rr_input.device,&vib);return 1;}
    return 0;
}
static void rr_input_apply(void){AcquireSRWLockExclusive(&rr_input_lock);rr_input_apply_locked();ReleaseSRWLockExclusive(&rr_input_lock);}
static int rr_input_rumble(u32 port,u16 lo,u16 hi){AcquireSRWLockExclusive(&rr_input_lock);int result=rr_input_rumble_locked(port,lo,hi);ReleaseSRWLockExclusive(&rr_input_lock);return result;}
#endif
