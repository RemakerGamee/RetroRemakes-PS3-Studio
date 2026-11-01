/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Windows system-cache correction. Old cache bytes are retained, never deleted.
 * Original implementation, independent of RPCS3 source; API semantics checked
 * against RPCS3 cellSysCacheClear (mounted cache remains mounted).
 */
#ifndef RR_SYSCACHE_HOST_H
#define RR_SYSCACHE_HOST_H
#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>

static int rr_cache_id_valid(const char* id)
{
    size_t n = strlen(id);
    if (!n || n > 31 || !strcmp(id,".") || !strcmp(id,"..")) return 0;
    for (size_t i=0;i<n;i++)
        if (!((id[i]>='a'&&id[i]<='z')||(id[i]>='A'&&id[i]<='Z')||
              (id[i]>='0'&&id[i]<='9')||id[i]=='_'||id[i]=='-')) return 0;
    return 1;
}

/* Only existing local absolute non-reparse directory ancestors are accepted.
 * There is no recursion and no file deletion, even for links inside a cache.
 */
static int rr_cache_plain_directory(const char* path)
{
    char full[1024];
    DWORD n=GetFullPathNameA(path,sizeof full,full,NULL);
    if (!n || n>=sizeof full || n>MAX_PATH-72 || n<3 || full[1]!=':' || full[2]!='\\') return 0;
    for (DWORD i=3;i<=n;i++) {
        if (full[i]!='\\' && full[i]!='/' && full[i]!=0) continue;
        char saved=full[i];full[i]=0;
        DWORD attr=GetFileAttributesA(full);
        full[i]=saved;
        if (attr==INVALID_FILE_ATTRIBUTES || (attr&FILE_ATTRIBUTE_REPARSE_POINT) || !(attr&FILE_ATTRIBUTE_DIRECTORY)) return 0;
    }
    return 1;
}

static int rr_cache_paths(const char* root,const char* id,char* parent,size_t pc,char* mount,size_t mc)
{
    if (!root || !*root || !rr_cache_id_valid(id) || !rr_cache_plain_directory(root)) return 0;
    int a=snprintf(parent,pc,"%s/cache",root),b=snprintf(mount,mc,"%s/cache/%s",root,id);
    if (a<0 || (size_t)a>=pc || b<0 || (size_t)b>=mc) return 0;
    return rr_cache_plain_directory(parent) && rr_cache_plain_directory(mount);
}

static int rr_cache_rotate(const char* root,const char* id,char* retained,size_t cap)
{
    char parent[1024],mount[1024],backup[1100];
    if (!rr_cache_paths(root,id,parent,sizeof parent,mount,sizeof mount)) return 0;
    /* Per-path mutex serializes this corrected runtime's cache operations. It
     * does not claim ownership of older runtimes, which do not use this lease.
     */
    unsigned long hash=2166136261u;
    char canonical[1024];GetFullPathNameA(mount,sizeof canonical,canonical,NULL);
    for (const char* p=canonical;*p;p++) {unsigned char c=(unsigned char)*p;if(c>='A'&&c<='Z')c+=32;hash=(hash^c)*16777619u;}
    char mutex_name[96];snprintf(mutex_name,sizeof mutex_name,"Local\\RetroRemakes.SysCache.%08lX",hash);
    HANDLE mutex=CreateMutexA(NULL,FALSE,mutex_name);
    if (!mutex) return 0;
    DWORD wait=WaitForSingleObject(mutex,0);
    if (wait!=WAIT_OBJECT_0 && wait!=WAIT_ABANDONED) {CloseHandle(mutex);return 0;}
    int ok=0;
    if (!rr_cache_paths(root,id,parent,sizeof parent,mount,sizeof mount)) goto done;
    for (unsigned i=0;i<128;i++) {
        int n=snprintf(backup,sizeof backup,"%s/.previous-%s-%lu-%llu-%u",parent,id,
                       (unsigned long)GetCurrentProcessId(),(unsigned long long)GetTickCount64(),i);
        if (n<0 || (size_t)n>=sizeof backup || (size_t)n+1>cap) goto done;
        if (GetFileAttributesA(backup)!=INVALID_FILE_ATTRIBUTES) continue;
        if (!MoveFileExA(mount,backup,0)) goto done;
        if (!CreateDirectoryA(mount,NULL)) {
            /* No success is reported unless the replacement mount exists.
             * Roll back the old directory when recreation fails.
             */
            MoveFileExA(backup,mount,0);goto done;
        }
        strcpy(retained,backup);ok=1;break;
    }
done:
    ReleaseMutex(mutex);CloseHandle(mutex);return ok;
}
#else
static int rr_cache_id_valid(const char* id)
{
    size_t n = strlen(id);
    if (!n || n > 31 || !strcmp(id,".") || !strcmp(id,"..")) return 0;
    for (size_t i=0;i<n;i++)
        if (!((id[i]>='a'&&id[i]<='z')||(id[i]>='A'&&id[i]<='Z')||
              (id[i]>='0'&&id[i]<='9')||id[i]=='_'||id[i]=='-')) return 0;
    return 1;
}
static int rr_cache_rotate(const char* root,const char* id,char* out,size_t cap)
{ (void)root;(void)id;(void)out;(void)cap;return 0; }
#endif
#endif
