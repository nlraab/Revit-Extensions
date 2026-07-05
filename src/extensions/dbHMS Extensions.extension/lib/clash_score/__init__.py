# -*- coding: utf-8 -*-
"""Importance engine: noise suppression (Layer A) + tiered scoring (Layer B).

Design: CLASH_IMPORTANCE_RESEARCH.md (Clash Detection.panel/), section 9.
The one-paragraph version: a clash's importance is the cost of moving the
thing that must move. Fixed structure/architecture never sit on a ladder --
they are context that selects which TIER RULE fires. An ordered rule list
assigns the band (Critical/Major/Minor) directly, each with a one-sentence
reason; a small sub-score, clamped so it can never cross a band boundary,
orders clashes within the band. Suppression never destroys data: suppressed
clashes stay stored, fully scored, and visible behind a count.

Contract with the rest of the codebase:
  - Pure data, zero Revit imports at module scope. Runs identically in
    IronPython 2.7 (Revit, at merge time) and CPython 3 (tests, rescore).
  - `score_all(clashes)` is called AFTER `merge.merge_runs` on the merged
    list (never on raw detection rows: merge's new-clash literal would drop
    a raw-row score). It stamps `clash['importance']` in place.
  - Inputs are nullable everywhere. A rule fires only when every feature it
    tests is non-null ("suppress only on proof"); missing data degrades to
    a defensible mid-band with confidence='degraded', never to silence.
  - Fingerprints are untouched by design: identity.py hashes only test_id +
    (source, element_id) pair + bucketed midpoint, so neither the enrichment
    fields on refs nor this block can ever reset clash history.

Config: `defaults.DEFAULTS` is the FIRM STANDARD (one standard across all
projects -- Nathan, 2026-07-01). `score_all(clashes, config)` accepts an
override dict for tests; production callers pass nothing.
"""

import math
import re

from clash_score import defaults as _d

# Precompiled once at import (both interpreters). Point-fixture name regexes.
_POINT_FIXTURE_RES = tuple(re.compile(p) for p in _d.POINT_FIXTURE_NAME_RES)


