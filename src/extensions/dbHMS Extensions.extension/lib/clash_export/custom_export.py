# -*- coding: utf-8 -*-
"""High-fidelity, view-faithful Revit -> glTF export via CustomExporter.

Replaces the basic `Element.get_Geometry()` + `Face.Triangulate()` path (in
revit_geometry.py) with Revit's own export engine, driven through an
`IExportContext` we implement here. This is what fixes, at the source, the three
problems the renderer cannot:

  * faceted / octagonal pipes  -> `ViewNode.LevelOfDetail` (smooth tessellation)
    plus per-vertex normals from `PolymeshTopology.GetNormals()`.
  * phantom walls/ceilings     -> CustomExporter only emits the geometry the
    given 3D view actually shows (we export the user's active 3D view).
  * z-fighting from coincident  -> Revit's export doesn't emit the duplicated
    internal faces the geometry API does.

IronPython note: `IExportContext` is a .NET *interface*, which IronPython 2.7
implements cleanly (same mechanism pyRevit uses for IExternalEventHandler).
Implementing an interface is supported; subclassing concrete .NET *classes* is
the thing IronPython can't do, and we don't. The base is resolved at import:
under CPython (the test runtime) the Revit assemblies are absent, so we fall
back to `object` -- the module still imports/parses; it's only ever instantiated
inside Revit.

Everything is defensive: any failure raises so the caller can fall back to the
old exporter, but per-element/per-mesh errors are swallowed so one bad element
never aborts a whole-building export.
"""

import math
import time

from clash_export.mesh import Mesh
from clash_export.gltf import GlbWriter
from clash_detect._compat import eid_int

FT_TO_M = 0.3048

# Resolve the interface base once. Revit present -> the real interface (IronPython
# implements it). Revit absent (CPython tests) -> object, so the module imports.
try:
    from Autodesk.Revit.DB import IExportContext as _CONTEXT_BASE
    _HAVE_REVIT = True
except Exception:
    _CONTEXT_BASE = object
    _HAVE_REVIT = False


def _is_non_model(el):
    """True for elements we never want as 3D geometry -- anything whose category
    is not a Model category (levels, grids, reference planes, scope boxes,
    cameras, and all annotation: text, dimensions, tags). Keeps the everything-on
    export to physical building geometry only. Null-category elements are kept
    (CustomExporter wouldn't be emitting geometry for them otherwise)."""
    try:
        from Autodesk.Revit.DB import CategoryType
        cat = el.Category
        if cat is None:
            return False
        return cat.CategoryType != CategoryType.Model
    except Exception:
        return False


