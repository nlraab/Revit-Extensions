# -*- coding: utf-8 -*-
"""PNG export from a Revit View, sized for thumbnails or full screenshots.

Wraps Document.ExportImage with sensible defaults. Output lives under
<project>/viewpoints/ and is referenced by relative path from each Clash's
viewpoints list.
"""


def export_png(doc, view, out_path, pixel_size=512, fit_to_section_box=True):
    """Render `view` to a PNG at `out_path`.

    `pixel_size` is the longer edge in pixels; aspect ratio is preserved.
    `fit_to_section_box` zooms the export to the view's section box rather
    than the full crop region (useful for clash thumbnails).
    """
    raise NotImplementedError


def thumbnail_path(project_dir, clash_id, viewpoint_id):
    """Return the conventional relative path for a viewpoint thumbnail."""
    raise NotImplementedError
