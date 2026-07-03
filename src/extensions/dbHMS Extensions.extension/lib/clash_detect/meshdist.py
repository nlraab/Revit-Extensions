# -*- coding: utf-8 -*-
"""Pure-math minimum distance between triangle meshes.

The narrow phase of soft ("near miss") clash detection: given two triangle
meshes in the same coordinate space (host feet), find the true minimum
surface-to-surface distance, the closest-point pair, and whether the meshes
touch/intersect. This is what replaces the V1 bounding-box-only soft check
that reported diagonal elements feet apart as "near misses" (AABBs of
diagonal geometry are hugely conservative).

Zero Revit imports, tuples and floats only, Python 2/3 compatible, so the
whole module is unit-tested in CPython (tests/test_clash_meshdist.py) --
including the diagonal-pipe-vs-diagonal-wall regression case.

Formulations: point-triangle via Voronoi-region walk and segment-segment via
clamped closest points (Ericson, "Real-Time Collision Detection" 5.1.5 /
5.1.9); triangle overlap via Moller's interval test. For NON-intersecting
triangles the minimum distance is always achieved by one of 15 candidates
(9 edge-edge + 6 vertex-face); intersection must be tested separately, which
is why tri_tri_closest runs the overlap test first.

A point is a (x, y, z) float tuple; a triangle is a (p0, p1, p2) tuple of
points; a mesh is a list of triangles.
"""

from math import sqrt

# Absolute epsilon for "touching" (squared feet). 1e-6 ft ~ 0.3 micron.
CONTACT_EPS_SQ = 1e-12
# Relative epsilon for degenerate/parallel guards.
_REL_EPS = 1e-12

# Brute-force pairing cap: above this many triangle pairs, hash one mesh
# into a uniform grid instead of testing every pair's AABBs.
BRUTE_PAIR_CAP = 250000


# ---------------------------------------------------------------------------
# Vector helpers (tuples in, tuples out)
# ---------------------------------------------------------------------------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _len_sq(a):
    return a[0] * a[0] + a[1] * a[1] + a[2] * a[2]


