# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio.core import Studio, Runner, sha256, write_json
from tests.test_core import make_elf


class NativeCandidateLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root_patch = patch("studio.core.ROOT", self.base / "studio")
        self.root_patch.start()
        self.studio = Studio(self.base / "workspace")
        self.game = self.base / "game"
        self.game.mkdir()
        self.elf = make_elf(self.game / "EBOOT.ELF")
        self.p = self.studio.add(self.game)
        self.old_exe = self.base / "old/player.exe"
        self.old_exe.parent.mkdir()
        self.old_exe.write_bytes(b"old candidate")
        self.receipt = self.studio.home / "verification/old.json"
        write_json(self.receipt, {"input_sha256": sha256(self.elf), "executable": str(self.old_exe),
                   "executable_sha256": sha256(self.old_exe), "diagnostic_env": {"SPU_INTERP_UNLIFTED": "1", "SPU_INTERP_BUDGET": "1000000"}})
        self.p.update(native_exe=str(self.old_exe), attempt=str(self.base / "old-attempt"), native_runtime_receipt=str(self.receipt))

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def fresh_output(self, success=True):
        attempt = self.studio.project_dir(self.p) / "attempts/fresh"
        exe = attempt / "build/player" / ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp")
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"new ordinary candidate")
        write_json(attempt / "build-report.json", {"status": "compiled; gameplay unverified" if success else "blocked", "input_sha256": sha256(self.elf)})
        self.p.update(attempt=str(attempt), native_exe=str(exe), status="compiled; untested")
        return attempt, exe

    def test_reconcile_only_proven_fresh_ordinary_candidate_on_restart(self):
        self.fresh_output()
        self.studio.save()
        reloaded = Studio(self.studio.home)
        project = reloaded.state["projects"][0]
        self.assertNotIn("native_runtime_receipt", project)
        self.assertEqual(project["native_candidate_history"][0]["native_exe"], str(self.old_exe))
        self.assertTrue(self.old_exe.is_file())
        self.assertTrue(self.receipt.is_file())
        self.assertFalse(reloaded.reconcile_native_receipt(project))

    def test_identical_binary_in_fresh_attempt_does_not_inherit_diagnostics(self):
        _, exe = self.fresh_output()
        exe.write_bytes(self.old_exe.read_bytes())
        self.assertTrue(self.studio.reconcile_native_receipt(self.p))
        self.assertNotIn("native_runtime_receipt", self.p)

    def test_unproven_or_failed_build_never_reconciles_receipt(self):
        self.fresh_output(success=False)
        self.assertFalse(self.studio.reconcile_native_receipt(self.p))
        self.assertEqual(self.p["native_runtime_receipt"], str(self.receipt))

    def test_adapter_proof_binds_actual_exe_only_after_success(self):
        attempt, exe = self.fresh_output()
        adapter = {"adapter_id": "fixture", "private": True, "input_sha256": sha256(self.elf)}
        path = self.studio.bind_private_native_candidate(self.p, attempt, exe, sha256(self.elf), adapter)
        result = self.studio.native_diagnostics(self.p, exe, sha256(self.elf))
        self.assertEqual(result["diagnostic_env"]["SPU_INTERP_BUDGET"], "1000000")
        self.assertEqual(json.loads(Path(path).read_text())["executable_sha256"], sha256(exe))
        exe.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "hash"):
            self.studio.native_diagnostics(self.p, exe, sha256(self.elf))

    def test_private_cache_profile_is_hash_bound_and_keeps_shared_cache_untouched(self):
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        attempt, exe = self.fresh_output()
        receipt = json.loads(self.receipt.read_text())
        receipt.update(executable=str(exe), executable_sha256=sha256(exe), cache_profile='isolated')
        write_json(self.receipt, receipt)
        diagnostics = self.studio.native_diagnostics(self.p, exe, sha256(self.elf))
        self.assertEqual(diagnostics['cache_profile'], 'isolated')
        candidate=dict(id='private-cache',label='Private cache test',exe=str(exe),attempt=str(attempt),
                       executable_sha256=sha256(exe),project_snapshot=dict(self.p))
        old_cache=self.base/'active-cache';old_cache.mkdir();(old_cache/'cache.idx').write_bytes(b'keep active cache')
        owner=MagicMock()
        with patch.object(self.studio,'list_test_candidates',return_value=[candidate]), patch.object(self.studio,'detect_dev_flash',return_value=None), patch.object(self.studio,'_manual_session_owner',return_value=owner), patch('studio.core.subprocess.Popen',return_value=SimpleNamespace(pid=123)) as start, patch.dict(os.environ,{'PS3_HDD1_ROOT':str(old_cache)}):
            self.studio.launch(self.p,candidate_id='private-cache')
        env=start.call_args.kwargs['env']
        expected=self.studio.home/'test-cache'/self.p['id']/sha256(exe)[:16]/'hdd1'
        self.assertEqual(Path(env['PS3_HDD1_ROOT']),expected)
        self.assertEqual((old_cache/'cache.idx').read_bytes(),b'keep active cache')
        self.assertTrue(expected.is_dir())
        receipt['cache_profile']='arbitrary-path';write_json(self.receipt,receipt)
        with self.assertRaisesRegex(RuntimeError,'cache profile'):
            self.studio.native_diagnostics(self.p,exe,sha256(self.elf))

    def test_new_ordinary_build_does_not_carry_hybrid_binding(self):
        backend = self.base / "backend"
        (backend / "templates/project/recompiled").mkdir(parents=True)
        (backend / "templates/project/recompiled/ppu_recomp.h").write_text("// fixture")
        (backend / "templates/project/recompiled/ppu_recomp_0.cpp").write_text("// fixture")
        (backend / "templates/project/CMakeLists.txt").write_text("# fixture")
        self.studio.state["settings"]["backend"] = str(backend)
        events = []
        def run(runner, label, argv, **kwargs):
            events.append(label)
            attempt = Path(self.p["attempt"])
            if label == "ppu-load":
                write_json(attempt / "loader/EBOOT.functions.json", [{"start": "0x10000", "end": "0x10008"}])
                write_json(attempt / "loader/EBOOT.imports.json", [])
            if label == "ppu-lift":
                recomp = attempt / "port/recompiled"
                recomp.mkdir(parents=True, exist_ok=True)
                (recomp / "ppu_recomp.h").write_text("// fixture lifted header")
                (recomp / "ppu_recomp_0.cpp").write_text("// fixture lifted source")
            if label == "cmake-build":
                exe = attempt / "build/player" / ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp")
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"fresh ordinary output")
        def apply(*args):
            events.append("adapter")
            return None
        with patch.object(self.studio, "validate_backend"), patch.object(self.studio, "provenance", return_value={}), patch("studio.core.shutil.which", return_value="tool"), patch("studio.private_native_adapter.apply", side_effect=apply), patch.object(Runner, "run", run), patch("studio.fulton.collect", return_value={"warnings": []}), patch("studio.fulton.apply_loader_inputs"):
            report = self.studio.build(self.p, lambda _: None)
        self.assertLess(events.index("spu-discover-lift"), events.index("adapter"))
        self.assertLess(events.index("adapter"), events.index("cmake-configure"))
        self.assertNotIn("native_runtime_receipt", self.p)
        self.assertNotIn("native_runtime_receipt", report)
        self.assertIsNone(self.studio.native_diagnostics(self.p, Path(self.p["native_exe"]), sha256(self.elf)))
        self.assertEqual(self.p["native_candidate_history"][0]["native_runtime_receipt"], str(self.receipt))
        self.assertTrue(self.old_exe.is_file())
        self.assertTrue(self.receipt.is_file())

    def test_rebuild_applies_adapter_before_configure_and_archives_old_binding(self):
        attempt = self.studio.project_dir(self.p) / "attempts/rebuild"
        port = attempt / "port"
        (port / "recompiled").mkdir(parents=True)
        (port / "recompiled/ppu_recomp.h").write_text("// preserve")
        (port / "recompiled/ppu_recomp_0.cpp").write_text("// preserve")
        (port / "CMakeLists.txt").write_text("# fixture")
        # Completed legacy adapter reuse has an existing workload registry.
        (port / "spu").mkdir()
        (port / "spu/spu_workloads.c").write_text("// retained successful registry")
        write_json(attempt / "loader/EBOOT.functions.json", [{"start": "0x10000", "end": "0x10008"}])
        write_json(attempt / "loader/EBOOT.imports.json", [])
        write_json(attempt / "build-report.json", {"input_sha256": sha256(self.elf)})
        self.p["attempt"] = str(attempt)
        events = []
        old_receipt_bytes = self.receipt.read_bytes()
        adapter = {"adapter_id": "fixture", "private": True, "input_sha256": sha256(self.elf)}
        def apply(stage, elf, backend, project):
            self.assertEqual(stage, port)
            self.assertNotIn("native_runtime_receipt", project)
            events.append("adapter")
            return adapter
        def run(runner, label, argv, **kwargs):
            events.append(label)
            if label == "cmake-build":
                exe = attempt / "build/player" / ("MyGameRecomp.exe" if os.name == "nt" else "MyGameRecomp")
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"fixture compiled adapter")
        with patch.object(self.studio, "validate_backend"), patch.object(self.studio, "provenance", return_value={}), patch("studio.core.shutil.which", return_value="tool"), patch("studio.private_native_adapter.apply", side_effect=apply), patch.object(Runner, "run", run):
            report = self.studio.build(self.p, lambda _: None, reuse=True)
        self.assertEqual(events, ["adapter", "cmake-configure", "cmake-build"])
        self.assertTrue((attempt / "private-native-adapter.json").is_file())
        self.assertEqual(report["native_runtime_receipt"], self.p["native_runtime_receipt"])
        self.assertEqual(self.p["native_candidate_history"][0]["native_exe"], str(self.old_exe))
        self.assertEqual(self.receipt.read_bytes(), old_receipt_bytes)

    def test_legacy_history_recovers_own_attempt_without_inheriting_live_attempt(self):
        old_attempt=self.old_exe.parent
        write_json(old_attempt/'build-report.json', {'status':'compiled; gameplay unverified','input_sha256':sha256(self.elf)})
        self.p['native_candidate_history']=[{'native_exe':str(self.old_exe),'native_runtime_receipt':str(self.receipt)}]
        self.p.update(native_exe='',attempt=str(self.base/'active'),status='building',native_runtime_receipt=None)
        candidates=self.studio.list_test_candidates(self.p)
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['attempt'],str(old_attempt.resolve()))
        self.assertEqual(candidates[0]['project_snapshot']['native_runtime_receipt'],str(self.receipt))
        self.assertEqual(self.p['attempt'],str(self.base/'active'))
        self.old_exe.write_bytes(b'changed')
        self.assertEqual(self.studio.list_test_candidates(self.p),[])


    def test_independent_repair_sidecar_is_input_and_output_bound(self):
        attempt,exe=self.fresh_output()
        self.p.update(native_exe='',attempt='',native_runtime_receipt=None)
        output_hash=sha256(exe)
        write_json(attempt/'build-report.json',dict(status='compiled; gameplay unverified',input_sha256=sha256(self.elf),
            native_repair_executable=str(exe),native_repair_executable_sha256=output_hash))
        sidecar=self.studio.home/'native-repairs'/self.p['id']/'repair.json'
        write_json(sidecar,dict(schema=1,status='ready',project_id=self.p['id'],input_sha256=sha256(self.elf),
            executable_sha256=output_hash,native_exe=str(exe),attempt=str(attempt),label='Targeted repair'))
        before=dict(self.p)
        candidates=self.studio.list_test_candidates(self.p)
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['label'],'Targeted repair')
        self.assertEqual(self.p,before)
        exe.write_bytes(b'altered repair')
        self.assertEqual(self.studio.list_test_candidates(self.p),[])

    def test_independent_repair_rejects_attempt_outside_project(self):
        attempt,exe=self.fresh_output()
        self.p.update(native_exe='',attempt='',native_runtime_receipt=None)
        sidecar=self.studio.home/'native-repairs'/self.p['id']/'bad.json'
        write_json(sidecar,dict(schema=1,status='ready',project_id=self.p['id'],input_sha256=sha256(self.elf),
            executable_sha256=sha256(exe),native_exe=str(exe),attempt=str(self.base)))
        self.assertEqual(self.studio.list_test_candidates(self.p),[])


if __name__ == "__main__":
    unittest.main()
