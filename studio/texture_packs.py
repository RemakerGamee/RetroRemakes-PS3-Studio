# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""HD texture packs for native ports.

A port run with RR_TEX_DUMP=1 saves each texture the game uploads, once, to
    <port folder>/textures/dump/<title id>/<hash>_<w>x<h>_<fmt>.dds
This module turns new dumps into HD replacements:
    1. decode each dump to PNG (BC1/BC2/BC3, RGBA8, BGRA8, RG8, R8),
    2. run the upscaler the player configured (no models are shipped),
    3. encode the result as DDS (BC7 with mips when texconv is installed,
       otherwise RGBA8 with mips) into
    <port folder>/textures/load/<title id>/<hash>_<w>x<h>.dds
The port loads that folder automatically on its next start.

Texture packs come from the player's own game and stay on the player's PC:
they are never part of a Studio release or a port update.

Upscaler setting (Studio settings "texture_upscaler", or --upscaler): a command
template. {in} and {out} are folders, or use {in_file} and {out_file} for a
tool that takes one image at a time; {scale} is the factor. Examples:
    realesrgan-ncnn-vulkan -i {in} -o {out} -s {scale} -f png
    chaiNNer run <chain file> --override in={in} out={out}
Without one, --upscale stops with a message; --scale 1 just repacks.

    python -m studio.texture_packs --game ID [--list] [--upscale] [--scale 4]
    python -m studio.texture_packs --port-dir DIR --title BCUS98155 --upscale
