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
from clash_detect._compat import eid_int, make_eid

FT_TO_M = 0.3048


class ExportCanceled(Exception):
    """Raised by export_region when the caller's is_canceled() hook asks to stop.
    Distinct from a hard failure: the model was never modified (the export runs in
    a throwaway view, no transaction on user data), so the caller can treat this
    as a clean user abort rather than an error."""
    pass


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

    def __init__(self, doc, writer, offset, lod, filter_color=None,
                 is_canceled=None, image_mode=False, keep_category_ids=None):
        self.doc = doc
        self.writer = writer
        # Optional caller hook polled by CustomExporter during Export(). It returns
        # True to abort. The UI passes a callable that also pumps its message queue
        # so an Abort button stays clickable while the (main-thread) export blocks.
        self._is_canceled = is_canceled
        self.was_canceled = False
        # image_mode: a leaner export tuned for clash preview IMAGES (flat-shaded
        # pair + ghosted context, never inspected up close). It drops per-vertex
        # normals + textures/UVs and skips pure-context clutter categories
        # (furniture, casework, ...) that are never clash participants, so a huge
        # federated model fits the WebView2 renderer. keep_category_ids protects
        # any category a clash test actually references from the prune.
        self.image_mode = image_mode
        self._skip_cats = self._build_skip_cats(keep_category_ids) if image_mode else set()
        self.ox, self.oy, self.oz = offset
        self.lod = lod                      # Revit ViewNode.LevelOfDetail, int 0..15
        # Optional resolver el -> (r,g,b) 0..1 from the source plan's view-filter
        # overrides, so MEP reads in its plan colour (e.g. green waste piping)
        # rather than its material colour. Host elements only; None = off.
        self._filter_color = filter_color
        self._filter_colored = False        # set per element when a filter colour applies
        self._xf = []                       # Transform stack (instances + links)
        self._docs = []                     # document stack (host -> link docs)
        self._ns = []                       # per-link namespace stack (federation-unique keys)
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
        self.with_uid = 0           # elements carrying a stable Revit UniqueId (Phase 0 identity check)
        self.triangles = 0
        self.polymeshes = 0
        self.normals_dist = {}
        self.cat_counts = {}        # category name -> element count (what exported)
        self.model_counts = {}      # model (doc) title -> element count
        self.skipped_cats = 0       # elements dropped by the image-mode prune

    @staticmethod
    def _build_skip_cats(keep_category_ids):
        """BuiltInCategory ints to SKIP in image_mode: pure visual-context
        clutter that is never a clash participant AND is triangle-heavy
        (furniture alone was ~55% of a real federated model's triangles;
        specialty equipment + casework another ~11%). Deliberately NOT pruned
        despite never clashing: curtain panels/mullions (the glass facade --
        cheap triangles, load-bearing for orientation), railings, and generic
        models. `keep_category_ids` (the categories the project's tests
        actually reference) is subtracted so a custom test can never have its
        category pruned. Revit-only; returns an empty set off-Revit so the
        module still parses under CPython for the test suite."""
        keep = set(keep_category_ids or [])
        block = set()
        try:
            from Autodesk.Revit.DB import BuiltInCategory as BIC
            names = (
                'OST_Furniture', 'OST_FurnitureSystems', 'OST_Casework',
                'OST_SpecialtyEquipment', 'OST_Planting', 'OST_Entourage',
            )
            for n in names:
                try:
                    block.add(int(getattr(BIC, n)))
                except Exception:
                    pass
        except Exception:
            return set()
        return block - keep

    # ---- transform + document stacks -------------------------------------
    def _topxf(self):
        from Autodesk.Revit.DB import Transform
        return self._xf[-1] if self._xf else Transform.Identity

    def _curdoc(self):
        # the document we're currently traversing (host, or a link's doc) so
        # element metadata (model/workset/level) resolves against the right file
        return self._docs[-1] if self._docs else self.doc

    def _curns(self):
        # namespace of the link we're currently inside (None = host document)
        return self._ns[-1] if self._ns else None

    def _make_link_ns(self, node):
        """A stable, per-link-INSTANCE namespace. A linked element's UniqueId is
        unique only within its own document, so across a federation a link
        element can collide with a host element (or another link's). Namespacing
        by the link's file path plus its placement origin makes the composite key
        unique across the whole federation, and keeps the same link file placed
        twice distinct."""
        name = "link"
        try:
            d = node.GetDocument()
            name = (d.PathName or d.Title or "link")
        except Exception:
            pass
        from clash_identity import link_ns_from_origin
        try:
            o = node.GetTransform().Origin
            return link_ns_from_origin(name, o.X, o.Y, o.Z)
        except Exception:
            return name

    # ---- lifecycle -------------------------------------------------------
    def Start(self):
        return True

    def Finish(self):
        pass

    def IsCanceled(self):
        # Polled repeatedly by CustomExporter. Once canceled, stay canceled.
        if self.was_canceled:
            return True
        try:
            if self._is_canceled is not None and self._is_canceled():
                self.was_canceled = True
                return True
        except Exception:
            pass
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
        self._filter_colored = False
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
                # image_mode: drop pure-context clutter categories (furniture,
                # casework, ...) that never participate in a clash, so the .glb
                # is small enough for the renderer. Never drops a category a
                # clash test references (protected in _build_skip_cats).
                if self._skip_cats:
                    try:
                        cat = el.Category
                        if cat is not None and eid_int(cat.Id) in self._skip_cats:
                            self.skipped_cats += 1
                            return RenderNodeAction.Skip
                    except Exception:
                        pass
                md = rg._metadata_for_element(el)         # uses el.Document internally
                if md:
                    self._meta = md
                    cat = md.get("category") or "?"
                    mdl = md.get("model") or "?"
                    self.cat_counts[cat] = self.cat_counts.get(cat, 0) + 1
                    self.model_counts[mdl] = self.model_counts.get(mdl, 0) + 1
                rgb, alpha, rough, metal, tex = rg._material_for_element(el, self._tex_cache)
                self._color = rgb
                self._alpha = alpha
                self._roughness = rough
                self._metallic = metal
                # image_mode ghosts/flat-shades everything, so textures (and the
                # UVs that ride with them) are dead weight -- drop them.
                self._texture = None if self.image_mode else tex
                # Plan view-filter colour override (host elements only): make MEP
                # read in its plan colour (e.g. green waste piping). Wins over the
                # material colour + the live OnMaterial colour for this element.
                if self._filter_color is not None and self._curdoc() is self.doc:
                    fc = None
                    try:
                        fc = self._filter_color(el)
                    except Exception:
                        fc = None
                    if fc is not None:
                        self._color = fc
                        self._texture = None      # never texture a filter-coloured element
                        self._filter_colored = True
        except Exception:
            pass
        # Compose the globally-unique federation key. Raw UniqueId is unique only
        # WITHIN a document, so a Phase 0 export of a host+link model showed 121
        # linked elements colliding with host ids one-for-one. Namespacing by
        # source (host = the bare id; a linked element = per-link-instance tag +
        # id) makes it unique across the whole federation -- the key the
        # clash <-> element <-> Revit join relies on.
        uid = self._meta.get("unique_id") if self._meta else None
        if uid:
            from clash_identity import fed_key as _fed_key
            ns = self._curns()
            self._meta["fed_key"] = _fed_key(uid, ns)
            if ns is not None:
                self._meta["link_ns"] = ns
        return RenderNodeAction.Proceed

    def OnElementEnd(self, element_id):
        try:
            if self._pos and self._idx:
                normals = self._nrm if len(self._nrm) == len(self._pos) else None
                nvert = len(self._pos) // 3
                # only ship UVs when there's a texture to map (and the count lines up)
                uvs = self._uv if (self._texture and len(self._uv) == nvert * 2) else None
                color = self._color
                if self.image_mode:
                    # Flat gray palette: red/blue are reserved for the clash
                    # pair, so context must carry no hue (red primer steel etc.
                    # reads as a clash). Luminance keeps the value contrast
                    # (dark frames vs light walls); alpha keeps glass glassy.
                    try:
                        lum = (0.299 * color[0] + 0.587 * color[1]
                               + 0.114 * color[2])
                    except Exception:
                        lum = 0.6
                    v = 0.38 + 0.50 * lum   # map into a light architectural range
                    color = (v, v, v)
                self.writer.add(Mesh(
                    positions=self._pos, indices=self._idx, normals=normals,
                    color=color, alpha=self._alpha,
                    roughness=self._roughness, metallic=self._metallic,
                    uvs=uvs, texture=(self._texture if uvs else None),
                    metadata=self._meta))
                self.elements += 1
                if self._meta and self._meta.get("unique_id"):
                    self.with_uid += 1
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
        self._ns.append(self._make_link_ns(node))
        return RenderNodeAction.Proceed

    def OnLinkEnd(self, node):
        if self._xf:
            self._xf.pop()
        if self._docs:
            self._docs.pop()
        if self._ns:
            self._ns.pop()

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
        # Also skip when a plan view-filter colour was applied -- that override wins.
        if not self._texture and not self._filter_colored:
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
        # image_mode: skip UVs and normals (but still build the triangle indices
        # below). UVs are useless without textures; normals are ~half the vertex
        # bytes, and xeokit computes flat face normals on the GPU (autoNormals)
        # when NORMAL is absent -- exactly right for a flat-shaded clash
        # thumbnail. The single biggest per-vertex saving after the category prune.
        if not self.image_mode:
            # texture coordinates per vertex (Revit supplies them when the surface
            # is textured); pad (0,0) when absent so _uv stays parallel to the
            # vertex count across an element's multiple polymeshes. glTF V is
            # top-down vs Revit's V.
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


