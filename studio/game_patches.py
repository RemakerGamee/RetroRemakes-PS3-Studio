# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Community game patches (RPCS3 patch.yml format) for the player's own ELF.

The Studio ships only this reader/applier. It ships no patch data. The player
supplies a patch file themselves, for example the patch.yml that RPCS3's own Patch
Manager downloads into its "patches" folder, a file they wrote, or one they put in
workspace/patches/.

How it works:
  * The file format follows the RPCS3 patch engine (file "Version" 1.2, top-level
    hash keys, per-patch Games/Author/Notes/Patch Version/Group/Configurable Values,
    and "Patch" lists of [type, address, value]). Anchors and "load" are supported.
  * The PPU hash is computed the way RPCS3 computes it for a main executable: SHA-1
    over every program header's p_type and p_flags (big-endian, as stored). For each
    LOAD segment with p_memsz > 0, it also covers p_vaddr, p_memsz and the segment's
    file bytes (p_filesz). The key is then "PPU-" plus 40 lowercase hex digits.
  * Addresses are virtual addresses. Each write is mapped to a file offset through
    the ELF program headers and must land inside a segment's file-backed bytes.
  * The original file is never modified. A patched copy is written elsewhere
    (normally the build attempt folder).

Supported types: byte, le16, le32, le64, lef32, lef64, be16, be32, bd32, be64, bd64,
bef32, bef64, utf8, c_utf8, load. A static file copy cannot express these, so a
patch that uses any of them is listed but cannot be applied: alloc, code_alloc,
jump, jump_link, jump_func, bp_exec, move_file, hide_file.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
from pathlib import Path

ENGINE_VERSION = "1.2"
SUPPORTED_TYPES = {"byte", "le16", "le32", "le64", "lef32", "lef64", "be16", "be32", "bd32",
                   "be64", "bd64", "bef32", "bef64", "utf8", "c_utf8"}
UNSUPPORTED_TYPES = {"alloc", "code_alloc", "jump", "jump_link", "jump_func", "bp_exec",
                     "move_file", "hide_file"}
_INT_BITS = {"byte": 8, "le16": 16, "be16": 16, "le32": 32, "be32": 32, "bd32": 32,
             "le64": 64, "be64": 64, "bd64": 64}
_PACK = {"byte": ">B", "le16": "<H", "be16": ">H", "le32": "<I", "be32": ">I", "bd32": ">I",
         "le64": "<Q", "be64": ">Q", "bd64": ">Q", "lef32": "<f", "bef32": ">f",
         "lef64": "<d", "bef64": ">d"}


class PatchError(ValueError):
    pass


# --------------------------------------------------------------------------- YAML

def load_yaml_text(text):
    """Parse YAML into plain dict/list/str/None (every scalar stays a string)."""
    try:
        import yaml  # optional dependency
        return yaml.load(text, Loader=yaml.BaseLoader)
    except ImportError:
        return _MiniYaml(text).parse()


