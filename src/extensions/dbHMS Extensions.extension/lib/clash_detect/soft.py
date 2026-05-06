# -*- coding: utf-8 -*-
"""Soft-clash detection - "near miss" intersections within a tolerance.

Algorithm (V1, kept simple):
  1. Get every element's bounding box in host coordinates (transforming
     link-side bboxes through the link's transform).
  2. Inflate set_a's boxes by the tolerance on every face.
  3. Use plain Python AABB-overlap test against set_b's natural boxes.
     Pairs whose A-inflated box overlaps B's box are reported.
  4. Skip self-pairs and dedupe symmetric pairs.

We're not running the Revit `BoundingBoxIntersectsFilter` because:
  * It silently misbehaves when a link instance is moved/rotated.
  * The Python AABB test is fast (constant per-pair after bbox lookup).
  * We need the inflated geometry anyway, which the API filter doesn't expose.

For V1 the midpoint is the center of the bbox overlap in host coords.
True minimum-distance / closest-point between solids is more expensive
and would land in a later iteration if needed.
"""

from clash_detect import hard


INCHES_PER_FOOT = 12.0


def find_soft_clashes(doc, set_a_elements, set_b_elements, tolerance_inches,
                      a_link_instance=None, b_link_instance=None):
    """Find pairs in (set_a x set_b) whose bounding boxes are within
    `tolerance_inches` of each other.

    Returns list of dicts:
        {'elem_a': Element, 'elem_b': Element, 'midpoint': XYZ-or-None,
         'gap_inches': approximate gap between bboxes}

    `gap_inches` is the smallest separation between A's and B's bboxes
    (0 if they touch or overlap, positive otherwise). Useful for ranking.
    """
    if tolerance_inches <= 0:
        # No tolerance = caller should be running hard detection instead.
        return []

    if not set_a_elements or not set_b_elements:
        return []

    tolerance_ft = float(tolerance_inches) / INCHES_PER_FOOT

    # Pre-compute bboxes in host coords
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

    from clash_detect._compat import eid_int

    out = []
    seen_pairs = set()
    same_doc = (a_link_instance is None) == (b_link_instance is None) and \
               (a_link_instance is None or eid_int(a_link_instance.Id) == (
                   eid_int(b_link_instance.Id) if b_link_instance else None))

    for elem_a, bb_a in a_boxes:
        bb_a_inflated = _inflate(bb_a, tolerance_ft)
        a_id = eid_int(elem_a.Id)
        for elem_b, bb_b in b_boxes:
            b_id = eid_int(elem_b.Id)
            # Skip self-pairs only when both are from the same doc
            if same_doc and a_id == b_id:
                continue
            pair_key = (min(a_id, b_id), max(a_id, b_id)) if same_doc else (a_id, b_id)
            if pair_key in seen_pairs:
                continue
            if not _aabb_overlap(bb_a_inflated, bb_b):
                continue
            seen_pairs.add(pair_key)
            gap_ft = _aabb_gap(bb_a, bb_b)
            out.append({
                'elem_a':     elem_a,
                'elem_b':     elem_b,
                'midpoint':   _aabb_overlap_center(bb_a_inflated, bb_b),
                'gap_inches': gap_ft * INCHES_PER_FOOT,
            })
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _bbox_in_host(elem, link_instance):
    bb = elem.get_BoundingBox(None)
    if bb is None:
        return None
    if link_instance is None:
        return bb
    return hard._transform_bbox_to_host(bb, link_instance)


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
