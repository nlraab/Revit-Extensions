# -*- coding: utf-8 -*-
"""Phase 4 clearance detection: code-mandated CLEARANCE ZONES around equipment.

Unlike hard/soft detection (element vs element), this engine SYNTHESIZES an
invisible zone Solid around a piece of owner equipment and flags any intruder
element that lands inside it. It emits the same pair-dict shape the runner
expects, with elem_a = the intruder (real element) and elem_b = the owner (real
element) -- the zone is transient geometry, never a ref, so fingerprints and
_make_ref work unchanged.

Rules (dispatched by the clearance TEST id, which the runner stamps as
`clearance_rule`):

  C-NEC       NEC 110.26(E)(1)(a) dedicated space: a column equal to the gear's
              footprint, from the top of the gear up to 6 ft OR the structural
              ceiling, whichever is lower. NO foreign duct/pipe allowed here.
  C-NEC-W     NEC 110.26(A) working space: a box in FRONT of the gear (front
              from FacingOrientation), depth by voltage condition, 30 in min
              width, 6.5 ft tall. Only built when the facing is trustworthy.
  M-NEC-PROT  NEC 110.26(E)(1)(b): leak-capable piping in the band ABOVE the
              dedicated space (between +6 ft and the real structural ceiling).
              Only exists when there is genuine headroom above the gear, so it
              never false-flags next-floor piping.
  M-SPR       NFPA 13 10.2.7 sprinkler obstruction: a clearance sphere/box
              around a sprinkler head. DORMANT on models with no modeled heads
              (owner set resolves to zero -> the engine is never entered).

Runtime safety: every Revit call is wrapped so a geometry failure skips one
owner/zone and never aborts the run. The pure NEC/NFPA math (voltage depth,
NFPA radius, dedicated cap, leak-capable + zone-owner classification) is
factored out at the top and CPython-unit-tested; everything below the divider
touches the Revit API and can only be validated on a real run. All Revit
imports are lazy so this module parses under CPython 3 (the test suite).
"""

import math

# ---------------------------------------------------------------------------
# Constants (firm standard, doctrine 8: no per-project knobs)
# ---------------------------------------------------------------------------

DEDICATED_HEIGHT_FT = 6.0        # NEC 110.26(E)(1)(a) dedicated space height
WORKING_HEIGHT_FT = 6.5          # NEC working-space height (78 in)
WORKING_MIN_WIDTH_IN = 30.0      # NEC minimum working-space width
WORKING_DEPTH_BY_COND_IN = {1: 36.0, 2: 42.0, 3: 48.0}  # Table 110.26(A)(1)
PROT_BAND_MIN_FT = 0.5           # min headroom above dedicated space to bother
STRUCT_PROBE_FT = 20.0           # how far above the gear to look for a ceiling
SPRINKLER_STANDOFF_IN = 24.0     # NFPA 13 10.2.7 obstruction standoff CAP: the
                                 # largest clearance any obstruction can require
                                 # (3x its max dimension, capped at 24 in). A
                                 # fixed box this size is conservative -- it
                                 # catches every possible obstruction; the
                                 # precise per-obstruction 3x test is a
                                 # validation-time task (see _sprinkler_zone).
_MIN_FT = 1.0 / 32.0             # ignore sub-1/32-inch degenerate extents
_AXIS_DOT = 0.95                 # facing must align this closely with X or Y

# Owner family-name tokens that do NOT own a clearance zone (a housekeeping pad
# is not gear; the gear standing on it carries the zone). Substring, upper.
_NON_OWNER_TOKENS = ('PAD',)
# Free-standing owners we still trust a facing for (wall-hosted gear is trusted
# by its host; these are the free-standing families a facing is meaningful on).
_PANEL_TOKENS = ('PANEL', 'SWITCHBOARD', 'SWITCH BOARD', 'DISTRIBUTION',
                 'SWITCHGEAR', 'MCC', 'MOTOR CONTROL')
