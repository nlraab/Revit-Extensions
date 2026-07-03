# -*- coding: utf-8 -*-
"""Soft-clash detection - "near miss" proximity within a tolerance, measured
on TRUE GEOMETRY.

Two phases:
  1. Broad phase (cheap): axis-aligned bounding boxes in host coordinates
     (link boxes through the link transform), set_a's inflated by the
     tolerance plus a small slack; overlapping pairs become candidates.
     AABBs of diagonal geometry are hugely conservative, so this phase
     OVER-reports by design -- it only prunes, it never decides.
  2. Narrow phase (exact): each candidate pair is tessellated to triangles
     (clash_detect.tess, cached per element) and the true minimum
     surface-to-surface distance computed (clash_detect.meshdist, pure
     math, unit-tested in CPython). Only pairs whose REAL distance is
     within tolerance are reported, with the measured gap, the closest-
     point pair, and a midpoint on real geometry.

The V1 of this module reported the broad phase directly, which flagged
diagonal elements FEET apart as near misses (the regression case in
tests/test_clash_meshdist.py). Never decide from bounding boxes.

Elements with no usable solids (mesh-only imports, symbolic families) fall
back to the bbox answer, flagged gap_method='bbox', so nothing is silently
dropped; everything measured carries gap_method='mesh'.

Note on accuracy: tessellation approximates curved faces with chords, so a
measured gap can read up to ~0.1" over the exact surface distance on
typical pipe diameters. The tolerance test uses the measured mesh distance
directly (no compensation) -- a boundary case within chord error of the
tolerance is noise either way.
"""

from clash_detect import hard
from clash_detect import meshdist
from clash_detect import tess


INCHES_PER_FOOT = 12.0

# Extra broad-phase inflation beyond the tolerance: cheap insurance against
# marginally loose Revit bboxes (the narrow phase discards the excess).
BROAD_SLACK_FT = 0.5 / 12.0


def find_soft_clashes(doc, set_a_elements, set_b_elements, tolerance_inches,
                      a_link_instance=None, b_link_instance=None,
                      tess_cache=None, log=None, progress=None):
    """Find pairs in (set_a x set_b) whose TRUE minimum surface-to-surface
    distance is within `tolerance_inches` (inclusive; touching/intersecting
    pairs report gap 0 and are typically removed later by
    dedupe.drop_soft_overlapping_hard when a matching hard test ran).

    Returns list of dicts:
        {'elem_a': Element, 'elem_b': Element,
         'midpoint': (x,y,z) between the closest points (host feet),
         'gap_inches': REAL measured surface gap,
         'closest_point_a': (x,y,z), 'closest_point_b': (x,y,z),
         'is_contact': bool, 'gap_method': 'mesh' | 'bbox'}

    `tess_cache` is a dict shared across tests in a run (see tess.py);
    `log` receives one-line diagnostics.
    """
    if tolerance_inches <= 0:
        # No tolerance = caller should be running hard detection instead.
        return []

    if not set_a_elements or not set_b_elements:
        return []

    tolerance_ft = float(tolerance_inches) / INCHES_PER_FOOT
    if tess_cache is None:
        tess_cache = {}

    # ---- broad phase: AABB candidates (prunes only, never decides) ------
    a_boxes = []  # list of (elem, raw_bbox_host_coords)
    for elem in set_a_elements:
        bb = _bbox_in_host(elem, a_link_instance)
        if bb is not None:
            a_boxes.append((elem, bb))

    b_boxes = []
    for elem in set_b_elements:
        bb = _bbox_in_host(elem, b_link_instance)
        if bb is not None:
            b_boxes.append((elem, bb))

    from clash_detect import broadphase
    from clash_detect._compat import eid_int

    candidates = []
    seen_pairs = set()
    same_doc = (a_link_instance is None) == (b_link_instance is None) and \
               (a_link_instance is None or eid_int(a_link_instance.Id) == (
                   eid_int(b_link_instance.Id) if b_link_instance else None))

    # Grid broad phase (clash_detect/broadphase) instead of the previous
    # A x B double loop: same candidates, but 8000 x 3000 element tests no
    # longer cost 24M pure-IronPython iterations.
    # An element whose id can't be read (eid_int returns 0 on failure)
    # gets a unique synthetic negative key so two such elements never
    # collide in the by-id maps and silently vanish from detection.
    synth = [0]

    def _key(elem):
        try:
            k = eid_int(elem.Id)
        except Exception:
            k = 0
        if not k:
            synth[0] -= 1
            k = synth[0]
        return k

    a_by_id = {}
    a_items = []
    for elem, bb in a_boxes:
        key = _key(elem)
        a_by_id[key] = (elem, bb)
        a_items.append((key, broadphase.bbox_tuple(bb)))
    b_by_id = {}
    b_items = []
    for elem, bb in b_boxes:
        key = _key(elem)
        b_by_id[key] = (elem, bb)
        b_items.append((key, broadphase.bbox_tuple(bb)))
    cand_map = broadphase.candidate_map(
        a_items, b_items, pad_ft=tolerance_ft + BROAD_SLACK_FT)
    for a_id in sorted(cand_map.keys()):
        elem_a, bb_a = a_by_id[a_id]
        for b_id in cand_map[a_id]:
            # Skip self-pairs only when both are from the same doc
            if same_doc and a_id == b_id:
                continue
            pair_key = (min(a_id, b_id), max(a_id, b_id)) if same_doc else (a_id, b_id)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            elem_b, bb_b = b_by_id[b_id]
            candidates.append((elem_a, bb_a, elem_b, bb_b))

    # ---- narrow phase: true mesh distance per candidate ------------------
    out = []
    n_mesh = 0
    n_fallback = 0
    n_contact = 0
    from clash_detect import hard as hard_mod
    beat = hard_mod._Heartbeat(len(candidates), 'soft narrow phase (pairs)',
                               log, progress)
    n_done = 0
    for elem_a, bb_a, elem_b, bb_b in candidates:
        n_done += 1
        beat.tick(n_done, len(out))
        tris_a = tess.element_triangles(elem_a, a_link_instance, tess_cache)
        tris_b = tess.element_triangles(elem_b, b_link_instance, tess_cache)
        if tris_a is None or tris_b is None:
            # No usable solids on one side: report the bbox estimate,
            # flagged, rather than silently dropping a possible near miss.
            n_fallback += 1
            gap_ft = _aabb_gap(bb_a, bb_b)
            out.append({
                'elem_a':          elem_a,
                'elem_b':          elem_b,
                'midpoint':        _aabb_overlap_center(_inflate(bb_a, tolerance_ft), bb_b),
                'gap_inches':      gap_ft * INCHES_PER_FOOT,
                'closest_point_a': None,
                'closest_point_b': None,
                'is_contact':      False,
                'gap_method':      'bbox',
            })
            continue
        n_mesh += 1
        hit = meshdist.mesh_min_distance(tris_a, tris_b, tolerance_ft)
        if hit is None:
            continue          # true distance beyond tolerance: not a near miss
        dist_ft, pa, pb = hit
        contact = dist_ft <= 1e-6
        if contact:
            n_contact += 1
        out.append({
            'elem_a':          elem_a,
            'elem_b':          elem_b,
            'midpoint':        ((pa[0] + pb[0]) / 2.0,
                                (pa[1] + pb[1]) / 2.0,
                                (pa[2] + pb[2]) / 2.0),
            'gap_inches':      dist_ft * INCHES_PER_FOOT,
            'closest_point_a': pa,
            'closest_point_b': pb,
            'is_contact':      contact,
            'gap_method':      'mesh',
        })

    if log:
        st = tess.stats(tess_cache)
        log("  - soft narrow phase: {0} candidate pair(s) -> {1} mesh-measured, "
            "{2} within tolerance ({3} touching/intersecting), {4} bbox-fallback; "
            "tessellated {5} element(s) / {6:,} triangle(s), {7} without solids".format(
                len(candidates), n_mesh, len(out) - n_fallback, n_contact,
                n_fallback, st['elements'], st['triangles'], st['no_solids']))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _bbox_in_host(elem, link_instance):
    # Guarded like hard.py's helper: one element with corrupt geometry
    # must degrade to "no bbox" (this element is skipped, as soft always
    # did for bbox-less elements), never abort the whole bucket pair.
    try:
        bb = elem.get_BoundingBox(None)
    except Exception:
        return None
    if bb is None:
        return None
    if link_instance is None:
        return bb
    try:
        return hard._transform_bbox_to_host(bb, link_instance)
    except Exception:
        return None


