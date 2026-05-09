# -*- coding: utf-8 -*-
"""View Templates Manager - all-in-one view template editor.

Iteration 1: UI shell + navigation + mock data.
Real Revit templates load on the left; right-side parameter table mirrors
Revit's native View Template editor (Parameter / Value / Include + Apply-to-N
in bulk mode). Sub-dialogs (V/G Overrides categories, Imports with CAD
layers, Filters, RVT Links) are populated with mock data so the layout can
be navigated end-to-end without API wiring.

See README.md in this folder for the iteration plan and API mapping.
"""

__title__  = 'View Templates\nManager'
__author__ = 'Nathaniel'
__doc__    = ('Edit and bulk-modify view templates - mirrors Revit\'s native '
              'template editor. Iteration 1 is a UI preview; wiring lands in '
              'iter 2+.')

import os
import clr  # noqa: F401

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")
clr.AddReference("System.Xaml")

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType,
    BuiltInParameter, BuiltInCategory,
)

# Revit-link APIs come and go a bit between versions — guard the imports.
try:
    from Autodesk.Revit.DB import RevitLinkType, RevitLinkInstance
    _HAS_REVIT_LINK = True
except Exception:
    _HAS_REVIT_LINK = False
try:
    from Autodesk.Revit.DB import RevitLinkGraphicsSettings
    _HAS_LINK_GRAPHICS = True
except Exception:
    _HAS_LINK_GRAPHICS = False
try:
    from Autodesk.Revit.DB import LinkVisibility
    _HAS_LINK_VISIBILITY = True
except Exception:
    _HAS_LINK_VISIBILITY = False

from System.Windows import (
    Visibility, Thickness, HorizontalAlignment, VerticalAlignment,
)
from System.Windows.Controls import (
    Border, StackPanel, Grid, ColumnDefinition, RowDefinition,
    CheckBox, TextBlock, Button, ComboBoxItem,
    Orientation, ScrollViewer,
)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Input import Cursors
from System import EventHandler

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

# Revit document handles
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# Resolve XAML paths next to this script
SCRIPT_DIR  = os.path.dirname(__file__)
MAIN_XAML   = os.path.join(SCRIPT_DIR, 'ViewTemplatesManagerForm.xaml')
VG_CAT_XAML = os.path.join(SCRIPT_DIR, 'VgCategoriesDialog.xaml')
VG_IMP_XAML = os.path.join(SCRIPT_DIR, 'VgImportsDialog.xaml')
VG_FLT_XAML = os.path.join(SCRIPT_DIR, 'VgFiltersDialog.xaml')
VG_LNK_XAML = os.path.join(SCRIPT_DIR, 'VgLinksDialog.xaml')


# ===========================================================================
# Helpers
# ===========================================================================

def _set_legend_mixed(form):
    """Set the legend's 'mixed' checkbox to the indeterminate state. Every
    form that ships the tri-state legend (per CLAUDE.md UI conventions)
    names that checkbox `chk_legend_mixed`. WPF's IsThreeState=True alone
    starts at False; we have to push None in code to render the indeterminate
    indicator."""
    try:
        chk = getattr(form, "chk_legend_mixed", None)
        if chk is not None:
            chk.IsChecked = None
    except Exception:
        pass


def eid_int(eid):
    """Revit 2024+ safe ElementId int read."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def make_eid(int_val):
    """Construct an ElementId. Revit 2024+ uses Int64; fall back to int32."""
    from Autodesk.Revit.DB import ElementId
    try:
        from System import Int64
        return ElementId(Int64(int_val))
    except Exception:
        try:
            return ElementId(int(int_val))
        except Exception:
            return ElementId.InvalidElementId


def is_template_param_included(view_template, param_id):
    """True if the parameter (by ElementId) is currently controlled by the
    template (i.e. NOT in its non-controlled list)."""
    if param_id is None:
        return True
    try:
        non_ctrl = view_template.GetNonControlledTemplateParameterIds()
        for eid in non_ctrl:
            if eid_int(eid) == eid_int(param_id):
                return False
        return True
    except Exception:
        return True


# ---- Category helpers (iter 3a) --------------------------------------------

def _categories_of_type(rdoc, cat_type):
    """Return [(name, Category), ...] sorted by name for the given CategoryType."""
    from Autodesk.Revit.DB import CategoryType
    out = []
    try:
        for cat in rdoc.Settings.Categories:
            try:
                if cat.CategoryType == cat_type:
                    out.append((cat.Name, cat))
            except Exception:
                continue
    except Exception:
        pass
    return sorted(out, key=lambda kv: kv[0].lower())


def get_model_categories(rdoc):
    from Autodesk.Revit.DB import CategoryType
    return _categories_of_type(rdoc, CategoryType.Model)


def get_annotation_categories(rdoc):
    from Autodesk.Revit.DB import CategoryType
    return _categories_of_type(rdoc, CategoryType.Annotation)


def get_analytical_categories(rdoc):
    from Autodesk.Revit.DB import CategoryType
    try:
        return _categories_of_type(rdoc, CategoryType.AnalyticalModel)
    except AttributeError:
        # Some Revit versions name it differently
        return []


# ---- Iter-4b per-aspect link helpers ---------------------------------------
#
# Each per-aspect setting on a `RevitLinkGraphicsSettings` is one of two
# shapes:
#
#   Inherit-only   — caller picks just LinkVisibility (ByHost / ByLink /
#                    Custom). View filters, View range, Color fill, Object
#                    styles, Nested links are this shape.
#
#   Inherit + value — picks LinkVisibility AND, when Custom, a specific
#                    value (Phase ElementId, ViewDetailLevel enum, etc.).
#                    Phase, Phase filter, Detail level, Discipline.
#
# We give each aspect a tuple value of shape (lv_name, specific_or_None).
# The combo's `.Tag` holds that tuple so `_selected_combo_value` returns
# the right thing on OK.

def _lv_str_to_name(s):
    """Map the str() of a LinkVisibility enum to one of our canonical
    names: 'ByHostView' / 'ByLinkView' / 'Custom' / None."""
    if not s:
        return None
    if "Custom" in s:
        return "Custom"
    if "ByLink" in s:
        return "ByLinkView"
    if "ByHost" in s or "ByParent" in s:
        return "ByHostView"
    return None


def _lv_name_to_enum(lv_name):
    """Resolve a canonical name to the LinkVisibility enum value."""
    if not _HAS_LINK_VISIBILITY:
        return None
    try:
        if lv_name == "ByLinkView":
            return LinkVisibility.ByLinkView
        if lv_name == "Custom":
            return LinkVisibility.Custom
        return LinkVisibility.ByHostView
    except Exception:
        return None


def _read_aspect_inherit(settings, candidate_attrs):
    """Inherit-only aspects: try a few likely attribute / getter names and
    return our canonical LinkVisibility name string (or None)."""
    if settings is None:
        return None
    for name in candidate_attrs:
        try:
            attr = getattr(settings, name, None)
        except Exception:
            attr = None
        if attr is None:
            continue
        try:
            val = attr() if callable(attr) else attr
        except Exception:
            continue
        s = None
        try:
            s = str(val) if val is not None else None
        except Exception:
            s = None
        n = _lv_str_to_name(s)
        if n is not None:
            return n
    return None


def _write_aspect_inherit(settings, candidate_attrs, lv_name):
    """Inherit-only aspects: write the LinkVisibility via either a property
    setter or a Set<Aspect>(LinkVisibility) method. Returns True on success."""
    lv = _lv_name_to_enum(lv_name)
    if lv is None:
        return False
    for name in candidate_attrs:
        # Try as a writable property
        try:
            setattr(settings, name, lv)
            return True
        except Exception:
            pass
        # Try a paired Set method (SetXxx)
        try:
            m = getattr(settings, "Set" + name, None)
            if callable(m):
                m(lv)
                return True
        except Exception:
            pass
    return False


def _read_aspect_paired(settings, type_method, value_method):
    """Paired Inherit+value aspects (Phase/Phase filter/Detail/Discipline).
    Returns (lv_name, specific_value) where specific_value is an int (or
    eid_int for ElementId-based aspects). Falls back to (None, None) on
    failure."""
    if settings is None:
        return (None, None)
    lv_name = None
    try:
        m = getattr(settings, type_method, None)
        if callable(m):
            v = m()
            lv_name = _lv_str_to_name(str(v) if v is not None else None)
    except Exception:
        lv_name = None
    spec = None
    try:
        m = getattr(settings, value_method, None)
        if callable(m):
            v = m()
            if v is not None:
                # ElementId? int? enum? convert to int safely.
                try:
                    spec = eid_int(v)
                except Exception:
                    try:
                        spec = int(v)
                    except Exception:
                        spec = None
    except Exception:
        pass
    return (lv_name, spec)


# Per-aspect candidate attribute names — Revit's API has been re-shuffled
# across versions, so each aspect tries a couple of likely shapes.
_ASPECT_FILTERS_ATTRS    = ("ViewFilterType", "ViewFilters")
_ASPECT_VIEWRANGE_ATTRS  = ("ViewRange",)
_ASPECT_COLORFILL_ATTRS  = ("ColorFill",)
_ASPECT_OBJSTYLES_ATTRS  = ("ObjectStyles",)
_ASPECT_NESTED_ATTRS     = ("NestedLinks",)


def _enumerate_linked_views(link_doc):
    """Return [(display_name, view_eid_int), ...] for non-template views in
    the linked document, plus a leading '<None>' option mapped to -1."""
    options = [("<None>", -1)]
    if link_doc is None:
        return options
    pairs = []
    try:
        for v in FilteredElementCollector(link_doc).OfClass(View):
            try:
                if v.IsTemplate:
                    continue
                vt = viewtype_label(v.ViewType)
                pairs.append(("{0}: {1}".format(vt, v.Name), eid_int(v.Id)))
            except Exception:
                continue
    except Exception:
        pass
    pairs.sort(key=lambda kv: kv[0].lower())
    options.extend(pairs)
    return options


def _enumerate_phases(link_doc):
    """Phases live in the linked document. Returns [(name, eid_int), ...]."""
    if link_doc is None:
        return []
    out = []
    try:
        from Autodesk.Revit.DB import Phase
        for p in FilteredElementCollector(link_doc).OfClass(Phase):
            try:
                out.append((p.Name, eid_int(p.Id)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _enumerate_phase_filters(link_doc):
    if link_doc is None:
        return []
    out = []
    try:
        from Autodesk.Revit.DB import PhaseFilter
        for f in FilteredElementCollector(link_doc).OfClass(PhaseFilter):
            try:
                out.append((f.Name, eid_int(f.Id)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _read_link_visibility_mode(settings):
    """Inspect a RevitLinkGraphicsSettings and return one of:
       'byhost' / 'bylinked' / 'custom' / 'unknown'.
    The accessor for the overall visibility type has shifted across Revit
    versions (some expose `GetLinkVisibilityType()`, others expose
    `LinkVisibility` as a property, etc.) so we try several shapes and
    convert whatever we find to a string. 'unknown' means an override
    exists but we couldn't determine its kind — better than guessing
    'bylinked' and lying to the UI."""
    if settings is None:
        return "byhost"
    candidates = (
        "GetLinkVisibilityType",
        "LinkVisibility",
        "LinkVisibilityType",
        "GetLinkVisibility",
        "VisibilityType",
        "GetVisibilityType",
    )
    for name in candidates:
        try:
            attr = getattr(settings, name, None)
        except Exception:
            attr = None
        if attr is None:
            continue
        try:
            value = attr() if callable(attr) else attr
        except Exception:
            continue
        if value is None:
            continue
        try:
            vt_str = str(value)
        except Exception:
            continue
        if not vt_str:
            continue
        if "Custom" in vt_str:
            return "custom"
        if "ByLink" in vt_str:
            return "bylinked"
        if "ByHost" in vt_str:
            return "byhost"
    return "unknown"


def _post_manage_view_templates():
    """Queue Revit's Manage View Templates dialog via PostCommand. The
    enum name has shifted across Revit versions, so try a few candidates
    and accept whichever resolves. Returns True iff PostCommand fired."""
    try:
        from Autodesk.Revit.UI import RevitCommandId, PostableCommand
    except Exception:
        return False
    candidates = (
        "ManageViewTemplates",  # Revit 2018+ canonical
        "ViewTemplates",        # older / shorthand
        "ApplyViewTemplate",    # last-resort: at least opens template UI
    )
    for name in candidates:
        try:
            cmd = getattr(PostableCommand, name, None)
            if cmd is None:
                continue
            cmd_id = RevitCommandId.LookupPostableCommandId(cmd)
            if cmd_id is None:
                continue
            __revit__.PostCommand(cmd_id)
            return True
        except Exception:
            continue
    return False


def _resolve_link_name(rlt, instance, link_doc):
    """Best-effort display name for a RevitLinkType. Tries, in order:
       1. linked Document.Title — the actual loaded .rvt filename, most
          reliable when the link is loaded
       2. RevitLinkType.Name — usually the .rvt filename but can be a
          generic 'Link' if the user renamed the type
       3. RVT_LINK_INSTANCE_NAME parameter on a sample instance
       4. 'Link <eid>' fallback so the picker entry is still distinguishable
    """
    # 1. Linked doc title
    if link_doc is not None:
        try:
            t = link_doc.Title
            if t:
                return t
        except Exception:
            pass
    # 2. Type name
    try:
        n = rlt.Name if rlt is not None else None
        if n and n.strip() and n.strip().lower() != "link":
            if n.lower().endswith(".rvt"):
                n = n[:-4]
            return n
    except Exception:
        pass
    # 3. Instance parameter
    if instance is not None:
        try:
            p = instance.get_Parameter(BuiltInParameter.RVT_LINK_INSTANCE_NAME)
            if p:
                v = p.AsString()
                if v and v.strip():
                    return v
        except Exception:
            pass
    # 4. Whatever the type name was (even "Link") + element id, so multiple
    #    same-named types remain distinguishable
    try:
        base = rlt.Name if rlt is not None else "Link"
    except Exception:
        base = "Link"
    try:
        suffix = " (id {0})".format(eid_int(rlt.Id) if rlt is not None else "?")
    except Exception:
        suffix = ""
    return (base or "Link") + suffix


def get_revit_link_types(rdoc):
    """Return [{name, type_id, doc}, ...] — one entry per unique
    RevitLinkType in the project. We walk RevitLinkInstance first to pick
    up loaded links (which have a real GetLinkDocument we can read the
    Title from), then sweep any RevitLinkType that wasn't represented
    (typically unloaded links) so the picker still shows them."""
    if not _HAS_REVIT_LINK:
        return []
    by_type = {}   # key: type_id int → record
    try:
        for inst in FilteredElementCollector(rdoc).OfClass(RevitLinkInstance):
            try:
                tid = inst.GetTypeId()
                if tid is None:
                    continue
                key = eid_int(tid)
                if key in by_type:
                    continue
                try:
                    link_doc = inst.GetLinkDocument()
                except Exception:
                    link_doc = None
                try:
                    rlt = rdoc.GetElement(tid)
                except Exception:
                    rlt = None
                name = _resolve_link_name(rlt, inst, link_doc)
                by_type[key] = {"name": name, "type_id": tid, "doc": link_doc}
            except Exception:
                continue
    except Exception:
        pass
    # Sweep types that had no instance (unloaded link types)
    try:
        for lt in FilteredElementCollector(rdoc).OfClass(RevitLinkType):
            try:
                key = eid_int(lt.Id)
                if key in by_type:
                    continue
                name = _resolve_link_name(lt, None, None)
                by_type[key] = {"name": name + "  (unloaded)",
                                "type_id": lt.Id, "doc": None}
            except Exception:
                continue
    except Exception:
        pass
    return sorted(by_type.values(), key=lambda d: d["name"].lower())


def get_imported_cad_links(rdoc):
    """Return list of {name, category, layers: [(layer_name, layer_category), ...]}.

    Mirrors what shows under Imported Categories in Revit's V/G dialog. Each
    DWG/DXF that is *actually placed* in the project (whether imported or
    linked) shows up as one entry, with its layers expanded as children.

    Implementation note: we walk `ImportInstance` elements to find the real
    imports first, then group by their file-level Category. This is more
    accurate than walking `OST_ImportObjectStyles.SubCategories` because the
    sub-category list can include phantom/family-only entries, and because
    on some projects Revit's category tree exposes the layers flat instead
    of grouped under the file. If no `ImportInstance` elements exist, we
    fall back to the sub-category walk so seed-file or family-internal
    imports still surface.
    """
    out = []
    seen_cat_ids = set()

    # 1. Primary path: enumerate ImportInstance elements (the actual placed
    #    CAD imports + links). Group by their Category (= file-level entry).
    try:
        from Autodesk.Revit.DB import ImportInstance
        try:
            instances = list(FilteredElementCollector(rdoc).OfClass(ImportInstance))
        except Exception:
            instances = []
        for inst in instances:
            try:
                cat = inst.Category
                if cat is None:
                    continue
                cid = eid_int(cat.Id)
                if cid in seen_cat_ids:
                    continue
                seen_cat_ids.add(cid)
                # Best display name: the CADLinkType's Name is the DWG
                # filename (e.g. "ARCH-Floor.dwg"). Fall back to the
                # category name. We deliberately ignore inst.Name because
                # for ImportInstance it returns positioning info like
                # "location <Not Shared>", not the filename.
                disp = None
                try:
                    tid = inst.GetTypeId()
                    if tid is not None:
                        type_elem = rdoc.GetElement(tid)
                        if type_elem is not None:
                            tname = type_elem.Name
                            if tname and tname.strip():
                                disp = tname.strip()
                except Exception:
                    pass
                if not disp:
                    try:
                        cn = cat.Name
                        if cn and cn.strip():
                            disp = cn.strip()
                    except Exception:
                        pass
                if not disp:
                    disp = "(unnamed import)"
                # Mark linked vs imported so the user can tell at a glance
                try:
                    is_linked = bool(inst.IsLinked)
                except Exception:
                    is_linked = False
                if is_linked and "(linked)" not in disp.lower():
                    disp = "{0}  (linked)".format(disp)
                layers = []
                try:
                    for layer in cat.SubCategories:
                        try:
                            layers.append((layer.Name, layer))
                        except Exception:
                            continue
                except Exception:
                    pass
                out.append({
                    "name":     disp,
                    "category": cat,
                    "layers":   sorted(layers, key=lambda kv: kv[0].lower()),
                })
            except Exception:
                continue
    except Exception:
        # ImportInstance class import failed — drop into the fallback below.
        pass

    if out:
        return sorted(out, key=lambda d: d["name"].lower())

    # 2. Fallback: walk the OST_ImportObjectStyles sub-tree directly. This
    #    covers projects where ImportInstance enumeration is empty (rare —
    #    e.g., imports that only live inside families).
    try:
        cats = rdoc.Settings.Categories
        for cat in cats:
            try:
                if eid_int(cat.Id) != int(BuiltInCategory.OST_ImportObjectStyles):
                    continue
            except Exception:
                continue
            try:
                for sub in cat.SubCategories:
                    # Only treat sub-categories that have their own layer
                    # children as real CAD files. A leaf-level sub here
                    # is more likely to be a flat layer entry.
                    layer_subs = []
                    try:
                        for layer in sub.SubCategories:
                            try:
                                layer_subs.append((layer.Name, layer))
                            except Exception:
                                continue
                    except Exception:
                        pass
                    if not layer_subs:
                        # Skip flat / phantom entries with no children
                        continue
                    out.append({
                        "name":     sub.Name,
                        "category": sub,
                        "layers":   sorted(layer_subs, key=lambda kv: kv[0].lower()),
                    })
            except Exception:
                pass
    except Exception:
        pass
    return sorted(out, key=lambda d: d["name"].lower())


def read_category_visible_bulk(templates, cat_id):
    """Return one of: True (all templates show this category),
                     False (all templates hide it),
                     None (templates disagree; leave indeterminate).
    Categories that throw or aren't supported on a given template are
    skipped — they don't count toward the consensus."""
    states = []
    for t in templates:
        try:
            states.append(not bool(t.GetCategoryHidden(cat_id)))
        except Exception:
            continue
    if not states:
        return None
    if all(states):
        return True
    if not any(states):
        return False
    return None


def apply_category_visibility(template, cat_id, visible):
    """Write visibility to a single template. Returns True on success,
    False if the category isn't hideable on this template or the API
    throws. Quiet on failure so bulk callers can continue."""
    try:
        if not template.CanCategoryBeHidden(cat_id):
            return False
    except Exception:
        # CanCategoryBeHidden missing on some Revit versions; try Set anyway
        pass
    try:
        template.SetCategoryHidden(cat_id, not bool(visible))
        return True
    except Exception:
        return False


# ---- Iter-3c OGS helpers (line weight / line color / line pattern / detail level)

# Combo options: weight goes from <no override> through 1..16
_LINE_WEIGHT_OPTIONS = (
    [("<no override>", -1)] + [(str(i), i) for i in range(1, 17)])

# Detail Level on an OGS uses 0=Undefined ('by view'), 1=Coarse, 2=Medium, 3=Fine
_OGS_DETAIL_LEVEL_OPTIONS = [
    ("<By View>", 0),
    ("Coarse",    1),
    ("Medium",    2),
    ("Fine",      3),
]


