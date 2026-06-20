# -*- coding: utf-8 -*-
"""Extract Revit geometry into plain Mesh objects for the 3D Viewer.

Revit API only, lazy-imported inside functions, so this module parses under
CPython 3 for the test suite; the live extraction needs Revit. The output
(Mesh objects + an asset-extras dict) is plain data consumed by
clash_export.gltf.

Scope (Phase 2 + linked models): a SLICE of model elements from the host
document AND every loaded linked model (the arch/structural links), so a
coordination review sees the building it's coordinating against. Geometry is
tessellated to triangle soup, transformed into HOST coordinates (link
elements get their link instance's transform applied), centered on the
slice's bounding-box center, and converted from Revit feet (Z-up) to glTF
meters (Y-up). The whole-model scale-up and indexed/deduped meshes come in
later phases.

Coordinate convention recorded in asset_extras so later phases can map a
glTF point back to Revit world feet:
    centered_ft = (Hx - ox, Hy - oy, Hz - oz)     # H = host-space feet
    meters      = centered_ft * FT_TO_M
    gltf        = (meters.x, meters.z, -meters.y)  # Z-up -> Y-up
Inverse:
    meters      = (gx, -gz, gy)
    Hft         = meters / FT_TO_M + offset_ft
"""

from clash_export.mesh import Mesh
from clash_detect._compat import eid_int

FT_TO_M = 0.3048

# discipline -> base color (rgb 0..1), aligned with the dbHMS palette.
_DISCIPLINE_COLOR = {
    "Mechanical":      (0.17, 0.42, 0.69),
    "Plumbing":        (0.20, 0.56, 0.74),
    "Electrical":      (0.90, 0.62, 0.00),
    "Fire Protection": (0.84, 0.23, 0.23),
    "Technology":      (0.50, 0.35, 0.84),
    "Architectural":   (0.62, 0.65, 0.69),
    "Structural":      (0.42, 0.37, 0.30),
}
_DEFAULT_COLOR = (0.70, 0.72, 0.74)

# Tessellation level of detail per source, 0 (coarsest) .. 1 (finest). The
# host MEP stays full detail (you need real pipe/duct geometry for clash
# review); linked arch/structural models are spatial context, so they get
# medium detail - far fewer triangles, much faster, smaller file.
_HOST_TRI_LEVEL = 1.0
_LINK_TRI_LEVEL = 0.5

# Set once on first tessellation: True if MeshTriangle exposes vertex indices
# (the fast, compact indexed path), False to fall back to non-indexed.
_TRIANGLE_INDEX_SUPPORTED = None


def _make_options(detail):
    from Autodesk.Revit.DB import Options
    o = Options()
    try:
        o.ComputeReferences = False
        o.IncludeNonVisibleObjects = False
        o.DetailLevel = detail
    except Exception:
        pass
    return o


def export_model(doc, out_path, view=None, max_elements=120000,
                 max_triangles=8000000, include_links=True):
    """Stream the host model + every loaded link to a .glb at `out_path`.

    Memory-safe: geometry is written to disk one element at a time via
    GlbWriter, so a large building won't exhaust RAM. Bounded by
    max_elements / max_triangles safety ceilings (reported as 'capped' when
    hit) so a runaway model can't produce an unbounded file or hang forever.

    Returns {"path", "asset_extras", "stats"} where stats includes
    host_elements, link_elements, triangles, models, bytes, capped.
    """
    from Autodesk.Revit.DB import ViewDetailLevel
    from clash_export.gltf import GlbWriter

    # Gather elements per source (host + each link), each capped, then
    # interleave so a dense host doesn't starve the links of the triangle
    # budget. `ordered` is a list of (element, link_instance_or_None).
    source_lists = _gather_sources(doc, view, max_elements, include_links)
    ordered = _round_robin(source_lists)
    offset = _slice_offset(ordered)

    # MEP host at fine detail, linked context at medium.
    opts_host = _make_options(ViewDetailLevel.Fine)
    opts_link = _make_options(ViewDetailLevel.Medium)

    asset_extras = {
        "generator": "dbHMS 3D Viewer",
        "units": "meters",
        "axis": "y_up_from_revit_z_up",
        "offset_ft": [offset[0], offset[1], offset[2]],
        "ft_to_m": FT_TO_M,
        "doc_title": _safe(lambda: doc.Title, ""),
    }

    writer = GlbWriter(out_path, asset_extras=asset_extras)
    total_tris = 0
    host_used = 0
    link_used = 0
    capped = False
    try:
        for el, link in ordered:
            if total_tris >= max_triangles:
                capped = True
                break
            if link is None:
                opts, tri_level, transform = opts_host, _HOST_TRI_LEVEL, None
            else:
                opts, tri_level = opts_link, _LINK_TRI_LEVEL
                transform = _safe(lambda: link.GetTotalTransform(), None)
            mesh = _element_to_mesh(el, opts, offset, transform, tri_level)
            if mesh is None:
                continue
            writer.add(mesh)
            total_tris += mesh.triangle_count
            if link is None:
                host_used += 1
            else:
                link_used += 1
        size = writer.finalize()
    except Exception:
        writer.close()
        raise

    if any(len(els) >= max_elements for _, els in source_lists):
        capped = True
    stats = {
        "elements": host_used + link_used,
        "host_elements": host_used,
        "link_elements": link_used,
        "triangles": total_tris,
        "models": len([s for s in source_lists if s[1]]),
        "bytes": size,
        "capped": capped,
    }
    return {"path": out_path, "asset_extras": asset_extras, "stats": stats}


