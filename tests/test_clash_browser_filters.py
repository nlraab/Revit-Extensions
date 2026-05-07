"""Tests for lib/clash_core/browser_filters.py.

Pure-data filter predicate — no Revit, no WPF. Verifies that the
combined filter logic (trade × status × test × search) does the right
thing under each control's edge cases.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_core import browser_filters  # noqa: E402


class _Row(object):
    """Minimal stand-in for the Browser's ClashRow — just the attrs the
    filter predicate reads."""
    def __init__(self, trade="Mechanical", status="Open",
                 test_name="MEP vs Architecture", haystack=""):
        self.Trade = trade
        self.Status = status
        self.TestName = test_name
        self.SearchHaystack = haystack


# ---------------------------------------------------------------------------
# row_passes — trade filter
# ---------------------------------------------------------------------------

class TradeFilterTests(unittest.TestCase):
    def test_none_means_no_filter(self):
        row = _Row(trade="Mechanical")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, ""))

    def test_in_allowed_set_passes(self):
        row = _Row(trade="Plumbing")
        self.assertTrue(browser_filters.row_passes(
            row, {"Mechanical", "Plumbing"}, None, None, ""))

    def test_not_in_allowed_set_blocks(self):
        row = _Row(trade="Electrical")
        self.assertFalse(browser_filters.row_passes(
            row, {"Mechanical", "Plumbing"}, None, None, ""))

    def test_empty_set_blocks_everything(self):
        # User unchecked every trade — every row should be hidden.
        row = _Row(trade="Mechanical")
        self.assertFalse(browser_filters.row_passes(
            row, set(), None, None, ""))


# ---------------------------------------------------------------------------
# row_passes — status filter
# ---------------------------------------------------------------------------

class StatusFilterTests(unittest.TestCase):
    def test_none_means_no_filter(self):
        row = _Row(status="Resolved")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, ""))

    def test_default_coordination_set_hides_resolved(self):
        # Mirror the XAML default: Open + Reviewed checked, Approved +
        # Resolved unchecked.
        defaults = {"Open", "Reviewed"}
        self.assertTrue(browser_filters.row_passes(
            _Row(status="Open"), None, defaults, None, ""))
        self.assertTrue(browser_filters.row_passes(
            _Row(status="Reviewed"), None, defaults, None, ""))
        self.assertFalse(browser_filters.row_passes(
            _Row(status="Approved"), None, defaults, None, ""))
        self.assertFalse(browser_filters.row_passes(
            _Row(status="Resolved"), None, defaults, None, ""))


# ---------------------------------------------------------------------------
# row_passes — test dropdown
# ---------------------------------------------------------------------------

class TestFilterTests(unittest.TestCase):
    def test_none_passes(self):
        row = _Row(test_name="MEP vs Architecture")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, ""))

    def test_all_tests_sentinel_passes(self):
        row = _Row(test_name="MEP vs Architecture")
        self.assertTrue(browser_filters.row_passes(
            row, None, None, "(All tests)", ""))

    def test_empty_string_passes(self):
        # Treat empty string the same as None.
        row = _Row(test_name="MEP vs Architecture")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, "", ""))

    def test_matching_name_passes(self):
        row = _Row(test_name="MEP vs Architecture")
        self.assertTrue(browser_filters.row_passes(
            row, None, None, "MEP vs Architecture", ""))

    def test_non_matching_name_blocks(self):
        row = _Row(test_name="MEP Soft Clearance")
        self.assertFalse(browser_filters.row_passes(
            row, None, None, "MEP vs Architecture", ""))


# ---------------------------------------------------------------------------
# row_passes — search box
# ---------------------------------------------------------------------------

class SearchFilterTests(unittest.TestCase):
    def test_empty_search_passes(self):
        row = _Row(haystack="duct chilled water 1250111")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, ""))

    def test_none_search_passes(self):
        row = _Row(haystack="duct chilled water 1250111")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, None))

    def test_whitespace_only_search_passes(self):
        row = _Row(haystack="duct chilled water 1250111")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, "   "))

    def test_substring_match_passes(self):
        row = _Row(haystack="round duct - 12 supply 1250111")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, "duct"))

    def test_search_is_case_insensitive(self):
        row = _Row(haystack="round duct - 12 supply 1250111")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, "DUCT"))
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, "Duct"))

    def test_search_finds_element_id(self):
        row = _Row(haystack="round duct - 12 supply 1250111")
        self.assertTrue(
            browser_filters.row_passes(row, None, None, None, "1250111"))

    def test_no_substring_blocks(self):
        row = _Row(haystack="round duct - 12 supply 1250111")
        self.assertFalse(
            browser_filters.row_passes(row, None, None, None, "wall"))

    def test_missing_haystack_attribute_safe(self):
        # Defensive: row without SearchHaystack attribute → empty haystack.
        row = _Row()
        # Use a fresh object that doesn't have SearchHaystack set:
        class _Bare(object):
            Trade = "Mechanical"
            Status = "Open"
            TestName = "X"
        self.assertFalse(browser_filters.row_passes(
            _Bare(), None, None, None, "anything"))


# ---------------------------------------------------------------------------
# row_passes — combined filters
# ---------------------------------------------------------------------------

class CombinedFilterTests(unittest.TestCase):
    def test_all_filters_must_pass_intersection(self):
        row = _Row(trade="Mechanical", status="Open",
                   test_name="MEP vs Architecture",
                   haystack="round duct 1250111")
        self.assertTrue(browser_filters.row_passes(
            row,
            {"Mechanical", "Plumbing"},
            {"Open"},
            "MEP vs Architecture",
            "duct",
        ))

    def test_one_filter_failing_blocks_the_row(self):
        # Trade matches but status doesn't.
        row = _Row(trade="Mechanical", status="Resolved",
                   test_name="MEP vs Architecture",
                   haystack="round duct 1250111")
        self.assertFalse(browser_filters.row_passes(
            row,
            {"Mechanical"},
            {"Open"},  # row's status is Resolved, not in the allowed set
            None,
            "duct",
        ))


# ---------------------------------------------------------------------------
# build_search_haystack
# ---------------------------------------------------------------------------

class BuildHaystackTests(unittest.TestCase):
    def test_includes_test_name(self):
        h = browser_filters.build_search_haystack({}, "MEP vs Architecture")
        self.assertIn("mep vs architecture", h)

    def test_includes_element_names_and_ids(self):
        clash = {
            "ref_a": {"name": "Round Duct", "element_id": 1250111},
            "ref_b": {"name": "Generic - 8", "element_id": 1240656},
        }
        h = browser_filters.build_search_haystack(clash, "")
        self.assertIn("round duct", h)
        self.assertIn("generic - 8", h)
        self.assertIn("1250111", h)
        self.assertIn("1240656", h)

    def test_includes_assignee(self):
        clash = {"assignee": "Plumbing"}
        h = browser_filters.build_search_haystack(clash, "")
        self.assertIn("plumbing", h)

    def test_includes_comment_author_and_body(self):
        clash = {
            "comments": [
                {"author": "alice", "body": "Talk to the architect"},
                {"author": "bob", "body": "Re-routed pipe"},
            ],
        }
        h = browser_filters.build_search_haystack(clash, "")
        self.assertIn("alice", h)
        self.assertIn("talk to the architect", h)
        self.assertIn("bob", h)
        self.assertIn("re-routed pipe", h)

    def test_handles_missing_fields_gracefully(self):
        # Empty clash dict — should still return a string, not crash.
        h = browser_filters.build_search_haystack({}, None)
        self.assertEqual(h.strip(), "")

    def test_output_is_lowercase(self):
        clash = {"ref_a": {"name": "Round Duct"}}
        h = browser_filters.build_search_haystack(clash, "MEP")
        self.assertEqual(h, h.lower())


if __name__ == "__main__":
    unittest.main()
