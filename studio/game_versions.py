# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Game versions: what each game is at, what it should be at, and how to get there.

    python -m studio.game_versions --scan [--online] [--hash]
    python -m studio.game_versions --project ID [--online]
    python -m studio.game_versions --install-pkg FILE [--pkg-key FILE]
    python -m studio.game_versions --update ID [--to latest|target|XX.YY]     (private build)
    python -m studio.game_versions --update-all-needed                        (private build)

What it does:
  * Reads PARAM.SFO (TITLE_ID, TITLE, APP_VER, VERSION, CATEGORY) of each game in
    the games folder, the update packages a disc carries (PS3_GAME/PKGDIR) and any
    update already installed (the Studio's updates folder, or RPCS3's dev_hdd0).
  * Finds a target version for a game: studio/version_targets.json (curated, only
    verified entries), routes in studio.upstream_ports, and the versions named by
    the player's own patch files (studio.game_patches). A port or patch set made for
    another version of the game is the reason to update.
  * Lists Sony's official update packages for the title from its public update
    list (titlepatch XML). The server certificate is not publicly trusted, so the
    list is read only over a certificate the player approved once (pinned SHA-256).
  * Installs an update PKG the player supplied (or one downloaded by the private
    feature) into a dev_hdd0 style folder beside the games folder. Originals are
    never changed: an existing install is renamed aside, never deleted.

The installed EBOOT.BIN of an update is an encrypted SELF. The Studio's build needs
a decrypted ELF (see core.choose_elf: SELF is refused). Decrypt it with RPCS3
(Utilities > Decrypt PS3 Binaries) and select the EBOOT.elf it writes with
"Set decrypted ELF" for the project.

Retail PKG decryption needs the PS3 package key. The Studio does not include it:
put it (32 hex digits) in workspace/keys/ps3-pkg.key, or set settings "pkg_key_file".
It is checked against a SHA-256 fingerprint before use.

PKG reading follows RPCS3's package reader.
Derived from RPCS3 (https://github.com/RPCS3/rpcs3), GPL-2.0-only
(PKG header/metadata/entry layout, stream cipher, install rules in pkg_* below).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import struct
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

APP = Path(__file__).resolve().parents[1]
TARGETS_FILE = Path(__file__).with_name("version_targets.json")
UPDATE_LIST_URL = "https://a0.ww.np.dl.playstation.net/tpl/np/{tid}/{tid}-ver.xml"
SONY_HOST_SUFFIX = ".playstation.net"
TITLE_ID_RE = re.compile(r"^[A-Z]{4}\d{5}$")
# SHA-256 fingerprints of the PS3 package keys (retail, then IDU). Not the keys.
PKG_KEY_FINGERPRINTS = {
    "retail": "9f59799419060804ae46982ba023f56c897206b951daeb507b8dcb384e597b5f",
    "idu": "005f335553aa6f4db950fb52bd142cb8357930589f38f21a317a28c2dac29a18",
}


class VersionError(RuntimeError):
    pass


def _now():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def version_key(text):
    """'01.02' -> (1, 2); unknown -> (-1,)."""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(p) for p in parts) if parts else (-1,)


def norm_version(text):
    k = version_key(text)
    if k == (-1,):
        return ""
    return ".".join(f"{n:02d}" for n in k[:2]) if len(k) >= 2 else f"{k[0]:02d}.00"


# --------------------------------------------------------------------------- PARAM.SFO

def parse_sfo(data):
    """Parse PARAM.SFO bytes into {key: str|int}."""
    if len(data) < 20 or data[:4] != b"\x00PSF":
        raise VersionError("not a PARAM.SFO")
    _, _, key_table, data_table, count = struct.unpack_from("<4sIIII", data, 0)
    out = {}
    for i in range(count):
        key_off, fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, 20 + 16 * i)
        start = key_table + key_off
        end = data.index(b"\x00", start)
        key = data[start:end].decode("ascii", "replace")
        raw = data[data_table + data_off: data_table + data_off + length]
        if fmt == 0x0404:
            out[key] = struct.unpack("<I", raw[:4])[0] if len(raw) >= 4 else 0
        else:
            out[key] = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return out


def read_sfo(path):
    try:
        return parse_sfo(Path(path).read_bytes())
    except (OSError, VersionError, struct.error, ValueError):
        return None


def sfo_summary(sfo, path=None):
    sfo = sfo or {}
    tid = str(sfo.get("TITLE_ID", ""))
    return {
        "title_id": tid,
        "title": str(sfo.get("TITLE", "")).replace("\n", " "),
        "category": str(sfo.get("CATEGORY", "")),
        "app_ver": norm_version(sfo.get("APP_VER", "")),
        "disc_version": norm_version(sfo.get("VERSION", "")),
        "system_ver": str(sfo.get("PS3_SYSTEM_VER", "")),
        "kind": title_kind(tid),
        "path": str(path) if path else "",
    }


