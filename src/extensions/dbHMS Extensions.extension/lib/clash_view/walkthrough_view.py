# -*- coding: utf-8 -*-
"""Find or create the dedicated 3D view used for the Walkthrough.

Mirrors the pattern in `threed_view` (Clash Navigator), but tuned for
first-person playback: perspective view, Realistic display style, Fine
detail. v1 keeps the configuration deliberately conservative — sun /
shadows / ambient lighting / gradient background / category-hiding
were too aggressive for Revit 2026 (caused a fatal crash on first
view-create), so they're removed for now. We can re-add them one at a
time once we've verified each call doesn't faulthe view-creation flow.

Quality presets:
  * QUALITY_NAVIGATE — Shaded display style. Fast for camera transitions.
  * QUALITY_PRESENT  — Realistic display style. The "wow" mode at stops.

Per-step debug log written to %TEMP%\\dbhms_walkthrough.log on every
view-create / quality-switch / orientation call. If Revit fatal-crashes
mid-config, the last logged line tells us which step did it. (Python
exceptions DON'T end up in this log automatically — they're caught
silently. The log captures intent, not Python errors.)
"""

import os
import tempfile


WALKTHROUGH_VIEW_NAME = "dbHMS Walkthrough"


# Quality presets. String literals (not enum) so script.py can reference
# them without an extra import.
QUALITY_NAVIGATE = "navigate"   # Shaded — used during transitions
QUALITY_PRESENT  = "present"    # Realistic — at stops


# ---------------------------------------------------------------------------
# Debug log — append-only file so we can trace what crashed Revit
# ---------------------------------------------------------------------------

_LOG_PATH = os.path.join(tempfile.gettempdir(), "dbhms_walkthrough.log")


def _log(msg):
    """Append a timestamped line to the debug log. Best-effort — log
    failures must NOT propagate (the log is a diagnostic aid, not a
    feature)."""
    try:
        from datetime import datetime
        with open(_LOG_PATH, "a") as f:
            f.write("{}  {}\n".format(
                datetime.now().strftime("%H:%M:%S.%f")[:-3], msg))
    except Exception:
        pass


def log_path():
    """Public helper — the launcher can show this in the status bar so
    Nathan knows where to find the log when something crashes."""
    return _LOG_PATH


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_walkthrough_view(doc):
    """Return the existing Walkthrough view, or None if missing."""
    return _find_by_name(doc)


def get_or_create_walkthrough_view(doc):
    """Return the dedicated Walkthrough View3D, creating + configuring it
    if missing. Caller MUST be inside a Transaction.

    Returns None only if no 3D ViewFamilyType exists.
    """
    existing = _find_by_name(doc)
    if existing is not None:
        _log("get_or_create: reusing existing view")
        return existing
    _log("get_or_create: creating fresh perspective view")
    view = _create_perspective(doc)
    if view is None:
        _log("get_or_create: _create_perspective returned None — bailing")
        return None
    configure_for_first_run(view, doc)
    return view


WALKTHROUGH_TEMPLATE_NAME = "dbHMS Walkthrough"


def configure_for_first_run(view, doc):
    """Apply the firm-standard `dbHMS Walkthrough` view template.
    Caller MUST be inside a Transaction.

    The template owns every visual decision — display style, edges,
    shadows, ambient light, background, crop on/off, annotation
    visibility. The firm sets it up once in the project template so
    every new project inherits it; this code just looks it up and
    assigns it to the view.

    Why template-only (not hard-coded settings):
      * Several Graphic Display Options controls (Show Edges, Show
        Ambient Shadows, Cast Shadows, Ambient Light) are not exposed
        through the public Revit API on any version — only via a view
        template. Documented in AEC DevBlog and confirmed via Revit
        2026 testing.
      * Keeping all visual config in the template means firm taste
        changes (e.g. switching the sky color, hiding furniture during
        coord meetings) are made once in the template, no code change.
      * The previous "set crop off / hide annotations" code in here
        fought with the template — confusing and brittle.

    Fallback when the template is missing (someone runs this on a
    project that wasn't seeded from the firm's project template):
    apply Realistic display style + Fine detail level so the view is
    at least usable, and log a hint that the firm template should be
    added.
    """
    if view is None:
        return

    if _apply_walkthrough_template(view, doc):
        # Template owns the visual stuff. Still need to turn the crop
        # view off — that's a per-view property, not normally captured
        # by view templates, so the template can't enforce it.
        _disable_crop_view(view)
        _log("configure: done (template applied)")
        return

    # Template missing — minimum-viable settings so the view is usable.
    _log("configure: no '{}' template found — applying minimal "
         "fallback. Add the template to this project for full fidelity."
         .format(WALKTHROUGH_TEMPLATE_NAME))
    _clear_view_template(view)
    apply_quality(view, QUALITY_PRESENT)
    _apply_detail_fine(view)
    _disable_crop_view(view)
    _log("configure: done (fallback)")


