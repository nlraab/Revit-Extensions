# -*- coding: utf-8 -*-
"""View Range Helper - visualize and edit a plan view's view range, live.

Opens a side-by-side editor for the active plan view:
  * Left: Revit-style numeric editor (per-plane Level + Offset).
  * Center: top-down preview of the view (walls + crop boundary) with a
    draggable / rotatable red section line.
  * Right: a synthetic section taken along the section line, showing the
    walls the line cuts plus four colored, draggable horizontal planes
    (Top / Cut / Bottom / View Depth) that map directly to the view range.

Edits stay in memory until you hit Apply. If the active view's view range
is locked by a view template, controls gray out and a banner offers to
detach the view from the template's view-range control or to open the
template for editing.
"""

__title__  = 'View Range\nHelper'
__author__ = 'Nathaniel'
__doc__    = ('Visualize and edit a plan view\'s view range with live '
              'plan + section preview. Handles view-template locks.')

import os
import math
import re
import tempfile
import traceback

import clr  # noqa: F401

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewPlan, ViewSection, Level,
    ViewFamily, ViewFamilyType, ViewDetailLevel, DisplayStyle,
    BuiltInCategory, BuiltInParameter,
    Transaction, TransactionGroup, ElementId, XYZ,
    PlanViewPlane, BoundingBoxXYZ, Transform,
    ImageExportOptions, ImageFileType, ImageResolution,
    ZoomFitType, ExportRange,
)
# Workset visibility — wrapped because non-workshared docs and some
# Revit builds raise on the import; we tolerate that and skip workset
# fixes if unavailable.
try:
    from Autodesk.Revit.DB import (
        FilteredWorksetCollector, WorksetKind, WorksetVisibility,
    )
    _HAVE_WORKSETS = True
except Exception:
    _HAVE_WORKSETS = False

import System
from System import Uri, UriKind
from System.Windows import (
    Thickness, Visibility, Point as WPoint, HorizontalAlignment, VerticalAlignment,
    MessageBox, MessageBoxButton, MessageBoxResult, MessageBoxImage,
)
from System.Windows.Controls import (
    ComboBoxItem, Canvas, TextBlock, Border, StackPanel, Orientation,
)
from System.Windows.Media import (
    SolidColorBrush, Color, Brushes, PointCollection, Stretch,
)
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Shapes import (
    Line as WLine, Rectangle as WRect, Ellipse as WEllipse,
    Polygon as WPolygon,
)
from System.Windows.Input import Cursors, MouseButtonState, Key, MouseButton

from pyrevit import revit, forms, script

# Revit document / UI doc handles
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'ViewRangeHelperForm.xaml')

output = script.get_output()


# ============================================================================
# Constants
# ============================================================================

# Plane palette - kept identical to the XAML swatches and the icon
COLOR_TOP  = "#38A169"  # green
COLOR_CUT  = "#E53E3E"  # red
COLOR_BOT  = "#3182CE"  # blue
COLOR_VD   = "#805AD5"  # purple
COLOR_LVL  = "#A0AEC0"  # gray
COLOR_WALL_FILL    = "#CBD5E0"
COLOR_WALL_OUTLINE = "#4A5568"
COLOR_CROP         = "#A0AEC0"
COLOR_SECTION_LINE = "#E53E3E"

PLANE_KEYS = ("top", "cut", "bot", "vd")


# ============================================================================
# Generic helpers
# ============================================================================

def eid_int(eid):
    """Return the integer Id from an ElementId across Revit API versions."""
    if eid is None:
        return -1
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def wpf_brush(hex_str):
    """Build a WPF SolidColorBrush from a #RRGGBB hex string."""
    h = hex_str.lstrip('#')
    r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    return SolidColorBrush(Color.FromRgb(r, g, b))


def fmt_feet_in(feet):
    """Render decimal feet as feet-and-inches string ('7'-6'')."""
    if feet is None:
        return "-"
    sign = '-' if feet < 0 else ''
    f = abs(feet)
    whole_feet  = int(math.floor(f))
    inches      = (f - whole_feet) * 12.0
    whole_in    = int(round(inches * 16.0)) / 16.0  # 1/16" precision
    if whole_in >= 12.0 - 1e-6:
        whole_feet += 1
        whole_in = 0.0
    return "{}{}'-{:g}\"".format(sign, whole_feet, whole_in)


_FT_IN_RE = re.compile(
    r"""^\s*(-?)\s*
        (?:(?P<ft>\d+(?:\.\d+)?)\s*'\s*[-\s]?\s*)?
        (?:(?P<in>\d+(?:\.\d+)?)\s*(?:")?)?\s*$
    """, re.VERBOSE)


def parse_offset(text):
    """Parse a user-typed offset string into decimal feet.

    Accepts: '7.5', '-3.25', '7'-6"', '7' 6', '6"', etc. Returns None on failure.
    """
    if text is None:
        return None
    s = str(text).strip().replace('’', "'").replace('”', '"').replace('′', "'")
    if s == "":
        return None
    # Decimal feet shortcut
    try:
        return float(s)
    except ValueError:
        pass
    m = _FT_IN_RE.match(s)
    if not m:
        return None
    sign = -1.0 if m.group(1) == '-' else 1.0
    ft   = float(m.group('ft')) if m.group('ft') else 0.0
    inch = float(m.group('in')) if m.group('in') else 0.0
    if not m.group('ft') and not m.group('in'):
        return None
    return sign * (ft + inch / 12.0)


def snap_feet(feet, distance):
    """Snap a decimal-feet value to the nearest multiple of `distance` (also
    in feet). distance <= 0 disables snapping (returns the original value)."""
    if feet is None:
        return None
    if distance is None or distance <= 0:
        return feet
    return round(feet / distance) * distance


# ============================================================================
# Revit data access - levels, view range, walls, crop, template lock
# ============================================================================

def get_all_levels_sorted(doc):
    """Return all Level elements sorted bottom-to-top by elevation."""
    lvls = list(FilteredElementCollector(doc).OfClass(Level).WhereElementIsNotElementType())
    return sorted(lvls, key=lambda l: l.Elevation)


def get_associated_level(view_plan):
    """Return the Level associated with this plan view (GenLevel)."""
    try:
        return view_plan.GenLevel
    except Exception:
        return None


def get_level_by_id(doc, lvl_id):
    """Look up a Level by ElementId. Returns None for invalid / sentinel ids."""
    if lvl_id is None or eid_int(lvl_id) <= 0:
        return None
    el = doc.GetElement(lvl_id)
    return el if isinstance(el, Level) else None


def absolute_z_for_plane(view_plan, level_id, offset_feet, all_levels):
    """Convert a (level_id, offset) pair from a PlanViewRange into an
    absolute Z elevation in feet. Handles the special sentinel ids
    'Level Above', 'Level Below', and 'Unlimited'."""
    base = get_associated_level(view_plan)
    if base is None:
        return None
    base_z = base.Elevation

    iid = eid_int(level_id)
    if iid <= 0:
        # Sentinel - figure out which one
        # Heuristic ordering used by Revit:
        #   PlanViewRange.LevelAbove ~ -3, .LevelBelow ~ -2, .Unlimited ~ -1
        # We'll fall back to detecting by value.
        if iid == -1:
            # Unlimited - place far below for visualization purposes only
            return None  # caller should treat as "off-canvas"
        # Above / Below: find adjacent level
        sorted_lvls = sorted(all_levels, key=lambda l: l.Elevation)
        idx = None
        for i, l in enumerate(sorted_lvls):
            if l.Id == base.Id:
                idx = i; break
        if idx is None:
            return base_z + (offset_feet or 0.0)
        if iid == -3 and idx + 1 < len(sorted_lvls):  # Level Above
            return sorted_lvls[idx + 1].Elevation + (offset_feet or 0.0)
        if iid == -2 and idx - 1 >= 0:                # Level Below
            return sorted_lvls[idx - 1].Elevation + (offset_feet or 0.0)
        return base_z + (offset_feet or 0.0)

    lvl = get_level_by_id(doc, level_id)
    if lvl is None:
        return base_z + (offset_feet or 0.0)
    return lvl.Elevation + (offset_feet or 0.0)


def read_view_range(view_plan):
    """Read the 4 plane settings from a ViewPlan into a dict.

    Returns dict keyed by 'top'/'cut'/'bot'/'vd', each value is
        {'level_id': ElementId, 'offset': float (feet)}.
    """
    pvr = view_plan.GetViewRange()
    out = {}
    mapping = (
        ("top", PlanViewPlane.TopClipPlane),
        ("cut", PlanViewPlane.CutPlane),
        ("bot", PlanViewPlane.BottomClipPlane),
        ("vd",  PlanViewPlane.ViewDepthPlane),
    )
    for key, plane in mapping:
        try:
            lid = pvr.GetLevelId(plane)
        except Exception:
            lid = ElementId.InvalidElementId
        try:
            off = pvr.GetOffset(plane)
        except Exception:
            off = 0.0
        out[key] = {"level_id": lid, "offset": float(off)}
    return out


def write_view_range(view_plan, state, skip_planes=None):
    """Apply a state dict (as built by read_view_range) back to the view.
    Wrap the call in a Revit Transaction. Returns (ok, error_msg).

    `skip_planes` is an optional set of plane keys to skip writing for -
    e.g. on a Ceiling Plan, Bottom is auto-locked to Cut Plane and any
    explicit write may either silently fail or fight Revit's auto-sync."""
    skip = skip_planes or set()
    pvr = view_plan.GetViewRange()
    mapping = (
        ("top", PlanViewPlane.TopClipPlane),
        ("cut", PlanViewPlane.CutPlane),
        ("bot", PlanViewPlane.BottomClipPlane),
        ("vd",  PlanViewPlane.ViewDepthPlane),
    )
    for key, plane in mapping:
        if key in skip:
            continue
        s = state[key]
        try:
            pvr.SetLevelId(plane, s["level_id"])
        except Exception:
            pass
        try:
            pvr.SetOffset(plane, float(s["offset"]))
        except Exception:
            pass
    t = Transaction(doc, "Edit View Range")
    try:
        t.Start()
        view_plan.SetViewRange(pvr)
        t.Commit()
        return True, ""
    except Exception as ex:
        try: t.RollBack()
        except Exception: pass
        return False, str(ex)


def _find_view_range_param_id(template):
    """Look up the ParameterId on a view template that corresponds to the
    'View Range' control. We can't rely on a single BuiltInParameter name
    across Revit versions, so we walk the template's parameters and match
    by definition name.
    """
    try:
        params = template.Parameters
    except Exception:
        return None
    target_names = ("view range", "plan view range")
    for p in params:
        try:
            d = p.Definition
            if d is None: continue
            nm = (d.Name or "").strip().lower()
            if nm in target_names:
                return p.Id
        except Exception:
            continue
    return None


def is_view_range_template_locked(view_plan):
    """Return (is_locked, template_view_or_None).

    A view's view range is template-locked when:
      * The view has a non-invalid ViewTemplateId, AND
      * The View Range parameter is NOT in the template's
        GetNonControlledTemplateParameterIds list (i.e., it IS controlled).
    """
    try:
        tpl_id = view_plan.ViewTemplateId
    except Exception:
        return False, None
    if tpl_id is None or eid_int(tpl_id) == -1:
        return False, None
    tpl = doc.GetElement(tpl_id)
    if tpl is None:
        return False, None
    vr_pid = _find_view_range_param_id(tpl)
    if vr_pid is None:
        # Couldn't locate the parameter by name - fall back to "not locked"
        return False, tpl
    try:
        non_ctrl = list(tpl.GetNonControlledTemplateParameterIds())
    except Exception:
        non_ctrl = []
    locked = True
    for eid in non_ctrl:
        if eid_int(eid) == eid_int(vr_pid):
            locked = False
            break
    return locked, tpl


def detach_view_range_from_template(view_plan):
    """Add the View Range parameter to the template's non-controlled list so
    that the individual view can override it. Returns (ok, msg)."""
    try:
        tpl_id = view_plan.ViewTemplateId
    except Exception:
        return False, "View has no template."
    if eid_int(tpl_id) == -1:
        return False, "View has no template."
    tpl = doc.GetElement(tpl_id)
    if tpl is None:
        return False, "Template element could not be loaded."
    vr_pid = _find_view_range_param_id(tpl)
    if vr_pid is None:
        return False, "Could not find a 'View Range' parameter on the template."
    try:
        non_ctrl = list(tpl.GetNonControlledTemplateParameterIds())
    except Exception:
        non_ctrl = []
    # Already detached?
    for eid in non_ctrl:
        if eid_int(eid) == eid_int(vr_pid):
            return True, "Already detached."
    non_ctrl.append(vr_pid)
    from System.Collections.Generic import List as NetList
    eid_list = NetList[ElementId]()
    for eid in non_ctrl:
        eid_list.Add(eid)
    t = Transaction(doc, "Detach view from template view range")
    try:
        t.Start()
        tpl.SetNonControlledTemplateParameterIds(eid_list)
        t.Commit()
        return True, ""
    except Exception as ex:
        try: t.RollBack()
        except Exception: pass
        return False, str(ex)


# ----------------------------------------------------------------------
# Wall / crop collection used by the plan + section canvases
# ----------------------------------------------------------------------

def get_view_crop_box_xy(view_plan):
    """Return ((min_x, min_y), (max_x, max_y)) of the view's crop region
    in world XY (feet).

    IMPORTANT: this MUST return the actual CropBox bounds even when the
    view has CropBoxActive == False, because the render pipeline forces
    CropBoxActive = True before exporting the PNG. The image will show
    whatever's in the CropBox area, so the overlay coordinates need to
    use the SAME area regardless of whether the user has the crop on
    or off in their view."""
    try:
        cb = view_plan.CropBox        # always available
        if cb is None: return None
        tr = cb.Transform
        corners = [
            tr.OfPoint(XYZ(cb.Min.X, cb.Min.Y, 0)),
            tr.OfPoint(XYZ(cb.Max.X, cb.Min.Y, 0)),
            tr.OfPoint(XYZ(cb.Max.X, cb.Max.Y, 0)),
            tr.OfPoint(XYZ(cb.Min.X, cb.Max.Y, 0)),
        ]
        xs = [p.X for p in corners]; ys = [p.Y for p in corners]
        return (min(xs), min(ys)), (max(xs), max(ys))
    except Exception:
        pass
    return None


def trim_png_white_margins(png_path, white_threshold=240):
    """Crop the white margin off an exported PNG so the rendered content
    fills the entire bitmap. Returns True if the file was rewritten,
    False if no trim was needed (or trim failed).

    WHY: Revit's image export adds a small white margin around the
    rendered view content. Our world↔canvas mapping assumes the PNG
    fills exactly the CropBox area, so any margin causes a proportional
    misalignment between where the section line appears on the plan and
    what the section actually cuts. Trimming the PNG before we hand it
    to WPF eliminates the margin entirely — m2c becomes pixel-accurate
    to the visible content.

    Algorithm: walk in from each edge of the bitmap until we hit a row
    (or column) containing a pixel below `white_threshold` on R/G/B.
    Anything outside the resulting rectangle is considered margin and
    cropped away."""
    try:
        from System.Drawing import Bitmap, Rectangle
        from System.Drawing.Imaging import ImageLockMode, PixelFormat, ImageFormat
        from System.Runtime.InteropServices import Marshal
        from System import Array, Byte
    except Exception:
        return False

    bmp = Bitmap(png_path)
    try:
        w, h = int(bmp.Width), int(bmp.Height)
        if w < 4 or h < 4:
            return False
        rect = Rectangle(0, 0, w, h)
        bmp_data = bmp.LockBits(rect, ImageLockMode.ReadOnly,
                                PixelFormat.Format32bppArgb)
        try:
            stride = int(bmp_data.Stride)
            size = stride * h
            buf = Array.CreateInstance(Byte, size)
            Marshal.Copy(bmp_data.Scan0, buf, 0, size)
        finally:
            bmp.UnlockBits(bmp_data)

        # Format32bppArgb byte order (little-endian): B G R A per pixel.
        T = int(white_threshold)

        def row_has_content(y):
            # Sample every 4th pixel for speed. The margin we're trying
            # to detect is solid white (Revit-imposed), so any sub-4-px
            # content stripe is almost certainly noise we'd want to crop
            # anyway. 4× speedup on big PNGs (~30 ms instead of ~120 ms).
            base = y * stride
            for x in range(0, w, 4):
                i = base + x * 4
                if (buf[i] & 0xFF) < T or (buf[i + 1] & 0xFF) < T or (buf[i + 2] & 0xFF) < T:
                    return True
            return False

        def col_has_content(x):
            col = x * 4
            for y in range(0, h, 4):
                i = y * stride + col
                if (buf[i] & 0xFF) < T or (buf[i + 1] & 0xFF) < T or (buf[i + 2] & 0xFF) < T:
                    return True
            return False

        # Walk in from each side until we hit content.
        top = 0
        while top < h and not row_has_content(top):
            top += 1
        bottom = h - 1
        while bottom > top and not row_has_content(bottom):
            bottom -= 1
        left = 0
        while left < w and not col_has_content(left):
            left += 1
        right = w - 1
        while right > left and not col_has_content(right):
            right -= 1

        if left >= right or top >= bottom:
            return False  # entire bitmap is white — nothing to do
        if left == 0 and top == 0 and right == w - 1 and bottom == h - 1:
            return False  # no margin found

        crop = Rectangle(left, top, right - left + 1, bottom - top + 1)
        cropped = bmp.Clone(crop, bmp.PixelFormat)
        try:
            # Dispose the original bitmap before overwriting the file.
            bmp.Dispose()
            cropped.Save(png_path, ImageFormat.Png)
        finally:
            cropped.Dispose()
        return True
    except Exception:
        try: bmp.Dispose()
        except Exception: pass
        return False


