# -*- coding: utf-8 -*-
"""Hard-clash detection - true geometric intersection.

Uses Revit's `ElementIntersectsElementFilter` for host-vs-host (one API
call wraps the geometry walk + intersection test) and
`ElementIntersectsSolidFilter` for host-vs-link (we transform the host
element's solid into link coordinates and run the filter against the link
document).

For host-vs-link, the README's "Linked-model intersection notes" section
explains the transform issue we're working around: bbox filters silently
misbehave when a link is moved/rotated relative to host. Solid-based
filters with proper transforms are the reliable path.
"""

from clash_detect import linked


def find_hard_clashes(doc, set_a_elements, set_b_elements,
                      a_link_instance=None, b_link_instance=None):
    """Find pairs in (set_a x set_b) whose geometries intersect.

    Args:
        doc: the host document.
        set_a_elements: list of Element objects in set A.
        set_b_elements: list of Element objects in set B.
        a_link_instance: if set_a is from a linked document, the
            RevitLinkInstance it lives in. Otherwise None.
        b_link_instance: same for set_b.

    Returns:
        list of dicts: {'elem_a': Element, 'elem_b': Element, 'midpoint': XYZ-or-None}

    Self-pairs (elem_a.Id == elem_b.Id when both come from the same doc)
    are skipped.

    The midpoint is the center of the bounding-box overlap in HOST
    coordinates. It's an approximation - the true intersection centroid
    would require Solid.Volume math which is more expensive than we need.
    """
    if not set_a_elements or not set_b_elements:
        return []

    # Both from host: simplest path. Use ElementIntersectsElementFilter
    # which natively handles geometry extraction.
    if a_link_instance is None and b_link_instance is None:
        return _hard_host_vs_host(doc, set_a_elements, set_b_elements)

    # Host vs link (or link vs host): use solid-based filter against the
    # right doc, transforming as needed.
    if a_link_instance is None and b_link_instance is not None:
        return _hard_host_vs_link(doc, set_a_elements, set_b_elements, b_link_instance)
    if a_link_instance is not None and b_link_instance is None:
        # Symmetric: just swap the roles, then swap back in the result
        flipped = _hard_host_vs_link(doc, set_b_elements, set_a_elements, a_link_instance)
        return [{'elem_a': p['elem_b'], 'elem_b': p['elem_a'], 'midpoint': p['midpoint']}
                for p in flipped]

    # Link vs link: both linked. Iterate set_a, transform each solid
    # link_a -> host -> link_b, filter against link_b doc.
    return _hard_link_vs_link(set_a_elements, a_link_instance,
                              set_b_elements, b_link_instance)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _hard_host_vs_host(doc, set_a, set_b):
    from Autodesk.Revit.DB import (
        ElementIntersectsElementFilter, FilteredElementCollector, ElementId,
    )
    from System.Collections.Generic import List as NetList
    from clash_detect._compat import eid_int

    set_b_ids = NetList[ElementId]()
    for e in set_b:
        try:
            set_b_ids.Add(e.Id)
        except Exception:
            continue
    if set_b_ids.Count == 0:
        return []

    out = []
    seen_pairs = set()
    for elem_a in set_a:
        try:
            inter_filter = ElementIntersectsElementFilter(elem_a)
        except Exception:
            continue  # element has no usable solid; skip

        try:
            hits = (
                FilteredElementCollector(doc, set_b_ids)
                .WherePasses(inter_filter)
                .ToElements()
            )
        except Exception:
            continue
        a_id_int = eid_int(elem_a.Id)
        for elem_b in hits:
            b_id_int = eid_int(elem_b.Id)
            if a_id_int == b_id_int:
                continue
            pair_key = (min(a_id_int, b_id_int), max(a_id_int, b_id_int))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            mp = _bbox_overlap_center(elem_a, elem_b)
            out.append({'elem_a': elem_a, 'elem_b': elem_b, 'midpoint': mp})
    return out