def title_kind(title_id):
    tid = str(title_id or "").upper()
    if tid[:1] == "B":
        return "disc"
    if tid[:1] == "N":
        return "psn"
    return "unknown"


# --------------------------------------------------------------------------- locations

def default_home():
    return Path(os.environ.get("RETROREMAKES_PS3_HOME", APP / "workspace")).expanduser()


def load_settings(home):
    try:
        return json.loads((Path(home) / "studio.json").read_text(encoding="utf-8")).get("settings", {})
    except (OSError, ValueError):
        return {}


def updates_root(settings):
    """Where the Studio installs updates: a dev_hdd0 tree beside the games folder.

    It is outside the games folder on purpose, so a games-folder import does not
    pick the update EBOOTs up as new games."""
    configured = (settings or {}).get("updates_folder")
    if configured:
        return Path(configured).expanduser()
    games = (settings or {}).get("games_folder")
    base = Path(games).expanduser().parent if games else default_home()
    return base / "PS3 UPDATES" / "dev_hdd0" / "game"


def hdd0_game_dirs(settings):
    """dev_hdd0/game folders that may hold installed updates, Studio first."""
    dirs = [updates_root(settings)]
    rpcs3 = (settings or {}).get("rpcs3") or ""
    flash = (settings or {}).get("dev_flash") or ""
    for base in ([Path(rpcs3).expanduser().parent] if rpcs3 else []) + ([Path(flash).expanduser().parent] if flash else []):
        dirs.append(base / "dev_hdd0" / "game")
    extra = (settings or {}).get("hdd0_game_folders") or []
    dirs.extend(Path(p).expanduser() for p in extra)
    seen, out = set(), []
    for d in dirs:
        key = str(d).casefold()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def installed_updates(title_id, settings):
    """Every installed update (CATEGORY GD) found for title_id, newest first."""
    found = []
    for base in hdd0_game_dirs(settings):
        folder = base / title_id
        sfo = read_sfo(folder / "PARAM.SFO") if folder.is_dir() else None
        if sfo and str(sfo.get("TITLE_ID", "")) == title_id:
            info = sfo_summary(sfo, folder)
            info["eboot"] = str(folder / "USRDIR" / "EBOOT.BIN") if (folder / "USRDIR" / "EBOOT.BIN").is_file() else ""
            found.append(info)
    found.sort(key=lambda i: version_key(i["app_ver"]), reverse=True)
    return found


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _ppu_hash(path):
    try:
        from studio import game_patches
        return game_patches.ppu_hash(path)
    except Exception:
        return ""


def game_info(root, settings=None, hash_files=False):
    """Version facts for one game folder (disc layout or HDD layout)."""
    root = Path(root)
    sfo_path = next((p for p in (root / "PS3_GAME" / "PARAM.SFO", root / "PARAM.SFO") if p.is_file()), None)
    if not sfo_path:
        return None
    info = sfo_summary(read_sfo(sfo_path), sfo_path)
    if not info["title_id"]:
        return None
    info["folder"] = str(root)
    usrdir = sfo_path.parent / "USRDIR"
    info["eboot_bin"] = str(usrdir / "EBOOT.BIN") if (usrdir / "EBOOT.BIN").is_file() else ""
    info["eboot_elf"] = str(usrdir / "EBOOT.elf") if (usrdir / "EBOOT.elf").is_file() else ""
    if hash_files:
        if info["eboot_bin"]:
            info["eboot_bin_sha256"] = sha256_file(info["eboot_bin"])
        if info["eboot_elf"]:
            info["eboot_elf_sha256"] = sha256_file(info["eboot_elf"])
            info["ppu_hash"] = _ppu_hash(info["eboot_elf"])
    info["disc_packages"] = disc_packages(sfo_path.parent)
    info["installed_updates"] = installed_updates(info["title_id"], settings or {})
    best = info["installed_updates"][0]["app_ver"] if info["installed_updates"] else ""
    info["current"] = max([v for v in (info["app_ver"], best) if v], key=version_key, default="")
    return info


def disc_packages(ps3_game):
    """Packages shipped on the disc (PKGDIR/*, INSDIR): header facts only."""
    out = []
    for pkg in sorted(list(Path(ps3_game).glob("PKGDIR/*/*.PKG")) + list(Path(ps3_game).glob("INSDIR/*.PKG"))):
        entry = {"path": str(pkg)}
        try:
            entry.update(pkg_summary(pkg))
        except (OSError, VersionError) as e:
            entry["error"] = str(e)
        sfo = read_sfo(pkg.parent / "PARAM.SFO")
        if sfo:
            entry["sfo"] = sfo_summary(sfo)
        out.append(entry)
    return out


def disc_updates(info, version=None):
    """Update packages the player's own disc carries (CATEGORY AR, patch flag), oldest first."""
    out = [p for p in info.get("disc_packages") or []
           if p.get("is_patch") and p.get("title_id") == info.get("title_id") and (p.get("sfo") or {}).get("app_ver")]
    out.sort(key=lambda p: version_key(p["sfo"]["app_ver"]))
    if version:
        out = [p for p in out if p["sfo"]["app_ver"] == norm_version(version)]
    return out