"""
import argparse
import json
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

DDS_MAGIC = b"DDS "
DXGI = {"BC1": 71, "BC2": 74, "BC3": 77, "BC7": 98, "RGBA8": 28, "BGRA8": 87, "RG8": 49, "R8": 61}
_DXGI_NAMES = {v: k for k, v in DXGI.items()}
_DXGI_NAMES.update({72: "BC1", 75: "BC2", 78: "BC3", 99: "BC7", 29: "RGBA8", 91: "BGRA8"})


# ------------------------------------------------------------------ DDS

def read_dds(path):
    """Return (format name, width, height, mip count, top-level bytes)."""
    data = Path(path).read_bytes()
    if data[:4] != DDS_MAGIC or len(data) < 128:
        raise ValueError(f"{path}: not a DDS file")
    (size, flags, height, width, _pitch, _depth, mips) = struct.unpack_from("<7I", data, 4)
    pf_flags, fourcc, bits, rmask = struct.unpack_from("<4I", data, 80)
    offset = 128
    if pf_flags & 4 and fourcc == 0x30315844:          # DX10
        dxgi = struct.unpack_from("<I", data, 128)[0]; offset = 148
        fmt = _DXGI_NAMES.get(dxgi)
    elif pf_flags & 4:
        fmt = {0x31545844: "BC1", 0x33545844: "BC2", 0x35545844: "BC3"}.get(fourcc)
    elif pf_flags & 0x40 and bits == 32:
        fmt = "RGBA8" if rmask == 0xFF else "BGRA8"
    else:
        fmt = None
    if not fmt:
        raise ValueError(f"{path}: unsupported DDS pixel format")
    mips = mips if flags & 0x20000 and mips else 1
    return fmt, width, height, mips, data[offset:offset + level_size(fmt, width, height)]


def level_size(fmt, w, h):
    if fmt.startswith("BC"):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * (8 if fmt == "BC1" else 16)
    return w * h * {"RGBA8": 4, "BGRA8": 4, "RG8": 2, "R8": 1}[fmt]


def write_dds(path, fmt, w, h, levels):
    """Write a DX10-header DDS; levels is a list of bytes, largest first."""
    blk = fmt.startswith("BC")
    pitch = max(1, (w + 3) // 4) * (8 if fmt == "BC1" else 16) if blk else w * (4 if fmt in ("RGBA8", "BGRA8") else 1)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | (0x80000 if blk else 0x8) | (0x20000 if len(levels) > 1 else 0)
    caps = 0x1000 | ((0x8 | 0x400000) if len(levels) > 1 else 0)
    head = DDS_MAGIC + struct.pack("<7I", 124, flags, h, w, pitch, 0, len(levels)) + b"\0" * 44
    head += struct.pack("<8I", 32, 4, 0x30315844, 0, 0, 0, 0, 0) + struct.pack("<5I", caps, 0, 0, 0, 0)
    head += struct.pack("<5I", DXGI[fmt], 3, 0, 1, 0)
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".dds.part")
    tmp.write_bytes(head + b"".join(levels)); tmp.replace(path)


def _c565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def _color_block(blk, four_always):
    c0, c1 = struct.unpack_from("<HH", blk, 0)
    p0, p1 = _c565(c0), _c565(c1)
    if c0 > c1 or four_always:
        pal = [p0 + (255,), p1 + (255,), tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,),
               tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,)]
    else:
        pal = [p0 + (255,), p1 + (255,), tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,), (0, 0, 0, 0)]
    bits = struct.unpack_from("<I", blk, 4)[0]
    return [pal[(bits >> (2 * i)) & 3] for i in range(16)]


def decode_rgba(fmt, w, h, data):
    """Top level of a dump -> bytes RGBA8, row-major."""
    if fmt == "RGBA8":
        return bytes(data[:w * h * 4])
    if fmt == "BGRA8":
        out = bytearray(data[:w * h * 4]); out[0::4], out[2::4] = out[2::4], out[0::4]; return bytes(out)
    if fmt == "RG8":
        out = bytearray(w * h * 4)
        out[0::4] = data[0:w * h * 2:2]; out[1::4] = data[1:w * h * 2:2]; out[3::4] = b"\xff" * (w * h); return bytes(out)
    if fmt == "R8":
        out = bytearray(w * h * 4); px = data[:w * h]
        out[0::4] = px; out[1::4] = px; out[2::4] = px; out[3::4] = b"\xff" * (w * h); return bytes(out)
    if fmt not in ("BC1", "BC2", "BC3"):
        raise ValueError(f"cannot decode {fmt}")
    out = bytearray(w * h * 4); bw = max(1, (w + 3) // 4); step = 8 if fmt == "BC1" else 16
    for by in range((h + 3) // 4):
        for bx in range(bw):
            blk = data[(by * bw + bx) * step:(by * bw + bx + 1) * step]
            if len(blk) < step:
                continue
            if fmt == "BC1":
                px = _color_block(blk, False); alpha = None
            else:
                px = _color_block(blk[8:], True)
                if fmt == "BC2":
                    a = int.from_bytes(blk[:8], "little"); alpha = [((a >> (4 * i)) & 15) * 17 for i in range(16)]
                else:
                    a0, a1 = blk[0], blk[1]; ab = int.from_bytes(blk[2:8], "little")
                    pal = [a0, a1] + ([((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)] if a0 > a1 else
                                      [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255])
                    alpha = [pal[(ab >> (3 * i)) & 7] for i in range(16)]
            for i, (r, g, b, a) in enumerate(px):
                x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                if x < w and y < h:
                    o = (y * w + x) * 4
                    out[o:o + 4] = bytes((r, g, b, alpha[i] if alpha else a))
    return bytes(out)


# ------------------------------------------------------------------ PNG (no extra packages needed)

def write_png(path, w, h, rgba):
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def read_png(path):
    """8-bit, non-interlaced grey/RGB/RGBA/grey-alpha PNG -> (w, h, RGBA bytes)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGBA"); return im.width, im.height, im.tobytes()
    except ImportError:
        pass
    data = Path(path).read_bytes(); pos = 8; idat = b""; w = h = ctype = 0
    while pos < len(data):
        n = struct.unpack_from(">I", data, pos)[0]; t = data[pos + 4:pos + 8]; d = data[pos + 8:pos + 8 + n]; pos += 12 + n
        if t == b"IHDR":
            w, h, depth, ctype, _c, _f, inter = struct.unpack(">IIBBBBB", d)
            if depth != 8 or inter:
                raise ValueError(f"{path}: only 8-bit non-interlaced PNG is supported without Pillow")
        elif t == b"IDAT":
            idat += d
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]; stride = w * ch; raw = zlib.decompress(idat)
    prev = bytearray(stride); rows = []
    for y in range(h):
        f = raw[y * (stride + 1)]; line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - ch] if i >= ch else 0; b = prev[i]; c = prev[i - ch] if i >= ch else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line)); prev = line
    px = b"".join(rows); out = bytearray(w * h * 4)
    if ch == 4: return w, h, px
    for i in range(w * h):
        if ch == 3: out[i * 4:i * 4 + 4] = px[i * 3:i * 3 + 3] + b"\xff"
        elif ch == 1: out[i * 4:i * 4 + 4] = bytes((px[i],) * 3 + (255,))
        else: out[i * 4:i * 4 + 4] = bytes((px[i * 2],) * 3 + (px[i * 2 + 1],))
    return w, h, bytes(out)


def mip_chain(w, h, rgba):
    levels = [rgba]
    while w > 1 or h > 1:
        nw, nh = max(1, w // 2), max(1, h // 2); src = levels[-1]; dst = bytearray(nw * nh * 4)
        for y in range(nh):
            for x in range(nw):
                for c in range(4):
                    s = 0
                    for dy in (0, 1):
                        for dx in (0, 1):
                            sx, sy = min(w - 1, x * 2 + dx), min(h - 1, y * 2 + dy)
                            s += src[(sy * w + sx) * 4 + c]
                    dst[(y * nw + x) * 4 + c] = s // 4
        levels.append(bytes(dst)); w, h = nw, nh
    return levels


# ------------------------------------------------------------------ packs

def parse_name(name):
    """'<16 hex>_<w>x<h>[_<fmt>].dds' -> hash string or None."""
    head = name.split("_", 1)[0].split(".", 1)[0]
    return head.upper() if len(head) == 16 and all(c in "0123456789abcdefABCDEF" for c in head) else None


def titles(port_dir):
    base = Path(port_dir) / "textures" / "dump"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def status(port_dir, title):
    dump = Path(port_dir) / "textures" / "dump" / title
    load = Path(port_dir) / "textures" / "load" / title
    dumped = {parse_name(p.name): p for p in dump.glob("*.dds") if parse_name(p.name)} if dump.is_dir() else {}
    done = {parse_name(p.name) for p in load.rglob("*.dds") if parse_name(p.name)} if load.is_dir() else set()
    return {"title": title, "dumped": len(dumped), "replaced": len(done & set(dumped)),
            "new": sorted(h for h in dumped if h not in done), "dump_files": dumped}


def find_tool(template):
    if not template:
        return None
    try:
        exe = shlex.split(template, posix=False)[0].strip('"')
    except ValueError:
        return None
    return shutil.which(exe) or (exe if Path(exe).is_file() else None)


def _run(cmd, emit):
    emit("running: " + cmd)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"upscaler failed ({r.returncode}): {(r.stderr or r.stdout)[-400:]}")