def _make_export_view(doc, hide_subcats=None, image_mode=False):
    """Create a fresh, throwaway isometric 3D view with EVERYTHING on (all model
    categories, all worksets, links) at Fine detail, for a complete export that
    doesn't depend on the user's active view (works even from a sheet/2D view).
    Visibility is then controlled entirely inside the web viewer. `hide_subcats`
    is an optional list of subcategory ElementId ints to turn OFF in the export
    view (host-model only), so e.g. equipment Clearances stay out of the .glb.
    `image_mode` is accepted for signature parity but detail stays Fine either
    way (MEP curves vanish below Fine -- see the comment at DetailLevel).
    Returns the view, or None if creation failed."""
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
            # ALWAYS Fine, image_mode included: pipes/ducts/conduit/cable tray
            # render as CENTERLINES (no surfaces, so nothing exports) at Coarse
            # and Medium -- an image_mode=Medium export shipped with every MEP
            # curve segment silently missing. Fine draws them as real solids;
            # the image export's triangle savings come from the low
            # LevelOfDetail (coarse curve tessellation), the category prune,
            # and dropping normals -- not from the view detail level.
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
        # Hide the subcategories the user opted to drop (host model only). CustomExporter
        # honours the view's V/G, so hidden subcategories simply aren't exported.
        if hide_subcats:
            hidden = 0
            for sid in hide_subcats:
                try:
                    cid = make_eid(sid)
                    if v3.CanCategoryBeHidden(cid):
                        v3.SetCategoryHidden(cid, True)
                        hidden += 1
                except Exception:
                    pass
            _log("export: hid {0} of {1} requested subcategories".format(hidden, len(hide_subcats)))
        t.Commit()
        _log("export: created dedicated all-on Fine 3D view{0}".format(
            " (image mode)" if image_mode else ""))
        return v3
    except Exception:
        try:
            t.RollBack()
        except Exception:
            pass
        _log("export: dedicated view creation failed ({0})".format(_exc_last()))
        return None