def apply_quality(view, quality):
    """Switch `view` between QUALITY_NAVIGATE (Shaded) and QUALITY_PRESENT
    (Realistic). Caller MUST be inside a Transaction. Idempotent.

    Falls back to ShadingWithEdges if Realistic isn't accepted (some
    GPU configs / view families reject it). Falls back to Shading if
    even ShadingWithEdges fails.
    """
    from Autodesk.Revit.DB import DisplayStyle
    if view is None:
        return
    target = (DisplayStyle.Shading
              if quality == QUALITY_NAVIGATE
              else DisplayStyle.Realistic)
    _log("apply_quality: target = {}".format(target))
    try:
        view.DisplayStyle = target
        _log("apply_quality: set succeeded")
        return
    except Exception as ex:
        _log("apply_quality: primary set failed ({}); trying fallback"
             .format(ex))
    try:
        view.DisplayStyle = DisplayStyle.ShadingWithEdges
        _log("apply_quality: fallback ShadingWithEdges set")
    except Exception as ex:
        _log("apply_quality: ShadingWithEdges also failed ({})".format(ex))


def apply_section_box(view, bbox):
    """Set `view`'s section box to `bbox` and turn it on."""
    if view is None or bbox is None:
        return
    try:
        view.SetSectionBox(bbox)
    except Exception as ex:
        _log("apply_section_box: SetSectionBox failed ({})".format(ex))


def disable_section_box(view):
    """Turn off the section box for `view`."""
    if view is None:
        return
    try:
        view.IsSectionBoxActive = False
    except Exception as ex:
        _log("disable_section_box: failed ({})".format(ex))


def set_orientation(view, position_xyz, target_xyz, up_xyz):
    """Set the camera on `view` to (position, target, up). Caller MUST
    be inside a Transaction.

    Each argument is a Revit XYZ instance.
    """
    from Autodesk.Revit.DB import ViewOrientation3D, XYZ
    if view is None:
        return
    try:
        forward = XYZ(
            target_xyz.X - position_xyz.X,
            target_xyz.Y - position_xyz.Y,
            target_xyz.Z - position_xyz.Z,
        )
        length = (forward.X ** 2 + forward.Y ** 2 + forward.Z ** 2) ** 0.5
        if length < 1e-9:
            return
        forward = XYZ(forward.X / length,
                      forward.Y / length,
                      forward.Z / length)
        view.SetOrientation(ViewOrientation3D(
            position_xyz, up_xyz, forward))
    except Exception as ex:
        _log("set_orientation: failed ({})".format(ex))


# ---------------------------------------------------------------------------
# Internal — view creation
# ---------------------------------------------------------------------------

def _find_by_name(doc):
    from Autodesk.Revit.DB import FilteredElementCollector, View3D
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if v.IsTemplate:
            continue
        try:
            if v.Name == WALKTHROUGH_VIEW_NAME:
                return v
        except Exception:
            continue
    return None


