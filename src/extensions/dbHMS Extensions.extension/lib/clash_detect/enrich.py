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
    """Return the enrichment dict for one element. Never raises."""
    facts = {
        'sys_class': None, 'sys_name': None, 'sys_abbr': None,
        'dims_in': None, 'ins_in': None, 'slope': None,
        'level': None, 'discipline': None,
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
