# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json, tempfile, unittest, zipfile
from pathlib import Path
from studio import port_updates as pu
from studio import support_bundles as sb


class PortUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.port = self.tmp / "port"
        pu.write_port_json(self.port, "test-port", "1.0.0", "Test", ["TEST00000"])
        self.src = self.tmp / "fix"
        (self.src / "runtime").mkdir(parents=True)
        (self.src / "runtime" / "fix.c").write_text("int fixed(void){return 1;}\n")
        (self.src / "recipes").mkdir()
        (self.src / "recipes" / "r.json").write_text('{"offset": 4096, "sha256": "00"}')

    def test_build_and_apply(self):
        out = self.tmp / "out"
        m = pu.build_update("test-port", "1.0.1", self.src, out, ["TEST00000"], "fix")
        self.assertTrue((out / "test-port.json").is_file())
        pu.apply_update(self.port, out / "test-port-1.0.1.zip", m)
        info = json.loads((self.port / "port.json").read_text())
        self.assertEqual(info["version"], "1.0.1"); self.assertTrue(info["pending_rebuild"])
        self.assertTrue((self.port / "update/runtime/fix.c").is_file())
        with self.assertRaises(ValueError):
            pu.apply_update(self.port, out / "test-port-1.0.1.zip", m)   # already up to date

    def test_refuses_game_data(self):
        (self.src / "runtime" / "game.elf").write_bytes(b"\x7fELF" + b"\0" * 60)
        with self.assertRaises(ValueError):
            pu.build_update("test-port", "1.0.1", self.src, self.tmp / "o")

    def test_refuses_other_folders_and_bad_hash(self):
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("EBOOT/x.txt", "x")
        import hashlib
        blob = bad.read_bytes()
        m = {"schema": 1, "kind": pu.MANIFEST_KIND, "port_id": "test-port", "version": "1.0.1", "url": "https://x/y.zip",
             "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
        with self.assertRaises(ValueError):
            pu.validate_package(bad, m)
        m["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            pu.validate_package(bad, m)

    def test_rollback_on_failure(self):
        out = self.tmp / "out"
        m = pu.build_update("test-port", "1.0.1", self.src, out)
        m2 = dict(m, port_id="other-port")
        with self.assertRaises(ValueError):
            pu.apply_update(self.port, out / "test-port-1.0.1.zip", m2)
        self.assertEqual(json.loads((self.port / "port.json").read_text())["version"], "1.0.0")

    def test_support_bundle(self):
        z = self.tmp / "s.zip"
        log = "\n".join(["[RR-PERF] fps 150.0"] * 5)
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("port.json", json.dumps({"port_id": "test-port", "version": "1.0.0", "title_ids": ["TEST00000"]}))
            f.writestr("system.json", json.dumps({"os": "Windows", "gpus": [{"name": "GPU", "driver": "1"}]}))
            f.writestr("native-play.log", log)
            f.writestr("WHAT-HAPPENED.txt", "it froze")
        s = sb.summarise(z, None)
        self.assertEqual(s["port_id"], "test-port"); self.assertEqual(s["player_notes"], "it froze")
        self.assertIn("native-play.log", s["logs"])


if __name__ == "__main__":
    unittest.main()
