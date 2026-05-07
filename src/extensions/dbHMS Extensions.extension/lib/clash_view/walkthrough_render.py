# -*- coding: utf-8 -*-
"""High-quality PNG render of the current walkthrough stop.

The walkthrough's "Render this stop" button captures the dbHMS
Walkthrough view at presentation quality (1920x1080, Realistic +
shadows + AO) and saves the PNG to
`<shared>/<project-hash>/walkthrough_renders/`.

This is NOT the same as the per-clash thumbnail in
`clash_view.snapshot` / `clash_view.viewpoint`:
  * thumbnail (snapshot.export)         — 800 px, used in the Browser
                                          detail panel and in BCF/XLSX
                                          exports. Optimized for size.
  * walkthrough render (this module)    — 1920 px, used for meeting
                                          handouts and screen sharing.
                                          Optimized for visual quality.

Filename: `clash-<seq>-<YYYYMMDD-HHMMSS>.png`. Timestamped (not just
keyed by clash id) because the user may render the same stop multiple
times during a meeting at different camera angles or zoom levels — we
want to keep all of them, not silently overwrite.

Caller is responsible for setting the view's camera to the correct
stop position before invoking. This module just exports.
"""

import os
from datetime import datetime

from clash_core import persistence


WALKTHROUGH_RENDER_DIRNAME = "walkthrough_renders"
DEFAULT_PIXEL_SIZE = 1920


def render_dir(project_hash):
    """Return the directory where walkthrough PNGs land for this project,
    creating it if missing."""
    d = os.path.join(persistence.project_dir(project_hash),
                     WALKTHROUGH_RENDER_DIRNAME)
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except Exception:
            pass
    return d


def render_filename(clash_dict, when=None):
    """Build the timestamped filename for a clash render. Pure data —
    unit-testable.

    `when` is a datetime instance for the timestamp, or None to use
    "now". Tests inject a fixed datetime so the filename is stable.
    """
    when = when or datetime.utcnow()
    seq = (clash_dict or {}).get('seq')
    seq_part = "{}".format(seq) if seq is not None else "x"
    stamp = when.strftime("%Y%m%d-%H%M%S")
    return "clash-{}-{}.png".format(seq_part, stamp)


def render_stop(doc, view, clash_dict, project_hash,
                pixel_size=DEFAULT_PIXEL_SIZE, when=None,
                use_raytrace=True):
    """Export `view` to a high-quality PNG for the current clash.

    Returns (success: bool, path: str or None, message: str).

    Caller MUST have already positioned the view's camera and applied
    its section box BEFORE calling. This routine just exports.

    `use_raytrace=True` (default) flips the view's display style to
    `DisplayStyle.RayTrace` for the duration of the export, then
    restores the original style. RayTrace is Revit's path-traced
    renderer — produces actual photorealistic output (PBR materials,
    GI, soft shadows) instead of the rasterized Realistic preview.
    Cost: a single 1920×1080 export takes 30s–2 min depending on
    scene complexity. Acceptable for a one-off render; not for live
    navigation. Set `use_raytrace=False` to use the current display
    style (Realistic) — much faster but less photorealistic.

    The display-style switch needs a transaction (it's a view
    property change). The export itself does NOT need one.
    """
    from Autodesk.Revit.DB import (
        ImageExportOptions, ImageFileType, ZoomFitType, ImageResolution,
        ElementId, ExportRange, Transaction, DisplayStyle,
    )
    from System.Collections.Generic import List as NetList

    if doc is None or view is None or not project_hash:
        return False, None, "Missing required arguments."

    out_dir = render_dir(project_hash)
    if not os.path.isdir(out_dir):
        return False, None, "Couldn't create render output directory."

    filename = render_filename(clash_dict, when=when)
    out_path = os.path.join(out_dir, filename)

    # Save the original display style so we can restore it after the
    # render. RayTrace is permanent on a view until you change it back,
    # and we want live navigation to stay on Realistic.
    original_style = None
    raytrace_applied = False
    if use_raytrace:
        try:
            original_style = view.DisplayStyle
        except Exception:
            original_style = None
        switch_txn = Transaction(doc, "dbHMS Walkthrough render → RayTrace")
        try:
            switch_txn.Start()
            view.DisplayStyle = DisplayStyle.RayTrace
            switch_txn.Commit()
            raytrace_applied = True
        except Exception:
            try:
                if switch_txn.HasStarted() and not switch_txn.HasEnded():
                    switch_txn.RollBack()
            except Exception:
                pass
            # RayTrace not available on this configuration — fall through
            # and render with whatever the view's current style is. Still
            # produces a usable image; just not photoreal.
            raytrace_applied = False

    options = ImageExportOptions()
    options.FilePath = out_path
    options.HLRandWFViewsFileType = ImageFileType.PNG
    options.ShadowViewsFileType   = ImageFileType.PNG
    options.ZoomType              = ZoomFitType.FitToPage
    options.PixelSize             = int(pixel_size)
    options.ImageResolution       = ImageResolution.DPI_300
    options.ExportRange           = ExportRange.SetOfViews
    options.SetViewsAndSheets(NetList[ElementId]([view.Id]))

    export_error = None
    try:
        doc.ExportImage(options)
    except Exception as ex:
        export_error = ex

    # Restore the original display style. ALWAYS run this, even if the
    # export failed — leaving the view stuck on RayTrace would lock up
    # live navigation.
    if raytrace_applied and original_style is not None:
        restore_txn = Transaction(doc, "dbHMS Walkthrough restore display style")
        try:
            restore_txn.Start()
            view.DisplayStyle = original_style
            restore_txn.Commit()
        except Exception:
            try:
                if restore_txn.HasStarted() and not restore_txn.HasEnded():
                    restore_txn.RollBack()
            except Exception:
                pass

    if export_error is not None:
        return False, None, "Export failed: {}".format(export_error)

    actual = _resolve_actual_path(out_path, out_dir, filename)
    if actual is None:
        return False, None, "Export produced no PNG file."

    note = " (ray-traced)" if raytrace_applied else ""
    return True, actual, "Render saved{}.".format(note)


def _resolve_actual_path(intended, out_dir, filename):
    """Revit's ExportImage sometimes appends a suffix like " - 3D View
    - <name>" to the file it actually writes. If our intended path
    doesn't exist, look for any PNG in `out_dir` whose name starts with
    the intended filename's stem and rename it onto the intended path.

    Returns the final path if found, None if nothing matches.
    """
    if os.path.isfile(intended):
        return intended
    stem = os.path.splitext(filename)[0]
    try:
        for name in os.listdir(out_dir):
            if not name.lower().endswith(".png"):
                continue
            if not name.startswith(stem):
                continue
            src = os.path.join(out_dir, name)
            try:
                if os.path.exists(intended):
                    os.remove(intended)
                os.rename(src, intended)
                return intended
            except Exception:
                # Couldn't rename — return the actual filename so the
                # caller still gets a usable path.
                return src
    except Exception:
        return None
    return None
