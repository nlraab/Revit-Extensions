# -*- coding: utf-8 -*-
"""Mesh container shared between the Revit geometry extractor and the glTF
writer.

Pure data: no Revit, no .NET. Parses and runs under both CPython 3 (the
test suite) and IronPython 2.7 (Revit runtime), so keep it Python-2-safe.
"""


class Mesh(object):
    """A single renderable mesh: a flat triangle list plus metadata.

    positions: flat list of floats [x0, y0, z0, x1, y1, z1, ...] in METERS,
        in glTF axis convention (Y-up), already centered on the export
        offset. Three consecutive floats per vertex.
    indices:   optional flat list of ints. None means non-indexed: the
        positions are already triangle soup (every 3 vertices form one
        triangle).
    color:     (r, g, b) floats 0..1 for the mesh's base color.
    metadata:  dict of per-element info (element_id, category, discipline,
        workset, level, name) carried into the glTF node's 'extras' so the
        viewer can filter and identify elements later.
    """

    def __init__(self, positions, indices=None, color=(0.7, 0.7, 0.7),
                 metadata=None):
        self.positions = positions or []
        self.indices = indices
        self.color = color
        self.metadata = metadata or {}

    @property
    def vertex_count(self):
        return len(self.positions) // 3

    @property
    def triangle_count(self):
        if self.indices is not None:
            return len(self.indices) // 3
        return self.vertex_count // 3

    def bounds(self):
        """Return (min_xyz, max_xyz) lists over the positions, or
        (None, None) if there are no vertices."""
        if not self.positions:
            return None, None
        xs = self.positions[0::3]
        ys = self.positions[1::3]
        zs = self.positions[2::3]
        return ([min(xs), min(ys), min(zs)],
                [max(xs), max(ys), max(zs)])
