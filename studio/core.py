# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
from __future__ import annotations

import codecs
import copy
import re
import collections
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from . import VERSION


def games_folders(settings):
    """Every games library the player has added, primary first, existing only."""
    out=[]
    for f in [settings.get('games_folder')]+list(settings.get('games_folders') or []):
        if f and str(f).casefold() not in [o.casefold() for o in out]:out.append(str(f))
    return [f for f in out if Path(f).expanduser().is_dir()]
from .progress import report as report_progress

ROOT = Path(__file__).resolve().parent.parent
BACKEND_URL = "https://github.com/sp00nznet/ps3recomp.git"
BACKEND_REV = "a6d76c895f77eb30b90a806c830544e61821b77f"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):   # antivirus/indexers briefly lock files on Windows
        try:
            os.replace(tmp, path); return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


def read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def spu_retry_reason(spu, prior):
    """Resume failed discovery without treating retained partial sources as complete."""
    for command in prior.get("commands", []):
        if command.get("stage") == "spu-discover-lift" and command.get("status") != "passed":
            return "previous SPU discovery did not finish successfully"
    if "spu-discover-lift" in str(prior.get("error", "")):
        return "previous SPU discovery failed"
    if not (spu / "spu_workloads.c").is_file():
        return "SPU workload registry is missing"
    inventory_path = spu / "inventory.json"
    if inventory_path.exists():
        try:
            inventory = read_json(inventory_path)
            if not isinstance(inventory, dict) or inventory.get("complete") is not True:
                return "SPU coverage is incomplete"
        except (OSError, ValueError, TypeError):
            return "SPU inventory cannot be validated"
    return None


def preserve_spu_retry(spu, destination, cancel):
    """Keep reviewed sources and inventory before discovery replaces its outputs."""
    if not spu.exists():
        return
    def copy_file(source, target):
        if cancel.is_set():
            raise Cancelled("Cancelled while preserving SPU sources")
        return shutil.copy2(source, target)
    shutil.copytree(spu, destination, symlinks=True, copy_function=copy_file)


