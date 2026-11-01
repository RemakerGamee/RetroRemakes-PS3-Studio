# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Selected-game verified fixes, isolated from the Studio build queue.

No AI service, uploads, game launch, active-attempt edits or shared state writes
occur in the worker. Main-thread registration creates only immutable sidecars;
selected-candidate promotion requires an idle queue and unchanged selection.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from .core import Studio, Cancelled, choose_elf, sha256, read_json
from . import private_native_adapter as adapter

class GameFixError(RuntimeError):
    pass

_FIELDS = ('id','source','elf','attempt','native_exe','native_runtime_receipt','status')
_COMPLETED = 'compiled; gameplay unverified'

# Runtime fixes (any game): recompile a finished attempt's existing sources
# against the current runtime, refreshing port/main.cpp only when it is the
# untouched pre-PARAM.SFO template (never a hand-edited one), plus any exact-
# input adapter (Killzone 2's WWS policy module) core.build applies itself.
_OLD_TEMPLATE_MAIN_SHA256 = {'367c7f45fe969e8e6101a38c47f0a7f48306093342189fa0e11ed84c9e157c86'}

def _atomic(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
        # Windows refuses a replace while another process (the Studio's
        # progress watcher, antivirus) has the target open; retry briefly.
        for attempt in range(20):
            try:
                os.replace(temp,path);break
            except PermissionError:
                if attempt==19:raise
                time.sleep(0.05*(attempt+1))
    finally:
        temp.unlink(missing_ok=True)

def _project(studio, value):
    ident=value.get('id') if isinstance(value,dict) else str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',str(ident)):
        raise GameFixError('Invalid selected game identity')
    return next((p for p in studio.state.get('projects',[]) if p.get('id')==ident),None)

def _token(p, settings):
    data={key:p.get(key) for key in _FIELDS}
    data['backend']=settings.get('backend')
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

def _not_link(path):
    st=path.lstat()
    if path.is_symlink() or getattr(st,'st_file_attributes',0)&0x400:
        raise GameFixError(f'Links/junctions cannot enter a fixes snapshot: {path}')

def _no_link_ancestors(path):
    for parent in [path,*path.parents]:
        if parent.exists():_not_link(parent)

def _owned_no_link_ancestors(studio, path):
    # Only an explicitly registered migration-root junction may be traversed.
    from .output_location import validate_owned_path
    try:
        validate_owned_path(studio, path)
    except (OSError, ValueError, TypeError) as error:
        raise GameFixError(str(error)) from error


def _files(root):
    _not_link(root)
    stack=[root]
    while stack:
        parent=stack.pop()
        for child in sorted(parent.iterdir()):
            _not_link(child)
            if child.is_dir():
                if child.name not in {'build','.git','__pycache__'}:stack.append(child)
            elif child.is_file() and child.name!='CMakeCache.txt':yield child
            elif not child.is_file():raise GameFixError(f'Unsupported snapshot file: {child}')

def _hash_file(path, cancel, chunk_done=None):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):
            if cancel.is_set():raise Cancelled('Fixes cancelled; original candidates preserved')
            digest.update(chunk)
            if chunk_done:chunk_done(len(chunk))
    return digest.hexdigest()


def _hash_tree(root,cancel,progress=None,stage='Checking source files'):
    paths=list(_files(root));total=sum(path.stat().st_size for path in paths);done=0
    result={}
    for index,path in enumerate(paths):
        if cancel.is_set():raise Cancelled('Fixes cancelled; original candidates preserved')
        def update(amount):
            nonlocal done
            done+=amount
            if progress:progress(stage,done,total,index,len(paths),str(path.relative_to(root)),amount)
        result[str(path.relative_to(root))]=_hash_file(path,cancel,update)
        if progress:progress(stage,done,total,index+1,len(paths),str(path.relative_to(root)),0)
    if progress:progress(stage,done,total,len(paths),len(paths),'',0)
    return result


def _copy_sources(source,dest,cancel,progress=None):
    """Copy and recheck all bytes; never weaken source mutation/link checks."""
    initial=_hash_tree(source,cancel,progress,'Checking '+source.name+' source files')
    total=sum((source/relative).stat().st_size for relative in initial)*2;done=0
    dest.mkdir(parents=True,exist_ok=True)
    for index,(relative,expected) in enumerate(initial.items()):
        if cancel.is_set():raise Cancelled('Fixes cancelled; original candidates preserved')
        src=source/relative;_not_link(src)
        target=dest/relative;target.parent.mkdir(parents=True,exist_ok=True)
        def update(amount):
            nonlocal done
            done+=amount
            if progress:progress('Copying and verifying '+source.name,done,total,index,len(initial),relative,amount)
        with src.open('rb') as incoming,target.open('xb') as outgoing:
            while chunk:=incoming.read(1024*1024):
                if cancel.is_set():raise Cancelled('Fixes cancelled; original candidates preserved')
                outgoing.write(chunk);update(len(chunk))
        if _hash_file(target,cancel,update)!=expected:raise GameFixError('Source changed during fixes snapshot')
        if progress:progress('Copying and verifying '+source.name,done,total,index+1,len(initial),relative,0)
    if _hash_tree(source,cancel,progress,'Rechecking '+source.name+' source files')!=initial:
        raise GameFixError('Source changed during fixes snapshot; retry after its operation finishes')
    return initial