def scan_games(settings, games_folder=None, hash_files=False):
    if games_folder:
        folders = [games_folder]
    else:
        folders = [settings.get("games_folder")] + list(settings.get("games_folders") or [])
        folders = [f for i, f in enumerate(folders) if f and f not in folders[:i]]
    existing = [Path(f) for f in folders if Path(f).is_dir()]
    if not existing:
        raise VersionError(f"games folder not found: {', '.join(map(str, folders)) or '(none set)'}")
    games = []
    for folder in existing:
        for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            if child.is_dir():
                info = game_info(child, settings, hash_files)
                if info:
                    games.append(info)
    return sorted(games, key=lambda g: str(g.get("name") or g.get("title") or "").casefold())


# --------------------------------------------------------------------------- targets

def load_targets(path=TARGETS_FILE):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (data.get("titles") or {}).items() if TITLE_ID_RE.match(k)}


def targets_from_routes():
    out = {}
    try:
        from studio import upstream_ports
    except Exception:
        return out
    for route in getattr(upstream_ports, "ROUTES", ()):
        tid, ver = route.get("title_id"), route.get("update_version")
        if tid and ver:
            out.setdefault(tid, []).append({"version": norm_version(ver), "source": route.get("id", "upstream port"),
                                            "url": route.get("repository", ""),
                                            "reason": f"{route.get('title', tid)} port builds from this version's EBOOT"})
    return out


def targets_from_patches(home, settings):
    """Versions the player's own patch files are written for, per title id."""
    out = {}
    try:
        from studio import game_patches
        db = game_patches.load_patch_files(game_patches.find_patch_files(home, settings))
    except Exception:
        return out
    for patch in db.get("patches", []):
        for _title, serials in (patch.get("games") or {}).items():
            for serial, versions in serials.items():
                if not TITLE_ID_RE.match(serial):
                    continue
                for v in versions:
                    if v and v != "All":
                        slot = out.setdefault(serial, {})
                        slot.setdefault(norm_version(v), []).append(patch.get("name", ""))
    return {tid: [{"version": v, "source": "patch file", "url": "",
                   "reason": f"{len(names)} patch(es): " + ", ".join(sorted(set(names)))[:200]}
                  for v, names in vers.items()] for tid, vers in out.items()}


def resolve_target(title_id, current, home=None, settings=None, curated=None):
    """Pick the version a game should be at, with the reason. None when nothing asks for a change."""
    curated = load_targets() if curated is None else curated
    if title_id in curated:
        t = dict(curated[title_id])
        t["version"] = norm_version(t.get("version"))
        t.setdefault("source", "version_targets.json")
        return t
    routes = targets_from_routes().get(title_id) or []
    if routes:
        return max(routes, key=lambda t: version_key(t["version"]))
    options = targets_from_patches(home or default_home(), settings or {}).get(title_id) or []
    if options:
        if any(o["version"] == current for o in options):
            return None  # the player's patches already cover the version they have
        return max(options, key=lambda t: version_key(t["version"]))
    return None


# --------------------------------------------------------------------------- official update list

def _host_ok(url, schemes=("https",)):
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme in schemes and (host.endswith(SONY_HOST_SUFFIX))


def parse_update_xml(text, title_id=""):
    """Parse Sony's titlepatch XML -> [{'version','size','sha1','url','system_ver','title'}] oldest first."""
    text = (text or "").strip()
    if not text:
        return []
    root = ET.fromstring(text)
    if root.tag != "titlepatch":
        raise VersionError("not a title update list")
    if title_id and root.get("titleid") and root.get("titleid") != title_id:
        raise VersionError(f"update list is for {root.get('titleid')}, not {title_id}")
    out = []
    for pkg in root.iter("package"):
        title = ""
        sfo = pkg.find("paramsfo")
        if sfo is not None and sfo.find("TITLE") is not None:
            title = sfo.find("TITLE").text or ""
        out.append({"version": norm_version(pkg.get("version")), "size": int(pkg.get("size") or 0),
                    "sha1": (pkg.get("sha1sum") or "").lower(), "url": pkg.get("url") or "",
                    "system_ver": pkg.get("ps3_system_ver") or "", "title": title})
    out.sort(key=lambda p: version_key(p["version"]))
    return out