def _hard_host_vs_link(doc, host_elems, link_elems, link_instance):
    from Autodesk.Revit.DB import (
        ElementIntersectsSolidFilter, FilteredElementCollector, ElementId,
    )
    from System.Collections.Generic import List as NetList
    from clash_detect._compat import eid_int

    link_doc = link_instance.GetLinkDocument()
    if link_doc is None or not host_elems or not link_elems:
        return []
    link_ids = NetList[ElementId]()
    for e in link_elems:
        try:
            link_ids.Add(e.Id)
        except Exception:
            continue
    if link_ids.Count == 0:
        return []

    out = []
    seen_pairs = set()
    for elem_a in host_elems:
        host_solids = _solids_for_element(elem_a)
        if not host_solids:
            continue
        a_id = eid_int(elem_a.Id)
        for host_solid in host_solids:
            try:
                link_space_solid = linked.host_solid_in_link_space(host_solid, link_instance)
                inter_filter = ElementIntersectsSolidFilter(link_space_solid)
                hits = (
                    FilteredElementCollector(link_doc, link_ids)
                    .WherePasses(inter_filter)
                    .ToElements()
                )
            except Exception:
                continue
            for elem_b in hits:
                pair_key = (a_id, eid_int(elem_b.Id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                mp = _bbox_overlap_center_xdoc(elem_a, None, elem_b, link_instance)
                out.append({'elem_a': elem_a, 'elem_b': elem_b, 'midpoint': mp})
    return out


def _hard_link_vs_link(set_a, link_a, set_b, link_b):
    from Autodesk.Revit.DB import (
        ElementIntersectsSolidFilter, FilteredElementCollector, ElementId,
    )
    from System.Collections.Generic import List as NetList
    from clash_detect._compat import eid_int

    link_b_doc = link_b.GetLinkDocument()
    if link_b_doc is None or not set_a or not set_b:
        return []
    link_b_ids = NetList[ElementId]()
    for e in set_b:
        try:
            link_b_ids.Add(e.Id)
        except Exception:
            continue
    if link_b_ids.Count == 0:
        return []

    out = []
    seen_pairs = set()
    for elem_a in set_a:
        a_solids = _solids_for_element(elem_a)
        if not a_solids:
            continue
        a_id = eid_int(elem_a.Id)
        for solid_a in a_solids:
            try:
                in_host = linked.link_solid_in_host_space(solid_a, link_a)
                in_link_b = linked.host_solid_in_link_space(in_host, link_b)
                inter_filter = ElementIntersectsSolidFilter(in_link_b)
                hits = (
                    FilteredElementCollector(link_b_doc, link_b_ids)
                    .WherePasses(inter_filter)
                    .ToElements()
                )
            except Exception:
                continue
            for elem_b in hits:
                pair_key = (a_id, eid_int(elem_b.Id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                mp = _bbox_overlap_center_xdoc(elem_a, link_a, elem_b, link_b)
                out.append({'elem_a': elem_a, 'elem_b': elem_b, 'midpoint': mp})
    return out


def _solids_for_element(elem):
    """Return all positive-volume Solids in the element's geometry."""
    from Autodesk.Revit.DB import Options, Solid, GeometryInstance
    out = []
    try:
        geom = elem.get_Geometry(Options())
    except Exception:
        return out
    if geom is None:
        return out
    for g in geom:
        if isinstance(g, Solid):
            try:
                if g.Volume > 0:
                    out.append(g)
            except Exception:
                continue
        elif isinstance(g, GeometryInstance):
            try:
                inst_geom = g.GetInstanceGeometry()
            except Exception:
                continue
            if inst_geom is None:
                continue
            for ig in inst_geom:
                if isinstance(ig, Solid):
                    try:
                        if ig.Volume > 0:
                            out.append(ig)
                    except Exception:
                        continue
    return out


def _bbox_overlap_center(elem_a, elem_b):
    """Center of the bbox overlap between two same-doc elements (host coords)."""
    bb_a = elem_a.get_BoundingBox(None)
    bb_b = elem_b.get_BoundingBox(None)
    return _overlap_center(bb_a, bb_b)


def _bbox_overlap_center_xdoc(elem_a, link_a, elem_b, link_b):
    """Center of bbox overlap when elements come from different docs.
    Transforms each bbox into host coords first."""
    bb_a = elem_a.get_BoundingBox(None)
    bb_b = elem_b.get_BoundingBox(None)
    if link_a is not None and bb_a is not None:
        bb_a = _transform_bbox_to_host(bb_a, link_a)
    if link_b is not None and bb_b is not None:
        bb_b = _transform_bbox_to_host(bb_b, link_b)
    return _overlap_center(bb_a, bb_b)


def _overlap_center(bb_a, bb_b):
    from Autodesk.Revit.DB import XYZ
    if bb_a is None or bb_b is None:
        return None
    min_x = max(bb_a.Min.X, bb_b.Min.X)
    min_y = max(bb_a.Min.Y, bb_b.Min.Y)
    min_z = max(bb_a.Min.Z, bb_b.Min.Z)
    max_x = min(bb_a.Max.X, bb_b.Max.X)
    max_y = min(bb_a.Max.Y, bb_b.Max.Y)
    max_z = min(bb_a.Max.Z, bb_b.Max.Z)
    if min_x > max_x or min_y > max_y or min_z > max_z:
        # Non-overlapping bboxes (shouldn't happen if intersection filter passed,
        # but defensive) - fall back to mean of centers
        return XYZ(
            (bb_a.Min.X + bb_a.Max.X + bb_b.Min.X + bb_b.Max.X) / 4.0,
            (bb_a.Min.Y + bb_a.Max.Y + bb_b.Min.Y + bb_b.Max.Y) / 4.0,
            (bb_a.Min.Z + bb_a.Max.Z + bb_b.Min.Z + bb_b.Max.Z) / 4.0,
        )
    return XYZ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0)


def _transform_bbox_to_host(bbox, link_instance):
    """Transform a link-local bbox into host coordinates (axis-aligned approximation).

    For non-axis-aligned link transforms this isn't perfectly tight but
    it's good enough for midpoint approximation.
    """
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    xform = link_instance.GetTransform()
    # Transform all 8 corners and take the new axis-aligned bbox
    corners = [
        XYZ(bbox.Min.X, bbox.Min.Y, bbox.Min.Z),
        XYZ(bbox.Max.X, bbox.Min.Y, bbox.Min.Z),
        XYZ(bbox.Min.X, bbox.Max.Y, bbox.Min.Z),
        XYZ(bbox.Max.X, bbox.Max.Y, bbox.Min.Z),
        XYZ(bbox.Min.X, bbox.Min.Y, bbox.Max.Z),
        XYZ(bbox.Max.X, bbox.Min.Y, bbox.Max.Z),
        XYZ(bbox.Min.X, bbox.Max.Y, bbox.Max.Z),
        XYZ(bbox.Max.X, bbox.Max.Y, bbox.Max.Z),
    ]
    transformed = [xform.OfPoint(c) for c in corners]
    out = BoundingBoxXYZ()
    out.Min = XYZ(
        min(p.X for p in transformed),
        min(p.Y for p in transformed),
        min(p.Z for p in transformed),
    )
    out.Max = XYZ(
        max(p.X for p in transformed),
        max(p.Y for p in transformed),
        max(p.Z for p in transformed),
    )
    return out