class _RepairProgress:
    """Bounded sidecar telemetry; percentages measure bytes or build actions, not time."""
    def __init__(self,job,project,emit):
        self.path=job/'progress.json';self.emit=emit;self.project=project;self.job_id=job.name
        self.source_done=0;self.source_total=0;self.last_write=0;self.last_stage=None;self.value={}
    def publish(self,stage,percent=None,overall=0,detail='',status='running',force=False,**extra):
        value=dict(schema=1,project_id=self.project['id'],project_name=self.project.get('name',self.project['id']),
                   job_id=self.job_id,stage=stage,step_percent=percent,overall_percent=overall,
                   detail=detail,status=status,updated=time.time(),**extra)
        self.value=value
        now=time.monotonic()
        if not force and self.last_stage==stage and now-self.last_write<0.25:return
        self.last_write=now;self.last_stage=stage
        try:_atomic(self.path,value)
        except OSError:pass   # a progress display hiccup must never fail the build
        callback=getattr(self.emit,'repair_progress',None)
        if callback:callback(value)
    def source(self,stage,done,total,files_done,files_total,current_file,delta):
        self.source_done+=delta
        fraction=min(1.,self.source_done/max(1,self.source_total))
        percent=100*done/total if total else 100.
        self.publish(stage,percent,50*fraction,
            f'{files_done}/{files_total} files; {done:,}/{total:,} bytes in this pass'+(f' | {current_file}' if current_file else ''),
            bytes_done=done,bytes_total=total,files_done=files_done,files_total=files_total,
            source_percent=100*fraction,current_file=current_file)
    def __call__(self,message):self.emit(message)
    def progress(self,key,event,**kwargs):
        self.build_state.event(key,event,**kwargs);value=self.build_state.snapshot()
        measured='no intermediate counts' not in value['step']
        self.publish(value['step'],value['step_percent'] if measured else None,
                     50+value['overall_percent']/2,value['detail'],value['status'])


def read_progress(home, project_ids):
    """Small JSON/lock reads only; safe for a background UI watcher, no file hashes."""
    rows=[];root=Path(home)/'game-fixes'
    for ident in project_ids:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',str(ident)):continue
        folder=root/ident
        lock=read_json(folder/'.fix-worker.lock',{})
        for path in folder.glob('*/progress.json'):
            try:
                if path.stat().st_size>32768:continue
                value=read_json(path,{})
                if value.get('schema')==1 and value.get('project_id')==ident and value.get('job_id')==path.parent.name:
                    rows.append(value)
            except (OSError,ValueError,TypeError):continue
        job_id=lock.get('job_id','')
        if re.fullmatch(r'[a-f0-9]{32}',str(job_id)) and not (folder/job_id/'progress.json').exists():
            rows.append(dict(project_id=ident,job_id=job_id,project_name=ident,status='unknown',updated=(folder/'.fix-worker.lock').stat().st_mtime,
                stage='Older repair worker: source counters unavailable',step_percent=None,overall_percent=None,
                detail='A repair worker is recorded. Exact source progress requires the updated worker; the game window is separate.'))
    return sorted(rows,key=lambda value:value.get('updated',0),reverse=True)