def export_view(doc, out_path, lod=8, asset_extras=None, hide_subcats=None,
                is_canceled=None, image_mode=False, keep_category_ids=None):
    """Export the WHOLE model (everything on) to a .glb via CustomExporter, using
    a dedicated throwaway 3D view so it works from any context and captures all
    geometry (visibility is controlled in the viewer). `hide_subcats` optionally
    turns off host subcategories (e.g. Clearances) for this export. `is_canceled`
    is an optional caller hook the CustomExporter polls; return True to abort,
    which raises ExportCanceled (a clean stop -- no user data is ever touched,
    the export runs in a throwaway view).

    `image_mode` produces a much leaner .glb tuned for clash preview IMAGES:
    a lower LevelOfDetail (coarser curve tessellation; detail level stays Fine
    because MEP curves export nothing below Fine), no per-vertex normals, no
    textures/UVs, gray-scale context colors, and heavy pure-context categories
    (furniture, casework, ...) skipped. `keep_category_ids` (BuiltInCategory
    ints the project's clash tests reference) is never pruned. This is what
    keeps a huge federated model (a real one was 580 MB / 35M tris at the full
    export) small enough for the WebView2 renderer. The 3D Viewer tool keeps
    the full default export (image_mode=False). Returns a stats dict; raises
    on hard failure so the caller can fall back to the old path."""
    if not _HAVE_REVIT:
        raise RuntimeError("CustomExporter unavailable (no Revit API)")
    from Autodesk.Revit.DB import CustomExporter

    # Coarser curve tessellation for images (octagonal pipes read fine at
    # thumbnail size); the full export stays at the crisp default.
    if image_mode:
        lod = 3

    temp_view = _make_export_view(doc, hide_subcats=hide_subcats,
                                  image_mode=image_mode)
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
    ctx = GltfExportContext(doc, writer, offset, lod, is_canceled=is_canceled,
                            image_mode=image_mode,
                            keep_category_ids=keep_category_ids)
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
        # A user abort surfaces as CustomExporter raising once IsCanceled()
        # returns True; translate it to the clean ExportCanceled signal (no
        # fallback, no partial file left behind).
        if ctx.was_canceled:
            _delete_view(doc, temp_view)
            _remove_quietly(out_path)
            _log("export ABORTED by user")
            raise ExportCanceled("export aborted by user")
        _log("export FAILED: {0}".format(_exc_last()))
        _delete_view(doc, temp_view)
        raise
    # Export() can also return normally once IsCanceled() starts saying True.
    if ctx.was_canceled:
        writer.close()
        _delete_view(doc, temp_view)
        _remove_quietly(out_path)
        _log("export ABORTED by user")
        raise ExportCanceled("export aborted by user")
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
        "elements_with_unique_id": ctx.with_uid,
        "image_mode": image_mode, "skipped_context_elements": ctx.skipped_cats,
    }
    _log("export done: {0} elements, {1:,} tris, {2:,} bytes ({3} MB), {4:.0f}s, "
         "image_mode={5}, skipped_context={6}".format(
             ctx.elements, ctx.triangles, size, size // 1048576, secs,
             image_mode, ctx.skipped_cats))
    _log("identity: {0}/{1} exported elements carry a stable Revit unique_id".format(
        ctx.with_uid, ctx.elements))
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


