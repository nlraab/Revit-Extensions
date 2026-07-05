# -*- coding: utf-8 -*-
"""Run-level dedupe of raw clashes before they hit the merge layer.

Detection orchestration runs every test that's in scope and concatenates
all raw clashes. The same physical pair of elements can show up in:

  * a hard test AND a soft-clearance test (the duct hits the wall AND
    is also "within 1in of" the wall — the soft test still flags it
    because the hard intersection is also within tolerance), or
  * two different hard tests whose category lists overlap (rare with
    the four firm defaults but possible with custom project tests).

For the user, those are duplicate rows in the Browser. The dedupe rule
here is intentionally narrow: drop SOFT clashes whose pair already has
a HARD clash in this run. A pair that actually intersects isn't a
"near miss" anymore — it's a hit. Hard-vs-hard duplicates from
overlapping test scopes are left alone for now (no case in production
yet, and collapsing them would lose the per-test_id provenance that
the clash fingerprint relies on).

Pure Python — no Revit imports. Runs in the CPython test suite.
"""


def drop_soft_overlapping_hard(raw_clashes):
    """Drop soft clashes whose element pair also has a hard clash this run.

    Pair identity uses (source, element_id, link_doc_title) for both
    sides, normalized to a sorted tuple so swapping A/B produces the
    same key. link_doc_title disambiguates the (rare) case where the
    same element_id exists in two different linked .rvt files.

    Returns (filtered_list, dropped_count). The order of filtered_list
    preserves the input order of the kept clashes.
    """
    if not raw_clashes:
        return [], 0
    hard_pairs = set()
    for c in raw_clashes:
        if (c or {}).get('kind') != 'hard':
            continue
        key = _pair_key(c)
        if key:
            hard_pairs.add(key)
    if not hard_pairs:
        return list(raw_clashes), 0
    out = []
    dropped = 0
    for c in raw_clashes:
        if (c or {}).get('kind') == 'soft':
            key = _pair_key(c)
            if key in hard_pairs:
                dropped += 1
                continue
        out.append(c)
    return out, dropped


def _pair_key(clash):
    """Build the dedupe key for a clash — a sorted-pair tuple of element refs.

    Returns None if either side has no element_id (defensive — these
    couldn't have come from real detection).
    """
    a = clash.get('ref_a') or {}
    b = clash.get('ref_b') or {}
    a_id = a.get('element_id')
    b_id = b.get('element_id')
    if not a_id or not b_id:
        return None
    a_key = (a.get('source') or 'host', int(a_id), a.get('link_doc_title'))
    b_key = (b.get('source') or 'host', int(b_id), b.get('link_doc_title'))
    return tuple(sorted([a_key, b_key]))


# ---------------------------------------------------------------------------
# Multi-layer penetration collapse (V3 finding): a single MEP run passing
# through one floor/roof/ceiling assembly clashes with EVERY modeled layer
# (structural slab + topping + metal deck + finish floor + rendering), so one
# physical penetration becomes four agenda rows. Collapse each cluster to the
# most significant layer. Pure Python; runs on raw clashes before merge.
# ---------------------------------------------------------------------------

_LAYERED_CATS = ('Floors', 'Roofs', 'Ceilings')
_ROUTED_CATS = ('Ducts', 'Duct Fittings', 'Duct Accessories', 'Flex Ducts',
                'Pipes', 'Pipe Fittings', 'Pipe Accessories', 'Flex Pipes',
                'Conduits', 'Conduit Fittings', 'Cable Trays',
                'Cable Tray Fittings')


def collapse_layered_penetrations(raw_clashes, radius_ft=3.0):
    """Collapse the redundant clashes one MEP run makes against the stacked
    layers of a single floor/roof/ceiling assembly into ONE, keeping the most
    significant layer (structural, then thickest, then lowest element id for a
    stable fingerprint). Only clashes of the SAME mover against the SAME
    layered category that sit in one spatial cluster collapse, so a riser
    through three floor LEVELS stays three penetrations.

    Returns (filtered_list, dropped_count). Input order of kept rows is not
    guaranteed (grouped), which the merge layer does not depend on."""
    if not raw_clashes:
        return [], 0
    out = []
    groups = {}
    for c in raw_clashes:
        sides = _penetration_sides(c)
        if sides is None:
            out.append(c)
            continue
        groups.setdefault(sides, []).append(c)
    dropped = 0
    for rows in groups.values():
        for cluster in _cluster_by_midpoint(rows, radius_ft):
            if len(cluster) == 1:
                out.append(cluster[0])
            else:
                out.append(_primary_layer(cluster))
                dropped += len(cluster) - 1
    return out, dropped


def _penetration_sides(c):
    """((mep source, id, link), layered category) for a routed-MEP-vs-stacked-
    layer clash, else None."""
    a = c.get('ref_a') or {}
    b = c.get('ref_b') or {}
    ca, cb = a.get('category'), b.get('category')
    if cb in _LAYERED_CATS and ca in _ROUTED_CATS:
        return (_mep_key(a), cb)
    if ca in _LAYERED_CATS and cb in _ROUTED_CATS:
        return (_mep_key(b), ca)
    return None


def _mep_key(ref):
    return (ref.get('source') or 'host', ref.get('element_id'),
            ref.get('link_doc_title'))


def _layer_ref(c):
    b = c.get('ref_b') or {}
    if b.get('category') in _LAYERED_CATS:
        return b
    return c.get('ref_a') or {}


def _cluster_by_midpoint(rows, radius_ft):
    """Single-linkage clusters of clashes within radius_ft (3D midpoint). Groups
    are small (one mover's layer hits), so O(n^2) is fine. Rows lacking a
    midpoint each stand alone."""
    n = len(rows)
    pts = []
    for c in rows:
        m = c.get('midpoint')
        pts.append(m if (m and len(m) >= 3) else None)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    r2 = radius_ft * radius_ft
    for i in range(n):
        if pts[i] is None:
            continue
        for j in range(i + 1, n):
            if pts[j] is None:
                continue
            dx = pts[i][0] - pts[j][0]
            dy = pts[i][1] - pts[j][1]
            dz = pts[i][2] - pts[j][2]
            if dx * dx + dy * dy + dz * dz <= r2:
                parent[find(i)] = find(j)
    clusters = {}
    for i in range(n):
        key = find(i) if pts[i] is not None else ('solo', i)
        clusters.setdefault(key, []).append(rows[i])
    return list(clusters.values())


def _primary_layer(cluster):
    """Keep the most significant clash in the cluster: a hard hit before a soft
    near-miss (never hide a real intersection behind a clearance), then the
    structural layer, then the thickest, then the lowest element id
    (deterministic -> stable fingerprint across runs)."""
    def rank(c):
        hard = 1 if (c.get('kind') or 'hard') == 'hard' else 0
        r = _layer_ref(c)
        struct = 1 if r.get('is_structural') is True else 0
        try:
            thick = float(r.get('thickness_in') or 0.0)
        except (TypeError, ValueError):
            thick = 0.0
        eid = r.get('element_id') or 0
        return (hard, struct, thick, -eid)
    return max(cluster, key=rank)
