# -*- coding: utf-8 -*-
"""The two walkthrough modes: Clash Navigator and Free-Fly.

Both modes share the same window, the same camera state, and the same
clash database. Only the input bindings and the on-screen overlay differ.
A toggle (gamepad START or keyboard Tab) flips between modes.
"""


# ---------------------------------------------------------------------------
# Clash Navigator
# ---------------------------------------------------------------------------

class ClashNavigatorMode(object):
    """Step-through controller for "next clash / prev clash / mark X".

    Holds the filtered list of clashes and the current index. On each
    step, calls clash_view.navigate.show_clash to update the view.
    """

    def __init__(self, doc, clashes):
        raise NotImplementedError

    def next_clash(self):
        raise NotImplementedError

    def prev_clash(self):
        raise NotImplementedError

    def current_clash(self):
        raise NotImplementedError

    def set_status(self, new_status):
        """Update status of the current clash and persist."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Free-Fly
# ---------------------------------------------------------------------------

class FreeFlyMode(object):
    """Continuous-camera controller for walking / flying through the model.

    Renders clash markers (colored spheres) at each clash midpoint as
    overlay graphics, with the controller able to jump to the nearest
    one or the next-by-id one.
    """

    def __init__(self, doc, clashes):
        raise NotImplementedError

    def update(self, dt_seconds, input_state):
        """Advance camera state by dt and process any input events."""
        raise NotImplementedError

    def jump_to_nearest_clash(self):
        raise NotImplementedError

    def jump_to_next_clash(self):
        raise NotImplementedError