class _MiniYaml:
    """A small YAML subset parser, enough for patch files when PyYAML is missing.

    It supports block maps and sequences, flow [..] and {..} (which may span
    lines), quoted scalars, | and > block scalars, comments, anchors and aliases.
    """

    def __init__(self, text):
        self.anchors = {}
        self.lines = []
        raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        i = 0
        while i < len(raw):
            line = raw[i]
            if line.lstrip().startswith(("---", "...")) and not line.startswith(" ") and line.strip() in ("---", "..."):
                i += 1
                continue
            body = self._strip_comment(line)
            if not body.strip():
                i += 1
                continue
            indent = len(body) - len(body.lstrip(" "))
            content = body.strip()
            # Join flow collections that span several lines.
            while self._depth(content) > 0 and i + 1 < len(raw):
                i += 1
                content += " " + self._strip_comment(raw[i]).strip()
            # Block scalars: keep the raw lines that follow.
            if re.search(r"(^|:\s|^-\s)[|>][-+]?$", content) or content.endswith((": |", ": >", ": |-", ": >-")):
                block, j = [], i + 1
                while j < len(raw) and (not raw[j].strip() or len(raw[j]) - len(raw[j].lstrip(" ")) > indent):
                    block.append(raw[j])
                    j += 1
                self.lines.append((indent, content, block))
                i = j
                continue
            self.lines.append((indent, content, None))
            i += 1

    @staticmethod
    def _strip_comment(line):
        quote = None
        for k, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"" and (k == 0 or line[k - 1] in " [{,:-"):
                quote = ch
            elif ch == "#" and (k == 0 or line[k - 1] in " \t"):
                return line[:k].rstrip()
        return line.rstrip()

    @staticmethod
    def _depth(s):
        depth, quote = 0, None
        for ch in s:
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
        return depth

    def parse(self):
        if not self.lines:
            return None
        node, _ = self._block(0, self.lines[0][0])
        return node

    def _block(self, i, indent):
        if self.lines[i][1].startswith("- ") or self.lines[i][1] == "-":
            return self._seq(i, indent)
        return self._map(i, indent)

    @staticmethod
    def _split_key(content):
        quote, depth = None, 0
        for k, ch in enumerate(content):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == ":" and depth == 0 and (k + 1 == len(content) or content[k + 1] == " "):
                return content[:k].strip(), content[k + 1:].strip()
        return None

    def _child(self, i, indent, value, block):
        """Resolve a value that may be inline, a block scalar, or a nested block."""
        anchor = None
        m = re.match(r"&(\S+)\s*(.*)$", value)
        if m:
            anchor, value = m.group(1), m.group(2)
        if block is not None and value[:1] in "|>":
            node = self._block_scalar(value, block)
            i += 1
        elif value:
            node = self._flow(value)
            i += 1
        else:
            i += 1
            if (i < len(self.lines) and self.lines[i][0] > indent and not self.lines[i][1].startswith("-")
                    and self._split_key(self.lines[i][1]) is None):
                # A scalar written on the following line(s), e.g. "key: &anchor" then a quoted string.
                deep, parts = self.lines[i][0], []
                while i < len(self.lines) and self.lines[i][0] >= deep and self._split_key(self.lines[i][1]) is None \
                        and not self.lines[i][1].startswith("-"):
                    parts.append(self.lines[i][1]); i += 1
                node = self._flow(" ".join(parts))
            elif i < len(self.lines) and (self.lines[i][0] > indent or
                                        (self.lines[i][0] == indent and self.lines[i][1].startswith("-"))):
                node, i = self._block(i, self.lines[i][0])
            else:
                node = None
        if anchor:
            self.anchors[anchor] = node
        return node, i

    def _map(self, i, indent):
        out = {}
        while i < len(self.lines) and self.lines[i][0] == indent and not self.lines[i][1].startswith("- "):
            _, content, block = self.lines[i]
            split = self._split_key(content)
            if split is None:
                raise PatchError(f"YAML: expected 'key: value' near: {content[:60]}")
            key, value = split
            key = self._scalar(key)
            out[key], i = self._child(i, indent, value, block)
        return out, i

    def _seq(self, i, indent):
        out = []
        while i < len(self.lines) and self.lines[i][0] == indent and (self.lines[i][1].startswith("- ") or self.lines[i][1] == "-"):
            _, content, block = self.lines[i]
            value = content[1:].strip()
            if value and self._split_key(value) and value[0] not in "[{'\"*&":
                # "- key: value" opens a map; rewrite it as a nested line.
                self.lines[i] = (indent + 2, value, block)
                node, i = self._map(i, indent + 2)
                out.append(node)
                continue
            node, i = self._child(i, indent, value, block)
            out.append(node)
        return out, i

    @staticmethod
    def _block_scalar(header, block):
        lines = [ln for ln in block]
        nonblank = [ln for ln in lines if ln.strip()]
        cut = min((len(ln) - len(ln.lstrip(" ")) for ln in nonblank), default=0)
        body = [ln[cut:] for ln in lines]
        if header.startswith(">"):
            text = " ".join(ln.strip() for ln in body if ln.strip())
        else:
            text = "\n".join(body).rstrip("\n")
        return text if header.endswith("-") else text + "\n"

    def _flow(self, s):
        node, k = self._flow_at(s.strip(), 0)
        return node

    def _flow_at(self, s, k):
        while k < len(s) and s[k] == " ":
            k += 1
        if k < len(s) and s[k] == "[":
            out, k = [], k + 1
            while True:
                while k < len(s) and s[k] in " ,":
                    k += 1
                if k >= len(s):
                    raise PatchError("YAML: unterminated [")
                if s[k] == "]":
                    return out, k + 1
                item, k = self._flow_at(s, k)
                out.append(item)
        if k < len(s) and s[k] == "{":
            out, k = {}, k + 1
            while True:
                while k < len(s) and s[k] in " ,":
                    k += 1
                if k >= len(s):
                    raise PatchError("YAML: unterminated {")
                if s[k] == "}":
                    return out, k + 1
                key, k = self._token(s, k, ":")
                k += 1
                value, k = self._flow_at(s, k)
                out[self._scalar(key)] = value
        token, k = self._token(s, k, ",]}")
        return self._scalar(token), k

    @staticmethod
    def _token(s, k, stops):
        start, quote = k, None
        while k < len(s):
            ch = s[k]
            if quote:
                if ch == quote:
                    if quote == "'" and k + 1 < len(s) and s[k + 1] == "'":
                        k += 2
                        continue
                    quote = None
            elif ch in "'\"" and k == start:
                quote = ch
            elif ch in stops and (ch != ":" or k + 1 == len(s) or s[k + 1] == " "):
                break
            k += 1
        return s[start:k].strip(), k

    def _scalar(self, token):
        token = token.strip()
        if token.startswith("*"):
            name = token[1:]
            if name not in self.anchors:
                raise PatchError(f"YAML: unknown alias *{name}")
            return self.anchors[name]
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            if "\\" not in token:
                return token[1:-1]
            try:
                return json.loads(token)
            except ValueError:   # YAML escapes JSON lacks (\x, \', ...): keep the text as written
                return token[1:-1].replace('\\"', '"')
        if token.startswith("'") and token.endswith("'") and len(token) >= 2:
            return token[1:-1].replace("''", "'")
        if token in ("", "~", "null", "Null", "NULL"):
            return None if token != "" else ""
        return token