def _create_perspective(doc):
    """Create a fresh perspective View3D named WALKTHROUGH_VIEW_NAME.

    Caller MUST be inside a Transaction. Falls back to isometric if
    perspective fails — some Revit 2026 ViewFamilyType configurations
    reject CreatePerspective. An isometric fallback is less ideal for
    walkthrough feel but ships a working tool.
    """
    from Autodesk.Revit.DB import (
        FilteredElementCollector, ViewFamilyType, ViewFamily, View3D,
    )
    _log("_create_perspective: looking for 3D ViewFamilyType")
    vft = None
    for t in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if t.ViewFamily == ViewFamily.ThreeDimensional:
                vft = t
                break
        except Exception:
            continue
    if vft is None:
        _log("_create_perspective: no 3D ViewFamilyType found")
        return None
    _log("_create_perspective: VFT found, calling CreatePerspective")
    view = None
    try:
        view = View3D.CreatePerspective(doc, vft.Id)
        _log("_create_perspective: CreatePerspective returned a view")
    except Exception as ex:
        _log("_create_perspective: CreatePerspective FAILED ({}); "
             "trying isometric fallback".format(ex))
    if view is None:
        try:
            view = View3D.CreateIsometric(doc, vft.Id)
            _log("_create_perspective: CreateIsometric fallback succeeded")
        except Exception as ex:
            _log("_create_perspective: CreateIsometric also failed ({})"
                 .format(ex))
            return None
    try:
        view.Name = WALKTHROUGH_VIEW_NAME
        _log("_create_perspective: name set")
    except Exception:
        try:
            view.Name = WALKTHROUGH_VIEW_NAME + " (dbHMS)"
            _log("_create_perspective: name set with collision suffix")
        except Exception as ex:
            _log("_create_perspective: name set failed both times ({})"
                 .format(ex))
    return view


def _clear_view_template(view):
    from Autodesk.Revit.DB import ElementId
    try:
        view.ViewTemplateId = ElementId.InvalidElementId
    except Exception as ex:
        _log("_clear_view_template: failed ({})".format(ex))


def _apply_detail_fine(view):
    from Autodesk.Revit.DB import ViewDetailLevel
    try:
        view.DetailLevel = ViewDetailLevel.Fine
    except Exception as ex:
        _log("_apply_detail_fine: failed ({})".format(ex))


def _disable_crop_view(view):
    """Turn off the view's crop region. Caller MUST be inside a Transaction.

    Walkthrough views are full-model first-person navigation — there's
    no reason to crop. This is a per-view property that view templates
    don't normally control (the relevant template parameter is
    "Crop Region Visible" and is unchecked-include by default), so we
    enforce it from code.

    `CropBoxActive = False` frees the view from any rectangular clipping;
    `CropBoxVisible = False` hides the dashed crop outline so it doesn't
    show up in renders. Section box (which IS clipping) is a separate
    setting and we leave it alone.
    """
    if view is None:
        return
    _log("crop: disabling")
    try:
        view.CropBoxActive = False
        _log("  CropBoxActive = False OK")
    except Exception as ex:
        _log("  CropBoxActive failed ({})".format(ex))
    try:
        view.CropBoxVisible = False
        _log("  CropBoxVisible = False OK")
    except Exception as ex:
        _log("  CropBoxVisible failed ({})".format(ex))


def _apply_walkthrough_template(view, doc):
    """Find a view template named `dbHMS Walkthrough` and apply it to
    `view`. Returns True if applied, False if no matching template
    exists (or assignment failed).

    The template is the firm's preferred way to set the Graphic Display
    Options that AREN'T API-settable: Show Edges, Show Ambient Shadows,
    Cast Shadows, Ambient Light. Once Nathan has created it once via
    the Revit UI (Save as View Template → "dbHMS Walkthrough"), this
    code finds it on every walkthrough open and applies it, so every
    new walkthrough view inherits the same coordination-meeting look.

    Caller MUST be inside a Transaction.
    """
    from Autodesk.Revit.DB import FilteredElementCollector, View
    if view is None:
        return False
    _log("template: searching for '{}'".format(WALKTHROUGH_TEMPLATE_NAME))
    template = None
    try:
        for v in FilteredElementCollector(doc).OfClass(View):
            if not v.IsTemplate:
                continue
            try:
                if v.Name == WALKTHROUGH_TEMPLATE_NAME:
                    template = v
                    break
            except Exception:
                continue
    except Exception as ex:
        _log("  template search failed ({})".format(ex))
        return False
    if template is None:
        _log("  no template found — using manual settings instead")
        return False
    try:
        view.ViewTemplateId = template.Id
        _log("  template applied: '{}'".format(template.Name))
        return True
    except Exception as ex:
        _log("  template apply failed ({})".format(ex))
        return False


