# -*- coding: utf-8 -*-
"""Capture viewpoints — section box + element overrides + offscreen-rendered
PNG — for clashes.

Architecture (rewritten — see comments below for why):
  Each clash gets its OWN fresh isometric View3D, configured specifically
  for that clash, used to export a single PNG, then deleted. No view is
  shared across clashes during batch generation.

Why fresh-view-per-clash:
  Earlier iterations re-used a single "Clash Navigator" view and updated
  its section box / CropBox / highlights per clash inside per-clash
  transactions. That approach repeatedly produced the same thumbnail for
  every clash — symptom-consistent with section box / CropBox state
  contamination from one iteration to the next, possibly via inherited
  view template state, CropBox setter being silently rejected on a view
  with stale settings, or some other Revit-side persistence we couldn't
  see. The fix is to STOP reusing state — create a new view, set
  exactly the properties we want, export, delete.

  Cost: 2 transactions per clash (create + delete) plus the export
  itself. For 13 clashes that's ~5-10 seconds. For 100 clashes ~30-60
  seconds. Acceptable for a batch operation that runs once after
  detection or as a one-time catch-up on Browser open.

Per-view configuration (per clash):
  * Name           — unique "dbHMS_temp_<id>_<rand>" so two simultaneous
                     batches couldn't collide
  * ViewTemplate   — cleared (so project templates can't override our
                     section-box / CropBox)
  * DisplayStyle   — ShadingWithEdges so highlights render
  * DetailLevel    — Fine, so pipe / duct thicknesses render as actual
                     geometry (Coarse renders them as single-line
                     symbols, which loses the visual information
                     thumbnails exist to convey)
  * SectionBox     — clipped tight to the clash midpoint ± 5 ft
  * CropBox        — set to match the section box, with proper
                     view-local-coord conversion via the view's existing
                     CropBox.Transform (the iso projection). This is what
                     makes ExportImage's FitToPage frame the clash
                     tightly instead of zooming to project extents.
  * Element overrides — Element A red, Element B blue (host elements
                     only — Revit's per-element override doesn't take
                     ElementIds across documents)

Stored:
  * dict        — overwrites clash['viewpoints'] in clashes.json
  * PNG file    — at <shared>/<project-hash>/viewpoints/<clash-id>.png
                  (deterministic per clash → save = overwrite)
"""

import uuid as _uuid

from clash_core import models, persistence
from clash_view import geometry, highlights, snapshot, threed_view


def generate_for_all(uidoc, clash_dicts, role_map, project_hash,
                     captured_by=None, log=None, only_missing=True):
    """Batch-generate viewpoint thumbnails for every clash that needs one.

    Returns the count generated. Caller persists the modified
    clash_dicts back to disk (so the journal write is batched).

    `only_missing=True` (default): skip clashes whose PNG already exists
    on disk. Checking disk (not just the dict) means deleting the
    viewpoints folder is a complete reset.
    """
    if uidoc is None or not clash_dicts or not project_hash:
        return 0
    doc = uidoc.Document

    todo = []
    for clash in clash_dicts:
        if not clash:
            continue
        if not clash.get('id'):
            continue
        if only_missing and viewpoint_image_for(clash, project_hash) is not None:
            continue
        todo.append(clash)
    if not todo:
        return 0

    vft = threed_view.find_three_d_view_family_type(doc)
    if vft is None:
        if log:
            log("No 3D ViewFamilyType available — skipping viewpoint generation.")
        return 0

    if log:
        log("Generating viewpoints for {} clash(es)...".format(len(todo)))

    from clash_view.navigate import _resolve_ref, _host_id_list

    generated = 0
    for clash in todo:
        try:
            if _capture_one(doc, vft, clash, role_map, project_hash,
                            captured_by, _resolve_ref, _host_id_list, log):
                generated += 1
        except Exception as ex:
            if log:
                log("  - clash {} unexpected error: {}".format(
                    clash.get('id', '?'), ex))

    if log:
        log("Done — {}/{} viewpoint(s) generated.".format(generated, len(todo)))
    return generated


def capture_for_clash(uidoc, clash_dict, role_map, project_hash,
                      captured_by=None):
    """Save a viewpoint for a single clash — used by Browser's manual
    Save Viewpoint button. Same fresh-view-per-clash flow as the batch.

    Returns (success: bool, message: str). Caller persists clash_dict.
    """
    if uidoc is None or clash_dict is None or not project_hash:
        return False, "Missing required arguments (uidoc / clash / project)."

    doc = uidoc.Document
    if not clash_dict.get('id'):
        return False, "Clash has no id; can't determine viewpoint file path."

    vft = threed_view.find_three_d_view_family_type(doc)
    if vft is None:
        return False, "No 3D ViewFamilyType available in this project."

    from clash_view.navigate import _resolve_ref, _host_id_list

    if _capture_one(doc, vft, clash_dict, role_map, project_hash,
                    captured_by, _resolve_ref, _host_id_list, log=None):
        return True, "Viewpoint saved."
    return False, "Couldn't capture viewpoint — element resolution or view export failed."


