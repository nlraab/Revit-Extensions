# -*- coding: utf-8 -*-
"""Generate the Solid that represents an element's required clearance zone.
STUB - see clearance/__init__.py.

Approach: for each rule that matches an element, build a Solid in the
element's local coordinate frame, then transform it into shared coordinates
based on the element's location/orientation. The clearance Solid is then
fed into the same intersection machinery used by hard.py.

The "anchor" field on a rule controls which face the volume extrudes from
(front, back, top, bottom, left, right) - critical for things like panels
where only the front face needs the 36" clearance.
"""


def build_clearance_solid(element, rule):
    """Return a Solid in shared coordinates representing the clearance zone
    required around `element` by `rule`."""
    raise NotImplementedError