def _round_half_away(x):
    """Round half away from zero, identical in IronPython 2.7 and CPython 3.
    (Py2 round() rounds half away; Py3 round() rounds half to even -- using
    either directly would make the offline harness disagree with production
    by 1 at boundaries.)"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0
    if f >= 0.0:
        return int(math.floor(f + 0.5))
    return int(math.ceil(f - 0.5))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_all(clashes, config=None):
    """Stamp `importance` on every clash dict, in place.

    Args:
        clashes: the MERGED clash list (dicts from clashes.json).
        config: optional override dict, shallow-merged over the firm
            defaults (tests only; production uses the standard).

    Returns a summary dict:
        {'scored': N, 'suppressed': N,
         'bands': {'Critical': N, 'Major': N, 'Minor': N}}
    counting non-Resolved, non-suppressed clashes per band.
    """
    cfg = _merged_config(config)
    cluster_n = _cluster_counts(clashes, cfg)
    summary = {'scored': 0, 'suppressed': 0,
               'bands': {'Critical': 0, 'Major': 0, 'Minor': 0}}
    for i, c in enumerate(clashes):
        try:
            prev_rule = (c.get('importance') or {}).get('rule')
            imp = _score_one(c, cluster_n.get(i, 0), cfg, prev_rule)
        except Exception:
            # A scoring bug must never break a detection run. Degrade to the
            # fallback tier with no factors rather than crash.
            imp = _fallback_importance(cfg)
        c['importance'] = imp
        summary['scored'] += 1
        if imp['suppressed']:
            summary['suppressed'] += 1
        elif c.get('status') != 'Resolved':
            band = imp.get('band')
            if band in summary['bands']:
                summary['bands'][band] += 1
    return summary


# Same pure function; the alias documents intent at rescore call sites
# (Settings change, engine upgrade) where no detection run happens.
rescore_all = score_all


def calibration_report(clashes, title='', groups=None):
    """Markdown calibration report for the pre-ship labeling exercise
    (research doc section 13): band histogram, flag buckets, suppression
    and confidence counts, the top 20 by score, and a deterministic sample
    of the bottom (Minor + suppressed) for blind labeling. Deterministic
    (every Nth row), so two runs on the same data produce the same report.
    Pass the groups list to include the standing rack-retention assertion
    (a cluster group must never lose its last Major/Critical member to a
    demotion rule - NIUHTC judge requirement)."""
    lines = []
    lines.append('# Importance calibration report' +
                 (' - {0}'.format(title) if title else ''))
    lines.append('')
    active = [c for c in clashes if c.get('status') != 'Resolved']
    lines.append('Active (non-Resolved) clashes: **{0}**  (of {1} total)'.format(
        len(active), len(clashes)))
    lines.append('')

    bands = {'Critical': 0, 'Major': 0, 'Minor': 0}
    sup_by_rule = {}
    conf = {'full': 0, 'degraded': 0}
    for c in active:
        imp = c.get('importance') or {}
        if imp.get('suppressed'):
            rule = imp.get('suppress_rule') or '?'
            sup_by_rule[rule] = sup_by_rule.get(rule, 0) + 1
            continue
        band = imp.get('band')
        if band in bands:
            bands[band] += 1
        conf_v = imp.get('confidence') or 'degraded'
        if conf_v in conf:
            conf[conf_v] += 1

    lines.append('## Bands (active, unsuppressed)')
    lines.append('')
    lines.append('| Band | Count |')
    lines.append('|---|---|')
    for b in ('Critical', 'Major', 'Minor'):
        lines.append('| {0} | {1} |'.format(b, bands[b]))
    lines.append('')
    lines.append('Healthy steady state is <= 20 open Criticals (the meeting '
                 'agenda size). More than ~25 means the tier rules need '
                 'tightening -- tune rules, never the 70/40 cutoffs.')
    lines.append('')
    lines.append('## Suppressed (still stored, still scored)')
    lines.append('')
    if sup_by_rule:
        lines.append('| Rule | Count |')
        lines.append('|---|---|')
        for rule in sorted(sup_by_rule):
            lines.append('| {0} | {1} |'.format(rule, sup_by_rule[rule]))
    else:
        lines.append('(none)')
    lines.append('')
    lines.append('Confidence: {0} full, {1} degraded (degraded = scored from '
                 'category fallback; re-run detection to enrich).'.format(
                     conf['full'], conf['degraded']))
    lines.append('')

    # Named Minor buckets: these are batch schedules, not queues.
    flag_counts = {}
    for c in active:
        imp = c.get('importance') or {}
        if imp.get('suppressed'):
            continue
        for f in (imp.get('flags') or []):
            flag_counts[f] = flag_counts.get(f, 0) + 1
    if flag_counts:
        lines.append('## Named buckets (flags)')
        lines.append('')
        lines.append('| Flag | Count |')
        lines.append('|---|---|')
        for f in sorted(flag_counts):
            lines.append('| {0} | {1} |'.format(f, flag_counts[f]))
        lines.append('')

    # Rack retention: how many open clusters carry no Major/Critical
    # member. NOT an expect-zero assertion - sleeve-dense wall zones are
    # legitimately hollow, and coordinators approving a rack's last Major
    # member hollows it too. The signal is the TREND: compare with the
    # archived previous report and investigate an INCREASE after a rule
    # change.
    if groups:
        by_id_g = {}
        for c in clashes:
            if c.get('id'):
                by_id_g[c['id']] = c
        hollow = 0
        for g in groups:
            if g.get('axis') != 'cluster' or g.get('status') == 'MergedInto':
                continue
            open_m = []
            for m in (g.get('member_ids') or []):
                cm = by_id_g.get(m)
                if (cm is not None and cm.get('status') in ('Open', 'Reviewed')
                        and not (cm.get('importance') or {}).get('suppressed')):
                    open_m.append(cm)
            if not open_m:
                continue
            if not any((cm.get('importance') or {}).get('band')
                       in ('Critical', 'Major') for cm in open_m):
                hollow += 1
        lines.append('Rack retention: {0} open cluster group(s) with no '
                     'Major/Critical member (sleeve-dense zones and racks '
                     'whose Majors were decided both count). Compare with '
                     'the archived previous report; investigate an INCREASE '
                     'after a rule change.'.format(hollow))
        lines.append('')

    def _row(c):
        imp = c.get('importance') or {}
        a = c.get('ref_a') or {}
        b = c.get('ref_b') or {}
        return '| {0} | {1} | {2} | {3} | {4} / {5} | {6} |'.format(
            c.get('seq', '?'), imp.get('score', '?'), imp.get('band', '?'),
            imp.get('rule', '?'),
            a.get('category') or '?', b.get('category') or '?',
            (imp.get('reason') or '').replace('|', '/'))

    ranked = sorted(
        [c for c in active if not (c.get('importance') or {}).get('suppressed')],
        key=lambda c: -((c.get('importance') or {}).get('score') or 0))
    lines.append('## Top 20 by score -- label each: agenda-worthy or not?')
    lines.append('')
    lines.append('| # | Score | Band | Rule | Pair | Reason |')
    lines.append('|---|---|---|---|---|---|')
    for c in ranked[:20]:
        lines.append(_row(c))
    lines.append('')

    low = ([c for c in ranked if (c.get('importance') or {}).get('band') == 'Minor'] +
           [c for c in active if (c.get('importance') or {}).get('suppressed')])
    step = max(1, len(low) // 30)
    sample = low[::step][:30]
    lines.append('## Bottom sample ({0} of {1} Minor/suppressed) -- '
                 'label each: safely down-ranked?'.format(len(sample), len(low)))
    lines.append('')
    lines.append('| # | Score | Band | Rule | Pair | Reason |')
    lines.append('|---|---|---|---|---|---|')
    for c in sample:
        lines.append(_row(c))
    lines.append('')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _merged_config(config):
    cfg = dict(_d.DEFAULTS)
    if config:
        for k, v in config.items():
            if k == 'rules' and isinstance(v, dict):
                rules = dict(cfg.get('rules') or {})
                rules.update(v)
                cfg['rules'] = rules
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# Congestion: clashes within cluster_radius_ft of each midpoint. Uniform
# grid so 2000 clashes stay cheap in IronPython. Counts run over ALL
# non-Resolved clashes (suppression must not feed back into the cluster
# signal that gates suppression).
# ---------------------------------------------------------------------------

def _cluster_counts(clashes, cfg):
    radius = float(cfg.get('cluster_radius_ft') or 5.0)
    pts = []
    for i, c in enumerate(clashes):
        if c.get('status') == 'Resolved':
            continue
        mid = c.get('midpoint')
        if not mid or len(mid) < 3:
            continue
        try:
            pts.append((i, float(mid[0]), float(mid[1]), float(mid[2])))
        except (TypeError, ValueError):
            continue

    cell = radius
    grid = {}
    for rec in pts:
        key = (int(rec[1] // cell), int(rec[2] // cell), int(rec[3] // cell))
        grid.setdefault(key, []).append(rec)

    r2 = radius * radius
    out = {}
    for i, x, y, z in pts:
        n = 0
        kx, ky, kz = int(x // cell), int(y // cell), int(z // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j, jx, jy, jz in grid.get((kx + dx, ky + dy, kz + dz), ()):
                        if j == i:
                            continue
                        ddx, ddy, ddz = jx - x, jy - y, jz - z
                        if ddx * ddx + ddy * ddy + ddz * ddz <= r2:
                            n += 1
        out[i] = n
    return out


# ---------------------------------------------------------------------------
# Participant classification
# ---------------------------------------------------------------------------

def _discipline(ref):
    d = ref.get('discipline')
    if d:
        return d
    return _d.DISCIPLINE_BY_CAT.get(ref.get('category'))


def _fixed_kind(ref):
    """None when movable dbHMS MEP; else 'structural' / 'architectural' /
    'unknown'. Category decides FIRST (structure delivered inside the arch
    link still reads structural), then the link role. Movable requires a
    HOST element: anything living in a consultant's link is not dbHMS's to
    move, even when its category is an MEP one (the architect's plumbing
    fixtures, a duct in their model)."""
    disc = _discipline(ref)
    if disc == 'Structural':
        return 'structural'
    # Phase 3: an arch-modeled wall/floor flagged structural (a shear or
    # bearing wall drawn in the architect's model) is STRUCTURE, not
    # architecture -- it takes the C1/C2/M2 structural path, never the N1
    # sleeve demotion. Tri-state: only a positive is_structural reroutes.
    if (ref.get('is_structural') is True
            and ref.get('category') in ('Walls', 'Floors')):
        return 'structural'
    src = ref.get('source') or 'host'
    if src.startswith('link:'):
        role = src[5:]
        # The STRUCTURAL link's Walls/Floors are concrete shear walls and
        # decks, not architecture: the role must win over the arch
        # category there, or a gravity main through a structural deck
        # gets sleeve-schedule wording (NIUHTC finding: 97 rows).
        if role == 'Structural':
            return 'structural'
        if disc == 'Architectural' or role == 'Architectural':
            return 'architectural'
        return 'unknown'                # unmapped link role: not ours, kind unknown
    if disc == 'Architectural':
        return 'architectural'
    return None                          # host element: ours to move


def _max_dim_in(ref):
    dims = ref.get('dims_in')
    if dims:
        try:
            vals = [float(v) for v in dims if v is not None]
            if vals:
                return max(vals)
        except (TypeError, ValueError):
            pass
    # Non-curve size fallback (Phase 2): equipment/fixtures carry no native
    # dims, so use the captured world-AABB. NEVER for routed curves -- a
    # diagonal duct's AABB is fiction and would inflate its rigidity rung, so
    # this is gated to placed-object categories (which classify rigidity by
    # class, not size, so only the size sub-score is affected).
    cat = ref.get('category')
    if cat in _d.EQUIPMENT_CATS or cat in _d.MOUNTED_CATS:
        try:
            vals = [float(v) for v in (ref.get('bbox_in') or []) if v is not None]
            if vals:
                return max(vals)
        except (TypeError, ValueError):
            pass
    return None


def _name_blob(ref):
    parts = []
    for k in ('sys_name', 'sys_abbr', 'name'):
        v = ref.get(k)
        if v:
            parts.append(v)
    return ' '.join(parts).lower()


def _matches(blob, words):
    for w in words:
        if w in blob:
            return True
    return False


def _sys_class_set(ref):
    """The system-classification string is comma-joined on multi-connector
    equipment ("Supply Air,Hydronic Supply,Hydronic Return"), so exact
    membership tests silently fail. Split into a set for intersection tests."""
    raw = ref.get('sys_class') or ''
    return set(s.strip() for s in raw.split(',') if s.strip())


def _class_signal(ref, classes):
    """True when the ref's system classification indicates membership in
    `classes`, with the multi-system guard that keeps a pump carrying
    'Sanitary' among several connectors from reading as a gravity main:
      - a Pipes CURVE: any matching class (a real single-service main);
      - a pipe fitting/accessory, flex, or sprinkler: ALL classes must be in
        `classes` (a genuine gravity elbow is {'Sanitary'}; a multi-service
        fitting is not a subset, so it is excluded);
      - anything else (equipment, duct, fixture): never a pipe-system signal.
    """
    scs = _sys_class_set(ref)
    if not scs:
        return False
    cat = ref.get('category')
    if cat == 'Pipes':
        return bool(scs & classes)
    if cat in _d.PIPE_CATS or cat in _d.FLEX_CATS or cat in _d.SPRINKLER_CATS:
        return scs <= classes
    return False


def _is_gravity(ref, cfg):
    """Classification-primary, name-regex backstop, slope as third signal."""
    if _class_signal(ref, _d.GRAVITY_CLASSES):
        return True
    blob = _name_blob(ref)
    if _matches(blob, _d.STORM_NAME_WORDS):
        return True
    abbr = (ref.get('sys_abbr') or '').strip().upper()
    if abbr in _d.STORM_ABBRS:
        return True
    cat = ref.get('category')
    if cat in _d.PIPE_CATS:
        try:
            slope = ref.get('slope')
            if slope is not None and abs(float(slope)) > 1e-9:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _rigidity(ref, cfg):
    """(rigidity 0-5, source 'system'|'size'|'category', class string,
    near_threshold bool) for a MOVABLE ref. Class strings feed the code-risk
    sub-term and the reason templates."""
    cat = ref.get('category')
    dim = _max_dim_in(ref)
    blob = _name_blob(ref)
    near = False

    def _near(value, boundary):
        if value is None:
            return False
        frac = float(cfg.get('near_threshold_frac') or 0.15)
        return abs(value - boundary) <= boundary * frac

    # Mounted fixtures (troffers, diffusers, wall-hung sanitary, device
    # boxes) are cheap field-adjusted movers. Checked FIRST: a wall-hung
    # WC can carry a Sanitary system classification, and the gravity
    # branch below must never turn a fixture into a rigidity-5 "main"
    # (NIUHTC stage-1 retune).
    if cat in _d.MOUNTED_CATS:
        return 2, 'class', 'fixture', False

    # Grease / kitchen exhaust duct: the least movable thing we model
    # (IMC 506.3.7: 2 percent slope toward the hood).
    if cat in _d.DUCT_CATS and _matches(blob, _d.GREASE_NAME_WORDS):
        return 5, 'system', 'grease', False

    # Gravity drainage (IPC 704.1 slope is irreplaceable elevation).
    if _is_gravity(ref, cfg):
        if _sys_class_set(ref) == set(['Vent']):
            # Stage 2: a 2-in vent branch regrades cheaply; only vents at
            # or above the gravity-main size keep rigidity 4. Unknown
            # diameter stays 4, conservative.
            main = float(cfg.get('gravity_main_in') or 3.0)
            if dim is not None and dim < main:
                return 2, 'size', 'gravity_vent', _near(dim, main)
            return 4, ('size' if dim is not None else 'system'), \
                'gravity_vent', _near(dim, main)
        main = float(cfg.get('gravity_main_in') or 3.0)
        if dim is not None and dim >= main:
            return 5, 'system', 'gravity', _near(dim, main)
        # Unknown diameter takes the conservative rung 4: fittings and
        # accessories mostly report no dims, and defaulting them to 5 would
        # flood Critical with every sanitary elbow that grazes framing.
        # Real gravity MAINS are curves, carry dims, and hit rung 5.
        return (4, ('size' if dim is not None else 'category'), 'gravity',
                _near(dim, main))

    if _matches(blob, _d.CONDENSATE_NAME_WORDS):
        return 4, 'system', 'condensate', False
    if _matches(blob, _d.MEDGAS_NAME_WORDS):
        return 4, 'system', 'medgas', False

    # NOTE on rig_src values: 'system' / 'size' = classified from enriched
    # data; 'class' = the category IS the primary classifier for this class
    # (tray, flex, sprinkler, equipment, tech) so it is NOT degraded data;
    # 'category' = a true data-missing fallback (drives confidence).
    if _class_signal(ref, _d.FP_DRY_CLASSES):
        return 4, 'system', 'fp_dry', False
    if cat in _d.SPRINKLER_CATS:
        return 1, 'class', 'sprinkler', False
    if _class_signal(ref, _d.FP_WET_CLASSES):
        main = float(cfg.get('fp_main_in') or 4.0)
        if dim is not None and dim >= main:
            return 3, 'size', 'fp_wet', _near(dim, main)
        return 2, ('size' if dim is not None else 'category'), 'fp_wet', _near(dim, main)

    if cat in _d.FLEX_CATS:
        return 0, 'class', 'flex', False

    if cat in _d.DUCT_CATS:
        big = float(cfg.get('big_duct_in') or 24.0)
        mid = float(cfg.get('mid_duct_in') or 12.0)
        if dim is None:
            return 3, 'category', 'duct', False
        if dim >= big:
            return 4, 'size', 'duct', _near(dim, big)
        if dim >= mid:
            return 3, 'size', 'duct', _near(dim, big) or _near(dim, mid)
        return 2, 'size', 'duct', _near(dim, mid)

    if cat in _d.TRAY_CATS:
        return 3, 'class', 'tray', False

    if cat in _d.CONDUIT_CATS:
        small = float(cfg.get('small_conduit_in') or 1.0)
        if dim is None:
            return 1, 'category', 'conduit', False
        if dim > small:
            return 1, 'size', 'conduit', _near(dim, small)
        return 0, 'size', 'conduit', _near(dim, small)

    if cat in _d.PIPE_CATS:
        big = float(cfg.get('big_pipe_in') or 4.0)
        if dim is None:
            return 2, 'category', 'pressure_pipe', False
        if dim >= big:
            return 3, 'size', 'pressure_pipe', _near(dim, big)
        return 2, 'size', 'pressure_pipe', _near(dim, big)

    if cat in _d.EQUIPMENT_CATS:
        return 4, 'class', 'equipment', False
    if cat in _d.TECH_CATS:
        return 1, 'class', 'tech', False

    return 2, 'category', 'unknown', False


def _participant(ref, cfg):
    ref = ref or {}
    fixed = _fixed_kind(ref)
    p = {
        'fixed': fixed is not None,
        'fixed_kind': fixed,
        'rigidity': None,
        'rig_src': None,
        'klass': None,
        'near': False,
        'dim_in': _max_dim_in(ref),
        'ins_in': ref.get('ins_in'),
        'sys': ref.get('sys_name') or ref.get('sys_class'),
        'cat': ref.get('category'),
        'desc': ref.get('name') or ref.get('category') or 'element',
        # Phase 3 arch/structure facts (nullable).
        'is_rated': ref.get('is_rated'),
        'thickness_in': ref.get('thickness_in'),
        'is_structural': ref.get('is_structural'),
        'wall_function': ref.get('wall_function'),
        'fire_rating_hr': ref.get('fire_rating_hr'),
        'top_ft': ref.get('top_ft'),
        'bot_ft': ref.get('bot_ft'),
        'ref': ref,
    }
    if fixed is None:
        rig, src, klass, near = _rigidity(ref, cfg)
        p['rigidity'] = rig
        p['rig_src'] = src
        p['klass'] = klass
        p['near'] = near
    return p


# ---------------------------------------------------------------------------
# Layer A
# ---------------------------------------------------------------------------

def _layer_a(c, pa, pb, cluster_n, cfg):
    """Return (rule_id, reason) or (None, None). First match wins.
    Every rule fires only on proof (non-null features)."""
    rules = cfg.get('rules') or {}
    ra = c.get('ref_a') or {}
    rb = c.get('ref_b') or {}

    if rules.get('R-SELF'):
        same_src = (ra.get('source') or 'host') == (rb.get('source') or 'host')
        uid_a, uid_b = ra.get('unique_id'), rb.get('unique_id')
        eid_a, eid_b = ra.get('element_id'), rb.get('element_id')
        if same_src and ((uid_a is not None and uid_a == uid_b) or
                         (eid_a is not None and eid_a == eid_b)):
            return 'R-SELF', 'Element intersecting itself: modeling artifact.'

    if rules.get('R-NOT-OURS'):
        sa = ra.get('source') or 'host'
        sb = rb.get('source') or 'host'
        if sa.startswith('link:') and sb.startswith('link:'):
            return ('R-NOT-OURS',
                    'Both elements belong to linked consultants; dbHMS can '
                    'move neither. Flag to the design team outside the grid.')

    if rules.get('R-SYS'):
        if (c.get('kind') == 'hard'
                and (ra.get('source') or 'host') == 'host'
                and (rb.get('source') or 'host') == 'host'):
            sys_a, sys_b = ra.get('sys_name'), rb.get('sys_name')
            if sys_a and sys_b and sys_a == sys_b and (
                    ra.get('category') in _d.FITTING_CATS or
                    rb.get('category') in _d.FITTING_CATS):
                return ('R-SYS',
                        'Same {0} run touching itself at a fitting seam.'.format(sys_a))

    if rules.get('R-FIELD'):
        # Deliberately NARROW: lone small conduit vs conduit only. Conduit
        # against anything else stays visible and scores low instead
        # (suppression that backfires once discredits the whole layer).
        dia_a, dia_b = _max_dim_in(ra), _max_dim_in(rb)
        floor = float(cfg.get('field_fix_dia_in') or 1.0)
        escape = int(cfg.get('field_fix_cluster_escape') or 10)
        if (ra.get('category') in _d.CONDUIT_CATS
                and rb.get('category') in _d.CONDUIT_CATS
                and dia_a is not None and dia_a <= floor
                and dia_b is not None and dia_b <= floor
                and cluster_n < escape):
            return ('R-FIELD',
                    'Single small-bore conduits (<= {0} in): field-routed by '
                    'the installing trade (NBIMS-US V3 5.5.4.8 + Annex A '
                    '1.5.1).'.format(_fmt_num(floor)))

    if rules.get('R-GRAZE'):
        pen = c.get('penetration_depth_in')
        vol = c.get('overlap_volume_cf')
        # Suppress only on a MEASURED boolean overlap (never a bbox proxy) that
        # is BOTH shallow and small: a wide shallow face-contact keeps its
        # volume above the cap and stays visible (v2 plan 6.4, feasibility fix).
        if (c.get('kind') == 'hard' and c.get('geom_method') == 'boolean'
                and pen is not None
                and float(pen) < float(cfg.get('graze_floor_in') or 0.375)
                and vol is not None
                and float(vol) < float(cfg.get('graze_max_vol_cf') or 0.02)):
            mover = _mover(pa, pb)
            structural = ('structural' in (pa.get('fixed_kind'), pb.get('fixed_kind')))
            if (mover is not None and mover.get('rigidity') is not None
                    and mover['rigidity'] <= 3 and not structural):
                return ('R-GRAZE',
                        'Sub-3/8 in graze: modeling tolerance noise.')

    return None, None


# ---------------------------------------------------------------------------
# Layer B: mover, tiers, sub-score
# ---------------------------------------------------------------------------

def _mover(pa, pb):
    """The participant that must move. Fixed elements never move; between
    two movable MEP elements, the lower-rigidity (cheaper) one moves."""
    if pa['fixed'] and pb['fixed']:
        return None
    if pa['fixed']:
        return pb
    if pb['fixed']:
        return pa
    ra = pa['rigidity'] if pa['rigidity'] is not None else 2
    rb = pb['rigidity'] if pb['rigidity'] is not None else 2
    return pa if ra <= rb else pb


def _other(pa, pb, mover):
    return pb if mover is pa else pa


def _tier(c, pa, pb, mover, cluster_n, cfg, prev_rule):
    """Ordered tier rules, first match wins.
    Returns (band, rule_id, reason, flags)."""
    kind = c.get('kind') or 'hard'
    flags = []

    # --- Phase 4: clearance (code-zone) rows -----------------------------
    # A clearance clash is an intruder element sitting in a code-mandated
    # zone around a piece of equipment. It is dispatched purely by its
    # clearance_rule (= the clearance test id) ABOVE every hard/soft rule
    # AND above the mover-None guard, so an architectural element intruding
    # a working space (both sides fixed -> no mover) still routes here, and
    # so C2's structure escalation / C4 deep-pen can never steal a zone
    # violation. Runner convention: ref_a is the intruder, ref_b the
    # equipment owner, so pa is the intruder.
    if kind == 'clearance':
        crule = c.get('clearance_rule')
        if crule == 'C-NEC':
            return ('Critical', 'C-NEC', '', flags)
        if crule == 'C-NEC-W':
            # An architectural, STRUCTURAL, or unmapped intruder obstructing the
            # working space is not an MEP action -- flag it to the design team,
            # never rank it Critical against the MEP model (MEP cannot move a
            # wall or a beam). Keep the rule id so the composer still explains
            # WHAT it is. A movable MEP intruder stays Critical.
            if pa['fixed_kind'] in ('architectural', 'unknown', 'structural'):
                flags.append('flag_design_team')
                return ('Minor', 'C-NEC-W', '', flags)
            return ('Critical', 'C-NEC-W', '', flags)
        if crule == 'M-NEC-PROT':
            return ('Major', 'M-NEC-PROT', '', flags)
        if crule == 'M-SPR':
            return ('Major', 'M-SPR', '', flags)
        # Residual: a clearance row with an unrecognized rule id.
        return ('Major', 'M-CODE', '', flags)

    if mover is None:
        return ('Minor', 'FB',
                'No movable dbHMS element in this pair; flag to the design '
                'team.', flags)

    other = _other(pa, pb, mover)
    rig = mover['rigidity'] if mover['rigidity'] is not None else 2
    vs_struct = other['fixed_kind'] == 'structural'
    # N1's arch demotion is strictly for the architect's model; an unmapped
    # link ('unknown') must never inherit "intended penetration" wording.
    vs_arch = other['fixed_kind'] == 'architectural'
    sys_label = mover['sys'] or mover['desc']

    # --- Critical ---------------------------------------------------------
    if kind == 'hard' and vs_struct and rig == 5:
        flags.append('escalate_candidate')
        if mover['klass'] == 'grease':
            reason = ('Grease exhaust duct vs structure: slope is code-fixed '
                      '(IMC 506.3.7) and the duct is welded liquid-tight. '
                      'May require a structural penetration request.')
        else:
            reason = ('Gravity {0} vs structure: slope is code-fixed '
                      '(IPC 704.1), no vertical reroute freedom. May require '
                      'a structural penetration request.'.format(sys_label))
        return 'Critical', 'C1', reason, flags

    # C2: the size gate reads the MOVER's dimension (not either participant).
    # klass guard: C2 is about DUCTWORK too big to reroute. Equipment never
    # carries dims_in today, but the day bounding-box capture ships, a
    # pad-mounted AHU on its foundation must stay N3 bearing, never flip
    # Critical (review finding; pinned by a fixture test).
    # (Clearance rows were historically escalated here too; they now dispatch
    # at the top of _tier, so this is hard-only.)
    if (vs_struct and rig == 4 and mover['klass'] != 'equipment'
            and kind == 'hard' and mover['dim_in'] is not None
            and mover['dim_in'] >= float(cfg.get('big_duct_in') or 24.0)):
        return ('Critical', 'C2',
                '{0} vs structure: a reroute this large consumes space that '
                'is not there; pre-pour / steel-fabrication deadline.'.format(
                    mover['desc']), flags)

    if (kind == 'hard' and not pa['fixed'] and not pb['fixed']
            # Stage 2: equipment PLACEMENT vs a gravity main is coordination
            # work, not two code-fixed routings - equipment never satisfies
            # C3 (falls through to M1 Major).
            and pa['klass'] != 'equipment' and pb['klass'] != 'equipment'):
        rig_a = pa['rigidity'] if pa['rigidity'] is not None else 2
        rig_b = pb['rigidity'] if pb['rigidity'] is not None else 2
        if min(rig_a, rig_b) >= 4 and max(rig_a, rig_b) == 5:
            return ('Critical', 'C3',
                    'Two no-slack systems collide: neither is a cheap mover.',
                    flags)

    pen = c.get('penetration_depth_in')
    if (pen is not None and float(pen) >= float(cfg.get('deep_pen_in') or 6.0)
            and vs_struct
            and not (mover['klass'] in ('equipment', 'fixture')
                     and other['fixed'])):
        # C4 is deep-INTO-STRUCTURE only (V3 finding). For two MEP elements the
        # measured "depth" (the thinnest extent of the intersection solid) is
        # really the SMALLER element's cross-section as it crosses the larger
        # one, so ANY 6 in+ pipe crossing a big duct would false-trigger a
        # Critical. Those are ordinary crossings (Major, M1), not systemic
        # overlap; and an arch wall/floor penetration is N1 / M-PEN, not C4.
        return ('Critical', 'C4',
                'Deep penetration ({0} in): systemic overlap, not a graze.'.format(
                    _fmt_num(pen)), flags)

    # --- Major before Minor, with two carve-outs learned from the NIUHTC
    # calibration: mounting adjacency (N3) and penetrations (N1) leave the
    # Major path BEFORE structure/congestion can promote them ---------------
    # (Clearance rows are handled at the top of _tier; they never reach here.)

    # N3: mounting/bearing adjacency (stage-1 retune). A troffer in its
    # ceiling, a lav on its carrier wall, equipment standing on a slab -
    # by-design contact, not routing. Demoted to a NAMED Minor bucket
    # (never suppressed: with no penetration depth yet, flush mounting and
    # fully-buried are indistinguishable, so rows stay visible+flagged).
    if (kind == 'hard' and mover['klass'] in ('equipment', 'fixture')
            and other['fixed']):
        ocat = other['cat']
        surface = (other['fixed_kind'] == 'architectural'
                   and ocat in _d.MOUNTING_SURFACE_CATS)
        bearing = ocat == 'Structural Foundations'
        recessed = (mover['klass'] == 'fixture'
                    and ocat in ('Structural Framing', 'Structural Columns'))
        if (surface or bearing or recessed) and not _is_switchgear(mover):
            flags.append('mounting_check')
            return ('Minor', 'N3',
                    '{0} mounted in/on {1}: by-design contact - verify '
                    'mounting/bearing, not a routing clash.'.format(
                        mover['desc'], other['desc']), flags)

    # M-STRUCT-ZONE (Phase 3): a routed run crossing a beam, classified by
    # which third of the beam depth it hits (middle web = often engineerable;
    # top/bottom flexural zone = escalate). Falls through to M2 when the beam
    # elevations aren't captured.
    if (kind == 'hard' and vs_struct
            and other['cat'] == 'Structural Framing'
            and mover['cat'] is not None and _is_routed(mover['cat'])):
        zone = _beam_zone(c, other, cfg)
        _bp = c.get('penetration_depth_in')
        # Only classify the beam zone for a REAL penetration INTO the beam. A
        # duct that merely meets the beam's underside (the common case -- MEP
        # routes below the steel, so the clash Z sits at the beam's bottom) has
        # no deep measured penetration and is a normal "route around structure"
        # (M2), NOT a flexural-zone escalation (V3 finding: all 60 zone rows
        # were bbox grazes reading "bottom third").
        if (zone is not None and _bp is not None
                and float(_bp) >= float(cfg.get('beam_pen_min_in') or 2.0)):
            flags.append('zone_' + zone)
            if zone in ('top', 'bottom'):
                flags.append('escalate_candidate')
            return ('Major', 'M-STRUCT-ZONE', '', flags)

    if kind == 'hard' and vs_struct and 2 <= rig <= 4:
        return ('Major', 'M2',
                '{0} must drop or rise around structure; the fix window '
                'closes at pour / steel fabrication.'.format(mover['desc']),
                flags)

    # N1 requires an actual intersection (hard) with a sleevable assembly:
    # a near miss to a wall is not a penetration, and a duct through a
    # stair or door is a routing error, not a sleeve candidate. Evaluated
    # BEFORE M4 (stage-1 retune): a wall crossing inside a congested zone
    # is still a sleeve-schedule item; the zone's congestion stays visible
    # through its MEP-vs-MEP members and the cluster group.
    # M-RATED (Phase 3): a DUCT through a rated wall/floor/roof needs a
    # fire/smoke damper -- IU Level One severity, not a Minor sleeve note.
    if (kind == 'hard' and vs_arch
            and other['cat'] in ('Walls', 'Floors', 'Roofs')
            and other.get('is_rated') is True
            and mover['cat'] is not None and mover['cat'] in _d.DUCT_CATS):
        return ('Major', 'M-RATED', '', flags)

    # M-PEN (Phase 3): a penetration TOO BIG for a standard sleeve needs a
    # framed/linteled opening -- design work, not an opening-schedule item.
    # Size is the only reliable discriminator here: penetration_depth_in is the
    # MIN extent of the overlap solid, so for a run SMALLER than the assembly
    # is thick it is just the run's own cross-section, NOT how far the run
    # stopped inside. The old 'partial penetration' trigger read that as
    # "stops part-way" and mislabeled ordinary through-penetrations by small
    # pipes/conduits as design work (V4: ~987 false Majors). Same min-extent
    # trap that restricted C4 to structure. So M-PEN now fires ONLY when the
    # run is oversized for a sleeve; everything else routed through a
    # significant assembly falls to the N1 sleeve/damper path below.
    if (kind == 'hard' and vs_arch
            and other['cat'] in _d.PENETRABLE_ARCH_CATS
            and mover['cat'] is not None and _is_routed(mover['cat'])
            and _significant_assembly(other, cfg)
            and _over_sleeve(mover, cfg)):
        flags.append('penetration_candidate')
        return ('Major', 'M-PEN', '', flags)

    if (kind == 'hard' and vs_arch
            and other['cat'] in _d.PENETRABLE_ARCH_CATS
            and mover['cat'] is not None and _is_routed(mover['cat'])):
        flags.append('penetration_candidate')
        # Rating unknown (V4 deep dive): fire ratings are captured on almost no
        # walls in practice, so M-RATED (duct through a rated assembly -> needs
        # a fire/smoke damper) silently under-fires. When a DUCT penetrates an
        # assembly whose rating we could NOT read (is_rated is None -- not a
        # positive "unrated"), flag it so a coordinator verifies the rating
        # rather than assuming a plain sleeve. Ducts only: dampers are the
        # air-side concern; pipe/conduit firestop is already in the wording.
        if (mover['cat'] in _d.DUCT_CATS
                and (other.get('ref') or {}).get('is_rated') is None):
            flags.append('rating_unknown')
        return ('Minor', 'N1',
                'Likely intended penetration through {0}: confirm sleeve / '
                'fire damper (IMC 607.4).'.format(other['desc']), flags)

    cluster_major = int(cfg.get('cluster_major') or 20)
    release = int(cfg.get('cluster_major_release') or 12)
    threshold = release if prev_rule == 'M4' else cluster_major
    if cluster_n >= threshold:
        return ('Major', 'M4',
                'Congested zone: {0} clashes within {1} ft. Rack-level '
                'rework, not a field fix.'.format(
                    cluster_n, _fmt_num(cfg.get('cluster_radius_ft') or 5.0)),
                flags)

    if _insulation_only(c, pa, pb):
        return ('Minor', 'N2',
                'Insulation-only pinch: metal clears, jacket is tight. '
                'Detailing, not routing.', flags)

    gap_ratio = _gap_ratio(c)
    if (kind == 'soft' and gap_ratio is not None
            and gap_ratio <= float(cfg.get('near_gap_ratio') or 0.25)
            and rig >= 2):
        # N4 (stage 2, split out of M3): tight-gap soft rows that are
        # by-design seating or imminent penetrations, not consumed
        # clearances. A floor drain sits at gap 0.0 to the slab it drains;
        # a riser 1/4 in from the arch floor is about to be a sleeve item.
        # M3 stays Major for structure, both-movable pairs, and unknowns.
        ocat = other['cat'] if other['fixed'] else None
        if (mover['klass'] == 'gravity'
                and mover['cat'] in ('Pipe Fittings', 'Pipe Accessories')
                and ocat in _d.DRAIN_SURFACE_CATS):
            flags.append('mounting_check')
            return ('Minor', 'N4',
                    '{0} seated at {1}: gap {2} by design - verify rim '
                    'elevation, not clearance.'.format(
                        mover['desc'], other['desc'],
                        _fmt_num(c.get('gap_inches') or 0)), flags)
        if (other['fixed_kind'] == 'architectural'
                and ocat in _d.MOUNTING_SURFACE_CATS):
            if mover['cat'] is not None and (
                    mover['cat'] in _d.DUCT_CATS or mover['cat'] in _d.PIPE_CATS or
                    mover['cat'] in _d.CONDUIT_CATS or mover['cat'] in _d.TRAY_CATS or
                    mover['cat'] in _d.FLEX_CATS):
                flags.append('penetration_candidate')
                return ('Minor', 'N4',
                        '{0} nearly touching {1}: imminent penetration - '
                        'sleeve-schedule item.'.format(
                            mover['desc'], other['desc']), flags)
            flags.append('mounting_check')
            return ('Minor', 'N4',
                    '{0} nearly touching {1}: verify mounting/seating, '
                    'not clearance.'.format(mover['desc'], other['desc']),
                    flags)
        return ('Major', 'M3',
                'Clearance nearly consumed: {0}% of the required gap '
                'remains.'.format(int(round(gap_ratio * 100))), flags)

    # N-DUP (hard): duplicate/placeholder family suspects. After M4 (a
    # congested equipment pair is still rack rework) and before the M1
    # dispatch (the artifact demotion beats M1-EQ-EQ) -- v2 plan 5.0 ladder.
    if kind == 'hard' and _is_dup_suspect(c, pa, pb):
        flags.append('family_artifact_suspect')
        return ('Minor', 'N-DUP', '', flags)

    if kind == 'hard':
        rig_a = pa['rigidity'] if not pa['fixed'] and pa['rigidity'] is not None else 0
        rig_b = pb['rigidity'] if not pb['fixed'] and pb['rigidity'] is not None else 0
        other_rig = other['rigidity'] if (not other['fixed']
                                          and other['rigidity'] is not None) else 0
        # The fixture clause (judge patch): an RCP-locked troffer against a
        # duct/pipe is real plenum coordination - the CURVE must move, so
        # the row stays Major instead of falling to the field-fix floor.
        if (max(rig_a, rig_b) >= 4 or rig == 3
                or (mover['klass'] == 'fixture' and other_rig >= 3)):
            return _m1_dispatch(mover, other, flags, cfg)

    if kind == 'hard' and mover['klass'] in ('fixture', 'tech'):
        flags.append('field_fix')
    return 'Minor', 'FB', '', flags


def _is_routed(cat):
    return (cat in _d.DUCT_CATS or cat in _d.PIPE_CATS
            or cat in _d.CONDUIT_CATS or cat in _d.TRAY_CATS
            or cat in _d.FLEX_CATS)


# ---------------------------------------------------------------------------
# Phase 3 helpers (penetration classification, beam zone). All None-safe.
# ---------------------------------------------------------------------------

def _pen_class(c, other, cfg):
    """'full' (through) / 'partial' (stops inside) / None, from the measured
    penetration depth and the assembly thickness. Needs both captured."""
    pen = c.get('penetration_depth_in')
    thick = other.get('thickness_in')
    if pen is None or thick is None:
        return None
    try:
        thick = float(thick)
        if thick <= 0:
            return None
        frac = float(cfg.get('pen_full_frac') or 0.85)
        return 'full' if float(pen) >= thick * frac else 'partial'
    except (TypeError, ValueError):
        return None


def _significant_assembly(other, cfg):
    """True for a penetrable assembly worth a design-grade penetration rule
    (M-PEN). Walls and anything structural always count; floors/roofs count
    only at or above min_assembly_in. Excludes the thin finish floors,
    membranes, and toppings that everything 'penetrates' as a modeling
    artifact (V3: a 36 in duct 'through' a 1/8 in finish floor is not a
    framed-opening event). Unknown thickness never suppresses (proof-only)."""
    if other.get('is_structural') is True or other.get('cat') == 'Walls':
        return True
    thick = other.get('thickness_in')
    if thick is None:
        return True
    try:
        return float(thick) >= float(cfg.get('min_assembly_in') or 1.5)
    except (TypeError, ValueError):
        return True


def _over_sleeve(mover, cfg):
    """True when a full penetration is too wide for a standard sleeve and needs
    a framed opening. dims_in is [dia] (round) or [w, h] (rect)."""
    dims = (mover.get('ref') or {}).get('dims_in')
    if not dims:
        return False
    try:
        vals = [float(v) for v in dims if v is not None]
    except (TypeError, ValueError):
        return False
    if not vals:
        return False
    # Rect vs round is decided by the ORIGINAL dims arity, not the filtered
    # list: a rect duct with one null axis ([w, None]) must not collapse to the
    # ROUND sleeve test. (Latent today -- enrich only emits [w, h] with both
    # axes -- but a trap if capture ever partial-fills.)
    if len(dims) == 1:
        return vals[0] > float(cfg.get('sleeve_round_max_in') or 16.0)
    return max(vals) > float(cfg.get('sleeve_rect_max_in') or 16.0)


def _clash_z(c):
    for key in ('overlap_centroid', 'midpoint'):
        v = c.get(key)
        if v and len(v) >= 3:
            try:
                return float(v[2])
            except (TypeError, ValueError):
                pass
    return None


def _beam_zone(c, other, cfg):
    """Which third of a beam's depth the clash crosses: 'top' / 'middle' /
    'bottom', or None when the beam elevations or the clash Z are unknown."""
    top, bot = other.get('top_ft'), other.get('bot_ft')
    z = _clash_z(c)
    if top is None or bot is None or z is None:
        return None
    try:
        depth = float(top) - float(bot)
        if depth <= 0:
            return None
        frac = (float(z) - float(bot)) / depth
    except (TypeError, ValueError):
        return None
    edge = float(cfg.get('beam_edge_frac') or 0.30)
    if frac <= edge:
        return 'bottom'
    if frac >= 1.0 - edge:
        return 'top'
    return 'middle'


def _m1_dispatch(mover, other, flags, cfg):
    """The v1 catch-all M1, split into 8 named Major sub-rules with the N-PT
    point-fixture demotion at the head (v2 plan 5.1). Mover-class first, which
    is how the NIUHTC populations were measured; first match wins. Reason
    text is '' here -- the composer owns wording, keyed by the rule id."""
    mk = mover['klass']
    ocat = other['cat']
    other_eq = (other['klass'] == 'equipment') or (ocat in _d.EQUIPMENT_CATS)

    # N-PT: a point-mounted fixture is a field relocate, not a reroute.
    if mk == 'fixture' and _is_point_fixture(mover, cfg):
        flags.append('field_fix')
        return ('Minor', 'N-PT', '', flags)

    if mk == 'equipment' and other_eq:
        return ('Major', 'M1-EQ-EQ', '', flags)
    if mk == 'fixture' and other_eq:
        return ('Major', 'M1-FIX-EQ', '', flags)
    if mk == 'fixture':
        return ('Major', 'M1-FIX-CURVE', '', flags)
    if mk in ('fp_wet', 'fp_dry'):
        return ('Major', 'M1-FP', '', flags)
    if mk in ('gravity', 'gravity_vent', 'condensate', 'medgas'):
        return ('Major', 'M1-SLOPE', '', flags)
    if mk == 'equipment' or other_eq:
        return ('Major', 'M1-EQ-SYS', '', flags)
    rig_m = mover['rigidity'] if mover['rigidity'] is not None else 2
    rig_o = other['rigidity'] if (not other['fixed']
                                  and other['rigidity'] is not None) else 0
    if min(rig_m, rig_o) >= 4:
        return ('Major', 'M1-RIGID', '', flags)
    return ('Major', 'M1-XING', '', flags)


def _is_point_fixture(mover, cfg):
    """A Lighting/Electrical Fixture whose NAME reads as a point device (exit
    sign, wall receptacle/switch): relocated with a box shift, not a routing
    change. Phase 2 replaces this name heuristic with a captured `mount`
    fact; scope is Decision D1 (defaults.n_pt_*)."""
    cat = mover.get('cat')
    if cat not in ('Lighting Fixtures', 'Electrical Fixtures'):
        return False
    name = mover.get('desc') or ''
    up = name.upper()
    for w in _d.POINT_FIXTURE_NAME_WORDS:
        if w in up:
            return True
    for rx in _POINT_FIXTURE_RES:
        if rx.search(name):
            return True
    if cfg.get('n_pt_include_wall_devices') and cat == 'Electrical Fixtures':
        toks = set(t for t in re.split(r'[^A-Z0-9]+', up) if t)
        if toks & _d.WALL_DEVICE_NAME_WORDS:
            return True
    if cfg.get('n_pt_include_typical') and cat == 'Electrical Fixtures':
        if up.strip() == 'TYPICAL':
            return True
    return False


def _is_dup_suspect(c, pa, pb):
    """Two equipment/mounted instances that share a family name, or carry a
    placeholder type name, in the SAME source model: usually one nested /
    double-placed / placeholder family, not two real units. A demotion (a
    name is not proof); R-SELF already suppresses literal self-intersections."""
    ra = c.get('ref_a') or {}
    rb = c.get('ref_b') or {}
    if (ra.get('source') or 'host') != (rb.get('source') or 'host'):
        return False
    ca, cb = pa['cat'], pb['cat']
    if ca is None or ca != cb:
        return False
    if not (ca in _d.EQUIPMENT_CATS or ca in _d.MOUNTED_CATS):
        return False
    na = ra.get('name') or ''
    nb = rb.get('name') or ''
    if na and nb and na == nb:
        return True
    ph = _d.PLACEHOLDER_TYPE_NAMES
    if na.strip().upper() in ph or nb.strip().upper() in ph:
        return True
    return False


def _is_switchgear(mover):
    """Electrical distribution gear intersecting a wall is an
    electrical-room layout check, not mounting adjacency (judge patch).

    Token-based: 'CT-1', 'CT.1', 'CT1', 'TX 2' must gate, while 'ATX-1'
    or a manufacturer name containing 'mcc' must not (review finding:
    raw substring matching failed both directions)."""
    if mover.get('cat') != 'Electrical Equipment':
        return False
    import re
    name = (mover.get('desc') or '').lower()
    for w in ('switchboard', 'switchgear', 'panelboard', 'transformer'):
        if w in name:
            return True
    for tok in re.split(r'[^a-z0-9]+', name):
        if not tok:
            continue
        if tok in ('msb', 'mcc', 'ct', 'tx'):
            return True
        m = re.match(r'^(msb|mcc|ct|tx)\d+$', tok)
        if m:
            return True
    return False


def _fb_reason(kind, mover):
    if kind == 'soft':
        return ('Near miss with clearance to spare; {0} is the cheap '
                'mover.'.format(mover['desc']))
    return ('Minor conflict; {0} reroutes cheaply.'.format(mover['desc']))


def _insulation_only(c, pa, pb):
    if (pa['cat'] in _d.INSULATION_CATS) or (pb['cat'] in _d.INSULATION_CATS):
        return True
    if c.get('kind') == 'soft':
        gap = c.get('gap_inches')
        try:
            ins = float(pa['ins_in'] or 0.0) + float(pb['ins_in'] or 0.0)
            if gap is not None and ins > 0.0 and float(gap) < ins:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _gap_ratio(c):
    gap = c.get('gap_inches')
    tol = c.get('tolerance_inches')
    if gap is None or not tol:
        return None
    try:
        tol = float(tol)
        if tol <= 0:
            return None
        r = float(gap) / tol
        return max(0.0, min(1.0, r))
    except (TypeError, ValueError):
        return None


def _bulk_in(pa, pb):
    dims = [d for d in (pa['dim_in'], pb['dim_in']) if d is not None]
    if not dims:
        return None
    return max(dims)


# ---------------------------------------------------------------------------
# Sub-score (ordering WITHIN the band; clamped so it can never cross a
# band boundary -- the property the whole design hangs on)
# ---------------------------------------------------------------------------

def _min_extent(box):
    """Smallest of a [dx, dy, dz] overlap bounding box, or None."""
    if not box:
        return None
    try:
        vals = [float(v) for v in box if v is not None]
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    return min(vals)


def _sub_score(c, pa, pb, mover, other, cluster_n, cfg, code_ref):
    """(bars, raw) for ordering WITHIN the band. The raw maps proportionally
    across the band width in _score_one and can never cross a band boundary
    (the property the whole design hangs on). v2 plan 5.4. All divisions use
    float literals (Py2 integer-division trap); rounding via _round_half_away.

    On rev-4 rescore of records with no captured geometry, pen/overlap/volume
    are null: geometry falls back to a flat 4 and volume to 0 -- the terms
    that spread the score are constraint, size, congestion, code. Real
    geometry (Phase 2) fills the low end of each band."""
    kind = c.get('kind') or 'hard'

    # constraint: pair stiffness. A fixed wall/structure side weighs a full 5,
    # so pipe-vs-wall outranks pipe-vs-mid-duct.
    if mover is None:
        constraint = 4
    else:
        rig_m = mover['rigidity'] if mover['rigidity'] is not None else 2
        if other is not None and other.get('fixed'):
            rig_o = 5
        else:
            rig_o = (other or {}).get('rigidity')
            rig_o = rig_o if rig_o is not None else 0
        constraint = 2 * rig_m + rig_o

    bulk = _bulk_in(pa, pb)
    full_in = float(cfg.get('size_full_in') or 30.0)
    size_f = 2.0 if bulk is None else min(8.0, 8.0 * float(bulk) / full_in)

    # Geometry / volume are None when uncaptured (every hard row on a rev-4
    # rescore) -- an honest "not measured", NOT a fake constant. They then
    # contribute 0 to raw and render "(not captured)" in the UI, so the score
    # is spread by the terms that ARE known (constraint, size, congestion,
    # code). Real geometry (Phase 2) fills each band's low end.
    pen = c.get('penetration_depth_in')
    if kind == 'hard':
        if pen is not None:
            geometry = min(8, _round_half_away(2.0 * float(pen)))
        else:
            mn = _min_extent(c.get('overlap_bbox_in'))
            geometry = min(6, _round_half_away(mn)) if mn is not None else None
    else:
        if c.get('is_contact'):
            geometry = 6
        else:
            ratio = _gap_ratio(c)
            if ratio is None:
                geometry = 3
            else:
                geometry = max(0, min(6, _round_half_away(6.0 * (1.0 - ratio))))

    vol = c.get('overlap_volume_cf')
    if vol is None:
        volume = None
    else:
        vfull = float(cfg.get('vol_full_cf') or 1.0)
        try:
            volume = min(6, _round_half_away(6.0 * (float(vol) / vfull) ** 0.5))
        except (TypeError, ValueError):
            volume = None

    cfull = float(cfg.get('congest_full_n') or 20.0)
    congestion_f = min(6.0, cluster_n * 6.0 / cfull)

    code = 6 if (code_ref or kind == 'clearance') else 0

    # RAW uses the FLOAT size/congestion terms so the score has real
    # granularity (23 distinct integer raws collapse the score to ~40 values;
    # the underlying dimensions are near-continuous). The BARS show the
    # rounded integer for the UI. Uncaptured geometry/volume contribute 0 and
    # render "(not captured)".
    raw = (constraint + size_f + (geometry or 0) + (volume or 0)
           + congestion_f + code)
    bars = [
        {'k': 'Mover constraint', 'v': constraint},
        {'k': 'Size', 'v': int(_round_half_away(size_f))},
        {'k': 'Geometry', 'v': geometry},
        {'k': 'Volume', 'v': volume},
        {'k': 'Congestion', 'v': int(_round_half_away(congestion_f))},
        {'k': 'Code', 'v': code},
    ]
    # Drop uncaptured components (None) so the bars show only measured score
    # terms; the uncaptured geometry/volume surface in the facts table as
    # "(not captured)". Keeps both the current and the v2 UI clean.
    bars = [b for b in bars if b['v'] is not None]
    return bars, raw


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Composer: the 2-3 sentence "why it ranks" reason, headline, code_ref,
# resolve_by, and the facts table -- all keyed off the tier rule id. The
# reason is COMPOSED (WHAT / WHY / ACT + optional qualifier), never the v1
# suffix concatenation. v2 plan 5.5.
# ---------------------------------------------------------------------------

_CAT_NOUN = {
    'Ducts': 'duct', 'Duct Fittings': 'duct fitting',
    'Duct Accessories': 'duct accessory', 'Flex Ducts': 'flex duct',
    'Pipes': 'pipe', 'Pipe Fittings': 'pipe fitting',
    'Pipe Accessories': 'pipe accessory', 'Flex Pipes': 'flex pipe',
    'Conduits': 'conduit', 'Conduit Fittings': 'conduit fitting',
    'Cable Trays': 'cable tray', 'Cable Tray Fittings': 'cable tray fitting',
    'Sprinklers': 'sprinkler', 'Air Terminals': 'air terminal',
    'Mechanical Equipment': 'mechanical unit',
    'Electrical Equipment': 'electrical unit',
    'Plumbing Fixtures': 'plumbing fixture',
    'Lighting Fixtures': 'light fixture',
    'Electrical Fixtures': 'electrical device',
    'Walls': 'wall', 'Floors': 'floor', 'Ceilings': 'ceiling',
    'Roofs': 'roof', 'Structural Framing': 'beam',
    'Structural Columns': 'column', 'Structural Foundations': 'foundation',
}


def _truncate(s, n):
    s = s or ''
    if len(s) <= n:
        return s
    return s[:max(0, n - 3)].rstrip() + '...'


def _sysword(p):
    if not p:
        return None
    s = p.get('sys')
    if not s:
        return None
    s = s.split(',')[0].strip()
    return s or None


def _clean_ident(x, cat):
    """A real identity string for a parenthetical, or None. Rejects blanks,
    'none', the bare category, and placeholder type names ('TYPICAL')."""
    if not x:
        return None
    xs = str(x).strip()
    if (not xs or xs.lower() == 'none' or xs == cat
            or xs.upper() in _d.PLACEHOLDER_TYPE_NAMES):
        return None
    return xs


def _desc_phrase(p):
    """Short human phrase for one participant: '24 in supply duct',
    'mechanical unit (528 GALLONS)', 'duct'. Degrades gracefully on missing
    data and is length-capped so headlines fit."""
    if not p:
        return 'element'
    cat = p.get('cat')
    noun = _CAT_NOUN.get(cat) or (cat or 'element')
    if cat in _d.EQUIPMENT_CATS or cat in _d.MOUNTED_CATS:
        # System word ONLY when it is single-valued and real: a multi-connector
        # equipment's sys like 'Undefined,Hydronic Return' otherwise printed
        # 'Undefined mechanical unit' / a wrong single system (V4 deep dive).
        sys = p.get('sys')
        sysw = None
        if sys and ',' not in sys:
            t = sys.strip()
            if t and t.lower() != 'undefined':
                sysw = t
        base = (sysw + ' ' + noun) if sysw else noun
        # Parenthetical identity: the type name, or the FAMILY name when the
        # type is a placeholder ('TYPICAL') -- the family carries the real
        # identity (CONDENSATE PUMP, FIRE PUMP, EXPANSION TANK) (V4 deep dive).
        label = _clean_ident(p.get('desc'), cat) or _clean_ident(
            (p.get('ref') or {}).get('family'), cat)
        if label:
            base = base + ' (' + _truncate(label, 22) + ')'
        return _truncate(base, 46)
    parts = []
    dim = p.get('dim_in')
    if dim is not None:
        parts.append(_fmt_num(dim) + ' in')
    sysw = _sysword(p)
    if sysw:
        parts.append(sysw)
    parts.append(noun)
    return _truncate(' '.join(parts), 46)


def _level_of(c):
    for r in (c.get('ref_a'), c.get('ref_b')):
        if r and r.get('level'):
            return r.get('level')
    return None


def _level_for(c, ctx):
    """Best level to CITE for this clash. Refs carry only a level NAME (no
    elevation), and the engine has no level table to resolve the true clash
    (midpoint Z) against -- so when the two sides disagree on floor we cannot
    compute the nearest. But the FIXED assembly being penetrated (a wall,
    floor, beam) sits AT the clash, while a mover (a riser, a long main) is
    tagged to its own host level and can be storeys away. So: sides agree ->
    cite it; disagree WITH a fixed side -> cite the fixed side (it is at the
    clash); disagree with NO fixed side (two movers) -> cite nothing rather
    than assert a wrong floor. (V4 deep dive: ref_a-first cited the wrong floor
    on 63 cross-level pairs, some off by 2-3 storeys.)"""
    a = c.get('ref_a') or {}
    b = c.get('ref_b') or {}
    la, lb = a.get('level'), b.get('level')
    if la and lb and la != lb:
        other = (ctx or {}).get('other')
        if other is not None and other.get('fixed'):
            return (other.get('ref') or {}).get('level') or la or lb
        return None
    return la or lb


def _loc_phrase(c, ctx=None):
    lvl = _level_for(c, ctx)
    if lvl:
        return ' on ' + _truncate(str(lvl), 24)
    return ''


def _confidence(c, pa, pb, mover):
    if mover is None:
        return 'degraded', 'no_mover'
    if mover['rig_src'] == 'category':
        return 'degraded', 'category'
    if c.get('kind') == 'soft' and c.get('gap_inches') is None:
        return 'degraded', 'soft_no_gap'
    if pa['fixed_kind'] == 'unknown' or pb['fixed_kind'] == 'unknown':
        return 'degraded', 'unknown_link'
    return 'full', None


def _relevance_class(rule, flags):
    """The research taxonomy (error / deliberate / pseudo), plus two dbHMS
    buckets (artifact, field), derived from the rule id + flags. Feeds the
    noise benchmark in the calibration report."""
    if rule in ('R-SELF', 'R-NEST', 'N-DUP'):
        return 'artifact'
    if rule == 'N1' or (rule == 'N4' and 'penetration_candidate' in flags):
        return 'deliberate'
    if rule == 'N2':
        return 'pseudo'
    if rule in ('R-FIELD', 'N-PT') or 'field_fix' in flags:
        return 'field'
    return 'error'


def _qualifier(ctx):
    mover = ctx['mover']
    if ctx.get('confidence') == 'degraded':
        cause = ctx.get('degrade_cause')
        if cause == 'unknown_link':
            return ('One side is an unmapped linked model; map its role in '
                    'Settings to rank this properly.')
        if cause == 'no_mover':
            return None
        return 'Sized from category alone here, so verify before acting on the order.'
    if mover is not None and mover.get('near'):
        return 'Note: a dimension sits within 15% of a rule boundary; verify it.'
    return None


def _compose(rule, band, ctx):
    """Return (headline, reason, code_ref, resolve_by). Headline is the WHAT
    sentence, length-capped for the meeting agenda."""
    c = ctx['c']
    mover = ctx['mover']
    other = ctx['other']
    loc = _loc_phrase(c, ctx)
    md = _desc_phrase(mover) if mover is not None else 'this element'
    od = _desc_phrase(other) if other is not None else 'the other element'
    onoun = _CAT_NOUN.get((other or {}).get('cat')) or 'run' if other else 'run'
    # Clearance rows: name the intruder (ref_a -> pa) and the equipment owner
    # (ref_b -> pb) by the runner's fixed convention, not the mover heuristic.
    intr = _desc_phrase(ctx['pa']) if ctx.get('pa') else 'a foreign run'
    own = _desc_phrase(ctx['pb']) if ctx.get('pb') else 'the equipment'
    code_ref = None
    resolve_by = 'next_cycle'
    what = why = act = ''

    if rule == 'C1':
        what = '{0} runs through structure{1}.'.format(md, loc)
        if mover is not None and mover['klass'] == 'grease':
            why = ('A grease exhaust duct holds a code-fixed slope to the hood '
                   '(IMC 506.3.7) and is welded liquid-tight, so it cannot bend '
                   'around the member.')
            code_ref = 'IMC 506.3.7'
        else:
            why = ('A gravity main holds a code-fixed slope (IPC 704.1) with no '
                   'vertical reroute freedom around the member.')
            code_ref = 'IPC 704.1'
        act = ('Submit a structural penetration request or agree the reroute '
               'before steel fabrication; this closes design options fastest '
               'of anything on the list.')
        resolve_by = 'steel_fab'
    elif rule == 'C2':
        what = '{0} conflicts with structure{1}.'.format(md, loc)
        why = ('A reroute at this size consumes structural depth that is not '
               'there; a change after the pour or steel-fabrication cutoff '
               'means re-sleeved concrete or refabricated steel.')
        act = ('Resolve it in section with the structural engineer before that '
               'cutoff.')
        resolve_by = 'steel_fab'
    elif rule == 'C3':
        what = '{0} and {1} collide{2}; both are size- or slope-locked.'.format(md, od, loc)
        why = 'Neither system is a cheap mover, so there is no obvious side to give.'
        act = ('Needs a section cut and an agreed elevation stack at the next '
               'coordination meeting, not a modeler nudge.')
    elif rule == 'C4':
        what = '{0} penetrates {1} in into {2}{3}.'.format(
            md, _fmt_num(c.get('penetration_depth_in')), od, loc)
        why = ('Overlap this deep means the two routes were designed through the '
               'same space; a nudge will not clear it.')
        act = 'Re-plan the shared corridor before either run is fabricated.'
    elif rule == 'C-NEC':
        what = '{0} sits in the NEC dedicated electrical space above {1}{2}.'.format(
            intr, own, loc)
        why = ('The space directly above a panel or switchboard must stay clear '
               'of foreign ducts and piping up to 6 ft (or the structural '
               'ceiling); anything in it is a code violation, not a graze.')
        act = 'Reroute the run out of the dedicated space before the gear is set.'
        code_ref = _d.CLEARANCE_CODE_BY_RULE.get('C-NEC')
        resolve_by = 'gear_setting'
    elif rule == 'C-NEC-W':
        if 'flag_design_team' in (ctx.get('flags') or []):
            what = '{0} obstructs the NEC working space in front of {1}{2}.'.format(
                intr, own, loc)
            why = ('The working space in front of live electrical gear must stay '
                   'clear for safe operation, but the obstruction here is an '
                   'architectural element the MEP team cannot move.')
            act = 'Flag it to the design team to clear the working space.'
        else:
            what = '{0} intrudes into the NEC working space in front of {1}{2}.'.format(
                intr, own, loc)
            why = ('The working-space depth in front of live gear must stay clear '
                   'for safe operation and code-required maintenance access.')
            act = ('Clear the working space before the gear is set and the feeder '
                   'routing freezes.')
        code_ref = _d.CLEARANCE_CODE_BY_RULE.get('C-NEC-W')
        resolve_by = 'gear_setting'
    elif rule == 'M-NEC-PROT':
        what = '{0} runs above the dedicated space of {1}{2}.'.format(intr, own, loc)
        why = ('Leak-capable piping above the dedicated space is allowed but needs '
               'a protective drip or deflection provision so a leak cannot fall '
               'onto the gear.')
        act = 'Add the protection detail or reroute this cycle.'
        code_ref = _d.CLEARANCE_CODE_BY_RULE.get('M-NEC-PROT')
        resolve_by = 'gear_setting'
    elif rule == 'M-SPR':
        clr = c.get('spr_clearance_in')
        clrtxt = ' ({0} in remain)'.format(_fmt_num(clr)) if clr is not None else ''
        what = '{0} sits within the required clearance of a sprinkler head{1}.'.format(
            intr, loc)
        why = ('NFPA 13 requires clearance from the sprinkler deflector to '
               'obstructions{0}; this obstruction is inside that radius and would '
               'block the spray pattern.'.format(clrtxt))
        act = ('Relocate the obstruction or the head before the sprinkler shop '
               'drawings release.')
        code_ref = _d.CLEARANCE_CODE_BY_RULE.get('M-SPR')
        resolve_by = 'next_cycle'
    elif rule == 'M-CODE':
        what = '{0} intrudes into the required clearance of {1}{2}.'.format(md, od, loc)
        why = ('A consumed code clearance is an inspection failure even though '
               'nothing physically touches.')
        act = 'Recover the clearance before this area is signed off.'
    elif rule == 'N3':
        what = '{0} sits in or on {1}{2}.'.format(md, od, loc)
        why = 'This reads as by-design mounting or bearing contact, not a routing clash.'
        act = 'Verify the mounting or bearing detail; no reroute is needed.'
        resolve_by = 'field'
    elif rule == 'M2':
        what = '{0} must route around structure{1}.'.format(md, loc)
        why = ('It has to drop or rise to clear the member, and that fix window '
               'closes at the pour or steel fabrication.')
        act = 'Coordinate the offset with the structural engineer before that cutoff.'
        resolve_by = 'steel_fab'
    elif rule == 'M-RATED':
        hr = (other or {}).get('fire_rating_hr')
        rating = '{0}-hr '.format(_fmt_num(hr)) if hr else ''
        onoun2 = _CAT_NOUN.get((other or {}).get('cat')) or 'assembly'
        what = '{0} penetrates a {1}rated {2}{3}.'.format(md, rating, onoun2, loc)
        why = ('A rated assembly requires a fire (or combination fire/smoke) '
               'damper and an access door at the duct penetration, and the '
               'damper changes both the opening size and the duct fabrication.')
        act = ('Lock the damper and the opening with the architect before '
               'ductwork fabrication release.')
        code_ref = 'IMC 607.5.1'
        resolve_by = 'duct_fab'
    elif rule == 'M-PEN':
        # M-PEN now fires only on oversized penetrations (too big for a
        # standard sleeve), so the wording is always the framed-opening case.
        what = ('{0} passes through {1} but is too large for a standard '
                'sleeve{2}.'.format(md, od, loc))
        why = ('An opening this size needs a framed or linteled opening '
               'coordinated with the design team, not a field sleeve.')
        act = ('Resolve the routing or the opening this cycle, before the '
               'opening package for this level issues.')
        resolve_by = 'sleeve_pkg'
    elif rule == 'M-STRUCT-ZONE':
        fl = ctx.get('flags') or []
        zone = ('top' if 'zone_top' in fl
                else ('bottom' if 'zone_bottom' in fl else 'middle'))
        what = '{0} crosses a beam in its {1} third{2}.'.format(md, zone, loc)
        if zone == 'middle':
            why = ('A penetration through the middle third of a beam web is '
                   'often an engineerable opening, but only the structural '
                   'engineer can approve it.')
            act = 'Send the opening to structural for sign-off before steel fabrication.'
        else:
            why = ('The {0} third is a flexural zone; a penetration here is '
                   'usually not permissible without a redesign.'.format(zone))
            act = 'Reroute the run or raise an RFI to structural before steel fabrication.'
        resolve_by = 'steel_fab'
    elif rule == 'N1':
        rated = bool(other and other.get('is_rated') is True)
        thick = other.get('thickness_in') if other else None
        thick_ph = ' the {0} in'.format(_fmt_num(thick)) if thick else ''
        what = '{0} passes through{1} {2}{3}.'.format(md, thick_ph, od, loc)
        if rated and mover is not None and mover.get('cat') not in _d.DUCT_CATS:
            why = ('It crosses a rated assembly, so it needs a listed '
                   'through-penetration firestop system at the opening, not '
                   'just a sleeve.')
            act = 'Schedule the firestop detail with the opening package for this level.'
            code_ref = 'IBC 714.4.1'
        else:
            why = ('This reads as an intended penetration of an architectural '
                   'assembly, not a routing error.')
            act = 'Confirm a sleeve is on the penetration schedule for this level.'
        resolve_by = 'sleeve_pkg'
    elif rule == 'M4':
        n = ctx['cluster_n']
        r = _fmt_num(ctx['radius'])
        what = 'This clash sits in a congested zone: {0} clashes within {1} ft{2}.'.format(n, r, loc)
        why = ('Density like this means rack-level rework, not {0} separate field '
               'fixes; it is the multi-trade decision the ASCE Medium band '
               'describes.'.format(n))
        act = 'Resolve the zone as one section study at the next coordination meeting.'
    elif rule == 'N2':
        what = '{0} and {1} pinch only at their insulation{2}.'.format(md, od, loc)
        why = 'The metal clears; the jacket is tight. That is detailing, not routing.'
        act = 'Adjust the insulation detail; no reroute is needed.'
        resolve_by = 'field'
    elif rule == 'N4':
        flags = ctx.get('flags') or []
        if 'penetration_candidate' in flags:
            what = '{0} nearly touches {1}{2}.'.format(md, od, loc)
            why = ('This is an imminent penetration of an architectural surface, '
                   'not a consumed clearance.')
            act = 'Add it to the sleeve schedule.'
            resolve_by = 'sleeve_pkg'
        elif mover is not None and mover['klass'] == 'gravity':
            what = '{0} seats at {1}{2}.'.format(md, od, loc)
            why = ('A gravity fitting sits at near-zero gap to the surface it '
                   'drains, by design.')
            act = 'Verify the rim elevation, not the clearance.'
            resolve_by = 'field'
        else:
            what = '{0} nearly touches {1}{2}.'.format(md, od, loc)
            why = 'This reads as by-design seating against the surface, not a consumed clearance.'
            act = 'Verify the mounting; no reroute is needed.'
            resolve_by = 'field'
    elif rule == 'M3':
        pct = int(_round_half_away((ctx.get('gap_ratio') or 0.0) * 100.0))
        what = '{0} intrudes into the clearance around {1}{2}.'.format(md, od, loc)
        why = ('Only {0}% of the required gap remains; a clearance this tight '
               'predicts a field or inspection problem.'.format(pct))
        act = 'Recover the gap at the next coordination meeting.'
    elif rule == 'N-DUP':
        what = '{0} and {1} overlap and share a name or placeholder type{2}.'.format(md, od, loc)
        why = ('This pattern is usually a nested, double-placed, or placeholder '
               'family rather than two real units.')
        act = ('Verify it once in the model this cycle; if they are two genuine '
               'units, comment here and it becomes a placement conflict.')
    elif rule == 'N-PT':
        what = '{0} is a point device hitting {1}{2}.'.format(md, od, loc)
        why = ('It relocates with a box shift at rough-in; the {0} is the '
               'stationary side.'.format(onoun))
        act = 'Move the device at install and note the RCP if the visible location changes.'
        resolve_by = 'field'
    elif rule == 'M1-EQ-EQ':
        what = '{0} and {1} occupy the same space{2}.'.format(md, od, loc)
        why = ('Equipment locations harden early around pads, hangers, and '
               'connected services, so a late move re-does all of it.')
        act = ('Relocate one unit and re-verify its service access before pad and '
               'hanger layout is released.')
        resolve_by = 'gear_setting'
    elif rule == 'M1-FIX-EQ':
        what = '{0} is locked to the ceiling grid over {1}{2}.'.format(md, od, loc)
        why = ('Moving the fixture means an architect-approved RCP change; moving '
               'the unit means new hangers and connections, so it is a two-party '
               'decision either way.')
        act = 'Settle it before the ceiling grid is installed in this area.'
        resolve_by = 'ceiling_close'
    elif rule == 'M1-FIX-CURVE':
        what = '{0} conflicts with {1} in the plenum{2}.'.format(md, od, loc)
        why = ('The fixture is fixed to the RCP grid, so the routed run is the '
               'side that moves; plenum space above the grid is the scarce '
               'resource.')
        act = 'Reroute the run or relocate the fixture on the RCP before the grid closes.'
        resolve_by = 'ceiling_close'
    elif rule == 'M1-FP':
        what = '{0} conflicts with {1}{2}.'.format(md, od, loc)
        why = ('Sprinkler mains and branches re-trigger spacing and obstruction '
               'checks when they move, so an FP relocation is never free.')
        act = ('Coordinate the new routing with the sprinkler contractor before '
               'their shop drawings release.')
    elif rule == 'M1-SLOPE':
        what = '{0} is slope-bound and cannot simply rise or dip around {1}{2}.'.format(md, od, loc)
        code_ref = _d.SLOPE_CODE_BY_KLASS.get(mover['klass']) if mover is not None else None
        why = ('Gravity, condensate, and vent lines hold a continuous pitch ({0}), '
               'so the pressurized or ducted side is almost always the '
               'mover.'.format(code_ref or 'IPC 704.1'))
        act = ('Reroute {0} this cycle; if the slope line must move, re-check its '
               'invert end to end.'.format(od))
    elif rule == 'M1-EQ-SYS':
        what = '{0} and {1} contend for the same space{2}.'.format(md, od, loc)
        why = ('Placed equipment is involved, so its location is a coordination '
               'decision shared between trades, not a field nudge.')
        act = ('Agree the unit location (or the other side) this cycle, before '
               'fabrication and gear setting.')
        resolve_by = 'gear_setting'
    elif rule == 'M1-RIGID':
        what = '{0} and {1} cross and neither reroutes cheaply{2}.'.format(md, od, loc)
        why = ('Two low-slack systems in one envelope is the riser-congestion '
               'pattern that generates RFIs when left to the field.')
        act = 'Pick who drops in section at the next coordination meeting.'
    elif rule == 'M1-XING':
        what = '{0} crosses {1} at the same elevation{2}.'.format(md, od, loc)
        why = ('One of the two has to drop or rise; the cheaper mover is the '
               'smaller or more flexible run, and it only costs money if it waits '
               'for fabrication.')
        act = 'Pick the mover this cycle and hold the elevation on both shop sets.'
    elif rule == 'FB' and mover is None:
        what = 'Neither side of this pair is a dbHMS element that can move{0}.'.format(loc)
        why = 'There is nothing here for us to reroute.'
        act = 'Flag it to the design team.'
    else:  # FB residual
        what = '{0} crosses {1}{2}.'.format(md, od, loc)
        why = 'A minor conflict where {0} is the cheap mover.'.format(md)
        act = 'Reroute it at the model or in the field.'
        resolve_by = 'field'

    # NOTE: no blanket slope-code fallback here. A code_ref must be EARNED by
    # the sentence the user reads (doctrine 2). The rules that argue slope --
    # C1 and M1-SLOPE -- set their own SLOPE_CODE_BY_KLASS citation inside their
    # own branch. A blanket "if the mover happens to be a gravity/vent system,
    # cite its slope code" bled an unearned IPC/IMC citation (and a "Code:"
    # facts row) onto ~450 N1/M4/M2/N4/M3/FB rows whose sentence never mentions
    # slope -- 70% of all citations in the V4 run. Removed (V4 deep dive).

    q = _qualifier(ctx)
    reason = ' '.join([s for s in (what, why, act, q) if s])
    headline = _truncate(what, 90)
    return headline, reason, code_ref, resolve_by


def _build_facts(ctx, rule, code_ref):
    """Ordered {'k','v','unit','method'} rows for the UI facts table.
    DERIVED-ONLY: nothing in the engine ever reads this back. A null `v`
    renders as '(not captured)' in the UI."""
    c = ctx['c']
    mover = ctx['mover']
    other = ctx['other']
    facts = [{'k': 'Rule', 'v': rule}]
    if ctx['kind'] == 'clearance':
        # Clearance framing: ref_a is the intruder, ref_b the equipment owner
        # (runner convention). Label them that way so the facts match the
        # composed sentence -- 'Mover'/'Other side' read backwards here (they
        # called the panel the Mover and the intruding wall the Other side).
        pa, pb = ctx.get('pa'), ctx.get('pb')
        if pa is not None:
            facts.append({'k': 'Intruder', 'v': _desc_phrase(pa)})
        if pb is not None:
            facts.append({'k': 'Equipment', 'v': _desc_phrase(pb)})
    else:
        if mover is not None:
            facts.append({'k': 'Mover', 'v': _desc_phrase(mover)})
        if other is not None:
            facts.append({'k': 'Other side', 'v': _desc_phrase(other)})
        sysw = _sysword(mover) if mover is not None else None
        if sysw:
            facts.append({'k': 'System', 'v': sysw})
    if ctx['kind'] == 'clearance':
        # Clearance rows measure zone intrusion, not penetration/overlap;
        # showing bare "(not captured)" pen/overlap rows would read as a
        # capture failure. Each is guarded so only measured values appear.
        intr_d = c.get('intrusion_depth_in')
        if intr_d is not None:
            facts.append({'k': 'Intrusion', 'v': _fmt_num(intr_d), 'unit': 'in'})
        cap = c.get('zone_cap_ft')
        if cap is not None:
            facts.append({'k': 'Zone cap', 'v': _fmt_num(cap), 'unit': 'ft'})
        spr = c.get('spr_clearance_in')
        if spr is not None:
            facts.append({'k': 'Head clearance', 'v': _fmt_num(spr), 'unit': 'in'})
    else:
        pen = c.get('penetration_depth_in')
        # Annotate the depth ONLY with the reliable read. penetration_depth_in
        # is the min extent of the overlap solid, so depth >= assembly
        # thickness unambiguously means the run spans it ('passes through'). A
        # 'partial' read is NOT reliable -- a run thinner than the wall shows a
        # min-extent below the thickness even when it passes straight through --
        # so we never assert "stops inside" (that was the V4 M-PEN false-Major
        # trap). Omit the annotation in the ambiguous case.
        pen_method = ('full - passes through'
                      if ctx.get('pen_class') == 'full' else None)
        pen_row = {'k': 'Penetration', 'v': (None if pen is None else _fmt_num(pen)),
                   'unit': 'in'}
        if pen_method:
            pen_row['method'] = pen_method
        facts.append(pen_row)
        vol = c.get('overlap_volume_cf')
        facts.append({'k': 'Overlap', 'v': (None if vol is None else _fmt_num(vol)),
                      'unit': 'cf'})
    if ctx['kind'] == 'soft':
        g = c.get('gap_inches')
        facts.append({'k': 'Gap', 'v': (None if g is None else _fmt_num(g)),
                      'unit': 'in', 'method': c.get('gap_method')})
    if ctx['cluster_n']:
        facts.append({'k': 'Cluster', 'v': '{0} within {1} ft'.format(
            ctx['cluster_n'], _fmt_num(ctx['radius']))})
    if code_ref:
        facts.append({'k': 'Code', 'v': code_ref})
    lvl = _level_for(c, ctx)
    if lvl:
        facts.append({'k': 'Level', 'v': _truncate(str(lvl), 30)})
    return facts


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _score_one(c, cluster_n, cfg, prev_rule):
    pa = _participant(c.get('ref_a'), cfg)
    pb = _participant(c.get('ref_b'), cfg)
    mover = _mover(pa, pb)
    other = _other(pa, pb, mover) if mover is not None else None

    # Layer A (manual override beats every rule)
    override = c.get('suppress_override')
    if override is True:
        sup_rule, sup_reason = 'manual', 'Suppressed by a coordinator.'
    elif override is False:
        sup_rule, sup_reason = None, None
    else:
        sup_rule, sup_reason = _layer_a(c, pa, pb, cluster_n, cfg)

    band, rule, _reason0, flags = _tier(c, pa, pb, mover, cluster_n, cfg, prev_rule)

    confidence, cause = _confidence(c, pa, pb, mover)
    ctx = {
        'c': c, 'mover': mover, 'other': other,
        # pa/pb keep the ref_a/ref_b participants so the clearance composer can
        # name the intruder (pa) and the equipment owner (pb) by the runner's
        # convention, instead of the routing-clash mover/other heuristic (which
        # does not map cleanly when the "owner" side is the fixed one).
        'pa': pa, 'pb': pb,
        'kind': c.get('kind') or 'hard', 'cluster_n': cluster_n,
        'radius': cfg.get('cluster_radius_ft') or 5.0, 'flags': flags,
        'confidence': confidence, 'degrade_cause': cause,
        'gap_ratio': _gap_ratio(c),
        'pen_class': (_pen_class(c, other, cfg) if other is not None else None),
    }
    headline, reason, code_ref, resolve_by = _compose(rule, band, ctx)

    bars, raw = _sub_score(c, pa, pb, mover, other, cluster_n, cfg, code_ref)
    bands = cfg.get('bands') or _d.DEFAULTS['bands']
    base, top = bands.get(band, (8, 39))
    raw_max = float(cfg.get('raw_realistic_max') or 36.0)
    frac = 0.0 if raw_max <= 0 else min(1.0, raw / raw_max)
    score = base + _round_half_away((top - base) * frac)
    if score < base:
        score = base
    elif score > top:
        score = top

    facts = _build_facts(ctx, rule, code_ref)
    rb_label = _d.DEADLINES.get(resolve_by) if resolve_by else None

    mover_side = 'a' if mover is pa else ('b' if mover is pb else None)

    return {
        'v': 1,
        'config_rev': cfg.get('rev', 1),
        'band': band,
        'tier': band.lower(),
        'rule': rule,
        'score': score,
        'headline': headline,
        'reason': reason,
        'code_ref': code_ref,
        'resolve_by': resolve_by,
        'resolve_by_label': rb_label,
        'relevance_class': _relevance_class(rule, flags),
        'facts': facts,
        'brk': bars,
        'suppressed': sup_rule is not None,
        'suppress_rule': sup_rule,
        'suppress_reason': sup_reason,
        'flags': flags,
        'confidence': confidence,
        'features': {
            'rigidity_a': pa['rigidity'],
            'rigidity_b': pb['rigidity'],
            'fixed_a': pa['fixed_kind'],
            'fixed_b': pb['fixed_kind'],
            'mover': mover_side,
            'mover_class': None if mover is None else mover['klass'],
            'rigidity_src': None if mover is None else mover['rig_src'],
            'cluster_n': cluster_n,
            'gap_ratio': _gap_ratio(c),
            'bulk_in': _bulk_in(pa, pb),
            'sub_raw': raw,
        },
    }


def _fallback_importance(cfg):
    bands = cfg.get('bands') or _d.DEFAULTS['bands']
    base, _top = bands.get('Minor', (8, 39))
    return {
        'v': 1, 'config_rev': cfg.get('rev', 1),
        'band': 'Minor', 'tier': 'minor', 'rule': 'ERR',
        'score': base,
        'headline': 'Scoring failed for this clash.',
        'reason': 'Scoring failed for this clash; showing at the Minor floor.',
        'code_ref': None, 'resolve_by': None, 'resolve_by_label': None,
        'relevance_class': 'error', 'facts': [{'k': 'Rule', 'v': 'ERR'}],
        'brk': [], 'suppressed': False, 'suppress_rule': None,
        'suppress_reason': None, 'flags': [], 'confidence': 'degraded',
        'features': {},
    }


def _fmt_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    # NaN/inf survive float() but int(f) raises -- guard so a bad measurement
    # degrades to a printable string instead of crashing the whole score.
    if f != f or f in (float('inf'), float('-inf')):
        return str(v)
    if f == int(f):
        return str(int(f))
    return '{0:g}'.format(f)
