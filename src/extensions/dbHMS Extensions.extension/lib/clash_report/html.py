# -*- coding: utf-8 -*-
"""Interactive, band-aware clash report - one self-contained HTML file.

Single file with inline CSS, inline JS, and base64-embedded thumbnails,
so it emails / posts to SharePoint / opens from a share with no external
assets. Two audiences, one document:

  * On screen it is INTERACTIVE - a filter toolbar (by importance band,
    by trade, by status, plus free-text search) slices the clash list
    live, the way the recipient would want to explore it in a browser.
  * On paper it is a clean REPORT - an `@media print` block hides the
    toolbar and forces backgrounds to print, so "Save as PDF" (or the
    host's silent WebView2 print) produces a tidy per-clash document.
    That is why this module also underpins the PDF export: render once,
    print the same HTML.

Band / score / the one-line "why" reason are first-class here - they are
the tool's headline output and appear on every clash. The numbers on the
cover come from `report_model.summarize`, the SAME function the Reports
tab uses on screen, so the printed cover and the live tab never disagree.

Pure data - no Revit, no WPF. Parses and runs under both IronPython 2.7
(pyRevit) and CPython 3 (the test suite). The host passes the branding
logo as a base64 data URI (`logo_data_uri`) because a pure-data module
must not reach into a pushbutton folder for a PNG.
"""

import base64
import codecs
import os

from . import report_model as rm


SCHEMA_VERSION = 2


# Importance band colors - (pill-bg, pill-fg, solid-accent). Match
# coord.html band() so the report reads like the app.
_BAND_COLORS = {
    'Critical': ('#FCEBEB', '#791F1F', '#C53030'),
    'Major':    ('#FAEEDA', '#633806', '#DD8A0B'),
    'Minor':    ('#F1EFE8', '#444441', '#A0AEC0'),
}

# Status pill colors - match coord.html STATUS_COLORS.
_STATUS_COLORS = {
    'Open':     ('#FCEBEB', '#791F1F'),
    'Reviewed': ('#FAEEDA', '#633806'),
    'Approved': ('#E6F1FB', '#0C447C'),
    'Resolved': ('#EAF3DE', '#27500A'),
}

