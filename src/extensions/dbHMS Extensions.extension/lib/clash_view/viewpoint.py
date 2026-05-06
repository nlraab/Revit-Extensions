# -*- coding: utf-8 -*-
"""Capture and restore Viewpoints (camera + section box + snapshot).

A Viewpoint is the data needed to put Revit back into the same on-screen
state later, plus a PNG thumbnail for the Clash Browser.

Stored in clashes.json as part of each Clash's history; the PNG itself
lives in <project>/viewpoints/ and is referenced by relative path.
"""


def capture(uidoc, view, save_snapshot=True):
    """Snapshot the current camera, section box, and (optionally) a PNG of `view`.

    Returns a Viewpoint dict (see clash_core.models.make_viewpoint) plus
    the PNG written to disk (if requested) at the path returned in the
    dict's `snapshot_relpath` field.
    """
    raise NotImplementedError


def restore(uidoc, viewpoint):
    """Apply `viewpoint` back to a 3D view: set camera position/target/up,
    apply the section box, optionally re-isolate. Inverse of capture()."""
    raise NotImplementedError
