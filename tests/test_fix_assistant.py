# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from studio import fix_assistant as fa

PATCH = """--- a/port/main.cpp
+++ b/port/main.cpp
@@ -1,2 +1,2 @@
 int main() {
-  return 1;
+  return 0;
"""


class FixAssistantTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "port").mkdir()
        (self.dir / "port" / "main.cpp").write_text("int main() {\n  return 1;\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_extract_and_files(self):
        reply = "Cause: bad exit.\n```diff\n" + PATCH + "```\nDone."
        patch = fa.extract_patch(reply)
        self.assertIn("+  return 0;", patch)
        self.assertEqual(fa.patch_files(patch), ["port/main.cpp"])

    def test_unsafe_paths_refused(self):
        for bad in ("--- a/../x\n+++ b/../x\n", "+++ /etc/passwd\n"):
            with self.assertRaises(ValueError):
                fa.patch_files(bad)

    def test_apply_and_undo(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        fix = fa.apply_patch(self.dir, PATCH)
        self.assertIn("return 0", (self.dir / "port" / "main.cpp").read_text())
        self.assertEqual(fa.list_fixes(self.dir), [fix])
        fa.undo(self.dir, fix)
        self.assertIn("return 1", (self.dir / "port" / "main.cpp").read_text())
        self.assertEqual(fa.list_fixes(self.dir), [])

    def test_request_has_no_key(self):
        info = {"name": "Demo", "status": "blocked", "build_report": {"error": "x"}, "build_log": "line", "test": {}}
        messages = fa.build_request(info, "please help")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("please help", messages[1]["content"])

    def test_ask_needs_settings(self):
        with self.assertRaises(ValueError):
            fa.ask({"url": "", "model": "m"}, [])
        with self.assertRaises(ValueError):
            fa.ask({"url": "https://example.invalid/v1", "model": ""}, [])


if __name__ == "__main__":
    unittest.main()