# Trade pill colors - (bg, fg).
_TRADE_COLORS = {
    'Mechanical':      ('#DBE5EE', '#3D5A75'),
    'Electrical':      ('#EDE2C6', '#7A5C1F'),
    'Plumbing':        ('#D5E5E0', '#3F6D6B'),
    'Fire Protection': ('#EAD2D2', '#7A4040'),
    'Technology':      ('#E0D5E8', '#5E4878'),
    'Architectural':   ('#E5E0D5', '#605A4D'),
    'Structural':      ('#D8DCE0', '#3E454F'),
}
_TRADE_DEFAULT_BG = '#EDF2F7'
_TRADE_DEFAULT_FG = '#4A5568'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_html(clashes, out_path, filter_predicate=None,
               project_name=None, viewpoints_dir=None,
               generated_by=None, filter_description=None,
               include_thumbnails=True, test_name_lookup=None,
               now_iso=None, groups=None, interactive=True,
               logo_data_uri=None):
    """Write a self-contained clash report to `out_path`. Returns the
    count of clashes written (after filtering).

    Backward-compatible with the original signature; new optional args:
        groups          - clashes.json `groups` list, for the issue count.
        interactive     - embed the filter toolbar + JS (default True).
                          Pass False for a pure static print source.
        logo_data_uri   - base64 data URI of the dbHMS logo for the
                          header. None renders a text wordmark instead.
    """
    if test_name_lookup is None:
        test_name_lookup = {}
    if now_iso is None:
        from datetime import datetime
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    filtered = []
    for c in clashes or []:
        if not isinstance(c, dict):
            continue
        if filter_predicate is not None:
            try:
                if not filter_predicate(c):
                    continue
            except Exception:
                continue
        filtered.append(c)

    summary = rm.summarize(filtered, groups=groups, include_suppressed=True)

    body = _render_document(
        clashes=filtered,
        summary=summary,
        project_name=project_name,
        generated_by=generated_by,
        generated_at=now_iso,
        filter_description=filter_description,
        viewpoints_dir=viewpoints_dir,
        include_thumbnails=include_thumbnails,
        test_name_lookup=test_name_lookup,
        interactive=interactive,
        logo_data_uri=logo_data_uri,
    )

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    tmp_path = out_path + '.tmp'
    try:
        with codecs.open(tmp_path, "w", "utf-8") as f:
            f.write(body)
        if os.path.exists(out_path):
            os.remove(out_path)
        os.rename(tmp_path, out_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise
    return len(filtered)


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

def _render_document(clashes, summary, project_name, generated_by,
                     generated_at, filter_description, viewpoints_dir,
                     include_thumbnails, test_name_lookup, interactive,
                     logo_data_uri):
    parts = []
    parts.append(_HEAD)
    parts.append(_render_header(project_name, generated_by, generated_at,
                                filter_description, summary, logo_data_uri))
    parts.append(_render_summary(summary))
    if interactive and clashes:
        parts.append(_render_toolbar(clashes, len(clashes)))
    parts.append('<section class="clashes" id="clashlist">')
    if not clashes:
        parts.append('<div class="empty">No clashes match the selected '
                     'filter.</div>')
    for c in clashes:
        parts.append(_render_clash(c, viewpoints_dir, include_thumbnails,
                                   test_name_lookup))
    parts.append('</section>')
    parts.append(_render_footer(generated_at))
    if interactive and clashes:
        parts.append(_SCRIPT)
    parts.append('</body></html>')
    return ''.join(parts)


def _render_header(project_name, generated_by, generated_at,
                   filter_description, summary, logo_data_uri):
    if logo_data_uri:
        brand = ('<img class="logo" alt="dbHMS" src="{0}"/>'
                 .format(_esc(logo_data_uri)))
    else:
        brand = ('<div class="wordmark"><span class="wm-db">db</span>'
                 '<span class="wm-hms">HMS</span></div>')
    p = []
    p.append('<header class="hdr">')
    p.append('  <div class="hdr-dots"></div>')
    p.append('  <div class="hdr-inner">')
    p.append('    <div class="title-block">')
    p.append('      <div class="title">Clash Report</div>')
    p.append('      <div class="subtitle">{0}</div>'.format(
        _esc(project_name or 'Untitled project')))
    p.append('    </div>')
    p.append('    ' + brand)
    p.append('  </div>')
    p.append('</header>')
    # Metadata strip.
    p.append('<section class="meta">')
    p.append('  <div><strong>Total clashes:</strong> <b>{0}</b></div>'.format(
        summary.get('total', 0)))
    if filter_description:
        p.append('  <div><strong>Scope:</strong> {0}</div>'.format(
            _esc(filter_description)))
    p.append('  <div><strong>Generated:</strong> {0}</div>'.format(
        _esc(_short_when(generated_at) or generated_at)))
    if generated_by:
        p.append('  <div><strong>By:</strong> {0}</div>'.format(
            _esc(generated_by)))
    p.append('</section>')
    return '\n'.join(p) + '\n'


def _render_summary(summary):
    total = summary.get('total', 0)
    by_band = summary.get('by_band', {})
    open_band = summary.get('open_band', {})
    crit = by_band.get('Critical', 0)
    maj = by_band.get('Major', 0)
    minr = by_band.get('Minor', 0)

    p = []
    p.append('<section class="cover">')

    # Stat cards.
    p.append('  <div class="stats">')
    p.append(_stat_card('Total', total, '#2B6CB0', 'clashes in scope'))
    p.append(_stat_card('Open', summary.get('open', 0), '#C53030',
                        'need a decision'))
    p.append(_stat_card('Closed', summary.get('closed', 0), '#2F855A',
                        '{0}% resolved or approved'.format(
                            summary.get('pct_closed', 0))))
    p.append(_stat_card('Critical', crit, _BAND_COLORS['Critical'][2],
                        '{0} still open'.format(open_band.get('Critical', 0))))
    p.append(_stat_card('Major', maj, _BAND_COLORS['Major'][2],
                        '{0} still open'.format(open_band.get('Major', 0))))
    p.append(_stat_card('Minor', minr, _BAND_COLORS['Minor'][2],
                        '{0} still open'.format(open_band.get('Minor', 0))))
    p.append('  </div>')

    # Importance distribution bar.
    p.append('  <div class="cover-row">')
    p.append('    <div class="panel">')
    p.append('      <h2>Importance mix</h2>')
    p.append(_stacked_bar([
        ('Critical', crit, _BAND_COLORS['Critical'][2]),
        ('Major', maj, _BAND_COLORS['Major'][2]),
        ('Minor', minr, _BAND_COLORS['Minor'][2]),
    ], total))
    # Status chips under the bar.
    p.append('      <div class="status-chips">')
    for st in ('Open', 'Reviewed', 'Approved', 'Resolved'):
        n = summary.get('by_status', {}).get(st, 0)
        bg, fg = _STATUS_COLORS.get(st, ('#EDF2F7', '#4A5568'))
        p.append('        <span class="chip" style="background:{0};color:{1}">'
                 '{2} <b>{3}</b></span>'.format(bg, fg, _esc(st), n))
    p.append('      </div>')
    p.append('    </div>')

    # By trade.
    p.append('    <div class="panel">')
    p.append('      <h2>By trade</h2>')
    p.append(_bar_list(summary.get('by_trade', []), _trade_color))
    p.append('    </div>')

    # By discipline pair.
    p.append('    <div class="panel">')
    p.append('      <h2>By discipline pair</h2>')
    p.append(_bar_list(summary.get('by_pair', [])[:8], None))
    p.append('    </div>')
    p.append('  </div>')

    if summary.get('new_count'):
        p.append('  <div class="note">{0} clash(es) are new since the '
                 'latest run.</div>'.format(summary['new_count']))
    p.append('</section>')
    return '\n'.join(p) + '\n'


def _stat_card(label, value, accent, sub):
    return ('    <div class="stat" style="border-top:3px solid {0}">'
            '<div class="stat-l">{1}</div>'
            '<div class="stat-v">{2}</div>'
            '<div class="stat-d">{3}</div></div>'.format(
                accent, _esc(label), value, _esc(sub)))


def _stacked_bar(segments, total):
    total = max(1, total)
    cells = []
    legend = []
    for label, n, color in segments:
        if n <= 0:
            continue
        pct = 100.0 * n / total
        cells.append('<span class="seg" style="width:{0:.1f}%;background:{1}" '
                     'title="{2}: {3}"></span>'.format(pct, color, _esc(label), n))
        legend.append('<span class="lg"><span class="sw" style="background:{0}">'
                      '</span>{1} <b>{2}</b></span>'.format(
                          color, _esc(label), n))
    if not cells:
        cells.append('<span class="seg" style="width:100%;background:#EDF2F7">'
                     '</span>')
    return ('      <div class="sbar">{0}</div>\n'
            '      <div class="legend">{1}</div>'.format(
                ''.join(cells), ''.join(legend)))


def _bar_list(items, color_fn):
    if not items:
        return '      <div class="muted">(none)</div>'
    top = max([1] + [it['count'] for it in items])
    rows = []
    for it in items:
        label = it['label']
        n = it['count']
        pct = 100.0 * n / top
        if color_fn:
            bg = color_fn(label)[0]
        else:
            bg = '#2B6CB0'
        rows.append(
            '      <div class="bl"><span class="bl-l" title="{0}">{0}</span>'
            '<span class="bl-t"><span class="bl-f" style="width:{1:.1f}%;'
            'background:{2}"></span></span><span class="bl-n">{3}</span></div>'
            .format(_esc(label), pct, bg, n))
    return '\n'.join(rows)


def _render_toolbar(clashes, total):
    trades = _distinct([rm.trade_of(c) for c in clashes])
    statuses = [s for s in ('Open', 'Reviewed', 'Approved', 'Resolved')
                if any(rm.status_of(c) == s for c in clashes)]
    p = []
    p.append('<section class="toolbar" id="toolbar">')
    p.append('  <div class="tb-row">')
    p.append('    <span class="tb-label">Importance</span>')
    for b in ('Critical', 'Major', 'Minor'):
        bg, fg, _s = _BAND_COLORS[b]
        p.append('    <button class="tgl on" data-kind="band" data-v="{0}" '
                 'style="background:{1};color:{2}">{0}</button>'.format(
                     _esc(b), bg, fg))
    p.append('  </div>')
    if len(statuses) > 1:
        p.append('  <div class="tb-row">')
        p.append('    <span class="tb-label">Status</span>')
        for s in statuses:
            bg, fg = _STATUS_COLORS.get(s, ('#EDF2F7', '#4A5568'))
            p.append('    <button class="tgl on" data-kind="status" '
                     'data-v="{0}" style="background:{1};color:{2}">{0}'
                     '</button>'.format(_esc(s), bg, fg))
        p.append('  </div>')
    if len(trades) > 1:
        p.append('  <div class="tb-row">')
        p.append('    <span class="tb-label">Trade</span>')
        for t in trades:
            bg, fg = _trade_color(t)
            p.append('    <button class="tgl on" data-kind="trade" '
                     'data-v="{0}" style="background:{1};color:{2}">{0}'
                     '</button>'.format(_esc(t), bg, fg))
        p.append('  </div>')
    p.append('  <div class="tb-row">')
    p.append('    <input class="tb-search" id="tbsearch" type="text" '
             'placeholder="Search element, test, comment, ID..."/>')
    p.append('    <span class="tb-count" id="tbcount">Showing {0} of {0}'
             '</span>'.format(total))
    p.append('  </div>')
    p.append('</section>')
    return '\n'.join(p) + '\n'


def _render_clash(clash, viewpoints_dir, include_thumbnails, test_name_lookup):
    row = rm.row_for(clash, test_name_lookup)
    seq = row['seq'] if row['seq'] is not None else '?'
    band = row['band']
    score = row['score']
    status = row['status']
    trade = row['trade'] or 'Unassigned'
    b_bg, b_fg, b_solid = _BAND_COLORS.get(band, _BAND_COLORS['Minor'])
    s_bg, s_fg = _STATUS_COLORS.get(status, ('#EDF2F7', '#4A5568'))
    t_bg, t_fg = _trade_color(trade)

    comments = clash.get('comments') or []
    history = clash.get('history') or []

    # Search haystack + filter attributes (lowercased).
    hay = ' '.join([str(x) for x in [
        seq, row['a_name'], row['b_name'], row['a_cat'], row['b_cat'],
        row['test'], row['reason'], row['pair'], trade, status,
        row['a_id'], row['b_id'],
    ] if x]).lower()

    img_html = ''
    if include_thumbnails and viewpoints_dir:
        uri = _read_thumbnail_data_uri(clash.get('id'), viewpoints_dir)
        if uri:
            img_html = ('<div class="thumb"><img alt="Clash #{0}" src="{1}"/>'
                        '</div>'.format(_esc(seq), uri))

    p = []
    p.append('<article class="clash" data-band="{0}" data-status="{1}" '
             'data-trade="{2}" data-text="{3}">'.format(
                 _esc(band), _esc(status), _esc(trade), _esc(hay)))
    # Head line.
    p.append('  <div class="c-head">')
    p.append('    <span class="c-num">#{0}</span>'.format(_esc(seq)))
    p.append('    <span class="band-chip" style="background:{0};color:{1}">'
             '{2} {3}</span>'.format(b_bg, b_fg, score, _esc(band)))
    p.append('    <span class="pill" style="background:{0};color:{1}">{2}'
             '</span>'.format(s_bg, s_fg, _esc(status)))
    p.append('    <span class="pill" style="background:{0};color:{1}">{2}'
             '</span>'.format(t_bg, t_fg, _esc(trade)))
    p.append('    <span class="c-kind">{0}</span>'.format(
        _esc((row['kind'] or 'hard').upper())))
    p.append('    <span class="c-pair">{0}</span>'.format(_esc(row['pair'])))
    p.append('  </div>')
    # Reason.
    if row['reason']:
        p.append('  <div class="c-reason">{0}</div>'.format(
            _esc(row['reason'])))
    # Body: thumb + element table.
    p.append('  <div class="c-body">')
    if img_html:
        p.append('    ' + img_html)
    p.append('    <div class="c-elems">')
    p.append('      <div class="test-name">{0}</div>'.format(
        _esc(row['test'])))
    p.append(_elem_line('A', row['a_name'], row['a_cat'], row['a_id'],
                        row['a_src']))
    p.append(_elem_line('B', row['b_name'], row['b_cat'], row['b_id'],
                        row['b_src']))
    loc = []
    if row['level']:
        loc.append('Level: {0}'.format(row['level']))
    if row['x'] is not None:
        loc.append('Location ft: {0}, {1}, {2}'.format(
            row['x'], row['y'], row['z']))
    if loc:
        p.append('      <div class="c-loc mono">{0}</div>'.format(
            _esc('   '.join(loc))))
    p.append('    </div>')
    p.append('  </div>')

    if comments:
        p.append('  <div class="c-comments">')
        p.append('    <h3>Comments ({0})</h3>'.format(len(comments)))
        for cm in comments:
            p.append('    <div class="comment"><span class="cm-when mono">{0}'
                     '</span> <span class="cm-author">{1}</span>'
                     '<div class="cm-body">{2}</div></div>'.format(
                         _esc(_short_when(cm.get('at'))),
                         _esc(cm.get('author') or 'unknown'),
                         _esc(cm.get('body') or '')))
        p.append('  </div>')

    if history:
        p.append('  <div class="c-history">')
        p.append('    <h3>History ({0})</h3>'.format(len(history)))
        p.append('    <ul>')
        for h in history:
            p.append('      <li><span class="h-when mono">{0}</span> '
                     '<span class="h-author">{1}</span> '
                     '<span class="h-action">{2}</span></li>'.format(
                         _esc(_short_when(h.get('at'))),
                         _esc(h.get('author') or 'unknown'),
                         _esc(_format_history_action(h))))
        p.append('    </ul>')
        p.append('  </div>')

    p.append('</article>')
    return '\n'.join(p) + '\n'


def _elem_line(letter, name, cat, eid, src):
    bits = []
    if cat:
        bits.append(cat)
    if eid not in (None, ''):
        bits.append('id {0}'.format(eid))
    if src:
        bits.append(src)
    meta = _esc('  -  '.join(bits))
    return ('      <div class="elem"><span class="elem-tag">{0}</span>'
            '<span class="elem-name">{1}</span>'
            '<span class="elem-meta mono">{2}</span></div>'.format(
                letter, _esc(name or 'unnamed'), meta))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _trade_color(trade):
    return _TRADE_COLORS.get(trade, (_TRADE_DEFAULT_BG, _TRADE_DEFAULT_FG))


def _distinct(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _format_history_action(h):
    action = h.get('action') or ''
    before = h.get('before')
    after = h.get('after')
    if before is not None and after is not None:
        return u'{0}: {1} → {2}'.format(action, before, after)
    return action


def _short_when(iso_str):
    if not iso_str:
        return ''
    s = str(iso_str)
    if 'T' not in s:
        return s
    date_part, time_part = s.split('T', 1)
    if '.' in time_part:
        time_part = time_part.split('.', 1)[0]
    if time_part.endswith('Z'):
        time_part = time_part[:-1]
    return '{0} {1}'.format(date_part, time_part[:5])


def _read_thumbnail_data_uri(clash_id, viewpoints_dir):
    """Per-clash snapshot as a data URI. Prefers .jpg (current), falls
    back to .png (legacy). '' when missing/unreadable."""
    if not clash_id or not viewpoints_dir:
        return ''
    for ext, mime in (('.jpg', 'image/jpeg'), ('.png', 'image/png')):
        path = os.path.join(viewpoints_dir, '{0}{1}'.format(clash_id, ext))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return ''
        encoded = base64.b64encode(data)
        if isinstance(encoded, bytes):
            encoded = encoded.decode('ascii')
        return 'data:{0};base64,{1}'.format(mime, encoded)
    return ''


def _esc(s):
    """HTML-escape. Portable across IronPython 2.7 and CPython 3."""
    return (str(s if s is not None else '')
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _render_footer(generated_at):
    return ('<footer class="foot">Generated by the dbHMS Clash Detection '
            'tool &middot; {0}</footer>\n'.format(
                _esc(_short_when(generated_at) or generated_at)))


# ---------------------------------------------------------------------------
# Static <head> (CSS) - kept as one raw block; NEVER run through .format()
# because the CSS braces would break it.
# ---------------------------------------------------------------------------

_HEAD = u"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>dbHMS Clash Report</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: #F7FAFC;
    font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 13px; color: #2D3748; line-height: 1.45;
  }
  .hdr {
    position: relative; overflow: hidden;
    background: linear-gradient(120deg, #143257 0%, #0F2748 55%, #0B1D34 100%);
    border-bottom: 3px solid #EE8A34; color: #F2F7FC;
  }
  .hdr-dots {
    position: absolute; inset: 0;
    background-image: radial-gradient(rgba(214,236,255,0.15) 1.1px, transparent 1.2px);
    background-size: 16px 16px;
    -webkit-mask-image: linear-gradient(#000, transparent);
    mask-image: linear-gradient(#000, transparent);
  }
  .hdr-inner {
    position: relative; padding: 22px 32px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .title-block .title { font-size: 24px; font-weight: 700; }
  .title-block .subtitle { font-size: 13px; color: #A9C2D8; margin-top: 3px; }
  .logo { height: 40px; }
  .wordmark { font-size: 30px; font-weight: 800; letter-spacing: .5px; }
  .wordmark .wm-db { color: #00B4EF; }
  .wordmark .wm-hms { color: #F2F7FC; }
  .meta {
    padding: 12px 32px; background: white; border-bottom: 1px solid #E2E8F0;
    display: flex; flex-wrap: wrap; gap: 4px 24px; color: #4A5568;
  }
  .meta strong { color: #1A365D; font-weight: 600; }
  .cover { padding: 18px 32px; background: white; border-bottom: 1px solid #E2E8F0; }
  .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
  .stat {
    flex: 1 1 0; min-width: 100px; background: #F7FAFC; border: 1px solid #E2E8F0;
    border-radius: 6px; padding: 10px 12px;
  }
  .stat-l { font-size: 11px; font-weight: 600; color: #4A5568; text-transform: uppercase; letter-spacing: .5px; }
  .stat-v { font-size: 26px; font-weight: 700; color: #1A365D; line-height: 1.1; }
  .stat-d { font-size: 11px; color: #718096; }
  .cover-row { display: flex; gap: 24px; flex-wrap: wrap; }
  .panel { flex: 1; min-width: 220px; }
  .panel h2 {
    font-size: 12px; font-weight: 600; color: #4A5568; margin: 0 0 8px;
    text-transform: uppercase; letter-spacing: .6px;
  }
  .sbar { display: flex; height: 16px; border-radius: 4px; overflow: hidden; background: #EDF2F7; }
  .sbar .seg { display: block; height: 100%; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 8px; }
  .legend .lg { font-size: 12px; color: #4A5568; }
  .legend .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  .status-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
  .chip { font-size: 12px; padding: 2px 9px; border-radius: 10px; }
  .bl { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
  .bl-l { flex: 0 0 40%; font-size: 12px; color: #2D3748; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bl-t { flex: 1; height: 10px; background: #EDF2F7; border-radius: 5px; overflow: hidden; }
  .bl-f { display: block; height: 100%; border-radius: 5px; }
  .bl-n { flex: 0 0 auto; font-size: 12px; font-weight: 600; color: #4A5568; min-width: 20px; text-align: right; }
  .muted { color: #A0AEC0; font-size: 12px; }
  .note { margin-top: 14px; font-size: 12px; color: #0C447C; background: #EBF8FF; border: 1px solid #D3EAF7; border-radius: 4px; padding: 8px 10px; }
  .toolbar { padding: 12px 32px; background: #F7FAFC; border-bottom: 1px solid #E2E8F0; position: sticky; top: 0; z-index: 5; }
  .tb-row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .tb-label { font-size: 11px; font-weight: 600; color: #718096; text-transform: uppercase; letter-spacing: .5px; width: 78px; }
  .tgl { border: 1px solid rgba(0,0,0,0.12); border-radius: 12px; padding: 2px 11px; font-size: 12px; cursor: pointer; opacity: 0.4; font-family: inherit; }
  .tgl.on { opacity: 1; }
  .tb-search { flex: 1; min-width: 200px; padding: 6px 10px; border: 1px solid #CBD5E0; border-radius: 5px; font-size: 13px; font-family: inherit; }
  .tb-count { font-size: 12px; color: #4A5568; font-weight: 600; }
  .clashes { padding: 16px 32px; }
  .empty { padding: 40px; text-align: center; color: #A0AEC0; font-size: 15px; }
  .clash { background: white; border: 1px solid #E2E8F0; border-radius: 6px; padding: 14px 16px; margin-bottom: 12px; }
  .c-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .c-num { font-weight: 700; color: #1A365D; font-size: 15px; }
  .band-chip { font-size: 12px; font-weight: 700; padding: 2px 9px; border-radius: 10px; }
  .pill { font-size: 12px; padding: 2px 9px; border-radius: 10px; }
  .c-kind { font-size: 11px; color: #A0AEC0; font-weight: 600; letter-spacing: .5px; }
  .c-pair { font-size: 12px; color: #718096; margin-left: auto; }
  .c-reason { margin-top: 8px; font-size: 13px; color: #2D3748; }
  .c-body { display: flex; gap: 16px; margin-top: 10px; }
  .thumb img { width: 260px; max-width: 40vw; border-radius: 5px; border: 1px solid #E2E8F0; display: block; }
  .c-elems { flex: 1; min-width: 0; }
  .test-name { font-size: 12px; color: #718096; margin-bottom: 6px; }
  .elem { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin-bottom: 4px; }
  .elem-tag { display: inline-block; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 4px; background: #EDF2F7; color: #4A5568; font-weight: 700; font-size: 11px; }
  .elem-name { font-weight: 600; color: #2D3748; }
  .elem-meta { font-size: 11px; color: #718096; }
  .c-loc { font-size: 11px; color: #718096; margin-top: 6px; }
  .mono { font-family: Consolas, 'Courier New', monospace; }
  .c-comments, .c-history { margin-top: 12px; border-top: 1px solid #EDF2F7; padding-top: 10px; }
  .c-comments h3, .c-history h3 { font-size: 12px; color: #4A5568; margin: 0 0 6px; }
  .comment { margin-bottom: 6px; }
  .cm-when { font-size: 11px; color: #A0AEC0; }
  .cm-author { font-size: 12px; font-weight: 600; color: #2D3748; }
  .cm-body { font-size: 12px; color: #2D3748; margin-top: 2px; }
  .c-history ul { margin: 0; padding-left: 16px; }
  .c-history li { font-size: 12px; color: #4A5568; margin-bottom: 2px; }
  .h-when { font-size: 11px; color: #A0AEC0; }
  .foot { padding: 16px 32px 28px; font-size: 11px; color: #A0AEC0; text-align: center; }
  @media print {
    body { background: white; }
    .toolbar { display: none !important; }
    .clash { page-break-inside: avoid; border-color: #CBD5E0; }
    .cover { page-break-after: avoid; }
    * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
    @page { margin: 12mm; }
  }
</style>
</head>
<body>
"""


# The live-filter script. One raw block; NEVER run through .format().
_SCRIPT = u"""
<script>
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.clash'));
  var active = { band: {}, status: {}, trade: {} };
  var toolbar = document.getElementById('toolbar');
  var searchEl = document.getElementById('tbsearch');
  var countEl = document.getElementById('tbcount');
  // Seed active sets from the toggle buttons (all on).
  Array.prototype.slice.call(toolbar.querySelectorAll('.tgl')).forEach(function (b) {
    active[b.dataset.kind][b.dataset.v] = true;
    b.addEventListener('click', function () {
      var on = !b.classList.contains('on');
      b.classList.toggle('on', on);
      active[b.dataset.kind][b.dataset.v] = on;
      apply();
    });
  });
  function anyOn(kind) {
    var k = active[kind]; for (var v in k) { if (k[v]) return true; } return false;
  }
  function pass(card, kind, attr) {
    // If nothing in a group is on, treat the whole group as "all" (a dead
    // end filter is unhelpful) so the list never blanks out entirely.
    if (!anyOn(kind)) return true;
    return !!active[kind][card.dataset[attr]];
  }
  function apply() {
    var q = (searchEl.value || '').toLowerCase().trim();
    var shown = 0;
    cards.forEach(function (c) {
      var ok = pass(c, 'band', 'band') && pass(c, 'status', 'status') &&
               pass(c, 'trade', 'trade') &&
               (!q || (c.dataset.text || '').indexOf(q) >= 0);
      c.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    countEl.textContent = 'Showing ' + shown + ' of ' + cards.length;
  }
  searchEl.addEventListener('input', apply);
  apply();
})();
</script>
"""
