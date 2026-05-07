"""Tests for lib/clash_view/walkthrough_bookmarks.py.

Pure-data — no Revit. Verifies make_bookmark shape, persistence
round-trip, append + delete + rename helpers, corrupt-file recovery,
and atomic-write absence of .tmp leftovers.
"""

import codecs
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_view import walkthrough_bookmarks as wb  # noqa: E402


class _TempProjectMixin(object):
    """Redirect bookmarks_path() to a tempdir so tests don't touch the
    real shared root. Same pattern as test_clash_filter_presets."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._target_dir = os.path.join(self._tmp, "project-hash-abc")
        os.makedirs(self._target_dir)
        target_path = os.path.join(self._target_dir, wb.BOOKMARKS_FILE_NAME)

        def fake_path(project_hash):
            return target_path

        self._patch = mock.patch.object(wb, 'bookmarks_path',
                                         side_effect=fake_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# make_bookmark
# ---------------------------------------------------------------------------

class MakeBookmarkTests(unittest.TestCase):

    def test_assigns_unique_id_with_bm_prefix(self):
        a = wb.make_bookmark("A", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        b = wb.make_bookmark("B", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        self.assertTrue(a["id"].startswith("bm-"))
        self.assertTrue(b["id"].startswith("bm-"))
        self.assertNotEqual(a["id"], b["id"])

    def test_uses_provided_name(self):
        b = wb.make_bookmark("Lobby", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        self.assertEqual(b["name"], "Lobby")

    def test_strips_whitespace(self):
        b = wb.make_bookmark("  Lobby  ", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        self.assertEqual(b["name"], "Lobby")

    def test_blank_name_falls_back(self):
        b = wb.make_bookmark("   ", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        self.assertEqual(b["name"], "Untitled bookmark")
        b = wb.make_bookmark(None, [0, 0, 0], [1, 0, 0], [0, 0, 1])
        self.assertEqual(b["name"], "Untitled bookmark")

    def test_camera_vectors_stored_as_lists_of_floats(self):
        b = wb.make_bookmark("X", [1, 2, 3], [4, 5, 6], [7, 8, 9])
        self.assertEqual(b["camera"]["position"], [1.0, 2.0, 3.0])
        self.assertEqual(b["camera"]["forward"],  [4.0, 5.0, 6.0])
        self.assertEqual(b["camera"]["up"],       [7.0, 8.0, 9.0])

    def test_created_at_is_iso(self):
        b = wb.make_bookmark("X", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        self.assertIn("T", b["created_at"])
        self.assertTrue(b["created_at"].endswith("Z"))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class PersistenceTests(_TempProjectMixin, unittest.TestCase):

    def test_read_returns_empty_when_no_file(self):
        self.assertEqual(wb.read_bookmarks("phash"), [])

    def test_write_then_read_round_trip(self):
        original = [
            wb.make_bookmark("Lobby",  [10, 20, 5], [1, 0, 0], [0, 0, 1]),
            wb.make_bookmark("Roof",   [10, 20, 60], [0, 1, 0], [0, 0, 1]),
        ]
        wb.write_bookmarks("phash", original)
        loaded = wb.read_bookmarks("phash")
        self.assertEqual(len(loaded), 2)
        self.assertEqual([b["name"] for b in loaded], ["Lobby", "Roof"])
        self.assertEqual(loaded[0]["camera"]["position"], [10.0, 20.0, 5.0])

    def test_append_bookmark(self):
        wb.write_bookmarks("phash", [])
        b = wb.make_bookmark("New", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.append_bookmark("phash", b)
        loaded = wb.read_bookmarks("phash")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "New")

    def test_append_preserves_existing(self):
        a = wb.make_bookmark("A", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a])
        b = wb.make_bookmark("B", [1, 1, 1], [1, 0, 0], [0, 0, 1])
        wb.append_bookmark("phash", b)
        loaded = wb.read_bookmarks("phash")
        self.assertEqual([x["name"] for x in loaded], ["A", "B"])

    def test_delete_removes_by_id(self):
        a = wb.make_bookmark("A", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        b = wb.make_bookmark("B", [1, 1, 1], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a, b])
        self.assertTrue(wb.delete_bookmark("phash", a["id"]))
        remaining = wb.read_bookmarks("phash")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "B")

    def test_delete_nonexistent_returns_false(self):
        wb.write_bookmarks("phash",
                           [wb.make_bookmark("A", [0, 0, 0],
                                              [1, 0, 0], [0, 0, 1])])
        self.assertFalse(wb.delete_bookmark("phash", "does-not-exist"))
        self.assertEqual(len(wb.read_bookmarks("phash")), 1)

    def test_rename_changes_name(self):
        a = wb.make_bookmark("Old", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a])
        self.assertTrue(wb.rename_bookmark("phash", a["id"], "New"))
        loaded = wb.read_bookmarks("phash")
        self.assertEqual(loaded[0]["name"], "New")

    def test_rename_strips_whitespace(self):
        a = wb.make_bookmark("Old", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a])
        wb.rename_bookmark("phash", a["id"], "  New  ")
        self.assertEqual(wb.read_bookmarks("phash")[0]["name"], "New")

    def test_rename_blank_falls_back(self):
        a = wb.make_bookmark("Old", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a])
        wb.rename_bookmark("phash", a["id"], "   ")
        self.assertEqual(wb.read_bookmarks("phash")[0]["name"],
                         "Untitled bookmark")

    def test_rename_nonexistent_returns_false(self):
        a = wb.make_bookmark("A", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a])
        self.assertFalse(wb.rename_bookmark("phash", "nope", "X"))

    def test_corrupt_file_returns_empty(self):
        with codecs.open(wb.bookmarks_path("phash"), "w", "utf-8") as f:
            f.write("garbage {{{")
        self.assertEqual(wb.read_bookmarks("phash"), [])

    def test_atomic_write_no_tmp_leftover(self):
        a = wb.make_bookmark("A", [0, 0, 0], [1, 0, 0], [0, 0, 1])
        wb.write_bookmarks("phash", [a])
        leftovers = [f for f in os.listdir(self._target_dir)
                     if f.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_schema_version_written(self):
        wb.write_bookmarks("phash",
                           [wb.make_bookmark("A", [0, 0, 0],
                                              [1, 0, 0], [0, 0, 1])])
        with codecs.open(wb.bookmarks_path("phash"), "r", "utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], wb.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