def _remove_quietly(path):
    """Delete a (possibly partial) output file, ignoring any error."""
    try:
        import os
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


# Architectural + structural "shell" categories. The View Range tool renders
# these with the clean clay + dark poche cut look (so the section reads like a
# Revit cut), and renders everything else (MEP, equipment, furniture, ...) in its
# real Revit colors so the engineer's target element pops. This list no longer
# filters the export -- the .glb now carries every model category -- it only
# classifies shell-vs-colored, surfaced to the web page as shell_categories in
# the meta. Resolved by name with getattr so a category missing in some Revit
# version is simply skipped, not a hard error.
_SHELL_CATEGORY_OST = (
    "OST_Walls", "OST_Floors", "OST_Roofs", "OST_Ceilings",
    "OST_Doors", "OST_Windows", "OST_Stairs", "OST_StairsRailing",
    "OST_Ramps", "OST_Columns", "OST_StructuralColumns",
    "OST_StructuralFraming", "OST_StructuralFoundation",
    "OST_CurtainWallPanels", "OST_CurtainWallMullions",
)


def shell_category_names(doc):
    """Display names (e.g. 'Walls', 'Structural Columns') of the architectural
    shell categories, matching the `category` tag stamped into the .glb. The web
    page renders these with the clay/poche look and everything else in Revit
    colors. Categories absent in this Revit version are skipped."""
    from Autodesk.Revit.DB import BuiltInCategory, Category
    names = set()
    for ost in _SHELL_CATEGORY_OST:
        bic = getattr(BuiltInCategory, ost, None)
        if bic is None:
            continue
        try:
            cat = Category.GetCategory(doc, bic)
            if cat is not None and cat.Name:
                names.add(cat.Name)
        except Exception:
            continue
    return sorted(names)


