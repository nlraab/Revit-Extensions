# -*- coding: utf-8 -*-
"""clearance - rules engine for code-required clearance zones.

STUB. Not implemented in the first release. The folder + module signatures
exist so that clearance support can be added later without restructuring
the package.

Concept: certain element categories (electrical panels, VAV boxes, water
heaters, etc.) are required by code or service practice to have an empty
volume around them ("36 in. front clearance for an electrical panel").
A clearance clash fires when something else - even another MEP element of
the same trade - encroaches on that volume.

This package will need:
  * rules.py    - JSON-driven rule definitions (per family, per category)
  * volumes.py  - Solid generation for each element's required clearance
  * detection   - lives in clash_detect.find_clearance_clashes (added later)

When adding clearance support, also extend clash_core.models.ClashKind.CLEARANCE
into Run Clash Test, the test library editor, and the Clash Browser filters.
"""
