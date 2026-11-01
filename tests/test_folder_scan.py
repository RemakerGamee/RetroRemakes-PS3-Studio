# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
from pathlib import Path
import tempfile,unittest
from studio.core import Studio
from test_core import make_elf

class FolderScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.studio=Studio(self.base/'workspace');self.games=self.base/'PS3 GAMES';self.games.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def game(self,name,encrypted=False):
        root=self.games/name;usr=root/'PS3_GAME/USRDIR';usr.mkdir(parents=True)
        if encrypted:(usr/'EBOOT.BIN').write_bytes(b'SCE\0'+name.encode()+bytes(128))
        else:make_elf(usr/'EBOOT.elf');(usr/'EBOOT.BIN').write_bytes(b'SCE\0'+bytes(128))
        return root
    def test_import_library_and_skip_duplicates_preserving_existing_receipts(self):
        a=self.game('First');self.game('Second',True)
        existing=self.studio.add(a);existing.update(native_exe='keep.exe',attempt='keep-attempt')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(len(result['added']),1);self.assertEqual(len(result['duplicates']),1)
        self.assertEqual(result['needs_decrypted_elf'],['Second']);self.assertEqual(existing['native_exe'],'keep.exe')
        self.assertEqual(len(self.studio.scan_games_folder(self.games,lambda x:None)['added']),0)
    def test_prefers_decrypted_elf_and_ignores_nested_copy(self):
        root=self.game('First');nested=root/'duplicate/PS3_GAME/USRDIR';nested.mkdir(parents=True);make_elf(nested/'EBOOT.elf')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['found'],1);self.assertTrue(result['added'][0]['native_input'])
        self.assertTrue(self.studio.state['projects'][0]['elf'].lower().endswith('eboot.elf'))
    def test_dry_scan_never_changes_queue(self):
        self.game('First');result=self.studio.scan_games_folder(self.games,lambda x:None,import_games=False)
        self.assertEqual(result['found'],1);self.assertEqual(self.studio.state['projects'],[])
    def test_file_entry_deduplicated_by_game_root(self):
        root=self.game('First');self.studio.add(root/'PS3_GAME/USRDIR/EBOOT.elf')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(len(result['duplicates']),1)

    def test_nested_distinct_game_is_discovered_alongside_root_game(self):
        root=self.game('Collection')
        usr=root/'Other Game/PS3_GAME/USRDIR';usr.mkdir(parents=True)
        elf=make_elf(usr/'EBOOT.elf');data=bytearray(elf.read_bytes());data[0x1007]^=1;elf.write_bytes(data)
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['found'],2);self.assertEqual(len(result['added']),2)
        self.assertEqual({p['title'] for p in result['added']},{'Collection','Other Game'})

    def test_identical_copy_in_other_folder_deduplicates_existing_game(self):
        root=self.game('Original');self.studio.add(root)
        duplicate=self.game('Copy')
        result=self.studio.scan_games_folder(duplicate,lambda x:None)
        self.assertEqual(len(result['added']),0);self.assertEqual(len(result['duplicates']),1)
        self.assertEqual(len(self.studio.state['projects']),1)
        self.assertTrue((duplicate/'PS3_GAME/USRDIR/EBOOT.elf').exists())

    def distinct_elf(self,path):
        elf=make_elf(path);data=bytearray(elf.read_bytes());data[0x1007]^=1;elf.write_bytes(data)
        return elf

    def test_all_game_inputs_are_reported_and_prefer_decrypted_alternate(self):
        root=self.game('Encrypted',True)
        alt=root/'PS3_GAME/USRDIR/modules/decrypted-main.ELF';alt.parent.mkdir()
        make_elf(alt)
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['files_found'],2)
        self.assertEqual({Path(x['path']).name for x in result['files']},{'EBOOT.BIN','decrypted-main.ELF'})
        self.assertEqual(len(result['added']),1)
        project=self.studio.state['projects'][0]
        self.assertEqual(project['elf'],str(alt))
        self.assertEqual(len(project['discovered_inputs']),2)

    def test_loose_inputs_are_distinct_and_shared_content_has_explicit_report(self):
        make_elf(self.games/'standalone.elf')
        self.distinct_elf(self.games/'EBOOT.BIN')
        make_elf(self.games/'copy.ELF')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['files_found'],3)
        self.assertEqual(len(result['added']),2)
        self.assertEqual(len(result['duplicates']),1)
        self.assertEqual(sum('duplicate_of' in x for x in result['files']),1)
        self.assertEqual(sum(len(p['discovered_inputs']) for p in self.studio.state['projects']),3)

    def test_rescan_enriches_game_without_replacing_selected_build_or_controller(self):
        root=self.game('Existing');project=self.studio.add(root)
        chosen=str(root/'PS3_GAME/USRDIR/EBOOT.elf')
        project.update(elf=chosen,native_exe='candidate05.exe',attempt='retained',status='built',
                       controller_profile={'type':'DualShock4'})
        self.distinct_elf(root/'PS3_GAME/USRDIR/alternate.elf')
        before={k:project[k] for k in ('elf','native_exe','attempt','status','controller_profile')}
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual({k:project[k] for k in before},before)
        self.assertEqual(len(project['discovered_inputs']),3)
        self.assertEqual(result['files_found'],3)
        self.assertEqual(len(self.studio.state['projects']),1)

    def test_invalid_elf_is_reported_and_generated_trees_are_excluded(self):
        (self.games/'bad.elf').write_bytes(b'not an elf')
        for folder in ('workspace','generated','recompiled'):
            child=self.games/folder;child.mkdir();make_elf(child/'skip.elf')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['files_found'],1)
        self.assertEqual(result['files'][0]['disposition'],'error')
        self.assertEqual(len(result['errors']),1)

    def test_dry_scan_does_not_enrich_existing_project(self):
        import copy
        root=self.game('Existing');self.studio.add(root)
        snapshot=copy.deepcopy(self.studio.state)
        result=self.studio.scan_games_folder(self.games,lambda x:None,import_games=False)
        self.assertEqual(self.studio.state,snapshot)
        self.assertEqual(result['files_found'],2)

    def test_copy_group_retains_unique_alternate_as_associated_input(self):
        self.game('Original');copy=self.game('Z-copy')
        self.distinct_elf(copy/'PS3_GAME/USRDIR/extra.elf')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['files_found'],5)
        self.assertEqual(len(self.studio.state['projects']),1)
        self.assertEqual(len(self.studio.state['projects'][0]['discovered_inputs']),5)

    def test_existing_encrypted_game_automatically_selects_new_decrypted_alternate(self):
        root=self.game('Encrypted',True);project=self.studio.add(root)
        project['status']='needs decrypted ELF'
        alt=root/'PS3_GAME/USRDIR/decrypted.elf';make_elf(alt)
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(project['elf'],str(alt))
        self.assertEqual(project['status'],'ready for attempt')
        self.assertTrue(result['updated'][0]['selected_decrypted_elf'])

    def test_mixed_encrypted_decrypted_copies_group_by_shared_main_eboot(self):
        root=self.game('Original',True);copy=self.game('Copy',True)
        (copy/'PS3_GAME/USRDIR/EBOOT.BIN').write_bytes((root/'PS3_GAME/USRDIR/EBOOT.BIN').read_bytes())
        make_elf(root/'PS3_GAME/USRDIR/EBOOT.elf')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['found'],1)
        self.assertEqual(len(self.studio.state['projects']),1)
        self.assertEqual(len(self.studio.state['projects'][0]['discovered_inputs']),3)

    def test_established_game_owns_inventory_over_manual_eboot_queue_row(self):
        root=self.game('Existing');project=self.studio.add(root)
        project.update(attempt='existing-attempt',elf=str(root/'PS3_GAME/USRDIR/EBOOT.elf'))
        manual=self.studio.add(root/'PS3_GAME/USRDIR/EBOOT.elf')
        result=self.studio.scan_games_folder(self.games,lambda x:None)
        self.assertEqual(result['updated'][0]['project_id'],project['id'])
        self.assertEqual(len(project['discovered_inputs']),2)
        self.assertNotIn('discovered_inputs',manual)
        self.assertEqual(len(self.studio.state['projects']),2)

    def test_encrypted_first_summary_reconciles_later_decrypted_copy(self):
        encrypted=self.game('A encrypted',True);decrypted=self.game('Z decrypted',True)
        main=encrypted/'PS3_GAME/USRDIR/EBOOT.BIN'
        (decrypted/'PS3_GAME/USRDIR/EBOOT.BIN').write_bytes(main.read_bytes())
        elf=make_elf(decrypted/'PS3_GAME/USRDIR/EBOOT.elf')
        for import_games in (False,True):
            with self.subTest(import_games=import_games):
                result=self.studio.scan_games_folder(self.games,lambda x:None,import_games=import_games)
                self.assertEqual(len(result['added']),1)
                item=result['added'][0]
                self.assertEqual(item['elf'],str(elf))
                self.assertEqual(item['format'],'ELF')
                self.assertTrue(item['native_input'])
                self.assertEqual(result['needs_decrypted_elf'],[])
                self.assertEqual(len(result['files']),3)
                self.assertEqual(sum(x['format']=='SELF' and not x['native_input'] for x in result['files']),2)
                self.assertEqual(sum(x['format']=='ELF' and x['native_input'] for x in result['files']),1)
                if import_games:self.assertEqual(self.studio.state['projects'][0]['elf'],str(elf))
