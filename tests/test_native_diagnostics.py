# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from studio.core import Studio, sha256, write_json


class NativeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "studio"
        self.root.mkdir()
        self.root_patch = patch("studio.core.ROOT", self.root)
        self.root_patch.start()
        self.studio = Studio(self.base / "workspace")
        self.game = self.base / "game"
        self.game.mkdir()
        self.elf = self.game / "EBOOT.ELF"
        self.elf.write_bytes(b"fixture ELF")
        self.exe = self.base / "player.exe"
        self.exe.write_bytes(b"fixture EXE")
        self.receipt = self.root / "verification/candidate.json"
        self.project = {"id": "fixture", "source": str(self.game), "elf": str(self.elf),
                        "native_exe": str(self.exe), "attempt": str(self.base / "attempt"),
                        "native_runtime_receipt": str(self.receipt)}
        self.evidence = {"input_sha256": sha256(self.elf), "executable_sha256": sha256(self.exe),
                         "executable": str(self.exe), "diagnostic_env": {
                             "SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": "1000000"}}
        self.save_receipt()
        write_json(self.base / "attempt/build-report.json", {"status": "compiled; gameplay unverified", "input_sha256": sha256(self.elf)})
        self.studio.project_dir(self.project).mkdir(parents=True)

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def save_receipt(self):
        write_json(self.receipt, self.evidence)

    def check(self):
        return self.studio.native_diagnostics(self.project, self.exe, sha256(self.elf))

    def test_hash_bound_launch_and_receipt_without_global_mutation(self):
        with patch("studio.core.subprocess.Popen", return_value=Mock(pid=123)) as popen, patch("studio.core.threading.Thread"), patch.dict(os.environ, {"SPU_INTERP_BUDGET": "9999999"}):
            self.studio.launch(self.project)
            self.assertEqual(popen.call_args.kwargs["env"]["SPU_INTERP_BUDGET"], "1000000")
            self.assertEqual(os.environ["SPU_INTERP_BUDGET"], "9999999")
        receipt = json.loads((self.studio.project_dir(self.project) / "native-test.json").read_text())
        self.assertEqual(receipt["runtime_diagnostics"]["mode"], "hybrid-SPU")
        self.assertEqual(receipt["runtime_diagnostics"]["receipt_sha256"], sha256(self.receipt))
        self.assertEqual(receipt["playability"], "unverified")

    def test_other_games_do_not_inherit_hybrid_flags(self):
        del self.project["native_runtime_receipt"]
        with patch("studio.core.subprocess.Popen", return_value=Mock(pid=123)) as popen, patch("studio.core.threading.Thread"), patch.dict(os.environ, {"SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": "20"}):
            self.studio.launch(self.project)
            child = popen.call_args.kwargs["env"]
            self.assertNotIn("SPU_INTERP_UNLIFTED", child)
            self.assertNotIn("SPU_INTERP_BUDGET", child)
            self.assertTrue((self.studio.project_dir(self.project) / "native-data/hdd0").is_dir())

    def test_candidate_data_dirs_reused_and_source_untouched(self):
        source_before = {str(p.relative_to(self.game)): p.read_bytes()
                         for p in self.game.rglob("*") if p.is_file()}
        with patch("studio.core.subprocess.Popen", return_value=Mock(pid=123)) as popen, patch("studio.core.threading.Thread"):
            self.studio.launch(self.project)
            first = popen.call_args.kwargs["env"]
            hdd0 = Path(first["PS3_HDD0_ROOT"])
            hdd1 = Path(first["PS3_HDD1_ROOT"])
            self.assertEqual(hdd0, (self.studio.project_dir(self.project) / "native-data/hdd0").resolve())
            self.assertTrue(hdd1.is_dir())
            saved = hdd0 / "existing-save.bin"
            saved.write_bytes(b"preserve existing save")
            cached = hdd1 / "existing-cache.bin"
            cached.write_bytes(b"preserve existing cache")
            self.studio.launch(self.project)
            second = popen.call_args.kwargs["env"]
            self.assertEqual(first["PS3_HDD0_ROOT"], second["PS3_HDD0_ROOT"])
            self.assertEqual(first["PS3_HDD1_ROOT"], second["PS3_HDD1_ROOT"])
            self.assertEqual(saved.read_bytes(), b"preserve existing save")
            self.assertEqual(cached.read_bytes(), b"preserve existing cache")
        receipt = json.loads((self.studio.project_dir(self.project) / "native-test.json").read_text())
        self.assertEqual(receipt["runtime_diagnostics"]["data_directories"], {"hdd0": str(hdd0), "hdd1": str(hdd1)})
        source_after = {str(p.relative_to(self.game)): p.read_bytes()
                        for p in self.game.rglob("*") if p.is_file()}
        self.assertEqual(source_before, source_after)
        self.assertEqual(sorted(p.name for p in self.game.iterdir()), ["EBOOT.ELF"])

    def test_each_native_and_reference_launch_has_unique_log(self):
        base = self.studio.project_dir(self.project)
        self.studio.state["settings"]["rpcs3"] = str(self.exe)
        for native, legacy in ((True, "native-play.log"), (False, "rpcs3-launch.log")):
            with self.subTest(native=native):
                old = base / legacy
                old.write_bytes(b"old running process output")
                paths = []
                with patch("studio.core.subprocess.Popen", return_value=Mock(pid=123)), patch("studio.core.threading.Thread"):
                    for _ in range(2):
                        self.studio.launch(self.project, native)
                        paths.append(self.studio.test_log_path(self.project, native))
                self.assertNotEqual(paths[0], paths[1])
                self.assertTrue(all(p.is_file() for p in paths))
                self.assertEqual(old.read_bytes(), b"old running process output")

    def test_failed_launch_closes_unique_log_and_preserves_previous_receipt(self):
        report = self.studio.project_dir(self.project) / "native-test.json"
        write_json(report, {"pid": 456, "status": "running"})
        with patch("studio.core.subprocess.Popen", side_effect=OSError("fixture launch failure")) as popen:
            with self.assertRaisesRegex(OSError, "fixture launch failure"):
                self.studio.launch(self.project)
            self.assertTrue(popen.call_args.kwargs["stdout"].closed)
        self.assertEqual(json.loads(report.read_text())["pid"], 456)

    def test_old_process_exit_cannot_replace_newer_launch_receipt(self):
        callbacks = []
        def thread_factory(*args, **kwargs):
            callbacks.append(kwargs["target"])
            return Mock()
        first, second = Mock(pid=123), Mock(pid=456)
        first.wait.return_value = 1
        with patch("studio.core.subprocess.Popen", side_effect=[first, second]), patch("studio.core.threading.Thread", side_effect=thread_factory):
            self.studio.launch(self.project)
            self.studio.launch(self.project)
        callbacks[0]()
        report = json.loads((self.studio.project_dir(self.project) / "native-test.json").read_text())
        self.assertEqual(report["pid"], 456)
        self.assertEqual(report["status"], "running")

    def test_receipt_log_path_cannot_escape_project(self):
        with self.assertRaises(ValueError):
            self.studio.test_log_path(self.project, result={"log_path": str(self.base / "outside.log")})
        with self.assertRaises(ValueError):
            self.studio.test_log_path(self.project, result={"log_path": str(self.studio.project_dir(self.project) / "asset.bin")})

    def test_changed_input_or_executable_refused(self):
        for field in ("input_sha256", "executable_sha256"):
            with self.subTest(field=field):
                old = self.evidence[field]
                self.evidence[field] = "0" * 64
                self.save_receipt()
                with self.assertRaisesRegex(RuntimeError, "hash"):
                    self.check()
                self.evidence[field] = old

    def test_path_binding_and_outside_receipt_refused(self):
        self.evidence["executable"] = str(self.base / "other.exe")
        self.save_receipt()
        with self.assertRaisesRegex(RuntimeError, "executable path"):
            self.check()
        self.project["native_runtime_receipt"] = str(self.base / "outside.json")
        with self.assertRaisesRegex(RuntimeError, "inside Studio verification"):
            self.check()

    def test_arbitrary_keys_and_invalid_budget_refused(self):
        for changes in ({"PATH": "untrusted"}, {"SPU_INTERP_BUDGET": 0}, {"SPU_INTERP_BUDGET": "1000001"},
                        {"SPU_INTERP_BUDGET": True}, {"SPU_INTERP_BUDGET": "1;command"},
                        {"SPU_INTERP_UNLIFTED": "0"}):
            with self.subTest(changes=changes):
                self.evidence["diagnostic_env"] = {"SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": "1000000", **changes}
                self.save_receipt()
                with self.assertRaises(RuntimeError):
                    self.check()

    def test_old_controller_evidence_is_compatible(self):
        self.evidence = {"controller": "sdl", "gameplay": "unverified"}
        self.save_receipt()
        self.assertIsNone(self.check())

    def test_workspace_verification_is_supported(self):
        self.receipt = self.studio.home / "verification/candidate.json"
        self.save_receipt()
        self.project["native_runtime_receipt"] = str(self.receipt)
        self.assertEqual(self.check()["diagnostic_env"]["SPU_INTERP_UNLIFTED"], "1")


if __name__ == "__main__":
    unittest.main()