def _pinned_fetch(url, pin_sha256, timeout=30):
    """GET url, accepting the server only if its certificate matches the pinned SHA-256.

    Sony's update-list server is signed by Sony's own CA, which no public trust
    store carries; a pinned fingerprint the player approved stands in for it."""
    p = urllib.parse.urlparse(url)
    host, port = p.hostname, p.port or 443
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint check below
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert(binary_form=True)
            seen = hashlib.sha256(cert).hexdigest()
            if not pin_sha256 or seen != pin_sha256.lower().replace(":", ""):
                raise VersionError(f"update server certificate not approved (sha256 {seen}). "
                                   f"If you trust it, approve it with --trust-update-cert {seen}")
            path = p.path + (("?" + p.query) if p.query else "")
            tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                        f"User-Agent: PS3Studio\r\n\r\n".encode())
            chunks = []
            while True:
                b = tls.recv(65536)
                if not b:
                    break
                chunks.append(b)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    code = int(status.split()[1]) if len(status.split()) > 1 else 0
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return code, body


def server_cert_fingerprint(title_id="BCUS98155", timeout=15):
    """SHA-256 of the update-list server's certificate, for the player to approve once.
    Only the handshake is made; nothing is requested or trusted here."""
    url = UPDATE_LIST_URL.format(tid=title_id)
    p = urllib.parse.urlparse(url)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # fingerprint shown to the player for approval
    with socket.create_connection((p.hostname, p.port or 443), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=p.hostname) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


def _dechunk(body):
    out, i = bytearray(), 0
    while i < len(body):
        j = body.index(b"\r\n", i)
        n = int(body[i:j].split(b";")[0], 16)
        if n == 0:
            break
        out += body[j + 2:j + 2 + n]
        i = j + 4 + n
    return bytes(out)


def official_updates(title_id, settings, fetch=None):
    """Official updates for a title. fetch(url) -> (status, bytes) can be supplied (tests)."""
    if not TITLE_ID_RE.match(title_id or ""):
        raise VersionError(f"bad title id: {title_id}")
    url = UPDATE_LIST_URL.format(tid=title_id)
    if fetch is None:
        pin = (settings or {}).get("update_server_cert_sha256", "")
        fetch = lambda u: _pinned_fetch(u, pin)  # noqa: E731
    code, body = fetch(url)
    if code == 404 or not body.strip():
        return []  # no updates published
    if code != 200:
        raise VersionError(f"update list request returned HTTP {code}")
    return parse_update_xml(body.decode("utf-8", "replace"), title_id)


def plan_updates(current, available, to="latest", target=None):
    """Which official packages to install, in order. PS3 updates install one after another
    (the console offers every package newer than the installed version)."""
    if not available:
        return []
    if to == "target":
        if not target:
            raise VersionError("no target version for this game")
        goal = target["version"] if isinstance(target, dict) else norm_version(target)
    elif to == "latest":
        goal = available[-1]["version"]
    else:
        goal = norm_version(to)
    if not any(p["version"] == goal for p in available):
        raise VersionError(f"version {goal} is not on the official update list")
    cur = version_key(current)
    return [p for p in available if cur < version_key(p["version"]) <= version_key(goal)]


# --------------------------------------------------------------------------- PKG
# Derived from RPCS3 (https://github.com/RPCS3/rpcs3), GPL-2.0-only:
# rpcs3/Crypto/unpkg.h/.cpp layout and rules.

PKG_MAGIC = b"\x7fPKG"
PKG_ENTRY_NPDRM, PKG_ENTRY_NPDRMEDAT, PKG_ENTRY_REGULAR, PKG_ENTRY_FOLDER = 1, 2, 3, 4
PKG_ENTRY_SDAT = 9
PKG_ENTRY_OVERWRITE = 0x80000000
PKG_FILE_TYPES = {1, 2, 3, 5, 6, 9, 0xE, 0x10, 0x11, 0x13, 0x14, 0x15, 0x16, 0x18, 0x19}
CONTENT_TYPES = {4: "game data / update", 5: "game executable", 6: "PS1 classic", 7: "PSP", 9: "theme",
                 0xA: "widget", 0xB: "license", 0xC: "VSH module", 0xD: "avatar", 0xF: "minis", 0x11: "VMC",
                 0x12: "PS2 classic", 0x14: "PSP remastered"}


def _read_pkg_header(f):
    head = f.read(0xC0)
    if len(head) < 0x80 or head[:4] != PKG_MAGIC:
        raise VersionError("not a PS3 package")
    pkg_type, platform, meta_off, meta_count, meta_size, file_count = struct.unpack_from(">HHIIII", head, 4)
    pkg_size, data_off, data_size = struct.unpack_from(">QQQ", head, 0x18)
    content_id = head[0x30:0x60].split(b"\x00", 1)[0].decode("ascii", "replace")
    qa = head[0x60:0x70]
    klic = int.from_bytes(head[0x70:0x80], "big")
    hdr = dict(pkg_type=pkg_type, platform=platform, meta_offset=meta_off, meta_count=meta_count,
               meta_size=meta_size, file_count=file_count, pkg_size=pkg_size, data_offset=data_off,
               data_size=data_size, content_id=content_id, title_id=content_id[7:16], qa_digest=qa, klicensee=klic,
               install_dir=content_id[7:16], content_type=0, drm_type=0, package_flags=0)
    f.seek(meta_off)
    for _ in range(meta_count):
        pid, psize = struct.unpack(">II", f.read(8))
        body = f.read(psize)
        if pid == 1 and psize == 4:
            hdr["drm_type"] = struct.unpack(">I", body)[0]
        elif pid == 2 and psize == 4:
            hdr["content_type"] = struct.unpack(">I", body)[0]
        elif pid == 3 and psize == 4:
            hdr["package_flags"] = struct.unpack(">I", body)[0]
        elif pid == 0xA and psize > 8:
            hdr["install_dir"] = body[8:].split(b"\x00", 1)[0].decode("ascii", "replace")
    return hdr


