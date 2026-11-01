# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""PS3-only Fulton catalog/source integration. Fulton files are read-only."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import threading
import inspect
import tempfile
from contextlib import contextmanager
from functools import wraps

_LOCK=threading.RLock()
def serialized(fn):
    @wraps(fn)
    def call(*args,**kwargs):
        cancel=inspect.signature(fn).bind_partial(*args,**kwargs).arguments.get('cancel')
        while not _LOCK.acquire(timeout=0.15):
            if cancel and cancel.is_set(): raise Cancelled('Fulton operation cancelled; existing sources retained')
        try:
            if cancel and cancel.is_set(): raise Cancelled('Fulton operation cancelled; existing sources retained')
            return fn(*args,**kwargs)
        finally: _LOCK.release()
    return call
from urllib.request import Request, urlopen
from .core import ROOT, BACKEND_REV, Runner, Cancelled, read_json, write_json, sha256

CURATED = 'https://raw.githubusercontent.com/TriwindBryan/fulton/main/catalog/games.json'
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z')

def normalize(data, origin):
    if isinstance(data, dict):
        data = next((data[k] for k in ('games','items','userGames','entries') if isinstance(data.get(k), list)), [])
    if not isinstance(data, list): return []
    result = []
    for item in data[:10000]:
        if not isinstance(item, dict): continue
        if str(item.get('system',item.get('console',''))).casefold() not in ('ps3','playstation 3'): continue
        repo = str(item.get('repo','')).removeprefix('https://github.com/').rstrip('/').removesuffix('.git')
        name = item.get('title',item.get('name',''))
        if not REPO.fullmatch(repo) or not isinstance(name,str) or not name.strip(): continue
        entry=dict(name=name[:300],repo=repo,system='ps3',origin=origin)
        # Retain only public, descriptive requirements; never copy launcher settings or credentials.
        rom=item.get('rom',{})
        note=rom.get('note','') if isinstance(rom,dict) else ''
        if not note: note=item.get('requirements',item.get('needs',''))
        if isinstance(note,str): entry['requirements']=note[:12000]
        result.append(entry)
    return result

def folder(studio):
    return studio.home/'ps3-fulton'


def write_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix=path.name+'-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(handle,'w',encoding='utf-8') as stream:json.dump(value,stream,indent=2)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

@contextmanager
def source_lock(studio, cancel=None):
    base=folder(studio);base.mkdir(parents=True,exist_ok=True)
    with (base/'sources.lock').open('a+b') as stream:
        if os.fstat(stream.fileno()).st_size==0:stream.write(b'0');stream.flush()
        while True:
            if cancel and cancel.is_set():raise Cancelled('Fulton source preparation cancelled')
            stream.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if cancel:cancel.wait(0.15)
                else:time.sleep(0.15)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)

def local_paths():
    paths = [ROOT/'studio/cartridge_bridge/games.json']
    for env in ('APPDATA','LOCALAPPDATA'):
        if os.environ.get(env):
            for app in ('Fulton','Cartridge'):
                paths.extend(Path(os.environ[env])/app/n for n in ('community.json','state.json'))
    for env in ('ProgramFiles','ProgramFiles(x86)'):
        if os.environ.get(env):
            base=Path(os.environ[env])/'Fulton'
            paths.extend(base/n for n in ('community.json','state.json','catalog/games.json','resources/catalog/games.json'))
    return list(dict.fromkeys(paths))

def catalog(studio):
    entries=[]
    for path in local_paths():
        try:
            if path.stat().st_size > 16*1024*1024: continue
            entries.extend(normalize(json.loads(path.read_text(encoding='utf-8-sig')),str(path)))
        except (OSError,ValueError): pass
    cached=normalize(read_json(folder(studio)/'catalog.json',{}),'Studio cache')
    entries=cached+entries
    return sorted({e['repo'].casefold():e for e in entries}.values(),key=lambda e:e['name'].casefold())

