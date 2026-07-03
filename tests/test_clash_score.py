# -*- coding: utf-8 -*-
"""Unit tests for lib/clash_score: the importance engine (Layer A noise
suppression + Layer B constraint-first tiers).

The worked scenarios here mirror CLASH_IMPORTANCE_RESEARCH.md section 9.10;
if a change moves one of them to a different band, that is a design change
and the research doc should move with it. The two invariants that the whole
design hangs on get their own named tests:

  - CLAMP: the within-band sub-score can never push a clash across a band
    boundary (band changes require a rule-input change, never a weight).
  - NULL PRINCIPLE: a suppression rule fires only when every feature it
    tests is non-null; old/degraded records can never be suppressed by data
    they lack, and they still classify into a sensible band.
"""
import copy
import os
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import clash_score
from clash_score import defaults as score_defaults
from clash_core import merge
from clash_core.identity import clash_fingerprint


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def ref(source='host', element_id=1, category='Pipes', name=None, **extra):
    r = {
        'source': source, 'element_id': element_id, 'category': category,
        'category_id': None, 'name': name or category,
        'link_doc_title': None, 'unique_id': None, 'fed_key': None,
    }
    r.update(extra)
    return r


def clash(ref_a, ref_b, kind='hard', midpoint=None, **extra):
    c = {
        'id': 'x', 'seq': 1, 'test_id': 't1', 'kind': kind,
        'status': 'Open', 'ref_a': ref_a, 'ref_b': ref_b,
        'midpoint': midpoint or [0.0, 0.0, 0.0],
        'gap_inches': None, 'is_contact': None,
    }
    c.update(extra)
    return c


def score_one(c, config=None):
    clash_score.score_all([c], config=config)
    return c['importance']


BEAM = dict(source='link:Structural', element_id=900,
            category='Structural Framing', name='W21 beam')
ARCH_WALL = dict(source='link:Architectural', element_id=901,
                 category='Walls', name='Corridor wall')


# ---------------------------------------------------------------------------
# Rigidity classification
# ---------------------------------------------------------------------------

class RigidityTests(unittest.TestCase):
    def rig(self, **kw):
        c = clash(ref(**kw), ref(**BEAM))
        return score_one(c)['features']['rigidity_a']

    def test_gravity_main_is_5(self):
        self.assertEqual(self.rig(sys_class='Sanitary', dims_in=[4.0]), 5)

    def test_gravity_small_is_4(self):
        self.assertEqual(self.rig(sys_class='Sanitary', dims_in=[2.0]), 4)

    def test_gravity_unknown_dia_is_conservative_rung_4(self):
        # Fittings mostly report no dims; defaulting them to 5 would make
        # every sanitary elbow vs framing a Critical C1 (review finding).
        self.assertEqual(self.rig(sys_class='Sanitary'), 4)

    def test_gravity_fitting_vs_beam_is_major_not_critical(self):
        fit = ref(category='Pipe Fittings', sys_class='Sanitary')
        imp = score_one(clash(fit, ref(**BEAM)))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M2'))
        self.assertEqual(imp['confidence'], 'degraded')

    def test_vent_rigidity_by_size(self):
        # Stage 2: a known-small vent branch regrades cheaply (rig 2);
        # main-size or unknown-diameter vents stay conservative at 4.
        self.assertEqual(self.rig(sys_class='Vent', dims_in=[2.0]), 2)
        self.assertEqual(self.rig(sys_class='Vent', dims_in=[4.0]), 4)
        self.assertEqual(self.rig(sys_class='Vent'), 4)

    def test_storm_by_abbreviation_backstop(self):
        # Revit has no Storm classification; storm-as-Other must be caught
        # by the abbreviation backstop.
        self.assertEqual(self.rig(sys_class='Other', sys_abbr='ST',
                                  dims_in=[6.0]), 5)

    def test_storm_by_name_backstop(self):
        self.assertEqual(self.rig(sys_class='Other',
                                  sys_name='Storm Drainage 01',
                                  dims_in=[6.0]), 5)

    def test_sloped_pipe_is_gravity_signal(self):
        self.assertEqual(self.rig(sys_class='Other', slope=0.0104,
                                  dims_in=[6.0]), 5)

    def test_grease_duct_is_5(self):
        self.assertEqual(self.rig(category='Ducts',
                                  sys_name='Kitchen Exhaust 1',
                                  sys_class='Exhaust Air',
                                  dims_in=[18.0, 18.0]), 5)

    def test_big_duct_is_4_mid_3_small_2(self):
        self.assertEqual(self.rig(category='Ducts', dims_in=[30.0, 20.0]), 4)
        self.assertEqual(self.rig(category='Ducts', dims_in=[16.0, 12.0]), 3)
        self.assertEqual(self.rig(category='Ducts', dims_in=[8.0, 6.0]), 2)

    def test_pressure_pipe_by_size(self):
        self.assertEqual(self.rig(sys_class='Domestic Cold Water',
                                  dims_in=[4.0]), 3)
        self.assertEqual(self.rig(sys_class='Domestic Cold Water',
                                  dims_in=[1.5]), 2)

    def test_conduit_by_size(self):
        self.assertEqual(self.rig(category='Conduits', dims_in=[1.5]), 1)
        self.assertEqual(self.rig(category='Conduits', dims_in=[0.75]), 0)

    def test_tray_flex_sprinkler_equipment(self):
        self.assertEqual(self.rig(category='Cable Trays'), 3)
        self.assertEqual(self.rig(category='Flex Ducts'), 0)
        self.assertEqual(self.rig(category='Sprinklers'), 1)
        self.assertEqual(self.rig(category='Mechanical Equipment'), 4)

    def test_mounted_fixtures_are_cheap_movers(self):
        # Stage-1 retune: troffers/diffusers/wall-hung sanitary are class
        # 'fixture' rig 2, no longer rig-4 equipment.
        self.assertEqual(self.rig(category='Lighting Fixtures'), 2)
        self.assertEqual(self.rig(category='Air Terminals'), 2)
        self.assertEqual(self.rig(category='Electrical Fixtures'), 2)

    def test_fixture_with_sanitary_system_never_becomes_a_gravity_main(self):
        # A wall-hung WC can carry a Sanitary classification; the mounted
        # check runs before the gravity branch.
        self.assertEqual(self.rig(category='Plumbing Fixtures',
                                  sys_class='Sanitary'), 2)

    def test_category_fallback_marks_degraded(self):
        c = clash(ref(category='Pipes'), ref(**BEAM))
        imp = score_one(c)
        self.assertEqual(imp['features']['rigidity_src'], 'category')
        self.assertEqual(imp['confidence'], 'degraded')
        self.assertIn('estimated from categories', imp['reason'])


