# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from studio import community_packs as cp
from studio import patch_packs as pp


def pack_zip(pack_id="kz3-community", version="1.0"):
    data = {"schema": 1, "id": pack_id, "name": "KZ3 community", "version": version, "author": "someone",
            "games": {"title_ids": ["BCUS98234"]}, "env": {"KZ3_X": "1"}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{pack_id}/pack.json", json.dumps(data))
    return buf.getvalue()


class CommunityPackTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.blob = pack_zip()
        self.entry = {"id": "kz3-community", "name": "KZ3 community", "version": "1.0", "author": "someone",
                      "title_ids": ["BCUS98234"], "url": "https://example.com/kz3.zip",
                      "sha256": hashlib.sha256(self.blob).hexdigest()}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_index_skips_bad_entries(self):
        index = {"packs": [self.entry, {"id": "x"}, dict(self.entry, url="http://insecure/x.zip")]}
        with mock.patch.object(cp, "_download", return_value=json.dumps(index).encode()):
            good, bad = cp.fetch_index()
        self.assertEqual([e["id"] for e in good], ["kz3-community"])
        self.assertEqual(len(bad), 2)

    def test_install_checks_checksum_and_id(self):
        with mock.patch.object(cp, "_download", return_value=self.blob):
            self.assertEqual(cp.install_entry(self.home, self.entry)["id"], "kz3-community")
        with mock.patch.object(cp, "_download", return_value=self.blob + b"x"):
            with self.assertRaises(pp.PackError):
                cp.install_entry(self.home, self.entry, replace=True)
        other = pack_zip("another-pack")
        wrong = dict(self.entry, sha256=hashlib.sha256(other).hexdigest())
        with mock.patch.object(cp, "_download", return_value=other):
            with self.assertRaises(pp.PackError):
                cp.install_entry(self.home, wrong)
        self.assertIsNone(pp.find(self.home, "another-pack"), "mismatched pack left installed")

    def test_export_round_trip(self):
        with mock.patch.object(cp, "_download", return_value=self.blob):
            pack = cp.install_entry(self.home, self.entry)
        path, entry = cp.export_zip(pack, self.home / "out")
        self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(entry["title_ids"], ["BCUS98234"])
        pp.remove(self.home, pack["id"])
        self.assertEqual(pp.install(self.home, path)["id"], "kz3-community")

    def test_library_sync_installs_updates_and_respects_own_packs(self):
        import types
        analysis = self.home / "analysis.json"
        analysis.write_text(json.dumps({"metadata": {"TITLE_ID": "BCUS98234"}}))
        studio = types.SimpleNamespace(home=self.home, state={"settings": {}, "projects": [
            {"name": "Killzone 3", "last_analysis": str(analysis)}]})
        other_blob = pack_zip("other-game")
        other = dict(self.entry, id="other-game", name="Other", title_ids=["BLES00001"],
                     sha256=hashlib.sha256(other_blob).hexdigest(), url="https://example.com/other.zip")
        blobs = {"https://example.com/kz3.zip": self.blob, "https://example.com/other.zip": other_blob}
        def fake(url, limit):
            if url == cp.INDEX_URL:
                return json.dumps({"schema": 1, "packs": index}).encode()
            return blobs[url]
        index = [self.entry, other]
        with mock.patch.object(cp, "_download", side_effect=fake):
            result = cp.sync(studio, scope="mine")
        self.assertEqual(result["installed"], ["KZ3 community"])          # only the pack for my game
        self.assertIsNone(pp.find(self.home, "other-game"))
        self.assertTrue(studio.state["settings"]["community_last_sync"])
        rows, _ = cp.library(studio)
        self.assertEqual({r["id"]: r["status"] for r in rows}, {"kz3-community": "installed", "other-game": "not installed"})
        self.assertEqual(rows[0]["your_games"], ["Killzone 3"])
        # the author releases 2.0: it updates automatically
        new_blob = pack_zip(version="2.0")
        blobs["https://example.com/kz3-2.zip"] = new_blob
        index[0] = dict(self.entry, version="2.0", url="https://example.com/kz3-2.zip",
                        sha256=hashlib.sha256(new_blob).hexdigest())
        with mock.patch.object(cp, "_download", side_effect=fake):
            result = cp.sync(studio, scope="mine")
        self.assertEqual(result["updated"], ["KZ3 community"])
        self.assertEqual(pp.find(self.home, "kz3-community")["version"], "2.0")
        # a pack the player made with the same id is never replaced
        pp.remove(self.home, "kz3-community")
        own = self.home / "own"; own.mkdir()
        (own / "pack.json").write_text(json.dumps({"schema": 1, "id": "kz3-community", "name": "Mine", "version": "0.1",
                                                   "games": {"title_ids": ["BCUS98234"]}, "env": {"A": "1"}}))
        pp.install(self.home, own)
        with mock.patch.object(cp, "_download", side_effect=fake):
            result = cp.sync(studio, scope="all")
        self.assertEqual(pp.find(self.home, "kz3-community")["name"], "Mine")
        self.assertEqual(result["installed"], ["Other"])
        self.assertIn("Community library", cp.describe(result))

    def test_matches_games(self):
        self.assertEqual(cp.matches_games(self.entry, [("Killzone 3", "BCUS98234"), ("Other", "BLES1")]), ["Killzone 3"])


if __name__ == "__main__":
    unittest.main()