@serialized
def sync(studio, force=False, emit=print, cancel=None):
    old=read_json(folder(studio)/'catalog.json',{})
    warnings=[]
    entries=catalog(studio)
    if force or time.time()-old.get('checked',0)>86400:
        try:
            with urlopen(Request(CURATED,headers={'User-Agent':'RetroRemakes-PS3-Studio'}),timeout=12) as response:
                if not response.geturl().startswith('https://raw.githubusercontent.com/'):
                    raise ValueError('Unexpected catalog origin')
                raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024: raise ValueError('Catalog exceeds size limit')
            entries+=normalize(json.loads(raw),'Fulton curated online')
        except Exception as error:
            warnings.append(str(error)); emit('Fulton online sync unavailable; using local/cached library: '+str(error))
    else: warnings=old.get('warnings',[])
    merged={}
    for entry in entries:
        previous=merged.get(entry['repo'].casefold(),{})
        if not entry.get('requirements') and previous.get('requirements'):entry['requirements']=previous['requirements']
        merged[entry['repo'].casefold()]=entry
    entries=sorted(merged.values(),key=lambda e:e['name'].casefold())
    write_json(folder(studio)/'catalog.json',dict(schema=1,checked=time.time() if force or time.time()-old.get('checked',0)>86400 else old['checked'],entries=entries,warnings=warnings))
    emit(f'Fulton: {len(entries)} PS3 source projects available. Local library preserved.')
    return entries

def pull(studio, entry, emit=print, cancel=None):
    repo=entry['repo']
    if not REPO.fullmatch(repo): raise ValueError('Invalid Fulton repository')
    base=folder(studio)/'sources'/repo.replace('/','--')
    current=read_json(base/'current.json',{})
    commit=current.get('commit','')
    if re.fullmatch('[0-9a-f]{40}',commit) and (base/commit).is_dir() and time.time()-current.get('checked',0)<86400:
        return dict(repo=repo,commit=commit,path=str(base/commit),status='source available; compatibility unverified')
    if not shutil.which('git'): raise RuntimeError('Git is needed to pull Fulton source projects')
    target=base/('fetch-'+uuid.uuid4().hex[:12]);base.mkdir(parents=True,exist_ok=True)
    runner=Runner(base/'logs'/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]),emit,cancel)
    runner.run('fulton-source',['git','-c','core.longpaths=true','-c','core.hooksPath='+str(base/'disabled-hooks'), 'clone','--depth','1','--no-recurse-submodules','https://github.com/'+repo+'.git',target],timeout=600)
    commit=subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True,timeout=15).strip()
    if not re.fullmatch('[0-9a-f]{40}',commit): raise ValueError('Invalid source commit')
    destination=base/commit
    if not destination.exists(): target.rename(destination)
    write_json(base/'current.json',dict(commit=commit,checked=time.time()))
    return dict(repo=repo,commit=commit,path=str(destination),status='source available; compatibility unverified')

@serialized
def pull_many(studio, entries, emit=print, cancel=None):
    with source_lock(studio,cancel):
        results=[]
        operation=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        for index,entry in enumerate(entries):
            if cancel and cancel.is_set(): raise Cancelled('Fulton source pull cancelled; completed snapshots retained')
            emit(f"Fulton {index+1}/{len(entries)}: {entry['repo']}")
            try: results.append(pull(studio,entry,emit,cancel))
            except Cancelled: raise
            except Exception as error:
                results.append(dict(repo=entry['repo'],error=str(error)));emit(str(error))
            receipt=dict(operation=operation,results=results)
            write_json(folder(studio)/'pull-reports'/(operation+'.json'),receipt)
            write_json(folder(studio)/'last-pull.json',receipt)
        return results


def prepare_library(studio, force=False, emit=print, cancel=None, entries=None):
    """Prepare every PS3 library entry before the user chooses a game."""
    if entries is None:entries=sync(studio,force,emit,cancel)
    results=pull_many(studio,entries,emit,cancel)
    report=dict(schema=1,prepared=time.time(),projects=len(entries),
                available=sum('path' in item for item in results),
                unavailable=sum('error' in item for item in results),results=results)
    write_json(folder(studio)/'library-readiness.json',report)
    emit(f"Fulton library prepared: {report['available']}/{report['projects']} sources cached; {report['unavailable']} unavailable.")
    return report