def _dist_sq(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return dx * dx + dy * dy + dz * dz


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def _clamp01(x):
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


# ---------------------------------------------------------------------------
# Point / segment / triangle primitives
# ---------------------------------------------------------------------------

def closest_point_on_segment(p, a, b):
    """Closest point to `p` on segment ab. Returns (point, dist_sq)."""
    ab = _sub(b, a)
    denom = _len_sq(ab)
    if denom <= 0.0:
        return a, _dist_sq(p, a)
    t = _clamp01(_dot(_sub(p, a), ab) / denom)
    q = _lerp(a, b, t)
    return q, _dist_sq(p, q)


def closest_point_on_triangle(p, tri):
    """Closest point to `p` on triangle `tri` (Ericson 5.1.5 Voronoi walk).
    Returns (point, dist_sq). Degenerate (sliver) triangles fall back to the
    nearest of the three edges, which is exact for a collapsed triangle."""
    a, b, c = tri
    ab = _sub(b, a)
    ac = _sub(c, a)

    # Sliver guard: near-zero area makes the interior barycentric math
    # ill-conditioned; the boundary (edges) IS the triangle then.
    n = _cross(ab, ac)
    if _len_sq(n) < _REL_EPS * _len_sq(ab) * _len_sq(ac):
        best_q, best_d = closest_point_on_segment(p, a, b)
        q, d = closest_point_on_segment(p, b, c)
        if d < best_d:
            best_q, best_d = q, d
        q, d = closest_point_on_segment(p, a, c)
        if d < best_d:
            best_q, best_d = q, d
        return best_q, best_d

    ap = _sub(p, a)
    d1 = _dot(ab, ap)
    d2 = _dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return a, _dist_sq(p, a)                    # vertex region A

    bp = _sub(p, b)
    d3 = _dot(ab, bp)
    d4 = _dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return b, _dist_sq(p, b)                    # vertex region B

    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / (d1 - d3)
        q = _lerp(a, b, v)                          # edge region AB
        return q, _dist_sq(p, q)

    cp = _sub(p, c)
    d5 = _dot(ab, cp)
    d6 = _dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return c, _dist_sq(p, c)                    # vertex region C

    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / (d2 - d6)
        q = _lerp(a, c, w)                          # edge region AC
        return q, _dist_sq(p, q)

    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        q = _lerp(b, c, w)                          # edge region BC
        return q, _dist_sq(p, q)

    denom = 1.0 / (va + vb + vc)                    # face interior
    v = vb * denom
    w = vc * denom
    q = (a[0] + ab[0] * v + ac[0] * w,
         a[1] + ab[1] * v + ac[1] * w,
         a[2] + ab[2] * v + ac[2] * w)
    return q, _dist_sq(p, q)


def closest_points_segments(p1, q1, p2, q2):
    """Closest points between segments p1q1 and p2q2 (Ericson 5.1.9).
    Returns (c1, c2, dist_sq). Handles zero-length and near-parallel."""
    d1 = _sub(q1, p1)
    d2 = _sub(q2, p2)
    r = _sub(p1, p2)
    a = _len_sq(d1)
    e = _len_sq(d2)
    f = _dot(d2, r)

    if a <= 0.0 and e <= 0.0:
        return p1, p2, _dist_sq(p1, p2)             # two points
    if a <= 0.0:
        t = _clamp01(f / e)                         # first is a point
        c2 = _lerp(p2, q2, t)
        return p1, c2, _dist_sq(p1, c2)
    c = _dot(d1, r)
    if e <= 0.0:
        s = _clamp01(-c / a)                        # second is a point
        c1 = _lerp(p1, q1, s)
        return c1, p2, _dist_sq(c1, p2)

    b = _dot(d1, d2)
    denom = a * e - b * b
    # Relative parallel guard: denom is ~0 for (near-)parallel segments; any
    # s works then, clamping picks a consistent end.
    if denom > _REL_EPS * a * e:
        s = _clamp01((b * f - c * e) / denom)
    else:
        s = 0.0
    t = (b * s + f) / e
    if t < 0.0:
        t = 0.0
        s = _clamp01(-c / a)
    elif t > 1.0:
        t = 1.0
        s = _clamp01((b - c) / a)
    c1 = _lerp(p1, q1, s)
    c2 = _lerp(p2, q2, t)
    return c1, c2, _dist_sq(c1, c2)


# ---------------------------------------------------------------------------
# Triangle-triangle overlap (Moller interval test)
# ---------------------------------------------------------------------------

def _signed_dists(tri, n, d):
    return (_dot(n, tri[0]) + d, _dot(n, tri[1]) + d, _dot(n, tri[2]) + d)


def _same_side(ds, eps):
    return (ds[0] > eps and ds[1] > eps and ds[2] > eps) or \
           (ds[0] < -eps and ds[1] < -eps and ds[2] < -eps)


def _interval_on_line(tri, ds, proj):
    """Interval of the triangle on the intersection line. `proj` are the
    projections of the vertices onto the line direction; `ds` the signed
    plane distances. Standard Moller construction: the two edges that cross
    the plane define the interval."""
    # Order vertices so the lone-signed one is v0.
    d0, d1, d2 = ds
    if (d0 > 0 and d1 <= 0 and d2 <= 0) or (d0 < 0 and d1 >= 0 and d2 >= 0) or \
       (d0 == 0 and (d1 * d2) > 0):
        i0, i1, i2 = 0, 1, 2
    elif (d1 > 0 and d0 <= 0 and d2 <= 0) or (d1 < 0 and d0 >= 0 and d2 >= 0) or \
         (d1 == 0 and (d0 * d2) > 0):
        i0, i1, i2 = 1, 0, 2
    else:
        i0, i1, i2 = 2, 0, 1
    dd0, dd1, dd2 = ds[i0], ds[i1], ds[i2]
    pp0, pp1, pp2 = proj[i0], proj[i1], proj[i2]
    denom1 = dd0 - dd1
    denom2 = dd0 - dd2
    t1 = pp0 + (pp1 - pp0) * (dd0 / denom1) if denom1 != 0.0 else pp0
    t2 = pp0 + (pp2 - pp0) * (dd0 / denom2) if denom2 != 0.0 else pp0
    return (t1, t2) if t1 <= t2 else (t2, t1)


def _tri_tri_overlap_coplanar(tri_a, tri_b, n):
    """2D overlap test for coplanar triangles: project onto the dominant
    axis plane, then edge-vs-edge intersection + containment checks."""
    ax, ay, az = abs(n[0]), abs(n[1]), abs(n[2])
    if ax >= ay and ax >= az:
        u, v = 1, 2
    elif ay >= az:
        u, v = 0, 2
    else:
        u, v = 0, 1
    a2 = [(p[u], p[v]) for p in tri_a]
    b2 = [(p[u], p[v]) for p in tri_b]

    def on_segment(a, b, c):
        # c is collinear with ab: is it within ab's bounding interval?
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and
                min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))

    def seg_intersect(p, q, r, s):
        # CLRS orientation test with explicit collinear handling: two
        # collinear-but-disjoint edges have all four cross products zero,
        # which a naive sign test wrongly reports as an intersection.
        d1 = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        d2 = (q[0] - p[0]) * (s[1] - p[1]) - (q[1] - p[1]) * (s[0] - p[0])
        d3 = (s[0] - r[0]) * (p[1] - r[1]) - (s[1] - r[1]) * (p[0] - r[0])
        d4 = (s[0] - r[0]) * (q[1] - r[1]) - (s[1] - r[1]) * (q[0] - r[0])
        if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
           ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
            return True
        if d1 == 0.0 and on_segment(p, q, r):
            return True
        if d2 == 0.0 and on_segment(p, q, s):
            return True
        if d3 == 0.0 and on_segment(r, s, p):
            return True
        if d4 == 0.0 and on_segment(r, s, q):
            return True
        return False

    def point_in(p, t):
        s0 = (t[1][0] - t[0][0]) * (p[1] - t[0][1]) - (t[1][1] - t[0][1]) * (p[0] - t[0][0])
        s1 = (t[2][0] - t[1][0]) * (p[1] - t[1][1]) - (t[2][1] - t[1][1]) * (p[0] - t[1][0])
        s2 = (t[0][0] - t[2][0]) * (p[1] - t[2][1]) - (t[0][1] - t[2][1]) * (p[0] - t[2][0])
        return (s0 >= 0 and s1 >= 0 and s2 >= 0) or (s0 <= 0 and s1 <= 0 and s2 <= 0)

    for i in range(3):
        for j in range(3):
            if seg_intersect(a2[i], a2[(i + 1) % 3], b2[j], b2[(j + 1) % 3]):
                return True
    return point_in(a2[0], b2) or point_in(b2[0], a2)


