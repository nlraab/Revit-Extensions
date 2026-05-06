# -*- coding: utf-8 -*-
"""Full-screen rendering setup: hide Revit chrome, set visual style, restore.

When walkthrough enters, we want:
  * the 3D view maximized to fill the screen
  * Project Browser, Properties palette, ribbon hidden
  * Visual Style = Consistent Colors, with Shadows ON, Ambient Occlusion ON,
    Sketchy Lines OFF
  * crop region disabled (we want to see the whole model from inside)

When walkthrough exits, we restore every UI element we hid plus the
view's pre-walkthrough display settings. Any failure during exit must
still attempt to restore as much as possible - never leave the user with
a broken Revit UI.
"""


def enter_walkthrough(uiapp, view):
    """Set up the full-screen, clash-friendly view state.
    Returns a `prior_state` dict that exit_walkthrough() needs to restore."""
    raise NotImplementedError


def exit_walkthrough(uiapp, prior_state):
    """Restore Revit's UI to the state captured by enter_walkthrough().

    Wraps every restore step in try/except - exit MUST be best-effort and
    cannot raise, even if some restore step fails.
    """
    raise NotImplementedError


def set_pretty_visual_style(view):
    """Apply Consistent Colors + shadows + AO. Best Revit-native render
    quality short of going Realistic (which is extremely slow)."""
    raise NotImplementedError
