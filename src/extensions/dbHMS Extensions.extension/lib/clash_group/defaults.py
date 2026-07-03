# -*- coding: utf-8 -*-
"""Firm-standard configuration for Layer C grouping.

Like lib/clash_score/defaults.py, these tables ARE the dbHMS standard
(decision: one standard across all projects, no per-project tuning). All
constants are frozen until the Phase-5 calibration run on a real project,
then adjusted once, firm-wide. Design: CLASH_GROUPING_DESIGN.md section 3.

Pure data; imports nothing.
"""

DEFAULTS = {
    # --- Spatial cluster formation (P4) ---------------------------------
    # A clash is a spatial CORE when its stamped importance.features
    # cluster_n (neighbors within 5 ft, computed by clash_score) is at or
    # above this. Density gating - not raw proximity - is what makes the
    # false merge of two nearby-but-distinct problems structurally
    # impossible.
    'core_cluster_n': 6,
    # Union / border-attach radius in feet (grid cell size too).
    'cluster_eps_ft': 6.0,
    # A connected component forms a group only at/above this many members.
    'cluster_min_members': 10,
    # Oversize components are cut deterministically at the largest
    # coordinate gap along their dominant axis.
    'max_span_ft': 50.0,
    'max_members': 150,

    # --- Element-star formation (P5) ------------------------------------
    # Minimum members sharing one anchor element to form a group.
    'anchor_min': 3,
    # Long-element bound: members spanning more than this along the
    # dominant axis split into span segments at formation.
    'anchor_span_segment_ft': 80.0,

    # --- Successor adoption (P2) -----------------------------------------
    # Tier-2 (suggestion-only) radius around the vanished member.
    'adopt_radius_ft': 10.0,

    # --- Agenda ----------------------------------------------------------
    # Rollup band order: a group's band is the MAX band among open
    # unsuppressed members, ranked by this order (read by rollup()).
    'band_order': ('Critical', 'Major', 'Minor'),
    # Group-card "congested" fact: any open member with stamped cluster_n
    # at/above this. Mirrors clash_score's M4 fire constant (cluster_major)
    # - keep the two in sync.
    'congested_cluster_n': 20,
}