def tri_tri_overlap(tri_a, tri_b):
    """True if the two triangles intersect (share any point), including the
    coplanar case. Moller's interval method."""
    n1 = _cross(_sub(tri_a[1], tri_a[0]), _sub(tri_a[2], tri_a[0]))
    n2 = _cross(_sub(tri_b[1], tri_b[0]), _sub(tri_b[2], tri_b[0]))
    if _len_sq(n1) <= 0.0 or _len_sq(n2) <= 0.0:
        # Degenerate input: no interior to intersect; the distance candidates
        # cover boundary contact, so report no overlap here.
        return False
    d2 = -_dot(n2, tri_b[0])
    ds_a = _signed_dists(tri_a, n2, d2)
    eps_a = _REL_EPS * max(abs(ds_a[0]), abs(ds_a[1]), abs(ds_a[2]), 1e-30)
    if _same_side(ds_a, eps_a):
        return False
    d1 = -_dot(n1, tri_a[0])
    ds_b = _signed_dists(tri_b, n1, d1)
    eps_b = _REL_EPS * max(abs(ds_b[0]), abs(ds_b[1]), abs(ds_b[2]), 1e-30)
    if _same_side(ds_b, eps_b):
        return False
    if abs(ds_a[0]) <= eps_a and abs(ds_a[1]) <= eps_a and abs(ds_a[2]) <= eps_a:
        return _tri_tri_overlap_coplanar(tri_a, tri_b, n1)
    # Interval overlap on the intersection line.
    line = _cross(n1, n2)
    lx, ly, lz = abs(line[0]), abs(line[1]), abs(line[2])
    if lx >= ly and lx >= lz:
        axis = 0
    elif ly >= lz:
        axis = 1
    else:
        axis = 2
    proj_a = (tri_a[0][axis], tri_a[1][axis], tri_a[2][axis])
    proj_b = (tri_b[0][axis], tri_b[1][axis], tri_b[2][axis])
    a1, a2i = _interval_on_line(tri_a, ds_a, proj_a)
    b1, b2i = _interval_on_line(tri_b, ds_b, proj_b)
    return a1 <= b2i and b1 <= a2i