def _model_z_extent_ft(doc):
    """Vertical span to capture in the scoped export, in feet (internal units):
    from a bit below the lowest level to well above the highest, so foundations
    and roofs/parapets are included. Falls back to a generous default if the doc
    has no levels."""
    from Autodesk.Revit.DB import FilteredElementCollector, Level
    elevs = []
    try:
        for lv in FilteredElementCollector(doc).OfClass(Level):
            try:
                elevs.append(float(lv.Elevation))
            except Exception:
                pass
    except Exception:
        pass
    if not elevs:
        return (-50.0, 300.0)
    return (min(elevs) - 15.0, max(elevs) + 30.0)


def _view_xy_extent(doc, view):
    """World-space XY footprint (Revit feet) of everything the view shows, as
    (minx, miny, maxx, maxy), or None. Used when the view has no active crop, so a
    full-level plan exports the whole building instead of a stale crop box.

    Uses get_BoundingBox(None) (the element's TRUE model bounds) rather than
    get_BoundingBox(view) -- the view-relative box is clipped to the crop, which
    would re-introduce the very cut-off we are trying to avoid."""
    from Autodesk.Revit.DB import FilteredElementCollector
    mnx = mny = 1e30
    mxx = mxy = -1e30
    found = False
    try:
        col = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
        for el in col:
            try:
                bb = el.get_BoundingBox(None)        # model bounds, NOT crop-clipped
                if bb is None:
                    continue
                if bb.Min.X < mnx: mnx = bb.Min.X
                if bb.Min.Y < mny: mny = bb.Min.Y
                if bb.Max.X > mxx: mxx = bb.Max.X
                if bb.Max.Y > mxy: mxy = bb.Max.Y
                found = True
            except Exception:
                continue
    except Exception:
        pass
    return (mnx, mny, mxx, mxy) if found else None


def _view_crop_world(src_view):
    """World-space XY footprint (Revit feet) of a plan view's crop box, as
    (wminx, wminy, wmaxx, wmaxy). The crop box stores crop-local coords; its
    Transform maps them to world. Returns None if the crop is not active (so the
    caller falls back to the full visible extent) or can't be read. Shared by the
    region export and the View Range meta so both agree on the footprint."""
    from Autodesk.Revit.DB import XYZ
    try:
        if not src_view.CropBoxActive:
            return None        # no crop -> use the full visible extent instead
    except Exception:
        pass
    try:
        cb = src_view.CropBox
        xf = cb.Transform
        xs = []
        ys = []
        for ax in (cb.Min.X, cb.Max.X):
            for ay in (cb.Min.Y, cb.Max.Y):
                for az in (cb.Min.Z, cb.Max.Z):
                    p = xf.OfPoint(XYZ(ax, ay, az))
                    xs.append(p.X)
                    ys.append(p.Y)
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        _log("region: crop box read failed ({0})".format(_exc_last()))
        return None


