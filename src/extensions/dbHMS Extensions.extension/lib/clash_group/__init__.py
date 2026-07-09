# -*- coding: utf-8 -*-
"""Layer C grouping: sticky issue containers over the scored clash list.

Design: Clash Detection.panel/README.md (Clash Detection.panel/), approved
2026-07-02. The one-paragraph version: a group is an ISSUE - a ticket a
coordinator names, assigns, and discusses - not a query result. Durable
identity lives on the member clashes (immortal clash ids; merge never
deletes records), and the group is a persistent container of them. The
algorithm NEVER re-derives existing rosters; it only proposes membership
for currently-ungrouped clashes (the iConstruct / BIMcollab Smart Issues
pattern). Automatic formation runs on two axes - density-gated spatial
clusters (the congested rack) and a participation-anchored element star
(the one bad duct with 12 hits) - but geometry never becomes identity.
Splits and merges are human acts only.

Contract with the rest of the codebase (mirrors lib/clash_score):
  - Pure data, zero Revit imports. IronPython 2.7 + CPython 3.
  - `regroup_all(clashes, groups, run_iso=...)` is called AFTER
    `clash_score.score_all(merged)` in both run pipelines; it returns a
    NEW groups list (existing group dicts are deep-copied, never aliased)
    and stamps `group_id` on member clashes.
  - Groups persist as the top-level "groups" key INSIDE clashes.json so
    one atomic write covers members + rosters + statuses. Both run
    pipelines' new_data literals carry it (integrity-tested).
  - M4 and clash_score are untouched: this module only READS the stamped
    `importance` blocks (suppressed, features.cluster_n, band/score).
  - Suppressed clashes are never assigned; a rostered member that later
    becomes suppressed keeps its seat but stops counting toward the
    band/open rollup.

Group statuses: Open / Reviewed / Approved / Resolved / MergedInto.
Member "open" means clash status Open or Reviewed and not suppressed;
Approved and Resolved members are "decided". A group auto-resolves when
every member is decided (or suppressed) and reopens when any member does.
"""

import copy
import uuid

from clash_group import defaults as _d


