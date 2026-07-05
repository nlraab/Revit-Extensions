# -*- coding: utf-8 -*-
"""Stable identifiers for clashes across detection runs.

Why we need this: when a user runs detection, comments on a clash, then
re-runs detection later, we have to recognize "this is the same clash we
saw last time" so we can preserve the comments, status, history, and
viewpoints. We do that with a fingerprint that's stable across runs but
distinct across clashes.

Strategy (matches Navisworks behavior):
  - Element pair: identity is symmetric, so we sort the two refs into a
    canonical order before hashing. Swapping ref_a and ref_b yields the
    same fingerprint.
  - Spatial bucket: round the midpoint to the nearest SPATIAL_BUCKET_FT
    (1 ft) on each axis. Shifts within the same 1-ft bucket keep the
    fingerprint; drifting across a bucket boundary re-keys the clash
    (surfacing as auto-resolved + new in the same run - the "compound
    churn" event lib/clash_group's successor adoption exists to heal).
    This also distinguishes "same pair clashing in two different
    places" - a long pipe crossing a wall twice produces two separate
    clashes. DO NOT change this constant on a live project: every
    existing clash would re-fingerprint and detach from its status,
    comments, and history (decision D1, CLASH_GROUPING_DESIGN.md).
  - Test ID: clashes from different tests get different fingerprints,
    even if the same pair fires both. Matches how Navisworks reports
    per-test results separately.

The fingerprint is a 16-char hex digest (first 16 chars of SHA-1 over the
canonical key string). That's plenty of bits to avoid collisions in any
realistic project.
"""

import hashlib


# Round midpoint coordinates to this resolution (in feet) for fingerprinting.
# Geometry shifts smaller than this keep the same clash; shifts larger
# create a new clash.
SPATIAL_BUCKET_FT = 1.0

# How many hex chars from the SHA-1 digest to keep
FINGERPRINT_LENGTH = 16


def _ref_key(ref):
    """Build a stable string key for an ElementRef dict."""
    if not ref:
        return "0:0"
    source = ref.get('source', '') or ''
    elem_id = ref.get('element_id', 0) or 0
    return '{}:{}'.format(source, elem_id)


def _bucket_xyz(xyz, bucket_ft=SPATIAL_BUCKET_FT):
    """Round each coordinate to the nearest `bucket_ft` and return a tuple of ints
    (in bucket units, so two midpoints in the same bucket round to the same tuple)."""
    if xyz is None:
        return (0, 0, 0)
    try:
        return tuple(int(round(float(c) / bucket_ft)) for c in xyz)
    except (TypeError, ValueError):
        return (0, 0, 0)


def clash_fingerprint(test_id, ref_a, ref_b, midpoint_xyz,
                      spatial_bucket_ft=SPATIAL_BUCKET_FT,
                      include_midpoint=True):
    """Return a stable hex fingerprint identifying this clash across runs.

    The fingerprint is invariant under:
      - swap of ref_a vs ref_b
      - midpoint shifts of less than `spatial_bucket_ft` on each axis
        (when `include_midpoint` is True)

    The fingerprint changes when:
      - the test_id changes
      - either element changes (different element_id, or moves between
        host vs linked)
      - the clash moves more than `spatial_bucket_ft` from its previous
        location (when `include_midpoint` is True)

    `include_midpoint=False` drops the spatial-bucket term entirely, so the
    key is just (test_id, sorted element pair). This is for CLEARANCE rows
    (Phase 4): a clearance clash is one intruder element violating one
    equipment's code zone, and that identity must NOT re-key when the
    intruder is nudged along a large zone -- the pair + test_id already
    identify the violation uniquely, so the midpoint would only cause
    spurious churn. The default True path is BYTE-IDENTICAL to before, so no
    existing hard/soft fingerprint changes (doctrine 5 / plan section 14.7).
    """
    pair = sorted([_ref_key(ref_a), _ref_key(ref_b)])
    parts = [test_id or '', pair[0], pair[1]]
    if include_midpoint:
        bucket = _bucket_xyz(midpoint_xyz, spatial_bucket_ft)
        parts.append(','.join(str(c) for c in bucket))
    key = '|'.join(parts)
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:FINGERPRINT_LENGTH]