# --------------------------------------------------------------------------- ELF

def program_headers(data):
    """Return (p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz) per header."""
    if data[:4] != b"\x7fELF":
        raise PatchError("Not an ELF file (decrypt the EBOOT first)")
    is64, big = data[4] == 2, data[5] == 2
    e = ">" if big else "<"
    if is64:
        phoff, = struct.unpack_from(e + "Q", data, 0x20)
        phentsize, phnum = struct.unpack_from(e + "HH", data, 0x36)
    else:
        phoff, = struct.unpack_from(e + "I", data, 0x1C)
        phentsize, phnum = struct.unpack_from(e + "HH", data, 0x2A)
    out = []
    for n in range(phnum):
        base = phoff + n * phentsize
        if is64:
            p_type, p_flags, p_offset, p_vaddr, _pa, p_filesz, p_memsz = struct.unpack_from(e + "IIQQQQQ", data, base)
        else:
            p_type, p_offset, p_vaddr, _pa, p_filesz, p_memsz, p_flags = struct.unpack_from(e + "IIIIIII", data, base)
        out.append((p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz))
    return out, is64, big


def ppu_hash(path_or_bytes):
    """Return the RPCS3-compatible main executable key, "PPU-<sha1>"."""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else Path(path_or_bytes).read_bytes()
    phdrs, is64, big = program_headers(data)
    e = ">" if big else "<"
    word = "Q" if is64 else "I"
    sha = hashlib.sha1()
    for p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz in phdrs:
        sha.update(struct.pack(e + "I", p_type))
        sha.update(struct.pack(e + "I", p_flags))
        if p_type == 1 and p_memsz:
            sha.update(struct.pack(e + word, p_vaddr))
            sha.update(struct.pack(e + word, p_memsz))
            sha.update(bytes(data[p_offset:p_offset + p_filesz]))
    return "PPU-" + sha.hexdigest()


def vaddr_to_offset(phdrs, vaddr, size):
    for p_type, _flags, p_offset, p_vaddr, p_filesz, _memsz in phdrs:
        if p_type == 1 and p_vaddr <= vaddr and vaddr + size <= p_vaddr + p_filesz:
            return p_offset + (vaddr - p_vaddr)
    raise PatchError(f"Address 0x{vaddr:08x} (+{size}) is not inside file-backed ELF segment data")


# --------------------------------------------------------------------------- patches

def _int(text, bits=64):
    s = str(text).strip()
    neg = s.startswith("-")
    body = s[1:] if neg or s.startswith("+") else s
    try:
        value = int(body, 16) if body.lower().startswith("0x") else int(body, 10)
    except ValueError:
        raise PatchError(f"Invalid integer '{text}'") from None
    value = -value if neg else value
    if value < -(1 << (bits - 1)) or value >= (1 << bits):
        raise PatchError(f"Value '{text}' does not fit in {bits} bits")
    return value & ((1 << bits) - 1)


def _num(text):
    s = str(text).strip()
    try:
        return float(int(s, 16)) if s.lower().lstrip("+-").startswith("0x") else float(s)
    except ValueError:
        raise PatchError(f"Invalid number '{text}'") from None


