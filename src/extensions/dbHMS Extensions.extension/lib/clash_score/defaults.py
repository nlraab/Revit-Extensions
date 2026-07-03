# -*- coding: utf-8 -*-
"""Firm-standard configuration for the importance engine.

These tables ARE the dbHMS standard (Nathan, 2026-07-01: one standard across
all projects; no per-project tuning). Tuning the engine means editing THIS
file (or, later, a firm-level override in the shared folder's global store),
never a per-project knob -- so "Critical" means the same thing on every job.

Every value is grounded in the research dossier
(CLASH_IMPORTANCE_RESEARCH.md): the rigidity ladder in code physics
(IPC 704.1 slope, IMC 506.3.7 grease, NEC bend budgets, NFPA 13), the noise
floors in published practitioner tolerances, and the band semantics in the
published owner standards (Ashghal, Indiana University).

Pure data, importable in CPython 3 (test suite) and IronPython 2.7 (Revit).
"""


# ---------------------------------------------------------------------------
# Category name tables (Revit Category.Name strings, stored on every clash
# ref as ref['category'] since the first engine version -- present on OLD
# records too, unlike the enrichment fields). Names are Revit's English
# category names, which is what dbHMS models use.
# ---------------------------------------------------------------------------

STRUCTURAL_CATS = frozenset((
    'Structural Framing', 'Structural Columns', 'Structural Foundations',
))

ARCH_CATS = frozenset((
    'Walls', 'Floors', 'Ceilings', 'Doors', 'Windows', 'Roofs', 'Stairs',
))

# The N1 "intended penetration" demotion applies only to assemblies that
# actually take sleeves/openings. A duct through a stair or door is a real
# routing error, not a penetration candidate.
PENETRABLE_ARCH_CATS = frozenset(('Walls', 'Floors', 'Ceilings', 'Roofs'))

DUCT_CATS = frozenset(('Ducts', 'Duct Fittings', 'Duct Accessories'))
PIPE_CATS = frozenset(('Pipes', 'Pipe Fittings', 'Pipe Accessories'))
FLEX_CATS = frozenset(('Flex Ducts', 'Flex Pipes'))
CONDUIT_CATS = frozenset(('Conduits', 'Conduit Fittings'))
TRAY_CATS = frozenset(('Cable Trays', 'Cable Tray Fittings'))
SPRINKLER_CATS = frozenset(('Sprinklers',))
# Genuine equipment: heavy, pad-mounted, service-clearance-bound (rig 4).
EQUIPMENT_CATS = frozenset((
    'Mechanical Equipment', 'Electrical Equipment',
))
# Mounted fixtures: troffers, diffusers, wall-hung sanitary, device boxes.
# Split out of EQUIPMENT_CATS after the NIUHTC calibration
# (NIUHTC_CALIBRATION_FINDINGS.md section 4): rigidity 4 for a light
# fixture made every troffer-in-its-own-ceiling a Major routing conflict
# (1,766 rows on one hospital). These are cheap, field-adjusted movers.
MOUNTED_CATS = frozenset((
    'Plumbing Fixtures', 'Electrical Fixtures', 'Lighting Fixtures',
    'Air Terminals',
))
# Surfaces that fixtures/equipment legitimately mount in/on - the N3
# "mounting adjacency" demotion applies only against these.
MOUNTING_SURFACE_CATS = frozenset(('Walls', 'Floors', 'Ceilings', 'Roofs'))
# Surfaces a drain legitimately seats AT (gap 0 by design): arch floors
# and roofs, and the structural foundation slab. The N4 stage-2 split
# keys gravity fittings/accessories against these.
DRAIN_SURFACE_CATS = frozenset(('Floors', 'Roofs', 'Structural Foundations'))
# Electrical distribution gear buried in a wall is an electrical-room
# layout problem, not mounting adjacency: name words that keep an
# Electrical Equipment mover OUT of the N3 demotion (judge patch).
SWITCHGEAR_NAME_WORDS = ('switchboard', 'switchgear', 'panelboard',
                         'msb', 'mcc', ' ct ', 'tx-', 'transformer')
TECH_CATS = frozenset((
    'Data Devices', 'Communication Devices', 'Telephone Devices',
    'Nurse Call Devices', 'Security Devices',
))
INSULATION_CATS = frozenset((
    'Pipe Insulations', 'Duct Insulations', 'Duct Linings',
))

# Fitting/accessory categories: the R-SYS same-system rule requires one of
# these on a side, so a genuine routing error (two BRANCHES of one system
# crossing mid-air, no fitting involved) is never suppressed.
FITTING_CATS = frozenset((
    'Duct Fittings', 'Duct Accessories', 'Pipe Fittings', 'Pipe Accessories',
    'Conduit Fittings', 'Cable Tray Fittings',
))

# Fallback discipline map for OLD records that lack the captured
# ref['discipline'] field. Keyed by Revit category name.
DISCIPLINE_BY_CAT = {}
for _c in DUCT_CATS | FLEX_CATS | EQUIPMENT_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Mechanical'
for _c in PIPE_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Plumbing'
for _c in SPRINKLER_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Fire Protection'
for _c in CONDUIT_CATS | TRAY_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Electrical'
for _c in TECH_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Technology'
for _c in ARCH_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Architectural'
for _c in STRUCTURAL_CATS:
    DISCIPLINE_BY_CAT[_c] = 'Structural'
