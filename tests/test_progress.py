# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
from pathlib import Path
import sys,tempfile,time,unittest
from studio.progress import ProgressState,ProgressEmitter,BUILD_STEPS
from studio.core import Runner

class ProgressTests(unittest.TestCase):
    def test_lift_100_is_not_whole_translation_or_build_100(self):
        state=ProgressState()
        for key in ('inspect','prepare','ppu-load'):state.event(key,'done')
        state.event('ppu-lift','start')
        state.event('ppu-lift','output',line='... lifted 24363/24363 functions (100%)')
        snap=state.snapshot()
        self.assertEqual(snap['key'],'ppu-finalize');self.assertEqual(snap['step_percent'],0)
        self.assertAlmostEqual(snap['overall_percent'],400/9)
        self.assertIn('no intermediate counts',snap['step'])
        state.event('ppu-lift','done');self.assertAlmostEqual(state.snapshot()['overall_percent'],500/9)
    def test_fractional_ninja_and_failed_build_do_not_claim_success(self):
        state=ProgressState()
        for key,_ in BUILD_STEPS[:7]:state.event(key,'done')
        state.event('cmake-build','start');state.event('cmake-build','output',line='[50/200] Building CXX object')
        snap=state.snapshot();self.assertEqual(snap['step_percent'],25)
        self.assertAlmostEqual(snap['overall_percent'],100*7.25/9)
        state.event('cmake-build','failed',detail='compiler error')
        self.assertEqual(state.snapshot()['status'],'failed');self.assertLess(state.snapshot()['overall_percent'],100)
    def test_success_and_queue_progress(self):
        state=ProgressState(index=1,total=2)
        for key,_ in BUILD_STEPS:state.event(key,'done')
        self.assertEqual(state.snapshot()['overall_percent'],100)
        state=ProgressState(index=1,total=2);self.assertEqual(state.snapshot()['overall_percent'],50)
    def test_runner_streams_before_process_finishes_and_records_active_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=[];snapshots=[];received=[]
            runner=None
            def emit(line):
                log.append(line)
                if line=='first':
                    received.append(time.monotonic())
                    record=json.loads((Path(tmp)/'commands.json').read_text())[0]
                    self.assertEqual(record['status'],'running');self.assertGreater(record['pid'],0)
            progress=ProgressEmitter(emit,snapshots.append)
            runner=Runner(tmp,progress)
            start=time.monotonic()
            runner.run('cmake-build',[sys.executable,'-c',"import time; print('first',flush=True); time.sleep(.7); print('[3/4] building',flush=True)"])
            end=time.monotonic()
            self.assertIn('first',log);self.assertLess(received[0],end-.35)
            self.assertTrue(any(s['step_percent']==75 for s in snapshots))
            self.assertEqual(runner.records[0]['exit_code'],0)
    def test_runner_failure_keeps_measured_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshots=[];emit=ProgressEmitter(lambda x:None,snapshots.append)
            runner=Runner(tmp,emit)
            with self.assertRaisesRegex(RuntimeError,'exit 2'):
                runner.run('cmake-build',[sys.executable,'-c',"import sys; print('[1/4] building',flush=True); sys.exit(2)"])
            self.assertEqual(snapshots[-1]['status'],'failed');self.assertEqual(snapshots[-1]['step_percent'],25)

    def test_existing_attempt_viewer_retains_progress_between_log_updates(self):
        from studio.progress_monitor import AttemptProgress
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);logs=base/'logs';logs.mkdir()
            keys=('ppu-load','ppu-lift','spu-discover-lift','cmake-configure')
            records=[]
            for i,key in enumerate(keys,1):
                (logs/f'{i:02}-{key}.log').write_text('done\n')
                records.append(dict(stage=key,status='passed'))
            (logs/'commands.json').write_text(json.dumps(records))
            active=logs/'05-cmake-build.log';active.write_text('[100/200] compile\n')
            (base/'build-report.json').write_text(json.dumps(dict(status='building')))
            reader=AttemptProgress(base)
            a=reader.update();b=reader.update()
            self.assertEqual(a['step_percent'],50);self.assertEqual(a,b)
            with active.open('a') as stream:stream.write('[150/200] compile\n')
            self.assertEqual(reader.update()['step_percent'],75)
