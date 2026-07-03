# -*- coding: utf-8 -*-
"""Pre-meeting agenda digest - a self-contained HTML handout.

This is what a coordinator emails the team ~48 hours before the weekly
meeting: the top open ISSUES (groups rolled up to their worst member, plus
any ungrouped Critical/Major singletons), ranked band then score, each with
its representative snapshot, one-line reason, owner, and open/total counts.
It is the "top 10-20 groups" agenda every research thread said the meeting
actually runs from - not a 5,000-row clash dump.

Distinct from html.py (the full per-clash report for the record) and from
the boss calibration form (which asks yes/no questions). This one just
presents the agenda, print-clean, with a per-owner breakdown so each trade
sees its share.

Pure Python - no Revit / WPF - so it runs in the CPython test suite.
Snapshots are embedded as base64 data URIs from <clash-id>.png files, so
the file is a single portable attachment.
"""

import base64
import codecs
import os


_BAND_ORDER = {'Critical': 0, 'Major': 1, 'Minor': 2}
_BAND_COLORS = {
    'Critical': ('#FCEBEB', '#791F1F'),
    'Major':    ('#FAEEDA', '#633806'),
    'Minor':    ('#F1EFE8', '#444441'),
}
_OPEN_STATUSES = ('Open', 'Reviewed')


def build_digest_html(groups, clashes, out_path, project_name=None,
                      viewpoints_dir=None, generated_by=None, now_iso=None,
                      top_n=20, include_thumbnails=True,
                      group_predicate=None, clash_predicate=None):
    """Write the agenda digest to `out_path`. Returns the number of agenda
    items rendered.

    Args:
        groups           - clashes.json `groups` list (may be []).
        clashes          - clashes.json `clashes` list.
        out_path         - destination .html.
        project_name     - header label.
        viewpoints_dir   - folder of <clash-id>.png snapshots (or None).
        generated_by     - "prepared by" name.
        now_iso          - timestamp; None uses current UTC (tests pass a
                           fixed value for determinism).
        top_n            - how many agenda items to show in the main list
                           (the rest are summarized as a tail count).
        include_thumbnails - embed snapshots as data URIs.
    """
    if now_iso is None:
        from datetime import datetime
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    by_id = {}
    for c in (clashes or []):
        if isinstance(c, dict) and c.get('id'):
            by_id[c['id']] = c

    all_items = _agenda_items(groups or [], clashes or [], by_id,
                              group_predicate, clash_predicate)
    # The MEETING agenda is Critical + Major work. Minor issues (sleeve
    # schedules, mounting checks) are batch passes, counted as a footer
    # note but never on the meeting list - the story is "your queue is the
    # agenda, not the schedules".
    items = [it for it in all_items if it['band'] in ('Critical', 'Major')]
    minor_count = len(all_items) - len(items)
    items.sort(key=_agenda_sort_key)

    owner_counts = {}
    band_counts = {'Critical': 0, 'Major': 0}
    for it in items:
        owner_counts[it['owner']] = owner_counts.get(it['owner'], 0) + 1
        if it['band'] in band_counts:
            band_counts[it['band']] += 1

    shown = items[:max(0, top_n)]
    tail = len(items) - len(shown)

    parts = [_HEAD]
    parts.append(_header(project_name, generated_by, now_iso, len(items),
                         minor_count))
    parts.append(_summary(band_counts, owner_counts))
    parts.append('<section class="agenda">')
    parts.append('<h2>Agenda &mdash; top {} issue(s)</h2>'.format(len(shown)))
    if not shown:
        parts.append('<div class="empty">No Critical or Major issues open. '
                     '{} batch item(s) (sleeves, mounting checks) remain in '
                     'the full report.</div>'.format(minor_count))
    for i, it in enumerate(shown):
        parts.append(_item_card(i + 1, it, viewpoints_dir, include_thumbnails))
    if tail > 0:
        parts.append('<div class="tail">&plus; {} more Critical/Major '
                     'issue(s) below the agenda line.</div>'.format(tail))
    if minor_count > 0:
        parts.append('<div class="tail">Plus {} Minor batch item(s) '
                     '(sleeve penetrations, mounting checks) handled as '
                     'schedules, not meeting items.</div>'.format(minor_count))
    parts.append('</section>')
    parts.append(_FOOT)

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with codecs.open(out_path, 'w', 'utf-8') as f:
        f.write(''.join(parts))
    return len(items)


# ---------------------------------------------------------------------------
# Agenda assembly
# ---------------------------------------------------------------------------

def _is_open(c):
    return (c.get('status') in _OPEN_STATUSES
            and not (c.get('importance') or {}).get('suppressed'))


