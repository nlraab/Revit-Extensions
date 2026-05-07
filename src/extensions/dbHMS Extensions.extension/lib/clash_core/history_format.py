# -*- coding: utf-8 -*-
"""Friendly-display formatters for clash history entries.

Each clash carries a chronological history list — every status change,
trade reassignment, comment, viewpoint capture, etc. logs an entry with
{action, at, author, before?, after?}. Raw entries look like:

    {"action": "status_changed", "at": "2026-05-06T05:11:16Z",
     "author": "Nathan", "before": "Open", "after": "Reviewed"}

The History panel renders these as human-readable rows. This module
turns one raw entry into the three display fields the panel binds to:
when (formatted timestamp), who (author), action (label + before/after
arrow). Pure data — no Revit / WPF imports — so the formatting logic
runs in the CPython test suite.
"""


# Map raw action keys to display labels. Anything not in here gets
# title-cased on the fly (so a future "exported_to_bcf" action shows as
# "Exported to bcf" rather than disappearing) — those rows show up
# correctly while we add a friendlier label here on next code pass.
_ACTION_LABELS = {
    'detected':                'Detected',
    'status_changed':          'Status changed',
    'reassigned':              'Reassigned',
    'auto_resolved':           'Auto-resolved',
    'comment_added':           'Comment added',
    'viewpoint_saved':         'Viewpoint saved',
    'viewpoint_auto_captured': 'Viewpoint auto-captured',
}


def format_action(entry):
    """Return the friendly action label for a history entry.

    Examples:
        {"action": "status_changed", "before": "Open", "after": "Reviewed"}
            → "Status changed: Open → Reviewed"
        {"action": "reassigned", "before": "Mechanical", "after": "Plumbing"}
            → "Reassigned: Mechanical → Plumbing"
        {"action": "comment_added"}
            → "Comment added"
        {"action": "auto_resolved", "before": "Open", "after": "Resolved"}
            → "Auto-resolved: Open → Resolved"
        {"action": "exported_to_bcf"}    # unknown action
            → "Exported to bcf"
    """
    if entry is None:
        return ''
    action = entry.get('action') or ''
    label = _ACTION_LABELS.get(
        action,
        action.replace('_', ' ').capitalize() if action else 'Unknown',
    )
    before = entry.get('before')
    after = entry.get('after')
    if before is not None and after is not None:
        return u'{}: {} → {}'.format(label, before, after)
    if after is not None:
        return u'{}: {}'.format(label, after)
    return label


def format_when(entry):
    """Return the entry's timestamp formatted as 'YYYY-MM-DD HH:MM UTC'.

    Raw timestamps look like '2026-05-06T05:11:16Z' or '2026-05-06T05:11:16.123Z'.
    If parsing fails (missing T separator, malformed string), returns
    the raw string unchanged so something is still shown.
    """
    iso = (entry or {}).get('at') or ''
    if not iso or 'T' not in iso:
        return iso
    date_part, time_part = iso.split('T', 1)
    # Strip trailing Z and fractional seconds before truncating to HH:MM.
    time_part = time_part.rstrip('Z')
    if '.' in time_part:
        time_part = time_part.split('.', 1)[0]
    pieces = time_part.split(':')
    if len(pieces) >= 2:
        time_short = u'{}:{}'.format(pieces[0], pieces[1])
    else:
        time_short = time_part
    return u'{} {} UTC'.format(date_part, time_short)


def format_author(entry):
    """Return the entry's author, defaulting to 'unknown' if missing."""
    raw = ((entry or {}).get('author') or u'').strip()
    return raw or u'unknown'
