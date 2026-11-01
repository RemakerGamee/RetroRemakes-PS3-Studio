# RetroRemakes PS3 Studio - backend fixes
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Parts come from open-source projects credited in NOTICE.md and keep their original licenses.
import struct
from test_spu_program_pipeline import Pipeline, elf


class MultiSegment(Pipeline):
    def test_distinct_file_and_ls_offsets_cross_segment_branch(self):
        blob = bytearray(elf(multi=True))
        # First segment at file 0x100 / LS 0x100 branches to second segment
        # at file 0x108 / LS 0x200. The LS hole must never become code.
        struct.pack_into('>I', blob, 0x100, (0x064 << 23) | (0x40 << 7))
        result = self.run_pipeline(('multi.elf', bytes(blob)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.manifest()
        self.assertTrue(report['complete'])
        program = report['programs'][0]
        self.assertEqual(program['status'], 'registered')
        self.assertEqual(program['executable_ranges'], [[0x100, 0x108], [0x200, 0x208]])
        self.assertNotIn('text_fingerprint', program)
        code = (self.output / program['name'] / 'code.bin').read_bytes()
        self.assertEqual(code[:8], blob[0x100:0x108])
        self.assertEqual(code[0x100:], blob[0x108:0x110])
        self.assertEqual(code[8:0x100], bytes(0xf8))
        self.assertTrue(any(s == 0x200 for s, e in program['diagnostics']['functions']))
        self.assertTrue(all(any(a <= s < e <= b for a, b in program['executable_ranges'])
                            for s, e in program['diagnostics']['functions']))

