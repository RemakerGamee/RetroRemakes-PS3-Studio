# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json, os, tempfile, threading, time, unittest
from pathlib import Path
from studio import build_cleanup as bc


class FakeStudio:
    def __init__(self, home):
        self.home = Path(home); self.state = {"projects": [], "settings": {}}; self.saved = 0
        self.state_lock = threading.RLock(); self.sessions = []
    def project_dir(self, p): return self.home / "projects" / p["id"]
    def list_test_sessions(self, pid=None): return self.sessions
    def save(self): self.saved += 1


def build(folder, age):
    exe = Path(folder) / "build/player/MyGameRecomp.exe"; exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ"); t = time.time() - age; os.utime(exe, (t, t)); return exe


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = FakeStudio(self.tmp.name)
        self.p = {"id": "game1", "name": "Game", "status": "compiled; untested"}
        self.s.state["projects"].append(self.p)
        root = self.s.project_dir(self.p)
        self.source = root / "attempts/source"; build(self.source, 5000)
        self.p["attempt"] = str(self.source)
        self.old_a = root / "attempts/performance-04"; build(self.old_a, 9000)
        (root / "native-data/save.bin").parent.mkdir(parents=True); (root / "native-data/save.bin").write_bytes(b"s")
        fixes = self.s.home / "game-fixes/game1"
        self.old_job = fixes / "job_old"; self.old_exe = build(self.old_job / "workspace/a", 3000)
        (self.old_job / "result.json").write_text(json.dumps({"source_attempt": str(self.source)}))
        self.new_job = fixes / "job_new"; self.new_exe = build(self.new_job / "workspace/a", 10)
        (self.new_job / "result.json").write_text(json.dumps({"source_attempt": str(self.source)}))
        side = self.s.home / "native-repairs/game1"; side.mkdir(parents=True)
        (side / "perf04.json").write_text(json.dumps({"attempt": str(self.old_a)}))
        self.p["native_candidate_history"] = [{"native_exe": str(self.old_exe)}]
        self.cands = [{"exe": str(e), "attempt": str(a)} for e, a in (
            (self.source / "build/player/MyGameRecomp.exe", self.source),
            (self.old_a / "build/player/MyGameRecomp.exe", self.old_a),
            (self.old_exe, self.old_job / "workspace/a"), (self.new_exe, self.new_job / "workspace/a"))]

    def tearDown(self): self.tmp.cleanup()

    def test_keeps_newest_source_and_saves_only(self):
        r = bc.prune_project(self.s, self.p, candidates=self.cands)
        self.assertTrue(self.new_exe.is_file())
        self.assertTrue(self.source.is_dir())                    # future fixes build from it
        self.assertTrue((self.s.project_dir(self.p) / "native-data/save.bin").is_file())
        self.assertFalse(self.old_job.exists()); self.assertFalse(self.old_a.exists())
        self.assertFalse((self.s.home / "native-repairs/game1/perf04.json").exists())
        self.assertEqual(self.p["native_candidate_history"], [])
        self.assertEqual(len(r["removed"]), 3); self.assertEqual(r["failed"], [])
        self.assertTrue((self.s.home / "cleanup.log").is_file())

    def test_skips_while_testing_or_building(self):
        self.s.sessions = [{"status": "running"}]
        r = bc.prune_project(self.s, self.p, candidates=self.cands)
        self.assertEqual(r["skipped"], "a test is running"); self.assertTrue(self.old_job.exists())
        self.s.sessions = []; self.p["status"] = "building"
        self.assertEqual(bc.prune_project(self.s, self.p, candidates=self.cands)["skipped"], "a build is running")

    def test_dry_run_changes_nothing(self):
        r = bc.prune_project(self.s, self.p, candidates=self.cands, dry_run=True)
        self.assertEqual(len(r["removed"]), 3); self.assertTrue(self.old_job.exists() and self.old_a.exists())

    def test_single_build_untouched(self):
        r = bc.prune_project(self.s, self.p, candidates=self.cands[3:])
        self.assertTrue(self.new_exe.is_file()); self.assertTrue(self.source.is_dir())


if __name__ == "__main__":
    unittest.main()
