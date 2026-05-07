# -*- coding: utf-8 -*-
"""clash_view - viewport, section box, and snapshot manipulation.

This is the bridge between the clash database and what Revit shows on
screen. Given a Clash, this package can:
  * navigate to it (zoom, section box, isolate)
  * capture a Viewpoint (camera + section box state) and a PNG thumbnail
  * restore a previously saved Viewpoint
  * create / find a dedicated 3D view to host clash navigation

Submodules:
    geometry     - bounding-box math (transform / union / pad), independent of
                   Revit document state, host vs linked coordinate handling
    threed_view  - find or create the dedicated "Clash Navigator" 3D view
    highlights   - per-element graphic overrides scoped to the navigator view
                   (paint clash element A red, B blue; clear on next click /
                    Browser close)
    navigate     - the "show this clash" entry point + clear_highlights cleanup
    snapshot     - PNG export of a 3D view to disk (used by viewpoint capture)
    viewpoint    - capture-for-clash: render a thumbnail + record camera +
                   section box state on the clash dict. Single viewpoint per
                   clash (overwrite on save). Restore is deferred to a future
                   iteration (the saved data is restore-ready, just no caller
                   yet).

All five submodules are implemented as of Iteration 6.
"""