def get_line_patterns(rdoc):
    """Return [(name, ElementId), ...] for project line patterns, with
    '<no override>' as the first option (mapped to InvalidElementId).
    Built-in 'Solid' is intentionally omitted; users who want explicit
    Solid pick the named LinePatternElement they have in the project."""
    from Autodesk.Revit.DB import LinePatternElement, ElementId
    out = [("<no override>", ElementId.InvalidElementId)]
    pats = []
    try:
        for lp in FilteredElementCollector(rdoc).OfClass(LinePatternElement):
            try:
                pats.append((lp.Name, lp.Id))
            except Exception:
                continue
    except Exception:
        pass
    pats.sort(key=lambda kv: kv[0].lower())
    return out + pats


def _ogs_color_to_rgb(color):
    """Convert Autodesk.Revit.DB.Color to (r, g, b). Returns None if the
    color has not been overridden (its IsValid flag is False)."""
    try:
        if color is None:
            return None
        # Some OGS getters return a sentinel "invalid" Color when the field
        # isn't overridden — IsValid flags those.
        if not bool(color.IsValid):
            return None
        return (int(color.Red), int(color.Green), int(color.Blue))
    except Exception:
        return None


def _rgb_to_revit_color(rgb):
    """Build an Autodesk.Revit.DB.Color from an (r, g, b) tuple. Returns
    Color.InvalidColorValue when rgb is None (so the caller can clear the
    override on a per-field basis)."""
    from Autodesk.Revit.DB import Color
    if rgb is None:
        try:
            return Color.InvalidColorValue
        except Exception:
            return Color(0, 0, 0)
    r, g, b = rgb
    return Color(int(r), int(g), int(b))


def pick_color(initial_rgb=None):
    """Open Windows Forms ColorDialog and return (r, g, b) tuple or None
    if the user cancelled. Reused for every Color button in the V/G
    sub-dialogs since WPF doesn't ship a built-in color picker."""
    try:
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
    except Exception:
        return None
    try:
        from System.Windows.Forms import ColorDialog, DialogResult
        from System.Drawing import Color as WinColor
    except Exception:
        return None
    dlg = ColorDialog()
    try:
        dlg.AnyColor = True
        dlg.AllowFullOpen = True
        dlg.FullOpen = True
    except Exception:
        pass
    if initial_rgb is not None:
        try:
            r, g, b = initial_rgb
            dlg.Color = WinColor.FromArgb(int(r), int(g), int(b))
        except Exception:
            pass
    res = dlg.ShowDialog()
    # IronPython enum compare via str() to dodge Forms-vs-WPF DialogResult
    if str(res) != "OK":
        return None
    c = dlg.Color
    return (int(c.R), int(c.G), int(c.B))


def _set_swatch_color(swatch_border, label_textblock, rgb):
    """Update a small color swatch + its text label to reflect the chosen
    RGB. Pass None to show the 'no override' sentinel state."""
    from System.Windows.Media import SolidColorBrush, Color as WpfColor
    if rgb is None:
        try:
            swatch_border.Background = SolidColorBrush(WpfColor.FromRgb(0xCC, 0xCC, 0xCC))
        except Exception:
            pass
        if label_textblock is not None:
            try:
                label_textblock.Text = "<no override>"
            except Exception:
                pass
        return
    try:
        r, g, b = rgb
        swatch_border.Background = SolidColorBrush(WpfColor.FromRgb(int(r), int(g), int(b)))
    except Exception:
        pass
    if label_textblock is not None:
        try:
            label_textblock.Text = "RGB({0}, {1}, {2})".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))
        except Exception:
            pass


def is_category_overridable(template, cat_id):
    """Pre-flight check before we call SetCategoryOverrides — categories
    Revit doesn't allow overriding (some auto-hidden ones, or analytical
    on a non-structural template) will throw at Set otherwise, which can
    leave the transaction in a state that crashes Commit. Default True
    on older Revit versions that don't expose the method."""
    try:
        return bool(template.IsCategoryOverridable(cat_id))
    except Exception:
        return True


def make_silent_failure_options(t):
    """Return a FailureHandlingOptions configured to swallow warning-level
    failures during commit, so a borderline V/G write can't pop a modal
    Revit dialog (which can crash pyRevit's modeless context). Apply via
    `t.SetFailureHandlingOptions(opts)` before `t.Start()`."""
    try:
        from Autodesk.Revit.DB import (
            IFailuresPreprocessor, FailureProcessingResult,
        )
    except Exception:
        return None

    class _SilentFailurePreprocessor(IFailuresPreprocessor):
        def PreprocessFailures(self, accessor):
            try:
                accessor.DeleteAllWarnings()
            except Exception:
                pass
            return FailureProcessingResult.Continue

    try:
        opts = t.GetFailureHandlingOptions()
        opts = opts.SetFailuresPreprocessor(_SilentFailurePreprocessor())
        try:
            opts = opts.SetClearAfterRollback(True)
        except Exception:
            pass
        try:
            opts = opts.SetForcedModalHandling(False)
        except Exception:
            pass
        return opts
    except Exception:
        return None


_VIEWTYPE_LABEL = {
    ViewType.FloorPlan:       "Floor Plan",
    ViewType.CeilingPlan:     "Ceiling Plan",
    ViewType.Elevation:       "Elevation",
    ViewType.Section:         "Section",
    ViewType.Detail:          "Detail",
    ViewType.ThreeD:          "3D",
    ViewType.Schedule:        "Schedule",
    ViewType.DraftingView:    "Drafting",
    ViewType.Legend:          "Legend",
    ViewType.EngineeringPlan: "MEP Plan",
    ViewType.AreaPlan:        "Area Plan",
    ViewType.Walkthrough:     "Walkthrough",
    ViewType.Rendering:       "Rendering",
}

_PLAN_VIEW_TYPES = (
    ViewType.FloorPlan, ViewType.CeilingPlan,
    ViewType.AreaPlan,  ViewType.EngineeringPlan,
)


def viewtype_label(vt):
    return _VIEWTYPE_LABEL.get(vt, str(vt))


def is_plan_view_type(vt):
    return vt in _PLAN_VIEW_TYPES


_IMPERIAL_SCALES = {
    1:    '12" = 1\'-0"',     2:    '6" = 1\'-0"',
    4:    '3" = 1\'-0"',      8:    '1 1/2" = 1\'-0"',
    12:   '1" = 1\'-0"',      16:   '3/4" = 1\'-0"',
    24:   '1/2" = 1\'-0"',    32:   '3/8" = 1\'-0"',
    48:   '1/4" = 1\'-0"',    64:   '3/16" = 1\'-0"',
    96:   '1/8" = 1\'-0"',    128:  '3/32" = 1\'-0"',
    192:  '1/16" = 1\'-0"',   384:  '1/32" = 1\'-0"',
    768:  '1/64" = 1\'-0"',
}

def scale_label(scale):
    if not scale or scale <= 0:
        return "-"
    try:
        s = int(scale)
    except Exception:
        return "1:{0}".format(scale)
    return _IMPERIAL_SCALES.get(s, "1:{0}".format(s))


def get_all_templates(rdoc):
    """Return list of view template View elements, sorted by name."""
    out = []
    for v in FilteredElementCollector(rdoc).OfClass(View):
        if v.IsTemplate:
            out.append(v)
    return sorted(out, key=lambda v: v.Name.lower())


def build_usage_map(rdoc):
    """Return {template_eid_int: count_of_views_using_it}."""
    usage = {}
    for v in FilteredElementCollector(rdoc).OfClass(View):
        if v.IsTemplate:
            continue
        try:
            tid = v.ViewTemplateId
            if tid and eid_int(tid) != -1:
                k = eid_int(tid)
                usage[k] = usage.get(k, 0) + 1
        except Exception:
            pass
    return usage


# ===========================================================================
# MOCK DATA - populates sub-dialogs so the UI can be navigated end-to-end
# without depending on what's in the user's project. Replaced by real
# project queries in iter 2-4.
# ===========================================================================

MOCK_RVT_LINKS = [
    {"name": "Architectural - Floor Plans.rvt",       "kind": "rvt"},
    {"name": "Structural - Steel Frame.rvt",          "kind": "rvt"},
    {"name": "Site - Survey Coordination.rvt",        "kind": "rvt"},
]

MOCK_CAD_IMPORTS = [
    {"name": "site-survey.dwg",       "layers": [
        "0", "BOUNDARY", "CONTOURS-MAJOR", "CONTOURS-MINOR",
        "EXISTING-BLDG", "ROAD", "UTILITIES", "VEGETATION",
    ]},
    {"name": "title-block-arch.dwg",  "layers": [
        "0", "BORDER", "LOGO", "NORTH-ARROW", "TEXT-NOTES",
    ]},
]

MOCK_MODEL_CATEGORIES = [
    "Air Terminals", "Cable Trays", "Casework", "Ceilings", "Columns",
    "Communication Devices", "Conduits", "Curtain Panels", "Curtain Wall Mullions",
    "Curtain Walls", "Data Devices", "Doors", "Ducts", "Duct Fittings",
    "Electrical Equipment", "Electrical Fixtures", "Floors", "Furniture",
    "Generic Models", "Grids", "Levels", "Lighting Fixtures",
    "Mass", "Mechanical Equipment", "Parts", "Pipe Fittings", "Pipes",
    "Plumbing Fixtures", "Railings", "Ramps", "Roofs", "Rooms", "Site",
    "Sprinklers", "Stairs", "Structural Columns", "Structural Foundations",
    "Structural Framing", "Topography", "Walls", "Windows",
]

MOCK_ANNOTATION_CATEGORIES = [
    "Callouts", "Color Fill", "Dimensions", "Door Tags", "Elevation Marks",
    "Furniture Tags", "Grids", "Keynote Tags", "Levels", "Lines",
    "Matchline", "Plan Region", "Reference Lines", "Reference Planes",
    "Revision Clouds", "Room Tags", "Section Marks", "Spot Coordinates",
    "Spot Elevations", "Text Notes", "Title Blocks", "View Reference",
    "View Titles", "Wall Tags", "Window Tags",
]

MOCK_ANALYTICAL_CATEGORIES = [
    "Analytical Beams", "Analytical Braces", "Analytical Columns",
    "Analytical Floors", "Analytical Foundations", "Analytical Links",
    "Analytical Nodes", "Analytical Walls", "Internal Loads",
    "Structural Loads",
]

MOCK_FILTERS = [
    {"name": "Arch - Demolished",         "enabled": True,  "visible": False},
    {"name": "Arch - Existing to Remain", "enabled": True,  "visible": True },
    {"name": "Mech - Hot Water Supply",   "enabled": True,  "visible": True },
    {"name": "Mech - Hot Water Return",   "enabled": True,  "visible": True },
    {"name": "Plumbing - Sanitary",       "enabled": True,  "visible": True },
    {"name": "Plumbing - Vent",           "enabled": True,  "visible": True },
    {"name": "Electrical - Lighting",     "enabled": False, "visible": True },
    {"name": "Fire - Sprinkler",          "enabled": True,  "visible": True },
]

# Combobox lists
MOCK_SCALES         = ['1/32" = 1\'-0"', '1/16" = 1\'-0"', '3/32" = 1\'-0"',
                       '1/8" = 1\'-0"',  '3/16" = 1\'-0"', '1/4" = 1\'-0"',
                       '3/8" = 1\'-0"',  '1/2" = 1\'-0"',  '3/4" = 1\'-0"',
                       '1" = 1\'-0"',    '1 1/2" = 1\'-0"', '3" = 1\'-0"',
                       '6" = 1\'-0"',    '12" = 1\'-0"', 'Custom']
MOCK_DISPLAY_MODEL  = ['Normal', 'Halftone', 'Do not display']
MOCK_DETAIL_LEVEL   = ['Coarse', 'Medium', 'Fine']
MOCK_PARTS_VIS      = ['Show Original', 'Show Parts', 'Show Both']
MOCK_DISCIPLINE     = ['Architectural', 'Structural', 'Mechanical',
                       'Electrical', 'Plumbing', 'Coordination']
MOCK_HIDDEN_LINES   = ['None', 'By Discipline', 'All']
MOCK_PHASE_FILTERS  = ['Show All', 'Show Complete', 'Show Demo + New',
                       'Show New', 'Show Previous + Demo', 'None']
MOCK_ORIENTATION    = ['Project North', 'True North']
MOCK_UNDERLAY       = ['Look up', 'Look down']
MOCK_DEPTH_CLIP     = ['No clip', 'Clip with line', 'Clip without line']
MOCK_COLOR_LOC      = ['Background', 'Foreground']
MOCK_COLOR_SCHEMES  = ['<none>', 'Departments', 'Names']
MOCK_PATTERNS       = ['<no override>', 'Solid fill', 'Diagonal crosshatch',
                       'Diagonal up', 'Diagonal down', 'Horizontal',
                       'Vertical', 'Crosshatch', 'Sand', 'Gypsum-Plaster',
                       'Concrete', 'Steel']
MOCK_LINE_WEIGHTS   = ['<no override>'] + [str(i) for i in range(1, 17)]
MOCK_LINE_PATTERNS  = ['<no override>', 'Solid', 'Dash', 'Dash dot',
                       'Center', 'Hidden', 'Dot', 'Long dash']
MOCK_LINK_VIEWS     = ['<None>', 'Floor Plan: Level 1 - Composite',
                       'Floor Plan: Level 2 - Composite', '3D View: {3D}']
MOCK_LINK_INHERIT   = ['<By host view>', '<Custom>']
MOCK_LINK_INHERIT_M = ['<By host model>', '<Custom>']
MOCK_LINK_NESTED    = ['<By parent link>', '<Custom>']


# ===========================================================================
# PARAMETER SPECS (iter 2) — readers, writers, options, and the registry of
# simple-parameter rows the form's Apply button knows how to write.
# ===========================================================================

# Sentinel placed in a combo when bulk-selected templates have differing
# values for a parameter. The user must pick a real value before Apply
# touches that row across the bulk set.
_VARIES = object()


def _populate_combo_kv(combo, options):
    """Replace `combo` items with ComboBoxItems carrying (label, value) data."""
    combo.Items.Clear()
    for label, value in options:
        item = ComboBoxItem()
        item.Content = label
        item.Tag = value
        combo.Items.Add(item)


def _selected_combo_value(combo):
    """Return the selected ComboBoxItem's .Tag (the underlying value), or None."""
    item = combo.SelectedItem
    if item is None:
        return None
    return getattr(item, "Tag", None)


def _select_combo_by_value(combo, value):
    """Select the ComboBoxItem whose .Tag == value. Returns True if found.
    On miss, leaves the combo unselected so the user notices the unknown."""
    for i in range(combo.Items.Count):
        item = combo.Items[i]
        tag = getattr(item, "Tag", None)
        if tag == value:
            combo.SelectedIndex = i
            return True
    combo.SelectedIndex = -1
    return False


def _remove_varies_marker(combo):
    """Drop any '(varies)' sentinel item left over from a previous bulk load."""
    for i in range(combo.Items.Count):
        item = combo.Items[i]
        if getattr(item, "Tag", None) is _VARIES:
            combo.Items.RemoveAt(i)
            return


def _set_combo_varies(combo):
    """Insert/select an italic '(varies)' sentinel at the top of the combo."""
    from System.Windows import FontStyles
    _remove_varies_marker(combo)
    item = ComboBoxItem()
    item.Content = "(varies)"
    item.Tag = _VARIES
    item.FontStyle = FontStyles.Italic
    item.Foreground = SolidColorBrush(Color.FromRgb(0xA0, 0xAE, 0xC0))
    combo.Items.Insert(0, item)
    combo.SelectedIndex = 0


# ---- Per-parameter readers --------------------------------------------------

def _read_int_param(template, bip):
    """Generic int-valued BIP read; None if param not present."""
    try:
        p = template.get_Parameter(bip)
        return p.AsInteger() if p else None
    except Exception:
        return None


def _read_eid_param(template, bip):
    """ElementId-valued BIP read; returns eid_int (or -1 for InvalidElementId)."""
    try:
        p = template.get_Parameter(bip)
        if p is None:
            return None
        eid = p.AsElementId()
        if eid is None:
            return -1
        return eid_int(eid)
    except Exception:
        return None


# ---- Per-parameter writers --------------------------------------------------

def _write_int_param(template, bip, value):
    try:
        p = template.get_Parameter(bip)
        if p is None or p.IsReadOnly:
            return False
        p.Set(int(value))
        return True
    except Exception:
        return False


def _write_eid_param(template, bip, value):
    try:
        p = template.get_Parameter(bip)
        if p is None or p.IsReadOnly:
            return False
        p.Set(make_eid(int(value)))
        return True
    except Exception:
        return False


# Some parameters are read-only via the BIP/Parameter route on view templates
# even though they're settable through the View class's direct properties.
# Use direct properties for those four; the rest can stay on the BIP path.

def _write_view_scale(template, value):
    try:
        template.Scale = int(value)
        return True
    except Exception:
        return False


def _write_detail_level(template, value):
    try:
        from Autodesk.Revit.DB import ViewDetailLevel
        m = {1: ViewDetailLevel.Coarse,
             2: ViewDetailLevel.Medium,
             3: ViewDetailLevel.Fine}
        template.DetailLevel = m.get(int(value), ViewDetailLevel.Medium)
        return True
    except Exception:
        return False


def _write_parts_visibility(template, value):
    try:
        from Autodesk.Revit.DB import PartsVisibility
        # Per the existing helper: 0=Show Original, 1=Show Parts only, 2=Show Both.
        m = {0: PartsVisibility.ShowOriginal,
             1: PartsVisibility.ShowPartsOnly,
             2: PartsVisibility.ShowPartsAndOriginal}
        template.PartsVisibility = m.get(int(value), PartsVisibility.ShowOriginal)
        return True
    except Exception:
        return False


def _write_discipline(template, value):
    try:
        from Autodesk.Revit.DB import ViewDiscipline
        from System import Enum
        template.Discipline = Enum.ToObject(ViewDiscipline, int(value))
        return True
    except Exception:
        return False


# Matching readers for the same four — direct properties are simpler and
# avoid Parameter wrapper inconsistencies.

def _read_view_scale(template):
    try:
        return int(template.Scale)
    except Exception:
        return None


def _read_detail_level(template):
    try:
        return int(template.DetailLevel)
    except Exception:
        return None


def _read_parts_visibility(template):
    try:
        return int(template.PartsVisibility)
    except Exception:
        return None


def _read_discipline(template):
    try:
        return int(template.Discipline)
    except Exception:
        return None


# ---- Option providers ------------------------------------------------------

# Imperial scale list with denominators that match Revit's `View.Scale` int.
_VIEW_SCALE_OPTIONS = [
    ('1/64" = 1\'-0"',    768), ('1/32" = 1\'-0"',  384),
    ('1/16" = 1\'-0"',    192), ('3/32" = 1\'-0"',  128),
    ('1/8" = 1\'-0"',      96), ('3/16" = 1\'-0"',   64),
    ('1/4" = 1\'-0"',      48), ('3/8" = 1\'-0"',    32),
    ('1/2" = 1\'-0"',      24), ('3/4" = 1\'-0"',    16),
    ('1" = 1\'-0"',        12), ('1 1/2" = 1\'-0"',   8),
    ('3" = 1\'-0"',         4), ('6" = 1\'-0"',       2),
    ('12" = 1\'-0"',        1),
]

_DISPLAY_MODEL_OPTIONS = [("Normal", 0), ("Halftone", 1), ("Do not display", 2)]
_DETAIL_LEVEL_OPTIONS  = [("Coarse", 1), ("Medium", 2), ("Fine", 3)]
_PARTS_VIS_OPTIONS     = [("Show Original", 0), ("Show Parts", 1), ("Show Both", 2)]
# ViewDiscipline enum is bitwise: Architectural=1, Structural=2, Mechanical=4,
# Electrical=8, Plumbing=16, Coordination=4095. Compound values (e.g. Mech+Elec)
# fall through to "(unknown)" — user can pick a single one to overwrite.
_DISCIPLINE_OPTIONS    = [
    ("Architectural", 1),
    ("Structural",    2),
    ("Mechanical",    4),
    ("Electrical",    8),
    ("Plumbing",      16),
    ("Coordination",  4095),
]
_HIDDEN_LINES_OPTIONS  = [("None", 0), ("By Discipline", 1), ("All", 2)]
_COLOR_LOC_OPTIONS     = [("Background", 0), ("Foreground", 1)]
_UNDERLAY_OPTIONS      = [("Look up", 0), ("Look down", 1)]
_ORIENTATION_OPTIONS   = [("Project North", 0), ("True North", 1)]


def _options_phase_filter():
    """Enumerate all PhaseFilter elements in the project + a 'None' option."""
    opts = [("None", -1)]
    try:
        from Autodesk.Revit.DB import PhaseFilter
        for pf in FilteredElementCollector(doc).OfClass(PhaseFilter):
            try:
                opts.append((pf.Name, eid_int(pf.Id)))
            except Exception:
                continue
    except Exception:
        pass
    return opts


# ---- Spec class + registry --------------------------------------------------

