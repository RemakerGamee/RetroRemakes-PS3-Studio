# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Game patch reader/applier tests. Synthetic ELF and patch file only, no game data."""
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from studio import game_patches as gp

CODE_VADDR, DATA_VADDR = 0x10000, 0x20000


def make_elf():
    """ELF64 big-endian: PT_LOAD code (0x100 bytes), PT_LOAD data (0x40 file / 0x80 mem), PT_NOTE."""
    phnum, phoff = 3, 0x40
    code_off = phoff + 56 * phnum
    code = bytes(range(256))
    data_off = code_off + len(code)
    data = b"\x11" * 0x40
    header = b"\x7fELF" + bytes([2, 2, 1, 0x66]) + b"\x00" * 8
    header += struct.pack(">HHIQQQIHHHHHH", 2, 21, 1, CODE_VADDR, phoff, 0, 0, 64, 56, phnum, 64, 0, 0)
    ph = struct.pack(">IIQQQQQQ", 1, 5, code_off, CODE_VADDR, CODE_VADDR, len(code), len(code), 0x10000)
    ph += struct.pack(">IIQQQQQQ", 1, 6, data_off, DATA_VADDR, DATA_VADDR, len(data), 0x80, 0x10000)
    ph += struct.pack(">IIQQQQQQ", 4, 4, 0, 0, 0, 0, 0, 4)
    return header + ph + code + data, code_off, data_off


def expected_hash(elf):
    sha = hashlib.sha1()
    for n in range(3):
        p_type, p_flags, off, vaddr, _pa, filesz, memsz, _al = struct.unpack_from(">IIQQQQQQ", elf, 0x40 + 56 * n)
        sha.update(struct.pack(">II", p_type, p_flags))
        if p_type == 1 and memsz:
            sha.update(struct.pack(">QQ", vaddr, memsz) + elf[off:off + filesz])
    return "PPU-" + sha.hexdigest()


PATCH_YML = """Version: 1.2

Anchors:
  shared: &shared_writes
    - [ be16, 0x00000010, 0x1234 ]  # a comment
    - [ byte, 0x00000012, 0xAB ]

{key}:
  "60 FPS unlock":
    Games:
      "Synthetic Game":
        TEST00001: [ 01.00 ]
    Author: "Tester"
    Notes: |
      Line one
      Line two
    Patch Version: 1.0
    Group: "Frame rate"
    Configurable Values:
      "Target":
        Type: long_range
        Value: 60
        Min: 30
        Max: 120
    Patch:
      - [ be32, 0x00010000, 0x60000000 ]
      - [ be32, 0x00010004, Target ]
      - [ bef32, 0x00020000, 0.5 ]
      - [ load, *shared_writes, 0x10000 ]
      - [ utf8, 0x00020010, "Hi" ]
      - [ c_utf8, 0x00020014, "OK" ]
      - [ be64, 0x00020020, -1 ]
  "Needs allocation":
    Games:
      "Synthetic Game":
        TEST00001: [ 01.00 ]
    Patch:
      - [ code_alloc, 0x00010000, 0x10 ]
  "Other region":
    Games:
      "Synthetic Game":
        TEST99999: [ 01.00 ]
    Patch:
      - [ byte, 0x00010000, 1 ]
  "Out of file":
    Patch:
      - [ be32, 0x00020060, 1 ]

PPU-0000000000000000000000000000000000000000:
  "Wrong game":
    Patch:
      - [ byte, 0x00010000, 1 ]
"""


class GamePatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.elf_bytes, self.code_off, self.data_off = make_elf()
        self.elf = self.root / "EBOOT.ELF"
        self.elf.write_bytes(self.elf_bytes)
        self.key = gp.ppu_hash(self.elf)
        (self.root / "patches").mkdir()
        self.yml = self.root / "patches" / "patch.yml"
        self.yml.write_text(PATCH_YML.format(key=self.key), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_hash_matches_reference_algorithm(self):
        self.assertEqual(self.key, expected_hash(self.elf_bytes))
        self.assertRegex(self.key, r"^PPU-[0-9a-f]{40}$")

    def test_builtin_yaml_matches_pyyaml(self):
        text = PATCH_YML.format(key=self.key)
        mini = gp._MiniYaml(text).parse()
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        self.assertEqual(mini, yaml.load(text, Loader=yaml.BaseLoader))

    def test_parse_and_list(self):
        db = gp.load_patch_files(gp.find_patch_files(self.root, {}))
        self.assertEqual(len(db["files"]), 1)
        names = [p["name"] for p in gp.patches_for(db, self.key, "TEST00001", "01.00")]
        self.assertEqual(names, ["60 FPS unlock", "Needs allocation", "Out of file"])
        fps = gp.patches_for(db, self.key)[0]
        self.assertTrue(fps["applicable"])
        self.assertEqual(fps["group"], "Frame rate")
        self.assertEqual(fps["notes"], "Line one\nLine two\n")
        self.assertEqual(fps["config"]["Target"]["value"], "60")
        alloc = [p for p in db["patches"] if p["name"] == "Needs allocation"][0]
        self.assertFalse(alloc["applicable"])
        self.assertEqual(alloc["unsupported"], ["code_alloc"])

    def test_apply_writes_copy_and_leaves_original(self):
        db = gp.load_patch_files([self.yml])
        fps = [p for p in db["patches"] if p["name"] == "60 FPS unlock"]
        out = self.root / "attempt" / "patched" / "EBOOT.ELF"
        record = gp.apply_patches(self.elf, out, fps, {"60 FPS unlock": {"Target": 120}})
        self.assertEqual(self.elf.read_bytes(), self.elf_bytes)
        new = out.read_bytes()
        c, d = self.code_off, self.data_off
        self.assertEqual(new[c:c + 4], bytes.fromhex("60000000"))
        self.assertEqual(new[c + 4:c + 8], struct.pack(">I", 120))
        self.assertEqual(new[c + 0x10:c + 0x13], bytes.fromhex("1234AB"))
        self.assertEqual(new[d:d + 4], struct.pack(">f", 0.5))
        self.assertEqual(new[d + 0x10:d + 0x12], b"Hi")
        self.assertEqual(new[d + 0x14:d + 0x17], b"OK\x00")
        self.assertEqual(new[d + 0x20:d + 0x28], b"\xff" * 8)
        self.assertEqual(len(new), len(self.elf_bytes))
        self.assertEqual(record["writes"], 8)
        self.assertEqual(record["ppu_hash"], self.key)

    def test_refusals(self):
        db = gp.load_patch_files([self.yml])
        by = {p["name"]: p for p in db["patches"]}
        out = self.root / "copy.elf"
        with self.assertRaises(gp.PatchError):
            gp.apply_patches(self.elf, out, [by["Out of file"]])
        with self.assertRaises(gp.PatchError):
            gp.apply_patches(self.elf, out, [by["Needs allocation"]])
        with self.assertRaises(gp.PatchError):
            gp.apply_patches(self.elf, out, [by["Wrong game"]])
        with self.assertRaises(gp.PatchError):
            gp.apply_patches(self.elf, self.elf, [by["60 FPS unlock"]])
        self.assertEqual(self.elf.read_bytes(), self.elf_bytes)

    def test_prepare_for_build(self):
        attempt = self.root / "attempt"
        project = {"patches": []}
        same, record = gp.prepare_patched_elf(self.root, {}, project, self.elf, attempt)
        self.assertEqual((same, record), (self.elf, None))
        project = {"patches": ["60 FPS unlock"]}
        meta = {"TITLE_ID": "TEST00001", "APP_VER": "01.00"}
        patched, record = gp.prepare_patched_elf(self.root, {}, project, self.elf, attempt, meta)
        self.assertEqual(patched, attempt / "patched" / "EBOOT.ELF")
        self.assertEqual(patched.name, self.elf.name)
        self.assertEqual(record["applied"][0]["name"], "60 FPS unlock")
        self.assertEqual(record["patch_files"][0]["sha256"], hashlib.sha256(self.yml.read_bytes()).hexdigest())
        with self.assertRaises(gp.PatchError):
            gp.prepare_patched_elf(self.root, {}, {"patches": ["Other region"]}, self.elf, attempt, meta)

    def test_works_without_pyyaml(self):
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"yaml": None}):
            db = gp.load_patch_files([self.yml])
        self.assertEqual(db["errors"], [])
        fps = [p for p in db["patches"] if p["name"] == "60 FPS unlock"]
        record = gp.apply_patches(self.elf, self.root / "nopy.elf", fps)
        self.assertEqual(record["writes"], 8)

    def test_version_mismatch_rejected(self):
        with self.assertRaises(gp.PatchError):
            gp.parse_patch_text("Version: 1.0\n")


if __name__ == "__main__":
    unittest.main()
