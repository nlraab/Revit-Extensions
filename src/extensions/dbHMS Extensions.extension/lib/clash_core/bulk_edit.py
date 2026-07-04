# -*- coding: utf-8 -*-
"""Bulk edits across many clashes — single-clash mutators that the Browser
loops over to apply Change Status / Reassign / Mark Resolved to every
selected row.

Pure data. No Revit / WPF imports — runs in the CPython test suite. The
Browser handles WPF selection + the picker dialog + the persistence
write; this module just owns the per-clash mutation rule (so the rule
itself is testable in isolation, and so the same rule could be reused
later by a future "rules engine" or import-from-BCF flow).

Each function:
  * Takes a single clash_dict + new value + author
  * Skips if the value already matches (returns False) — avoids polluting
    the audit log with no-op entries when a bulk edit overlaps clashes
    that were already at the target value
  * Otherwise mutates clash_dict in place AND appends one history entry
    matching the shape used by the existing single-row handlers in the
    Browser (so the audit trail looks the same regardless of bulk vs
    single-row edit)
  * Returns True if a change was made

`at` lets the caller pin a uniform timestamp for an entire bulk so the
history entries from one batch share one ISO time (cleaner audit log:
"these 47 entries are all part of one bulk action").
"""

from clash_core import models


def apply_status(clash_dict, new_status, author, at=None):
    """Set `clash_dict['status']` and append a status_changed history entry.

    Returns True if the status changed, False if it was already at
    `new_status` (in which case nothing is mutated).
    """
    if not clash_dict or not new_status:
        return False
    old_status = clash_dict.get('status') or 'Open'
    if old_status == new_status:
        return False
    clash_dict['status'] = new_status
    entry = models.make_history_entry(
        author, 'status_changed',
        before=old_status, after=new_status,
    )
    if at:
        entry['at'] = at
    clash_dict.setdefault('history', []).append(entry)
    return True


def apply_deadline(clash_dict, new_deadline, author, at=None):
    """Set `clash_dict['deadline']` (ISO date string, or None/'' to clear)
    and append a deadline_changed history entry.

    Returns True if the deadline changed, False if it was already at
    `new_deadline` (in which case nothing is mutated). Unlike status/trade,
    an empty value is meaningful here: it clears the due date.
    """
    if not clash_dict:
        return False
    new_deadline = new_deadline or None
    old_deadline = clash_dict.get('deadline') or None
    if old_deadline == new_deadline:
        return False
    clash_dict['deadline'] = new_deadline
    entry = models.make_history_entry(
        author, 'deadline_changed',
        before=old_deadline or '-', after=new_deadline or '-',
    )
    if at:
        entry['at'] = at
    clash_dict.setdefault('history', []).append(entry)
    return True


def apply_trade(clash_dict, new_trade, author, at=None):
    """Set `clash_dict['assignee']` and append a reassigned history entry.

    Returns True if the trade changed, False if it was already at
    `new_trade`. Like apply_deadline, an empty value is meaningful: it
    UN-assigns the clash (the web card's "(unassigned)" choice), which
    must round-trip as a real change rather than a silent no-op.
    """
    if not clash_dict:
        return False
    new_trade = new_trade or None
    old_trade = clash_dict.get('assignee') or None
    if old_trade == new_trade:
        return False
    clash_dict['assignee'] = new_trade
    entry = models.make_history_entry(
        author, 'reassigned',
        before=old_trade or '-', after=new_trade or '-',
    )
    if at:
        entry['at'] = at
    clash_dict.setdefault('history', []).append(entry)
    return True