# Whitelist of architectural categories to keep visible in the hybrid Revit
# render. Anything NOT in this list (and that CanCategoryBeHidden) gets
# hidden so the rendered section shows only walls, floors, roofs, openings,
# structure, and similar - no MEP, no annotations.
_ARCH_BIC = (
    BuiltInCategory.OST_Walls,
    BuiltInCategory.OST_Floors,
    BuiltInCategory.OST_Roofs,
    BuiltInCategory.OST_Doors,
    BuiltInCategory.OST_Windows,
    BuiltInCategory.OST_Ceilings,
    BuiltInCategory.OST_Stairs,
    BuiltInCategory.OST_StairsRailing,
    BuiltInCategory.OST_Columns,
    BuiltInCategory.OST_StructuralColumns,
    BuiltInCategory.OST_StructuralFraming,
    BuiltInCategory.OST_StructuralFoundation,
    BuiltInCategory.OST_GenericModel,
    BuiltInCategory.OST_Casework,
    BuiltInCategory.OST_CurtainWallPanels,
    BuiltInCategory.OST_CurtainWallMullions,
    BuiltInCategory.OST_Ramps,
    BuiltInCategory.OST_StructConnections,
    # Revit Links - keep visible so linked architectural models render.
    # Each link's per-category visibility defaults to "By Host View",
    # so the categories we keep visible above will show inside the link.
    BuiltInCategory.OST_RvtLinks,
)


def _copy_view_param_id(src_view, dst_view, bip):
    """Copy an ElementId-typed view parameter (e.g. VIEW_PHASE,
    VIEW_PHASE_FILTER) from src_view to dst_view. Silent on every
    failure — these helpers run inside the section-render transaction
    where any exception would surface as 'Section create failed' and
    be misleading."""
    try:
        src = src_view.get_Parameter(bip)
        dst = dst_view.get_Parameter(bip)
        if src is None or dst is None or dst.IsReadOnly:
            return
        try:
            dst.Set(src.AsElementId())
        except Exception:
            pass
    except Exception:
        pass


def _copy_view_param_int(src_view, dst_view, bip):
    """Copy an integer-typed view parameter (e.g. VIEW_DISCIPLINE)
    from src_view to dst_view. Silent on every failure."""
    try:
        src = src_view.get_Parameter(bip)
        dst = dst_view.get_Parameter(bip)
        if src is None or dst is None or dst.IsReadOnly:
            return
        try:
            dst.Set(src.AsInteger())
        except Exception:
            pass
    except Exception:
        pass


def _copy_workset_visibility(src_view, dst_view):
    """Copy each user workset's visibility (Visible / Hidden /
    UseGlobalSetting) from src_view to dst_view. Falls back to forcing
    'Visible' on any workset that's currently visible in the source.

    On production MEP projects with linked architectural models, the
    link very often lives on a dedicated workset (e.g. 'Linked
    Architecture'). New views inherit project workset defaults — and
    those defaults frequently HIDE that workset on fresh views, so
    spawning a temp ViewSection produces an empty image even though
    OST_RvtLinks is technically a visible category. Mirroring the
    active view's per-workset visibility makes the temp section see
    exactly what the user already sees in plan."""
    if not _HAVE_WORKSETS:
        return
    try:
        if not doc.IsWorkshared:
            return
    except Exception:
        return
    try:
        wsets = FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset)
    except Exception:
        return
    for ws in wsets:
        try:
            try:
                vis = src_view.GetWorksetVisibility(ws.Id)
            except Exception:
                vis = WorksetVisibility.Visible
            try:
                dst_view.SetWorksetVisibility(ws.Id, vis)
            except Exception:
                continue
        except Exception:
            continue


def _apply_arch_only_visibility(view):
    """Hide every category on `view` that isn't in the architectural
    whitelist. Levels are also hidden - the helper draws its own overlay.
    Silently ignores categories that can't be hidden in this view."""
    try:
        keep = set()
        for bic in _ARCH_BIC:
            try:
                keep.add(eid_int(ElementId(bic)))
            except Exception:
                continue
        for cat in doc.Settings.Categories:
            try:
                cid = cat.Id
                if eid_int(cid) in keep:
                    continue
                if view.CanCategoryBeHidden(cid):
                    if not view.GetCategoryHidden(cid):
                        view.SetCategoryHidden(cid, True)
            except Exception:
                continue
    except Exception:
        pass


def get_disabled_planes(view_plan):
    """Return the set of plane keys that should be grayed out for a given
    view type, mirroring Revit's native View Range dialog behavior.

    Reflected Ceiling Plans look UP at the ceiling, so Revit auto-locks
    the Bottom of Primary Range to equal the Cut Plane elevation - a
    ceiling plan never shows anything below the cut. The Bottom field is
    consequently grayed in Revit's dialog. Top, Cut Plane, and View Depth
    remain editable (View Depth on an RCP extends visibility UPWARD past
    the cut, e.g. for seeing through ceilings to floors above).

    Floor Plans, Area Plans, and Engineering Plans: nothing forced.
    """
    try:
        vt_str = str(view_plan.ViewType)
    except Exception:
        return set()
    if vt_str == "CeilingPlan":
        return {"bot"}
    return set()


# ============================================================================
# Geometry helpers (segment/line intersection, projection)
# ============================================================================

def segment_segment_intersect(p1, p2, q1, q2):
    """Return the (x, y) intersection of two line segments, or None.

    Using the standard parametric solver. Coordinates in floats."""
    x1, y1 = p1; x2, y2 = p2
    x3, y3 = q1; x4, y4 = q2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        ix = x1 + t * (x2 - x1)
        iy = y1 + t * (y2 - y1)
        return ix, iy, t, u
    return None


def line_rect_intersect_t_range(p1, p2, rmin, rmax):
    """Liang-Barsky clip: return (t_in, t_out) parameters along p1->p2 where
    the segment is inside the axis-aligned rectangle rmin/rmax, or None."""
    x1, y1 = p1; x2, y2 = p2
    dx = x2 - x1; dy = y2 - y1
    p_arr = (-dx, dx, -dy, dy)
    q_arr = (x1 - rmin[0], rmax[0] - x1, y1 - rmin[1], rmax[1] - y1)
    t_min = 0.0; t_max = 1.0
    for p, q in zip(p_arr, q_arr):
        if abs(p) < 1e-12:
            if q < 0: return None
        else:
            t = q / p
            if p < 0:
                if t > t_max: return None
                if t > t_min: t_min = t
            else:
                if t < t_min: return None
                if t < t_max: t_max = t
    if t_min >= t_max: return None
    return t_min, t_max


def project_param_on_segment(seg_start, seg_end, point):
    """Return parameter t in [0,1] of the closest point on the segment
    to `point` (perpendicular projection, clamped)."""
    sx, sy = seg_start; ex, ey = seg_end
    dx, dy = ex - sx, ey - sy
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return 0.0
    px, py = point
    t = ((px - sx) * dx + (py - sy) * dy) / L2
    return max(0.0, min(1.0, t))


def seg_length(p1, p2):
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def snap_orthogonal(anchor, target):
    """Project `target` onto the closest of (horizontal, vertical) lines
    through `anchor`, in WORLD XY. Used as a fallback when no CropBox
    transform is available; the form's `_snap_orthogonal_image` does
    the same thing in CropBox-local space so rotated plans still get
    on-screen-orthogonal section lines."""
    dx = abs(target[0] - anchor[0])
    dy = abs(target[1] - anchor[1])
    if dx >= dy:
        return (target[0], anchor[1])  # horizontal (Y locked to anchor)
    return (anchor[0], target[1])      # vertical (X locked to anchor)


# ============================================================================
# WPF Form
# ============================================================================

