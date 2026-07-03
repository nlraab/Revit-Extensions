# -*- coding: utf-8 -*-
"""Unit tests for lib/clash_detect/broadphase: the grid AABB broad phase
both detection engines prune with.

The one property that matters: the broad phase may OVER-report but must
never drop a genuinely overlapping pair - a missed candidate is a missed
clash. Tests pin overlap semantics (touching counts), padding, long
elements spanning many grid cells, cell-boundary cases, and conservative
handling of unknown (None) bboxes.
"""
import os
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_detect import broadphase


def box(x0, y0, z0, x1, y1, z1):
    return (float(x0), float(y0), float(z0), float(x1), float(y1), float(z1))


class OverlapTests(unittest.TestCase):
    def test_touching_counts_as_overlap(self):
        a = box(0, 0, 0, 1, 1, 1)
        b = box(1, 0, 0, 2, 1, 1)
        self.assertTrue(broadphase.overlaps(a, b))

    def test_separated_is_not_overlap(self):
        a = box(0, 0, 0, 1, 1, 1)
        b = box(1.01, 0, 0, 2, 1, 1)
        self.assertFalse(broadphase.overlaps(a, b))

    def test_containment_is_overlap(self):
        outer = box(0, 0, 0, 10, 10, 10)
        inner = box(4, 4, 4, 5, 5, 5)
        self.assertTrue(broadphase.overlaps(outer, inner))
        self.assertTrue(broadphase.overlaps(inner, outer))