OPEN_STATUSES = ('Open', 'Reviewed')
DECIDED_STATUSES = ('Approved', 'Resolved')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def regroup_all(clashes, groups, run_iso=None, config=None):
    """Reconcile existing groups and form new ones. Pure; deterministic
    except for freshly minted group ids (uuid4).

    Args:
        clashes: the MERGED, SCORED clash list (dicts). Mutated only by
            stamping `group_id` (members) / None (ungrouped).
        groups: the existing groups list from clashes.json (may be []).
            Never mutated; a deep-copied, updated list is returned.
        run_iso: this run's ISO timestamp (the same value passed to
            merge_runs). Used to recognize this-run churn (new members,
            fresh auto-resolves) for successor adoption and the churn
            counters. None (rescore outside a run) skips churn detection.
        config: optional override dict merged over the firm standard
            (tests only).

    Returns:
        (groups_out, summary) where summary counts what happened:
        {'groups_total', 'groups_new', 'adopted', 'joined', 'suggested',
         'ungrouped'}
    """
    cfg = dict(_d.DEFAULTS)
    if config:
        cfg.update(config)

    out = [copy.deepcopy(g) for g in (groups or [])]
    by_id = {}
    for c in clashes:
        cid = c.get('id')
        if cid:
            by_id[cid] = c

    # run_iso=None (a rescore outside a run) intentionally skips churn
    # detection: no adoption, churn counters preserved. Do NOT derive a
    # fallback run stamp - a Browser-side rescore after a group edit must
    # never re-run the adoption cascade.

    summary = {'groups_total': 0, 'groups_new': 0, 'adopted': 0,
               'joined': 0, 'suggested': 0, 'ungrouped': 0}

    # P1 - sticky rosters: nothing to compute. Every member id stays
    # UNCONDITIONALLY - including ids that point at no record (possible
    # only via an external-writer race, decision D2); dropping them would
    # be silent roster shrinkage. member_of indexes live records only
    # (dangling ids cannot appear in any assignment pool, and every
    # downstream consumer filters on `m in by_id`).
    member_of = {}
    for g in out:
        for mid in g.get('member_ids') or []:
            if mid in by_id:
                member_of[mid] = g['id']

    # P2 - successor adoption (heals fingerprint re-key churn)
    _adopt_successors(out, clashes, by_id, member_of, run_iso, cfg, summary)

    # P3 - anchor-key claims (the one sanctioned silent join)
    _claim_by_anchor(out, clashes, by_id, member_of, cfg, summary)

    # P4 - spatial cluster formation (density-gated)
    pool = _assignable(clashes, member_of)
    new_cluster = _form_clusters(pool, cfg)
    for g in new_cluster:
        out.append(g)
        for mid in g['member_ids']:
            member_of[mid] = g['id']
        summary['groups_new'] += 1

    # P5 - element-star formation (participation anchoring)
    pool = _assignable(clashes, member_of)
    new_star = _form_stars(pool, cfg)
    for g in new_star:
        out.append(g)
        for mid in g['member_ids']:
            member_of[mid] = g['id']
        summary['groups_new'] += 1

    # P6 - spatial adjacency: suggest into curated cluster groups,
    # auto-join untouched ones
    pool = _assignable(clashes, member_of)
    _adjacency(out, pool, by_id, member_of, cfg, summary)

    # P7 - lifecycle + P8 - stamp
    all_member_ids = set(member_of.keys())
    for g in out:
        if g.get('status') == 'MergedInto':
            continue
        # suggestions must never overlap membership anywhere, and a dead
        # suggestion (resolved/suppressed since) must not be offered
        g['suggested_ids'] = [s for s in (g.get('suggested_ids') or [])
                              if s not in all_member_ids and s in by_id
                              and _is_assignable(by_id[s])]
        _lifecycle(g, by_id, run_iso)
        g['rollup'] = rollup(g, by_id, run_iso=run_iso, config=cfg)
        if not g.get('title_locked'):
            g['title'] = auto_title(g, [by_id[m]
                                        for m in (g.get('member_ids') or [])
                                        if m in by_id])
        summary['groups_total'] += 1

    # Mint stable per-project group seqs (agenda tie-break: created_at is
    # second-granular, so all groups formed in one run share a stamp and
    # ordering would hang on list position).
    next_seq = 1
    for g in out:
        try:
            s = int(g.get('seq') or 0)
        except (TypeError, ValueError):
            s = 0
        if s >= next_seq:
            next_seq = s + 1
    for g in out:
        if g.get('status') == 'MergedInto':
            continue                     # tombstones pass through untouched
        if not g.get('seq'):
            g['seq'] = next_seq
            next_seq += 1

    # Disambiguate duplicate auto-titles with a centroid locator (NIUHTC:
    # 30% of groups shared a title; nine identical ceiling-vs-lights
    # titles sat 70 ft apart along one corridor). Locked titles are the
    # human's business; unlocked titles were just regenerated above, so
    # the suffix never accumulates run over run.
    by_title = {}
    for g in out:
        if g.get('status') == 'MergedInto' or g.get('title_locked'):
            continue
        by_title.setdefault(g.get('title'), []).append(g)
    for title, gs in by_title.items():
        if not title or len(gs) < 2:
            continue
        for g in gs:
            mids = [_mid(by_id[m]) for m in (g.get('member_ids') or [])
                    if m in by_id]
            mids = [m for m in mids if m is not None]
            if not mids:
                continue
            cx = sum(m[0] for m in mids) / len(mids)
            cy = sum(m[1] for m in mids) / len(mids)
            g['title'] = u'{0} @ {1},{2} ft'.format(
                title, int(round(cx / 5.0) * 5), int(round(cy / 5.0) * 5))

    # Per-clash stamp: derived read convenience, rewritten every pass.
    for c in clashes:
        gid = member_of.get(c.get('id'))
        c['group_id'] = gid
        if gid is None and _is_assignable(c):
            summary['ungrouped'] += 1

    return out, summary


