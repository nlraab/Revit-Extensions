# -*- coding: utf-8 -*-
"""clash_view - viewport, section box, and snapshot manipulation.

This is the bridge between the clash database and what Revit shows on
screen. Given a Clash, this package can:
  * navigate to it (zoom, section box, isolate)
  * capture a Viewpoint (camera + section box state) and a PNG thumbnail
  * restore a previously saved Viewpoint
  * create / find a dedicated 3D view to host clash navigation

Submodules:
    navigate     - the "show this clash" entry point
    viewpoint    - serialize/restore camera + section box
    snapshot     - export PNG thumbnails
    threed_view  - find or create the dedicated "Clash Navigator" 3D view
"""
