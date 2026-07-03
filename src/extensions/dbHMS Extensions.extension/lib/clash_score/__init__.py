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

from clash_score import defaults as _d


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
    if not dims:
        return None
    try:
        vals = [float(v) for v in dims if v is not None]
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    return max(vals)


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


def _is_gravity(ref, cfg):
    """Classification-primary, name-regex backstop, slope as third signal."""
    sys_class = ref.get('sys_class')
    if sys_class in _d.GRAVITY_CLASSES:
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
        if ref.get('sys_class') == 'Vent':
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
    sys_class = ref.get('sys_class')
    if sys_class in _d.FP_DRY_CLASSES:
        return 4, 'system', 'fp_dry', False
    if cat in _d.SPRINKLER_CATS:
        return 1, 'class', 'sprinkler', False
    if sys_class in _d.FP_WET_CLASSES:
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
        if (c.get('kind') == 'hard' and pen is not None
                and float(pen) < float(cfg.get('graze_floor_in') or 0.375)):
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

    # C2: the size gate reads the MOVER's dimension (not either participant),
    # and a code-clearance test vs structure escalates too (dormant until
    # clearance detection ships -- same pattern as C4/R-GRAZE).
    # klass guard: C2 is about DUCTWORK too big to reroute. Equipment never
    # carries dims_in today, but the day bounding-box capture ships, a
    # pad-mounted AHU on its foundation must stay N3 bearing, never flip
    # Critical (review finding; pinned by a fixture test).
    if (vs_struct and rig == 4 and mover['klass'] != 'equipment' and (
            (kind == 'hard' and mover['dim_in'] is not None
             and mover['dim_in'] >= float(cfg.get('big_duct_in') or 24.0))
            or kind == 'clearance')):
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
    if pen is not None and float(pen) >= float(cfg.get('deep_pen_in') or 6.0):
        return ('Critical', 'C4',
                'Deep penetration ({0} in): systemic overlap, not a graze.'.format(
                    _fmt_num(pen)), flags)

    # --- Major before Minor, with two carve-outs learned from the NIUHTC
    # calibration: mounting adjacency (N3) and penetrations (N1) leave the
    # Major path BEFORE structure/congestion can promote them ---------------
    if kind == 'clearance':
        return ('Major', 'M-CODE',
                'Code clearance consumed: a clearance test predicts an '
                'inspection failure, not a graze.', flags)

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
    if (kind == 'hard' and vs_arch
            and other['cat'] in _d.PENETRABLE_ARCH_CATS
            and mover['cat'] is not None and (
                mover['cat'] in _d.DUCT_CATS or mover['cat'] in _d.PIPE_CATS or
                mover['cat'] in _d.CONDUIT_CATS or mover['cat'] in _d.TRAY_CATS or
                mover['cat'] in _d.FLEX_CATS)):
        flags.append('penetration_candidate')
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
            return ('Major', 'M1',
                    'Reroute around a large obstruction: needs a coordinated '
                    'path, not a nudge.', flags)

    if kind == 'hard' and mover['klass'] in ('fixture', 'tech'):
        flags.append('field_fix')
    return 'Minor', 'FB', _fb_reason(kind, mover), flags


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

def _sub_score(c, pa, pb, mover, cluster_n, cfg):
    """Returns (list of {'k','v'} bars, clamped_total, raw_total)."""
    kind = c.get('kind') or 'hard'

    rig = None if mover is None else mover['rigidity']
    constraint = 3 * rig if rig is not None else 4

    bulk = _bulk_in(pa, pb)
    if bulk is None:
        size = 2
    elif bulk >= 24.0:
        size = 8
    elif bulk >= 12.0:
        size = 5
    elif bulk >= 6.0:
        size = 3
    else:
        size = 1

    pen = c.get('penetration_depth_in')
    if kind == 'hard':
        if pen is None:
            geometry = 4
        else:
            geometry = min(8, int(round(2 * float(pen))))
    else:
        if c.get('is_contact'):
            geometry = 6
        else:
            ratio = _gap_ratio(c)
            if ratio is None:
                geometry = 3
            else:
                geometry = max(0, min(6, int(round(6 * (1.0 - ratio)))))

    if cluster_n >= 20:
        congestion = 6
    elif cluster_n >= 10:
        congestion = 4
    elif cluster_n >= 5:
        congestion = 2
    else:
        congestion = 0

    code = 0
    if kind == 'clearance':
        code = 6
    elif mover is not None and mover['klass'] in ('gravity', 'grease',
                                                  'gravity_vent', 'condensate',
                                                  'medgas'):
        code = 6

    bars = [
        {'k': 'Mover constraint', 'v': constraint},
        {'k': 'Size', 'v': size},
        {'k': 'Geometry', 'v': geometry},
        {'k': 'Congestion', 'v': congestion},
        {'k': 'Code', 'v': code},
    ]
    raw = constraint + size + geometry + congestion + code
    return bars, raw


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _score_one(c, cluster_n, cfg, prev_rule):
    pa = _participant(c.get('ref_a'), cfg)
    pb = _participant(c.get('ref_b'), cfg)
    mover = _mover(pa, pb)

    # Layer A (manual override beats every rule)
    override = c.get('suppress_override')
    if override is True:
        sup_rule, sup_reason = 'manual', 'Suppressed by a coordinator.'
    elif override is False:
        sup_rule, sup_reason = None, None
    else:
        sup_rule, sup_reason = _layer_a(c, pa, pb, cluster_n, cfg)

    band, rule, reason, flags = _tier(c, pa, pb, mover, cluster_n, cfg, prev_rule)

    bars, raw = _sub_score(c, pa, pb, mover, cluster_n, cfg)
    bands = cfg.get('bands') or _d.DEFAULTS['bands']
    base, top = bands.get(band, (8, 39))
    score = base + max(0, min(top - base, raw))

    confidence = 'full'
    if mover is None:
        confidence = 'degraded'
    elif mover['rig_src'] == 'category':
        confidence = 'degraded'
    elif (c.get('kind') == 'soft' and c.get('gap_inches') is None):
        confidence = 'degraded'
    elif pa['fixed_kind'] == 'unknown' or pb['fixed_kind'] == 'unknown':
        confidence = 'degraded'

    if confidence == 'degraded':
        reason += ' (estimated from categories; re-run detection for system/size detail)'
    if mover is not None and mover.get('near'):
        reason += ' (near a size threshold: verify dimensions)'

    mover_side = None
    if mover is pa:
        mover_side = 'a'
    elif mover is pb:
        mover_side = 'b'

    return {
        'v': 1,
        'config_rev': cfg.get('rev', 1),
        'band': band,
        'tier': band.lower(),
        'rule': rule,
        'score': score,
        'reason': reason,
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
        'reason': 'Scoring failed for this clash; showing at the Minor floor.',
        'brk': [], 'suppressed': False, 'suppress_rule': None,
        'suppress_reason': None, 'flags': [], 'confidence': 'degraded',
        'features': {},
    }


def _fmt_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return str(int(f))
    return '{0:g}'.format(f)