def _contact_point(tri_a, tri_b):
    """A representative contact point for two INTERSECTING triangles: the
    first edge-plane crossing that lands inside the other triangle. Falls
    back to a vertex midpoint (still finite/near the pair) if the edge walk
    is defeated by degeneracy."""
    for ta, tb in ((tri_a, tri_b), (tri_b, tri_a)):
        n = _cross(_sub(tb[1], tb[0]), _sub(tb[2], tb[0]))
        nn = _len_sq(n)
        if nn <= 0.0:
            continue
        d = -_dot(n, tb[0])
        for i in range(3):
            p = ta[i]
            q = ta[(i + 1) % 3]
            dp = _dot(n, p) + d
            dq = _dot(n, q) + d
            if dp * dq > 0.0 or dp == dq:
                continue
            t = dp / (dp - dq)
            x = _lerp(p, q, t)
            cp, dist_sq = closest_point_on_triangle(x, tb)
            if dist_sq <= max(CONTACT_EPS_SQ, _REL_EPS * nn):
                return x
    return _mul(_add(tri_a[0], tri_b[0]), 0.5)


def tri_tri_closest(tri_a, tri_b):
    """Minimum distance between two triangles. Returns (pa, pb, dist_sq).
    Intersecting triangles return dist_sq 0.0 with pa == pb at a contact
    point. Non-intersecting: the exact minimum over 9 edge-edge + 6
    vertex-face candidates (sufficient for disjoint triangles)."""
    if tri_tri_overlap(tri_a, tri_b):
        p = _contact_point(tri_a, tri_b)
        return p, p, 0.0
    best = None
    for i in range(3):
        p1 = tri_a[i]
        q1 = tri_a[(i + 1) % 3]
        for j in range(3):
            c1, c2, d = closest_points_segments(p1, q1, tri_b[j], tri_b[(j + 1) % 3])
            if best is None or d < best[2]:
                best = (c1, c2, d)
    for i in range(3):
        q, d = closest_point_on_triangle(tri_a[i], tri_b)
        if d < best[2]:
            best = (tri_a[i], q, d)
    for j in range(3):
        q, d = closest_point_on_triangle(tri_b[j], tri_a)
        if d < best[2]:
            best = (q, tri_b[j], d)
    return best


# ---------------------------------------------------------------------------
# Mesh-level query
# ---------------------------------------------------------------------------

def tri_aabb(tri):
    """((minx,miny,minz), (maxx,maxy,maxz)) of a triangle."""
    xs = (tri[0][0], tri[1][0], tri[2][0])
    ys = (tri[0][1], tri[1][1], tri[2][1])
    zs = (tri[0][2], tri[1][2], tri[2][2])
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def aabb_gap_sq(min_a, max_a, min_b, max_b):
    """Squared minimum distance between two AABBs (0 if they overlap).
    This is a LOWER BOUND on the distance between any geometry inside them."""
    d = 0.0
    for i in range(3):
        lo = min_b[i] - max_a[i]
        if lo > 0.0:
            d += lo * lo
        else:
            hi = min_a[i] - max_b[i]
            if hi > 0.0:
                d += hi * hi
    return d


