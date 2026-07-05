# -*- coding: utf-8 -*-
"""Per-element MEP enrichment captured at detection time.

The importance engine (lib/clash_score) is pure data: everything it needs
must be captured here, while the live Revit Element handle is in scope
(runner._make_ref). Every field is nullable and every read is try/excepted;
a missing parameter degrades the score's confidence, never breaks a run.

Fields added to each element ref:
    sys_class   System Classification string ("Sanitary", "Supply Air", ...)
    sys_name    System Name string
    sys_abbr    System Abbreviation string
    dims_in     [diameter] or [width, height] in INCHES, or None
    ins_in      insulation thickness in INCHES (0.0 = explicitly none)
    slope       raw pipe slope value (nonzero = sloped run; gravity signal)
    level       level name string
    discipline  dbHMS discipline from the category table (authoritative on
                new records; old records fall back to a category-name map
                inside clash_score)

Insulation is deliberately NOT read from a builtin parameter:
RBS_REFERENCE_INSULATION_THICKNESS is officially obsolete in Revit 2025/2026
(replaced by DUCT_/PIPE_INSULATION_THICKNESS, which do not exist in 2024)
and returns 0 on insulated fittings even where live. The version-stable path
(API stable since 2012, covers curves, fittings, accessories, linked docs)
is one FilteredElementCollector pass per document over DuctInsulation +
PipeInsulation, building a HostElementId -> thickness map. Cached per bucket
(one bucket = one document), so the pass runs once per doc per test run.
"""

from clash_detect._compat import eid_int


def mep_facts(elem, bucket, cat_id=None):
    """Return the enrichment dict for one element. Never raises.

    Phase 2 (importance v2) adds bbox dims (a size fallback for equipment/
    fixtures that carry no native dims), absolute top/bot elevations, family
    + host + mount (for the N-PT mount signal and R-NEST nesting), the
    super-component root, and the parsed multi-value sys_class list. Every
    field is nullable; every read is try/excepted."""
    facts = {
        'sys_class': None, 'sys_name': None, 'sys_abbr': None,
        'dims_in': None, 'ins_in': None, 'slope': None,
        'level': None, 'discipline': None,
        # --- Phase 2 additions (all nullable) ---
        'bbox_in': None, 'top_ft': None, 'bot_ft': None,
        'family': None, 'host_id': None, 'host_cat': None,
        'mount': None, 'super_root_id': None, 'sys_class_list': None,
        # --- Phase 3 arch/structure facts (all nullable) ---
        'fire_rating_raw': None, 'fire_rating_hr': None, 'thickness_in': None,
        'wall_function': None, 'is_structural': None, 'is_rated': None,
    }
    try:
        facts['sys_class'] = _bip_str(elem, 'RBS_SYSTEM_CLASSIFICATION_PARAM')
        facts['sys_name'] = _bip_str(elem, 'RBS_SYSTEM_NAME_PARAM')
        facts['sys_abbr'] = _bip_str(elem, 'RBS_DUCT_PIPE_SYSTEM_ABBREVIATION_PARAM')
        facts['dims_in'] = _dims_in(elem)
        facts['slope'] = _bip_double(elem, 'RBS_PIPE_SLOPE')
        facts['level'] = _level_name(elem, bucket)
        # None = the collector pass failed (unknown), which downstream rules
        # must treat differently from an explicit 0.0 (known-uninsulated).
        ins_map = _insulation_map(bucket)
        if ins_map is not None:
            facts['ins_in'] = ins_map.get(eid_int(elem.Id), 0.0)
        sc = facts['sys_class']
        if sc:
            facts['sys_class_list'] = [s.strip() for s in sc.split(',') if s.strip()]
    except Exception:
        pass
    try:
        facts.update(_bbox_facts(elem, bucket))
    except Exception:
        pass
    try:
        facts.update(_instance_facts(elem))
    except Exception:
        pass
    try:
        facts.update(_arch_facts(elem))
    except Exception:
        pass
    try:
        if cat_id is not None:
            from clash_core import categories
            facts['discipline'] = categories.discipline_for_category_id(cat_id)
    except Exception:
        pass
    return facts