# ---------------------------------------------------------------------------
# Fixed elements and the mover
# ---------------------------------------------------------------------------

class MoverTests(unittest.TestCase):
    def test_structure_is_never_the_mover(self):
        c = clash(ref(sys_class='Sanitary', dims_in=[4.0]), ref(**BEAM))
        imp = score_one(c)
        self.assertEqual(imp['features']['mover'], 'a')
        self.assertEqual(imp['features']['fixed_b'], 'structural')

    def test_structural_category_inside_arch_link_reads_structural(self):
        # Category decides before link role: structure delivered inside the
        # architect's model must not take the arch demotion.
        b = ref(source='link:Architectural', element_id=7,
                category='Structural Framing')
        c = clash(ref(sys_class='Sanitary', dims_in=[4.0]), b)
        imp = score_one(c)
        self.assertEqual(imp['features']['fixed_b'], 'structural')
        self.assertEqual(imp['rule'], 'C1')

    def test_between_two_mep_the_lower_rigidity_moves(self):
        duct = ref(element_id=1, category='Ducts', dims_in=[24.0, 20.0])
        pipe = ref(element_id=2, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[4.0])
        imp = score_one(clash(duct, pipe))
        self.assertEqual(imp['features']['mover'], 'b')

    def test_mep_category_inside_a_link_is_never_the_mover(self):
        # The architect's duct is not dbHMS's to move, even though its
        # category is MEP (review finding: link-side elements were scored
        # as the cheap mover, demoting real conflicts).
        grav = ref(sys_class='Sanitary', dims_in=[4.0])
        their_duct = ref(source='link:Architectural', element_id=7,
                         category='Ducts', dims_in=[8.0, 6.0])
        imp = score_one(clash(grav, their_duct))
        self.assertEqual(imp['features']['fixed_b'], 'architectural')
        self.assertEqual(imp['features']['mover'], 'a')
        self.assertEqual(imp['band'], 'Major')

    def test_mep_category_in_structural_link_keeps_c1(self):
        grav = ref(sys_class='Sanitary', dims_in=[4.0])
        their_conduit = ref(source='link:Structural', element_id=7,
                            category='Conduits', dims_in=[0.75])
        imp = score_one(clash(grav, their_conduit))
        self.assertEqual((imp['band'], imp['rule']), ('Critical', 'C1'))

    def test_unknown_link_counterpart_never_gets_penetration_wording(self):
        grav = ref(sys_class='Sanitary', dims_in=[4.0])
        mystery = ref(source='link:Electrical Consultant', element_id=7,
                      category='Generic Models')
        imp = score_one(clash(grav, mystery))
        self.assertNotEqual(imp['rule'], 'N1')
        self.assertEqual(imp['band'], 'Major')