def _grid_candidate_pairs(boxes_a, boxes_b, cutoff):
    """Uniform-grid pairing for large meshes: hash B's triangle AABBs into
    grid cells, query each A box inflated by the cutoff. Returns index pairs
    (i, j). Pure dict/set machinery, no recursion."""
    # Cell size: the cutoff or the median B extent, whichever is larger,
    # so boxes rarely straddle more than a few cells.
    extents = []
    for mn, mx in boxes_b:
        extents.append(max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]))
    extents.sort()
    med = extents[len(extents) // 2] if extents else 1.0
    cell = max(cutoff, med, 1e-6)
    inv = 1.0 / cell

    grid = {}
    for j, (mn, mx) in enumerate(boxes_b):
        x0 = int(mn[0] * inv)
        y0 = int(mn[1] * inv)
        z0 = int(mn[2] * inv)
        x1 = int(mx[0] * inv)
        y1 = int(mx[1] * inv)
        z1 = int(mx[2] * inv)
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                for cz in range(z0, z1 + 1):
                    grid.setdefault((cx, cy, cz), []).append(j)

    pairs = []
    for i, (mn, mx) in enumerate(boxes_a):
        x0 = int((mn[0] - cutoff) * inv)
        y0 = int((mn[1] - cutoff) * inv)
        z0 = int((mn[2] - cutoff) * inv)
        x1 = int((mx[0] + cutoff) * inv)
        y1 = int((mx[1] + cutoff) * inv)
        z1 = int((mx[2] + cutoff) * inv)
        seen = set()
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                for cz in range(z0, z1 + 1):
                    for j in grid.get((cx, cy, cz), ()):
                        if j not in seen:
                            seen.add(j)
                            pairs.append((i, j))
    return pairs


def mesh_min_distance(tris_a, tris_b, cutoff_ft):
    """True minimum distance between two triangle meshes, bounded by a
    cutoff. Returns None when the real distance exceeds `cutoff_ft`, else
    (dist_ft, point_a, point_b) with dist_ft == |point_a - point_b|.

    Strategy: per-triangle AABB lower bounds pruned against the best
    distance so far, ascending walk so the tightest candidates run first,
    exact triangle-triangle distance only for survivors, hard exit on
    contact. Intersecting meshes return (0.0, p, p)."""
    if not tris_a or not tris_b:
        return None
    boxes_a = [tri_aabb(t) for t in tris_a]
    boxes_b = [tri_aabb(t) for t in tris_b]

    # Inclusive tolerance: a pair at exactly the cutoff still reports.
    best_sq = cutoff_ft * cutoff_ft + 1e-12
    best = None

    if len(tris_a) * len(tris_b) <= BRUTE_PAIR_CAP:
        candidates = []
        for i in range(len(tris_a)):
            mna, mxa = boxes_a[i]
            for j in range(len(tris_b)):
                mnb, mxb = boxes_b[j]
                lb = aabb_gap_sq(mna, mxa, mnb, mxb)
                if lb < best_sq:
                    candidates.append((lb, i, j))
    else:
        candidates = []
        for i, j in _grid_candidate_pairs(boxes_a, boxes_b, cutoff_ft):
            mna, mxa = boxes_a[i]
            mnb, mxb = boxes_b[j]
            lb = aabb_gap_sq(mna, mxa, mnb, mxb)
            if lb < best_sq:
                candidates.append((lb, i, j))

    candidates.sort()
    for lb, i, j in candidates:
        if lb >= best_sq:
            break            # sorted ascending: nothing later can win
        pa, pb, d_sq = tri_tri_closest(tris_a[i], tris_b[j])
        if d_sq < best_sq:
            best_sq = d_sq
            best = (pa, pb)
            if best_sq <= CONTACT_EPS_SQ:
                break        # touching/intersecting: nothing can beat 0
    if best is None:
        return None
    return sqrt(best_sq), best[0], best[1]