def pkg_summary(path):
    with open(path, "rb") as f:
        h = _read_pkg_header(f)
    return {"content_id": h["content_id"], "title_id": h["title_id"], "install_dir": h["install_dir"],
            "content_type": CONTENT_TYPES.get(h["content_type"], hex(h["content_type"])),
            "is_patch": bool(h["package_flags"] & 0x10), "debug": h["pkg_type"] == 0, "files": h["file_count"],
            "size": h["pkg_size"]}


def _aes_ecb_encryptor(key):
    """Return encrypt(bytes)->bytes for AES-128-ECB, using whatever is available."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # pycryptodome
        return AES.new(key, AES.MODE_ECB).encrypt
    except ImportError:
        pass
    return _PyAES(key).encrypt_ecb


class _PyAES:
    """Small pure-Python AES-128 encryptor (slow; used only if no crypto library exists).
    Tables are generated at import, not stored."""
    _sbox = None

    @classmethod
    def _tables(cls):
        if cls._sbox:
            return
        def mul(a, b):
            r = 0
            while b:
                if b & 1:
                    r ^= a
                a = ((a << 1) ^ 0x11B) if a & 0x80 else (a << 1)
                b >>= 1
            return r
        inv = [0] * 256
        for a in range(1, 256):
            for b in range(1, 256):
                if mul(a, b) == 1:
                    inv[a] = b
                    break
        sbox = []
        for a in range(256):
            x = inv[a]
            s = x
            for _ in range(4):
                x = ((x << 1) | (x >> 7)) & 0xFF
                s ^= x
            sbox.append(s ^ 0x63)
        cls._sbox = sbox
        cls._mul2 = [mul(a, 2) for a in range(256)]
        cls._mul3 = [mul(a, 3) for a in range(256)]

    def __init__(self, key):
        self._tables()
        s, w, rcon = self._sbox, list(key), 1
        while len(w) < 176:
            t = w[-4:]
            if len(w) % 16 == 0:
                t = [s[t[1]] ^ rcon, s[t[2]], s[t[3]], s[t[0]]]
                rcon = ((rcon << 1) ^ 0x11B) if rcon & 0x80 else rcon << 1
            w.extend([w[-16 + i] ^ t[i] for i in range(4)])
        self.rk = [w[i:i + 16] for i in range(0, 176, 16)]

    def _block(self, b):
        s, m2, m3 = self._sbox, self._mul2, self._mul3
        st = [b[i] ^ self.rk[0][i] for i in range(16)]
        for rnd in range(1, 11):
            st = [s[x] for x in st]
            st = [st[(i + 4 * (i % 4)) % 16] for i in range(16)]  # shift rows (column-major)
            if rnd != 10:
                out = []
                for c in range(4):
                    a = st[4 * c:4 * c + 4]
                    out += [m2[a[0]] ^ m3[a[1]] ^ a[2] ^ a[3], a[0] ^ m2[a[1]] ^ m3[a[2]] ^ a[3],
                            a[0] ^ a[1] ^ m2[a[2]] ^ m3[a[3]], m3[a[0]] ^ a[1] ^ a[2] ^ m2[a[3]]]
                st = out
            st = [st[i] ^ self.rk[rnd][i] for i in range(16)]
        return bytes(st)

    def encrypt_ecb(self, data):
        return b"".join(self._block(data[i:i + 16]) for i in range(0, len(data), 16))


def load_pkg_key(home, settings):
    """The player's package key, checked against the known fingerprint. Returns (name, key) list."""
    candidates = []
    configured = (settings or {}).get("pkg_key_file")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path(home) / "keys" / "ps3-pkg.key")
    keys = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for token in re.findall(r"[0-9A-Fa-f]{32}", text.replace(" ", "")):
            key = bytes.fromhex(token)
            fp = hashlib.sha256(key).hexdigest()
            for name, want in PKG_KEY_FINGERPRINTS.items():
                if fp == want and (name, key) not in keys:
                    keys.append((name, key))
    if not keys and (Path(home) / "keys" / "self-keys.json").is_file():
        # Only once the private decrypter has fetched RPCS3's key source for this workspace.
        keys = _pkg_keys_from_rpcs3_source(home)
    keys.sort(key=lambda k: 0 if k[0] == "retail" else 1)
    return keys


