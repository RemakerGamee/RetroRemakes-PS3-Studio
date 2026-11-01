# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import hashlib,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from studio.updater import APP_ID,stage,validate_manifest,validate_package,version
from update_engine import apply_pending

class UpdateTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'app';self.root.mkdir()
  self.pkg=Path(self.tmp.name)/'release.zip';self.release='0.9.9';self.prefix='RetroRemakes-PS3-Studio-v'+self.release
  self.files={'bootstrap.py':'# new bootstrap','update_engine.py':'# new updater','studio/__init__.py':'VERSION = "0.9.9"',
              'studio/__main__.py':'#new main','studio/gui.py':'#new gui','studio/updater.py':'#new updater'}
 def tearDown(self):self.tmp.cleanup()
 def package(self,extra=None):
  with zipfile.ZipFile(self.pkg,'w') as z:
   for name,text in {**self.files,**(extra or {})}.items():z.writestr(self.prefix+'/'+name,text)
  return {'schema':1,'app_id':APP_ID,'version':self.release,'root':self.prefix,'url':'https://example.org/release.zip',
          'size':self.pkg.stat().st_size,'sha256':hashlib.sha256(self.pkg.read_bytes()).hexdigest()}
 def staged(self,manifest):
  with patch('studio.updater.fetch',return_value=self.pkg.read_bytes()):stage(self.root,manifest)
 def test_semantic_versions(self):self.assertGreater(version('0.10.0'),version('0.9.9'))
 def test_wrong_studio_rejected(self):
  m=self.package();m['app_id']='other-studio'
  with self.assertRaises(ValueError):validate_manifest(m)
 def test_http_channel_rejected(self):
  m=self.package();m['url']='http://example.org/file.zip'
  with self.assertRaises(ValueError):validate_manifest(m)
 def test_checksum_rejected_before_staging(self):
  m=self.package();m['sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'SHA-256'):self.staged(m)
  self.assertFalse((self.root/'.studio-updates/pending.json').exists())
 def test_path_traversal_rejected(self):
  m=self.package({'../outside.txt':'no'})
  with self.assertRaisesRegex(ValueError,'Unsafe'):validate_package(self.pkg,m)
 def test_workspace_replacement_rejected(self):
  m=self.package({'workspace/studio.json':'no'})
  with self.assertRaisesRegex(ValueError,'outside'):validate_package(self.pkg,m)
 def test_wrong_payload_version_rejected(self):
  m=self.package({'studio/__init__.py':'VERSION = "0.0.1"'})
  with self.assertRaisesRegex(ValueError,'VERSION'):validate_package(self.pkg,m)
 def test_stage_does_not_modify_running_app(self):
  (self.root/'bootstrap.py').write_text('old');m=self.package();self.staged(m)
  self.assertEqual((self.root/'bootstrap.py').read_text(),'old')
  self.assertTrue((self.root/'.studio-updates/pending.json').is_file())
 def test_apply_preserves_workspace_and_backs_up(self):
  (self.root/'studio').mkdir();(self.root/'studio/gui.py').write_text('old gui')
  (self.root/'workspace/projects').mkdir(parents=True);keep=self.root/'workspace/projects/save.bin';keep.write_bytes(b'save-data')
  m=self.package();self.staged(m);r=apply_pending(self.root)
  self.assertEqual((self.root/'studio/gui.py').read_text(),'#new gui')
  self.assertEqual(keep.read_bytes(),b'save-data');self.assertEqual((Path(r['backup'])/'studio/gui.py').read_text(),'old gui')
  self.assertFalse((self.root/'.studio-updates/pending.json').exists())
 def test_replace_failure_rolls_back(self):
  (self.root/'bootstrap.py').write_text('old bootstrap');m=self.package();self.staged(m)
  original=Path.write_bytes
  def broken(p,data):
   if p.name=='gui.py.update-tmp':raise OSError('simulated disk failure')
   return original(p,data)
  with patch.object(Path,'write_bytes',broken):
   with self.assertRaisesRegex(OSError,'disk failure'):apply_pending(self.root)
  self.assertEqual((self.root/'bootstrap.py').read_text(),'old bootstrap')
  self.assertFalse((self.root/'studio/__main__.py').exists())
  self.assertTrue((self.root/'.studio-updates/pending.json').is_file())
 def test_symlink_destination_rejected(self):
  outside=Path(self.tmp.name)/'outside';outside.mkdir();(self.root/'studio').symlink_to(outside,target_is_directory=True)
  m=self.package();self.staged(m)
  with self.assertRaisesRegex(ValueError,'escapes'):apply_pending(self.root)
  self.assertFalse(list(outside.iterdir()))
 def test_no_pending_is_noop(self):self.assertIsNone(apply_pending(self.root))

if __name__=='__main__':unittest.main()
