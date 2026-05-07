# -*- coding: utf-8 -*-
"""Pure-data filter predicate for the Clash Browser's row list.

The Browser's left-side filter card has Trade checkboxes, Status
checkboxes, a Test dropdown, and a Search box. Each user interaction
re-applies a filter against the live ListCollectionView that drives
the grid. The actual WPF wiring lives in the Browser script; the
filter LOGIC lives here so it can be unit-tested without Revit or
WPF.

A row passes iff every active filter accepts it. Filters compose by
intersection (AND), not union — same as standard search-form UX.
"""


def row_passes(row, allowed_trades, allowed_statuses, test_filter, search_text):
    """Return True iff `row` should be visible under the given filter state.

    Args:
        row              - any object with `.Trade`, `.Status`, `.TestName`,
                           and `.SearchHaystack` (lowercase string)
                           attributes. The Browser's ClashRow class
                           provides all four.
        allowed_trades   - set of trade strings the user has checked, OR
                           None to skip the trade filter entirely.
                           Empty set means "user unchecked every trade"
                           and correctly hides everything.
        allowed_statuses - same semantics as allowed_trades, for the
                           Status checkboxes.
        test_filter      - test name string the user picked from the
                           dropdown, or None / "(All tests)" / empty
                           string to skip the test filter.
        search_text      - raw search box text. Case-insensitive
                           substring match against row.SearchHaystack.
                           Empty / None / whitespace-only skips it.

    The "None vs empty set" distinction matters: None means "no filter
    configured" (e.g. the caller doesn't want trade filtering at all),
    while an empty set means "user explicitly hid every option" and
    nothing should pass.
    """
    if allowed_trades is not None and row.Trade not in allowed_trades:
        return False
    if allowed_statuses is not None and row.Status not in allowed_statuses:
        return False
    if test_filter and test_filter != "(All tests)" and row.TestName != test_filter:
        return False
    if search_text:
        needle = search_text.lower().strip()
        if needle:
            haystack = getattr(row, 'SearchHaystack', '') or ''
            if needle not in haystack:
                return False
    return True


def build_search_haystack(clash_dict, test_name):
    """Build the lowercase searchable text for one clash.

    Pre-computed once at row construction so each filter pass against
    the search box is just a substring check. Includes everything a
    user might reasonably type to find a clash:

      - test name (so "soft" matches all soft-clearance clashes)
      - element A and B display names
      - element A and B numeric IDs (so "1250111" finds it)
      - assignee trade (so "plumbing" matches even though Trade is its
        own filter — search overlap doesn't hurt)
      - all comment authors and bodies (so a note about a wall hit can
        be found by searching the note text later)
    """
    parts = [test_name or '']
    ref_a = clash_dict.get('ref_a') or {}
    ref_b = clash_dict.get('ref_b') or {}
    parts.append(str(ref_a.get('name') or ''))
    parts.append(str(ref_b.get('name') or ''))
    parts.append(str(ref_a.get('element_id') or ''))
    parts.append(str(ref_b.get('element_id') or ''))
    parts.append(str(clash_dict.get('assignee') or ''))
    for comment in (clash_dict.get('comments') or []):
        parts.append(str(comment.get('author') or ''))
        parts.append(str(comment.get('body') or ''))
    return ' '.join(parts).lower()