# ---------------------------------------------------------------------------
# Source gathering (host + links)
# ---------------------------------------------------------------------------

def _gather_sources(doc, view, max_elements, include_links):
    """Return [(link_or_None, [elements]), ...] - the host first, then each
    loaded link. Each list is capped at max_elements."""
    sources = [(None, _select_elements(doc, view, max_elements))]
    if include_links:
        for link in _loaded_links(doc):
            link_doc = _safe(lambda: link.GetLinkDocument(), None)
            if link_doc is None:
                continue
            link_els = _select_elements(link_doc, None, max_elements)
            if link_els:
                sources.append((link, link_els))
    return sources


def _loaded_links(doc):
    try:
        from clash_detect import linked
        return list(linked.find_link_instances(doc))
    except Exception:
        return []


def _round_robin(source_lists):
    """Flatten [(link, [els]), ...] into [(el, link), ...] taking one element
    from each source per round, so no single source dominates the budget.

    Pure logic (no Revit) - unit-tested."""
    out = []
    pointers = [0] * len(source_lists)
    remaining = sum(len(els) for _, els in source_lists)
    while remaining > 0:
        progressed = False
        for si, (link, els) in enumerate(source_lists):
            p = pointers[si]
            if p < len(els):
                out.append((els[p], link))
                pointers[si] = p + 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return out


def _select_elements(source_doc, view, max_elements):
    """Pick up to `max_elements` model elements from `source_doc`. Prefers
    what's visible in the active 3D view when one is given (host only);
    falls back to the whole document (used for link docs)."""
    from Autodesk.Revit.DB import FilteredElementCollector, View3D

    try:
        if isinstance(view, View3D) and not view.IsTemplate:
            collector = FilteredElementCollector(source_doc, view.Id)
        else:
            collector = FilteredElementCollector(source_doc)
    except Exception:
        collector = FilteredElementCollector(source_doc)

    try:
        collector = (collector
                     .WhereElementIsNotElementType()
                     .WhereElementIsViewIndependent())
    except Exception:
        try:
            collector = collector.WhereElementIsNotElementType()
        except Exception:
            pass

    out = []
    for el in collector:
        if _is_model_element(el):
            out.append(el)
            if len(out) >= max_elements:
                break
    return out


def _is_model_element(el):
    from Autodesk.Revit.DB import CategoryType
    try:
        cat = el.Category
        if cat is None:
            return False
        return cat.CategoryType == CategoryType.Model
    except Exception:
        return False


def _slice_offset(ordered):
    """Bounding-box center (host-space feet) of the selected elements, used
    to keep exported coordinates near the origin (large absolute coords hurt
    float precision in WebGL). Link boxes are transformed to host coords."""
    from clash_view.geometry import element_world_box, union_boxes
    boxes = []
    for el, link in ordered:
        try:
            boxes.append(element_world_box(el, link))
        except Exception:
            continue
    ub = union_boxes(boxes)
    if ub is None:
        return (0.0, 0.0, 0.0)
    return ((ub.Min.X + ub.Max.X) / 2.0,
            (ub.Min.Y + ub.Max.Y) / 2.0,
            (ub.Min.Z + ub.Max.Z) / 2.0)


# ---------------------------------------------------------------------------
# Geometry -> Mesh
# ---------------------------------------------------------------------------

def _element_to_mesh(el, opts, offset, transform, tri_level):
    solids = _solids_for_element(el, opts)
    if not solids:
        return None
    positions = []
    indices = []
    for solid in solids:
        _triangulate_solid_into(solid, offset, transform, tri_level,
                                positions, indices)
    if not positions or not indices:
        return None
    return Mesh(positions=positions, indices=indices,
                color=_color_for_element(el),
                metadata=_metadata_for_element(el))


def _solids_for_element(el, opts):
    from Autodesk.Revit.DB import GeometryInstance, Solid
    out = []
    try:
        geom = el.get_Geometry(opts)
    except Exception:
        return out
    if geom is None:
        return out
    _collect_solids(geom, out)
    return out


