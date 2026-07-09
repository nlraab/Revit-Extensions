# -*- coding: utf-8 -*-
"""Shared report data model - band-aware summary + normalized rows.

One source of truth for the numbers every export puts on paper (Excel,
HTML, PDF) AND the numbers the Reports tab shows live on screen, so they
can never disagree. Kept deliberately small and pure: no Revit, no WPF,
no I/O. Runs identically under IronPython 2.7 (Revit) and CPython 3 (the
test suite).

The band thresholds MUST match `coord.html`'s `band()` (70 / 40) and the
scoring engine's `defaults.py` cutoffs. They are duplicated here on
purpose (the README documents the four other JS sites); if the firm ever
moves a cutoff, it moves in every one of them.
"""


# Band cutoffs - mirror coord.html band() and clash_score defaults.
CRITICAL_MIN = 70
MAJOR_MIN = 40

BANDS = ('Critical', 'Major', 'Minor')
STATUSES = ('Open', 'Reviewed', 'Approved', 'Resolved')

# "Open" work = still needs a decision; "Closed" = decided (Approved or
# Resolved). Matches the Browser's status semantics.
OPEN_STATUSES = ('Open', 'Reviewed')
CLOSED_STATUSES = ('Approved', 'Resolved')


# ---------------------------------------------------------------------------
# Per-clash accessors (defensive: clashes.json rows can be sparse / legacy)
# ---------------------------------------------------------------------------

def _imp(clash):
    return (clash.get('importance') or {}) if isinstance(clash, dict) else {}


def score_of(clash):
    """Integer importance score (0-99). Falls back to 0 when unscored."""
    imp = _imp(clash)
    s = imp.get('score')
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return 0


def band_of(clash):
    """'Critical' / 'Major' / 'Minor'. Prefers the engine-stamped band,
    then derives from the score against the frozen cutoffs."""
    imp = _imp(clash)
    b = imp.get('band')
    if b in BANDS:
        return b
    s = score_of(clash)
    if s >= CRITICAL_MIN:
        return 'Critical'
    if s >= MAJOR_MIN:
        return 'Major'
    return 'Minor'


def is_suppressed(clash):
    return bool(_imp(clash).get('suppressed'))


def status_of(clash):
    st = clash.get('status') if isinstance(clash, dict) else None
    return st if st in STATUSES else (st or 'Open')


def trade_of(clash):
    """The assignee/trade, or a stable placeholder for unassigned rows."""
    t = clash.get('assignee') if isinstance(clash, dict) else None
    return t or '(unassigned)'


def pair_of(clash):
    """'A / B' discipline label - identical logic to script.py _pair_label
    so the report matches the Home tab's 'Open by discipline pair'."""
    a = clash.get('ref_a') or {}
    b = clash.get('ref_b') or {}

    def side(ref):
        src = ref.get('source') or 'host'
        if src.startswith('link:'):
            return src.split(':', 1)[1]
        return ref.get('category') or 'Host'
    try:
        return u'{0} / {1}'.format(side(a), side(b))
    except Exception:
        return ''


def reason_of(clash):
    """One-line 'why it ranks' text: the crisp headline, else the full
    composed reason, else empty."""
    imp = _imp(clash)
    return imp.get('headline') or imp.get('reason') or ''


def test_name_of(clash, test_lookup=None):
    test_lookup = test_lookup or {}
    tid = clash.get('test_id')
    return (clash.get('test_name')
            or test_lookup.get(tid)
            or tid
            or '(unknown test)')


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

def clean(clashes):
    """Drop non-dict / falsy entries from a raw clashes list."""
    return [c for c in (clashes or []) if isinstance(c, dict)]


def select_by_ids(clashes, ids):
    """Keep only clashes whose id is in `ids` (a list/set). None -> all.

    Preserves the input order so an export honors the page's sort.
    """
    clashes = clean(clashes)
    if ids is None:
        return clashes
    wanted = set(ids)
    return [c for c in clashes if c.get('id') in wanted]


# ---------------------------------------------------------------------------
# Summary (the coordination snapshot - cover page of every report)
# ---------------------------------------------------------------------------