def sha256(path, cancel=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled("Cancelled during input hashing")
            h.update(block)
    return h.hexdigest()


def inspect_binary(path):
    """Bounded inspection; never treat a filename extension as proof of ELF."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        b = f.read(64)
        if b.startswith(b"SCE\0"):
            return {"format": "SELF", "native_input": False,
                    "reason": "Supply a decrypted PPU ELF. A SELF container is not a native-build input."}
        if not b.startswith(b"\x7fELF"):
            return {"format": "other", "native_input": False,
                    "reason": "Select a decrypted PS3 ELF or an extracted game folder; ISO/PKG extraction is external."}
        if len(b) < 16 or b[4] not in (1, 2) or b[5] not in (1, 2):
            raise ValueError("Invalid ELF identification")
        cls, endian = b[4], ">" if b[5] == 2 else "<"
        fmt = endian + ("HHIQQQIHHHHHH" if cls == 2 else "HHIIIIIHHHHHH")
        n = struct.calcsize(fmt)
        if len(b) < 16 + n:
            raise ValueError("Truncated ELF header")
        v = struct.unpack_from(fmt, b, 16)
        typ, machine, version, entry, phoff, shoff, flags, ehsize, phentsize, phnum, shentsize, shnum, shstr = v
        if version != 1 or ehsize < n + 16:
            raise ValueError("Invalid ELF header version/size")
        phmin = 56 if cls == 2 else 32
        if phnum and (phentsize < phmin or phoff + phnum * phentsize > size):
            raise ValueError("ELF program-header table extends beyond the file")
        shmin = 64 if cls == 2 else 40
        if shnum and (shentsize < shmin or shoff + shnum * shentsize > size):
            raise ValueError("ELF section-header table extends beyond the file")
        segments = []
        for i in range(phnum):
            f.seek(phoff + i * phentsize)
            p = struct.unpack(endian + ("IIQQQQQQ" if cls == 2 else "IIIIIIII"), f.read(phmin))
            if cls == 2:
                pt, pf, off, va, pa, fs, ms, align = p
            else:
                pt, off, va, pa, fs, ms, pf, align = p
            if pt == 1 and (fs > ms or off + fs > size):
                raise ValueError("Invalid ELF load-segment bounds")
            if pt == 1:
                segments.append({"offset": off, "address": hex(va), "filesz": fs, "memsz": ms, "flags": pf})
        native = cls == 2 and b[5] == 2 and machine == 21 and typ == 2 and bool(segments)
        return {"format": "ELF", "bits": 64 if cls == 2 else 32,
                "endian": "big" if b[5] == 2 else "little", "machine": machine,
                "architecture": {21: "PPU / PowerPC64", 23: "SPU"}.get(machine, "other"),
                "entry": hex(entry), "elf_type": typ, "segments": segments,
                "native_input": native,
                "reason": "Eligible for an experimental PPU build attempt" if native else
                "Build expects a big-endian ELF64 PPU executable with load segments (not an SPU image/PRX)."}


def parse_sfo(path):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("PARAM.SFO is unexpectedly large")
    b = path.read_bytes()
    if len(b) < 20 or b[:4] != b"\0PSF":
        raise ValueError("Invalid PARAM.SFO")
    version, keys, data, count = struct.unpack_from("<IIII", b, 4)
    if count > 4096 or 20 + count * 16 > len(b) or not 20 <= keys <= data <= len(b):
        raise ValueError("Invalid SFO tables")
    out = {}
    for i in range(count):
        ko, fmt, length, maximum, do = struct.unpack_from("<HHIII", b, 20 + i * 16)
        start = keys + ko
        end = b.find(b"\0", start, data)
        if not keys <= start < data or end < 0 or length > maximum or data + do + length > len(b):
            raise ValueError("Invalid SFO entry")
        key, val = b[start:end].decode("utf-8", "replace"), b[data + do:data + do + length]
        out[key] = int.from_bytes(val, "little") if fmt == 0x404 else val.rstrip(b"\0").decode("utf-8", "replace")
    return out


def choose_elf(source, explicit=""):
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise ValueError("Selected ELF does not exist")
        return p
    p = Path(source).expanduser().resolve()
    if p.is_file():
        return p
    if not p.is_dir():
        raise ValueError("Game input does not exist")
    for name in ("PS3_GAME/USRDIR/EBOOT.ELF", "USRDIR/EBOOT.ELF", "EBOOT.ELF",
                 "PS3_GAME/USRDIR/EBOOT.elf", "USRDIR/EBOOT.elf", "EBOOT.elf",
                 "PS3_GAME/USRDIR/EBOOT.BIN", "USRDIR/EBOOT.BIN", "EBOOT.BIN"):
        if (p / name).is_file():
            return p / name
    raise ValueError("No EBOOT found. Use Set decrypted ELF for this project.")


def inventory(source, limit=100000, cancel=None):
    base = Path(source).resolve()
    base = base if base.is_dir() else base.parent
    counts, binaries, samples, warnings = collections.Counter(), [], [], []
    total, size, truncated = 0, 0, False
    def error(e):
        warnings.append(str(e))
    for folder, dirs, files in os.walk(base, followlinks=False, onerror=error):
        dirs[:] = sorted(d for d in dirs if d not in (".git", ".venv", "__pycache__") and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            if cancel is not None and cancel.is_set():
                raise Cancelled("Cancelled during file inventory")
            p = Path(folder) / name
            if p.is_symlink():
                continue
            total += 1
            if total > limit:
                truncated = True
                break
            try:
                size += p.stat().st_size
                counts[p.suffix.lower() or "(none)"] += 1
                if len(samples) < 300:
                    samples.append(str(p.relative_to(base)))
                if p.suffix.lower() in (".elf", ".prx", ".sprx", ".self") or name.upper() == "EBOOT.BIN":
                    if len(binaries) >= 4096:
                        continue
                    try:
                        info = inspect_binary(p)
                    except ValueError as e:
                        info = {"format": "invalid", "reason": str(e)}
                    binaries.append({"path": str(p), **info})
            except OSError as e:
                warnings.append(str(e))
        if truncated:
            break
    return {"root": str(base), "files_scanned": min(total, limit), "total_bytes": size,
            "extensions": dict(counts.most_common()), "binaries": binaries, "samples": samples,
            "truncated": truncated, "warnings": warnings,
            "note": "Lists loose files only. Embedded/compressed SPU jobs and proprietary archives need further analysis."}


class Cancelled(RuntimeError):
    pass


def compiler_crashed_sources(log):
    """Source files (relative to the port folder) whose compile crashed the compiler."""
    try:
        text = Path(log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for block in text.split("FAILED: ")[1:]:
        head = block[:20000]
        if "PLEASE submit a bug report" not in head and "Exception Code: 0xC0000005" not in head:
            continue
        obj = head.split()[0]
        marker = ".dir/"
        if marker in obj and obj.endswith(".obj"):
            rel = obj.split(marker, 1)[1][:-4].replace("\\", "/")
            if rel not in found:
                found.append(rel)
    return found


def compiler_ran_out_of_memory(log):
    """True when a build log shows the compiler itself ran out of memory."""
    markers=(b"LLVM ERROR: out of memory",b"fatal error C1060",b"fatal error C1002",b"error: unable to allocate",b"Allocation failed")
    try:
        with Path(log).open("rb") as stream:
            tail=b""
            while True:
                chunk=stream.read(1<<20)
                if not chunk:return False
                window=tail+chunk
                if any(m in window for m in markers):return True
                tail=chunk[-64:]
    except OSError:
        return False


class Runner:
    def __init__(self, log_dir, emit=print, cancel=None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.cancel = cancel or threading.Event()
        self.records = []

    def run(self, label, argv, cwd=None, timeout=7200):
        if self.cancel.is_set():raise Cancelled("Cancelled")
        argv=[str(x) for x in argv]
        log=self.log_dir/f"{len(self.records)+1:02}-{label}.log"
        record=dict(stage=label,argv=argv,cwd=str(cwd or Path.cwd()),log=log.name,started=time.time(),status="running")
        self.records.append(record)
        write_json(self.log_dir/"commands.json",self.records)
        self.emit(f"[{label}] {' '.join(argv)}")
        report_progress(self.emit,label,'start')
        decoder=codecs.getincrementaldecoder('utf-8')('replace');pending=''
        def publish(reader, final=False):
            nonlocal pending
            shown=0;suppressed=0
            while True:
                data=reader.read(65536)
                if not data:break
                pending+=decoder.decode(data)
                lines=re.split(r'[\r\n]+',pending);pending=lines.pop()
                for line in lines:
                    if line:
                        if shown<200 or re.search(r'(?i)(error:|fatal error|FAILED:|ninja: build stopped)',line):
                            self.emit(line);shown+=1
                        else:suppressed+=1
                        report_progress(self.emit,label,'output',line=line)
            if suppressed:self.emit(f'[{label}] {suppressed} additional output lines saved in {log}')
            if final:
                pending+=decoder.decode(b'',final=True)
                if pending:self.emit(pending);report_progress(self.emit,label,'output',line=pending)
                pending=''
        try:
            kwargs={"start_new_session":True} if os.name!='nt' else {"creationflags":subprocess.CREATE_NEW_PROCESS_GROUP}
            env=os.environ.copy();env['PYTHONUNBUFFERED']='1';env['PYTHONIOENCODING']='utf-8'
            with log.open('wb') as output:
                proc=subprocess.Popen(argv,cwd=cwd,env=env,stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,shell=False,**kwargs)
                record['pid']=proc.pid;write_json(self.log_dir/'commands.json',self.records)
                with log.open('rb') as reader:
                    while proc.poll() is None:
                        publish(reader)
                        if self.cancel.wait(.15) or time.time()-record['started']>timeout:
                            if os.name=='nt':subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
                            else:
                                try:os.killpg(proc.pid,signal.SIGTERM)
                                except ProcessLookupError:pass
                            try:proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                if os.name!='nt':os.killpg(proc.pid,signal.SIGKILL)
                                proc.kill();proc.wait()
                            publish(reader,True)
                            if self.cancel.is_set():raise Cancelled('Cancelled; partial artifacts retained for diagnosis')
                            raise RuntimeError(f'{label} timed out after {timeout}s')
                    publish(reader,True)
            record['exit_code']=proc.returncode
            record['status']='passed' if proc.returncode==0 else 'failed'
            with log.open('rb') as stream:
                stream.seek(max(0,log.stat().st_size-12000));tail=stream.read().decode('utf-8','replace')
            if proc.returncode:raise RuntimeError(f'{label} failed (exit {proc.returncode}). See {log}')
            report_progress(self.emit,label,'done')
            return tail
        except Exception as error:
            record['status']='cancelled' if isinstance(error,Cancelled) else 'failed';record['error']=str(error)
            report_progress(self.emit,label,record['status'],detail=str(error))
            raise
        finally:
            record['elapsed_s']=round(time.time()-record['started'],3)
            write_json(self.log_dir/'commands.json',self.records)



class Studio:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("RETROREMAKES_PS3_HOME", ROOT / "workspace")).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_path = self.home / "studio.json"
        self.state = read_json(self.state_path, {"version": VERSION, "projects": [], "settings": {
            "backend": str(self.home / "tools" / "ps3recomp"), "rpcs3": "", "jobs": 2}})
        # A hard interruption cannot leave a queue item permanently running.
        for p in self.state["projects"]:
            if p.get("status") in ("analysing", "building"):
                p["status"] = "interrupted"
            self.reconcile_native_receipt(p)
        self.detect_firmware()
        self.detect_dev_flash()
        self.save()

    @classmethod
    def read_only_snapshot(cls, home=None):
        """Open queue metadata without startup recovery, persistence or build changes.

        Companion tools may refresh ``state`` from disk while retaining their
        owned manual session registry. Only manual receipts/logs are written.
        """
        studio = cls.__new__(cls)
        studio.home = Path(home or os.environ.get("RETROREMAKES_PS3_HOME", ROOT / "workspace")).expanduser().resolve()
        studio.state_path = studio.home / "studio.json"
        studio.state = copy.deepcopy(read_json(studio.state_path, {"projects": [], "settings": {}}))
        studio._read_only_state = True
        return studio

    @property
    def read_only(self):
        return getattr(self, "_read_only_state", False)

    @property
    def state_lock(self):
        # Same-process protection only; companion tools never promote queue state.
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        return self._state_lock

    def save(self):
        if getattr(self, "_read_only_state", False):
            raise RuntimeError("This companion reads queue state without saving it")
        with self.state_lock:
            write_json(self.state_path, self.state)

    def detect_firmware(self, folder=None):
        """Find the local PUP without moving it or claiming RPCS3 installation."""
        settings = self.state["settings"]
        candidates = []
        if folder is not None:
            settings["firmware_folder"] = str(Path(folder).expanduser())
            settings.pop("firmware", None)
        saved_folder = settings.get("firmware_folder", "")
        if saved_folder:
            candidates.append(Path(saved_folder).expanduser() / "PS3UPDAT.PUP")
        elif settings.get("firmware"):
            candidates.append(Path(settings["firmware"]).expanduser())
        if not saved_folder:
            candidates.extend((ROOT / "PS3UPDAT.PUP", ROOT / "firmware" / "PS3UPDAT.PUP",
                               Path.home() / "Downloads" / "PS3UPDAT.PUP"))
            for parent in ROOT.parents:
                candidates.append(parent / "PS3 FW" / "PS3UPDAT.PUP")
        for candidate in candidates:
            try:
                with candidate.open("rb") as stream:
                    if stream.read(8) != b"SCEUF\x00\x00\x00":
                        continue
                settings["firmware"] = str(candidate.resolve())
                settings["firmware_folder"] = str(candidate.resolve().parent)
                return settings["firmware"]
            except OSError:
                continue
        settings.pop("firmware", None)
        return ""

    def detect_dev_flash(self):
        """Find installed firmware separately from the update PUP.

        Search only configured locations and direct RPCS3 siblings near Studio;
        never recurse through game libraries or scan an entire drive.
        """
        settings = self.state["settings"]
        candidates = []
        if settings.get("rpcs3"):
            candidates.append(Path(settings["rpcs3"]).expanduser().parent / "dev_flash")
        if settings.get("dev_flash"):
            candidates.append(Path(settings["dev_flash"]).expanduser())
        for parent in (ROOT.parent, ROOT.parent.parent):
            try:
                candidates.extend(child / "dev_flash" for child in sorted(parent.glob("rpcs3*"))
                                  if child.is_dir())
            except OSError:
                continue
        for candidate in candidates:
            try:
                if candidate.is_dir() and (candidate / "sys/external/flashMP3.pic").is_file():
                    settings["dev_flash"] = str(candidate.resolve())
                    return settings["dev_flash"]
            except OSError:
                continue
        return ""

    def add(self, source):
        src = Path(source).expanduser().resolve()
        if not src.exists():
            raise ValueError("Input does not exist")
        if src == self.home or self.home.is_relative_to(src if src.is_dir() else src.parent):
            raise ValueError("Keep the Studio workspace outside the game input directory")
        p = {"id": uuid.uuid4().hex[:12], "name": src.stem if src.is_file() else src.name,
             "source": str(src), "elf": "", "status": "queued", "native_exe": "", "attempt": ""}
        self.state["projects"].append(p)
        (self.project_dir(p) / "mods").mkdir(parents=True)
        self.save()
        return p

    @staticmethod
    def vfs_root_for(p, elf=None):
        """Disc root (the folder that holds PS3_GAME) for /dev_bdvd.

        The ELF path is authoritative: an imported game folder is often a
        wrapper around the real disc folder (e.g. "Game (USA)/Game (USA)/PS3_GAME"),
        and mapping /dev_bdvd to the wrapper makes every disc read miss, which
        leaves the title on a black screen with no audio.
        """
        candidates = []
        for raw in (elf, p.get("elf"), p.get("source")):
            if raw:
                path = Path(raw)
                candidates.append(path if path.is_dir() else path.parent)
        for start in candidates:
            for ancestor in (start, *start.parents):
                if ancestor.name.upper() == "PS3_GAME":
                    return ancestor.parent.resolve()
        for start in candidates:
            if not start.is_dir():
                continue
            if (start / "PS3_GAME").is_dir() or (start / "USRDIR").is_dir():
                return start.resolve()
            # Wrapper folder: descend into the single child that holds the disc.
            hits = [c for c in start.iterdir() if c.is_dir() and (c / "PS3_GAME").is_dir()]
            if len(hits) == 1:
                return hits[0].resolve()
        return candidates[0].resolve() if candidates else Path(".").resolve()

    @staticmethod
    def game_root(source):
        path=Path(source).expanduser().resolve()
        if path.is_file():path=path.parent
        for parent in (path,*path.parents):
            if parent.name.upper()=='PS3_GAME':return parent.parent
        if path.name.upper()=='USRDIR':return path.parent
        return path

    def import_elf_tree(self, folder, emit=print, cancel=None):
        """Queue every regular ELF by path, without changing existing candidates."""
        if self.read_only:
            raise RuntimeError("Open the main Studio to import ELF files")
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Select an existing games folder")
        if root == self.home or root.is_relative_to(self.home):
            raise ValueError("Select the original games folder, not the Studio workspace")
        cancel = cancel or threading.Event()
        def linked(path):
            st = path.lstat()
            return path.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & 0x400)
        paths, errors = [], []
        def scan_error(error):
            errors.append(dict(path=str(error.filename or root), error=str(error)))
        directories = 0
        for base, dirs, names in os.walk(root, followlinks=False, onerror=scan_error):
            if cancel.is_set():
                raise Cancelled("ELF import cancelled; no queue entries changed")
            parent = Path(base)
            kept = []
            for name in sorted(dirs, key=str.casefold):
                child = parent / name
                try:
                    if not linked(child) and child.resolve() != self.home:
                        kept.append(name)
                except OSError as error:
                    errors.append(dict(path=str(child), error=str(error)))
            dirs[:] = kept
            directories += 1
            if directories == 1 or directories % 50 == 0:
                emit(f"Scanning ELF subfolders: {directories} folders; {len(paths)} ELF files found")
            for name in sorted(names, key=str.casefold):
                if Path(name).suffix.casefold() != '.elf':
                    continue
                path = parent / name
                try:
                    if not linked(path) and path.is_file():
                        paths.append(path.resolve())
                except OSError as error:
                    errors.append(dict(path=str(path), error=str(error)))
        if cancel.is_set():
            raise Cancelled("ELF import cancelled; no queue entries changed")
        result = dict(folder=str(root), found=len(paths), added=[], duplicates=[], errors=errors)
        with self.state_lock:
            existing = set()
            for project in self.state['projects']:
                try:
                    existing.add(str(choose_elf(project['source'], project.get('elf', ''))).casefold())
                except (OSError, ValueError):
                    pass
            additions = []
            for path in paths:
                if cancel.is_set():
                    raise Cancelled("ELF import cancelled; no queue entries changed")
                key = str(path).casefold()
                if key in existing:
                    result['duplicates'].append(str(path))
                    continue
                game = self.game_root(path)
                name = f"{game.name} / {path.relative_to(game)}" if game != path.parent else str(path.relative_to(root))
                project = dict(id=uuid.uuid4().hex[:12], name=name, source=str(path), elf=str(path),
                               status='queued', native_exe='', attempt='')
                additions.append(project)
                existing.add(key)
                result['added'].append(dict(project_id=project['id'], path=str(path)))
            # Commit only after the complete scan/dedup pass; no partial queue on cancellation.
            self.state['projects'].extend(additions)
            self.state['settings']['elf_import_folder'] = str(root)
            self.save()
        report = self.home / 'elf-imports' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
        write_json(report, result)
        result['report'] = str(report)
        emit(f"ELF import complete: {result['found']} found; {len(result['added'])} queued; {len(result['duplicates'])} already in library; {len(errors)} scan errors")
        return result

    def scan_games_folder(self, folder, emit=print, cancel=None, import_games=True):
        # Hashes are cached by path + size + modified time, so a rescan only reads new or changed files.
        cache_path=self.home/'hash-cache.json'
        try:hash_cache=json.loads(cache_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):hash_cache={}
        def sha256(path,cancel=None):
            st=Path(path).stat();key=f"{str(path).casefold()}|{st.st_size}|{st.st_mtime_ns}"
            if key not in hash_cache:hash_cache[key]=globals()['sha256'](path,cancel)
            return hash_cache[key]
        try:
            return self._scan_games_folder(folder,emit,cancel,import_games,sha256)
        finally:
            try:write_json(cache_path,hash_cache)
            except OSError:pass

    def _scan_games_folder(self, folder, emit, cancel, import_games, sha256):
        root=Path(folder).expanduser().resolve()
        if not root.is_dir():raise ValueError('Select an existing games folder')
        if root==self.home or self.home.is_relative_to(root):
            raise ValueError('Keep the games library separate from the Studio workspace')
        def check_cancel():
            if cancel and cancel.is_set():raise Cancelled('Games-folder scan cancelled; already imported games retained')
        # Prefer the established game project over manually queued copies of its EBOOT.
        projects=sorted(self.state['projects'],key=lambda p:(not bool(p.get('attempt')),not bool(p.get('native_exe')),
                        Path(p['source']).is_file(),not bool(p.get('elf'))))
        existing_roots={}
        for project in projects:existing_roots.setdefault(str(self.game_root(project['source'])).casefold(),project)
        existing_hashes={}
        emit(f'Checking {len(projects)} games already in the queue...')
        for number,project in enumerate(projects,1):
            check_cancel()
            if number%10==0:emit(f'Checked {number}/{len(projects)} queued games')
            try:existing_hashes.setdefault(sha256(choose_elf(project['source'],project.get('elf','')),cancel),project)
            except (ValueError,OSError):pass
            game=self.game_root(project['source'])
            for rel in ('PS3_GAME/USRDIR/EBOOT.BIN','USRDIR/EBOOT.BIN','EBOOT.BIN'):
                main=game/rel
                if main.is_file():
                    try:existing_hashes.setdefault(sha256(main,cancel),project)
                    except OSError:pass
        groups={};all_files=[];errors=[];known_roots={};file_hashes={}
        excluded={'.git','.venv','__pycache__','ps3_update','workspace','recompiled','generated'}
        def onerror(error):errors.append(dict(path=str(error.filename),error=str(error)))
        emit('Searching the games folder for ELF / EBOOT files...')
        for current,dirs,files in os.walk(root,followlinks=False,onerror=onerror):
            check_cancel();path=Path(current)
            dirs[:]=sorted((d for d in dirs if d.casefold() not in excluded
                           and not (path/d).is_symlink()
                           and not getattr(path/d,'is_junction',lambda:False)()
                           and (path/d).resolve()!=self.home),key=str.casefold)
            names={d.casefold() for d in dirs}
            if 'ps3_game' in names or ('usrdir' in names and any(n.casefold()=='param.sfo' for n in files)):
                known_roots[str(path).casefold()]=path
            for name in sorted(files,key=str.casefold):
                check_cancel()
                if not (Path(name).suffix.casefold()=='.elf' or name.casefold()=='eboot' or name.casefold().startswith('eboot.')):continue
                elf=path/name
                if elf.is_symlink():continue
                game=None
                for parent in (path,*path.parents):
                    if parent.name.casefold()=='ps3_game':game=parent.parent;break
                    if str(parent).casefold() in known_roots:game=parent;break
                    if parent==root:break
                item=dict(path=str(elf),root=str(game or elf),grouped=game is not None)
                all_files.append(item)
                try:
                    binary=inspect_binary(elf)
                    if binary.get('format')=='other':raise ValueError(binary['reason'])
                    item.update(elf=str(elf),native_input=binary['native_input'],format=binary.get('format','unknown'),
                                input_sha256=sha256(elf,cancel))
                    if item['input_sha256'] in file_hashes:item['duplicate_of']=file_hashes[item['input_sha256']]
                    else:file_hashes[item['input_sha256']]=str(elf)
                    key=str(game or elf).casefold()
                    groups.setdefault(key,dict(root=str(game or elf),grouped=game is not None,inputs=[]))['inputs'].append(item)
                except (ValueError,OSError) as error:
                    item.update(disposition='error',error=str(error))
                    errors.append(dict(path=str(elf),error=str(error)))
        result=dict(folder=str(root),found=0,files_found=len(all_files),files=all_files,added=[],duplicates=[],
                    errors=errors,needs_decrypted_elf=[],updated=[])
        seen={};discovered_groups=set();canonical_keys={}
        for group in groups.values():
            check_cancel()
            # Prefer decrypted PPU inputs, then the main executable, without discarding alternates.
            inputs=group['inputs']
            selected=min(inputs,key=lambda x:(not x['native_input'],Path(x['elf']).stem.casefold()!='eboot',x['elf'].casefold()))
            game=Path(group['root']);title=game.name if group['grouped'] else game.stem
            if group['grouped']:
                for rel in ('PS3_GAME/PARAM.SFO','PARAM.SFO'):
                    sf=game/rel
                    if sf.is_file():
                        try:title=parse_sfo(sf).get('TITLE',title)
                        except (ValueError,OSError):pass
                        break
            item=dict(root=str(game),elf=selected['elf'],title=title,native_input=selected['native_input'],
                      format=selected['format'],input_sha256=selected['input_sha256'],inputs=inputs)
            digest=item['input_sha256'];identity=str(game).casefold()
            main_paths={str(game/rel).casefold() for rel in ('PS3_GAME/USRDIR/EBOOT.BIN','PS3_GAME/USRDIR/EBOOT.elf','USRDIR/EBOOT.BIN','USRDIR/EBOOT.elf','EBOOT.BIN','EBOOT.elf')}
            aliases={digest}|{x['input_sha256'] for x in inputs if x['path'].casefold() in main_paths}
            project=existing_roots.get(identity) if group['grouped'] else None
            project=project or next((existing_hashes[h] for h in sorted(aliases) if h in existing_hashes),None)
            duplicate=next((seen[h] for h in sorted(aliases) if h in seen),None)
            if duplicate and not project and duplicate.get('project_id'):
                project=next((p for p in self.state['projects'] if p['id']==duplicate['project_id']),None)
            canonical=duplicate or item
            discovered_groups.add(canonical_keys.setdefault(id(canonical),project['id'] if project else canonical['input_sha256']))
            for alias in aliases:seen[alias]=canonical
            if duplicate or project:
                item['duplicate_reason']='Game root or identical executable already in queue' if project else 'Identical executable already found in this scan'
                if project:
                    item['project_id']=project['id']
                    for entry in inputs:entry.update(disposition='associated',duplicate_reason=item['duplicate_reason'])
                    if import_games:
                        # Discovery never replaces an existing build, explicit ELF, status or profile.
                        prior={x['path']:x for x in project.get('discovered_inputs',[])}
                        prior.update({x['path']:dict(x) for x in inputs})
                        project['discovered_inputs']=list(prior.values())
                        if item['native_input'] and not project.get('native_exe') and not project.get('attempt'):
                            try:current_native=inspect_binary(choose_elf(project['source'],project.get('elf','')))['native_input']
                            except (ValueError,OSError):current_native=False
                            if not current_native or not project.get('elf'):
                                project['elf']=item['elf'];project['status']='ready for attempt'
                                item['selected_decrypted_elf']=True
                    result['updated'].append(item)
                # A later copy can supply the decrypted input for a game first
                # discovered as encrypted. Reconcile the group summary only;
                # retain each file's independent inspection/discovery evidence.
                if duplicate and not canonical['native_input'] and item['native_input']:
                    for field in ('elf','native_input','format','input_sha256'):
                        canonical[field]=item[field]
                    canonical['inputs']=canonical['inputs']+inputs
                result['duplicates'].append(item)
                for entry in inputs:entry.update(disposition='associated' if project else 'duplicate',duplicate_reason=item['duplicate_reason'])
                emit('Already in queue: '+title if project else 'Duplicate: '+title)
                continue
            result['found']+=1;seen[digest]=item
            if import_games:
                project=self.add(game);project['name']=title
                project['elf']=item['elf'] if item['native_input'] else ''
                project['status']='ready for attempt' if item['native_input'] else 'needs decrypted ELF'
                item['project_id']=project['id']
            for entry in inputs:entry['disposition']='selected' if entry is selected else 'associated'
            if import_games:project['discovered_inputs']=[dict(x) for x in inputs]
            result['added'].append(item)
            if not item['native_input']:result['needs_decrypted_elf'].append(title)
            emit(('Added: ' if import_games else 'Found: ')+title)
        # found counts discovered unique groups, including games already queued.
        result['found']=len(discovered_groups)
        result['needs_decrypted_elf']=[item['title'] for item in result['added'] if not item['native_input']]
        if import_games:
            settings=self.state['settings']
            known=list(settings.get('games_folders') or [])
            if settings.get('games_folder') and settings['games_folder'] not in known:known.insert(0,settings['games_folder'])
            if str(root).casefold() not in [k.casefold() for k in known]:known.append(str(root))
            settings['games_folders']=known
            settings.setdefault('games_folder',str(root))
            if not settings.get('games_folder'):settings['games_folder']=str(root)
            self.save()
            write_json(self.home/'game-folder-imports'/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.json'),result)
        return result

    def project_dir(self, p):
        # Canonical output paths keep future builds on the configured relocated drive.
        return (self.home / "projects" / p["id"]).resolve()

    def analyse(self, p, emit=print, cancel=None):
        report_progress(emit,'inspect','start')
        p["status"] = "analysing"
        self.save()
        try:
            elf = choose_elf(p["source"], p.get("elf", ""))
            emit(f"Inspecting {elf}")
            binary = inspect_binary(elf)
            report = {"studio_version": VERSION, "input": str(elf), "sha256": sha256(elf, cancel),
                      "binary": binary, "inventory": inventory(p["source"], cancel=cancel), "metadata": {}}
            base = Path(p["source"])
            base = base if base.is_dir() else base.parent
            for rel in ("PS3_GAME/PARAM.SFO", "PARAM.SFO", "../PARAM.SFO"):
                if (base / rel).is_file():
                    try:
                        report["metadata"] = parse_sfo(base / rel)
                    except ValueError as e:
                        report["metadata_error"] = str(e)
                    break
            p["name"] = report["metadata"].get("TITLE", p["name"])
            p["status"] = "ready for attempt" if binary["native_input"] else "needs decrypted ELF"
            p["last_analysis"] = str(self.project_dir(p) / "analysis.json")
            write_json(p["last_analysis"], report)
            emit(binary["reason"])
            report_progress(emit,'inspect','done')
            return report
        except Exception as e:
            p["status"], p["error"] = "cancelled" if isinstance(e, Cancelled) else "analysis blocked", str(e)
            raise
        finally:
            self.save()

    def backend_python(self):
        env = self.home / "tools" / "python-env"
        p = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return str(p if p.is_file() else Path(sys.executable))

    def setup_backend(self, emit=print, cancel=None):
        runner = Runner(self.home / "setup-logs", emit, cancel)
        backend = Path(self.state["settings"]["backend"]).expanduser().resolve()
        if backend == self.home or self.home.is_relative_to(backend):
            raise ValueError("Backend directory must not contain the Studio workspace")
        if not shutil.which("git"):
            raise RuntimeError("Install Git for Windows, then reopen the Studio")
        if not backend.exists():
            backend.parent.mkdir(parents=True, exist_ok=True)
            runner.run("clone", ["git", "clone", "--no-checkout", BACKEND_URL, backend])
            runner.run("checkout", ["git", "-C", backend, "checkout", "--detach", BACKEND_REV])
        else:
            report_progress(emit,'clone','done',detail='Existing backend reused')
            report_progress(emit,'checkout','done',detail='Existing checkout preserved')
        # Never reset an existing checkout or erase local backend modifications.
        self.validate_backend(backend)
        from .backend_overlay import apply as apply_backend_overlay
        apply_backend_overlay(backend, emit)
        env = self.home / "tools" / "python-env"
        if not (env / "pyvenv.cfg").exists():
            runner.run("python-env", [sys.executable, "-m", "venv", env])
        else:report_progress(emit,'python-env','done',detail='Existing Python environment reused')
        req = backend / "tools" / "requirements.txt"
        if not req.is_file():
            req = backend / "requirements.txt"
        runner.run("dependencies", [self.backend_python(), "-m", "pip", "install", "-r", req], timeout=1800)
        emit("Backend and isolated Python dependencies ready. Run Check environment before building.")

    def validate_backend(self, backend):
        for name in ("tools/ppu_loader.py", "tools/ppu_lifter.py", "tools/build_spu_workloads.py",
                     "templates/project/CMakeLists.txt", "templates/project/main.cpp"):
            if not (backend / name).is_file():
                raise RuntimeError(f"Backend is missing {name}; install ps3recomp or select a complete checkout")

    def doctor(self):
        result = {"studio": VERSION, "python": sys.version, "platform": platform.platform(),
                  "workspace": str(self.home), "backend_python": self.backend_python(), "tools": {}}
        for name in ("git", "cmake", "ninja", "clang-cl" if os.name == "nt" else "clang", "gcc"):
            result["tools"][name] = shutil.which(name)
        backend = Path(self.state["settings"]["backend"]).expanduser().resolve()
        try:
            self.validate_backend(backend)
            result["backend"] = self.provenance(backend)
        except RuntimeError as e:
            result["backend_error"] = str(e)
        cp = subprocess.run([self.backend_python(), "-c", "import capstone, construct; print('ready')"],
                            capture_output=True, text=True, timeout=20)
        result["python_dependencies"] = cp.returncode == 0
        result["note"] = "On Windows, launch through START-STUDIO.bat to load Visual Studio C++ headers/libraries. Linux also requires SDL2 development files. A tool on PATH does not prove the whole C++ toolchain works."
        write_json(self.home / "environment.json", result)
        return result

    @staticmethod
    def provenance(backend):
        def git(*args):
            cp = subprocess.run(["git", "-C", str(backend), *args], capture_output=True, text=True, timeout=15)
            return cp.stdout.strip() if cp.returncode == 0 else "unknown"
        return {"path": str(backend), "revision": git("rev-parse", "HEAD"),
                "working_tree_changes": git("status", "--porcelain"), "expected_revision": BACKEND_REV}

    def preserve_native_candidate(self, p, reason):
        candidate = {key: p[key] for key in ("native_exe", "attempt", "native_runtime_receipt") if p.get(key)}
        if candidate.get("native_exe") or candidate.get("native_runtime_receipt"):
            history = p.setdefault("native_candidate_history", [])
            if not history or any(history[-1].get(k) != v for k, v in candidate.items()):
                history.append({**candidate, "reason": reason, "preserved_at": time.time()})

    def reconcile_native_receipt(self, p):
        """Retire old diagnostic metadata only for a proven fresh ordinary build."""
        if not p.get("native_runtime_receipt") or not p.get("native_exe") or not p.get("attempt"):
            return False
        try:
            attempt, exe = Path(p["attempt"]).resolve(), Path(p["native_exe"]).resolve()
            expected = attempt / "build/player" / ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp")
            if exe != expected or not exe.is_file():
                return False
            report = read_json(attempt / "build-report.json", {})
            if report.get("status") != "compiled; gameplay unverified" or report.get("private_native_adapter"):
                return False
            elf = choose_elf(p["source"], p.get("elf", ""))
            input_hash = sha256(elf)
            if report.get("input_sha256") != input_hash:
                return False
            path = Path(p["native_runtime_receipt"]).resolve()
            if not any(path.is_relative_to(root.resolve()) for root in (ROOT / "verification", self.home / "verification")):
                return False
            if path.stat().st_size > 65536:
                return False
            old = read_json(path, {})
            if not old.get("diagnostic_env") or old.get("input_sha256") != input_hash or not old.get("executable"):
                return False
            if Path(old["executable"]).resolve() == exe:
                return False
            previous = {"native_exe": old["executable"], "native_runtime_receipt": str(path),
                        "reason": "superseded by a fresh ordinary build", "preserved_at": time.time()}
            if old.get("attempt"):
                previous["attempt"] = old["attempt"]
            p.setdefault("native_candidate_history", []).append(previous)
            p.pop("native_runtime_receipt", None)
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def bind_private_native_candidate(self, p, attempt, exe, input_hash, adapter):
        if not adapter or adapter.get("applied") is False:
            return None
        if adapter.get("input_sha256") != input_hash or not adapter.get("adapter_id") or not adapter.get("private"):
            raise RuntimeError("Private native adapter returned an invalid input binding")
        receipt = {"adapter_id": adapter["adapter_id"], "private": True,
                   "input_sha256": input_hash, "executable": str(exe.resolve()),
                   "executable_sha256": sha256(exe), "attempt": str(attempt.resolve()),
                   "adapter_receipt": str((attempt / "private-native-adapter.json").resolve()),
                   "diagnostic_env": {"SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": "1000000"},
                   "mode": "hybrid-SPU", "playability": "unverified"}
        path = self.home / "verification/native-runtime" / p["id"] / (uuid.uuid4().hex + ".json")
        write_json(path, receipt)
        p["native_runtime_receipt"] = str(path.resolve())
        return str(path.resolve())

    def build_upstream_port(self, p, report, route, emit=print, cancel=None):
        """Build a pinned upstream per-title port (studio/upstream_ports.py) from the player's own game."""
        from . import upstream_ports
        cancel = cancel or threading.Event()
        attempt = self.project_dir(p) / "attempts" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        attempt.mkdir(parents=True)
        self.preserve_native_candidate(p, "new upstream port attempt started")
        p.pop("native_runtime_receipt", None)
        p["attempt"], p["status"], p["native_exe"] = str(attempt), "building", ""
        p.pop("error", None)
        self.save()
        runner = Runner(attempt / "logs", emit, cancel)
        manifest = {"studio": VERSION, "input": report["input"], "input_sha256": report["sha256"],
                    "route": route["id"], "status": "building", "playability": "unverified"}
        write_json(attempt / "build-report.json", manifest)
        emit(f"{route['title']}: building the pinned upstream port {route['repository']} @ {route['commit'][:12]} "
             "on this PC from your own game files.")
        try:
            report_progress(emit,'prepare','start')
            result = upstream_ports.run(self, p, report, attempt, runner, emit, cancel)
            manifest.update(result)
            manifest["executable_sha256"] = sha256(result["native_exe"])
            manifest["status"], p["status"], p["native_exe"] = "compiled; gameplay unverified", "compiled; untested", result["native_exe"]
            emit("Upstream port compiled. Playability, graphics, audio and correctness remain unverified until tested.")
            return manifest
        except Exception as e:
            manifest["status"] = "cancelled" if isinstance(e, Cancelled) else "blocked"
            manifest["error"] = str(e)
            p["status"], p["error"] = manifest["status"], str(e)
            emit(str(e))
            raise
        finally:
            manifest["commands"] = runner.records
            write_json(attempt / "build-report.json", manifest)
            self.save()
            if manifest["status"] == "compiled; gameplay unverified":report_progress(emit,'verify','done')

    def build(self, p, emit=print, cancel=None, stop_after="build", reuse=False):
        cancel = cancel or threading.Event()
        report = self.analyse(p, emit, cancel)
        if not report["binary"]["native_input"]:
            raise RuntimeError(report["binary"]["reason"])
        from . import upstream_ports
        route = upstream_ports.route_for(report["sha256"])
        if route and not reuse:
            return self.build_upstream_port(p, report, route, emit, cancel)
        note = upstream_ports.unrouted_note(report["sha256"])
        if note:
            emit(note)
        report_progress(emit,'prepare','start')
        backend = Path(self.state["settings"]["backend"]).expanduser().resolve()
        trial = p.get("backend_override")   # per-game backend trial (auto loop)
        from . import patch_packs
        pack = None if trial else patch_packs.for_project(self, p, report)
        if pack:
            emit(f"Using patch pack: {pack['name']} {pack['version']}".rstrip())
            own = patch_packs.prepare_backend(self, pack, emit, cancel)
            if own:
                trial = str(own)
        if trial and Path(trial).is_dir():
            backend = Path(trial).expanduser().resolve()
            emit(f"Using trial backend for this game: {backend.name}")
        self.validate_backend(backend)
        from .backend_overlay import apply as apply_backend_overlay
        if not trial:
            apply_backend_overlay(backend, emit)
        if cancel.is_set():
            raise Cancelled("Cancelled")
        if reuse:
            attempt = Path(p.get("attempt", ""))
            prior = read_json(attempt / "build-report.json", {})
            if not (attempt / "port/recompiled/ppu_recomp.h").is_file():
                raise RuntimeError("No generated attempt to rebuild. Generate sources or attempt a build first.")
            if prior.get("input_sha256") != report["sha256"]:
                raise RuntimeError("Input ELF changed. Start a new attempt rather than rebuilding mismatched sources.")
        else:
            attempt = self.project_dir(p) / "attempts" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            attempt.mkdir(parents=True)
        self.preserve_native_candidate(p, "rebuild started" if reuse else "new attempt started")
        p.pop("native_runtime_receipt", None)
        p["attempt"], p["status"], p["native_exe"] = str(attempt), "building", ""
        p.pop("error", None)
        self.save()
        log_dir = attempt / "logs"
        if reuse:
            stamp = "rebuild-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
            log_dir = log_dir / stamp
            write_json(attempt / "history" / (stamp + ".json"), prior)
        runner = Runner(log_dir, emit, cancel)
        manifest = {"studio": VERSION, "input": report["input"], "input_sha256": report["sha256"],
                    "backend": self.provenance(backend), "status": "building", "playability": "unverified",
                    "patch_pack": patch_packs.summary(pack)}
        write_json(attempt / "build-report.json", manifest)
        try:
            py, elf = self.backend_python(), Path(report["input"])
            out, port = attempt / "loader", attempt / "port"
            from . import game_patches
            if reuse:
                prior_patches = prior.get("game_patches") or {}
                if prior_patches.get("patched_elf") and Path(prior_patches["patched_elf"]).is_file():
                    elf, manifest["game_patches"] = Path(prior_patches["patched_elf"]), prior_patches
            else:
                elf, patch_record = game_patches.prepare_patched_elf(self.home, self.state["settings"], p, elf,
                                                                     attempt, report.get("metadata"))
                if patch_record:
                    manifest["game_patches"] = patch_record
                    write_json(attempt / "build-report.json", manifest)
                    emit("Game patches applied to a copy of the ELF: " +
                         ", ".join(a["name"] for a in patch_record["applied"]))
            fulton_plan = None
            if not reuse:
                from . import fulton
                fulton_plan = fulton.collect(self, p, report, attempt, emit, cancel)
                manifest["fulton_base_plan"] = str(attempt / "fulton-base-plan.json")
                shutil.copytree(backend / "templates" / "project", port,
                                ignore=shutil.ignore_patterns("build", "recompiled", ".git", "__pycache__"))
                report_progress(emit,'prepare','done')
                runner.run("ppu-load", [py, backend / "tools/ppu_loader.py", elf, "-o", out], cwd=backend)
            if fulton_plan is not None:
                fulton.apply_loader_inputs(fulton_plan, report, backend, out, elf)
                write_json(attempt / "fulton-base-plan.json", fulton_plan)
                for warning in fulton_plan["warnings"]: emit("Fulton: " + warning)
            funcs = out / (elf.stem + ".functions.json")
            imports = out / (elf.stem + ".imports.json")
            if not funcs.is_file() or not read_json(funcs, []):
                if reuse:
                    raise RuntimeError("Supply corrected function metadata in the loader directory before rebuilding")
                emit("No OPD functions recovered; attempting heuristic function discovery.")
                runner.run("function-discovery", [py, backend / "tools/find_functions.py", elf, "--output", funcs], cwd=backend)
                if not read_json(funcs, []):
                    raise RuntimeError("OPD and heuristic discovery found no functions. Supply corrected boundaries in loader/*.functions.json; an empty lift cannot form a port.")
                manifest["function_boundaries"] = "heuristic; requires verification"
            if not imports.is_file():
                raise RuntimeError("Loader did not produce the import table")
            recomp = port / "recompiled"
            if not reuse:
                runner.run("ppu-lift", [py, backend / "tools/ppu_lifter.py", elf,
                           "--functions", funcs, "--hle-stubs", imports, "--jobs", str(max(1, min(32, int(self.state["settings"].get("jobs", 2))))), "--output", recomp], cwd=backend)
            if not (recomp / "ppu_recomp.h").is_file() or not list(recomp.glob("*.cpp")):
                raise RuntimeError("Lifter returned without the required generated sources")
            # Scan all loose decrypted ELF/PRX candidates, not only EBOOT.
            inputs = [str(elf)]
            for b in report["inventory"]["binaries"]:
                if b.get("format") == "ELF" and b["path"] not in inputs:
                    inputs.append(b["path"])
            spu = port / "spu"
            cmd = [py, backend / "tools/build_spu_workloads.py"]
            for i in inputs:
                cmd.extend(["--input", i])
            cmd.extend(["--lifted", spu, "--out", spu / "spu_workloads.c", "--constructor"])
            if p.get("spu_build_anyway"):
                # Opt-in per game ("Build blocked games anyway"): accept SPU
                # programs whose only problem is data words in executable
                # sections; each one halts its SPU program if ever executed.
                cmd.append("--allow-data-words")
                manifest["spu_build_anyway"] = True
            retry_reason = spu_retry_reason(spu, prior) if reuse else None
            if not reuse or retry_reason:
                if reuse:
                    preserved = attempt / "history" / stamp / "spu"
                    preserve_spu_retry(spu, preserved, cancel)
                    manifest["spu_retry"] = {"reason": retry_reason, "preserved_sources": str(preserved)}
                    emit("Retrying SPU discovery: " + retry_reason + "; previous sources retained in history.")
                try:
                    runner.run("spu-discover-lift", cmd, cwd=backend)
                except RuntimeError:
                    # Automatic build-anyway: when the only problems are data words inside
                    # SPU code (and the branches they decode to), rebuild with them halting.
                    inv = read_json(spu / "inventory.json", {}) or {}
                    allowed = ("unsupported instructions emitted by lifter", "registration omitted because coverage is incomplete",
                               "direct branch/call targets lack generated function entries")
                    reasons = {str(x.get("reason")) for x in inv.get("programs", []) if x.get("status") not in ("registered", "generated")}
                    reasons |= {str(x.get("reason")) for x in inv.get("programs", [])
                                if x.get("status") == "generated" and x.get("reason")}
                    if "--allow-data-words" in cmd or not reasons or not reasons <= set(allowed):
                        raise
                    emit("SPU programs contain data inside code; rebuilding them in build-anyway mode automatically.")
                    cmd.append("--allow-data-words")
                    p["spu_build_anyway"] = True
                    manifest["spu_build_anyway"] = "automatic"
                    runner.run("spu-discover-lift-anyway", cmd, cwd=backend)
            manifest["spu_inputs"] = inputs
            adapter = None
            from . import private_native_adapter
            if (private_native_adapter.PACKAGE_ROOT / "manifest.json").is_file():
                # Local-only legacy package (never distributed); keeps this
                # machine's verified MotorStorm builds byte-identical.
                try:
                    adapter = private_native_adapter.apply(port, elf, backend, p)
                except Exception as error:
                    # The legacy package pins exact backend file hashes, so any later runtime
                    # fix makes it refuse. The recipe below generates the same policy code
                    # from the user's EBOOT and works with the current backend.
                    emit(f"Legacy MotorStorm package skipped ({str(error)[:120]}); using the build recipe instead.")
                    adapter = None
            if adapter is None:
                # Recipes regenerate game-specific SPU code from the user's own
                # EBOOT at build time; the Studio ships no game code.
                from .wws_policy_recipes import apply as apply_wws_recipe
                adapter = apply_wws_recipe(port, elf, backend, p)
            if adapter and adapter.get("applied") is not False:
                if adapter.get("input_sha256") != report["sha256"]:
                    raise RuntimeError("Private native adapter input hash differs from this attempt")
                write_json(attempt / "private-native-adapter.json", adapter)
                manifest["private_native_adapter"] = adapter
            # SPURS job binaries kept raw inside the EBOOT (job CRT format): SPU
            # discovery finds only ELFs, so lift them here from the user's EBOOT.
            try:
                from .spurs_job_recipes import apply as apply_spurs_jobs
                inventory = read_json(spu / "inventory.json", {}) or {}
                known = {int(str(x.get("fingerprint")), 16) for x in inventory.get("programs", [])
                         if x.get("fingerprint") and x.get("status") == "registered"}
                jobs = apply_spurs_jobs(port, elf, backend, known)
                if jobs:
                    manifest["spurs_job_binaries"] = jobs
                    if jobs.get("applied"):
                        emit(f"Lifted {len(jobs['jobs'])} SPURS job binary(ies) embedded in the EBOOT.")
            except Exception as error:
                emit(f"SPURS job binaries were not lifted: {str(error)[:200]}")
            # WWS SPU job code modules linked into the EBOOT (C0DEC0DE format):
            # lift them so job code runs natively instead of on the SPU interpreter.
            try:
                from .wws_jobmod_recipes import apply as apply_wws_jobmods
                jobmods = apply_wws_jobmods(port, elf, backend, p, emit)
                if jobmods:
                    manifest["wws_job_modules"] = {k: v for k, v in jobmods.items() if k != "modules"}
                    emit(f"Lifted {jobmods['lifted']} of {jobmods['found']} WWS job module(s) embedded in the EBOOT.")
            except Exception as error:
                emit(f"WWS job modules were not lifted: {str(error)[:200]}")
            # SPU code the game builds in local store at run time, captured by
            # earlier test runs on this machine: lift it so it stops running on
            # the SPU interpreter (byte-guarded at run time).
            try:
                from .spu_snapshot_recipes import apply as apply_spu_snapshots
                snaps = apply_spu_snapshots(port, self.project_dir(p), self.home / "game-fixes" / p["id"], backend, emit)
                if snaps:
                    manifest["spu_snapshots"] = snaps
            except Exception as error:
                emit(f"Runtime SPU snapshots were not lifted: {str(error)[:200]}")
            # Game-shipped PRX modules (USRDIR .sprx the EBOOT loads at run time):
            # decrypt, relocate and lift them from the user's own files so
            # sys_prx_load_module can hand them back. Nothing for other games.
            try:
                from .game_prx import apply as apply_game_prx
                modules = apply_game_prx(port, self.vfs_root_for(p), elf, backend, self.home,
                                         self.state.get("settings", {}), py, emit)
                if modules:
                    manifest["game_prx_modules"] = modules
            except Exception as error:
                emit(f"Game PRX modules were not recompiled: {str(error)[:200]}")
            if stop_after == "lift":
                manifest["status"], p["status"] = "sources generated", "sources generated"
                return manifest
            if pack and not reuse:
                patch_packs.apply_project(pack, port, emit)
            for tool in ("cmake", "ninja"):
                if not shutil.which(tool):
                    raise RuntimeError(f"Missing {tool}. Generated sources retained; install build prerequisites and retry.")
            cfg = ["cmake", "-S", port, "-B", attempt / "build", "-G", "Ninja",
                   f"-DPS3RECOMP_DIR={backend}", f"-DRECOMP_DIR={recomp}", f"-DSPU_DIR={spu}",
                   f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={attempt / 'build/player'}",
                   "-DCMAKE_BUILD_TYPE=Release", "-DPS3RECOMP_BUILD_TESTS=OFF",
                   f"-DPython3_EXECUTABLE={py}"]
            if os.name == "nt":
                if not shutil.which("clang-cl"):
                    raise RuntimeError("Install Visual Studio C++ Build Tools with Clang tools, then run START-STUDIO.bat")
                cfg += ["-DCMAKE_C_COMPILER=clang-cl", "-DCMAKE_CXX_COMPILER=clang-cl"]
            elif shutil.which("clang") and shutil.which("clang++"):
                cfg += ["-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++"]
            runner.run("cmake-configure", cfg, cwd=port)
            jobs = max(1, min(32, int(self.state["settings"].get("jobs", 2))))
            try:
                runner.run("cmake-build", ["cmake", "--build", attempt / "build", "--parallel", str(jobs)], cwd=port)
            except RuntimeError:
                # Huge lifted TUs (Killzone 2's are ~40 MB each) can exhaust RAM
                # when several compile at once, especially beside another build.
                # Ninja keeps every finished object, so resume one at a time.
                log = runner.log_dir / runner.records[-1]["log"]
                oom_retry_ok = False
                if jobs > 1 and compiler_ran_out_of_memory(log):
                    emit(f"Compiler ran out of memory with {jobs} parallel jobs; resuming one file at a time (finished files are kept).")
                    try:
                        runner.run("cmake-build-1job", ["cmake", "--build", attempt / "build", "--parallel", "1"], cwd=port)
                        oom_retry_ok = True
                    except RuntimeError:
                        # Still out of memory one file at a time (another build is
                        # using the RAM): fall through and rebuild the failing
                        # file(s) with lower optimisation, which needs far less.
                        log = runner.log_dir / runner.records[-1]["log"]
                        jobs = 1
                if not oom_retry_ok:
                    # The compiler itself can crash on one giant lifted function at full
                    # optimisation. Rebuild only the crashing files with lower optimisation.
                    level_by_round = ("/O1", "/Od")
                    for round_number, level in enumerate(level_by_round, 1):
                        crashed = compiler_crashed_sources(log)
                        if not crashed:
                            raise
                        emit(f"Compiler crashed on {len(crashed)} file(s); rebuilding them with {level} (other files keep full optimisation).")
                        lines = [f'set_source_files_properties("{src}" PROPERTIES COMPILE_OPTIONS "{level}")' for src in crashed]
                        with open(port / "CMakeLists.txt", "a", encoding="utf-8") as fh:
                            fh.write("\n# Studio: compiler crash recovery\n" + "\n".join(lines) + "\n")
                        runner.run(f"cmake-configure-crashfix{round_number}", cfg, cwd=port)
                        try:
                            runner.run(f"cmake-build-crashfix{round_number}", ["cmake", "--build", attempt / "build", "--parallel", str(jobs)], cwd=port)
                            break
                        except RuntimeError:
                            log = runner.log_dir / runner.records[-1]["log"]
                            if round_number == len(level_by_round):
                                raise
            report_progress(emit,'verify','start')
            exe = attempt / "build/player" / ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp")
            if not exe.is_file():
                raise RuntimeError("Build finished but the expected MyGameRecomp executable is missing; inspect the template target")
            controller_runtime=ROOT/'runtime/controller'
            for name in ('SDL2.dll','SDL2-LICENSE.txt'):
                source=controller_runtime/name
                if source.is_file():shutil.copy2(source,exe.parent/name)
            if (backend/'libs/input/rr_input_profile.h').is_file():
                manifest['controller_profiles']=dict(supported=True,types=['DualShock 4 via SDL2','XInput','Keyboard'],profile=str(self.home/'controller-profiles/default.ini'))
            runtime_receipt = self.bind_private_native_candidate(p, attempt, exe, report["sha256"], adapter)
            if runtime_receipt:
                manifest["native_runtime_receipt"] = runtime_receipt
            manifest["status"], p["status"], p["native_exe"] = "compiled; gameplay unverified", "compiled; untested", str(exe)
            emit("Native executable compiled. Playability, graphics, audio and correctness remain unverified until tested.")
            return manifest
        except Exception as e:
            manifest["status"] = "cancelled" if isinstance(e, Cancelled) else "blocked"
            manifest["error"] = str(e)
            p["status"], p["error"] = manifest["status"], str(e)
            emit(str(e))
            raise
        finally:
            manifest["commands"] = runner.records
            write_json(attempt / "build-report.json", manifest)
            write_json(attempt / "HANDOFF.json", {"project": p, "build": manifest,
                "next_work": ["Compare execution against RPCS3", "Resolve missing HLE calls and indirect targets",
                              "Verify SPU jobs, DMA, SPURS scheduling", "Check RSX/shaders, audio, save/load and input"],
                "note": "Share this report and logs when asking for help. Nothing is uploaded automatically."})
            self.save()
            if manifest['status']=='compiled; gameplay unverified':report_progress(emit,'verify','done')

    def native_diagnostics(self, p, exe, input_hash):
        """Allow only private, hash-bound hybrid-SPU settings from local receipts."""
        value = p.get("native_runtime_receipt")
        if not value:
            return None
        try:
            receipt_path = Path(value).expanduser().resolve()
            roots = ((ROOT / "verification").resolve(), (self.home / "verification").resolve())
            if not any(receipt_path.is_relative_to(root) for root in roots):
                raise ValueError("receipt must be inside Studio verification")
            if receipt_path.stat().st_size > 65536:
                raise ValueError("receipt is too large")
            receipt = read_json(receipt_path)
            if not isinstance(receipt, dict):
                raise ValueError("receipt must be an object")
            # Existing controller evidence receipts do not request diagnostics.
            if "diagnostic_env" not in receipt:
                return None
            diagnostic_env = receipt["diagnostic_env"]
            allowed = {"SPU_INTERP_UNLIFTED", "SPU_INTERP_BUDGET"}
            if not isinstance(diagnostic_env, dict) or set(diagnostic_env) != allowed:
                raise ValueError("unsupported diagnostic environment keys")
            if diagnostic_env["SPU_INTERP_UNLIFTED"] != "1":
                raise ValueError("hybrid SPU must be explicitly enabled as '1'")
            budget = diagnostic_env["SPU_INTERP_BUDGET"]
            if type(budget) not in (str, int) or not re.fullmatch(r"[0-9]{1,7}", str(budget)):
                raise ValueError("invalid SPU instruction budget")
            budget = int(budget)
            if not 1 <= budget <= 1000000:
                raise ValueError("SPU instruction budget must be between 1 and 1000000")
            if receipt.get("input_sha256") != input_hash:
                raise ValueError("receipt input hash does not match this ELF")
            if receipt.get("executable_sha256") != sha256(exe):
                raise ValueError("receipt executable hash does not match this build")
            if "executable" in receipt and Path(receipt["executable"]).expanduser().resolve() != exe.resolve():
                raise ValueError("receipt executable path does not match this build")
            cache_profile = receipt.get("cache_profile")
            if cache_profile not in (None, "isolated"):
                raise ValueError("unsupported private cache profile")
            return {"receipt": str(receipt_path), "receipt_sha256": sha256(receipt_path),
                    "mode": "hybrid-SPU", "playability": "unverified",
                    "cache_profile": cache_profile,
                    "diagnostic_env": {"SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": str(budget)}}
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Invalid native diagnostic receipt: {exc}") from exc

    def test_log_path(self, p, native=True, result=None):
        """Resolve only project-local launch logs, with legacy receipt support."""
        base = self.project_dir(p).resolve()
        if result is None:
            result = read_json(base / ("native-test.json" if native else "rpcs3-test.json"), {})
        value = result.get("log_path")
        if not value:
            return base / ("native-play.log" if native else "rpcs3-launch.log")
        path = Path(value).resolve()
        if not path.is_relative_to(base) or path.suffix.lower() != ".log":
            raise ValueError("Launch log must be a log file inside this project")
        return path

    def _manual_session_owner(self):
        from .test_sessions import TestSessions
        if not hasattr(self, "_test_sessions"):
            self._test_sessions = TestSessions(write_json, read_json)
        return self._test_sessions

    def list_test_sessions(self, project_id=None):
        return self._manual_session_owner().list(project_id)

    def stop_test_session(self, session_id):
        return self._manual_session_owner().stop(session_id)

    def list_test_candidates(self, project):
        """The finished build to test: only the newest one (settings
        "show_all_builds": true lists every build, newest first)."""
        candidates = self.all_test_candidates(project)
        def built_at(candidate):
            try:
                return Path(candidate["exe"]).stat().st_mtime
            except OSError:
                return 0.0
        candidates.sort(key=built_at, reverse=True)
        if self.state.get("settings", {}).get("show_all_builds"):
            return candidates
        return candidates[:1]

    def all_test_candidates(self, project):
        """Snapshot finished candidates; never offer the output currently being linked."""
        p = copy.deepcopy(project)
        records = [p]
        for item in reversed(p.get("native_candidate_history", [])):
            # Legacy history omitted attempt; do not inherit the current attempt.
            record = {**p, "attempt": None, "native_runtime_receipt": None, **item}
            if not record.get("attempt") and record.get("native_exe"):
                try:
                    exe = Path(record["native_exe"]).resolve()
                    prior = next((parent for parent in list(exe.parents)[:6]
                                  if (parent / "build-report.json").is_file()), None)
                    if prior:
                        record["attempt"] = str(prior)
                except (OSError, ValueError, TypeError):
                    pass
            records.append(record)
        try:
            from .game_fixes import list_ready_candidates
        except ModuleNotFoundError as exc:
            if exc.name != "studio.game_fixes":
                raise
        else:
            for item in list_ready_candidates(self, p):
                records.append({**p, "native_runtime_receipt": None, **item, "_ready_fix_candidate": True})
        # Local independently built repairs are registered without editing the live queue.
        repair_root = self.home / "native-repairs" / str(p.get("id", ""))
        project_attempts = (self.project_dir(p) / "attempts").resolve()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(p.get("id", ""))):
            for sidecar in repair_root.glob("*.json"):
                try:
                    if (not sidecar.resolve().is_relative_to((self.home / "native-repairs").resolve())
                            or sidecar.stat().st_size > 65536):
                        continue
                    item = read_json(sidecar, {})
                    if item.get("schema") != 1 or item.get("status") != "ready" or item.get("project_id") != p["id"]:
                        continue
                    attempt = Path(item["attempt"]).resolve()
                    exe = Path(item["native_exe"]).resolve()
                    if not attempt.is_relative_to(project_attempts) or not exe.is_relative_to(attempt):
                        continue
                    report = read_json(attempt / "build-report.json", {})
                    if (report.get("status") != "compiled; gameplay unverified"
                            or report.get("input_sha256") != item.get("input_sha256")
                            or report.get("native_repair_executable_sha256") != item.get("executable_sha256")
                            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("executable_sha256", "")))
                            or Path(report.get("native_repair_executable", "")).resolve() != exe):
                        continue
                    records.append({**p, "native_runtime_receipt": report.get("native_runtime_receipt"), "native_exe": str(exe),
                                    "attempt": str(attempt), "label": str(item.get("label", "Repaired build; gameplay unverified")),
                                    "_repair_executable_sha256": item["executable_sha256"]})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        # Older Studio builds did not preserve a candidate before clearing native_exe.
        value = p.get("native_runtime_receipt")
        if value:
            try:
                path = Path(value).resolve()
                if any(path.is_relative_to(root.resolve()) for root in (ROOT / "verification", self.home / "verification")) and path.stat().st_size <= 65536:
                    receipt = read_json(path, {})
                    if receipt.get("executable"):
                        exe = Path(receipt["executable"]).resolve()
                        attempt = next((parent for parent in list(exe.parents)[:6]
                                        if (parent / "build-report.json").is_file()), None)
                        if attempt:
                            records.append({**p, "native_exe": str(exe), "attempt": str(attempt)})
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        # Explicit repair/diagnostic records win over generic folder discovery.
        from .compiled_library import records_for
        for item in records_for(self, p):
            records.append({**p, **item})
        # Unbuilt games have nothing to validate. Do not read/hash their game
        # executables just to discover that no finished output exists.
        records = [record for record in records
                   if record.get("native_exe") and record.get("attempt")]
        if not records:
            return []
        candidates, seen = [], set()
        try:
            elf = choose_elf(p["source"], p.get("elf", ""))
            input_hash = sha256(elf)
        except (OSError, ValueError):
            return []
        for record in records:
            try:
                if not record.get("native_exe") or not record.get("attempt"):
                    continue
                exe, attempt = Path(record["native_exe"]).resolve(), Path(record["attempt"]).resolve()
                linking = (Path(p.get("attempt", "")).resolve() / "build/player" /
                           ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp"))
                if p.get("status") == "building" and exe == linking:
                    continue
                if exe in seen or not exe.is_file():
                    continue
                report = read_json(attempt / "build-report.json", {})
                if report.get("status") != "compiled; gameplay unverified":
                    continue
                bound_input = record.get("input_sha256") if record.get("_ready_fix_candidate") else report.get("input_sha256")
                if bound_input != input_hash:
                    continue
                self.native_diagnostics(record, exe, input_hash)
                exe_hash = sha256(exe)
                if record.get("_repair_executable_sha256") and exe_hash != record["_repair_executable_sha256"]:
                    continue
                key = hashlib.sha256((str(exe) + "|" + exe_hash).encode()).hexdigest()[:20]
                record.update(native_exe=str(exe), attempt=str(attempt))
                candidates.append({"id": key, "label": record.get("label") or f"{attempt.name} / {exe.parent.name}",
                                   "exe": str(exe), "attempt": str(attempt), "valid": True, "completed": True,
                                   "input_sha256": input_hash, "executable_sha256": exe_hash,
                                   "project_snapshot": record})
                seen.add(exe)
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
        return candidates

    def launch(self, p, native=True, candidate_id=None):
        candidate = None
        if native:
            candidates = self.list_test_candidates(p)
            candidate = next((item for item in candidates if item["id"] == candidate_id), None) if candidate_id else next(iter(candidates), None)
            if candidate is None:
                raise RuntimeError("No finished, input-matched native candidate is available for manual testing")
            p = copy.deepcopy(candidate["project_snapshot"])
        else:
            p = copy.deepcopy(p)
        elf = choose_elf(p["source"], p.get("elf", ""))
        env = os.environ.copy()
        if native:
            # Hybrid fallback is private per-candidate evidence, never a global default.
            env.pop("SPU_INTERP_UNLIFTED", None)
            env.pop("SPU_INTERP_BUDGET", None)
            profile=self.home/'controller-profiles/default.ini'
            if profile.is_file():env['RR_PS3_INPUT_PROFILE']=str(profile)
            exe = Path(p.get("native_exe", ""))
            if not exe.is_file():
                raise RuntimeError("Build a native executable first")
            build_report = read_json(Path(p.get("attempt", "")) / "build-report.json", {})
            input_hash = sha256(elf)
            if build_report.get("input_sha256") != input_hash:
                raise RuntimeError("ELF differs from the input used for this build. Start a new build attempt.")
            diagnostics = self.native_diagnostics(p, exe, input_hash)
            if diagnostics:
                env.update(diagnostics["diagnostic_env"])
            # Per-game diagnostic switches: workspace/launch-env/<project id>.json {"VAR": "value"}
            extra = read_json(self.home / "launch-env" / f"{p['id']}.json", {}) or {}
            env.update({str(k): str(v) for k, v in extra.items() if re.fullmatch(r"[A-Z0-9_]{1,64}", str(k))})
            # Manual games share persistent project data across candidate versions.
            # Preserve explicit existing VFS overrides; ordinary sessions need the
            # same protection from writing caches into the original game folder.
            data_root = (self.project_dir(p) / "native-data").resolve()
            data_directories = {}
            for name in ("hdd0", "hdd1"):
                key = f"PS3_{name.upper()}_ROOT"
                if name == "hdd1" and diagnostics and diagnostics.get("cache_profile") == "isolated":
                    # Private cache-reset fixes never touch another running candidate's cache.
                    data_dir = self.home / "test-cache" / p["id"] / candidate["executable_sha256"][:16] / "hdd1"
                    data_dir.mkdir(parents=True, exist_ok=True)
                elif env.get(key):
                    data_dir = Path(env[key]).expanduser()
                    if not data_dir.is_absolute():
                        data_dir = exe.parent / data_dir
                    data_dir = data_dir.resolve()
                else:
                    data_dir = data_root / name
                    data_dir.mkdir(parents=True, exist_ok=True)
                env[key] = str(data_dir)
                data_directories[name] = str(data_dir)
            if diagnostics:
                diagnostics["data_directories"] = dict(data_directories)
            env["PS3_VFS_ROOT"] = str(self.vfs_root_for(p, elf))
            if native:
                from . import patch_packs
                env.update(patch_packs.launch_env(self, p))
            # Upstream port routes record the folders their port reads (disc root, update USRDIR).
            for key, value in (build_report.get("upstream_launch_env") or {}).items():
                if re.fullmatch(r"[A-Z0-9_]{1,64}", str(key)) and isinstance(value, str):
                    env[key] = value
            dev_flash = self.detect_dev_flash()
            if dev_flash:
                env["PS3_DEV_FLASH"] = dev_flash
            command = [str(exe), str(elf)]
            cwd = exe.parent
        else:
            exe = Path(self.state["settings"].get("rpcs3", ""))
            if not exe.is_file():
                raise RuntimeError("Set the RPCS3 executable under Settings")
            command = [str(exe), str(elf)]
            cwd = exe.parent
        # This is a launch, not an automatic claim that gameplay passed.
        log_dir = self.project_dir(p) / "launch-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (f"{'native' if native else 'rpcs3'}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}.log")
        log = log_path.open("x", encoding="utf-8")
        try:
            ownership = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, shell=False, **ownership)
        finally:
            log.close()
        report_path = self.project_dir(p) / ("native-test.json" if native else "rpcs3-test.json")
        result = {"pid": proc.pid, "command": command, "started": time.time(), "status": "running",
                  "playability": "unverified", "kind": "native" if native else "emulator reference",
                  "log_path": str(log_path.resolve()), "session_id": uuid.uuid4().hex,
                  "project_id": p["id"], "candidate_label": candidate["label"] if candidate else "RPCS3 reference",
                  "cwd": str(cwd), "input_sha256": sha256(elf), "project_snapshot": copy.deepcopy(p)}
        if candidate:
            result["candidate_id"] = candidate["id"]
            result["executable_sha256"] = candidate["executable_sha256"]
        if native:
            result["data_directories"] = data_directories
            if diagnostics:
                result["runtime_diagnostics"] = diagnostics
            result["vfs_root"] = env["PS3_VFS_ROOT"]
            if env.get("PS3_DEV_FLASH"):
                result["dev_flash"] = env["PS3_DEV_FLASH"]
        write_json(report_path, result)
        session_path = self.project_dir(p) / "test-sessions" / (result["session_id"] + ".json")
        self._manual_session_owner().register(proc, result, session_path, report_path)
        return proc.pid

    def export(self, p, destination):
        destination = Path(destination).resolve()
        base = self.project_dir(p)
        if destination.is_relative_to(base):
            raise ValueError("Save diagnostics outside the project directory")
        allowed = [base / "analysis.json", base / "native-play.log", base / "native-test.json",
                   base / "rpcs3-launch.log", base / "rpcs3-test.json"]
        for native in (True, False):
            try:
                allowed.append(self.test_log_path(p, native))
            except (OSError, ValueError, TypeError):
                pass
        if p.get("attempt"):
            a = Path(p["attempt"])
            allowed += [a / "build-report.json", a / "HANDOFF.json"]
            allowed += list((a / "logs").rglob("*.log")) + list((a / "logs").rglob("*.json"))
            allowed += list((a / "history").glob("*.json"))
            if (a / "port/spu/inventory.json").exists():
                allowed.append(a / "port/spu/inventory.json")
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.json", json.dumps(p, indent=2))
            z.writestr("README.txt", "Diagnostics only; includes local paths and log output. No game assets or generated game code are collected by this exporter. Review before sharing.\n")
            for f in allowed:
                if f.is_file():
                    z.write(f, str(f.relative_to(base)))
        return str(destination)
