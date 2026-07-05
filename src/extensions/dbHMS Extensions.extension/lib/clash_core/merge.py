# -*- coding: utf-8 -*-
"""Merge a fresh detection run into the existing clash list.

Without this, every re-run would wipe the user's comments, status changes,
and history. With it:

  - **New** clashes (no fingerprint match in the previous run) get added
    fresh, with status Open and a 'detected' history entry.

  - **Persisting** clashes (fingerprint matches a previous clash) keep
    everything: id, comments, status, history, viewpoints. Only their
    midpoint and last_seen_run get updated.

  - **Reappeared** clashes (matched a previously-Resolved clash) get
    reopened, with a 'reopened' history entry. This handles the case
    where someone moves an element back and the clash returns.

  - **Auto-resolved** clashes (in the previous run but not in the new run)
    keep their old metadata but get auto-marked Resolved with an
    'auto_resolved' history entry. This is how Navisworks behaves.

The whole module is dict-in / dict-out and Revit-independent, so it's
unit-testable in CPython 3.
"""

import copy
import uuid

from clash_core import models
from clash_core.identity import clash_fingerprint


# Trades that "own" a clash - if either ref's category resolves to one of
# these, that trade gets the assignee. Architectural / Structural don't own
# clashes; in coordination practice the MEP party reroutes around arch/struct.
_MEP_DISCIPLINES = frozenset((
    models.Discipline.MECHANICAL,
    models.Discipline.ELECTRICAL,
    models.Discipline.PLUMBING,
    models.Discipline.FIRE_PROTECTION,
    models.Discipline.TECHNOLOGY,
))


def _now_iso():
    """Reuse the same timestamp formatter models uses."""
    return models._now_iso()


def _new_id():
    return str(uuid.uuid4())


def _derive_assignee(raw_clash):
    """Auto-derive the assignee trade from the element categories involved.

    Strategy: walk ref_a then ref_b, look up each ref's category_id in
    `clash_core.categories.discipline_for_category_id`, and return the first
    MEP discipline found. If neither side is MEP (e.g. a clash between two
    arch elements - shouldn't happen in our default tests but possible with
    custom ones), fall back to the test's `default_assignee`.

    Wrapped in try/except so callers without a Revit API in scope (the test
    suite) safely fall through to the default.
    """
    for ref in (raw_clash.get('ref_a'), raw_clash.get('ref_b')):
        if not ref:
            continue
        cat_id = ref.get('category_id')
        if cat_id is None:
            continue
        try:
            from clash_core import categories
            disc = categories.discipline_for_category_id(cat_id)
        except Exception:
            disc = None
        if disc and disc in _MEP_DISCIPLINES:
            return disc
    return raw_clash.get('default_assignee')


def _next_seq(old_clashes):
    """Pick the next per-project sequential clash number.

    The seq is just for display ("Clash #42" reads better than
    "Clash #a3f4b7c2..."). Stored on each clash, never reused.
    """
    max_seq = 0
    for c in old_clashes:
        try:
            s = int(c.get('seq', 0))
            if s > max_seq:
                max_seq = s
        except (TypeError, ValueError):
            continue
    return max_seq + 1


# Measurement fields refreshed from the raw detection on EVERY run, for both
# new and persisting clashes -- one list so the two branches can never drift.
# midpoint: detection midpoint (soft: closest-point midpoint on real geometry).
# gap_inches/closest_point_*/is_contact/gap_method: soft-clash measurements
# (None on hard rows).
# tolerance_inches: the owning test's tolerance, stamped per clash so the
# pure scoring layer can compute gap/tolerance with no test-library lookup.
# Pair geometry (Phase 2, emitted by clash_detect/pairgeom via hard.py):
# overlap_bbox_in (free AABB-overlap extents [dx,dy,dz] in), the boolean tier
# penetration_depth_in / overlap_volume_cf / overlap_centroid, pen_class
# (Phase 3), and geom_method ('boolean'|'bbox'). Listed here so they refresh
# unconditionally each run and backfill re-appearing clashes with no
# migration; the dormant scoring rules (R-GRAZE, C4) activate on the run that
# first emits them. Readers must use .get() (auto-resolved rows never gain
# keys).
_PER_RUN_FIELDS = ('midpoint', 'gap_inches', 'closest_point_a',
                   'closest_point_b', 'is_contact', 'gap_method',
                   'tolerance_inches',
                   'penetration_depth_in', 'overlap_volume_cf',
                   'overlap_bbox_in', 'overlap_centroid', 'pen_class',
                   'geom_method')


