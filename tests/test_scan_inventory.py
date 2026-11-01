# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
from unittest import TestCase
from studio.gui import input_inventory_rows, scan_summary

class InventoryFormattingTests(TestCase):
    def test_inventory_exposes_ready_encrypted_duplicate_and_error(self):
        rows=input_inventory_rows([
            dict(path='z.elf',format='ELF',native_input=True,disposition='selected'),
            dict(path='a/EBOOT.BIN',format='SELF',native_input=False,duplicate_of='original/EBOOT.BIN',disposition='associated'),
            dict(path='bad.elf',error='Invalid ELF header',disposition='error')])
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0][2],'Encrypted SELF')
        self.assertIn('original/EBOOT.BIN',rows[0][4])
        self.assertEqual(rows[1][2],'Error')
        self.assertEqual(rows[2][2],'PPU ELF ready')

    def test_summary_counts_all_files_separately_from_games(self):
        text=scan_summary(dict(files_found=19,found=9,added=[],duplicates=[{}],errors=[]))
        self.assertIn('19 ELF/EBOOT files',text)
        self.assertIn('9 unique games/inputs',text)