# ---------------------------------------------------------------------------
# Layer A
# ---------------------------------------------------------------------------

class LayerATests(unittest.TestCase):
    def test_r_self(self):
        a = ref(element_id=5, unique_id='u5')
        b = ref(element_id=5, unique_id='u5')
        imp = score_one(clash(a, b))
        self.assertTrue(imp['suppressed'])
        self.assertEqual(imp['suppress_rule'], 'R-SELF')

    def test_r_not_ours(self):
        imp = score_one(clash(ref(**BEAM), ref(**ARCH_WALL)))
        self.assertTrue(imp['suppressed'])
        self.assertEqual(imp['suppress_rule'], 'R-NOT-OURS')

    def test_r_sys_needs_a_fitting(self):
        a = ref(element_id=1, category='Pipes', sys_name='SAN 1')
        b = ref(element_id=2, category='Pipe Fittings', sys_name='SAN 1')
        imp = score_one(clash(a, b))
        self.assertEqual(imp['suppress_rule'], 'R-SYS')
        # Two curves of the same system crossing (a genuine routing error)
        # must NOT be suppressed.
        b2 = ref(element_id=2, category='Pipes', sys_name='SAN 1')
        imp2 = score_one(clash(a, b2))
        self.assertFalse(imp2['suppressed'])

    def test_r_sys_null_system_fails_open(self):
        a = ref(element_id=1, category='Pipes')
        b = ref(element_id=2, category='Pipe Fittings')
        self.assertFalse(score_one(clash(a, b))['suppressed'])

    def test_r_field_conduit_vs_conduit_only(self):
        a = ref(element_id=1, category='Conduits', dims_in=[0.75])
        b = ref(element_id=2, category='Conduits', dims_in=[0.75])
        imp = score_one(clash(a, b))
        self.assertEqual(imp['suppress_rule'], 'R-FIELD')
        # Conduit against another trade stays visible (scores low instead).
        duct = ref(element_id=3, category='Ducts', dims_in=[16.0, 12.0])
        imp2 = score_one(clash(a, duct))
        self.assertFalse(imp2['suppressed'])

    def test_r_field_congestion_escape(self):
        # The same conduit pair inside a 12-clash cluster is NOT field-fix:
        # congestion converts field noise into rack-level rework.
        rows = []
        for i in range(12):
            a = ref(element_id=100 + i, category='Conduits', dims_in=[0.75])
            b = ref(element_id=200 + i, category='Conduits', dims_in=[0.75])
            rows.append(clash(a, b, midpoint=[1.0 * (i % 3), 0.0, 0.0]))
        clash_score.score_all(rows)
        self.assertFalse(any(c['importance']['suppressed'] for c in rows))

    def test_r_graze_dormant_until_depth_ships(self):
        a = ref(element_id=1, category='Conduits', dims_in=[1.5])
        duct = ref(element_id=2, category='Ducts', dims_in=[16.0, 12.0])
        c = clash(a, duct)                      # no penetration_depth_in key
        self.assertFalse(score_one(c)['suppressed'])
        c2 = clash(a, duct, penetration_depth_in=0.2)
        imp2 = score_one(c2)
        self.assertEqual(imp2['suppress_rule'], 'R-GRAZE')

    def test_r_graze_never_fires_against_structure(self):
        pipe = ref(element_id=1, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[2.0])
        c = clash(pipe, ref(**BEAM), penetration_depth_in=0.2)
        self.assertFalse(score_one(c)['suppressed'])

    def test_suppress_override_beats_rules_both_ways(self):
        a = ref(element_id=5, unique_id='u5')
        b = ref(element_id=5, unique_id='u5')
        c = clash(a, b, suppress_override=False)   # force-show a self-clash
        self.assertFalse(score_one(c)['suppressed'])
        c2 = clash(ref(sys_class='Sanitary', dims_in=[4.0]), ref(**BEAM),
                   suppress_override=True)          # force-suppress a C1
        imp2 = score_one(c2)
        self.assertTrue(imp2['suppressed'])
        self.assertEqual(imp2['suppress_rule'], 'manual')

    def test_suppressed_rows_are_still_fully_scored(self):
        imp = score_one(clash(ref(**BEAM), ref(**ARCH_WALL)))
        self.assertTrue(imp['suppressed'])
        self.assertIn(imp['band'], ('Critical', 'Major', 'Minor'))
        self.assertTrue(imp['reason'])


# ---------------------------------------------------------------------------
# Tier scenarios (research doc section 9.10)
# ---------------------------------------------------------------------------