def rollup(group, clashes_by_id, run_iso=None, config=None):
    """Recompute a group's rollup dict from live member records. Pure;
    also used by the Browser/host after a group edit."""
    cfg = dict(_d.DEFAULTS)
    if config:
        cfg.update(config)
    members = [clashes_by_id[m] for m in (group.get('member_ids') or [])
               if m in clashes_by_id]
    open_members = [c for c in members if _is_open(c)]

    # Band = the MAX band among open members per the firm band order (never
    # inferred from scores: that would silently couple this lib to
    # clash_score's clamp invariant). The controlling member - whose score
    # and reason surface on the card - is the highest-score member WITHIN
    # that band.
    order = list(cfg.get('band_order') or ('Critical', 'Major', 'Minor'))
    best_band = None
    for c in open_members:
        b = (c.get('importance') or {}).get('band')
        if b in order and (best_band is None
                           or order.index(b) < order.index(best_band)):
            best_band = b
    # A fully-decided group (all members Approved/Resolved/suppressed)
    # keeps its HISTORICAL worst band instead of rolling up null - a
    # Resolved rack whose worst member was Critical must not render as a
    # "0 Minor" chip under the Resolved preset (review finding).
    if not open_members and members:
        for c in members:
            b = (c.get('importance') or {}).get('band')
            if b in order and (best_band is None
                               or order.index(b) < order.index(best_band)):
                best_band = b
        band_pool = [c for c in members
                     if (c.get('importance') or {}).get('band') == best_band]
        if not band_pool:
            band_pool = members
    else:
        band_pool = [c for c in open_members
                     if (c.get('importance') or {}).get('band') == best_band]
        if not band_pool:
            band_pool = open_members
    best = None
    for c in band_pool:
        score = ((c.get('importance') or {}).get('score')) or 0
        if best is None or score > (((best.get('importance') or {})
                                     .get('score')) or 0):
            best = c
    best_imp = (best.get('importance') or {}) if best is not None else {}

    prev = group.get('rollup') or {}
    if run_iso:
        # Tier-1 rekeys are identity continuity, not new membership: a
        # "+5 new" badge on a group where five members merely drifted
        # across the 1-ft fingerprint bucket is exactly the false-alarm
        # noise the successor cascade exists to suppress.
        rekeyed = set()
        for h in (group.get('history') or []):
            if (h.get('action') == 'member_rekeyed'
                    and (h.get('at') or '') >= run_iso and h.get('after')):
                rekeyed.add(h['after'])
        n_new_run = len([c for c in members
                         if c.get('first_seen_run') == run_iso
                         and c.get('id') not in rekeyed])
        n_resolved_run = len([c for c in members
                              if _resolved_this_run(c, run_iso)])
        n_reopened_run = len([c for c in members
                              if _last_action_this_run(c, 'reopened', run_iso)])
    else:
        n_new_run = prev.get('n_new_run', 0)
        n_resolved_run = prev.get('n_resolved_run', 0)
        n_reopened_run = prev.get('n_reopened_run', 0)

    mids = [c.get('midpoint') for c in open_members]
    mids = [m for m in mids if m and len(m) >= 3]
    drifted = False
    # Drift is only meaningful for spatial containers: an element star's
    # members lie along its anchor by nature (a duct run hitting walls
    # every 10 ft), so multi-component is its normal day-one state
    # (NIUHTC calibration: 151 of 246 stars false-flagged).
    if (group.get('axis') in ('cluster', 'manual')
            and len(open_members) >= 2 and len(mids) >= 2):
        comps = _component_count(mids, float(cfg['cluster_eps_ft']))
        drifted = comps >= 2

    congested = False
    threshold = int(cfg.get('congested_cluster_n') or 20)
    for c in open_members:
        n = ((c.get('importance') or {}).get('features') or {}).get('cluster_n')
        if n is not None and n >= threshold:
            congested = True
            break

    level = _modal([_level_of(c) for c in members])
    return {
        'band': best_imp.get('band'),
        'score': best_imp.get('score'),
        # rev 4: the group gets its OWN composed 2-3 sentence narrative
        # (composition / governing constraint / action + governing deadline),
        # not a copy of one member's reason. The controlling member's code and
        # deadline surface so the group card can show the governing item.
        'reason': _group_reason(group, members, open_members, best_imp,
                                congested, level),
        'code_ref': best_imp.get('code_ref'),
        'resolve_by': best_imp.get('resolve_by'),
        'resolve_by_label': best_imp.get('resolve_by_label'),
        'trades': _trades(members),
        'level': level,
        'n_open': len(open_members),
        'n_resolved': len([c for c in members
                           if c.get('status') == 'Resolved']),
        'n_approved': len([c for c in members
                           if c.get('status') == 'Approved']),
        'n_suppressed': len([c for c in members if _is_suppressed(c)]),
        'n_new_run': n_new_run,
        'n_resolved_run': n_resolved_run,
        'n_reopened_run': n_reopened_run,
        'drifted': drifted,
        'congested': congested,
        'diameter_ft': _diameter_ft(mids),
    }


def _trades(members):
    out = []
    for c in members:
        t = c.get('assignee')
        if t and t not in out:
            out.append(t)
    return out


def _group_reason(group, members, open_members, best_imp, congested, level):
    """The group's 2-3 sentence narrative. Mirrors clash_score's grammar:
    S1 composition, S2 governing constraint, S3 action + governing deadline."""
    n = len(members)
    n_open = len(open_members)
    trades = _trades(members)
    if len(trades) >= 2:
        trade_ph = ', {0} trades ({1})'.format(
            len(trades), ', '.join(trades[:3]))
    else:
        trade_ph = ''
    if group.get('axis') == 'cluster':
        where = ' in one congested zone'
    else:
        anchor = group.get('anchor') or {}
        a = anchor.get('name') or anchor.get('category')
        where = ' anchored on {0}'.format(a) if a else ''
    lvl = ' on {0}'.format(level) if level else ''
    score = best_imp.get('score')
    band = best_imp.get('band') or 'Minor'
    s1 = '{0} clash{1}{2}{3}{4}; worst member scores {5} ({6}).'.format(
        n, '' if n == 1 else 'es', trade_ph, where, lvl, score, band)

    code = best_imp.get('code_ref')
    if code:
        s2 = ('The governing member carries a code constraint ({0}), so it '
              'sets the priority for the set.'.format(code))
    elif congested:
        s2 = ('The zone is dense enough that fixes interact, so treat it as one '
              'design decision rather than many separate ones.')
    else:
        s2 = ('The worst member sets the priority; clearing it usually clears '
              'much of the set.')

    rb = best_imp.get('resolve_by_label')
    s3 = 'Resolve the set as one study{0}; {1} of {2} still open.'.format(
        (' ' + rb) if rb else '', n_open, n)
    return ' '.join([s1, s2, s3])


