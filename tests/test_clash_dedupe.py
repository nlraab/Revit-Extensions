"""Tests for lib/clash_core/dedupe.py — pre-merge run-level dedupe.

Pure-data module — no Revit needed. Verifies the rule that drops soft
"near miss" clashes for a pair that already has a hard "actual hit"
clash in the same run.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_core import dedupe  # noqa: E402


def _hard(a_id, b_id, a_src="host", b_src="host", a_link=None, b_link=None):
    return {
        "kind": "hard",
        "ref_a": {
            "source": a_src,
            "element_id": a_id,
            "link_doc_title": a_link,
        },
        "ref_b": {
            "source": b_src,
            "element_id": b_id,
            "link_doc_title": b_link,
        },
    }


def _soft(a_id, b_id, a_src="host", b_src="host", a_link=None, b_link=None):
    out = _hard(a_id, b_id, a_src, b_src, a_link, b_link)
    out["kind"] = "soft"
    return out


class DedupeTests(unittest.TestCase):
    def test_empty_input(self):
        out, dropped = dedupe.drop_soft_overlapping_hard([])
        self.assertEqual(out, [])
        self.assertEqual(dropped, 0)

    def test_no_hard_keeps_all_soft(self):
        clashes = [_soft(1, 2), _soft(3, 4)]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)

    def test_no_soft_keeps_all_hard(self):
        clashes = [_hard(1, 2), _hard(3, 4)]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)

    def test_drops_soft_when_hard_exists_for_same_pair(self):
        clashes = [
            _hard(1, 2),
            _soft(1, 2),  # to drop
            _soft(3, 4),  # keep — different pair
        ]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 1)
        kept_kinds = [c["kind"] for c in out]
        self.assertEqual(kept_kinds, ["hard", "soft"])
        self.assertEqual(out[1]["ref_a"]["element_id"], 3)

    def test_pair_order_doesnt_matter(self):
        # Hard (1, 2) should also dedupe Soft (2, 1) — sorted-tuple key.
        clashes = [_hard(1, 2), _soft(2, 1)]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(out[0]["kind"], "hard")

    def test_link_doc_title_disambiguates(self):
        # Same element_id in two different linked .rvts = different pair.
        clashes = [
            _hard(1, 2, b_src="link:Architectural", b_link="Arch_A"),
            _soft(1, 2, b_src="link:Architectural", b_link="Arch_B"),
        ]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 2,
                         "different link_doc_titles should not dedupe")
        self.assertEqual(dropped, 0)

    def test_source_disambiguates(self):
        # Same element_id 5 living in host vs in a link is a different ref.
        clashes = [
            _hard(5, 6, a_src="host"),
            _soft(5, 6, a_src="link:Architectural"),
        ]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)

    def test_preserves_original_order(self):
        clashes = [
            _soft(5, 6),
            _hard(1, 2),
            _soft(1, 2),  # to drop
            _soft(7, 8),
        ]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(dropped, 1)
        self.assertEqual([c["ref_a"]["element_id"] for c in out], [5, 1, 7])

    def test_multiple_softs_for_same_hard_pair_all_dropped(self):
        # If detection produced two soft clashes for the same pair (slightly
        # different midpoints inside the inflation tolerance), both should
        # drop when a hard clash exists.
        clashes = [
            _hard(1, 2),
            _soft(1, 2),
            _soft(1, 2),
        ]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 1)
        self.assertEqual(dropped, 2)
        self.assertEqual(out[0]["kind"], "hard")

    def test_hard_vs_hard_not_collapsed(self):
        # Out of scope for this rule — keep both hards even if they share a
        # pair (overlapping custom test scopes). Only soft-vs-hard is deduped.
        clashes = [_hard(1, 2), _hard(1, 2)]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 0)

    def test_missing_element_id_skips_safely(self):
        # Defensive: a malformed clash with no element_id shouldn't crash;
        # _pair_key returns None and the clash falls through unfiltered.
        clashes = [
            _hard(1, 2),
            {"kind": "soft", "ref_a": {"source": "host"}, "ref_b": {}},
            _soft(1, 2),
        ]
        out, dropped = dedupe.drop_soft_overlapping_hard(clashes)
        self.assertEqual(dropped, 1, "only the (1,2) soft pair is dropped")
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
