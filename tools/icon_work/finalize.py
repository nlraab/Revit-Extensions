# -*- coding: utf-8 -*-
"""Write the final 3D Viewer + Parameters Management icons to their icon.png."""
import os
from gen_icons import make_3d_viewer, make_parameters

REPO = r"C:\Users\natha\src\nlraab\Revit-Extensions"
TAB = os.path.join(REPO, r"src\extensions\dbHMS Extensions.extension\dbHMS Tools.tab")

viewer = os.path.join(TAB, r"Clash Detection.panel\3D Viewer.pushbutton\icon.png")
params = os.path.join(TAB, r"BIM Tools.panel\Parameters Management.pushbutton\icon.png")

make_3d_viewer(viewer, (176, 42, 96, 255))      # C - crimson-purple
make_parameters(params, (43, 108, 176, 255))    # firm primary blue
