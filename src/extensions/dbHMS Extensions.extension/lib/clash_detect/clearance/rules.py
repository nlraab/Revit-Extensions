# -*- coding: utf-8 -*-
"""Clearance rule definitions. STUB - see clearance/__init__.py.

A rule will look something like:

    {
        "id": "elec-panel-front-36",
        "name": "Electrical Panel Front Clearance",
        "applies_to": {
            "category": "OST_ElectricalEquipment",
            "family_filter": "Panel*"
        },
        "volume": {
            "shape": "box",
            "anchor": "front_face",
            "size": {"depth_in": 36, "width_in": 30, "height_in": 78}
        }
    }

When implemented, rules will live in test_library.json under a "clearance_rules"
key, with the same global / per-project override pattern as clash tests.
"""


def load_rules():
    """Return the merged firm-wide + per-project clearance rules."""
    raise NotImplementedError


def applicable_rules_for_element(element, rules):
    """Return the subset of `rules` whose `applies_to` matches `element`."""
    raise NotImplementedError
