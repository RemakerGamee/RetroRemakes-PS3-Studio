# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from studio import upstream_ports as up

SHA129 = "e8b0ddefc0cb1dc116fb4ae9675a5f551a26cc7eb7d74b06f00ec294ceea8c3d"
SHA101 = "39a28358efd91fd0e914944392a3807b0141e4c5b6ba22e4964640c9c43b9799"


class SubprocessRunner:
    def __init__(self):
        self.calls = []

    def run(self, label, argv, cwd=None, timeout=None):
        self.calls.append((label, [str(a) for a in argv]))
        r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True)
        out = r.stdout + r.stderr
        if r.returncode:
            raise RuntimeError(f"{label} failed: {out}")
        return out[-12000:]


class Routes(unittest.TestCase):
    def test_route_and_unrouted(self):
        self.assertEqual(up.route_for(SHA129)["id"], "killzone2-project-pyrrhus")
        self.assertIsNone(up.route_for(SHA101))
        self.assertIn("1.29", up.unrouted_note(SHA101))
        self.assertIsNone(up.unrouted_note("0" * 64))

    def test_progress_line(self):
        self.assertEqual(up.progress_from_line("PROGRESS step=3/7 name=ppu-lift done=1 total=4 pct=12.5"),
                         ("ppu-lift", 12.5))
        self.assertIsNone(up.progress_from_line("LOG path=x"))


class UpdateDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.route = up.ROUTES[0]
        self.elf = self.tmp / "eboot" / "EBOOT.elf"
        self.elf.parent.mkdir()
        self.elf.touch()

    def test_project_setting_wins(self):
        d = self.tmp / "u"; d.mkdir()
        self.assertEqual(up.update_dir(self.route, {"update_dir": str(d)}, {}, self.elf), d)
        with self.assertRaises(up.UpstreamPortError):
            up.update_dir(self.route, {"update_dir": str(self.tmp / "missing")}, {}, self.elf)

    def test_studio_setting(self):
        d = self.tmp / "s"; d.mkdir()
        s = {"upstream_update_dirs": {self.route["id"]: str(d)}}
        self.assertEqual(up.update_dir(self.route, {}, s, self.elf), d)

    def test_eboot_folder_then_rpcs3(self):
        rp = self.tmp / "rpcs3"; usr = rp / "dev_hdd0/game/BCUS98116/USRDIR"; usr.mkdir(parents=True)
        (usr / "patch_data_ps3").mkdir()
        s = {"rpcs3": str(rp / "rpcs3.exe")}
        self.assertEqual(up.update_dir(self.route, {}, s, self.elf), usr)
        (self.elf.parent / "added_data_ps3.psarc").touch()
        self.assertEqual(up.update_dir(self.route, {}, s, self.elf), self.elf.parent)

    def test_missing_explains(self):
        with self.assertRaisesRegex(up.UpstreamPortError, "1.29 update"):
            up.update_dir(self.route, {}, {}, self.elf)


