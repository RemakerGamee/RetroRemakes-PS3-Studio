# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Metadata-only output discovery regressions; no game/native/UI execution."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from studio import compiled_library as lib

DIGEST = '1' * 64
OTHER = '2' * 64

class CompiledLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'studio'
        self.home.mkdir()
        self.folder = self.root / 'selected-outputs'
        self.folder.mkdir()
        self.project = dict(id='game-one', source=str(self.root/'readonly-game'),
                            status='building', attempt='do-not-change', input_sha256=DIGEST)
        self.studio = types.SimpleNamespace(home=self.home, state={'projects':[self.project]},
                    project_dir=lambda p:self.home/'projects'/p['id'])
        self.studio.save = mock.Mock(side_effect=AssertionError('Queue save is forbidden'))

    def report(self, attempt, *, status='compiled; gameplay unverified', digest=DIGEST, value=None, exe=True):
        attempt.mkdir(parents=True, exist_ok=True)
        report = dict(status=status, input_sha256=digest) if value is None else value
        (attempt/'build-report.json').write_text(json.dumps(report), encoding='utf-8')
        path=attempt/'build/player/MyGameRecomp.exe'
        if exe:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'fixture; never executed')
        return path

    def scan(self, folder=None):
        # If known metadata is present, discovery must not hash any real input
        # or start the existing expensive candidate/runtime validator.
        with mock.patch('studio.core.sha256', side_effect=AssertionError('Unexpected file hashing')):
            return lib.scan_outputs(self.studio, self.folder if folder is None else folder)

    def test_nested_completed_metadata_input_binding_and_queue_unchanged(self):
        exe=self.report(self.folder/'archive/game/attempts/finished')
        self.report(self.folder/'wrong-edition',digest=OTHER)
        self.report(self.folder/'still-linking',status='building')
        self.report(self.folder/'failed',status='failed')
        self.report(self.folder/'no-output',exe=False)
        before=copy.deepcopy(self.studio.state)
        queue=self.home/'studio.json';queue.write_bytes(b'queue stays byte-for-byte')
        progress=[]
        with mock.patch('studio.core.sha256', side_effect=AssertionError('Unexpected hashing')):
            result=lib.scan_outputs(self.studio,self.folder,progress.append)
        self.assertEqual(len(result['records']),1)
        self.assertEqual(Path(result['records'][0]['native_exe']),exe.resolve())
        self.assertEqual(result['records'][0]['project_id'],'game-one')
        self.assertEqual(self.studio.state,before)
        self.assertEqual(queue.read_bytes(),b'queue stays byte-for-byte')
        self.studio.save.assert_not_called()
        self.assertTrue(progress)

    def test_selected_folder_persists_default_roots_and_no_duplicate_exes(self):
        default=self.home/'projects/game-one/attempts/default'
        self.report(default)
        self.report(self.home/'game-fixes/game-one/job/workspace/a')
        self.report(self.folder/'nested/attempt')
        first=self.scan()
        self.assertEqual(len(first['records']),3)
        self.assertEqual(lib.selected_folder(self.studio),str(self.folder.resolve()))
        again=lib.scan_outputs(self.studio)
        self.assertEqual(len(again['records']),3)
        self.assertEqual(again['selected_folder'],first['selected_folder'])
        overlapping=self.scan(self.home)
        self.assertEqual(len(overlapping['records']),2)

    def test_completed_report_cannot_import_escaped_executable(self):
        attempt=self.folder/'attempt'
        self.report(attempt,exe=False)
        outside=self.root/'outside.exe';outside.write_bytes(b'not a candidate')
        (attempt/'build-report.json').write_text(json.dumps(dict(status='compiled; gameplay unverified',
               input_sha256=DIGEST,native_exe=str(outside),native_repair_executable=str(outside))))
        self.assertEqual(self.scan()['records'],[])

    def test_records_for_rechecks_containment_and_project_identity(self):
        attempt=self.folder/'attempt';exe=self.report(attempt)
        valid=dict(project_id='game-one',attempt=str(attempt),native_exe=str(exe),scan_root=str(self.folder))
        records=[valid,{**valid,'project_id':'other'},
                 {**valid,'native_exe':str(self.root/'escaped.exe')},
                 {**valid,'scan_root':str(self.root/'unrelated')},None,{'project_id':'game-one'}]
        lib.index_path(self.studio).write_text(json.dumps(dict(schema=1,records=records)))
        got=lib.records_for(self.studio,self.project)
        self.assertEqual(len(got),1)
        self.assertEqual(Path(got[0]['native_exe']),exe.resolve())
        self.assertIsNone(got[0]['native_runtime_receipt'])

    def test_identical_input_projects_keep_studio_owned_output_ownership(self):
        other = {**self.project, 'id': 'game-two'}
        self.studio.state['projects'].append(other)
        one = self.report(self.home/'projects/game-one/attempts/one')
        two = self.report(self.home/'projects/game-two/attempts/two')
        fix = self.report(self.home/'game-fixes/game-two/job/workspace/attempt')
        result = self.scan(self.home)
        bindings = {(row['project_id'], Path(row['native_exe'])) for row in result['records']}
        self.assertEqual(bindings, {('game-one', one.resolve()), ('game-two', two.resolve()), ('game-two', fix.resolve())})
        self.assertEqual(lib.output_owner(self.studio, one.parents[2]), 'game-one')
        self.assertEqual(lib.output_owner(self.studio, fix.parents[2]), 'game-two')
        self.assertIsNone(lib.output_owner(self.studio, self.folder/'external'))

    def test_stale_index_cannot_assign_foreign_studio_output_to_same_input_project(self):
        own_attempt = self.home/'projects/game-one/attempts/one'
        foreign_attempt = self.home/'projects/game-two/attempts/two'
        own, foreign = self.report(own_attempt), self.report(foreign_attempt)
        rows = [dict(project_id='game-one', attempt=str(attempt), native_exe=str(exe), scan_root=str(self.home))
                for attempt, exe in ((own_attempt, own), (foreign_attempt, foreign))]
        lib.index_path(self.studio).write_text(json.dumps(dict(schema=1, records=rows)))
        got = lib.records_for(self.studio, self.project)
        self.assertEqual([Path(row['native_exe']) for row in got], [own.resolve()])

    def candidate_studio(self):
        from studio.core import Studio
        studio = Studio.__new__(Studio)
        studio.home = self.home
        studio.state = self.studio.state
        source = self.root/'private-input.elf'
        source.write_bytes(b'manufactured input; never executed')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.project.update(source=str(source), elf=str(source), input_sha256=digest,
                            native_exe='', attempt=str(self.home/'projects/game-one/attempts/live'), status='building')
        return studio, digest

    def test_explicit_repair_label_wins_duplicate_generic_folder_record(self):
        from studio.core import write_json
        studio, digest = self.candidate_studio()
        attempt = self.home/'projects/game-one/attempts/repaired'
        exe = self.report(attempt, digest=digest)
        exe_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
        write_json(attempt/'build-report.json', dict(status='compiled; gameplay unverified', input_sha256=digest,
                   native_repair_executable=str(exe), native_repair_executable_sha256=exe_hash))
        write_json(self.home/'native-repairs/game-one/repair.json', dict(schema=1, status='ready', project_id='game-one',
                   attempt=str(attempt), native_exe=str(exe), input_sha256=digest, executable_sha256=exe_hash,
                   label='Verified context repair; gameplay unverified'))
        lib.scan_outputs(studio, self.home)
        self.assertEqual(len(lib.records_for(studio, self.project)), 1)
        with mock.patch('studio.game_fixes.list_ready_candidates', return_value=[]):
            candidates = studio.list_test_candidates(self.project)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['label'], 'Verified context repair; gameplay unverified')
        self.assertEqual(candidates[0]['project_snapshot']['_repair_executable_sha256'], exe_hash)

    def test_trusted_diagnostic_receipt_wins_duplicate_generic_folder_record(self):
        from studio.core import write_json
        studio, digest = self.candidate_studio()
        attempt = self.home/'projects/game-one/attempts/hybrid'
        exe = self.report(attempt, digest=digest)
        receipt = self.home/'verification/hybrid.json'
        write_json(receipt, dict(executable=str(exe), executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                   input_sha256=digest, diagnostic_env={'SPU_INTERP_UNLIFTED': '1', 'SPU_INTERP_BUDGET': '1000000'}))
        self.project.update(native_runtime_receipt=str(receipt), label='Explicit hybrid repair')
        lib.scan_outputs(studio, self.home)
        self.assertEqual(len(lib.records_for(studio, self.project)), 1)
        with mock.patch('studio.game_fixes.list_ready_candidates', return_value=[]):
            candidates = studio.list_test_candidates(self.project)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['label'], 'Explicit hybrid repair')
        selected = candidates[0]['project_snapshot']
        self.assertEqual(selected['native_runtime_receipt'], str(receipt))
        self.assertEqual(studio.native_diagnostics(selected, exe, digest)['diagnostic_env']['SPU_INTERP_UNLIFTED'], '1')

    def test_hidden_ids_persist_independently_of_rescans_queue_and_output_files(self):
        exe = self.report(self.home/'projects/game-one/attempts/finished')
        queue = self.home/'studio.json'; queue.write_bytes(b'active queue must remain unchanged')
        source = self.root/'private-input.elf'; source.write_bytes(b'original input')
        state_before = copy.deepcopy(self.studio.state)
        bytes_before = {path: path.read_bytes() for path in (queue, exe, exe.parents[2]/'build-report.json', source)}
        self.scan()
        self.assertEqual(lib.set_hidden_projects(self.studio, ['game-one', '../invalid']), {'game-one'})
        pref = self.home/'compiled-library-preferences.json'
        preferences_before = pref.read_bytes()
        # A fresh object and a replacement scan index must preserve visibility.
        reopened = types.SimpleNamespace(home=self.home)
        self.assertEqual(lib.hidden_project_ids(reopened), {'game-one'})
        lib.index_path(self.studio).write_text('{"schema":1,"records":[]}')
        self.scan()
        self.assertEqual(pref.read_bytes(), preferences_before)
        self.assertEqual(lib.hidden_project_ids(self.studio), {'game-one'})
        self.assertEqual(len(lib.records_for(self.studio, self.project)), 1)
        self.assertEqual(self.studio.state, state_before)
        for path, value in bytes_before.items(): self.assertEqual(path.read_bytes(), value)
        self.studio.save.assert_not_called()

    def test_read_only_visibility_changes_are_refused_and_restore_all_is_supported(self):
        lib.set_hidden_projects(self.studio, ['game-one', 'game-two'])
        pref = self.home/'compiled-library-preferences.json'
        before = pref.read_bytes()
        self.studio.read_only = True
        with self.assertRaises(RuntimeError): lib.set_hidden_projects(self.studio, ['game-three'])
        with self.assertRaises(RuntimeError): lib.set_hidden_projects(self.studio, [], hidden=False)
        self.assertEqual(pref.read_bytes(), before)
        self.studio.read_only = False
        self.assertEqual(lib.set_hidden_projects(self.studio, ['game-one'], hidden=False), {'game-two'})
        self.assertEqual(lib.set_hidden_projects(self.studio, [], hidden=False), set())
        self.assertEqual(lib.hidden_project_ids(self.studio), set())

    def test_malformed_metadata_is_skipped_without_aborting_scan(self):
        for i,value in enumerate(([1],42,'bad report')):
            self.report(self.folder/str(i),value=value)
        self.report(self.folder/'valid')
        result=self.scan()
        self.assertEqual(len(result['records']),1)

    def test_oversized_and_invalid_json_report_are_ignored(self):
        bad=self.folder/'oversized';bad.mkdir()
        (bad/'build-report.json').write_text(' '*(4*1024*1024+1))
        invalid=self.folder/'invalid';invalid.mkdir();(invalid/'build-report.json').write_text('{')
        self.report(self.folder/'valid')
        self.assertEqual(len(self.scan()['records']),1)

    def test_index_corruption_or_wrong_schema_is_safe(self):
        p=lib.index_path(self.studio)
        for value in ('{','[]','null','{"schema":2}','{"schema":1,"records":42}'):
            p.write_text(value)
            self.assertEqual(lib.records_for(self.studio,self.project),[])

    def test_nonexistent_selection_preserves_existing_index(self):
        p=lib.index_path(self.studio);p.write_bytes(b'{"schema":1,"selected_folder":"existing"}')
        before=p.read_bytes()
        with self.assertRaises(ValueError):self.scan(self.root/'missing')
        self.assertEqual(p.read_bytes(),before)

    def test_failed_and_inprogress_attempts_prune_generated_trees(self):
        for name,status in [('failed','failed'),('busy','building'),('wrong','compiled; gameplay unverified')]:
            attempt=self.folder/name
            self.report(attempt,status=status,digest=OTHER if name=='wrong' else DIGEST)
            for sub in ['build/objects/deep','port/recompiled/deep','loader/x','logs/x']:
                (attempt/sub).mkdir(parents=True,exist_ok=True)
        visits=[];walk=os.walk
        def spy(*args,**kwargs):
            for result in walk(*args,**kwargs):
                visits.append(Path(result[0]));yield result
        with mock.patch.object(lib.os,'walk',side_effect=spy):self.scan()
        self.assertFalse(any(p.name in ('build','port','loader','logs') for p in visits),visits)

    def test_candidate_validator_never_borrows_old_report_for_active_link_target(self):
        from studio.core import Studio
        attempt=self.folder/'same-attempt'
        exe=self.report(attempt)
        self.project.update(attempt=str(attempt),native_exe=None,status='building')
        self.studio.native_diagnostics=mock.Mock(return_value=None)
        self.scan()
        input_path=self.root/'fixture-input.elf'
        def digest(path):return DIGEST if Path(path)==input_path else OTHER
        with mock.patch('studio.core.choose_elf',return_value=input_path), \
             mock.patch('studio.core.sha256',side_effect=digest), \
             mock.patch('studio.game_fixes.list_ready_candidates',return_value=[]):
            self.assertEqual(Studio.list_test_candidates(self.studio,self.project),[])
            self.studio.native_diagnostics.assert_not_called()
            # A different active attempt may coexist with an older finished
            # candidate. It stays selectable without borrowing current state.
            self.project['attempt']=str(self.folder/'different-live-attempt')
            got=Studio.list_test_candidates(self.studio,self.project)
            self.assertEqual(len(got),1)
            self.assertEqual(Path(got[0]['exe']),exe.resolve())
            self.assertEqual(Path(got[0]['attempt']),attempt.resolve())
            # A changed input-bound report must be rejected on each use even
            # when a previous scan still contains its old index record.
            report=attempt/'build-report.json'
            report.write_text(json.dumps(dict(status='compiled; gameplay unverified',input_sha256=OTHER)))
            self.assertEqual(Studio.list_test_candidates(self.studio,self.project),[])

    def test_symlink_file_guard_branches_without_needing_windows_link_privilege(self):
        attempt=self.folder/'attempt';exe=self.report(attempt)
        original=Path.is_symlink
        for blocked in (exe,attempt/'build-report.json'):
            with self.subTest(blocked=blocked):
                def marked(path):return path==blocked or original(path)
                with mock.patch.object(Path,'is_symlink',marked):
                    self.assertEqual(self.scan()['records'],[])

    def link(self,target,link,directory=False):
        try:link.symlink_to(target,target_is_directory=directory)
        except (OSError,NotImplementedError) as e:self.skipTest('Windows symlink privilege unavailable: '+str(e))

    def test_symlink_directory_is_not_followed(self):
        outside=self.root/'outside-attempt';self.report(outside)
        self.link(outside,self.folder/'linked-attempt',True)
        self.assertEqual(self.scan()['records'],[])

    def test_symlink_executable_is_rejected_even_when_target_is_inside_attempt(self):
        attempt=self.folder/'attempt';exe=self.report(attempt)
        real=exe.with_name('real.exe');exe.rename(real);self.link(real,exe)
        self.assertEqual(self.scan()['records'],[])

    def test_symlink_report_is_not_followed(self):
        attempt=self.folder/'attempt';self.report(attempt)
        report=attempt/'build-report.json';real=self.root/'outside-report.json';report.rename(real);self.link(real,report)
        self.assertEqual(self.scan()['records'],[])

if __name__=='__main__':unittest.main()