class TierScenarioTests(unittest.TestCase):
    def s1(self):
        return clash(ref(sys_class='Sanitary', sys_name='SAN 1',
                         dims_in=[4.0], name='Sanitary 4"'), ref(**BEAM))

    def test_s1_gravity_main_vs_beam_is_critical_c1(self):
        imp = score_one(self.s1())
        self.assertEqual((imp['band'], imp['rule']), ('Critical', 'C1'))
        self.assertIn('escalate_candidate', imp['flags'])
        self.assertIn('IPC 704.1', imp['reason'])

    def test_s2_duct_main_vs_domestic_is_major_m1(self):
        duct = ref(element_id=1, category='Ducts', dims_in=[24.0, 20.0])
        pipe = ref(element_id=2, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[4.0])
        imp = score_one(clash(duct, pipe))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M1'))

    def test_s8_tray_vs_beam_is_major_m2(self):
        tray = ref(category='Cable Trays', dims_in=[24.0, 4.0])
        imp = score_one(clash(tray, ref(**BEAM)))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M2'))

    def test_c2_huge_duct_vs_beam_is_critical(self):
        duct = ref(category='Ducts', dims_in=[30.0, 20.0])
        imp = score_one(clash(duct, ref(**BEAM)))
        self.assertEqual((imp['band'], imp['rule']), ('Critical', 'C2'))

    def test_mid_duct_vs_beam_is_major_not_critical(self):
        duct = ref(category='Ducts', dims_in=[20.0, 16.0])
        imp = score_one(clash(duct, ref(**BEAM)))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M2'))

    def test_c3_two_no_slack_systems_is_critical(self):
        grav = ref(element_id=1, sys_class='Sanitary', dims_in=[4.0])
        duct = ref(element_id=2, category='Ducts', dims_in=[30.0, 20.0])
        imp = score_one(clash(grav, duct))
        self.assertEqual((imp['band'], imp['rule']), ('Critical', 'C3'))

    def test_two_rigidity4_without_a_5_is_not_critical(self):
        duct1 = ref(element_id=1, category='Ducts', dims_in=[30.0, 20.0])
        duct2 = ref(element_id=2, category='Ducts', dims_in=[26.0, 20.0])
        imp = score_one(clash(duct1, duct2))
        self.assertEqual(imp['band'], 'Major')

    def test_s6_duct_through_arch_wall_is_penetration_candidate(self):
        duct = ref(category='Ducts', dims_in=[16.0, 12.0])
        imp = score_one(clash(duct, ref(**ARCH_WALL)))
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N1'))
        self.assertIn('penetration_candidate', imp['flags'])

    def test_n1_requires_an_actual_intersection(self):
        # A near miss to a wall is not an N1 penetration (review finding:
        # N1 was swallowing soft clashes with a wrong reason). Since
        # stage 2, a TIGHT near miss to an arch wall is N4 "imminent
        # penetration" (was M3 Major); a loose one stays plain FB.
        duct = ref(category='Ducts', dims_in=[16.0, 12.0])
        loose = clash(duct, ref(**ARCH_WALL), kind='soft', gap_inches=0.9,
                      tolerance_inches=1.0)
        imp = score_one(loose)
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'FB'))
        tight = clash(duct, ref(**ARCH_WALL), kind='soft', gap_inches=0.2,
                      tolerance_inches=1.0)
        imp2 = score_one(tight)
        self.assertEqual((imp2['band'], imp2['rule']), ('Minor', 'N4'))
        self.assertIn('penetration_candidate', imp2['flags'])

    def test_n1_never_fires_on_doors_or_stairs(self):
        # You cannot sleeve a stair: that is a routing error, not a
        # penetration candidate (review finding).
        duct = ref(category='Ducts', dims_in=[30.0, 20.0])
        stair = ref(source='link:Architectural', element_id=8,
                    category='Stairs')
        imp = score_one(clash(duct, stair))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M1'))

    def test_congested_arch_wall_zone_stays_on_the_sleeve_schedule(self):
        # REVERSED by the NIUHTC stage-1 retune (approved 2026-07-02): a
        # wall crossing inside a congested zone is still a sleeve item
        # (N1); the zone's congestion stays visible through its MEP-vs-MEP
        # members and the cluster group. Previously these promoted to M4.
        rows = []
        for i in range(25):
            duct = ref(element_id=100 + i, category='Ducts',
                       dims_in=[16.0, 12.0])
            wall = dict(ARCH_WALL)
            wall['element_id'] = 500 + i
            rows.append(clash(duct, ref(**wall), midpoint=[0.1 * i, 0.0, 0.0]))
        clash_score.score_all(rows)
        self.assertEqual(rows[0]['importance']['rule'], 'N1')

    def test_c2_clearance_arm_is_wired_dormant(self):
        # Design 9.4: a code-clearance test vs structure with a rigidity-4
        # mover escalates to C2 (dormant until clearance detection ships).
        duct = ref(category='Ducts', dims_in=[30.0, 20.0])
        c = clash(duct, ref(**BEAM), kind='clearance', gap_inches=2.0,
                  tolerance_inches=12.0)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Critical', 'C2'))

    def test_enriched_class_primary_categories_are_not_degraded(self):
        # Tray/sprinkler/equipment classify BY category by design; that is
        # not missing data (review finding: permanent 'estimated' chip).
        tray = ref(category='Cable Trays', dims_in=[24.0, 4.0],
                   sys_name='CT-1')
        imp = score_one(clash(tray, ref(**BEAM)))
        self.assertEqual(imp['confidence'], 'full')

    def test_s5_insulation_pinch_is_minor_n2_not_suppressed(self):
        pipe = ref(element_id=1, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[2.0], ins_in=1.0)
        duct = ref(element_id=2, category='Ducts', dims_in=[8.0, 6.0],
                   ins_in=0.0)
        c = clash(pipe, duct, kind='soft', gap_inches=0.5,
                  tolerance_inches=1.0)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N2'))
        self.assertFalse(imp['suppressed'])

    def test_m3_clearance_nearly_consumed(self):
        pipe = ref(element_id=1, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[2.0])
        duct = ref(element_id=2, category='Ducts', dims_in=[16.0, 12.0])
        c = clash(pipe, duct, kind='soft', gap_inches=0.2,
                  tolerance_inches=1.0)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M3'))

    def test_s4_half_gap_sprinkler_vs_tray_is_minor(self):
        spr = ref(element_id=1, category='Sprinklers')
        tray = ref(element_id=2, category='Cable Trays')
        c = clash(spr, tray, kind='soft', gap_inches=0.5,
                  tolerance_inches=1.0)
        imp = score_one(c)
        self.assertEqual(imp['band'], 'Minor')

    def test_clearance_kind_is_major_m_code(self):
        pipe = ref(element_id=1, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[2.0])
        c = clash(pipe, ref(**ARCH_WALL), kind='clearance', gap_inches=0.5,
                  tolerance_inches=36.0)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M-CODE'))

    def test_s12_old_record_categories_only_is_major_degraded(self):
        pipe = ref(category='Pipes')
        beam = ref(source='link:Structural', element_id=9,
                   category='Structural Framing')
        imp = score_one(clash(pipe, beam))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M2'))
        self.assertEqual(imp['confidence'], 'degraded')

    def test_n3_troffer_in_its_ceiling_is_a_mounting_check(self):
        troffer = ref(category='Lighting Fixtures', name='R8 Troffer')
        ceiling = ref(source='link:Architectural', element_id=60,
                      category='Ceilings', name='APC1 Acoustic Ceiling')
        imp = score_one(clash(troffer, ceiling))
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N3'))
        self.assertIn('mounting_check', imp['flags'])
        self.assertFalse(imp['suppressed'])          # visible, never hidden

    def test_n3_equipment_bearing_on_foundation(self):
        equip = ref(category='Mechanical Equipment', name='Water Heater')
        slab = ref(source='link:Structural', element_id=61,
                   category='Structural Foundations', name='7in slab')
        imp = score_one(clash(equip, slab))
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N3'))

    def test_n3_fixture_recessed_in_framing_but_equipment_vs_beam_stays_major(self):
        light = ref(category='Lighting Fixtures', name='S2 Recessed')
        imp = score_one(clash(light, ref(**BEAM)))
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N3'))
        # Genuine equipment against a BEAM is still steel coordination.
        ahu = ref(category='Mechanical Equipment', name='AHU-3')
        imp2 = score_one(clash(ahu, ref(**BEAM)))
        self.assertEqual(imp2['band'], 'Major')

    def test_n3_switchgear_in_a_wall_is_not_mounting_adjacency(self):
        gear = ref(category='Electrical Equipment', name='MSB-1 Switchboard')
        imp = score_one(clash(gear, ref(**ARCH_WALL)))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M1'))

    def test_switchgear_gate_matches_tokens_not_substrings(self):
        # CT-1 / CT.1 / CT1 / TX 1 are gear (stay M1); ATX-1 and a
        # manufacturer name containing 'mcc' are not (demote to N3).
        for name in ('CT-1', 'CT.1', 'CT1', 'TX 1', 'MSB-1'):
            gear = ref(category='Electrical Equipment', name=name)
            imp = score_one(clash(gear, ref(**ARCH_WALL)))
            self.assertEqual(imp['rule'], 'M1', name)
        for name in ('ATX-1 Air Terminal Box', 'McCarthy Panel 42'):
            not_gear = ref(category='Electrical Equipment', name=name)
            imp = score_one(clash(not_gear, ref(**ARCH_WALL)))
            self.assertEqual(imp['rule'], 'N3', name)

    def test_equipment_with_dims_never_flips_c2_on_its_foundation(self):
        # Latent guard: the day equipment bounding-box dims ship, a
        # pad-mounted AHU on its slab must stay a mounting check.
        ahu = ref(category='Mechanical Equipment', name='AHU-3',
                  dims_in=[60.0, 40.0])
        slab = ref(source='link:Structural', element_id=61,
                   category='Structural Foundations')
        imp = score_one(clash(ahu, slab))
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N3'))
        # ...and against a beam it is Major steel coordination, not C2.
        imp2 = score_one(clash(ahu, ref(**BEAM)))
        self.assertEqual((imp2['band'], imp2['rule']), ('Major', 'M2'))

    def test_n1_wins_over_congestion_for_wall_penetrations(self):
        # 25 pipe-through-wall clashes in one tight zone: congestion used
        # to promote these to M4 Major; they are sleeve items (retune P3).
        rows = []
        for i in range(25):
            pipe = ref(element_id=100 + i, unique_id='PP{0}'.format(i),
                       category='Pipes', sys_class='Domestic Cold Water',
                       dims_in=[2.0])
            wall = ref(source='link:Architectural', element_id=200 + i,
                       unique_id='WW{0}'.format(i), category='Walls')
            rows.append(clash(pipe, wall, midpoint=[0.1 * i, 0.0, 0.0]))
        clash_score.score_all(rows)
        for c in rows:
            self.assertEqual(c['importance']['rule'], 'N1',
                             c['importance']['rule'])

    def test_fixture_vs_rigid_curve_stays_major(self):
        # RCP-locked troffer vs a duct: the duct must move - real plenum
        # coordination, not a field fix (judge patch).
        light = ref(element_id=1, category='Lighting Fixtures')
        duct = ref(element_id=2, category='Ducts', dims_in=[16.0, 12.0])
        imp = score_one(clash(light, duct))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M1'))

    def test_fixture_vs_small_device_falls_to_flagged_field_fix(self):
        light = ref(element_id=1, category='Lighting Fixtures')
        pipe = ref(element_id=2, category='Pipes',
                   sys_class='Domestic Cold Water', dims_in=[1.0])
        imp = score_one(clash(light, pipe))
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'FB'))
        self.assertIn('field_fix', imp['flags'])

    def test_structural_links_walls_and_floors_read_structural(self):
        # The structural link's Walls/Floors are shear walls and decks:
        # a gravity main through one is C1, never a sleeve candidate.
        grav = ref(sys_class='Sanitary', dims_in=[4.0])
        deck = ref(source='link:Structural', element_id=70,
                   category='Floors', name='Concrete deck')
        imp = score_one(clash(grav, deck))
        self.assertEqual((imp['band'], imp['rule']), ('Critical', 'C1'))
        self.assertEqual(imp['features']['fixed_b'], 'structural')

    def test_config_rev_is_3(self):
        imp = score_one(clash(ref(sys_class='Sanitary', dims_in=[4.0]),
                              ref(**BEAM)))
        self.assertEqual(imp['config_rev'], 3)

    def test_c3_equipment_vs_gravity_is_major_not_critical(self):
        # Stage 2: equipment placement vs a gravity main is coordination
        # work, not two code-fixed routings.
        ahu = ref(element_id=1, category='Mechanical Equipment', name='AHU')
        grav = ref(element_id=2, sys_class='Sanitary', dims_in=[4.0])
        imp = score_one(clash(ahu, grav))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M1'))

    def test_small_vent_vs_storm_leader_is_major_not_critical(self):
        vent = ref(element_id=1, sys_class='Vent', dims_in=[2.0])
        storm = ref(element_id=2, sys_class='Other', sys_abbr='ST',
                    dims_in=[4.0])
        imp = score_one(clash(vent, storm))
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M1'))

    def test_n4_floor_drain_seated_at_slab(self):
        drain = ref(category='Pipe Fittings', sys_class='Sanitary',
                    name='Floor Drain')
        floor = ref(source='link:Architectural', element_id=80,
                    category='Floors')
        c = clash(drain, floor, kind='soft', gap_inches=0.0,
                  tolerance_inches=1.0, is_contact=True)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N4'))
        self.assertIn('mounting_check', imp['flags'])

    def test_n4_drain_at_foundation_slab(self):
        drain = ref(category='Pipe Accessories', sys_class='Sanitary',
                    name='4in ROOF DRAIN')
        slab = ref(source='link:Structural', element_id=81,
                   category='Structural Foundations')
        c = clash(drain, slab, kind='soft', gap_inches=0.0,
                  tolerance_inches=1.0, is_contact=True)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N4'))

    def test_n4_riser_near_arch_floor_is_an_imminent_penetration(self):
        pipe = ref(category='Pipes', sys_class='Domestic Cold Water',
                   dims_in=[2.0])
        floor = ref(source='link:Architectural', element_id=82,
                    category='Floors')
        c = clash(pipe, floor, kind='soft', gap_inches=0.2,
                  tolerance_inches=1.0)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N4'))
        self.assertIn('penetration_candidate', imp['flags'])

    def test_m3_stays_major_against_structure(self):
        pipe = ref(category='Pipes', sys_class='Domestic Cold Water',
                   dims_in=[2.0])
        c = clash(pipe, ref(**BEAM), kind='soft', gap_inches=0.2,
                  tolerance_inches=1.0)
        imp = score_one(c)
        self.assertEqual((imp['band'], imp['rule']), ('Major', 'M3'))

    def test_agenda_ordering_reads_like_a_veterans(self):
        s1 = score_one(self.s1())['score']
        tray = score_one(clash(ref(category='Cable Trays', dims_in=[24.0, 4.0]),
                               ref(**BEAM)))['score']
        pen = score_one(clash(ref(category='Ducts', dims_in=[16.0, 12.0]),
                              ref(**ARCH_WALL)))['score']
        self.assertTrue(s1 > tray > pen)