def _make_region_view(doc, src_view):
    """Create a throwaway 3D view section-boxed to the active plan view's crop
    footprint (X/Y) crossed with the building's vertical extent (Z), at Fine
    detail with every category on (host + links). Returns the view, or None on
    failure. Visibility is controlled later in the web tool."""
    from Autodesk.Revit.DB import (
        View3D, ViewFamilyType, ViewFamily, FilteredElementCollector,
        ViewDetailLevel, Transaction, BoundingBoxXYZ, Transform, XYZ,
        WorksetKind, WorksetVisibility, FilteredWorksetCollector,
    )
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

    # World-space XY footprint: the plan view's crop box when cropped, else the
    # full visible extent so an uncropped full-level plan exports the whole
    # building (not a stale crop box). Z comes from the building extent.
    crop = _view_crop_world(src_view)
    src = "crop"
    if crop is None:
        crop = _view_xy_extent(doc, src_view)
        src = "full-extent"
    if crop is None:
        return None
    wminx, wminy, wmaxx, wmaxy = crop
    # Small margin so geometry sitting on the footprint edge isn't shaved off.
    pad = max(2.0, 0.02 * max(wmaxx - wminx, wmaxy - wminy))
    wminx -= pad; wminy -= pad; wmaxx += pad; wmaxy += pad
    zmin, zmax = _model_z_extent_ft(doc)
    _log("region: section box source={0}, X {1:.1f}..{2:.1f} ({3:.1f} ft), "
         "Y {4:.1f}..{5:.1f} ({6:.1f} ft), Z {7:.1f}..{8:.1f}".format(
             src, wminx, wmaxx, wmaxx - wminx, wminy, wmaxy, wmaxy - wminy, zmin, zmax))

    t = Transaction(doc, "dbHMS: view-range region export view")
    t.Start()
    try:
        v3 = View3D.CreateIsometric(doc, vft.Id)
        try:
            # FINE, not Coarse: most MEP (ducts, pipes) and many MEP family
            # instances draw NO 3D body at Coarse detail, so a Coarse export
            # silently drops all of it -- the engineer would never see the
            # equipment they ran the tool to find. Fine matches the 3D Viewer.
            v3.DetailLevel = ViewDetailLevel.Fine
        except Exception:
            pass
        # Every user workset visible so linked arch on a hidden-by-default workset
        # still exports (the same gotcha the old PNG tool had to work around).
        try:
            if doc.IsWorkshared:
                for ws in FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset):
                    try:
                        v3.SetWorksetVisibility(ws.Id, WorksetVisibility.Visible)
                    except Exception:
                        pass
        except Exception:
            pass
        # Export EVERY model category in the footprint (no arch/structural
        # whitelist). The View Range tool needs the whole model present -- an
        # engineer raises the cut plane to find a fan coil unit, ductwork, etc.,
        # which only works if that geometry is in the .glb. Visibility is then
        # controlled per-category in the web tool, with categories the active
        # plan hides starting toggled OFF (see read_hidden_category_names). The
        # element-level _is_non_model filter in OnElementBegin still drops
        # annotation + datum categories, so the .glb stays physical geometry only.
        # Section box = crop footprint x building height. Identity transform keeps
        # it world-axis-aligned.
        box = BoundingBoxXYZ()
        box.Transform = Transform.Identity
        box.Min = XYZ(wminx, wminy, zmin)
        box.Max = XYZ(wmaxx, wmaxy, zmax)
        v3.IsSectionBoxActive = True
        v3.SetSectionBox(box)
        t.Commit()
        _log("region: created scoped 3D view ({0:.1f} x {1:.1f} ft footprint, "
             "Z {2:.1f}..{3:.1f})".format(wmaxx - wminx, wmaxy - wminy, zmin, zmax))
        return v3
    except Exception:
        try:
            t.RollBack()
        except Exception:
            pass
        _log("region: view creation failed ({0})".format(_exc_last()))
        return None


def _ogs_color(ogs):
    """Pick a usable RGB (0..1) from an OverrideGraphicSettings: prefer the filled
    surface/cut colour, fall back to the projection/cut LINE colour (MEP filters
    often only set the line colour, since pipes/ducts read as lines in plan).
    Returns (r,g,b) in 0..1 or None."""
    for attr in ("SurfaceForegroundPatternColor", "CutForegroundPatternColor",
                 "ProjectionLineColor", "CutLineColor"):
        try:
            c = getattr(ogs, attr, None)
            if c is not None and c.IsValid:
                return (c.Red / 255.0, c.Green / 255.0, c.Blue / 255.0)
        except Exception:
            continue
    return None


def _build_filter_color_resolver(doc, src_view):
    """Build a resolver el -> (r,g,b) 0..1 from the plan view's view-filter
    overrides, so MEP exports in its PLAN colour (e.g. green return/waste piping)
    rather than its material colour. Filters are tested in the view's order; the
    first one the element passes wins (Revit applies the top filter with highest
    priority). Returns None if the view has no usable colour filters. Host doc
    only; fully defensive -- any failure just means no filter colour. Parameter
    filters expose a rule-based ElementFilter; selection filters (no ElementFilter)
    are skipped."""
    try:
        fids = list(src_view.GetFilters())
    except Exception:
        return None
    entries = []   # (ElementFilter, (r,g,b))
    for fid in fids:
        try:
            pfe = doc.GetElement(fid)
            if pfe is None:
                continue
            ef = pfe.GetElementFilter()    # None for selection filters
            if ef is None:
                continue
            ogs = src_view.GetFilterOverrides(fid)
            col = _ogs_color(ogs)
            if col is None:
                continue
            entries.append((ef, col))
        except Exception:
            continue
    if not entries:
        _log("region: no colour view-filters on '{0}'".format(getattr(src_view, "Name", "?")))
        return None
    _log("region: {0} colour view-filter(s) will tint matching MEP".format(len(entries)))

    def resolve(el):
        for ef, col in entries:
            try:
                if ef.PassesFilter(el):
                    return col
            except Exception:
                continue
        return None
    return resolve


