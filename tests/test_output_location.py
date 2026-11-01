# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio.output_location import validate_owned_path
from studio import game_fixes


class OutputLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "workspace"
        self.home.mkdir()
        self.output = self.base / "PS3 OUTPUT"
        (self.output / "Projects").mkdir(parents=True)
        (self.output / "Repairs").mkdir()
        self.studio = SimpleNamespace(home=self.home)
        self.config = dict(schema=1, status="ready", output_root=str(self.output),
                           project_root=str(self.output/"Projects"), repair_root=str(self.output/"Repairs"))

    def tearDown(self):
        self.temp.cleanup()

    def save_config(self):
        (self.home / "output-location.json").write_text(json.dumps(self.config), encoding="utf-8")

    def junction(self, source, target):
        if os.name != "nt":
            self.skipTest("Actual Windows junction regression")
        result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J", str(source), str(target)],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("Test junction creation unavailable: " + result.stderr.strip())
        self.assertTrue(source.is_junction())

    def migrated(self, category="game-fixes"):
        self.save_config()
        target = self.output / ("Repairs" if category == "game-fixes" else "Projects")
        alias = self.home/category
        self.junction(alias, target)
        return alias, target

    def test_regular_existing_and_new_paths_need_no_migration_registration(self):
        target = self.home/"game-fixes/p/job/not-created"
        self.assertEqual(validate_owned_path(self.studio, target), target)
        target.parent.mkdir(parents=True)
        self.assertEqual(validate_owned_path(self.studio, target), target)

    def test_exact_registered_repair_junction_is_allowed(self):
        alias, target = self.migrated()
        path = alias/"p/job/workspace/a"
        self.assertEqual(validate_owned_path(self.studio, path), target/"p/job/workspace/a")
        game_fixes._owned_no_link_ancestors(self.studio, path)

    def test_exact_registered_project_junction_is_allowed(self):
        alias, target = self.migrated("projects")
        self.assertEqual(validate_owned_path(self.studio, alias/"p/attempts/a"), target/"p/attempts/a")

    def test_unregistered_root_junction_is_rejected(self):
        alias = self.home/"game-fixes"
        self.junction(alias, self.output/"Repairs")
        with self.assertRaisesRegex(ValueError, "Unregistered"):
            validate_owned_path(self.studio, alias/"p")
        with self.assertRaises(game_fixes.GameFixError):
            game_fixes._owned_no_link_ancestors(self.studio, alias/"p")

    def test_incomplete_migration_does_not_permit_junction(self):
        alias, _ = self.migrated()
        self.config["status"] = "pending"
        self.save_config()
        with self.assertRaises(ValueError):
            validate_owned_path(self.studio, alias/"p")

    def test_root_must_point_to_exact_configured_destination(self):
        self.save_config()
        wrong = self.base/"wrong"
        wrong.mkdir()
        alias = self.home/"game-fixes"
        self.junction(alias, wrong)
        with self.assertRaises(ValueError):
            validate_owned_path(self.studio, alias/"p")

    def test_nested_junction_is_not_hidden_by_root_registration(self):
        alias, target = self.migrated()
        target.joinpath("p/job/workspace").mkdir(parents=True)
        other = target/"other"
        other.mkdir()
        nested = alias/"p/job/workspace/unregistered"
        self.junction(nested, other)
        with self.assertRaisesRegex(ValueError, "Unregistered"):
            validate_owned_path(self.studio, nested/"source.c")

    def test_registered_root_must_be_junction_not_another_link_type(self):
        alias, _ = self.migrated()
        with patch("studio.output_location._is_junction", return_value=False):
            with self.assertRaises(ValueError):
                validate_owned_path(self.studio, alias/"p")

    def test_linked_destination_ancestry_is_rejected(self):
        real = self.base/"real-output"
        (real/"Projects").mkdir(parents=True)
        (real/"Repairs").mkdir()
        alternate = self.base/"output-alias"
        self.junction(alternate, real)
        self.config.update(output_root=str(alternate), project_root=str(alternate/"Projects"), repair_root=str(alternate/"Repairs"))
        self.save_config()
        alias = self.home/"game-fixes"
        self.junction(alias, real/"Repairs")
        with self.assertRaises(ValueError):
            validate_owned_path(self.studio, alias/"p")

    def test_malformed_config_cannot_allow_root_junction(self):
        alias, _ = self.migrated()
        for data in ([], {"schema":1,"status":"ready"}, {**self.config,"repair_root":str(self.base/"other")}):
            (self.home/"output-location.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_owned_path(self.studio, alias/"p")

    def test_actual_repaired_candidate_validation_survives_registered_root_migration(self):
        alias, target = self.migrated()
        job = alias/"p/j"
        attempt = job/"workspace/a"
        exe = attempt/"build/player/MyGameRecomp.exe"
        exe.parent.mkdir(parents=True)
        image = bytearray(256)
        image[:2] = b"MZ"
        image[60:64] = (128).to_bytes(4, "little")
        image[128:132] = b"PE\0\0"
        exe.write_bytes(image)
        digest = "a"*64
        exe_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
        def write(path, data):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        write(attempt/"build-report.json", dict(status="compiled; gameplay unverified",input_sha256=digest))
        write(attempt/"private-native-adapter.json", dict(applied=True,manifest_sha256=game_fixes.adapter.MANIFEST_SHA256,input_sha256=digest))
        receipt = self.home/"verification/game-fixes/p/j.json"
        write(receipt, dict(input_sha256=digest,executable_sha256=exe_hash,executable=str(exe),
                            diagnostic_env={"SPU_INTERP_UNLIFTED":"1","SPU_INTERP_BUDGET":"1000000"}))
        result = job/"result.json"
        write(result, dict(status="ready",project_id="p",job_id="j",input_sha256=digest,
                           candidate=dict(attempt=str(attempt),native_exe=str(exe),executable_sha256=exe_hash,native_runtime_receipt=str(receipt))))
        data, evidence = game_fixes._validated(self.studio, {"result_path":str(result)})
        self.assertEqual(data["project_id"], "p")
        self.assertEqual(evidence["executable_sha256"], exe_hash)
        self.assertEqual(exe.resolve(), target/"p/j/workspace/a/build/player/MyGameRecomp.exe")


if __name__ == "__main__":
    unittest.main()
