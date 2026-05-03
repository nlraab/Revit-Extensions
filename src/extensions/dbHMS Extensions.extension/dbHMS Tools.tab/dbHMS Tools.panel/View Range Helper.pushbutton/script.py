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
    Transaction, TransactionGroup, ElementId, XYZ, Line as DBLine,
    PlanViewPlane, BoundingBoxXYZ, Transform,
    ImageExportOptions, ImageFileType, ImageResolution,
    ZoomFitType, ExportRange,
)

import System
from System import Uri, UriKind
from System.Windows import (
    Thickness, Visibility, Point as WPoint,
    MessageBox, MessageBoxButton, MessageBoxResult, MessageBoxImage,
)
from System.Windows.Controls import (
    ComboBoxItem, Canvas, TextBlock, Border,
)
from System.Windows.Media import (
    SolidColorBrush, Color, Brushes, PointCollection,
    PathGeometry, PathFigure, ArcSegment, LineSegment,
    SweepDirection,
)
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Windows.Shapes import (
    Line as WLine, Rectangle as WRect, Ellipse as WEllipse,
    Polygon as WPolygon, Path as WPath,
)
from System.Windows.Input import Cursors, MouseButtonState, Key

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


def snap_feet_to_6in(feet):
    """Snap a decimal-feet value to nearest 0.5 ft (= 6 inches)."""
    if feet is None:
        return None
    return round(feet * 2.0) / 2.0


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
    """Return ((min_x, min_y), (max_x, max_y)) of the view crop box in
    model XY (feet). Falls back to a generous box around all walls when
    the crop is not enabled."""
    try:
        if view_plan.CropBoxActive:
            cb = view_plan.CropBox  # BoundingBoxXYZ in view-relative coords
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


def collect_walls(view_plan, crop_min, crop_max):
    """Collect a lightweight list of walls with their plan footprint and
    vertical extent.

    Returns list of dicts:
        { 'p1': (x, y), 'p2': (x, y), 'z_bot': float, 'z_top': float,
          'thickness': float }

    Walls are filtered to those whose plan footprint intersects the crop
    bbox (or all walls if crop is None)."""
    walls = list(FilteredElementCollector(doc)
                 .OfCategory(BuiltInCategory.OST_Walls)
                 .WhereElementIsNotElementType())
    out = []
    for w in walls:
        try:
            loc = w.Location
            if loc is None or not hasattr(loc, "Curve"):
                continue
            curve = loc.Curve
            if not isinstance(curve, DBLine):
                # Skip arc walls for now - they complicate the section cut math
                continue
            p1 = curve.GetEndPoint(0); p2 = curve.GetEndPoint(1)
            # Vertical extent of the wall
            z_bot = None; z_top = None
            try:
                base_lvl = doc.GetElement(w.LevelId)
                base_off = w.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
                z_bot = (base_lvl.Elevation if base_lvl else 0.0) + (base_off.AsDouble() if base_off else 0.0)
            except Exception:
                pass
            try:
                # Top by constraint
                top_cnst_param = w.get_Parameter(BuiltInParameter.WALL_HEIGHT_TYPE)
                top_off_param  = w.get_Parameter(BuiltInParameter.WALL_TOP_OFFSET)
                top_cnst_id    = top_cnst_param.AsElementId() if top_cnst_param else None
                if top_cnst_id and eid_int(top_cnst_id) > 0:
                    top_lvl = doc.GetElement(top_cnst_id)
                    if top_lvl:
                        z_top = top_lvl.Elevation + (top_off_param.AsDouble() if top_off_param else 0.0)
                if z_top is None:
                    h_param = w.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                    if h_param and z_bot is not None:
                        z_top = z_bot + h_param.AsDouble()
            except Exception:
                pass
            if z_bot is None: z_bot = 0.0
            if z_top is None: z_top = z_bot + 10.0  # fallback - 10 ft tall

            # Wall thickness for the section render
            try:
                thk = w.Width
            except Exception:
                thk = 0.5

            # Crop filter: AABB-vs-segment quick reject
            if crop_min is not None and crop_max is not None:
                xs = (p1.X, p2.X); ys = (p1.Y, p2.Y)
                if (max(xs) < crop_min[0] - 5 or min(xs) > crop_max[0] + 5 or
                    max(ys) < crop_min[1] - 5 or min(ys) > crop_max[1] + 5):
                    continue

            out.append({
                "id": eid_int(w.Id),
                "p1": (p1.X, p1.Y),
                "p2": (p2.X, p2.Y),
                "z_bot": z_bot, "z_top": z_top,
                "thickness": thk,
            })
        except Exception:
            continue
    return out


def collect_doors_windows(walls):
    """Collect doors and windows whose host wall is in `walls`.

    Returns (doors, windows) - each a list of dicts with:
        { 'host_id', 'pt': (x,y), 'width', 'z_bot', 'z_top' }
    """
    walls_by_id = {w["id"]: w for w in walls}
    doors = []
    windows = []
    for cat_enum, target in (
        (BuiltInCategory.OST_Doors,   doors),
        (BuiltInCategory.OST_Windows, windows),
    ):
        try:
            insts = list(FilteredElementCollector(doc)
                         .OfCategory(cat_enum)
                         .WhereElementIsNotElementType())
        except Exception:
            continue
        for inst in insts:
            try:
                host = inst.Host
                if host is None: continue
                hid = eid_int(host.Id)
                if hid not in walls_by_id: continue
                loc = inst.Location
                if loc is None or not hasattr(loc, "Point"): continue
                pt = loc.Point
                # Width / height from the symbol
                sym = None
                try: sym = inst.Symbol
                except Exception: pass

                def _param_double(elem, bip):
                    try:
                        p = elem.get_Parameter(bip) if elem else None
                        if p: return p.AsDouble()
                    except Exception:
                        pass
                    return None

                width  = _param_double(sym, BuiltInParameter.FAMILY_WIDTH_PARAM)  or 3.0
                height = _param_double(sym, BuiltInParameter.FAMILY_HEIGHT_PARAM) or 7.0

                # Sill height for windows; doors usually sit at host base
                sill = _param_double(inst, BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM) or 0.0

                base_z = walls_by_id[hid]["z_bot"]
                z_b = base_z + sill
                z_t = z_b + height
                target.append({
                    "host_id": hid,
                    "pt":      (pt.X, pt.Y),
                    "width":   width,
                    "z_bot":   z_b,
                    "z_top":   z_t,
                })
            except Exception:
                continue
    return doors, windows


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
)


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


