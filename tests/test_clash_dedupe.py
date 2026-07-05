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


def _pen(mep_id, layer_id, layer_cat="Floors", mep_cat="Ducts",
         midpoint=(0.0, 0.0, 0.0), thickness_in=None, is_structural=None,
         mep_src="host", layer_src="link:Architectural",
         layer_link="Arch"):
    """A routed-MEP-vs-layered-assembly penetration clash. ref_a is the mover,
    ref_b is the pierced layer."""
    return {
        "kind": "hard",
        "midpoint": list(midpoint),
        "ref_a": {
            "source": mep_src,
            "element_id": mep_id,
            "category": mep_cat,
            "link_doc_title": None,
        },
        "ref_b": {
            "source": layer_src,
            "element_id": layer_id,
            "category": layer_cat,
            "link_doc_title": layer_link,
            "thickness_in": thickness_in,
            "is_structural": is_structural,
        },
    }


class CollapseLayeredPenetrationsTests(unittest.TestCase):
    def test_empty_input(self):
        out, dropped = dedupe.collapse_layered_penetrations([])
        self.assertEqual(out, [])
        self.assertEqual(dropped, 0)

    def test_non_penetration_clashes_pass_through(self):
        # A duct-vs-duct and a duct-vs-wall clash are not layered penetrations.
        clashes = [
            _hard(1, 2),  # no categories at all
            _pen(10, 20, layer_cat="Walls"),  # walls are not stacked layers
        ]
        out, dropped = dedupe.collapse_layered_penetrations(clashes)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 2)

    def test_stacked_layers_collapse_to_one(self):
        # One duct through a floor assembly: structural slab + topping +
        # finish, all at the same location. Collapse to the structural slab.
        clashes = [
            _pen(100, 201, thickness_in=6.0, is_structural=True,
                 midpoint=(10.0, 5.0, 12.0)),
            _pen(100, 202, thickness_in=2.0, is_structural=False,
                 midpoint=(10.1, 5.0, 12.4)),
            _pen(100, 203, thickness_in=0.125, is_structural=False,
                 midpoint=(10.0, 5.1, 12.6)),
        ]
        out, dropped = dedupe.collapse_layered_penetrations(clashes)
        self.assertEqual(dropped, 2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ref_b"]["element_id"], 201,
                         "structural slab is the kept layer")

    def test_thickest_kept_when_no_structural_flag(self):
        clashes = [
            _pen(100, 201, thickness_in=2.0, midpoint=(0, 0, 0)),
            _pen(100, 202, thickness_in=6.0, midpoint=(0.2, 0, 0.3)),
            _pen(100, 203, thickness_in=0.5, midpoint=(0, 0.1, 0.1)),
        ]
        out, dropped = dedupe.collapse_layered_penetrations(clashes)
        self.assertEqual(dropped, 2)
        self.assertEqual(out[0]["ref_b"]["element_id"], 202,
                         "thickest layer kept when nothing is structural")

    def test_different_movers_do_not_collapse(self):
        # Two different ducts each piercing the same slab is two penetrations.
        clashes = [
            _pen(100, 201, thickness_in=6.0, midpoint=(0, 0, 0)),
            _pen(101, 201, thickness_in=6.0, midpoint=(0, 0, 0)),
        ]
        out, dropped = dedupe.collapse_layered_penetrations(clashes)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 2)

    def test_same_mover_far_apart_stays_separate(self):
        # A riser through three floor LEVELS: same duct, same category, but the
        # midpoints are storeys apart -> three distinct penetrations.
        clashes = [
            _pen(100, 201, thickness_in=6.0, is_structural=True,
                 midpoint=(0, 0, 0)),
            _pen(100, 301, thickness_in=6.0, is_structural=True,
                 midpoint=(0, 0, 12.0)),
            _pen(100, 401, thickness_in=6.0, is_structural=True,
                 midpoint=(0, 0, 24.0)),
        ]
        out, dropped = dedupe.collapse_layered_penetrations(clashes)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 3)

    def test_different_layered_categories_do_not_cross_collapse(self):
        # A duct piercing a floor and a ceiling at the same point are two
        # different assemblies -> not collapsed together.
        clashes = [
            _pen(100, 201, layer_cat="Floors", thickness_in=6.0,
                 midpoint=(0, 0, 0)),
            _pen(100, 301, layer_cat="Ceilings", thickness_in=0.625,
                 midpoint=(0, 0, 0)),
        ]
        out, dropped = dedupe.collapse_layered_penetrations(clashes)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 2)

    def test_missing_midpoint_layers_stand_alone(self):
        # Without midpoints we cannot prove co-location, so we must NOT collapse
        # (a false collapse would hide a real distinct penetration).
        a = _pen(100, 201, thickness_in=6.0)
        b = _pen(100, 202, thickness_in=2.0)
        a["midpoint"] = None
        b["midpoint"] = None
        out, dropped = dedupe.collapse_layered_penetrations([a, b])
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 2)

    def test_deterministic_kept_layer_across_orderings(self):
        # Same cluster, two input orderings -> same layer kept (stable
        # fingerprint). Tie broken by lowest element id when struct+thick equal.
        base = [
            _pen(100, 205, thickness_in=6.0, midpoint=(0, 0, 0)),
            _pen(100, 202, thickness_in=6.0, midpoint=(0.1, 0, 0)),
        ]
        out1, _ = dedupe.collapse_layered_penetrations(list(base))
        out2, _ = dedupe.collapse_layered_penetrations(list(reversed(base)))
        self.assertEqual(out1[0]["ref_b"]["element_id"],
                         out2[0]["ref_b"]["element_id"])
        self.assertEqual(out1[0]["ref_b"]["element_id"], 202,
                         "lowest element id wins the tie")

    def test_protected_band_dropped_when_dedicated_space_also_hit(self):
        # A pipe in the dedicated space (C-NEC Critical) that also grazes the
        # abutting protected band (M-NEC-PROT) is one problem -> drop the
        # M-NEC-PROT row.
        def clr(rule, mep_id, owner_id):
            r = _pen(mep_id, owner_id, layer_cat='Electrical Equipment',
                     mep_cat='Pipes')
            r['clearance_rule'] = rule
            return r
        rows = [clr('C-NEC', 100, 900), clr('M-NEC-PROT', 100, 900),
                clr('M-NEC-PROT', 101, 900)]  # different pipe: kept
        out, dropped = dedupe.drop_redundant_protected_band(rows)
        self.assertEqual(dropped, 1)
        rules = sorted((c.get('clearance_rule'), (c.get('ref_a') or {}).get('element_id')) for c in out)
        self.assertEqual(rules, [('C-NEC', 100), ('M-NEC-PROT', 101)])

    def test_protected_band_kept_without_dedicated_hit(self):
        r = _pen(100, 900, layer_cat='Electrical Equipment', mep_cat='Pipes')
        r['clearance_rule'] = 'M-NEC-PROT'
        out, dropped = dedupe.drop_redundant_protected_band([r])
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 1)

    def test_hard_hit_wins_over_soft_near_miss_in_cluster(self):
        # A mixed cluster: the thick concrete slab is only a soft near-miss but
        # a thinner layer is a hard hit. Never hide the hard intersection --
        # keep it even though the soft layer is thicker/structural.
        soft_slab = _pen(100, 201, thickness_in=6.0, is_structural=True,
                         midpoint=(0, 0, 0))
        soft_slab["kind"] = "soft"
        hard_finish = _pen(100, 202, thickness_in=0.5, is_structural=False,
                           midpoint=(0.1, 0, 0.2))
        out, dropped = dedupe.collapse_layered_penetrations(
            [soft_slab, hard_finish])
        self.assertEqual(dropped, 1)
        self.assertEqual(out[0]["ref_b"]["element_id"], 202)
        self.assertEqual(out[0]["kind"], "hard")

    def test_mover_on_either_side(self):
        # The mover can be ref_b and the layer ref_a — still collapses.
        c1 = _pen(100, 201, thickness_in=6.0, midpoint=(0, 0, 0))
        c2 = _pen(100, 202, thickness_in=2.0, midpoint=(0.1, 0, 0))
        # flip c2 so the layer is ref_a and the duct is ref_b
        c2["ref_a"], c2["ref_b"] = c2["ref_b"], c2["ref_a"]
        out, dropped = dedupe.collapse_layered_penetrations([c1, c2])
        self.assertEqual(dropped, 1)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
