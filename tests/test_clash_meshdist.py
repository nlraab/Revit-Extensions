# -*- coding: utf-8 -*-
"""Unit tests for lib/clash_detect/meshdist.py -- the pure-math narrow phase
of soft clash detection. These run in CPython and pin down the geometry so
the Revit-side detector inherits proven math.

Includes THE REGRESSION: a diagonal pipe and a diagonal wall whose axis-
aligned bounding boxes overlap while the true geometry is 3 ft apart. The V1
bbox-only detector reported that as a 1-inch near miss; mesh distance must
say "not within tolerance"."""
import math
import os
import random
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_detect import meshdist as md


def box_tris(center, half, rot_z_deg=0.0):
    """12 triangles of a box: `half` = (hx, hy, hz) half-extents, rotated
    about Z by `rot_z_deg`, then translated to `center`."""
    cx, cy, cz = center
    hx, hy, hz = half
    th = math.radians(rot_z_deg)
    c, s = math.cos(th), math.sin(th)
    corners = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                x, y, z = sx * hx, sy * hy, sz * hz
                corners.append((cx + x * c - y * s, cy + x * s + y * c, cz + z))
    # corner index: sx*4 + sy*2 + sz with (-1 -> 0, 1 -> 1)
    def idx(sx, sy, sz):
        return (sx * 4) + (sy * 2) + sz
    quads = [
        (idx(0,0,0), idx(1,0,0), idx(1,1,0), idx(0,1,0)),   # bottom  z-
        (idx(0,0,1), idx(0,1,1), idx(1,1,1), idx(1,0,1)),   # top     z+
        (idx(0,0,0), idx(0,0,1), idx(1,0,1), idx(1,0,0)),   # y-
        (idx(0,1,0), idx(1,1,0), idx(1,1,1), idx(0,1,1)),   # y+
        (idx(0,0,0), idx(0,1,0), idx(0,1,1), idx(0,0,1)),   # x-
        (idx(1,0,0), idx(1,0,1), idx(1,1,1), idx(1,1,0)),   # x+
    ]
    tris = []
    for a, b, cq, d in quads:
        tris.append((corners[a], corners[b], corners[cq]))
        tris.append((corners[a], corners[cq], corners[d]))
    return tris


def dist(a, b):
    return math.sqrt(md._dist_sq(a, b))


class SegmentSegmentTests(unittest.TestCase):
    def test_perpendicular_skew(self):
        c1, c2, d = md.closest_points_segments(
            (0, 0, 0), (1, 0, 0), (0.5, -0.5, 1), (0.5, 0.5, 1))
        self.assertAlmostEqual(math.sqrt(d), 1.0, places=12)
        self.assertAlmostEqual(c1[0], 0.5, places=12)
        self.assertAlmostEqual(c2[2], 1.0, places=12)

    def test_parallel_offset(self):
        _c1, _c2, d = md.closest_points_segments(
            (0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0))
        self.assertAlmostEqual(math.sqrt(d), 1.0, places=12)

    def test_collinear_gap(self):
        c1, c2, d = md.closest_points_segments(
            (0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0))
        self.assertAlmostEqual(math.sqrt(d), 1.0, places=12)
        self.assertAlmostEqual(c1[0], 1.0, places=12)
        self.assertAlmostEqual(c2[0], 2.0, places=12)

    def test_zero_length_segments(self):
        _c1, _c2, d = md.closest_points_segments(
            (0, 0, 0), (0, 0, 0), (1, 1, 1), (1, 1, 1))
        self.assertAlmostEqual(math.sqrt(d), math.sqrt(3.0), places=12)

    def test_near_parallel_no_blowup(self):
        c1, c2, d = md.closest_points_segments(
            (0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1 + 1e-8, 0))
        self.assertTrue(0.9 < math.sqrt(d) < 1.1)
        for v in c1 + c2:
            self.assertFalse(math.isnan(v))