class CandidateMapTests(unittest.TestCase):
    def test_finds_the_overlapping_pair_only(self):
        a_items = [('a1', box(0, 0, 0, 2, 2, 2)),
                   ('a2', box(100, 100, 100, 102, 102, 102))]
        b_items = [('b1', box(1, 1, 1, 3, 3, 3)),
                   ('b2', box(50, 50, 50, 52, 52, 52))]
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertEqual(cand, {'a1': ['b1']})

    def test_pad_pulls_in_near_misses(self):
        a_items = [('a1', box(0, 0, 0, 1, 1, 1))]
        b_items = [('b1', box(2, 0, 0, 3, 1, 1))]      # 1 ft away
        self.assertEqual(broadphase.candidate_map(a_items, b_items), {})
        cand = broadphase.candidate_map(a_items, b_items, pad_ft=1.0)
        self.assertEqual(cand, {'a1': ['b1']})

    def test_long_wall_spanning_many_cells_is_found_from_any_point(self):
        wall = [('wall', box(0, 0, 0, 200, 1, 10))]     # 200-ft wall
        for x in (0.5, 95.0, 199.5):
            duct = [('duct', box(x, 0.5, 5, x + 1, 2, 6))]
            cand = broadphase.candidate_map(duct, wall)
            self.assertEqual(cand, {'duct': ['wall']},
                             'missed the wall at x={0}'.format(x))

    def test_cell_boundary_pair_is_not_missed(self):
        # Straddling a 10-ft cell boundary exactly.
        a_items = [('a', box(9.9, 0, 0, 10.0, 1, 1))]
        b_items = [('b', box(10.0, 0, 0, 10.1, 1, 1))]
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertEqual(cand, {'a': ['b']})

    def test_negative_coordinates(self):
        a_items = [('a', box(-25, -25, -5, -24, -24, -4))]
        b_items = [('b', box(-24.5, -25, -5, -23, -24, -4))]
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertEqual(cand, {'a': ['b']})

    def test_none_a_bbox_gets_every_b(self):
        a_items = [('a', None)]
        b_items = [('b1', box(0, 0, 0, 1, 1, 1)), ('b2', None)]
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertEqual(sorted(cand['a']), ['b1', 'b2'])

    def test_none_b_bbox_reaches_every_a(self):
        a_items = [('a1', box(0, 0, 0, 1, 1, 1)),
                   ('a2', box(500, 500, 500, 501, 501, 501))]
        b_items = [('b-mystery', None)]
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertEqual(cand, {'a1': ['b-mystery'], 'a2': ['b-mystery']})

    def test_no_duplicates_for_multi_cell_neighbors(self):
        a_items = [('a', box(0, 0, 0, 25, 25, 25))]     # covers many cells
        b_items = [('b', box(5, 5, 5, 20, 20, 20))]     # also many cells
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertEqual(cand['a'].count('b'), 1)

    def test_empty_inputs(self):
        self.assertEqual(broadphase.candidate_map([], [('b', box(0, 0, 0, 1, 1, 1))]), {})
        self.assertEqual(broadphase.candidate_map([('a', box(0, 0, 0, 1, 1, 1))], []), {})

    def test_nan_and_inf_bboxes_degrade_to_conservative_not_crash(self):
        nan = float('nan')
        inf = float('inf')
        a_items = [('a-nan', box(nan, 0, 0, 1, 1, 1)),
                   ('a-ok', box(0, 0, 0, 1, 1, 1))]
        b_items = [('b-inf', (0.0, 0.0, 0.0, inf, 1.0, 1.0)),
                   ('b-ok', box(0.5, 0, 0, 2, 1, 1))]
        cand = broadphase.candidate_map(a_items, b_items)
        # garbage boxes act like unknowns: always candidates, never a crash
        self.assertEqual(sorted(cand['a-nan']), ['b-inf', 'b-ok'])
        self.assertIn('b-ok', cand['a-ok'])
        self.assertIn('b-inf', cand['a-ok'])

    def test_mile_wide_corrupt_bbox_is_fast_and_conservative(self):
        # One stray-vertex import spanning 100,000 ft must not grid-hang;
        # it degrades to always-candidate. Both sides.
        import time
        big = box(-50000, -50000, -50000, 50000, 50000, 50000)
        a_items = [('a', box(0, 0, 0, 1, 1, 1))]
        b_items = [('b-big', big), ('b-far', box(900, 900, 900, 901, 901, 901))]
        t0 = time.time()
        cand = broadphase.candidate_map(a_items, b_items)
        self.assertLess(time.time() - t0, 1.0)
        self.assertIn('b-big', cand['a'])
        self.assertNotIn('b-far', cand['a'])
        cand2 = broadphase.candidate_map([('a-big', big)], a_items + [])
        self.assertEqual(cand2['a-big'], ['a'])

    def test_inverted_bbox_is_treated_as_unknown(self):
        self.assertIsNone(broadphase._grid_range(box(5, 0, 0, 1, 1, 1), 10.0))

    def test_bbox_tuple_sanitizes_garbage(self):
        class P(object):
            def __init__(self, x, y, z):
                self.X, self.Y, self.Z = x, y, z

        class BB(object):
            def __init__(self, mn, mx):
                self.Min, self.Max = mn, mx
        self.assertIsNone(broadphase.bbox_tuple(None))
        self.assertIsNone(broadphase.bbox_tuple(
            BB(P(float('nan'), 0, 0), P(1, 1, 1))))
        self.assertIsNone(broadphase.bbox_tuple(
            BB(P(0, 0, 0), P(1e12, 1, 1))))
        self.assertIsNone(broadphase.bbox_tuple(BB(P(5, 0, 0), P(1, 1, 1))))
        self.assertEqual(broadphase.bbox_tuple(BB(P(0, 0, 0), P(1, 2, 3))),
                         (0.0, 0.0, 0.0, 1.0, 2.0, 3.0))

    def test_never_drops_a_real_overlap_randomized_grid(self):
        # Deterministic pseudo-random layout: every truly overlapping pair
        # found by brute force must appear in the candidate map.
        def rng(seed):
            s = [seed]

            def nxt():
                s[0] = (s[0] * 1103515245 + 12345) % (2 ** 31)
                return s[0] / float(2 ** 31)
            return nxt
        r = rng(42)
        a_items, b_items = [], []
        for i in range(80):
            x, y, z = r() * 120 - 60, r() * 120 - 60, r() * 40
            a_items.append(('a{0}'.format(i),
                            box(x, y, z, x + r() * 8, y + r() * 8, z + r() * 8)))
        for j in range(80):
            x, y, z = r() * 120 - 60, r() * 120 - 60, r() * 40
            b_items.append(('b{0}'.format(j),
                            box(x, y, z, x + r() * 8, y + r() * 8, z + r() * 8)))
        cand = broadphase.candidate_map(a_items, b_items, pad_ft=0.05)
        pad = 0.05
        missed = []
        for ak, ab in a_items:
            qb = (ab[0] - pad, ab[1] - pad, ab[2] - pad,
                  ab[3] + pad, ab[4] + pad, ab[5] + pad)
            for bk, bb in b_items:
                if broadphase.overlaps(qb, bb) and bk not in cand.get(ak, []):
                    missed.append((ak, bk))
        self.assertEqual(missed, [])


if __name__ == '__main__':
    unittest.main()
