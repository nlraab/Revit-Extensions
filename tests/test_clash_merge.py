"""Tests for lib/clash_core/identity.py and lib/clash_core/merge.py.

Pure-data modules - no Revit needed. Verifies the diff/merge invariants
that protect a user's comments + history across re-runs of detection.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


def _ref(source, eid, name=None):
    return {
        "source": source,
        "element_id": eid,
        "category": None,
        "name": name,
        "link_doc_title": None,
    }


def _raw(test_id, ref_a, ref_b, midpoint, kind="hard", assignee="Mechanical"):
    return {
        "test_id": test_id,
        "kind": kind,
        "ref_a": ref_a,
        "ref_b": ref_b,
        "midpoint": midpoint,
        "default_assignee": assignee,
    }


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

class FingerprintTests(unittest.TestCase):

    def test_stable_across_calls(self):
        from clash_core.identity import clash_fingerprint
        a = _ref("host", 100)
        b = _ref("host", 200)
        f1 = clash_fingerprint("test-1", a, b, [1.0, 2.0, 3.0])
        f2 = clash_fingerprint("test-1", a, b, [1.0, 2.0, 3.0])
        self.assertEqual(f1, f2)

    def test_symmetric_swap_of_refs(self):
        """Swapping ref_a and ref_b yields the same fingerprint."""
        from clash_core.identity import clash_fingerprint
        a = _ref("host", 100)
        b = _ref("host", 200)
        f_ab = clash_fingerprint("test-1", a, b, [1.0, 2.0, 3.0])
        f_ba = clash_fingerprint("test-1", b, a, [1.0, 2.0, 3.0])
        self.assertEqual(f_ab, f_ba)

    def test_different_test_ids_differ(self):
        from clash_core.identity import clash_fingerprint
        a = _ref("host", 100)
        b = _ref("host", 200)
        f1 = clash_fingerprint("test-1", a, b, [1.0, 2.0, 3.0])
        f2 = clash_fingerprint("test-2", a, b, [1.0, 2.0, 3.0])
        self.assertNotEqual(f1, f2)

    def test_different_elements_differ(self):
        from clash_core.identity import clash_fingerprint
        f1 = clash_fingerprint("t", _ref("host", 100), _ref("host", 200), [1, 2, 3])
        f2 = clash_fingerprint("t", _ref("host", 100), _ref("host", 201), [1, 2, 3])
        self.assertNotEqual(f1, f2)

    def test_host_vs_link_differs_from_host_vs_host(self):
        from clash_core.identity import clash_fingerprint
        f1 = clash_fingerprint("t", _ref("host", 100), _ref("host", 200), [1, 2, 3])
        f2 = clash_fingerprint("t",
                               _ref("host", 100),
                               _ref("link:Architectural", 200),
                               [1, 2, 3])
        self.assertNotEqual(f1, f2)

    def test_close_midpoints_share_fingerprint(self):
        """Midpoints within the spatial bucket should hash to the same fingerprint."""
        from clash_core.identity import clash_fingerprint
        a, b = _ref("host", 100), _ref("host", 200)
        f1 = clash_fingerprint("t", a, b, [1.0, 2.0, 3.0])
        f2 = clash_fingerprint("t", a, b, [1.1, 2.05, 3.2])  # < 1 ft shift
        self.assertEqual(f1, f2)

    def test_far_midpoints_differ(self):
        """Same element pair clashing far apart should produce different fingerprints
        (e.g. a long pipe crossing a wall twice = two distinct clashes)."""
        from clash_core.identity import clash_fingerprint
        a, b = _ref("host", 100), _ref("host", 200)
        f1 = clash_fingerprint("t", a, b, [1.0, 2.0, 3.0])
        f2 = clash_fingerprint("t", a, b, [50.0, 2.0, 3.0])
        self.assertNotEqual(f1, f2)


# ---------------------------------------------------------------------------
# merge_runs
# ---------------------------------------------------------------------------

class MergeRunsTests(unittest.TestCase):

    def test_first_run_all_new(self):
        from clash_core.merge import merge_runs
        raw = [
            _raw("t1", _ref("host", 1), _ref("host", 2), [0, 0, 0]),
            _raw("t1", _ref("host", 3), _ref("host", 4), [10, 0, 0]),
        ]
        merged, summary = merge_runs([], raw, run_iso="2026-05-05T10:00:00Z")
        self.assertEqual(len(merged), 2)
        self.assertEqual(summary["new"], 2)
        self.assertEqual(summary["persisting"], 0)
        self.assertEqual(summary["auto_resolved"], 0)
        for c in merged:
            self.assertEqual(c["status"], "Open")
            self.assertEqual(c["first_seen_run"], "2026-05-05T10:00:00Z")
            self.assertEqual(c["last_seen_run"],  "2026-05-05T10:00:00Z")
            self.assertIn("fingerprint", c)
            self.assertIn("seq", c)
            self.assertEqual(len(c["history"]), 1)
            self.assertEqual(c["history"][0]["action"], "detected")

    def test_seq_numbers_are_sequential_starting_from_one(self):
        from clash_core.merge import merge_runs
        raw = [
            _raw("t1", _ref("host", 1), _ref("host", 2), [0, 0, 0]),
            _raw("t1", _ref("host", 3), _ref("host", 4), [10, 0, 0]),
            _raw("t1", _ref("host", 5), _ref("host", 6), [20, 0, 0]),
        ]
        merged, _ = merge_runs([], raw)
        seqs = sorted(c["seq"] for c in merged)
        self.assertEqual(seqs, [1, 2, 3])

    def test_seq_numbers_continue_after_existing(self):
        from clash_core.merge import merge_runs
        existing = [{
            "id": "old", "seq": 7, "fingerprint": "deadbeef",
            "test_id": "t1", "kind": "hard", "status": "Open",
            "ref_a": _ref("host", 99), "ref_b": _ref("host", 100),
            "midpoint": [99, 99, 99],
            "first_seen_run": "x", "last_seen_run": "x",
            "comments": [], "viewpoints": [], "history": [],
        }]
        raw = [_raw("t1", _ref("host", 1), _ref("host", 2), [0, 0, 0])]
        merged, _ = merge_runs(existing, raw)
        new_clash = next(c for c in merged if c["seq"] != 7)
        self.assertEqual(new_clash["seq"], 8)

    def test_persisting_preserves_comments_and_history(self):
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        midpoint = [0.0, 0.0, 0.0]
        fp = clash_fingerprint("t1", ref_a, ref_b, midpoint)
        existing = [{
            "id": "preserve-me",
            "seq": 1,
            "fingerprint": fp,
            "test_id": "t1", "kind": "hard", "status": "Reviewed",
            "ref_a": ref_a, "ref_b": ref_b,
            "midpoint": midpoint,
            "first_seen_run": "2026-04-01T00:00:00Z",
            "last_seen_run":  "2026-04-01T00:00:00Z",
            "comments": [{"author": "Nathan", "at": "2026-04-01T01:00:00Z",
                          "body": "looking at this"}],
            "viewpoints": [],
            "history": [{"author": "Nathan", "at": "2026-04-01T00:00:00Z",
                         "action": "detected"}],
        }]
        raw = [_raw("t1", ref_a, ref_b, midpoint)]
        merged, summary = merge_runs(existing, raw, run_iso="2026-05-05T10:00:00Z")

        self.assertEqual(len(merged), 1)
        self.assertEqual(summary["persisting"], 1)
        self.assertEqual(summary["new"], 0)
        self.assertEqual(merged[0]["id"], "preserve-me")
        self.assertEqual(merged[0]["seq"], 1)
        self.assertEqual(merged[0]["status"], "Reviewed")  # unchanged
        self.assertEqual(len(merged[0]["comments"]), 1)
        self.assertEqual(merged[0]["comments"][0]["body"], "looking at this")
        self.assertEqual(merged[0]["last_seen_run"], "2026-05-05T10:00:00Z")
        self.assertEqual(merged[0]["first_seen_run"], "2026-04-01T00:00:00Z")

    def test_disappeared_clash_auto_resolves_open_one(self):
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        midpoint = [0.0, 0.0, 0.0]
        fp = clash_fingerprint("t1", ref_a, ref_b, midpoint)
        existing = [{
            "id": "old", "seq": 1, "fingerprint": fp,
            "test_id": "t1", "kind": "hard", "status": "Open",
            "ref_a": ref_a, "ref_b": ref_b, "midpoint": midpoint,
            "first_seen_run": "x", "last_seen_run": "x",
            "comments": [], "viewpoints": [], "history": [],
        }]
        merged, summary = merge_runs(existing, [], run_iso="2026-05-05T10:00:00Z")

        self.assertEqual(summary["auto_resolved"], 1)
        self.assertEqual(merged[0]["status"], "Resolved")
        self.assertEqual(merged[0]["history"][-1]["action"], "auto_resolved")
        self.assertEqual(merged[0]["history"][-1]["before"], "Open")
        self.assertEqual(merged[0]["history"][-1]["after"], "Resolved")

    def test_disappeared_already_resolved_does_not_log_again(self):
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        midpoint = [0.0, 0.0, 0.0]
        fp = clash_fingerprint("t1", ref_a, ref_b, midpoint)
        existing = [{
            "id": "old", "seq": 1, "fingerprint": fp,
            "test_id": "t1", "kind": "hard", "status": "Resolved",
            "ref_a": ref_a, "ref_b": ref_b, "midpoint": midpoint,
            "first_seen_run": "x", "last_seen_run": "x",
            "comments": [], "viewpoints": [],
            "history": [{"author": "n", "at": "x", "action": "resolved"}],
        }]
        merged, summary = merge_runs(existing, [])
        self.assertEqual(summary["auto_resolved"], 0)
        self.assertEqual(len(merged[0]["history"]), 1)  # unchanged

    def test_reappearing_resolved_clash_reopens(self):
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        midpoint = [0.0, 0.0, 0.0]
        fp = clash_fingerprint("t1", ref_a, ref_b, midpoint)
        existing = [{
            "id": "comeback", "seq": 1, "fingerprint": fp,
            "test_id": "t1", "kind": "hard", "status": "Resolved",
            "ref_a": ref_a, "ref_b": ref_b, "midpoint": midpoint,
            "first_seen_run": "x", "last_seen_run": "x",
            "comments": [], "viewpoints": [],
            "history": [],
        }]
        raw = [_raw("t1", ref_a, ref_b, midpoint)]
        merged, summary = merge_runs(existing, raw)

        self.assertEqual(summary["reopened"], 1)
        self.assertEqual(summary["persisting"], 1)
        self.assertEqual(merged[0]["status"], "Open")
        self.assertEqual(merged[0]["history"][-1]["action"], "reopened")
        self.assertEqual(merged[0]["history"][-1]["before"], "Resolved")
        self.assertEqual(merged[0]["history"][-1]["after"], "Open")

    def test_midpoint_drift_within_bucket_still_persists(self):
        """A clash that moves a few inches between runs is still the same clash."""
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        old_mid = [10.0, 5.0, 3.0]
        new_mid = [10.2, 5.1, 3.0]  # < 1 ft drift
        fp_old = clash_fingerprint("t1", ref_a, ref_b, old_mid)
        existing = [{
            "id": "stable", "seq": 1, "fingerprint": fp_old,
            "test_id": "t1", "kind": "hard", "status": "Open",
            "ref_a": ref_a, "ref_b": ref_b, "midpoint": old_mid,
            "first_seen_run": "x", "last_seen_run": "x",
            "comments": [], "viewpoints": [], "history": [],
        }]
        raw = [_raw("t1", ref_a, ref_b, new_mid)]
        merged, summary = merge_runs(existing, raw)

        self.assertEqual(summary["persisting"], 1)
        self.assertEqual(summary["new"], 0)
        self.assertEqual(merged[0]["midpoint"], new_mid)  # updated to current

    def test_midpoint_far_apart_is_new_clash(self):
        """Same pair clashing in two different spots = two clashes (not just one)."""
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        existing_mid = [0.0, 0.0, 0.0]
        new_mid = [50.0, 0.0, 0.0]
        fp_old = clash_fingerprint("t1", ref_a, ref_b, existing_mid)
        existing = [{
            "id": "first-spot", "seq": 1, "fingerprint": fp_old,
            "test_id": "t1", "kind": "hard", "status": "Open",
            "ref_a": ref_a, "ref_b": ref_b, "midpoint": existing_mid,
            "first_seen_run": "x", "last_seen_run": "x",
            "comments": [], "viewpoints": [], "history": [],
        }]
        # Both: old spot still firing AND a new spot
        raw = [
            _raw("t1", ref_a, ref_b, existing_mid),
            _raw("t1", ref_a, ref_b, new_mid),
        ]
        merged, summary = merge_runs(existing, raw)

        self.assertEqual(summary["persisting"], 1)
        self.assertEqual(summary["new"], 1)
        self.assertEqual(len(merged), 2)

    def test_dedupes_repeated_raw_clashes(self):
        """If detection somehow reports the same logical clash twice, we keep one."""
        from clash_core.merge import merge_runs
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        raw = [
            _raw("t1", ref_a, ref_b, [0, 0, 0]),
            _raw("t1", ref_a, ref_b, [0, 0, 0]),
            _raw("t1", ref_b, ref_a, [0, 0, 0]),  # swapped order = same fingerprint
        ]
        merged, summary = merge_runs([], raw)
        self.assertEqual(len(merged), 1)
        self.assertEqual(summary["new"], 1)

    def test_measurement_fields_survive_merge_new_and_persisting(self):
        """gap_inches + closest points + contact flags must reach the persisted
        record for NEW clashes, refresh on PERSISTING ones, and stay None-keyed
        (no KeyError) on hard rows that never had them."""
        from clash_core import merge

        raw_soft = _raw("t1", _ref("host", 1), _ref("host", 2), [0, 0, 0], kind="soft")
        raw_soft.update({
            "gap_inches": 0.9,
            "closest_point_a": [1.0, 2.0, 3.0],
            "closest_point_b": [1.0, 2.0, 3.075],
            "is_contact": False,
            "gap_method": "mesh",
        })
        merged, summary = merge.merge_runs([], [raw_soft], run_iso="2026-01-01T00:00:00Z")
        self.assertEqual(summary["new"], 1)
        rec = merged[0]
        self.assertEqual(rec["gap_inches"], 0.9)
        self.assertEqual(rec["closest_point_a"], [1.0, 2.0, 3.0])
        self.assertEqual(rec["closest_point_b"], [1.0, 2.0, 3.075])
        self.assertIs(rec["is_contact"], False)
        self.assertEqual(rec["gap_method"], "mesh")

        # second run: same fingerprint, tighter gap -> refreshed in place
        raw_again = dict(raw_soft)
        raw_again.update({"gap_inches": 0.5, "closest_point_a": [1.0, 2.0, 3.01]})
        merged2, summary2 = merge.merge_runs(merged, [raw_again], run_iso="2026-01-02T00:00:00Z")
        self.assertEqual(summary2["persisting"], 1)
        self.assertEqual(merged2[0]["gap_inches"], 0.5)
        self.assertEqual(merged2[0]["closest_point_a"], [1.0, 2.0, 3.01])

        # hard raw without measurement fields: keys exist as None, no KeyError
        raw_hard = _raw("t1", _ref("host", 5), _ref("host", 6), [9, 9, 9], kind="hard")
        merged3, _ = merge.merge_runs([], [raw_hard], run_iso="2026-01-01T00:00:00Z")
        for k in ("gap_inches", "closest_point_a", "closest_point_b",
                  "is_contact", "gap_method"):
            self.assertIn(k, merged3[0])
            self.assertIsNone(merged3[0][k])

    def test_does_not_mutate_input_old_clashes(self):
        """merge_runs should deep-copy old clashes, not mutate them in place."""
        from clash_core.merge import merge_runs
        from clash_core.identity import clash_fingerprint
        ref_a, ref_b = _ref("host", 1), _ref("host", 2)
        midpoint = [0.0, 0.0, 0.0]
        fp = clash_fingerprint("t1", ref_a, ref_b, midpoint)
        original = {
            "id": "x", "seq": 1, "fingerprint": fp,
            "test_id": "t1", "kind": "hard", "status": "Open",
            "ref_a": ref_a, "ref_b": ref_b, "midpoint": midpoint,
            "first_seen_run": "old", "last_seen_run": "old",
            "comments": [], "viewpoints": [], "history": [],
        }
        old_snapshot = dict(original)
        raw = [_raw("t1", ref_a, ref_b, midpoint)]
        merge_runs([original], raw, run_iso="new-time")
        # last_seen_run on the input should be unchanged
        self.assertEqual(original["last_seen_run"], old_snapshot["last_seen_run"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
