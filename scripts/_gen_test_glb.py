# -*- coding: utf-8 -*-
"""Throwaway: build a tiny .glb (3 boxes, each with element metadata) using the
REAL GlbWriter/Mesh, so we can verify selection in viewer3.html via the preview.
Writes web/test_meta.glb. Not shipped; lives under scripts/ and is .gitignored
by intent (delete after).
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "src", "extensions", "dbHMS Extensions.extension", "lib")
sys.path.insert(0, LIB)

from clash_export.mesh import Mesh
from clash_export.gltf import GlbWriter

WEB = os.path.join(REPO, "src", "extensions", "dbHMS Extensions.extension",
                   "dbHMS Tools.tab", "Clash Detection.panel",
                   "3D Viewer.pushbutton", "web")


def box(cx, cy, cz, s=0.5):
    """Axis-aligned cube centered at (cx,cy,cz), edge 2s. Indexed, with normals."""
    # 8 corners
    c = [(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
         (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]
    pos = []
    for (x, y, z) in c:
        pos += [cx + x, cy + y, cz + z]
    # 12 triangles (no shared normals -> fine for a smoke test; viewer bakes flat)
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6),
             (0, 4, 5), (0, 5, 1), (1, 5, 6), (1, 6, 2),
             (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    idx = []
    for f in faces:
        idx += list(f)
    return pos, idx


def box_normals():
    """Flat per-vertex normals for the 8-corner indexed cube above (approx; just
    needs len==positions so GlbWriter emits a NORMAL accessor like the real export)."""
    # 8 corners -> 8 normals (radial unit vectors, normalized)
    import math as _m
    c = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
         (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
    out = []
    for (x, y, z) in c:
        l = _m.sqrt(x * x + y * y + z * z)
        out += [x / l, y / l, z / l]
    return out


def main():
    # Mirror the REAL export: per-vertex normals, building-scale offset coords,
    # many elements, metadata shaped exactly like rg._metadata_for_element output.
    w = GlbWriter(os.path.join(WEB, "test_meta.glb"), asset_extras={
        "generator": "dbHMS 3D Viewer (CustomExporter)", "units": "meters",
        "axis": "y_up_from_revit_z_up", "offset_ft": [0, 0, 0], "ft_to_m": 0.3048})
    cats = [("Pipes", "Plumbing", "MEP.rvt", (0.85, 0.30, 0.30)),
            ("Ducts", "Mechanical", "MEP.rvt", (0.30, 0.55, 0.85)),
            ("Walls", "Linked", "ARCH.rvt", (0.45, 0.75, 0.40))]
    n = 0
    nrm = box_normals()
    # spread across a ~40m x 40m footprint, 3 levels, like a real building
    for gx in range(8):
        for gz in range(8):
            ci = (gx + gz) % 3
            cat, ws, model, col = cats[ci]
            x = (gx - 3.5) * 5.0      # meters, building-scale
            z = (gz - 3.5) * 5.0
            y = (n % 3) * 4.0
            pos, idx = box(x, y, z, s=0.8)
            meta = {"element_id": 100000 + n, "category": cat, "name": cat[:-1] + " " + str(n),
                    "model": model, "workset": ws, "level": "L" + str((n % 3) + 1),
                    "family": cat + " Family", "creator": "nraab",
                    "discipline": "MEP" if model == "MEP.rvt" else "Arch"}
            w.add(Mesh(positions=pos, indices=idx, normals=nrm,
                       color=col, alpha=1.0, metadata=meta))
            n += 1
    size = w.finalize()
    print("wrote test_meta.glb:", size, "bytes,", n, "elements (with normals, offset coords)")


if __name__ == "__main__":
    main()