class SimpleParamSpec(object):
    """Recipe for one simple-parameter row: how to read it from a template,
    how to write it back, what options to show in the combobox, and which
    Include / Apply checkboxes to consult."""

    def __init__(self, label, combo_attr, inc_chk_attr, app_chk_attr,
                 options_provider, reader, writer, bip):
        self.label = label
        self.combo_attr = combo_attr
        self.inc_chk_attr = inc_chk_attr
        self.app_chk_attr = app_chk_attr
        self.options_provider = options_provider
        self.reader = reader
        self.writer = writer
        self.bip = bip


def _make_simple_param_specs():
    """Build the iter-2 spec list. Each row is added through a helper that
    looks up the BuiltInParameter by name and silently skips the row if the
    BIP doesn't exist on this Revit version, so a single missing parameter
    never breaks the form load."""
    specs = []

    def safe_bip(name):
        try:
            return getattr(BuiltInParameter, name)
        except AttributeError:
            return None

    def _wrap_options(options):
        # Allow callers to pass either a static list or a callable provider.
        return options if callable(options) else (lambda _o=options: _o)

    def add_int(label, combo_attr, inc_chk_attr, app_chk_attr, options, bip_name):
        bip = safe_bip(bip_name)
        if bip is None:
            return
        specs.append(SimpleParamSpec(
            label, combo_attr, inc_chk_attr, app_chk_attr,
            _wrap_options(options),
            (lambda t, _b=bip: _read_int_param(t, _b)),
            (lambda t, v, _b=bip: _write_int_param(t, _b, v)),
            bip))

    def add_eid(label, combo_attr, inc_chk_attr, app_chk_attr, options, bip_name):
        bip = safe_bip(bip_name)
        if bip is None:
            return
        specs.append(SimpleParamSpec(
            label, combo_attr, inc_chk_attr, app_chk_attr,
            _wrap_options(options),
            (lambda t, _b=bip: _read_eid_param(t, _b)),
            (lambda t, v, _b=bip: _write_eid_param(t, _b, v)),
            bip))

    def add_direct(label, combo_attr, inc_chk_attr, app_chk_attr, options,
                   bip_name, reader, writer):
        """Same as add_int but uses caller-supplied direct-property reader
        and writer (the BIP is still recorded so the Include checkbox can
        target the right Parameter ID)."""
        bip = safe_bip(bip_name)
        if bip is None:
            return
        specs.append(SimpleParamSpec(
            label, combo_attr, inc_chk_attr, app_chk_attr,
            _wrap_options(options), reader, writer, bip))

    # Direct-property rows: the Parameter route is read-only on view
    # templates, so we route through the View class's actual setters.
    add_direct("View Scale",       "cmb_scale",      "chk_inc_scale",        "chk_app_scale",
               _VIEW_SCALE_OPTIONS,    "VIEW_SCALE",
               _read_view_scale, _write_view_scale)
    add_direct("Detail Level",     "cmb_detail",     "chk_inc_detail",       "chk_app_detail",
               _DETAIL_LEVEL_OPTIONS,  "VIEW_DETAIL_LEVEL",
               _read_detail_level, _write_detail_level)
    add_direct("Parts Visibility", "cmb_parts",      "chk_inc_parts",        "chk_app_parts",
               _PARTS_VIS_OPTIONS,     "VIEW_PARTS_VISIBILITY",
               _read_parts_visibility, _write_parts_visibility)
    add_direct("Discipline",       "cmb_discipline", "chk_inc_discipline",   "chk_app_discipline",
               _DISCIPLINE_OPTIONS,    "VIEW_DISCIPLINE",
               _read_discipline, _write_discipline)

    # BIP-route rows: these don't have direct View properties, so we set
    # them through Parameter.Set. If any turn out to also be read-only
    # via the BIP route on templates, _write_int_param returns False
    # silently and the row stays preview-only on this Revit version.
    add_int("Display Model",     "cmb_display_model", "chk_inc_display_model", "chk_app_display_model",
            _DISPLAY_MODEL_OPTIONS, "VIEW_MODEL_DISPLAY_MODE")
    add_eid("Phase Filter",      "cmb_phase_filter",  "chk_inc_phase_filter",  "chk_app_phase_filter",
            _options_phase_filter,  "VIEW_PHASE_FILTER")
    add_int("Show Hidden Lines", "cmb_hidden_lines",  "chk_inc_hidden_lines",  "chk_app_hidden_lines",
            _HIDDEN_LINES_OPTIONS,  "VIEW_SHOW_HIDDEN_LINES")
    # Color Scheme Location — no reliable BIP exists for this; the right API
    # is `View.ColorSchemeLocation` (a property, not a parameter). Deferred
    # to a later iteration so the row stays preview-only for now.
    add_int("Underlay Orientation","cmb_underlay",    "chk_inc_underlay",      "chk_app_underlay",
            _UNDERLAY_OPTIONS,      "VIEWER_UNDERLAY_ORIENTATION")
    add_int("Orientation",       "cmb_orientation",   "chk_inc_orientation",   "chk_app_orientation",
            _ORIENTATION_OPTIONS,   "PLAN_VIEW_NORTH")

    return specs


# ---- Include-only specs ----------------------------------------------------
#
# Some parameter-table rows show an "Edit…" button or are otherwise preview-
# only for value editing, but their **Include** checkbox should still drive
# `View.SetNonControlledTemplateParameterIds` — that's what tells the
# template "stop enforcing this parameter on views applied with me." Each
# spec is just (chk_attr, BuiltInParameter) — no reader/writer, no combo.
#
# BIP names vary across Revit versions, so each spec gives a list of
# candidates and we keep the first one that resolves. Specs whose BIP can't
# be resolved at all get their checkbox hidden so users don't see a control
# that does nothing.

class IncludeOnlySpec(object):
    def __init__(self, label, chk_attr, bip):
        self.label = label
        self.chk_attr = chk_attr
        self.bip = bip


def _make_include_only_specs():
    specs = []
    unresolved_chks = []   # checkbox attr names whose BIP didn't resolve

    def safe_bip(*names):
        for n in names:
            try:
                bip = getattr(BuiltInParameter, n)
                if bip is not None:
                    return bip
            except AttributeError:
                continue
        return None

    def add(label, chk_attr, *bip_names):
        bip = safe_bip(*bip_names)
        if bip is None:
            unresolved_chks.append(chk_attr)
            return
        specs.append(IncludeOnlySpec(label, chk_attr, bip))

    # V/G Overrides rows — typical BIPs are VIS_GRAPHICS_<KIND>
    add("V/G Overrides Model",          "chk_inc_vg_model",
        "VIS_GRAPHICS_MODEL")
    add("V/G Overrides Annotation",     "chk_inc_vg_annotation",
        "VIS_GRAPHICS_ANNOTATION")
    add("V/G Overrides Analytical Model","chk_inc_vg_analytical",
        "VIS_GRAPHICS_ANALYTICAL_MODEL", "VIS_GRAPHICS_ANALYTICAL")
    add("V/G Overrides Import",         "chk_inc_vg_import",
        "VIS_GRAPHICS_IMPORT")
    add("V/G Overrides Filters",        "chk_inc_vg_filters",
        "VIS_GRAPHICS_FILTERS")
    add("V/G Overrides RVT Links",      "chk_inc_vg_links",
        "VIS_GRAPHICS_LINKS")
    # Graphic Display rows
    add("Model Display",        "chk_inc_gd_model_display",
        "MODEL_GRAPHICS_STYLE", "VIEW_MODEL_DISPLAY_MODE")
    add("Shadows",              "chk_inc_gd_shadows",
        "SHADOWS_RENDERING_OPTIONS", "VIEW_GRAPH_SHADOWS")
    add("Sketchy Lines",        "chk_inc_gd_sketchy",
        "SKETCHY_LINES_SETTINGS", "VIEW_SKETCHY_LINES")
    add("Lighting",             "chk_inc_gd_lighting",
        "LIGHTING_OPTIONS", "VIEW_LIGHTING")
    add("Photographic Exposure","chk_inc_gd_photo",
        "PHOTOGRAPHIC_EXPOSURE", "PHOTO_EXPOSURE_SETTINGS",
        "VIEW_PHOTO_EXPOSURE")
    add("Background",           "chk_inc_gd_background",
        "BACKGROUND_RENDERING_OPTIONS", "VIEW_BACKGROUND")
    # Other preview-value rows whose Include is still meaningful
    add("View Range",           "chk_inc_view_range",
        "VIEWER_VOLUME_OF_INTEREST_CROP", "PLAN_VIEW_RANGE",
        "VIEWER_VIEW_RANGE")
    add("Color Scheme Location","chk_inc_color_loc",
        "ROOM_COLOR_SCHEME_LOCATION", "VIEW_COLOR_SCHEME_LOCATION")
    add("Color Scheme",         "chk_inc_color_scheme",
        "ROOM_COLOR_SCHEME_ID", "VIEW_COLOR_SCHEME")
    add("System Color Schemes", "chk_inc_sys_color",
        "VIEW_SYSTEM_COLOR_SCHEMES")
    add("Depth Clipping",       "chk_inc_depth_clip",
        "PLAN_VIEW_DEPTH_CLIPPING", "VIEWER_DEPTH_CLIP")

    return specs, unresolved_chks


# ===========================================================================
# Data model: TemplateItem (left-side list rows)
# ===========================================================================

class TemplateItem(object):
    """One row in the left-side templates list. Pure-Python; UI state is
    held in widget references rather than INotifyPropertyChanged here."""

    def __init__(self, view, usage_count):
        self.view         = view
        self.eid_int      = eid_int(view.Id)
        self.name         = view.Name
        self.usage_count  = usage_count
        try:
            self.view_type     = view.ViewType
            self.view_type_str = viewtype_label(view.ViewType)
        except Exception:
            self.view_type     = None
            self.view_type_str = "-"
        try:
            self.scale_str = scale_label(view.Scale)
        except Exception:
            self.scale_str = "-"
        # UI bits filled in by builder
        self.row_border = None
        self.checkbox   = None


# ===========================================================================
# MAIN FORM
# ===========================================================================

class ViewTemplatesManagerForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, MAIN_XAML)
        self._all_templates    = []   # list of TemplateItem
        self._search_text      = ""
        self._chip_filter      = "All"   # All | Plan | Section | 3D | Other
        self._chip_buttons     = {}      # name -> ToggleButton
        # Multi-row highlight state for shift/ctrl-click on the template list.
        # Highlight is a visual "selection" separate from the per-row checkbox;
        # when a checkbox in a highlighted row is clicked, we propagate the
        # check to all currently highlighted rows.
        self._highlighted_eids = set()
        self._last_clicked_eid = None
        # Persisted "checked" set: survives chip-filter changes / search edits
        # so a user who checked 5 templates while on the All filter doesn't
        # lose the selection when they switch to e.g. Plan and back.
        self._checked_eids = set()
        # Iter-2 spec list — built once per form so the BIP enums are
        # resolved against the live Revit API.
        self._param_specs = _make_simple_param_specs()
        # Iter-2.x include-only specs — rows where the value editor is
        # preview-only / informational, but the Include checkbox itself
        # should still drive SetNonControlledTemplateParameterIds.
        self._include_only_specs, _unresolved_inc_chks = _make_include_only_specs()
        self._unresolved_inc_chks = list(_unresolved_inc_chks)

        self._populate_combos()
        self._build_chips()
        self._wire_events()
        self._load_templates()
        self._render_template_list()
        self._hide_apply_column()
        self._hide_unresolved_includes()
        self._update_mode()
        _set_legend_mixed(self)

    def _hide_unresolved_includes(self):
        """Hide Include checkboxes whose BIP didn't resolve on this Revit
        version, so users don't see a control that would never write
        anything. (e.g. if VIS_GRAPHICS_LINKS is missing on a given Revit
        build, the row's Include cell is collapsed.)"""
        for chk_attr in self._unresolved_inc_chks:
            try:
                getattr(self, chk_attr).Visibility = Visibility.Collapsed
            except Exception:
                pass

    def _hide_apply_column(self):
        """Iter-2: the per-row Apply checkbox column is gone. Apply Changes
        writes every row's current value to all selected templates (rows
        showing '(varies)' are skipped). We hide the column instead of
        editing every row's XAML — saves a lot of mechanical edits."""
        try:
            self.col_apply_hdr.Width = self._grid_length(0)
            self.hdr_apply.Visibility = Visibility.Collapsed
        except Exception:
            pass
        # Walk each Expander section, zero the 4th column on every row Grid.
        sections = []
        for nm in ("exp_view", "exp_vg", "exp_graphic",
                   "exp_view_adv", "exp_plan"):
            try:
                sections.append(getattr(self, nm))
            except AttributeError:
                pass
        for section in sections:
            try:
                inner = section.Content
                if inner is None:
                    continue
                for child in inner.Children:
                    if not isinstance(child, Border):
                        continue
                    grid = child.Child
                    if not isinstance(grid, Grid):
                        continue
                    if grid.ColumnDefinitions.Count >= 4:
                        grid.ColumnDefinitions[3].Width = self._grid_length(0)
            except Exception:
                pass
        # Hide every chk_app_* checkbox (no longer in use)
        for nm in [a for a in dir(self) if a.startswith("chk_app_")]:
            try:
                getattr(self, nm).Visibility = Visibility.Collapsed
            except Exception:
                pass

    # ---- populate value combos -------------------------------------------

    def _populate_combos(self):
        # Iter-2 wired rows: use spec system (label, value tuples) so we can
        # round-trip the API value back to the UI on load and read it back
        # on Apply.
        for spec in self._param_specs:
            try:
                combo = getattr(self, spec.combo_attr)
            except AttributeError:
                continue
            _populate_combo_kv(combo, spec.options_provider())
            if combo.Items.Count > 0:
                combo.SelectedIndex = 0
            # Make Include checkbox 3-state so we can show "(varies)" via
            # the indeterminate state when bulk templates differ on Include.
            try:
                inc_chk = getattr(self, spec.inc_chk_attr)
                inc_chk.IsThreeState = True
                inc_chk.IsChecked = True
            except AttributeError:
                pass

        # Mock-only rows still using simple string lists (Color Scheme, Color
        # Scheme Location, Depth Clipping). Wired in a later iteration.
        def fill(combo, items, default_idx=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default_idx, combo.Items.Count - 1)
        fill(self.cmb_color_loc,     MOCK_COLOR_LOC,     default_idx=0)
        fill(self.cmb_color_scheme,  MOCK_COLOR_SCHEMES, default_idx=0)
        fill(self.cmb_depth_clip,    MOCK_DEPTH_CLIP,    default_idx=0)

        # Include-only checkboxes (rows whose value editor is preview-only
        # but whose Include is wired). Same 3-state setup as the simple-
        # param Include checkboxes so we can render '(varies)' across bulk.
        for spec in self._include_only_specs:
            try:
                chk = getattr(self, spec.chk_attr)
                chk.IsThreeState = True
                chk.IsChecked = True
            except Exception:
                pass

        # Initial scale value derived display
        self.txt_scale_value.Text = "96"

    # ---- type chips ------------------------------------------------------

    def _build_chips(self):
        self._chip_buttons.clear()
        self.pnl_tpl_chips.Children.Clear()
        for label in ("All", "Plan", "Section", "3D", "Other"):
            btn = ToggleButton()
            btn.Content   = label
            btn.Style     = self.Resources["ChipButton"]
            btn.IsChecked = (label == "All")
            btn.Cursor    = Cursors.Hand
            btn.Click    += self._on_chip_click
            self._chip_buttons[label] = btn
            self.pnl_tpl_chips.Children.Add(btn)

    def _on_chip_click(self, sender, args):
        # Make this chip exclusive
        for label, btn in self._chip_buttons.items():
            if btn is sender:
                btn.IsChecked = True
                self._chip_filter = label
            else:
                btn.IsChecked = False
        # Re-render visible rows; persisted _checked_eids keeps prior
        # selections alive so checked templates that are filtered out of
        # view stay selected and reappear with their checkmark when the
        # chip switches back.
        self._render_template_list()
        self._update_mode()

    # ---- wire events -----------------------------------------------------

    def _wire_events(self):
        self.btn_close.Click  += self._on_close
        self.btn_apply.Click  += self._on_apply

        self.btn_tpl_refresh.Click += self._on_tpl_refresh
        self.btn_tpl_all.Click     += self._on_tpl_all
        self.btn_tpl_none.Click    += self._on_tpl_none
        self.txt_tpl_search.TextChanged += self._on_tpl_search

        self.chk_show_analytical.Click += self._on_toggle_analytical

        # V/G Edit buttons - launch sub-dialogs. The RVT Links row is now
        # informational only (no Edit button) — see ViewTemplatesManagerForm.xaml.
        self.btn_vg_model.Click      += lambda s, e: self._open_vg_categories("Model")
        self.btn_vg_annotation.Click += lambda s, e: self._open_vg_categories("Annotation")
        self.btn_vg_analytical.Click += lambda s, e: self._open_vg_categories("Analytical")
        self.btn_vg_import.Click     += lambda s, e: self._open_vg_imports()
        self.btn_vg_filters.Click    += lambda s, e: self._open_vg_filters()

        # Graphic Display, View Range, and System Color Schemes rows are
        # informational only — they delegate to Revit's native View Templates
        # dialog (see ViewTemplatesManagerForm.xaml). No buttons to wire here.

        # When scale combo changes, update Scale Value: 1: derived
        self.cmb_scale.SelectionChanged += self._on_scale_changed

    def _on_scale_changed(self, sender, args):
        # Scale Value 1: shows the integer denominator of the selected scale
        # (or '-' for varies / nothing selected).
        val = _selected_combo_value(self.cmb_scale)
        if val is None or val is _VARIES:
            self.txt_scale_value.Text = "-"
        else:
            self.txt_scale_value.Text = str(val)

    # ---- footer ----------------------------------------------------------

    def _on_close(self, sender, args):
        self.Close()

    def _on_apply(self, sender, args):
        """Iter 2: write the spec'd rows and their Include flags to every
        checked template inside one Revit Transaction. Whatever value is
        currently shown in each combo gets written; rows still showing the
        '(varies)' marker are skipped (user didn't pick a real value).
        Same rule applies in single-edit and bulk: there is no per-row
        gate — Apply Changes writes everything that has a real value."""
        checked = [it.view for it in self._checked()]
        n = len(checked)
        if n == 0:
            return

        # plan: list of (spec, value_or_None, include_state_or_None). For
        # include-only specs (V/G Overrides, etc.) value is always None;
        # we still pipe them through the same plan so the Include-flag
        # write loop picks them up.
        plan = []
        for spec in self._param_specs:
            try:
                combo = getattr(self, spec.combo_attr)
            except AttributeError:
                continue
            value = _selected_combo_value(combo)
            if value is _VARIES:
                # Templates differ on this row and the user didn't pick a
                # replacement — leave each template's existing value alone.
                value = None
            try:
                inc_chk = getattr(self, spec.inc_chk_attr)
                inc_state = inc_chk.IsChecked   # True / False / None (indeterminate)
            except AttributeError:
                inc_state = None
            plan.append((spec, value, inc_state))
        # Include-only rows (V/G Overrides Model / Annotation / Analytical /
        # Import / Filters / Links + Graphic Display rows + a few preview
        # rows). These have no value writer; only the Include flag matters.
        for ispec in self._include_only_specs:
            try:
                inc_chk = getattr(self, ispec.chk_attr)
                inc_state = inc_chk.IsChecked
            except AttributeError:
                continue
            plan.append((ispec, None, inc_state))

        if not plan:
            self.txt_status.Text = "Nothing to apply."
            return

        # One Revit transaction wraps every template + every spec.
        from Autodesk.Revit.DB import Transaction
        successes = 0
        failures = 0
        affected = set()
        t = Transaction(doc, "Apply view template properties")
        last_error = None
        try:
            t.Start()
            for tpl in checked:
                # 1. Value writes (each writer handles its own exceptions
                # and just returns False on failure, so they can't roll
                # back the transaction).
                for spec, value, _inc in plan:
                    if value is None:
                        continue
                    try:
                        if spec.writer(tpl, value):
                            successes += 1
                            affected.add(eid_int(tpl.Id))
                        else:
                            # Writer returned False — not really a failure
                            # for our purposes (param read-only or missing
                            # on this template). Don't surface.
                            pass
                    except Exception as ex:
                        failures += 1
                        last_error = ex
                # 2. Include flag writes — wrap the whole call so any
                # SetNonControlledTemplateParameterIds quirk does not
                # roll back the value writes we just made.
                try:
                    self._apply_include_changes(tpl, plan)
                except Exception as ex:
                    failures += 1
                    last_error = ex
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            dbhms_ui.info(
                "Apply Changes hit an error and was rolled back:\n\n{0}\n\n"
                "If this keeps happening, tell Claude which row(s) you were "
                "editing and we'll narrow it down.".format(str(ex)),
                title="View Templates Manager - Apply failed")
            return

        tpl_word = "template" if len(affected) == 1 else "templates"
        chg_word = "change" if successes == 1 else "changes"
        if successes == 0 and failures == 0:
            self.txt_status.Text = (
                "Nothing was written — values were unchanged or the rows "
                "didn't apply to the selected template(s).")
        elif successes == 0 and failures > 0 and last_error is not None:
            dbhms_ui.info(
                "Apply Changes wrote 0 values; {0} attempts failed.\n\n"
                "Last error:\n{1}".format(failures, str(last_error)),
                title="View Templates Manager - Apply failed")
            self.txt_status.Text = "Apply failed - see message."
        else:
            msg = "Applied {0} {1} across {2} {3}.".format(
                successes, chg_word, len(affected), tpl_word)
            if failures:
                msg += " ({0} also failed.)".format(failures)
            self.txt_status.Text = msg

        # Refresh UI to reflect what actually got written
        self._load_values(checked)

    def _apply_include_changes(self, template, plan):
        """For each spec in `plan`, update template.GetNonControlledTemplate-
        ParameterIds based on the desired include state. Indeterminate state
        (None) leaves the template's flag untouched. Writes back only if at
        least one flag actually changed."""
        try:
            current_non_ctrl = list(template.GetNonControlledTemplateParameterIds())
        except Exception:
            return
        non_ctrl_ints = set(eid_int(e) for e in current_non_ctrl)
        changed = False
        for spec, _value, inc_state in plan:
            if inc_state is None:
                continue   # indeterminate → don't touch
            try:
                p = template.get_Parameter(spec.bip)
                if p is None:
                    continue
                pid_int = eid_int(p.Id)
            except Exception:
                continue
            should_be_excluded = not bool(inc_state)
            currently_excluded = pid_int in non_ctrl_ints
            if should_be_excluded == currently_excluded:
                continue
            if should_be_excluded:
                non_ctrl_ints.add(pid_int)
            else:
                non_ctrl_ints.discard(pid_int)
            changed = True
        if not changed:
            return
        try:
            from System.Collections.Generic import List
            from Autodesk.Revit.DB import ElementId
            new_list = List[ElementId]()
            for v in non_ctrl_ints:
                new_list.Add(make_eid(v))
            template.SetNonControlledTemplateParameterIds(new_list)
        except Exception:
            pass

    # ---- iter-2: load real template values into the parameter table ------

    def _load_values(self, templates):
        """Read each iter-2 spec from `templates` and reflect it in the UI.
        - 1 template:  combo selects the actual value, Include checkbox
                       reflects whether the template enforces it.
        - 2+ templates: '(varies)' marker if values differ; Include checkbox
                       goes indeterminate if templates disagree on enforcement.
        Combos for params no template exposes are left at their default."""
        if not templates:
            return
        for spec in self._param_specs:
            try:
                combo = getattr(self, spec.combo_attr)
            except AttributeError:
                continue

            # Phase Filter options come from the project, refresh on each
            # load to pick up filters added/removed since the form opened.
            if spec.options_provider is _options_phase_filter:
                current_val = _selected_combo_value(combo)
                _populate_combo_kv(combo, spec.options_provider())
                if current_val not in (None, _VARIES):
                    _select_combo_by_value(combo, current_val)
            else:
                _remove_varies_marker(combo)

            values = []
            for tpl in templates:
                try:
                    values.append(spec.reader(tpl))
                except Exception:
                    values.append(None)
            readable = [v for v in values if v is not None]
            if readable:
                unique = set(readable)
                if len(unique) == 1:
                    _select_combo_by_value(combo, next(iter(unique)))
                else:
                    _set_combo_varies(combo)

            # Include checkbox state
            try:
                inc_chk = getattr(self, spec.inc_chk_attr)
            except AttributeError:
                inc_chk = None
            if inc_chk is not None:
                states = []
                for tpl in templates:
                    try:
                        p = tpl.get_Parameter(spec.bip)
                        if p is None:
                            continue
                        states.append(is_template_param_included(tpl, p.Id))
                    except Exception:
                        continue
                if not states:
                    pass
                elif all(states):
                    inc_chk.IsChecked = True
                elif not any(states):
                    inc_chk.IsChecked = False
                else:
                    inc_chk.IsThreeState = True
                    inc_chk.IsChecked = None
        # Same Include-state load for the preview / informational rows
        # (V/G Overrides, Graphic Display, etc.) so unchecking those
        # actually drives SetNonControlledTemplateParameterIds.
        for spec in self._include_only_specs:
            try:
                inc_chk = getattr(self, spec.chk_attr)
            except AttributeError:
                continue
            states = []
            for tpl in templates:
                try:
                    p = tpl.get_Parameter(spec.bip)
                    if p is None:
                        continue
                    states.append(is_template_param_included(tpl, p.Id))
                except Exception:
                    continue
            if not states:
                continue
            if all(states):
                inc_chk.IsChecked = True
            elif not any(states):
                inc_chk.IsChecked = False
            else:
                inc_chk.IsThreeState = True
                inc_chk.IsChecked = None
        # Keep the Scale Value: 1: cell in sync with whatever's now selected
        self._on_scale_changed(None, None)

    # ---- left-side template list ----------------------------------------

    def _on_tpl_refresh(self, sender, args):
        self._load_templates()
        self._render_template_list()
        self._update_mode()

    def _on_tpl_all(self, sender, args):
        # Check every currently-visible template (respecting chip + search)
        for it in self._visible_items():
            self._checked_eids.add(it.eid_int)
            if it.checkbox is not None:
                it.checkbox.IsChecked = True
        self._update_mode()

    def _on_tpl_none(self, sender, args):
        # Uncheck everything regardless of visibility
        self._checked_eids.clear()
        for it in self._all_templates:
            if it.checkbox is not None:
                it.checkbox.IsChecked = False
        self._update_mode()

    def _on_tpl_search(self, sender, args):
        self._search_text = (self.txt_tpl_search.Text or "").strip().lower()
        self._render_template_list()
        self._update_mode()

    def _on_toggle_analytical(self, sender, args):
        if self.chk_show_analytical.IsChecked:
            self.row_vg_analytical.Visibility = Visibility.Visible
        else:
            self.row_vg_analytical.Visibility = Visibility.Collapsed

    def _load_templates(self):
        usage = build_usage_map(doc)
        items = []
        for v in get_all_templates(doc):
            items.append(TemplateItem(v, usage.get(eid_int(v.Id), 0)))
        self._all_templates = items

    def _matches_chip(self, item):
        if self._chip_filter == "All":
            return True
        if self._chip_filter == "Plan":
            return is_plan_view_type(item.view_type)
        if self._chip_filter == "Section":
            return item.view_type in (ViewType.Section, ViewType.Elevation,
                                      ViewType.Detail)
        if self._chip_filter == "3D":
            return item.view_type == ViewType.ThreeD
        if self._chip_filter == "Other":
            return (item.view_type not in _PLAN_VIEW_TYPES and
                    item.view_type not in (ViewType.Section, ViewType.Elevation,
                                           ViewType.Detail, ViewType.ThreeD))
        return True

    def _matches_search(self, item):
        if not self._search_text:
            return True
        return self._search_text in item.name.lower()

    def _visible_items(self):
        return [it for it in self._all_templates
                if self._matches_chip(it) and self._matches_search(it)]

    def _render_template_list(self):
        self.pnl_tpl_list.Children.Clear()
        # Reset row_border refs on items that won't be visible this pass —
        # _render_highlights() skips items with row_border None, so we
        # can't accidentally repaint a recycled UI element.
        for it in self._all_templates:
            it.row_border = None
        visible = self._visible_items()
        for item in visible:
            row = self._build_template_row(item)
            self.pnl_tpl_list.Children.Add(row)
            item.row_border = row
        self.txt_tpl_summary.Text = (
            "{0} templates shown (of {1}). Click to highlight, shift/ctrl for "
            "multi-select. Tick a checkbox in a highlighted row to bulk-check."
            .format(len(visible), len(self._all_templates))
        )
        # Reapply highlights to whatever rows are now visible
        self._render_highlights()

    def _build_template_row(self, item):
        outer = Border()
        outer.Padding = Thickness(6, 4, 6, 4)
        outer.Margin  = Thickness(0, 0, 0, 4)
        outer.BorderBrush     = SolidColorBrush(Color.FromRgb(0xE2, 0xE8, 0xF0))
        outer.BorderThickness = Thickness(1)
        outer.CornerRadius    = self._zero_radius(3)
        outer.Background      = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
        outer.Cursor          = Cursors.Hand
        outer.Tag             = item
        outer.MouseLeftButtonDown += self._on_row_mouse_down

        grid = Grid()
        c0 = ColumnDefinition(); c0.Width = self._grid_length(28)
        c1 = ColumnDefinition(); c1.Width = self._grid_length_star()
        c2 = ColumnDefinition(); c2.Width = self._grid_length(36)
        for c in (c0, c1, c2):
            grid.ColumnDefinitions.Add(c)

        chk = CheckBox()
        # Restore from the persisted set so chip-filter / search refreshes
        # don't visually un-check rows the user has already chosen.
        chk.IsChecked = item.eid_int in self._checked_eids
        chk.VerticalAlignment   = VerticalAlignment.Center
        chk.HorizontalAlignment = HorizontalAlignment.Center
        chk.Tag = item
        chk.Click += self._on_template_check
        Grid.SetColumn(chk, 0)
        grid.Children.Add(chk)
        item.checkbox = chk

        # Center column: name + sub-line
        body = StackPanel()
        body.Orientation = Orientation.Vertical
        Grid.SetColumn(body, 1)

        name_block = TextBlock()
        name_block.Text       = item.name
        name_block.FontSize   = 12
        name_block.FontWeight = self._semi_bold()
        name_block.Foreground = SolidColorBrush(Color.FromRgb(0x1A, 0x20, 0x2C))
        name_block.TextTrimming = self._trim_char_ellipsis()
        body.Children.Add(name_block)

        sub_block = TextBlock()
        sub_block.Text       = "{0}  -  {1}  -  used by {2}".format(
            item.view_type_str, item.scale_str, item.usage_count
        )
        sub_block.FontSize   = 10
        sub_block.Foreground = SolidColorBrush(Color.FromRgb(0x71, 0x80, 0x96))
        sub_block.Margin     = Thickness(0, 1, 0, 0)
        body.Children.Add(sub_block)

        grid.Children.Add(body)

        # Right column: usage badge
        usage_block = TextBlock()
        usage_block.Text = str(item.usage_count)
        usage_block.FontSize = 11
        usage_block.FontWeight = self._semi_bold()
        usage_block.Foreground = SolidColorBrush(Color.FromRgb(0x4A, 0x55, 0x68))
        usage_block.HorizontalAlignment = HorizontalAlignment.Right
        usage_block.VerticalAlignment   = VerticalAlignment.Center
        Grid.SetColumn(usage_block, 2)
        grid.Children.Add(usage_block)

        outer.Child = grid
        return outer

    def _on_row_mouse_down(self, sender, args):
        """Row body click: manages the highlight set with shift/ctrl semantics.
        WPF's CheckBox handles its own mouse-down, so a click on the row's
        checkbox does not trigger this handler (the event is marked handled
        before it bubbles up to the Border)."""
        item = getattr(sender, "Tag", None)
        if item is None or not isinstance(item, TemplateItem):
            return
        from System.Windows.Input import Keyboard, ModifierKeys
        # Bitwise AND for IronPython enum-flag compatibility
        shift = bool(int(Keyboard.Modifiers) & int(ModifierKeys.Shift))
        ctrl  = bool(int(Keyboard.Modifiers) & int(ModifierKeys.Control))

        visible = self._visible_items()

        if shift and self._last_clicked_eid is not None:
            last_idx = None
            this_idx = None
            for i, it in enumerate(visible):
                if it.eid_int == self._last_clicked_eid:
                    last_idx = i
                if it.eid_int == item.eid_int:
                    this_idx = i
            if last_idx is not None and this_idx is not None:
                lo, hi = min(last_idx, this_idx), max(last_idx, this_idx)
                self._highlighted_eids = set(it.eid_int for it in visible[lo:hi+1])
            else:
                self._highlighted_eids = {item.eid_int}
                self._last_clicked_eid = item.eid_int
        elif ctrl:
            if item.eid_int in self._highlighted_eids:
                self._highlighted_eids.discard(item.eid_int)
            else:
                self._highlighted_eids.add(item.eid_int)
            self._last_clicked_eid = item.eid_int
        else:
            self._highlighted_eids = {item.eid_int}
            self._last_clicked_eid = item.eid_int

        self._render_highlights()

    def _render_highlights(self):
        hl_bg     = SolidColorBrush(Color.FromRgb(0xEB, 0xF8, 0xFF))
        hl_border = SolidColorBrush(Color.FromRgb(0x31, 0x82, 0xCE))
        normal_bg     = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
        normal_border = SolidColorBrush(Color.FromRgb(0xE2, 0xE8, 0xF0))
        for it in self._all_templates:
            if it.row_border is None:
                continue
            if it.eid_int in self._highlighted_eids:
                it.row_border.Background  = hl_bg
                it.row_border.BorderBrush = hl_border
            else:
                it.row_border.Background  = normal_bg
                it.row_border.BorderBrush = normal_border

    def _on_template_check(self, sender, args):
        """Checkbox toggle: write the new state into the persisted set so
        the selection survives chip-filter and search re-renders. If the
        checkbox's row is part of a multi-row highlight set, propagate the
        new state to every highlighted row in the set as well."""
        target_item = getattr(sender, "Tag", None)
        if target_item is None or not isinstance(target_item, TemplateItem):
            self._update_mode()
            return

        new_state = bool(sender.IsChecked)
        if new_state:
            self._checked_eids.add(target_item.eid_int)
        else:
            self._checked_eids.discard(target_item.eid_int)

        # Multi-row propagation: if 2+ rows are highlighted and the clicked
        # row is one of them, push the new check state to the rest of the
        # highlight set too. Items that aren't currently visible (filtered
        # out by chip / search) still get their persisted state updated.
        if (target_item.eid_int in self._highlighted_eids and
                len(self._highlighted_eids) > 1):
            for it in self._all_templates:
                if it.eid_int == target_item.eid_int:
                    continue
                if it.eid_int not in self._highlighted_eids:
                    continue
                if new_state:
                    self._checked_eids.add(it.eid_int)
                else:
                    self._checked_eids.discard(it.eid_int)
                if it.checkbox is not None:
                    it.checkbox.IsChecked = new_state

        self._update_mode()

    # ---- single / bulk mode plumbing ------------------------------------

    def _checked(self):
        # Source of truth is _checked_eids (persistent across renders);
        # widgets are just the current mirror of that set.
        return [it for it in self._all_templates
                if it.eid_int in self._checked_eids]

    def _update_mode(self):
        n = len(self._checked())
        # Apply button is live whenever at least 1 template is checked.
        try:
            self.btn_apply.IsEnabled = (n >= 1)
        except Exception:
            pass
        if n == 0:
            self.bnr_bulk.Visibility = Visibility.Hidden
            self.txt_right_title.Text   = "Select a template on the left"
            self.txt_right_subtitle.Text = "Edit one template, or check 2+ to bulk-edit."
            self.txt_views_using.Text   = "Number of views with this template assigned: 0"
            self._set_param_table_enabled(False)
        elif n == 1:
            self.bnr_bulk.Visibility = Visibility.Hidden
            it = self._checked()[0]
            self.txt_right_title.Text   = it.name
            self.txt_right_subtitle.Text = "{0}  -  {1}".format(
                it.view_type_str, it.scale_str
            )
            self.txt_views_using.Text = (
                "Number of views with this template assigned: {0}".format(it.usage_count)
            )
            self._set_param_table_enabled(True)
            self._toggle_plan_section(is_plan_view_type(it.view_type))
            # Iter-2: read the template's actual values into the spec'd combos.
            self._load_values([it.view])
        else:
            checked = self._checked()
            self.bnr_bulk.Visibility = Visibility.Visible
            self.txt_bulk_banner.Text = (
                "Bulk mode: editing {0} templates - the values shown will be "
                "written to all of them when you click Apply Changes. "
                "'(varies)' rows are skipped.".format(n)
            )
            self.txt_right_title.Text = "Bulk edit ({0} templates)".format(n)
            self.txt_right_subtitle.Text = (
                "Combos show common values, or '(varies)' when templates "
                "differ. Pick any value to overwrite all checked templates "
                "with it; leave '(varies)' alone to keep each template's "
                "current value.")
            total_usage = sum(it.usage_count for it in checked)
            self.txt_views_using.Text = "Total views affected: {0}".format(total_usage)
            self._set_param_table_enabled(True)
            self._toggle_plan_section(any(is_plan_view_type(it.view_type) for it in checked))
            # Iter-2: read each template, mark "(varies)" where they differ.
            self._load_values([it.view for it in checked])

    def _set_param_table_enabled(self, enabled):
        self.scr_params.IsEnabled = bool(enabled)
        opacity = 1.0 if enabled else 0.55
        self.scr_params.Opacity = opacity

    def _toggle_plan_section(self, is_plan):
        try:
            self.exp_plan.Visibility = Visibility.Visible if is_plan else Visibility.Collapsed
        except Exception:
            pass

    # ---- sub-dialog launchers -------------------------------------------

    def _selected_template_names(self):
        return [it.name for it in self._checked()]

    def _open_vg_categories(self, kind):
        checked = self._checked()
        if not checked:
            dbhms_ui.info("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        templates = [it.view for it in checked]
        names = [it.name for it in checked]
        dlg = VgCategoriesDialog(kind=kind, templates=templates, template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

    def _open_vg_imports(self):
        checked = self._checked()
        if not checked:
            dbhms_ui.info("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        templates = [it.view for it in checked]
        names = [it.name for it in checked]
        dlg = VgImportsDialog(templates=templates, template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

    def _open_vg_filters(self):
        names = self._selected_template_names()
        if not names:
            dbhms_ui.info("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        dlg = VgFiltersDialog(template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

    # _open_vg_links removed — the V/G Overrides RVT Links row is now an
    # informational note rather than a clickable Edit button. The dialog
    # stayed in script.py / VgLinksDialog.xaml so we can resurrect it if a
    # workflow shows up that needs the mode toggle or linked-view picker.


    # ---- WPF helpers (IronPython sometimes can't infer overloads) -------

    def _grid_length(self, px):
        from System.Windows import GridLength
        return GridLength(float(px))

    def _grid_length_star(self):
        from System.Windows import GridLength, GridUnitType
        return GridLength(1.0, GridUnitType.Star)

    def _zero_radius(self, r):
        from System.Windows import CornerRadius
        return CornerRadius(float(r))

    def _semi_bold(self):
        from System.Windows import FontWeights
        return FontWeights.SemiBold

    def _trim_char_ellipsis(self):
        from System.Windows import TextTrimming
        return TextTrimming.CharacterEllipsis


# ===========================================================================
# SUB-DIALOG: V/G Categories (Model / Annotation / Analytical)
# ===========================================================================

class VgCategoriesDialog(forms.WPFWindow):
    def __init__(self, kind="Model", templates=None, template_names=None):
        forms.WPFWindow.__init__(self, VG_CAT_XAML)
        self._kind  = kind
        self._templates = list(templates or [])
        self._names = list(template_names or [it.Name for it in self._templates])
        self._cat_rows = []           # list of {row, chk_show, name, cat_id, initial}
        self._selected_name = None
        # Names of categories currently in the multi-row highlight set on
        # the left list. Initialized empty so OK works even if the user
        # never clicks a category row before clicking OK.
        self._selected_names = []
        # Iter-3b OGS state:
        # _loading_ogs: suppresses dirty marking while the panel programmatically
        #   loads values from a category (so a load doesn't look like a user edit)
        # _dirty_fields: set of OGS field names the user has actually changed
        #   since the last category-selection. Only dirty fields write on OK.
        self._loading_ogs = False
        self._dirty_fields = set()
        self._populate_title()
        self._populate_lookups()
        self._populate_categories()
        self._wire_events()
        _set_legend_mixed(self)

    def _populate_title(self):
        if self._kind == "Annotation":
            self.txt_dialog_title.Text = "V/G Overrides - Annotation Categories"
            self.txt_dialog_sub.Text = (
                "Toggle annotation category visibility per template; tick a "
                "Show box in a highlighted row to bulk-toggle.")
        elif self._kind == "Analytical":
            self.txt_dialog_title.Text = "V/G Overrides - Analytical Model Categories"
            self.txt_dialog_sub.Text = (
                "Analytical category visibility - typically only useful for "
                "structural workflows.")
        else:
            self.txt_dialog_title.Text = "V/G Overrides - Model Categories"
            self.txt_dialog_sub.Text = (
                "Toggle model category visibility per template; tick a Show "
                "box in a highlighted row to bulk-toggle.")
        # Footer status — concise. The footer Grid trims long text with
        # ellipsis if it ever gets longer than the available column.
        if len(self._names) > 1:
            self.txt_dialog_status.Text = (
                "OK applies changes to {0} templates.".format(len(self._names)))
        elif self._names:
            self.txt_dialog_status.Text = (
                "OK applies changes to template: {0}".format(self._names[0]))
        # OK button is now live for iter-3a (visibility writes)
        try:
            self.btn_dlg_ok.IsEnabled = True
            self.btn_dlg_ok.ToolTip = (
                "Apply visibility changes to the selected template(s). "
                "Right-panel override controls (color/weight/pattern) are "
                "still preview-only — those wire up in iter 3b.")
        except Exception:
            pass

    def _populate_lookups(self):
        # Iter-3c: real options for line weight, line pattern, detail level
        line_patterns = get_line_patterns(doc)
        _populate_combo_kv(self.cmb_detail_level,  _OGS_DETAIL_LEVEL_OPTIONS)
        _populate_combo_kv(self.cmb_proj_weight,   _LINE_WEIGHT_OPTIONS)
        _populate_combo_kv(self.cmb_proj_pattern,  line_patterns)
        _populate_combo_kv(self.cmb_cut_weight,    _LINE_WEIGHT_OPTIONS)
        _populate_combo_kv(self.cmb_cut_pattern,   line_patterns)
        # Surface and cut foreground/background patterns are not yet wired —
        # leave them populated with their mock options so the UI doesn't
        # render empty.
        def fill(combo, items, default=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default, combo.Items.Count - 1)
        fill(self.cmb_surf_fg_pattern, MOCK_PATTERNS)
        fill(self.cmb_surf_bg_pattern, MOCK_PATTERNS)
        fill(self.cmb_cut_fg_pattern,  MOCK_PATTERNS)
        fill(self.cmb_cut_bg_pattern,  MOCK_PATTERNS)

        # Halftone + transparency
        self.sld_transparency.ValueChanged += self._on_transparency
        self.chk_cat_halftone.Click        += self._on_halftone_clicked

        # Wire each OGS line-control change to its dirty key. Combos use
        # SelectionChanged; color buttons open the picker and write back.
        self.cmb_proj_weight.SelectionChanged += self._on_proj_weight_changed
        self.cmb_proj_pattern.SelectionChanged += self._on_proj_pattern_changed
        self.btn_proj_color.Click             += self._on_proj_color_clicked
        self.cmb_cut_weight.SelectionChanged  += self._on_cut_weight_changed
        self.cmb_cut_pattern.SelectionChanged += self._on_cut_pattern_changed
        self.btn_cut_color.Click              += self._on_cut_color_clicked
        self.cmb_detail_level.SelectionChanged += self._on_detail_level_changed

        # Color buttons hold their current (r,g,b) tuple in .Tag so apply
        # can read them straight off without parsing the swatch back.
        self.btn_proj_color.Tag = None
        self.btn_cut_color.Tag  = None

    def _on_transparency(self, sender, args):
        self.txt_transparency_val.Text = "{0}%".format(int(self.sld_transparency.Value))
        if not self._loading_ogs:
            self._dirty_fields.add("transparency")

    def _on_halftone_clicked(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("halftone")

    def _on_proj_weight_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("proj_weight")

    def _on_proj_pattern_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("proj_pattern")

    def _on_cut_weight_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("cut_weight")

    def _on_cut_pattern_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("cut_pattern")

    def _on_detail_level_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("detail_level")

    def _on_proj_color_clicked(self, sender, args):
        rgb = pick_color(self.btn_proj_color.Tag)
        if rgb is None:
            return
        self.btn_proj_color.Tag = rgb
        _set_swatch_color(self.sw_proj_color, self.txt_proj_color, rgb)
        self._dirty_fields.add("proj_color")

    def _on_cut_color_clicked(self, sender, args):
        rgb = pick_color(self.btn_cut_color.Tag)
        if rgb is None:
            return
        self.btn_cut_color.Tag = rgb
        _set_swatch_color(self.sw_cut_color, self.txt_cut_color, rgb)
        self._dirty_fields.add("cut_color")

    def _list_categories(self):
        """Return [(name, Category), ...] for this dialog's kind, pulled
        live from the project."""
        if self._kind == "Annotation":
            return get_annotation_categories(doc)
        if self._kind == "Analytical":
            return get_analytical_categories(doc)
        return get_model_categories(doc)

    def _populate_categories(self):
        cats = self._list_categories()
        self.pnl_categories.Children.Clear()
        self._cat_rows = []
        for cat_name, cat in cats:
            info = _build_category_row(cat_name)
            # Make the Show checkbox 3-state so we can show '(varies)' when
            # bulk-loaded templates disagree on this category's visibility.
            try:
                info["chk_show"].IsThreeState = True
            except Exception:
                pass
            # Read initial visibility consensus across the template set
            initial = read_category_visible_bulk(self._templates, cat.Id)
            info["chk_show"].IsChecked = initial
            info["cat_id"]   = cat.Id
            info["category"] = cat
            info["initial"]  = initial
            self.pnl_categories.Children.Add(info["row"])
            self._cat_rows.append(info)
        # Shift/ctrl-click multi-select; ticking the Show checkbox in any
        # highlighted row propagates the new state to all highlighted rows.
        self._select_helper = RowMultiSelectHelper(
            self._cat_rows, on_select=self._on_cat_select)
        # Halftone is 3-state: programmatic 'mixed consensus' shows up as
        # indeterminate. The user can still cycle the checkbox; at OK we
        # treat None (indeterminate) as 'no change'.
        try:
            self.chk_cat_halftone.IsThreeState = True
        except Exception:
            pass
        # Trigger an initial OGS load so the right panel shows the project's
        # current consensus across all categories (not bare defaults).
        self._on_cat_select([])

    def _on_cat_select(self, names):
        self._selected_names = list(names)
        if not names:
            # Empty highlight — header explicitly says edits apply to all
            self.txt_selected_cat.Text = (
                "Nothing selected — edits apply to all {0} categories"
                .format(len(self._cat_rows)))
            self.pnl_selected_names.Children.Clear()
        else:
            _render_multi_select(
                self.txt_selected_cat, self.pnl_selected_names, names,
                "categories",
                on_show_all=lambda nms: _show_full_name_list("Selected categories", nms))
        # Load consensus OGS state into the right-panel controls. Empty
        # highlight = read across every row in the dialog (matches the
        # all-rows fallback in _on_dlg_ok). Switching selection discards
        # any unapplied OGS edits — matches Revit's native dialog.
        self._load_ogs_panel(names)

    def _load_ogs_panel(self, scope_names):
        """Populate every right-panel OGS control from the consensus state
        across the rows in scope. Empty scope = read across every row.
        Bool/int fields take the first encountered value (or 'mixed' for
        the halftone 3-state). Selection-style fields take the first too."""
        self._loading_ogs = True
        try:
            self._dirty_fields.clear()
            if scope_names:
                want = set(scope_names)
                scope = [info for info in self._cat_rows
                         if info.get("name") in want]
            else:
                scope = list(self._cat_rows)
            if not scope or not self._templates:
                self._reset_ogs_panel_to_defaults()
                return
            halftones = []
            first_transparency = None
            first_proj_weight  = None
            first_proj_pattern = None
            first_proj_color   = None
            first_cut_weight   = None
            first_cut_pattern  = None
            first_cut_color    = None
            first_detail       = None
            for tpl in self._templates:
                for info in scope:
                    cid = info.get("cat_id")
                    if cid is None:
                        continue
                    try:
                        ogs = tpl.GetCategoryOverrides(cid)
                        if ogs is None:
                            continue
                    except Exception:
                        continue
                    try:
                        halftones.append(bool(ogs.Halftone))
                    except Exception:
                        pass
                    if first_transparency is None:
                        try:
                            first_transparency = int(ogs.Transparency)
                        except Exception:
                            first_transparency = 0
                    if first_proj_weight is None:
                        try:
                            first_proj_weight = int(ogs.ProjectionLineWeight)
                        except Exception:
                            first_proj_weight = -1
                    if first_proj_pattern is None:
                        try:
                            first_proj_pattern = eid_int(ogs.ProjectionLinePatternId)
                        except Exception:
                            first_proj_pattern = -1
                    if first_proj_color is None:
                        try:
                            first_proj_color = _ogs_color_to_rgb(ogs.ProjectionLineColor)
                        except Exception:
                            first_proj_color = None
                    if first_cut_weight is None:
                        try:
                            first_cut_weight = int(ogs.CutLineWeight)
                        except Exception:
                            first_cut_weight = -1
                    if first_cut_pattern is None:
                        try:
                            first_cut_pattern = eid_int(ogs.CutLinePatternId)
                        except Exception:
                            first_cut_pattern = -1
                    if first_cut_color is None:
                        try:
                            first_cut_color = _ogs_color_to_rgb(ogs.CutLineColor)
                        except Exception:
                            first_cut_color = None
                    if first_detail is None:
                        try:
                            first_detail = int(ogs.DetailLevel)
                        except Exception:
                            first_detail = 0
            # Halftone — 3-state checkbox
            if not halftones:
                self.chk_cat_halftone.IsChecked = False
            elif all(halftones):
                self.chk_cat_halftone.IsChecked = True
            elif not any(halftones):
                self.chk_cat_halftone.IsChecked = False
            else:
                self.chk_cat_halftone.IsChecked = None
            # Transparency
            try:
                self.sld_transparency.Value = float(first_transparency or 0)
            except Exception:
                self.sld_transparency.Value = 0
            # Lines: weight + pattern combos
            _select_combo_by_value(self.cmb_proj_weight,  first_proj_weight if first_proj_weight is not None else -1)
            _select_combo_by_value(self.cmb_proj_pattern, first_proj_pattern if first_proj_pattern is not None else -1)
            _select_combo_by_value(self.cmb_cut_weight,   first_cut_weight if first_cut_weight is not None else -1)
            _select_combo_by_value(self.cmb_cut_pattern,  first_cut_pattern if first_cut_pattern is not None else -1)
            # Colors: button label + swatch
            self.btn_proj_color.Tag = first_proj_color
            _set_swatch_color(self.sw_proj_color, self.txt_proj_color, first_proj_color)
            self.btn_cut_color.Tag = first_cut_color
            _set_swatch_color(self.sw_cut_color, self.txt_cut_color, first_cut_color)
            # Detail level
            _select_combo_by_value(self.cmb_detail_level, first_detail if first_detail is not None else 0)
        finally:
            self._loading_ogs = False

    def _reset_ogs_panel_to_defaults(self):
        """When scope is empty (no templates) reset every right-panel
        control to neutral defaults so we don't leave stale values."""
        try:
            self.chk_cat_halftone.IsChecked = False
            self.sld_transparency.Value = 0
            _select_combo_by_value(self.cmb_proj_weight, -1)
            _select_combo_by_value(self.cmb_proj_pattern, -1)
            _select_combo_by_value(self.cmb_cut_weight, -1)
            _select_combo_by_value(self.cmb_cut_pattern, -1)
            _select_combo_by_value(self.cmb_detail_level, 0)
            self.btn_proj_color.Tag = None
            self.btn_cut_color.Tag  = None
            _set_swatch_color(self.sw_proj_color, self.txt_proj_color, None)
            _set_swatch_color(self.sw_cut_color,  self.txt_cut_color,  None)
        except Exception:
            pass

    def _wire_events(self):
        self.btn_dlg_cancel.Click += lambda s, e: self.Close()
        self.btn_dlg_ok.Click     += self._on_dlg_ok
        self.btn_cat_all.Click    += self._on_cat_all
        self.btn_cat_none.Click   += self._on_cat_none
        self.btn_cat_invert.Click += self._on_cat_invert
        self.btn_cat_reset.Click  += self._on_reset_overrides
        try:
            self.txt_cat_search.TextChanged += self._on_cat_search
        except Exception:
            pass

    def _on_cat_search(self, sender, args):
        query = (self.txt_cat_search.Text or "").strip().lower()
        for info in self._cat_rows:
            name = (info.get("name") or "").lower()
            match = (not query) or (query in name)
            info["row"].Visibility = Visibility.Visible if match else Visibility.Collapsed

    def _on_cat_all(self, sender, args):
        for info in self._cat_rows:
            info["chk_show"].IsChecked = True
        self._reset_highlight_for_global_scope()

    def _on_cat_none(self, sender, args):
        for info in self._cat_rows:
            info["chk_show"].IsChecked = False
        self._reset_highlight_for_global_scope()

    def _on_cat_invert(self, sender, args):
        for info in self._cat_rows:
            chk = info["chk_show"]
            # Toggle: True → False, False/None → True
            chk.IsChecked = not bool(chk.IsChecked)

    def _reset_highlight_for_global_scope(self):
        """Clear the multi-row highlight so subsequent OGS edits use the
        all-rows fallback in _on_dlg_ok. Called by All/None which imply
        a global scope."""
        try:
            self._select_helper.clear_highlight()
        except Exception:
            pass

    def _on_dlg_ok(self, sender, args):
        """Apply visibility changes (per-row Show checkbox) AND OGS overrides
        (Halftone / Transparency) to every template, all inside one Revit
        Transaction. Visibility writes only when a row's current state
        differs from what was loaded; OGS writes only if the user actually
        changed a control AND the rows are part of the current selection."""
        if not self._templates:
            self.Close()
            return

        # 1. Visibility plan — same as iter-3a
        vis_plan = []
        for info in self._cat_rows:
            chk = info["chk_show"]
            current = chk.IsChecked
            if current is None:
                continue
            if current == info["initial"]:
                continue
            vis_plan.append((info["cat_id"], bool(current), info["name"]))

        # 2. OGS plan — only if user changed at least one OGS field. Scope:
        #    if any rows are highlighted, only those get the override;
        #    otherwise (empty highlight) we fall back to applying to every
        #    row in the dialog. This matches users' expectation when they
        #    pick "All" then tick Halftone — apply to everything visible.
        ogs_targets = []
        if self._dirty_fields:
            target_names = set(self._selected_names) if self._selected_names \
                           else set(info["name"] for info in self._cat_rows)
            for info in self._cat_rows:
                if info.get("name") in target_names:
                    cid = info.get("cat_id")
                    if cid is not None:
                        ogs_targets.append((cid, info["name"]))

        if not vis_plan and not ogs_targets:
            self.Close()
            return

        # Snapshot all OGS edits before we cross into the Transaction so a
        # mid-flight WPF change can't influence what we write. Halftone may
        # be None (indeterminate) — in that case we skip writing halftone
        # so the user's "no decision" leaves each template's value alone.
        halftone_state   = self.chk_cat_halftone.IsChecked
        ogs_halftone     = bool(halftone_state) if halftone_state is not None else None
        ogs_transparency = int(self.sld_transparency.Value)
        ogs_proj_weight  = _selected_combo_value(self.cmb_proj_weight)
        ogs_proj_pattern = _selected_combo_value(self.cmb_proj_pattern)
        ogs_proj_color   = self.btn_proj_color.Tag   # (r,g,b) or None
        ogs_cut_weight   = _selected_combo_value(self.cmb_cut_weight)
        ogs_cut_pattern  = _selected_combo_value(self.cmb_cut_pattern)
        ogs_cut_color    = self.btn_cut_color.Tag
        ogs_detail_level = _selected_combo_value(self.cmb_detail_level)
        dirty = set(self._dirty_fields)
        if "halftone" in dirty and ogs_halftone is None:
            dirty.discard("halftone")

        from Autodesk.Revit.DB import Transaction
        successes = 0
        failures = 0
        affected = set()
        last_error = None
        t = Transaction(doc, "Apply V/G category overrides")
        opts = make_silent_failure_options(t)
        if opts is not None:
            try:
                t.SetFailureHandlingOptions(opts)
            except Exception:
                pass
        try:
            t.Start()
            for tpl in self._templates:
                # Visibility writes
                for cat_id, visible, _name in vis_plan:
                    try:
                        if apply_category_visibility(tpl, cat_id, visible):
                            successes += 1
                            affected.add(eid_int(tpl.Id))
                    except Exception as ex:
                        failures += 1
                        last_error = ex
                # OGS writes (halftone / transparency for now). Skip categories
                # the template won't accept overrides for — calling
                # SetCategoryOverrides on those can poison the transaction
                # and even crash Revit's commit phase.
                for cat_id, _name in ogs_targets:
                    if not is_category_overridable(tpl, cat_id):
                        continue
                    try:
                        ogs = tpl.GetCategoryOverrides(cat_id)
                        if ogs is None:
                            continue
                        if "halftone" in dirty:
                            try:
                                ogs.SetHalftone(ogs_halftone)
                            except Exception as ex:
                                last_error = ex
                        if "transparency" in dirty:
                            try:
                                ogs.SetSurfaceTransparency(ogs_transparency)
                            except Exception as ex:
                                last_error = ex
                        if "proj_weight" in dirty and ogs_proj_weight is not None:
                            try:
                                ogs.SetProjectionLineWeight(int(ogs_proj_weight))
                            except Exception as ex:
                                last_error = ex
                        if "proj_pattern" in dirty and ogs_proj_pattern is not None:
                            try:
                                ogs.SetProjectionLinePatternId(make_eid(int(ogs_proj_pattern)))
                            except Exception as ex:
                                last_error = ex
                        if "proj_color" in dirty:
                            try:
                                ogs.SetProjectionLineColor(_rgb_to_revit_color(ogs_proj_color))
                            except Exception as ex:
                                last_error = ex
                        if "cut_weight" in dirty and ogs_cut_weight is not None:
                            try:
                                ogs.SetCutLineWeight(int(ogs_cut_weight))
                            except Exception as ex:
                                last_error = ex
                        if "cut_pattern" in dirty and ogs_cut_pattern is not None:
                            try:
                                ogs.SetCutLinePatternId(make_eid(int(ogs_cut_pattern)))
                            except Exception as ex:
                                last_error = ex
                        if "cut_color" in dirty:
                            try:
                                ogs.SetCutLineColor(_rgb_to_revit_color(ogs_cut_color))
                            except Exception as ex:
                                last_error = ex
                        if "detail_level" in dirty and ogs_detail_level is not None:
                            try:
                                from Autodesk.Revit.DB import ViewDetailLevel
                                from System import Enum
                                ogs.SetDetailLevel(Enum.ToObject(ViewDetailLevel, int(ogs_detail_level)))
                            except Exception as ex:
                                last_error = ex
                        tpl.SetCategoryOverrides(cat_id, ogs)
                        successes += 1
                        affected.add(eid_int(tpl.Id))
                    except Exception as ex:
                        failures += 1
                        last_error = ex
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            dbhms_ui.info(
                "V/G category apply hit an error and was rolled back:\n\n"
                "{0}".format(str(ex)),
                title="View Templates Manager - V/G apply failed")
            return

        if successes == 0 and failures > 0 and last_error is not None:
            dbhms_ui.info(
                "V/G category apply wrote 0 changes; {0} attempts failed.\n\n"
                "Last error:\n{1}".format(failures, str(last_error)),
                title="View Templates Manager - V/G apply failed")
            return
        # Stay open so the user can keep editing — green checkmark fades
        # back to the default status text after a few seconds. Reload the
        # row checkboxes + right-panel OGS so the UI matches reality.
        if successes:
            tpl_word = "template" if len(affected) == 1 else "templates"
            chg_word = "change" if successes == 1 else "changes"
            _flash_applied(
                self.txt_dialog_status,
                "Applied {0} {1} to {2} {3}".format(successes, chg_word,
                                                    len(affected), tpl_word),
                revert_text=self._default_status_text())
            self._refresh_after_apply()

    def _default_status_text(self):
        if len(self._names) > 1:
            return "Editing {0} templates.".format(len(self._names))
        if self._names:
            return "Editing template: {0}".format(self._names[0])
        return ""

    def _refresh_after_apply(self):
        """Re-read state from templates so the visibility checkboxes and
        the OGS panel reflect what's actually now on the templates."""
        try:
            for info in self._cat_rows:
                cid = info.get("cat_id")
                if cid is None:
                    continue
                new_initial = read_category_visible_bulk(self._templates, cid)
                info["initial"] = new_initial
                info["chk_show"].IsChecked = new_initial
        except Exception:
            pass
        try:
            self._load_ogs_panel(self._selected_names)
        except Exception:
            pass

    def _on_reset_overrides(self, sender, args):
        """Clear all graphic overrides on the currently highlighted categories
        across every template in the dialog's set. Visibility (hide/show)
        is left alone — only OGS overrides are reset."""
        if not self._selected_names or not self._templates:
            return
        cat_ids = []
        for info in self._cat_rows:
            if info.get("name") in self._selected_names:
                cid = info.get("cat_id")
                if cid is not None:
                    cat_ids.append(cid)
        if not cat_ids:
            return

        from Autodesk.Revit.DB import Transaction, OverrideGraphicSettings
        t = Transaction(doc, "Reset V/G category overrides")
        opts = make_silent_failure_options(t)
        if opts is not None:
            try:
                t.SetFailureHandlingOptions(opts)
            except Exception:
                pass
        successes = 0
        try:
            t.Start()
            for tpl in self._templates:
                for cat_id in cat_ids:
                    if not is_category_overridable(tpl, cat_id):
                        continue
                    try:
                        tpl.SetCategoryOverrides(cat_id, OverrideGraphicSettings())
                        successes += 1
                    except Exception:
                        pass
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            dbhms_ui.info("Reset failed: {0}".format(str(ex)),
                        title="V/G Reset Overrides")
            return
        # Reload panel from the (now-cleared) OGS so the user sees the result
        self._load_ogs_panel(self._selected_names)


# ===========================================================================
# SUB-DIALOG: V/G Imports (CAD link/layer tree)
# ===========================================================================

class VgImportsDialog(forms.WPFWindow):
    def __init__(self, templates=None, template_names=None):
        forms.WPFWindow.__init__(self, VG_IMP_XAML)
        self._templates = list(templates or [])
        self._names = list(template_names or [it.Name for it in self._templates])
        self._imp_rows = []           # list of {row, chk_show, name, cat_id, initial}
        self._selected_name = None
        self._selected_names = []     # see VgCategoriesDialog for rationale
        # Iter-3b OGS state (same shape as VgCategoriesDialog)
        self._loading_ogs = False
        self._dirty_fields = set()
        self._populate_lookups()
        self._populate_imports()
        self._wire_events()
        _set_legend_mixed(self)
        # Footer status + enable OK
        if len(self._names) > 1:
            self.txt_imp_status.Text = (
                "OK applies changes to {0} templates.".format(len(self._names)))
        elif self._names:
            self.txt_imp_status.Text = (
                "OK applies changes to template: {0}".format(self._names[0]))
        try:
            self.btn_imp_ok.IsEnabled = True
            self.btn_imp_ok.ToolTip = (
                "Apply visibility changes to the selected template(s). "
                "Right-panel override controls (color/weight/pattern) are "
                "still preview-only — those wire up in iter 3b.")
        except Exception:
            pass

    def _populate_lookups(self):
        def fill(combo, items, default=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default, combo.Items.Count - 1)
        # Iter-3c: real options
        line_patterns = get_line_patterns(doc)
        _populate_combo_kv(self.cmb_imp_weight,  _LINE_WEIGHT_OPTIONS)
        _populate_combo_kv(self.cmb_imp_pattern, line_patterns)
        # Wire OGS controls to dirty tracking
        self.sld_imp_transparency.ValueChanged += self._on_transparency
        self.chk_imp_halftone.Click            += self._on_halftone_clicked
        self.cmb_imp_weight.SelectionChanged   += self._on_imp_weight_changed
        self.cmb_imp_pattern.SelectionChanged  += self._on_imp_pattern_changed
        self.btn_imp_color.Click               += self._on_imp_color_clicked
        # Color button keeps current (r,g,b) tuple in .Tag
        self.btn_imp_color.Tag = None

    def _on_transparency(self, sender, args):
        self.txt_imp_transparency_val.Text = "{0}%".format(int(self.sld_imp_transparency.Value))
        if not self._loading_ogs:
            self._dirty_fields.add("transparency")

    def _on_halftone_clicked(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("halftone")

    def _on_imp_weight_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("proj_weight")

    def _on_imp_pattern_changed(self, sender, args):
        if not self._loading_ogs:
            self._dirty_fields.add("proj_pattern")

    def _on_imp_color_clicked(self, sender, args):
        rgb = pick_color(self.btn_imp_color.Tag)
        if rgb is None:
            return
        self.btn_imp_color.Tag = rgb
        # Imports XAML has txt_imp_color but no separate sw_imp_color border;
        # the swatch is a Border *inside* btn_imp_color's content, find it.
        # Fallback: just update the label.
        try:
            content = self.btn_imp_color.Content
            sp = content
            for child in list(sp.Children):
                from System.Windows.Controls import Border as _Brd
                if isinstance(child, _Brd):
                    _set_swatch_color(child, self.txt_imp_color, rgb)
                    break
        except Exception:
            try:
                self.txt_imp_color.Text = "RGB({0}, {1}, {2})".format(*rgb)
            except Exception:
                pass
        self._dirty_fields.add("proj_color")

    def _populate_imports(self):
        self.pnl_imports.Children.Clear()
        self._imp_rows = []
        # Track expander buttons so we can flip them on Expand/Collapse All
        self._imp_expander_state = []  # list of (btn, panel)
        # Track CAD groups so search can show parent rows when a child layer
        # matches and auto-expand the panel containing the match.
        self._imp_groups = []  # list of (cad_info, layer_panel, [layer_infos])
        cad_links = get_imported_cad_links(doc)
        if not cad_links:
            # Nothing imported — show a hint instead of an empty list
            tb = TextBlock()
            tb.Text = "No CAD imports found in this project."
            tb.FontSize = 11
            tb.Foreground = SolidColorBrush(Color.FromRgb(0xA0, 0xAE, 0xC0))
            tb.Padding = Thickness(8, 12, 8, 12)
            self.pnl_imports.Children.Add(tb)
        for cad in cad_links:
            cad_cat = cad["category"]
            cad_info = _build_import_link_row(cad["name"])
            try:
                cad_info["chk_show"].IsThreeState = True
            except Exception:
                pass
            initial = read_category_visible_bulk(self._templates, cad_cat.Id)
            cad_info["chk_show"].IsChecked = initial
            cad_info["cat_id"]  = cad_cat.Id
            cad_info["initial"] = initial
            self.pnl_imports.Children.Add(cad_info["row"])
            self._imp_rows.append(cad_info)
            # Layer rows (collapsed by default; visible when user expands)
            layer_panel = StackPanel()
            layer_panel.Visibility = Visibility.Collapsed
            layer_infos = []
            for layer_name, layer_cat in cad["layers"]:
                lr_info = _build_import_layer_row(layer_name, parent_cad=cad["name"])
                try:
                    lr_info["chk_show"].IsThreeState = True
                except Exception:
                    pass
                lr_initial = read_category_visible_bulk(self._templates, layer_cat.Id)
                lr_info["chk_show"].IsChecked = lr_initial
                lr_info["cat_id"]  = layer_cat.Id
                lr_info["initial"] = lr_initial
                layer_panel.Children.Add(lr_info["row"])
                self._imp_rows.append(lr_info)
                layer_infos.append(lr_info)
            self.pnl_imports.Children.Add(layer_panel)
            cad_info["expander_btn"].Click += self._make_layer_toggle(cad_info["expander_btn"], layer_panel)
            self._imp_expander_state.append((cad_info["expander_btn"], layer_panel))
            self._imp_groups.append((cad_info, layer_panel, layer_infos))
        # Multi-select across CAD links AND layers, plus checkbox propagation
        self._select_helper = RowMultiSelectHelper(
            self._imp_rows, on_select=self._on_imp_select)
        try:
            self.chk_imp_halftone.IsThreeState = True
        except Exception:
            pass
        # Initial OGS load so the panel reflects current consensus, not defaults
        self._on_imp_select([])

    def _make_layer_toggle(self, btn, panel):
        def handler(sender, args):
            if panel.Visibility == Visibility.Collapsed:
                panel.Visibility = Visibility.Visible
                btn.Content = u"▾"  # down-pointing
            else:
                panel.Visibility = Visibility.Collapsed
                btn.Content = u"▸"  # right-pointing
        return handler

    def _on_imp_select(self, names):
        self._selected_names = list(names)
        if not names:
            self.txt_imp_selected.Text = (
                "Nothing selected — edits apply to all {0} CAD links and layers"
                .format(len(self._imp_rows)))
            self.pnl_imp_selected_names.Children.Clear()
        else:
            _render_multi_select(
                self.txt_imp_selected, self.pnl_imp_selected_names, names,
                "items",
                on_show_all=lambda nms: _show_full_name_list("Selected CAD items", nms))
        self._load_ogs_panel(names)

    def _load_ogs_panel(self, scope_names):
        """Empty scope = consensus across every CAD link + layer row.
        Iter-3c: also populates line weight / pattern / color from OGS."""
        self._loading_ogs = True
        try:
            self._dirty_fields.clear()
            if scope_names:
                want = set(scope_names)
                scope = [info for info in self._imp_rows
                         if info.get("name") in want]
            else:
                scope = list(self._imp_rows)
            if not scope or not self._templates:
                self.chk_imp_halftone.IsChecked = False
                try:
                    self.sld_imp_transparency.Value = 0
                    _select_combo_by_value(self.cmb_imp_weight, -1)
                    _select_combo_by_value(self.cmb_imp_pattern, -1)
                    self.btn_imp_color.Tag = None
                    if hasattr(self, "txt_imp_color"):
                        self.txt_imp_color.Text = "<no override>"
                except Exception:
                    pass
                return
            halftones = []
            first_transparency = None
            first_weight  = None
            first_pattern = None
            first_color   = None
            for tpl in self._templates:
                for info in scope:
                    cid = info.get("cat_id")
                    if cid is None:
                        continue
                    try:
                        ogs = tpl.GetCategoryOverrides(cid)
                        if ogs is None:
                            continue
                    except Exception:
                        continue
                    try:
                        halftones.append(bool(ogs.Halftone))
                    except Exception:
                        pass
                    if first_transparency is None:
                        try:
                            first_transparency = int(ogs.Transparency)
                        except Exception:
                            first_transparency = 0
                    if first_weight is None:
                        try:
                            first_weight = int(ogs.ProjectionLineWeight)
                        except Exception:
                            first_weight = -1
                    if first_pattern is None:
                        try:
                            first_pattern = eid_int(ogs.ProjectionLinePatternId)
                        except Exception:
                            first_pattern = -1
                    if first_color is None:
                        try:
                            first_color = _ogs_color_to_rgb(ogs.ProjectionLineColor)
                        except Exception:
                            first_color = None
            if not halftones:
                self.chk_imp_halftone.IsChecked = False
            elif all(halftones):
                self.chk_imp_halftone.IsChecked = True
            elif not any(halftones):
                self.chk_imp_halftone.IsChecked = False
            else:
                self.chk_imp_halftone.IsChecked = None
            try:
                self.sld_imp_transparency.Value = float(first_transparency or 0)
            except Exception:
                self.sld_imp_transparency.Value = 0
            _select_combo_by_value(self.cmb_imp_weight,  first_weight if first_weight is not None else -1)
            _select_combo_by_value(self.cmb_imp_pattern, first_pattern if first_pattern is not None else -1)
            self.btn_imp_color.Tag = first_color
            # Color label (no separate swatch border in this dialog)
            if first_color is None:
                try:
                    self.txt_imp_color.Text = "<no override>"
                except Exception:
                    pass
            else:
                try:
                    self.txt_imp_color.Text = "RGB({0}, {1}, {2})".format(*first_color)
                except Exception:
                    pass
        finally:
            self._loading_ogs = False

    def _wire_events(self):
        self.btn_imp_cancel.Click       += lambda s, e: self.Close()
        self.btn_imp_ok.Click           += self._on_dlg_ok
        self.btn_imp_show_all.Click     += self._on_show_all
        self.btn_imp_hide_all.Click     += self._on_hide_all
        self.btn_imp_expand_all.Click   += self._on_expand_all
        self.btn_imp_collapse_all.Click += self._on_collapse_all
        self.btn_imp_reset.Click        += self._on_reset_overrides
        try:
            self.txt_imp_search.TextChanged += self._on_imp_search
        except Exception:
            pass

    def _on_imp_search(self, sender, args):
        query = (self.txt_imp_search.Text or "").strip().lower()
        for cad_info, layer_panel, layer_infos in self._imp_groups:
            cad_name = (cad_info.get("name") or "").lower()
            cad_match = (not query) or (query in cad_name)
            any_layer_match = False
            for li in layer_infos:
                ln = (li.get("name") or "").lower()
                # When the parent CAD matches, all its layers are "matches"
                # so the expanded view stays consistent.
                li_match = cad_match or (not query) or (query in ln)
                if li_match and query and (query in ln):
                    any_layer_match = True
                li["row"].Visibility = Visibility.Visible if li_match else Visibility.Collapsed
            cad_visible = cad_match or any_layer_match
            cad_info["row"].Visibility = Visibility.Visible if cad_visible else Visibility.Collapsed
            # Auto-expand a layer panel when a child layer matches but the
            # parent CAD name doesn't, so the user can see the match.
            if query and any_layer_match and not cad_match:
                layer_panel.Visibility = Visibility.Visible
                try:
                    cad_info["expander_btn"].Content = u"▾"  # ▾
                except Exception:
                    pass
            elif not cad_visible:
                layer_panel.Visibility = Visibility.Collapsed

    def _on_show_all(self, sender, args):
        for info in self._imp_rows:
            info["chk_show"].IsChecked = True
        self._reset_highlight_for_global_scope()

    def _on_hide_all(self, sender, args):
        for info in self._imp_rows:
            info["chk_show"].IsChecked = False
        self._reset_highlight_for_global_scope()

    def _reset_highlight_for_global_scope(self):
        try:
            self._select_helper.clear_highlight()
        except Exception:
            pass

    def _on_expand_all(self, sender, args):
        for btn, panel in self._imp_expander_state:
            panel.Visibility = Visibility.Visible
            btn.Content = u"▾"

    def _on_collapse_all(self, sender, args):
        for btn, panel in self._imp_expander_state:
            panel.Visibility = Visibility.Collapsed
            btn.Content = u"▸"

    def _on_dlg_ok(self, sender, args):
        """Apply visibility (per-row Show) AND OGS halftone/transparency
        changes across every template in self._templates. Same write
        rules as VgCategoriesDialog._on_dlg_ok."""
        if not self._templates:
            self.Close()
            return

        # 1. Visibility plan
        vis_plan = []
        for info in self._imp_rows:
            chk = info["chk_show"]
            current = chk.IsChecked
            if current is None:
                continue
            if current == info.get("initial"):
                continue
            cat_id = info.get("cat_id")
            if cat_id is None:
                continue
            vis_plan.append((cat_id, bool(current), info["name"]))

        # 2. OGS plan — same scope rule as VgCategoriesDialog: highlighted
        #    rows if any are selected, otherwise fall back to every row.
        ogs_targets = []
        if self._dirty_fields:
            target_names = set(self._selected_names) if self._selected_names \
                           else set(info["name"] for info in self._imp_rows)
            for info in self._imp_rows:
                if info.get("name") in target_names:
                    cid = info.get("cat_id")
                    if cid is not None:
                        ogs_targets.append((cid, info["name"]))

        if not vis_plan and not ogs_targets:
            self.Close()
            return

        # Halftone may be None (indeterminate) — skip writing it in that case.
        halftone_state   = self.chk_imp_halftone.IsChecked
        ogs_halftone     = bool(halftone_state) if halftone_state is not None else None
        ogs_transparency = int(self.sld_imp_transparency.Value)
        ogs_weight       = _selected_combo_value(self.cmb_imp_weight)
        ogs_pattern      = _selected_combo_value(self.cmb_imp_pattern)
        ogs_color        = self.btn_imp_color.Tag
        dirty = set(self._dirty_fields)
        if "halftone" in dirty and ogs_halftone is None:
            dirty.discard("halftone")

        from Autodesk.Revit.DB import Transaction
        successes = 0
        failures = 0
        affected = set()
        last_error = None
        t = Transaction(doc, "Apply V/G import overrides")
        opts = make_silent_failure_options(t)
        if opts is not None:
            try:
                t.SetFailureHandlingOptions(opts)
            except Exception:
                pass
        try:
            t.Start()
            for tpl in self._templates:
                for cat_id, visible, _name in vis_plan:
                    try:
                        if apply_category_visibility(tpl, cat_id, visible):
                            successes += 1
                            affected.add(eid_int(tpl.Id))
                    except Exception as ex:
                        failures += 1
                        last_error = ex
                for cat_id, _name in ogs_targets:
                    if not is_category_overridable(tpl, cat_id):
                        continue
                    try:
                        ogs = tpl.GetCategoryOverrides(cat_id)
                        if ogs is None:
                            continue
                        if "halftone" in dirty:
                            try:
                                ogs.SetHalftone(ogs_halftone)
                            except Exception as ex:
                                last_error = ex
                        if "transparency" in dirty:
                            try:
                                ogs.SetSurfaceTransparency(ogs_transparency)
                            except Exception as ex:
                                last_error = ex
                        if "proj_weight" in dirty and ogs_weight is not None:
                            try:
                                ogs.SetProjectionLineWeight(int(ogs_weight))
                            except Exception as ex:
                                last_error = ex
                        if "proj_pattern" in dirty and ogs_pattern is not None:
                            try:
                                ogs.SetProjectionLinePatternId(make_eid(int(ogs_pattern)))
                            except Exception as ex:
                                last_error = ex
                        if "proj_color" in dirty:
                            try:
                                ogs.SetProjectionLineColor(_rgb_to_revit_color(ogs_color))
                            except Exception as ex:
                                last_error = ex
                        tpl.SetCategoryOverrides(cat_id, ogs)
                        successes += 1
                        affected.add(eid_int(tpl.Id))
                    except Exception as ex:
                        failures += 1
                        last_error = ex
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            dbhms_ui.info(
                "V/G imports apply hit an error and was rolled back:\n\n"
                "{0}".format(str(ex)),
                title="View Templates Manager - V/G apply failed")
            return

        if successes == 0 and failures > 0 and last_error is not None:
            dbhms_ui.info(
                "V/G imports apply wrote 0 changes; {0} attempts failed.\n\n"
                "Last error:\n{1}".format(failures, str(last_error)),
                title="View Templates Manager - V/G apply failed")
            return
        if successes:
            tpl_word = "template" if len(affected) == 1 else "templates"
            chg_word = "change" if successes == 1 else "changes"
            _flash_applied(
                self.txt_imp_status,
                "Applied {0} {1} to {2} {3}".format(successes, chg_word,
                                                    len(affected), tpl_word),
                revert_text=self._default_status_text())
            self._refresh_after_apply()

    def _default_status_text(self):
        if len(self._names) > 1:
            return "Editing {0} templates.".format(len(self._names))
        if self._names:
            return "Editing template: {0}".format(self._names[0])
        return ""

    def _refresh_after_apply(self):
        try:
            for info in self._imp_rows:
                cid = info.get("cat_id")
                if cid is None:
                    continue
                new_initial = read_category_visible_bulk(self._templates, cid)
                info["initial"] = new_initial
                info["chk_show"].IsChecked = new_initial
        except Exception:
            pass
        try:
            self._load_ogs_panel(self._selected_names)
        except Exception:
            pass

    def _on_reset_overrides(self, sender, args):
        """Clear OGS overrides on the currently highlighted import rows
        (CAD links and/or layers) across every template. Visibility is
        left alone — only graphic overrides are reset."""
        if not self._selected_names or not self._templates:
            return
        cat_ids = []
        for info in self._imp_rows:
            if info.get("name") in self._selected_names:
                cid = info.get("cat_id")
                if cid is not None:
                    cat_ids.append(cid)
        if not cat_ids:
            return

        from Autodesk.Revit.DB import Transaction, OverrideGraphicSettings
        t = Transaction(doc, "Reset V/G import overrides")
        opts = make_silent_failure_options(t)
        if opts is not None:
            try:
                t.SetFailureHandlingOptions(opts)
            except Exception:
                pass
        try:
            t.Start()
            for tpl in self._templates:
                for cat_id in cat_ids:
                    if not is_category_overridable(tpl, cat_id):
                        continue
                    try:
                        tpl.SetCategoryOverrides(cat_id, OverrideGraphicSettings())
                    except Exception:
                        pass
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            dbhms_ui.info("Reset failed: {0}".format(str(ex)),
                        title="V/G Reset Overrides")
            return
        self._load_ogs_panel(self._selected_names)


# ===========================================================================
# SUB-DIALOG: V/G Filters
# ===========================================================================

class VgFiltersDialog(forms.WPFWindow):
    def __init__(self, template_names=None):
        forms.WPFWindow.__init__(self, VG_FLT_XAML)
        self._names = template_names or []
        self._flt_rows = []
        self._selected_name = None
        self._selected_names = []
        self._populate_lookups()
        self._populate_filters()
        self._wire_events()
        _set_legend_mixed(self)
        if len(self._names) > 1:
            self.txt_flt_status.Text = (
                "Preview only — filter wiring is deferred. {0} templates would be affected on Apply."
                .format(len(self._names)))

    def _populate_lookups(self):
        def fill(combo, items, default=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default, combo.Items.Count - 1)
        fill(self.cmb_flt_weight,        MOCK_LINE_WEIGHTS)
        fill(self.cmb_flt_pattern,       MOCK_LINE_PATTERNS)
        fill(self.cmb_flt_surf_pattern,  MOCK_PATTERNS)
        fill(self.cmb_flt_cut_pattern,   MOCK_PATTERNS)
        self.sld_flt_transparency.ValueChanged += self._on_transparency

    def _on_transparency(self, sender, args):
        self.txt_flt_transparency_val.Text = "{0}%".format(int(self.sld_flt_transparency.Value))

    def _populate_filters(self):
        self.pnl_filters.Children.Clear()
        self._flt_rows = []
        for f in MOCK_FILTERS:
            info = _build_filter_row(f["name"], f["enabled"], f["visible"])
            self.pnl_filters.Children.Add(info["row"])
            self._flt_rows.append(info)
        # Filter rows have two propagating checkboxes — Visible and Enabled.
        # Whichever the user toggles in a highlighted row is propagated to
        # the same column across every other highlighted row.
        self._select_helper = RowMultiSelectHelper(
            self._flt_rows, on_select=self._on_flt_select,
            checkbox_keys=("chk_visible", "chk_enabled"))

    def _on_flt_select(self, names):
        self._selected_names = list(names)
        _render_multi_select(
            self.txt_flt_selected, self.pnl_flt_selected_names, names,
            "filters",
            on_show_all=lambda nms: _show_full_name_list("Selected filters", nms))

    def _wire_events(self):
        self.btn_flt_cancel.Click += lambda s, e: self.Close()
        self.btn_flt_ok.Click     += lambda s, e: self.Close()
        self.btn_flt_add.Click    += self._on_add
        self.btn_flt_remove.Click += self._on_remove

    def _on_add(self, sender, args):
        dbhms_ui.info(
            "Filter picker arrives in iter 3. For now, the list shows mock "
            "filters so you can see how the layout works.",
            title="V/G Overrides - Filters")

    def _on_remove(self, sender, args):
        self.txt_flt_status.Text = "Remove clicked - iter 3 will detach the filter from selected templates."


# ===========================================================================
# SUB-DIALOG: RVT Link Display Settings
# ===========================================================================

class VgLinksDialog(forms.WPFWindow):
    def __init__(self, templates=None, template_names=None):
        forms.WPFWindow.__init__(self, VG_LNK_XAML)
        self._templates = list(templates or [])
        self._names = list(template_names or [it.Name for it in self._templates])
        self._link_records = get_revit_link_types(doc)
        self._current_link = None
        self._loading_state = False     # suppress dirty marking during load
        self._dirty_fields = set()      # 'mode' / 'halftone'
        self._populate_link_picker()
        self._populate_inherit_combos()
        self._wire_events()
        # Footer status / OK button enable
        if self._templates:
            self.txt_lnk_status.Text = (
                "Editing {0} template{1}.".format(
                    len(self._names), "" if len(self._names) == 1 else "s"))
        try:
            self.btn_lnk_ok.IsEnabled = bool(self._templates and self._link_records)
            self.btn_lnk_ok.ToolTip = (
                "Apply the visibility mode to the selected template(s) "
                "for the picked link. Per-aspect dropdowns (Phase, Detail Level, "
                "etc.) are still preview-only and will wire up in iter 4b.")
        except Exception:
            pass
        # Pre-load state for the first link in the list
        self._on_link_picker_changed(None, None)

    def _populate_link_picker(self):
        if not self._link_records:
            _populate_combo_kv(self.cmb_link_picker,
                               [("(no Revit links in this project)", -1)])
            self.cmb_link_picker.IsEnabled = False
            return
        opts = [(rec["name"], i) for i, rec in enumerate(self._link_records)]
        _populate_combo_kv(self.cmb_link_picker, opts)
        if self.cmb_link_picker.Items.Count > 0:
            self.cmb_link_picker.SelectedIndex = 0

    def _populate_inherit_combos(self):
        # Iter-4b: real per-aspect options keyed by the aspect's API value.
        # Each combo's Tag for "By host" / "By linked" is the canonical
        # LinkVisibility name. Aspects with specific values (Phase, etc.)
        # use a (lv_name, specific_int) tuple so the OK handler can write
        # both in one call.
        link_doc = (self._current_link or {}).get("doc")
        # Inherit-only aspects (just ByHost / ByLinked)
        _populate_combo_kv(self.cmb_lnk_filters, [
            ("<By host view>",   "ByHostView"),
            ("<By linked view>", "ByLinkView"),
        ])
        _populate_combo_kv(self.cmb_lnk_viewrange, [
            ("<By host view>",   "ByHostView"),
            ("<By linked view>", "ByLinkView"),
        ])
        _populate_combo_kv(self.cmb_lnk_colorfill, [
            ("<By host view>",   "ByHostView"),
            ("<By linked view>", "ByLinkView"),
        ])
        _populate_combo_kv(self.cmb_lnk_objstyles, [
            ("<By host model>",  "ByHostView"),
            ("<By linked view>", "ByLinkView"),
        ])
        _populate_combo_kv(self.cmb_lnk_nested, [
            ("<By parent link>", "ByHostView"),
            ("<By linked view>", "ByLinkView"),
        ])
        # Phase / Phase filter — list from the linked doc
        phase_opts = [("<By host view>",  ("ByHostView", None)),
                      ("<By linked view>", ("ByLinkView", None))]
        for name, eid in _enumerate_phases(link_doc):
            phase_opts.append((name, ("Custom", eid)))
        _populate_combo_kv(self.cmb_lnk_phase, phase_opts)
        pf_opts = [("<By host view>",  ("ByHostView", None)),
                   ("<By linked view>", ("ByLinkView", None))]
        for name, eid in _enumerate_phase_filters(link_doc):
            pf_opts.append((name, ("Custom", eid)))
        _populate_combo_kv(self.cmb_lnk_phasefilter, pf_opts)
        # Detail level
        _populate_combo_kv(self.cmb_lnk_detail, [
            ("<By host view>",   ("ByHostView", None)),
            ("<By linked view>", ("ByLinkView", None)),
            ("Coarse",           ("Custom", 1)),
            ("Medium",           ("Custom", 2)),
            ("Fine",             ("Custom", 3)),
        ])
        # Discipline
        _populate_combo_kv(self.cmb_lnk_discipline, [
            ("<By host view>",  ("ByHostView", None)),
            ("<By linked view>", ("ByLinkView", None)),
            ("Architectural",   ("Custom", 1)),
            ("Structural",      ("Custom", 2)),
            ("Mechanical",      ("Custom", 4)),
            ("Electrical",      ("Custom", 8)),
            ("Plumbing",        ("Custom", 16)),
            ("Coordination",    ("Custom", 4095)),
        ])
        # Linked view picker — enumerate views from the link's loaded doc
        _populate_combo_kv(self.cmb_lnk_linkedview, _enumerate_linked_views(link_doc))

    def _wire_events(self):
        self.btn_lnk_cancel.Click  += lambda s, e: self.Close()
        self.btn_lnk_ok.Click      += self._on_dlg_ok
        self.btn_open_native.Click += self._on_open_native
        self.cmb_link_picker.SelectionChanged += self._on_link_picker_changed
        self.rb_link_byhost.Checked  += self._on_mode_changed
        self.rb_link_bylinked.Checked += self._on_mode_changed
        # Per-aspect dirty hooks. Each maps the combo's SelectionChanged
        # to a unique dirty-field name we use later in _on_dlg_ok.
        for combo_attr, dirty_key in (
                ("cmb_lnk_linkedview",  "linkedview"),
                ("cmb_lnk_filters",     "filters"),
                ("cmb_lnk_viewrange",   "viewrange"),
                ("cmb_lnk_phase",       "phase"),
                ("cmb_lnk_phasefilter", "phasefilter"),
                ("cmb_lnk_detail",      "detail"),
                ("cmb_lnk_discipline",  "discipline"),
                ("cmb_lnk_colorfill",   "colorfill"),
                ("cmb_lnk_objstyles",   "objstyles"),
                ("cmb_lnk_nested",      "nested"),
        ):
            try:
                combo = getattr(self, combo_attr)
                combo.SelectionChanged += self._make_aspect_dirty_handler(dirty_key)
            except Exception:
                pass

    def _make_aspect_dirty_handler(self, dirty_key):
        def handler(sender, args):
            if not self._loading_state:
                self._dirty_fields.add(dirty_key)
        return handler

    def _on_link_picker_changed(self, sender, args):
        idx = _selected_combo_value(self.cmb_link_picker)
        if idx is None or idx < 0 or idx >= len(self._link_records):
            self._current_link = None
        else:
            self._current_link = self._link_records[idx]
        # Re-populate aspect combos because Phase / Phase filter / Linked
        # view options come from the chosen link's document, not the host.
        self._loading_state = True
        try:
            self._populate_inherit_combos()
        finally:
            self._loading_state = False
        self._load_state_from_templates()

    def _on_mode_changed(self, sender, args):
        if not self._loading_state:
            self._dirty_fields.add("mode")

    def _load_state_from_templates(self):
        """Read the current link override state across all selected
        templates and populate the visibility-mode radios with the
        consensus value."""
        self._loading_state = True
        try:
            self._dirty_fields.clear()
            if (self._current_link is None or not self._templates
                    or not _HAS_LINK_GRAPHICS):
                self.rb_link_byhost.IsChecked = True
                return
            link_id = self._current_link["type_id"]
            modes = []
            for tpl in self._templates:
                try:
                    settings = tpl.GetLinkOverrides(link_id)
                except Exception:
                    continue
                modes.append(_read_link_visibility_mode(settings))
            # Mode consensus
            unique = set(modes)
            # Custom is read-only via API; flag the disabled radio when
            # the mode is detected so user can see it accurately.
            try:
                self.rb_link_custom.IsChecked = (modes and unique == {"custom"})
            except Exception:
                pass
            if not modes:
                self.rb_link_byhost.IsChecked = True
            elif unique == {"byhost"}:
                self.rb_link_byhost.IsChecked = True
            elif unique == {"bylinked"}:
                self.rb_link_bylinked.IsChecked = True
            elif unique == {"custom"}:
                # Disabled radio already flagged above; leave the editable
                # radios unchecked since user can't switch from Custom via API.
                self.rb_link_byhost.IsChecked = False
                self.rb_link_bylinked.IsChecked = False
            elif unique == {"unknown"}:
                # Override exists but we couldn't read mode — leave both
                # editable radios unchecked rather than guessing.
                self.rb_link_byhost.IsChecked = False
                self.rb_link_bylinked.IsChecked = False
                try:
                    self.txt_lnk_status.Text = (
                        "Couldn't read this link's visibility mode from the API "
                        "(unknown shape on this Revit version). Use 'Open in "
                        "Revit's native dialog' to see the actual mode.")
                except Exception:
                    pass
            else:
                # Mixed across templates — leave radios unchecked.
                self.rb_link_byhost.IsChecked = False
                self.rb_link_bylinked.IsChecked = False
            # Iter-4b: load each per-aspect value from the FIRST template's
            # settings. Multi-template "varies" detection is overkill here
            # (per-aspect overrides aren't usually templated bulk-edited
            # to begin with) — we just show what one template has.
            self._load_aspects_from_first_template()
            # Disable per-aspect editing when ALL selected templates have
            # this link in Custom mode (writing would silently fail). Mode
            # radios stay live so the user can switch out of Custom.
            all_custom = bool(modes) and unique == {"custom"}
            self._set_aspects_enabled(not all_custom)
        finally:
            self._loading_state = False

    def _set_aspects_enabled(self, enabled):
        """Toggle the per-aspect dropdowns + show/hide the Custom-mode
        explanation banner. Used so users with Custom-mode links see at a
        glance that those fields can't be edited from here."""
        for combo_attr in ("cmb_lnk_linkedview", "cmb_lnk_filters",
                           "cmb_lnk_viewrange", "cmb_lnk_phase",
                           "cmb_lnk_phasefilter", "cmb_lnk_detail",
                           "cmb_lnk_discipline", "cmb_lnk_colorfill",
                           "cmb_lnk_objstyles", "cmb_lnk_nested"):
            try:
                getattr(self, combo_attr).IsEnabled = bool(enabled)
            except Exception:
                pass
        try:
            self.bnr_aspects_custom.Visibility = (
                Visibility.Collapsed if enabled else Visibility.Visible)
        except Exception:
            pass

    def _load_aspects_from_first_template(self):
        """Populate each per-aspect combo with the value read from the
        first template's RevitLinkGraphicsSettings. No-ops cleanly when
        the link has no override (everything reads as ByHostView)."""
        if not self._templates or self._current_link is None:
            return
        link_id = self._current_link["type_id"]
        try:
            settings = self._templates[0].GetLinkOverrides(link_id)
        except Exception:
            settings = None
        # Inherit-only aspects: the combo's Tag is just the lv_name string
        for combo_attr, attrs in (
                ("cmb_lnk_filters",   _ASPECT_FILTERS_ATTRS),
                ("cmb_lnk_viewrange", _ASPECT_VIEWRANGE_ATTRS),
                ("cmb_lnk_colorfill", _ASPECT_COLORFILL_ATTRS),
                ("cmb_lnk_objstyles", _ASPECT_OBJSTYLES_ATTRS),
                ("cmb_lnk_nested",    _ASPECT_NESTED_ATTRS),
        ):
            try:
                combo = getattr(self, combo_attr)
            except AttributeError:
                continue
            lv_name = _read_aspect_inherit(settings, attrs)
            if lv_name is None:
                lv_name = "ByHostView"
            _select_combo_by_value(combo, lv_name)
        # Paired aspects: combo Tag is (lv_name, specific_int_or_None)
        paired = (
            ("cmb_lnk_phase",       "GetPhaseType",          "GetPhaseId"),
            ("cmb_lnk_phasefilter", "GetPhaseFilterType",    "GetPhaseFilterId"),
            ("cmb_lnk_detail",      "GetViewDetailLevelType","GetViewDetailLevel"),
            ("cmb_lnk_discipline",  "GetDisciplineType",     "GetDiscipline"),
        )
        for combo_attr, type_method, value_method in paired:
            try:
                combo = getattr(self, combo_attr)
            except AttributeError:
                continue
            lv_name, spec = _read_aspect_paired(settings, type_method, value_method)
            if lv_name is None:
                lv_name = "ByHostView"
            target = (lv_name, spec) if lv_name == "Custom" else (lv_name, None)
            _select_combo_by_value(combo, target)
        # Linked view picker — read settings.LinkedViewId
        try:
            combo = self.cmb_lnk_linkedview
            current_view = -1
            if settings is not None:
                try:
                    eid = settings.LinkedViewId
                    if eid is not None:
                        current_view = eid_int(eid)
                except Exception:
                    current_view = -1
            _select_combo_by_value(combo, current_view)
        except Exception:
            pass

    def _on_dlg_ok(self, sender, args):
        if (self._current_link is None or not self._templates
                or not _HAS_LINK_GRAPHICS):
            self.Close()
            return
        if not self._dirty_fields:
            return
        # The Custom radio is read-only (the API doesn't let us *set* Custom),
        # but if the link is already in Custom mode we still want OK to work
        # for switching the link OUT of Custom into ByHost / ByLinked.
        mode = "byhost" if self.rb_link_byhost.IsChecked else \
               "bylinked" if self.rb_link_bylinked.IsChecked else None
        dirty = set(self._dirty_fields)
        link_id = self._current_link["type_id"]

        # Snapshot per-aspect values from the combos so a mid-write WPF
        # change can't shift what we write.
        aspect_values = {
            "linkedview":  _selected_combo_value(self.cmb_lnk_linkedview),
            "filters":     _selected_combo_value(self.cmb_lnk_filters),
            "viewrange":   _selected_combo_value(self.cmb_lnk_viewrange),
            "phase":       _selected_combo_value(self.cmb_lnk_phase),
            "phasefilter": _selected_combo_value(self.cmb_lnk_phasefilter),
            "detail":      _selected_combo_value(self.cmb_lnk_detail),
            "discipline":  _selected_combo_value(self.cmb_lnk_discipline),
            "colorfill":   _selected_combo_value(self.cmb_lnk_colorfill),
            "objstyles":   _selected_combo_value(self.cmb_lnk_objstyles),
            "nested":      _selected_combo_value(self.cmb_lnk_nested),
        }
        any_aspect_dirty = any(k in dirty for k in (
            "linkedview", "filters", "viewrange", "phase", "phasefilter",
            "detail", "discipline", "colorfill", "objstyles", "nested"))

        from Autodesk.Revit.DB import Transaction
        successes = 0
        failures = 0
        affected = set()
        last_error = None
        custom_aspects_skipped = 0
        t = Transaction(doc, "Apply V/G RVT Link overrides")
        opts = make_silent_failure_options(t)
        if opts is not None:
            try:
                t.SetFailureHandlingOptions(opts)
            except Exception:
                pass
        try:
            t.Start()
            for tpl in self._templates:
                template_did_something = False
                try:
                    # 1. Mode change. ByHost = RemoveLinkOverrides; ByLinked =
                    #    keep / build settings and SetLinkOverrides. After
                    #    this step the link is guaranteed not to be Custom
                    #    (RemoveLinkOverrides clears it; SetLinkOverrides
                    #    rewrites it as non-Custom by virtue of the new
                    #    settings object's mode).
                    if "mode" in dirty and mode == "byhost":
                        try:
                            tpl.RemoveLinkOverrides(link_id)
                            template_did_something = True
                        except Exception as ex:
                            last_error = ex
                    elif "mode" in dirty and mode == "bylinked":
                        try:
                            settings = tpl.GetLinkOverrides(link_id)
                        except Exception:
                            settings = None
                        if settings is None:
                            settings = RevitLinkGraphicsSettings()
                        try:
                            tpl.SetLinkOverrides(link_id, settings)
                            template_did_something = True
                        except Exception as ex:
                            last_error = ex

                    # 2. Per-aspect writes. Build / reuse a settings object,
                    #    apply each dirty aspect, then SetLinkOverrides.
                    #    This silently no-ops on Custom-mode links — we
                    #    detect that and increment custom_aspects_skipped.
                    if any_aspect_dirty:
                        try:
                            settings = tpl.GetLinkOverrides(link_id)
                        except Exception:
                            settings = None
                        live_mode = _read_link_visibility_mode(settings)
                        if (live_mode == "custom" and
                                "mode" not in dirty):
                            custom_aspects_skipped += 1
                        else:
                            if settings is None:
                                settings = RevitLinkGraphicsSettings()
                            self._apply_aspects_to_settings(settings, dirty,
                                                            aspect_values)
                            try:
                                tpl.SetLinkOverrides(link_id, settings)
                                template_did_something = True
                            except Exception as ex:
                                last_error = ex
                    if template_did_something:
                        successes += 1
                        affected.add(eid_int(tpl.Id))
                except Exception as ex:
                    failures += 1
                    last_error = ex
            t.Commit()
        except Exception as ex:
            try:
                t.RollBack()
            except Exception:
                pass
            dbhms_ui.info(
                "V/G RVT Link apply hit an error and was rolled back:\n\n{0}"
                .format(str(ex)),
                title="View Templates Manager - V/G apply failed")
            return

        if successes == 0 and failures > 0 and last_error is not None:
            dbhms_ui.info(
                "V/G RVT Link apply wrote 0 changes; {0} attempts failed.\n\n"
                "Last error:\n{1}".format(failures, str(last_error)),
                title="View Templates Manager - V/G apply failed")
            return
        # Surface Custom-mode write limit if any templates skipped per-aspect
        # writes because the live mode is Custom (and user didn't switch out).
        if custom_aspects_skipped > 0 and any_aspect_dirty:
            dbhms_ui.info(
                "Couldn't write per-aspect overrides on {0} template{1} — the "
                "link is in Custom mode there, and the Revit API doesn't "
                "allow writing to a Custom-mode link's settings.\n\n"
                "Two ways forward:\n"
                "  • Switch the link out of Custom mode here (pick By host "
                "view or By linked view above and OK), then reopen the "
                "dialog and edit the per-aspect dropdowns.\n"
                "  • Edit them in Revit's native V/G Overrides RVT Links "
                "dialog.".format(custom_aspects_skipped,
                                 "" if custom_aspects_skipped == 1 else "s"),
                title="Custom-mode per-aspect")
        if successes:
            tpl_word = "template" if len(affected) == 1 else "templates"
            chg_word = "change" if successes == 1 else "changes"
            _flash_applied(
                self.txt_lnk_status,
                "Applied {0} {1} to {2} {3}".format(successes, chg_word,
                                                    len(affected), tpl_word),
                revert_text="Iter 4b: link visibility + per-aspect overrides wired.")
            # Reload state so the radios reflect the new reality.
            self._load_state_from_templates()

    def _apply_aspects_to_settings(self, settings, dirty, vals):
        """Mutate a RevitLinkGraphicsSettings in place to apply each dirty
        per-aspect change. Skips silently per-aspect on individual API
        failures so one quirky setter doesn't kill the others."""
        # Inherit-only aspects
        inherit_pairs = (
            ("filters",   _ASPECT_FILTERS_ATTRS),
            ("viewrange", _ASPECT_VIEWRANGE_ATTRS),
            ("colorfill", _ASPECT_COLORFILL_ATTRS),
            ("objstyles", _ASPECT_OBJSTYLES_ATTRS),
            ("nested",    _ASPECT_NESTED_ATTRS),
        )
        for key, attrs in inherit_pairs:
            if key not in dirty:
                continue
            lv_name = vals.get(key)
            if not lv_name:
                continue
            try:
                _write_aspect_inherit(settings, attrs, lv_name)
            except Exception:
                pass
        # Paired aspects (each takes a LinkVisibility + specific value)
        if "phase" in dirty:
            self._write_paired(settings, "SetPhase", vals.get("phase"), is_eid=True)
        if "phasefilter" in dirty:
            self._write_paired(settings, "SetPhaseFilter", vals.get("phasefilter"), is_eid=True)
        if "detail" in dirty:
            self._write_paired(settings, "SetViewDetailLevel", vals.get("detail"),
                               is_eid=False, enum_name="ViewDetailLevel")
        if "discipline" in dirty:
            self._write_paired(settings, "SetDiscipline", vals.get("discipline"),
                               is_eid=False, enum_name="ViewDiscipline")
        # Linked view picker — settings.LinkedViewId
        if "linkedview" in dirty:
            view_eid_int = vals.get("linkedview")
            try:
                from Autodesk.Revit.DB import ElementId
                if view_eid_int is None or view_eid_int == -1:
                    new_id = ElementId.InvalidElementId
                else:
                    new_id = make_eid(int(view_eid_int))
                # Try property assignment first; fall back to setter method
                try:
                    settings.LinkedViewId = new_id
                except Exception:
                    try:
                        settings.SetLinkedView(new_id)
                    except Exception:
                        pass
            except Exception:
                pass

    def _write_paired(self, settings, set_method_name, value_tuple,
                      is_eid=True, enum_name=None):
        """value_tuple is (lv_name, specific_int_or_None). Calls
        settings.<set_method_name>(LinkVisibility, specific_value) where
        specific_value is an ElementId (is_eid=True) or .NET enum (False)."""
        if value_tuple is None:
            return
        try:
            lv_name, spec = value_tuple
        except Exception:
            return
        lv = _lv_name_to_enum(lv_name)
        if lv is None:
            return
        # Build the specific value the setter expects
        if is_eid:
            try:
                from Autodesk.Revit.DB import ElementId
                if lv_name == "Custom" and spec is not None and spec != -1:
                    specific = make_eid(int(spec))
                else:
                    specific = ElementId.InvalidElementId
            except Exception:
                return
        else:
            # enum (ViewDetailLevel / ViewDiscipline)
            specific = None
            if lv_name == "Custom" and spec is not None:
                try:
                    from System import Enum
                    enum_type = None
                    if enum_name == "ViewDetailLevel":
                        from Autodesk.Revit.DB import ViewDetailLevel
                        enum_type = ViewDetailLevel
                    elif enum_name == "ViewDiscipline":
                        from Autodesk.Revit.DB import ViewDiscipline
                        enum_type = ViewDiscipline
                    if enum_type is not None:
                        specific = Enum.ToObject(enum_type, int(spec))
                except Exception:
                    specific = None
            # When not Custom, pass a default (Coarse / Architectural)
            if specific is None:
                try:
                    from System import Enum
                    if enum_name == "ViewDetailLevel":
                        from Autodesk.Revit.DB import ViewDetailLevel
                        specific = ViewDetailLevel.Coarse
                    elif enum_name == "ViewDiscipline":
                        from Autodesk.Revit.DB import ViewDiscipline
                        specific = ViewDiscipline.Architectural
                except Exception:
                    return
        try:
            m = getattr(settings, set_method_name, None)
            if callable(m):
                m(lv, specific)
        except Exception:
            pass

    def _on_open_native(self, sender, args):
        """Post Revit's Manage View Templates command and close our dialogs
        so Revit's native UI takes over (the API can't set Custom mode for
        a link's V/G overrides, but Revit's own dialog can). After the
        user sets Custom there and closes Revit's dialog, they can reopen
        this tool to keep editing — next load will read the Custom mode
        back as 'Custom (read-only via API)'.

        Modal dialogs block Revit's message loop, so we have to fully
        close ours before the posted command can actually fire."""
        posted = _post_manage_view_templates()
        # Close this dialog, then the parent main form, so Revit can
        # actually process the queued command.
        try:
            owner = self.Owner
        except Exception:
            owner = None
        try:
            self.Close()
        except Exception:
            pass
        if owner is not None:
            try:
                owner.Close()
            except Exception:
                pass
        if not posted:
            dbhms_ui.info(
                "Couldn't auto-open Revit's View Templates dialog "
                "(PostableCommand wasn't recognized on this Revit version). "
                "Open it manually: View tab → View Templates → Manage View "
                "Templates → pick template → Edit next to V/G Overrides RVT "
                "Links → Custom.",
                title="Open in Revit's native dialog")


# ===========================================================================
# Multi-select helper — shared across every list in this tool
# ===========================================================================

class RowMultiSelectHelper(object):
    """Wires shift/ctrl-click multi-select onto a list of row dicts and
    propagates checkbox toggles across the highlight set.

    Each row dict in `rows` must have at least:
        "row"  : the outer Border (clicking it manages highlight)
        "name" : a string identifier unique within the list
    Optionally, a `checkbox_keys` tuple names which checkboxes on each row
    propagate. When the user ticks a checkbox in a row that's part of a
    multi-row highlight, the new check state is applied to every other
    highlighted row's same-key checkbox.

    `on_select` is called with the **full list of currently highlighted
    names**, in row order, after every click. Single-click yields a list
    of length 1; shift/ctrl-click yields the multi-select set. The dialog
    decides how to render that (single name vs. "N selected: ...").

    Single click  -> highlight just this row
    Shift-click   -> extend highlight from last clicked to this one
    Ctrl-click    -> toggle this row in/out of the highlight
    """

    HL_BG     = (0xEB, 0xF8, 0xFF)
    HL_BORDER = (0x31, 0x82, 0xCE)
    NORMAL_BG     = (0xFF, 0xFF, 0xFF)
    NORMAL_BORDER = (0xF1, 0xF5, 0xF9)

    def __init__(self, rows, on_select=None, checkbox_keys=("chk_show",)):
        self._rows = rows
        self._on_select = on_select
        self._checkbox_keys = list(checkbox_keys)
        self._highlighted = set()
        self._last_clicked = None
        for info in rows:
            name = info["name"]
            info["row"].MouseLeftButtonDown += self._make_row_handler(name)
            for key in self._checkbox_keys:
                chk = info.get(key)
                if chk is not None:
                    chk.Tag = (name, key)
                    chk.Click += self._on_checkbox_clicked

    def _make_row_handler(self, name):
        def handler(sender, args):
            self._on_row_clicked(name)
        return handler

    def _on_row_clicked(self, name):
        from System.Windows.Input import Keyboard, ModifierKeys
        shift = bool(int(Keyboard.Modifiers) & int(ModifierKeys.Shift))
        ctrl  = bool(int(Keyboard.Modifiers) & int(ModifierKeys.Control))
        names = [info["name"] for info in self._rows]
        if (shift and self._last_clicked is not None
                and self._last_clicked in names and name in names):
            li = names.index(self._last_clicked)
            ti = names.index(name)
            lo, hi = min(li, ti), max(li, ti)
            self._highlighted = set(names[lo:hi + 1])
        elif ctrl:
            if name in self._highlighted:
                self._highlighted.discard(name)
            else:
                self._highlighted.add(name)
            self._last_clicked = name
        else:
            self._highlighted = {name}
            self._last_clicked = name
        if self._on_select is not None:
            highlighted_in_order = [info["name"] for info in self._rows
                                    if info["name"] in self._highlighted]
            self._on_select(highlighted_in_order)
        self._render()

    def _on_checkbox_clicked(self, sender, args):
        tag = getattr(sender, "Tag", None)
        if (not isinstance(tag, tuple)) or len(tag) != 2:
            return
        target_name, key = tag
        if (target_name in self._highlighted
                and len(self._highlighted) > 1):
            new_state = bool(sender.IsChecked)
            for info in self._rows:
                if (info["name"] in self._highlighted
                        and info["name"] != target_name):
                    chk = info.get(key)
                    if chk is not None:
                        chk.IsChecked = new_state

    def _render(self):
        hl_bg     = SolidColorBrush(Color.FromRgb(*self.HL_BG))
        hl_border = SolidColorBrush(Color.FromRgb(*self.HL_BORDER))
        n_bg      = SolidColorBrush(Color.FromRgb(*self.NORMAL_BG))
        n_border  = SolidColorBrush(Color.FromRgb(*self.NORMAL_BORDER))
        for info in self._rows:
            if info["name"] in self._highlighted:
                info["row"].Background  = hl_bg
                info["row"].BorderBrush = hl_border
                info["row"].BorderThickness = Thickness(1)
            else:
                info["row"].Background  = n_bg
                info["row"].BorderBrush = n_border
                info["row"].BorderThickness = Thickness(0, 0, 0, 1)

    def clear_highlight(self):
        """Drop the current highlight set entirely. Used when the dialog
        does an All/None action so the right-panel scope resets to the
        all-rows fallback."""
        self._highlighted = set()
        self._last_clicked = None
        self._render()
        if self._on_select is not None:
            try:
                self._on_select([])
            except Exception:
                pass


# ===========================================================================
# Row builders for sub-dialog lists
# ===========================================================================

def _build_category_row(cat_name, on_click=None):
    """Return a dict {row, chk_show, name} so the dialog can later
    bulk-toggle Show checkboxes and highlight the selected row."""
    outer = Border()
    outer.Padding = Thickness(0, 2, 0, 2)
    outer.BorderBrush = SolidColorBrush(Color.FromRgb(0xF1, 0xF5, 0xF9))
    outer.BorderThickness = Thickness(0, 0, 0, 1)
    outer.Background = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
    outer.Cursor = Cursors.Hand

    grid = Grid()
    for w in (40, None, 50, 50):
        c = ColumnDefinition()
        if w is None:
            from System.Windows import GridLength, GridUnitType
            c.Width = GridLength(1.0, GridUnitType.Star)
        else:
            from System.Windows import GridLength
            c.Width = GridLength(float(w))
        grid.ColumnDefinitions.Add(c)

    chk_show = CheckBox()
    chk_show.IsChecked = True
    chk_show.HorizontalAlignment = HorizontalAlignment.Center
    chk_show.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_show, 0)
    grid.Children.Add(chk_show)

    name_block = TextBlock()
    name_block.Text = cat_name
    name_block.VerticalAlignment = VerticalAlignment.Center
    name_block.Padding = Thickness(6, 4, 6, 4)
    Grid.SetColumn(name_block, 1)
    grid.Children.Add(name_block)

    chk_ht = CheckBox()
    chk_ht.HorizontalAlignment = HorizontalAlignment.Center
    chk_ht.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_ht, 2)
    grid.Children.Add(chk_ht)

    or_dot = TextBlock()
    or_dot.Text = ""
    or_dot.HorizontalAlignment = HorizontalAlignment.Center
    or_dot.VerticalAlignment   = VerticalAlignment.Center
    or_dot.FontSize = 11
    or_dot.Foreground = SolidColorBrush(Color.FromRgb(0xA0, 0xAE, 0xC0))
    Grid.SetColumn(or_dot, 3)
    grid.Children.Add(or_dot)

    outer.Child = grid

    if on_click is not None:
        def handler(sender, args):
            on_click(cat_name)
        outer.MouseLeftButtonDown += handler

    return {"row": outer, "chk_show": chk_show, "name": cat_name}


def _build_import_link_row(cad_name, on_click=None):
    outer = Border()
    outer.Padding = Thickness(0, 4, 0, 4)
    outer.BorderBrush = SolidColorBrush(Color.FromRgb(0xCB, 0xD5, 0xE0))
    outer.BorderThickness = Thickness(0, 0, 0, 1)
    outer.Background = SolidColorBrush(Color.FromRgb(0xED, 0xF2, 0xF7))
    outer.Cursor = Cursors.Hand

    grid = Grid()
    from System.Windows import GridLength, GridUnitType
    # CAD-file row: wider expander column so the ▸/▾ glyph is unmistakable.
    widths = [28, 40, None, 50, 50]
    for w in widths:
        c = ColumnDefinition()
        if w is None:
            c.Width = GridLength(1.0, GridUnitType.Star)
        else:
            c.Width = GridLength(float(w))
        grid.ColumnDefinitions.Add(c)

    expander_btn = Button()
    expander_btn.Content = u"▸"  # ▸ collapsed by default
    expander_btn.Background = SolidColorBrush(Color.FromRgb(0xED, 0xF2, 0xF7))
    expander_btn.BorderThickness = Thickness(0)
    expander_btn.Cursor = Cursors.Hand
    expander_btn.FontSize = 14
    expander_btn.FontWeight = _semi_bold()
    expander_btn.Foreground = SolidColorBrush(Color.FromRgb(0x2D, 0x37, 0x48))
    expander_btn.Padding = Thickness(0)
    expander_btn.ToolTip = "Expand / collapse layers in this CAD file"
    Grid.SetColumn(expander_btn, 0)
    grid.Children.Add(expander_btn)

    chk_show = CheckBox()
    chk_show.IsChecked = True
    chk_show.HorizontalAlignment = HorizontalAlignment.Center
    chk_show.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_show, 1)
    grid.Children.Add(chk_show)

    name_block = TextBlock()
    name_block.Text = cad_name
    name_block.VerticalAlignment = VerticalAlignment.Center
    name_block.Padding = Thickness(6, 4, 6, 4)
    name_block.FontWeight = _semi_bold()
    Grid.SetColumn(name_block, 2)
    grid.Children.Add(name_block)

    chk_ht = CheckBox()
    chk_ht.HorizontalAlignment = HorizontalAlignment.Center
    chk_ht.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_ht, 3)
    grid.Children.Add(chk_ht)

    or_dot = TextBlock()
    or_dot.HorizontalAlignment = HorizontalAlignment.Center
    or_dot.VerticalAlignment   = VerticalAlignment.Center
    or_dot.Text = ""
    Grid.SetColumn(or_dot, 4)
    grid.Children.Add(or_dot)

    outer.Child = grid

    if on_click is not None:
        def handler(sender, args):
            on_click()
        name_block.MouseLeftButtonDown += handler

    return {"row": outer, "expander_btn": expander_btn, "chk_show": chk_show,
            "name": cad_name}


def _build_import_layer_row(layer_name, parent_cad, on_click=None):
    outer = Border()
    outer.Padding = Thickness(0, 2, 0, 2)
    outer.BorderBrush = SolidColorBrush(Color.FromRgb(0xF1, 0xF5, 0xF9))
    outer.BorderThickness = Thickness(0, 0, 0, 1)
    outer.Background = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
    outer.Cursor = Cursors.Hand

    grid = Grid()
    from System.Windows import GridLength, GridUnitType
    # Match the CAD-row 28-wide expander column so columns align across rows.
    widths = [28, 40, None, 50, 50]
    for w in widths:
        c = ColumnDefinition()
        if w is None:
            c.Width = GridLength(1.0, GridUnitType.Star)
        else:
            c.Width = GridLength(float(w))
        grid.ColumnDefinitions.Add(c)

    spacer = TextBlock()
    Grid.SetColumn(spacer, 0)
    grid.Children.Add(spacer)

    chk_show = CheckBox()
    chk_show.IsChecked = True
    chk_show.HorizontalAlignment = HorizontalAlignment.Center
    chk_show.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_show, 1)
    grid.Children.Add(chk_show)

    name_block = TextBlock()
    name_block.Text = u"  └  " + layer_name
    name_block.VerticalAlignment = VerticalAlignment.Center
    name_block.Padding = Thickness(6, 4, 6, 4)
    name_block.Foreground = SolidColorBrush(Color.FromRgb(0x4A, 0x55, 0x68))
    Grid.SetColumn(name_block, 2)
    grid.Children.Add(name_block)

    chk_ht = CheckBox()
    chk_ht.HorizontalAlignment = HorizontalAlignment.Center
    chk_ht.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_ht, 3)
    grid.Children.Add(chk_ht)

    or_dot = TextBlock()
    or_dot.HorizontalAlignment = HorizontalAlignment.Center
    or_dot.VerticalAlignment   = VerticalAlignment.Center
    or_dot.Text = ""
    Grid.SetColumn(or_dot, 4)
    grid.Children.Add(or_dot)

    outer.Child = grid

    full_label = "{0} > {1}".format(parent_cad, layer_name)
    if on_click is not None:
        def handler(sender, args):
            on_click()
        outer.MouseLeftButtonDown += handler

    return {"row": outer, "chk_show": chk_show, "name": full_label}


def _build_filter_row(filter_name, enabled, visible, on_click=None):
    outer = Border()
    outer.Padding = Thickness(0, 2, 0, 2)
    outer.BorderBrush = SolidColorBrush(Color.FromRgb(0xF1, 0xF5, 0xF9))
    outer.BorderThickness = Thickness(0, 0, 0, 1)
    outer.Background = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
    outer.Cursor = Cursors.Hand

    grid = Grid()
    from System.Windows import GridLength, GridUnitType
    widths = [None, 60, 60, 60]
    for w in widths:
        c = ColumnDefinition()
        if w is None:
            c.Width = GridLength(1.0, GridUnitType.Star)
        else:
            c.Width = GridLength(float(w))
        grid.ColumnDefinitions.Add(c)

    name_block = TextBlock()
    name_block.Text = filter_name
    name_block.VerticalAlignment = VerticalAlignment.Center
    name_block.Padding = Thickness(6, 4, 6, 4)
    Grid.SetColumn(name_block, 0)
    grid.Children.Add(name_block)

    chk_en = CheckBox()
    chk_en.IsChecked = bool(enabled)
    chk_en.HorizontalAlignment = HorizontalAlignment.Center
    chk_en.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_en, 1)
    grid.Children.Add(chk_en)

    chk_vis = CheckBox()
    chk_vis.IsChecked = bool(visible)
    chk_vis.HorizontalAlignment = HorizontalAlignment.Center
    chk_vis.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(chk_vis, 2)
    grid.Children.Add(chk_vis)

    or_dot = TextBlock()
    or_dot.Text = ""
    or_dot.HorizontalAlignment = HorizontalAlignment.Center
    or_dot.VerticalAlignment   = VerticalAlignment.Center
    Grid.SetColumn(or_dot, 3)
    grid.Children.Add(or_dot)

    outer.Child = grid

    if on_click is not None:
        def handler(sender, args):
            on_click()
        outer.MouseLeftButtonDown += handler

    return {"row": outer, "chk_visible": chk_vis, "chk_enabled": chk_en, "name": filter_name}