class PointTriangleTests(unittest.TestCase):
    TRI = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))

    def check(self, p, want_point, want_d2):
        q, d2 = md.closest_point_on_triangle(p, self.TRI)
        self.assertAlmostEqual(d2, want_d2, places=12)
        for i in range(3):
            self.assertAlmostEqual(q[i], want_point[i], places=12)

    def test_vertex_regions(self):
        self.check((-1, -1, 0), (0, 0, 0), 2.0)
        self.check((2, -1, 0), (1, 0, 0), 2.0)
        self.check((-1, 2, 0), (0, 1, 0), 2.0)

    def test_edge_regions(self):
        self.check((0.5, -1, 0), (0.5, 0, 0), 1.0)
        self.check((-1, 0.5, 0), (0, 0.5, 0), 1.0)
        self.check((1, 1, 0), (0.5, 0.5, 0), 0.5)

    def test_face_interior(self):
        self.check((0.25, 0.25, 5.0), (0.25, 0.25, 0.0), 25.0)

    def test_sliver_falls_back_to_edges(self):
        sliver = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.5, 1e-12, 0.0))
        q, d2 = md.closest_point_on_triangle((0.5, 1.0, 0.0), sliver)
        self.assertAlmostEqual(math.sqrt(d2), 1.0, places=6)
        self.assertFalse(math.isnan(q[0]))

    def test_collinear_triangle(self):
        collinear = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0))
        _q, d2 = md.closest_point_on_triangle((0.5, 1.0, 0.0), collinear)
        self.assertAlmostEqual(math.sqrt(d2), 1.0, places=12)


class TriTriTests(unittest.TestCase):
    def assert_consistent(self, pa, pb, d2):
        self.assertAlmostEqual(md._dist_sq(pa, pb), d2, places=9)

    def test_parallel_offset(self):
        a = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        b = ((0, 0, 0.5), (1, 0, 0.5), (0, 1, 0.5))
        pa, pb, d2 = md.tri_tri_closest(a, b)
        self.assertAlmostEqual(math.sqrt(d2), 0.5, places=12)
        self.assert_consistent(pa, pb, d2)

    def test_skew_edge_edge(self):
        a = ((0, 0, 0), (2, 0, 0), (1, -2, 0))
        b = ((1, 1, 1), (1, -1, 1), (3, 3, 1))
        pa, pb, d2 = md.tri_tri_closest(a, b)
        self.assertAlmostEqual(math.sqrt(d2), 1.0, places=12)
        self.assert_consistent(pa, pb, d2)

    def test_coplanar_separated(self):
        a = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        b = ((3, 0, 0), (4, 0, 0), (3, 1, 0))
        pa, pb, d2 = md.tri_tri_closest(a, b)
        self.assertAlmostEqual(math.sqrt(d2), 2.0, places=12)
        self.assert_consistent(pa, pb, d2)

    def test_vertex_touch_is_contact(self):
        a = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        b = ((1, 0, 0), (2, 1, 0), (2, -1, 0))
        _pa, _pb, d2 = md.tri_tri_closest(a, b)
        self.assertEqual(d2, 0.0)

    def test_shared_edge_is_contact(self):
        a = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        b = ((0, 0, 0), (1, 0, 0), (0, -1, 0))
        _pa, _pb, d2 = md.tri_tri_closest(a, b)
        self.assertEqual(d2, 0.0)

    def test_interpenetrating_plus_sign(self):
        # B's vertical edge passes through A's interior: none of the 15
        # boundary candidates is zero, ONLY the overlap test catches this.
        a = ((-1, 0, 0), (1, 0, 0), (0, 2, 0))
        b = ((0, 0.5, -1), (0, 0.5, 1), (0, 3, 0))
        pa, pb, d2 = md.tri_tri_closest(a, b)
        self.assertEqual(d2, 0.0)
        for v in pa + pb:
            self.assertFalse(math.isnan(v))
        # the contact point must genuinely lie on BOTH triangles (there are
        # multiple valid intersection points; any one of them is correct)
        _q, da = md.closest_point_on_triangle(pa, a)
        _q, db = md.closest_point_on_triangle(pa, b)
        self.assertLess(da, 1e-9)
        self.assertLess(db, 1e-9)

    def test_disjoint_far(self):
        a = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        b = ((10, 10, 10), (11, 10, 10), (10, 11, 10))
        self.assertFalse(md.tri_tri_overlap(a, b))
        _pa, _pb, d2 = md.tri_tri_closest(a, b)
        self.assertTrue(d2 > 200)