DISCIPLINE_BY_CAT['Mechanical Equipment'] = 'Mechanical'
DISCIPLINE_BY_CAT['Electrical Equipment'] = 'Electrical'
DISCIPLINE_BY_CAT['Electrical Fixtures'] = 'Electrical'
DISCIPLINE_BY_CAT['Lighting Fixtures'] = 'Electrical'
DISCIPLINE_BY_CAT['Plumbing Fixtures'] = 'Plumbing'
# MOUNTED_CATS members are no longer inside EQUIPMENT_CATS, so they need
# explicit entries (Air Terminals used to ride the EQUIPMENT union).
DISCIPLINE_BY_CAT['Air Terminals'] = 'Mechanical'


# ---------------------------------------------------------------------------
# Gravity / special-system detection.
#
# Revit has NO usable Storm classification (verified through Revit 2026: the
# API enum value exists but is documented "Reserved for future use"), so
# storm systems typically inherit Sanitary when their system type was
# duplicated -- classification-primary detection already catches those. The
# name/abbreviation lists below are the BACKSTOP for storm modeled under
# Other or Domestic Cold Water, and pipe slope is a third OR-able signal.
# ---------------------------------------------------------------------------

GRAVITY_CLASSES = frozenset(('Sanitary', 'Vent'))

STORM_NAME_WORDS = ('storm', 'roof drain', 'overflow')      # substring, lowercase
STORM_ABBRS = frozenset(('ST', 'SD', 'RD', 'OD', 'STM', 'OVFL'))  # exact, upper

GREASE_NAME_WORDS = ('grease', 'kitchen exhaust')
CONDENSATE_NAME_WORDS = ('condensate',)
MEDGAS_NAME_WORDS = ('medical', 'med gas', 'med-gas', 'oxygen',
                     'vacuum', 'nitrous')

FP_DRY_CLASSES = frozenset((
    'Fire Protection Dry', 'Fire Protection Pre-Action',
    'Fire Protection Other',
))
FP_WET_CLASSES = frozenset(('Fire Protection Wet',))


# ---------------------------------------------------------------------------
# The engine config. score_all() deep-merges a caller dict over this, but
# per decision (2026-07-01) there is no per-project config: the defaults are
# the firm standard.
# ---------------------------------------------------------------------------

DEFAULTS = {
    # rev 2 = the NIUHTC stage-1 retune (mounted-class split, N3 mounting
    # rule, N1-before-M4, structural-link fix).
    # rev 3 = stage 2: the M3 drain/penetration split (N4) and the C3
    # tightening (equipment never satisfies C3; known-small vents drop to
    # rigidity 2). A band step-down between runs at these revs is a RULE
    # change, not resolved work.
    'rev': 3,

    # Layer A rule toggles (all on).
    'rules': {
        'R-SELF': True,
        'R-NOT-OURS': True,
        'R-SYS': True,
        'R-FIELD': True,
        'R-GRAZE': True,    # dormant until penetration depth ships
    },

    # R-FIELD: lone small conduit vs conduit is field-routed
    # (NBIMS-US V3 sec 5.5.4.8 + Annex A Note 1 item 1.5.1: single conduits
    # under 2 in are outside required modeling scope; racks of 2+ must be
    # modeled, hence the cluster escape below). dbHMS default is 1 in,
    # tighter than the NBIMS 2 in floor, on the conservative side.
    'field_fix_dia_in': 1.0,
    'field_fix_cluster_escape': 10,   # >= this many neighbors: not field-fix

    # R-GRAZE (dormant): sub-3/8 in penetrations are modeling noise
    # (the sourced 10 mm practitioner floor). Never fires on rigidity-4+
    # movers or against structure.
    'graze_floor_in': 0.375,

    # Tier thresholds.
    'deep_pen_in': 6.0,          # C4 (dormant): deep systemic overlap
    'big_duct_in': 24.0,         # rigidity 4 duct / C2 size gate
    'mid_duct_in': 12.0,         # rigidity 3 duct
    'big_pipe_in': 4.0,          # rigidity 3 pressurized pipe
    'gravity_main_in': 3.0,      # rigidity 5 gravity at/above this dia
    'small_conduit_in': 1.0,     # rigidity 0 conduit at/below this dia
    'fp_main_in': 4.0,           # FP wet main vs branch

    # Congestion (ClashMEP insight): many clashes in one spot = rack-level
    # rework, not N field fixes. Hysteresis so neighbor resolution between
    # runs cannot flap a band.
    'cluster_radius_ft': 5.0,
    'cluster_major': 20,          # M4 fires at/above this
    'cluster_major_release': 12,  # ...and keeps holding down to this

    # M3: soft clash with most of its clearance consumed.
    'near_gap_ratio': 0.25,

    # Near-size-threshold honesty margin (fraction of the boundary).
    'near_threshold_frac': 0.15,

    # Bands: base score and inclusive max. Band is DECIDED BY THE TIER RULE;
    # these numbers only map tiers onto the 0-100 scale the UI renders
    # (70/40 cutoffs are hardcoded in coord.html in four places).
    'bands': {
        'Critical': (70, 99),
        'Major':    (40, 69),
        'Minor':    (8, 39),
    },
}