def _semi_bold():
    from System.Windows import FontWeights
    return FontWeights.SemiBold


def _multi_select_summary(names, kind="items"):
    """Format a one-line multi-select header (legacy, used as a tooltip / fallback)."""
    n = len(names)
    preview = ", ".join(names[:3])
    if n > 3:
        preview += ", +{0} more".format(n - 3)
    return "{0} {1} selected: {2}".format(n, kind, preview)


_MULTI_SELECT_INLINE_LIMIT = 4


def _render_multi_select(header_tb, names_panel, names, kind, on_show_all=None):
    """Render the right-panel selection summary as a vertical list.

    - 0 names: leave the existing header text alone, clear the panel.
    - 1 name:  header shows the name, panel is empty.
    - 2-4 names: header shows count + kind, panel lists each name on its own line.
    - 5+ names: panel lists the first 4 names, then a clickable
      '...and N more (click to see all)' affordance that calls on_show_all(names).
    """
    if not names:
        names_panel.Children.Clear()
        return
    names_panel.Children.Clear()
    if len(names) == 1:
        header_tb.Text = names[0]
        return
    header_tb.Text = "{0} {1} selected:".format(len(names), kind)
    inline = names[:_MULTI_SELECT_INLINE_LIMIT]
    for nm in inline:
        tb = TextBlock()
        tb.Text = u"• " + nm
        tb.FontSize = 11
        tb.Foreground = SolidColorBrush(Color.FromRgb(0x4A, 0x55, 0x68))
        tb.Margin = Thickness(6, 1, 0, 1)
        from System.Windows import TextWrapping
        tb.TextWrapping = TextWrapping.Wrap
        names_panel.Children.Add(tb)
    overflow = len(names) - len(inline)
    if overflow > 0:
        link = TextBlock()
        link.Text = "...and {0} more (click to see all)".format(overflow)
        link.FontSize = 11
        link.Foreground = SolidColorBrush(Color.FromRgb(0x2B, 0x6C, 0xB0))
        link.Margin = Thickness(6, 6, 0, 1)
        link.Cursor = Cursors.Hand
        from System.Windows import TextDecorations
        link.TextDecorations = TextDecorations.Underline
        if on_show_all is not None:
            def handler(sender, args, _names=list(names)):
                on_show_all(_names)
            link.MouseLeftButtonDown += handler
        names_panel.Children.Add(link)


