# -*- coding: utf-8 -*-
"""Tests for lib/clash_report/report_model.py.

Pure-data module shared by every export + the Reports tab preview. Verifies
the band/score/trade/pair accessors, the coordination summary math, and the
id-based selection. Runs under CPython 3 (and the same source under
IronPython 2.7 in Revit).
"""

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))

from clash_report import report_model as rm  # noqa: E402


def _clash(cid, seq=1, score=50, band=None, status="Open", trade="Mechanical",
           suppressed=False, a_src="host", b_src="link:Architectural",
           a_cat="Ducts", b_cat="Walls", first_seen="2026-07-01T00:00:00Z",
           gid=None):
    imp = {"score": score, "headline": "why {0}".format(seq)}
    if band is not None:
        imp["band"] = band
    if suppressed:
        imp["suppressed"] = True
    c = {
        "id": cid, "seq": seq, "status": status, "assignee": trade,
        "importance": imp, "first_seen_run": first_seen,
        "ref_a": {"source": a_src, "category": a_cat, "name": "A", "element_id": 1},
        "ref_b": {"source": b_src, "category": b_cat, "name": "B", "element_id": 2},
    }
    if gid:
        c["group_id"] = gid
    return c


class AccessorTests(unittest.TestCase):

    def test_band_prefers_stamped_band(self):
        self.assertEqual(rm.band_of(_clash("a", score=10, band="Critical")),
                         "Critical")

    def test_band_derived_from_score_when_unstamped(self):
        self.assertEqual(rm.band_of(_clash("a", score=85)), "Critical")
        self.assertEqual(rm.band_of(_clash("a", score=55)), "Major")
        self.assertEqual(rm.band_of(_clash("a", score=20)), "Minor")

    def test_band_cutoffs_are_70_and_40(self):
        self.assertEqual(rm.band_of(_clash("a", score=70)), "Critical")
        self.assertEqual(rm.band_of(_clash("a", score=69)), "Major")
        self.assertEqual(rm.band_of(_clash("a", score=40)), "Major")
        self.assertEqual(rm.band_of(_clash("a", score=39)), "Minor")

    def test_score_defaults_to_zero(self):
        self.assertEqual(rm.score_of({"id": "x"}), 0)

    def test_suppressed_flag(self):
        self.assertTrue(rm.is_suppressed(_clash("a", suppressed=True)))
        self.assertFalse(rm.is_suppressed(_clash("a")))

    def test_trade_falls_back_to_placeholder(self):
        self.assertEqual(rm.trade_of({"id": "x"}), "(unassigned)")
        self.assertEqual(rm.trade_of(_clash("a", trade="Plumbing")), "Plumbing")

    def test_pair_uses_link_role_and_category(self):
        c = _clash("a", a_src="host", a_cat="Ducts",
                   b_src="link:Structural", b_cat="Framing")
        self.assertEqual(rm.pair_of(c), "Ducts / Structural")

    def test_reason_prefers_headline(self):
        c = _clash("a")
        c["importance"]["reason"] = "long reason"
        c["importance"]["headline"] = "short headline"
        self.assertEqual(rm.reason_of(c), "short headline")

    def test_test_name_falls_back_to_id_then_placeholder(self):
        self.assertEqual(rm.test_name_of({"test_id": "t1"}, {"t1": "MEP"}), "MEP")
        self.assertEqual(rm.test_name_of({"test_id": "t1"}, {}), "t1")
        self.assertEqual(rm.test_name_of({}, {}), "(unknown test)")


class SelectTests(unittest.TestCase):

    def test_select_by_ids_keeps_order_and_membership(self):
        cs = [_clash("a"), _clash("b"), _clash("c")]
        got = rm.select_by_ids(cs, ["c", "a"])
        self.assertEqual([c["id"] for c in got], ["a", "c"])

    def test_select_none_returns_all(self):
        cs = [_clash("a"), _clash("b")]
        self.assertEqual(len(rm.select_by_ids(cs, None)), 2)

    def test_clean_drops_non_dicts(self):
        self.assertEqual(len(rm.clean([_clash("a"), None, "x", {}])), 2)


class SummaryTests(unittest.TestCase):

    def setUp(self):
        self.cs = [
            _clash("a", seq=1, score=82, band="Critical", status="Open",
                   trade="Mechanical", first_seen="2026-07-05T00:00:00Z", gid="g1"),
            _clash("b", seq=2, score=45, band="Major", status="Reviewed",
                   trade="Plumbing", first_seen="2026-07-01T00:00:00Z"),
            _clash("c", seq=3, score=20, band="Minor", status="Resolved",
                   trade="Mechanical", first_seen="2026-07-01T00:00:00Z"),
            _clash("d", seq=4, score=30, band="Minor", status="Approved",
                   trade="Electrical", first_seen="2026-07-01T00:00:00Z"),
            # suppressed: excluded from every default count
            _clash("e", seq=5, score=60, band="Major", status="Open",
                   trade="Mechanical", suppressed=True),
        ]
        self.groups = [{"id": "g1", "status": "Open", "rollup": {"n_open": 1}}]

    def test_suppressed_excluded_by_default(self):
        s = rm.summarize(self.cs, self.groups)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["suppressed"], 1)

    def test_band_and_status_counts(self):
        s = rm.summarize(self.cs, self.groups)
        self.assertEqual(s["by_band"], {"Critical": 1, "Major": 1, "Minor": 2})
        self.assertEqual(s["by_status"]["Open"], 1)
        self.assertEqual(s["by_status"]["Resolved"], 1)

    def test_open_and_closed_and_pct(self):
        s = rm.summarize(self.cs, self.groups)
        self.assertEqual(s["open"], 2)      # Open + Reviewed
        self.assertEqual(s["closed"], 2)    # Approved + Resolved
        self.assertEqual(s["pct_closed"], 50)

    def test_by_trade_sorted_desc(self):
        s = rm.summarize(self.cs, self.groups)
        self.assertEqual(s["by_trade"][0], {"label": "Mechanical", "count": 2})

    def test_new_since_latest_run(self):
        s = rm.summarize(self.cs, self.groups)
        self.assertEqual(s["newest_run"], "2026-07-05T00:00:00Z")
        self.assertEqual(s["new_count"], 1)   # only clash "a"

    def test_open_issue_count(self):
        s = rm.summarize(self.cs, self.groups)
        self.assertEqual(s["issues_open"], 1)

    def test_empty_input(self):
        s = rm.summarize([], [])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["pct_closed"], 0)


class RowTests(unittest.TestCase):

    def test_row_shape(self):
        row = rm.row_for(_clash("a", seq=7, score=82, band="Critical"),
                         {"t1": "MEP"})
        self.assertEqual(row["seq"], 7)
        self.assertEqual(row["band"], "Critical")
        self.assertEqual(row["score"], 82)
        self.assertEqual(row["pair"], "Ducts / Architectural")
        self.assertEqual(row["reason"], "why 7")

    def test_report_rows_skips_non_dicts(self):
        rows = rm.report_rows([_clash("a"), None, "junk"])
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