# Sprinkler-piping system hints -> a pipe that is itself leak-capable.
_LEAK_SYS_TOKENS = ('WATER', 'SANITARY', 'STORM', 'WASTE', 'VENT', 'DRAIN',
                    'CONDENSATE', 'HYDRONIC', 'GLYCOL', 'STEAM',
                    'SPRINKLER', 'FIRE', 'FP', 'DOMESTIC', 'PLUMB')


# ---------------------------------------------------------------------------
# Pure helpers (CPython-testable -- no Revit)
# ---------------------------------------------------------------------------

def voltage_condition(name):
    """NEC working-space Condition (1/2/3). The real NEC Table 110.26(A)(1)
    Condition is set by what FACES the working space (grounded parts, exposed
    live parts, or a clear space), NOT by the voltage -- and the model does not
    encode that. So every normal <=600 V panel/switchboard defaults to
    Condition 1 (36 in), the smallest depth, because OVER-sizing the box would
    invent violations. We only widen for genuine MEDIUM voltage (>600 V), which
    NEC does treat with a larger baseline clearance (Table 110.26(A)(2)); a
    positive MV signal in the name is required to grow the box."""
    s = (name or '').upper()
    if any(t in s for t in ('MEDIUM VOLTAGE', ' MV', 'MV ', '4160', '12470',
                            '13800', '15KV', '5KV', '4.16KV', '13.8KV')):
        return 3
    return 1


def working_depth_in(condition):
    """Working-space depth (in) for an NEC condition; default Condition 1."""
    return WORKING_DEPTH_BY_COND_IN.get(condition, WORKING_DEPTH_BY_COND_IN[1])


def nfpa_radius_in(obstruction_max_dim_in):
    """NFPA 13 10.2.7 obstruction clearance: three times the maximum dimension
    of the OBSTRUCTION, capped at 24 in. Guards a bad/zero dimension to a
    conservative floor so a missing size never yields a zero-radius zone.

    RESERVED for the future per-obstruction sprinkler test: the current
    _sprinkler_zone builds a fixed conservative box at the 24 in cap because it
    runs per-head (before the obstruction is known). When the sprinkler test is
    refined to measure each intruder at intrusion time, THAT is where this
    helper is called -- with the intruder's cross-section, the element the rule
    is actually about."""
    try:
        d = float(obstruction_max_dim_in)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0.0:
        d = 4.0
    return min(3.0 * d, 24.0)


def dedicated_cap_ft(equip_top_ft, lowest_struct_bot_ft,
                     height_ft=DEDICATED_HEIGHT_FT):
    """Top elevation of the dedicated space: equip top + 6 ft, but no higher
    than the structural ceiling above it (whichever is LOWER, NEC 110.26(E)(1)(a))."""
    cap = equip_top_ft + height_ft
    if lowest_struct_bot_ft is not None and lowest_struct_bot_ft < cap:
        cap = lowest_struct_bot_ft
    return cap


def is_zone_owner(family_name):
    """False for family names that never own a clearance zone (e.g. a bare
    housekeeping pad). Substring match, case-insensitive."""
    s = (family_name or '').upper()
    return not any(t in s for t in _NON_OWNER_TOKENS)


def facing_trusted(mount, family_name):
    """Whether we trust a FacingOrientation enough to build a working-space box.
    Wall-hosted gear (mount 'face') faces reliably away from its wall; a
    free-standing panel/switchboard family also has a meaningful face. Anything
    else -> skip the working-space zone rather than guess a direction (a wrong
    facing would invent a Critical in the wrong place)."""
    if mount == 'face':
        return True
    s = (family_name or '').upper()
    return any(t in s for t in _PANEL_TOKENS)


def is_leak_capable(sys_class, sys_abbr, category):
    """Whether an intruder is a leak-capable system (water/waste/sprinkler
    piping) -- classified from its system, NOT its category, so sprinkler
    PIPING counts. Ducts and conduit are not leak-capable."""
    for v in (sys_class, sys_abbr):
        s = (v or '').upper()
        if any(t in s for t in _LEAK_SYS_TOKENS):
            return True
    # A pipe with no system hint is conservatively leak-capable; a duct/tray is
    # not.
    cat = (category or '').lower()
    if 'pipe' in cat or 'sprinkler' in cat:
        return True
    return False