def _agenda_items(groups, clashes, by_id, group_predicate=None,
                  clash_predicate=None):
    """Open groups (rolled up) + ungrouped open Critical/Major singletons.

    When a predicate is supplied (from the Reports filter), groups that
    fail it, and singletons that fail the clash predicate, are dropped -
    so the preview count and the emitted handout agree."""
    items = []
    grouped_ids = set()
    for g in groups:
        if not isinstance(g, dict) or g.get('status') in ('Resolved', 'MergedInto'):
            for m in (g.get('member_ids') or []):
                grouped_ids.add(m)
            continue
        members = [by_id[m] for m in (g.get('member_ids') or []) if m in by_id]
        for m in (g.get('member_ids') or []):
            grouped_ids.add(m)
        open_members = [c for c in members if _is_open(c)]
        if not open_members:
            continue
        if group_predicate is not None:
            try:
                if not group_predicate(g):
                    continue
            except Exception:
                continue
        rollup = g.get('rollup') or {}
        items.append({
            'title': g.get('title') or 'Coordination issue',
            'band': rollup.get('band') or 'Minor',
            'score': rollup.get('score') or 0,
            'reason': rollup.get('reason') or '',
            'owner': g.get('assignee') or 'Unassigned',
            'n_open': rollup.get('n_open') if rollup.get('n_open') is not None
                      else len(open_members),
            'n_total': rollup.get('n_total')
                       or len(g.get('member_ids') or []) or len(members),
            'n_new': rollup.get('n_new_run') or 0,
            'rep_id': g.get('rep_clash_id') or (open_members[0].get('id')),
            'members': open_members,
            'is_group': True,
        })

    for c in clashes:
        if not isinstance(c, dict) or c.get('id') in grouped_ids:
            continue
        if not _is_open(c):
            continue
        imp = c.get('importance') or {}
        if imp.get('band') not in ('Critical', 'Major'):
            continue
        if clash_predicate is not None:
            try:
                if not clash_predicate(c):
                    continue
            except Exception:
                continue
        a = (c.get('ref_a') or {}).get('name') or (c.get('ref_a') or {}).get('category') or '?'
        b = (c.get('ref_b') or {}).get('name') or (c.get('ref_b') or {}).get('category') or '?'
        items.append({
            'title': u"{} vs {}".format(a, b),
            'band': imp.get('band') or 'Minor',
            'score': imp.get('score') or 0,
            'reason': imp.get('reason') or '',
            'owner': c.get('assignee') or 'Unassigned',
            'n_open': 1, 'n_total': 1, 'n_new': 0,
            'rep_id': c.get('id'),
            'members': [c],
            'is_group': False,
        })
    return items


def _agenda_sort_key(it):
    return (_BAND_ORDER.get(it['band'], 3), -(it['score'] or 0),
            it['title'] or '')


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _header(project_name, generated_by, generated_at, n_items, minor_count):
    p = ['<header class="hdr">',
         '  <div class="title-block">',
         '    <div class="title">Coordination agenda</div>',
         '    <div class="subtitle">{}</div>'.format(_esc(project_name or 'Untitled project')),
         '  </div>',
         '  <div class="wordmark"><span class="wm">db</span>'
         '<span class="wd"> | </span><span class="wm">HMS</span></div>',
         '</header>',
         '<section class="meta">',
         '  <div><strong>Agenda issues (Critical + Major):</strong> {}</div>'.format(n_items),
         '  <div><strong>Batch items (Minor):</strong> {}</div>'.format(minor_count),
         '  <div><strong>Prepared:</strong> {}</div>'.format(_esc(generated_at))]
    if generated_by:
        p.append('  <div><strong>By:</strong> {}</div>'.format(_esc(generated_by)))
    p.append('</section>')
    return '\n'.join(p) + '\n'


def _summary(band_counts, owner_counts):
    p = ['<section class="summary">']
    p.append('<div class="sblock"><h3>Agenda by importance</h3><ul>')
    for band in ('Critical', 'Major'):
        bg, fg = _BAND_COLORS[band]
        p.append('<li><span class="chip" style="background:{};color:{}">{}</span>'
                 '<span class="c">{}</span></li>'.format(
                     bg, fg, band, band_counts.get(band, 0)))
    p.append('</ul></div>')
    p.append('<div class="sblock"><h3>By owner</h3><ul>')
    for owner, n in sorted(owner_counts.items(), key=lambda kv: -kv[1]):
        p.append('<li><span class="owner">{}</span><span class="c">{}</span></li>'.format(
            _esc(owner), n))
    p.append('</ul></div>')
    p.append('</section>')
    return '\n'.join(p)


