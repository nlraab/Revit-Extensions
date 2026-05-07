"""Tests for lib/clash_core/history_format.py — friendly display
formatters for clash audit-trail entries.

Pure-data — no Revit, no WPF. Verifies:
  * Each known action key gets a friendly label
  * Unknown actions get title-cased gracefully (don't disappear)
  * before/after pairs render with a → arrow
  * Timestamps reformat from ISO to 'YYYY-MM-DD HH:MM UTC'
  * Defensive handling of None / missing fields
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_core import history_format  # noqa: E402


# ---------------------------------------------------------------------------
# format_action
# ---------------------------------------------------------------------------

class FormatActionTests(unittest.TestCase):

    def test_status_changed_renders_arrow(self):
        entry = {"action": "status_changed",
                 "before": "Open", "after": "Reviewed"}
        self.assertEqual(history_format.format_action(entry),
                         u"Status changed: Open → Reviewed")

    def test_reassigned_renders_arrow(self):
        entry = {"action": "reassigned",
                 "before": "Mechanical", "after": "Plumbing"}
        self.assertEqual(history_format.format_action(entry),
                         u"Reassigned: Mechanical → Plumbing")

    def test_comment_added_no_before_after(self):
        entry = {"action": "comment_added"}
        self.assertEqual(history_format.format_action(entry), "Comment added")

    def test_detected_no_before_after(self):
        entry = {"action": "detected"}
        self.assertEqual(history_format.format_action(entry), "Detected")

    def test_viewpoint_saved(self):
        entry = {"action": "viewpoint_saved"}
        self.assertEqual(history_format.format_action(entry), "Viewpoint saved")

    def test_viewpoint_auto_captured(self):
        entry = {"action": "viewpoint_auto_captured"}
        self.assertEqual(history_format.format_action(entry),
                         "Viewpoint auto-captured")

    def test_auto_resolved_with_before_after(self):
        entry = {"action": "auto_resolved",
                 "before": "Open", "after": "Resolved"}
        self.assertEqual(history_format.format_action(entry),
                         u"Auto-resolved: Open → Resolved")

    def test_unknown_action_title_cased(self):
        # An unknown future action should render as a friendly label
        # rather than disappearing.
        entry = {"action": "exported_to_bcf"}
        self.assertEqual(history_format.format_action(entry),
                         "Exported to bcf")

    def test_only_after_no_before(self):
        # Edge case: an entry has after but no before (some action types
        # might just record the new state without a prior).
        entry = {"action": "comment_added", "after": "Note added"}
        self.assertEqual(history_format.format_action(entry),
                         "Comment added: Note added")

    def test_empty_action_string(self):
        # Defensive: a malformed entry with empty action.
        entry = {"action": ""}
        self.assertEqual(history_format.format_action(entry), "Unknown")

    def test_none_entry(self):
        self.assertEqual(history_format.format_action(None), "")

    def test_missing_action_key(self):
        # A dict that lacks 'action' entirely.
        self.assertEqual(history_format.format_action({}), "Unknown")


# ---------------------------------------------------------------------------
# format_when
# ---------------------------------------------------------------------------

class FormatWhenTests(unittest.TestCase):

    def test_iso_timestamp_reformatted(self):
        entry = {"at": "2026-05-06T05:11:16Z"}
        self.assertEqual(history_format.format_when(entry),
                         "2026-05-06 05:11 UTC")

    def test_strips_fractional_seconds(self):
        entry = {"at": "2026-05-06T05:11:16.123456Z"}
        self.assertEqual(history_format.format_when(entry),
                         "2026-05-06 05:11 UTC")

    def test_strips_trailing_z_when_no_seconds(self):
        entry = {"at": "2026-05-06T05:11Z"}
        self.assertEqual(history_format.format_when(entry),
                         "2026-05-06 05:11 UTC")

    def test_no_timezone_marker(self):
        # Some ISO strings might not have the trailing Z.
        entry = {"at": "2026-05-06T14:30:00"}
        self.assertEqual(history_format.format_when(entry),
                         "2026-05-06 14:30 UTC")

    def test_missing_at_returns_empty(self):
        self.assertEqual(history_format.format_when({}), "")
        self.assertEqual(history_format.format_when({"at": ""}), "")

    def test_malformed_at_returns_raw(self):
        # Anything without a T separator is returned as-is rather than
        # mangled — better to show "garbage in" than fail silently.
        entry = {"at": "not-a-timestamp"}
        self.assertEqual(history_format.format_when(entry), "not-a-timestamp")

    def test_none_entry(self):
        self.assertEqual(history_format.format_when(None), "")


# ---------------------------------------------------------------------------
# format_author
# ---------------------------------------------------------------------------

class FormatAuthorTests(unittest.TestCase):

    def test_returns_author(self):
        self.assertEqual(history_format.format_author({"author": "Nathan"}),
                         "Nathan")

    def test_strips_whitespace(self):
        self.assertEqual(
            history_format.format_author({"author": "  Nathan  "}),
            "Nathan")

    def test_missing_author_defaults_to_unknown(self):
        self.assertEqual(history_format.format_author({}), "unknown")

    def test_empty_author_defaults_to_unknown(self):
        self.assertEqual(history_format.format_author({"author": ""}),
                         "unknown")
        self.assertEqual(history_format.format_author({"author": "   "}),
                         "unknown")

    def test_none_entry(self):
        self.assertEqual(history_format.format_author(None), "unknown")


if __name__ == "__main__":
    unittest.main()