def auto_title(group, members):
    """Meeting-grade title: facet + what + where + count. Regenerated only
    while title_locked is false.

    Known gap vs the design's title recipes: no grid/zone span ("B/3-F/3").
    Grid data does not exist on clash records yet; adding it needs a
    detection-time enrichment (nearest grid intersection stamped per
    clash) - this pure lib cannot derive it. Titles carry level + count
    until that lands."""
    n = len(members)
    level = _modal([_level_of(c) for c in members]) or 'multiple levels'
    axis = group.get('axis')

    if axis == 'cluster':
        trades = set()
        for c in members:
            t = c.get('assignee')
            if t:
                trades.add(t)
        return 'Congested rack - {0} ({1} clashes, {2} trade{3})'.format(
            level, n, max(1, len(trades)),
            '' if len(trades) == 1 else 's')

    anchor = group.get('anchor') or {}
    a_desc = anchor.get('name') or anchor.get('category') or 'element'

    # Penetration set: every member carries the N1 flag
    if members and all('penetration_candidate' in
                       ((c.get('importance') or {}).get('flags') or [])
                       for c in members):
        return 'Penetrations - {0}, {1} ({2})'.format(a_desc, level, n)

    other_cats = []
    for c in members:
        for ref_key in ('ref_a', 'ref_b'):
            ref = c.get(ref_key) or {}
            if not _ref_matches_anchor(ref, anchor):
                cat = ref.get('category')
                if cat:
                    other_cats.append(cat)
    other = _modal(other_cats) or 'elements'
    return "{0} vs {1}x {2} - {3}".format(a_desc, n, other, level)


# ---------------------------------------------------------------------------
# Eligibility / small predicates
# ---------------------------------------------------------------------------

def _is_suppressed(c):
    return bool((c.get('importance') or {}).get('suppressed'))


def _is_open(c):
    return (c.get('status') in OPEN_STATUSES) and not _is_suppressed(c)


def _is_assignable(c):
    return _is_open(c)


def _assignable(clashes, member_of):
    return [c for c in clashes
            if c.get('id') and c['id'] not in member_of and _is_assignable(c)]


def _cluster_n(c):
    n = ((c.get('importance') or {}).get('features') or {}).get('cluster_n')
    return n if n is not None else 0


def _level_of(c):
    a = c.get('ref_a') or {}
    b = c.get('ref_b') or {}
    return a.get('level') or b.get('level')


def _modal(values):
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    best, best_n = None, 0
    for v in sorted(counts.keys(), key=lambda x: str(x)):
        if counts[v] > best_n:
            best, best_n = v, counts[v]
    return best


def _resolved_this_run(c, run_iso):
    """Auto-resolved by THIS run's merge: history is stamped moments after
    run_iso is minted, so `at >= run_iso` holds only for this run."""
    if c.get('status') != 'Resolved':
        return False
    return _last_action_this_run(c, 'auto_resolved', run_iso)


def _last_action_this_run(c, action, run_iso):
    hist = c.get('history') or []
    if not hist:
        return False
    last = hist[-1] or {}
    at = last.get('at') or ''
    return last.get('action') == action and run_iso is not None and at >= run_iso


def _is_curated(g):
    """A human has touched this group. Phase-3 group ops set curated=True
    explicitly; until then the observable signals below cover it."""
    if g.get('curated'):
        return True
    if g.get('title_locked'):
        return True
    if g.get('comments'):
        return True
    if g.get('axis') == 'manual':
        return True
    if g.get('status') not in (None, 'Open'):
        return True
    return False


# ---------------------------------------------------------------------------
# Element keys (identity primitives)
# ---------------------------------------------------------------------------

def _elem_key(ref):
    """Stable element key: (source, unique_id) when unique_id exists, else
    the old-data fallback source + element_id + link doc title. Never
    fed_key (embeds link placement origin) and never enrichment values."""
    if not ref:
        return None
    src = ref.get('source') or 'host'
    uid = ref.get('unique_id')
    if uid:
        return u'{0}|uid|{1}'.format(src, uid)
    eid = ref.get('element_id')
    if eid is None:
        return None
    return u'{0}|eid|{1}|{2}'.format(src, eid, ref.get('link_doc_title') or '')


