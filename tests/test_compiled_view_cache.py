# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from studio.compiled_library import read_cached_rows, write_cached_rows
from studio.core import Studio

class CompiledViewCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)
        self.studio=SimpleNamespace(home=self.home,read_only=False)
    def tearDown(self):self.tmp.cleanup()
    def test_verified_rows_survive_new_reader_without_queue_changes(self):
        queue=self.home/'studio.json';queue.write_bytes(b'queue must stay unchanged')
        rows=[('game1','Finished game',2,True)]
        write_cached_rows(self.studio,rows)
        fresh=SimpleNamespace(home=self.home,read_only=False)
        self.assertEqual(read_cached_rows(fresh),rows)
        self.assertEqual(queue.read_bytes(),b'queue must stay unchanged')
        self.assertFalse(list(self.home.glob('*.tmp')))
    def test_malformed_oversized_and_foreign_workspace_views_are_ignored(self):
        p=self.home/'compiled-library-view.json'
        for value in ['{', 'x'*262145, json.dumps(dict(schema=1,workspace='other',rows=[['p','Game',1,False]]))]:
            p.write_text(value)
            self.assertEqual(read_cached_rows(self.studio),[])
    def test_invalid_and_duplicate_rows_cannot_become_display_entries(self):
        rows=[['p','Game',1,False],['p','Duplicate',2,False],['../escape','Bad',1,False],['bad','Bad',True,False],['missing']]
        write_cached_rows(self.studio,rows)
        self.assertEqual(read_cached_rows(self.studio),[('p','Game',1,False)])
    def test_read_only_snapshot_does_not_write_view(self):
        self.studio.read_only=True
        with self.assertRaisesRegex(RuntimeError,'read-only'):write_cached_rows(self.studio,[])
        self.assertFalse((self.home/'compiled-library-view.json').exists())
    def test_unbuilt_project_never_reads_or_hashes_its_game_input(self):
        studio=Studio.__new__(Studio);studio.home=self.home
        project=dict(id='unbuilt',source='unused-game-input',elf='',native_exe='',attempt='')
        with patch('studio.core.choose_elf',side_effect=AssertionError('Unbuilt input must not be read')) as choose:
            self.assertEqual(studio.list_test_candidates(project),[])
        choose.assert_not_called()
