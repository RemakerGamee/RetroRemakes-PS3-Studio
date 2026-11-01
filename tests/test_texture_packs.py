# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Texture pack tools on synthetic textures only (no game data)."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio import texture_packs as tp
from studio import release_package as rp


def bc1_block(c0, c1, bits):
    return struct.pack("<HHI", c0, c1, bits)


class TexturePackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.port = Path(self.tmp.name)
        self.dump = self.port / "textures" / "dump" / "TEST00001"; self.dump.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dds_round_trip_and_mips(self):
        rgba = bytes(range(256)) * 4 * 4          # 32x32 RGBA8
        levels = tp.mip_chain(32, 32, rgba)
        self.assertEqual(len(levels), 6)
        f = self.port / "a.dds"; tp.write_dds(f, "RGBA8", 32, 32, levels)
        fmt, w, h, mips, top = tp.read_dds(f)
        self.assertEqual((fmt, w, h, mips, top), ("RGBA8", 32, 32, 6, rgba))

    def test_bc1_decode(self):
        red, blue = 0xF800, 0x001F
        data = bc1_block(red, blue, 0x55555555) * 16           # every texel = colour 1 (blue)
        out = tp.decode_rgba("BC1", 16, 16, data)
        self.assertEqual(out[:4], bytes((0, 0, 255, 255)))
        self.assertEqual(len(out), 16 * 16 * 4)

    def test_bc3_alpha(self):
        alpha = bytes((255, 0)) + b"\0" * 6                     # index 0 everywhere -> 255
        data = (alpha + bc1_block(0xFFFF, 0, 0)) * 16
        out = tp.decode_rgba("BC3", 16, 16, data)
        self.assertEqual(out[:4], bytes((255, 255, 255, 255)))

    def test_png_round_trip_without_pillow(self):
        rgba = bytes((x * 7 + y) & 255 for y in range(20) for x in range(17 * 4))
        f = self.port / "p.png"; tp.write_png(f, 17, 20, rgba)
        with mock.patch.dict(sys.modules, {"PIL": None}):
            self.assertEqual(tp.read_png(f), (17, 20, rgba))

    def _dump(self, h="0123456789ABCDEF"):
        rgba = bytes((i * 3) & 255 for i in range(16 * 16 * 4))
        tp.write_dds(self.dump / f"{h}_16x16_85.dds", "RGBA8", 16, 16, [rgba])
        return rgba

    def test_repack_without_upscaler(self):
        self._dump()
        st = tp.status(self.port, "TEST00001"); self.assertEqual((st["dumped"], len(st["new"])), (1, 1))
        n = tp.upscale(self.port, "TEST00001", None, scale=1, emit=lambda *_: None)
        self.assertEqual(n, 1)
        out = self.port / "textures" / "load" / "TEST00001" / "0123456789ABCDEF_16x16.dds"
        self.assertTrue(out.is_file())
        self.assertEqual(tp.status(self.port, "TEST00001")["new"], [])

    def test_external_upscaler_folder_template(self):
        self._dump()
        script = self.port / "fake_up.py"
        script.write_text(
            "import sys,pathlib\nsys.path.insert(0, %r)\nfrom studio import texture_packs as tp\n"
            "src,dst,s=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),int(sys.argv[3])\n"
            "for p in src.glob('*.png'):\n w,h,px=tp.read_png(p)\n"
            " out=b''.join(px[((y//s)*w+(x//s))*4:((y//s)*w+(x//s))*4+4] for y in range(h*s) for x in range(w*s))\n"
            " tp.write_png(dst/p.name,w*s,h*s,out)\n" % str(Path(tp.__file__).resolve().parents[1]))
        template = f'"{sys.executable}" "{script}" {{in}} {{out}} {{scale}}'
        with mock.patch.object(tp, "find_tool", return_value=sys.executable), \
             mock.patch.object(tp.shutil, "which", return_value=None):
            n = tp.upscale(self.port, "TEST00001", template, scale=2, emit=lambda *_: None)
        self.assertEqual(n, 1)
        fmt, w, h, mips, _ = tp.read_dds(self.port / "textures" / "load" / "TEST00001" / "0123456789ABCDEF_32x32.dds")
        self.assertEqual((fmt, w, h, mips), ("RGBA8", 32, 32, 6))

    def test_missing_upscaler_is_explained(self):
        self._dump()
        with self.assertRaises(RuntimeError):
            tp.upscale(self.port, "TEST00001", None, scale=4, emit=lambda *_: None)

    def test_release_check_refuses_textures(self):
        self.assertIn(".dds", rp.BINARY_EXT)


if __name__ == "__main__":
    unittest.main()