def _entries(node, modifier, info, errors, depth=0):
    """Flatten a Patch list (resolving 'load'), collecting [type, addr, value]."""
    if depth > 16:
        errors.append("load nesting too deep")
        return []
    out = []
    for item in node or []:
        if not isinstance(item, list) or len(item) < 2:
            errors.append(f"invalid patch entry {item!r}")
            continue
        kind = str(item[0]).strip()
        if kind == "load":
            sub = item[1]
            mod = _int(item[2], 32) if len(item) > 2 and item[2] not in (None, "") else 0
            if not isinstance(sub, list):
                errors.append("load target is not a list (unresolved anchor?)")
                continue
            out.extend(_entries(sub, (modifier + mod) & 0xFFFFFFFF, info, errors, depth + 1))
            continue
        value = "" if len(item) < 3 or item[2] is None else str(item[2])
        if kind in UNSUPPORTED_TYPES:
            info["unsupported"].add(kind)
            out.append(dict(type=kind, address=None, value=value))
            continue
        if kind not in SUPPORTED_TYPES:
            errors.append(f"unknown patch type '{kind}'")
            continue
        addr_text = str(item[1]).strip()
        if not addr_text.startswith("0x"):
            errors.append(f"address '{addr_text}' must be hex (0x...)")
            continue
        address = _int(addr_text, 32) + modifier
        if address > 0xFFFFFFFF:
            errors.append(f"address 0x{address:x} overflows 32 bits")
            continue
        out.append(dict(type=kind, address=address, value=value))
    return out


def parse_patch_text(text, source="patch file"):
    """Parse patch.yml text. Returns {"version", "patches": [patch dicts], "errors"}."""
    root = load_yaml_text(text)
    if not isinstance(root, dict):
        raise PatchError(f"{source}: not a patch file (expected a map at top level)")
    version = str(root.get("Version", "")).strip()
    if not version:
        raise PatchError(f"{source}: no 'Version' entry")
    if version != ENGINE_VERSION:
        raise PatchError(f"{source}: file version {version} does not match supported version {ENGINE_VERSION}")
    patches, errors = [], []
    for key, entries in root.items():
        if key in ("Version", "Anchors") or not isinstance(entries, dict):
            continue
        for name, body in entries.items():
            if not isinstance(body, dict):
                errors.append(f"{key}/{name}: expected a map")
                continue
            info = dict(hash=str(key), name=str(name), source=str(source), author=str(body.get("Author") or ""),
                        notes=str(body.get("Notes") or ""), patch_version=str(body.get("Patch Version") or ""),
                        group=str(body.get("Group") or ""), games={}, config={}, unsupported=set(), errors=[])
            games = body.get("Games") or {}
            if isinstance(games, dict):
                for title, serials in games.items():
                    if isinstance(serials, dict):
                        info["games"][str(title)] = {str(s): [str(v) for v in (vers or [])] if isinstance(vers, list) else [str(vers)]
                                                     for s, vers in serials.items()}
            config = body.get("Configurable Values") or {}
            if isinstance(config, dict):
                for ckey, cbody in config.items():
                    if isinstance(cbody, dict) and cbody.get("Value") is not None:
                        info["config"][str(ckey)] = dict(type=str(cbody.get("Type") or ""), value=str(cbody["Value"]),
                                                         min=cbody.get("Min"), max=cbody.get("Max"),
                                                         allowed=cbody.get("Allowed Values") or {})
            info["entries"] = _entries(body.get("Patch"), 0, info, info["errors"])
            info["unsupported"] = sorted(info["unsupported"])
            info["applicable"] = not info["unsupported"] and not info["errors"] and bool(info["entries"])
            errors.extend(f"{key}/{name}: {e}" for e in info["errors"])
            patches.append(info)
    return dict(version=version, patches=patches, errors=errors)


def load_patch_files(paths):
    patches, errors, files = [], [], []
    for path in paths:
        path = Path(path)
        try:
            raw = path.read_bytes()
            parsed = parse_patch_text(raw.decode("utf-8-sig"), path.name)
        except (OSError, UnicodeDecodeError, PatchError) as e:
            errors.append(f"{path.name}: {e}")
            continue
        files.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest()))
        patches.extend(parsed["patches"])
        errors.extend(parsed["errors"])
    return dict(files=files, patches=patches, errors=errors)


def find_patch_files(home, settings):
    """Patch files the player has supplied, in priority order."""
    found = []
    configured = (settings or {}).get("patch_file", "")
    for item in ([configured] if isinstance(configured, str) else list(configured)):
        if item and Path(item).expanduser().is_file():
            found.append(Path(item).expanduser().resolve())
    folder = Path(home) / "patches"
    if folder.is_dir():
        found.extend(sorted(p.resolve() for p in folder.glob("*.yml")))
    emulator = (settings or {}).get("rpcs3", "")
    if emulator:
        for name in ("patch.yml", "imported_patch.yml"):
            candidate = Path(emulator).expanduser().parent / "patches" / name
            if candidate.is_file():
                found.append(candidate.resolve())
    unique = []
    for p in found:
        if p not in unique:
            unique.append(p)
    return unique


