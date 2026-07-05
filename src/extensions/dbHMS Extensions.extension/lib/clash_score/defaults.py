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
    # rigidity 2).
    # rev 4 = importance v2 Phase 1 (pure rescore, CLASH_IMPORTANCE_V2_PLAN.md
    # section 5): M1 split into 8 named sub-rules, N-PT + N-DUP demotions,
    # sys_class set-matching (gated), the composed 2-3 sentence reasons with
    # headline/code_ref/resolve_by/facts, and the score-composition remap
    # (continuous size/congestion, pair-stiffness constraint, proportional
    # band mapping).
    # rev 6 = Phase 2 pair geometry (R-GRAZE/C4 activated + guarded, real
    # geometry term) AND Phase 3 arch/structure payload: rated-assembly facts
    # drive M-RATED (fire/smoke damper) and firestop-graded N1; captured
    # penetration depth + wall thickness drive pen_class and M-PEN (partial
    # penetrations are not sleeve-resolvable); beam elevations drive
    # M-STRUCT-ZONE; a wall's structural usage reroutes an arch-modeled bearing
    # wall onto the structural path. Band movement across a rev bump is a RULE
    # change, not resolved work.
    # rev 7 = V3 detection cleanup: the layered-penetration collapse (one
    # physical penetration of a stacked floor/roof/ceiling assembly is now one
    # row, not one-per-modeled-layer -- runs pre-merge in clash_core.dedupe)
    # and the raw_realistic_max retune 30 -> 48 (once real boolean geometry
    # filled the score terms, 30 re-piled the Major band into 60-69). Scores
    # shift WITHIN bands only; no band cutoff moved.
    # rev 8 = Phase 4 clearance engine + NEC 110.26 zones (C-NEC dedicated
    # space, C-NEC-W working space, M-NEC-PROT leak-capable-above-cap) + the
    # NFPA 13 sprinkler-obstruction test (M-SPR, dormant on models with no
    # modeled sprinkler heads). New synthetic clearance rows key on (test id +
    # element pair) with the midpoint EXCLUDED from the fingerprint. Band
    # movement across this bump is a rule change, not resolved work.
    'rev': 8,

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

    # R-GRAZE: sub-3/8 in penetrations are modeling noise (the sourced 10 mm
    # practitioner floor). Never fires on rigidity-4+ movers or against
    # structure. Phase 2: also requires a MEASURED boolean overlap (never a
    # bbox proxy -- a diagonal graze has a small AABB extent while being a
    # real hit) whose volume is under graze_max_vol_cf, so a wide shallow
    # face-contact (two parallel ducts touching along their length: small
    # depth, large volume) is NOT suppressed.
    'graze_floor_in': 0.375,
    'graze_max_vol_cf': 0.02,

    # Tier thresholds.
    'deep_pen_in': 6.0,          # C4 (dormant): deep systemic overlap
    'big_duct_in': 24.0,         # rigidity 4 duct / C2 size gate
    'mid_duct_in': 12.0,         # rigidity 3 duct
    'big_pipe_in': 4.0,          # rigidity 3 pressurized pipe
    'gravity_main_in': 3.0,      # rigidity 5 gravity at/above this dia
    'small_conduit_in': 1.0,     # rigidity 0 conduit at/below this dia
    'fp_main_in': 4.0,           # FP wet main vs branch

    # Phase 3: rated assemblies + penetration classification.
    'rated_wall_min_hr': 1.0,    # >= this fire rating -> rated (damper/firestop)
    # M-PEN "needs a framed/linteled opening" size gate (the ONLY use of
    # _over_sleeve). Rect ducts up to 16 in through a partition get a routine
    # cut opening (N1); wider needs an engineered opening (M-PEN). Retuned
    # 10 -> 16 on the V4 run: 10 in flagged ordinary 12-16 in duct openings as
    # design work. TUNABLE -- confirm against firm practice; raise toward
    # 20-24 if even 18 in openings are routine in your partitions.
    'sleeve_rect_max_in': 16.0,  # ~400 mm: rect opening wider needs framing
    'sleeve_round_max_in': 16.0, # ~400 mm dia round
    'pen_full_frac': 0.85,       # depth >= thickness*this -> full (through) pen
    'beam_edge_frac': 0.30,      # clash within this fraction of a beam's top or
                                 # bottom -> flexural/edge zone (escalate)
    'min_assembly_in': 1.5,      # a penetrable assembly is "significant" only at
                                 # or above this thickness -- excludes finish
                                 # floors / membranes / toppings that everything
                                 # "penetrates" as a modeling artifact
    'beam_pen_min_in': 2.0,      # M-STRUCT-ZONE needs a real penetration this
                                 # deep INTO the beam; a duct merely meeting the
                                 # beam's underside is M2 "route around", not a
                                 # flexural-zone escalation

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

    # --- rev 4 (importance v2 P1) score-composition constants ---------------
    # The sub-score raw total maps proportionally across a band's width
    # (base..top) via min(1, raw / raw_realistic_max). Recalibrated to 48 on
    # the V3 run (5,451 clashes AFTER the layered-penetration collapse, with
    # real boolean geometry captured on ~1,630 rows). The prior value of 30
    # was tuned before pairgeom filled the geometry/volume terms; once those
    # raised the raws, ~80 percent of the Major band piled into 60-69 again.
    # 48 drains that pile into a heavy 50-59 middle (~90 percent) and reserves
    # 60-69 for a genuine top ~8 percent (the near-critical Majors), with a
    # 40-49 tail beginning to fill. Tune ONLY this constant against the
    # histogram in calibration_report -- never the 70/40 cutoffs.
    'raw_realistic_max': 48.0,
    'size_full_in': 30.0,      # size term saturates (8) at this max dimension
    'vol_full_cf': 1.0,        # volume term saturates (6) near this overlap
    'congest_full_n': 20.0,    # congestion term saturates (6) at this cluster n

    # N-PT scope (Decision D1). 'wall_device' = strict EXIT/#N point fixtures
    # PLUS wall-device receptacles/switches (option A, recommended). Set to
    # True to also demote Electrical Fixtures literally named 'TYPICAL'
    # (option B). Every demoted row is in the spot-check sheet regardless.
    'n_pt_include_wall_devices': True,
    'n_pt_include_typical': False,

    # Bands: base score and inclusive max. Band is DECIDED BY THE TIER RULE;
    # these numbers only map tiers onto the 0-100 scale the UI renders
    # (70/40 cutoffs are hardcoded in coord.html in four places).
    'bands': {
        'Critical': (70, 99),
        'Major':    (40, 69),
        'Minor':    (8, 39),
    },
}