def summarize(clashes, groups=None, include_suppressed=False):
    """Roll a clash list up into the report cover numbers.

    By default suppressed (engine-filtered noise) rows are excluded from
    every count, exactly like the Browser's default view. Returns a plain
    dict (JSON-friendly) so the same shape can be posted to the page.
    """
    clashes = clean(clashes)
    if not include_suppressed:
        rows = [c for c in clashes if not is_suppressed(c)]
    else:
        rows = clashes
    suppressed_n = sum(1 for c in clashes if is_suppressed(c))

    by_band = {'Critical': 0, 'Major': 0, 'Minor': 0}
    by_status = {'Open': 0, 'Reviewed': 0, 'Approved': 0, 'Resolved': 0}
    by_trade = {}
    by_pair = {}
    # Open (still-actionable) band split, for the "what's left" headline.
    open_band = {'Critical': 0, 'Major': 0, 'Minor': 0}

    newest_run = None
    for c in rows:
        fs = c.get('first_seen_run')
        if fs and (newest_run is None or fs > newest_run):
            newest_run = fs

    new_count = 0
    for c in rows:
        b = band_of(c)
        st = status_of(c)
        by_band[b] = by_band.get(b, 0) + 1
        if st in by_status:
            by_status[st] += 1
        else:
            by_status[st] = by_status.get(st, 0) + 1
        t = trade_of(c)
        by_trade[t] = by_trade.get(t, 0) + 1
        p = pair_of(c)
        if p:
            by_pair[p] = by_pair.get(p, 0) + 1
        if st in OPEN_STATUSES:
            open_band[b] = open_band.get(b, 0) + 1
        if newest_run and c.get('first_seen_run') == newest_run:
            new_count += 1

    total = len(rows)
    open_n = by_status.get('Open', 0) + by_status.get('Reviewed', 0)
    closed_n = by_status.get('Approved', 0) + by_status.get('Resolved', 0)
    resolved_n = by_status.get('Resolved', 0)
    pct_closed = int(round(100.0 * closed_n / total)) if total else 0
    pct_resolved = int(round(100.0 * resolved_n / total)) if total else 0

    # Issues (groups): open = has >=1 open unsuppressed member. Cheap proxy
    # via the stored rollup when present, else member roster length.
    groups = [g for g in (groups or []) if isinstance(g, dict)]
    open_issues = 0
    for g in groups:
        if g.get('status') in ('Resolved', 'MergedInto'):
            continue
        r = g.get('rollup') or {}
        n_open = r.get('n_open')
        if n_open is None:
            n_open = len(g.get('member_ids') or [])
        if n_open > 0:
            open_issues += 1

    return {
        'total': total,
        'suppressed': suppressed_n,
        'open': open_n,
        'closed': closed_n,
        'resolved': resolved_n,
        'pct_closed': pct_closed,
        'pct_resolved': pct_resolved,
        'by_band': by_band,
        'open_band': open_band,
        'by_status': by_status,
        'by_trade': _sorted_pairs(by_trade),
        'by_pair': _sorted_pairs(by_pair),
        'newest_run': newest_run,
        'new_count': new_count,
        'issues_total': len(groups),
        'issues_open': open_issues,
    }


def _sorted_pairs(counts):
    """dict -> list of {label, count}, highest count first, label as
    tiebreak (stable, deterministic across runs and interpreters)."""
    items = list(counts.items())
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return [{'label': k, 'count': v} for k, v in items]


# ---------------------------------------------------------------------------
# Normalized rows (the report table body)
# ---------------------------------------------------------------------------

def report_rows(clashes, test_lookup=None):
    """Project each clash into a flat, display-ready dict. Order preserved."""
    out = []
    for c in clean(clashes):
        out.append(row_for(c, test_lookup))
    return out


def row_for(clash, test_lookup=None):
    a = clash.get('ref_a') or {}
    b = clash.get('ref_b') or {}
    mid = clash.get('midpoint') or [None, None, None]
    comments = clash.get('comments') or []
    latest = comments[-1] if comments else {}
    return {
        'id': clash.get('id') or '',
        'seq': clash.get('seq'),
        'band': band_of(clash),
        'score': score_of(clash),
        'status': status_of(clash),
        'trade': clash.get('assignee') or '',
        'suppressed': is_suppressed(clash),
        'group_id': clash.get('group_id') or '',
        'test': test_name_of(clash, test_lookup),
        'kind': clash.get('kind') or 'hard',
        'reason': reason_of(clash),
        'pair': pair_of(clash),
        'a_name': a.get('name') or a.get('category') or '',
        'a_id': a.get('element_id'),
        'a_cat': a.get('category') or '',
        'a_src': a.get('source') or 'host',
        'b_name': b.get('name') or b.get('category') or '',
        'b_id': b.get('element_id'),
        'b_cat': b.get('category') or '',
        'b_src': b.get('source') or 'host',
        'level': a.get('level') or b.get('level') or '',
        'x': _coord(mid[0] if len(mid) > 0 else None),
        'y': _coord(mid[1] if len(mid) > 1 else None),
        'z': _coord(mid[2] if len(mid) > 2 else None),
        'first_seen': clash.get('first_seen_run') or '',
        'last_seen': clash.get('last_seen_run') or '',
        'comment_count': len(comments),
        'latest_comment': latest.get('body') or '',
        'latest_comment_author': latest.get('author') or '',
    }


def _coord(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
