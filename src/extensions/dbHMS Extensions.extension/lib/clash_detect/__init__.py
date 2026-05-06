# -*- coding: utf-8 -*-
"""clash_detect - the detection algorithms.

This package is responsible for taking a list of ClashTest definitions plus
the active Revit document and returning a list of detected Clash dicts. It
does not own persistence (clash_core does), it does not own UI (the
pushbuttons do), it does not own viewport / section-box manipulation
(clash_view does). Pure detection.

Submodules:
    hard       - Revit's built-in InterferenceCheck path (host vs host,
                 host vs link)
    soft       - bounding-box-inflation overlap test for "near miss" clashes
    linked     - element collection + transform handling for linked docs
    clearance/ - rules engine for "this category needs N inches of clearance"
                 [stubbed for a future release; see clearance/__init__.py]
"""