def _pair_key(c):
    """Unordered element pair key (same shape the fingerprint hashes)."""
    a = c.get('ref_a') or {}
    b = c.get('ref_b') or {}
    ka = u'{0}:{1}'.format(a.get('source') or 'host', a.get('element_id'))
    kb = u'{0}:{1}'.format(b.get('source') or 'host', b.get('element_id'))
    return tuple(sorted((ka, kb)))


def _cat_pair_key(c):
    a = (c.get('ref_a') or {}).get('category') or ''
    b = (c.get('ref_b') or {}).get('category') or ''
    return tuple(sorted((a, b)))


def _ref_matches_anchor(ref, anchor):
    if not ref or not anchor:
        return False
    if (ref.get('source') or 'host') != (anchor.get('source') or 'host'):
        return False
    uid_a = anchor.get('unique_id')
    if uid_a and ref.get('unique_id'):
        return ref['unique_id'] == uid_a
    if anchor.get('element_id') is None or ref.get('element_id') is None:
        return False
    return (ref['element_id'] == anchor['element_id']
            and (ref.get('link_doc_title') or '') ==
                (anchor.get('link_doc_title') or ''))


def _anchor_from_ref(ref):
    ref = ref or {}
    return {
        'source': ref.get('source') or 'host',
        'unique_id': ref.get('unique_id'),
        'element_id': ref.get('element_id'),
        'name': ref.get('name'),
        'category': ref.get('category'),
        'link_doc_title': ref.get('link_doc_title'),
    }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _mid(c):
    m = c.get('midpoint')
    if not m or len(m) < 3:
        return None
    try:
        return (float(m[0]), float(m[1]), float(m[2]))
    except (TypeError, ValueError):
        return None


def _d2(p, q):
    dx, dy, dz = p[0] - q[0], p[1] - q[1], p[2] - q[2]
    return dx * dx + dy * dy + dz * dz


def _diameter_ft(mids):
    """Bounding-box diagonal of the midpoints (cheap diameter proxy)."""
    if len(mids) < 2:
        return 0.0
    pts = [(float(m[0]), float(m[1]), float(m[2])) for m in mids]
    lo = [min(p[i] for p in pts) for i in (0, 1, 2)]
    hi = [max(p[i] for p in pts) for i in (0, 1, 2)]
    d2 = sum((hi[i] - lo[i]) ** 2 for i in (0, 1, 2))
    return round(d2 ** 0.5, 1)