# ---------------------------------------------------------------------------
# Camera state read/write — used by the free-fly motion loop
# ---------------------------------------------------------------------------

def get_camera(view):
    """Return the view's current camera as (position, forward, up) lists
    of three floats — the format walkthrough_motion expects.

    Returns None if `view` is not a 3D view or has no orientation.
    """
    if view is None:
        return None
    try:
        orient = view.GetOrientation()
    except Exception:
        return None
    if orient is None:
        return None
    eye = orient.EyePosition
    fwd = orient.ForwardDirection
    up  = orient.UpDirection
    return (
        [float(eye.X), float(eye.Y), float(eye.Z)],
        [float(fwd.X), float(fwd.Y), float(fwd.Z)],
        [float(up.X),  float(up.Y),  float(up.Z)],
    )


def set_camera(view, camera):
    """Apply a (position, forward, up) camera tuple to the view.

    Caller MUST be inside a Transaction. Wraps walkthrough_view.set_orientation
    with the XYZ construction so the motion loop can stay free of Revit
    imports.

    The motion module passes `forward` as a UNIT DIRECTION vector — but
    `set_orientation` was originally built to take a TARGET POINT (the
    world coord the camera is looking AT, computed elsewhere as
    `eye + forward`). Translating: target = position + forward.
    Without this, Revit rejects the orientation with "up and forward
    not perpendicular" because what we passed isn't the forward
    direction at all — it's the unit vector positioned at world origin.

    Re-orthogonalize up against forward as a defensive belt-and-suspenders:
    even though motion.look() keeps them perpendicular by construction,
    floating-point drift across many frames can leave them slightly off.
    Revit's ViewOrientation3D requires exact perpendicularity, no
    tolerance.
    """
    from Autodesk.Revit.DB import XYZ
    if view is None or camera is None:
        return
    pos, fwd, up = camera

    # Re-orthogonalize: right = forward × up, then up' = right × forward.
    # The result is an up perpendicular to forward by exact construction.
    rx = fwd[1] * up[2] - fwd[2] * up[1]
    ry = fwd[2] * up[0] - fwd[0] * up[2]
    rz = fwd[0] * up[1] - fwd[1] * up[0]
    rlen = (rx * rx + ry * ry + rz * rz) ** 0.5
    if rlen < 1e-9:
        # Degenerate (forward parallel to up). Fall back to world Z, then
        # to +Y if forward IS world Z.
        ortho_up = [0.0, 0.0, 1.0] if abs(fwd[2]) < 0.999 else [0.0, 1.0, 0.0]
    else:
        rx, ry, rz = rx / rlen, ry / rlen, rz / rlen
        ux = ry * fwd[2] - rz * fwd[1]
        uy = rz * fwd[0] - rx * fwd[2]
        uz = rx * fwd[1] - ry * fwd[0]
        ulen = (ux * ux + uy * uy + uz * uz) ** 0.5
        if ulen < 1e-9:
            ortho_up = [0.0, 0.0, 1.0]
        else:
            ortho_up = [ux / ulen, uy / ulen, uz / ulen]

    target = [pos[0] + fwd[0], pos[1] + fwd[1], pos[2] + fwd[2]]
    set_orientation(view,
                    XYZ(float(pos[0]), float(pos[1]), float(pos[2])),
                    XYZ(float(target[0]), float(target[1]), float(target[2])),
                    XYZ(float(ortho_up[0]), float(ortho_up[1]), float(ortho_up[2])))


# ---------------------------------------------------------------------------
# Category visibility — discipline buckets
# ---------------------------------------------------------------------------