# ---------------------------------------------------------------------------
# Phase 2 geometry / instance facts (never raise)
# ---------------------------------------------------------------------------

def _bbox_facts(elem, bucket):
    """World-AABB dims (inches) + absolute top/bot (host feet). Dims are the
    non-curve size fallback for equipment/fixtures; a LocationCurve's world
    AABB is fiction, so downstream (clash_score._max_dim_in) uses these only
    for non-routed categories. Link elements: dims are link-frame extents
    (fine for the mostly axis-aligned placed objects this feeds), elevation
    shifted by the link origin Z."""
    out = {'bbox_in': None, 'top_ft': None, 'bot_ft': None}
    try:
        bb = elem.get_BoundingBox(None)
        if bb is None:
            return out
        mn, mx = bb.Min, bb.Max
        out['bbox_in'] = [abs(float(mx.X) - float(mn.X)) * 12.0,
                          abs(float(mx.Y) - float(mn.Y)) * 12.0,
                          abs(float(mx.Z) - float(mn.Z)) * 12.0]
        zoff = 0.0
        inst = bucket.get('link_instance')
        if inst is not None:
            try:
                zoff = float(inst.GetTotalTransform().Origin.Z)
            except Exception:
                zoff = 0.0
        out['bot_ft'] = float(mn.Z) + zoff
        out['top_ft'] = float(mx.Z) + zoff
    except Exception:
        pass
    return out


