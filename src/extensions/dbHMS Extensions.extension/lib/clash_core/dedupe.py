# -*- coding: utf-8 -*-
"""Run-level dedupe of raw clashes before they hit the merge layer.

Detection orchestration runs every test that's in scope and concatenates
all raw clashes. The same physical pair of elements can show up in:

  * a hard test AND a soft-clearance test (the duct hits the wall AND
    is also "within 1in of" the wall — the soft test still flags it
    because the hard intersection is also within tolerance), or
  * two different hard tests whose category lists overlap (rare with
    the four firm defaults but possible with custom project tests).

For the user, those are duplicate rows in the Browser. The dedupe rule
here is intentionally narrow: drop SOFT clashes whose pair already has
a HARD clash in this run. A pair that actually intersects isn't a
"near miss" anymore — it's a hit. Hard-vs-hard duplicates from
overlapping test scopes are left alone for now (no case in production
yet, and collapsing them would lose the per-test_id provenance that
the clash fingerprint relies on).

Pure Python — no Revit imports. Runs in the CPython test suite.
"""


def drop_soft_overlapping_hard(raw_clashes):
    """Drop soft clashes whose element pair also has a hard clash this run.

    Pair identity uses (source, element_id, link_doc_title) for both
    sides, normalized to a sorted tuple so swapping A/B produces the
    same key. link_doc_title disambiguates the (rare) case where the
    same element_id exists in two different linked .rvt files.

    Returns (filtered_list, dropped_count). The order of filtered_list
    preserves the input order of the kept clashes.
    """
    if not raw_clashes:
        return [], 0
    hard_pairs = set()
    for c in raw_clashes:
        if (c or {}).get('kind') != 'hard':
            continue
        key = _pair_key(c)
        if key:
            hard_pairs.add(key)
    if not hard_pairs:
        return list(raw_clashes), 0
    out = []
    dropped = 0
    for c in raw_clashes:
        if (c or {}).get('kind') == 'soft':
            key = _pair_key(c)
            if key in hard_pairs:
                dropped += 1
                continue
        out.append(c)
    return out, dropped


def _pair_key(clash):
    """Build the dedupe key for a clash — a sorted-pair tuple of element refs.

    Returns None if either side has no element_id (defensive — these
    couldn't have come from real detection).
    """
    a = clash.get('ref_a') or {}
    b = clash.get('ref_b') or {}
    a_id = a.get('element_id')
    b_id = b.get('element_id')
    if not a_id or not b_id:
        return None
    a_key = (a.get('source') or 'host', int(a_id), a.get('link_doc_title'))
    b_key = (b.get('source') or 'host', int(b_id), b.get('link_doc_title'))
    return tuple(sorted([a_key, b_key]))
