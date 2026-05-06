# -*- coding: utf-8 -*-
"""clash_walkthrough - full-screen, clash-aware navigation of a 3D view.

The Walkthrough pushbutton's runtime lives here. Two modes share one
WPF window:

  1. Clash Navigator mode - flip through every open clash, one at a time.
     Each step calls clash_view.navigate.show_clash and waits for next /
     prev / status-change input.

  2. Free-Fly mode - WASD + mouse look (or Xbox left-stick + right-stick),
     with clash markers as colored spheres at each clash midpoint.

Submodules:
    xinput  - P/Invoke wrappers for the Win32 XInput API (Xbox controller)
    camera  - convert input deltas (stick / mouse / WASD) into View camera
              position + orientation changes
    modes   - the Clash Navigator and Free-Fly mode controllers
    render  - hide Revit chrome, set visual style + shadows + AO, restore
              everything on exit
"""