def _show_full_name_list(title, names):
    """Open a simple modal listing every name. Used by the 'and N more' link."""
    body = "\n".join(u"• " + n for n in names)
    dbhms_ui.info(body, title=title)


def _flash_applied(status_textblock, message, revert_text="", revert_italic=True):
    """Briefly turn the given status TextBlock green with a check-mark
    confirmation, then fade back to the default text after ~3 seconds.
    Avoids the 'OK closes the window' pattern so users can keep editing,
    while still giving visual confirmation that something landed."""
    if status_textblock is None:
        return
    from System.Windows.Media import SolidColorBrush, Color
    from System.Windows import FontStyles
    from System.Windows.Threading import DispatcherTimer
    from System import TimeSpan

    accent = SolidColorBrush(Color.FromRgb(0x22, 0x8B, 0x22))   # green
    default = SolidColorBrush(Color.FromRgb(0x4A, 0x55, 0x68))  # status gray
    try:
        status_textblock.Text = u"✓ " + message
        status_textblock.Foreground = accent
        status_textblock.FontStyle = FontStyles.Normal
    except Exception:
        return

    timer = DispatcherTimer()
    try:
        timer.Interval = TimeSpan.FromSeconds(3.0)
    except Exception:
        return

    def on_tick(s, e):
        try:
            timer.Stop()
        except Exception:
            pass
        try:
            status_textblock.Foreground = default
            status_textblock.FontStyle = FontStyles.Italic if revert_italic else FontStyles.Normal
            status_textblock.Text = revert_text or ""
        except Exception:
            pass
    timer.Tick += on_tick
    try:
        timer.Start()
    except Exception:
        pass


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    if doc is None:
        forms.alert("No active Revit document.", exitscript=True)
        return
    ViewTemplatesManagerForm().ShowDialog()


if __name__ == "__main__":
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