def merge_runs(old_clashes, raw_new_clashes, run_iso=None, author='system'):
    """Merge a fresh detection run into the existing clash list.

    Args:
        old_clashes: list of clash dicts loaded from clashes.json
        raw_new_clashes: list of dicts from the detection engine. Each must
            have at least: test_id, kind, ref_a, ref_b, midpoint. May also
            include default_assignee.
        run_iso: ISO-8601 timestamp string for this run. Defaults to now.
        author: who to attribute auto-actions to in history. 'system' by
            default; pass the human user for runs they personally triggered.

    Returns:
        (merged_clashes, summary) where summary is:
            {'new': N, 'persisting': N, 'auto_resolved': N, 'reopened': N}
    """
    if run_iso is None:
        run_iso = _now_iso()

    old_by_fp = {}
    for c in old_clashes:
        fp = c.get('fingerprint')
        if fp:
            old_by_fp[fp] = c

    merged = []
    seen_fps = set()
    new_count = 0
    persisting_count = 0
    reopened_count = 0
    next_seq = _next_seq(old_clashes)

    for raw in raw_new_clashes:
        fp = clash_fingerprint(
            raw.get('test_id'),
            raw.get('ref_a', {}),
            raw.get('ref_b', {}),
            raw.get('midpoint'),
        )
        # Avoid double-counting if the detection engine reports the same
        # logical clash twice (e.g. same pair, same midpoint).
        if fp in seen_fps:
            continue
        seen_fps.add(fp)

        existing = old_by_fp.get(fp)
        if existing is not None:
            updated = copy.deepcopy(existing)
            updated['last_seen_run'] = run_iso
            # Per-run measurements refresh unconditionally: a stale gap from
            # an earlier run is worse than None (hard rows carry None).
            for k in _PER_RUN_FIELDS:
                updated[k] = raw.get(k)
            # Refresh element refs in case names/categories changed
            if raw.get('ref_a'):
                updated['ref_a'] = raw['ref_a']
            if raw.get('ref_b'):
                updated['ref_b'] = raw['ref_b']
            # Reopen if it had been Resolved (auto or manual)
            if updated.get('status') == models.ClashStatus.RESOLVED:
                history = updated.setdefault('history', [])
                history.append(models.make_history_entry(
                    author, 'reopened',
                    before=models.ClashStatus.RESOLVED,
                    after=models.ClashStatus.OPEN,
                ))
                updated['status'] = models.ClashStatus.OPEN
                reopened_count += 1
            merged.append(updated)
            persisting_count += 1
        else:
            fresh = {
                'id':              _new_id(),
                'seq':             next_seq,
                'fingerprint':     fp,
                'test_id':         raw.get('test_id'),
                'kind':            raw.get('kind') or 'hard',
                'status':          models.ClashStatus.OPEN,
                'assignee':        _derive_assignee(raw),
                'ref_a':           raw.get('ref_a'),
                'ref_b':           raw.get('ref_b'),
                'first_seen_run':  run_iso,
                'last_seen_run':   run_iso,
                'comments':        [],
                'viewpoints':      [],
                'history':         [models.make_history_entry(author, 'detected')],
            }
            for k in _PER_RUN_FIELDS:
                fresh[k] = raw.get(k)
            merged.append(fresh)
            next_seq += 1
            new_count += 1

    # Old clashes that didn't reappear: keep them, auto-mark Resolved if open
    auto_resolved_count = 0
    for fp, old in old_by_fp.items():
        if fp in seen_fps:
            continue
        kept = copy.deepcopy(old)
        old_status = kept.get('status')
        if old_status in (models.ClashStatus.OPEN,
                          models.ClashStatus.REVIEWED,
                          models.ClashStatus.APPROVED):
            history = kept.setdefault('history', [])
            history.append(models.make_history_entry(
                author, 'auto_resolved',
                before=old_status,
                after=models.ClashStatus.RESOLVED,
            ))
            kept['status'] = models.ClashStatus.RESOLVED
            auto_resolved_count += 1
        merged.append(kept)

    summary = {
        'new':           new_count,
        'persisting':    persisting_count,
        'auto_resolved': auto_resolved_count,
        'reopened':      reopened_count,
    }
    return merged, summary
