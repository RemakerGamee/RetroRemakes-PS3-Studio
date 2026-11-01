# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from studio.core import Studio, Cancelled

class BatchElfImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.patcher=patch('studio.core.ROOT',self.root/'program');self.patcher.start();self.addCleanup(self.patcher.stop)
        self.studio=Studio(self.root/'workspace')
        self.games=self.root/'games';self.games.mkdir()
    def file(self,name,body=b'ELF input unchanged'):
        p=self.games/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body);return p
    def test_all_nested_case_insensitive_elfs_import_once_and_persist(self):
        paths=[self.file('Game A/PS3_GAME/USRDIR/EBOOT.elf'),self.file('Game A/PS3_GAME/USRDIR/plugins/helper.ELF'),self.file('Game B/deep/boot.ElF')]
        self.file('Game B/EBOOT.BIN');self.file('Game B/assets.bin')
        before={p:p.read_bytes() for p in paths}
        result=self.studio.import_elf_tree(self.games,lambda _:None)
        self.assertEqual(result['found'],3);self.assertEqual(len(result['added']),3)
        self.assertEqual({p['elf'] for p in self.studio.state['projects']},{str(p.resolve()) for p in paths})
        again=self.studio.import_elf_tree(self.games,lambda _:None)
        self.assertEqual(len(again['added']),0);self.assertEqual(len(again['duplicates']),3)
        self.assertTrue(Path(result['report']).is_file())
        self.assertEqual(len(Studio(self.studio.home).state['projects']),3)
        self.assertEqual(before,{p:p.read_bytes() for p in paths})
    def test_existing_completed_archived_game_is_not_reset_or_requeued(self):
        path=self.file('Game/PS3_GAME/USRDIR/EBOOT.elf');project=self.studio.add(path)
        project.update(status='compiled; untested',queue_removed=True,native_exe='retained.exe',attempt='retained')
        before=copy.deepcopy(project)
        result=self.studio.import_elf_tree(self.games,lambda _:None)
        self.assertEqual(len(result['duplicates']),1);self.assertEqual(project,before)
    def test_cancelled_scan_does_not_partially_change_queue(self):
        self.file('Game/a.elf');self.file('Game/deep/b.elf');cancel=threading.Event()
        def emit(_):cancel.set()
        with self.assertRaises(Cancelled):self.studio.import_elf_tree(self.games,emit,cancel)
        self.assertEqual(self.studio.state['projects'],[])
    def test_workspace_tree_is_excluded_even_under_parent_scan(self):
        self.file('a.elf');p=self.studio.home/'generated.elf';p.write_bytes(b'not original')
        result=self.studio.import_elf_tree(self.root,lambda _:None)
        self.assertEqual(result['found'],1)
        with self.assertRaises(ValueError):self.studio.import_elf_tree(self.studio.home)
    def test_symlink_subtree_is_not_traversed(self):
        target=self.root/'outside';target.mkdir();(target/'outside.elf').write_bytes(b'outside')
        try:(self.games/'linked').symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('Symlinks unavailable')
        self.assertEqual(self.studio.import_elf_tree(self.games,lambda _:None)['found'],0)

if __name__=='__main__':unittest.main()
