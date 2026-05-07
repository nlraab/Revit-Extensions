# -*- coding: utf-8 -*-
"""Find or create the dedicated 3D view used for clash navigation.

We don't want to mutate the user's saved "{3D}" view every time they click
through clashes — it would mess with their personal section boxes and
visibility settings. Instead we own a single 3D view named "Clash
Navigator" that lives in the project (visible in the Project Browser) and
reuse it across every Show in 3D click. Section box updates as the user
clicks different clashes; the view itself is persistent.

Isometric (orthographic) rather than perspective: orthographic views are
trivially rotatable in Revit's view cube without weird foreshortening, and
SetSectionBox + ZoomToFit just works without explicit camera positioning.
A perspective variant for the Walkthrough use case can come later.

Revit imports are inside function bodies so this module parses cleanly in
CPython 3 for the test suite.
"""


# Public name so callers can refer to the view in user-facing messages.
NAVIGATOR_VIEW_NAME = "Clash Navigator"


def get_or_create_navigator_view(doc):
    """Return the View3D named NAVIGATOR_VIEW_NAME.

    Looks up an existing view by name first; if missing, creates a new
    isometric 3D view. Caller MUST be inside a Transaction when creation
    is possible (i.e. always, since we can't tell from outside whether the
    view exists).

    Returns None if no 3D ViewFamilyType is available in the project to
    create a view from — vanishingly unlikely but worth the defensive check.
    """
    existing = find_navigator_view(doc)
    if existing is not None:
        return existing
    return _create_navigator_view(doc)


def find_navigator_view(doc):
    """Look up the existing Clash Navigator view, returning None if missing.

    Use this (instead of `get_or_create_navigator_view`) when the caller
    only wants to operate on an existing view — e.g. cleanup paths that
    shouldn't materialize a new empty view as a side effect.
    """
    return _find_navigator_view(doc)


def find_three_d_view_family_type(doc):
    """Return any 3D ViewFamilyType in the document, or None.

    Used by viewpoint.generate_for_all when creating fresh per-clash
    views — having the VFT lookup live in this module keeps all the
    "find the 3D view scaffolding" knowledge in one place.
    """
    from Autodesk.Revit.DB import (
        FilteredElementCollector, ViewFamilyType, ViewFamily,
    )
    for t in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if t.ViewFamily == ViewFamily.ThreeDimensional:
                return t
        except Exception:
            continue
    return None


def is_navigator_view(view):
    """True if `view` is the dedicated clash navigator view (by name).

    We deliberately match by name rather than by an extensible-storage tag
    so users can find / inspect / delete the view through Revit's normal
    Project Browser UI. If they delete it, the next Show in 3D click just
    re-creates it.
    """
    from Autodesk.Revit.DB import View3D
    if view is None or not isinstance(view, View3D):
        return False
    try:
        return view.Name == NAVIGATOR_VIEW_NAME
    except Exception:
        return False


def set_section_box(view, bbox):
    """Set `view`'s section box to `bbox`. Caller MUST be inside a Transaction.

    SetSectionBox auto-enables IsSectionBoxActive in modern Revit so we
    don't have to flip that separately.

    No CropBox manipulation — the viewpoint pipeline (clash_view.viewpoint)
    handles CropBox setup on the throwaway per-clash temp views it creates,
    where having full control over the view's state matters. The
    interactive navigator view used by Show in 3D doesn't need a CropBox
    constraint — the user can see and rotate freely.
    """
    if view is None or bbox is None:
        return
    view.SetSectionBox(bbox)


def ensure_color_friendly_display_style(view):
    """Force `view` into a DisplayStyle that renders surface color overrides.

    Caller MUST be inside a Transaction. Idempotent.

    Why this exists: new View3Ds in Revit default to Hidden Line (HLR),
    and Hidden Line + Wireframe styles don't render surface foreground
    patterns — so the red+blue surface fills we apply in
    highlights.apply have no visible effect in those modes. The data is
    set correctly on the view; Revit just isn't drawing it. The result
    is "colors don't show on the first Show-in-3D click," which is
    surprising and bad. Force-bumping to ShadingWithEdges ensures the
    overrides actually paint.

    Respects the user's choice if they've already picked any other
    style that DOES render overrides (Shading, Realistic, etc.) — we
    only intervene when the current style would silently swallow our
    color work.
    """
    from Autodesk.Revit.DB import DisplayStyle
    if view is None:
        return
    try:
        current = view.DisplayStyle
    except Exception:
        current = None
    # Styles where SurfaceForegroundPatternColor is NOT rendered.
    no_color_styles = (DisplayStyle.Wireframe, DisplayStyle.HLR)
    if current is not None and current not in no_color_styles:
        return  # user picked something that works; leave it alone
    try:
        view.DisplayStyle = DisplayStyle.ShadingWithEdges
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_navigator_view(doc):
    from Autodesk.Revit.DB import FilteredElementCollector, View3D
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if v.IsTemplate:
            continue
        try:
            if v.Name == NAVIGATOR_VIEW_NAME:
                return v
        except Exception:
            continue
    return None


def _create_navigator_view(doc):
    """Create a fresh isometric View3D named NAVIGATOR_VIEW_NAME, configured
    for clash review.

    Caller MUST be inside a Transaction.

    Configuration applied at creation time:
      * Name           — NAVIGATOR_VIEW_NAME (or "{name} (dbHMS)" on collision)
      * ViewTemplate   — cleared, so project templates don't override our
                          explicit settings
      * DisplayStyle   — ShadingWithEdges so surface color overrides render
                          immediately on the first Show-in-3D click
      * DetailLevel    — Fine, so the user sees full geometry detail of
                          the clashing elements (per Nathan's request —
                          accuracy beats first-render speed)

    All settings are best-effort — Revit version differences or template
    locks might reject some of them, in which case we fall through and
    the view is still usable, just less optimized.
    """
    from Autodesk.Revit.DB import (
        FilteredElementCollector, ViewFamilyType, ViewFamily, View3D,
        ElementId, DisplayStyle, ViewDetailLevel,
    )
    vft = None
    for t in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if t.ViewFamily == ViewFamily.ThreeDimensional:
                vft = t
                break
        except Exception:
            continue
    if vft is None:
        return None
    try:
        view = View3D.CreateIsometric(doc, vft.Id)
    except Exception:
        return None
    try:
        view.Name = NAVIGATOR_VIEW_NAME
    except Exception:
        # Name collision (unlikely - we just searched). Append a marker so
        # the view is still usable; users will see the dbHMS-tagged copy.
        try:
            view.Name = NAVIGATOR_VIEW_NAME + " (dbHMS)"
        except Exception:
            pass
    # Clear any inherited view template so our explicit DisplayStyle /
    # DetailLevel / section-box settings stick rather than being
    # overridden by template values.
    try:
        view.ViewTemplateId = ElementId.InvalidElementId
    except Exception:
        pass
    # Color-friendly visual style — without this, the first Show in 3D
    # click renders the red+blue overrides invisibly. (Hidden Line and
    # Wireframe styles silently skip surface foreground patterns.)
    try:
        view.DisplayStyle = DisplayStyle.ShadingWithEdges
    except Exception:
        pass
    # Fine detail level so the user sees full element geometry in the
    # 10-ft cube section box. First activation pays a one-time render
    # cost; subsequent activations are cached.
    try:
        view.DetailLevel = ViewDetailLevel.Fine
    except Exception:
        pass
    return view