def wall_polygon(p1, p2, thickness):
    """Return 4 (x,y) corners of a wall rectangle of given thickness,
    centered along the line p1->p2. Order: p1-left, p2-left, p2-right, p1-right
    (winding doesn't matter for fill, but is consistent)."""
    x1, y1 = p1; x2, y2 = p2
    dx = x2 - x1; dy = y2 - y1
    L  = math.hypot(dx, dy)
    if L < 1e-9: L = 1e-9
    # Perpendicular unit (left-side when walking p1 -> p2)
    nx = -dy / L; ny = dx / L
    h  = thickness / 2.0
    return [
        (x1 + nx * h, y1 + ny * h),  # p1 left
        (x2 + nx * h, y2 + ny * h),  # p2 left
        (x2 - nx * h, y2 - ny * h),  # p2 right
        (x1 - nx * h, y1 - ny * h),  # p1 right
    ]


def snap_orthogonal(anchor, target):
    """Project `target` onto the closest of (horizontal, vertical) lines
    through `anchor`. Used to constrain the section line."""
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

        # Geometry caches
        self.crop_bounds    = get_view_crop_box_xy(view_plan)
        c_min = self.crop_bounds[0] if self.crop_bounds else None
        c_max = self.crop_bounds[1] if self.crop_bounds else None
        self.walls          = collect_walls(view_plan, c_min, c_max)
        self.doors, self.windows = collect_doors_windows(self.walls)
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

        # Section vertical extent - user-editable via Top/Bottom textboxes.
        # Defaults to project levels + buffers for foundations and roofs.
        if self.all_levels:
            self.section_z_top = max(l.Elevation for l in self.all_levels) + 15.0
            self.section_z_bot = min(l.Elevation for l in self.all_levels) - 4.0
        else:
            self.section_z_top = 25.0
            self.section_z_bot = -10.0

        # Cache plan bounds ONCE (crop + walls + floors). The section line is
        # deliberately excluded so dragging it doesn't rescale the plan.
        self.plan_bounds    = self._compute_plan_bounds()

        # Drag state (transient - reset between drags)
        self._plan_drag     = None    # dict: kind ('endpoint_a'/'endpoint_b'/'body'), ...
        self._sec_drag      = None    # dict: kind ('top'/'cut'/'bot'/'vd'), start_y, start_z

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
            self._draw_plan()
            self._draw_section()
            self._update_status()
            # Kick off the first Revit-rendered section image
            try:
                self._refresh_revit_render()
            except Exception:
                pass
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
        """Auto-place a section line spanning the crop region horizontally."""
        if self.crop_bounds is not None:
            (mnx, mny), (mxx, mxy) = self.crop_bounds
            cy = (mny + mxy) / 2.0
            # Pad inward by 5%
            pad = (mxx - mnx) * 0.05
            return [(mnx + pad, cy), (mxx - pad, cy)]
        # Fall back to spanning the wall extents
        if self.walls:
            xs = [p[0] for w in self.walls for p in (w['p1'], w['p2'])]
            ys = [p[1] for w in self.walls for p in (w['p1'], w['p2'])]
            cy = (min(ys) + max(ys)) / 2.0
            return [(min(xs), cy), (max(xs), cy)]
        return [(-50.0, 0.0), (50.0, 0.0)]

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

        # Footer
        self.btn_apply.Click  += self._on_apply
        self.btn_revert.Click += self._on_revert
        self.btn_close.Click  += self._on_close

        # Plan toolbar
        self.btn_recenter_section.Click += self._on_recenter_section

        # Manual section refresh
        self.btn_refresh_render.Click   += lambda s, e: self._refresh_revit_render()

        # Canvas mouse - bound at canvas level; child shapes set their own cursors
        self.cnv_plan.MouseLeftButtonDown += self._plan_mouse_down
        self.cnv_plan.MouseMove           += self._plan_mouse_move
        self.cnv_plan.MouseLeftButtonUp   += self._plan_mouse_up
        self.cnv_plan.MouseLeave          += self._plan_mouse_up

        self.cnv_section.MouseLeftButtonDown += self._sec_mouse_down
        self.cnv_section.MouseMove           += self._sec_mouse_move
        self.cnv_section.MouseLeftButtonUp   += self._sec_mouse_up
        self.cnv_section.MouseLeave          += self._sec_mouse_up

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
            # Validate top > cut > bot ordering
            self._validate_state()
        finally:
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
        if self.chk_snap_6in.IsChecked:
            val = snap_feet_to_6in(val)
        self.state[key]['offset'] = val
        # Reformat to canonical
        self._suppress_editor_events = True
        tb.Text = fmt_feet_in(val)
        self._suppress_editor_events = False
        self._draw_section()
        self._update_status(dirty=True)
        self._validate_state()

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

    def _on_revert(self, s, e):
        self.state = self._copy_state(self.state_initial)
        self._refresh_editor_from_state()
        self._draw_section()
        self._update_status(extra="Reverted to last applied values.")

    def _on_close(self, s, e):
        self.Close()

    def _on_recenter_section(self, s, e):
        self.section_line = self._default_section_line()
        self._draw_plan()
        self._draw_section()
        if self.use_revit_render:
            self._refresh_revit_render()

    # ------------------------------------------------------------------
    # Revit section render
    # ------------------------------------------------------------------

    def _refresh_revit_render(self):
        """Render the current section configuration via Revit and load
        the resulting PNG into the inline image. Called after drags end
        and on the manual refresh button."""
        # Show progress feedback
        self.txt_section_hint.Text = "Rendering Revit section preview..."
        try:
            path = self._render_revit_section()
        except Exception as ex:
            self.txt_section_hint.Text = "Render failed: {}".format(ex)
            return
        if not path or not os.path.exists(path):
            self.txt_section_hint.Text = "Render produced no image."
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
        self.img_section.Source = bi
        # Cache natural image dimensions so the overlay can be positioned
        # to align with the rendered architecture (Stretch="Uniform" causes
        # letterboxing whenever the canvas and image aspects don't match).
        try:
            self._img_natural_w = float(bi.PixelWidth)
            self._img_natural_h = float(bi.PixelHeight)
        except Exception:
            self._img_natural_w = self._img_natural_h = None
        # Repaint the overlay so A/B chips and view range planes align
        self._draw_section()

    def _section_image_layout(self):
        """Compute where the rendered image is actually displayed inside the
        canvas (with Stretch=Uniform letterboxing). Returns
        (offset_x, offset_y, width, height). Falls back to the full canvas
        if no image has loaded yet."""
        cw = max(1.0, float(self.cnv_section.ActualWidth))
        ch = max(1.0, float(self.cnv_section.ActualHeight))
        nw = getattr(self, '_img_natural_w', None)
        nh = getattr(self, '_img_natural_h', None)
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

    def _render_revit_section(self):
        """Create a temporary ViewSection at the current section line +
        far clip, export it to PNG, and roll back the section creation so
        nothing persists in the document. Returns the PNG path or None."""
        a = self.section_line[0]; b = self.section_line[1]
        dx = b[0] - a[0]; dy = b[1] - a[1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return None

        # Build the section's local coordinate system in the model.
        # Critical: it MUST be right-handed (BasisX X BasisY = BasisZ).
        # If we hand Revit a left-handed system it silently flips it,
        # which inverts the view direction - so a section the user expects
        # to look "east" actually looks "west" and they see the wrong wall
        # in the distance. This was the cut-loss bug.
        ux_m = dx / L; uy_m = dy / L                    # along-section A->B

        # When user has section_flip on, swap BasisX direction (B->A).
        # That's how Revit flips a section: BasisX flips, BasisZ follows
        # via cross product so the system stays right-handed.
        if self.section_flip:
            basisX = XYZ(-ux_m, -uy_m, 0)
        else:
            basisX = XYZ(ux_m, uy_m, 0)
        section_up = XYZ(0, 0, 1)
        basisZ     = basisX.CrossProduct(section_up)    # right-handed

        z_min, z_max = self._section_z_range()
        origin = XYZ((a[0] + b[0]) / 2.0,
                     (a[1] + b[1]) / 2.0,
                     (z_min + z_max) / 2.0)

        transform = Transform.Identity
        transform.Origin = origin
        transform.BasisX = basisX
        transform.BasisY = section_up
        transform.BasisZ = basisZ

        half_w = L / 2.0
        half_h = max(1.0, (z_max - z_min) / 2.0)
        far    = max(self.far_clip_offset, 1.0)

        # Section line at the front clip (Max.Z = 0). Cut walls that
        # straddle Z=0 render with poché correctly with right-handed coords.
        bbox = BoundingBoxXYZ()
        bbox.Transform = transform
        bbox.Min = XYZ(-half_w, -half_h, -far)
        bbox.Max = XYZ(+half_w, +half_h,  0.0)

        # Find a section ViewFamilyType
        vft_id = None
        for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
            try:
                if vft.ViewFamily == ViewFamily.Section:
                    vft_id = vft.Id; break
            except Exception:
                continue
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
                # Force Hidden Line + Coarse so the section renders cuts
                # consistently regardless of any default view template that
                # might have been applied. These two settings most
                # influence whether cut walls show poché vs being drawn
                # over by projected elements.
                try:
                    section.DisplayStyle = DisplayStyle.HiddenLine
                except Exception:
                    pass
                try:
                    section.DetailLevel = ViewDetailLevel.Coarse
                except Exception:
                    pass
                # Hybrid mode: hide everything except architectural categories.
                # Our overlay handles levels and view range planes, so we also
                # hide Revit's own level lines to avoid double-up.
                _apply_arch_only_visibility(section)
                t.Commit()
            except Exception:
                try: t.RollBack()
                except Exception: pass
                raise

            opts = ImageExportOptions()
            opts.ZoomType         = ZoomFitType.FitToPage
            opts.PixelSize        = 1400
            opts.ImageResolution  = ImageResolution.DPI_150
            opts.ExportRange      = ExportRange.SetOfViews
            from System.Collections.Generic import List as NetList
            view_list = NetList[ElementId]()
            view_list.Add(section.Id)
            opts.SetViewsAndSheets(view_list)
            opts.FilePath              = out_base
            opts.HLRandWFViewsFileType = ImageFileType.PNG

            doc.ExportImage(opts)
        finally:
            try: tg.RollBack()
            except Exception: pass

        # Revit appends the view name to the file path, so scan the dir
        try:
            for f in os.listdir(tmp_dir):
                if f.lower().endswith('.png'):
                    return os.path.join(tmp_dir, f)
        except Exception:
            pass
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

    def _compute_plan_bounds(self):
        """Build the static plan extent from crop + walls. The
        section line is intentionally excluded so dragging it does NOT
        cause the plan to rescale or pan around it."""
        xs = []; ys = []
        if self.crop_bounds is not None:
            (mnx, mny), (mxx, mxy) = self.crop_bounds
            xs.extend([mnx, mxx]); ys.extend([mny, mxy])
        for w in self.walls:
            xs.extend([w['p1'][0], w['p2'][0]])
            ys.extend([w['p1'][1], w['p2'][1]])
        if not xs:
            return ((-50.0, -50.0), (50.0, 50.0))
        mnx, mxx = min(xs), max(xs); mny, mxy = min(ys), max(ys)
        pad = max(2.0, max(mxx - mnx, mxy - mny) * 0.04)
        return ((mnx - pad, mny - pad), (mxx + pad, mxy + pad))

    def _plan_transform(self):
        """Return a (model_to_canvas, canvas_to_model, w, h, scale) tuple.
        Uses the cached self.plan_bounds so the plan stays fixed while the
        section line is dragged."""
        cw = max(1.0, float(self.cnv_plan.ActualWidth))
        ch = max(1.0, float(self.cnv_plan.ActualHeight))
        margin = 16.0

        (mnx, mny), (mxx, mxy) = self.plan_bounds
        span_x = max(0.001, mxx - mnx); span_y = max(0.001, mxy - mny)
        s = min((cw - 2 * margin) / span_x, (ch - 2 * margin) / span_y)

        # Center the content within the canvas
        used_w = span_x * s; used_h = span_y * s
        ox = (cw - used_w) / 2.0
        oy = (ch - used_h) / 2.0

        def m2c(mx, my):
            cx = ox + (mx - mnx) * s
            cy = ch - (oy + (my - mny) * s)   # flip Y so north is up
            return cx, cy

        def c2m(px, py):
            mx = mnx + (px - ox) / s
            my = mny + ((ch - py) - oy) / s
            return mx, my

        return m2c, c2m, cw, ch, s

    def _draw_plan(self):
        self.cnv_plan.Children.Clear()
        m2c, _c2m, cw, ch, scale = self._plan_transform()

        # Crop boundary (dashed)
        if self.crop_bounds is not None:
            (mnx, mny), (mxx, mxy) = self.crop_bounds
            corners = [(mnx, mny), (mxx, mny), (mxx, mxy), (mnx, mxy)]
            for i in range(4):
                a = corners[i]; b = corners[(i + 1) % 4]
                ax, ay = m2c(*a); bx, by = m2c(*b)
                ln = WLine()
                ln.X1 = ax; ln.Y1 = ay; ln.X2 = bx; ln.Y2 = by
                ln.Stroke = wpf_brush(COLOR_CROP)
                ln.StrokeThickness = 1.0
                ln.StrokeDashArray = self._dash_array([4, 3])
                self.cnv_plan.Children.Add(ln)

        # Walls - filled polygons sized by real thickness
        for w in self.walls:
            corners = wall_polygon(w['p1'], w['p2'], w['thickness'])
            poly = WPolygon()
            pts = PointCollection()
            for (mx, my) in corners:
                cxp, cyp = m2c(mx, my)
                pts.Add(WPoint(cxp, cyp))
            poly.Points = pts
            poly.Fill   = wpf_brush(COLOR_WALL_FILL)
            poly.Stroke = wpf_brush(COLOR_WALL_OUTLINE)
            poly.StrokeThickness = 0.8
            self.cnv_plan.Children.Add(poly)

        # Windows - light blue strokes spanning the wall opening
        self._draw_plan_windows(m2c)

        # Doors - gap + swing arc
        self._draw_plan_doors(m2c)

        # Far clip line (dashed parallel offset showing how far ahead of
        # the section line the section preview will pull elements from)
        self._draw_plan_far_clip_line(m2c)

        # Section line (drawn on top so its bubbles aren't hidden)
        self._draw_plan_section_line(m2c)

        # North arrow (small) in top-right
        nax = cw - 22; nay = 22
        north = WLine()
        north.X1 = nax; north.Y1 = nay + 12; north.X2 = nax; north.Y2 = nay - 4
        north.Stroke = wpf_brush(COLOR_WALL_OUTLINE); north.StrokeThickness = 1.5
        self.cnv_plan.Children.Add(north)
        ntb = TextBlock(); ntb.Text = "N"; ntb.FontSize = 10; ntb.Foreground = wpf_brush(COLOR_WALL_OUTLINE)
        Canvas.SetLeft(ntb, nax - 4); Canvas.SetTop(ntb, nay - 18)
        self.cnv_plan.Children.Add(ntb)

    # --- Plan sub-renderers --------------------------------------------------

    def _wall_by_id(self, wid):
        for w in self.walls:
            if w['id'] == wid:
                return w
        return None

    def _wall_unit_vectors(self, w):
        """Return (along_unit, perp_unit, length) for a wall in model XY."""
        x1, y1 = w['p1']; x2, y2 = w['p2']
        dx = x2 - x1; dy = y2 - y1
        L = math.hypot(dx, dy) or 1e-9
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux
        return (ux, uy), (nx, ny), L

    def _draw_plan_windows(self, m2c):
        for win in self.windows:
            w = self._wall_by_id(win['host_id'])
            if w is None: continue
            (ux, uy), (nx, ny), L = self._wall_unit_vectors(w)
            cx, cy = win['pt']
            half = win['width'] / 2.0
            thk_h = w['thickness'] / 2.0
            # 4 corners of the window opening footprint
            corners = [
                (cx - ux * half + nx * thk_h, cy - uy * half + ny * thk_h),
                (cx + ux * half + nx * thk_h, cy + uy * half + ny * thk_h),
                (cx + ux * half - nx * thk_h, cy + uy * half - ny * thk_h),
                (cx - ux * half - nx * thk_h, cy - uy * half - ny * thk_h),
            ]
            poly = WPolygon()
            pts = PointCollection()
            for (mx, my) in corners:
                px, py = m2c(mx, my); pts.Add(WPoint(px, py))
            poly.Points = pts
            poly.Fill = wpf_brush("#BEE3F8")           # light blue
            poly.Stroke = wpf_brush("#3182CE")
            poly.StrokeThickness = 0.8
            self.cnv_plan.Children.Add(poly)
            # Glass line through center of window
            g_a = (cx - ux * half, cy - uy * half)
            g_b = (cx + ux * half, cy + uy * half)
            ax, ay = m2c(*g_a); bx, by = m2c(*g_b)
            gl = WLine(); gl.X1 = ax; gl.Y1 = ay; gl.X2 = bx; gl.Y2 = by
            gl.Stroke = wpf_brush("#2B6CB0"); gl.StrokeThickness = 1.0
            self.cnv_plan.Children.Add(gl)

    def _draw_plan_doors(self, m2c):
        for d in self.doors:
            w = self._wall_by_id(d['host_id'])
            if w is None: continue
            (ux, uy), (nx, ny), L = self._wall_unit_vectors(w)
            cx, cy = d['pt']
            half = d['width'] / 2.0
            thk_h = w['thickness'] / 2.0
            # White "gap" polygon erases the wall fill in the door opening
            corners = [
                (cx - ux * half + nx * thk_h, cy - uy * half + ny * thk_h),
                (cx + ux * half + nx * thk_h, cy + uy * half + ny * thk_h),
                (cx + ux * half - nx * thk_h, cy + uy * half - ny * thk_h),
                (cx - ux * half - nx * thk_h, cy - uy * half - ny * thk_h),
            ]
            poly = WPolygon()
            pts = PointCollection()
            for (mx, my) in corners:
                px, py = m2c(mx, my); pts.Add(WPoint(px, py))
            poly.Points = pts
            poly.Fill   = wpf_brush("#F7FAFC")   # erase the wall body
            poly.Stroke = wpf_brush("#A0522D")   # door brown jamb
            poly.StrokeThickness = 0.8
            self.cnv_plan.Children.Add(poly)
            # Door leaf (line at one jamb perpendicular to wall, length = door width)
            leaf_start = (cx - ux * half, cy - uy * half)
            leaf_end   = (leaf_start[0] + nx * d['width'],
                          leaf_start[1] + ny * d['width'])
            ax, ay = m2c(*leaf_start); bx, by = m2c(*leaf_end)
            ll = WLine(); ll.X1 = ax; ll.Y1 = ay; ll.X2 = bx; ll.Y2 = by
            ll.Stroke = wpf_brush("#A0522D"); ll.StrokeThickness = 1.4
            self.cnv_plan.Children.Add(ll)
            # Swing arc - quarter arc from leaf_end back along wall direction
            arc_end = (leaf_start[0] + ux * d['width'],
                       leaf_start[1] + uy * d['width'])
            self._add_swing_arc(m2c, leaf_start, leaf_end, arc_end)

    def _add_swing_arc(self, m2c, center_m, start_m, end_m):
        """Draw a quarter-circle arc from start_m to end_m centered at center_m
        in plan canvas coords."""
        cx, cy = m2c(*center_m)
        sx, sy = m2c(*start_m)
        ex, ey = m2c(*end_m)
        radius = math.hypot(sx - cx, sy - cy)
        path = WPath()
        geom = PathGeometry()
        fig  = PathFigure()
        fig.StartPoint = WPoint(sx, sy)
        arc = ArcSegment()
        arc.Point  = WPoint(ex, ey)
        arc.Size   = System.Windows.Size(radius, radius)
        arc.SweepDirection = SweepDirection.Counterclockwise
        arc.IsLargeArc = False
        fig.Segments.Add(arc)
        geom.Figures.Add(fig)
        path.Data = geom
        path.Stroke = wpf_brush("#A0522D")
        path.StrokeThickness = 0.8
        path.IsHitTestVisible = False
        self.cnv_plan.Children.Add(path)

    # Far clip handle radius (px). Used by both draw + hit-test.
    _FC_HANDLE_R = 11

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

    def _draw_plan_far_clip_line(self, m2c):
        """Dashed parallel line showing where the far clip plane is in plan,
        with a draggable two-arrow handle at its midpoint."""
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
        ln.StrokeThickness = 1.0
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
            tk.StrokeThickness = 0.6
            tk.StrokeDashArray = self._dash_array([2, 2])
            tk.IsHitTestVisible = False
            self.cnv_plan.Children.Add(tk)

        # ---------- Draggable handle at the midpoint of the far clip line ----------
        # We need the SCREEN-space perpendicular direction so the in-handle
        # arrows point perpendicular to the on-page section line regardless
        # of the model orientation.
        sx_a, sy_a = m2c(*self.section_line[0])
        sx_b, sy_b = m2c(*self.section_line[1])
        sdx = sx_b - sx_a; sdy = sy_b - sy_a
        sL  = math.hypot(sdx, sdy) or 1e-9
        sux, suy = sdx / sL, sdy / sL
        snx, sny = -suy, sux                  # screen-coord perpendicular
        if self.section_flip:
            snx, sny = -snx, -sny

        hx, hy = self._far_clip_handle_screen(m2c)

        handle_r = self._FC_HANDLE_R

        # Background circle
        handle = WEllipse()
        handle.Width = 2 * handle_r; handle.Height = 2 * handle_r
        handle.Fill = Brushes.White
        handle.Stroke = wpf_brush("#4A5568")
        handle.StrokeThickness = 1.4
        handle.Tag = "far_clip_handle"
        handle.Cursor = Cursors.SizeAll
        handle.ToolTip = "Drag to change the far clip distance"
        Canvas.SetLeft(handle, hx - handle_r); Canvas.SetTop(handle, hy - handle_r)
        self.cnv_plan.Children.Add(handle)

        # Two opposing arrows inside, perpendicular to the section line
        arrow_len = handle_r - 2
        head_w    = 3.0
        for sign in (+1, -1):
            tip   = (hx + snx * arrow_len * sign,
                     hy + sny * arrow_len * sign)
            base_c = (hx + snx * (arrow_len - 4) * sign,
                      hy + sny * (arrow_len - 4) * sign)
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

    def _draw_plan_section_line(self, m2c):
        (ax, ay), (bx, by), ux, uy, nx, ny = self._section_line_screen_geom(m2c)

        # Section line itself - dash-dot, Revit-ish
        sl = WLine()
        sl.X1 = ax; sl.Y1 = ay; sl.X2 = bx; sl.Y2 = by
        sl.Stroke = wpf_brush(COLOR_SECTION_LINE)
        sl.StrokeThickness = 2.0
        sl.StrokeDashArray = self._dash_array([8, 3, 1, 3])
        sl.Tag = "section_body"
        sl.Cursor = Cursors.SizeAll
        self.cnv_plan.Children.Add(sl)

        R = self._BUBBLE_R
        for label, tag, (px, py) in (("A", "section_a", (ax, ay)),
                                     ("B", "section_b", (bx, by))):
            # Bubble
            bubble = WEllipse()
            bubble.Width = 2 * R; bubble.Height = 2 * R
            bubble.Fill = Brushes.White
            bubble.Stroke = wpf_brush(COLOR_SECTION_LINE)
            bubble.StrokeThickness = 2.2
            bubble.Tag = tag
            bubble.Cursor = Cursors.SizeAll
            Canvas.SetLeft(bubble, px - R); Canvas.SetTop(bubble, py - R)
            self.cnv_plan.Children.Add(bubble)
            # Letter
            tb = TextBlock()
            tb.Text = label
            tb.Foreground = wpf_brush("#742A2A")
            tb.FontWeight = System.Windows.FontWeights.Bold
            tb.FontSize = 14
            tb.IsHitTestVisible = False
            tb.Measure(System.Windows.Size(System.Double.PositiveInfinity,
                                           System.Double.PositiveInfinity))
            tw = tb.DesiredSize.Width; th = tb.DesiredSize.Height
            Canvas.SetLeft(tb, px - tw / 2.0); Canvas.SetTop(tb, py - th / 2.0)
            self.cnv_plan.Children.Add(tb)
            # External view-direction triangle — reinforces direction at a
            # glance from a distance.
            tip_x = px + nx * (R + 8); tip_y = py + ny * (R + 8)
            base1_x = px + nx * R + ux * 5; base1_y = py + ny * R + uy * 5
            base2_x = px + nx * R - ux * 5; base2_y = py + ny * R - uy * 5
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
        self._draw_flip_icon(ax, ay, ux, uy, nx, ny)

    def _draw_flip_icon(self, ax, ay, ux, uy, nx, ny):
        R = self._BUBBLE_R
        fx, fy = self._flip_icon_center((ax, ay), nx, ny)
        size = self._FLIP_R

        # Background circle (white with red ring) - the click target
        circle = WEllipse()
        circle.Width = 2 * size; circle.Height = 2 * size
        circle.Fill = Brushes.White
        circle.Stroke = wpf_brush(COLOR_SECTION_LINE)
        circle.StrokeThickness = 1.4
        circle.Tag = "flip_icon"
        circle.Cursor = Cursors.Hand
        circle.ToolTip = "Flip section view direction"
        Canvas.SetLeft(circle, fx - size); Canvas.SetTop(circle, fy - size)
        self.cnv_plan.Children.Add(circle)

        # Two opposing arrows inside (←  →) drawn ALONG the section direction
        # so they read "swap" regardless of section orientation.
        head = 4.0    # arrow head half-width
        tail = 5.5    # arrow length from center
        # Right arrow: tip at (fx + ux*tail, fy + uy*tail), base at (fx + ux*1, fy + uy*1)
        right = WPolygon()
        rpts = PointCollection()
        rpts.Add(WPoint(fx + ux * tail, fy + uy * tail))
        rpts.Add(WPoint(fx + ux * 1.5 + nx * head, fy + uy * 1.5 + ny * head))
        rpts.Add(WPoint(fx + ux * 1.5 - nx * head, fy + uy * 1.5 - ny * head))
        right.Points = rpts
        right.Fill = wpf_brush(COLOR_SECTION_LINE)
        right.IsHitTestVisible = False
        self.cnv_plan.Children.Add(right)
        # Left arrow (mirror)
        left = WPolygon()
        lpts = PointCollection()
        lpts.Add(WPoint(fx - ux * tail, fy - uy * tail))
        lpts.Add(WPoint(fx - ux * 1.5 + nx * head, fy - uy * 1.5 + ny * head))
        lpts.Add(WPoint(fx - ux * 1.5 - nx * head, fy - uy * 1.5 - ny * head))
        left.Points = lpts
        left.Fill = wpf_brush(COLOR_SECTION_LINE)
        left.IsHitTestVisible = False
        self.cnv_plan.Children.Add(left)

    def _flip_icon_center(self, bubble_screen, nx, ny):
        """Position the flip icon perpendicular to the section line, on the
        OPPOSITE side from the view-direction triangle (so they don't
        overlap visually). The user clicks to swap which side the section
        looks from."""
        bx, by = bubble_screen
        # Place the icon on the "back" side (-nx, -ny) so the front side
        # stays clear for the view-direction arrows.
        d = self._BUBBLE_R + self._FLIP_OFFSET
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
        / 'endpoint_a' / 'endpoint_b' / 'body' / None."""
        m2c, _c2m, _cw, _ch, _s = self._plan_transform()
        (ax, ay), (bx, by), ux, uy, nx, ny = self._section_line_screen_geom(m2c)
        # Flip icon (small target - prioritize)
        fx, fy = self._flip_icon_center((ax, ay), nx, ny)
        if (pos.X - fx) ** 2 + (pos.Y - fy) ** 2 <= self._FLIP_R ** 2:
            return 'flip_icon'
        # Far clip handle
        fc = self._far_clip_handle_screen(m2c)
        if fc is not None:
            if (pos.X - fc[0]) ** 2 + (pos.Y - fc[1]) ** 2 <= self._FC_HANDLE_R ** 2:
                return 'far_clip_handle'
        # Endpoints (use bubble radius)
        R = self._BUBBLE_R
        if (pos.X - ax) ** 2 + (pos.Y - ay) ** 2 <= R * R:
            return 'endpoint_a'
        if (pos.X - bx) ** 2 + (pos.Y - by) ** 2 <= R * R:
            return 'endpoint_b'
        # Body (within 6px perpendicular distance)
        dx = bx - ax; dy = by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-6: return None
        t = ((pos.X - ax) * dx + (pos.Y - ay) * dy) / L2
        if t < 0 or t > 1: return None
        cx = ax + t * dx; cy = ay + t * dy
        if (pos.X - cx) ** 2 + (pos.Y - cy) ** 2 <= 6 * 6:
            return 'body'
        return None

    def _plan_mouse_down(self, sender, e):
        pos = e.GetPosition(self.cnv_plan)
        kind = self._plan_hit(pos)
        if kind is None:
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

    def _plan_mouse_move(self, sender, e):
        if self._plan_drag is None:
            # Update cursor based on hover
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
            new_a  = snap_orthogonal(anchor, (mx, my))
            self.section_line = [new_a, anchor]
        elif kind == 'endpoint_b':
            anchor = self._plan_drag['line_start'][0]
            new_b  = snap_orthogonal(anchor, (mx, my))
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
            if self.chk_snap_6in.IsChecked:
                d = snap_feet_to_6in(d)
            self.far_clip_offset = d
        self._draw_plan()
        self._draw_section()

    def _plan_mouse_up(self, sender, e):
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
        """Vertical extent of the section view. User-controlled via the
        Section Extent textboxes (raise to see parapets / roof peaks,
        lower to see foundations).

        Deliberately INDEPENDENT of self.far_clip_offset - extending the
        far clip pulls in distant walls but doesn't change the vertical
        zoom. Cut walls stay anchored at the same screen position
        regardless of how deep we look behind the section."""
        return self.section_z_bot, self.section_z_top

    def _section_view_basis(self):
        """Return (a, b, ux, uy, nx, ny, L) describing the section line in
        MODEL coords plus the view-direction perpendicular.

        nx, ny is the math 'left-turn' from A->B (CCW) in model coords.
        This matches Revit's right-handed section coordinate system:
        view direction = -BasisZ where BasisZ = BasisX X BasisY = (uy,-ux).
        So view direction in model = (-uy, ux) = the CCW perpendicular.
        Putting plan UI on the same side as the actual render keeps the
        far-clip indicator and the rendered section in agreement."""
        a, b = self.section_line[0], self.section_line[1]
        dx = b[0] - a[0]; dy = b[1] - a[1]
        L = math.hypot(dx, dy) or 1e-9
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux                      # CCW left-turn from A->B
        if self.section_flip:
            nx, ny = -nx, -ny
        return a, b, ux, uy, nx, ny, L

    def _draw_section(self):
        """The section view is always a Revit-rendered PNG behind the
        canvas. Here we just draw the overlay (A/B chips, level lines,
        view range planes) on top of the image, aligned to the image's
        actual displayed area."""
        self.cnv_section.Children.Clear()
        cw = max(1.0, float(self.cnv_section.ActualWidth))
        ch = max(1.0, float(self.cnv_section.ActualHeight))
        self._draw_section_overlays_only(None, None, cw, ch)

    def _draw_section_overlays_only(self, _unused_x_at, _unused_y_at, cw, ch):
        """In Revit-render mode, draw only the A/B chips, levels, and the
        colored view range plane lines on top of the rendered image.

        The overlay aligns with the IMAGE's actual displayed area (not the
        full canvas), so when the rendered section is letterboxed by
        Stretch=Uniform, our level lines still match the building."""
        ox, oy, dw, dh = self._section_image_layout()
        z_min, z_max = self._section_z_range()
        z_span = max(0.001, z_max - z_min)

        # Image-aligned mappers
        def x_at(t):  return ox + t * dw
        def y_at(z):  return oy + (z_max - z) / z_span * dh

        # A / B chips at the image's left/right edges (not canvas edges)
        chip_r = 11
        for label, x_pos in (("A", ox + chip_r + 4),
                             ("B", ox + dw - chip_r - 4)):
            cx = x_pos; cy = oy + chip_r + 4
            chip = WEllipse()
            chip.Width = 2 * chip_r; chip.Height = 2 * chip_r
            chip.Fill = Brushes.White
            chip.Stroke = wpf_brush(COLOR_SECTION_LINE)
            chip.StrokeThickness = 1.6
            chip.IsHitTestVisible = False
            Canvas.SetLeft(chip, cx - chip_r); Canvas.SetTop(chip, cy - chip_r)
            self.cnv_section.Children.Add(chip)
            tb = TextBlock()
            tb.Text = label
            tb.Foreground = wpf_brush("#742A2A")
            tb.FontWeight = System.Windows.FontWeights.Bold
            tb.FontSize = 11
            tb.IsHitTestVisible = False
            tb.Measure(System.Windows.Size(System.Double.PositiveInfinity,
                                           System.Double.PositiveInfinity))
            tw = tb.DesiredSize.Width; th = tb.DesiredSize.Height
            Canvas.SetLeft(tb, cx - tw / 2.0); Canvas.SetTop(tb, cy - th / 2.0)
            self.cnv_section.Children.Add(tb)

        # Levels - drawn here since the rendered image hides Revit's level
        # category. Span the image's displayed width (not the full canvas).
        for lvl in self.all_levels:
            if not (z_min <= lvl.Elevation <= z_max):
                continue
            yz = y_at(lvl.Elevation)
            ln = WLine()
            ln.X1 = ox; ln.X2 = ox + dw
            ln.Y1 = yz; ln.Y2 = yz
            ln.Stroke = wpf_brush("#A0AEC0"); ln.StrokeThickness = 0.6
            ln.StrokeDashArray = self._dash_array([3, 3])
            self.cnv_section.Children.Add(ln)
            tb = TextBlock()
            tb.Text = "{}  {}".format(lvl.Name, fmt_feet_in(lvl.Elevation))
            tb.FontSize = 9; tb.Foreground = wpf_brush("#4A5568")
            tb.IsHitTestVisible = False
            # Inside-the-image label so it stays anchored to the level line
            Canvas.SetLeft(tb, ox + 4); Canvas.SetTop(tb, yz - 12)
            self.cnv_section.Children.Add(tb)

        # Section vertical-extent handles (top + bottom). Each is a dashed
        # gray horizontal line at z_top / z_bot plus a small triangle on
        # the right edge of the image that the user can grab and drag.
        for ext_key, z_val, point_up in (("extent_top", self.section_z_top, True),
                                         ("extent_bot", self.section_z_bot, False)):
            yz = y_at(z_val)
            # Dashed line spanning the image width
            ln = WLine()
            ln.X1 = ox; ln.X2 = ox + dw
            ln.Y1 = yz; ln.Y2 = yz
            ln.Stroke = wpf_brush("#718096")
            ln.StrokeThickness = 0.7
            ln.StrokeDashArray = self._dash_array([4, 4])
            ln.IsHitTestVisible = False
            self.cnv_section.Children.Add(ln)
            # Triangle handle on the right edge of the image. Apex points
            # away from the image center (up for top, down for bottom).
            tri = WPolygon()
            tri.Tag = ext_key
            tri.Cursor = Cursors.SizeNS
            tri.Fill = wpf_brush("#4A5568")
            tri.Stroke = Brushes.White
            tri.StrokeThickness = 1.0
            tri.ToolTip = "Drag to change how far {} the section shows".format(
                "above" if point_up else "below")
            tri_x = ox + dw - 4   # 4 px in from the image's right edge
            tpts = PointCollection()
            if point_up:
                tpts.Add(WPoint(tri_x, yz - 9))         # apex up
                tpts.Add(WPoint(tri_x - 7, yz + 1))
                tpts.Add(WPoint(tri_x + 7, yz + 1))
            else:
                tpts.Add(WPoint(tri_x, yz + 9))         # apex down
                tpts.Add(WPoint(tri_x - 7, yz - 1))
                tpts.Add(WPoint(tri_x + 7, yz - 1))
            tri.Points = tpts
            self.cnv_section.Children.Add(tri)
            # Small elevation label next to the handle
            elev_tb = TextBlock()
            elev_tb.Text = fmt_feet_in(z_val)
            elev_tb.FontSize = 9
            elev_tb.Foreground = wpf_brush("#4A5568")
            elev_tb.IsHitTestVisible = False
            Canvas.SetLeft(elev_tb, ox + dw - 60)
            Canvas.SetTop(elev_tb, yz - 14 if point_up else yz + 4)
            self.cnv_section.Children.Add(elev_tb)

        # View range planes
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
                tb = TextBlock(); tb.Text = "{} - Unlimited".format(label)
                tb.Foreground = wpf_brush(color); tb.FontSize = 10
                tb.FontWeight = System.Windows.FontWeights.SemiBold
                Canvas.SetLeft(tb, max(0, cw - 120))
                Canvas.SetTop(tb, 6 + (1 if key == "vd" else 0) * 16)
                self.cnv_section.Children.Add(tb)
                continue
            yz = y_at(z)
            ln = WLine()
            ln.X1 = ox; ln.X2 = ox + dw
            ln.Y1 = yz; ln.Y2 = yz
            ln.Stroke = wpf_brush(color)
            ln.StrokeThickness = 3.0 if key == "cut" else 2.0
            ln.StrokeDashArray = self._dash_array([6, 4])
            ln.Tag = "plane_{}".format(key)
            ln.Cursor = Cursors.SizeNS
            self.cnv_section.Children.Add(ln)
            chip = Border()
            chip.Background = wpf_brush(color)
            chip.CornerRadius = System.Windows.CornerRadius(2)
            chip.Padding = Thickness(4, 1, 4, 1)
            chip.Tag = "plane_{}".format(key)
            chip.Cursor = Cursors.SizeNS
            ctb = TextBlock(); ctb.Text = "{}  {}".format(label, fmt_feet_in(z))
            ctb.Foreground = Brushes.White; ctb.FontSize = 10
            ctb.FontWeight = System.Windows.FontWeights.SemiBold
            chip.Child = ctb
            chip.Measure(System.Windows.Size(System.Double.PositiveInfinity, System.Double.PositiveInfinity))
            chip_w = chip.DesiredSize.Width if chip.DesiredSize.Width > 0 else 80
            Canvas.SetLeft(chip, max(2.0, ox + dw - chip_w - 2))
            Canvas.SetTop(chip, yz - 18 if key in ("top", "cut") else yz + 2)
            self.cnv_section.Children.Add(chip)

    # --- Section drag handlers ----------------------------------------------

    def _section_mappers(self):
        """Return (x_at, y_at, m_at_y) aligned to the rendered section
        image's actual display area, so drags + drawing stay aligned."""
        ox, oy, dw, dh = self._section_image_layout()
        z_min, z_max = self._section_z_range()
        z_span = max(0.001, z_max - z_min)
        def x_at(t):
            return ox + t * dw
        def y_at(z):
            return oy + (z_max - z) / z_span * dh
        def m_at_y(py):
            return z_max - (py - oy) / max(dh, 0.001) * z_span
        return x_at, y_at, m_at_y

    def _sec_hit(self, pos):
        """Return what's under `pos`: a plane key ('top'/'cut'/'bot'/'vd'),
        an extent handle key ('extent_top'/'extent_bot'), or None.
        Extent handles take priority since they're small targets on the
        right edge of the image."""
        ox, oy, dw, dh = self._section_image_layout()
        _x_at, y_at, _m_at_y = self._section_mappers()

        # Extent handles (right edge of image, ~7px half-width, ~9px tall)
        right_edge = ox + dw
        if abs(pos.X - (right_edge - 4)) <= 11:    # within the triangle's x band
            for ext_key, z_val in (("extent_top", self.section_z_top),
                                   ("extent_bot", self.section_z_bot)):
                yz = y_at(z_val)
                # Triangle's vertical extent is ~9px
                if abs(pos.Y - yz) <= 11:
                    return ext_key

        # View range planes
        for key in PLANE_KEYS:
            if key in self.disabled_planes:
                continue
            z = self._abs_z(key)
            if z is None: continue
            yz = y_at(z)
            if abs(pos.Y - yz) <= 6:
                return key
        return None

    def _sec_mouse_down(self, sender, e):
        pos = e.GetPosition(self.cnv_section)
        key = self._sec_hit(pos)
        if key is None:
            return
        # Extent handles - drag without view-range-plane state, no template lock
        if key in ('extent_top', 'extent_bot'):
            self._sec_drag = {'key': key}
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

    def _sec_mouse_move(self, sender, e):
        if self._sec_drag is None:
            return
        if e.LeftButton != MouseButtonState.Pressed:
            self._sec_drag = None
            return
        pos = e.GetPosition(self.cnv_section)
        _x_at, _y_at, m_at_y = self._section_mappers()
        key = self._sec_drag['key']
        # ----- Extent handle drag: change vertical extent of section view -----
        if key in ('extent_top', 'extent_bot'):
            z_new = m_at_y(pos.Y)
            if self.chk_snap_6in.IsChecked:
                z_new = snap_feet_to_6in(z_new)
            if key == 'extent_top':
                # Don't let top drop below current bottom (+ 1 ft margin)
                self.section_z_top = max(z_new, self.section_z_bot + 1.0)
            else:
                self.section_z_bot = min(z_new, self.section_z_top - 1.0)
            # Repaint overlay live so the dashed line + handle follow the mouse
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
        if self.chk_snap_6in.IsChecked:
            new_off = snap_feet_to_6in(new_off)
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
        if self._sec_drag is not None:
            kind = self._sec_drag['key']
            self._sec_drag = None
            try: self.cnv_section.ReleaseMouseCapture()
            except Exception: pass
            # If the user just changed the section's vertical extent, the
            # bbox we send to Revit needs to be re-rendered.
            if kind in ('extent_top', 'extent_bot'):
                self._refresh_revit_render()


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
