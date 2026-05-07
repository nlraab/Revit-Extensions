"""Tests for lib/clash_core/filter_presets.py.

Pure-data — no Revit, no WPF. Verifies:
  * make_preset shape (id, name, defaults, created_at)
  * Built-in preset list has the expected entries with builtin=True
  * Read/write round trip
  * Append + delete helpers
  * Corrupt-file recovery returns empty list
  * Persistence is atomic (no .tmp leftover on success)
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


from clash_core import filter_presets  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixture: a fresh tempdir as %APPDATA% per test
# ---------------------------------------------------------------------------

class _TempAppDataMixin(object):
    """Redirect filter_presets.presets_path() to a tempdir so tests don't
    touch the real %APPDATA%/dbHMS_clash/filter_presets.json.

    Patches presets_path itself (rather than the imported _appdata_root
    binding inside filter_presets) — patching the imported binding via
    string path was unreliable enough that the real APPDATA file got
    written during early test runs. Patching the function directly with
    mock.patch.object is unambiguous: every call to presets_path inside
    filter_presets goes through our mock.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        target_dir = os.path.join(self._tmp, 'dbHMS_clash')
        os.makedirs(target_dir)
        target_path = os.path.join(target_dir, filter_presets.PRESETS_FILE_NAME)

        def fake_presets_path():
            return target_path

        self._target_dir = target_dir
        self._patch = mock.patch.object(
            filter_presets, 'presets_path', side_effect=fake_presets_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# make_preset
# ---------------------------------------------------------------------------

class MakePresetTests(unittest.TestCase):

    def test_assigns_unique_id_with_preset_prefix(self):
        a = filter_presets.make_preset("A")
        b = filter_presets.make_preset("B")
        self.assertTrue(a["id"].startswith("preset-"))
        self.assertTrue(b["id"].startswith("preset-"))
        self.assertNotEqual(a["id"], b["id"])

    def test_uses_provided_name(self):
        p = filter_presets.make_preset("My open clashes")
        self.assertEqual(p["name"], "My open clashes")

    def test_strips_whitespace_from_name(self):
        p = filter_presets.make_preset("  Plumbing  ")
        self.assertEqual(p["name"], "Plumbing")

    def test_blank_name_falls_back_to_default(self):
        p = filter_presets.make_preset("   ")
        self.assertEqual(p["name"], "Untitled preset")
        p2 = filter_presets.make_preset(None)
        self.assertEqual(p2["name"], "Untitled preset")

    def test_marked_as_not_builtin(self):
        p = filter_presets.make_preset("X")
        self.assertFalse(p["builtin"])

    def test_trades_default_to_none(self):
        p = filter_presets.make_preset("X")
        self.assertIsNone(p["trades"])
        self.assertIsNone(p["statuses"])

    def test_trades_list_preserved(self):
        p = filter_presets.make_preset("X", trades=["Mechanical", "Plumbing"])
        self.assertEqual(p["trades"], ["Mechanical", "Plumbing"])

    def test_test_defaults_to_all_tests_sentinel(self):
        p = filter_presets.make_preset("X")
        self.assertEqual(p["test"], "(All tests)")

    def test_search_defaults_to_empty(self):
        p = filter_presets.make_preset("X")
        self.assertEqual(p["search"], "")

    def test_created_at_is_iso(self):
        p = filter_presets.make_preset("X")
        self.assertIn("T", p["created_at"])
        self.assertTrue(p["created_at"].endswith("Z"))


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

class BuiltInPresetTests(unittest.TestCase):

    def test_three_built_in_presets(self):
        # Active / Mechanical / Resolved
        self.assertEqual(len(filter_presets.BUILT_IN_PRESETS), 3)

    def test_all_marked_as_builtin(self):
        for p in filter_presets.BUILT_IN_PRESETS:
            self.assertTrue(p["builtin"], "preset {!r} should have builtin=True"
                            .format(p["name"]))

    def test_all_have_required_fields(self):
        required = ("id", "name", "trades", "statuses", "test", "search",
                    "builtin")
        for p in filter_presets.BUILT_IN_PRESETS:
            for key in required:
                self.assertIn(key, p,
                              "preset {!r} missing field {!r}"
                              .format(p["name"], key))

    def test_unique_ids(self):
        ids = [p["id"] for p in filter_presets.BUILT_IN_PRESETS]
        self.assertEqual(len(ids), len(set(ids)),
                         "built-in presets should have unique ids")


# ---------------------------------------------------------------------------
# Persistence — read / write / append / delete
# ---------------------------------------------------------------------------

class PersistenceTests(_TempAppDataMixin, unittest.TestCase):

    def test_read_returns_empty_when_no_file_exists(self):
        self.assertEqual(filter_presets.read_user_presets(), [])

    def test_write_then_read_round_trip(self):
        original = [
            filter_presets.make_preset("A", trades=["Mechanical"]),
            filter_presets.make_preset("B", statuses=["Open"]),
        ]
        filter_presets.write_user_presets(original)
        round_tripped = filter_presets.read_user_presets()
        self.assertEqual(len(round_tripped), 2)
        names = [p["name"] for p in round_tripped]
        self.assertIn("A", names)
        self.assertIn("B", names)

    def test_append_user_preset(self):
        filter_presets.write_user_presets([])
        new_preset = filter_presets.make_preset("New")
        filter_presets.append_user_preset(new_preset)
        loaded = filter_presets.read_user_presets()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "New")

    def test_append_preserves_existing(self):
        a = filter_presets.make_preset("A")
        filter_presets.write_user_presets([a])
        b = filter_presets.make_preset("B")
        filter_presets.append_user_preset(b)
        loaded = filter_presets.read_user_presets()
        self.assertEqual(len(loaded), 2)
        self.assertEqual([p["name"] for p in loaded], ["A", "B"])

    def test_delete_removes_by_id(self):
        a = filter_presets.make_preset("A")
        b = filter_presets.make_preset("B")
        filter_presets.write_user_presets([a, b])
        removed = filter_presets.delete_user_preset(a["id"])
        self.assertTrue(removed)
        remaining = filter_presets.read_user_presets()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "B")

    def test_delete_nonexistent_returns_false(self):
        a = filter_presets.make_preset("A")
        filter_presets.write_user_presets([a])
        removed = filter_presets.delete_user_preset("does-not-exist")
        self.assertFalse(removed)
        # And the original preset is still there
        self.assertEqual(len(filter_presets.read_user_presets()), 1)

    def test_corrupt_file_returns_empty_list(self):
        # Write garbage to the presets file directly.
        with codecs.open(filter_presets.presets_path(), "w", "utf-8") as f:
            f.write("not valid json {{{")
        self.assertEqual(filter_presets.read_user_presets(), [])

    def test_atomic_write_no_tmp_leftover(self):
        a = filter_presets.make_preset("A")
        filter_presets.write_user_presets([a])
        leftovers = [f for f in os.listdir(self._target_dir)
                     if f.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_schema_version_written(self):
        filter_presets.write_user_presets([filter_presets.make_preset("A")])
        with codecs.open(filter_presets.presets_path(), "r", "utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"],
                         filter_presets.SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# all_presets — combined built-in + user list
# ---------------------------------------------------------------------------

class AllPresetsTests(_TempAppDataMixin, unittest.TestCase):

    def test_includes_built_ins_when_no_user_presets(self):
        all_p = filter_presets.all_presets()
        self.assertEqual(len(all_p), len(filter_presets.BUILT_IN_PRESETS))

    def test_built_ins_appear_first(self):
        filter_presets.write_user_presets([
            filter_presets.make_preset("My save"),
        ])
        all_p = filter_presets.all_presets()
        # First entries are built-ins; user save appears after.
        for i, builtin in enumerate(filter_presets.BUILT_IN_PRESETS):
            self.assertEqual(all_p[i]["id"], builtin["id"])
        self.assertEqual(all_p[-1]["name"], "My save")


if __name__ == "__main__":
    unittest.main()