# ---------------------------------------------------------------------------
# Congestion
# ---------------------------------------------------------------------------

class CongestionTests(unittest.TestCase):
    def _rack(self, n, prev_rule=None):
        rows = []
        for i in range(n):
            a = ref(element_id=100 + i, category='Conduits', dims_in=[1.5])
            b = ref(element_id=300 + i, category='Pipes',
                    sys_class='Domestic Cold Water', dims_in=[1.0])
            c = clash(a, b, midpoint=[0.1 * i, 0.0, 0.0])
            if prev_rule:
                c['importance'] = {'rule': prev_rule}
            rows.append(c)
        clash_score.score_all(rows)
        return rows

    def test_m4_fires_in_a_congested_rack(self):
        rows = self._rack(25)
        self.assertEqual(rows[0]['importance']['rule'], 'M4')
        self.assertEqual(rows[0]['importance']['band'], 'Major')

    def test_m4_does_not_fire_below_threshold(self):
        rows = self._rack(10)
        self.assertNotEqual(rows[0]['importance']['rule'], 'M4')

    def test_m4_hysteresis_holds_between_thresholds(self):
        # 15 neighbors: below the 20 trigger, above the 12 release. A clash
        # that was M4 last run stays M4; a fresh one does not become M4.
        held = self._rack(16, prev_rule='M4')
        self.assertEqual(held[0]['importance']['rule'], 'M4')
        fresh = self._rack(16)
        self.assertNotEqual(fresh[0]['importance']['rule'], 'M4')