def viewpoint_image_for(clash_dict, project_hash):
    """Absolute path of the saved viewpoint PNG, or None if not saved.

    Verifies the PNG file actually exists on disk (not just that the
    dict has a viewpoint entry) so a deleted-files reset triggers full
    regeneration on the next batch.
    """
    if not clash_dict or not project_hash:
        return None
    clash_id = clash_dict.get('id')
    if not clash_id:
        return None
    import os
    path = persistence.viewpoint_image_path(project_hash, clash_id)
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------------------
# Per-clash capture: create fresh view, configure, export, delete
# ---------------------------------------------------------------------------

def _capture_one(doc, vft, clash_dict, role_map, project_hash,
                 captured_by, resolve_fn, host_id_list_fn, log):
    """Create a fresh isometric View3D for this clash, export the PNG,
    then delete the view. Returns True on success.

    Each clash gets a brand-new view in a known-clean state — the section
    box / CropBox / highlights are set on a freshly-created View3D that
    has no leftover state from previous iterations. This is what makes
    each clash's thumbnail actually different from every other clash.
    """
    from Autodesk.Revit.DB import (
        Transaction, View3D, ElementId, DisplayStyle, ViewDetailLevel,
        BoundingBoxXYZ, XYZ,
    )

    clash_id = clash_dict['id']
    framed = _compute_framed_box(doc, clash_dict, role_map, resolve_fn)
    if framed is None:
        if log:
            log("  - clash {}: no usable bbox — skipped.".format(clash_id))
        return False

    a_info = resolve_fn(doc, clash_dict.get('ref_a'), role_map)
    b_info = resolve_fn(doc, clash_dict.get('ref_b'), role_map)
    a_host_ids = host_id_list_fn(a_info)
    b_host_ids = host_id_list_fn(b_info)

    # Stage transaction: create + configure the fresh view.
    temp_view = None
    create_txn = Transaction(doc, "dbHMS Stage clash viewpoint")
    try:
        create_txn.Start()
        temp_view = View3D.CreateIsometric(doc, vft.Id)

        # Unique name so concurrent batches (unlikely but possible)
        # don't collide on view name.
        try:
            temp_view.Name = "dbHMS_temp_{}_{}".format(
                str(clash_id)[:8], _uuid.uuid4().hex[:6])
        except Exception:
            pass

        # Drop any inherited view template so our explicit settings
        # below stick rather than being overridden.
        try:
            temp_view.ViewTemplateId = ElementId.InvalidElementId
        except Exception:
            pass

        # Visual style that renders surface color overrides.
        try:
            temp_view.DisplayStyle = DisplayStyle.ShadingWithEdges
        except Exception:
            pass

        # Fine detail so the user can see actual pipe / duct thickness
        # in the thumbnail — Coarse would render those as single-line
        # representations, which loses the "is the duct hitting the
        # wall?" information that's the whole point of the thumbnail.
        try:
            temp_view.DetailLevel = ViewDetailLevel.Fine
        except Exception:
            pass

        # Section box — clips geometry to just the clash region.
        temp_view.SetSectionBox(framed)

        # CropBox — what makes ExportImage's FitToPage frame the clash
        # tightly. The trap: for a 3D view, CropBox is in view-local
        # coordinates, not world. We have to read the view's existing
        # CropBox.Transform (the iso projection) and convert the
        # world-coord section box to view-local by transforming all 8
        # corners through the inverse, then refit an AABB.
        _set_crop_box_to_world_region(temp_view, framed)

        # Element color overrides (red / blue).
        highlights.apply(temp_view, a_host_ids, b_host_ids)

        create_txn.Commit()
    except Exception as ex:
        try:
            if create_txn.HasStarted() and not create_txn.HasEnded():
                create_txn.RollBack()
        except Exception:
            pass
        if log:
            log("  - clash {}: stage transaction failed: {}".format(clash_id, ex))
        # If we got far enough to create temp_view but the commit failed,
        # the view doesn't actually exist (rolled back). Nothing to clean
        # up.
        return False

    # Build the viewpoint dict from the view's state BEFORE deleting it
    # (otherwise the GetOrientation call below would fail on a deleted
    # view).
    viewpoint_dict = _build_viewpoint_dict(
        temp_view, framed, clash_id, captured_by, label='auto')

    # Export PNG. ExportImage doesn't require the view to be active —
    # SetOfViews points at temp_view explicitly. Pure offscreen render
    # so any overlapping windows (Browser, dialogs) don't appear.
    image_path = persistence.viewpoint_image_path(project_hash, clash_id)
    success = snapshot.export(doc, temp_view, image_path)

    # Always delete the temp view, even if the export failed — we don't
    # want to accumulate dbHMS_temp_* views in the project file.
    delete_txn = Transaction(doc, "dbHMS Delete temp clash view")
    try:
        delete_txn.Start()
        try:
            doc.Delete(temp_view.Id)
        except Exception:
            pass
        delete_txn.Commit()
    except Exception:
        try:
            if delete_txn.HasStarted() and not delete_txn.HasEnded():
                delete_txn.RollBack()
        except Exception:
            pass

    if not success:
        if log:
            log("  - clash {}: export failed.".format(clash_id))
        return False

    clash_dict['viewpoints'] = [viewpoint_dict]
    return True


