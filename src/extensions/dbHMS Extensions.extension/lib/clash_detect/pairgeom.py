# -*- coding: utf-8 -*-
"""Per-clash pair geometry for the importance engine (Phase 2 capture).

Captured AT DETECTION TIME from hard.py, where both live Element handles and
their link transforms are already in scope -- no fragile re-resolution of a
saved record. Two tiers per pair:

  overlap_bbox_in : the AABB-overlap extents [dx, dy, dz] in inches. Nearly
                    FREE (the bounding boxes are already read for the
                    midpoint). Feeds the geometry sub-score as a depth proxy
                    on every hard row, so grazes and deep overlaps stop
                    scoring the same.
  boolean tier    : the real solid intersection -- overlap_volume_cf,
                    penetration_depth_in (the intersection's thinnest extent),
                    overlap_centroid. Feeds the precise geometry term and
                    wakes the dormant R-GRAZE / C4 rules. Guarded by a
                    per-pair solid cap and a run-wide wall-clock budget; on
                    any failure or over-budget the row keeps the bbox tier
                    only, so the pass can never crash or hang a run.

penetration_depth_in is set ONLY from the boolean (geom_method='boolean'), so
R-GRAZE never suppresses on a bbox proxy. pen_class (full/partial) needs the
counterpart's thickness and lands in Phase 3.

Everything is try/excepted: geometry math must never break a detection run.
Solid EXTRACTION (the expensive step) is cached per run and keyed by
(document, element id); the cheap per-pair transform is applied fresh.
"""
import time

GEOM_SOLID_CAP = 8            # max solids per side fed to the boolean
GEOM_TIME_BUDGET_S = 300.0    # run-wide wall-clock cap on the boolean tier.
                              # 90s covered only 1,630 of 5,248 hard rows on the
                              # real NIUHTC run; 300s covers essentially all of
                              # them and still lands the whole run under 30 min.
_SOLID_CACHE_MAX = 1200       # raw-solid extraction cache size cap. Raised with
                              # the budget so the ~3x more rows now reaching the
                              # boolean tier reuse extracted solids instead of
                              # re-extracting (each re-extraction is pure cost).


def new_cache():
    """One geometry cache per RUN, threaded like tess_cache / ins_cache. Holds
    the boolean deadline, a raw-solid extraction cache, and simple counters."""
    return {'deadline': time.time() + GEOM_TIME_BUDGET_S,
            'solids': {}, 'booleans': 0, 'bbox_only': 0, 'errors': 0}


def pair_geometry(elem_a, a_link, elem_b, b_link, cache=None):
    """Return the geometry dict for one hard clash pair. Never raises."""
    out = {'overlap_bbox_in': None, 'overlap_volume_cf': None,
           'penetration_depth_in': None, 'overlap_centroid': None,
           'pen_class': None, 'geom_method': 'bbox'}
    try:
        out['overlap_bbox_in'] = _overlap_extents(elem_a, a_link, elem_b, b_link)
    except Exception:
        pass

    # Boolean tier, budget-gated.
    if cache is not None:
        dl = cache.get('deadline')
        if dl is not None and time.time() > dl:
            cache['bbox_only'] = cache.get('bbox_only', 0) + 1
            return out
    try:
        vol, depth, cen = _boolean_overlap(elem_a, a_link, elem_b, b_link, cache)
        if vol is not None:
            out['overlap_volume_cf'] = vol
            out['penetration_depth_in'] = depth
            out['overlap_centroid'] = cen
            out['geom_method'] = 'boolean'
            if cache is not None:
                cache['booleans'] = cache.get('booleans', 0) + 1
    except Exception:
        if cache is not None:
            cache['errors'] = cache.get('errors', 0) + 1
    return out


# ---------------------------------------------------------------------------
# Free tier: AABB overlap extents (host coordinates)
# ---------------------------------------------------------------------------