class MeshDistanceTests(unittest.TestCase):
    def test_cubes_with_gap(self):
        a = box_tris((0, 0, 0), (0.5, 0.5, 0.5))
        b = box_tris((1.25, 0, 0), (0.5, 0.5, 0.5))
        hit = md.mesh_min_distance(a, b, 0.5)
        self.assertIsNotNone(hit)
        d, pa, pb = hit
        self.assertAlmostEqual(d, 0.25, places=12)
        self.assertAlmostEqual(pa[0], 0.5, places=12)
        self.assertAlmostEqual(pb[0], 0.75, places=12)

    def test_cubes_beyond_cutoff(self):
        a = box_tris((0, 0, 0), (0.5, 0.5, 0.5))
        b = box_tris((2.0, 0, 0), (0.5, 0.5, 0.5))
        self.assertIsNone(md.mesh_min_distance(a, b, 0.5))

    def test_touching_cubes_contact(self):
        a = box_tris((0, 0, 0), (0.5, 0.5, 0.5))
        b = box_tris((1.0, 0, 0), (0.5, 0.5, 0.5))
        hit = md.mesh_min_distance(a, b, 0.1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], 0.0)

    def test_empty_mesh(self):
        a = box_tris((0, 0, 0), (0.5, 0.5, 0.5))
        self.assertIsNone(md.mesh_min_distance(a, [], 1.0))
        self.assertIsNone(md.mesh_min_distance([], a, 1.0))

    def test_THE_REGRESSION_diagonal_far_apart(self):
        """Diagonal pipe vs diagonal wall: AABBs overlap, true gap 3 ft.
        The V1 bbox detector called this a 1-inch near miss. Mesh distance
        must return None at a 1-inch cutoff."""
        # 20 ft long, thin, both rotated 45 degrees; offset 3.5 ft
        # perpendicular to their length -> true face-to-face gap 3.0 ft.
        pipe = box_tris((0, 0, 0), (10.0, 0.25, 0.25), rot_z_deg=45)
        off = 3.5 / math.sqrt(2.0)
        wall = box_tris((-off, off, 0), (10.0, 0.25, 4.0), rot_z_deg=45)

        # Sanity: their AABBs DO overlap (this is what fooled V1)
        def mesh_aabb(tris):
            mn = [1e30] * 3
            mx = [-1e30] * 3
            for t in tris:
                for p in t:
                    for i in range(3):
                        mn[i] = min(mn[i], p[i])
                        mx[i] = max(mx[i], p[i])
            return mn, mx
        mn_a, mx_a = mesh_aabb(pipe)
        mn_b, mx_b = mesh_aabb(wall)
        self.assertTrue(all(mn_a[i] <= mx_b[i] and mn_b[i] <= mx_a[i] for i in range(3)),
                        "test setup broken: AABBs should overlap")

        # The fix: true distance says NOT within 1 inch.
        self.assertIsNone(md.mesh_min_distance(pipe, wall, 1.0 / 12.0))

    def test_diagonal_true_near_miss(self):
        """Same diagonal geometry moved to a true 0.9-inch gap: must report,
        with the analytic gap and closest points on the facing faces."""
        gap_ft = 0.9 / 12.0
        pipe = box_tris((0, 0, 0), (10.0, 0.25, 0.25), rot_z_deg=45)
        off = (0.25 + gap_ft + 0.25) / 1.0    # centerline offset perpendicular
        offv = off / math.sqrt(2.0)
        wall = box_tris((-offv, offv, 0), (10.0, 0.25, 4.0), rot_z_deg=45)
        hit = md.mesh_min_distance(pipe, wall, 1.0 / 12.0)
        self.assertIsNotNone(hit)
        d, pa, pb = hit
        self.assertAlmostEqual(d, gap_ft, places=9)
        self.assertAlmostEqual(dist(pa, pb), d, places=9)

    def test_grid_path_matches_brute(self):
        rnd = random.Random(42)

        def soup(n, cx):
            tris = []
            for _ in range(n):
                base = (cx + rnd.uniform(-5, 5), rnd.uniform(-5, 5), rnd.uniform(-5, 5))
                tris.append((base,
                             (base[0] + rnd.uniform(0.1, 1), base[1], base[2]),
                             (base[0], base[1] + rnd.uniform(0.1, 1), base[2])))
            return tris
        a = soup(30, 0.0)
        b = soup(30, 4.0)
        cutoff = 3.0
        brute = md.mesh_min_distance(a, b, cutoff)
        # reference: plain min over every pair
        ref = None
        for ta in a:
            for tb in b:
                _pa, _pb, d2 = md.tri_tri_closest(ta, tb)
                if ref is None or d2 < ref:
                    ref = d2
        old_cap = md.BRUTE_PAIR_CAP
        try:
            md.BRUTE_PAIR_CAP = 1          # force the grid path
            grid = md.mesh_min_distance(a, b, cutoff)
        finally:
            md.BRUTE_PAIR_CAP = old_cap
        if ref is not None and math.sqrt(ref) <= cutoff:
            self.assertIsNotNone(brute)
            self.assertIsNotNone(grid)
            self.assertAlmostEqual(brute[0], math.sqrt(ref), places=9)
            self.assertAlmostEqual(grid[0], math.sqrt(ref), places=9)
        else:
            self.assertIsNone(brute)
            self.assertIsNone(grid)


if __name__ == "__main__":
    unittest.main()
