"""Tests for the Revit-independent parts of lib/clash_core/.

These run in CPython 3 (the project's test runtime) and exercise the
foundational data layer without touching the Revit API. Anything that
needs Revit (project.central_model_path, project.project_hash_for, etc.)
is skipped here and verified by hand inside Revit.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


def _fresh_modules():
    """Drop cached clash_core modules so each test gets a fresh import.

    Necessary because clash_core.config caches the APPDATA-derived path at
    function-call time, but if a test-modified env var is read by a previously
    imported module, we'd get cross-test contamination.
    """
    for mod_name in list(sys.modules.keys()):
        if mod_name == "clash_core" or mod_name.startswith("clash_core."):
            del sys.modules[mod_name]


class _AppdataIsolation(unittest.TestCase):
    """Base class that points %APPDATA% at a temp dir for the duration of each test."""

    def setUp(self):
        _fresh_modules()
        self.tmpdir = tempfile.mkdtemp(prefix="dbhms_clash_test_")
        self._orig_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmpdir

    def tearDown(self):
        if self._orig_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._orig_appdata
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _fresh_modules()


class ProjectHashTests(unittest.TestCase):
    def test_normalize_collapses_separators_and_case(self):
        from clash_core import project
        self.assertEqual(
            project.normalize_path(r"T:\Projects\Foo.rvt"),
            project.normalize_path(r"t:/projects/foo.rvt"),
        )

    def test_normalize_strips_whitespace(self):
        from clash_core import project
        self.assertEqual(
            project.normalize_path("  T:/foo.rvt  "),
            project.normalize_path("T:/foo.rvt"),
        )

    def test_normalize_handles_empty(self):
        from clash_core import project
        self.assertEqual(project.normalize_path(""), "")
        self.assertEqual(project.normalize_path(None), "")

    def test_hash_is_stable_across_calls(self):
        from clash_core import project
        h1 = project.hash_path(r"T:\Projects\Foo.rvt")
        h2 = project.hash_path(r"T:\Projects\Foo.rvt")
        self.assertEqual(h1, h2)

    def test_hash_collapses_equivalent_paths(self):
        from clash_core import project
        h_a = project.hash_path(r"T:\Projects\Foo.rvt")
        h_b = project.hash_path(r"t:/projects/foo.rvt  ")
        self.assertEqual(h_a, h_b)

    def test_hash_differs_for_different_paths(self):
        from clash_core import project
        h_a = project.hash_path(r"T:\Projects\Foo.rvt")
        h_b = project.hash_path(r"T:\Projects\Bar.rvt")
        self.assertNotEqual(h_a, h_b)

    def test_hash_is_short_hex(self):
        from clash_core import project
        h = project.hash_path(r"T:\Projects\Foo.rvt")
        self.assertEqual(len(h), project.HASH_LENGTH)
        int(h, 16)  # raises if not hex

    def test_hash_empty_for_empty_path(self):
        from clash_core import project
        self.assertEqual(project.hash_path(""), "")


class ConfigTests(_AppdataIsolation):
    def test_first_run_returns_defaults(self):
        from clash_core import config
        cfg = config.load()
        self.assertIsNone(cfg["shared_root"])
        self.assertIsNone(cfg["user_display_name"])
        self.assertEqual(cfg["warn_threshold"], 2000)

    def test_is_first_run_true_when_no_shared_root(self):
        from clash_core import config
        self.assertTrue(config.is_first_run())

    def test_save_then_load_round_trip(self):
        from clash_core import config
        config.save({
            "shared_root":       "T:/clash_data",
            "user_display_name": "Test User",
            "warn_threshold":    5000,
        })
        cfg = config.load()
        self.assertEqual(cfg["shared_root"], "T:/clash_data")
        self.assertEqual(cfg["user_display_name"], "Test User")
        self.assertEqual(cfg["warn_threshold"], 5000)
        self.assertFalse(config.is_first_run())

    def test_save_writes_pretty_json(self):
        from clash_core import config
        config.save({"shared_root": "T:/foo"})
        with open(config.config_path()) as f:
            text = f.read()
        # indent=2 means newlines and spaces - not just one packed line
        self.assertIn("\n", text)
        self.assertIn('"shared_root"', text)

    def test_save_preserves_unknown_keys(self):
        from clash_core import config
        # Simulate a future schema with an extra field
        config.save({"shared_root": "T:/foo", "future_field": "keep me"})
        cfg = config.load()
        self.assertEqual(cfg.get("future_field"), "keep me")

    def test_corrupt_file_falls_back_to_defaults(self):
        from clash_core import config
        # Write garbage to the config file
        path = config.config_path()
        with open(path, "w") as f:
            f.write("{ not valid json")
        cfg = config.load()
        # Should NOT raise; should return defaults
        self.assertIsNone(cfg["shared_root"])
        self.assertEqual(cfg["warn_threshold"], 2000)

    def test_save_sets_schema_version(self):
        from clash_core import config
        config.save({"shared_root": "T:/foo"})
        cfg = config.load()
        self.assertEqual(cfg["schema_version"], config.SCHEMA_VERSION)


class PersistenceTests(_AppdataIsolation):
    def setUp(self):
        super(PersistenceTests, self).setUp()
        self.shared = os.path.join(self.tmpdir, "shared")
        os.makedirs(self.shared)
        from clash_core import config
        config.save({"shared_root": self.shared})

    def test_paths_root_under_shared_folder(self):
        from clash_core import persistence
        ph = "abcdef123456"
        self.assertTrue(persistence.project_dir(ph).startswith(self.shared))
        self.assertTrue(persistence.global_dir().startswith(self.shared))

    def test_project_dir_is_created_on_demand(self):
        from clash_core import persistence
        ph = "abcdef123456"
        d = persistence.project_dir(ph)
        self.assertTrue(os.path.isdir(d))

    def test_clashes_round_trip(self):
        from clash_core import persistence
        ph = "abcdef123456"
        data = {
            "schema_version": 1,
            "project_hash":   ph,
            "clashes": [{"id": "c1", "status": "Open"}],
        }
        persistence.write_clashes(ph, data)
        loaded = persistence.read_clashes(ph)
        self.assertEqual(loaded["clashes"], data["clashes"])

    def test_atomic_write_does_not_leave_tmp_file(self):
        from clash_core import persistence
        ph = "abcdef123456"
        persistence.write_clashes(ph, {"clashes": []})
        tmp = persistence.clashes_path(ph) + ".tmp"
        self.assertFalse(os.path.exists(tmp), "Stray .tmp file left after write")

    def test_atomic_write_overwrites_stale_tmp(self):
        from clash_core import persistence
        ph = "abcdef123456"
        persistence.write_clashes(ph, {"clashes": [{"id": "c1"}]})
        # Manually create a stale .tmp file (would have been left by a crash)
        stale = persistence.clashes_path(ph) + ".tmp"
        with open(stale, "w") as f:
            f.write("garbage")
        # Subsequent writes succeed
        persistence.write_clashes(ph, {"clashes": [{"id": "c2"}]})
        loaded = persistence.read_clashes(ph)
        self.assertEqual(loaded["clashes"][0]["id"], "c2")

    def test_read_missing_returns_empty_schema(self):
        from clash_core import persistence
        loaded = persistence.read_clashes("nonexistenthash")
        self.assertEqual(loaded["clashes"], [])
        self.assertEqual(loaded["project_hash"], "nonexistenthash")
        self.assertEqual(loaded["schema_version"], persistence.SCHEMA_VERSION)

    def test_read_corrupt_returns_default(self):
        from clash_core import persistence
        ph = "abcdef123456"
        # clashes_path() goes through project_dir() which creates the folder,
        # so we don't need to mkdir ourselves.
        path = persistence.clashes_path(ph)
        with open(path, "w") as f:
            f.write("{ not json")
        loaded = persistence.read_clashes(ph)
        self.assertEqual(loaded["clashes"], [])

    def test_global_test_library_round_trip(self):
        from clash_core import persistence
        data = {
            "$schema_version": 1,
            "tests": [{"id": "t1", "name": "Foo vs Bar"}],
        }
        persistence.write_global_test_library(data)
        loaded = persistence.read_global_test_library()
        self.assertEqual(loaded["tests"][0]["name"], "Foo vs Bar")

    def test_overrides_round_trip(self):
        from clash_core import persistence
        ph = "abcdef123456"
        data = {
            "schema_version": 1,
            "disabled_test_ids": ["t1"],
            "custom_tests": [{"id": "ct1"}],
        }
        persistence.write_overrides(ph, data)
        loaded = persistence.read_overrides(ph)
        self.assertEqual(loaded["disabled_test_ids"], ["t1"])

    def test_project_meta_round_trip(self):
        from clash_core import persistence
        ph = "abcdef123456"
        data = {
            "schema_version": 1,
            "project_hash":   ph,
            "display_name":   "Sample Project",
            "disciplines":    ["Mechanical", "Electrical"],
            "link_role_map":  {"ArchModel.rvt": "Architectural"},
        }
        persistence.write_project_meta(ph, data)
        loaded = persistence.read_project_meta(ph)
        self.assertEqual(loaded["display_name"], "Sample Project")
        self.assertEqual(loaded["link_role_map"], {"ArchModel.rvt": "Architectural"})


class PersistenceWithoutSharedRootTests(_AppdataIsolation):
    """Persistence ops fail loudly when no shared folder is configured."""

    def test_project_dir_raises(self):
        from clash_core import persistence
        with self.assertRaises(persistence.SharedFolderNotConfigured):
            persistence.project_dir("abcdef123456")

    def test_global_dir_raises(self):
        from clash_core import persistence
        with self.assertRaises(persistence.SharedFolderNotConfigured):
            persistence.global_dir()


class ModelsTests(unittest.TestCase):
    def test_make_element_ref(self):
        from clash_core import models
        ref = models.make_element_ref(
            source=models.ElementSource.HOST,
            element_id=12345,
            category="OST_DuctCurves",
            name='Round Duct 12"',
        )
        self.assertEqual(ref["source"], "host")
        self.assertEqual(ref["element_id"], 12345)
        self.assertEqual(ref["category"], "OST_DuctCurves")
        self.assertIsNone(ref["link_doc_title"])

    def test_make_clash_test(self):
        from clash_core import models
        t = models.make_clash_test(
            name="Mech vs Plumbing",
            kind=models.ClashKind.HARD,
            set_a={"source": "host", "categories": ["OST_DuctCurves"]},
            set_b={"source": "host", "categories": ["OST_PipeCurves"]},
            tolerance_inches=0.0,
            default_assignee=models.Discipline.MECHANICAL,
        )
        self.assertIn("id", t)
        self.assertEqual(t["kind"], "hard")
        self.assertEqual(t["default_assignee"], "Mechanical")
        self.assertEqual(t["tolerance_inches"], 0.0)

    def test_make_clash_defaults(self):
        from clash_core import models
        ref_a = models.make_element_ref("host", 1)
        ref_b = models.make_element_ref("host", 2)
        c = models.make_clash("test-id", ref_a, ref_b, [1.0, 2.0, 3.0])
        self.assertIn("id", c)
        self.assertEqual(c["status"], "Open")
        self.assertEqual(c["kind"], "hard")
        self.assertEqual(c["midpoint"], [1.0, 2.0, 3.0])
        self.assertEqual(c["comments"], [])
        self.assertEqual(c["history"], [])

    def test_make_clash_xyz_accepts_revit_xyz(self):
        from clash_core import models

        class FakeXYZ(object):
            X, Y, Z = 4.0, 5.0, 6.0

        c = models.make_clash("test-id", {}, {}, FakeXYZ())
        self.assertEqual(c["midpoint"], [4.0, 5.0, 6.0])

    def test_make_comment_has_timestamp(self):
        from clash_core import models
        c = models.make_comment("Nathan", "looks good")
        self.assertEqual(c["author"], "Nathan")
        self.assertEqual(c["body"], "looks good")
        self.assertTrue(c["at"].endswith("Z"))

    def test_make_viewpoint_with_section_box(self):
        from clash_core import models
        vp = models.make_viewpoint(
            camera_position=[1, 2, 3],
            target=[4, 5, 6],
            up_vector=[0, 0, 1],
            section_box=([0, 0, 0], [10, 10, 10]),
            snapshot_relpath="viewpoints/foo.png",
            captured_by="Nathan",
        )
        self.assertEqual(vp["camera"]["position"], [1.0, 2.0, 3.0])
        self.assertEqual(vp["section_box"]["max"], [10.0, 10.0, 10.0])
        self.assertEqual(vp["snapshot_relpath"], "viewpoints/foo.png")

    def test_make_history_entry(self):
        from clash_core import models
        e = models.make_history_entry(
            author="Nathan",
            action="status_changed",
            before="Open",
            after="Reviewed",
        )
        self.assertEqual(e["action"], "status_changed")
        self.assertEqual(e["before"], "Open")
        self.assertEqual(e["after"], "Reviewed")

    def test_status_constants_match_documented_set(self):
        from clash_core import models
        self.assertEqual(set(models.ClashStatus.ALL),
                         {"Open", "Reviewed", "Approved", "Resolved"})

    def test_discipline_constants_complete(self):
        from clash_core import models
        self.assertIn("Mechanical",      models.Discipline.ALL)
        self.assertIn("Electrical",      models.Discipline.ALL)
        self.assertIn("Plumbing",        models.Discipline.ALL)
        self.assertIn("Fire Protection", models.Discipline.ALL)
        self.assertIn("Technology",      models.Discipline.ALL)
        self.assertIn("Architectural",   models.Discipline.ALL)
        self.assertIn("Structural",      models.Discipline.ALL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
