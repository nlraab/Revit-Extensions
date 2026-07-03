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

Every path runs a grid AABB BROAD PHASE first (clash_detect/broadphase):
the precise (slow) filters are seeded with only the handful of elements
whose bounding boxes could possibly touch, and elements with no
candidates skip solid extraction entirely. Without this, host-vs-link on
a real hospital model was ~27 million precise geometry tests (hours);
with it, tens of thousands (minutes). The broad phase only prunes -- a
real intersection always survives it.
"""

import time

from clash_detect import broadphase, linked

# Broad-phase safety pad (feet) for hard tests: bboxes from Revit are
# already conservative; this covers float fuzz on touching pairs.
_HARD_PAD_FT = 0.05
# Progress heartbeat cadence (seconds) for the log/status callbacks.
_PROGRESS_EVERY_S = 10.0


def find_hard_clashes(doc, set_a_elements, set_b_elements,
                      a_link_instance=None, b_link_instance=None,
                      log=None, progress=None):
    """Find pairs in (set_a x set_b) whose geometries intersect.

    Args:
        doc: the host document.
        set_a_elements: list of Element objects in set A.
        set_b_elements: list of Element objects in set B.
        a_link_instance: if set_a is from a linked document, the
            RevitLinkInstance it lives in. Otherwise None.
        b_link_instance: same for set_b.
        log: optional callable for one-line diagnostics (broad-phase
            stats, periodic progress) - a long run must never go silent.
        progress: optional callable for short user-facing status strings
            (the coordination page's status bar).

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
        return _hard_host_vs_host(doc, set_a_elements, set_b_elements,
                                  log=log, progress=progress)

    # Host vs link (or link vs host): use solid-based filter against the
    # right doc, transforming as needed.
    if a_link_instance is None and b_link_instance is not None:
        return _hard_host_vs_link(doc, set_a_elements, set_b_elements,
                                  b_link_instance, log=log, progress=progress)
    if a_link_instance is not None and b_link_instance is None:
        # Symmetric: just swap the roles, then swap back in the result
        flipped = _hard_host_vs_link(doc, set_b_elements, set_a_elements,
                                     a_link_instance, log=log, progress=progress)
        return [{'elem_a': p['elem_b'], 'elem_b': p['elem_a'], 'midpoint': p['midpoint']}
                for p in flipped]

    # Link vs link: both linked. Iterate set_a, transform each solid
    # link_a -> host -> link_b, filter against link_b doc.
    return _hard_link_vs_link(set_a_elements, a_link_instance,
                              set_b_elements, b_link_instance,
                              log=log, progress=progress)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _broad_candidates(a_elems, a_link, b_elems, b_link, log, label):
    """Grid broad phase over both sets in HOST coordinates.

    Returns (cand_map {a_id_int: [b_id_int,...]}, a_by_id, b_by_id).
    Conservative: elements whose bbox can't be read stay in play, and an
    element whose id can't be read gets a unique synthetic negative key
    (eid_int returns 0 on failure - two such elements must not collide in
    the by-id maps and silently vanish from detection)."""
    from clash_detect._compat import eid_int
    t0 = time.time()
    synth = [0]

    def _key(e):
        try:
            k = eid_int(e.Id)
        except Exception:
            k = 0
        if not k:
            synth[0] -= 1
            k = synth[0]
        return k

    a_items, a_by_id = [], {}
    a_no_bbox = 0
    for e in a_elems:
        key = _key(e)
        a_by_id[key] = e
        bb = broadphase.bbox_tuple(_bbox_in_host_or_none(e, a_link))
        if bb is None:
            a_no_bbox += 1
        a_items.append((key, bb))
    b_items, b_by_id = [], {}
    b_no_bbox = 0
    for e in b_elems:
        key = _key(e)
        b_by_id[key] = e
        bb = broadphase.bbox_tuple(_bbox_in_host_or_none(e, b_link))
        if bb is None:
            b_no_bbox += 1
        b_items.append((key, bb))
    cand = broadphase.candidate_map(a_items, b_items, pad_ft=_HARD_PAD_FT)
    if log:
        try:
            n_pairs = sum(len(v) for v in cand.values())
            log("  - broad phase ({0}): {1} of {2} A-elements have candidates; "
                "{3} candidate pair(s) instead of {4} (took {5:.1f}s); "
                "{6} A / {7} B without usable bbox (kept conservatively)".format(
                    label, len(cand), len(a_items), n_pairs,
                    len(a_items) * len(b_items), time.time() - t0,
                    a_no_bbox, b_no_bbox))
        except Exception:
            pass
    return cand, a_by_id, b_by_id


def _bbox_in_host_or_none(elem, link_instance):
    try:
        bb = elem.get_BoundingBox(None)
    except Exception:
        return None
    if bb is None:
        return None
    if link_instance is None:
        return bb
    try:
        return _transform_bbox_to_host(bb, link_instance)
    except Exception:
        return None


class _Heartbeat(object):
    """Rate-limited progress reporting so a long test is never silent."""

    def __init__(self, total, label, log, progress):
        self.total = total
        self.label = label
        self.log = log
        self.progress = progress
        self.t0 = time.time()
        self.last = self.t0

    def tick(self, done, hits):
        now = time.time()
        if now - self.last < _PROGRESS_EVERY_S:
            return
        self.last = now
        msg = "{0}: {1}/{2} elements checked, {3} clash(es) so far ({4:.0f}s)".format(
            self.label, done, self.total, hits, now - self.t0)
        # Both callbacks are guarded: a dying logger (e.g. a closed pyRevit
        # output window) must never abort a detection loop and discard the
        # clashes already found.
        if self.log:
            try:
                self.log("  - " + msg)
            except Exception:
                pass
        if self.progress:
            try:
                self.progress(msg)
            except Exception:
                pass


def _hard_host_vs_host(doc, set_a, set_b, log=None, progress=None):
    from Autodesk.Revit.DB import (
        ElementIntersectsElementFilter, FilteredElementCollector, ElementId,
    )
    from System.Collections.Generic import List as NetList
    from clash_detect._compat import eid_int

    cand, a_by_id, b_by_id = _broad_candidates(
        set_a, None, set_b, None, log, 'host vs host')
    out = []
    seen_pairs = set()
    beat = _Heartbeat(len(cand), 'hard host vs host', log, progress)
    done = 0
    for a_id_int in sorted(cand.keys()):
        done += 1
        beat.tick(done, len(out))
        elem_a = a_by_id[a_id_int]
        b_keys = [k for k in cand[a_id_int] if k != a_id_int]
        if not b_keys:
            continue
        try:
            inter_filter = ElementIntersectsElementFilter(elem_a)
        except Exception:
            continue  # element has no usable solid; skip
        seed = NetList[ElementId]()
        for k in b_keys:
            try:
                seed.Add(b_by_id[k].Id)
            except Exception:
                continue
        if seed.Count == 0:
            continue
        try:
            hits = (
                FilteredElementCollector(doc, seed)
                .WherePasses(inter_filter)
                .ToElements()
            )
        except Exception:
            continue
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


def _hard_host_vs_link(doc, host_elems, link_elems, link_instance,
                       log=None, progress=None):
    from Autodesk.Revit.DB import (
        ElementIntersectsSolidFilter, FilteredElementCollector, ElementId,
    )
    from System.Collections.Generic import List as NetList
    from clash_detect._compat import eid_int

    link_doc = link_instance.GetLinkDocument()
    if link_doc is None or not host_elems or not link_elems:
        return []

    cand, a_by_id, b_by_id = _broad_candidates(
        host_elems, None, link_elems, link_instance, log, 'host vs link')
    out = []
    seen_pairs = set()
    beat = _Heartbeat(len(cand), 'hard host vs link', log, progress)
    done = 0
    for a_id in sorted(cand.keys()):
        done += 1
        beat.tick(done, len(out))
        elem_a = a_by_id[a_id]
        seed = NetList[ElementId]()
        for k in cand[a_id]:
            try:
                seed.Add(b_by_id[k].Id)
            except Exception:
                continue
        if seed.Count == 0:
            continue
        # Solids are extracted ONLY for elements that survived the broad
        # phase - geometry extraction is itself expensive.
        host_solids = _solids_for_element(elem_a)
        if not host_solids:
            continue
        for host_solid in host_solids:
            try:
                link_space_solid = linked.host_solid_in_link_space(host_solid, link_instance)
                inter_filter = ElementIntersectsSolidFilter(link_space_solid)
                hits = (
                    FilteredElementCollector(link_doc, seed)
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


def _hard_link_vs_link(set_a, link_a, set_b, link_b, log=None, progress=None):
    from Autodesk.Revit.DB import (
        ElementIntersectsSolidFilter, FilteredElementCollector, ElementId,
    )
    from System.Collections.Generic import List as NetList
    from clash_detect._compat import eid_int

    link_b_doc = link_b.GetLinkDocument()
    if link_b_doc is None or not set_a or not set_b:
        return []

    cand, a_by_id, b_by_id = _broad_candidates(
        set_a, link_a, set_b, link_b, log, 'link vs link')
    out = []
    seen_pairs = set()
    beat = _Heartbeat(len(cand), 'hard link vs link', log, progress)
    done = 0
    for a_id in sorted(cand.keys()):
        done += 1
        beat.tick(done, len(out))
        elem_a = a_by_id[a_id]
        seed = NetList[ElementId]()
        for k in cand[a_id]:
            try:
                seed.Add(b_by_id[k].Id)
            except Exception:
                continue
        if seed.Count == 0:
            continue
        a_solids = _solids_for_element(elem_a)
        if not a_solids:
            continue
        for solid_a in a_solids:
            try:
                in_host = linked.link_solid_in_host_space(solid_a, link_a)
                in_link_b = linked.host_solid_in_link_space(in_host, link_b)
                inter_filter = ElementIntersectsSolidFilter(in_link_b)
                hits = (
                    FilteredElementCollector(link_b_doc, seed)
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
    """Return all valid Solids in the element's geometry, descending
    RECURSIVELY into nested GeometryInstances. The previous one-level
    descent silently dropped the geometry of nested families (e.g. a valve
    family placed inside a piping assembly), which then could never
    hard-clash."""
    from Autodesk.Revit.DB import Options
    out = []
    try:
        geom = elem.get_Geometry(Options())
    except Exception:
        return out
    if geom is None:
        return out
    _collect_solids_recursive(geom, out)
    return out


def _collect_solids_recursive(geom, out):
    """Collector body matching the proven exporter semantics: keep solids
    that actually have faces AND volume, recurse into instances."""
    from Autodesk.Revit.DB import Solid, GeometryInstance
    for g in geom:
        if isinstance(g, Solid):
            try:
                if g.Faces.Size > 0 and g.Volume > 0:
                    out.append(g)
            except Exception:
                continue
        elif isinstance(g, GeometryInstance):
            try:
                inst_geom = g.GetInstanceGeometry()
            except Exception:
                inst_geom = None
            if inst_geom is not None:
                _collect_solids_recursive(inst_geom, out)


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
