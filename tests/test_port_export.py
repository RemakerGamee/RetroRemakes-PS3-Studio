# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json, tempfile, unittest
from pathlib import Path
from studio import port_export as pe


class PortExportTests(unittest.TestCase):
    def test_display_ini_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "display.ini"
            pe.write_display_ini(path, {"RR_OUTPUT": "2560x1440", "RR_FPS_LIMIT": "120", "RR_AA": "fxaa"})
            values = pe.read_display_ini(path)
            self.assertEqual(values, {"RR_OUTPUT": "2560x1440", "RR_FPS_LIMIT": "120", "RR_AA": "fxaa"})

    def test_display_ini_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            pe.display_ini_text({"RR_OUTPUT": "1920x1080\nRR_X=1"})

    def test_export_port_copies_build_and_adds_tools(self):
        with tempfile.TemporaryDirectory() as d:
            build = Path(d) / "build" / "player"; build.mkdir(parents=True)
            (build / "MyGameRecomp.exe").write_bytes(b"MZ" + b"\0" * 62)
            (build / "SDL2.dll").write_bytes(b"MZ")
            (build / "MyGameRecomp.pdb").write_bytes(b"x")
            out = Path(d) / "Ports"
            port = pe.export_port(build / "MyGameRecomp.exe", out, "Test Game: Deluxe", ["BCUS00000"], emit=lambda m: None)
            self.assertTrue((port / "MyGameRecomp.exe").is_file())
            self.assertTrue((port / "SDL2.dll").is_file())
            self.assertFalse((port / "MyGameRecomp.pdb").exists())
            self.assertTrue((port / "display.ini").is_file())
            data = json.loads((port / "port.json").read_text())
            self.assertEqual(data["port_id"], "test-game-deluxe")
            self.assertTrue((port / "Launch.bat").is_file())
            again = pe.export_port(build / "MyGameRecomp.exe", out, "Test Game: Deluxe", emit=lambda m: None)
            self.assertTrue(again.is_dir())
            self.assertEqual(len([p for p in out.iterdir() if p.name.startswith("Test Game Deluxe.previous-")]), 1)

    def test_update_skips_without_param_sfo(self):
        with tempfile.TemporaryDirectory() as d:
            r = pe.update_game_version({"source": d}, {}, d, emit=lambda m: None)
            self.assertEqual(r["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
