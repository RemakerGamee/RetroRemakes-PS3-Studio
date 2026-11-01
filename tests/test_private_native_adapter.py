# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import os
"""Private adapter regression checks; original-owner fixture is never distributed."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from studio import private_native_adapter as adapter

ROOT = Path(__file__).resolve().parents[1]
ELF = Path(os.environ.get('RR_TEST_MOTORSTORM_ELF','__missing__'))  # set to your own MotorStorm: Pacific Rift EBOOT.elf
BACKEND = ROOT / "workspace/tools/ps3recomp"
PORT_TEMPLATE = ROOT / "workspace/projects/300bd4d8e919/attempts/20261025-173320-f59fa6/port/CMakeLists.txt"

def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

class UnmatchedAdapterTests(unittest.TestCase):
    def test_unmatched_input_is_strict_noop_without_package_backend_or_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); elf=root/"other.elf";elf.write_bytes(b"unrelated game")
            with patch.object(adapter,"PACKAGE_ROOT",root/"missing"):
                self.assertIsNone(adapter.apply(root/"port",elf,root/"backend",{"id":"other"}))
            self.assertEqual(list(root.iterdir()),[elf])

    def test_unsafe_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for value in ["../outside", "C:/outside", "spu/../../outside", "spu\\outside"]:
                with self.assertRaises(adapter.PrivateNativeAdapterError):
                    adapter._under(Path(temp),value)

@unittest.skipUnless(ELF.is_file() and PORT_TEMPLATE.is_file() and adapter.PACKAGE_ROOT.is_dir(),
                     "private exact owner ELF/package not present")
class ExactPrivateAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.stage=self.root/"port";self.stage.mkdir()
        # Copy a known actual generator contract, excluding earlier private edits.
        cmake=PORT_TEMPLATE.read_text();cmake="\n".join(s for s in cmake.splitlines() if "motorstorm" not in s.lower())+"\n"
        (self.stage/"CMakeLists.txt").write_text(cmake)
        self.backend=self.root/"backend"
        self.manifest=json.loads((adapter.PACKAGE_ROOT/"manifest.json").read_text())
        for rel in self.manifest["backend_contract"]:
            dst=self.backend/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(BACKEND/rel,dst)
    def tearDown(self):self.tmp.cleanup()
    def apply(self):return adapter.apply(self.stage,ELF,self.backend,{"id":"fixture"})
    def assert_unchanged_failure(self,match):
        before=snapshot(self.stage)
        with self.assertRaisesRegex(adapter.PrivateNativeAdapterError,match):self.apply()
        self.assertEqual(snapshot(self.stage),before)

    def test_actual_input_applies_all_hashes_and_repeat_is_idempotent(self):
        receipt=self.apply();self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["input_sha256"],adapter.INPUT_SHA256)
        self.assertEqual(receipt["raw_fingerprint"],"093FC7EE6C9F7E84")
        self.assertEqual(receipt["mode"],"hybrid-SPU");self.assertEqual(receipt["playability"],"unverified")
        self.assertNotIn("executable",receipt)
        for rel,expected in receipt["installed_sources"].items():
            self.assertEqual(hashlib.sha256((self.stage/rel).read_bytes()).hexdigest(),expected)
        self.assertTrue((self.stage/"private-native-adapter/MIT-LICENSE.txt").is_file())
        self.assertFalse(list(self.stage.rglob("*multistream_register*")))
        self.assertEqual((self.stage/"CMakeLists.txt").read_text().count(adapter.CMAKE_LINE),1)
        before=snapshot(self.stage);again=self.apply();self.assertEqual(before,snapshot(self.stage));self.assertEqual(receipt,again)

    def test_backend_contract_mismatch_preserves_stage(self):
        rel=next(iter(self.manifest["backend_contract"]));(self.backend/rel).write_bytes(b"changed")
        self.assert_unchanged_failure("runtime contract.*hash mismatch")

    def test_corrupt_payload_preserves_stage(self):
        package=self.root/"package";shutil.copytree(adapter.PACKAGE_ROOT,package)
        (package/"payload/spu/motorstorm_wws_policy/spu_recomp.c").write_bytes(b"changed")
        with patch.object(adapter,"PACKAGE_ROOT",package):self.assert_unchanged_failure("private source.*hash mismatch")

    def test_missing_private_package_preserves_stage(self):
        with patch.object(adapter,"PACKAGE_ROOT",self.root/"missing"):
            self.assert_unchanged_failure("missing manifest")

    def test_changed_destination_is_preserved_without_partial_copy(self):
        dest=self.stage/"spu/motorstorm_policy_register.c";dest.parent.mkdir();dest.write_bytes(b"owner fix")
        self.assert_unchanged_failure("preserve changed destination")

    def test_cmake_contract_and_stale_fixed_registry_refuse_prewrite(self):
        p=self.stage/"CMakeLists.txt";original=p.read_bytes();p.write_text("project(other)\n")
        self.assert_unchanged_failure("CMake contract mismatch")
        p.write_bytes(original+b'\ntarget_sources(${PROJECT_NAME} PRIVATE spu/motorstorm_multistream_register.c)\n')
        self.assert_unchanged_failure("stale fixed-address")

    def test_raw_span_mismatch_refuses_prewrite(self):
        with patch.object(adapter,"RAW_SHA256","0"*64):self.assert_unchanged_failure("raw policy hash/fingerprint mismatch")

    def test_manifest_tampering_refuses_prewrite(self):
        package=self.root/"package";shutil.copytree(adapter.PACKAGE_ROOT,package)
        p=package/"manifest.json";p.write_bytes(p.read_bytes()+b" ")
        with patch.object(adapter,"PACKAGE_ROOT",package):self.assert_unchanged_failure("manifest hash mismatch")

if __name__=="__main__":unittest.main()
