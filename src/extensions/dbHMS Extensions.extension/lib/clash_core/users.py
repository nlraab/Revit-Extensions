# -*- coding: utf-8 -*-
"""Current-user resolution.

We never assign clashes to individual people - that's a deliberate scope
decision (assignment is by trade). But comments and history entries DO
record who authored them, so we need to know who's at the keyboard right
now.

Source of truth: Revit's `Application.Username` (the logged-in Autodesk
account). We allow per-machine override via `clash_core.config` in case the
display name we want differs from the Autodesk account name.
"""

from clash_core import config


def current_user(uiapp):
    """Return the display name for the user currently driving Revit.

    Prefers the per-machine config override (`user_display_name`) if set,
    falls back to `uiapp.Application.Username`, then to "Unknown" if even
    that lookup fails (test contexts, badly initialized uiapp, etc.).
    """
    cfg = config.load()
    override = cfg.get("user_display_name")
    if override:
        return override
    try:
        name = uiapp.Application.Username
        if name:
            return name
    except Exception:
        pass
    return "Unknown"