def _item_card(rank, it, viewpoints_dir, include_thumbnails):
    bg, fg = _BAND_COLORS.get(it['band'], ('#EDF2F7', '#2D3748'))
    img = ''
    if include_thumbnails:
        uri = _thumb_uri(it['rep_id'], viewpoints_dir)
        if uri:
            img = '<img class="shot" src="{}" alt="issue view"/>'.format(uri)
    churn = ''
    if it['n_new']:
        churn = '<span class="new">+{} new</span>'.format(it['n_new'])
    p = ['<div class="card">',
         '  <div class="top">',
         '    <span class="rank">{}</span>'.format(rank),
         '    <span class="chip" style="background:{};color:{}">{}</span>'.format(bg, fg, it['band']),
         '    <span class="ttl">{}</span>'.format(_esc(it['title'])),
         '    <span class="owner-pill">{}</span>'.format(_esc(it['owner'])),
         '  </div>',
         '  <div class="sub">{} of {} open{}{}</div>'.format(
             it['n_open'], it['n_total'],
             ' &middot; issue' if it['is_group'] else ' &middot; single clash',
             ' ' + churn if churn else ''),
         ]
    if it['reason']:
        p.append('  <div class="reason">{}</div>'.format(_esc(it['reason'])))
    if img:
        p.append('  ' + img)
    p.append('</div>')
    return '\n'.join(p)


def _thumb_uri(clash_id, viewpoints_dir):
    if not clash_id or not viewpoints_dir:
        return None
    path = os.path.join(viewpoints_dir, '{}.png'.format(clash_id))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        if len(raw) > 1500000:      # keep the file email-able
            return None
        return 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')
    except Exception:
        return None


def _esc(s):
    # IronPython 2.7-safe: str(u'non-ascii') raises UnicodeEncodeError, and
    # Revit family/type names routinely carry en-dashes, degree/diameter
    # symbols, and accents. Stay in unicode throughout (the ASCII bytes-str
    # templates promote cleanly, and codecs.open('utf-8') handles the write).
    if s is None:
        v = u''
    elif isinstance(s, bytes):
        v = s.decode('utf-8', 'replace')
    else:
        v = u'{0}'.format(s)
    return (v.replace(u'&', u'&amp;').replace(u'<', u'&lt;')
            .replace(u'>', u'&gt;').replace(u'"', u'&quot;'))


_HEAD = u"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<title>Coordination agenda</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  * { box-sizing: border-box; margin: 0; }
  body { font-family: 'Segoe UI', sans-serif; font-size: 14px; color: #2D3748; background: #F7FAFC; }
  .hdr { background: #2D3748; color: white; padding: 18px 24px; display: flex; justify-content: space-between; align-items: center; }
  .title { font-size: 20px; font-weight: bold; }
  .subtitle { color: #CBD5E0; font-size: 12px; margin-top: 3px; }
  .wordmark .wm { color: #00BFFF; font-size: 26px; font-weight: bold; }
  .wordmark .wd { color: #7A8FA6; font-size: 26px; }
  .meta { display: flex; gap: 22px; padding: 12px 24px; background: white; border-bottom: 1px solid #E2E8F0; font-size: 13px; }
  .summary { display: flex; gap: 16px; padding: 16px 24px; }
  .sblock { background: white; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px 16px; min-width: 220px; }
  .sblock h3 { font-size: 13px; margin-bottom: 8px; color: #4A5568; }
  .sblock ul { list-style: none; }
  .sblock li { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .sblock .c { margin-left: auto; font-weight: 600; }
  .agenda { padding: 0 24px 32px; max-width: 900px; }
  .agenda h2 { font-size: 16px; margin: 8px 0 14px; }
  .chip { padding: 2px 9px; border-radius: 10px; font-size: 12px; font-weight: 600; }
  .card { background: white; border: 1px solid #E2E8F0; border-radius: 6px; padding: 14px; margin-bottom: 12px; page-break-inside: avoid; }
  .card .top { display: flex; align-items: center; gap: 10px; }
  .rank { font-weight: 700; color: #A0AEC0; min-width: 22px; }
  .ttl { font-weight: 600; flex: 1; }
  .owner-pill { background: #EDF2F7; color: #2D3748; border-radius: 10px; padding: 2px 10px; font-size: 12px; }
  .sub { color: #718096; font-size: 12px; margin: 6px 0 0 32px; }
  .new { color: #2F855A; font-weight: 600; }
  .reason { background: #F7FAFC; border: 1px solid #E2E8F0; border-radius: 4px; padding: 8px 10px; margin: 8px 0 0 32px; font-size: 13px; }
  .shot { display: block; max-width: 100%; border: 1px solid #E2E8F0; border-radius: 4px; margin: 10px 0 0 32px; }
  .owner { }
  .tail { color: #718096; font-style: italic; padding: 8px 0; }
  .empty { color: #A0AEC0; padding: 20px 0; }
  @media print { body { background: white; } .summary, .card { break-inside: avoid; } }
</style></head><body>
"""

_FOOT = u"\n</body></html>\n"
