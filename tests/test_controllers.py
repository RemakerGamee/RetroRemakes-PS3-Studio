# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import tempfile,unittest
from pathlib import Path
from studio.controllers import defaults,save_profile,load_profile,validate
class ControllerTests(unittest.TestCase):
    def test_save_reload_ds4_custom_mapping(self):
        with tempfile.TemporaryDirectory() as home:
            p=defaults();p.update(type='ds4',map_13='14',map_14='13',invert_left_y='1')
            save_profile(home,p);self.assertEqual(load_profile(home),p)
    def test_reject_invalid_binding_and_unsafe_type(self):
        for change in (dict(map_0='16'),dict(type='untrusted'),dict(device='4'),dict(deadzone='99999')):
            p=defaults();p.update(change)
            with self.assertRaises(ValueError):validate(p)
    def test_default_identity_and_no_settings_overwrite(self):
        with tempfile.TemporaryDirectory() as home:
            sentinel=Path(home)/'studio.json';sentinel.write_bytes(b'keep-existing-settings')
            save_profile(home,defaults());self.assertEqual(sentinel.read_bytes(),b'keep-existing-settings')
            self.assertEqual([load_profile(home)['map_'+str(i)] for i in range(16)],[str(i) for i in range(16)])

    def test_invalid_saved_profile_recovers_without_overwriting(self):
        with tempfile.TemporaryDirectory() as home:
            p=Path(home)/'controller-profiles/default.ini';p.parent.mkdir();p.write_text('[controller]\nmap_0=99\n')
            before=p.read_bytes();warnings=[]
            self.assertEqual(load_profile(home,warnings.append),defaults());self.assertTrue(warnings)
            self.assertEqual(p.read_bytes(),before)
