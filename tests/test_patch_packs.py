# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path

from studio import patch_packs as pp


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def diff(path, old, new):
    lines = [f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@", f"-{old}", f"+{new}", ""]
    return "\n".join(lines)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class PatchPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.backend = self.tmp / "backend"
        (self.backend / "libs").mkdir(parents=True)
        (self.backend / "libs" / "a.c").write_text("int a = 0;\n")
        git("init", "-q", cwd=self.backend)
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".", cwd=self.backend)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base", cwd=self.backend)
        self.rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.backend, capture_output=True,
                                  text=True).stdout.strip()
        self.studio = types.SimpleNamespace(home=self.home, state={"settings": {"backend": str(self.backend)}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pack(self, patches, **extra):
        folder = self.tmp / "src-pack"
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "patches").mkdir(parents=True)
        for name, text in patches.items():
            (folder / "patches" / name).write_text(text)
        data = {"schema": 1, "id": "kz3-test", "name": "KZ3 test", "version": "1",
                "games": {"title_ids": ["BCUS98234"]},
                "backend": {"revision": self.rev, "patches": ["patches/*.patch"]}}
        data.update(extra)
        (folder / "pack.json").write_text(json.dumps(data))
        return folder

    def test_patches_apply_in_order_to_a_separate_copy(self):
        src = self.make_pack({"0002-second.patch": diff("libs/a.c", "int a = 1;", "int a = 2;"),
                              "0001-first.patch": diff("libs/a.c", "int a = 0;", "int a = 1;")})
        pack = pp.install(self.home, src)
        self.assertEqual([p.name for p in pack["backend_patches"]], ["0001-first.patch", "0002-second.patch"])
        built = pp.prepare_backend(self.studio, pack, emit=lambda m: None)
        self.assertEqual((built / "libs" / "a.c").read_text(), "int a = 2;\n")
        self.assertEqual((self.backend / "libs" / "a.c").read_text(), "int a = 0;\n", "shared backend changed")
        self.assertEqual(pp.prepare_backend(self.studio, pack, emit=lambda m: None), built, "not cached")

    def test_bad_patch_is_named(self):
        pack = pp.install(self.home, self.make_pack({"0007-broken.patch": diff("libs/a.c", "nope", "x")}))
        with self.assertRaises(pp.PackError) as error:
            pp.prepare_backend(self.studio, pack, emit=lambda m: None)
        self.assertIn("0007-broken.patch", str(error.exception))

    def test_cancel(self):
        pack = pp.install(self.home, self.make_pack({"0001.patch": diff("libs/a.c", "int a = 0;", "int a = 1;")}))
        stop = threading.Event(); stop.set()
        with self.assertRaises(pp.Cancelled):
            pp.prepare_backend(self.studio, pack, emit=lambda m: None, cancel=stop)

    def test_matching_and_choice(self):
        pp.install(self.home, self.make_pack({"0001.patch": diff("libs/a.c", "int a = 0;", "int a = 1;")}))
        report = {"sha256": "0" * 64, "metadata": {"TITLE_ID": "BCUS98234"}}
        project = {}
        self.assertEqual(pp.for_project(self.studio, project, report)["id"], "kz3-test")
        self.assertEqual(project["patch_pack"], "kz3-test")
        self.assertIsNone(pp.for_project(self.studio, {}, {"sha256": "1" * 64, "metadata": {"TITLE_ID": "BLES00001"}}))
        off = {"patch_pack_choice": "none"}
        self.assertIsNone(pp.for_project(self.studio, off, report))

    def test_project_files_patches_and_env(self):
        src = self.make_pack({}, backend={}, project={"files": "project", "patches": ["pp/*.patch"]},
                             env={"KZ3_SPURS_THREADS": "8"})
        (src / "project").mkdir(); (src / "project" / "extra.cpp").write_text("// extra\n")
        (src / "pp").mkdir(); (src / "pp" / "1.patch").write_text(diff("main.cpp", "int main();", "int main(void);"))
        pack = pp.install(self.home, src)
        self.assertIsNone(pp.prepare_backend(self.studio, pack, emit=lambda m: None))
        port = self.tmp / "port"; port.mkdir(); (port / "main.cpp").write_text("int main();\n")
        pp.apply_project(pack, port, emit=lambda m: None)
        self.assertEqual((port / "extra.cpp").read_text(), "// extra\n")
        self.assertEqual((port / "main.cpp").read_text(), "int main(void);\n")
        self.assertEqual(pp.launch_env(self.studio, {"patch_pack": "kz3-test"}), {"KZ3_SPURS_THREADS": "8"})

    def test_unsafe_inputs_refused(self):
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("../evil/pack.json", "{}")
        with self.assertRaises(pp.PackError):
            pp.install(self.home, bad)
        with self.assertRaises(pp.PackError):
            pp.load(self.make_pack({"1.patch": "x"}, backend={"patches": ["../../*.patch"]}))
        with self.assertRaises(pp.PackError):
            pp.load(self.make_pack({"1.patch": "x"}, games={"title_ids": ["not-an-id"]}))

    def test_zip_install_and_create_from_folder(self):
        src = self.make_pack({"0001.patch": diff("libs/a.c", "int a = 0;", "int a = 1;")})
        archive = self.tmp / "pack.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for f in src.rglob("*"):
                if f.is_file():
                    z.write(f, "kz3-pack/" + f.relative_to(src).as_posix())
        self.assertEqual(pp.install(self.home, archive)["id"], "kz3-test")
        loose = self.tmp / "loose"; loose.mkdir()
        (loose / "0001-a.patch").write_text(diff("libs/a.c", "int a = 0;", "int a = 5;"))
        (loose / "notes.txt").write_text("ignored")
        made = pp.create_from_folder(self.home, loose, "Killzone 3 Fixes", "BCUS98234, BCES01007", self.rev,
                                     author="someone")
        self.assertEqual(made["id"], "killzone-3-fixes")
        self.assertEqual(made["title_ids"], ["BCUS98234", "BCES01007"])
        self.assertEqual(len(made["backend_patches"]), 1)
        self.assertEqual(len(pp.installed(self.home)[0]), 2)


if __name__ == "__main__":
    unittest.main()
