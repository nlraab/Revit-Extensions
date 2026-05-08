"""Tests for lib/clash_view/walkthrough_handoff.py.

Pure-data — no Revit. Covers make_pending_fly_to shape, persistence
round-trip, malformed-input handling, viewpoint→camera translation,
and the atomic-write absence of .tmp leftovers.
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


from clash_view import walkthrough_handoff as wh  # noqa: E402


class _TempProjectMixin(object):
    """Redirect pending_path() to a tempdir so tests don't touch the
    real shared root."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._target_dir = os.path.join(self._tmp, "project-hash-abc")
        os.makedirs(self._target_dir)
        target_path = os.path.join(self._target_dir, wh.PENDING_FILE_NAME)

        def fake_path(project_hash):
            return target_path

        self._patch = mock.patch.object(wh, 'pending_path',
                                         side_effect=fake_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


def _viewpoint(position, target, up):
    return {
        "camera": {
            "position": list(position),
            "target":   list(target),
            "up":       list(up),
        }
    }


# ---------------------------------------------------------------------------
# make_pending_fly_to
# ---------------------------------------------------------------------------

class MakePendingTests(unittest.TestCase):

    def test_falls_back_to_saved_viewpoint_when_no_midpoint(self):
        # No clash midpoint → can't compute perspective offset, so we
        # fall back to copying the saved viewpoint verbatim.
        clash = {"id": "clash-42", "seq": 42}
        vp = _viewpoint([1, 2, 3], [4, 5, 6], [0, 0, 1])
        cmd = wh.make_pending_fly_to(clash, vp)
        self.assertEqual(cmd["clash_id"], "clash-42")
        self.assertEqual(cmd["clash_seq"], 42)
        self.assertEqual(cmd["schema_version"], wh.SCHEMA_VERSION)
        self.assertEqual(cmd["camera"]["position"], [1.0, 2.0, 3.0])
        self.assertEqual(cmd["camera"]["target"],   [4.0, 5.0, 6.0])
        self.assertEqual(cmd["camera"]["up"],       [0.0, 0.0, 1.0])
        self.assertIn("T", cmd["queued_at"])

    def test_uses_midpoint_with_perspective_offset(self):
        # When the clash has a midpoint, we should get a perspective-
        # friendly camera position: backed off from the midpoint along
        # the saved viewpoint's view direction by the offset distance.
        # Saved viewpoint at (0,0,0) looking at (1,0,0) → direction = +X.
        # Midpoint at (100, 50, 10). Camera should land 15 ft back in -X
        # from the midpoint, looking AT the midpoint.
        clash = {"id": "x", "seq": 1, "midpoint": [100, 50, 10]}
        vp = _viewpoint([0, 0, 0], [1, 0, 0], [0, 0, 1])
        cmd = wh.make_pending_fly_to(clash, vp)
        # Camera position = midpoint - 15 * (+X) = (85, 50, 10)
        self.assertAlmostEqual(cmd["camera"]["position"][0], 85.0, places=4)
        self.assertAlmostEqual(cmd["camera"]["position"][1], 50.0, places=4)
        self.assertAlmostEqual(cmd["camera"]["position"][2], 10.0, places=4)
        # Look-at point = midpoint
        self.assertEqual(cmd["camera"]["target"], [100.0, 50.0, 10.0])

    def test_perspective_offset_distance_is_15ft(self):
        # Verify the standoff distance. Direction is unit +Y.
        clash = {"id": "x", "midpoint": [0, 0, 0]}
        vp = _viewpoint([0, -1, 0], [0, 0, 0], [0, 0, 1])  # looking at origin from -Y
        cmd = wh.make_pending_fly_to(clash, vp)
        # Direction from camera to target is (0,1,0). Camera should be
        # at midpoint - 15 * (0,1,0) = (0, -15, 0).
        pos = cmd["camera"]["position"]
        dist = (pos[0]**2 + pos[1]**2 + pos[2]**2) ** 0.5
        self.assertAlmostEqual(dist, 15.0, places=4)
        self.assertAlmostEqual(pos[1], -15.0, places=4)

    def test_returns_none_for_missing_viewpoint(self):
        self.assertIsNone(wh.make_pending_fly_to({"id": "x"}, None))
        self.assertIsNone(wh.make_pending_fly_to({"id": "x"}, {}))

    def test_returns_none_for_malformed_camera(self):
        bad = {"camera": {"position": [1, 2, 3]}}  # missing target/up
        self.assertIsNone(wh.make_pending_fly_to({"id": "x"}, bad))

    def test_handles_missing_clash_fields(self):
        # Defensive — clash dict may not have id/seq in pathological cases.
        vp = _viewpoint([1, 2, 3], [4, 5, 6], [0, 0, 1])
        cmd = wh.make_pending_fly_to({}, vp)
        self.assertIsNone(cmd["clash_id"])
        self.assertIsNone(cmd["clash_seq"])


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class PersistenceTests(_TempProjectMixin, unittest.TestCase):

    def test_read_returns_none_when_no_file(self):
        self.assertIsNone(wh.read_pending("phash"))

    def test_write_then_read_round_trip(self):
        cmd = wh.make_pending_fly_to(
            {"id": "x", "seq": 1},
            _viewpoint([1, 2, 3], [4, 5, 6], [0, 0, 1]))
        self.assertTrue(wh.write_pending("phash", cmd))
        loaded = wh.read_pending("phash")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["clash_seq"], 1)
        self.assertEqual(loaded["camera"]["position"], [1.0, 2.0, 3.0])

    def test_clear_removes_file(self):
        cmd = wh.make_pending_fly_to(
            {"id": "x"},
            _viewpoint([1, 2, 3], [4, 5, 6], [0, 0, 1]))
        wh.write_pending("phash", cmd)
        wh.clear_pending("phash")
        self.assertIsNone(wh.read_pending("phash"))

    def test_clear_safe_when_no_file(self):
        # Should not raise.
        wh.clear_pending("phash")

    def test_overwrite_last_write_wins(self):
        cmd1 = wh.make_pending_fly_to(
            {"id": "a", "seq": 1},
            _viewpoint([1, 0, 0], [2, 0, 0], [0, 0, 1]))
        cmd2 = wh.make_pending_fly_to(
            {"id": "b", "seq": 2},
            _viewpoint([10, 0, 0], [11, 0, 0], [0, 0, 1]))
        wh.write_pending("phash", cmd1)
        wh.write_pending("phash", cmd2)
        loaded = wh.read_pending("phash")
        self.assertEqual(loaded["clash_id"], "b")

    def test_corrupt_file_returns_none(self):
        path = wh.pending_path("phash")
        with codecs.open(path, "w", "utf-8") as f:
            f.write("garbage {{{")
        self.assertIsNone(wh.read_pending("phash"))

    def test_atomic_write_no_tmp_leftover(self):
        cmd = wh.make_pending_fly_to(
            {"id": "x"},
            _viewpoint([1, 2, 3], [4, 5, 6], [0, 0, 1]))
        wh.write_pending("phash", cmd)
        leftovers = [f for f in os.listdir(self._target_dir)
                     if f.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_schema_version_written(self):
        cmd = wh.make_pending_fly_to(
            {"id": "x"},
            _viewpoint([1, 2, 3], [4, 5, 6], [0, 0, 1]))
        wh.write_pending("phash", cmd)
        with codecs.open(wh.pending_path("phash"), "r", "utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], wh.SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# viewpoint_to_camera_tuple
# ---------------------------------------------------------------------------

class ViewpointToCameraTupleTests(unittest.TestCase):

    def test_translates_target_to_unit_forward(self):
        # Position at origin, target at (5, 0, 0) → forward = (1, 0, 0).
        cmd = wh.make_pending_fly_to(
            {"id": "x"},
            _viewpoint([0, 0, 0], [5, 0, 0], [0, 0, 1]))
        cam = wh.viewpoint_to_camera_tuple(cmd)
        self.assertEqual(cam[0], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(cam[1][0], 1.0, places=5)
        self.assertAlmostEqual(cam[1][1], 0.0, places=5)
        self.assertAlmostEqual(cam[1][2], 0.0, places=5)
        self.assertEqual(cam[2], [0.0, 0.0, 1.0])

    def test_returns_none_for_zero_length_forward(self):
        # Position == target → forward is zero-length, can't normalize.
        cmd = wh.make_pending_fly_to(
            {"id": "x"},
            _viewpoint([1, 2, 3], [1, 2, 3], [0, 0, 1]))
        self.assertIsNone(wh.viewpoint_to_camera_tuple(cmd))

    def test_returns_none_for_none_input(self):
        self.assertIsNone(wh.viewpoint_to_camera_tuple(None))

    def test_returns_none_for_malformed(self):
        self.assertIsNone(wh.viewpoint_to_camera_tuple({}))


if __name__ == "__main__":
    unittest.main()
