# -*- coding: utf-8 -*-
"""Element tessellation for the soft-clash narrow phase.

Turns a Revit element into a list of triangles ((p0, p1, p2) float tuples,
host coordinates, FEET) that clash_detect.meshdist can measure true
distances on. Linked elements are transformed into host space via the link
instance's total transform (placement + shared coordinates -- the same
choice as clash_identity's link namespace and the CustomExporter's output).

The solid collector mirrors the proven exporter path
(clash_export.revit_geometry._collect_solids): full recursive descent into
nested GeometryInstances and the Faces/Volume validity filter. Copied, not
imported: the export version is private by name and bakes in glTF unit
conversion this path must not have.

All Revit imports live inside functions so the module parses under CPython
for the test suite. Results are cached per (link-instance, element) --
including the None result -- because the same wall/pipe appears in many
candidate pairs.
"""

# Above this many triangles, retessellate coarser; still above the max, give
# up and let the caller fall back to a bbox estimate (never a silent skip).
RETRY_TRI_CAP = 10000
MAX_TRI_CAP = 25000
COARSE_LEVEL = 0.1


def element_triangles(elem, link_instance, cache, tri_level=0.4):
    """Triangles of `elem` in host feet, or None when the element has no
    usable solids (mesh-only imports, symbolic families) or stays over the
    triangle cap even at coarse detail. `cache` is a plain dict shared
    across a run; the None result is cached too."""
    from clash_detect._compat import eid_int
    key = ('host' if link_instance is None else eid_int(link_instance.Id),
           eid_int(elem.Id))
    if key in cache:
        return cache[key]
    tris = _tessellate(elem, link_instance, tri_level)
    if tris is not None and len(tris) > RETRY_TRI_CAP:
        coarse = _tessellate(elem, link_instance, COARSE_LEVEL)
        if coarse is not None:
            tris = coarse
        if tris is not None and len(tris) > MAX_TRI_CAP:
            tris = None
    cache[key] = tris
    return tris


def stats(cache):
    """Run diagnostics: how many elements tessellated, total triangles, and
    how many came back with no usable solids (the bbox-fallback rows)."""
    n_elem = 0
    n_tris = 0
    n_none = 0
    for v in cache.values():
        n_elem += 1
        if v is None:
            n_none += 1
        else:
            n_tris += len(v)
    return {'elements': n_elem, 'triangles': n_tris, 'no_solids': n_none}


def _tessellate(elem, link_instance, tri_level):
    from Autodesk.Revit.DB import Options, ViewDetailLevel
    try:
        opts = Options()
        opts.DetailLevel = ViewDetailLevel.Fine
        opts.IncludeNonVisibleObjects = False
        geom = elem.get_Geometry(opts)
    except Exception:
        return None
    if geom is None:
        return None
    solids = []
    _collect_solids(geom, solids)
    if not solids:
        return None
    xf = None
    if link_instance is not None:
        try:
            xf = link_instance.GetTotalTransform()
        except Exception:
            try:
                xf = link_instance.GetTransform()
            except Exception:
                return None
    tris = []
    for solid in solids:
        _triangulate_solid_into(solid, xf, tri_level, tris)
    return tris or None


def _collect_solids(geom, out):
    """Every valid Solid in a geometry tree, descending recursively into
    nested GeometryInstances (a one-level descent silently drops geometry
    of nested families)."""
    from Autodesk.Revit.DB import GeometryInstance, Solid
    for g in geom:
        if isinstance(g, Solid):
            try:
                if g.Faces.Size > 0 and g.Volume > 0:
                    out.append(g)
            except Exception:
                continue
        elif isinstance(g, GeometryInstance):
            try:
                inst = g.GetInstanceGeometry()
            except Exception:
                inst = None
            if inst is not None:
                _collect_solids(inst, out)


def _triangulate_solid_into(solid, xf, tri_level, tris):
    """Append every face triangle of `solid` (transformed by `xf` when
    given) to `tris` as ((x,y,z), (x,y,z), (x,y,z)) float tuples in feet.
    Per-face failures are skipped; one bad face never drops the element."""
    try:
        faces = solid.Faces
    except Exception:
        return
    for face in faces:
        try:
            mesh = face.Triangulate(tri_level)
        except Exception:
            continue
        if mesh is None:
            continue
        try:
            ntri = mesh.NumTriangles
        except Exception:
            continue
        for i in range(ntri):
            try:
                t = mesh.get_Triangle(i)
                pts = []
                for j in range(3):
                    v = t.get_Vertex(j)
                    if xf is not None:
                        v = xf.OfPoint(v)
                    pts.append((v.X, v.Y, v.Z))
                tris.append((pts[0], pts[1], pts[2]))
            except Exception:
                continue
