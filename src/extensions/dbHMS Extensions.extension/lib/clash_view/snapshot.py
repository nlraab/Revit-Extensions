# -*- coding: utf-8 -*-
"""PNG export from a Revit View, sized for the Browser's thumbnail panel.

Uses `Document.ExportImage` with `ExportRange.SetOfViews` and FitToPage
zoom. The export's framing is determined by the view's CropBox (see
`threed_view.set_section_box`, which sets CropBox to match the section
box with the proper view-local transform). Without the matching CropBox,
FitToPage falls back to the view's natural extent — project extents for
a 3D view — and every clash thumbnail looks zoomed-out from project
center.

Pure offscreen render — Revit produces the PNG by rendering the view's
geometry, NOT by reading screen pixels. Overlapping windows (the Clash
Browser, dialogs, tooltips) do NOT appear in the captured image. The
view doesn't even need to be the active view.

Output is a PNG at a deterministic per-clash path so saving a new
viewpoint for the same clash overwrites the previous file in place
(single-viewpoint-per-clash design for v1).

Revit imports are inside function bodies so this module parses cleanly
in CPython 3 for the structural test suite.
"""


# Default longer-edge size in pixels for the Browser's thumbnail panel.
# 800 produces a clean image at the 180-tall panel size and is small
# enough that the PNG file lands at ~30-80 KB — fine even with hundreds
# of clashes per project.
DEFAULT_PIXEL_SIZE = 800


def export(uidoc, view, out_path, pixel_size=DEFAULT_PIXEL_SIZE):
    """Render `view` to a PNG at `out_path` via Document.ExportImage.

    Returns True on success.

    Caller must have set the view's section box AND CropBox (typically
    via threed_view.set_section_box, which sets both with proper
    transform handling). The view's CropBox determines the export
    framing under FitToPage. The view does NOT need to be the active
    view — ExportImage with SetOfViews can render any view offscreen.

    `uidoc` is accepted (not just `doc`) so the call site is consistent
    with capture flows that DO need uidoc; we just pull doc out
    internally.
    """
    from Autodesk.Revit.DB import (
        ImageExportOptions, ImageFileType, ZoomFitType, ImageResolution,
        ElementId, ExportRange,
    )
    from System.Collections.Generic import List as NetList
    import os

    if uidoc is None or view is None or not out_path:
        return False
    doc = uidoc.Document if hasattr(uidoc, 'Document') else uidoc

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir)
        except Exception:
            return False

    options = ImageExportOptions()
    options.FilePath = out_path
    options.HLRandWFViewsFileType = ImageFileType.PNG
    options.ShadowViewsFileType   = ImageFileType.PNG
    options.ZoomType              = ZoomFitType.FitToPage
    options.PixelSize             = int(pixel_size)
    options.ImageResolution       = ImageResolution.DPI_150
    # CRITICAL: explicitly set ExportRange to SetOfViews. The default
    # in modern Revit is VisibleRegionOfCurrentView, which IGNORES
    # SetViewsAndSheets entirely and exports whatever view is currently
    # active. Without this line, every clash thumbnail was a snapshot
    # of the user's active view (sheet / default 3D / whatever) — same
    # image for every clash, none of them showing the clash itself.
    options.ExportRange = ExportRange.SetOfViews
    options.SetViewsAndSheets(NetList[ElementId]([view.Id]))

    try:
        doc.ExportImage(options)
    except Exception:
        return False

    if os.path.isfile(out_path):
        return True

    # Revit's ExportImage sometimes appends a suffix to the filename
    # (e.g. " - 3D View - <name>") when exporting a single view via
    # SetOfViews. Look for any PNG in the output dir whose basename
    # starts with our intended stem and rename it to the path we
    # promised our caller.
    stem = os.path.splitext(os.path.basename(out_path))[0]
    try:
        for name in os.listdir(out_dir):
            if name == os.path.basename(out_path):
                return True
            if name.lower().endswith(".png") and name.startswith(stem):
                src = os.path.join(out_dir, name)
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    os.rename(src, out_path)
                    return True
                except Exception:
                    return False
    except Exception:
        return False
    return False