def _instance_facts(elem):
    """family / host / mount / super-component root for a FamilyInstance.
    All None on non-instances or on any read failure."""
    out = {'family': None, 'host_id': None, 'host_cat': None,
           'mount': None, 'super_root_id': None}
    try:
        sym = getattr(elem, 'Symbol', None)
        if sym is not None:
            try:
                out['family'] = sym.FamilyName
            except Exception:
                pass
        host = getattr(elem, 'Host', None)
        host_cat = None
        if host is not None:
            try:
                out['host_id'] = eid_int(host.Id)
                hc = host.Category
                host_cat = hc.Name if hc is not None else None
                out['host_cat'] = host_cat
            except Exception:
                pass
        face = None
        try:
            face = getattr(elem, 'HostFace', None)
        except Exception:
            face = None
        if host_cat == 'Ceilings':
            out['mount'] = 'ceiling'
        elif host_cat == 'Walls':
            out['mount'] = 'wall'
        elif host_cat in ('Floors', 'Roofs'):
            out['mount'] = 'floor'
        elif face is not None:
            out['mount'] = 'face'
        elif host is not None:
            out['mount'] = 'hosted'
        else:
            out['mount'] = 'free'
        try:
            sc = getattr(elem, 'SuperComponent', None)
            root = None
            guard = 0
            while sc is not None and guard < 10:
                root = sc
                sc = getattr(sc, 'SuperComponent', None)
                guard += 1
            if root is not None:
                out['super_root_id'] = eid_int(root.Id)
        except Exception:
            pass
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Phase 3 arch/structure facts (never raise). Drive M-RATED (rated-assembly
# damper), M-PEN / N1 grades (penetration classification with wall thickness),
# M-STRUCT-ZONE (beam elevations), and the arch-modeled-bearing-wall reroute.
# ---------------------------------------------------------------------------

_ARCH_STRUCT_CATS = ('Walls', 'Floors', 'Roofs', 'Ceilings',
                     'Structural Framing', 'Structural Columns',
                     'Structural Foundations')


def _arch_facts(elem):
    out = {'fire_rating_raw': None, 'fire_rating_hr': None,
           'thickness_in': None, 'wall_function': None,
           'is_structural': None, 'is_rated': None}
    try:
        cat = elem.Category
        cat_name = cat.Name if cat is not None else None
    except Exception:
        return out
    if cat_name not in _ARCH_STRUCT_CATS:
        return out
    try:
        raw = _bip_str(elem, 'FIRE_RATING')
        if not raw:
            te = _type_of(elem)
            if te is not None:
                raw = _bip_str(te, 'FIRE_RATING')
        out['fire_rating_raw'] = raw
        out['fire_rating_hr'] = _parse_hr(raw)
        out['thickness_in'] = _thickness_in(elem, cat_name)
        if cat_name == 'Walls':
            te = _type_of(elem)
            if te is not None:
                out['wall_function'] = _bip_int(te, 'FUNCTION_PARAM')
        out['is_structural'] = _is_structural(elem, cat_name)
        out['is_rated'] = _is_rated(out['fire_rating_hr'], elem)
    except Exception:
        pass
    return out


def _type_of(elem):
    try:
        return elem.Document.GetElement(elem.GetTypeId())
    except Exception:
        return None


def _type_name(elem):
    te = _type_of(elem)
    if te is not None:
        try:
            return te.Name
        except Exception:
            pass
    return None


def _parse_hr(raw):
    """'2 HR' / '2-hour' / '120 min' / '2' -> float hours, else None.
    Many firms put the rating in the TYPE NAME, not the parameter; that path
    is handled by _is_rated, so a blank param here is fine."""
    if not raw:
        return None
    import re
    s = str(raw).strip().lower()
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except (TypeError, ValueError):
        return None
    if 'min' in s:
        return v / 60.0
    return v


def _thickness_in(elem, cat_name):
    if cat_name == 'Walls':
        try:
            w = float(elem.Width)
            if w > 0:
                return w * 12.0
        except Exception:
            pass
    return _bip_len_in(elem, 'FLOOR_ATTR_THICKNESS_PARAM')


def _is_structural(elem, cat_name):
    if cat_name in ('Structural Framing', 'Structural Columns',
                    'Structural Foundations'):
        return True
    if cat_name == 'Walls':
        v = _bip_int(elem, 'WALL_STRUCTURAL_SIGNIFICANT')
        return bool(v) if v is not None else None
    if cat_name == 'Floors':
        v = _bip_int(elem, 'FLOOR_PARAM_IS_STRUCTURAL')
        return bool(v) if v is not None else None
    return None


def _is_rated(hr, elem):
    """Tri-state: True on positive proof (rating >= 1 hr, or a type name that
    reads rated/shaft), None when unknown -- never False, so a rule keyed on
    `is_rated is True` fires only on proof."""
    if hr is not None and hr >= 1.0:
        return True
    import re
    tn = (_type_name(elem) or '').upper()
    if re.search(r'\d\s*HR', tn) or 'RATED' in tn or 'SHAFT' in tn:
        return True
    return None


def display_facts(elem):
    """System + size facts for one element, no document context needed.

    A lighter sibling of mep_facts for the glTF exporter's element-info
    extras (click-an-element card in the web viewers): system class/name/
    abbreviation and dims, nothing that needs a bucket (no insulation map,
    no level, no discipline). Never raises; absent values are simply
    missing from the dict.
    """
    out = {}
    try:
        for key, param in (('sys_class', 'RBS_SYSTEM_CLASSIFICATION_PARAM'),
                           ('sys_name', 'RBS_SYSTEM_NAME_PARAM'),
                           ('sys_abbr', 'RBS_DUCT_PIPE_SYSTEM_ABBREVIATION_PARAM')):
            v = _bip_str(elem, param)
            if v:
                out[key] = v
        dims = _dims_in(elem)
        if dims:
            out['dims_in'] = dims
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------

def _bip(elem, name):
    """Look up a BuiltInParameter by NAME so a member missing from an older
    Revit version degrades to None instead of raising at import time."""
    try:
        from Autodesk.Revit.DB import BuiltInParameter
        bip = getattr(BuiltInParameter, name, None)
        if bip is None:
            return None
        return elem.get_Parameter(bip)
    except Exception:
        return None


def _bip_str(elem, name):
    p = _bip(elem, name)
    try:
        if p is None:
            return None
        s = p.AsString()
        if not s:
            s = p.AsValueString()
        return s or None
    except Exception:
        return None


def _bip_double(elem, name):
    p = _bip(elem, name)
    try:
        if p is None or not p.HasValue:
            return None
        return float(p.AsDouble())
    except Exception:
        return None


def _bip_int(elem, name):
    p = _bip(elem, name)
    try:
        if p is None or not p.HasValue:
            return None
        return int(p.AsInteger())
    except Exception:
        return None


def _bip_len_in(elem, name):
    """Length parameter in internal feet -> inches; None when absent/zero."""
    v = _bip_double(elem, name)
    if not v:
        return None
    return v * 12.0


def _dims_in(elem):
    """[dia] for round things, [w, h] for rectangular, else None.
    Tried in order: pipe dia, conduit dia (trade then outer), round duct
    dia, rect duct w/h, cable tray w/h. Fittings mostly report nothing,
    which is fine (unknown dims are treated conservatively downstream)."""
    for name in ('RBS_PIPE_DIAMETER_PARAM',
                 'RBS_CONDUIT_DIAMETER_PARAM',
                 'RBS_CONDUIT_OUTER_DIAM_PARAM',
                 'RBS_CURVE_DIAMETER_PARAM'):
        dia = _bip_len_in(elem, name)
        if dia is not None:
            return [dia]
    for w_name, h_name in (('RBS_CURVE_WIDTH_PARAM', 'RBS_CURVE_HEIGHT_PARAM'),
                           ('RBS_CABLETRAY_WIDTH_PARAM', 'RBS_CABLETRAY_HEIGHT_PARAM')):
        w = _bip_len_in(elem, w_name)
        h = _bip_len_in(elem, h_name)
        if w is not None and h is not None:
            return [w, h]
    return None


def _level_name(elem, bucket):
    try:
        lid = elem.LevelId
        if lid is not None and eid_int(lid) not in (-1, None):
            doc = bucket.get('doc')
            if doc is not None:
                lv = doc.GetElement(lid)
                if lv is not None and lv.Name:
                    return lv.Name
    except Exception:
        pass
    return _bip_str(elem, 'RBS_START_LEVEL_PARAM')


# ---------------------------------------------------------------------------
# Insulation map (per document, cached on the bucket)
# ---------------------------------------------------------------------------

def _doc_key(doc):
    try:
        return u'{0}|{1}'.format(doc.Title or '', doc.PathName or '')
    except Exception:
        return str(id(doc))


def _insulation_map(bucket):
    """HostElementId -> combined insulation thickness (inches) for the
    bucket's document. Returns None when the collector pass FAILS (unknown
    is not the same as known-zero, and a failure is never cached). Cached
    per document in the session-scope dict the runner threads through
    `bucket['_ins_cache']` (buckets are rebuilt every test, so a per-bucket
    memo alone would re-run the collector for each test and side), with a
    per-bucket memo as fallback."""
    memo = bucket.get('_ins_map')
    if memo is not None:
        return memo
    doc = bucket.get('doc')
    if doc is None:
        return None
    cache = bucket.get('_ins_cache')
    key = _doc_key(doc)
    if cache is not None and key in cache:
        bucket['_ins_map'] = cache[key]
        return cache[key]
    m = {}
    try:
        from Autodesk.Revit.DB import FilteredElementCollector
        from Autodesk.Revit.DB.Plumbing import PipeInsulation
        from Autodesk.Revit.DB.Mechanical import DuctInsulation
        for cls in (PipeInsulation, DuctInsulation):
            for ins in FilteredElementCollector(doc).OfClass(cls):
                try:
                    host = eid_int(ins.HostElementId)
                    m[host] = m.get(host, 0.0) + float(ins.Thickness) * 12.0
                except Exception:
                    continue
    except Exception:
        return None    # transient failure: unknown, do NOT cache as empty
    bucket['_ins_map'] = m
    if cache is not None:
        cache[key] = m
    return m
