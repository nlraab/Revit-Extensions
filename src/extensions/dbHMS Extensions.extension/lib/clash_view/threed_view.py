# -*- coding: utf-8 -*-
"""Find or create the dedicated 3D view used for clash navigation.

We don't want to mutate the user's saved "{3D}" view every time they click
through clashes - it would mess with their personal section boxes and
visibility settings. Instead we own a single hidden 3D view named
"Clash Navigator" that lives in the project, and reuse it.
"""


NAVIGATOR_VIEW_NAME = "Clash Navigator"


def get_or_create_navigator_view(doc):
    """Return the View3D named NAVIGATOR_VIEW_NAME, creating it if missing.

    The created view:
      - is a perspective 3D view
      - has Visual Style = Consistent Colors with shadows + AO on
      - has the same view template as a default 3D (to inherit categories)
      - is hidden from the project browser if possible (or named with a
        leading marker so users see it's a tool view)
    """
    raise NotImplementedError


def is_navigator_view(view):
    """True if `view` is the dedicated clash navigator view."""
    raise NotImplementedError