def _pid_alive(pid):
    """Liveness without signalling: on Windows os.kill(pid, 0) TERMINATES the
    process, so query it with OpenProcess/GetExitCodeProcess instead."""
    if pid<=0:return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.restype=wintypes.HANDLE
        h=k.OpenProcess(0x1000,False,pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:return ctypes.get_last_error()==5   # access denied => it exists
        try:
            code=wintypes.DWORD()
            if not k.GetExitCodeProcess(h,ctypes.byref(code)):return True
            return code.value==259   # STILL_ACTIVE
        finally:k.CloseHandle(h)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except PermissionError:return True
    return True

def _release_stale_lock(lock):
    """A worker killed mid-build (e.g. the Studio closed during a compile)
    leaves its lock behind and every later run for that game reports 'busy'.
    Rename the lock aside (never delete) when its owner is another process
    that no longer exists, and mark that job's result as interrupted."""
    try:
        data=json.loads(lock.read_text())
        pid=int(data.get('pid',0))
    except (OSError,ValueError,TypeError):return
    if pid==os.getpid() or _pid_alive(pid):return
    try:
        lock.rename(lock.with_name(lock.name+f'.stale-pid{pid}-{int(time.time())}'))
    except OSError:return
    job=data.get('job_id','')
    result=lock.parent/str(job)/'result.json'
    if re.fullmatch(r'[0-9a-f]{32}',str(job)) and result.is_file():
        prior=read_json(result,{})
        if prior.get('status') in ('building',None):
            prior.update(status='failed',reason=f'Interrupted: worker process {pid} ended before finishing')
            _atomic(result,prior)

def _canonical(studio, attempt):
    """Attempt path with only the registered PS3 OUTPUT junctions followed.

    Since the output migration every attempt is reached through the
    workspace/projects junction, which _source_ok's no-link walk rejected --
    so Apply fixes found no usable build for any game. validate_owned_path
    allows exactly the registered aliases and still refuses any other link."""
    from .output_location import validate_owned_path
    try:
        return validate_owned_path(studio, attempt)
    except (OSError, ValueError):
        return None

def _source_ok(attempt, input_hash):
    attempt=Path(attempt)
    for parent in [attempt,*attempt.parents]:
        if parent.exists():_not_link(parent)
    report=read_json(attempt/'build-report.json',{})
    return (report.get('status')==_COMPLETED and report.get('input_sha256')==input_hash and
            (attempt/'port/recompiled/ppu_recomp.h').is_file() and
            (attempt/'port/CMakeLists.txt').is_file() and (attempt/'loader').is_dir())

def preview(studio, project_or_id, candidate=None, force=False):
    """Read-only preview; explicit candidate selects a finished source snapshot."""
    p=_project(studio,project_or_id)
    if not p:return {'available':False,'reason':'Selected game is no longer in the library'}
    result={'available':False,'project_id':p['id'],'reason':'No verified fixes available for this game'}
    try:
        elf=choose_elf(p['source'],p.get('elf',''));input_hash=sha256(elf)
        if input_hash!=adapter.INPUT_SHA256:return _runtime_preview(studio,p,candidate,elf,input_hash,result,force)
        backend=Path(studio.state['settings']['backend']).expanduser().resolve()
        try:
            manifest=json.loads(adapter._verified(adapter.PACKAGE_ROOT/'manifest.json',adapter.MANIFEST_SHA256,'manifest'))
            for rel, expected in manifest['backend_contract'].items():
                adapter._verified(adapter._under(backend,rel),expected,'supporting runtime contract')
        except Exception:
            # Legacy package no longer matches the improved runtime: use the recipe path.
            return _runtime_preview(studio,p,candidate,elf,input_hash,result,force)
        attempts=[]
        if candidate is not None:
            if not candidate.get('completed',True):raise GameFixError('Choose a finished candidate for Apply fixes')
            attempts=[candidate.get('attempt','')]
        else:
            if p.get('status') not in {'building','analysing'}:attempts.append(p.get('attempt',''))
            attempts.extend(item.get('attempt','') for item in reversed(p.get('native_candidate_history',[])))
        source=next((c for c in (_canonical(studio,a) for a in attempts if a) if c and _source_ok(c,input_hash)),None)
        if source is None:raise GameFixError('No completed source attempt for this exact ELF; finish one build first')
        return {**result,'available':True,'reason':'Verified MotorStorm policy and idle-worker fixes are available',
                'input':str(Path(elf).resolve()),'input_sha256':input_hash,'source_attempt':str(source),
                'source_report_sha256':sha256(source/'build-report.json'),'backend':str(backend),
                'selection_token':_token(p,studio.state['settings']),'adapter_id':adapter.ADAPTER_ID,
                'project':copy.deepcopy(p),'settings':copy.deepcopy(studio.state['settings']),
                'backend_python':studio.backend_python(),
                'fixes':['Restore verified raw WWS policy and preserve proven empty worker polling'],
                'note':'Builds a separate candidate; gameplay and black-screen resolution remain unverified'}
    except (OSError,RuntimeError,ValueError,KeyError) as exc:
        return {**result,'reason':str(exc)}

def known_template_hashes(backend):
    """Every earlier stock version of the game runner (one rule, used by both
    the planner and the builder so they can never disagree)."""
    folder=Path(backend)/'templates/project'
    return set(_OLD_TEMPLATE_MAIN_SHA256)|{sha256(b) for b in folder.glob('main.cpp.before-*.bak')}

def _template_main(studio):
    return Path(studio.state['settings']['backend']).expanduser().resolve()/'templates/project/main.cpp'

def _runtime_preview(studio,p,candidate,elf,input_hash,result,force=False):
    from . import wws_policy_recipes as recipes
    attempts=[]
    if candidate is not None:
        if not candidate.get('completed',True):raise GameFixError('Choose a finished candidate for Apply fixes')
        attempts=[candidate.get('attempt','')]
    else:
        if p.get('status') not in {'building','analysing'}:attempts.append(p.get('attempt',''))
        attempts.extend(item.get('attempt','') for item in reversed(p.get('native_candidate_history',[])))
    source=next((c for c in (_canonical(studio,a) for a in attempts if a) if c and _source_ok(c,input_hash)),None)
    if source is None and candidate is None:
        # Repairs built outside the queue (e.g. a single relinked library) have
        # no sources; fall back to the newest complete attempt on disk.
        try:
            root=_canonical(studio,Path(studio.home)/'projects'/p['id']/'attempts')
            found=sorted((d for d in root.iterdir() if d.is_dir()),key=lambda d:d.stat().st_mtime,reverse=True) if root else []
            source=next((d.resolve() for d in found if _source_ok(d.resolve(),input_hash)),None)
        except OSError:source=None
    if source is None:raise GameFixError('No completed source attempt for this exact ELF; finish one build first')
    # Already fixed with the current fix release: skip. (The runner/recipe checks
    # below look at the original source attempt, which never changes, so on their
    # own they would rebuild the same game every time.)
    if not force and _applied_fingerprints(studio).get(p['id'])==current_fix_fingerprint(studio):
        try:has_fixed_build=bool(list_ready_candidates(studio,p))
        except Exception:has_fixed_build=False
        if has_fixed_build:return {**result,'reason':'Already fixed with the current fix release'}
    fixes=[];refresh=False
    main=source/'port/main.cpp';tmpl=_template_main(studio)
    # Any earlier version of the template (its dated backups) counts as an unmodified copy to refresh.
    old_templates=known_template_hashes(tmpl.parent.parent.parent)
    if main.is_file() and sha256(main) in old_templates and tmpl.is_file() and sha256(tmpl)!=sha256(main):
        refresh=True;fixes.append('Update the game runner to the latest version (title ID, stall reports, frame pacing, display options)')
    recipe=recipes.recipe_for(input_hash)
    if recipe and not (source/'port/spu'/recipes.register_name(recipe)).is_file():
        fixes.append(f"Compile {recipe['title']} WWS job-system policy module (graphics/physics/audio SPU jobs)")
    # Any change to the runtime/library fixes since this game's last fix build counts as a fix.
    fingerprint=current_fix_fingerprint(studio)
    if _applied_fingerprints(studio).get(p['id'])!=fingerprint:
        fixes.append('Runtime and system-library fixes added since this game was last built')
    if not fixes and not force:return {**result,'reason':'This build already has every available fix'}
    fixes.append('Rebuild against the latest runtime library fixes')
    backend=Path(studio.state['settings']['backend']).expanduser().resolve()
    return {**result,'available':True,'kind':'runtime','reason':'; '.join(fixes),
            'input':str(Path(elf).resolve()),'input_sha256':input_hash,'source_attempt':str(source),
            'source_report_sha256':sha256(source/'build-report.json'),'backend':str(backend),
            'selection_token':_token(p,studio.state['settings']),'refresh_main':refresh,
            'project':copy.deepcopy(p),'settings':copy.deepcopy(studio.state['settings']),
            'backend_python':studio.backend_python(),'fixes':fixes,'fix_fingerprint':fingerprint,
            'note':'Builds a separate candidate; gameplay remains unverified'}

FIX_RELEASE_FILE='fix-release.txt'

def current_fix_fingerprint(studio=None):
    """The current fix release. It only changes when a new batch of fixes is
    released (workspace/fix-release.txt), so "Apply fixes to games" skips every
    game already built with this release, across restarts."""
    from .core import ROOT
    home=Path(studio.home) if studio is not None else ROOT/'workspace'
    try:value=(home/FIX_RELEASE_FILE).read_text(encoding='utf-8').strip()
    except OSError:value=''
    return 'release-'+(value or '1')

def _applied_fingerprints(studio):
    data=read_json(Path(studio.home)/'fix-fingerprints.json',{}) or {}
    # Builds recorded before releases existed count as release 1.
    return {k:(v if str(v).startswith('release-') else 'release-1') for k,v in data.items()}

def _record_fingerprint(studio,project_id,fingerprint):
    data=_applied_fingerprints(studio);data[project_id]=fingerprint
    _atomic(Path(studio.home)/'fix-fingerprints.json',data)

def _private_repairs(studio):
    """Project ids and input hashes that have hand-made repaired builds
    (workspace/native-repairs/<id>/*.json). Those carry fixes
    that exist only in their private candidate, so a rebuild from the original
    attempt would drop them: never plan or promote over them."""
    ids,hashes=set(),set()
    for sidecar in (Path(studio.home)/'native-repairs').glob('*/*.json'):
        try:
            if sidecar.stat().st_size>65536:continue
            item=read_json(sidecar,{})
        except OSError:continue
        if item.get('status')=='ready' and item.get('project_id'):
            ids.add(str(item['project_id']))
            if item.get('input_sha256'):hashes.add(item['input_sha256'])
    return ids,hashes

def plan_all(studio, emit=print, cancel=None):
    """Fix plans for every library game that has fixes, one per distinct ELF,
    quickest compile first (generated-source size as the proxy). Games with a
    private repaired build are skipped (see _private_repairs)."""
    plans={}
    protected_ids,protected_hashes=_private_repairs(studio)
    projects=[copy.deepcopy(p) for p in studio.state.get('projects',[])]
    for i,p in enumerate(projects,1):
        if cancel is not None and cancel.is_set():break
        if p.get('id') in protected_ids:
            # Still gets a fixed build as a separate candidate; promote() keeps the
            # hand-made build selected, so you choose which one to test.
            emit(f'{p.get("name","?")}: has a hand-made repaired build; the fixed build is added beside it')
        emit(f'Checking {i}/{len(projects)}: {p.get("name","?")}')
        try:plan=preview(studio,p)
        except Exception as exc:plan={'available':False,'reason':str(exc)}
        if not plan.get('available'):continue
        if plan.get('input_sha256') in protected_hashes:
            emit(f'{p.get("name","?")}: the same game has a hand-made repaired build; the fixed build is added beside it')
        src=Path(plan['source_attempt'])
        try:
            plan['_size']=sum(f.stat().st_size for f in (src/'port/recompiled').glob('*.cpp'))
            plan['_mtime']=(src/'build-report.json').stat().st_mtime
        except OSError:plan['_size'],plan['_mtime']=0,0
        prior=plans.get(plan['input_sha256'])
        if prior is None or plan['_mtime']>prior['_mtime']:plans[plan['input_sha256']]=plan
    return sorted(plans.values(),key=lambda x:x['_size'])

class _WorkerStudio(Studio):
    def __init__(self,home,settings,project,python):
        # Never instantiate Studio against the primary workspace: its init saves
        # and reconciles active queue entries. This state belongs only to the job.
        self.home=Path(home);self.home.mkdir(parents=True,exist_ok=True)
        self.state_path=self.home/'studio.json'
        self.state={'projects':[project],'settings':copy.deepcopy(settings),'version':'isolated-fixes'}
        self._worker_python=python
    def backend_python(self):return self._worker_python
    def project_dir(self,p):return self.home/"p"

def _exe_valid(path):
    if not path.is_file() or path.stat().st_size<64:raise GameFixError('Fix build did not produce an executable')
    with path.open('rb') as stream:
        head=stream.read(64)
        if os.name=='nt':
            if head[:2]!=b'MZ':raise GameFixError('Fix output is not a Windows executable')
            stream.seek(int.from_bytes(head[60:64],'little'))
            if stream.read(4)!=b'PE\0\0':raise GameFixError('Fix output has an invalid PE header')
        elif head[:4]!=b'\x7fELF':raise GameFixError('Fix output is not a native executable')

def run(studio, plan, cancel=None, emit=print):
    """Synchronous worker API; use an independent UI thread/Event to call it."""
    cancel=cancel or threading.Event()
    if not plan.get('available'):return {'status':'unavailable','reason':plan.get('reason','No verified fixes available')}
    if cancel.is_set():return {'status':'cancelled','reason':'Fixes cancelled before starting'}
    ident=plan.get('project_id','')
    runtime=plan.get('kind')=='runtime'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',str(ident)) or (not runtime and plan.get('input_sha256')!=adapter.INPUT_SHA256):
        return {'status':'unavailable','reason':'No verified fixes available for this input'}
    job_id=uuid.uuid4().hex
    root=Path(studio.home)/'game-fixes'/ident;_owned_no_link_ancestors(studio,root);root=root.resolve();root.mkdir(parents=True,exist_ok=True)
    lock=root/'.fix-worker.lock';token=uuid.uuid4().hex
    _release_stale_lock(lock)
    try:
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:return {'status':'busy','reason':'A fixes worker already owns this game; use its separate Stop fixes control'}
    with os.fdopen(fd,'w') as stream:json.dump({'pid':os.getpid(),'token':token,'job_id':job_id},stream)
    job=root/job_id;job.mkdir();result_path=job/'result.json'
    progress=_RepairProgress(job,plan['project'],emit)
    result={'status':'building','job_id':job_id,'project_id':ident,'input_sha256':plan['input_sha256'],
            'selection_token':plan['selection_token'],'source_attempt':plan['source_attempt'],
            'result_path':str(result_path.resolve()),'mode':'hybrid-SPU','playability':'unverified',
            'kind':plan.get('kind','motorstorm')}
    try:
        _atomic(result_path,result)
        progress.publish('Checking game input and completed-build receipt',None,0,'No byte counts from input validation',force=True)
        elf=Path(plan['input']);source=Path(plan['source_attempt'])
        if sha256(elf,cancel)!=plan['input_sha256'] or not _source_ok(source,plan['input_sha256']):
            raise GameFixError('Selected input or completed source attempt changed; preview fixes again')
        if sha256(source/'build-report.json')!=plan['source_report_sha256']:
            raise GameFixError('Selected source receipt changed; preview fixes again')
        worker_home=job/'workspace';attempt=worker_home/'a'
        emit('Copying the selected completed sources into a separate fixes workspace')
        progress.publish('Counting source files',None,0,'Reading file sizes before source preparation',force=True)
        progress.source_total=4*sum(path.stat().st_size for name in ('port','loader') for path in _files(source/name))
        source_hashes={name:_copy_sources(source/name,attempt/name,cancel,progress.source) for name in ('port','loader')}
        progress.publish('Source preparation complete',100,50,'All source bytes copied and verified',force=True)

        if runtime and plan.get('refresh_main'):
            dest=attempt/'port/main.cpp'
            if sha256(dest) not in known_template_hashes(plan['backend']):raise GameFixError('Copied main.cpp is not the stock template; refusing to replace it')
            dest.write_bytes(Path(plan['backend'],'templates/project/main.cpp').read_bytes())
            emit('Refreshed port/main.cpp from the current runtime template')
        prior=read_json(source/'build-report.json',{})
        _atomic(attempt/'build-report.json',prior)
        _atomic(job/'source-snapshot.json',source_hashes)
        project=copy.deepcopy(plan['project']);project.update(attempt=str(attempt),elf=str(elf),status='compiled; untested')
        worker=_WorkerStudio(worker_home,plan['settings'],project,plan['backend_python'])
        if cancel.is_set():raise Cancelled('Fixes cancelled; original candidates preserved')
        # Core's reuse branch performs the exact adapter preflight on this copied
        # port before configuring a NEW build directory. No cache is copied.
        from .progress import ProgressState
        progress.build_state=ProgressState('rebuild',project.get('name',''))
        manifest=worker.build(project,progress,cancel,reuse=True)
        if cancel.is_set():raise Cancelled('Fixes cancelled; original candidates preserved')
        if manifest.get('status')!=_COMPLETED:raise GameFixError('Fix worker returned without a completed build')
        progress.publish('Verifying repaired executable and receipts',None,99,'Verifying hashes and runtime identity; not a gameplay test',force=True)
        exe=Path(project.get('native_exe','')).resolve()
        if not exe.is_relative_to(attempt.resolve()):raise GameFixError('Fix output escaped its owned attempt')
        _exe_valid(exe)
        if sha256(elf,cancel)!=plan['input_sha256']:raise GameFixError('ELF changed during the fix build; candidate was not selected')
        proof=read_json(attempt/'private-native-adapter.json',{})
        exe_hash=sha256(exe)
        if runtime:
            receipt_value=project.get('native_runtime_receipt') or ''
            if not receipt_value:
                # Fix candidates always run hybrid-SPU: titles load SPU code at
                # runtime (Juiced 2's MultiStream DSP plugin, WWS job modules)
                # that no static lift can contain, and without the interpreter
                # those jobs halt and the title blocks on them forever.
                hybrid=worker_home/'verification'/'native-runtime'/(job_id+'.json')
                _atomic(hybrid,{'schema':1,'input_sha256':plan['input_sha256'],'executable':str(exe),
                                'executable_sha256':exe_hash,'mode':'hybrid-SPU','playability':'unverified',
                                'diagnostic_env':{'SPU_INTERP_UNLIFTED':'1','SPU_INTERP_BUDGET':'1000000'}})
                receipt_value=str(hybrid.resolve())
            if receipt_value:
                receipt=Path(receipt_value).resolve()
                if not receipt.is_relative_to(worker_home.resolve()/'verification'):raise GameFixError('Fix runtime receipt escaped its private worker')
                evidence=read_json(receipt,{})
                if evidence.get('input_sha256')!=plan['input_sha256'] or evidence.get('executable_sha256')!=exe_hash:
                    raise GameFixError('Fix runtime receipt does not match the actual executable')
            result.update(status='ready',candidate={'native_exe':str(exe),'attempt':str(attempt.resolve()),
                          'native_runtime_receipt':str(receipt) if receipt_value else None,'input_sha256':plan['input_sha256'],
                          'executable_sha256':exe_hash,'label':'Applied runtime fixes '+time.strftime('%m-%d %H:%M')+'; gameplay unverified'},
                          adapter_receipt=proof or None,backend=plan['backend'],input=str(elf),registered=False,fixes=plan.get('fixes',[]))
            emit('Fixes compiled into a separate candidate; gameplay remains unverified')
            if plan.get('fix_fingerprint'):
                try:_record_fingerprint(studio,plan['project_id'],plan['fix_fingerprint'])
                except OSError:pass
            return result
        if not proof.get('applied') or proof.get('input_sha256')!=plan['input_sha256'] or proof.get('manifest_sha256')!=adapter.MANIFEST_SHA256:
            raise GameFixError('Fix build has no verified private adapter source proof')
        receipt=Path(project.get('native_runtime_receipt','')).resolve()
        if not receipt.is_relative_to(worker_home.resolve()/'verification'):raise GameFixError('Fix runtime receipt escaped its private worker')
        evidence=read_json(receipt,{})
        exe_hash=sha256(exe)
        if (evidence.get('input_sha256')!=plan['input_sha256'] or evidence.get('executable_sha256')!=exe_hash or
                Path(evidence.get('executable','')).resolve()!=exe or
                evidence.get('diagnostic_env')!={'SPU_INTERP_UNLIFTED':'1','SPU_INTERP_BUDGET':'1000000'}):
            raise GameFixError('Fix runtime receipt does not match the actual executable')
        result.update(status='ready',candidate={'native_exe':str(exe),'attempt':str(attempt.resolve()),
                      'native_runtime_receipt':str(receipt),'input_sha256':plan['input_sha256'],
                      'executable_sha256':exe_hash,'label':'Applied verified fixes; gameplay unverified'},
                      adapter_receipt=proof,backend=plan['backend'],input=str(elf),registered=False)
        emit('Selected-game fixes compiled into a separate candidate; gameplay remains unverified')
    except Exception as exc:
        result.update(status='cancelled' if isinstance(exc,Cancelled) else 'failed',reason=str(exc))
        emit(str(exc))
    finally:
      try:
        try:_atomic(result_path,result)
        except OSError:pass
        progress.publish('Repair candidate ready' if result['status']=='ready' else 'Repair '+result['status'],
                         100 if result['status']=='ready' else None,100 if result['status']=='ready' else progress.value.get('overall_percent',0),
                         result.get('reason','Compiled; gameplay remains unverified'),status=result['status'],force=True)
      finally:
        # Always release this worker's own lock, even if the final result or
        # progress write failed; a leftover lock blocks the game until removed.
        if read_json(lock,{}).get('token')==token:lock.unlink(missing_ok=True)
    return result

apply_fixes=run

def _validated(studio, result):
    raw_path=Path(result['result_path']);_owned_no_link_ancestors(studio,raw_path)
    path=raw_path.resolve();base=(Path(studio.home)/'game-fixes').resolve()
    if not path.is_relative_to(base) or path.name!='result.json' or path.stat().st_size>262144:
        raise GameFixError('Fix result is outside its owned workspace')
    data=read_json(path,{})
    if data.get('status')!='ready':raise GameFixError('Fix candidate is not ready')
    ident=data.get('project_id','')
    if path.parent.parent.name!=ident or path.parent.name!=data.get('job_id'):raise GameFixError('Fix result ownership mismatch')
    candidate=data['candidate'];attempt=Path(candidate['attempt']).resolve();exe=Path(candidate['native_exe']).resolve()
    if not attempt.is_relative_to(path.parent/'workspace') or not exe.is_relative_to(attempt):raise GameFixError('Fix candidate paths escaped their owned job')
    for target in [path,attempt,exe]:_not_link(target)
    _exe_valid(exe)
    if sha256(exe)!=candidate.get('executable_sha256'):raise GameFixError('Fix executable changed after verification')
    report=read_json(attempt/'build-report.json',{})
    if report.get('status')!=_COMPLETED or report.get('input_sha256')!=data.get('input_sha256'):raise GameFixError('Fix build report/input mismatch')
    if data.get('kind')=='runtime':
        value=candidate.get('native_runtime_receipt')
        if not value:return data,None
        receipt=Path(value).resolve()
        trusted=[path.parent/'workspace/verification',Path(studio.home).resolve()/'verification/game-fixes']
        if not any(receipt.is_relative_to(root) for root in trusted):raise GameFixError('Untrusted fix runtime receipt')
        _not_link(receipt);evidence=read_json(receipt,{})
        if evidence.get('input_sha256')!=data['input_sha256'] or evidence.get('executable_sha256')!=candidate['executable_sha256']:
            raise GameFixError('Fix runtime receipt identity mismatch')
        return data,evidence
    proof=read_json(attempt/'private-native-adapter.json',{})
    if not proof.get('applied') or proof.get('manifest_sha256')!=adapter.MANIFEST_SHA256 or proof.get('input_sha256')!=data.get('input_sha256'):
        raise GameFixError('Fix source proof changed or is unsupported')
    receipt=Path(candidate['native_runtime_receipt']).resolve()
    trusted=[path.parent/'workspace/verification',Path(studio.home).resolve()/'verification/game-fixes']
    if not any(receipt.is_relative_to(root) for root in trusted):raise GameFixError('Untrusted fix runtime receipt')
    _not_link(receipt);evidence=read_json(receipt,{})
    if (evidence.get('input_sha256')!=data['input_sha256'] or evidence.get('executable_sha256')!=candidate['executable_sha256'] or
        Path(evidence.get('executable','')).resolve()!=exe or evidence.get('diagnostic_env')!={'SPU_INTERP_UNLIFTED':'1','SPU_INTERP_BUDGET':'1000000'}):
        raise GameFixError('Fix runtime receipt identity mismatch')
    return data,evidence

def register_test_candidate(studio,result):
    """Main-thread sidecar registration; never mutates studio.state/studio.json."""
    data,evidence=_validated(studio,result)
    if evidence is None:
        data['registered']=True;_atomic(Path(data['result_path']),data);return data
    receipt=Path(studio.home)/'verification/game-fixes'/data['project_id']/(data['job_id']+'.json')
    if receipt.exists() and read_json(receipt,{})!=evidence:raise GameFixError('Preserve conflicting fix receipt')
    _atomic(receipt,evidence)
    data['candidate']['native_runtime_receipt']=str(receipt.resolve());data['registered']=True
    _atomic(Path(data['result_path']),data)
    return data

def list_ready_candidates(studio,project):
    """Read-only validated candidates for independent manual Test selection."""
    ident=project.get('id','');root=Path(studio.home)/'game-fixes'/ident;result=[]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',str(ident)):return result
    for path in sorted(root.glob('*/result.json')):
        try:
            data,_=_validated(studio,{'result_path':str(path)})
            if data.get('registered'):result.append({**data['candidate'],'result_path':str(path),'completed':True,'valid':True})
        except (OSError,KeyError,ValueError,RuntimeError):continue
    return result

def promote(studio,result,queue_busy=False):
    """Main-thread idle-only CAS promotion; otherwise retain independent candidate."""
    data=register_test_candidate(studio,result)
    retained={'promoted':False,'candidate':data['candidate'],'result_path':data['result_path']}
    ids,hashes=_private_repairs(studio)
    if data.get('kind')=='runtime' and (data['project_id'] in ids or data.get('input_sha256') in hashes):
        return {**retained,'reason':'Candidate kept for Test; this game has a hand-made repaired build, so it was not switched'}
    if queue_busy or getattr(studio,'read_only',False):return {**retained,'reason':'Candidate retained for independent Test; queue/companion state was preserved'}
    lock=getattr(studio,'state_lock',None)
    if lock is None:return {**retained,'reason':'Candidate retained; Studio state lock is unavailable'}
    with lock:
        p=_project(studio,data['project_id'])
        if not p:return {**retained,'reason':'Candidate retained; game was removed from the library'}
        if any(item.get('status') in {'building','analysing'} for item in studio.state.get('projects',[])):
            return {**retained,'reason':'Candidate retained; build queue is active'}
        if _token(p,studio.state['settings'])!=data['selection_token']:
            return {**retained,'reason':'Candidate retained; a newer game candidate or setting was selected'}
        elf=choose_elf(p['source'],p.get('elf',''))
        if sha256(elf)!=data['input_sha256']:return {**retained,'reason':'Candidate retained; game ELF changed'}
        prior=copy.deepcopy(p)
        try:
            studio.preserve_native_candidate(p,'verified per-game fixes selected')
            p.update({key:data['candidate'][key] for key in ('native_exe','attempt','native_runtime_receipt')})
            p['status']='compiled; untested';p.pop('error',None)
            studio.save()
        except Exception:
            p.clear();p.update(prior);raise
        return {'promoted':True,'candidate':data['candidate'],'result_path':data['result_path'],
                'reason':'Verified fixes compiled and selected; gameplay remains unverified'}

def main(argv=None):
    parser=argparse.ArgumentParser(description='Preview or separately build verified fixes for one PS3 game')
    parser.add_argument('--workspace',required=True);parser.add_argument('--project',required=True)
    parser.add_argument('--apply',action='store_true',help='Build separately; never edits the live queue or selects a game')
    args=parser.parse_args(argv)
    # CLI is a read-only snapshot of the primary state; constructor must not
    # reconcile/save an existing GUI queue, including across processes.
    studio=object.__new__(Studio);studio.home=Path(args.workspace).resolve();studio.state_path=studio.home/'studio.json'
    studio.state=read_json(studio.state_path,{});studio._read_only_state=True
    plan=preview(studio,args.project)
    if not args.apply or not plan.get('available'):
        print(json.dumps({k:v for k,v in plan.items() if k not in {'project','settings'}},indent=2));return 0 if plan.get('available') else 2
    result=run(studio,plan)
    if result.get('status')=='ready':result=register_test_candidate(studio,result)
    print(json.dumps(result,indent=2));return 0 if result.get('status')=='ready' else 1

if __name__=='__main__':raise SystemExit(main())