# ---------------------------------------------------------------------------
# rev 4 (importance v2 P1) name tables + deadline vocabulary.
# Pure data, importable in CPython 3 and IronPython 2.7. All comparisons in
# clash_score upper-case the model name first, so keep these UPPER-CASE.
# ---------------------------------------------------------------------------

# N-DUP: two equipment/mounted instances that share a family name, or carry a
# placeholder type name, are usually a nested/double-placed/placeholder family
# rather than two real units. Demoted (never suppressed: a name is not proof).
PLACEHOLDER_TYPE_NAMES = frozenset(('TYPICAL', 'STANDARD'))

# N-PT: point-mounted fixtures (exit signs, wall devices) relocate with a box
# shift at rough-in, not a routing change -- they are not a Major coordination
# item against a duct/pipe. Substring words + regex on the fixture NAME.
POINT_FIXTURE_NAME_WORDS = ('EXIT',)
POINT_FIXTURE_NAME_RES = (r'^#\d+',)               # e.g. '#21 (EXIT)', '#3'
# Wall-device tokens (option A): matched as whole tokens on Electrical
# Fixtures, so 'WP' never hits a name that merely contains those letters.
WALL_DEVICE_NAME_WORDS = frozenset((
    'GFI', 'CONVENIENCE', 'SWITCHED', 'WP', 'COUNTERTOP',
))

# resolve_by tokens -> event-anchored deadline phrases (Ashghal / Indiana
# University stage-gate precedent; never date-anchored, the tool has no
# schedule). The stamped `resolve_by_label` is DEADLINES[token] so the web UI
# stays dumb.
DEADLINES = {
    'pre_pour':      'before the slab/deck pour',
    'steel_fab':     'before steel fabrication release',
    'duct_fab':      'before ductwork fabrication release',
    'gear_setting':  'before gear pads and feeder routing are frozen',
    'sleeve_pkg':    'with the sleeve/opening package for this level',
    'ceiling_close': 'before ceiling grid/close-in in this area',
    'next_cycle':    'this coordination cycle',
    'field':         'in the field at install',
}

# No-slack mover classes: systems that are genuinely irreducible (code-fixed
# slope or pressure / welded), NOT merely big. C3 (two no-slack systems collide
# -> Critical) requires BOTH sides here; a big air DUCT is rigidity-4 but can be
# rerouted with effort, so duct-vs-gravity-pipe is Major (M1), not Critical
# (V4 deep dive: C3 was over-ranking air-vs-gravity crossings).
NO_SLACK_KLASSES = frozenset((
    'grease', 'gravity', 'gravity_vent', 'condensate', 'medgas', 'fp_dry'))

# Slope/gravity code citations by mover class. The class IS a measured fact
# (from system classification), so citing here honors "cite only when
# measured" (doctrine 2 of the v2 plan).
SLOPE_CODE_BY_KLASS = {
    'gravity':      'IPC 704.1',
    'grease':       'IMC 506.3.7',
    'gravity_vent': 'IPC 905.2',
    'condensate':   'IMC 307.2.1',
    'medgas':       'NFPA 99',
}

# Phase 4 clearance-rule code citations. The zone test IS the measurement (the
# intrusion is the measured fact), so each cite is earned (doctrine 2). The
# composer reads these rather than inlining the clause numbers.
CLEARANCE_CODE_BY_RULE = {
    'C-NEC':      'NEC 110.26(E)(1)(a)',   # dedicated equipment space
    'C-NEC-W':    'NEC 110.26(A)',         # working space in front of gear
    'M-NEC-PROT': 'NEC 110.26(E)(1)(b)',   # foreign systems above, protected
    'M-SPR':      'NFPA 13 10.2.7',        # sprinkler obstruction clearance
}
