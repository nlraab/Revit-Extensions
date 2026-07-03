# -*- coding: utf-8 -*-
"""Grid-based AABB broad phase shared by the hard and soft engines.

Why this exists: on a real hospital model, "MEP vs Architecture" is
8,000+ host elements against 3,400+ link elements. Without a broad phase
the hard engine ran a PRECISE solid-intersection filter for every host
element against every link element (~27 million geometry tests -> the
45-minutes-and-counting run this module was written to kill), and the
soft engine compared every bbox pair in a pure-IronPython double loop.
A uniform grid over one side's bboxes turns both into "look only at the
handful of things that could possibly touch".

Pure data - plain tuples in, dict out - so it unit-tests in CPython and
never imports the Revit API. It PRUNES only, never decides: every real
intersection survives (a solid overlap implies a bbox overlap, and
transformed AABBs only ever grow), and unknown (None) bboxes are treated
conservatively so an element without a bbox is never silently dropped.
"""


# A bbox spanning more cells than this is treated as UNKNOWN (conservative
# candidate-everything) instead of gridded: one corrupt import with a
# stray vertex miles away must never hang the grid or OOM the run.
_MAX_CELLS_PER_ITEM = 50000
# Coordinates beyond this (feet) are garbage; Revit's own world is far
# smaller. Non-finite values (NaN/inf) are rejected by the v != v check.
_MAX_ABS_COORD = 1.0e9


def candidate_map(a_items, b_items, pad_ft=0.0, cell_ft=10.0):
    """Map each A key to the B keys whose bboxes could touch it.

    Args:
        a_items / b_items: iterable of (key, bbox) where bbox is
            (minx, miny, minz, maxx, maxy, maxz) in ONE shared coordinate
            system (host feet), or None when unknown.
        pad_ft: inflation applied to the A side (tolerance + slack for
            soft tests; a small safety margin for hard tests).
        cell_ft: grid cell size. Long elements (walls, mains) simply span
            several cells; correctness never depends on this value.

    Returns:
        {a_key: [b_key, ...]} - only entries with at least one candidate.
        Conservative on unknowns: an A with bbox None gets EVERY B key;
        a B with bbox None appears in EVERY A's list.
    """
    a_items = list(a_items)
    b_items = list(b_items)
    if not a_items or not b_items:
        return {}
    cell = float(cell_ft) if cell_ft > 0 else 10.0
    pad = float(pad_ft)

    grid = {}
    b_unknown = []
    for key, bb in b_items:
        rng = _grid_range(bb, cell)
        if rng is None:
            b_unknown.append(key)     # unknown/garbage: always a candidate
            continue
        for cx in range(rng[0][0], rng[0][1] + 1):
            for cy in range(rng[1][0], rng[1][1] + 1):
                for cz in range(rng[2][0], rng[2][1] + 1):
                    grid.setdefault((cx, cy, cz), []).append((key, bb))

    all_b_keys = [key for key, _bb in b_items]
    out = {}
    for a_key, bb in a_items:
        qb = None
        if bb is not None:
            qb = (bb[0] - pad, bb[1] - pad, bb[2] - pad,
                  bb[3] + pad, bb[4] + pad, bb[5] + pad)
        rng = _grid_range(qb, cell)
        if rng is None:
            out[a_key] = list(all_b_keys)   # unknown/garbage: check everything
            continue
        seen = set()
        found = []
        for cx in range(rng[0][0], rng[0][1] + 1):
            for cy in range(rng[1][0], rng[1][1] + 1):
                for cz in range(rng[2][0], rng[2][1] + 1):
                    for b_key, b_bb in grid.get((cx, cy, cz), ()):
                        if b_key in seen:
                            continue
                        seen.add(b_key)
                        if overlaps(qb, b_bb):
                            found.append(b_key)
        for b_key in b_unknown:
            if b_key not in seen:
                found.append(b_key)
        if found:
            out[a_key] = found
    return out


def _grid_range(bb, cell):
    """Per-axis (lo, hi) cell ranges for a bbox, or None when the bbox is
    unusable: None, non-finite, absurd coordinates, inverted, or spanning
    more than _MAX_CELLS_PER_ITEM cells. None means "treat as unknown"
    (conservative), never "drop"."""
    if bb is None:
        return None
    total = 1
    rng = []
    try:
        for i in (0, 1, 2):
            lo_v, hi_v = bb[i], bb[i + 3]
            if (lo_v != lo_v or hi_v != hi_v          # NaN
                    or lo_v < -_MAX_ABS_COORD or hi_v > _MAX_ABS_COORD
                    or hi_v < lo_v):
                return None
            lo = int(lo_v // cell)
            hi = int(hi_v // cell)
            total *= (hi - lo + 1)
            if total > _MAX_CELLS_PER_ITEM:
                return None
            rng.append((lo, hi))
    except (ValueError, OverflowError):
        return None
    return rng


def overlaps(a, b):
    """Axis-aligned overlap test on (minx,miny,minz,maxx,maxy,maxz) tuples.
    Touching counts as overlapping."""
    return (a[0] <= b[3] and b[0] <= a[3] and
            a[1] <= b[4] and b[1] <= a[4] and
            a[2] <= b[5] and b[2] <= a[5])


def bbox_tuple(bb):
    """Plain-tuple view of a Revit BoundingBoxXYZ, sanitized: returns None
    for unreadable, non-finite, absurdly distant, or inverted boxes so
    garbage geometry degrades to the conservative unknown path instead of
    crashing the grid (None means "always a candidate", never "dropped")."""
    if bb is None:
        return None
    try:
        t = (float(bb.Min.X), float(bb.Min.Y), float(bb.Min.Z),
             float(bb.Max.X), float(bb.Max.Y), float(bb.Max.Z))
    except Exception:
        return None
    for v in t:
        if v != v or v < -_MAX_ABS_COORD or v > _MAX_ABS_COORD:
            return None
    if t[0] > t[3] or t[1] > t[4] or t[2] > t[5]:
        return None
    return t