def _overlap_extents(elem_a, a_link, elem_b, b_link):
    from clash_detect import hard as _h
    bb_a = elem_a.get_BoundingBox(None)
    bb_b = elem_b.get_BoundingBox(None)
    if bb_a is None or bb_b is None:
        return None
    if a_link is not None:
        bb_a = _h._transform_bbox_to_host(bb_a, a_link)
    if b_link is not None:
        bb_b = _h._transform_bbox_to_host(bb_b, b_link)
    dx = min(bb_a.Max.X, bb_b.Max.X) - max(bb_a.Min.X, bb_b.Min.X)
    dy = min(bb_a.Max.Y, bb_b.Max.Y) - max(bb_a.Min.Y, bb_b.Min.Y)
    dz = min(bb_a.Max.Z, bb_b.Max.Z) - max(bb_a.Min.Z, bb_b.Min.Z)
    return [max(0.0, float(dx)) * 12.0, max(0.0, float(dy)) * 12.0,
            max(0.0, float(dz)) * 12.0]


# ---------------------------------------------------------------------------
# Boolean tier: real solid intersection
# ---------------------------------------------------------------------------

def _boolean_overlap(elem_a, a_link, elem_b, b_link, cache):
    from Autodesk.Revit.DB import BooleanOperationsUtils, BooleanOperationsType
    solids_a = _host_solids(elem_a, a_link, cache)
    solids_b = _host_solids(elem_b, b_link, cache)
    if not solids_a or not solids_b:
        return None, None, None
    total_vol = 0.0
    best = None                     # (volume, intersection Solid) largest piece
    for sa in solids_a[:GEOM_SOLID_CAP]:
        for sb in solids_b[:GEOM_SOLID_CAP]:
            try:
                inter = BooleanOperationsUtils.ExecuteBooleanOperation(
                    sa, sb, BooleanOperationsType.Intersect)
            except Exception:
                continue
            if inter is None:
                continue
            try:
                v = float(inter.Volume)
            except Exception:
                continue
            if v <= 0.0:
                continue
            total_vol += v
            if best is None or v > best[0]:
                best = (v, inter)
    if best is None:
        return None, None, None
    return total_vol, _min_extent_in(best[1]), _centroid_list(best[1])


def _host_solids(elem, link, cache):
    """Element solids transformed into HOST coordinates. Raw extraction is
    cached (per document + element id); the transform is applied fresh."""
    raw = _raw_solids(elem, cache)
    if not raw:
        return []
    if link is None:
        return raw
    from clash_detect import linked
    out = []
    for s in raw:
        try:
            out.append(linked.link_solid_in_host_space(s, link))
        except Exception:
            continue
    return out


def _raw_solids(elem, cache):
    from clash_detect.hard import _solids_for_element
    if cache is None:
        return _solids_for_element(elem)
    key = _elem_key(elem)
    sc = cache.setdefault('solids', {})
    if key is not None and key in sc:
        return sc[key]
    solids = _solids_for_element(elem)
    if key is not None and len(sc) < _SOLID_CACHE_MAX:
        sc[key] = solids
    return solids


def _elem_key(elem):
    from clash_detect._compat import eid_int
    try:
        return (id(elem.Document), eid_int(elem.Id))
    except Exception:
        return None


def _min_extent_in(solid):
    """Thinnest extent of a solid's bounding box, in inches. For a through
    penetration this is the assembly thickness; for a graze it is the sliver
    depth -- exactly the graze-vs-impale signal. Box dims are frame-invariant,
    so the solid's local-frame box is fine."""
    try:
        bb = solid.GetBoundingBox()
        if bb is None:
            return None
        dx = abs(float(bb.Max.X) - float(bb.Min.X))
        dy = abs(float(bb.Max.Y) - float(bb.Min.Y))
        dz = abs(float(bb.Max.Z) - float(bb.Min.Z))
        return min(dx, dy, dz) * 12.0
    except Exception:
        return None


def _centroid_list(solid):
    try:
        c = solid.ComputeCentroid()
        return [float(c.X), float(c.Y), float(c.Z)]
    except Exception:
        return None
