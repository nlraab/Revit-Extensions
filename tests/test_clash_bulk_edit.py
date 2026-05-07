"""Tests for lib/clash_core/bulk_edit.py — single-clash mutators used
by the Browser's bulk-action handlers.

Pure-data module — no Revit, no WPF. Verifies:
  * apply_status / apply_trade mutate the clash dict correctly
  * a no-op (already-at-target) returns False and doesn't pollute the
    history log
  * history entries match the shape the single-row handlers produce
    (action / before / after / author / at)
  * the optional `at` kwarg pins a uniform timestamp for batch edits
  * defensive handling of None / empty inputs
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_core import bulk_edit  # noqa: E402


def _clash(status="Open", assignee="Mechanical", history=None):
    """Minimal clash dict for tests."""
    out = {
        "id": "clash-1",
        "status": status,
        "assignee": assignee,
    }
    if history is not None:
        out["history"] = list(history)
    return out


# ---------------------------------------------------------------------------
# apply_status
# ---------------------------------------------------------------------------

class ApplyStatusTests(unittest.TestCase):

    def test_changes_status_when_different(self):
        clash = _clash(status="Open")
        changed = bulk_edit.apply_status(clash, "Reviewed", "Nathan")
        self.assertTrue(changed)
        self.assertEqual(clash["status"], "Reviewed")

    def test_appends_history_entry_with_correct_shape(self):
        clash = _clash(status="Open")
        bulk_edit.apply_status(clash, "Resolved", "Nathan")
        self.assertEqual(len(clash["history"]), 1)
        entry = clash["history"][0]
        self.assertEqual(entry["action"], "status_changed")
        self.assertEqual(entry["before"], "Open")
        self.assertEqual(entry["after"], "Resolved")
        self.assertEqual(entry["author"], "Nathan")
        self.assertIn("at", entry)

    def test_skips_when_already_at_target(self):
        clash = _clash(status="Reviewed")
        changed = bulk_edit.apply_status(clash, "Reviewed", "Nathan")
        self.assertFalse(changed)
        self.assertEqual(clash["status"], "Reviewed")
        self.assertNotIn("history", clash,
                         "no-op should not create a history list either")

    def test_skips_doesnt_mutate_existing_history(self):
        clash = _clash(status="Reviewed", history=[{"action": "detected"}])
        bulk_edit.apply_status(clash, "Reviewed", "Nathan")
        self.assertEqual(len(clash["history"]), 1,
                         "no-op should not append a history entry")

    def test_creates_history_list_if_missing(self):
        clash = _clash(status="Open")
        # Note: `_clash` doesn't include 'history' unless asked.
        self.assertNotIn("history", clash)
        bulk_edit.apply_status(clash, "Reviewed", "Nathan")
        self.assertEqual(len(clash["history"]), 1)

    def test_appends_to_existing_history(self):
        clash = _clash(status="Open",
                       history=[{"action": "detected", "at": "2026-01-01T00:00:00Z"}])
        bulk_edit.apply_status(clash, "Reviewed", "Nathan")
        self.assertEqual(len(clash["history"]), 2)
        self.assertEqual(clash["history"][0]["action"], "detected")
        self.assertEqual(clash["history"][1]["action"], "status_changed")

    def test_treats_missing_status_as_open(self):
        # Defensive: a clash with no 'status' field defaults to 'Open' for
        # the before-value. Setting it to Reviewed should change.
        clash = {"id": "clash-1"}
        changed = bulk_edit.apply_status(clash, "Reviewed", "Nathan")
        self.assertTrue(changed)
        self.assertEqual(clash["history"][0]["before"], "Open")

    def test_at_kwarg_pins_uniform_timestamp(self):
        clash = _clash(status="Open")
        bulk_edit.apply_status(clash, "Reviewed", "Nathan",
                               at="2026-05-06T12:00:00Z")
        self.assertEqual(clash["history"][0]["at"], "2026-05-06T12:00:00Z")

    def test_returns_false_for_none_clash(self):
        self.assertFalse(bulk_edit.apply_status(None, "Reviewed", "Nathan"))

    def test_returns_false_for_empty_new_status(self):
        clash = _clash()
        self.assertFalse(bulk_edit.apply_status(clash, "", "Nathan"))
        self.assertFalse(bulk_edit.apply_status(clash, None, "Nathan"))


# ---------------------------------------------------------------------------
# apply_trade
# ---------------------------------------------------------------------------

class ApplyTradeTests(unittest.TestCase):

    def test_changes_trade_when_different(self):
        clash = _clash(assignee="Mechanical")
        changed = bulk_edit.apply_trade(clash, "Plumbing", "Nathan")
        self.assertTrue(changed)
        self.assertEqual(clash["assignee"], "Plumbing")

    def test_appends_history_entry_with_reassigned_action(self):
        clash = _clash(assignee="Mechanical")
        bulk_edit.apply_trade(clash, "Architectural", "Nathan")
        entry = clash["history"][0]
        self.assertEqual(entry["action"], "reassigned")
        self.assertEqual(entry["before"], "Mechanical")
        self.assertEqual(entry["after"], "Architectural")

    def test_skips_when_already_at_target(self):
        clash = _clash(assignee="Plumbing")
        changed = bulk_edit.apply_trade(clash, "Plumbing", "Nathan")
        self.assertFalse(changed)
        self.assertNotIn("history", clash)

    def test_treats_missing_assignee_as_dash(self):
        clash = {"id": "clash-1"}
        changed = bulk_edit.apply_trade(clash, "Mechanical", "Nathan")
        self.assertTrue(changed)
        self.assertEqual(clash["history"][0]["before"], "-")

    def test_at_kwarg_pins_uniform_timestamp(self):
        clash = _clash(assignee="Mechanical")
        bulk_edit.apply_trade(clash, "Plumbing", "Nathan",
                              at="2026-05-06T12:00:00Z")
        self.assertEqual(clash["history"][0]["at"], "2026-05-06T12:00:00Z")


# ---------------------------------------------------------------------------
# Batch-style usage: looping over many clashes
# ---------------------------------------------------------------------------

class BatchUsageTests(unittest.TestCase):
    """The Browser uses these in a loop. Verify the loop pattern — count
    how many actually changed and how many were no-ops."""

    def test_status_loop_counts_only_actual_changes(self):
        clashes = [
            _clash(status="Open"),
            _clash(status="Open"),
            _clash(status="Reviewed"),  # already at target — should skip
            _clash(status="Open"),
        ]
        changed_count = sum(
            1 for c in clashes
            if bulk_edit.apply_status(c, "Reviewed", "Nathan")
        )
        self.assertEqual(changed_count, 3)
        # All 4 are now Reviewed
        self.assertTrue(all(c["status"] == "Reviewed" for c in clashes))
        # But only 3 history entries were appended (the 4th was a no-op)
        history_total = sum(len(c.get("history", [])) for c in clashes)
        self.assertEqual(history_total, 3)

    def test_uniform_timestamp_across_batch(self):
        clashes = [_clash(status="Open"), _clash(status="Open"),
                   _clash(status="Open")]
        batch_at = "2026-05-06T12:00:00Z"
        for c in clashes:
            bulk_edit.apply_status(c, "Reviewed", "Nathan", at=batch_at)
        timestamps = [c["history"][0]["at"] for c in clashes]
        self.assertEqual(timestamps, [batch_at, batch_at, batch_at])


if __name__ == "__main__":
    unittest.main()
