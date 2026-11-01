# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Game-shipped PRX module discovery (synthetic files only, no game data)."""
import tempfile
import unittest
from pathlib import Path

from studio import game_prx


class GamePrxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        usr = self.root / "PS3_GAME" / "USRDIR"
        (usr / "libs").mkdir(parents=True)
        (usr / "Engine.sprx").write_bytes(b"SCE\0")
        (usr / "libs" / "Unused.sprx").write_bytes(b"SCE\0")
        (usr / "libs" / "Physics.prx").write_bytes(b"\x7fELF")
        (usr / "readme.txt").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_modules_the_eboot_names_are_found(self):
        eboot = b"\0/dev_bdvd/PS3_GAME/USRDIR/engine.sprx\0physics.sprx\0"
        names = [p.name for p in game_prx.find_modules(self.root, eboot)]
        self.assertEqual(names, ["Engine.sprx", "Physics.prx"])

    def test_no_modules_means_no_change(self):
        port = self.root / "port"; port.mkdir()
        (port / "CMakeLists.txt").write_text("add_executable(${PROJECT_NAME} main.cpp)\n")
        eboot = self.root / "EBOOT.elf"; eboot.write_bytes(b"nothing here")
        self.assertIsNone(game_prx.apply(port, self.root, eboot, self.root, self.root))
        self.assertEqual((port / "CMakeLists.txt").read_text(), "add_executable(${PROJECT_NAME} main.cpp)\n")

    def test_symbol_prefix_is_a_valid_identifier(self):
        self.assertEqual(game_prx._slug("PathEngine"), "pathengine")
        self.assertEqual(game_prx._slug("2nd-Module v1"), "prx_2nd_module_v1")


if __name__ == "__main__":
    unittest.main()