class ViewRangeHelperForm(forms.WPFWindow):

    def __init__(self, view_plan):
        forms.WPFWindow.__init__(self, FORM_XAML)

        # The original active plan view - used for the plan/section
        # visualization regardless of what we're editing.
        self.view_plan      = view_plan
        # The element whose view range is being edited. Starts as the view;
        # may be swapped to a view template via the "Edit template" action.
        self.target         = view_plan

        self.all_levels     = get_all_levels_sorted(doc)
        self.assoc_level    = get_associated_level(view_plan)

        # Initial view range (also kept as 'baseline' for Revert)
        self.state_initial  = read_view_range(self.target)
        self.state          = self._copy_state(self.state_initial)

        # ---- Plan crop bounds + transform (data-accurate plan↔world mapping) ----
        # The rendered plan PNG shows the area inside the view's CropBox.
        # CropBox.Min/Max are in CROP-LOCAL coords; CropBox.Transform converts
        # crop-local → world. For axis-aligned plans the Transform is a pure
        # translation (or identity) and crop-local == world. For rotated plans
        # (CropBox angled to the world axes — common when buildings are not
        # north-aligned), crop-local is rotated relative to world; we must
        # use Transform.Inverse to map world XY to the rendered PNG.
        try:
            cb = view_plan.CropBox
            self.crop_box_min = (float(cb.Min.X), float(cb.Min.Y))
            self.crop_box_max = (float(cb.Max.X), float(cb.Max.Y))
            self.crop_box_transform         = cb.Transform
            self.crop_box_transform_inverse = cb.Transform.Inverse
            self._crop_box_ok = True
        except Exception:
            # Fall back to a generic 100x100 box at origin if the view has no
            # usable CropBox (rare; would mean the view doesn't crop at all).
            self.crop_box_min = (-50.0, -50.0)
            self.crop_box_max = ( 50.0,  50.0)
            self.crop_box_transform         = Transform.Identity
            self.crop_box_transform_inverse = Transform.Identity
            self._crop_box_ok = False
        # Keep a world-AABB version for the initial section line placement
        # (it's intuitive: section line spans the on-screen plan horizontally).
        self.crop_bounds = get_view_crop_box_xy(view_plan)
        if self.crop_bounds is None:
            self.crop_bounds = ((-50.0, -50.0), (50.0, 50.0))
        self.disabled_planes = get_disabled_planes(view_plan)

        # Plan section line - in MODEL coords (feet)
        self.section_line   = self._default_section_line()
        self.section_flip   = False  # toggled by the inline flip icon

        # Far clip - "what you can see ahead of the section line" (visualization
        # only, not written to the view). Default 10' is a reasonable starting
        # value that pulls in nearby walls without flooding the section.
        self.far_clip_offset = 10.0

        # The section view is always a real Revit-rendered PNG of a
        # temporary section. Auto-refreshes after section line / far clip
        # drags; manual refresh button in the section header.
        self.use_revit_render = True
        self._cached_revit_render_path = None

        # Section RENDER bounds — large fixed range that the underlying
        # PNG always covers. Independent of the user's view extent, so
        # the rendered image (and therefore the building) NEVER changes
        # size or position when the user drags the top / bottom extent
        # handles. Re-render only fires when the section LINE itself
        # changes (different a/b on plan) or far-clip changes.
        if self.all_levels:
            proj_z_min = float(min(l.Elevation for l in self.all_levels))
            proj_z_max = float(max(l.Elevation for l in self.all_levels))
        else:
            proj_z_min = 0.0
            proj_z_max = 0.0
        self.section_render_z_min = proj_z_min - 50.0
        self.section_render_z_max = proj_z_max + 50.0

        # Section vertical EXTENT — what the user wants to SEE. The
        # section is rendered with this extent, and the dashed extent
        # lines sit at the literal top/bottom of the rendered image.
        if self.all_levels:
            self.section_z_top = proj_z_max + 15.0
            self.section_z_bot = proj_z_min - 4.0
        else:
            self.section_z_top = 25.0
            self.section_z_bot = -10.0
        # Live preview of an extent drag in progress. While the drag
        # is going, ONLY the dragged extent line follows the cursor;
        # levels, plane lines, and the OTHER extent line stay anchored
        # to the existing image (which still reflects the OLD extent).
        # On release, we commit the preview to section_z_top/_bot and
        # re-render the section so the new bbox renders properly.
        self._ext_preview = None      # None or {'key': 'extent_top'|'_bot', 'z': float}

        # Snap settings (user-editable in the left sidebar)
        self.snap_enabled     = True
        self.snap_distance_ft = 0.5   # 6 inches default

        # Cached image dimensions for the rendered plan PNG (set on load).
        # Used to compute where the image is actually displayed within the
        # plan canvas (Stretch=Uniform letterbox handling).
        self._plan_img_natural_w = None
        self._plan_img_natural_h = None

        # Drag state (transient - reset between drags)
        self._plan_drag     = None    # dict: kind ('endpoint_a'/'endpoint_b'/'body'), ...
        self._sec_drag      = None    # dict: kind ('top'/'cut'/'bot'/'vd'), start_y, start_z
        # Active pan drag (middle-mouse OR confirmed left-empty drag)
        self._plan_pan_drag = None    # dict: 'start_x'/'_y' window coords, 'init_tx'/'_ty'
        self._sec_pan_drag  = None
        # PENDING left-button pan — set on left-click on empty space, but the
        # actual pan only ENGAGES once the cursor moves past _PAN_THRESHOLD_PX.
        # Without this, a near-miss click on a bubble (a few pixels off) feels
        # like the bubble silently broke and the canvas started panning. With
        # the threshold, releasing without dragging stays a no-op so the user
        # can simply click again. See _plan_mouse_down for the rationale.
        self._plan_pan_pending = None
        self._sec_pan_pending  = None

        # Suppress event re-entry
        self._suppress_editor_events = False
        self._suppress_combo_events  = False

        # Wire UI
        self._populate_view_meta()
        self._populate_level_combos()
        self._wire_events()

        # Initial paint - use Loaded so canvases have real sizes
        def _on_loaded(s, e):
            self._refresh_editor_from_state()
            self._refresh_lock_banner()
            # Suppress hit-testing on the plan canvas until the Revit
            # PNG has rendered and the bubbles have snapped to their
            # FINAL positions. Reasoning: _draw_plan is called twice on
            # startup — once now (with the canvas-stretched fallback
            # layout because the image hasn't loaded yet), then again
            # from _load_plan_image after the export completes (with
            # the letterboxed-image layout). If the user clicks during
            # that window, WPF queues the click and dispatches it
            # AFTER the redraw — so a click on the provisional bubble
            # position misses the final bubble position by several
            # pixels and feels like the bubble silently broke. Turning
            # off hit-testing means clicks during this window are
            # simply ignored; the user has to wait for the image, then
            # click cleanly.
            try: self.cnv_plan.IsHitTestVisible = False
            except Exception: pass
            try: self.cnv_section.IsHitTestVisible = False
            except Exception: pass
            self._draw_plan()
            self._draw_section()
            self._update_status()
            # Activate the form and put keyboard focus on the plan
            # canvas so the very first MouseLeftButtonDown is dispatched
            # to our handler instead of being absorbed by Windows'
            # click-to-activate handshake. Without this, on Revit's
            # modal-dialog flow the first click on a bubble feels like
            # a no-op — exactly the "the view needs to be clicked
            # into" symptom users have reported.
            try: self.Activate()
            except Exception: pass
            try:
                self.cnv_plan.Focusable    = True
                self.cnv_section.Focusable = True
                self.cnv_plan.Focus()
            except Exception: pass
            # Force WPF to flush any pending measure / arrange passes
            # so cnv_plan.ActualWidth / Height are stable before the
            # first hit test runs. _plan_transform reads ActualWidth
            # synchronously; a stale value would compute bubble hit
            # coordinates relative to a different canvas size than
            # what's actually painted.
            try: self.UpdateLayout()
            except Exception: pass
            # Kick off the first Revit-rendered plan + section images
            try:    self._refresh_revit_plan()
            except Exception: pass
            try:    self._refresh_revit_render()
            except Exception: pass
            # Plan + section PNGs have loaded and overlay elements are
            # at their final positions; safe to accept clicks now.
            try: self.cnv_plan.IsHitTestVisible = True
            except Exception: pass
            try: self.cnv_section.IsHitTestVisible = True
            except Exception: pass
        self.Loaded += _on_loaded

        # Repaint on resize so the canvases stretch
        self.SizeChanged += lambda s, e: (self._draw_plan(), self._draw_section())

    # ----------------------------------------------------------------------
    # State helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _copy_state(s):
        return { k: { 'level_id': v['level_id'], 'offset': v['offset'] }
                 for k, v in s.items() }

    def _default_section_line(self):
        """Auto-place a section line spanning the crop region horizontally
        ON THE RENDERED IMAGE — i.e. across the CropBox's local X axis,
        not the world X axis. On rotated plans (CropBox angled to true
        north, very common when the building isn't north-aligned) the
        world AABB extends well beyond the actual rendered area, so a
        line placed at world-AABB extremes draws diagonally and lands
        partly off the visible image. Working in CropBox-local coords
        and projecting to world keeps the line horizontal on screen
        and inside the rendered area regardless of plan rotation."""
        try:
            cmnx, cmny = self.crop_box_min
            cmxx, cmxy = self.crop_box_max
            span_x = cmxx - cmnx
            if span_x > 1e-6:
                pad = span_x * 0.05
                cy_local = (cmny + cmxy) / 2.0
                p_a = self.crop_box_transform.OfPoint(XYZ(cmnx + pad, cy_local, 0))
                p_b = self.crop_box_transform.OfPoint(XYZ(cmxx - pad, cy_local, 0))
                return [(float(p_a.X), float(p_a.Y)),
                        (float(p_b.X), float(p_b.Y))]
        except Exception:
            pass
        # Fallback to world-AABB if the CropBox isn't usable
        if self.crop_bounds is not None:
            (mnx, mny), (mxx, mxy) = self.crop_bounds
            cy = (mny + mxy) / 2.0
            pad = (mxx - mnx) * 0.05
            return [(mnx + pad, cy), (mxx - pad, cy)]
        return [(-50.0, 0.0), (50.0, 0.0)]

    def _snap_orthogonal_image(self, anchor_world, target_world):
        """Constrain the endpoint so anchor→endpoint is horizontal or
        vertical on the RENDERED IMAGE — i.e. aligned to CropBox-local
        axes, not world axes. Without this, dragging an endpoint on a
        rotated plan produces a line that's locked to world XY but
        appears diagonal on screen, which is what users were seeing."""
        try:
            inv_t = self.crop_box_transform_inverse
            fwd_t = self.crop_box_transform
            a = inv_t.OfPoint(XYZ(float(anchor_world[0]), float(anchor_world[1]), 0))
            t = inv_t.OfPoint(XYZ(float(target_world[0]), float(target_world[1]), 0))
            ax = float(a.X); ay = float(a.Y)
            tx = float(t.X); ty = float(t.Y)
            if abs(tx - ax) >= abs(ty - ay):
                snapped_local = XYZ(tx, ay, 0)
            else:
                snapped_local = XYZ(ax, ty, 0)
            sw = fwd_t.OfPoint(snapped_local)
            return (float(sw.X), float(sw.Y))
        except Exception:
            return snap_orthogonal(anchor_world, target_world)

    # ----------------------------------------------------------------------
    # Population & wiring
    # ----------------------------------------------------------------------

    def _populate_view_meta(self):
        self.txt_view_name.Text = self.view_plan.Name
        try:
            vt   = self.view_plan.ViewType
            lvl  = self.assoc_level.Name if self.assoc_level else "(no level)"
            crop = "crop on" if self.view_plan.CropBoxActive else "crop off"
            self.txt_view_meta.Text = "{}  |  Level: {}  |  {}".format(str(vt), lvl, crop)
        except Exception:
            self.txt_view_meta.Text = ""

    def _populate_level_combos(self):
        """Populate each plane's level ComboBox with all real levels +
        the appropriate sentinel options."""
        # Special items
        # We expose 'Level Above' (-3), 'Level Below' (-2), 'Unlimited' (-1)
        # in the appropriate combos (matches Revit's native dialog).
        self._suppress_combo_events = True
        try:
            for key, combo, allow_above, allow_below, allow_unlim in (
                ("top", self.cmb_top_level, True,  False, False),
                ("cut", self.cmb_cut_level, False, False, False),
                ("bot", self.cmb_bot_level, False, True,  False),
                ("vd",  self.cmb_vd_level,  False, True,  True),
            ):
                combo.Items.Clear()
                if allow_above:
                    combo.Items.Add(self._build_combo_item("<Above>",     -3))
                # All levels (bottom-up)
                for lvl in self.all_levels:
                    combo.Items.Add(self._build_combo_item(lvl.Name, eid_int(lvl.Id)))
                if allow_below:
                    combo.Items.Add(self._build_combo_item("<Below>",     -2))
                if allow_unlim:
                    combo.Items.Add(self._build_combo_item("<Unlimited>", -1))
        finally:
            self._suppress_combo_events = False

    def _build_combo_item(self, label, lid_int):
        item = ComboBoxItem()
        item.Content = label
        item.Tag = lid_int
        return item

    def _wire_events(self):
        # Numeric editor
        self.cmb_top_level.SelectionChanged += self._on_combo_changed_top
        self.cmb_cut_level.SelectionChanged += self._on_combo_changed_cut
        self.cmb_bot_level.SelectionChanged += self._on_combo_changed_bot
        self.cmb_vd_level.SelectionChanged  += self._on_combo_changed_vd

        for tb in (self.txt_top_offset, self.txt_cut_offset,
                   self.txt_bot_offset, self.txt_vd_offset):
            tb.LostFocus += self._on_offset_lost_focus
            tb.KeyDown   += self._on_offset_keydown

        # Snap controls in the left sidebar
        self.chk_snap.Checked    += self._on_snap_toggle
        self.chk_snap.Unchecked  += self._on_snap_toggle
        self.txt_snap_distance.LostFocus += self._on_snap_distance_lost_focus
        self.txt_snap_distance.KeyDown   += self._on_snap_distance_keydown

        # Footer
        self.btn_apply.Click  += self._on_apply
        self.btn_revert.Click += self._on_revert
        self.btn_close.Click  += self._on_close

        # Plan toolbar
        self.btn_recenter_section.Click += self._on_recenter_section
        self.btn_reset_zoom.Click       += self._on_reset_plan_zoom

        # Manual section refresh + zoom reset
        self.btn_refresh_render.Click      += lambda s, e: self._refresh_revit_render()
        self.btn_reset_section_zoom.Click  += self._on_reset_section_zoom

        # Canvas mouse - bound at canvas level; child shapes set their own cursors
        self.cnv_plan.MouseLeftButtonDown += self._plan_mouse_down
        self.cnv_plan.MouseMove           += self._plan_mouse_move
        self.cnv_plan.MouseLeftButtonUp   += self._plan_mouse_up
        self.cnv_plan.MouseLeave          += self._plan_mouse_up
        # Zoom + pan
        self.cnv_plan.MouseWheel          += self._plan_mouse_wheel
        self.cnv_plan.MouseDown           += self._plan_mouse_down_any
        self.cnv_plan.MouseUp             += self._plan_mouse_up_any

        self.cnv_section.MouseLeftButtonDown += self._sec_mouse_down
        self.cnv_section.MouseMove           += self._sec_mouse_move
        self.cnv_section.MouseLeftButtonUp   += self._sec_mouse_up
        self.cnv_section.MouseLeave          += self._sec_mouse_up
        # Section zoom + pan (mirrors plan: wheel = zoom, middle drag = pan)
        self.cnv_section.MouseWheel          += self._sec_mouse_wheel
        self.cnv_section.MouseDown           += self._sec_mouse_down_any
        self.cnv_section.MouseUp             += self._sec_mouse_up_any

        # Lock banner
        self.btn_detach_view_range.Click += self._on_detach_view_range
        self.btn_edit_template_vr.Click  += self._on_edit_template_vr

    # ----------------------------------------------------------------------
    # Editor sync (state <-> controls)
    # ----------------------------------------------------------------------

    def _refresh_editor_from_state(self):
        """Mirror self.state into the combo boxes + offset textboxes."""
        self._suppress_editor_events = True
        try:
            for key, combo, txt in self._editor_rows():
                s = self.state[key]
                # Select level
                target = eid_int(s['level_id'])
                self._select_combo_by_tag(combo, target)
                # Offset
                txt.Text = fmt_feet_in(s['offset'])
            # Snap distance + checkbox
            self.chk_snap.IsChecked       = bool(self.snap_enabled)
            self.txt_snap_distance.Text   = fmt_feet_in(self.snap_distance_ft)
            # Validate top > cut > bot ordering
            self._validate_state()
        finally:
            self._suppress_editor_events = False

    # ------------------ snap helpers ------------------------------------

    def _snap_value(self, value):
        """Snap a feet value to self.snap_distance_ft if snap is enabled."""
        if not self.snap_enabled or self.snap_distance_ft is None or self.snap_distance_ft <= 0:
            return value
        return snap_feet(value, self.snap_distance_ft)

    def _on_snap_toggle(self, sender, e):
        if self._suppress_editor_events: return
        self.snap_enabled = bool(self.chk_snap.IsChecked)

    def _on_snap_distance_lost_focus(self, sender, e):
        if self._suppress_editor_events: return
        self._commit_snap_distance()

    def _on_snap_distance_keydown(self, sender, e):
        try:
            if e.Key == Key.Enter:
                self._commit_snap_distance()
                e.Handled = True
        except Exception:
            pass

    def _commit_snap_distance(self):
        val = parse_offset(self.txt_snap_distance.Text)
        if val is None or val < 0:
            self._suppress_editor_events = True
            self.txt_snap_distance.Text = fmt_feet_in(self.snap_distance_ft)
            self._suppress_editor_events = False
            return
        self.snap_distance_ft = val
        self._suppress_editor_events = True
        self.txt_snap_distance.Text = fmt_feet_in(val)
        self._suppress_editor_events = False

    def _editor_rows(self):
        return (
            ("top", self.cmb_top_level, self.txt_top_offset),
            ("cut", self.cmb_cut_level, self.txt_cut_offset),
            ("bot", self.cmb_bot_level, self.txt_bot_offset),
            ("vd",  self.cmb_vd_level,  self.txt_vd_offset),
        )

    def _select_combo_by_tag(self, combo, target_int):
        """Select the ComboBoxItem whose .Tag matches target_int. Falls
        back to the level whose Id == target_int for normal-level cases.
        If nothing matches, leaves the selection unchanged."""
        for i in range(combo.Items.Count):
            it = combo.Items[i]
            if int(it.Tag) == int(target_int):
                combo.SelectedIndex = i
                return
        combo.SelectedIndex = -1

    def _on_combo_changed_top(self, s, e): self._handle_combo_change("top", self.cmb_top_level)
    def _on_combo_changed_cut(self, s, e): self._handle_combo_change("cut", self.cmb_cut_level)
    def _on_combo_changed_bot(self, s, e): self._handle_combo_change("bot", self.cmb_bot_level)
    def _on_combo_changed_vd(self, s, e):  self._handle_combo_change("vd",  self.cmb_vd_level)

    def _handle_combo_change(self, key, combo):
        if self._suppress_combo_events or self._suppress_editor_events:
            return
        item = combo.SelectedItem
        if item is None:
            return
        tag = int(item.Tag)
        self.state[key]['level_id'] = ElementId(tag)
        self._draw_section()
        self._update_status(dirty=True)
        self._validate_state()
        # The plan render reflects the cut plane so any view range plane
        # change requires a new plan PNG.
        self._refresh_revit_plan()

    def _on_offset_lost_focus(self, sender, e):
        if self._suppress_editor_events:
            return
        self._commit_offset_textbox(sender)

    def _on_offset_keydown(self, sender, e):
        # Commit on Enter
        try:
            if e.Key == Key.Enter:
                self._commit_offset_textbox(sender)
                e.Handled = True
        except Exception:
            pass

    def _commit_offset_textbox(self, tb):
        # Identify which row this textbox belongs to
        key = None
        for k, _c, t in self._editor_rows():
            if t is tb:
                key = k; break
        if key is None: return
        val = parse_offset(tb.Text)
        if val is None:
            # Re-render the previous valid value
            self._suppress_editor_events = True
            tb.Text = fmt_feet_in(self.state[key]['offset'])
            self._suppress_editor_events = False
            return
        val = self._snap_value(val)
        self.state[key]['offset'] = val
        # Reformat to canonical
        self._suppress_editor_events = True
        tb.Text = fmt_feet_in(val)
        self._suppress_editor_events = False
        self._draw_section()
        self._update_status(dirty=True)
        self._validate_state()
        self._refresh_revit_plan()

    def _validate_state(self):
        """Show an inline warning when the plane elevations don't make
        geometric sense for this view type.

        Floor Plan ordering (descending):  Top >= Cut >= Bottom >= View Depth
        Ceiling Plan ordering:             Top >= Cut, View Depth >= Cut
            (Bottom is locked to Cut by Revit, so we skip Bottom checks.)

        Doesn't block editing - it's just a heads-up."""
        z_top = self._abs_z('top'); z_cut = self._abs_z('cut')
        z_bot = self._abs_z('bot'); z_vd  = self._abs_z('vd')
        msgs = []

        # Top-vs-Cut applies to all view types
        if z_top is not None and z_cut is not None and z_top < z_cut - 1e-6:
            msgs.append("Top ({}) is below Cut Plane ({}).".format(
                fmt_feet_in(z_top), fmt_feet_in(z_cut)))

        if 'bot' in self.disabled_planes:
            # Ceiling Plan: Bottom is auto-locked to Cut, View Depth is
            # expected ABOVE the cut (extends visibility upward).
            if z_vd is not None and z_cut is not None and z_vd < z_cut - 1e-6:
                msgs.append("View Depth ({}) is below Cut Plane ({}) - "
                            "on a Ceiling Plan, View Depth is normally above the "
                            "cut to let you see through ceilings to floors above.".format(
                    fmt_feet_in(z_vd), fmt_feet_in(z_cut)))
        else:
            # Floor Plan ordering
            if z_cut is not None and z_bot is not None and z_cut < z_bot - 1e-6:
                msgs.append("Cut Plane ({}) is below Bottom ({}).".format(
                    fmt_feet_in(z_cut), fmt_feet_in(z_bot)))
            if z_bot is not None and z_vd is not None and z_bot < z_vd - 1e-6:
                msgs.append("Bottom ({}) is below View Depth ({}).".format(
                    fmt_feet_in(z_bot), fmt_feet_in(z_vd)))

        if msgs:
            self.txt_validation.Text = "  ".join(msgs)
            self.bnr_validation.Visibility = Visibility.Visible
        else:
            self.bnr_validation.Visibility = Visibility.Collapsed

    def _abs_z(self, key):
        # Always anchor visualization to the *active view's* level - if
        # we're editing a template that has no associated level, falling
        # back here keeps the section preview meaningful.
        s = self.state[key]
        return absolute_z_for_plane(self.view_plan, s['level_id'], s['offset'], self.all_levels)

    # ----------------------------------------------------------------------
    # Lock banner / detach / edit template
    # ----------------------------------------------------------------------

    def _refresh_lock_banner(self):
        locked, tpl = is_view_range_template_locked(self.view_plan)
        self._template_locked = locked
        self._template = tpl
        if locked and tpl is not None:
            self.bnr_template_lock.Visibility = Visibility.Visible
            self.txt_lock_title.Text = "View range is controlled by template '{}'".format(tpl.Name)
            self.txt_lock_detail.Text = (
                "Editing is disabled. Detach this view's view range from the template, "
                "or open the template's view range to edit it there.")
            self._set_editor_enabled(False)
        else:
            self.bnr_template_lock.Visibility = Visibility.Collapsed
            self._set_editor_enabled(True)

    def _set_editor_enabled(self, enabled):
        """`enabled` = False when the template lock is active (gray everything).
        Otherwise apply the per-view-type plane rules so e.g. View Depth on
        a Ceiling Plan stays grayed out the way Revit's native dialog does."""
        for k, c, t in self._editor_rows():
            row_enabled = enabled and (k not in self.disabled_planes)
            c.IsEnabled = row_enabled
            t.IsEnabled = row_enabled
        self.btn_apply.IsEnabled = enabled

    def _on_detach_view_range(self, s, e):
        ok, msg = detach_view_range_from_template(self.view_plan)
        if not ok:
            MessageBox.Show("Could not detach view range from template:\n\n" + msg,
                            "View Range Helper", MessageBoxButton.OK, MessageBoxImage.Warning)
            return
        # Edit target returns to the view itself. Refresh state; the template
        # lock flag should now be False.
        self.target        = self.view_plan
        self.state_initial = read_view_range(self.target)
        self.state         = self._copy_state(self.state_initial)
        self._refresh_lock_banner()
        self._refresh_editor_from_state()
        self._draw_section()
        self._update_status(extra="Detached view range from template '{}'.".format(
            self._template.Name if self._template else ""))

    def _on_edit_template_vr(self, s, e):
        # The template's view range is edited the same way - via SetViewRange.
        # We simply switch our editing target from the view to the template
        # for the remainder of the session. A short prompt confirms.
        tpl = self._template
        if tpl is None:
            return
        result = MessageBox.Show(
            "Switch this editor to edit the view range of template '{}' instead?\n\n"
            "Any plane changes will then apply to every view that uses the template.".format(tpl.Name),
            "Edit template view range",
            MessageBoxButton.YesNo, MessageBoxImage.Question)
        if result != MessageBoxResult.Yes:
            return
        # Swap edit target only - keep self.view_plan for the visualization
        # context (associated level, walls, crop) so the previews stay useful.
        try:
            self.target        = tpl
            self.state_initial = read_view_range(self.target)
            self.state         = self._copy_state(self.state_initial)
            self.txt_view_name.Text = "[Template] " + tpl.Name
            self.txt_view_meta.Text = ("Editing view range on the template directly. "
                                       "Plan/section preview still uses '{}' for context.".format(self.view_plan.Name))
            self.bnr_template_lock.Visibility = Visibility.Collapsed
            self._set_editor_enabled(True)
            self._refresh_editor_from_state()
            self._draw_section()
            self._update_status(extra="Now editing template view range.")
        except Exception as ex:
            MessageBox.Show("Could not load the template's view range:\n\n" + str(ex),
                            "View Range Helper", MessageBoxButton.OK, MessageBoxImage.Warning)

    # ----------------------------------------------------------------------
    # Footer actions
    # ----------------------------------------------------------------------

    def _on_apply(self, s, e):
        # Skip writing back planes Revit auto-locks (e.g. Bottom on RCPs).
        ok, err = write_view_range(self.target, self.state,
                                   skip_planes=self.disabled_planes)
        if not ok:
            MessageBox.Show("Could not apply view range:\n\n" + err,
                            "View Range Helper", MessageBoxButton.OK, MessageBoxImage.Warning)
            return
        # Refresh baseline so 'Revert' targets the new state
        self.state_initial = self._copy_state(self.state)
        target_label = "template '{}'".format(self.target.Name) \
            if self.target is not self.view_plan else "view"
        self._update_status(extra="Applied to {}.".format(target_label))
        self._refresh_revit_plan()

    def _on_revert(self, s, e):
        self.state = self._copy_state(self.state_initial)
        self._refresh_editor_from_state()
        self._draw_section()
        self._update_status(extra="Reverted to last applied values.")
        self._refresh_revit_plan()

    def _on_close(self, s, e):
        self.Close()

    def _on_recenter_section(self, s, e):
        self.section_line = self._default_section_line()
        self._draw_plan()
        self._draw_section()
        if self.use_revit_render:
            self._refresh_revit_render()

    # ------------------------------------------------------------------
    # Revit plan render - re-renders the active plan view's PNG with the
    # in-progress view range applied, so the user can see what the plan
    # WILL look like once they hit Apply.
    # ------------------------------------------------------------------

    def _refresh_revit_plan(self):
        """Render the active plan view with the pending view range applied
        and load it into the inline image. Called whenever a view range
        plane changes so the user sees the resulting plan live."""
        try:
            path = self._render_revit_plan()
        except Exception as ex:
            self.txt_plan_hint.Text = "Plan render failed: {}".format(ex)
            return
        if not path or not os.path.exists(path):
            self.txt_plan_hint.Text = "Plan render produced no image."
            return
        try:
            self._load_plan_image(path)
        except Exception as ex:
            self.txt_plan_hint.Text = "Could not load plan image: {}".format(ex)
            return
        self.txt_plan_hint.Text = ("Drag the red section line to slide it. Drag an endpoint to "
                                   "rotate or extend it. Click the swap icon to flip view direction.")

    def _load_plan_image(self, path):
        bi = BitmapImage()
        bi.BeginInit()
        bi.UriSource = Uri(path, UriKind.Absolute)
        bi.CacheOption = BitmapCacheOption.OnLoad
        bi.EndInit()
        bi.Freeze()
        self.img_plan.Source = bi
        try:
            self._plan_img_natural_w = float(bi.PixelWidth)
            self._plan_img_natural_h = float(bi.PixelHeight)
        except Exception:
            self._plan_img_natural_w = self._plan_img_natural_h = None
        self._draw_plan()

    def _plan_image_layout(self):
        """Where the rendered plan image is actually displayed inside the
        plan canvas (Stretch=Uniform letterboxing). Returns
        (offset_x, offset_y, width, height). Falls back to the full canvas
        if no image has loaded yet."""
        cw = max(1.0, float(self.cnv_plan.ActualWidth))
        ch = max(1.0, float(self.cnv_plan.ActualHeight))
        nw = self._plan_img_natural_w
        nh = self._plan_img_natural_h
        if not nw or not nh or nw <= 0 or nh <= 0:
            return (0.0, 0.0, cw, ch)
        img_aspect    = nw / nh
        canvas_aspect = cw / ch
        if img_aspect > canvas_aspect:
            display_w = cw
            display_h = cw / img_aspect
            return (0.0, (ch - display_h) / 2.0, display_w, display_h)
        display_h = ch
        display_w = ch * img_aspect
        return ((cw - display_w) / 2.0, 0.0, display_w, display_h)

    def _render_revit_plan(self):
        """Apply the pending view range to the active plan view inside a
        TransactionGroup, force CropBoxActive=True so the rendered image
        matches the crop bounds we use for overlay coordinates, export to
        PNG, then roll back so nothing persists in the model."""
        view = self.view_plan
        tmp_dir = tempfile.mkdtemp(prefix="vrh_plan_")
        out_base = os.path.join(tmp_dir, "preview")

        tg = TransactionGroup(doc, "Render plan preview")
        tg.Start()
        try:
            t = Transaction(doc, "Apply pending view range for preview")
            t.Start()
            try:
                # Force the crop on so the exported image matches our
                # overlay bounds EXACTLY. The two settings below together
                # guarantee the rendered PNG covers exactly the model
                # CropBox area:
                #   - CropBoxActive=True: render is bounded by CropBox
                #   - AnnotationCropActive=False: render isn't expanded
                #     by an annotation crop offset (which would shift the
                #     content and cause the section line on the plan to
                #     no longer line up with what's in the section)
                #   - CropBoxVisible=False: crop boundary line isn't drawn
                try:
                    if not view.CropBoxActive:
                        view.CropBoxActive = True
                    try:    view.CropBoxVisible = False
                    except Exception: pass
                    try:
                        mgr = view.GetCropRegionShapeManager()
                        # Some Revit builds expose this as a property,
                        # others as Get/Set methods. Try both.
                        try:
                            if getattr(mgr, 'AnnotationCropActive', False):
                                mgr.AnnotationCropActive = False
                        except Exception:
                            try:
                                if mgr.GetAnnotationCropActive():
                                    mgr.SetAnnotationCropActive(False)
                            except Exception:
                                pass
                    except Exception:
                        pass
                except Exception:
                    pass
                # Apply the in-progress view range
                pvr = view.GetViewRange()
                for key, plane in (
                    ("top", PlanViewPlane.TopClipPlane),
                    ("cut", PlanViewPlane.CutPlane),
                    ("bot", PlanViewPlane.BottomClipPlane),
                    ("vd",  PlanViewPlane.ViewDepthPlane),
                ):
                    if key in self.disabled_planes:
                        continue
                    s = self.state[key]
                    try: pvr.SetLevelId(plane, s["level_id"])
                    except Exception: pass
                    try: pvr.SetOffset(plane, float(s["offset"]))
                    except Exception: pass
                view.SetViewRange(pvr)
                t.Commit()
            except Exception as ex:
                try: t.RollBack()
                except Exception: pass
                tg.RollBack()
                self.txt_plan_hint.Text = "Plan setup failed: {}".format(ex)
                return None

            opts = ImageExportOptions()
            opts.ZoomType         = ZoomFitType.FitToPage
            # Higher PixelSize = more pixels per foot = less alignment error
            # from any residual border / fractional-pixel rendering.
            opts.PixelSize        = 3000
            opts.ImageResolution  = ImageResolution.DPI_150
            opts.ExportRange      = ExportRange.SetOfViews
            from System.Collections.Generic import List as NetList
            view_list = NetList[ElementId]()
            view_list.Add(view.Id)
            opts.SetViewsAndSheets(view_list)
            opts.FilePath              = out_base
            opts.HLRandWFViewsFileType = ImageFileType.PNG
            try:
                doc.ExportImage(opts)
            except Exception as ex:
                tg.RollBack()
                self.txt_plan_hint.Text = "Plan export failed: {}".format(ex)
                return None
        finally:
            try: tg.RollBack()
            except Exception: pass

        # Locate the exported PNG, then trim Revit's white margins so that
        # the bitmap's pixel bounds match the rendered crop area exactly.
        # Without this, the PNG content sits inset from the bitmap edges,
        # and our world↔canvas math drifts proportionally with distance
        # from the center of the view.
        try:
            for f in os.listdir(tmp_dir):
                if f.lower().endswith('.png'):
                    png_path = os.path.join(tmp_dir, f)
                    try: trim_png_white_margins(png_path)
                    except Exception: pass
                    return png_path
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Revit section render
    # ------------------------------------------------------------------

    def _refresh_revit_render(self):
        """Render the current section configuration via Revit and load
        the resulting PNG into the inline image. Called after drags end
        and on the manual refresh button."""
        # Show progress feedback
        self.txt_section_hint.Text = "Rendering Revit section preview..."
        # Reset diagnostic on each attempt so stale messages don't bleed
        # through between renders.
        self._last_render_msg = None
        try:
            path = self._render_revit_section()
        except Exception as ex:
            self.txt_section_hint.Text = "Render failed: {}".format(ex)
            return
        if not path or not os.path.exists(path):
            # _render_revit_section sets _last_render_msg with a more
            # actionable description (file count, paths it looked at,
            # etc.) when it returns None at the export-output-search
            # step. Fall back to the generic message only if nothing
            # specific was set.
            msg = getattr(self, '_last_render_msg', None) \
                  or "Render produced no image."
            self.txt_section_hint.Text = msg
            return
        try:
            self._load_image(path)
        except Exception as ex:
            self.txt_section_hint.Text = "Could not load image: {}".format(ex)
            return
        self._cached_revit_render_path = path
        self.txt_section_hint.Text = ("Drag colored lines on the overlay to "
                                      "move each plane. Apply writes to the view.")

    def _load_image(self, path):
        bi = BitmapImage()
        bi.BeginInit()
        bi.UriSource = Uri(path, UriKind.Absolute)
        bi.CacheOption = BitmapCacheOption.OnLoad
        bi.EndInit()
        bi.Freeze()
        # Cache natural dims FIRST so _section_image_layout can use them
        # to compute the slot height that matches the new PNG's aspect.
        try:
            self._img_natural_w = float(bi.PixelWidth)
            self._img_natural_h = float(bi.PixelHeight)
        except Exception:
            self._img_natural_w = self._img_natural_h = None
        # Apply the new geometry BEFORE swapping the source, so the new
        # bitmap is rendered into the new (correctly-shaped) slot from
        # frame zero — no flash of the new content stretched into the
        # old slot.
        self._update_section_image_geometry()
        self.img_section.Source = bi
        # Repaint the overlay (A/B chips, plane labels, etc.) so they
        # align with the newly positioned image.
        self._draw_section()

    def _update_section_image_geometry(self):
        """Push canvas-local position + size from _section_image_layout
        onto the img_section element. Stretch.Fill (no letterbox) so
        the PNG content fills the slot exactly — building elements at
        z=B always land at the slot pixel corresponding to z=B per the
        bbox math, which equals canvas_anchor_y - K*(B - anchor_z).
        Building stays nailed in place when the user drags an extent
        handle (image grows / shrinks; building doesn't move)."""
        try:
            ox, oy, dw, dh = self._section_image_layout()
            self.img_section.Stretch             = Stretch.Fill
            self.img_section.HorizontalAlignment = HorizontalAlignment.Left
            self.img_section.VerticalAlignment   = VerticalAlignment.Top
            self.img_section.Width  = max(1.0, dw)
            self.img_section.Height = max(1.0, dh)
            self.img_section.Margin = Thickness(ox, oy, 0, 0)
        except Exception:
            pass

    def _section_anchor_z(self):
        """Model elevation that stays glued to a fixed canvas Y, so
        levels / planes / building elements DO NOT move when the user
        drags the section's top / bottom extent handles.

        Midpoint of project levels (or 0 if none). Centering on the
        midpoint puts the building's middle at the canvas center, so
        for tall buildings the upper levels and the lower levels are
        BOTH within the visible canvas instead of one set being
        pushed off-screen."""
        try:
            if self.all_levels:
                elevs = [l.Elevation for l in self.all_levels]
                return float((min(elevs) + max(elevs)) / 2.0)
        except Exception:
            pass
        return 0.0

    def _section_canvas_scale(self):
        """Pixels per foot for the section overlay. Uses the FULL inner
        canvas width (not the gutter-narrowed visible area) so the
        building is rendered at the same scale as Revit's native
        section view. Section line endpoints fall under the gutters,
        but the building proper sits at the right size in the middle.

        FIXED with respect to extent changes (depends only on L), so
        levels and planes stay glued to their canvas Y when the user
        drags the top / bottom extent handles."""
        try:
            cw = float(self.cnv_section.ActualWidth)
        except Exception:
            cw = 700.0
        if cw < 50: cw = 700.0
        L = max(0.001, seg_length(self.section_line[0], self.section_line[1]))
        return cw / L

    def _section_image_layout(self):
        """Position + size of the rendered section image in canvas-local
        coords using a FIXED scale anchor. Returns (ox, oy, dw, dh).

        The math:
          canvas_scale = cw / L  (constant for a given section line)
          dw           = cw      (image fills canvas width)
          dh           = canvas_scale × z_span  (varies with extent)
          oy           = canvas_anchor_y - (z_max - anchor_z) × canvas_scale

        anchor_z (lowest project level) is glued to canvas_anchor_y
        (canvas_h × 0.85, near the bottom) for the entire session. With
        this, y_at(z) = canvas_anchor_y - (z - anchor_z) × canvas_scale
        is INDEPENDENT of z_min / z_max — building elements, levels,
        and plane lines all stay at their canvas Y when the user drags
        the top or bottom extent handle. Only the image's top edge
        moves (to expose more or less area)."""
        try:
            cw = float(self.cnv_section.ActualWidth)
            ch = float(self.cnv_section.ActualHeight)
        except Exception:
            cw, ch = 700.0, 400.0
        if cw < 50: cw = 700.0
        if ch < 50: ch = 400.0
        L = max(0.001, seg_length(self.section_line[0], self.section_line[1]))
        canvas_scale = cw / L
        z_min, z_max = self._section_z_range()
        z_span = max(0.001, z_max - z_min)
        if self.all_levels:
            anchor_z = float(min(l.Elevation for l in self.all_levels))
        else:
            anchor_z = 0.0
        canvas_anchor_y = ch * 0.85
        image_w   = cw
        image_h   = canvas_scale * z_span
        image_top = canvas_anchor_y - (z_max - anchor_z) * canvas_scale
        return (0.0, image_top, image_w, image_h)

    def _render_revit_section(self):
        """Create a temporary ViewSection at the current section line +
        far clip, export it to PNG, and roll back the section creation so
        nothing persists in the document. Returns the PNG path or None."""
        a = self.section_line[0]; b = self.section_line[1]
        dx = b[0] - a[0]; dy = b[1] - a[1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return None

        # Section coordinate system - Revit-standard "Convention B":
        #   BasisX = section_dir (A->B)            (right side of section view)
        #   BasisY = up
        #   BasisZ = BasisX x BasisY               (right-handed; into model)
        #   Origin Z = z_min  (bottom of vertical extent)
        #   bbox.Min = (-L/2, 0, 0)                near clip at section line
        #   bbox.Max = (+L/2, height, +far_clip)   far clip into the model
        #
        # Default (no flip): looking "right of A->B walking"
        #   - A=south, B=north section -> look east (matches Revit default)
        # When flipped: BasisX = -section_dir, view direction reverses,
        #   system stays right-handed.
        ux_m = dx / L; uy_m = dy / L
        if not self.section_flip:
            basisX = XYZ(ux_m, uy_m, 0)                 # A -> B direction
        else:
            basisX = XYZ(-ux_m, -uy_m, 0)               # B -> A direction
        section_up = XYZ(0, 0, 1)
        basisZ     = basisX.CrossProduct(section_up)    # right-handed, into model

        # Render with the USER's current view extent. Image fills the
        # canvas via Stretch.Uniform; overlay derives Y positions from
        # the same layout, so image and overlay always align perfectly.
        z_min, z_max = self._section_z_range()
        # Origin Z at the BOTTOM of the rendered extent. With
        # bbox.Min.Y = 0 and Max.Y = height, the section view spans
        # exactly from z_min to z_max in model elevation.
        origin = XYZ((a[0] + b[0]) / 2.0,
                     (a[1] + b[1]) / 2.0,
                     z_min)

        transform = Transform.Identity
        transform.Origin = origin
        transform.BasisX = basisX
        transform.BasisY = section_up
        transform.BasisZ = basisZ

        half_w = L / 2.0
        height = max(1.0, z_max - z_min)
        far    = max(self.far_clip_offset, 1.0)

        # Bbox spans EXACTLY from A to B in basisX with no horizontal
        # padding. Data accuracy: what the user draws as the section line
        # on the plan is precisely what's cut. The screen-anchored gutters
        # on cnv_section_screen handle label space — they don't need any
        # model-coord padding to back them up. (Padding remains a hook in
        # _section_render_padding for future use, currently 0/0.)
        left_pad, right_pad = self._section_render_padding()

        bbox = BoundingBoxXYZ()
        bbox.Transform = transform
        bbox.Min = XYZ(-half_w - left_pad,  0,      0)
        bbox.Max = XYZ(+half_w + right_pad, height, far)

        # Find a section ViewFamilyType — cache it on first use; the
        # project's view family types don't change during a session.
        vft_id = getattr(self, '_section_vft_id_cache', None)
        if vft_id is None:
            for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
                try:
                    if vft.ViewFamily == ViewFamily.Section:
                        vft_id = vft.Id; break
                except Exception:
                    continue
            self._section_vft_id_cache = vft_id
        if vft_id is None:
            return None

        # Render in a TransactionGroup we'll roll back so the temp section
        # doesn't persist. ExportImage runs synchronously, so the file is on
        # disk by the time we roll back.
        tmp_dir = tempfile.mkdtemp(prefix="vrh_")
        out_base = os.path.join(tmp_dir, "preview")

        tg = TransactionGroup(doc, "Render section preview")
        tg.Start()
        try:
            t = Transaction(doc, "Create temp section")
            t.Start()
            try:
                section = ViewSection.CreateSection(doc, vft_id, bbox)
                # Drop any project default view template - if one was
                # applied automatically, it can override visibility / display
                # style and produce inconsistent renders.
                try:
                    section.ViewTemplateId = ElementId.InvalidElementId
                except Exception:
                    try: section.ViewTemplateId = ElementId(-1)
                    except Exception: pass
                # Copy phase, phase filter, and discipline from the active
                # plan view (`self.view_plan` — _render_revit_section
                # doesn't have a local `view` variable; that only exists
                # in _render_revit_plan, and using `view` here previously
                # raised NameError, which the wider try/except surfaced
                # as 'Section create failed: global name view is not
                # defined' and rendered the whole section blank).
                src_view = self.view_plan
                _copy_view_param_id(src_view, section, BuiltInParameter.VIEW_PHASE)
                _copy_view_param_id(src_view, section, BuiltInParameter.VIEW_PHASE_FILTER)
                _copy_view_param_int(src_view, section, BuiltInParameter.VIEW_DISCIPLINE)
                # Design option (some projects scope linked content to
                # specific options — without this the section sees the
                # main model only).
                try:
                    _copy_view_param_id(src_view, section,
                                        BuiltInParameter.VIEW_DESIGN_OPTION_ID)
                except Exception:
                    pass
                # Workset visibility — mirror per-workset visibility from
                # the active view. Critical for MEP host + linked arch
                # workflow: the link's workset defaults to hidden on new
                # views in many projects, so the temp section renders
                # blank without this fix.
                _copy_workset_visibility(src_view, section)
                # Force Hidden Line + Coarse so cut walls render with poché.
                # Try the property first, then the BuiltInParameter as backup
                # in case the property isn't writable in this Revit version.
                try:
                    section.DisplayStyle = DisplayStyle.HiddenLine
                except Exception:
                    try:
                        p = section.get_Parameter(BuiltInParameter.MODEL_GRAPHICS_STYLE)
                        if p: p.Set(2)   # 2 = Hidden Line
                    except Exception: pass
                try:
                    section.DetailLevel = ViewDetailLevel.Coarse
                except Exception:
                    try:
                        p = section.get_Parameter(BuiltInParameter.VIEW_DETAIL_LEVEL)
                        if p: p.Set(1)   # 1 = Coarse
                    except Exception: pass
                # Hybrid mode: hide everything except architectural categories.
                # Hide the crop region annotation line so it doesn't eat
                # content area in the rendered image.
                try:    section.CropBoxVisible = False
                except Exception: pass
                _apply_arch_only_visibility(section)
                t.Commit()
            except Exception as ex:
                try: t.RollBack()
                except Exception: pass
                # Surface the actual exception so we can debug section
                # creation. Stashed on _last_render_msg so the caller
                # (_refresh_revit_render) shows it rather than the
                # generic 'Render produced no image' fallback.
                tg.RollBack()
                self._last_render_msg = "Section create failed: {}".format(ex)
                return None

            opts = ImageExportOptions()
            opts.ZoomType         = ZoomFitType.FitToPage
            # Higher PixelSize = more pixels per foot = less alignment
            # error from any residual border / fractional-pixel rendering.
            opts.PixelSize        = 3000
            opts.ImageResolution  = ImageResolution.DPI_150
            opts.ExportRange      = ExportRange.SetOfViews
            from System.Collections.Generic import List as NetList
            view_list = NetList[ElementId]()
            view_list.Add(section.Id)
            opts.SetViewsAndSheets(view_list)
            opts.FilePath              = out_base
            opts.HLRandWFViewsFileType = ImageFileType.PNG

            try:
                doc.ExportImage(opts)
            except Exception as ex:
                tg.RollBack()
                self._last_render_msg = "Image export failed: {}".format(ex)
                return None
        finally:
            try: tg.RollBack()
            except Exception: pass

        # Locate the exported PNG. Do NOT trim — for the SECTION the
        # bbox legitimately includes empty space above and below the
        # building (the user's view extent), and trimming would remove
        # that empty space, leaving only the building. Stretching the
        # trimmed PNG into the slot would then put building elements
        # 30-70 px below their proper canvas Y, breaking alignment with
        # level / plane markers. Plan PNG still gets trimmed (its bbox
        # = building footprint, no empty space).
        try:
            files = os.listdir(tmp_dir)
        except Exception as ex:
            self._last_render_msg = ("Render output dir unreadable ({}): {}"
                                     .format(tmp_dir, ex))
            return None
        for f in files:
            if f.lower().endswith('.png'):
                return os.path.join(tmp_dir, f)
        # No PNG produced. Surface what's actually in the temp dir so
        # we can tell whether ExportImage wrote nothing, wrote with a
        # different extension, or wrote elsewhere. Without this the
        # user only sees a generic "Render produced no image" message
        # and we can't iterate.
        if not files:
            self._last_render_msg = (
                "Section render: ExportImage produced no files in {}. "
                "This usually means the section view rendered empty "
                "(no geometry intersects the section bbox). Check that "
                "the red section line on the plan crosses through "
                "building elements, increase the far-clip handle, or "
                "verify the active view's phase / workset settings show "
                "the linked model.".format(tmp_dir))
        else:
            self._last_render_msg = (
                "Section render: no PNG in {}; got files: {}"
                .format(tmp_dir, ", ".join(files[:8])))
        return None

    def _update_status(self, dirty=False, extra=None):
        bits = []
        if extra: bits.append(extra)
        if dirty:
            bits.append("Pending changes - hit Apply to write.")
        else:
            tgt_label = self.target.Name
            if self.target is not self.view_plan:
                tgt_label = "[Template] " + tgt_label
            bits.append("Editing: {}".format(tgt_label))
        self.txt_status.Text = "   ".join(bits)

    # ======================================================================
    # PLAN canvas - drawing & dragging
    # ======================================================================

    def _plan_transform(self):
        """Return a (model_to_canvas, canvas_to_model, w, h, scale) tuple
        for the plan canvas. m2c returns INTRINSIC canvas-local coords
        (no zoom / pan applied). The actual zoom + pan lives on the parent
        Grid's RenderTransform — so both the image AND the canvas (and
        everything drawn on it) get transformed together. That means the
        section line / bubbles stay glued to the building at the pixel
        level; there's no sub-pixel drift between two independently
        transformed elements.

        DATA ACCURACY: m2c/c2m use the CropBox's LOCAL coord system + its
        Transform — so a click at canvas position X corresponds to the
        WORLD position that's actually under that pixel of the rendered
        PNG, regardless of whether the plan view is rotated. The PNG
        Revit exports (with CropBoxActive=True) is precisely the
        rectangle [cb.Min.X, cb.Max.X] × [cb.Min.Y, cb.Max.Y] in
        crop-local coords, so:
            world → crop-local: Transform.Inverse
            crop-local → image fraction: (lx - cb.Min.X) / (cb.Max.X - cb.Min.X)
            image fraction → canvas: ox + frac * dw
        For un-rotated views, Transform is identity (or pure translation)
        and the math reduces to the simple AABB case. For rotated views,
        the PNG content sits where it actually is on screen — section
        lines stop bleeding off the building.

        WPF's hit testing automatically inverts the Grid transform when
        you call e.GetPosition(self.cnv_plan), so cursor positions also
        come in canvas-local coords; c2m is the data-accurate inverse.

        Because the Grid transform scales children visually, drawing
        helpers must multiply all SIZES (radii, stroke widths, font
        sizes, dash arrays) by self._plan_inv_scale so bubbles / lines
        appear at constant screen-pixel sizes regardless of zoom."""
        ox, oy, dw, dh = self._plan_image_layout()
        cw = max(1.0, float(self.cnv_plan.ActualWidth))
        ch = max(1.0, float(self.cnv_plan.ActualHeight))

        cmnx, cmny = self.crop_box_min
        cmxx, cmxy = self.crop_box_max
        span_x = max(0.001, cmxx - cmnx)
        span_y = max(0.001, cmxy - cmny)
        inv_t  = self.crop_box_transform_inverse
        fwd_t  = self.crop_box_transform

        def m2c(mx, my):
            # World XY → CropBox local XY → image fraction → canvas-local
            p = inv_t.OfPoint(XYZ(mx, my, 0))
            lx = p.X; ly = p.Y
            cx_img = ox + (lx - cmnx) / span_x * dw
            cy_img = oy + (cmxy - ly) / span_y * dh   # flip Y so north is up
            return cx_img, cy_img

        def c2m(px, py):
            # Canvas-local → image fraction → CropBox local → world
            fx = (px - ox) / max(0.001, dw)
            fy = (py - oy) / max(0.001, dh)
            lx = cmnx + fx * span_x
            ly = cmxy - fy * span_y
            p  = fwd_t.OfPoint(XYZ(lx, ly, 0))
            return p.X, p.Y

        # px-per-ft at zoom = 1 (intrinsic). Multiply by current zoom for
        # actual on-screen scale; we expose the intrinsic value here.
        scale = (dw / span_x) if span_x > 0 else 1.0
        return m2c, c2m, cw, ch, scale

    def _current_plan_inv_scale(self):
        """Inverse of the plan Grid's current zoom. Multiply lengths by
        this when drawing on cnv_plan so they stay constant size on
        screen (because the Grid scales everything visually)."""
        try:
            s = float(self.trf_plan_scale.ScaleX)
        except Exception:
            s = 1.0
        if s < 1e-6: s = 1.0
        return 1.0 / s

    def _draw_plan(self):
        self.cnv_plan.Children.Clear()
        m2c, _c2m, cw, ch, scale = self._plan_transform()
        # Stash the current inverse zoom so every overlay-drawing helper
        # can scale its sizes back down (the parent Grid scales them up).
        self._plan_inv_scale = self._current_plan_inv_scale()
        inv_s = self._plan_inv_scale
        # The rendered plan PNG sits behind cnv_plan (img_plan element).
        # The canvas just hosts overlays: section line, far clip, bubbles,
        # flip icon. Walls / doors / windows / crop boundary all show up
        # in the rendered image itself.

        # Far clip line (dashed parallel showing how far ahead of the
        # section line the section preview pulls elements from)
        self._draw_plan_far_clip_line(m2c, inv_s)

        # Section line + bubbles + flip icon (drawn on top)
        self._draw_plan_section_line(m2c, inv_s)

    # Far clip handle radius (px). Used by both draw + hit-test.
    _FC_HANDLE_R = 11

    # Cursor must travel this many pixels (in WINDOW coords) before a
    # left-click on empty space is promoted to a pan. Stops near-miss
    # clicks on bubbles from feeling like the bubble broke and the
    # canvas started panning instead.
    _PAN_THRESHOLD_PX = 5
    # Square of the threshold so the move-handler can avoid sqrt.
    _PAN_THRESHOLD_PX_SQ = 25

    # Bubble / body hit-test slack (extra px outside the visible
    # element that still registers as a hit). Generous targets reduce
    # the rate of accidental empty-space clicks → unwanted pans.
    _BUBBLE_HIT_SLACK = 6
    _BODY_HIT_SLACK   = 4

    def _far_clip_handle_screen(self, m2c):
        """Return the screen (x, y) of the draggable far clip handle, or
        None if the handle isn't currently rendered (far_clip_offset = 0)."""
        if self.far_clip_offset <= 0:
            return None
        a, b, _ux, _uy, nx, ny, _L = self._section_view_basis()
        b = self.section_line[1]
        far = self.far_clip_offset
        # Midpoint of the far clip line in model coords
        mid_a_m = ((a[0] + b[0]) / 2.0 + nx * far,
                   (a[1] + b[1]) / 2.0 + ny * far)
        return m2c(*mid_a_m)

    def _draw_plan_far_clip_line(self, m2c, inv_s):
        """Dashed parallel line showing where the far clip plane is in plan,
        with a draggable two-arrow handle at its midpoint.

        inv_s = 1 / current Grid zoom. All visual sizes (stroke widths,
        radii, dash patterns, arrow dimensions) get multiplied by it so
        the handle stays a constant pixel size on screen regardless of
        zoom (the parent Grid transform cancels out the inv_s)."""
        if self.far_clip_offset <= 0:
            return
        a, _b, ux, uy, nx, ny, _L = self._section_view_basis()
        b = self.section_line[1]
        far = self.far_clip_offset
        fc_a = (a[0] + nx * far, a[1] + ny * far)
        fc_b = (b[0] + nx * far, b[1] + ny * far)
        ax, ay = m2c(*fc_a); bx, by = m2c(*fc_b)

        ln = WLine()
        ln.X1 = ax; ln.Y1 = ay; ln.X2 = bx; ln.Y2 = by
        ln.Stroke = wpf_brush("#A0AEC0")
        ln.StrokeThickness = 1.0 * inv_s
        # Dash array values are MULTIPLES of stroke thickness (WPF math).
        # We already scale stroke by inv_s, so leave dash values constant —
        # otherwise dashes would shrink as inv_s² and look like specks at
        # high zoom.
        ln.StrokeDashArray = self._dash_array([3, 3])
        ln.IsHitTestVisible = False
        self.cnv_plan.Children.Add(ln)

        # Connector ticks at the ends so the eye links section line ↔ far clip
        for end_m, end_fc in ((self.section_line[0], fc_a),
                              (self.section_line[1], fc_b)):
            ex1, ey1 = m2c(*end_m); ex2, ey2 = m2c(*end_fc)
            tk = WLine()
            tk.X1 = ex1; tk.Y1 = ey1; tk.X2 = ex2; tk.Y2 = ey2
            tk.Stroke = wpf_brush("#CBD5E0")
            tk.StrokeThickness = 0.6 * inv_s
            tk.StrokeDashArray = self._dash_array([2, 2])
            tk.IsHitTestVisible = False
            self.cnv_plan.Children.Add(tk)

        # ---------- Draggable handle at the midpoint of the far clip line ----------
        # Perpendicular direction in canvas-LOCAL coords (post-Grid-scale,
        # but ratios are scale-invariant so the arrow direction is correct).
        sx_a, sy_a = m2c(*self.section_line[0])
        sx_b, sy_b = m2c(*self.section_line[1])
        sdx = sx_b - sx_a; sdy = sy_b - sy_a
        sL  = math.hypot(sdx, sdy) or 1e-9
        sux, suy = sdx / sL, sdy / sL
        snx, sny = -suy, sux                  # canvas-local perpendicular
        if self.section_flip:
            snx, sny = -snx, -sny

        hx, hy = self._far_clip_handle_screen(m2c)

        handle_r = self._FC_HANDLE_R * inv_s   # constant ~11px on screen

        # Background circle
        handle = WEllipse()
        handle.Width = 2 * handle_r; handle.Height = 2 * handle_r
        handle.Fill = Brushes.White
        handle.Stroke = wpf_brush("#4A5568")
        handle.StrokeThickness = 1.4 * inv_s
        handle.Tag = "far_clip_handle"
        handle.Cursor = Cursors.SizeAll
        handle.ToolTip = "Drag to change the far clip distance"
        Canvas.SetLeft(handle, hx - handle_r); Canvas.SetTop(handle, hy - handle_r)
        self.cnv_plan.Children.Add(handle)

        # Two opposing arrows inside, perpendicular to the section line
        arrow_len = handle_r - 2 * inv_s
        head_w    = 3.0 * inv_s
        for sign in (+1, -1):
            tip   = (hx + snx * arrow_len * sign,
                     hy + sny * arrow_len * sign)
            base_c = (hx + snx * (arrow_len - 4 * inv_s) * sign,
                      hy + sny * (arrow_len - 4 * inv_s) * sign)
            base1 = (base_c[0] + sux * head_w, base_c[1] + suy * head_w)
            base2 = (base_c[0] - sux * head_w, base_c[1] - suy * head_w)
            tri = WPolygon()
            tpts = PointCollection()
            tpts.Add(WPoint(tip[0], tip[1]))
            tpts.Add(WPoint(base1[0], base1[1]))
            tpts.Add(WPoint(base2[0], base2[1]))
            tri.Points = tpts
            tri.Fill = wpf_brush("#4A5568")
            tri.IsHitTestVisible = False
            self.cnv_plan.Children.Add(tri)


    # Bubble + flip icon geometry (constants used by both draw + hit-test)
    _BUBBLE_R     = 18    # bubble radius (px)
    _FLIP_OFFSET  = 22    # distance from bubble center to flip icon center
    _FLIP_R       = 9     # flip icon radius

    def _section_line_screen_geom(self, m2c):
        """Return ((ax,ay),(bx,by), ux, uy, nx, ny) for the section line in
        screen coords. Used by both drawing and hit testing so the flip icon
        position is identical."""
        a, b = self.section_line
        ax, ay = m2c(*a); bx, by = m2c(*b)
        dx = bx - ax; dy = by - ay
        L  = math.hypot(dx, dy) or 1e-9
        ux, uy = dx / L, dy / L
        # "Right" perpendicular (view direction). Flip negates to look from
        # the other side. Same direction at both ends so triangles agree.
        nx, ny = -uy, ux
        if self.section_flip:
            nx, ny = -nx, -ny
        return (ax, ay), (bx, by), ux, uy, nx, ny

    def _draw_plan_section_line(self, m2c, inv_s):
        (ax, ay), (bx, by), ux, uy, nx, ny = self._section_line_screen_geom(m2c)

        # Section line itself - dash-dot, Revit-ish.
        # Stroke multiplied by inv_s; dash values are unitless multiples of
        # stroke (WPF math) so they stay constant.
        sl = WLine()
        sl.X1 = ax; sl.Y1 = ay; sl.X2 = bx; sl.Y2 = by
        sl.Stroke = wpf_brush(COLOR_SECTION_LINE)
        sl.StrokeThickness = 2.0 * inv_s
        sl.StrokeDashArray = self._dash_array([8, 3, 1, 3])
        sl.Tag = "section_body"
        sl.Cursor = Cursors.SizeAll
        self.cnv_plan.Children.Add(sl)

        R = self._BUBBLE_R * inv_s             # constant ~18px on screen
        for label, tag, (px, py) in (("A", "section_a", (ax, ay)),
                                     ("B", "section_b", (bx, by))):
            # Bubble
            bubble = WEllipse()
            bubble.Width = 2 * R; bubble.Height = 2 * R
            bubble.Fill = Brushes.White
            bubble.Stroke = wpf_brush(COLOR_SECTION_LINE)
            bubble.StrokeThickness = 2.2 * inv_s
            bubble.Tag = tag
            bubble.Cursor = Cursors.SizeAll
            Canvas.SetLeft(bubble, px - R); Canvas.SetTop(bubble, py - R)
            self.cnv_plan.Children.Add(bubble)
            # Letter — FontSize is in canvas-local DIPs, the Grid then
            # scales it up to ~14pt on screen.
            tb = TextBlock()
            tb.Text = label
            tb.Foreground = wpf_brush("#742A2A")
            tb.FontWeight = System.Windows.FontWeights.Bold
            tb.FontSize = 14 * inv_s
            tb.IsHitTestVisible = False
            tb.Measure(System.Windows.Size(System.Double.PositiveInfinity,
                                           System.Double.PositiveInfinity))
            tw = tb.DesiredSize.Width; th = tb.DesiredSize.Height
            Canvas.SetLeft(tb, px - tw / 2.0); Canvas.SetTop(tb, py - th / 2.0)
            self.cnv_plan.Children.Add(tb)
            # External view-direction triangle — reinforces direction at a
            # glance from a distance.
            tip_x = px + nx * (R + 8 * inv_s); tip_y = py + ny * (R + 8 * inv_s)
            base1_x = px + nx * R + ux * 5 * inv_s
            base1_y = py + ny * R + uy * 5 * inv_s
            base2_x = px + nx * R - ux * 5 * inv_s
            base2_y = py + ny * R - uy * 5 * inv_s
            tri = WPolygon()
            tpts = PointCollection()
            tpts.Add(WPoint(tip_x, tip_y))
            tpts.Add(WPoint(base1_x, base1_y))
            tpts.Add(WPoint(base2_x, base2_y))
            tri.Points = tpts
            tri.Fill = wpf_brush(COLOR_SECTION_LINE)
            tri.IsHitTestVisible = False
            self.cnv_plan.Children.Add(tri)

        # Inline flip icon next to bubble A (Revit-style swap toggle).
        # Position perpendicular to the line on the "behind" side so it
        # doesn't collide with the view-direction triangle.
        self._draw_flip_icon(ax, ay, ux, uy, nx, ny, inv_s)

    def _draw_flip_icon(self, ax, ay, ux, uy, nx, ny, inv_s):
        fx, fy = self._flip_icon_center((ax, ay), nx, ny, inv_s)
        size = self._FLIP_R * inv_s

        # Background circle (white with red ring) - the click target
        circle = WEllipse()
        circle.Width = 2 * size; circle.Height = 2 * size
        circle.Fill = Brushes.White
        circle.Stroke = wpf_brush(COLOR_SECTION_LINE)
        circle.StrokeThickness = 1.4 * inv_s
        circle.Tag = "flip_icon"
        circle.Cursor = Cursors.Hand
        circle.ToolTip = "Flip section view direction"
        Canvas.SetLeft(circle, fx - size); Canvas.SetTop(circle, fy - size)
        self.cnv_plan.Children.Add(circle)

        # Two opposing arrows inside (←  →) drawn ALONG the section direction
        # so they read "swap" regardless of section orientation.
        head = 4.0 * inv_s     # arrow head half-width
        tail = 5.5 * inv_s     # arrow length from center
        base = 1.5 * inv_s     # arrow base offset from center
        # Right arrow: tip at (fx + ux*tail, fy + uy*tail), base at (fx + ux*base, fy + uy*base)
        right = WPolygon()
        rpts = PointCollection()
        rpts.Add(WPoint(fx + ux * tail, fy + uy * tail))
        rpts.Add(WPoint(fx + ux * base + nx * head, fy + uy * base + ny * head))
        rpts.Add(WPoint(fx + ux * base - nx * head, fy + uy * base - ny * head))
        right.Points = rpts
        right.Fill = wpf_brush(COLOR_SECTION_LINE)
        right.IsHitTestVisible = False
        self.cnv_plan.Children.Add(right)
        # Left arrow (mirror)
        left = WPolygon()
        lpts = PointCollection()
        lpts.Add(WPoint(fx - ux * tail, fy - uy * tail))
        lpts.Add(WPoint(fx - ux * base + nx * head, fy - uy * base + ny * head))
        lpts.Add(WPoint(fx - ux * base - nx * head, fy - uy * base - ny * head))
        left.Points = lpts
        left.Fill = wpf_brush(COLOR_SECTION_LINE)
        left.IsHitTestVisible = False
        self.cnv_plan.Children.Add(left)

    def _flip_icon_center(self, bubble_screen, nx, ny, inv_s):
        """Position the flip icon perpendicular to the section line, on the
        OPPOSITE side from the view-direction triangle (so they don't
        overlap visually). The user clicks to swap which side the section
        looks from. Distance is scaled by inv_s so the icon stays a
        constant pixel offset from the bubble on screen."""
        bx, by = bubble_screen
        # Place the icon on the "back" side (-nx, -ny) so the front side
        # stays clear for the view-direction arrows.
        d = (self._BUBBLE_R + self._FLIP_OFFSET) * inv_s
        return (bx - nx * d, by - ny * d)

    def _dash_array(self, pattern):
        from System.Windows.Media import DoubleCollection
        dc = DoubleCollection()
        for v in pattern: dc.Add(float(v))
        return dc

    # --- Plan drag handlers --------------------------------------------------

    def _plan_hit(self, pos):
        """Hit-test against the flip icon, far clip handle, section
        endpoints, or section body. Returns 'flip_icon' / 'far_clip_handle'
        / 'endpoint_a' / 'endpoint_b' / 'body' / None.

        pos is in canvas-LOCAL coords (e.GetPosition(self.cnv_plan), with
        WPF inverting the parent Grid's transform automatically). All
        target radii are multiplied by inv_s so the hit area matches the
        bubbles' on-screen sizes — the user clicks where they SEE the
        bubble, regardless of zoom."""
        m2c, _c2m, _cw, _ch, _s = self._plan_transform()
        inv_s = self._current_plan_inv_scale()
        (ax, ay), (bx, by), ux, uy, nx, ny = self._section_line_screen_geom(m2c)
        # Flip icon (small target - prioritize)
        fx, fy = self._flip_icon_center((ax, ay), nx, ny, inv_s)
        flip_r = self._FLIP_R * inv_s
        if (pos.X - fx) ** 2 + (pos.Y - fy) ** 2 <= flip_r * flip_r:
            return 'flip_icon'
        # Far clip handle
        fc = self._far_clip_handle_screen(m2c)
        if fc is not None:
            fc_r = self._FC_HANDLE_R * inv_s
            if (pos.X - fc[0]) ** 2 + (pos.Y - fc[1]) ** 2 <= fc_r * fc_r:
                return 'far_clip_handle'
        # Endpoints — hit radius is the visible bubble radius PLUS slack,
        # so a click that lands a few pixels outside the drawn ring still
        # registers. Without slack, near-miss clicks would fall into the
        # empty-space-pan code path and feel like the bubble broke.
        R = (self._BUBBLE_R + self._BUBBLE_HIT_SLACK) * inv_s
        if (pos.X - ax) ** 2 + (pos.Y - ay) ** 2 <= R * R:
            return 'endpoint_a'
        if (pos.X - bx) ** 2 + (pos.Y - by) ** 2 <= R * R:
            return 'endpoint_b'
        # Body — generous tolerance so the user doesn't have to be
        # pixel-perfect (the line is dashed and only ~2 px thick; without
        # slack a click that visually lands on the line can miss).
        body_r = (14.0 + self._BODY_HIT_SLACK) * inv_s
        dx = bx - ax; dy = by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-6: return None
        t = ((pos.X - ax) * dx + (pos.Y - ay) * dy) / L2
        if t < 0 or t > 1: return None
        cx = ax + t * dx; cy = ay + t * dy
        if (pos.X - cx) ** 2 + (pos.Y - cy) ** 2 <= body_r * body_r:
            return 'body'
        return None

    def _plan_mouse_down(self, sender, e):
        pos = e.GetPosition(self.cnv_plan)
        kind = self._plan_hit(pos)
        if kind is None:
            # Empty space → record a PENDING pan, but don't engage yet.
            # Pan only starts after the cursor moves more than
            # _PAN_THRESHOLD_PX while the button is held (see
            # _plan_mouse_move). Without this threshold, a near-miss
            # click on a bubble would feel like the bubble silently
            # broke — the click would fall into this branch, mouse
            # capture would engage, and any further movement would pan
            # the canvas instead of the bubble. The threshold makes a
            # release-without-drag a no-op so the user can simply re-aim
            # and click again.
            pos_w = e.GetPosition(self)
            self._plan_pan_pending = {
                'start_x_w': pos_w.X, 'start_y_w': pos_w.Y,
            }
            try: e.Handled = True
            except Exception: pass
            return
        # Click-to-flip - no drag setup needed
        if kind == 'flip_icon':
            self.section_flip = not self.section_flip
            self._draw_plan()
            self._draw_section()
            return
        m2c, c2m, _cw, _ch, _s = self._plan_transform()
        mx, my = c2m(pos.X, pos.Y)
        self._plan_drag = {
            'kind': kind,
            'mouse_start_model': (mx, my),
            'line_start': [tuple(self.section_line[0]), tuple(self.section_line[1])],
            'far_clip_start': self.far_clip_offset,
        }
        try: self.cnv_plan.CaptureMouse()
        except Exception: pass

    def _begin_plan_pan(self, e, button):
        """Start a pan drag on the plan. `button` is 'left' or 'middle';
        we track which so the matching mouse-up actually ends it."""
        pos_w = e.GetPosition(self)
        try:
            init_tx = float(self.trf_plan_pan.X)
            init_ty = float(self.trf_plan_pan.Y)
        except Exception:
            init_tx = init_ty = 0.0
        self._plan_pan_drag = {
            'start_x': pos_w.X, 'start_y': pos_w.Y,
            'init_tx': init_tx, 'init_ty': init_ty,
            'button':  button,
        }
        try: self.cnv_plan.CaptureMouse()
        except Exception: pass
        try: self.cnv_plan.Cursor = Cursors.SizeAll
        except Exception: pass

    def _plan_mouse_move(self, sender, e):
        # Promote a PENDING left-button pan to an ACTIVE pan once the
        # cursor crosses the movement threshold. This is what stops
        # near-miss bubble clicks from instantly engaging a pan.
        if self._plan_pan_pending is not None:
            if e.LeftButton != MouseButtonState.Pressed:
                # User released without dragging far — discard the
                # pending pan, do nothing.
                self._plan_pan_pending = None
                return
            pos_w = e.GetPosition(self)
            dx = pos_w.X - self._plan_pan_pending['start_x_w']
            dy = pos_w.Y - self._plan_pan_pending['start_y_w']
            if dx * dx + dy * dy < self._PAN_THRESHOLD_PX_SQ:
                return  # below threshold — keep waiting
            # Threshold crossed: promote to a real pan. Carry over the
            # ORIGINAL window-coord click position as the pan anchor so
            # the move that just promoted the pan still applies cleanly
            # (no jump on the first frame).
            try:
                init_tx = float(self.trf_plan_pan.X)
                init_ty = float(self.trf_plan_pan.Y)
            except Exception:
                init_tx = init_ty = 0.0
            self._plan_pan_drag = {
                'start_x': self._plan_pan_pending['start_x_w'],
                'start_y': self._plan_pan_pending['start_y_w'],
                'init_tx': init_tx, 'init_ty': init_ty,
                'button':  'left',
            }
            self._plan_pan_pending = None
            try: self.cnv_plan.CaptureMouse()
            except Exception: pass
            try: self.cnv_plan.Cursor = Cursors.SizeAll
            except Exception: pass
            # Fall through to the active-pan branch below to apply
            # this move's translation immediately.

        # Pan in progress takes priority over the section/bubble/far-clip drag
        if self._plan_pan_drag is not None:
            btn = self._plan_pan_drag.get('button', 'middle')
            still_down = (e.LeftButton   == MouseButtonState.Pressed) if btn == 'left' \
                    else (e.MiddleButton == MouseButtonState.Pressed)
            if not still_down:
                self._end_plan_pan()
                return
            pos = e.GetPosition(self)
            dx = pos.X - self._plan_pan_drag['start_x']
            dy = pos.Y - self._plan_pan_drag['start_y']
            self.trf_plan_pan.X = self._plan_pan_drag['init_tx'] + dx
            self.trf_plan_pan.Y = self._plan_pan_drag['init_ty'] + dy
            # Redraw overlay so section line / bubbles follow the image
            self._draw_plan()
            return
        if self._plan_drag is None:
            # Hover cursor: change shape based on what's under the mouse so
            # the user gets immediate feedback that the section line, its
            # endpoints, the flip icon, and the far-clip handle are all
            # grabbable. Without this, the user has to click-and-hope.
            try:
                hover_pos = e.GetPosition(self.cnv_plan)
                hover_kind = self._plan_hit(hover_pos)
            except Exception:
                hover_kind = None
            if hover_kind in ('endpoint_a', 'endpoint_b', 'body'):
                self.cnv_plan.Cursor = Cursors.SizeAll
            elif hover_kind == 'flip_icon':
                self.cnv_plan.Cursor = Cursors.Hand
            elif hover_kind == 'far_clip_handle':
                self.cnv_plan.Cursor = Cursors.SizeAll
            else:
                # Empty space: still grabbable (left-drag pans).
                self.cnv_plan.Cursor = Cursors.SizeAll
            return
        if e.LeftButton != MouseButtonState.Pressed:
            self._plan_drag = None
            return
        pos = e.GetPosition(self.cnv_plan)
        _m2c, c2m, _cw, _ch, _s = self._plan_transform()
        mx, my = c2m(pos.X, pos.Y)
        kind = self._plan_drag['kind']
        if kind == 'endpoint_a':
            anchor = self._plan_drag['line_start'][1]
            new_a  = self._snap_orthogonal_image(anchor, (mx, my))
            self.section_line = [new_a, anchor]
        elif kind == 'endpoint_b':
            anchor = self._plan_drag['line_start'][0]
            new_b  = self._snap_orthogonal_image(anchor, (mx, my))
            self.section_line = [anchor, new_b]
        elif kind == 'body':
            sx, sy = self._plan_drag['mouse_start_model']
            dx = mx - sx; dy = my - sy
            (a0, a1), (b0, b1) = self._plan_drag['line_start']
            self.section_line = [(a0 + dx, a1 + dy), (b0 + dx, b1 + dy)]
        elif kind == 'far_clip_handle':
            # Compute signed perpendicular distance from section line to mouse
            a, _b, _ux, _uy, nx, ny, _L = self._section_view_basis()
            d = (mx - a[0]) * nx + (my - a[1]) * ny
            d = max(0.0, d)
            d = self._snap_value(d)
            self.far_clip_offset = d
        self._draw_plan()
        self._draw_section()

    # ------------------------------------------------------------------
    # Plan zoom (mouse wheel) and pan (middle-click drag)
    # The transform applies to the entire Grid containing both the image
    # AND the canvas, so the section line / bubbles / far-clip stay
    # anchored to the building when the user zooms or pans.
    # ------------------------------------------------------------------

    def _plan_mouse_wheel(self, sender, e):
        try:
            cur_scale = float(self.trf_plan_scale.ScaleX)
        except Exception:
            cur_scale = 1.0
        factor = 1.10 if e.Delta > 0 else 1.0 / 1.10
        new_scale = max(0.2, min(20.0, cur_scale * factor))
        if abs(new_scale - cur_scale) < 1e-9:
            e.Handled = True
            return
        # Anchor the zoom on the cursor: keep the LOCAL POINT under the
        # cursor visually fixed in screen space.
        #
        # The Grid transform is:  screen = local * scale + pan
        # GetPosition(self.cnv_plan) returns the LOCAL position (WPF
        # inverts the parent Grid transform automatically). We want the
        # screen position of that local point to be unchanged after the
        # scale change:
        #     local * scale_old + pan_old = local * scale_new + pan_new
        #     pan_new = pan_old - local * (scale_new - scale_old)
        pos = e.GetPosition(self.cnv_plan)
        cx, cy = pos.X, pos.Y
        try:
            tx = float(self.trf_plan_pan.X); ty = float(self.trf_plan_pan.Y)
        except Exception:
            tx = ty = 0.0
        ds = new_scale - cur_scale
        self.trf_plan_pan.X        = tx - cx * ds
        self.trf_plan_pan.Y        = ty - cy * ds
        self.trf_plan_scale.ScaleX = new_scale
        self.trf_plan_scale.ScaleY = new_scale
        # The section line / bubbles are drawn at the same canvas-local
        # coords (they use model coords through m2c, which is independent
        # of the Grid transform). The Grid scales them visually with the
        # building, then _draw_plan inverse-scales their sizes so they
        # appear constant on screen - just like Revit.
        self._draw_plan()
        e.Handled = True

    def _plan_mouse_down_any(self, sender, e):
        # Middle-click also starts a pan. Left-button-on-empty-space is
        # the primary pan path (handled in _plan_mouse_down) — middle
        # stays here for users who prefer the Revit-native gesture.
        if e.ChangedButton != MouseButton.Middle:
            return
        self._begin_plan_pan(e, button='middle')
        e.Handled = True

    def _plan_mouse_up_any(self, sender, e):
        if self._plan_pan_drag is not None \
                and e.ChangedButton == MouseButton.Middle \
                and self._plan_pan_drag.get('button') == 'middle':
            self._end_plan_pan()
            e.Handled = True

    def _end_plan_pan(self):
        self._plan_pan_drag = None
        try: self.cnv_plan.ReleaseMouseCapture()
        except Exception: pass
        try: self.cnv_plan.Cursor = Cursors.Arrow
        except Exception: pass

    def _on_reset_plan_zoom(self, s, e):
        self.trf_plan_scale.ScaleX = 1.0
        self.trf_plan_scale.ScaleY = 1.0
        self.trf_plan_pan.X = 0.0
        self.trf_plan_pan.Y = 0.0
        self._draw_plan()

    def _plan_mouse_up(self, sender, e):
        # Pending pan that never crossed the threshold → just discard.
        # This is the path that makes a click-without-drag on empty
        # space a no-op, instead of locking into a pan.
        if self._plan_pan_pending is not None:
            self._plan_pan_pending = None
            return
        # End a left-button pan IF the button was actually released.
        # MouseLeave is wired to this handler too, but it can fire while
        # the button is still held — we must not end the pan in that
        # case or the user's drag breaks the moment the cursor crosses
        # a sub-element edge.
        if self._plan_pan_drag is not None \
                and self._plan_pan_drag.get('button') == 'left':
            try:
                still_pressed = (e.LeftButton == MouseButtonState.Pressed)
            except Exception:
                still_pressed = False
            if not still_pressed:
                self._end_plan_pan()
            return
        if self._plan_drag is not None:
            kind = self._plan_drag['kind']
            # Things that change what the section preview should show
            section_changed = kind in ('endpoint_a', 'endpoint_b', 'body',
                                       'far_clip_handle')
            self._plan_drag = None
            try: self.cnv_plan.ReleaseMouseCapture()
            except Exception: pass
            # In Revit-render mode, re-render now that the drag is done.
            # (We can't re-render during the drag - it takes too long.)
            if section_changed and self.use_revit_render:
                self._refresh_revit_render()

    # ======================================================================
    # SECTION canvas - drawing & dragging
    # ======================================================================

    def _section_z_range(self):
        """User's view extent (z_bot, z_top) — what the user wants to
        SEE. The section is RENDERED with this extent (Stretch.Uniform
        fits the bbox to the canvas), so top / bottom extent lines sit
        at the literal top and bottom of the rendered image."""
        return self.section_z_bot, self.section_z_top

    def _section_render_padding(self):
        """Horizontal padding in feet added to the section bbox.

        DATA ACCURACY: This is now ZERO on both sides. The section bbox
        spans EXACTLY the section line — what the user draws on the plan
        is exactly what's in the section, down to the inch. The empty
        space for level / plane labels is handled entirely by the white
        screen gutters on cnv_section_screen, which are decoupled from
        the model. Earlier versions used 5–20 ft of padding, which
        extended the bbox into the building when the line was placed
        near or just outside an exterior wall — that's why sections
        showed unexpected content."""
        return 0.0, 0.0

    def _section_view_basis(self):
        """Return (a, b, ux, uy, nx, ny, L) describing the section line in
        MODEL coords plus the view-direction perpendicular.

        nx, ny is the 'right-turn' from A->B (CW) in model coords. Walking
        from A to B, the section "looks" to your right - this is Revit's
        default section direction. For an A=south / B=north section line
        that means default view direction = east.

        _render_revit_section uses the same convention: BasisX = section_dir,
        BasisZ = BasisX x BasisY = (uy, -ux) = view direction (into model).
        Plan UI's nx, ny is exactly that view direction so the far-clip
        line and triangles in the plan match what's actually rendered."""
        a, b = self.section_line[0], self.section_line[1]
        dx = b[0] - a[0]; dy = b[1] - a[1]
        L = math.hypot(dx, dy) or 1e-9
        ux, uy = dx / L, dy / L
        nx, ny = uy, -ux                      # CW right-turn from A->B
        if self.section_flip:
            nx, ny = -nx, -ny
        return a, b, ux, uy, nx, ny, L

    def _draw_section(self):
        """The section view is always a Revit-rendered PNG behind the
        canvas. Here we draw two overlays:
          * cnv_section (inside the transformed Grid) — model-anchored
            elements (A/B chips, level lines, plane lines, extent
            handles). They scale + pan with the image when the user
            zooms / drags.
          * cnv_section_screen (NOT transformed) — screen-anchored
            elements (plane name + elevation labels). They stay glued
            to the right edge of the visible card so the user never
            loses Top / Cut Plane / Bottom / View Depth labels."""
        self.cnv_section.Children.Clear()
        try: self.cnv_section_screen.Children.Clear()
        except Exception: pass
        # Stash the inverse zoom so every overlay-drawing helper can
        # multiply its sizes by it. The Grid scales children visually,
        # so this gives us SVG-like constant pixel sizes.
        self._sec_inv_scale = self._current_section_inv_scale()
        # Keep the section image geometry in sync with the current
        # layout (extent, canvas size). The overlay and the image both
        # use _section_image_layout, so they stay aligned automatically.
        self._update_section_image_geometry()
        self._draw_section_overlays_only()

    # Reserved gutter widths (px on screen). Section content is clipped
    # to NEVER intrude here — guarantees room for level + plane labels.
    _SEC_LEFT_GUTTER  = 60.0
    _SEC_RIGHT_GUTTER = 110.0

    def _draw_section_overlays_only(self):
        """Two-layer overlay:
          * INNER canvas (cnv_section, transformed with the image): the
            dashed lines — levels, plane lines, extent lines + pill
            handles. They scale + pan with the section.
          * SCREEN overlay (cnv_section_screen, NOT transformed): white
            gutter strips on left + right, plus all label TEXT (level
            names, plane labels, extent elevation labels, A/B chips).

        The image is rendered with the user's current view extent and
        Stretch.Uniform-fit to the canvas. Top/bottom extent lines sit
        at the literal top and bottom of the rendered image. Levels
        and plane lines align with the building because they all
        derive their canvas Y from the same _section_image_layout
        math the image uses."""
        inv_s = self._sec_inv_scale
        ox, oy, dw, dh = self._section_image_layout()
        z_min, z_max = self._section_z_range()
        z_span = max(0.001, z_max - z_min)

        def y_at(z): return oy + (z_max - z) / z_span * dh

        # Current section Grid transform — used to project line Y to
        # screen Y for the screen-anchored labels.
        try:
            sec_scale = float(self.trf_sec_scale.ScaleX)
            sec_tx    = float(self.trf_sec_pan.X)
            sec_ty    = float(self.trf_sec_pan.Y)
        except Exception:
            sec_scale = 1.0; sec_tx = sec_ty = 0.0
        def screen_y_for(local_y):
            return local_y * sec_scale + sec_ty

        screen_w = max(1.0, float(self.cnv_section_screen.ActualWidth))
        screen_h = max(1.0, float(self.cnv_section_screen.ActualHeight))
        LG = self._SEC_LEFT_GUTTER
        RG = self._SEC_RIGHT_GUTTER

        # Canvas-local X range that the lines should span. We want the lines
        # to ALWAYS reach exactly to the inner edge of each gutter on screen,
        # regardless of zoom + pan. Solving screen_x = local_x * scale + tx:
        #   local_x = (screen_x - tx) / scale
        # So the line endpoints in canvas-local are:
        if sec_scale < 1e-6: sec_scale = 1.0
        line_x_left  = (LG               - sec_tx) / sec_scale
        line_x_right = (screen_w - RG    - sec_tx) / sec_scale
        # Stash for _sec_hit so the hit zone matches the visible line range.
        self._sec_line_x_left  = line_x_left
        self._sec_line_x_right = line_x_right

        # ============================================================
        # SCREEN OVERLAY — gutters first (drawn UNDER the labels)
        # ============================================================
        # Each gutter rect over-covers its outer edge by 4px so any subpixel
        # render bleed at the canvas / border boundary is hidden.
        gl = WRect()
        gl.Width = LG + 4; gl.Height = screen_h
        gl.Fill = Brushes.White
        Canvas.SetLeft(gl, -4); Canvas.SetTop(gl, 0)
        self.cnv_section_screen.Children.Add(gl)
        # Thin separator so the gutter feels intentional, not like dead space
        gl_sep = WLine()
        gl_sep.X1 = LG; gl_sep.X2 = LG; gl_sep.Y1 = 0; gl_sep.Y2 = screen_h
        gl_sep.Stroke = wpf_brush("#E2E8F0"); gl_sep.StrokeThickness = 1.0
        self.cnv_section_screen.Children.Add(gl_sep)
        # Right gutter
        gr = WRect()
        gr.Width = RG + 4; gr.Height = screen_h
        gr.Fill = Brushes.White
        Canvas.SetLeft(gr, screen_w - RG); Canvas.SetTop(gr, 0)
        self.cnv_section_screen.Children.Add(gr)
        gr_sep = WLine()
        gr_sep.X1 = screen_w - RG; gr_sep.X2 = screen_w - RG
        gr_sep.Y1 = 0;             gr_sep.Y2 = screen_h
        gr_sep.Stroke = wpf_brush("#E2E8F0"); gr_sep.StrokeThickness = 1.0
        self.cnv_section_screen.Children.Add(gr_sep)

        # ============================================================
        # INNER CANVAS — dashed lines
        # ============================================================
        # Drawing order: levels → planes → extent lines + pills.
        # The image is rendered with the user's current extent, so
        # everything visible in the image lies within [z_bot, z_top].

        # 1. Levels (gray dashed) — span from gutter to gutter on screen
        for lvl in self.all_levels:
            if not (z_min <= lvl.Elevation <= z_max):
                continue
            yz = y_at(lvl.Elevation)
            ln = WLine()
            ln.X1 = line_x_left; ln.X2 = line_x_right
            ln.Y1 = yz; ln.Y2 = yz
            ln.Stroke = wpf_brush("#A0AEC0")
            ln.StrokeThickness = 0.6 * inv_s
            ln.StrokeDashArray = self._dash_array([3, 3])
            ln.IsHitTestVisible = False
            self.cnv_section.Children.Add(ln)

        # 2. View range plane lines (colored dashed)
        plane_visuals = (
            ("top", COLOR_TOP, "Top"),
            ("cut", COLOR_CUT, "Cut Plane"),
            ("bot", COLOR_BOT, "Bottom"),
            ("vd",  COLOR_VD,  "View Depth"),
        )
        for key, color, label in plane_visuals:
            if key in self.disabled_planes:
                continue
            z = self._abs_z(key)
            if z is None:
                continue
            yz = y_at(z)
            ln = WLine()
            ln.X1 = line_x_left; ln.X2 = line_x_right
            ln.Y1 = yz; ln.Y2 = yz
            ln.Stroke = wpf_brush(color)
            ln.StrokeThickness = (3.0 if key == "cut" else 2.0) * inv_s
            ln.StrokeDashArray = self._dash_array([6, 4])
            ln.IsHitTestVisible = False     # hit-test handled by _sec_hit
            self.cnv_section.Children.Add(ln)

        # 3. Extent dashed lines + right-edge pill handles. They sit
        # at the literal top and bottom of the rendered image (since
        # the bbox covers exactly the user's extent). Hit-tested
        # anywhere along the line (see _sec_hit).
        #
        # During an extent drag, the dragged line uses the PREVIEW
        # value (cursor position) so it follows the mouse smoothly,
        # while the OTHER line stays at its committed value. Levels,
        # planes, and the image stay anchored to the committed extent
        # because z_top/_bot only change on release (then we re-render).
        preview_key = self._ext_preview['key'] if self._ext_preview else None
        preview_z   = self._ext_preview['z']   if self._ext_preview else None
        ext_z = {
            "extent_top": self.section_z_top,
            "extent_bot": self.section_z_bot,
        }
        if preview_key in ext_z:
            ext_z[preview_key] = preview_z
        for ext_key in ("extent_top", "extent_bot"):
            yz = y_at(ext_z[ext_key])
            ln = WLine()
            ln.X1 = line_x_left; ln.X2 = line_x_right
            ln.Y1 = yz; ln.Y2 = yz
            ln.Stroke = wpf_brush("#4A5568")
            ln.StrokeThickness = 1.4 * inv_s
            ln.StrokeDashArray = self._dash_array([6, 4])
            ln.IsHitTestVisible = False
            self.cnv_section.Children.Add(ln)
            # Pill handle anchored at the inner edge of the right gutter.
            HANDLE_W = 22 * inv_s
            HANDLE_H = 22 * inv_s
            tri_x = line_x_right - HANDLE_W / 2.0 - 2 * inv_s
            bg = WEllipse()
            bg.Width = HANDLE_W; bg.Height = HANDLE_H
            bg.Fill = Brushes.White
            bg.Stroke = wpf_brush("#2D3748")
            bg.StrokeThickness = 1.6 * inv_s
            bg.IsHitTestVisible = False
            Canvas.SetLeft(bg, tri_x - HANDLE_W / 2.0)
            Canvas.SetTop(bg,  yz - HANDLE_H / 2.0)
            self.cnv_section.Children.Add(bg)
            for sign in (+1, -1):
                arr = WPolygon()
                apts = PointCollection()
                apts.Add(WPoint(tri_x,             yz + sign * 7 * inv_s))
                apts.Add(WPoint(tri_x - 4 * inv_s, yz + sign * 1 * inv_s))
                apts.Add(WPoint(tri_x + 4 * inv_s, yz + sign * 1 * inv_s))
                arr.Points = apts
                arr.Fill = wpf_brush("#2D3748")
                arr.IsHitTestVisible = False
                self.cnv_section.Children.Add(arr)

        # ============================================================
        # SCREEN OVERLAY — labels (drawn ON the gutters)
        # ============================================================
        # A / B chips at the top corners of the visible content area.
        # Empirically Revit puts section_line[0] (= "A" in the plan) on
        # the RIGHT of the rendered image when not flipped.
        if self.section_flip:
            label_left, label_right = "A", "B"
        else:
            label_left, label_right = "B", "A"
        chip_r = 11
        chip_y = 4 + chip_r
        for label, cx in ((label_left,  LG + 4 + chip_r),
                          (label_right, screen_w - RG - 4 - chip_r)):
            chip = WEllipse()
            chip.Width = 2 * chip_r; chip.Height = 2 * chip_r
            chip.Fill = Brushes.White
            chip.Stroke = wpf_brush(COLOR_SECTION_LINE)
            chip.StrokeThickness = 1.6
            Canvas.SetLeft(chip, cx - chip_r); Canvas.SetTop(chip, chip_y - chip_r)
            self.cnv_section_screen.Children.Add(chip)
            tb = TextBlock()
            tb.Text = label
            tb.Foreground = wpf_brush("#742A2A")
            tb.FontWeight = System.Windows.FontWeights.Bold
            tb.FontSize = 11
            tb.Measure(System.Windows.Size(System.Double.PositiveInfinity,
                                           System.Double.PositiveInfinity))
            tw = tb.DesiredSize.Width; th = tb.DesiredSize.Height
            Canvas.SetLeft(tb, cx - tw / 2.0); Canvas.SetTop(tb, chip_y - th / 2.0)
            self.cnv_section_screen.Children.Add(tb)

        # Level labels in the LEFT gutter — two lines: name on top
        # (bigger, semi-bold), elevation below (smaller, lighter).
        # Vertically tracked to each level line. Filtered to the USER's
        # view extent so labels for masked levels don't clutter the
        # gutter.
        ut_top = self.section_z_top
        ut_bot = self.section_z_bot
        for lvl in self.all_levels:
            if not (ut_bot <= lvl.Elevation <= ut_top):
                continue
            sy = screen_y_for(y_at(lvl.Elevation))
            sp = StackPanel()
            sp.Orientation = Orientation.Vertical
            name_tb = TextBlock()
            name_tb.Text = lvl.Name
            name_tb.FontSize = 12
            name_tb.FontWeight = System.Windows.FontWeights.SemiBold
            name_tb.Foreground = wpf_brush("#2D3748")
            sp.Children.Add(name_tb)
            elev_tb = TextBlock()
            elev_tb.Text = fmt_feet_in(lvl.Elevation)
            elev_tb.FontSize = 10
            elev_tb.Foreground = wpf_brush("#718096")
            sp.Children.Add(elev_tb)
            sp.Measure(System.Windows.Size(LG - 6,
                                           System.Double.PositiveInfinity))
            sh = sp.DesiredSize.Height if sp.DesiredSize.Height > 0 else 28
            # Anchor by the level line: name sits above the line, elevation below
            sy_top = sy - sh / 2.0
            if sy_top + sh < 0 or sy_top > screen_h:
                continue
            Canvas.SetLeft(sp, 4); Canvas.SetTop(sp, sy_top)
            self.cnv_section_screen.Children.Add(sp)

        # Extent (Top / Bottom) elevation labels in the RIGHT gutter
        for ext_key, point_up in (("extent_top", True), ("extent_bot", False)):
            sy = screen_y_for(y_at(ext_z[ext_key]))
            etb = TextBlock()
            etb.Text = "{}  {}".format("Top" if point_up else "Bottom",
                                       fmt_feet_in(ext_z[ext_key]))
            etb.FontSize = 10
            etb.FontWeight = System.Windows.FontWeights.SemiBold
            etb.Foreground = wpf_brush("#2D3748")
            etb.Measure(System.Windows.Size(RG - 6,
                                            System.Double.PositiveInfinity))
            eth = etb.DesiredSize.Height if etb.DesiredSize.Height > 0 else 14
            ety = sy + (-eth - 2 if point_up else 2)
            ety = max(2, min(screen_h - eth - 2, ety))
            Canvas.SetLeft(etb, screen_w - RG + 4); Canvas.SetTop(etb, ety)
            self.cnv_section_screen.Children.Add(etb)

        # View range plane labels in the RIGHT gutter, glued to their lines
        unlimited_y = chip_y + chip_r + 6   # stack below the A/B chip
        for key, color, label in plane_visuals:
            if key in self.disabled_planes:
                continue
            z = self._abs_z(key)
            chip = Border()
            chip.Background = wpf_brush(color)
            chip.CornerRadius = System.Windows.CornerRadius(3)
            chip.Padding = Thickness(6, 2, 6, 2)
            ctb = TextBlock()
            if z is None:
                ctb.Text = "{}  Unlimited".format(label)
            else:
                ctb.Text = "{}  {}".format(label, fmt_feet_in(z))
            ctb.Foreground = Brushes.White
            ctb.FontSize = 11
            ctb.FontWeight = System.Windows.FontWeights.SemiBold
            chip.Child = ctb
            chip.Measure(System.Windows.Size(RG - 6,
                                             System.Double.PositiveInfinity))
            chip_w = chip.DesiredSize.Width  if chip.DesiredSize.Width  > 0 else RG - 6
            chip_h = chip.DesiredSize.Height if chip.DesiredSize.Height > 0 else 18
            chip_x = screen_w - RG + 4
            if z is None:
                cy_ = unlimited_y
                unlimited_y += chip_h + 3
            else:
                sy_ = screen_y_for(y_at(z))
                if key in ("top", "cut"):
                    cy_ = sy_ - chip_h - 2
                else:
                    cy_ = sy_ + 2
                cy_ = max(2.0, min(screen_h - chip_h - 2, cy_))
            Canvas.SetLeft(chip, chip_x); Canvas.SetTop(chip, cy_)
            self.cnv_section_screen.Children.Add(chip)

    # --- Section drag handlers ----------------------------------------------

    def _section_mappers(self):
        """Return (x_at, y_at, m_at_y) aligned to the rendered section
        image's actual display area. y_at / m_at_y derive from the
        same Stretch.Uniform layout the image uses, so overlay
        elements (level lines, plane lines, extent handles) stay
        pixel-aligned with what's visible in the bitmap."""
        ox, oy, dw, dh = self._section_image_layout()
        z_min, z_max = self._section_z_range()
        z_span = max(0.001, z_max - z_min)

        L = max(0.001, seg_length(self.section_line[0], self.section_line[1]))
        left_pad, right_pad = self._section_render_padding()
        total_w  = L + left_pad + right_pad
        left_frac = left_pad / total_w
        line_frac = L / total_w

        def x_at(t):
            return ox + (left_frac + t * line_frac) * dw
        def y_at(z):
            return oy + (z_max - z) / z_span * dh
        def m_at_y(py):
            return z_max - (py - oy) / max(dh, 0.001) * z_span
        return x_at, y_at, m_at_y

    def _sec_hit(self, pos):
        """Return what's under `pos`: a plane key ('top'/'cut'/'bot'/'vd'),
        an extent handle key ('extent_top'/'extent_bot'), or None.

        Hit tolerances are scaled by the inverse of the current section
        zoom so the user clicks where they SEE the line on screen,
        regardless of zoom. The X range matches the visible line range
        (gutter inner edges) so any click between gutters at the line's
        Y registers."""
        _x_at, y_at, _m_at_y = self._section_mappers()

        try:
            inv_s = self._sec_inv_scale
        except AttributeError:
            inv_s = self._current_section_inv_scale()
        # X range that the lines visually span (canvas-local, mapping to
        # the inner gutter edges in screen coords). Falls back to the full
        # image width if the cached values don't exist yet.
        try:
            x_left  = self._sec_line_x_left
            x_right = self._sec_line_x_right
        except AttributeError:
            ox, _oy, dw, _dh = self._section_image_layout()
            x_left  = ox
            x_right = ox + dw

        # Generous hit tolerance in screen pixels (12 px), converted to
        # canvas-local via inv_s so the click area always matches the
        # line's apparent thickness on screen.
        line_tol = 12.0 * inv_s
        pill_tol = 18.0 * inv_s

        # Extent handles. The chunky pill sits at the right gutter edge,
        # but the dashed line itself is hit-testable end-to-end too.
        for ext_key, z_val in (("extent_top", self.section_z_top),
                               ("extent_bot", self.section_z_bot)):
            yz = y_at(z_val)
            # Pill handle: ~36 px wide on screen near the right gutter edge
            if (x_right - 36 * inv_s) <= pos.X <= (x_right + 6 * inv_s) \
                    and abs(pos.Y - yz) <= pill_tol:
                return ext_key
            # Dashed line: anywhere between the gutter edges
            if x_left <= pos.X <= x_right and abs(pos.Y - yz) <= line_tol:
                return ext_key

        # View range planes — anywhere along the colored line is grabbable
        for key in PLANE_KEYS:
            if key in self.disabled_planes:
                continue
            z = self._abs_z(key)
            if z is None: continue
            yz = y_at(z)
            if x_left <= pos.X <= x_right and abs(pos.Y - yz) <= line_tol:
                return key
        return None

    def _sec_mouse_down(self, sender, e):
        pos = e.GetPosition(self.cnv_section)
        key = self._sec_hit(pos)
        if key is None:
            # Empty space → record a PENDING left-button pan, but don't
            # engage yet. Pan only starts after the cursor moves past
            # _PAN_THRESHOLD_PX (see _sec_mouse_move). Same rationale as
            # the plan canvas: stops near-miss clicks on plane / extent
            # handles from feeling like the handle broke.
            pos_w = e.GetPosition(self)
            self._sec_pan_pending = {
                'start_x_w': pos_w.X, 'start_y_w': pos_w.Y,
            }
            try: e.Handled = True
            except Exception: pass
            return
        # Extent handles - drag without view-range-plane state, no template lock
        if key in ('extent_top', 'extent_bot'):
            self._sec_drag = {'key': key}
            self._ext_preview = None      # set on first move
            try: self.cnv_section.CaptureMouse()
            except Exception: pass
            return
        # View range plane drag - blocked by template lock + per-view disabled
        if getattr(self, '_template_locked', False):
            return
        if key in self.disabled_planes:
            return
        self._sec_drag = {
            'key': key,
            'start_y': pos.Y,
            'start_offset': self.state[key]['offset'],
        }
        try: self.cnv_section.CaptureMouse()
        except Exception: pass

    def _begin_sec_pan(self, e, button):
        """Start a section pan drag. `button` is 'left' or 'middle';
        mouse-up checks this so the matching release ends it."""
        pos_w = e.GetPosition(self)
        try:
            init_tx = float(self.trf_sec_pan.X)
            init_ty = float(self.trf_sec_pan.Y)
        except Exception:
            init_tx = init_ty = 0.0
        self._sec_pan_drag = {
            'start_x': pos_w.X, 'start_y': pos_w.Y,
            'init_tx': init_tx, 'init_ty': init_ty,
            'button':  button,
        }
        try: self.cnv_section.CaptureMouse()
        except Exception: pass
        try: self.cnv_section.Cursor = Cursors.SizeAll
        except Exception: pass

    def _end_sec_pan(self):
        self._sec_pan_drag = None
        try: self.cnv_section.ReleaseMouseCapture()
        except Exception: pass
        try: self.cnv_section.Cursor = Cursors.Arrow
        except Exception: pass

    def _sec_mouse_move(self, sender, e):
        # Promote a PENDING left-button pan to an ACTIVE pan once the
        # cursor crosses the movement threshold. Mirrors the plan
        # canvas — see _plan_mouse_move for the rationale.
        if self._sec_pan_pending is not None:
            if e.LeftButton != MouseButtonState.Pressed:
                self._sec_pan_pending = None
                return
            pos_w = e.GetPosition(self)
            dx = pos_w.X - self._sec_pan_pending['start_x_w']
            dy = pos_w.Y - self._sec_pan_pending['start_y_w']
            if dx * dx + dy * dy < self._PAN_THRESHOLD_PX_SQ:
                return
            try:
                init_tx = float(self.trf_sec_pan.X)
                init_ty = float(self.trf_sec_pan.Y)
            except Exception:
                init_tx = init_ty = 0.0
            self._sec_pan_drag = {
                'start_x': self._sec_pan_pending['start_x_w'],
                'start_y': self._sec_pan_pending['start_y_w'],
                'init_tx': init_tx, 'init_ty': init_ty,
                'button':  'left',
            }
            self._sec_pan_pending = None
            try: self.cnv_section.CaptureMouse()
            except Exception: pass
            try: self.cnv_section.Cursor = Cursors.SizeAll
            except Exception: pass
            # Fall through to apply the first frame of pan immediately.

        # Pan in progress takes priority over plane / extent drags
        if self._sec_pan_drag is not None:
            btn = self._sec_pan_drag.get('button', 'middle')
            still_down = (e.LeftButton   == MouseButtonState.Pressed) if btn == 'left' \
                    else (e.MiddleButton == MouseButtonState.Pressed)
            if not still_down:
                self._end_sec_pan()
                return
            pos_w = e.GetPosition(self)
            dx = pos_w.X - self._sec_pan_drag['start_x']
            dy = pos_w.Y - self._sec_pan_drag['start_y']
            self.trf_sec_pan.X = self._sec_pan_drag['init_tx'] + dx
            self.trf_sec_pan.Y = self._sec_pan_drag['init_ty'] + dy
            self._draw_section()
            return
        # No active drag: just hover-update the cursor based on what's under it
        if self._sec_drag is None:
            self._update_section_hover_cursor(e)
            return
        if e.LeftButton != MouseButtonState.Pressed:
            self._sec_drag = None
            return
        pos = e.GetPosition(self.cnv_section)
        _x_at, _y_at, m_at_y = self._section_mappers()
        key = self._sec_drag['key']
        # ----- Extent handle drag: PREVIEW only — don't touch committed -----
        # Critical: during drag, we DO NOT change section_z_top/_bot.
        # If we did, _draw_section would compute level / plane / image
        # positions using the new z_span while the existing PNG is
        # still rendered for the OLD z_span — everything would shift
        # relative to the unchanged image. So we just stash the cursor
        # position in _ext_preview and only the dragged extent line
        # uses it; everything else keeps using committed values and
        # stays anchored to the image. Commit + re-render happens on
        # release.
        if key in ('extent_top', 'extent_bot'):
            z_new = m_at_y(pos.Y)
            z_new = self._snap_value(z_new)
            self._ext_preview = {'key': key, 'z': z_new}
            label_word = "top" if key == 'extent_top' else "bottom"
            self.txt_section_hint.Text = "Section {} → {} (release to re-render)".format(
                label_word, fmt_feet_in(z_new))
            self._draw_section()
            return
        # ----- View range plane drag -----
        # Convert the new mouse Y to absolute Z, then back into an offset
        z_new = m_at_y(pos.Y)
        s = self.state[key]
        ref_z = absolute_z_for_plane(self.view_plan, s['level_id'], 0.0, self.all_levels)
        if ref_z is None:
            return
        new_off = z_new - ref_z
        new_off = self._snap_value(new_off)
        s['offset'] = new_off
        self._suppress_editor_events = True
        try:
            mapping = {'top': self.txt_top_offset, 'cut': self.txt_cut_offset,
                       'bot': self.txt_bot_offset, 'vd': self.txt_vd_offset}
            mapping[key].Text = fmt_feet_in(new_off)
        finally:
            self._suppress_editor_events = False
        self._draw_section()
        self._update_status(dirty=True)
        self._validate_state()

    def _sec_mouse_up(self, sender, e):
        # Pending pan that never crossed the threshold → just discard.
        if self._sec_pan_pending is not None:
            self._sec_pan_pending = None
            return
        # End a left-button pan IF the button was actually released.
        # MouseLeave is wired to this same handler; we ignore those.
        if self._sec_pan_drag is not None \
                and self._sec_pan_drag.get('button') == 'left':
            try:
                still_pressed = (e.LeftButton == MouseButtonState.Pressed)
            except Exception:
                still_pressed = False
            if not still_pressed:
                self._end_sec_pan()
            return
        if self._sec_drag is None:
            return
        kind = self._sec_drag['key']
        self._sec_drag = None
        try: self.cnv_section.ReleaseMouseCapture()
        except Exception: pass
        # Extent drag: commit the preview value to section_z_top/_bot
        # NOW (we held the change off until release to keep the rest of
        # the overlay stable during drag), then re-render with the new
        # bbox so the image catches up.
        if kind in ('extent_top', 'extent_bot'):
            if self._ext_preview is not None:
                z_new = self._ext_preview['z']
                if kind == 'extent_top':
                    self.section_z_top = max(z_new, self.section_z_bot + 1.0)
                else:
                    self.section_z_bot = min(z_new, self.section_z_top - 1.0)
            self._ext_preview = None
            self._refresh_revit_render()
            return
        if kind in PLANE_KEYS:
            # View range plane drag committed: re-render the plan PNG
            # since the cut plane / top / bottom / view depth all affect
            # what's visible in the active plan view.
            self._refresh_revit_plan()

    # ------------------------------------------------------------------
    # Section zoom (mouse wheel) + pan (middle-click drag) -- mirrors
    # the plan: the RenderTransform lives on the parent Grid so the
    # image and the content overlay scale together; the screen-anchored
    # canvas (plane labels) sits OUTSIDE the Grid and stays glued to
    # the visible card edge.
    # ------------------------------------------------------------------

    def _current_section_inv_scale(self):
        """1 / current section Grid zoom — overlay sizes get multiplied
        by this so they stay constant on screen."""
        try:
            s = float(self.trf_sec_scale.ScaleX)
        except Exception:
            s = 1.0
        if s < 1e-6: s = 1.0
        return 1.0 / s

    def _sec_mouse_wheel(self, sender, e):
        try:
            cur_scale = float(self.trf_sec_scale.ScaleX)
        except Exception:
            cur_scale = 1.0
        factor = 1.10 if e.Delta > 0 else 1.0 / 1.10
        new_scale = max(0.2, min(20.0, cur_scale * factor))
        if abs(new_scale - cur_scale) < 1e-9:
            e.Handled = True
            return
        # Anchor the zoom on the cursor (canvas-LOCAL coords; WPF inverts
        # the parent Grid transform automatically).
        pos = e.GetPosition(self.cnv_section)
        cx, cy = pos.X, pos.Y
        try:
            tx = float(self.trf_sec_pan.X); ty = float(self.trf_sec_pan.Y)
        except Exception:
            tx = ty = 0.0
        ds = new_scale - cur_scale
        self.trf_sec_pan.X        = tx - cx * ds
        self.trf_sec_pan.Y        = ty - cy * ds
        self.trf_sec_scale.ScaleX = new_scale
        self.trf_sec_scale.ScaleY = new_scale
        self._draw_section()
        e.Handled = True

    def _sec_mouse_down_any(self, sender, e):
        # Middle-click also starts a pan. Left-button-on-empty-space is
        # the primary path (handled in _sec_mouse_down) — middle stays
        # here for users who prefer the Revit-native gesture.
        if e.ChangedButton != MouseButton.Middle:
            return
        self._begin_sec_pan(e, button='middle')
        e.Handled = True

    def _sec_mouse_up_any(self, sender, e):
        if self._sec_pan_drag is not None \
                and e.ChangedButton == MouseButton.Middle \
                and self._sec_pan_drag.get('button') == 'middle':
            self._end_sec_pan()
            e.Handled = True

    def _on_reset_section_zoom(self, s, e):
        self.trf_sec_scale.ScaleX = 1.0
        self.trf_sec_scale.ScaleY = 1.0
        self.trf_sec_pan.X = 0.0
        self.trf_sec_pan.Y = 0.0
        self._draw_section()

    def _update_section_hover_cursor(self, e):
        """Update cnv_section's cursor based on what's under the mouse —
        gives the user immediate feedback that a target is grabbable.
        Without this the user has to click-and-hope."""
        try:
            pos = e.GetPosition(self.cnv_section)
            kind = self._sec_hit(pos)
        except Exception:
            kind = None
        if kind in ('extent_top', 'extent_bot'):
            self.cnv_section.Cursor = Cursors.SizeNS
        elif kind in PLANE_KEYS:
            if kind in self.disabled_planes or getattr(self, '_template_locked', False):
                self.cnv_section.Cursor = Cursors.Arrow
            else:
                self.cnv_section.Cursor = Cursors.SizeNS
        else:
            # Empty space: still grabbable (left-drag pans).
            self.cnv_section.Cursor = Cursors.SizeAll


# ============================================================================
# Main
# ============================================================================

def main():
    av = doc.ActiveView
    if av is None:
        forms.alert("There is no active view.", exitscript=True)

    if not isinstance(av, ViewPlan):
        forms.alert(
            "View Range Helper only works on plan views (floor plans, ceiling "
            "plans, area plans, and engineering plans).\n\n"
            "The active view is: {}".format(str(av.ViewType)),
            title="View Range Helper", exitscript=True)

    try:
        win = ViewRangeHelperForm(av)
        win.ShowDialog()
    except Exception:
        output.print_md("### View Range Helper failed")
        output.print_md("```\n{}\n```".format(traceback.format_exc()))
        forms.alert("View Range Helper hit an error - see the pyRevit "
                    "output window for the traceback.", title="View Range Helper")


if __name__ == '__main__':
    main()