def _collect_solids(geom, out):
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


def _triangulate_solid_into(solid, offset, transform, tri_level,
                            positions, indices):
    """Triangulate every face of `solid` into INDEXED geometry appended to
    `positions` + `indices`.

    Each face's unique vertices are transformed once (link -> host if a
    transform is given, feet -> glTF meters, Z-up -> Y-up) and triangles
    reference them by index. Indexing both shrinks the file and cuts work
    (one transform per unique vertex instead of three per triangle).

    Probes once whether MeshTriangle exposes vertex indices; if not, falls
    back to a non-indexed expansion so the export still succeeds (just
    larger) on API variants that don't.
    """
    global _TRIANGLE_INDEX_SUPPORTED
    try:
        faces = solid.Faces
    except Exception:
        return
    ox, oy, oz = offset
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
        if ntri <= 0:
            continue

        if _TRIANGLE_INDEX_SUPPORTED is None:
            try:
                mesh.get_Triangle(0).get_Index(0)
                _TRIANGLE_INDEX_SUPPORTED = True
            except Exception:
                _TRIANGLE_INDEX_SUPPORTED = False

        if _TRIANGLE_INDEX_SUPPORTED:
            try:
                verts = mesh.Vertices
                nverts = verts.Count
            except Exception:
                continue
            base = len(positions) // 3
            for vi in range(nverts):
                v = verts[vi]
                if transform is not None:
                    v = transform.OfPoint(v)   # link-local -> host
                mx = (v.X - ox) * FT_TO_M
                my = (v.Y - oy) * FT_TO_M
                mz = (v.Z - oz) * FT_TO_M
                # Revit Z-up -> glTF Y-up
                positions.append(mx)
                positions.append(mz)
                positions.append(-my)
            for i in range(ntri):
                try:
                    tri = mesh.get_Triangle(i)
                    indices.append(base + int(tri.get_Index(0)))
                    indices.append(base + int(tri.get_Index(1)))
                    indices.append(base + int(tri.get_Index(2)))
                except Exception:
                    continue
        else:
            for i in range(ntri):
                try:
                    tri = mesh.get_Triangle(i)
                    verts3 = (tri.get_Vertex(0), tri.get_Vertex(1),
                              tri.get_Vertex(2))
                except Exception:
                    continue
                for v in verts3:
                    if transform is not None:
                        v = transform.OfPoint(v)
                    mx = (v.X - ox) * FT_TO_M
                    my = (v.Y - oy) * FT_TO_M
                    mz = (v.Z - oz) * FT_TO_M
                    indices.append(len(positions) // 3)
                    positions.append(mx)
                    positions.append(mz)
                    positions.append(-my)


# ---------------------------------------------------------------------------
# Color + metadata
# ---------------------------------------------------------------------------

def _color_for_element(el):
    from clash_core.categories import discipline_for_category_id
    try:
        cat = el.Category
        if cat is not None:
            disc = discipline_for_category_id(eid_int(cat.Id))
            if disc in _DISCIPLINE_COLOR:
                return _DISCIPLINE_COLOR[disc]
    except Exception:
        pass
    return _DEFAULT_COLOR


def _metadata_for_element(el):
    """Per-element tags carried into the glTF node 'extras'. Uses the
    element's OWN document for level/workset lookups so linked elements
    resolve against the link doc, not the host."""
    from clash_core.categories import discipline_for_category_id
    src_doc = _safe(lambda: el.Document, None)
    md = {"element_id": eid_int(el.Id)}
    md["category"] = _safe(lambda: el.Category.Name, None)
    try:
        cat = el.Category
        if cat is not None:
            md["discipline"] = discipline_for_category_id(eid_int(cat.Id))
    except Exception:
        pass
    md["level"] = _level_name(src_doc, el)
    md["workset"] = _workset_name(src_doc, el)
    md["name"] = _safe(lambda: el.Name, None)
    md["model"] = _safe(lambda: src_doc.Title, None)
    return dict((k, v) for k, v in md.items() if v is not None)


def _level_name(src_doc, el):
    if src_doc is None:
        return None
    try:
        lid = el.LevelId
    except Exception:
        return None
    try:
        if lid is None or eid_int(lid) <= 0:
            return None
        lvl = src_doc.GetElement(lid)
        return lvl.Name if lvl is not None else None
    except Exception:
        return None


def _workset_name(src_doc, el):
    if src_doc is None:
        return None
    try:
        if not src_doc.IsWorkshared:
            return None
        ws = src_doc.GetWorksetTable().GetWorkset(el.WorksetId)
        return ws.Name if ws is not None else None
    except Exception:
        return None


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default