def _log(msg):
    """Best-effort log to the viewer log (shared with the tool); never raises."""
    try:
        import os
        from datetime import datetime
        root = os.path.join(
            os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or '.',
            'dbHMS', '3DViewer')
        if not os.path.isdir(root):
            os.makedirs(root)
        with open(os.path.join(root, 'viewer.log'), 'a') as f:
            f.write("{0} CE: {1}\n".format(datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


class GltfExportContext(_CONTEXT_BASE):
    """IExportContext that streams Revit's exported geometry into a GlbWriter.

    Builds one Mesh per element (positions + indices + per-vertex normals), in
    glTF meters/Y-up centered on `offset`, carrying the element's metadata. A
    transform stack tracks instance + link placement so linked-model geometry
    lands in host coordinates.
    """

    def __init__(self, doc, writer, offset, lod):
        self.doc = doc
        self.writer = writer
        self.ox, self.oy, self.oz = offset
        self.lod = lod                      # Revit ViewNode.LevelOfDetail, int 0..15
        self._xf = []                       # Transform stack (instances + links)
        self._docs = []                     # document stack (host -> link docs)
        self._meta = None
        self._color = (0.70, 0.72, 0.74)
        self._alpha = 1.0
        self._roughness = 0.85              # glTF PBR roughnessFactor (1 = matte)
        self._metallic = 0.0
        self._texture = None                # base-color texture data: URI (or None)
        self._tex_cache = {}                # material id -> texture data: URI, resolved once
        self._pos = []
        self._nrm = []
        self._uv = []
        self._idx = []
        # stats / diagnostics
        self.elements = 0
        self.triangles = 0
        self.polymeshes = 0
        self.normals_dist = {}

    # ---- transform + document stacks -------------------------------------
    def _topxf(self):
        from Autodesk.Revit.DB import Transform
        return self._xf[-1] if self._xf else Transform.Identity

    def _curdoc(self):
        # the document we're currently traversing (host, or a link's doc) so
        # element metadata (model/workset/level) resolves against the right file
        return self._docs[-1] if self._docs else self.doc

    # ---- lifecycle -------------------------------------------------------
    def Start(self):
        return True

    def Finish(self):
        pass

    def IsCanceled(self):
        return False

    def OnViewBegin(self, node):
        from Autodesk.Revit.DB import RenderNodeAction
        # The faceting fix: ask Revit for fine tessellation of curved faces.
        # LevelOfDetail is an INTEGER 0..15 (8 = normal); passing a 0..1 float
        # silently failed, leaving pipes coarse. Higher = rounder.
        try:
            node.LevelOfDetail = int(self.lod)
        except Exception:
            pass
        return RenderNodeAction.Proceed

    def OnViewEnd(self, element_id):
        pass

    # ---- element grouping ------------------------------------------------
    def OnElementBegin(self, element_id):
        from Autodesk.Revit.DB import RenderNodeAction
        self._pos = []
        self._nrm = []
        self._uv = []
        self._idx = []
        self._color = (0.70, 0.72, 0.74)
        self._alpha = 1.0
        self._roughness = 0.85
        self._metallic = 0.0
        self._texture = None
        # Always carry at least the element id so the viewer can select it, even
        # if the richer metadata lookup below fails.
        self._meta = {"element_id": eid_int(element_id)}
        try:
            import clash_export.revit_geometry as rg
            el = self._curdoc().GetElement(element_id)   # right doc for links
            if el is not None:
                # Physical models only: skip annotation + datum categories (levels,
                # grids, reference planes, scope boxes, text, dims, tags) that can
                # show in a 3D view -- Nathan wants just the real building geometry.
                if _is_non_model(el):
                    return RenderNodeAction.Skip
                md = rg._metadata_for_element(el)         # uses el.Document internally
                if md:
                    self._meta = md
                rgb, alpha, rough, metal, tex = rg._material_for_element(el, self._tex_cache)
                self._color = rgb
                self._alpha = alpha
                self._roughness = rough
                self._metallic = metal
                self._texture = tex
        except Exception:
            pass
        return RenderNodeAction.Proceed

    def OnElementEnd(self, element_id):
        try:
            if self._pos and self._idx:
                normals = self._nrm if len(self._nrm) == len(self._pos) else None
                nvert = len(self._pos) // 3
                # only ship UVs when there's a texture to map (and the count lines up)
                uvs = self._uv if (self._texture and len(self._uv) == nvert * 2) else None
                self.writer.add(Mesh(
                    positions=self._pos, indices=self._idx, normals=normals,
                    color=self._color, alpha=self._alpha,
                    roughness=self._roughness, metallic=self._metallic,
                    uvs=uvs, texture=(self._texture if uvs else None),
                    metadata=self._meta))
                self.elements += 1
                # progress heartbeat so the log shows alive-vs-hung on big models
                if self.elements % 2000 == 0:
                    _log("export progress: {0} elements, {1:,} tris".format(
                        self.elements, self.triangles))
        except Exception:
            pass
        self._pos = []
        self._nrm = []
        self._uv = []
        self._idx = []

    # ---- instances + links: push/pop the placement transform -------------
    def OnInstanceBegin(self, node):
        from Autodesk.Revit.DB import RenderNodeAction
        try:
            self._xf.append(self._topxf().Multiply(node.GetTransform()))
        except Exception:
            self._xf.append(self._topxf())
        return RenderNodeAction.Proceed

    def OnInstanceEnd(self, node):
        if self._xf:
            self._xf.pop()

    def OnLinkBegin(self, node):
        from Autodesk.Revit.DB import RenderNodeAction
        try:
            self._xf.append(self._topxf().Multiply(node.GetTransform()))
        except Exception:
            self._xf.append(self._topxf())
        # enter the link's document so its elements' metadata resolves correctly
        try:
            self._docs.append(node.GetDocument())
        except Exception:
            self._docs.append(self._curdoc())
        return RenderNodeAction.Proceed

    def OnLinkEnd(self, node):
        if self._xf:
            self._xf.pop()
        if self._docs:
            self._docs.pop()

    # ---- skip the per-face callbacks (big speed win) ---------------------
    def OnFaceBegin(self, node):
        from Autodesk.Revit.DB import RenderNodeAction
        return RenderNodeAction.Skip

    def OnFaceEnd(self, node):
        pass

    # ---- material: capture colour + transparency -------------------------
    def OnMaterial(self, node):
        # Skip the colour override when this element has a base-colour texture: the texture
        # IS the colour and _material_for_element set the factor to white; the live node's
        # shading colour is the tint, which would re-darken the texture (see CMU fix).
        if not self._texture:
            try:
                c = node.Color
                if c is not None:
                    self._color = (c.Red / 255.0, c.Green / 255.0, c.Blue / 255.0)
            except Exception:
                pass
        try:
            t = node.Transparency       # 0..1
            if t and t > 0:
                self._alpha = max(0.05, 1.0 - float(t))
        except Exception:
            pass
        # Live finish: the render node's glossiness is more authoritative than the
        # Material element's Smoothness for how the surface actually shades. Some
        # Revit builds report 0..1, others 0..100 -- normalise either way.
        try:
            g = node.Glossiness
            if g is not None:
                g = float(g)
                if g > 1.0:
                    g = g / 100.0
                self._roughness = min(1.0, max(0.04, 1.0 - g))
        except Exception:
            pass

    # ---- the hot path: tessellated geometry ------------------------------
    def OnPolymesh(self, node):
        self.polymeshes += 1
        try:
            pts = node.GetPoints()
            facets = node.GetFacets()
        except Exception:
            return
        xf = self._topxf()
        ox, oy, oz = self.ox, self.oy, self.oz
        base = len(self._pos) // 3
        # vertices: link/instance transform, recenter (ft), ft->m, Z-up -> Y-up
        for p in pts:
            q = xf.OfPoint(p)
            self._pos.append((q.X - ox) * FT_TO_M)
            self._pos.append((q.Z - oz) * FT_TO_M)
            self._pos.append(-(q.Y - oy) * FT_TO_M)
        # texture coordinates per vertex (Revit supplies them when the surface is
        # textured); pad (0,0) when absent so _uv stays parallel to the vertex count
        # across an element's multiple polymeshes. glTF V is top-down vs Revit's V.
        try:
            uvs = node.GetUVs()
        except Exception:
            uvs = None
        if uvs is not None and len(uvs) == len(pts):
            for uv in uvs:
                self._uv.append(uv.U); self._uv.append(1.0 - uv.V)
        else:
            for _ in pts:
                self._uv.append(0.0); self._uv.append(0.0)
        # per-vertex normals when Revit gives them at each point (smooth shading)
        try:
            from Autodesk.Revit.DB import DistributionOfNormals
            dist = node.DistributionOfNormals
            self.normals_dist[str(dist)] = self.normals_dist.get(str(dist), 0) + 1
            if dist == DistributionOfNormals.AtEachPoint:
                nrms = node.GetNormals()
                if nrms is not None and len(nrms) == len(pts):
                    for nv in nrms:
                        m = xf.OfVector(nv)
                        nx, ny, nz = m.X, m.Z, -m.Y
                        l = math.sqrt(nx * nx + ny * ny + nz * nz)
                        if l > 1e-9:
                            self._nrm.append(nx / l); self._nrm.append(ny / l); self._nrm.append(nz / l)
                        else:
                            self._nrm.append(0.0); self._nrm.append(1.0); self._nrm.append(0.0)
                else:
                    self._mark_normals_unavailable()
            else:
                self._mark_normals_unavailable()
        except Exception:
            self._mark_normals_unavailable()
        # facets -> triangle indices (already indexed by Revit)
        for f in facets:
            self._idx.append(base + f.V1)
            self._idx.append(base + f.V2)
            self._idx.append(base + f.V3)
        self.triangles += len(facets)

    def _mark_normals_unavailable(self):
        # If any polymesh in this element lacks per-vertex normals, drop normals
        # for the whole element so positions/normals stay 1:1 (viewer then falls
        # back to flat shading for it).
        self._nrm = []
        self._nrm_broken = True

    # ---- required no-ops -------------------------------------------------
    def OnRPC(self, node):
        pass

    def OnLight(self, node):
        pass


def _view_offset(doc, view):
    """Bounding-box centre (host feet) of what the view shows, to keep exported
    coordinates near the origin (large absolute coords hurt float precision)."""
    from Autodesk.Revit.DB import FilteredElementCollector
    mnx = mny = mnz = 1e30
    mxx = mxy = mxz = -1e30
    found = False
    try:
        col = (FilteredElementCollector(doc, view.Id)
               .WhereElementIsNotElementType())
        for el in col:
            try:
                bb = el.get_BoundingBox(view)
                if bb is None:
                    continue
                mn, mx = bb.Min, bb.Max
                if mn.X < mnx: mnx = mn.X
                if mn.Y < mny: mny = mn.Y
                if mn.Z < mnz: mnz = mn.Z
                if mx.X > mxx: mxx = mx.X
                if mx.Y > mxy: mxy = mx.Y
                if mx.Z > mxz: mxz = mx.Z
                found = True
            except Exception:
                continue
    except Exception:
        pass
    if not found:
        return (0.0, 0.0, 0.0)
    return ((mnx + mxx) / 2.0, (mny + mxy) / 2.0, (mnz + mxz) / 2.0)


def _make_export_view(doc):
    """Create a fresh, throwaway isometric 3D view with EVERYTHING on (all model
    categories, all worksets, links) at Fine detail, for a complete export that
    doesn't depend on the user's active view (works even from a sheet/2D view).
    Visibility is then controlled entirely inside the web viewer. Returns the
    view, or None if creation failed."""
    from Autodesk.Revit.DB import (View3D, ViewFamilyType, ViewFamily,
                                   FilteredElementCollector, ViewDetailLevel,
                                   Transaction, WorksetKind, WorksetVisibility,
                                   FilteredWorksetCollector)
    vft = None
    for v in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if v.ViewFamily == ViewFamily.ThreeDimensional:
                vft = v
                break
        except Exception:
            continue
    if vft is None:
        return None
    t = Transaction(doc, "dbHMS: export view")
    t.Start()
    try:
        v3 = View3D.CreateIsometric(doc, vft.Id)
        try:
            v3.DetailLevel = ViewDetailLevel.Fine
        except Exception:
            pass
        # Force every user workset visible (a new view inherits defaults that may
        # hide some), so the export is genuinely complete.
        try:
            if doc.IsWorkshared:
                for ws in FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset):
                    try:
                        v3.SetWorksetVisibility(ws.Id, WorksetVisibility.Visible)
                    except Exception:
                        pass
        except Exception:
            pass
        t.Commit()
        _log("export: created dedicated all-on Fine 3D view")
        return v3
    except Exception:
        try:
            t.RollBack()
        except Exception:
            pass
        _log("export: dedicated view creation failed ({0})".format(_exc_last()))
        return None


def export_view(doc, out_path, lod=8, asset_extras=None):
    """Export the WHOLE model (everything on) to a .glb via CustomExporter, using
    a dedicated throwaway 3D view so it works from any context and captures all
    geometry (visibility is controlled in the viewer). Returns a stats dict;
    raises on hard failure so the caller can fall back to the old path."""
    if not _HAVE_REVIT:
        raise RuntimeError("CustomExporter unavailable (no Revit API)")
    from Autodesk.Revit.DB import CustomExporter

    temp_view = _make_export_view(doc)
    if temp_view is None:
        raise RuntimeError("could not create an export 3D view")
    src = temp_view

    offset = _view_offset(doc, src)
    extras = dict(asset_extras or {})
    extras.update({
        "generator": "dbHMS 3D Viewer (CustomExporter)",
        "units": "meters", "axis": "y_up_from_revit_z_up",
        "offset_ft": [offset[0], offset[1], offset[2]], "ft_to_m": FT_TO_M,
    })

    writer = GlbWriter(out_path, asset_extras=extras)
    ctx = GltfExportContext(doc, writer, offset, lod)
    try:
        import clash_export.revit_geometry as _rg
        _rg.TEXTURE_DEBUG[:] = []        # fresh per-material texture diagnostics this export
    except Exception:
        _rg = None
    t0 = time.time()
    try:
        exporter = CustomExporter(doc, ctx)
        try:
            exporter.IncludeGeometricObjects = False   # we only want polymeshes
        except Exception:
            pass
        try:
            exporter.ShouldStopOnError = False
        except Exception:
            pass
        _log("export start: view={0} lod={1}".format(getattr(src, "Name", "?"), lod))
        exporter.Export(src)
        size = writer.finalize()
    except Exception:
        writer.close()
        _log("export FAILED: {0}".format(_exc_last()))
        _delete_view(doc, temp_view)
        raise
    secs = time.time() - t0
    _delete_view(doc, temp_view)
    # textures: how many distinct materials resolved a base-color image (vs flat)
    mats_seen = len(ctx._tex_cache)
    mats_textured = len([v for v in ctx._tex_cache.values() if v])
    stats = {
        "elements": ctx.elements, "triangles": ctx.triangles,
        "polymeshes": ctx.polymeshes, "bytes": size, "seconds": secs,
        "normals_dist": ctx.normals_dist,
        "materials": mats_seen, "textured_materials": mats_textured,
    }
    _log("export done: {0} elements, {1:,} tris, {2:,} bytes, {3:.0f}s, normals={4}, "
         "textures={5}/{6} materials".format(
             ctx.elements, ctx.triangles, size, secs, ctx.normals_dist,
             mats_textured, mats_seen))
    # per-material texture diagnostics (one TEX line each) so a misbehaving material can
    # be diagnosed from the log without guessing at Revit's appearance-asset slot names
    try:
        if _rg is not None and _rg.TEXTURE_DEBUG:
            _log("--- texture diagnostics ({0} materials) ---".format(len(_rg.TEXTURE_DEBUG)))
            for line in _rg.TEXTURE_DEBUG:
                _log(line)
    except Exception:
        pass
    return stats


def _delete_view(doc, view):
    """Best-effort delete of the temporary export view."""
    if view is None:
        return
    try:
        from Autodesk.Revit.DB import Transaction
        t = Transaction(doc, "dbHMS: cleanup export view")
        t.Start()
        doc.Delete(view.Id)
        t.Commit()
    except Exception:
        _log("cleanup view failed: {0}".format(_exc_last()))


def _exc_last():
    import traceback
    try:
        return traceback.format_exc().splitlines()[-1]
    except Exception:
        return "?"
