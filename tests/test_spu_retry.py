# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from studio import core


class SpuRetryTests(unittest.TestCase):
    def test_registry_complete_and_legacy_adapter_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            spu=Path(temp); (spu/"spu_workloads.c").write_text("reviewed adapter")
            self.assertIsNone(core.spu_retry_reason(spu, {"status":"compiled; gameplay unverified"}))
            core.write_json(spu/"inventory.json", {"complete":True})
            self.assertIsNone(core.spu_retry_reason(spu, {"commands":[{"stage":"spu-discover-lift","status":"passed"}]}))

    def test_missing_failed_incomplete_and_malformed_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            spu=Path(temp)
            self.assertIsNotNone(core.spu_retry_reason(spu, {}))
            (spu/"spu_workloads.c").write_text("partial")
            for prior in [{"commands":[{"stage":"spu-discover-lift","status":"failed"}]}, {"error":"spu-discover-lift failed (exit 1)"}]:
                self.assertIsNotNone(core.spu_retry_reason(spu, prior))
            for value in [{"complete":False}, [], {"complete":"true"}]:
                core.write_json(spu/"inventory.json", value)
                self.assertIsNotNone(core.spu_retry_reason(spu, {}))
            (spu/"inventory.json").write_text("broken json")
            self.assertIsNotNone(core.spu_retry_reason(spu, {}))

    def test_reuse_runs_only_failed_spu_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); attempt=root/"attempt"; spu=attempt/"port/spu"; recomp=attempt/"port/recompiled"; loader=attempt/"loader"
            spu.mkdir(parents=True); recomp.mkdir(); loader.mkdir()
            (recomp/"ppu_recomp.h").write_text("reviewed header")
            (recomp/"chunk.cpp").write_text("reviewed PPU")
            (spu/"reviewed.c").write_text("reviewed SPU")
            core.write_json(spu/"inventory.json", {"complete":False})
            core.write_json(loader/"game.functions.json", [1]); core.write_json(loader/"game.imports.json", [])
            prior={"input_sha256":"original", "status":"blocked", "error":"spu-discover-lift failed", "commands":[{"stage":"ppu-lift","status":"passed"}]}
            core.write_json(attempt/"build-report.json", prior)
            studio=core.Studio.__new__(core.Studio); studio.home=root; studio.state={"settings":{"backend":str(root/"backend")}}
            studio.analyse=lambda *args: {"binary":{"native_input":True},"sha256":"original","input":str(root/"game.elf"),"inventory":{"binaries":[]}}
            studio.validate_backend=lambda *args:None; studio.provenance=lambda *args:{}; studio.backend_python=lambda: "python"
            studio.save=lambda:None; studio.preserve_native_candidate=lambda *args:None
            calls=[]
            def run(runner, stage, argv, **kwargs):
                calls.append(stage)
                (spu/"reviewed.c").write_text("new generated SPU")
                core.write_json(spu/"inventory.json", {"complete":True})
                (spu/"spu_workloads.c").write_text("registry")
            with patch.object(core.Runner,"run",run), patch("studio.private_native_adapter.apply",return_value=None):
                result=studio.build({"attempt":str(attempt),"id":"test"},emit=lambda *args:None,reuse=True,stop_after="lift")
            self.assertEqual(calls,["spu-discover-lift"])
            self.assertEqual((recomp/"chunk.cpp").read_text(),"reviewed PPU")
            preserved=Path(result["spu_retry"]["preserved_sources"])
            self.assertEqual((preserved/"reviewed.c").read_text(),"reviewed SPU")
            self.assertFalse(json.loads((preserved/"inventory.json").read_text())["complete"])
            history=list((attempt/"history").glob("*.json"))
            self.assertEqual(len(history),1)
            self.assertEqual(json.loads(history[0].read_text()),prior)

    def test_cancelled_snapshot_does_not_replace_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); spu=root/"spu"; spu.mkdir(); (spu/"reviewed.c").write_text("keep")
            cancel=threading.Event(); cancel.set()
            with self.assertRaises(core.Cancelled): core.preserve_spu_retry(spu,root/"history",cancel)
            self.assertEqual((spu/"reviewed.c").read_text(),"keep")

if __name__=="__main__": unittest.main()
