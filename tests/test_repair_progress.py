# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from studio import game_fixes as fixes
from studio.core import Cancelled

class RepairProgressTests(unittest.TestCase):
    def test_snapshot_counts_every_actual_byte_pass_and_keeps_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'port';source.mkdir()
            data=b'x'*(2*1024*1024+23);(source/'large.cpp').write_bytes(data)
            (source/'empty.h').write_bytes(b'')
            rows=[]
            hashes=fixes._copy_sources(source,root/'copy',threading.Event(),lambda *row:rows.append(row))
            self.assertEqual(hashes['large.cpp'],hashlib.sha256(data).hexdigest())
            self.assertEqual((root/'copy/large.cpp').read_bytes(),data)
            self.assertEqual(sum(row[-1] for row in rows),4*len(data))
            self.assertTrue(any(0<row[1]<row[2] for row in rows))
            self.assertEqual(len(set(row[0] for row in rows)),3)

    def test_chunk_cancellation_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'port';source.mkdir()
            data=b'x'*(2*1024*1024);(source/'game.cpp').write_bytes(data)
            cancel=threading.Event()
            def update(*row):
                if row[-1]:cancel.set()
            with self.assertRaises(Cancelled):fixes._copy_sources(source,root/'copy',cancel,update)
            self.assertEqual((source/'game.cpp').read_bytes(),data)

    def test_sidecar_is_small_readable_and_failure_does_not_claim_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);job=home/'game-fixes/title'/('a'*32);job.mkdir(parents=True)
            publisher=fixes._RepairProgress(job,dict(id='title',name='A game'),lambda _:None)
            publisher.source_total=100
            publisher.source('Checking sources',20,100,0,1,'game.cpp',20)
            publisher.publish('Repair failed',None,publisher.value['overall_percent'],'Failure',status='failed',force=True)
            rows=fixes.read_progress(home,['title'])
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'failed')
            self.assertLess(rows[0]['overall_percent'],100)
            self.assertLess((job/'progress.json').stat().st_size,32768)

    def test_legacy_worker_reports_unknown_instead_of_inventing_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);folder=home/'game-fixes/title';folder.mkdir(parents=True)
            (folder/'.fix-worker.lock').write_text(json.dumps(dict(job_id='a'*32,pid=123)))
            rows=fixes.read_progress(home,['title'])
            self.assertEqual(rows[0]['status'],'unknown')
            self.assertIsNone(rows[0]['step_percent']);self.assertIsNone(rows[0]['overall_percent'])
