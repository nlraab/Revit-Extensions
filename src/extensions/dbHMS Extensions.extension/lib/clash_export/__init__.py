# -*- coding: utf-8 -*-
"""clash_export - turn Revit geometry into a lightweight 3D file for the
3D Viewer tool.

Two layers, split so the data half is unit-testable under CPython 3 while
the Revit half only needs to parse there:

  * mesh.py          - plain Mesh container (positions / indices / color /
                       metadata). Pure data.
  * gltf.py          - glTF 2.0 binary (.glb) writer. Pure data, stdlib only.
  * revit_geometry.py - walks a Revit document, tessellates elements to
                       Mesh objects (meters, glTF Y-up, centered). Revit API,
                       lazy-imported inside functions.

This is part of the clash detection system's shared lib (the documented
`lib/clash_*` exception). The 3D Viewer lives in the Clash Detection panel
and will overlay clash data on the exported model in a later phase.
"""
