# -*- coding: utf-8 -*-
"""Generate a synthetic test building (.glb) + metadata sidecar for developing
the View Range Helper web viewer WITHOUT Revit.

It reuses the repo's own pure-Python glTF writer (lib/clash_export/gltf.py +
mesh.py), so the output matches the real tool's export conventions exactly:

  * binary glTF (.glb), Y-up, METERS, geometry centered on an "export offset"
  * each element is one node; per-element info lives in the node's `extras`
    (element_id, category, level, name) -- same keys the real CustomExporter
    writes.

Coordinate convention (mirrors lib/clash_export/custom_export.py OnPolymesh):
    glTF_X = (revit_X - off_x) * FT_TO_M
    glTF_Y = (revit_Z - off_z) * FT_TO_M     <- UP comes from Revit Z
    glTF_Z = -(revit_Y - off_y) * FT_TO_M     <- Revit North (+Y) -> glTF -Z

So to place a horizontal plane at Revit absolute elevation E (feet) in the
viewer:  glTF_world_Y = (E - off_z) * FT_TO_M.

Run:  python tools/vr_fixtures/make_sample_glb.py
Outputs (next to the View Range Helper web/ folder, under web/sample/):
    sample_building.glb
    sample_meta.json
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
LIB = os.path.join(REPO, 'src', 'extensions', 'dbHMS Extensions.extension', 'lib')
sys.path.insert(0, LIB)

from clash_export.mesh import Mesh        # noqa: E402
from clash_export.gltf import GlbWriter   # noqa: E402

FT_TO_M = 0.3048

# Export offset (center of the model bbox, in Revit feet). Chosen so the building
# straddles the origin in glTF space, like a real centered export.
OFF = (30.0, 20.0, 18.0)   # (off_x, off_y, off_z)


def g(xr, yr, zr):
    """Revit feet -> glTF meters (Y-up, centered, north -> -Z)."""
    return ((xr - OFF[0]) * FT_TO_M,
            (zr - OFF[2]) * FT_TO_M,
            -(yr - OFF[1]) * FT_TO_M)


def revit_box(x0, x1, y0, y1, z0, z1, color, meta):
    """Axis-aligned box given Revit-feet extents. Returns a Mesh in glTF space
    with outward per-face normals (clean flat shading) and 12 triangles."""
    # Convert the 8 corners, then rebuild an AABB in glTF space (the mapping is
    # axis-aligned, so min/max per glTF axis define the same box).
    gs = [g(xr, yr, zr)
          for xr in (x0, x1) for yr in (y0, y1) for zr in (z0, z1)]
    gx = [p[0] for p in gs]; gy = [p[1] for p in gs]; gz = [p[2] for p in gs]
    ax0, ax1 = min(gx), max(gx)
    ay0, ay1 = min(gy), max(gy)
    az0, az1 = min(gz), max(gz)

    # 6 faces, 4 verts each, outward normal per face.
    faces = [
        # (normal, [4 corners ccw seen from outside])
        ((0, 0, 1),  [(ax0, ay0, az1), (ax1, ay0, az1), (ax1, ay1, az1), (ax0, ay1, az1)]),
        ((0, 0, -1), [(ax1, ay0, az0), (ax0, ay0, az0), (ax0, ay1, az0), (ax1, ay1, az0)]),
        ((1, 0, 0),  [(ax1, ay0, az1), (ax1, ay0, az0), (ax1, ay1, az0), (ax1, ay1, az1)]),
        ((-1, 0, 0), [(ax0, ay0, az0), (ax0, ay0, az1), (ax0, ay1, az1), (ax0, ay1, az0)]),
        ((0, 1, 0),  [(ax0, ay1, az1), (ax1, ay1, az1), (ax1, ay1, az0), (ax0, ay1, az0)]),
        ((0, -1, 0), [(ax0, ay0, az0), (ax1, ay0, az0), (ax1, ay0, az1), (ax0, ay0, az1)]),
    ]
    positions = []; normals = []; indices = []
    base = 0
    for normal, corners in faces:
        for c in corners:
            positions.extend(c)
            normals.extend(normal)
        indices.extend([base, base + 1, base + 2, base, base + 2, base + 3])
        base += 4
    # Default every element to the linked architectural model + its workset unless
    # the caller tags it otherwise (the host MEP model carries equipment/ducts/pipes).
    meta.setdefault("model", MODEL_ARCH)
    meta.setdefault("workset", "Architecture")
    meta.setdefault("discipline", "Architectural")   # drives gray-vs-colour in the tool
    return Mesh(positions, indices=indices, color=color, normals=normals,
                metadata=meta)


# ---------------------------------------------------------------------------
# Build a small 3-storey + roof office box with rooms, columns, openings.
# Footprint Revit X 0..60, Y 0..40. Levels at Z = 0, 12, 24; roof slab at 36.
# ---------------------------------------------------------------------------

# dbHMS works MEP-in-host + linked architecture, so the fixture mirrors that:
# the shell (walls/floors/roofs/columns) is a LINKED arch model, and the host
# model carries the MEP equipment the engineer toggles on to chase the cut plane.
MODEL_ARCH = "Sample-ARCH.rvt"   # linked architecture
MODEL_MEP  = "Sample-MEP.rvt"    # host MEP model

GREY   = (0.80, 0.80, 0.82)   # slabs
WALL   = (0.86, 0.84, 0.80)   # walls
COLM   = (0.55, 0.57, 0.60)   # columns
ROOFC  = (0.62, 0.64, 0.68)   # roof
# Vivid MEP colours so the flat Revit-colour rendering in the tool pops against
# the clay shell (real exports carry the element's own Revit material colour).
EQUIP  = (0.00, 0.62, 0.66)   # mechanical equipment - teal
DUCT   = (0.18, 0.45, 0.82)   # ducts - blue
PIPE   = (0.27, 0.70, 0.42)   # pipes - green

LEVELS = [
    {"id": 101, "name": "L1 - Ground", "elev_ft": 0.0},
    {"id": 102, "name": "L2 - Office", "elev_ft": 12.0},
    {"id": 103, "name": "L3 - Office", "elev_ft": 24.0},
    {"id": 104, "name": "Roof",        "elev_ft": 36.0},
]

meshes = []
eid = 1000


def add(m):
    global eid
    meshes.append(m)
    eid += 1


SLAB_T = 1.0      # slab thickness (ft)
WALL_T = 0.5      # wall thickness (ft)
WALL_H = 11.0     # wall height within a storey (ft), leaving a slab gap
DOOR_W = 3.5      # interior door gap (ft)

for li, lv in enumerate(LEVELS):
    z = lv["elev_ft"]
    lname = lv["name"]
    # Floor slab for every level (incl. roof slab at top).
    add(revit_box(0, 60, 0, 40, z - SLAB_T, z,
                  ROOFC if li == 3 else GREY,
                  {"element_id": eid, "category": ("Roofs" if li == 3 else "Floors"),
                   "level": lname, "name": ("Roof Slab" if li == 3 else "Floor Slab " + lname)}))
    if li == 3:
        # Parapet around the roof, then stop (no storey above the roof).
        add(revit_box(0, 60, 0, WALL_T, z, z + 3, WALL,
                      {"element_id": eid, "category": "Walls", "level": lname, "name": "Parapet S"}))
        add(revit_box(0, 60, 40 - WALL_T, 40, z, z + 3, WALL,
                      {"element_id": eid, "category": "Walls", "level": lname, "name": "Parapet N"}))
        add(revit_box(0, WALL_T, 0, 40, z, z + 3, WALL,
                      {"element_id": eid, "category": "Walls", "level": lname, "name": "Parapet W"}))
        add(revit_box(60 - WALL_T, 60, 0, 40, z, z + 3, WALL,
                      {"element_id": eid, "category": "Walls", "level": lname, "name": "Parapet E"}))
        continue

    zt = z + WALL_H
    # Exterior walls (perimeter).
    add(revit_box(0, 60, 0, WALL_T, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Ext Wall S"}))
    add(revit_box(0, 60, 40 - WALL_T, 40, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Ext Wall N"}))
    add(revit_box(0, WALL_T, 0, 40, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Ext Wall W"}))
    add(revit_box(60 - WALL_T, 60, 0, 40, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Ext Wall E"}))
    # Interior partition splitting the floor into two rooms, with a door gap.
    add(revit_box(30 - WALL_T / 2, 30 + WALL_T / 2, 0, 20 - DOOR_W, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Partition A"}))
    add(revit_box(30 - WALL_T / 2, 30 + WALL_T / 2, 20, 40, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Partition B"}))
    # A short east-west partition so the plan reads as rooms, not just a line.
    add(revit_box(0, 24, 24 - WALL_T / 2, 24 + WALL_T / 2, z, zt, WALL,
                  {"element_id": eid, "category": "Walls", "level": lname, "name": "Partition C"}))

# Columns running full height (one element each, all storeys).
for cx in (15, 45):
    for cy in (13, 27):
        add(revit_box(cx - 0.75, cx + 0.75, cy - 0.75, cy + 0.75, 0, 36, COLM,
                      {"element_id": eid, "category": "Columns", "level": "L1 - Ground",
                       "name": "Column {0},{1}".format(cx, cy)}))

# ---------------------------------------------------------------------------
# Host MEP model: equipment, ducts, pipes. These live in MODEL_MEP and let us
# exercise the category/model show-hide tree + flat Revit-colour rendering, and
# the "raise the cut plane to reveal a unit above it" workflow.
#   * Fan coil units sit WITHIN the L2 plan slice (z 13..15) so they show in plan.
#   * The duct run sits ABOVE the cut (z ~21) so it is hidden in plan until the
#     engineer raises the Cut/Top -- the whole point of the tool.
#   * Pipes start toggled OFF (hidden_categories) to prove a hidden category can
#     be switched back on inside the tool.
# ---------------------------------------------------------------------------
# Fan coil units (Mechanical Equipment, HVAC workset).
add(revit_box(8, 14, 28, 34, 13, 15, EQUIP,
              {"element_id": eid, "category": "Mechanical Equipment", "model": MODEL_MEP,
               "workset": "HVAC", "discipline": "Mechanical", "level": "L2 - Office", "name": "Fan Coil Unit L2"}))
add(revit_box(46, 52, 6, 12, 25, 27, EQUIP,
              {"element_id": eid, "category": "Mechanical Equipment", "model": MODEL_MEP,
               "workset": "HVAC", "discipline": "Mechanical", "level": "L3 - Office", "name": "Fan Coil Unit L3"}))
# Supply duct run just under the L2 ceiling (above the cut plane).
add(revit_box(5, 55, 30, 31.5, 20.5, 21.5, DUCT,
              {"element_id": eid, "category": "Ducts", "model": MODEL_MEP,
               "workset": "HVAC", "discipline": "Mechanical", "level": "L2 - Office", "name": "Supply Duct L2"}))
add(revit_box(20, 21.5, 8, 31.5, 20.5, 21.5, DUCT,
              {"element_id": eid, "category": "Ducts", "model": MODEL_MEP,
               "workset": "HVAC", "discipline": "Mechanical", "level": "L2 - Office", "name": "Branch Duct L2"}))
# Pipe run within the L2 slice, modelled as a HOLLOW square tube (4 thin walls
# with an empty core) like real Revit pipe geometry -- so the section-cut poche
# behaviour through the bore is exercised. Starts hidden two ways (Pipes category
# AND Plumbing workset) so the model/category/workset AND logic is tested too.
def _pipe_meta(name):
    return {"element_id": eid, "category": "Pipes", "model": MODEL_MEP,
            "workset": "Plumbing", "discipline": "Plumbing", "level": "L2 - Office", "name": name}
_pw = 0.08   # wall thickness
add(revit_box(5, 55, 8.0, 8.6, 13.1 - _pw, 13.1, PIPE, _pipe_meta("CHW Supply top")))
add(revit_box(5, 55, 8.0, 8.6, 12.5, 12.5 + _pw, PIPE, _pipe_meta("CHW Supply bottom")))
add(revit_box(5, 55, 8.0, 8.0 + _pw, 12.5, 13.1, PIPE, _pipe_meta("CHW Supply back")))
add(revit_box(5, 55, 8.6 - _pw, 8.6, 12.5, 13.1, PIPE, _pipe_meta("CHW Supply front")))

# ---------------------------------------------------------------------------
# Write the .glb
# ---------------------------------------------------------------------------
OUT_DIR = os.path.join(
    REPO, 'src', 'extensions', 'dbHMS Extensions.extension',
    'dbHMS Tools.tab', 'BIM Tools.panel', 'View Range Helper.pushbutton',
    'web', 'sample')
if not os.path.isdir(OUT_DIR):
    os.makedirs(OUT_DIR)
glb_path = os.path.join(OUT_DIR, 'sample_building.glb')

extras = {
    "generator": "dbHMS View Range sample fixture",
    "units": "meters", "axis": "y_up_from_revit_z_up",
    "offset_ft": list(OFF), "ft_to_m": FT_TO_M,
}
writer = GlbWriter(glb_path, asset_extras=extras)
tris = 0
for m in meshes:
    writer.add(m)
    tris += m.triangle_count
size = writer.finalize()
print("wrote {0} ({1} elements, {2} tris, {3} bytes)".format(
    glb_path, len(meshes), tris, size))

# ---------------------------------------------------------------------------
# Metadata sidecar: exactly what the Python tool will post to the page as the
# "meta:" message (levels, current view range, view info, template state).
# Modelled on an L2 floor plan whose view range references L2 (id 102).
# ---------------------------------------------------------------------------
def plane(level_id, level_name, offset_ft, sentinel=None):
    lv = next((l for l in LEVELS if l["id"] == level_id), None)
    base = lv["elev_ft"] if lv else 0.0
    abs_ft = None if sentinel == "unlimited" else base + offset_ft
    return {"level_id": level_id, "level_name": level_name,
            "offset_ft": offset_ft, "abs_ft": abs_ft,
            "sentinel": sentinel}

LEVELS_META = [{"id": l["id"], "name": l["name"], "elev_ft": l["elev_ft"]} for l in LEVELS]
SENTINELS = {
    "top": {"above": True,  "below": False, "unlimited": False},
    "cut": {"above": False, "below": False, "unlimited": False},
    "bot": {"above": False, "below": True,  "unlimited": False},
    "vd":  {"above": False, "below": True,  "unlimited": True},
}


def base_meta():
    """The default L2 floor-plan meta (Section 4.1 of the build spec)."""
    return {
        "schema": "dbhms.viewrange.meta/1",
        "units": "meters", "ft_to_m": FT_TO_M, "offset_ft": list(OFF),
        "model_z_extent_ft": [-15.0, 66.0],
        "view": {
            "name": "L2 - Office  Floor Plan",
            "view_type": "FloorPlan",
            "is_ceiling_plan": False,
            "associated_level_id": 102,
            "associated_level_name": "L2 - Office",
            "crop_ft": {"min_x": 0.0, "max_x": 60.0, "min_y": 0.0, "max_y": 40.0},
            "up_dir": [0.0, 1.0],   # axis-aligned sample; real views may be rotated
        },
        "levels": LEVELS_META,
        "view_range": {
            "top": plane(102, "L2 - Office", 7.5),
            "cut": plane(102, "L2 - Office", 4.0),
            "bot": plane(102, "L2 - Office", 0.0),
            "vd":  plane(102, "L2 - Office", 0.0),
        },
        "sentinels": SENTINELS,
        "sentinel_ids": {"above": -3, "below": -2, "unlimited": -1},
        "disabled_planes": [],
        # Architectural shell categories render with the clay/poche look; all
        # others (the MEP above) render in their flat Revit colours so they pop.
        "shell_categories": ["Walls", "Floors", "Roofs", "Columns"],
        # Categories/worksets hidden in the source plan start toggled OFF in the
        # tool but remain in the .glb so they can be switched on. Here the pipe is
        # hidden by BOTH its category (Pipes) and its workset (Plumbing).
        "hidden_categories": ["Pipes"],
        "hidden_worksets": ["Plumbing"],
        # Host model (the one the tool is run from); links are listed separately.
        "host_model": MODEL_MEP,
        "template_lock": {"locked": False, "template_name": None},
        "snap": {"enabled": True, "distance_ft": 0.5},
    }


def ceiling_meta():
    """A reflected ceiling plan: Bottom is disabled (locked to Cut by Revit), the
    cut looks up, and View Depth normally sits above the Cut plane. Drag View
    Depth below the Cut to exercise the RCP-specific validation warning."""
    m = base_meta()
    m["view"]["name"] = "L2 - Reflected Ceiling Plan"
    m["view"]["view_type"] = "CeilingPlan"
    m["view"]["is_ceiling_plan"] = True
    m["view_range"] = {
        "top": plane(102, "L2 - Office", 10.0),   # abs 22.0
        "cut": plane(102, "L2 - Office", 7.5),    # abs 19.5
        "bot": plane(102, "L2 - Office", 7.5),    # abs 19.5 (locked to cut, disabled)
        "vd":  plane(102, "L2 - Office", 9.0),    # abs 21.0 (above cut)
    }
    m["disabled_planes"] = ["bot"]
    return m


def locked_meta():
    """A floor plan whose view range is controlled by a view template, so the
    page shows the warn banner with Detach / Edit-template actions."""
    m = base_meta()
    m["template_lock"] = {"locked": True, "template_name": "MEP - Floor Plan"}
    return m


def write_meta(meta, name):
    path = os.path.join(OUT_DIR, name)
    with open(path, 'w') as f:
        json.dump(meta, f, indent=2)
    print("wrote {0}".format(path))


write_meta(base_meta(),    'sample_meta.json')
write_meta(ceiling_meta(), 'sample_meta_ceiling.json')
write_meta(locked_meta(),  'sample_meta_locked.json')
