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


class DevFlashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "programs/studio/release"
        self.root.mkdir(parents=True)
        self.patch_root = patch("studio.core.ROOT", self.root)
        self.patch_root.start()
        self.studio = Studio(self.base / "workspace")

    def tearDown(self):
        self.patch_root.stop()
        self.temp.cleanup()

    def installed(self, base):
        folder = base / "dev_flash"
        marker = folder / "sys/external/flashMP3.pic"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"fixture")
        return folder.resolve()

    def test_configured_rpcs3_precedes_saved_and_sibling(self):
        preferred = self.installed(self.base / "configured")
        saved = self.installed(self.base / "saved")
        self.installed(self.root.parent / "rpcs3-other")
        self.studio.state["settings"].update(rpcs3=str(preferred.parent / "rpcs3.exe"), dev_flash=str(saved))
        self.assertEqual(self.studio.detect_dev_flash(), str(preferred))

    def test_saved_valid_install_preserved_across_restart(self):
        saved = self.installed(self.base / "saved")
        self.studio.state["settings"]["dev_flash"] = str(saved)
        self.studio.save()
        self.assertEqual(Studio(self.studio.home).state["settings"]["dev_flash"], str(saved))

    def test_bounded_sibling_discovery(self):
        installed = self.installed(self.root.parent.parent / "rpcs3-actual")
        self.assertEqual(self.studio.detect_dev_flash(), str(installed))

    def test_pup_and_empty_directory_do_not_count_as_install(self):
        (self.root.parent / "rpcs3-empty/dev_flash").mkdir(parents=True)
        pup = self.base / "PS3UPDAT.PUP"
        pup.write_bytes(b"SCEUF\0\0\0")
        self.studio.state["settings"].update(firmware=str(pup), firmware_folder=str(self.base))
        self.assertEqual(self.studio.detect_dev_flash(), "")
        self.assertEqual(self.studio.state["settings"]["firmware"], str(pup))

    def test_does_not_recurse_into_other_folders(self):
        self.installed(self.root.parent.parent / "library/nested/rpcs3-hidden")
        self.assertEqual(self.studio.detect_dev_flash(), "")

    def test_native_child_receives_install_without_global_env_mutation(self):
        installed = self.installed(self.base / "configured")
        self.studio.state["settings"]["rpcs3"] = str(installed.parent / "rpcs3.exe")
        game = self.base / "game"
        game.mkdir()
        elf = game / "EBOOT.ELF"
        elf.write_bytes(b"fixture ELF")
        exe = self.base / "player.exe"
        exe.write_bytes(b"fixture EXE")
        attempt = self.base / "attempt"
        write_json(attempt / "build-report.json", {"status": "compiled; gameplay unverified", "input_sha256": sha256(elf)})
        project = {"id": "fixture", "source": str(game), "elf": str(elf), "native_exe": str(exe), "attempt": str(attempt)}
        project_dir = self.studio.project_dir(project)
        project_dir.mkdir(parents=True, exist_ok=True)
        process = Mock(pid=123)
        with patch("studio.core.subprocess.Popen", return_value=process) as popen, patch("studio.core.threading.Thread"), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.studio.launch(project), 123)
            self.assertEqual(popen.call_args.kwargs["env"]["PS3_DEV_FLASH"], str(installed))
            self.assertNotIn("PS3_DEV_FLASH", os.environ)
        receipt = json.loads((project_dir / "native-test.json").read_text())
        self.assertEqual(receipt["dev_flash"], str(installed))
        self.assertEqual(receipt["playability"], "unverified")


if __name__ == "__main__":
    unittest.main()