def _game_matches(info, serial, app_version):
    if not info["games"] or not serial:
        return True
    for title, serials in info["games"].items():
        for s, versions in serials.items():
            if s == "All" or s == serial:
                if not app_version or not versions or "All" in versions or app_version in versions:
                    return True
    return False


def patches_for(db, hash_key, serial="", app_version=""):
    """Patches in db (from load_patch_files) that target this executable hash."""
    return [p for p in db["patches"] if p["hash"] == hash_key and _game_matches(p, serial, app_version)]


def _encode(entry, info, values):
    kind, raw = entry["type"], entry["value"]
    if raw in info["config"]:
        raw = str((values or {}).get(raw, info["config"][raw]["value"]))
    if kind == "utf8":
        return raw.encode("utf-8")
    if kind == "c_utf8":
        return raw.encode("utf-8") + b"\x00"
    if kind in ("lef32", "bef32", "lef64", "bef64"):
        return struct.pack(_PACK[kind], _num(raw))
    bits = _INT_BITS[kind]
    if raw.strip().lstrip("+-").lower().startswith("0x") or re.fullmatch(r"[+-]?\d+", raw.strip()):
        value = _int(raw, bits)
    else:
        value = int(_num(raw)) & ((1 << bits) - 1)  # configurable values may be written as floats
    return struct.pack(_PACK[kind], value)


def apply_patches(source, destination, patches, values=None):
    """Copy source to destination and apply the patches to the copy only."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise PatchError("Refusing to patch the original file in place")
    data = bytearray(source.read_bytes())
    phdrs, _is64, _big = program_headers(data)
    key = ppu_hash(data)
    writes, applied = [], []
    for info in patches:
        if info["hash"] != key:
            raise PatchError(f"'{info['name']}' targets {info['hash']}, but this ELF is {key}")
        if not info["applicable"]:
            why = ", ".join(info["unsupported"]) or "; ".join(info["errors"]) or "no entries"
            raise PatchError(f"'{info['name']}' cannot be applied to a file copy ({why})")
        for entry in info["entries"]:
            blob = _encode(entry, info, (values or {}).get(info["name"]))
            offset = vaddr_to_offset(phdrs, entry["address"], len(blob))
            data[offset:offset + len(blob)] = blob
            writes.append(dict(patch=info["name"], type=entry["type"], address=f"0x{entry['address']:08x}",
                               file_offset=f"0x{offset:x}", size=len(blob)))
        applied.append(dict(name=info["name"], hash=info["hash"], source=info["source"],
                            patch_version=info["patch_version"], author=info["author"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(bytes(data))
    shutil.copystat(source, destination)
    return dict(ppu_hash=key, applied=applied, writes=len(writes), write_list=writes,
                original_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                patched_elf=str(destination), patched_sha256=hashlib.sha256(bytes(data)).hexdigest())


def game_identity(metadata):
    metadata = metadata or {}
    return str(metadata.get("TITLE_ID", "")), str(metadata.get("APP_VER", ""))


def available_for_project(home, settings, elf, metadata=None):
    """For the GUI: (ppu hash, matching patches, load errors, patch files)."""
    db = load_patch_files(find_patch_files(home, settings))
    key = ppu_hash(elf)
    serial, ver = game_identity(metadata)
    return key, patches_for(db, key, serial, ver), db["errors"], db["files"]


def prepare_patched_elf(home, settings, project, elf, attempt, metadata=None):
    """Return (elf to lift, manifest record or None). None = no patches enabled."""
    wanted = [str(n) for n in (project.get("patches") or []) if str(n).strip()]
    if not wanted:
        return Path(elf), None
    files = find_patch_files(home, settings)
    if not files:
        raise PatchError("Game patches are enabled for this game, but no patch file was found. Set a patch file or "
                         "put .yml files in the workspace 'patches' folder.")
    db = load_patch_files(files)
    key = ppu_hash(elf)
    serial, ver = game_identity(metadata)
    by_name = {}
    for info in patches_for(db, key, serial, ver):
        by_name.setdefault(info["name"], info)
    missing = [n for n in wanted if n not in by_name]
    if missing:
        raise PatchError(f"Enabled patches not found for this ELF ({key}): " + ", ".join(missing))
    destination = Path(attempt) / "patched" / Path(elf).name
    record = apply_patches(elf, destination, [by_name[n] for n in wanted], project.get("patch_values"))
    record["patch_files"] = db["files"]
    record["original_elf"] = str(elf)
    return destination, record