def _inflate(bb, padding_ft):
    """Return a new BoundingBoxXYZ inflated by `padding_ft` on every face."""
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    out = BoundingBoxXYZ()
    out.Min = XYZ(bb.Min.X - padding_ft, bb.Min.Y - padding_ft, bb.Min.Z - padding_ft)
    out.Max = XYZ(bb.Max.X + padding_ft, bb.Max.Y + padding_ft, bb.Max.Z + padding_ft)
    return out


def _aabb_overlap(bb_a, bb_b):
    return (bb_a.Min.X <= bb_b.Max.X and bb_a.Max.X >= bb_b.Min.X and
            bb_a.Min.Y <= bb_b.Max.Y and bb_a.Max.Y >= bb_b.Min.Y and
            bb_a.Min.Z <= bb_b.Max.Z and bb_a.Max.Z >= bb_b.Min.Z)


def _aabb_gap(bb_a, bb_b):
    """Minimum axis-aligned separation between two bboxes (0 if they touch or overlap)."""
    dx = max(0.0, bb_a.Min.X - bb_b.Max.X, bb_b.Min.X - bb_a.Max.X)
    dy = max(0.0, bb_a.Min.Y - bb_b.Max.Y, bb_b.Min.Y - bb_a.Max.Y)
    dz = max(0.0, bb_a.Min.Z - bb_b.Max.Z, bb_b.Min.Z - bb_a.Max.Z)
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def _aabb_overlap_center(bb_a, bb_b):
    from Autodesk.Revit.DB import XYZ
    min_x = max(bb_a.Min.X, bb_b.Min.X)
    min_y = max(bb_a.Min.Y, bb_b.Min.Y)
    min_z = max(bb_a.Min.Z, bb_b.Min.Z)
    max_x = min(bb_a.Max.X, bb_b.Max.X)
    max_y = min(bb_a.Max.Y, bb_b.Max.Y)
    max_z = min(bb_a.Max.Z, bb_b.Max.Z)
    return XYZ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0)