# ---------------------------------------------------------------------------
# The two structural invariants
# ---------------------------------------------------------------------------

class InvariantTests(unittest.TestCase):
    def test_clamp_score_never_leaves_its_band(self):
        # Max out every sub-term on a Minor-band clash: the score must stay
        # under 40. Band changes require a rule-input change, never weight.
        # (Midpoints spread far apart so congestion cannot promote to M4.)
        rows = []
        for i in range(30):
            duct = ref(element_id=100 + i, category='Ducts',
                       dims_in=[30.0, 20.0], ins_in=2.0)
            wall = dict(ARCH_WALL)
            wall['element_id'] = 500 + i
            c = clash(duct, ref(**wall), midpoint=[200.0 * i, 0.0, 0.0],
                      penetration_depth_in=4.0)
            rows.append(c)
        clash_score.score_all(rows)
        for c in rows:
            imp = c['importance']
            self.assertEqual((imp['band'], imp['rule']), ('Minor', 'N1'))
            lo, hi = {'Critical': (70, 99), 'Major': (40, 69),
                      'Minor': (8, 39)}[imp['band']]
            self.assertTrue(lo <= imp['score'] <= hi,
                            '{0} escaped {1}'.format(imp['score'], imp['band']))

    def test_null_principle_all_unknown_still_classifies(self):
        c = {'id': 'x', 'kind': 'hard', 'status': 'Open',
             'ref_a': {}, 'ref_b': {}, 'midpoint': None}
        clash_score.score_all([c])
        imp = c['importance']
        self.assertFalse(imp['suppressed'])
        self.assertIn(imp['band'], ('Critical', 'Major', 'Minor'))

    def test_scoring_never_raises_on_garbage(self):
        c = {'ref_a': None, 'ref_b': 'not-a-dict', 'kind': 7,
             'midpoint': 'nope'}
        clash_score.score_all([c])
        self.assertIn('importance', c)

    def test_brk_values_sum_to_features_sub_raw(self):
        imp = score_one(clash(ref(sys_class='Sanitary', dims_in=[4.0]),
                              ref(**BEAM)))
        self.assertEqual(sum(b['v'] for b in imp['brk']),
                         imp['features']['sub_raw'])