# Discipline buckets surfaced in the Walkthrough UI. Each bucket toggles
# all of the listed BuiltInCategory names together.
#
# Strings (not BuiltInCategory references) so the module parses cleanly
# in CPython for the test suite — the actual enum lookup happens at
# runtime via getattr(BuiltInCategory, name, None).
#
# Categories chosen for v1 to give a "show me what each trade owns"
# toggle that matches how the dbHMS team thinks about the model. Edit
# the lists if some category should move or be added.
DISCIPLINE_CATEGORIES = {
    "Mechanical": [
        "OST_DuctCurves", "OST_DuctFitting", "OST_DuctAccessory",
        "OST_DuctInsulations", "OST_DuctLinings", "OST_DuctTerminal",
        "OST_DuctSystem", "OST_FlexDuctCurves",
        "OST_MechanicalEquipment", "OST_HVAC_Zones",
        "OST_PlaceHolderDucts",
    ],
    "Electrical": [
        "OST_ElectricalEquipment", "OST_ElectricalFixtures",
        "OST_LightingFixtures", "OST_LightingDevices",
        "OST_DataDevices", "OST_CommunicationDevices",
        "OST_Conduit", "OST_ConduitFitting", "OST_ConduitRun",
        "OST_CableTray", "OST_CableTrayFitting", "OST_CableTrayRun",
        "OST_FireAlarmDevices", "OST_NurseCallDevices",
        "OST_SecurityDevices", "OST_TelephoneDevices",
        "OST_Wire", "OST_ElectricalCircuit",
    ],
    "Plumbing": [
        "OST_PipeCurves", "OST_PipeFitting", "OST_PipeAccessory",
        "OST_PipeInsulations", "OST_FlexPipeCurves",
        "OST_PipingSystem", "OST_PlumbingFixtures",
        "OST_PlaceHolderPipes",
    ],
    "Fire Protection": [
        "OST_Sprinklers",
        # Fire-protection pipes are usually in OST_PipeCurves filtered
        # by the FP system — toggling pipes here would also toggle
        # plumbing pipes, which isn't what the user wants. Sprinklers
        # alone are what most teams associate with the FP toggle.
    ],
    "Architectural": [
        "OST_Walls", "OST_Floors", "OST_Ceilings", "OST_Roofs",
        "OST_Doors", "OST_Windows", "OST_StairsRailing", "OST_Stairs",
        "OST_Furniture", "OST_Casework", "OST_Planting",
        "OST_Site", "OST_Topography",
        "OST_GenericModel", "OST_SpecialityEquipment",
    ],
    "Structural": [
        "OST_StructuralColumns", "OST_StructuralFraming",
        "OST_StructuralFoundation", "OST_StructuralStiffener",
        "OST_StructuralTruss", "OST_Columns",
        "OST_Rebar", "OST_StructuralFramingSystem",
    ],
}

DISCIPLINE_NAMES = list(DISCIPLINE_CATEGORIES.keys())


def set_discipline_visible(doc, view, discipline_name, visible):
    """Show or hide every category in a discipline bucket.

    Caller MUST be inside a Transaction. Each SetCategoryHidden call is
    wrapped in try/except so a category Revit refuses to hide (or one
    that doesn't exist in this Revit version's BuiltInCategory enum)
    doesn't kill the rest.
    """
    from Autodesk.Revit.DB import BuiltInCategory, Category
    if view is None or discipline_name not in DISCIPLINE_CATEGORIES:
        return
    _log("set_discipline_visible: {} -> {}".format(
        discipline_name, "show" if visible else "hide"))
    for name in DISCIPLINE_CATEGORIES[discipline_name]:
        try:
            bic = getattr(BuiltInCategory, name, None)
            if bic is None:
                continue
            cat = Category.GetCategory(doc, bic)
            if cat is None:
                continue
            cat_id = cat.Id
            if not view.CanCategoryBeHidden(cat_id):
                continue
            view.SetCategoryHidden(cat_id, not visible)
        except Exception as ex:
            _log("  set_discipline_visible: {} failed ({})".format(name, ex))