def _axis_of(vx, vy):
    """Return ('x'|'y', sign) if (vx, vy) points essentially along an axis,
    else None. Pure 2-vector classification for the working-space box."""
    n = math.hypot(vx, vy)
    if n < 1e-6:
        return None
    ux, uy = vx / n, vy / n
    if abs(ux) >= _AXIS_DOT:
        return ('x', 1.0 if ux > 0 else -1.0)
    if abs(uy) >= _AXIS_DOT:
        return ('y', 1.0 if uy > 0 else -1.0)
    return None


# ---------------------------------------------------------------------------
# Revit-runtime engine (validated only on a real run)
# ---------------------------------------------------------------------------

def find_clearance_intrusions(doc, owner_elems, owner_link, intruder_buckets,
                              rule, role_map, geom_cache=None, log=None,
                              status=None):
    """Build the zone for each owner and return raw pair dicts for every
    intruder inside it. rule is the clearance test id (C-NEC / C-NEC-W /
    M-NEC-PROT / M-SPR). Fully guarded: any per-owner failure is skipped."""
    out = []
    if not owner_elems or not intruder_buckets:
        return out
    # Precompute each intruder bucket's host-space bboxes once (broad phase).
    prepped = []
    for b in intruder_buckets:
        try:
            prepped.append(_prep_bucket(b))
        except Exception:
            continue
    # Structural ceiling candidates (host + Structural links), host coords,
    # collected once for the whole owner set. Only needed for C-NEC/M-NEC-PROT.
    struct = None
    if rule in ('C-NEC', 'M-NEC-PROT'):
        try:
            struct = _structural_tops(doc, role_map, log)
        except Exception:
            struct = []

    n_zoned = 0
    for owner in owner_elems:
        try:
            zone, cap_ft = _zone_for(owner, owner_link, doc, rule, struct, log)
        except Exception:
            zone, cap_ft = None, None
        if zone is None:
            continue
        n_zoned += 1
        try:
            hits = _intrusions(zone, prepped, rule)
        except Exception:
            hits = []
        for (intruder, intruder_bucket, mid) in hits:
            row = {
                'elem_a': intruder,
                'elem_b': owner,
                'midpoint': mid,
                'clearance_rule': rule,
                '_intruder_bucket': intruder_bucket,
            }
            if cap_ft is not None:
                row['zone_cap_ft'] = round(float(cap_ft), 3)
            out.append(row)
    if log:
        try:
            log("  - {0}: {1} owner zone(s) built, {2} intrusion(s)".format(
                rule, n_zoned, len(out)))
        except Exception:
            pass
    return out


def _prep_bucket(bucket):
    """(bucket, [(elem, host_bbox_tuple), ...]) for broad-phase filtering."""
    from clash_detect import broadphase
    link = bucket.get('link_instance')
    items = []
    for e in bucket.get('elements') or []:
        try:
            bb = _host_bbox(e, link)
            items.append((e, broadphase.bbox_tuple(bb)))
        except Exception:
            items.append((e, None))
    return {'bucket': bucket, 'items': items}


def _host_bbox(elem, link_instance):
    """Element bounding box in HOST coordinates (link bbox transformed), or None."""
    try:
        bb = elem.get_BoundingBox(None)
    except Exception:
        return None
    if bb is None:
        return None
    if link_instance is None:
        return bb
    try:
        return _bbox_to_host(bb, link_instance)
    except Exception:
        return None


def _bbox_to_host(bbox, link_instance):
    """Axis-aligned host bbox from a link-local bbox (8-corner transform)."""
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    xform = link_instance.GetTransform()
    xs, ys, zs = [], [], []
    for cx in (bbox.Min.X, bbox.Max.X):
        for cy in (bbox.Min.Y, bbox.Max.Y):
            for cz in (bbox.Min.Z, bbox.Max.Z):
                p = xform.OfPoint(XYZ(cx, cy, cz))
                xs.append(p.X); ys.append(p.Y); zs.append(p.Z)
    out = BoundingBoxXYZ()
    out.Min = XYZ(min(xs), min(ys), min(zs))
    out.Max = XYZ(max(xs), max(ys), max(zs))
    return out


