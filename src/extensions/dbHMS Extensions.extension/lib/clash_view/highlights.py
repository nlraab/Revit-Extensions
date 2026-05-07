# -*- coding: utf-8 -*-
"""View-scoped element color overrides for the Clash Navigator view.

When show_clash frames a clash in the navigator view, we color the
host elements so the user can see at a glance which two elements are
the clash:

    Element A (ref_a) → red   (#E53E3E, same red as status:Open)
    Element B (ref_b) → blue  (#2B6CB0, the dbHMS primary blue)

Overrides are scoped to the Clash Navigator view only — the project's
other views (floor plans, the user's personal {3D}, etc.) are
untouched. The next show_clash call clears the previous pair before
painting the new pair, so the navigator view never accumulates stale
highlights across clicks.

Linked elements aren't colored. Revit's per-element override API
(`View.SetElementOverrides(ElementId, ...)`) only takes host
ElementIds, and reference-based link overrides are finicky enough to
defer (same trade-off as linked-element selection in navigate.py).
The linked element still appears inside the section box; it just
isn't colored.

State:
    Module-level _last_highlighted dict tracks {view_id: [eid_int, ...]}
    so a fresh show_clash call knows which IDs to clear. Module-level
    state survives multiple clicks within one pyRevit session. If state
    is lost (Revit reload, pyRevit reload), the previous overrides
    persist in the view as a harmless visual artifact until the next
    clash is shown — at which point we paint over them and start
    tracking again. Clearing on Browser close handles the "I closed
    the Browser and now my model is stuck colored" case explicitly.

Revit imports are inside function bodies so this module parses
cleanly in CPython 3 for the structural test suite.
"""

from clash_detect._compat import eid_int, make_eid


# Hex colors. Matched to the existing dbHMS palette tokens where they
# overlap so the navigator view feels consistent with the Browser UI.
HIGHLIGHT_A_HEX = "#E53E3E"   # red — same as status:Open
HIGHLIGHT_B_HEX = "#2B6CB0"   # blue — primary
HIGHLIGHT_LINE_WEIGHT = 5     # slightly bold so highlighted edges read

# {view_eid_int: [overridden eid_int, ...]} — what we painted last on each view.
_last_highlighted = {}


def apply(view, ids_for_a, ids_for_b):
    """Color the clash's host elements in `view`.

    Caller MUST be inside a Transaction. This function does NOT open
    its own transaction so the override + section-box update can
    commit together as a single undo step.

    `ids_for_a` and `ids_for_b` are lists of host-doc ElementIds
    (the actual Revit ElementId objects, not raw ints). Linked
    elements aren't colored — pass empty lists for those.
    """
    if view is None:
        return
    _clear_last(view)

    doc = view.Document
    settings_a = _override_settings(doc, HIGHLIGHT_A_HEX)
    settings_b = _override_settings(doc, HIGHLIGHT_B_HEX)

    new_tracked = []
    for elem_id in (ids_for_a or []):
        if _try_set(view, elem_id, settings_a):
            new_tracked.append(eid_int(elem_id))
    for elem_id in (ids_for_b or []):
        if _try_set(view, elem_id, settings_b):
            new_tracked.append(eid_int(elem_id))

    if new_tracked:
        _last_highlighted[eid_int(view.Id)] = new_tracked


def clear(view):
    """Clear the last clash's highlights from `view`. Idempotent.

    Caller MUST be inside a Transaction.
    """
    if view is None:
        return
    _clear_last(view)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _clear_last(view):
    """Reset overrides on whichever IDs we last tracked for this view."""
    from Autodesk.Revit.DB import OverrideGraphicSettings
    view_key = eid_int(view.Id)
    last = _last_highlighted.pop(view_key, [])
    if not last:
        return
    blank = OverrideGraphicSettings()
    for raw_eid in last:
        elem_id = make_eid(raw_eid)
        if elem_id is None:
            continue
        try:
            view.SetElementOverrides(elem_id, blank)
        except Exception:
            # Element may have been deleted between sessions — fine, the
            # override has nothing to clear from anyway.
            continue


def _try_set(view, elem_id, settings):
    """Apply `settings` to `elem_id` in `view`; return True on success."""
    if elem_id is None:
        return False
    try:
        view.SetElementOverrides(elem_id, settings)
        return True
    except Exception:
        return False


def _override_settings(doc, hex_color):
    """Build OverrideGraphicSettings: solid surface fill + matching lines.

    Sets BOTH SurfaceForeground AND SurfaceBackground pattern overrides
    so the color renders consistently across visual styles — different
    styles in Revit pick up different layers (Shading uses background;
    ShadingWithEdges blends both; Realistic ignores both and uses
    materials). Setting both makes the highlight reliable across
    whatever visual style the view ends up in.

    Also sets projection line color + heavier weight so the silhouette
    pops even in styles that don't render surface patterns at all.
    """
    from Autodesk.Revit.DB import OverrideGraphicSettings
    color = _color_from_hex(hex_color)
    fill_id = _solid_fill_id(doc)
    settings = OverrideGraphicSettings()
    if fill_id is not None:
        # Foreground layer
        try:
            settings.SetSurfaceForegroundPatternId(fill_id)
            settings.SetSurfaceForegroundPatternVisible(True)
            settings.SetSurfaceForegroundPatternColor(color)
        except Exception:
            pass
        # Background layer — covers visual styles that use this layer for
        # the surface fill (and prevents stripe-effects when both layers
        # are visible simultaneously).
        try:
            settings.SetSurfaceBackgroundPatternId(fill_id)
            settings.SetSurfaceBackgroundPatternVisible(True)
            settings.SetSurfaceBackgroundPatternColor(color)
        except Exception:
            pass
    try:
        settings.SetProjectionLineColor(color)
        settings.SetProjectionLineWeight(HIGHLIGHT_LINE_WEIGHT)
    except Exception:
        pass
    return settings


def _color_from_hex(hex_str):
    from Autodesk.Revit.DB import Color
    h = hex_str.lstrip('#')
    return Color(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _solid_fill_id(doc):
    """Find the project's solid-fill FillPatternElement, preferring Drafting target.

    Required for SetSurfaceForegroundPatternId / SetSurfaceBackgroundPatternId
    — without a solid fill pattern the surface override falls back to
    lines-only (which is still a useful highlight, just less vivid).

    Drafting-target solid fill is the standard one used for view
    overrides; Model-target solid fill exists in some templates but is
    intended for material patterns. Prefer Drafting; fall back to any
    solid fill so we still work in projects that don't have one.

    Returns None if no solid fill exists at all.
    """
    from Autodesk.Revit.DB import (
        FilteredElementCollector, FillPatternElement, FillPatternTarget,
    )
    drafting_solid = None
    any_solid = None
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            pattern = fp.GetFillPattern()
        except Exception:
            continue
        try:
            if not pattern.IsSolidFill:
                continue
        except Exception:
            continue
        if any_solid is None:
            any_solid = fp.Id
        try:
            if pattern.Target == FillPatternTarget.Drafting and drafting_solid is None:
                drafting_solid = fp.Id
        except Exception:
            pass
    return drafting_solid or any_solid