def export_region(doc, out_path, src_view, lod=8, asset_extras=None,
                  is_canceled=None):
    """Export the geometry inside the active plan view's crop footprint to a .glb
    for the View Range tool: ALL model categories (coarse detail, low LOD), in
    full 3D so the section can slice through the whole vertical extent. Scoped to
    the crop footprint x building height, so it's still much smaller/faster than
    export_view's whole-model export. The web tool controls per-category
    visibility (categories hidden in the source plan start toggled off). Returns a
    stats dict; raises on hard failure so the caller can surface an error."""
    if not _HAVE_REVIT:
        raise RuntimeError("CustomExporter unavailable (no Revit API)")
    from Autodesk.Revit.DB import CustomExporter

    temp_view = _make_region_view(doc, src_view)
    if temp_view is None:
        raise RuntimeError("could not create a scoped region export view")

    offset = _view_offset(doc, temp_view)
    extras = dict(asset_extras or {})
    extras.update({
        "generator": "dbHMS View Range (CustomExporter, scoped)",
        "units": "meters", "axis": "y_up_from_revit_z_up",
        "offset_ft": [offset[0], offset[1], offset[2]], "ft_to_m": FT_TO_M,
    })

    writer = GlbWriter(out_path, asset_extras=extras)
    filter_color = _build_filter_color_resolver(doc, src_view)
    ctx = GltfExportContext(doc, writer, offset, lod, filter_color=filter_color,
                            is_canceled=is_canceled)
    t0 = time.time()
    try:
        exporter = CustomExporter(doc, ctx)
        try:
            exporter.IncludeGeometricObjects = False
        except Exception:
            pass
        try:
            exporter.ShouldStopOnError = False
        except Exception:
            pass
        _log("region export start: lod={0}".format(lod))
        exporter.Export(temp_view)
    except Exception:
        # A cancel may surface either as ctx.was_canceled or as an exception from
        # Export(); treat the cancel case as a clean abort, not a failure.
        writer.close()
        _delete_view(doc, temp_view)
        if ctx.was_canceled:
            _remove_quietly(out_path)
            _log("region export ABORTED by user")
            raise ExportCanceled("export aborted by user")
        _log("region export FAILED: {0}".format(_exc_last()))
        raise
    # Export() can also return normally once IsCanceled() starts saying True.
    if ctx.was_canceled:
        writer.close()
        _delete_view(doc, temp_view)
        _remove_quietly(out_path)
        _log("region export ABORTED by user")
        raise ExportCanceled("export aborted by user")
    size = writer.finalize()
    secs = time.time() - t0
    _delete_view(doc, temp_view)
    stats = {
        "elements": ctx.elements, "triangles": ctx.triangles,
        "polymeshes": ctx.polymeshes, "bytes": size, "seconds": secs,
        # the export centering offset (Revit feet), so the View Range meta can
        # place its clipping planes in the same coordinate frame as the .glb.
        "offset_ft": [offset[0], offset[1], offset[2]],
        # what actually exported, so the caller can log/diagnose missing disciplines
        "by_model": dict(ctx.model_counts), "by_category": dict(ctx.cat_counts),
        "elements_with_unique_id": ctx.with_uid,
    }
    _log("region export done: {0} elements, {1:,} tris, {2:,} bytes, {3:.1f}s".format(
        ctx.elements, ctx.triangles, size, secs))
    _log("identity: {0}/{1} exported elements carry a stable Revit unique_id".format(
        ctx.with_uid, ctx.elements))
    # What actually exported, so a missing-discipline report can be diagnosed from
    # the log (e.g. confirm Mechanical Equipment / Ducts / Pipes made it in).
    try:
        _log("region export by model: {0}".format(
            ", ".join("{0}={1}".format(k, ctx.model_counts[k])
                      for k in sorted(ctx.model_counts))))
        _log("region export by category: {0}".format(
            ", ".join("{0}={1}".format(k, ctx.cat_counts[k])
                      for k in sorted(ctx.cat_counts))))
    except Exception:
        pass
    return stats