def _zone_for(owner, owner_link, doc, rule, struct, log):
    """(zone_solid_host, cap_ft) for one owner + rule, or (None, None)."""
    bb = _host_bbox(owner, owner_link)
    if bb is None:
        return None, None
    fam = _family_name(owner)
    if not is_zone_owner(fam):
        return None, None
    mnx, mny, mnz = bb.Min.X, bb.Min.Y, bb.Min.Z
    mxx, mxy, mxz = bb.Max.X, bb.Max.Y, bb.Max.Z

    if rule == 'M-SPR':
        return _sprinkler_zone(owner, owner_link, bb), None

    if rule == 'C-NEC-W':
        return _working_zone(owner, bb, fam), None

    # C-NEC / M-NEC-PROT both need the structural cap above the gear footprint.
    lowest = _lowest_struct_above(struct, mnx, mny, mxx, mxy, mxz)
    cap = dedicated_cap_ft(mxz, lowest)
    if rule == 'C-NEC':
        return _box_solid(mnx, mny, mxx, mxy, mxz, cap), cap
    if rule == 'M-NEC-PROT':
        # Band between the top of the dedicated space and the real ceiling.
        # Only meaningful when there is genuine headroom (a modeled ceiling
        # above +6 ft), else no zone (never guess next-floor piping).
        band_base = mxz + DEDICATED_HEIGHT_FT
        if lowest is None or lowest <= band_base + PROT_BAND_MIN_FT:
            return None, None
        return _box_solid(mnx, mny, mxx, mxy, band_base, lowest), lowest
    return None, None


def _working_zone(owner, bb, fam):
    """NEC working-space box in front of the gear, or None when the facing is
    not trustworthy or not axis-aligned."""
    mount = _mount_of(owner)
    if not facing_trusted(mount, fam):
        return None
    facing = _facing_xy(owner)
    if facing is None:
        return None
    axis = _axis_of(facing[0], facing[1])
    if axis is None:
        return None
    depth_ft = working_depth_in(voltage_condition(_type_or_name(owner))) / 12.0
    min_w_ft = WORKING_MIN_WIDTH_IN / 12.0
    z0 = bb.Min.Z
    z1 = z0 + WORKING_HEIGHT_FT
    which, sign = axis
    if which == 'x':
        wy = max(min_w_ft, bb.Max.Y - bb.Min.Y)
        cy = 0.5 * (bb.Min.Y + bb.Max.Y)
        y0, y1 = cy - wy / 2.0, cy + wy / 2.0
        if sign > 0:
            x0, x1 = bb.Max.X, bb.Max.X + depth_ft
        else:
            x0, x1 = bb.Min.X - depth_ft, bb.Min.X
    else:
        wx = max(min_w_ft, bb.Max.X - bb.Min.X)
        cx = 0.5 * (bb.Min.X + bb.Max.X)
        x0, x1 = cx - wx / 2.0, cx + wx / 2.0
        if sign > 0:
            y0, y1 = bb.Max.Y, bb.Max.Y + depth_ft
        else:
            y0, y1 = bb.Min.Y - depth_ft, bb.Min.Y
    return _box_solid(x0, y0, x1, y1, z0, z1)


