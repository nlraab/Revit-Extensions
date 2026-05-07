# -*- coding: utf-8 -*-
"""Bounding-box math for clash navigation.

A single clash can mix host elements (in host coordinates) with linked
elements (in link-local coordinates that need a transform applied to land
in host coordinates). The functions here always return BoundingBoxXYZs in
HOST coordinates so callers don't have to track which coord system they're
in - they just hand us elements + their link instances.

Revit imports are inside function bodies so this module parses cleanly in
CPython 3 for the structural test suite — only the live functions need
the API.
"""


def element_world_box(element, link_instance=None):
    """Return `element`'s axis-aligned bounding box in HOST coordinates.

    For a host element, returns its model-space box directly.

    For a linked element, transforms the link-local box into host coords by
    transforming all 8 corners through the link's total transform and
    re-fitting an axis-aligned BoundingBoxXYZ around them. We can't just
    transform Min/Max - under any rotation the new AABB needs all 8
    corners to stay tight.

    Returns None if the element has no bounding box (some elements like
    pure annotations don't expose one) or if either accessor raises.
    """
    if element is None:
        return None
    try:
        bbox = element.get_BoundingBox(None)
    except Exception:
        return None
    if bbox is None:
        return None
    if link_instance is None:
        return bbox
    try:
        # GetTotalTransform handles nested links (a link inside a link);
        # falls back to the same value as GetTransform for a single-level
        # link, which is the common case.
        transform = link_instance.GetTotalTransform()
    except Exception:
        return bbox
    return _transform_bbox(bbox, transform)


def _transform_bbox(bbox, transform):
    """Apply a Transform to a BoundingBoxXYZ, refitting an AABB around the
    transformed corners. Pure math - no Revit document state touched."""
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    mn, mx = bbox.Min, bbox.Max
    corners = [
        XYZ(mn.X, mn.Y, mn.Z), XYZ(mn.X, mn.Y, mx.Z),
        XYZ(mn.X, mx.Y, mn.Z), XYZ(mn.X, mx.Y, mx.Z),
        XYZ(mx.X, mn.Y, mn.Z), XYZ(mx.X, mn.Y, mx.Z),
        XYZ(mx.X, mx.Y, mn.Z), XYZ(mx.X, mx.Y, mx.Z),
    ]
    transformed = [transform.OfPoint(c) for c in corners]
    xs = [p.X for p in transformed]
    ys = [p.Y for p in transformed]
    zs = [p.Z for p in transformed]
    out = BoundingBoxXYZ()
    out.Min = XYZ(min(xs), min(ys), min(zs))
    out.Max = XYZ(max(xs), max(ys), max(zs))
    return out


def union_boxes(boxes):
    """Combined AABB of all non-None boxes. Returns None if all are None."""
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    xs_min = min(b.Min.X for b in valid)
    ys_min = min(b.Min.Y for b in valid)
    zs_min = min(b.Min.Z for b in valid)
    xs_max = max(b.Max.X for b in valid)
    ys_max = max(b.Max.Y for b in valid)
    zs_max = max(b.Max.Z for b in valid)
    out = BoundingBoxXYZ()
    out.Min = XYZ(xs_min, ys_min, zs_min)
    out.Max = XYZ(xs_max, ys_max, zs_max)
    return out


def pad_box(bbox, pad_feet=2.0):
    """Inflate a bbox by `pad_feet` in every direction so a section box has
    breathing room around the clash for context."""
    if bbox is None:
        return None
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    out = BoundingBoxXYZ()
    out.Min = XYZ(bbox.Min.X - pad_feet, bbox.Min.Y - pad_feet, bbox.Min.Z - pad_feet)
    out.Max = XYZ(bbox.Max.X + pad_feet, bbox.Max.Y + pad_feet, bbox.Max.Z + pad_feet)
    return out


def box_around_point(point_xyz_list, half_size=3.0):
    """Build a small AABB centered on a [x, y, z] list of feet.

    Used as a fallback when we can't get a bounding box from the element
    itself (deleted, hidden, no geometry) but the clash still has a
    midpoint stored. half_size = 3 ft gives a 6-ft cube around the point,
    enough for the section box to land somewhere sensible.
    """
    if not point_xyz_list or len(point_xyz_list) < 3:
        return None
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    cx = float(point_xyz_list[0])
    cy = float(point_xyz_list[1])
    cz = float(point_xyz_list[2])
    out = BoundingBoxXYZ()
    out.Min = XYZ(cx - half_size, cy - half_size, cz - half_size)
    out.Max = XYZ(cx + half_size, cy + half_size, cz + half_size)
    return out