def upscale(port_dir, title, template=None, scale=4, texconv=None, limit=0, emit=print):
    """Upscale every dump with no replacement yet. Returns the number written."""
    st = status(port_dir, title)
    todo = st["new"][:limit] if limit else st["new"]
    if not todo:
        emit(f"{title}: nothing new ({st['dumped']} dumped, {st['replaced']} already replaced)"); return 0
    if scale > 1 and not find_tool(template):
        raise RuntimeError("No upscaler configured or found. Set the Studio setting 'texture_upscaler' "
                           "(for example: realesrgan-ncnn-vulkan -i {in} -o {out} -s {scale} -f png).")
    texconv = texconv or shutil.which("texconv")
    load = Path(port_dir) / "textures" / "load" / title
    written = 0
    with tempfile.TemporaryDirectory(prefix="rr-tex-") as tmp:
        src, dst = Path(tmp, "in"), Path(tmp, "out"); src.mkdir(); dst.mkdir()
        sizes = {}
        for h in todo:
            try:
                fmt, w, hh, _m, top = read_dds(st["dump_files"][h])
                write_png(src / f"{h}.png", w, hh, decode_rgba(fmt, w, hh, top)); sizes[h] = (w, hh)
            except (ValueError, OSError) as exc:
                emit(f"skip {h}: {exc}")
        if scale > 1:
            if "{in_file}" in template:
                for png in sorted(src.glob("*.png")):
                    _run(template.format(in_file=f'"{png}"', out_file=f'"{dst / png.name}"', scale=scale), emit)
            else:
                _run(template.format(**{"in": f'"{src}"', "out": f'"{dst}"', "scale": scale}), emit)
        else:
            for png in src.glob("*.png"):
                shutil.copy2(png, dst / png.name)
        for png in sorted(dst.glob("*.png")):
            h = parse_name(png.stem)
            if not h or h not in sizes:
                continue
            w0, h0 = sizes[h]
            if texconv:
                r = subprocess.run([texconv, "-nologo", "-y", "-f", "BC7_UNORM", "-m", "0", "-o", tmp, str(png)],
                                   capture_output=True, text=True)
                made = Path(tmp, png.stem + ".dds")
                if r.returncode == 0 and made.is_file():
                    w, hh = read_dds(made)[1:3]
                    target = load / f"{h}_{w}x{hh}.dds"; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(made), target); written += 1; continue
                emit(f"texconv failed for {h}; writing uncompressed")
            w, hh, rgba = read_png(png)
            write_dds(load / f"{h}_{w}x{hh}.dds", "RGBA8", w, hh, mip_chain(w, hh, rgba))
            written += 1
            emit(f"{h}: {w0}x{h0} -> {w}x{hh}")
    emit(f"{title}: wrote {written} HD texture(s) to {load}")
    return written


def port_dir_for(studio, game):
    p = next((x for x in studio.state.get("projects", []) if x.get("id") == game or x.get("name") == game), None)
    if not p:
        raise SystemExit(f"unknown game: {game}")
    for c in studio.list_test_candidates(p):
        exe = c.get("native_exe")
        if exe and Path(exe).is_file():
            return Path(exe).parent, p
    raise SystemExit(f"{p.get('name')}: no finished native build yet")


def main(argv=None):
    ap = argparse.ArgumentParser(description="HD texture packs for native ports")
    ap.add_argument("--game", help="Studio project id or name")
    ap.add_argument("--port-dir", help="folder holding the port's exe (instead of --game)")
    ap.add_argument("--title", help="title id folder (default: every dumped title)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--upscale", action="store_true")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--upscaler", help="command template; overrides the Studio setting")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    template = a.upscaler
    if a.game:
        from .core import Studio
        studio = Studio(); port, _p = port_dir_for(studio, a.game)
        template = template or studio.state.get("settings", {}).get("texture_upscaler")
    elif a.port_dir:
        port = Path(a.port_dir)
    else:
        ap.error("--game or --port-dir is required")
    for title in [a.title] if a.title else titles(port):
        st = status(port, title)
        print(json.dumps({k: (v if k != "new" else len(v)) for k, v in st.items() if k != "dump_files"}))
        if a.upscale:
            upscale(port, title, template, a.scale, limit=a.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