# ---------------------------------------------------------------------------
# Merge integration + fingerprint safety (the regression the verification
# pass found missing)
# ---------------------------------------------------------------------------

class MergeIntegrationTests(unittest.TestCase):
    def _raw(self):
        return {
            'test_id': 't1', 'kind': 'hard',
            'ref_a': ref(element_id=1, sys_class='Sanitary', dims_in=[4.0]),
            'ref_b': ref(**BEAM),
            'midpoint': [1.0, 2.0, 3.0],
            'tolerance_inches': 0.0,
        }

    def test_reserved_per_run_fields_materialize_as_none(self):
        merged, _ = merge.merge_runs([], [self._raw()])
        self.assertIn('penetration_depth_in', merged[0])
        self.assertIsNone(merged[0]['penetration_depth_in'])
        self.assertIn('overlap_volume_cf', merged[0])
        self.assertIn('tolerance_inches', merged[0])

    def test_enriched_refs_replace_on_persisting_rows(self):
        merged, _ = merge.merge_runs([], [self._raw()])
        old = copy.deepcopy(merged)
        raw2 = self._raw()
        raw2['ref_a']['sys_name'] = 'SAN 99'   # enrichment arrives on re-run
        merged2, summary = merge.merge_runs(old, [raw2])
        self.assertEqual(summary['persisting'], 1)
        again = [c for c in merged2 if c.get('status') != 'Resolved'][0]
        self.assertEqual(again['ref_a']['sys_name'], 'SAN 99')
        self.assertEqual(again['id'], merged[0]['id'])   # history preserved

    def test_fingerprint_ignores_enrichment_fields(self):
        bare_a = {'source': 'host', 'element_id': 1}
        bare_b = {'source': 'link:Structural', 'element_id': 900}
        rich_a = ref(element_id=1, sys_class='Sanitary', dims_in=[4.0],
                     ins_in=1.0, level='L3', discipline='Plumbing')
        rich_b = ref(**BEAM)
        rich_b.update({'sys_class': None, 'level': 'L3'})
        mid = [1.0, 2.0, 3.0]
        self.assertEqual(clash_fingerprint('t1', bare_a, bare_b, mid),
                         clash_fingerprint('t1', rich_a, rich_b, mid))

    def test_importance_block_survives_merge_on_persisting_rows(self):
        merged, _ = merge.merge_runs([], [self._raw()])
        clash_score.score_all(merged)
        self.assertIn('importance', merged[0])
        merged2, _ = merge.merge_runs(copy.deepcopy(merged), [self._raw()])
        live = [c for c in merged2 if c.get('status') != 'Resolved'][0]
        # deepcopy of the existing record keeps the block until rescoring...
        self.assertIn('importance', live)
        # ...and the standard flow rescores the merged list every run.
        clash_score.score_all(merged2)
        self.assertEqual(live['importance']['rule'], 'C1')