class _UnionFind(object):
    def __init__(self):
        self.parent = {}
        self.size = {}

    def add(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.size[x] = 1

    def find(self, x):
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]          # path halving
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def _grid_key(p, cell):
    return (int(p[0] // cell), int(p[1] // cell), int(p[2] // cell))


def _neighbors_within(items, cell):
    """Yield (i, j) index pairs with distance <= cell, via a uniform grid.
    items: list of (index, point)."""
    grid = {}
    for idx, p in items:
        grid.setdefault(_grid_key(p, cell), []).append((idx, p))
    r2 = cell * cell
    seen = set()
    for idx, p in items:
        k = _grid_key(p, cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for jdx, q in grid.get((k[0] + dx, k[1] + dy, k[2] + dz), ()):
                        if jdx <= idx:
                            continue
                        pair = (idx, jdx)
                        if pair in seen:
                            continue
                        seen.add(pair)
                        if _d2(p, q) <= r2:
                            yield pair


def _component_count(mids, eps):
    items = list(enumerate(mids))
    uf = _UnionFind()
    for i, _p in items:
        uf.add(i)
    for i, j in _neighbors_within(items, eps):
        uf.union(i, j)
    return len(set(uf.find(i) for i, _p in items))


def _dominant_axis(pts):
    lo = [min(p[i] for p in pts) for i in (0, 1, 2)]
    hi = [max(p[i] for p in pts) for i in (0, 1, 2)]
    extents = [hi[i] - lo[i] for i in (0, 1, 2)]
    return extents.index(max(extents)), max(extents)


def _cut_oversize(members_pts, max_span, max_members):
    """Deterministically cut an oversize set at the largest coordinate gap
    along its dominant axis. members_pts: list of (clash, point). Returns
    a list of fragments."""
    if len(members_pts) <= max_members:
        pts = [p for _c, p in members_pts]
        _axis, extent = _dominant_axis(pts)
        if extent <= max_span:
            return [members_pts]
    pts = [p for _c, p in members_pts]
    axis, _extent = _dominant_axis(pts)
    ordered = sorted(members_pts, key=lambda cp: cp[1][axis])
    # Largest adjacent gap wins; among (near-)equal gaps prefer the most
    # central cut, so a uniformly spaced chain halves instead of peeling
    # one member per recursion. Deterministic.
    mid = len(ordered) / 2.0
    best_i, best_key = len(ordered) // 2, None
    for i in range(1, len(ordered)):
        gap = ordered[i][1][axis] - ordered[i - 1][1][axis]
        key = (round(gap, 6), -abs(i - mid))
        if best_key is None or key > best_key:
            best_key, best_i = key, i
    left, right = ordered[:best_i], ordered[best_i:]
    if not left or not right:
        return [ordered]
    return (_cut_oversize(left, max_span, max_members) +
            _cut_oversize(right, max_span, max_members))


# ---------------------------------------------------------------------------
# P2 - successor adoption
# ---------------------------------------------------------------------------

def _adopt_successors(groups, clashes, by_id, member_of, run_iso, cfg,
                      summary):
    if not run_iso:
        return
    new_pool = [c for c in clashes
                if c.get('id') and c['id'] not in member_of
                and _is_assignable(c)
                and c.get('first_seen_run') == run_iso]
    if not new_pool:
        return
    consumed = set()
    adopt_r2 = float(cfg['adopt_radius_ft']) ** 2

    for g in sorted(groups, key=lambda g: (g.get('created_at') or '',
                                           g.get('id') or '')):
        if g.get('status') == 'MergedInto':
            continue
        roster = g.get('member_ids') or []
        # Strictly members AUTO-RESOLVED BY THIS RUN's merge: a member a
        # human resolved weeks ago must never be evicted because the same
        # element pair re-clashes somewhere else in the building.
        vanished = [by_id[m] for m in roster
                    if m in by_id and _resolved_this_run(by_id[m], run_iso)]
        for old in sorted(vanished, key=lambda c: c.get('seq') or 0):
            old_pair = _pair_key(old)
            old_mid = _mid(old)

            # Tier 1: same test + same exact element pair -> silent adopt
            # (the same two elements re-keyed by the 1-ft fingerprint
            # bucket: identity continuity, not new membership).
            candidates = []
            for c in new_pool:
                if c['id'] in consumed or c['id'] in member_of:
                    continue
                if c.get('test_id') != old.get('test_id'):
                    continue
                if _pair_key(c) != old_pair:
                    continue
                mid = _mid(c)
                dist = _d2(old_mid, mid) if (old_mid and mid) else 0.0
                candidates.append((dist, c.get('seq') or 0, c))
            if candidates:
                candidates.sort(key=lambda t: (t[0], t[1]))
                new_c = candidates[0][2]
                idx = roster.index(old['id'])
                roster[idx] = new_c['id']
                member_of[new_c['id']] = g['id']
                # The evicted predecessor must lose its membership index
                # too, or P8 stamps it with a group_id its group denies.
                member_of.pop(old['id'], None)
                consumed.add(new_c['id'])
                _hist(g, 'member_rekeyed',
                      before=old['id'], after=new_c['id'])
                summary['adopted'] += 1
                continue

            # Tier 2: same test + same category pair + within 10 ft ->
            # suggestion only (covers delete-and-recreate; weaker evidence
            # never silently enters a named group).
            if old_mid is None:
                continue
            for c in new_pool:
                if c['id'] in consumed or c['id'] in member_of:
                    continue
                if c.get('test_id') != old.get('test_id'):
                    continue
                if _cat_pair_key(c) != _cat_pair_key(old):
                    continue
                mid = _mid(c)
                if mid is None or _d2(old_mid, mid) > adopt_r2:
                    continue
                sugg = g.setdefault('suggested_ids', [])
                if c['id'] not in sugg:
                    sugg.append(c['id'])
                    summary['suggested'] += 1


# ---------------------------------------------------------------------------
# P3 - anchor-key claims
# ---------------------------------------------------------------------------

def _claim_by_anchor(groups, clashes, by_id, member_of, cfg, summary):
    anchored = [(g.get('created_at') or '', g.get('id') or '', g)
                for g in groups
                if g.get('anchor') and g.get('axis') == 'element'
                and g.get('status') != 'MergedInto']
    if not anchored:
        return
    anchored.sort(key=lambda t: (t[0], t[1]))
    for c in clashes:
        cid = c.get('id')
        if not cid or cid in member_of or not _is_assignable(c):
            continue
        for _ca, _gi, g in anchored:
            anchor = g['anchor']
            if (_ref_matches_anchor(c.get('ref_a'), anchor)
                    or _ref_matches_anchor(c.get('ref_b'), anchor)):
                g.setdefault('member_ids', []).append(cid)
                member_of[cid] = g['id']
                _hist(g, 'member_joined', after=cid)
                if g.get('status') not in (None, 'Open'):
                    g['needs_review'] = True
                    _hist(g, 'needs_review',
                          before='status={0}'.format(g.get('status')))
                summary['joined'] += 1
                break


# ---------------------------------------------------------------------------
# P4 - spatial cluster formation
# ---------------------------------------------------------------------------

def _form_clusters(pool, cfg):
    eps = float(cfg['cluster_eps_ft'])
    core_n = int(cfg['core_cluster_n'])
    min_members = int(cfg['cluster_min_members'])

    cores, borders = [], []
    for c in pool:
        mid = _mid(c)
        if mid is None:
            continue
        if _cluster_n(c) >= core_n:
            cores.append((c, mid))
        else:
            borders.append((c, mid))
    if not cores:
        return []

    # union-find over cores within eps
    items = [(i, mid) for i, (_c, mid) in enumerate(cores)]
    uf = _UnionFind()
    for i, _p in items:
        uf.add(i)
    for i, j in _neighbors_within(items, eps):
        uf.union(i, j)

    comps = {}
    for i, (c, mid) in enumerate(cores):
        comps.setdefault(uf.find(i), []).append((c, mid))

    # Border attachment: within eps of exactly ONE component, measured
    # against the CORE-ONLY snapshot (DBSCAN semantics). Attachments
    # collect in a side dict and merge after the loop, so a border can
    # never chain through a previously attached border - chaining would
    # extend a component's reach past its core footprint and make the
    # outcome depend on pool iteration order.
    eps2 = eps * eps
    attached = {}
    for c, mid in borders:
        touching = set()
        for root, members in comps.items():
            for _mc, mmid in members:
                if _d2(mid, mmid) <= eps2:
                    touching.add(root)
                    break
            if len(touching) > 1:
                break
        if len(touching) == 1:
            attached.setdefault(touching.pop(), []).append((c, mid))
    for root, extra in attached.items():
        comps[root].extend(extra)

    groups = []
    # deterministic component order: by min member seq
    ordered = sorted(comps.values(),
                     key=lambda ms: min(m[0].get('seq') or 0 for m in ms))
    for members_pts in ordered:
        if len(members_pts) < min_members:
            continue
        fragments = _cut_oversize(members_pts, float(cfg['max_span_ft']),
                                  int(cfg['max_members']))
        for frag in fragments:
            if len(frag) < min_members:
                continue        # undersize offcut returns to the residue
            members = [c for c, _p in frag]
            groups.append(_new_group('cluster', members, anchor=None))
    return groups


# ---------------------------------------------------------------------------
# P5 - element-star formation
# ---------------------------------------------------------------------------

def _form_stars(pool, cfg):
    anchor_min = int(cfg['anchor_min'])
    seg_ft = float(cfg['anchor_span_segment_ft'])

    participation = {}
    for c in pool:
        for ref_key in ('ref_a', 'ref_b'):
            k = _elem_key(c.get(ref_key))
            if k is not None:
                participation[k] = participation.get(k, 0) + 1

    buckets = {}
    for c in pool:
        ka = _elem_key(c.get('ref_a'))
        kb = _elem_key(c.get('ref_b'))
        if ka is None and kb is None:
            continue
        if kb is None:
            anchor_key, anchor_ref = ka, c.get('ref_a')
        elif ka is None:
            anchor_key, anchor_ref = kb, c.get('ref_b')
        else:
            pa, pb = participation.get(ka, 0), participation.get(kb, 0)
            # higher participation anchors; deterministic tie-break on the
            # key string so the anchor never flips between equal counts
            if pa > pb or (pa == pb and ka <= kb):
                anchor_key, anchor_ref = ka, c.get('ref_a')
            else:
                anchor_key, anchor_ref = kb, c.get('ref_b')
        buckets.setdefault(anchor_key, ([], anchor_ref))[0].append(c)

    groups = []
    for key in sorted(buckets.keys()):
        members, anchor_ref = buckets[key]
        if len(members) < anchor_min:
            continue
        anchor = _anchor_from_ref(anchor_ref)
        # long-element bound: split into span segments
        pts = [(c, _mid(c)) for c in members]
        with_pts = [(c, p) for c, p in pts if p is not None]
        without_pts = [c for c, p in pts if p is None]
        segments = [with_pts]
        if with_pts:
            axis, extent = _dominant_axis([p for _c, p in with_pts])
            if extent > seg_ft:
                segments = _split_spans(with_pts, axis, seg_ft)
        first = True
        for seg in segments:
            seg_members = [c for c, _p in seg]
            if first:
                seg_members = seg_members + without_pts
                first = False
            if len(seg_members) < anchor_min:
                continue        # undersize segment returns to the residue
            groups.append(_new_group('element', seg_members, anchor=anchor))
    return groups


def _split_spans(members_pts, axis, seg_ft):
    ordered = sorted(members_pts, key=lambda cp: cp[1][axis])
    segments, current, start = [], [], None
    for c, p in ordered:
        if start is None:
            start = p[axis]
        if p[axis] - start > seg_ft:
            segments.append(current)
            current, start = [], p[axis]
        current.append((c, p))
    if current:
        segments.append(current)
    return segments


# ---------------------------------------------------------------------------
# P6 - spatial adjacency (suggest into curated, join untouched)
# ---------------------------------------------------------------------------

def _adjacency(groups, pool, by_id, member_of, cfg, summary):
    eps2 = float(cfg['cluster_eps_ft']) ** 2
    cluster_groups = [g for g in groups
                      if g.get('axis') == 'cluster'
                      and g.get('status') != 'MergedInto']
    if not cluster_groups:
        return
    open_mids = []
    for g in cluster_groups:
        mids = []
        for mid_id in g.get('member_ids') or []:
            c = by_id.get(mid_id)
            if c is not None and _is_open(c):
                p = _mid(c)
                if p is not None:
                    mids.append(p)
        open_mids.append((g, mids))

    for c in pool:
        cid = c.get('id')
        if not cid or cid in member_of:
            continue
        p = _mid(c)
        if p is None:
            continue
        # true nearest group: the minimum over ALL open members, so the
        # winner never depends on roster ordering
        best_g, best_d = None, None
        for g, mids in open_mids:
            for q in mids:
                d = _d2(p, q)
                if d <= eps2 and (best_d is None or d < best_d):
                    best_g, best_d = g, d
        if best_g is None:
            continue
        if _is_curated(best_g):
            sugg = best_g.setdefault('suggested_ids', [])
            if cid not in sugg:
                sugg.append(cid)
                summary['suggested'] += 1
        else:
            best_g.setdefault('member_ids', []).append(cid)
            member_of[cid] = best_g['id']
            _hist(best_g, 'member_joined', after=cid)
            summary['joined'] += 1


# ---------------------------------------------------------------------------
# P7 - lifecycle
# ---------------------------------------------------------------------------

def _lifecycle(g, by_id, run_iso):
    members = [by_id[m] for m in (g.get('member_ids') or []) if m in by_id]
    if not members:
        return
    n_open = len([c for c in members if _is_open(c)])
    status = g.get('status') or 'Open'
    if status != 'Resolved' and status != 'MergedInto' and n_open == 0:
        _hist(g, 'auto_resolved', before=status, after='Resolved')
        g['status'] = 'Resolved'
    elif status == 'Resolved' and n_open > 0:
        _hist(g, 'reopened', before='Resolved', after='Open')
        g['status'] = 'Open'
    elif status in ('Reviewed', 'Approved'):
        # A member reopening under a human-decided status must never be
        # silent ("a growing problem reads as settled"), but the human
        # status is also not ours to stomp: flag for review instead.
        reopened_now = any(_last_action_this_run(c, 'reopened', run_iso)
                           for c in members)
        if reopened_now and not g.get('needs_review'):
            g['needs_review'] = True
            _hist(g, 'needs_review',
                  before='member reopened while {0}'.format(status))


# ---------------------------------------------------------------------------
# Group construction / history
# ---------------------------------------------------------------------------

def _new_group(axis, members, anchor=None):
    best = None
    for c in members:
        score = ((c.get('importance') or {}).get('score')) or 0
        if best is None or score > (((best.get('importance') or {})
                                     .get('score')) or 0):
            best = c
    assignee = _modal([c.get('assignee') for c in members])
    created = _now_iso()
    g = {
        'id': str(uuid.uuid4()),
        'created_at': created,
        'created_by': 'system',
        'axis': axis,
        'anchor': anchor,
        'title': '',
        'title_locked': False,
        'status': 'Open',
        'assignee': assignee,
        'priority': None,
        'member_ids': [c['id'] for c in members],
        'suggested_ids': [],
        'needs_review': False,
        'lineage': {'split_from': None, 'merged_from': [], 'merged_into': None},
        'history': [],
        'comments': [],
        'rep_clash_id': best['id'] if best is not None else None,
        'rollup': {},
    }
    _hist(g, 'created', after='{0} members'.format(len(members)))
    return g


def _hist(g, action, before=None, after=None):
    try:
        from clash_core import models
        entry = models.make_history_entry('system', action,
                                          before=before, after=after)
    except Exception:
        entry = {'author': 'system', 'action': action,
                 'before': before, 'after': after, 'at': _now_iso()}
    g.setdefault('history', []).append(entry)


def _now_iso():
    try:
        from clash_core import models
        return models._now_iso()
    except Exception:
        import datetime
        return datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
