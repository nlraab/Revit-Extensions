# -*- coding: utf-8 -*-
"""Tests for the PURE (Revit-free) helpers of clash_detect/clearance_engine.

The zone-building, solid-intersection, and FacingOrientation code is
Revit-runtime-only and cannot be exercised here; it is validated on a real
Revit run. These tests pin the NEC/NFPA math + classification that decide how
big each zone is and which rule an intruder gets -- the parts that must be
right BEFORE any geometry is built."""

import os
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_detect import clearance_engine as ce  # noqa: E402


class VoltageConditionTests(unittest.TestCase):
    def test_default_is_condition_1(self):
        # No voltage signal -> the safest (smallest) working space, so we never
        # invent a violation by over-sizing the box.
        self.assertEqual(ce.voltage_condition("Panelboard LP-1"), 1)
        self.assertEqual(ce.voltage_condition(""), 1)
        self.assertEqual(ce.voltage_condition(None), 1)

    def test_normal_lv_gear_stays_condition_1(self):
        # NEC Conditions are about what FACES the space, not voltage magnitude;
        # a 480 V panel is still Condition 1 (36 in) by default. Widening the
        # box from nominal voltage alone would invent working-space violations.
        self.assertEqual(ce.voltage_condition("480V MSB-1"), 1)
        self.assertEqual(ce.voltage_condition("277/480 Switchboard"), 1)

    def test_medium_voltage_is_condition_3(self):
        self.assertEqual(ce.voltage_condition("4160 Medium Voltage Switchgear"), 3)
        self.assertEqual(ce.voltage_condition("13.8KV Primary"), 3)

    def test_depth_by_condition(self):
        self.assertEqual(ce.working_depth_in(1), 36.0)
        self.assertEqual(ce.working_depth_in(2), 42.0)
        self.assertEqual(ce.working_depth_in(3), 48.0)
        self.assertEqual(ce.working_depth_in(99), 36.0)  # unknown -> default


class NfpaRadiusTests(unittest.TestCase):
    def test_three_times_dimension(self):
        self.assertEqual(ce.nfpa_radius_in(4.0), 12.0)

    def test_capped_at_24(self):
        self.assertEqual(ce.nfpa_radius_in(20.0), 24.0)

    def test_zero_or_bad_uses_floor(self):
        self.assertEqual(ce.nfpa_radius_in(0.0), 12.0)
        self.assertEqual(ce.nfpa_radius_in(None), 12.0)
        self.assertEqual(ce.nfpa_radius_in("nope"), 12.0)


class DedicatedCapTests(unittest.TestCase):
    def test_flat_six_feet_when_no_structure(self):
        self.assertEqual(ce.dedicated_cap_ft(7.0, None), 13.0)

    def test_structure_lowers_the_cap(self):
        # A slab 4 ft above the gear top caps the dedicated space there.
        self.assertEqual(ce.dedicated_cap_ft(7.0, 11.0), 11.0)

    def test_high_structure_does_not_raise_above_six_feet(self):
        self.assertEqual(ce.dedicated_cap_ft(7.0, 20.0), 13.0)


class OwnerClassificationTests(unittest.TestCase):
    def test_pad_is_not_a_zone_owner(self):
        self.assertFalse(ce.is_zone_owner("ELECTRICAL EQUIPMENT PAD"))

    def test_real_gear_is_a_zone_owner(self):
        for fam in ("SWITCHBOARD", "PANEL BOARD - RECESSED", "TRANSFORMER",
                    "DISTRIBUTION BOARD"):
            self.assertTrue(ce.is_zone_owner(fam), fam)


class FacingTrustTests(unittest.TestCase):
    def test_wall_hosted_is_trusted(self):
        self.assertTrue(ce.facing_trusted("face", "anything at all"))

    def test_free_panel_family_is_trusted(self):
        self.assertTrue(ce.facing_trusted("free", "SWITCHBOARD"))
        self.assertTrue(ce.facing_trusted("free", "PANEL BOARD - SURFACE"))

    def test_free_unknown_is_not_trusted(self):
        # A free-standing generic box with no panel signal: don't guess a face.
        self.assertFalse(ce.facing_trusted("free", "GENERIC ENCLOSURE"))
        self.assertFalse(ce.facing_trusted("free", "TRANSFORMER PAD"))


class LeakCapableTests(unittest.TestCase):
    def test_water_and_waste_are_leak_capable(self):
        self.assertTrue(ce.is_leak_capable("Domestic Cold Water", None, "Pipes"))
        self.assertTrue(ce.is_leak_capable("Sanitary", "SAN", "Pipes"))
        self.assertTrue(ce.is_leak_capable(None, "FP", "Pipes"))  # sprinkler pipe

    def test_ducts_and_conduit_are_not_leak_capable(self):
        self.assertFalse(ce.is_leak_capable("Supply Air", "SA", "Ducts"))
        self.assertFalse(ce.is_leak_capable(None, None, "Conduits"))

    def test_bare_pipe_is_conservatively_leak_capable(self):
        self.assertTrue(ce.is_leak_capable(None, None, "Pipes"))


class AxisClassificationTests(unittest.TestCase):
    def test_axis_aligned_x(self):
        self.assertEqual(ce._axis_of(0.99, 0.02), ("x", 1.0))
        self.assertEqual(ce._axis_of(-1.0, 0.0), ("x", -1.0))

    def test_axis_aligned_y(self):
        self.assertEqual(ce._axis_of(0.01, 0.999), ("y", 1.0))
        self.assertEqual(ce._axis_of(0.0, -1.0), ("y", -1.0))

    def test_diagonal_is_rejected(self):
        self.assertIsNone(ce._axis_of(0.7, 0.7))

    def test_zero_vector_is_rejected(self):
        self.assertIsNone(ce._axis_of(0.0, 0.0))


class DormantSafetyTests(unittest.TestCase):
    def test_find_intrusions_returns_empty_without_owners(self):
        # The dormant-safe contract: no owners -> no work, no error (this is the
        # M-SPR path on a model with zero modeled heads).
        self.assertEqual(
            ce.find_clearance_intrusions(None, [], None, [{}], "M-SPR", {}),
            [])

    def test_find_intrusions_returns_empty_without_intruders(self):
        self.assertEqual(
            ce.find_clearance_intrusions(None, [object()], None, [], "C-NEC", {}),
            [])


if __name__ == "__main__":
    unittest.main()