def _set_crop_box_to_world_region(view, world_bbox):
    """Set `view`'s CropBox so it covers the world-coord region of `world_bbox`,
    converting through the view's existing CropBox.Transform.

    For a 3D View, CropBox.Min / .Max are in VIEW-LOCAL coordinates
    (relative to the view's projection plane). Setting them to world
    coords directly puts the crop somewhere completely unrelated to the
    world region. Correct procedure:
      1. Read the existing CropBox.Transform (encodes the projection)
      2. Invert it
      3. Transform all 8 corners of world_bbox through the inverse
      4. Refit an AABB around the 8 view-local points
      5. Build a new CropBox with the original Transform + the new
         view-local bounds
      6. Assign + activate

    Best-effort: if any step fails, the section box still does the
    geometry clipping; only the export framing degrades to project
    extents.
    """
    from Autodesk.Revit.DB import BoundingBoxXYZ, XYZ
    if view is None or world_bbox is None:
        return
    try:
        existing = view.CropBox
    except Exception:
        return
    if existing is None or existing.Transform is None:
        return
    try:
        t = existing.Transform
        inv = t.Inverse
        corners = [
            XYZ(world_bbox.Min.X, world_bbox.Min.Y, world_bbox.Min.Z),
            XYZ(world_bbox.Max.X, world_bbox.Min.Y, world_bbox.Min.Z),
            XYZ(world_bbox.Min.X, world_bbox.Max.Y, world_bbox.Min.Z),
            XYZ(world_bbox.Max.X, world_bbox.Max.Y, world_bbox.Min.Z),
            XYZ(world_bbox.Min.X, world_bbox.Min.Y, world_bbox.Max.Z),
            XYZ(world_bbox.Max.X, world_bbox.Min.Y, world_bbox.Max.Z),
            XYZ(world_bbox.Min.X, world_bbox.Max.Y, world_bbox.Max.Z),
            XYZ(world_bbox.Max.X, world_bbox.Max.Y, world_bbox.Max.Z),
        ]
        local = [inv.OfPoint(c) for c in corners]
        xs = [p.X for p in local]
        ys = [p.Y for p in local]
        zs = [p.Z for p in local]
        new_crop = BoundingBoxXYZ()
        new_crop.Transform = t
        new_crop.Min = XYZ(min(xs), min(ys), min(zs))
        new_crop.Max = XYZ(max(xs), max(ys), max(zs))
        view.CropBox = new_crop
    except Exception:
        return
    try:
        view.CropBoxActive = True
    except Exception:
        pass
    try:
        view.CropBoxVisible = False
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers shared with the navigator-view interactive flow
# ---------------------------------------------------------------------------

def _compute_framed_box(doc, clash_dict, role_map, resolve_fn):
    """Section-box framing for a clash — midpoint ± 5 ft, with fallback to
    padded element-bbox union if midpoint is missing."""
    midpoint = clash_dict.get('midpoint')
    framed = geometry.box_around_point(midpoint, half_size=5.0)
    if framed is not None:
        return framed
    boxes = []
    for ref_key in ('ref_a', 'ref_b'):
        info = resolve_fn(doc, clash_dict.get(ref_key), role_map)
        if info is None:
            continue
        elem, link_inst = info
        b = geometry.element_world_box(elem, link_inst)
        if b is not None:
            boxes.append(b)
    union = geometry.union_boxes(boxes)
    if union is None:
        return None
    return geometry.pad_box(union, pad_feet=2.0)


def _build_viewpoint_dict(view, section_box, clash_id, captured_by, label):
    """Build the viewpoint dict from the view's state. Must be called
    BEFORE the view is deleted (uses view.GetOrientation)."""
    orientation = None
    try:
        orientation = view.GetOrientation()
    except Exception:
        pass
    if orientation is not None:
        camera_position = orientation.EyePosition
        forward = orientation.ForwardDirection
        target = _add_xyz(camera_position, forward)
        up = orientation.UpDirection
    else:
        camera_position = None
        target = None
        up = None

    section_box_pair = None
    if section_box is not None:
        try:
            section_box_pair = (section_box.Min, section_box.Max)
        except Exception:
            pass

    rel_image = "viewpoints/{}.png".format(clash_id)
    vp = models.make_viewpoint(
        camera_position=camera_position,
        target=target,
        up_vector=up,
        section_box=section_box_pair,
        snapshot_relpath=rel_image,
        captured_by=captured_by,
    )
    vp['source'] = label
    return vp


def _add_xyz(a, b):
    if a is None or b is None:
        return None
    try:
        from Autodesk.Revit.DB import XYZ
        return XYZ(a.X + b.X, a.Y + b.Y, a.Z + b.Z)
    except Exception:
        return None