# ---------------------------------------------------------------------------
# Reporting + summary
# ---------------------------------------------------------------------------

class ReportTests(unittest.TestCase):
    def test_score_all_summary_counts(self):
        rows = [
            clash(ref(sys_class='Sanitary', dims_in=[4.0]), ref(**BEAM)),
            clash(ref(element_id=10, category='Conduits', dims_in=[0.75]),
                  ref(element_id=11, category='Conduits', dims_in=[0.75]),
                  midpoint=[500.0, 0.0, 0.0]),
        ]
        summary = clash_score.score_all(rows)
        self.assertEqual(summary['scored'], 2)
        self.assertEqual(summary['suppressed'], 1)
        self.assertEqual(summary['bands']['Critical'], 1)

    def test_calibration_report_shape(self):
        rows = [clash(ref(sys_class='Sanitary', dims_in=[4.0]), ref(**BEAM))]
        clash_score.score_all(rows)
        text = clash_score.calibration_report(rows, title='Test Project')
        self.assertIn('# Importance calibration report - Test Project', text)
        self.assertIn('Top 20 by score', text)
        self.assertIn('C1', text)

    def test_rescore_is_pure_and_repeatable(self):
        c = clash(ref(sys_class='Sanitary', dims_in=[4.0]), ref(**BEAM))
        first = dict(score_one(c))
        second = dict(score_one(c))
        self.assertEqual(first['score'], second['score'])
        self.assertEqual(first['rule'], second['rule'])


if __name__ == '__main__':
    unittest.main()