def _sprinkler_zone(head, head_link, bb):
    """A conservative NFPA obstruction box around a sprinkler head. Dormant on
    the current model (no modeled heads).

    The precise NFPA 13 10.2.7 rule keys the standoff on 3x the OBSTRUCTION's
    max dimension (capped 24 in) -- but the zone is built per HEAD, before we
    know which intruder we're testing, so we cannot size it from the
    obstruction here (sizing it from the head, as an earlier draft did, applied
    the 3x rule to the wrong element). Instead we build a fixed box at the 24
    in CAP -- the largest standoff any obstruction can require -- so it never
    misses a real violation. It will over-flag an obstruction whose true 3x
    standoff is smaller; the precise per-obstruction 3x test (and the measured
    spr_clearance_in) is a validation-time task for the first sprinklered
    model, done at intrusion time where the intruder cross-section is known."""
    r_ft = SPRINKLER_STANDOFF_IN / 12.0
    cx = 0.5 * (bb.Min.X + bb.Max.X)
    cy = 0.5 * (bb.Min.Y + bb.Max.Y)
    cz = 0.5 * (bb.Min.Z + bb.Max.Z)
    return _box_solid(cx - r_ft, cy - r_ft, cx + r_ft, cy + r_ft,
                      cz - r_ft, cz + r_ft)


def _box_solid(min_x, min_y, max_x, max_y, z_base, z_top):
    """An axis-aligned box Solid (host coords) from a rectangle extruded +Z.
    Returns None for a degenerate (sub-1/32-inch) extent."""
    from Autodesk.Revit.DB import (XYZ, Line, CurveLoop,
                                   GeometryCreationUtilities)
    from System.Collections.Generic import List as NetList
    h = z_top - z_base
    if (h <= _MIN_FT or (max_x - min_x) <= _MIN_FT
            or (max_y - min_y) <= _MIN_FT):
        return None
    p0 = XYZ(min_x, min_y, z_base)
    p1 = XYZ(max_x, min_y, z_base)
    p2 = XYZ(max_x, max_y, z_base)
    p3 = XYZ(min_x, max_y, z_base)
    loop = CurveLoop()
    loop.Append(Line.CreateBound(p0, p1))
    loop.Append(Line.CreateBound(p1, p2))
    loop.Append(Line.CreateBound(p2, p3))
    loop.Append(Line.CreateBound(p3, p0))
    loops = NetList[CurveLoop]()
    loops.Add(loop)
    return GeometryCreationUtilities.CreateExtrusionGeometry(
        loops, XYZ.BasisZ, h)


def _intrusions(zone_host, prepped, rule):
    """[(intruder_elem, bucket, midpoint_list), ...] for elements inside the
    zone. Broad-phases by host-bbox overlap with the zone bbox, then confirms
    with ElementIntersectsSolidFilter in each bucket's own coordinate frame."""
    from Autodesk.Revit.DB import (ElementIntersectsSolidFilter,
                                   FilteredElementCollector, ElementId)
    from System.Collections.Generic import List as NetList
    from clash_detect import linked, broadphase
    from clash_detect._compat import eid_int

    zbb = broadphase.bbox_tuple(_solid_host_bbox(zone_host))
    out = []
    for prep in prepped:
        bucket = prep['bucket']
        link = bucket.get('link_instance')
        bdoc = bucket.get('doc')
        seed = NetList[ElementId]()
        by_id = {}
        for (elem, bbt) in prep['items']:
            if zbb is not None and bbt is not None and not broadphase.overlaps(
                    zbb, bbt):
                continue
            try:
                seed.Add(elem.Id)
                by_id[eid_int(elem.Id)] = (elem, bbt)
            except Exception:
                continue
        if seed.Count == 0:
            continue
        try:
            if link is None:
                zone_in_frame = zone_host
            else:
                zone_in_frame = linked.host_solid_in_link_space(zone_host, link)
            hits = (FilteredElementCollector(bdoc, seed)
                    .WherePasses(ElementIntersectsSolidFilter(zone_in_frame))
                    .ToElements())
        except Exception:
            continue
        for elem in hits:
            key = eid_int(elem.Id)
            _, bbt = by_id.get(key, (None, None))
            mid = _bbox_center_list(bbt) if bbt is not None else _solid_center(
                zone_host)
            out.append((elem, bucket, mid))
    return out


