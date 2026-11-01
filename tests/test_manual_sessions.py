# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import copy
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from studio.core import Studio, sha256, write_json
from tests.test_core import make_elf


class ManualSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root_patch = patch("studio.core.ROOT", self.base / "studio")
        self.root_patch.start()
        self.studio = Studio(self.base / "workspace")
        game = self.base / "game"
        game.mkdir()
        self.elf = make_elf(game / "EBOOT.ELF")
        self.project = self.studio.add(game)
        self.old = self.studio.project_dir(self.project) / "attempts/finished"
        self.exe = self.old / "build/player" / ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp")
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"finished candidate")
        write_json(self.old / "build-report.json", {"status": "compiled; gameplay unverified", "input_sha256": sha256(self.elf)})
        self.active = self.studio.project_dir(self.project) / "attempts/building"
        self.project.update(status="building", attempt=str(self.active), native_exe="", native_candidate_history=[
            {"native_exe": str(self.exe), "attempt": str(self.old)}])
        self.studio.save()

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def test_finished_candidate_launch_during_build_preserves_queue(self):
        before = copy.deepcopy(self.project)
        disk = self.studio.state_path.read_bytes()
        candidates = self.studio.list_test_candidates(self.project)
        self.assertEqual(len(candidates), 1)
        with patch("studio.core.subprocess.Popen", return_value=Mock(pid=100)), patch("studio.core.threading.Thread"):
            self.studio.launch(self.project, candidate_id=candidates[0]["id"])
        session = self.studio.list_test_sessions(self.project["id"])[0]
        self.assertEqual(session["command"], [str(self.exe), str(self.elf)])
        self.assertEqual(session["cwd"], str(self.exe.parent))
        self.assertTrue(Path(session["log_path"]).is_file())
        self.assertEqual(self.project, before)
        self.assertEqual(self.studio.state_path.read_bytes(), disk)

    def test_output_currently_being_linked_and_changed_input_not_offered(self):
        linking = self.active / "build/player" / self.exe.name
        linking.parent.mkdir(parents=True)
        linking.write_bytes(b"not finished")
        write_json(self.active / "build-report.json", {"status": "building", "input_sha256": sha256(self.elf)})
        self.project["native_exe"] = str(linking)
        self.assertEqual([row["exe"] for row in self.studio.list_test_candidates(self.project)], [str(self.exe)])
        self.elf.write_bytes(b"changed input")
        self.assertEqual(self.studio.list_test_candidates(self.project), [])
        with patch("studio.core.subprocess.Popen") as popen:
            with self.assertRaises(RuntimeError):
                self.studio.launch(self.project)
            popen.assert_not_called()

    def test_sessions_finish_independently_and_stop_only_owned_selected_session(self):
        callbacks = []
        def thread_factory(**kwargs):
            callbacks.append(kwargs["target"])
            return Mock()
        first, second = Mock(pid=100), Mock(pid=200)
        first.poll.return_value = second.poll.return_value = None
        first.wait.return_value = second.wait.return_value = 0
        before = copy.deepcopy(self.project)
        with patch("studio.core.subprocess.Popen", side_effect=[first, second]), patch("studio.core.threading.Thread", side_effect=thread_factory):
            self.studio.launch(self.project)
            self.studio.launch(self.project)
        rows = self.studio.list_test_sessions()
        self.assertEqual(len(rows), 2)
        with patch("studio.test_sessions.stop_process_tree", return_value=True) as stop:
            self.assertTrue(self.studio.stop_test_session(rows[0]["session_id"]))
            stop.assert_called_once_with(first)
            with self.assertRaises(ValueError):
                self.studio.stop_test_session("a-build-or-unowned-process")
        callbacks[0]()
        current = self.studio.list_test_sessions()
        self.assertEqual(current[0]["status"], "stopped")
        self.assertEqual(current[1]["status"], "running")
        latest = json.loads((self.studio.project_dir(self.project) / "native-test.json").read_text())
        self.assertEqual(latest["pid"], 200)
        callbacks[1]()
        self.assertEqual(self.studio.list_test_sessions()[1]["status"], "exited normally")
        self.assertEqual(self.project, before)
        for row in rows:
            self.assertTrue((self.studio.project_dir(self.project) / "test-sessions" / (row["session_id"] + ".json")).is_file())

    def test_missing_or_unknown_completion_status_is_never_offered(self):
        for status in (None, "", "unknown", "building", "sources generated", "blocked"):
            with self.subTest(status=status):
                report = {"input_sha256": sha256(self.elf)}
                if status is not None:
                    report["status"] = status
                write_json(self.old / "build-report.json", report)
                self.assertEqual(self.studio.list_test_candidates(self.project), [])

    def test_ordinary_native_sessions_reuse_project_data_and_preserve_source(self):
        source_before = self.elf.read_bytes()
        source_names = sorted(p.name for p in self.elf.parent.iterdir())
        with patch.dict(os.environ, {"PS3_HDD0_ROOT": "", "PS3_HDD1_ROOT": ""}), patch("studio.core.subprocess.Popen", return_value=Mock(pid=100)) as popen, patch("studio.core.threading.Thread"):
            self.studio.launch(self.project)
            first = popen.call_args.kwargs["env"]
            hdd0, hdd1 = Path(first["PS3_HDD0_ROOT"]), Path(first["PS3_HDD1_ROOT"])
            self.assertEqual(hdd0, (self.studio.project_dir(self.project) / "native-data/hdd0").resolve())
            self.assertEqual(hdd1, (self.studio.project_dir(self.project) / "native-data/hdd1").resolve())
            (hdd0 / "save.bin").write_bytes(b"keep save")
            (hdd1 / "cache.bin").write_bytes(b"keep cache")
            self.studio.launch(self.project)
            second = popen.call_args.kwargs["env"]
            self.assertEqual(first["PS3_HDD0_ROOT"], second["PS3_HDD0_ROOT"])
            self.assertEqual(first["PS3_HDD1_ROOT"], second["PS3_HDD1_ROOT"])
        self.assertEqual((hdd0 / "save.bin").read_bytes(), b"keep save")
        self.assertEqual((hdd1 / "cache.bin").read_bytes(), b"keep cache")
        self.assertEqual(self.studio.list_test_sessions()[0]["data_directories"], {"hdd0": str(hdd0), "hdd1": str(hdd1)})
        self.assertEqual(self.elf.read_bytes(), source_before)
        self.assertEqual(sorted(p.name for p in self.elf.parent.iterdir()), source_names)

    def test_explicit_existing_data_roots_are_preserved(self):
        hdd0, hdd1 = self.base / "existing-hdd0", self.base / "existing-hdd1"
        hdd0.mkdir()
        hdd1.mkdir()
        saved = hdd0 / "save.bin"
        saved.write_bytes(b"existing data")
        with patch.dict(os.environ, {"PS3_HDD0_ROOT": str(hdd0), "PS3_HDD1_ROOT": str(hdd1)}), patch("studio.core.subprocess.Popen", return_value=Mock(pid=100)) as popen, patch("studio.core.threading.Thread"):
            self.studio.launch(self.project)
            self.assertEqual(popen.call_args.kwargs["env"]["PS3_HDD0_ROOT"], str(hdd0))
            self.assertEqual(popen.call_args.kwargs["env"]["PS3_HDD1_ROOT"], str(hdd1))
        self.assertEqual(saved.read_bytes(), b"existing data")
        self.assertFalse((self.studio.project_dir(self.project) / "native-data").exists())

    def test_verified_fix_sidecar_is_listed_without_state_registration(self):
        candidate = {"native_exe": str(self.exe), "attempt": str(self.old),
                     "input_sha256": sha256(self.elf), "executable_sha256": sha256(self.exe),
                     "label": "Applied verified fixes; gameplay unverified"}
        before = copy.deepcopy(self.project)
        self.project["native_candidate_history"] = []
        baseline = copy.deepcopy(self.project)
        fake = types.ModuleType("studio.game_fixes")
        fake.list_ready_candidates = Mock(return_value=[candidate])
        with patch.dict("sys.modules", {"studio.game_fixes": fake}):
            rows = self.studio.list_test_candidates(self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], candidate["label"])
        self.assertEqual(self.project, baseline)
        self.assertEqual(json.loads(self.studio.state_path.read_text())["projects"][0], before)

    def test_preserved_ordinary_candidate_does_not_inherit_active_receipt(self):
        self.project["native_runtime_receipt"] = str(self.base / "not-the-old-candidate.json")
        rows = self.studio.list_test_candidates(self.project)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["project_snapshot"]["native_runtime_receipt"])

    def test_companion_snapshot_does_not_recover_or_save_active_queue(self):
        before = self.studio.state_path.read_bytes()
        companion = Studio.read_only_snapshot(self.studio.home)
        self.assertEqual(companion.state["projects"][0]["status"], "building")
        self.assertEqual(companion.list_test_sessions(), [])
        with self.assertRaises(ValueError):
            companion.stop_test_session("not-owned-here")
        with self.assertRaises(RuntimeError):
            companion.save()
        self.assertEqual(self.studio.state_path.read_bytes(), before)
        self.assertIs(companion.state_lock, companion.state_lock)


if __name__ == "__main__":
    unittest.main()