class Toolkit(unittest.TestCase):
    def test_synthetic_patch_apply_and_stamp(self):
        src = Path(tempfile.mkdtemp())
        route = dict(up.ROUTES[0], submodule="third_party/kit", submodule_commit="abcdef0123456789")
        kit = src / "third_party/kit"; kit.mkdir(parents=True)
        (kit / "a.txt").write_text("one\n")
        (kit / "gone.txt").write_text("x\n")
        (kit / ".git").write_text("gitdir: elsewhere\n")
        patches = src / "port/patches"; patches.mkdir(parents=True)
        (patches / "0001-a.patch").write_text(
            "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
        runner = SubprocessRunner()
        target = up.build_toolkit(route, src, runner, emit=lambda *_: None)
        self.assertEqual((target / "a.txt").read_text(), "two\n")
        self.assertFalse((target / ".git").exists())
        stamp = (target / ".kz2-patchset").read_text().strip()
        self.assertRegex(stamp, r"^[0-9a-f]{16}-abcdef0$")
        self.assertEqual(stamp, up.patchset_stamp(route, src))
        n = len(runner.calls)
        up.build_toolkit(route, src, runner, emit=lambda *_: None)
        self.assertEqual(len(runner.calls), n)  # current stamp: no re-patch
        (kit / "gone.txt").unlink()
        (patches / "0002-b.patch").write_text(
            "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-two\n+three\n")
        up.build_toolkit(route, src, runner, emit=lambda *_: None)
        self.assertEqual((target / "a.txt").read_text(), "three\n")
        self.assertFalse((target / "gone.txt").exists())

    def test_bad_patch_refused(self):
        src = Path(tempfile.mkdtemp())
        route = dict(up.ROUTES[0], submodule="k")
        (src / "k").mkdir(); (src / "k/a.txt").write_text("one\n")
        (src / "port/patches").mkdir(parents=True)
        (src / "port/patches/1.patch").write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-zzz\n+two\n")
        with self.assertRaises(RuntimeError):
            up.build_toolkit(route, src, SubprocessRunner(), emit=lambda *_: None)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "dump/PS3_GAME").mkdir(parents=True)
        self.upd = self.tmp / "upd"; self.upd.mkdir()
        self.elf = self.tmp / "e.elf"; self.elf.touch()
        self.attempt = self.tmp / "attempt"; self.attempt.mkdir()
        self.studio = SimpleNamespace(home=self.tmp / "home", state={"settings": {"jobs": 99}},
                                      vfs_root_for=lambda p, e: self.tmp / "dump")
        self.p = {"update_dir": str(self.upd)}
        self.report = {"sha256": SHA129, "input": str(self.elf)}
        src = self.tmp / "src"; src.mkdir()
        up_acq, up_kit, up_tc = up.acquire, up.build_toolkit, up.fetch_toolchain
        self.addCleanup(lambda: (setattr(up, "acquire", up_acq), setattr(up, "build_toolkit", up_kit),
                                 setattr(up, "fetch_toolchain", up_tc)))
        up.acquire = lambda *a: src
        up.build_toolkit = lambda *a: None
        up.fetch_toolchain = lambda *a: self.tmp / "tc"

    def fake(self, tail, make_exe=True, fail=False, status=""):
        test = self

        class R:
            argv = None

            def run(self, label, argv, cwd=None, timeout=None):
                R.argv = [str(a) for a in argv]
                if status:
                    (test.attempt / "port-status.txt").write_text(status)
                if make_exe:
                    (test.attempt / "port-build").mkdir(exist_ok=True)
                    (test.attempt / "port-build/kz2.exe").write_bytes(b"MZ")
                if fail:
                    raise RuntimeError("exit 1")
                return tail
        return R()

    def test_success(self):
        r = self.fake("PROGRESS step=7/7 name=stamp pct=100\nRESULT ok=1\n")
        out = up.run(self.studio, self.p, self.report, self.attempt, r, print)
        self.assertTrue(out["native_exe"].endswith("kz2.exe"))
        self.assertEqual(out["upstream_launch_env"]["KZ2_UPDATE_DIR"], str(self.upd))
        self.assertIn("--jobs", r.argv)
        self.assertEqual(r.argv[r.argv.index("--jobs") + 1], "8")

    def test_result_not_ok(self):
        r = self.fake("REASON E5 bad thing\nRESULT ok=0\n")
        with self.assertRaisesRegex(up.UpstreamPortError, "E5 bad thing"):
            up.run(self.studio, self.p, self.report, self.attempt, r, print)

    def test_failure_reasons_from_status(self):
        r = self.fake("", make_exe=False, fail=True, status="REASON E1 wrong EBOOT\n")
        with self.assertRaisesRegex(up.UpstreamPortError, "E1 wrong EBOOT"):
            up.run(self.studio, self.p, self.report, self.attempt, r, print)

    def test_dump_required(self):
        self.studio.vfs_root_for = lambda p, e: self.tmp
        with self.assertRaisesRegex(up.UpstreamPortError, "PS3_GAME"):
            up.run(self.studio, self.p, self.report, self.attempt, self.fake("RESULT ok=1"), print)


if __name__ == "__main__":
    unittest.main()