def _structural_tops(doc, role_map, log):
    """Host-coord bboxes of structural ceiling candidates (framing, floors,
    columns, roofs) from the host doc and any Structural-role links. Ceilings
    (OST_Ceilings) are deliberately excluded -- they are not the structure."""
    from clash_detect import linked
    names = ['OST_StructuralFraming', 'OST_Floors', 'OST_StructuralColumns',
             'OST_Roofs']
    out = []
    try:
        for e in linked.collect_doc_elements(doc, names, log=log) or []:
            bb = _host_bbox(e, None)
            if bb is not None:
                out.append(bb)
    except Exception:
        pass
    try:
        for link in linked.links_for_role(doc, role_map, 'Structural'):
            for e in linked.collect_link_elements(link, names) or []:
                bb = _host_bbox(e, link)
                if bb is not None:
                    out.append(bb)
    except Exception:
        pass
    return out


def _lowest_struct_above(struct, mnx, mny, mxx, mxy, top_z):
    """Lowest bottom-elevation (ft) of a structural bbox that sits ABOVE the
    gear top and overlaps its footprint in plan, or None."""
    if not struct:
        return None
    best = None
    for bb in struct:
        try:
            if bb.Max.X < mnx or bb.Min.X > mxx:
                continue
            if bb.Max.Y < mny or bb.Min.Y > mxy:
                continue
            bot = bb.Min.Z
            if bot <= top_z + _MIN_FT:
                continue
            if bot > top_z + STRUCT_PROBE_FT:
                continue
            if best is None or bot < best:
                best = bot
        except Exception:
            continue
    return best


# --- small geometry accessors (guarded) ------------------------------------

def _solid_host_bbox(solid):
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    try:
        bb = solid.GetBoundingBox()
    except Exception:
        return None
    if bb is None:
        return None
    # Solid.GetBoundingBox is in the solid's own space; our zone solids are
    # built directly in host coords with an identity transform, so Min/Max map
    # straight through.
    try:
        t = bb.Transform
        lo = t.OfPoint(bb.Min)
        hi = t.OfPoint(bb.Max)
    except Exception:
        lo, hi = bb.Min, bb.Max
    out = BoundingBoxXYZ()
    out.Min = XYZ(min(lo.X, hi.X), min(lo.Y, hi.Y), min(lo.Z, hi.Z))
    out.Max = XYZ(max(lo.X, hi.X), max(lo.Y, hi.Y), max(lo.Z, hi.Z))
    return out


def _bbox_center_list(bbt):
    """Center XYZ (as [x,y,z]) of a broadphase bbox tuple (minx,miny,minz,maxx,maxy,maxz)."""
    try:
        return [0.5 * (bbt[0] + bbt[3]), 0.5 * (bbt[1] + bbt[4]),
                0.5 * (bbt[2] + bbt[5])]
    except Exception:
        return None


def _solid_center(solid):
    try:
        c = solid.ComputeCentroid()
        return [c.X, c.Y, c.Z]
    except Exception:
        return None


# --- owner property reads (guarded) ----------------------------------------

def _family_name(elem):
    from Autodesk.Revit.DB import BuiltInParameter
    for getter in (
        lambda: elem.Symbol.Family.Name,
        lambda: elem.get_Parameter(
            BuiltInParameter.ELEM_FAMILY_PARAM).AsValueString(),
    ):
        try:
            v = getter()
            if v:
                return v
        except Exception:
            continue
    return ''


def _type_or_name(elem):
    from Autodesk.Revit.DB import BuiltInParameter
    for getter in (
        lambda: elem.Name,
        lambda: elem.get_Parameter(
            BuiltInParameter.ELEM_TYPE_PARAM).AsValueString(),
        lambda: _family_name(elem),
    ):
        try:
            v = getter()
            if v:
                return v
        except Exception:
            continue
    return ''


def _mount_of(elem):
    """'face' when the element is hosted on a wall/face, else 'free'. Mirrors
    the enrich.py mount read at a coarse level (we only need face vs free)."""
    try:
        host = getattr(elem, 'Host', None)
        if host is not None:
            return 'face'
    except Exception:
        pass
    return 'free'


def _facing_xy(elem):
    """The element's FacingOrientation projected to (x, y), or None."""
    try:
        f = elem.FacingOrientation
        return (f.X, f.Y)
    except Exception:
        return None