def _pkg_keys_from_rpcs3_source(home):
    """No key file: read the package keys from RPCS3's public key source (the same
    pinned, hash-checked download the EBOOT decrypter uses; private build only),
    keep only keys matching the known fingerprints, and cache them locally."""
    try:
        from .private_features.self_decrypt import _fetch
    except ImportError:
        return []
    try:
        header = _fetch("key_vault.h")
    except Exception:
        return []
    keys = []
    for name, label in (("PKG_AES_KEY", "retail"), ("PKG_AES_KEY_IDU", "idu")):
        m = re.search(r"constexpr u8 %s\[0x10\]\s*=\s*\{(.*?)\};" % name, header, re.S)
        if not m:
            continue
        key = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", m.group(1)))
        if hashlib.sha256(key).hexdigest() == PKG_KEY_FINGERPRINTS[label]:
            keys.append((label, key))
    if keys:
        try:
            path = Path(home) / "keys" / "ps3-pkg.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("\n".join(k.hex() for _, k in keys) + "\n", encoding="utf-8")
        except OSError:
            pass
    return keys


class PkgReader:
    """Reads a PS3 PKG: header, metadata, entries and file data (decrypted on the fly)."""

    def __init__(self, path, keys=()):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        try:
            self._load(keys)
        except BaseException:
            self.f.close()
            raise

    def _load(self, keys):
        self.h = _read_pkg_header(self.f)
        self.size = self.path.stat().st_size
        if self.h["platform"] != 1:
            raise VersionError("only PS3 packages are supported")
        self.cipher = None
        if self.h["pkg_type"] == 0:  # debug package: SHA-1 stream, no key
            self.cipher = "debug"
            self.entries = self._entries()
        else:
            for _name, key in keys:
                self.cipher = _aes_ecb_encryptor(key)
                try:
                    self.entries = self._entries()
                    break
                except VersionError:
                    self.cipher = None
            if self.cipher is None:
                raise VersionError("retail package: the package key is missing or wrong "
                                   "(put it in workspace/keys/ps3-pkg.key)")

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _keystream(self, first_block, count):
        if self.cipher == "debug":
            qa = self.h["qa_digest"]
            base = qa[:8] + qa[:8] + qa[8:16] + qa[8:16] + b"\x00" * 24
            return b"".join(hashlib.sha1(base + struct.pack(">Q", first_block + i)).digest()[:16] for i in range(count))
        klic = self.h["klicensee"]
        ctr = b"".join(((klic + first_block + i) & ((1 << 128) - 1)).to_bytes(16, "big") for i in range(count))
        return self.cipher(ctr)

    def read(self, offset, size):
        """Decrypted bytes at data-relative offset."""
        if size <= 0:
            return b""
        start = offset - offset % 16
        skip = offset - start
        total = skip + size
        blocks = (total + 15) // 16
        self.f.seek(self.h["data_offset"] + start)
        raw = self.f.read(blocks * 16)
        raw += b"\x00" * (blocks * 16 - len(raw))
        ks = self._keystream(start // 16, blocks)
        out = (int.from_bytes(raw, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(raw), "big")
        return out[skip:skip + size]

    def _entries(self):
        n = self.h["file_count"]
        table = self.read(0, n * 32)
        avail = self.size - self.h["data_offset"]
        entries = []
        for i in range(n):
            name_off, name_size, file_off, file_size, typ, pad = struct.unpack_from(">IIQQII", table, 32 * i)
            if name_size > 256 or name_size > avail or name_off > avail - name_size:
                raise VersionError("package entry table did not decrypt")
            if file_size and (file_size > avail or file_off > avail - file_size or name_off == file_off):
                raise VersionError("package entry table did not decrypt")
            entries.append(dict(name_offset=name_off, name_size=name_size, file_offset=file_off,
                                file_size=file_size, type=typ, pad=pad))
        for e in entries:
            raw = self.read(e["name_offset"], e["name_size"]).split(b"\x00", 1)[0]
            try:
                e["name"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise VersionError("package entry names did not decrypt")
        return entries

    def param_sfo(self):
        for e in self.entries:
            if e["name"] == "PARAM.SFO" and e["file_size"]:
                return parse_sfo(self.read(e["file_offset"], e["file_size"]))
        return None


def _safe_rel(name):
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        raise VersionError(f"unsafe file name in package: {name!r}")
    return Path(*parts)


def extract_pkg(reader, dest, emit=print, chunk=8 << 20):
    """Write the package's files into dest (RPCS3 rules: an existing file is replaced only
    when the entry has the overwrite flag). Returns list of written relative paths."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for e in reader.entries:
        typ = e["type"] & 0xFF
        rel = _safe_rel(e["name"])
        target = dest / rel
        if typ == PKG_ENTRY_FOLDER:
            target.mkdir(parents=True, exist_ok=True)
            continue
        if typ not in PKG_FILE_TYPES:
            emit(f"skipped entry of unknown type 0x{e['type']:08x}: {e['name']}")
            continue
        if target.exists() and not (e["type"] & PKG_ENTRY_OVERWRITE):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        with open(tmp, "wb") as out:
            pos = 0
            while pos < e["file_size"]:
                n = min(chunk, e["file_size"] - pos)
                out.write(reader.read(e["file_offset"] + pos, n))
                pos += n
        os.replace(tmp, target)
        written.append(rel.as_posix())
    return written


def _aside(path):
    """Rename an existing path aside (never delete)."""
    path = Path(path)
    if not path.exists():
        return None
    new = path.with_name(f"{path.name}.before-update-{_now()}")
    n = 1
    while new.exists():
        new = path.with_name(f"{path.name}.before-update-{_now()}-{n}")
        n += 1
    path.rename(new)
    return new


def install_pkgs(pkgs, settings, home=None, emit=print, title_id=None):
    """Install one or more update packages, in the order given, into the updates folder.

    The new install is built in a staging folder (starting from a copy of the current
    install, so a chain of updates layers correctly). Only when every package is in,
    the old install is renamed aside and the staging folder takes its place."""
    home = Path(home or default_home())
    keys = load_pkg_key(home, settings)
    readers = []
    try:
        for p in pkgs:
            readers.append(PkgReader(p, keys))
        dirs = {r.h["install_dir"] for r in readers}
        if len(dirs) != 1:
            raise VersionError(f"packages are for different titles: {sorted(dirs)}")
        install_dir = dirs.pop()
        if not TITLE_ID_RE.match(install_dir):
            raise VersionError(f"unexpected install folder name {install_dir!r}")
        if title_id and install_dir != title_id:
            raise VersionError(f"package is for {install_dir}, not {title_id}")
        root = updates_root(settings)
        final = root / install_dir
        staging = root / f".{install_dir}.installing-{_now()}"
        if final.is_dir():
            shutil.copytree(final, staging)
        staging.mkdir(parents=True, exist_ok=True)
        files = []
        for r in readers:
            emit(f"installing {r.path.name} ({r.h['content_id']})")
            files += extract_pkg(r, staging, emit)
        sfo = read_sfo(staging / "PARAM.SFO")
        old = _aside(final)
        staging.rename(final)
        result = {"title_id": install_dir, "folder": str(final), "replaced_to": str(old) if old else "",
                  "version": norm_version((sfo or {}).get("APP_VER", "")), "files": len(files),
                  "packages": [str(r.path) for r in readers],
                  "eboot": str(final / "USRDIR" / "EBOOT.BIN") if (final / "USRDIR" / "EBOOT.BIN").is_file() else "",
                  "next": "EBOOT.BIN is an encrypted SELF: decrypt it with RPCS3 (Utilities > Decrypt PS3 "
                          "Binaries) and select the EBOOT.elf with Set decrypted ELF."}
        _write_json(final.parent.parent.parent / "install-log" / f"{install_dir}-{_now()}.json", result)
        return result
    finally:
        for r in readers:
            r.close()


# --------------------------------------------------------------------------- report

def compare(games, home, settings, online=False, fetch=None):
    curated = load_targets()
    rows = []
    for g in games:
        tid, cur = g["title_id"], g.get("current") or g.get("app_ver")
        target = resolve_target(tid, cur, home, settings, curated)
        row = {"title_id": tid, "title": g.get("title"), "folder": g.get("folder"), "kind": g.get("kind"),
               "disc": g.get("app_ver"), "installed_update": (g.get("installed_updates") or [{}])[0].get("app_ver", ""),
               "current": cur, "target": target["version"] if target else "",
               "target_reason": (target or {}).get("reason", ""), "target_source": (target or {}).get("source", ""),
               "on_disc": [p["sfo"]["app_ver"] for p in disc_updates(g)],
               "latest": "", "official": None, "disc_packages": len(g.get("disc_packages") or []),
               "elf": bool(g.get("eboot_elf"))}
        if online:
            try:
                row["official"] = official_updates(tid, settings, fetch)
                row["latest"] = row["official"][-1]["version"] if row["official"] else "none"
            except (VersionError, OSError, ET.ParseError) as e:
                row["latest"] = "?"
                row["official_error"] = str(e)
        row["action"] = _action(row, target)
        rows.append(row)
    return rows


def _action(row, target):
    cur = version_key(row["current"])
    if target:
        t = version_key(target["version"])
        if t == cur:
            return "ok (at target)"
        if t > cur and target["version"] in row.get("on_disc", []):
            return f"update to {target['version']} (the update is on your disc: --install-disc-update)"
        if t < cur:
            return f"installed {row['current']} is newer than the target {target['version']}: keep a {target['version']} copy for this port"
        return f"update to {target['version']}"
    if row["latest"] not in ("", "?", "none") and version_key(row["latest"]) > cur:
        return f"optional: official {row['latest']} available"
    return "ok"


def write_report(rows, out_dir):
    out_dir = Path(out_dir)
    _write_json(out_dir / "report.json", {"schema": 1, "generated": _now(), "games": rows})
    need = [r for r in rows if r["action"].startswith("update")]
    lines = [f"# Game versions ({len(rows)} games, {len(need)} need an update)", "",
             "| Title ID | Game | Disc | Update on disc | Installed update | Target | Latest official | Action |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['title_id']} | {(r['title'] or '')[:48]} | {r['disc'] or '-'} | {', '.join(sorted(set(r.get('on_disc') or []))) or '-'} | {r['installed_update'] or '-'} | "
                     f"{r['target'] or '-'} | {r['latest'] or 'not checked'} | {r['action']} |")
    reasons = [r for r in rows if r["target"]]
    if reasons:
        lines += ["", "## Why these targets", ""]
        lines += [f"- {r['title_id']} {r['target']}: {r['target_reason']} ({r['target_source']})" for r in reasons]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir / "report.md"


# --------------------------------------------------------------------------- CLI

def _projects(home):
    try:
        return json.loads((Path(home) / "studio.json").read_text(encoding="utf-8")).get("projects", [])
    except (OSError, ValueError):
        return []


def project_game(home, settings, ident):
    for p in _projects(home):
        if ident in (p.get("id"), p.get("name")):
            src = Path(p.get("source") or "")
            for parent in (src, *src.parents):
                info = game_info(parent, settings)
                if info:
                    return p, info
            raise VersionError(f"no PARAM.SFO found for project {ident}")
    raise VersionError(f"no project {ident}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="PS3 game versions and updates")
    ap.add_argument("--home", help="Studio workspace (default: workspace/)")
    ap.add_argument("--games-folder", help="override settings games_folder")
    ap.add_argument("--scan", action="store_true", help="scan every game and write the report")
    ap.add_argument("--project", help="one project id or name")
    ap.add_argument("--online", action="store_true", help="also read Sony's official update lists")
    ap.add_argument("--hash", action="store_true", help="hash EBOOTs (slow on large libraries)")
    ap.add_argument("--install-pkg", nargs="+", metavar="PKG", help="install update package(s) you supplied, in order")
    ap.add_argument("--install-disc-update", metavar="ID", help="install the update package your own disc carries")
    ap.add_argument("--trust-update-cert", metavar="SHA256", help="approve Sony's update-list server certificate")
    ap.add_argument("--update", metavar="ID", help="(not in the Public Edition) download + install official updates for a project")
    ap.add_argument("--update-all-needed", action="store_true", help="(not in the Public Edition) update every game below its target")
    ap.add_argument("--to", default="target", help="latest | target | XX.YY (default target)")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(APP))
    home = Path(a.home).expanduser() if a.home else default_home()
    settings = load_settings(home)
    if a.games_folder:
        settings["games_folder"] = a.games_folder
    if a.trust_update_cert:
        fp = a.trust_update_cert.lower().replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{64}", fp):
            print("expected a 64-digit SHA-256 fingerprint")
            return 2
        state_path = home / "studio.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("settings", {})["update_server_cert_sha256"] = fp
        _write_json(state_path, state)
        print("approved")
        return 0
    if a.install_pkg:
        res = install_pkgs(a.install_pkg, settings, home)
        print(json.dumps(res, indent=2))
        return 0
    if a.install_disc_update:
        p, info = project_game(home, settings, a.install_disc_update)
        want = None if a.to in ("target", "latest") else a.to
        if a.to == "target":
            t = resolve_target(info["title_id"], info["current"], home, settings)
            want = t["version"] if t else None
        pkgs = disc_updates(info, want)
        if not pkgs:
            print("no matching update package on this disc")
            return 2
        best = pkgs[-1]["sfo"]["app_ver"]
        pkgs = [x for x in pkgs if x["sfo"]["app_ver"] == best][:1]
        print(json.dumps(install_pkgs([x["path"] for x in pkgs], settings, home, title_id=info["title_id"]), indent=2))
        return 0
    if a.update or a.update_all_needed:
        try:
            from studio.private_features import update_download
        except ImportError:
            print("Downloading updates is not part of this build. Download the update yourself and use --install-pkg.")
            return 2
        if a.update:
            p, info = project_game(home, settings, a.update)
            print(json.dumps(update_download.update_game(info, settings, home, to=a.to), indent=2))
        else:
            print(json.dumps(update_download.update_all_needed(scan_games(settings), settings, home), indent=2))
        return 0
    if a.project:
        p, info = project_game(home, settings, a.project)
        rows = compare([info], home, settings, a.online)
        print(json.dumps(rows[0], indent=2))
        return 0
    if a.scan:
        games = scan_games(settings, None, a.hash)
        rows = compare(games, home, settings, a.online)
        path = write_report(rows, home / "game-versions")
        need = sum(1 for r in rows if r["action"].startswith("update"))
        print(f"{len(rows)} games scanned, {need} need an update. Report: {path}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
