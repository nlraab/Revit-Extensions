"""Tests for lib/clash_core/categories.py.

Pure-data module - tests verify the OST_ <-> friendly mapping is consistent
and that the discipline grouping preserves declaration order.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


class CategoriesTests(unittest.TestCase):
    def test_categories_list_not_empty(self):
        from clash_core import categories
        self.assertTrue(len(categories.CATEGORIES) > 0)

    def test_lookup_round_trip(self):
        """Every (ost, friendly) pair round-trips through both lookup dicts."""
        from clash_core import categories
        for ost, friendly, _group in categories.CATEGORIES:
            self.assertEqual(categories.friendly_for(ost), friendly)
            self.assertEqual(categories.ost_for(friendly), ost)

    def test_friendly_for_unknown_returns_input(self):
        from clash_core import categories
        unknown = "OST_NotARealCategory"
        self.assertEqual(categories.friendly_for(unknown), unknown)

    def test_ost_for_unknown_returns_input(self):
        from clash_core import categories
        self.assertEqual(categories.ost_for("Not A Real Friendly Name"),
                         "Not A Real Friendly Name")

    def test_no_duplicate_ost_entries(self):
        from clash_core import categories
        osts = [ost for ost, _, _ in categories.CATEGORIES]
        self.assertEqual(len(osts), len(set(osts)),
                         "Duplicate OST_ name in CATEGORIES")

    def test_no_duplicate_friendly_entries(self):
        from clash_core import categories
        friendlies = [friendly for _, friendly, _ in categories.CATEGORIES]
        self.assertEqual(len(friendlies), len(set(friendlies)),
                         "Duplicate friendly name in CATEGORIES")

    def test_grouped_preserves_declaration_order(self):
        """categories_grouped returns groups in the order they first appear."""
        from clash_core import categories
        groups = categories.categories_grouped()
        # Groups appear in declaration order
        seen_names = []
        last = None
        for ost, _friendly, group in categories.CATEGORIES:
            if group != last:
                seen_names.append(group)
                last = group
        self.assertEqual([g for g, _ in groups], seen_names)

    def test_grouped_includes_all_categories(self):
        from clash_core import categories
        groups = categories.categories_grouped()
        flat = []
        for _name, members in groups:
            for ost, friendly in members:
                flat.append((ost, friendly))
        # Should contain every entry from CATEGORIES (ignoring the group field)
        original = [(o, f) for o, f, _ in categories.CATEGORIES]
        self.assertEqual(flat, original)

    def test_expected_disciplines_represented(self):
        """Sanity check: every dbHMS discipline has at least one category mapped.

        Architectural and Structural are linked-only; Mechanical/Electrical/
        Plumbing/Fire Protection/Technology are MEP host categories. All
        seven must be represented so the editor shows useful options for
        every test the firm runs.
        """
        from clash_core import categories
        groups = {g for _o, _f, g in categories.CATEGORIES}
        for required in ("Mechanical", "Electrical", "Plumbing",
                         "Fire Protection", "Technology",
                         "Architectural", "Structural"):
            self.assertIn(required, groups,
                          "No categories declared for %s" % required)


class LinkedRoleConstantsTests(unittest.TestCase):
    """Sanity check on lib/clash_detect/linked.py role constants."""

    def test_role_options(self):
        from clash_detect import linked
        self.assertEqual(set(linked.ROLE_OPTIONS),
                         {"Architectural", "Structural", "ignore"})

    def test_ignore_display_distinct_from_value(self):
        from clash_detect import linked
        # Display label is "(ignore)" but the canonical stored value is "ignore"
        self.assertEqual(linked.ROLE_IGNORE, "ignore")
        self.assertEqual(linked.IGNORE_DISPLAY, "(ignore)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