def readiness(studio, entry):
    base=folder(studio)/'sources'/entry['repo'].replace('/','--')
    current=read_json(base/'current.json',{})
    commit=current.get('commit','')
    if re.fullmatch('[0-9a-f]{40}',commit) and (base/commit).is_dir():return 'Cached'
    previous=read_json(folder(studio)/'last-pull.json',{})
    if any(item.get('repo')==entry['repo'] and 'error' in item for item in previous.get('results',[])):return 'Unavailable - see log'
    return 'Not downloaded yet'

def title_key(value):
    return ''.join(re.findall('[a-z0-9]+',re.sub(r'\([^)]*\)|\[[^]]*\]',' ',value.casefold())))

def collect(studio, project, report, attempt, emit=print, cancel=None):
    plan=dict(schema=1,console='ps3',input_sha256=report['sha256'],sources=[],warnings=[],applied=[])
    if studio.state['settings'].get('fulton_auto',False):
        entries=sync(studio,emit=emit)
        entries=[e for e in entries if title_key(e['name'])==title_key(project['name'])]
        plan['sources']=pull_many(studio,entries,emit,cancel)
        if not entries: plan['warnings'].append('No matching Fulton source project; generic PS3 attempts remain available.')
    else: plan['warnings'].append('Automatic Fulton source use disabled')
    write_json(Path(attempt)/'fulton-base-plan.json',plan)
    return plan

def safe_file(base, relative):
    if not isinstance(relative,str) or not relative or ':' in relative or '\\' in relative:
        raise ValueError('Unsafe Fulton adapter path')
    path=(base/relative).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file(): raise ValueError('Fulton adapter path escapes source or is missing')
    return path

def apply_loader_inputs(plan, report, backend, loader, elf):
    """Use exact-input, exact-backend function/import metadata only; never infer ABI."""
    candidates=[]
    actual=subprocess.check_output(['git','-C',str(backend),'rev-parse','HEAD'],text=True,timeout=15).strip()
    for source in plan['sources']:
        if 'path' not in source: continue
        base=Path(source['path']);manifest=read_json(base/'rr-ps3-base.json',{})
        if not manifest:
            plan['warnings'].append(source['repo']+': no PS3 adapter; source retained for review');continue
        try:
            if manifest.get('schema')!=1 or manifest.get('console')!='ps3': raise ValueError('Unsupported adapter')
            if manifest.get('boot_sha256')!=report['sha256'] or manifest.get('backend_revision')!=actual or actual!=BACKEND_REV:
                raise ValueError('Adapter input/backend does not match')
            paths={}
            for key in ('functions','imports'):
                spec=manifest.get(key,{})
                path=safe_file(base,spec.get('path'))
                if sha256(path)!=spec.get('sha256'): raise ValueError('Adapter metadata hash mismatch')
                data=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(data,(list,dict)) or not data: raise ValueError('Empty/invalid loader metadata')
                paths[key]=path
            candidates.append((source,paths))
        except (ValueError,OSError,TypeError) as error: plan['warnings'].append(source['repo']+': '+str(error))
    if len(candidates)>1:
        plan['warnings'].append('Conflicting exact-input adapters; generated loader metadata retained');return
    if candidates:
        source,paths=candidates[0]
        originals={}
        try:
            for key,path in paths.items():
                destination=Path(loader)/(Path(elf).stem+'.'+key+'.json')
                originals[destination]=destination.read_bytes() if destination.exists() else None
                if destination.exists(): shutil.copy2(destination,destination.with_suffix(destination.suffix+'.before-fulton'))
                shutil.copy2(path,destination)
        except Exception:
            for destination,content in originals.items():
                if content is None: destination.unlink(missing_ok=True)
                else: destination.write_bytes(content)
            raise
        plan['applied'].append(dict(repo=source['repo'],commit=source['commit'],kind='exact-input loader metadata'))
