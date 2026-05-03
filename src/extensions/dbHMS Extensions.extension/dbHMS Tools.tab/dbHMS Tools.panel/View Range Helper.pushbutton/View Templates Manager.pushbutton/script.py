# -*- coding: utf-8 -*-
"""View Templates Manager — All-in-one view template management for Revit.

Layout mirrors Revit's native View Template editor:
  - Left: list of view templates with checkbox multi-select
  - Right: tabbed editor — Properties / VG Overrides / Revit Links / Usage

Selection drives mode:
  - 0 templates checked  → editor disabled
  - 1 template checked   → single-edit mode (shows current values)
  - 2+ templates checked → bulk mode (yellow banner, override-builder UI)
"""
__title__ = "View Templates\nManager"
__doc__   = "Manage view templates: edit, duplicate, apply, and bulk-modify across the project."

import clr
import re
import sys
import traceback

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")

from Autodesk.Revit.DB import (
    FilteredElementCollector, View, ViewType, ViewSheet, ViewPlan, Viewport,
    Transaction, TransactionGroup, BuiltInParameter, BuiltInCategory,
    ElementId, CategoryType, Category, OverrideGraphicSettings,
    RevitLinkInstance, RevitLinkType, ParameterFilterElement,
    PlanViewPlane,
)

try:
    from Autodesk.Revit.DB import RevitLinkGraphicsSettings
    _HAS_LINK_GRAPHICS = True
except ImportError:
    _HAS_LINK_GRAPHICS = False

# Newer API: LinkVisibility enum
try:
    from Autodesk.Revit.DB import LinkVisibility
    _HAS_LINK_VISIBILITY = True
except ImportError:
    _HAS_LINK_VISIBILITY = False

from Autodesk.Revit.UI import TaskDialog, TaskDialogCommonButtons, TaskDialogResult

import System
from System.Windows import (
    Window, Thickness, HorizontalAlignment, VerticalAlignment,
    Visibility, RoutedEventHandler, MessageBox, MessageBoxButton,
    MessageBoxResult, MessageBoxImage
)
from System.Windows.Controls import (
    DataGrid, DataGridCheckBoxColumn, DataGridTextColumn,
    DataGridTemplateColumn, DataGridLength,
    ComboBox, ComboBoxItem, TextBox, ListBox, ListBoxItem,
    Button, CheckBox, RadioButton, StackPanel, Grid, Border,
    TextBlock, ScrollViewer, Expander, TabControl, TabItem,
    WrapPanel, Orientation, Label, Separator
)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Threading import DispatcherPriority
from System import Action
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Data import (
    CollectionViewSource, Binding, BindingMode
)
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List
from System.ComponentModel import (
    INotifyPropertyChanged, PropertyChangedEventArgs,
    SortDescription, ListSortDirection
)
import System.Windows.Markup as Markup
from System.Windows.Interop import WindowInteropHelper

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def eid_int(eid):
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def viewtype_label(vt):
    """Return a short human-readable string for a ViewType enum value."""
    mapping = {
        ViewType.FloorPlan:          "Floor Plan",
        ViewType.CeilingPlan:        "Ceiling Plan",
        ViewType.Elevation:          "Elevation",
        ViewType.Section:            "Section",
        ViewType.Detail:             "Detail",
        ViewType.ThreeD:             "3D",
        ViewType.Schedule:           "Schedule",
        ViewType.DraftingView:       "Drafting",
        ViewType.Legend:             "Legend",
        ViewType.EngineeringPlan:    "Mech Plan",
        ViewType.AreaPlan:           "Area Plan",
        ViewType.Walkthrough:        "Walkthrough",
        ViewType.Rendering:          "Rendering",
    }
    return mapping.get(vt, str(vt))


def is_plan_view_type(vt):
    return vt in (ViewType.FloorPlan, ViewType.CeilingPlan,
                  ViewType.AreaPlan,  ViewType.EngineeringPlan)


# Map of integer scale denominator -> imperial display string
_IMPERIAL_SCALES = {
    1:    '12" = 1\'-0"',
    2:    '6" = 1\'-0"',
    4:    '3" = 1\'-0"',
    8:    '1 1/2" = 1\'-0"',
    12:   '1" = 1\'-0"',
    16:   '3/4" = 1\'-0"',
    24:   '1/2" = 1\'-0"',
    32:   '3/8" = 1\'-0"',
    48:   '1/4" = 1\'-0"',
    64:   '3/16" = 1\'-0"',
    96:   '1/8" = 1\'-0"',
    128:  '3/32" = 1\'-0"',
    192:  '1/16" = 1\'-0"',
    384:  '1/32" = 1\'-0"',
    768:  '1/64" = 1\'-0"',
}

def scale_label(scale):
    """Return imperial fractional label, or '1:N' if non-standard."""
    if not scale or scale <= 0:
        return "-"
    try:
        s = int(scale)
    except Exception:
        return "1:{}".format(scale)
    if s in _IMPERIAL_SCALES:
        return _IMPERIAL_SCALES[s]
    return "1:{}".format(s)


def get_detail_level_name(val):
    return {1: "Coarse", 2: "Medium", 3: "Fine"}.get(val, "-")


def get_parts_visibility_name(val):
    return {0: "Show Original", 1: "Show Parts", 2: "Show Both"}.get(val, "-")


def get_discipline_name(val):
    return {
        0:   "Architectural",
        1:   "Structural",
        2:   "Mechanical",
        3:   "Electrical",
        4:   "Plumbing",
        255: "Coordination",
    }.get(val, str(val))


def get_all_templates(doc):
    """Return list of all view templates sorted by name."""
    result = []
    for v in FilteredElementCollector(doc).OfClass(View):
        if v.IsTemplate:
            result.append(v)
    return sorted(result, key=lambda v: v.Name.lower())


def build_usage_map(doc):
    """Return dict: template ElementId int → count of views using it."""
    usage = {}
    for v in FilteredElementCollector(doc).OfClass(View):
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


# ──────────────────────────────────────────────
# Revit Link helpers
# ──────────────────────────────────────────────

def get_link_instances(doc):
    """Return list of all RevitLinkInstance elements."""
    try:
        return list(FilteredElementCollector(doc).OfClass(RevitLinkInstance))
    except Exception:
        return []


def get_link_name(link, host_doc):
    """Return a clean display name for a Revit link instance.

    Priority:
      1. Linked document title (best — actual file name)
      2. RevitLinkType name (the family/type name)
      3. RVT_LINK_INSTANCE_NAME parameter
      4. .Name as fallback
    """
    # 1) Linked doc title
    try:
        ldoc = link.GetLinkDocument()
        if ldoc and ldoc.Title:
            return ldoc.Title
    except Exception:
        pass
    # 2) Link type name
    try:
        type_id = link.GetTypeId()
        if type_id and eid_int(type_id) != -1:
            ltype = host_doc.GetElement(type_id)
            if ltype and ltype.Name:
                # Strip ".rvt" suffix if present for cleaner display
                nm = ltype.Name
                if nm.lower().endswith(".rvt"):
                    nm = nm[:-4]
                return nm
    except Exception:
        pass
    # 3) Instance name parameter
    try:
        p = link.get_Parameter(BuiltInParameter.RVT_LINK_INSTANCE_NAME)
        if p:
            v = p.AsString()
            if v:
                return v
    except Exception:
        pass
    # 4) .Name
    try:
        return link.Name
    except Exception:
        return "Link"


def get_link_types_deduped(doc):
    """Return list of dicts, one per unique link type:
       { 'name': str, 'type_id': ElementId, 'instance_ids': [ElementId, ...],
         'first_instance': RevitLinkInstance }
    """
    insts = get_link_instances(doc)
    by_type = {}
    for inst in insts:
        try:
            tid = inst.GetTypeId()
            if not tid or eid_int(tid) == -1:
                # Treat as orphan — group by instance id
                key = -eid_int(inst.Id)
            else:
                key = eid_int(tid)
        except Exception:
            key = -eid_int(inst.Id)
        if key not in by_type:
            by_type[key] = {
                "name": get_link_name(inst, doc),
                "type_id": inst.GetTypeId() if hasattr(inst, "GetTypeId") else None,
                "instance_ids": [inst.Id],
                "first_instance": inst,
            }
        else:
            by_type[key]["instance_ids"].append(inst.Id)
    # Stable sort by name
    return sorted(by_type.values(), key=lambda d: d["name"].lower())


def get_link_display_type(view_template, link_id):
    """Return a human-readable string for the link's display type in this template."""
    if view_template is None:
        return "-"
    if not _HAS_LINK_GRAPHICS:
        return "N/A"
    try:
        settings = view_template.GetLinkOverrides(link_id)
        if settings is None:
            return "Not Overridden"
        try:
            vt = settings.GetLinkViewType()
        except Exception:
            return "By Host View"
        vt_str = str(vt)
        if "ByHostView" in vt_str or vt_str == "0":
            return "By Host View"
        elif "ByLinkedView" in vt_str or vt_str == "1":
            return "By Linked View"
        elif "Custom" in vt_str:
            return "Custom"
        else:
            return vt_str
    except Exception:
        return "Not Overridden"


def get_link_halftone(view_template, link_id):
    """Return True if the link is set to halftone in this template, else False."""
    if view_template is None or not _HAS_LINK_GRAPHICS:
        return False
    try:
        settings = view_template.GetLinkOverrides(link_id)
        if settings is None:
            return False
        return settings.Halftone
    except Exception:
        return False


# ──────────────────────────────────────────────
# Category helpers
# ──────────────────────────────────────────────

def get_model_categories(doc):
    """Return sorted list of (name, ElementId) for model categories."""
    result = []
    try:
        cats = doc.Settings.Categories
        for cat in cats:
            try:
                if cat.CategoryType != CategoryType.Model:
                    continue
                result.append((cat.Name, cat.Id))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(result, key=lambda x: x[0])


def get_annotation_categories(doc):
    """Return sorted list of (name, ElementId) for annotation categories."""
    result = []
    try:
        cats = doc.Settings.Categories
        for cat in cats:
            try:
                if cat.CategoryType != CategoryType.Annotation:
                    continue
                result.append((cat.Name, cat.Id))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(result, key=lambda x: x[0])


def get_imported_categories(doc):
    """Return sorted list of (name, ElementId) for imported (CAD) sub-categories.

    Mirrors Revit's VG > Imported Categories tab. Recurses two levels deep:
      OST_ImportObjectStyles
        ├─ Imports in Families (and each DWG/DXF link)
        │    ├─ Layer1
        │    └─ Layer2
        └─ ...
    DWG layers are formatted "DWG_Name > Layer_Name" so they are easy to spot.
    """
    result = []
    try:
        cats = doc.Settings.Categories
        for cat in cats:
            try:
                if eid_int(cat.Id) != int(BuiltInCategory.OST_ImportObjectStyles):
                    continue
                # The OST_ImportObjectStyles parent itself
                result.append((cat.Name, cat.Id))
                # Children: each imported file shows as a subcategory
                try:
                    for sub in cat.SubCategories:
                        sub_name = sub.Name
                        result.append((sub_name, sub.Id))
                        # Layers within this CAD link (depth-2)
                        try:
                            for layer in sub.SubCategories:
                                result.append(("{} > {}".format(sub_name, layer.Name), layer.Id))
                        except Exception:
                            pass
                except Exception:
                    pass
                break
            except Exception:
                pass
    except Exception:
        pass
    return sorted(result, key=lambda x: x[0])


def category_can_be_hidden(view, cat_id):
    """Check if category visibility can be controlled in this view."""
    try:
        return view.CanCategoryBeHidden(cat_id)
    except Exception:
        return False


# ──────────────────────────────────────────────
# Filter helpers
# ──────────────────────────────────────────────

def get_project_filters(doc):
    """Return list of (name, ElementId) for all ParameterFilterElement in project."""
    result = []
    try:
        for f in FilteredElementCollector(doc).OfClass(ParameterFilterElement):
            try:
                result.append((f.Name, f.Id))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(result, key=lambda x: x[0])


def get_template_filters(view_template, doc):
    """Return list of dicts for filters applied to a view template.
       Each dict: { 'name', 'filter_id', 'visible', 'enabled' }
    """
    result = []
    if view_template is None:
        return result
    try:
        ids = view_template.GetFilters()
    except Exception:
        return result
    for fid in ids:
        try:
            f = doc.GetElement(fid)
            name = f.Name if f else "<unknown>"
            try:
                vis = view_template.GetFilterVisibility(fid)
            except Exception:
                vis = True
            try:
                enabled = view_template.GetIsFilterEnabled(fid)
            except Exception:
                enabled = True
            result.append({
                "name": name, "filter_id": fid,
                "visible": vis, "enabled": enabled,
            })
        except Exception:
            pass
    return result


# ──────────────────────────────────────────────
# Phase filter helpers
# ──────────────────────────────────────────────

def get_phase_filters(doc):
    """Return list of (name, ElementId) for all PhaseFilter elements."""
    result = []
    try:
        from Autodesk.Revit.DB import PhaseFilter
        for pf in FilteredElementCollector(doc).OfClass(PhaseFilter):
            try:
                result.append((pf.Name, pf.Id))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(result, key=lambda x: x[0])



# ──────────────────────────────────────────────
# Template-controlled parameter helpers
# ──────────────────────────────────────────────

def get_template_include_specs(view_template):
    """Return list of (BuiltInParameter, label, parameter_id_or_None)
    for the inclusion-controllable properties this view template supports.
    """
    specs = []
    candidates = [
        (BuiltInParameter.VIEW_SCALE,            "View Scale"),
        (BuiltInParameter.VIEW_DETAIL_LEVEL,     "Detail Level"),
        (BuiltInParameter.VIEW_PARTS_VISIBILITY, "Parts Visibility"),
        (BuiltInParameter.VIEW_PHASE_FILTER,     "Phase Filter"),
        (BuiltInParameter.VIEW_DISCIPLINE,       "Discipline"),
    ]
    for bip, label in candidates:
        try:
            p = view_template.get_Parameter(bip)
            specs.append((bip, label, p.Id if p else None))
        except Exception:
            specs.append((bip, label, None))
    return specs


def is_template_param_included(view_template, param_id):
    """True if the parameter (by ElementId) is currently controlled by the
    template (i.e., NOT in the non-controlled list)."""
    if param_id is None: return True
    try:
        non_ctrl = view_template.GetNonControlledTemplateParameterIds()
        for eid in non_ctrl:
            if eid_int(eid) == eid_int(param_id):
                return False
        return True
    except Exception:
        return True


# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class TemplateItem(INotifyPropertyChanged):
    def __init__(self, view, usage_count=0):
        self._view         = view
        self._is_selected  = False
        self._usage_count  = usage_count
        self._handlers     = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def View(self):       return self._view
    @property
    def Id(self):         return self._view.Id
    @property
    def Name(self):       return self._view.Name
    @property
    def ViewTypeName(self):
        try:    return viewtype_label(self._view.ViewType)
        except Exception: return "-"
    @property
    def Scale(self):
        try:    return scale_label(self._view.Scale)
        except Exception: return "-"
    @property
    def UsedBy(self):     return self._usage_count
    @property
    def IsUsed(self):     return self._usage_count > 0

    @property
    def IsSelected(self):
        return self._is_selected
    @IsSelected.setter
    def IsSelected(self, value):
        if self._is_selected != value:
            self._is_selected = value
            self._notify("IsSelected")


class CategoryVisItem(INotifyPropertyChanged):
    def __init__(self, name, cat_id, is_hidden):
        self._name      = name
        self._cat_id    = cat_id
        self._is_hidden = is_hidden
        self._handlers  = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def Name(self):   return self._name
    @property
    def CatId(self):  return self._cat_id
    @property
    def IsHidden(self): return self._is_hidden
    @IsHidden.setter
    def IsHidden(self, v):
        if self._is_hidden != v:
            self._is_hidden = v
            self._notify("IsHidden")
            self._notify("IsVisible")
    @property
    def IsVisible(self): return not self._is_hidden
    @IsVisible.setter
    def IsVisible(self, v):
        self.IsHidden = not v


class FilterRowItem(INotifyPropertyChanged):
    """A single filter row inside a view template's Filters tab (single-edit mode)."""
    def __init__(self, name, filter_id, visible, enabled):
        self._name      = name
        self._filter_id = filter_id
        self._visible   = visible
        self._enabled   = enabled
        self._handlers  = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def Name(self):     return self._name
    @property
    def FilterId(self): return self._filter_id
    @property
    def Visible(self):  return self._visible
    @Visible.setter
    def Visible(self, v):
        if self._visible != v:
            self._visible = v
            self._notify("Visible")
    @property
    def Enabled(self):  return self._enabled
    @Enabled.setter
    def Enabled(self, v):
        if self._enabled != v:
            self._enabled = v
            self._notify("Enabled")


class LinkRowItem(INotifyPropertyChanged):
    """One row per linked .rvt file (deduped) in the Links tab."""
    def __init__(self, name, type_id, instance_ids, display_type, halftone):
        self._name         = name
        self._type_id      = type_id
        self._instance_ids = instance_ids
        self._display_type = display_type
        self._halftone     = halftone
        self._instances    = len(instance_ids)
        self._handlers     = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def Name(self):        return self._name
    @property
    def TypeId(self):      return self._type_id
    @property
    def InstanceIds(self): return self._instance_ids
    @property
    def Instances(self):   return self._instances
    @property
    def DisplayType(self): return self._display_type
    @DisplayType.setter
    def DisplayType(self, v):
        if self._display_type != v:
            self._display_type = v
            self._notify("DisplayType")
    @property
    def Halftone(self):    return self._halftone
    @Halftone.setter
    def Halftone(self, v):
        if self._halftone != v:
            self._halftone = v
            self._notify("Halftone")


class BulkCatItem(INotifyPropertyChanged):
    """Represents one category override in the bulk-mode VG tab."""
    def __init__(self, name, cat_id, hide=True, cat_type="model"):
        self._name     = name
        self._cat_id   = cat_id
        self._hide     = hide
        self._cat_type = cat_type   # "model" / "ann" / "imp"
        self._handlers = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def Name(self):  return self._name
    @property
    def CatId(self): return self._cat_id
    @property
    def CatType(self): return self._cat_type
    @property
    def Hide(self):  return self._hide
    @Hide.setter
    def Hide(self, v):
        if self._hide != v:
            self._hide = v
            self._notify("Hide")
            self._notify("ActionLabel")
    @property
    def ActionLabel(self): return "Hide" if self._hide else "Show"


class BulkFilterItem(INotifyPropertyChanged):
    """Represents a filter override row in the bulk-mode Filters tab."""
    def __init__(self, name, filter_id, action="Add"):
        # action: "Add" (apply filter to template), "Remove", "Hide", "Show"
        self._name      = name
        self._filter_id = filter_id
        self._action    = action
        self._handlers  = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def Name(self):     return self._name
    @property
    def FilterId(self): return self._filter_id
    @property
    def Action(self):   return self._action
    @Action.setter
    def Action(self, v):
        if self._action != v:
            self._action = v
            self._notify("Action")


class BulkLinkItem(INotifyPropertyChanged):
    """Represents one Revit Link row in the bulk-mode Links tab."""
    def __init__(self, name, type_id, instance_ids, apply=False):
        self._name         = name
        self._type_id      = type_id
        self._instance_ids = instance_ids
        self._apply        = apply
        self._handlers     = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def Name(self):        return self._name
    @property
    def TypeId(self):      return self._type_id
    @property
    def InstanceIds(self): return self._instance_ids
    @property
    def Apply(self):       return self._apply
    @Apply.setter
    def Apply(self, v):
        if self._apply != v:
            self._apply = v
            self._notify("Apply")


class UsageRow(object):
    def __init__(self, template_name, view_name, view_type, sheet_ref):
        self.TemplateName = template_name
        self.ViewName     = view_name
        self.ViewType     = view_type
        self.SheetRef     = sheet_ref


class ApplyViewRow(INotifyPropertyChanged):
    def __init__(self, view, current_template_name):
        self._view             = view
        self._is_checked       = False
        self._current_template = current_template_name
        self._handlers         = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)
    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
    def _notify(self, prop):
        args = PropertyChangedEventArgs(prop)
        for h in self._handlers:
            h(self, args)

    @property
    def View(self):  return self._view
    @property
    def ViewName(self): return self._view.Name
    @property
    def TypeLabel(self): return viewtype_label(self._view.ViewType)
    @property
    def CurrentTemplate(self): return self._current_template
    @property
    def IsChecked(self): return self._is_checked
    @IsChecked.setter
    def IsChecked(self, v):
        if self._is_checked != v:
            self._is_checked = v
            self._notify("IsChecked")


# ═══════════════════════════════════════════════════════════════
# SHARED RESOURCES (matches Sheet Manager / Revisions Manager)
# ═══════════════════════════════════════════════════════════════

SHARED_RESOURCES = """
    <Style x:Key="SectionHeader" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="13"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#1A202C"/>
      <Setter Property="Margin"     Value="0,0,0,6"/>
    </Style>
    <Style x:Key="FieldLabel" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="11"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#4A5568"/>
      <Setter Property="Margin"     Value="0,6,0,2"/>
    </Style>
    <Style x:Key="HelperText" TargetType="TextBlock">
      <Setter Property="FontSize"     Value="11"/>
      <Setter Property="Foreground"   Value="#718096"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="PrimaryBtn" TargetType="Button">
      <Setter Property="Background"    Value="#2B6CB0"/>
      <Setter Property="Foreground"    Value="White"/>
      <Setter Property="FontWeight"    Value="SemiBold"/>
      <Setter Property="FontSize"      Value="12"/>
      <Setter Property="Padding"       Value="14,6"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor"        Value="Hand"/>
      <Setter Property="Margin"        Value="0,0,6,0"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    CornerRadius="3" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#2C5282"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#2A4365"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="#A0AEC0"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SecondaryBtn" TargetType="Button">
      <Setter Property="Background"    Value="#EDF2F7"/>
      <Setter Property="Foreground"    Value="#2D3748"/>
      <Setter Property="FontSize"      Value="12"/>
      <Setter Property="Padding"       Value="12,5"/>
      <Setter Property="BorderBrush"   Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Cursor"        Value="Hand"/>
      <Setter Property="Margin"        Value="0,0,6,0"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    CornerRadius="3" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E2E8F0"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#CBD5E0"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="#F7FAFC"/>
                <Setter Property="Foreground" Value="#A0AEC0"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="DangerBtn" TargetType="Button">
      <Setter Property="Background"    Value="#FC8181"/>
      <Setter Property="Foreground"    Value="White"/>
      <Setter Property="FontSize"      Value="12"/>
      <Setter Property="Padding"       Value="12,5"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor"        Value="Hand"/>
      <Setter Property="Margin"        Value="0,0,6,0"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    CornerRadius="3" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E53E3E"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="#FED7D7"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Foreground"    Value="#1A202C"/>
      <Setter Property="BorderBrush"   Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"       Value="8,4"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"    Value="#2B6CB0"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Foreground"    Value="#1A202C"/>
      <Setter Property="BorderBrush"   Value="#CBD5E0"/>
      <Setter Property="Padding"       Value="6,4"/>
      <Setter Property="Height"        Value="28"/>
    </Style>
    <Style TargetType="CheckBox">
      <Setter Property="Foreground"    Value="#1A202C"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="TabControl">
      <Setter Property="Background"    Value="Transparent"/>
      <Setter Property="BorderBrush"   Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="0,1,0,0"/>
      <Setter Property="Padding"       Value="0"/>
    </Style>
    <Style TargetType="TabItem">
      <Setter Property="Foreground"    Value="#4A5568"/>
      <Setter Property="FontSize"      Value="12"/>
      <Setter Property="Padding"       Value="14,8"/>
      <Setter Property="Background"    Value="Transparent"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TabItem">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderThickness="0,0,0,2" BorderBrush="Transparent"
                    Padding="{TemplateBinding Padding}">
              <ContentPresenter ContentSource="Header"
                                HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="bd" Property="BorderBrush" Value="#2B6CB0"/>
                <Setter Property="Foreground" Value="#2B6CB0"/>
                <Setter Property="FontWeight" Value="SemiBold"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#EDF2F7"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="DataGrid">
      <Setter Property="Background"               Value="White"/>
      <Setter Property="Foreground"               Value="#1A202C"/>
      <Setter Property="BorderBrush"              Value="#E2E8F0"/>
      <Setter Property="BorderThickness"          Value="1"/>
      <Setter Property="RowBackground"            Value="White"/>
      <Setter Property="AlternatingRowBackground" Value="#F7FAFC"/>
      <Setter Property="GridLinesVisibility"      Value="None"/>
      <Setter Property="HeadersVisibility"        Value="Column"/>
      <Setter Property="SelectionMode"            Value="Single"/>
      <Setter Property="SelectionUnit"            Value="FullRow"/>
      <Setter Property="CanUserAddRows"           Value="False"/>
      <Setter Property="CanUserDeleteRows"        Value="False"/>
      <Setter Property="AutoGenerateColumns"      Value="False"/>
      <Setter Property="CanUserResizeRows"        Value="False"/>
      <Setter Property="RowHeight"                Value="28"/>
    </Style>
    <Style TargetType="DataGridColumnHeader">
      <Setter Property="Background"      Value="#EDF2F7"/>
      <Setter Property="Foreground"      Value="#2D3748"/>
      <Setter Property="FontSize"        Value="11"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
      <Setter Property="Padding"         Value="8,6"/>
      <Setter Property="BorderBrush"     Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="0,0,1,1"/>
    </Style>
    <Style TargetType="DataGridRow">
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Style.Triggers>
        <Trigger Property="IsMouseOver" Value="True">
          <Setter Property="Background" Value="#EDF2F7"/>
        </Trigger>
        <Trigger Property="IsSelected" Value="True">
          <Setter Property="Background" Value="#BEE3F8"/>
        </Trigger>
      </Style.Triggers>
    </Style>
    <Style TargetType="DataGridCell">
      <Setter Property="BorderThickness"  Value="0"/>
      <Setter Property="Foreground"       Value="#1A202C"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
      <Style.Triggers>
        <Trigger Property="IsSelected" Value="True">
          <Setter Property="Background" Value="Transparent"/>
          <Setter Property="Foreground" Value="#1A202C"/>
        </Trigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="RowCheck" TargetType="CheckBox">
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment"   Value="Center"/>
      <Setter Property="Margin" Value="0"/>
    </Style>

    <!-- RadioButton: properly-sized centered bullet -->
    <Style TargetType="RadioButton">
      <Setter Property="Foreground" Value="#1A202C"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RadioButton">
            <StackPanel Orientation="Horizontal" Background="Transparent">
              <Grid Width="16" Height="16" VerticalAlignment="Center" Margin="0,0,6,0">
                <Ellipse x:Name="outer" Width="14" Height="14"
                         Stroke="#A0AEC0" StrokeThickness="1.5" Fill="White"/>
                <Ellipse x:Name="dot" Width="7" Height="7"
                         Fill="#2B6CB0" Visibility="Collapsed"/>
              </Grid>
              <ContentPresenter VerticalAlignment="Center"
                                RecognizesAccessKey="True"/>
            </StackPanel>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="dot" Property="Visibility" Value="Visible"/>
                <Setter TargetName="outer" Property="Stroke" Value="#2B6CB0"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="outer" Property="Stroke" Value="#2B6CB0"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- Chip-style ToggleButton for filter panel -->
    <Style x:Key="ChipToggle" TargetType="ToggleButton">
      <Setter Property="Background"      Value="#EDF2F7"/>
      <Setter Property="Foreground"      Value="#2D3748"/>
      <Setter Property="BorderBrush"     Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="10,3"/>
      <Setter Property="Margin"          Value="0,0,6,4"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="FontSize"        Value="11"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ToggleButton">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    CornerRadius="12" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <MultiTrigger>
                <MultiTrigger.Conditions>
                  <Condition Property="IsMouseOver" Value="True"/>
                  <Condition Property="IsChecked"   Value="False"/>
                </MultiTrigger.Conditions>
                <Setter TargetName="bd" Property="Background" Value="#E2E8F0"/>
              </MultiTrigger>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="bd" Property="Background"  Value="#2B6CB0"/>
                <Setter TargetName="bd" Property="BorderBrush" Value="#2B6CB0"/>
                <Setter Property="Foreground" Value="White"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <!-- Filter ToggleButton (shows/hides filter pane) -->
    <Style x:Key="FilterToggle" TargetType="ToggleButton">
      <Setter Property="Background"      Value="#EDF2F7"/>
      <Setter Property="Foreground"      Value="#2D3748"/>
      <Setter Property="BorderBrush"     Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="12,4"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="FontSize"        Value="12"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ToggleButton">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    CornerRadius="3" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="bd" Property="Background"  Value="#2B6CB0"/>
                <Setter TargetName="bd" Property="BorderBrush" Value="#2B6CB0"/>
                <Setter Property="Foreground" Value="White"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E2E8F0"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
"""

# ═══════════════════════════════════════════════════════════════
# MAIN WINDOW XAML
# ═══════════════════════════════════════════════════════════════

MAIN_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="View Templates Manager" Width="1240" Height="820"
        WindowStartupLocation="CenterScreen"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>   <!-- 0: header -->
      <RowDefinition Height="Auto"/>   <!-- 1: toolbar -->
      <RowDefinition Height="Auto"/>   <!-- 2: filter pane (collapsible) -->
      <RowDefinition Height="*"/>      <!-- 3: main content -->
      <RowDefinition Height="Auto"/>   <!-- 4: status bar -->
    </Grid.RowDefinitions>

    <!-- ═══ HEADER BAR ═══ -->
    <Border Grid.Row="0" Background="#2D3748" Padding="20,14">
      <Grid>
        <StackPanel Orientation="Vertical" HorizontalAlignment="Left">
          <StackPanel Orientation="Horizontal">
            <StackPanel Orientation="Vertical" VerticalAlignment="Center">
              <TextBlock Text="View Templates Manager" Foreground="White"
                         FontSize="20" FontWeight="Bold"/>
              <TextBlock Text="Edit, duplicate, apply, and bulk-modify view templates across the project."
                         Foreground="#CBD5E0" FontSize="12" Margin="0,2,0,0"/>
            </StackPanel>
          </StackPanel>
        </StackPanel>
        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                    VerticalAlignment="Center" Opacity="0.85">
          <TextBlock Text="db" Foreground="#00BFFF" FontSize="32"
                     FontWeight="Bold" FontFamily="Segoe UI" VerticalAlignment="Center"/>
          <TextBlock Text=" | " Foreground="#7A8FA6" FontSize="32"
                     FontWeight="Light" VerticalAlignment="Center"/>
          <TextBlock Text="HMS" Foreground="#00BFFF" FontSize="32"
                     FontWeight="Bold" FontFamily="Segoe UI" VerticalAlignment="Center"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- ═══ TOOLBAR ═══ -->
    <Border Grid.Row="1" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="16,10">
      <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
        <Grid Width="220" Margin="0,0,8,0">
          <TextBox x:Name="txt_search" Height="30" FontSize="12"
                   VerticalContentAlignment="Center" Padding="8,0"/>
          <TextBlock x:Name="ph_search" Text="🔍  Search templates…"
                     Foreground="#A0AEC0" FontSize="12" IsHitTestVisible="False"
                     VerticalAlignment="Center" Margin="10,0"/>
        </Grid>
        <ToggleButton x:Name="btn_filter"
                      Style="{StaticResource FilterToggle}" Height="30"
                      Margin="0,0,16,0" ToolTip="Show / hide the filter pane">
          <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
            <TextBlock Text="▽" Foreground="#718096" FontSize="13"
                       Margin="0,0,6,0" VerticalAlignment="Center"/>
            <TextBlock Text="Filter" VerticalAlignment="Center"/>
            <TextBlock x:Name="lbl_filter_dot" Text=" •"
                       Visibility="Collapsed" VerticalAlignment="Center"/>
          </StackPanel>
        </ToggleButton>
        <Separator Width="1" Height="22" Background="#E2E8F0" Margin="0,0,12,0"/>
        <Button x:Name="btn_new"     Content="＋ New"          Style="{StaticResource SecondaryBtn}" Height="30" ToolTip="Create a new view template (copy of selected)"/>
        <Button x:Name="btn_rename"  Content="✎ Rename"        Style="{StaticResource SecondaryBtn}" Height="30" ToolTip="Rename selected template"/>
        <Button x:Name="btn_dup"     Content="⧉ Duplicate"     Style="{StaticResource SecondaryBtn}" Height="30" ToolTip="Duplicate selected template"/>
        <Button x:Name="btn_delete"  Content="🗑 Delete"        Style="{StaticResource DangerBtn}"    Height="30" ToolTip="Delete selected template(s)"/>
        <Separator Width="1" Height="22" Background="#E2E8F0" Margin="0,0,12,0"/>
        <Button x:Name="btn_apply"   Content="▶ Apply to Views" Style="{StaticResource PrimaryBtn}"  Height="30" ToolTip="Apply selected template to chosen views"/>
      </StackPanel>
    </Border>

    <!-- ═══ FILTER PANE (collapsible, shown via btn_filter) ═══ -->
    <Border Grid.Row="2" x:Name="filter_panel"
            Background="#F7FAFC" BorderBrush="#E2E8F0" BorderThickness="0,0,0,1"
            Padding="16,10" Visibility="Collapsed">
      <StackPanel>
        <Grid Margin="0,0,0,4">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="80"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Text="Type:" FontWeight="SemiBold" Foreground="#4A5568"
                     FontSize="11" VerticalAlignment="Center"/>
          <WrapPanel x:Name="pnl_filter_types" Grid.Column="1"/>
        </Grid>
        <Grid Margin="0,0,0,4">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="80"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Text="Used:" FontWeight="SemiBold" Foreground="#4A5568"
                     FontSize="11" VerticalAlignment="Center"/>
          <WrapPanel x:Name="pnl_filter_used" Grid.Column="1"/>
        </Grid>
        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,4,0,0">
          <TextBlock x:Name="lbl_filter_summary" Foreground="#718096" FontSize="11"
                     VerticalAlignment="Center" Margin="0,0,12,0"/>
          <Button x:Name="btn_filter_reset" Content="Reset Filters"
                  Style="{StaticResource SecondaryBtn}" Height="26" MinWidth="110"/>
        </StackPanel>
      </StackPanel>
    </Border>

    <!-- ═══ MAIN CONTENT (two-panel) ═══ -->
    <Grid Grid.Row="3">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="400" MinWidth="280"/>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>

      <!-- ── LEFT: Template List ── -->
      <Grid Grid.Column="0">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <!-- Local controls strip: select all / none -->
        <Border Grid.Row="0" Background="#EDF2F7"
                BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="10,6">
          <Grid>
            <TextBlock x:Name="lbl_list_count" Text="0 templates"
                       Foreground="#4A5568" FontSize="11" FontWeight="SemiBold"
                       VerticalAlignment="Center"/>
            <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
              <Button x:Name="btn_sel_all"  Content="✓ All"  Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11"/>
              <Button x:Name="btn_sel_none" Content="✕ None" Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11" Margin="0"/>
            </StackPanel>
          </Grid>
        </Border>
        <Border Grid.Row="1" Background="White"
                BorderBrush="#E2E8F0" BorderThickness="0,0,1,0">
          <DataGrid x:Name="dg_templates"
                    SelectionMode="Extended"
                    Background="White"
                    BorderThickness="0"
                    GridLinesVisibility="Horizontal"
                    HorizontalGridLinesBrush="#F0F4F8"
                    EnableRowVirtualization="True"
                    EnableColumnVirtualization="True">
            <DataGrid.Columns>
              <DataGridTemplateColumn Header="" Width="34" CanUserResize="False" CanUserSort="False">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <CheckBox IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                              Style="{StaticResource RowCheck}" Cursor="Hand"/>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
              <DataGridTextColumn Header="Name"  Binding="{Binding Name}"         Width="*"   IsReadOnly="True"/>
              <DataGridTextColumn Header="Type"  Binding="{Binding ViewTypeName}" Width="90"  IsReadOnly="True"/>
              <DataGridTextColumn Header="Scale" Binding="{Binding Scale}"        Width="60"  IsReadOnly="True"/>
              <DataGridTextColumn Header="Used"  Binding="{Binding UsedBy}"       Width="46"  IsReadOnly="True"/>
            </DataGrid.Columns>
          </DataGrid>
        </Border>
      </Grid>

      <GridSplitter Grid.Column="1" Width="4" HorizontalAlignment="Center"
                    VerticalAlignment="Stretch" Background="#E2E8F0" Cursor="SizeWE"/>

      <!-- ── RIGHT: Editor ── -->
      <Grid Grid.Column="2" x:Name="grid_right">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>

        <!-- Mode header strip -->
        <Border Grid.Row="0" x:Name="brd_single_header" Background="#EDF2F7"
                BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="16,8">
          <TextBlock x:Name="lbl_selected_header"
                     Text="Select a template on the left to view or edit its settings."
                     Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
        </Border>

        <!-- Bulk-mode warning banner (visible when 2+ templates checked) -->
        <Border Grid.Row="1" x:Name="brd_bulk_banner"
                Background="#FEFCBF" BorderBrush="#F6E05E"
                BorderThickness="0,0,0,1" Padding="16,10"
                Visibility="Collapsed">
          <StackPanel Orientation="Horizontal">
            <TextBlock Text="⚡" FontSize="18" Foreground="#975A16"
                       VerticalAlignment="Center" Margin="0,0,10,0"/>
            <StackPanel>
              <TextBlock x:Name="lbl_bulk_title" Text="Bulk edit mode"
                         Foreground="#744210" FontWeight="Bold" FontSize="13"/>
              <TextBlock x:Name="lbl_bulk_subtitle"
                         Text="Editing multiple templates. Each tab now applies overrides to ALL checked templates."
                         Foreground="#975A16" FontSize="11"/>
            </StackPanel>
          </StackPanel>
        </Border>

        <!-- Tab Control -->
        <TabControl Grid.Row="2" x:Name="tab_main"
                    Background="#F7FAFC" BorderThickness="0">

          <!-- ─────────────────────  PROPERTIES TAB  ───────────────────── -->
          <TabItem Header="Properties">
            <ScrollViewer VerticalScrollBarVisibility="Auto"
                          HorizontalScrollBarVisibility="Disabled" Padding="20">
              <StackPanel x:Name="pnl_props" Margin="0,4,0,16">

                <!-- Single-mode panel -->
                <StackPanel x:Name="pnl_props_single">
                  <TextBlock Text="Template Properties" Style="{StaticResource SectionHeader}"/>
                  <TextBlock Text="Name" Style="{StaticResource FieldLabel}"/>
                  <Grid>
                    <Grid.ColumnDefinitions>
                      <ColumnDefinition Width="*"/>
                      <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    <TextBox x:Name="txt_prop_name" Height="30" Grid.Column="0" Margin="0,0,8,0"/>
                    <Button x:Name="btn_prop_rename" Content="Rename" Grid.Column="1"
                            Style="{StaticResource SecondaryBtn}" Height="30"/>
                  </Grid>

                  <TextBlock Text="View Type" Style="{StaticResource FieldLabel}"/>
                  <TextBlock x:Name="lbl_prop_type" Foreground="#4A5568" FontSize="12"/>

                  <StackPanel Orientation="Horizontal">
                    <CheckBox x:Name="chk_inc_scale" Margin="0,8,6,0" VerticalAlignment="Center"
                              ToolTip="Include View Scale in this template"/>
                    <TextBlock Text="View Scale" Style="{StaticResource FieldLabel}"/>
                  </StackPanel>
                  <ComboBox x:Name="cmb_prop_scale" Width="180" HorizontalAlignment="Left"/>

                  <StackPanel Orientation="Horizontal">
                    <CheckBox x:Name="chk_inc_detail" Margin="0,8,6,0" VerticalAlignment="Center"
                              ToolTip="Include Detail Level in this template"/>
                    <TextBlock Text="Display / Detail Level" Style="{StaticResource FieldLabel}"/>
                  </StackPanel>
                  <ComboBox x:Name="cmb_prop_detail" Width="180" HorizontalAlignment="Left">
                    <ComboBoxItem Content="Coarse" Tag="1"/>
                    <ComboBoxItem Content="Medium" Tag="2"/>
                    <ComboBoxItem Content="Fine"   Tag="3"/>
                  </ComboBox>

                  <StackPanel Orientation="Horizontal">
                    <CheckBox x:Name="chk_inc_parts" Margin="0,8,6,0" VerticalAlignment="Center"
                              ToolTip="Include Parts Visibility in this template"/>
                    <TextBlock Text="Parts Visibility" Style="{StaticResource FieldLabel}"/>
                  </StackPanel>
                  <ComboBox x:Name="cmb_prop_parts" Width="180" HorizontalAlignment="Left">
                    <ComboBoxItem Content="Show Original" Tag="0"/>
                    <ComboBoxItem Content="Show Parts"    Tag="1"/>
                    <ComboBoxItem Content="Show Both"     Tag="2"/>
                  </ComboBox>

                  <StackPanel Orientation="Horizontal">
                    <CheckBox x:Name="chk_inc_phase" Margin="0,8,6,0" VerticalAlignment="Center"
                              ToolTip="Include Phase Filter in this template"/>
                    <TextBlock Text="Phase Filter" Style="{StaticResource FieldLabel}"/>
                  </StackPanel>
                  <ComboBox x:Name="cmb_prop_phase" Width="220" HorizontalAlignment="Left"/>

                  <StackPanel Orientation="Horizontal">
                    <CheckBox x:Name="chk_inc_disc" Margin="0,8,6,0" VerticalAlignment="Center"
                              ToolTip="Include Discipline in this template"/>
                    <TextBlock Text="Discipline" Style="{StaticResource FieldLabel}"/>
                  </StackPanel>
                  <ComboBox x:Name="cmb_prop_discipline" Width="180" HorizontalAlignment="Left">
                    <ComboBoxItem Content="Architectural"  Tag="0"/>
                    <ComboBoxItem Content="Structural"     Tag="1"/>
                    <ComboBoxItem Content="Mechanical"     Tag="2"/>
                    <ComboBoxItem Content="Electrical"     Tag="3"/>
                    <ComboBoxItem Content="Plumbing"       Tag="4"/>
                    <ComboBoxItem Content="Coordination"   Tag="255"/>
                  </ComboBox>

                  <StackPanel Orientation="Horizontal">
                    <CheckBox x:Name="chk_inc_vrange" Margin="0,8,6,0" VerticalAlignment="Center"
                              ToolTip="Include View Range in this template"/>
                    <TextBlock Text="View Range (plan views only)" Style="{StaticResource FieldLabel}"/>
                  </StackPanel>
                  <StackPanel Orientation="Horizontal">
                    <TextBlock x:Name="lbl_prop_viewrange" Text="—"
                               Foreground="#4A5568" FontSize="11"
                               VerticalAlignment="Center" Margin="0,0,10,0"/>
                    <Button x:Name="btn_prop_viewrange" Content="Edit View Range…"
                            Style="{StaticResource SecondaryBtn}" Height="28"
                            ToolTip="Edit view range top/bottom/cut plane offsets"/>
                  </StackPanel>

                  <StackPanel Orientation="Horizontal" Margin="0,18,0,0">
                    <Button x:Name="btn_prop_save" Content="💾  Save Properties"
                            Style="{StaticResource PrimaryBtn}" Height="32"/>
                    <TextBlock x:Name="lbl_prop_status" Text="" Foreground="#48BB78"
                               FontSize="11" VerticalAlignment="Center" Margin="6,0,0,0"/>
                  </StackPanel>
                </StackPanel>

                <!-- Bulk-mode panel -->
                <StackPanel x:Name="pnl_props_bulk" Visibility="Collapsed">
                  <TextBlock Text="Bulk Properties Override" Style="{StaticResource SectionHeader}"/>
                  <TextBlock Style="{StaticResource HelperText}" Margin="0,0,0,8">
                    Check the boxes for properties you want to overwrite, set their values,
                    then click Apply. Properties left unchecked are not modified.
                  </TextBlock>
                  <Grid>
                    <Grid.ColumnDefinitions>
                      <ColumnDefinition Width="200"/>
                      <ColumnDefinition Width="220"/>
                      <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <Grid.RowDefinitions>
                      <RowDefinition Height="Auto"/>
                      <RowDefinition Height="Auto"/>
                      <RowDefinition Height="Auto"/>
                      <RowDefinition Height="Auto"/>
                      <RowDefinition Height="Auto"/>
                    </Grid.RowDefinitions>
                    <CheckBox x:Name="chk_bulk_scale" Content="View Scale"
                              Grid.Row="0" Grid.Column="0" Margin="0,4,10,8" VerticalAlignment="Center"/>
                    <ComboBox x:Name="cmb_bulk_scale" Grid.Row="0" Grid.Column="1" Margin="0,4,0,8"/>
                    <CheckBox x:Name="chk_bulk_detail" Content="Display / Detail Level"
                              Grid.Row="1" Grid.Column="0" Margin="0,0,10,8" VerticalAlignment="Center"/>
                    <ComboBox x:Name="cmb_bulk_detail" Grid.Row="1" Grid.Column="1" Margin="0,0,0,8">
                      <ComboBoxItem Content="Coarse" Tag="1"/>
                      <ComboBoxItem Content="Medium" Tag="2"/>
                      <ComboBoxItem Content="Fine"   Tag="3"/>
                    </ComboBox>
                    <CheckBox x:Name="chk_bulk_parts" Content="Parts Visibility"
                              Grid.Row="2" Grid.Column="0" Margin="0,0,10,8" VerticalAlignment="Center"/>
                    <ComboBox x:Name="cmb_bulk_parts" Grid.Row="2" Grid.Column="1" Margin="0,0,0,8">
                      <ComboBoxItem Content="Show Original" Tag="0"/>
                      <ComboBoxItem Content="Show Parts"    Tag="1"/>
                      <ComboBoxItem Content="Show Both"     Tag="2"/>
                    </ComboBox>
                    <CheckBox x:Name="chk_bulk_phase" Content="Phase Filter"
                              Grid.Row="3" Grid.Column="0" Margin="0,0,10,8" VerticalAlignment="Center"/>
                    <ComboBox x:Name="cmb_bulk_phase" Grid.Row="3" Grid.Column="1" Margin="0,0,0,8"/>
                    <CheckBox x:Name="chk_bulk_disc" Content="Discipline"
                              Grid.Row="4" Grid.Column="0" Margin="0,0,10,0" VerticalAlignment="Center"/>
                    <ComboBox x:Name="cmb_bulk_disc" Grid.Row="4" Grid.Column="1">
                      <ComboBoxItem Content="Architectural"  Tag="0"/>
                      <ComboBoxItem Content="Structural"     Tag="1"/>
                      <ComboBoxItem Content="Mechanical"     Tag="2"/>
                      <ComboBoxItem Content="Electrical"     Tag="3"/>
                      <ComboBoxItem Content="Plumbing"       Tag="4"/>
                      <ComboBoxItem Content="Coordination"   Tag="255"/>
                    </ComboBox>
                  </Grid>
                  <StackPanel Orientation="Horizontal" Margin="0,18,0,0">
                    <Button x:Name="btn_props_bulk_apply" Content="⚡  Apply to Selected Templates"
                            Style="{StaticResource PrimaryBtn}" Height="34"/>
                    <TextBlock x:Name="lbl_props_bulk_status" Text="" Foreground="#48BB78"
                               FontSize="11" VerticalAlignment="Center" Margin="6,0,0,0"/>
                  </StackPanel>
                </StackPanel>

              </StackPanel>
            </ScrollViewer>
          </TabItem>

          <!-- ─────────────────────  VG OVERRIDES TAB  ───────────────────── -->
          <TabItem Header="VG Overrides">
            <Grid>
              <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
              </Grid.RowDefinitions>

              <!-- Sub-tab selector -->
              <Border Grid.Row="0" Background="White"
                      BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="12,6">
                <StackPanel Orientation="Horizontal">
                  <RadioButton x:Name="rb_vg_model"  Content="Model"      IsChecked="True"
                               GroupName="vg_type" Margin="0,0,16,0" VerticalContentAlignment="Center"/>
                  <RadioButton x:Name="rb_vg_ann"    Content="Annotations"
                               GroupName="vg_type" Margin="0,0,16,0" VerticalContentAlignment="Center"/>
                  <RadioButton x:Name="rb_vg_imp"    Content="Imported"
                               GroupName="vg_type" Margin="0,0,16,0" VerticalContentAlignment="Center"/>
                  <RadioButton x:Name="rb_vg_filt"   Content="Filters"
                               GroupName="vg_type" Margin="0,0,16,0" VerticalContentAlignment="Center"/>
                  <Separator Width="1" Height="20" Background="#E2E8F0" Margin="0,0,12,0"/>
                  <TextBlock x:Name="lbl_vg_info" Text=""
                             Foreground="#718096" FontSize="11" VerticalAlignment="Center"/>
                </StackPanel>
              </Border>

              <!-- Body: two stacked panels — single mode and bulk mode -->
              <Grid Grid.Row="1">
                <!-- Single mode: shows current state with checkboxes -->
                <Grid x:Name="pnl_vg_single">
                  <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                    <RowDefinition Height="Auto"/>
                  </Grid.RowDefinitions>
                  <Border Grid.Row="0" Background="White"
                          BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="10,6">
                    <Grid>
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <Grid Width="220" Grid.Column="0" HorizontalAlignment="Left">
                        <TextBox x:Name="txt_vg_search" Height="26" FontSize="12"
                                 VerticalContentAlignment="Center" Padding="8,0"/>
                        <TextBlock x:Name="ph_vg_search" Text="▽  Filter…"
                                   Foreground="#A0AEC0" FontSize="12"
                                   IsHitTestVisible="False"
                                   VerticalAlignment="Center" Margin="10,0"/>
                      </Grid>
                      <StackPanel Grid.Column="1" Orientation="Horizontal">
                        <Button x:Name="btn_vg_show_hl"  Content="Show Highlighted"  Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11"
                                ToolTip="Show categories highlighted in the list (Shift/Ctrl-click rows)"/>
                        <Button x:Name="btn_vg_hide_hl"  Content="Hide Highlighted"  Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11"
                                ToolTip="Hide highlighted categories"/>
                        <Separator Width="1" Height="20" Background="#CBD5E0" Margin="2,0,8,0"/>
                        <Button x:Name="btn_vg_show_all"  Content="Show All"  Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11"/>
                        <Button x:Name="btn_vg_hide_all"  Content="Hide All"  Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11" Margin="0"/>
                      </StackPanel>
                    </Grid>
                  </Border>
                  <DataGrid Grid.Row="1" x:Name="dg_vg_cats" BorderThickness="0"
                            SelectionMode="Extended" SelectionUnit="FullRow"
                            EnableRowVirtualization="True">
                    <DataGrid.Columns>
                      <DataGridTemplateColumn Header="Visible" Width="60" CanUserResize="False">
                        <DataGridTemplateColumn.CellTemplate>
                          <DataTemplate>
                            <CheckBox IsChecked="{Binding IsVisible, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                      Style="{StaticResource RowCheck}" Cursor="Hand"/>
                          </DataTemplate>
                        </DataGridTemplateColumn.CellTemplate>
                      </DataGridTemplateColumn>
                      <DataGridTextColumn Header="Category" Binding="{Binding Name}" Width="*" IsReadOnly="True"/>
                    </DataGrid.Columns>
                  </DataGrid>
                  <!-- Filters body (sub-tab) — shown only when rb_vg_filt is checked -->
                  <DataGrid Grid.Row="1" x:Name="dg_vg_filters" BorderThickness="0"
                            Visibility="Collapsed" EnableRowVirtualization="True">
                    <DataGrid.Columns>
                      <DataGridTextColumn Header="Filter" Binding="{Binding Name}" Width="*" IsReadOnly="True"/>
                      <DataGridTemplateColumn Header="Visible" Width="70" CanUserResize="False">
                        <DataGridTemplateColumn.CellTemplate>
                          <DataTemplate>
                            <CheckBox IsChecked="{Binding Visible, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                      Style="{StaticResource RowCheck}" Cursor="Hand"/>
                          </DataTemplate>
                        </DataGridTemplateColumn.CellTemplate>
                      </DataGridTemplateColumn>
                      <DataGridTemplateColumn Header="Enabled" Width="70" CanUserResize="False">
                        <DataGridTemplateColumn.CellTemplate>
                          <DataTemplate>
                            <CheckBox IsChecked="{Binding Enabled, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                      Style="{StaticResource RowCheck}" Cursor="Hand"/>
                          </DataTemplate>
                        </DataGridTemplateColumn.CellTemplate>
                      </DataGridTemplateColumn>
                    </DataGrid.Columns>
                  </DataGrid>
                  <Border Grid.Row="2" x:Name="brd_filters_buttons"
                          Background="White" BorderBrush="#E2E8F0"
                          BorderThickness="0,1,0,0" Padding="10,6"
                          Visibility="Collapsed">
                    <StackPanel Orientation="Horizontal">
                      <Button x:Name="btn_filter_add"    Content="＋ Add Filter…"      Style="{StaticResource SecondaryBtn}" Height="26" FontSize="11"/>
                      <Button x:Name="btn_filter_remove" Content="✕ Remove Selected" Style="{StaticResource DangerBtn}"    Height="26" FontSize="11"/>
                    </StackPanel>
                  </Border>
                </Grid>

                <!-- Bulk mode: override-builder UI -->
                <Grid x:Name="pnl_vg_bulk" Visibility="Collapsed">
                  <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                    <RowDefinition Height="Auto"/>
                  </Grid.RowDefinitions>
                  <Border Grid.Row="0" Background="White"
                          BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="14,8">
                    <TextBlock x:Name="lbl_vg_bulk_help" Style="{StaticResource HelperText}">
                      Build a list of category overrides to apply to ALL checked templates.
                      Existing settings on templates not listed here are preserved.
                    </TextBlock>
                  </Border>
                  <DataGrid Grid.Row="1" x:Name="dg_vg_bulk_cats" BorderThickness="0"
                            EnableRowVirtualization="True">
                    <DataGrid.Columns>
                      <DataGridTextColumn Header="Category" Binding="{Binding Name}"        Width="*"   IsReadOnly="True"/>
                      <DataGridTextColumn Header="Action"   Binding="{Binding ActionLabel}" Width="80"  IsReadOnly="True"/>
                    </DataGrid.Columns>
                  </DataGrid>
                  <DataGrid Grid.Row="1" x:Name="dg_vg_bulk_filters" BorderThickness="0"
                            Visibility="Collapsed" EnableRowVirtualization="True">
                    <DataGrid.Columns>
                      <DataGridTextColumn Header="Filter" Binding="{Binding Name}"   Width="*"  IsReadOnly="True"/>
                      <DataGridTextColumn Header="Action" Binding="{Binding Action}" Width="100" IsReadOnly="True"/>
                    </DataGrid.Columns>
                  </DataGrid>
                  <Border Grid.Row="2" Background="White"
                          BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="14,8">
                    <StackPanel x:Name="pnl_vg_bulk_buttons" Orientation="Horizontal">
                      <Button x:Name="btn_bulk_add_hide"     Content="＋ Add to Hide…"  Style="{StaticResource SecondaryBtn}" Height="28" FontSize="11"
                              ToolTip="Pick categories to add as Hide overrides"/>
                      <Button x:Name="btn_bulk_add_show"     Content="＋ Add to Show…"  Style="{StaticResource SecondaryBtn}" Height="28" FontSize="11"
                              ToolTip="Pick categories to add as Show overrides"/>
                      <Button x:Name="btn_bulk_add_all_hide" Content="＋ All to Hide"   Style="{StaticResource SecondaryBtn}" Height="28" FontSize="11"
                              ToolTip="Add ALL categories of the current sub-tab as Hide overrides"/>
                      <Button x:Name="btn_bulk_add_all_show" Content="＋ All to Show"   Style="{StaticResource SecondaryBtn}" Height="28" FontSize="11"
                              ToolTip="Add ALL categories of the current sub-tab as Show overrides"/>
                      <Button x:Name="btn_bulk_filt_add"   Content="＋ Add Filter (Apply)"  Style="{StaticResource SecondaryBtn}" Height="28" FontSize="11" Visibility="Collapsed"/>
                      <Button x:Name="btn_bulk_filt_remove" Content="✕ Remove Filter (Detach)" Style="{StaticResource SecondaryBtn}" Height="28" FontSize="11" Visibility="Collapsed"/>
                      <Button x:Name="btn_bulk_remove"     Content="✕ Remove Row"   Style="{StaticResource DangerBtn}"    Height="28" FontSize="11"/>
                      <Separator Width="1" Height="20" Background="#E2E8F0" Margin="0,0,12,0"/>
                      <Button x:Name="btn_vg_bulk_apply"   Content="⚡  Apply Overrides" Style="{StaticResource PrimaryBtn}" Height="30"/>
                      <TextBlock x:Name="lbl_vg_bulk_status" Text="" Foreground="#48BB78"
                                 FontSize="11" VerticalAlignment="Center" Margin="6,0,0,0"/>
                    </StackPanel>
                  </Border>
                </Grid>
              </Grid>
            </Grid>
          </TabItem>

          <!-- ─────────────────────  REVIT LINKS TAB  ───────────────────── -->
          <TabItem Header="Revit Links">
            <Grid>
              <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
                <RowDefinition Height="Auto"/>
              </Grid.RowDefinitions>
              <Border Grid.Row="0" Background="#EDF2F7"
                      BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="16,10">
                <TextBlock Style="{StaticResource HelperText}">
                  Each row is one linked .rvt file (deduplicated across instances).
                  <Run FontWeight="SemiBold">By Host View</Run> = link inherits this template's
                  category visibility. <Run FontWeight="SemiBold">By Linked View</Run> = link uses its own
                  template-by-template settings.
                </TextBlock>
              </Border>

              <!-- Single mode -->
              <Grid Grid.Row="1" x:Name="pnl_links_single">
                <DataGrid x:Name="dg_links" BorderThickness="0">
                  <DataGrid.Columns>
                    <DataGridTextColumn Header="Linked Model"    Binding="{Binding Name}"        Width="*"   IsReadOnly="True"/>
                    <DataGridTextColumn Header="Instances"       Binding="{Binding Instances}"   Width="80"  IsReadOnly="True"/>
                    <DataGridTextColumn Header="Display Setting" Binding="{Binding DisplayType}" Width="160" IsReadOnly="True"/>
                    <DataGridTemplateColumn Header="Halftone" Width="74" CanUserResize="False">
                      <DataGridTemplateColumn.CellTemplate>
                        <DataTemplate>
                          <CheckBox IsChecked="{Binding Halftone, Mode=OneWay}"
                                    Style="{StaticResource RowCheck}" IsHitTestVisible="False"/>
                        </DataTemplate>
                      </DataGridTemplateColumn.CellTemplate>
                    </DataGridTemplateColumn>
                  </DataGrid.Columns>
                </DataGrid>
              </Grid>

              <!-- Bulk mode -->
              <Grid Grid.Row="1" x:Name="pnl_links_bulk" Visibility="Collapsed">
                <DataGrid x:Name="dg_bulk_links" BorderThickness="0">
                  <DataGrid.Columns>
                    <DataGridTemplateColumn Header="Apply" Width="50" CanUserResize="False">
                      <DataGridTemplateColumn.CellTemplate>
                        <DataTemplate>
                          <CheckBox IsChecked="{Binding Apply, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                    Style="{StaticResource RowCheck}" Cursor="Hand"/>
                        </DataTemplate>
                      </DataGridTemplateColumn.CellTemplate>
                    </DataGridTemplateColumn>
                    <DataGridTextColumn Header="Linked Model" Binding="{Binding Name}" Width="*" IsReadOnly="True"/>
                  </DataGrid.Columns>
                </DataGrid>
              </Grid>

              <!-- Buttons strip -->
              <Border Grid.Row="2" Background="White"
                      BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="16,10">
                <StackPanel Orientation="Horizontal">
                  <!-- Single-mode actions -->
                  <StackPanel x:Name="pnl_link_btns_single" Orientation="Horizontal">
                    <Button x:Name="btn_link_set_host"   Content="Set → By Host View"
                            Style="{StaticResource PrimaryBtn}" Height="30"
                            ToolTip="Set selected link to By Host View for this template"/>
                    <Button x:Name="btn_link_set_linked" Content="Set → By Linked View"
                            Style="{StaticResource SecondaryBtn}" Height="30"
                            ToolTip="Set selected link to By Linked View for this template"/>
                    <Button x:Name="btn_link_toggle_ht"  Content="Toggle Halftone"
                            Style="{StaticResource SecondaryBtn}" Height="30"/>
                    <Button x:Name="btn_link_reset"      Content="Reset (Not Overridden)"
                            Style="{StaticResource SecondaryBtn}" Height="30"/>
                  </StackPanel>
                  <!-- Bulk-mode actions -->
                  <StackPanel x:Name="pnl_link_btns_bulk" Orientation="Horizontal" Visibility="Collapsed">
                    <TextBlock Text="Set checked links to:" Foreground="#4A5568"
                               VerticalAlignment="Center" Margin="0,0,6,0"/>
                    <ComboBox x:Name="cmb_bulk_link_action" Width="170" Height="30" Margin="0,0,8,0">
                      <ComboBoxItem Content="By Host View"        Tag="host"/>
                      <ComboBoxItem Content="By Linked View"      Tag="linked"/>
                      <ComboBoxItem Content="Halftone ON"         Tag="ht_on"/>
                      <ComboBoxItem Content="Halftone OFF"        Tag="ht_off"/>
                      <ComboBoxItem Content="Reset (Not Overridden)" Tag="reset"/>
                    </ComboBox>
                    <Button x:Name="btn_link_bulk_apply"  Content="⚡ Apply"
                            Style="{StaticResource PrimaryBtn}" Height="30"/>
                  </StackPanel>
                  <Button x:Name="btn_links_reload"  Content="↺ Reload"
                          Style="{StaticResource SecondaryBtn}" Height="30" Margin="12,0,6,0"/>
                  <TextBlock x:Name="lbl_links_status" Text=""
                             Foreground="#48BB78" FontSize="11" VerticalAlignment="Center" Margin="6,0,0,0"/>
                </StackPanel>
              </Border>
            </Grid>
          </TabItem>

          <!-- ─────────────────────  USAGE TAB  ───────────────────── -->
          <TabItem Header="Usage">
            <Grid>
              <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
              </Grid.RowDefinitions>
              <Border Grid.Row="0" Background="#EDF2F7"
                      BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="16,10">
                <TextBlock x:Name="lbl_usage_header" Style="{StaticResource HelperText}"
                           Text="Select a template to see which views use it."/>
              </Border>
              <DataGrid Grid.Row="1" x:Name="dg_usage" BorderThickness="0">
                <DataGrid.Columns>
                  <DataGridTextColumn Header="Template"   Binding="{Binding TemplateName}" Width="180" IsReadOnly="True"/>
                  <DataGridTextColumn Header="View Name"  Binding="{Binding ViewName}"    Width="*"   IsReadOnly="True"/>
                  <DataGridTextColumn Header="View Type"  Binding="{Binding ViewType}"    Width="110" IsReadOnly="True"/>
                  <DataGridTextColumn Header="On Sheet"   Binding="{Binding SheetRef}"    Width="160" IsReadOnly="True"/>
                </DataGrid.Columns>
              </DataGrid>
            </Grid>
          </TabItem>
        </TabControl>
      </Grid>
    </Grid>

    <!-- ═══ STATUS BAR ═══ -->
    <Border Grid.Row="4" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="16,8">
      <Grid>
        <TextBlock x:Name="lbl_status" Text="" Foreground="#4A5568"
                   VerticalAlignment="Center" FontSize="11"/>
      </Grid>
    </Border>
  </Grid>
</Window>
"""

# ═══════════════════════════════════════════════════════════════
# DIALOG XAMLs
# ═══════════════════════════════════════════════════════════════

RENAME_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Rename Template" Width="400" Height="170"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Grid Margin="20">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>   <!-- 0: header -->
      <RowDefinition Height="Auto"/>   <!-- 1: toolbar -->
      <RowDefinition Height="Auto"/>   <!-- 2: filter pane (collapsible) -->
      <RowDefinition Height="*"/>      <!-- 3: main content -->
      <RowDefinition Height="Auto"/>   <!-- 4: status bar -->
    </Grid.RowDefinitions>
    <TextBlock Grid.Row="0" Text="New template name:" Foreground="#4A5568"
               FontWeight="SemiBold" Margin="0,0,0,6"/>
    <TextBox Grid.Row="1" x:Name="txt_new_name" Height="30"
             BorderBrush="#CBD5E0" Padding="8,4"/>
    <StackPanel Grid.Row="3" Orientation="Horizontal" HorizontalAlignment="Right"
                Margin="0,10,0,0">
      <Button x:Name="btn_ok"     Content="Rename" Width="80" Height="30" Margin="0,0,8,0"
              Background="#2B6CB0" Foreground="White" FontWeight="SemiBold"
              BorderThickness="0"/>
      <Button x:Name="btn_cancel" Content="Cancel"  Width="70" Height="30"
              Background="#EDF2F7" Foreground="#2D3748" BorderBrush="#CBD5E0" BorderThickness="1"/>
    </StackPanel>
  </Grid>
</Window>
"""

APPLY_VIEWS_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Apply Template to Views" Width="640" Height="560"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>   <!-- 0: header -->
      <RowDefinition Height="Auto"/>   <!-- 1: toolbar -->
      <RowDefinition Height="Auto"/>   <!-- 2: filter pane (collapsible) -->
      <RowDefinition Height="*"/>      <!-- 3: main content -->
      <RowDefinition Height="Auto"/>   <!-- 4: status bar -->
    </Grid.RowDefinitions>
    <Border Grid.Row="0" Background="#2D3748" Padding="16,12">
      <TextBlock x:Name="lbl_title" Text="Apply Template to Views"
                 Foreground="White" FontSize="16" FontWeight="Bold"/>
    </Border>
    <Border Grid.Row="1" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="12,8">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <Grid Width="240" HorizontalAlignment="Left">
          <TextBox x:Name="txt_av_search" Height="28" FontSize="12"
                   VerticalContentAlignment="Center" Padding="8,0"/>
          <TextBlock x:Name="ph_av_search" Text="▽  Filter views…"
                     Foreground="#A0AEC0" FontSize="12"
                     IsHitTestVisible="False" VerticalAlignment="Center" Margin="10,0"/>
        </Grid>
        <StackPanel Grid.Column="1" Orientation="Horizontal">
          <Button x:Name="btn_av_all"  Content="✓ Select All"  Width="90" Height="28" Margin="0,0,6,0"
                  Background="#EDF2F7" BorderBrush="#CBD5E0" BorderThickness="1"/>
          <Button x:Name="btn_av_none" Content="✕ Select None" Width="100" Height="28"
                  Background="#EDF2F7" BorderBrush="#CBD5E0" BorderThickness="1"/>
        </StackPanel>
      </Grid>
    </Border>
    <DataGrid Grid.Row="2" x:Name="dg_av_views"
              Background="White" BorderThickness="0"
              GridLinesVisibility="Horizontal" HorizontalGridLinesBrush="#F0F4F8"
              RowHeight="26" CanUserAddRows="False" CanUserDeleteRows="False"
              AutoGenerateColumns="False" SelectionMode="Single"
              EnableRowVirtualization="True">
      <DataGrid.Columns>
        <DataGridTemplateColumn Header="" Width="34" CanUserResize="False">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <CheckBox IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                        HorizontalAlignment="Center" VerticalAlignment="Center" Cursor="Hand"/>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
        <DataGridTextColumn Header="View Name"  Binding="{Binding ViewName}"  Width="*"  IsReadOnly="True"/>
        <DataGridTextColumn Header="Type"       Binding="{Binding TypeLabel}" Width="100" IsReadOnly="True"/>
        <DataGridTextColumn Header="Current Template" Binding="{Binding CurrentTemplate}" Width="160" IsReadOnly="True"/>
      </DataGrid.Columns>
    </DataGrid>
    <Border Grid.Row="3" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="16,10">
      <Grid>
        <TextBlock x:Name="lbl_av_status" Text="" Foreground="#4A5568"
                   VerticalAlignment="Center" FontSize="11"/>
        <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
          <Button x:Name="btn_av_apply"  Content="Apply Template" Width="120" Height="30" Margin="0,0,8,0"
                  Background="#2B6CB0" Foreground="White" FontWeight="SemiBold" BorderThickness="0"/>
          <Button x:Name="btn_av_cancel" Content="Cancel"          Width="70"  Height="30"
                  Background="#EDF2F7" BorderBrush="#CBD5E0" BorderThickness="1"/>
        </StackPanel>
      </Grid>
    </Border>
  </Grid>
</Window>
"""

PICK_CAT_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Pick Category" Width="440" Height="540"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>   <!-- 0: header -->
      <RowDefinition Height="Auto"/>   <!-- 1: toolbar -->
      <RowDefinition Height="Auto"/>   <!-- 2: filter pane (collapsible) -->
      <RowDefinition Height="*"/>      <!-- 3: main content -->
      <RowDefinition Height="Auto"/>   <!-- 4: status bar -->
    </Grid.RowDefinitions>
    <Border Grid.Row="0" Background="#2D3748" Padding="14,10">
      <TextBlock Text="Select a Category" Foreground="White" FontSize="14" FontWeight="Bold"/>
    </Border>
    <Border Grid.Row="1" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="10,8">
      <StackPanel>
        <Grid>
          <TextBox x:Name="txt_cat_search" Height="28"/>
          <TextBlock Text="Search categories…" Foreground="#A0AEC0" FontSize="11"
                     IsHitTestVisible="False" VerticalAlignment="Center" Margin="8,0"/>
        </Grid>
        <StackPanel Orientation="Horizontal" Margin="0,6,0,0">
          <RadioButton x:Name="rb_cat_model" Content="Model" IsChecked="True"
                       GroupName="cat_type" Margin="0,0,14,0"/>
          <RadioButton x:Name="rb_cat_ann"   Content="Annotation"
                       GroupName="cat_type" Margin="0,0,14,0"/>
          <RadioButton x:Name="rb_cat_imp"   Content="Imported"
                       GroupName="cat_type"/>
          <TextBlock Text="    (Shift / Ctrl click to select multiple)"
                     Foreground="#A0AEC0" FontSize="11" VerticalAlignment="Center"/>
        </StackPanel>
      </StackPanel>
    </Border>
    <ListBox Grid.Row="3" x:Name="lb_cats"
             SelectionMode="Extended"
             Background="White" BorderThickness="0"
             ScrollViewer.VerticalScrollBarVisibility="Auto"
             ScrollViewer.HorizontalScrollBarVisibility="Disabled"/>
    <Border Grid.Row="4" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="12,8">
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <TextBlock x:Name="lbl_cat_count" Text="0 selected"
                   Foreground="#718096" FontSize="11" VerticalAlignment="Center"
                   Margin="0,0,12,0"/>
        <Button x:Name="btn_cat_ok"     Content="✓ Add Selected" Width="120" Height="30" Margin="0,0,8,0"
                Background="#2B6CB0" Foreground="White" FontWeight="SemiBold" BorderThickness="0"/>
        <Button x:Name="btn_cat_cancel" Content="Cancel" Width="80" Height="30"
                Background="#EDF2F7" BorderBrush="#CBD5E0" BorderThickness="1"/>
      </StackPanel>
    </Border>
  </Grid>
</Window>
"""

PICK_FILTER_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Pick Filter" Width="420" Height="500"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>   <!-- 0: header -->
      <RowDefinition Height="Auto"/>   <!-- 1: toolbar -->
      <RowDefinition Height="Auto"/>   <!-- 2: filter pane (collapsible) -->
      <RowDefinition Height="*"/>      <!-- 3: main content -->
      <RowDefinition Height="Auto"/>   <!-- 4: status bar -->
    </Grid.RowDefinitions>
    <Border Grid.Row="0" Background="#2D3748" Padding="14,10">
      <TextBlock Text="Select a Filter" Foreground="White" FontSize="14" FontWeight="Bold"/>
    </Border>
    <Border Grid.Row="1" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,0,0,1" Padding="10,8">
      <Grid>
        <TextBox x:Name="txt_filt_search" Height="28"/>
        <TextBlock Text="Search filters…" Foreground="#A0AEC0" FontSize="11"
                   IsHitTestVisible="False" VerticalAlignment="Center" Margin="8,0"/>
      </Grid>
    </Border>
    <ListBox Grid.Row="2" x:Name="lb_filters" Background="White" BorderThickness="0"/>
    <Border Grid.Row="3" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="12,8">
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <Button x:Name="btn_filt_ok"     Content="Select" Width="80" Height="28" Margin="0,0,8,0"
                Background="#2B6CB0" Foreground="White" FontWeight="SemiBold" BorderThickness="0"/>
        <Button x:Name="btn_filt_cancel" Content="Cancel" Width="70" Height="28"
                Background="#EDF2F7" BorderBrush="#CBD5E0" BorderThickness="1"/>
      </StackPanel>
    </Border>
  </Grid>
</Window>
"""

VIEW_RANGE_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Edit View Range" Width="430" Height="320"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Grid Margin="18">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>
    <TextBlock Grid.Row="0" Foreground="#4A5568" FontSize="11" Margin="0,0,0,10"
               TextWrapping="Wrap">
      Edit the offset (in feet) for each plane relative to its associated level.
      Level associations are not changed by this tool — use Revit's native dialog
      to switch which level a plane is associated with.
    </TextBlock>
    <Grid Grid.Row="1">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="160"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
      <TextBlock Grid.Row="0" Grid.Column="0" Text="Top Offset (ft):"      VerticalAlignment="Center" Margin="0,4"/>
      <TextBox   Grid.Row="0" Grid.Column="1" x:Name="txt_vr_top"   Height="28" Margin="0,4"/>
      <TextBlock Grid.Row="1" Grid.Column="0" Text="Cut Plane Offset (ft):" VerticalAlignment="Center" Margin="0,4"/>
      <TextBox   Grid.Row="1" Grid.Column="1" x:Name="txt_vr_cut"   Height="28" Margin="0,4"/>
      <TextBlock Grid.Row="2" Grid.Column="0" Text="Bottom Offset (ft):"   VerticalAlignment="Center" Margin="0,4"/>
      <TextBox   Grid.Row="2" Grid.Column="1" x:Name="txt_vr_bot"   Height="28" Margin="0,4"/>
      <TextBlock Grid.Row="3" Grid.Column="0" Text="View Depth Offset (ft):" VerticalAlignment="Center" Margin="0,4"/>
      <TextBox   Grid.Row="3" Grid.Column="1" x:Name="txt_vr_depth" Height="28" Margin="0,4"/>
    </Grid>
    <StackPanel Grid.Row="2" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,12,0,0">
      <Button x:Name="btn_vr_ok"     Content="Save"   Width="80" Height="30" Margin="0,0,8,0"
              Background="#2B6CB0" Foreground="White" FontWeight="SemiBold" BorderThickness="0"/>
      <Button x:Name="btn_vr_cancel" Content="Cancel" Width="70" Height="30"
              Background="#EDF2F7" Foreground="#2D3748" BorderBrush="#CBD5E0" BorderThickness="1"/>
    </StackPanel>
  </Grid>
</Window>
"""


# ═══════════════════════════════════════════════════════════════
# MAIN WINDOW CLASS
# ═══════════════════════════════════════════════════════════════

class ViewTemplateManagerWindow(Window):

    def __init__(self):
        # Parse XAML
        self._root = Markup.XamlReader.Parse(MAIN_XAML)
        self.Content = self._root.Content
        self.Width   = self._root.Width
        self.Height  = self._root.Height
        self.Title   = self._root.Title
        self.WindowStartupLocation = self._root.WindowStartupLocation
        self.Background = self._root.Background
        self.FontFamily = self._root.FontFamily
        self.FontSize   = self._root.FontSize
        self.ResizeMode = self._root.ResizeMode

        # Bind to Revit window
        try:
            helper = WindowInteropHelper(self)
            helper.Owner = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
        except Exception:
            pass

        F = lambda n: self._root.FindName(n)

        # Toolbar
        self.txt_search          = F("txt_search")
        self.ph_search           = F("ph_search")
        self.btn_filter          = F("btn_filter")
        self.lbl_filter_dot      = F("lbl_filter_dot")
        self.filter_panel        = F("filter_panel")
        self.pnl_filter_types    = F("pnl_filter_types")
        self.pnl_filter_used     = F("pnl_filter_used")
        self.btn_filter_reset    = F("btn_filter_reset")
        self.lbl_filter_summary  = F("lbl_filter_summary")
        self.btn_new             = F("btn_new")
        self.btn_rename          = F("btn_rename")
        self.btn_dup             = F("btn_dup")
        self.btn_delete          = F("btn_delete")
        self.btn_apply           = F("btn_apply")
        # Left panel local strip
        self.lbl_list_count      = F("lbl_list_count")
        self.btn_sel_all         = F("btn_sel_all")
        self.btn_sel_none        = F("btn_sel_none")
        self.dg_templates        = F("dg_templates")
        # Right panel headers
        self.lbl_selected_header = F("lbl_selected_header")
        self.brd_bulk_banner     = F("brd_bulk_banner")
        self.lbl_bulk_title      = F("lbl_bulk_title")
        self.lbl_bulk_subtitle   = F("lbl_bulk_subtitle")
        self.tab_main            = F("tab_main")

        # Properties (single)
        self.pnl_props_single    = F("pnl_props_single")
        self.txt_prop_name       = F("txt_prop_name")
        self.btn_prop_rename     = F("btn_prop_rename")
        self.lbl_prop_type       = F("lbl_prop_type")
        self.cmb_prop_scale      = F("cmb_prop_scale")
        self.cmb_prop_detail     = F("cmb_prop_detail")
        self.cmb_prop_parts      = F("cmb_prop_parts")
        self.cmb_prop_phase      = F("cmb_prop_phase")
        self.cmb_prop_discipline = F("cmb_prop_discipline")
        self.lbl_prop_viewrange  = F("lbl_prop_viewrange")
        self.btn_prop_viewrange  = F("btn_prop_viewrange")
        self.btn_prop_save       = F("btn_prop_save")
        self.lbl_prop_status     = F("lbl_prop_status")
        # Include-in-template checkboxes
        self.chk_inc_scale       = F("chk_inc_scale")
        self.chk_inc_detail      = F("chk_inc_detail")
        self.chk_inc_parts       = F("chk_inc_parts")
        self.chk_inc_phase       = F("chk_inc_phase")
        self.chk_inc_disc        = F("chk_inc_disc")
        self.chk_inc_vrange      = F("chk_inc_vrange")
        # Properties (bulk)
        self.pnl_props_bulk      = F("pnl_props_bulk")
        self.chk_bulk_scale      = F("chk_bulk_scale")
        self.cmb_bulk_scale      = F("cmb_bulk_scale")
        self.chk_bulk_detail     = F("chk_bulk_detail")
        self.cmb_bulk_detail     = F("cmb_bulk_detail")
        self.chk_bulk_parts      = F("chk_bulk_parts")
        self.cmb_bulk_parts      = F("cmb_bulk_parts")
        self.chk_bulk_phase      = F("chk_bulk_phase")
        self.cmb_bulk_phase      = F("cmb_bulk_phase")
        self.chk_bulk_disc       = F("chk_bulk_disc")
        self.cmb_bulk_disc       = F("cmb_bulk_disc")
        self.btn_props_bulk_apply = F("btn_props_bulk_apply")
        self.lbl_props_bulk_status = F("lbl_props_bulk_status")

        # VG tab
        self.rb_vg_model         = F("rb_vg_model")
        self.rb_vg_ann           = F("rb_vg_ann")
        self.rb_vg_imp           = F("rb_vg_imp")
        self.rb_vg_filt          = F("rb_vg_filt")
        self.lbl_vg_info         = F("lbl_vg_info")
        self.pnl_vg_single       = F("pnl_vg_single")
        self.pnl_vg_bulk         = F("pnl_vg_bulk")
        self.txt_vg_search       = F("txt_vg_search")
        self.ph_vg_search        = F("ph_vg_search")
        self.btn_vg_show_all     = F("btn_vg_show_all")
        self.btn_vg_hide_all     = F("btn_vg_hide_all")
        self.btn_vg_show_hl      = F("btn_vg_show_hl")
        self.btn_vg_hide_hl      = F("btn_vg_hide_hl")
        self.dg_vg_cats          = F("dg_vg_cats")
        self.dg_vg_filters       = F("dg_vg_filters")
        self.brd_filters_buttons = F("brd_filters_buttons")
        self.btn_filter_add      = F("btn_filter_add")
        self.btn_filter_remove   = F("btn_filter_remove")
        # VG bulk
        self.lbl_vg_bulk_help    = F("lbl_vg_bulk_help")
        self.dg_vg_bulk_cats     = F("dg_vg_bulk_cats")
        self.dg_vg_bulk_filters  = F("dg_vg_bulk_filters")
        self.btn_bulk_add_hide      = F("btn_bulk_add_hide")
        self.btn_bulk_add_show      = F("btn_bulk_add_show")
        self.btn_bulk_add_all_hide  = F("btn_bulk_add_all_hide")
        self.btn_bulk_add_all_show  = F("btn_bulk_add_all_show")
        self.btn_bulk_filt_add   = F("btn_bulk_filt_add")
        self.btn_bulk_filt_remove = F("btn_bulk_filt_remove")
        self.btn_bulk_remove     = F("btn_bulk_remove")
        self.btn_vg_bulk_apply   = F("btn_vg_bulk_apply")
        self.lbl_vg_bulk_status  = F("lbl_vg_bulk_status")

        # Links tab
        self.pnl_links_single    = F("pnl_links_single")
        self.pnl_links_bulk      = F("pnl_links_bulk")
        self.dg_links            = F("dg_links")
        self.dg_bulk_links       = F("dg_bulk_links")
        self.pnl_link_btns_single = F("pnl_link_btns_single")
        self.pnl_link_btns_bulk  = F("pnl_link_btns_bulk")
        self.btn_link_set_host   = F("btn_link_set_host")
        self.btn_link_set_linked = F("btn_link_set_linked")
        self.btn_link_toggle_ht  = F("btn_link_toggle_ht")
        self.btn_link_reset      = F("btn_link_reset")
        self.cmb_bulk_link_action = F("cmb_bulk_link_action")
        self.btn_link_bulk_apply = F("btn_link_bulk_apply")
        self.btn_links_reload    = F("btn_links_reload")
        self.lbl_links_status    = F("lbl_links_status")

        # Usage
        self.lbl_usage_header    = F("lbl_usage_header")
        self.dg_usage            = F("dg_usage")
        self.lbl_status          = F("lbl_status")

        # Collections
        self._all_items       = ObservableCollection[object]()
        self._filtered_items  = ObservableCollection[object]()
        self._vg_items        = ObservableCollection[object]()
        self._vg_all          = []
        self._vg_filter_items = ObservableCollection[object]()
        self._link_items      = ObservableCollection[object]()
        self._bulk_cat_items  = ObservableCollection[object]()
        self._bulk_cat_view   = ObservableCollection[object]()  # sub-tab-filtered view
        self._bulk_filt_items = ObservableCollection[object]()
        self._bulk_link_items = ObservableCollection[object]()
        self._last_checked_ids = frozenset()  # track which templates are checked
        self._usage_items     = ObservableCollection[object]()

        # State
        self._usage_map       = {}
        self._all_templates   = []
        self._link_types      = []      # deduped link types
        self._model_cats      = []
        self._ann_cats        = []
        self._imp_cats        = []
        self._cat_type_map    = {}    # eid_int(cat_id) -> "model"/"ann"/"imp"
        self._proj_filters    = []
        self._phase_filters   = []
        self._current_template = None
        self._suppress_vg_changes = False
        self._suppress_filter_changes = False
        self._filter_types = set()       # type names enabled by chips
        self._filter_usage = None        # None / "used" / "unused"
        self._filter_types = set()       # type names enabled by chips
        self._filter_usage = None        # None / "used" / "unused"
        self._chip_handlers_wired = []
        self._scales = [
            ('12" = 1\'-0"',     1),
            ('6" = 1\'-0"',      2),
            ('3" = 1\'-0"',      4),
            ('1 1/2" = 1\'-0"',  8),
            ('1" = 1\'-0"',      12),
            ('3/4" = 1\'-0"',    16),
            ('1/2" = 1\'-0"',    24),
            ('3/8" = 1\'-0"',    32),
            ('1/4" = 1\'-0"',    48),
            ('3/16" = 1\'-0"',   64),
            ('1/8" = 1\'-0"',    96),
            ('3/32" = 1\'-0"',   128),
            ('1/16" = 1\'-0"',   192),
            ('1/32" = 1\'-0"',   384),
            ('1/64" = 1\'-0"',   768),
            ("1:100", 100),
            ("1:200", 200),
            ("1:500", 500),
        ]

        # Wire events
        self.dg_templates.SelectionChanged    += self._on_template_selection_changed
        self.txt_search.TextChanged           += self._on_search_changed
        self.txt_search.GotFocus              += lambda s, e: setattr(self.ph_search, 'Visibility', Visibility.Collapsed)
        self.txt_search.LostFocus             += lambda s, e: self._toggle_placeholder()
        self.btn_filter.Checked   += self._on_filter_toggle
        self.btn_filter.Unchecked += self._on_filter_toggle
        self.btn_filter_reset.Click += self._on_filter_reset

        self.btn_new.Click           += self._on_new_click
        self.btn_rename.Click        += self._on_rename_click
        self.btn_dup.Click           += self._on_dup_click
        self.btn_delete.Click        += self._on_delete_click
        self.btn_apply.Click         += self._on_apply_to_views_click
        self.btn_sel_all.Click       += lambda s, e: self._select_all(True)
        self.btn_sel_none.Click      += lambda s, e: self._select_all(False)
        # Clicking a row checkbox propagates the state to all selected (highlighted) rows
        from System.Windows.Controls.Primitives import ButtonBase as _ButtonBase
        self.dg_templates.AddHandler(
            _ButtonBase.ClickEvent,
            RoutedEventHandler(self._on_dg_checkbox_click)
        )
        self.tab_main.SelectionChanged += self._on_tab_changed

        # Properties tab events
        self.btn_prop_rename.Click   += self._on_rename_click
        self.btn_prop_save.Click     += self._on_prop_save_click
        # Include-in-template checkboxes — toggle to enable/disable corresponding combos
        for chk, combo in (
                (self.chk_inc_scale,  self.cmb_prop_scale),
                (self.chk_inc_detail, self.cmb_prop_detail),
                (self.chk_inc_parts,  self.cmb_prop_parts),
                (self.chk_inc_phase,  self.cmb_prop_phase),
                (self.chk_inc_disc,   self.cmb_prop_discipline)):
            def _make(c, cb):
                def _h(s, e): cb.IsEnabled = bool(c.IsChecked)
                return _h
            handler = _make(chk, combo)
            chk.Checked   += handler
            chk.Unchecked += handler
        # View Range include toggle controls the View Range button
        def _vr_h(s, e):
            self.btn_prop_viewrange.IsEnabled = (
                bool(self.chk_inc_vrange.IsChecked) and self._checked_count() == 1)
        self.chk_inc_vrange.Checked   += _vr_h
        self.chk_inc_vrange.Unchecked += _vr_h
        self.btn_prop_viewrange.Click += self._on_view_range_click
        self.btn_props_bulk_apply.Click += self._on_props_bulk_apply

        # VG tab events
        self.rb_vg_model.Checked     += lambda s, e: self._refresh_vg_subtab()
        self.rb_vg_ann.Checked       += lambda s, e: self._refresh_vg_subtab()
        self.rb_vg_imp.Checked       += lambda s, e: self._refresh_vg_subtab()
        self.rb_vg_filt.Checked      += lambda s, e: self._refresh_vg_subtab()
        self.txt_vg_search.TextChanged += self._on_vg_search_changed
        self.btn_vg_show_all.Click   += lambda s, e: self._vg_set_all(visible=True)
        self.btn_vg_hide_all.Click   += lambda s, e: self._vg_set_all(visible=False)
        self.btn_vg_show_hl.Click    += lambda s, e: self._vg_set_highlighted(visible=True)
        self.btn_vg_hide_hl.Click    += lambda s, e: self._vg_set_highlighted(visible=False)
        self.btn_filter_add.Click    += self._on_filter_add_click
        self.btn_filter_remove.Click += self._on_filter_remove_click
        self.btn_bulk_add_hide.Click     += lambda s, e: self._bulk_add_cat(hide=True)
        self.btn_bulk_add_show.Click     += lambda s, e: self._bulk_add_cat(hide=False)
        self.btn_bulk_add_all_hide.Click += lambda s, e: self._bulk_add_all_cat(hide=True)
        self.btn_bulk_add_all_show.Click += lambda s, e: self._bulk_add_all_cat(hide=False)
        self.btn_bulk_filt_add.Click += lambda s, e: self._bulk_add_filter(action="Apply")
        self.btn_bulk_filt_remove.Click += lambda s, e: self._bulk_add_filter(action="Detach")
        self.btn_bulk_remove.Click   += self._on_bulk_remove_row
        self.btn_vg_bulk_apply.Click += self._on_vg_bulk_apply

        # Links tab events
        self.btn_link_set_host.Click   += lambda s, e: self._on_link_set_single("host")
        self.btn_link_set_linked.Click += lambda s, e: self._on_link_set_single("linked")
        self.btn_link_toggle_ht.Click  += lambda s, e: self._on_link_set_single("toggle_ht")
        self.btn_link_reset.Click      += lambda s, e: self._on_link_set_single("reset")
        self.btn_links_reload.Click    += lambda s, e: self._refresh_links_tab()
        self.btn_link_bulk_apply.Click += self._on_link_bulk_apply

        # Bind collections
        self.dg_templates.ItemsSource     = self._filtered_items
        self.dg_vg_cats.ItemsSource       = self._vg_items
        self.dg_vg_filters.ItemsSource    = self._vg_filter_items
        self.dg_links.ItemsSource         = self._link_items
        self.dg_bulk_links.ItemsSource    = self._bulk_link_items
        self.dg_vg_bulk_cats.ItemsSource  = self._bulk_cat_view
        self.dg_vg_bulk_filters.ItemsSource = self._bulk_filt_items
        self.dg_usage.ItemsSource         = self._usage_items

        # Populate scale combos
        for lbl, val in self._scales:
            for combo in (self.cmb_prop_scale, self.cmb_bulk_scale):
                item = ComboBoxItem()
                item.Content = lbl
                item.Tag     = val
                combo.Items.Add(item)

        # Default link bulk action
        self.cmb_bulk_link_action.SelectedIndex = 0

        # Load data
        self._load_all()
        self._update_mode()

    # ─────────────────────────────────────────────
    # DATA LOADING
    # ─────────────────────────────────────────────

    def _load_all(self):
        self._usage_map     = build_usage_map(doc)
        self._all_templates = get_all_templates(doc)
        self._link_types    = get_link_types_deduped(doc)
        self._model_cats    = get_model_categories(doc)
        self._ann_cats      = get_annotation_categories(doc)
        self._imp_cats      = get_imported_categories(doc)
        self._proj_filters  = get_project_filters(doc)
        self._phase_filters = get_phase_filters(doc)

        # Track previous selection to preserve checks across reloads
        prev_checked = set()
        for it in self._all_items:
            if it.IsSelected:
                prev_checked.add(eid_int(it.Id))

        self._all_items.Clear()
        for vt in self._all_templates:
            usage = self._usage_map.get(eid_int(vt.Id), 0)
            ti = TemplateItem(vt, usage)
            if eid_int(vt.Id) in prev_checked:
                ti.IsSelected = True
            ti.add_PropertyChanged(self._on_template_item_changed)
            self._all_items.Add(ti)

        # Type / Used filter chips
        types_seen = sorted(set(viewtype_label(v.ViewType) for v in self._all_templates))
        # Drop type-chips that no longer exist in the project
        self._filter_types = self._filter_types.intersection(types_seen)
        self._populate_filter_chips(types_seen)

        # Phase filter combos
        for combo in (self.cmb_prop_phase, self.cmb_bulk_phase):
            combo.Items.Clear()
            ci = ComboBoxItem()
            ci.Content = "<None>"
            ci.Tag     = -1
            combo.Items.Add(ci)
            for name, fid in self._phase_filters:
                ci = ComboBoxItem()
                ci.Content = name
                ci.Tag     = eid_int(fid)
                combo.Items.Add(ci)

        # Bulk link list (one row per deduped link type)
        prev_apply = set(eid_int(it.TypeId) if it.TypeId else 0
                         for it in self._bulk_link_items if it.Apply)
        self._bulk_link_items.Clear()
        for lt in self._link_types:
            tid = lt["type_id"]
            apply = (tid is not None and eid_int(tid) in prev_apply)
            self._bulk_link_items.Add(
                BulkLinkItem(lt["name"], tid, lt["instance_ids"], apply=apply))

        self._apply_filter()
        self._update_status()

    def _apply_filter(self):
        search = (self.txt_search.Text or "").strip().lower()
        type_set = self._filter_types
        usage = self._filter_usage  # None / "used" / "unused"

        self._filtered_items.Clear()
        for item in self._all_items:
            if search and search not in item.Name.lower():
                continue
            if type_set and item.ViewTypeName not in type_set:
                continue
            if usage == "used"   and not item.IsUsed: continue
            if usage == "unused" and item.IsUsed:     continue
            self._filtered_items.Add(item)
        self._update_status()
        self._update_filter_summary()

    def _update_status(self):
        total    = self._all_items.Count
        filtered = self._filtered_items.Count
        checked  = sum(1 for i in self._all_items if i.IsSelected)
        if filtered == total:
            self.lbl_status.Text = "{}  templates  |  {} checked".format(total, checked)
        else:
            self.lbl_status.Text = "Showing {}  of {}  templates  |  {} checked".format(
                filtered, total, checked)
        self.lbl_list_count.Text = "{} templates  |  {} checked".format(filtered, checked)

    # ─────────────────────────────────────────────
    # FILTER CHIPS
    # ─────────────────────────────────────────────

    def _populate_filter_chips(self, types_seen):
        """Build the chip toggle buttons for the filter pane."""
        chip_style = self._root.TryFindResource("ChipToggle")
        # TYPE chips
        self.pnl_filter_types.Children.Clear()
        for t in types_seen:
            tb = ToggleButton()
            tb.Content = t
            tb.Tag = "TYP:" + t
            if chip_style is not None:
                tb.Style = chip_style
            tb.IsChecked = (t in self._filter_types)
            tb.Checked   += self._on_filter_chip
            tb.Unchecked += self._on_filter_chip
            self.pnl_filter_types.Children.Add(tb)
        # USED chips (mutually exclusive — like radios but toggleable to clear)
        self.pnl_filter_used.Children.Clear()
        for label, tag in (("Used", "used"), ("Unused", "unused")):
            tb = ToggleButton()
            tb.Content = label
            tb.Tag = "USE:" + tag
            if chip_style is not None:
                tb.Style = chip_style
            tb.IsChecked = (self._filter_usage == tag)
            tb.Checked   += self._on_filter_chip
            tb.Unchecked += self._on_filter_chip
            self.pnl_filter_used.Children.Add(tb)
        self._update_filter_summary()

    def _on_filter_toggle(self, sender, e):
        self.filter_panel.Visibility = (
            Visibility.Visible if self.btn_filter.IsChecked else Visibility.Collapsed)

    def _on_filter_chip(self, sender, e):
        tag = str(sender.Tag) if sender.Tag is not None else ""
        is_on = bool(sender.IsChecked)
        if tag.startswith("TYP:"):
            val = tag[4:]
            if is_on: self._filter_types.add(val)
            else:     self._filter_types.discard(val)
        elif tag.startswith("USE:"):
            val = tag[4:]
            if is_on:
                # Make this chip exclusive among USE: chips
                self._filter_usage = val
                for child in self.pnl_filter_used.Children:
                    if child is not sender and str(child.Tag).startswith("USE:"):
                        child.Checked   -= self._on_filter_chip
                        child.Unchecked -= self._on_filter_chip
                        child.IsChecked = False
                        child.Checked   += self._on_filter_chip
                        child.Unchecked += self._on_filter_chip
            else:
                if self._filter_usage == val:
                    self._filter_usage = None
        self._apply_filter()

    def _on_filter_reset(self, sender, e):
        self._filter_types.clear()
        self._filter_usage = None
        # Uncheck all chip toggles silently
        for panel in (self.pnl_filter_types, self.pnl_filter_used):
            for child in panel.Children:
                child.Checked   -= self._on_filter_chip
                child.Unchecked -= self._on_filter_chip
                child.IsChecked = False
                child.Checked   += self._on_filter_chip
                child.Unchecked += self._on_filter_chip
        self._apply_filter()

    def _update_filter_summary(self):
        bits = []
        if self._filter_types:
            bits.append("{} type{}".format(len(self._filter_types),
                                            "s" if len(self._filter_types) != 1 else ""))
        if self._filter_usage:
            bits.append(self._filter_usage)
        if bits:
            self.lbl_filter_summary.Text = "Active: " + ", ".join(bits)
        else:
            self.lbl_filter_summary.Text = ""
        # Visual hint on the filter button — show a small dot when active.
        try:
            dot = getattr(self, "lbl_filter_dot", None)
            if dot is not None:
                dot.Visibility = (Visibility.Visible if bits
                                  else Visibility.Collapsed)
        except Exception:
            pass

    # ─────────────────────────────────────────────
    # MODE SWITCHING
    # ─────────────────────────────────────────────

    def _checked_count(self):
        return sum(1 for i in self._all_items if i.IsSelected)

    def _checked_templates(self):
        return [i for i in self._all_items if i.IsSelected]

    def _clear_bulk_overrides(self):
        """Clear all pending bulk override rows and reset the status label."""
        self._bulk_cat_items.Clear()
        self._bulk_filt_items.Clear()
        self._bulk_cat_view.Clear()
        self._bulk_link_items.Clear()
        self.lbl_vg_bulk_status.Text = ""

    def _update_mode(self):
        """Update banner, header, and panel visibility based on check count."""
        n = self._checked_count()

        # If the set of checked templates has changed, reset the bulk override lists
        # so overrides built for one selection don't silently carry over to another.
        current_ids = frozenset(
            eid_int(ti.View.Id) for ti in self._checked_templates()
        )
        if current_ids != self._last_checked_ids:
            self._last_checked_ids = current_ids
            self._clear_bulk_overrides()

        # Banner
        if n >= 2:
            self.brd_bulk_banner.Visibility = Visibility.Visible
            self.lbl_bulk_subtitle.Text = (
                "Editing {} templates. Each tab now applies overrides to ALL checked templates."
                .format(n))
        else:
            self.brd_bulk_banner.Visibility = Visibility.Collapsed

        # Selected header
        if n == 0:
            if self._current_template is not None:
                self.lbl_selected_header.Text = "Previewing: " + self._current_template.Name + "  (no templates checked — editor disabled)"
            else:
                self.lbl_selected_header.Text = "Select a template on the left to view or edit its settings."
        elif n == 1:
            tmpl = self._checked_templates()[0].View
            self.lbl_selected_header.Text = "Editing: {}  ({})".format(
                tmpl.Name, viewtype_label(tmpl.ViewType))
            # Auto-set current template to the single checked one
            self._current_template = tmpl
        else:
            self.lbl_selected_header.Text = "Bulk editing {} templates".format(n)

        # Panel visibility per tab
        single_vis = (Visibility.Visible if n == 1 else Visibility.Collapsed)
        bulk_vis   = (Visibility.Visible if n >= 2 else Visibility.Collapsed)

        # Properties tab
        self.pnl_props_single.Visibility = single_vis
        self.pnl_props_bulk.Visibility   = bulk_vis
        # If no selection, also collapse single panel (so nothing editable)
        if n == 0:
            self.pnl_props_single.Visibility = Visibility.Collapsed

        # VG tab — single vs bulk panel
        self.pnl_vg_single.Visibility = single_vis if n == 1 else Visibility.Collapsed
        self.pnl_vg_bulk.Visibility   = bulk_vis

        # Links tab
        self.pnl_links_single.Visibility = single_vis if n == 1 else Visibility.Collapsed
        self.pnl_links_bulk.Visibility   = bulk_vis
        self.pnl_link_btns_single.Visibility = single_vis if n == 1 else Visibility.Collapsed
        self.pnl_link_btns_bulk.Visibility   = bulk_vis

        # If no templates checked, show a friendly empty state on Properties
        # Disable action buttons
        any_checked = (n >= 1)
        # Enable/disable
        self.btn_prop_save.IsEnabled       = (n == 1)
        self.btn_prop_rename.IsEnabled     = (n == 1)
        self.btn_prop_viewrange.IsEnabled  = (n == 1)
        self.btn_props_bulk_apply.IsEnabled = (n >= 2)
        self.btn_vg_bulk_apply.IsEnabled   = (n >= 2)
        self.btn_link_bulk_apply.IsEnabled = (n >= 2)
        self.btn_link_set_host.IsEnabled   = (n == 1)
        self.btn_link_set_linked.IsEnabled = (n == 1)
        self.btn_link_toggle_ht.IsEnabled  = (n == 1)
        self.btn_link_reset.IsEnabled      = (n == 1)
        self.btn_filter_add.IsEnabled      = (n == 1)
        self.btn_filter_remove.IsEnabled   = (n == 1)
        self.btn_vg_show_all.IsEnabled     = (n == 1)
        self.btn_vg_hide_all.IsEnabled     = (n == 1)
        self.btn_vg_show_hl.IsEnabled      = (n == 1)
        self.btn_vg_hide_hl.IsEnabled      = (n == 1)

        # Auto-refresh active tab content
        self._refresh_active_tab()

    # ─────────────────────────────────────────────
    # EVENT HANDLERS — LIST
    # ─────────────────────────────────────────────

    def _toggle_placeholder(self):
        if not self.txt_search.Text:
            self.ph_search.Visibility = Visibility.Visible

    def _on_search_changed(self, sender, e):
        self.ph_search.Visibility = (Visibility.Collapsed if self.txt_search.Text
                                     else Visibility.Visible)
        self._apply_filter()

    def _on_template_selection_changed(self, sender, e):
        item = self.dg_templates.SelectedItem
        if item is None:
            return
        # Track the currently focused template for "preview" purposes,
        # but mode is driven by checkbox count.
        self._current_template = item.View
        # If exactly one is checked AND that template equals this row,
        # show single-mode editing for it. Otherwise just preview.
        if self._checked_count() == 0:
            self.lbl_selected_header.Text = "Previewing: {}  ({}) — check the box to edit".format(
                item.Name, item.ViewTypeName)
        self._refresh_active_tab()

    def _on_template_item_changed(self, sender, e):
        if e.PropertyName == "IsSelected":
            self._update_mode()
            self._update_status()

    def _on_tab_changed(self, sender, e):
        # SelectionChanged bubbles — ignore events from child DataGrids/ListBoxes
        if e.OriginalSource is not self.tab_main:
            return
        self._refresh_active_tab()

    def _refresh_active_tab(self):
        idx = self.tab_main.SelectedIndex
        n = self._checked_count()
        if idx == 0:
            self._load_properties_tab()
        elif idx == 1:
            self._refresh_vg_subtab()
        elif idx == 2:
            self._refresh_links_tab()
        elif idx == 3:
            self._load_usage_tab()

    def _select_all(self, state):
        for item in self._filtered_items:
            item.IsSelected = state
        self._update_mode()
        self._update_status()

    def _check_highlighted(self, state):
        """Apply check/uncheck to whichever rows are currently row-highlighted."""
        rows = list(self.dg_templates.SelectedItems)
        if not rows:
            self._set_status("No rows highlighted — Shift/Ctrl-click rows first.")
            return
        for item in rows:
            try: item.IsSelected = state
            except Exception: pass
        self._update_mode()
        self._update_status()

    def _on_dg_checkbox_click(self, sender, e):
        """When a row checkbox is clicked, propagate its new state to all
        currently highlighted (row-selected) rows so a single click
        checks/unchecks the whole selection."""
        chk = e.OriginalSource
        if not isinstance(chk, CheckBox):
            return
        new_state = bool(chk.IsChecked)
        for item in list(self.dg_templates.SelectedItems):
            try:
                item.IsSelected = new_state
            except Exception:
                pass
        self._update_mode()
        self._update_status()

    # ─────────────────────────────────────────────
    # TOOLBAR ACTIONS
    # ─────────────────────────────────────────────

    def _resolve_active_template(self):
        """Return the View object the user is targeting for single-template
        actions: prefer single-checked, fall back to selected row."""
        n = self._checked_count()
        if n == 1:
            return self._checked_templates()[0].View
        item = self.dg_templates.SelectedItem
        if item is not None:
            return item.View
        return None

    def _on_new_click(self, sender, e):
        base = self._resolve_active_template()
        if base is None and self._all_templates:
            base = self._all_templates[0]
        if base is None:
            MessageBox.Show("No view templates found to base the new template on.",
                            "No Templates", MessageBoxButton.OK, MessageBoxImage.Warning)
            return
        new_name = self._prompt_rename("New Template Name", base.Name + " Copy")
        if new_name is None:
            return
        try:
            import Autodesk.Revit.DB as DB
            t = Transaction(doc, "VTM: New Template")
            t.Start()
            new_ids = DB.ElementTransformUtils.CopyElement(doc, base.Id, DB.XYZ(0, 0, 0))
            if new_ids and new_ids.Count > 0:
                new_elem = doc.GetElement(new_ids[0])
                if new_elem:
                    new_elem.Name = new_name
                    t.Commit()
                    self._load_all()
                    self._set_status("Created template: " + new_name)
                    return
            t.RollBack()
        except Exception as ex:
            try: t.RollBack()
            except Exception: pass
            MessageBox.Show("Could not create template:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _on_rename_click(self, sender=None, e=None):
        target = self._resolve_active_template()
        if target is None:
            MessageBox.Show("Select a template first.", "No Selection",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return
        new_name = self._prompt_rename("Rename Template", target.Name)
        if new_name is None or new_name == target.Name:
            return
        try:
            t = Transaction(doc, "VTM: Rename Template")
            t.Start()
            target.Name = new_name
            t.Commit()
            self._load_all()
            self._update_mode()
            self._set_status("Renamed to: " + new_name)
        except Exception as ex:
            try: t.RollBack()
            except Exception: pass
            MessageBox.Show("Rename failed:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _on_dup_click(self, sender, e):
        target = self._resolve_active_template()
        if target is None:
            MessageBox.Show("Select a template to duplicate.", "No Selection",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return
        new_name = self._prompt_rename("Duplicate Template", target.Name + " Copy")
        if new_name is None:
            return
        try:
            import Autodesk.Revit.DB as DB
            t = Transaction(doc, "VTM: Duplicate Template")
            t.Start()
            new_ids = DB.ElementTransformUtils.CopyElement(doc, target.Id, DB.XYZ(0, 0, 0))
            if new_ids and new_ids.Count > 0:
                new_elem = doc.GetElement(new_ids[0])
                if new_elem:
                    new_elem.Name = new_name
                    t.Commit()
                    self._load_all()
                    self._set_status("Duplicated as: " + new_name)
                    return
            t.RollBack()
            MessageBox.Show("Could not duplicate — no element returned.",
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)
        except Exception as ex:
            try: t.RollBack()
            except Exception: pass
            MessageBox.Show("Duplicate failed:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _on_delete_click(self, sender, e):
        selected = self._checked_templates()
        if not selected:
            row = self.dg_templates.SelectedItem
            if row:
                selected = [row]
        if not selected:
            MessageBox.Show("Select at least one template to delete.", "No Selection",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return
        names = "\n".join("  • " + i.Name for i in selected[:10])
        if len(selected) > 10:
            names += "\n  … and {} more".format(len(selected) - 10)
        res = MessageBox.Show(
            "Delete {} template{}?\n\n{}".format(
                len(selected), "s" if len(selected) != 1 else "", names),
            "Confirm Delete", MessageBoxButton.YesNo, MessageBoxImage.Warning)
        if res != MessageBoxResult.Yes:
            return
        deleted, errors = 0, []
        try:
            t = Transaction(doc, "VTM: Delete Templates")
            t.Start()
            for item in selected:
                try:
                    if item.UsedBy > 0:
                        errors.append("{} (used by {} views)".format(item.Name, item.UsedBy))
                        continue
                    doc.Delete(item.Id)
                    deleted += 1
                except Exception as ex:
                    errors.append("{}: {}".format(item.Name, str(ex)))
            t.Commit()
        except Exception as ex:
            try: t.RollBack()
            except Exception: pass
            MessageBox.Show("Delete failed:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)
            return
        self._load_all()
        msg = "Deleted {} template{}.".format(deleted, "s" if deleted != 1 else "")
        if errors:
            msg += "\n\nSkipped:\n" + "\n".join(errors)
        self._set_status(msg)

    def _on_apply_to_views_click(self, sender, e):
        target = self._resolve_active_template()
        if target is None:
            MessageBox.Show("Select a single template first (one row).", "No Selection",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return
        match = None
        for it in self._all_items:
            if eid_int(it.Id) == eid_int(target.Id):
                match = it
                break
        if match:
            self._show_apply_to_views_dialog(match)

    # ─────────────────────────────────────────────
    # PROPERTIES TAB
    # ─────────────────────────────────────────────

    def _load_properties_tab(self):
        n = self._checked_count()
        if n != 1:
            return
        vt = self._current_template
        if vt is None:
            return
        self.txt_prop_name.Text = vt.Name
        try:    self.lbl_prop_type.Text = viewtype_label(vt.ViewType)
        except Exception: self.lbl_prop_type.Text = "-"
        try:
            sc = vt.Scale
            self._select_combo_by_tag(self.cmb_prop_scale, sc)
        except Exception: pass
        try:
            p = vt.get_Parameter(BuiltInParameter.VIEW_DETAIL_LEVEL)
            if p:
                self._select_combo_by_tag(self.cmb_prop_detail, p.AsInteger())
        except Exception: pass
        try:
            p = vt.get_Parameter(BuiltInParameter.VIEW_PARTS_VISIBILITY)
            if p:
                self._select_combo_by_tag(self.cmb_prop_parts, p.AsInteger())
        except Exception: pass
        try:
            p = vt.get_Parameter(BuiltInParameter.VIEW_PHASE_FILTER)
            if p:
                cur_id = eid_int(p.AsElementId()) if p.AsElementId() else -1
                self._select_combo_by_tag(self.cmb_prop_phase, cur_id)
            else:
                self.cmb_prop_phase.SelectedIndex = 0
        except Exception:
            self.cmb_prop_phase.SelectedIndex = 0
        try:
            disc = int(vt.Discipline)
            self._select_combo_by_tag(self.cmb_prop_discipline, disc)
        except Exception: pass
        self._update_view_range_label()
        self._load_inclusion_state()
        self.lbl_prop_status.Text = ""

    def _load_inclusion_state(self):
        """Read non-controlled template parameter IDs and set Include checkboxes."""
        vt = self._current_template
        if vt is None:
            return
        try:
            non_ctrl_eids = vt.GetNonControlledTemplateParameterIds()
            non_ctrl = set()
            for eid in non_ctrl_eids:
                try: non_ctrl.add(eid_int(eid))
                except Exception: pass
        except Exception:
            non_ctrl = set()
        # Map: checkbox -> (BuiltInParameter, combo)
        for chk, bip, combo in (
                (self.chk_inc_scale,  BuiltInParameter.VIEW_SCALE,            self.cmb_prop_scale),
                (self.chk_inc_detail, BuiltInParameter.VIEW_DETAIL_LEVEL,     self.cmb_prop_detail),
                (self.chk_inc_parts,  BuiltInParameter.VIEW_PARTS_VISIBILITY, self.cmb_prop_parts),
                (self.chk_inc_phase,  BuiltInParameter.VIEW_PHASE_FILTER,     self.cmb_prop_phase),
                (self.chk_inc_disc,   BuiltInParameter.VIEW_DISCIPLINE,       self.cmb_prop_discipline)):
            included = True
            try:
                p = vt.get_Parameter(bip)
                if p:
                    included = (eid_int(p.Id) not in non_ctrl)
            except Exception:
                pass
            chk.IsChecked = included
            combo.IsEnabled = included
        # View range: try to find a matching parameter
        included_vr = True
        for bip in (BuiltInParameter.PLAN_VIEW_RANGE,):
            try:
                p = vt.get_Parameter(bip)
                if p:
                    included_vr = (eid_int(p.Id) not in non_ctrl)
                    break
            except Exception:
                pass
        self.chk_inc_vrange.IsChecked = included_vr

    def _update_view_range_label(self):
        vt = self._current_template
        if vt is None:
            self.lbl_prop_viewrange.Text = "—"
            self.btn_prop_viewrange.IsEnabled = False
            return
        if isinstance(vt, ViewPlan) or is_plan_view_type(vt.ViewType):
            try:
                vr = vt.GetViewRange()
                top = vr.GetOffset(PlanViewPlane.TopClipPlane)
                cut = vr.GetOffset(PlanViewPlane.CutPlane)
                bot = vr.GetOffset(PlanViewPlane.BottomClipPlane)
                dep = vr.GetOffset(PlanViewPlane.ViewDepthPlane)
                self.lbl_prop_viewrange.Text = "Top {:.2f}'  Cut {:.2f}'  Bot {:.2f}'  Depth {:.2f}'".format(
                    top, cut, bot, dep)
                self.btn_prop_viewrange.IsEnabled = (
                    self._checked_count() == 1 and bool(self.chk_inc_vrange.IsChecked))
            except Exception:
                self.lbl_prop_viewrange.Text = "(View range not editable for this template)"
                self.btn_prop_viewrange.IsEnabled = False
        else:
            self.lbl_prop_viewrange.Text = "(Not applicable — non-plan view template)"
            self.btn_prop_viewrange.IsEnabled = False

    def _select_combo_by_tag(self, combo, tag):
        for i in range(combo.Items.Count):
            ci = combo.Items[i]
            if hasattr(ci, "Tag") and ci.Tag == tag:
                combo.SelectedIndex = i
                return

    def _on_prop_save_click(self, sender, e):
        if self._checked_count() != 1:
            self.lbl_prop_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_prop_status.Text = "Check exactly one template to save its properties."
            return
        vt = self._current_template
        if vt is None:
            return
        errors = []
        t = None
        try:
            t = Transaction(doc, "VTM: Save Template Properties")
            t.Start()
            if self.cmb_prop_scale.SelectedItem:
                ci = self.cmb_prop_scale.SelectedItem
                if hasattr(ci, "Tag") and ci.Tag:
                    try: vt.Scale = int(ci.Tag)
                    except Exception as ex: errors.append("Scale: " + str(ex))
            if self.cmb_prop_detail.SelectedItem:
                ci = self.cmb_prop_detail.SelectedItem
                if hasattr(ci, "Tag"):
                    try:
                        p = vt.get_Parameter(BuiltInParameter.VIEW_DETAIL_LEVEL)
                        if p and not p.IsReadOnly:
                            p.Set(int(ci.Tag))
                    except Exception as ex: errors.append("DetailLevel: " + str(ex))
            if self.cmb_prop_parts.SelectedItem:
                ci = self.cmb_prop_parts.SelectedItem
                if hasattr(ci, "Tag"):
                    try:
                        p = vt.get_Parameter(BuiltInParameter.VIEW_PARTS_VISIBILITY)
                        if p and not p.IsReadOnly:
                            p.Set(int(ci.Tag))
                    except Exception as ex: errors.append("PartsVis: " + str(ex))
            if self.cmb_prop_phase.SelectedItem:
                ci = self.cmb_prop_phase.SelectedItem
                if hasattr(ci, "Tag"):
                    try:
                        p = vt.get_Parameter(BuiltInParameter.VIEW_PHASE_FILTER)
                        if p and not p.IsReadOnly:
                            tag = ci.Tag
                            if tag is None or int(tag) == -1:
                                p.Set(ElementId.InvalidElementId)
                            else:
                                p.Set(ElementId(int(tag)))
                    except Exception as ex: errors.append("PhaseFilter: " + str(ex))
            if self.cmb_prop_discipline.SelectedItem:
                ci = self.cmb_prop_discipline.SelectedItem
                if hasattr(ci, "Tag"):
                    try:
                        from Autodesk.Revit.DB import ViewDiscipline
                        vt.Discipline = ViewDiscipline(int(ci.Tag))
                    except Exception as ex: errors.append("Discipline: " + str(ex))
            # Update non-controlled-parameter list from include checkboxes
            try:
                cur_eids = list(vt.GetNonControlledTemplateParameterIds())
                cur_set = set(eid_int(eid) for eid in cur_eids)
                # Pairs: (checkbox, BuiltInParameter)
                pairs = [
                    (self.chk_inc_scale,  BuiltInParameter.VIEW_SCALE),
                    (self.chk_inc_detail, BuiltInParameter.VIEW_DETAIL_LEVEL),
                    (self.chk_inc_parts,  BuiltInParameter.VIEW_PARTS_VISIBILITY),
                    (self.chk_inc_phase,  BuiltInParameter.VIEW_PHASE_FILTER),
                    (self.chk_inc_disc,   BuiltInParameter.VIEW_DISCIPLINE),
                    (self.chk_inc_vrange, BuiltInParameter.PLAN_VIEW_RANGE),
                ]
                changed = False
                for chk, bip in pairs:
                    try:
                        p = vt.get_Parameter(bip)
                    except Exception:
                        p = None
                    if p is None:
                        continue
                    pid = eid_int(p.Id)
                    if bool(chk.IsChecked):
                        if pid in cur_set:
                            cur_set.discard(pid)
                            changed = True
                    else:
                        if pid not in cur_set:
                            cur_set.add(pid)
                            changed = True
                if changed:
                    new_list = List[ElementId]()
                    for v in cur_set:
                        new_list.Add(ElementId(int(v)))
                    vt.SetNonControlledTemplateParameterIds(new_list)
            except Exception as ex:
                errors.append("Include flags: " + str(ex))
            t.Commit()
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass
            self.lbl_prop_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_prop_status.Text = "Save failed: " + str(ex)
            return

        self._load_all()
        if errors:
            self.lbl_prop_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_prop_status.Text = "Partial save. Issues: " + "; ".join(errors)
        else:
            self.lbl_prop_status.Foreground = SolidColorBrush(Color.FromRgb(72, 187, 120))
            self.lbl_prop_status.Text = "✓ Saved"
        self._set_status("Properties saved for: " + vt.Name)

    def _on_view_range_click(self, sender, e):
        if self._checked_count() != 1:
            return
        vt = self._current_template
        if vt is None:
            return
        try:
            vr = vt.GetViewRange()
            top = vr.GetOffset(PlanViewPlane.TopClipPlane)
            cut = vr.GetOffset(PlanViewPlane.CutPlane)
            bot = vr.GetOffset(PlanViewPlane.BottomClipPlane)
            dep = vr.GetOffset(PlanViewPlane.ViewDepthPlane)
        except Exception as ex:
            MessageBox.Show("This template doesn't support a View Range:\n" + str(ex),
                            "Not Available", MessageBoxButton.OK, MessageBoxImage.Information)
            return
        result = self._prompt_view_range(top, cut, bot, dep)
        if result is None:
            return
        new_top, new_cut, new_bot, new_dep = result
        t = None
        try:
            t = Transaction(doc, "VTM: Edit View Range")
            t.Start()
            vr.SetOffset(PlanViewPlane.TopClipPlane,    float(new_top))
            vr.SetOffset(PlanViewPlane.CutPlane,        float(new_cut))
            vr.SetOffset(PlanViewPlane.BottomClipPlane, float(new_bot))
            vr.SetOffset(PlanViewPlane.ViewDepthPlane,  float(new_dep))
            vt.SetViewRange(vr)
            t.Commit()
            self._update_view_range_label()
            self._set_status("View range updated for: " + vt.Name)
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass
            MessageBox.Show("Could not save view range:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _on_props_bulk_apply(self, sender, e):
        targets = self._checked_templates()
        if len(targets) < 2:
            return
        apply_scale  = self.chk_bulk_scale.IsChecked  and self.cmb_bulk_scale.SelectedItem
        apply_detail = self.chk_bulk_detail.IsChecked and self.cmb_bulk_detail.SelectedItem
        apply_parts  = self.chk_bulk_parts.IsChecked  and self.cmb_bulk_parts.SelectedItem
        apply_phase  = self.chk_bulk_phase.IsChecked  and self.cmb_bulk_phase.SelectedItem
        apply_disc   = self.chk_bulk_disc.IsChecked   and self.cmb_bulk_disc.SelectedItem
        if not (apply_scale or apply_detail or apply_parts or apply_phase or apply_disc):
            self.lbl_props_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_props_bulk_status.Text = "Nothing checked — pick at least one property."
            return

        new_scale  = int(self.cmb_bulk_scale.SelectedItem.Tag)  if apply_scale  else None
        new_detail = int(self.cmb_bulk_detail.SelectedItem.Tag) if apply_detail else None
        new_parts  = int(self.cmb_bulk_parts.SelectedItem.Tag)  if apply_parts  else None
        new_phase  = int(self.cmb_bulk_phase.SelectedItem.Tag)  if apply_phase  else None
        new_disc   = int(self.cmb_bulk_disc.SelectedItem.Tag)   if apply_disc   else None

        applied, errors = 0, []
        try:
            from Autodesk.Revit.DB import ViewDiscipline
            tg = TransactionGroup(doc, "VTM: Bulk Properties")
            tg.Start()
            for ti in targets:
                vt = ti.View
                t = Transaction(doc, "VTM: Bulk Properties - " + vt.Name)
                try:
                    t.Start()
                    if new_scale is not None:
                        try: vt.Scale = new_scale
                        except Exception as ex: errors.append("{}/Scale: {}".format(vt.Name, ex))
                    if new_detail is not None:
                        try:
                            p = vt.get_Parameter(BuiltInParameter.VIEW_DETAIL_LEVEL)
                            if p and not p.IsReadOnly: p.Set(new_detail)
                        except Exception as ex: errors.append("{}/Detail: {}".format(vt.Name, ex))
                    if new_parts is not None:
                        try:
                            p = vt.get_Parameter(BuiltInParameter.VIEW_PARTS_VISIBILITY)
                            if p and not p.IsReadOnly: p.Set(new_parts)
                        except Exception as ex: errors.append("{}/Parts: {}".format(vt.Name, ex))
                    if new_phase is not None:
                        try:
                            p = vt.get_Parameter(BuiltInParameter.VIEW_PHASE_FILTER)
                            if p and not p.IsReadOnly:
                                if new_phase == -1:
                                    p.Set(ElementId.InvalidElementId)
                                else:
                                    p.Set(ElementId(new_phase))
                        except Exception as ex: errors.append("{}/Phase: {}".format(vt.Name, ex))
                    if new_disc is not None:
                        try: vt.Discipline = ViewDiscipline(new_disc)
                        except Exception as ex: errors.append("{}/Disc: {}".format(vt.Name, ex))
                    t.Commit()
                    applied += 1
                except Exception as ex:
                    try: t.RollBack()
                    except Exception: pass
                    errors.append(vt.Name + ": " + str(ex))
            tg.Assimilate()
        except Exception as ex:
            self.lbl_props_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_props_bulk_status.Text = "Failed: " + str(ex)
            return

        self._load_all()
        self._update_mode()
        msg = "✓ Applied to {} template{}.".format(applied, "s" if applied != 1 else "")
        if errors:
            msg += " ({} issues)".format(len(errors))
        self.lbl_props_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(72, 187, 120))
        self.lbl_props_bulk_status.Text = msg
        self._set_status(msg)

    # ─────────────────────────────────────────────
    # VG OVERRIDES TAB
    # ─────────────────────────────────────────────

    def _current_vg_subtab(self):
        if self.rb_vg_filt.IsChecked:  return "filt"
        if self.rb_vg_imp.IsChecked:   return "imp"
        if self.rb_vg_ann.IsChecked:   return "ann"
        return "model"

    def _refresh_bulk_cat_view(self):
        """Filter _bulk_cat_items down to the active sub-tab for display."""
        sub = self._current_vg_subtab()
        if sub == "filt":
            return  # filter sub-tab uses dg_vg_bulk_filters
        self._bulk_cat_view.Clear()
        for item in self._bulk_cat_items:
            if item.CatType == sub:
                self._bulk_cat_view.Add(item)

    def _refresh_vg_subtab(self):
        sub = self._current_vg_subtab()
        n = self._checked_count()

        if sub == "filt":
            self.dg_vg_cats.Visibility    = Visibility.Collapsed
            self.dg_vg_filters.Visibility = Visibility.Visible
            self.brd_filters_buttons.Visibility = Visibility.Visible if n == 1 else Visibility.Collapsed
            self.dg_vg_bulk_cats.Visibility    = Visibility.Collapsed
            self.dg_vg_bulk_filters.Visibility = Visibility.Visible
            self.btn_bulk_add_hide.Visibility       = Visibility.Collapsed
            self.btn_bulk_add_show.Visibility       = Visibility.Collapsed
            self.btn_bulk_add_all_hide.Visibility   = Visibility.Collapsed
            self.btn_bulk_add_all_show.Visibility   = Visibility.Collapsed
            self.btn_bulk_filt_add.Visibility       = Visibility.Visible
            self.btn_bulk_filt_remove.Visibility    = Visibility.Visible
        else:
            self.dg_vg_cats.Visibility    = Visibility.Visible
            self.dg_vg_filters.Visibility = Visibility.Collapsed
            self.brd_filters_buttons.Visibility = Visibility.Collapsed
            self.dg_vg_bulk_cats.Visibility    = Visibility.Visible
            self.dg_vg_bulk_filters.Visibility = Visibility.Collapsed
            self.btn_bulk_add_hide.Visibility       = Visibility.Visible
            self.btn_bulk_add_show.Visibility       = Visibility.Visible
            self.btn_bulk_add_all_hide.Visibility   = Visibility.Visible
            self.btn_bulk_add_all_show.Visibility   = Visibility.Visible
            self.btn_bulk_filt_add.Visibility       = Visibility.Collapsed
            self.btn_bulk_filt_remove.Visibility    = Visibility.Collapsed

        if n == 1:
            if sub == "filt":
                self._reload_template_filters()
            else:
                self._reload_vg_categories()
        else:
            self._vg_items.Clear()
            self._vg_filter_items.Clear()
            if n == 0:
                self.lbl_vg_info.Text = "Check a template to see/edit categories."
            else:
                self.lbl_vg_info.Text = "Bulk mode — build override list below."
        # Always sync the bulk filtered view to the current sub-tab
        if sub != "filt":
            self._refresh_bulk_cat_view()

    def _reload_vg_categories(self):
        vt = self._current_template
        if vt is None:
            self._vg_items.Clear()
            self.lbl_vg_info.Text = ""
            return
        sub = self._current_vg_subtab()
        if sub == "imp":
            cats = self._imp_cats
        elif sub == "ann":
            cats = self._ann_cats
        else:
            cats = self._model_cats
        self._vg_all = []
        search = (self.txt_vg_search.Text or "").strip().lower()
        self._suppress_vg_changes = True
        self._vg_items.Clear()
        for name, cat_id in cats:
            try:
                if not category_can_be_hidden(vt, cat_id):
                    continue
                is_hidden = vt.GetCategoryHidden(cat_id)
                item = CategoryVisItem(name, cat_id, is_hidden)
                item.add_PropertyChanged(self._on_vg_item_changed)
                self._vg_all.append(item)
                if not search or search in name.lower():
                    self._vg_items.Add(item)
            except Exception:
                pass
        self._suppress_vg_changes = False
        sub_label = {"model":"model","ann":"annotation","imp":"imported"}[sub]
        self.lbl_vg_info.Text = "{} {} categories for '{}'.".format(
            len(self._vg_all), sub_label, vt.Name)

    def _reload_template_filters(self):
        vt = self._current_template
        self._suppress_filter_changes = True
        self._vg_filter_items.Clear()
        if vt is None:
            self._suppress_filter_changes = False
            self.lbl_vg_info.Text = ""
            return
        rows = get_template_filters(vt, doc)
        for r in rows:
            item = FilterRowItem(r["name"], r["filter_id"], r["visible"], r["enabled"])
            item.add_PropertyChanged(self._on_filter_row_changed)
            self._vg_filter_items.Add(item)
        self._suppress_filter_changes = False
        self.lbl_vg_info.Text = "{} filter{} on '{}'.".format(
            len(rows), "s" if len(rows) != 1 else "", vt.Name)

    def _on_vg_search_changed(self, sender, e):
        # Toggle placeholder visibility regardless of sub-tab so the
        # gray hint disappears the moment the user starts typing.
        ph = getattr(self, "ph_vg_search", None)
        if ph is not None:
            ph.Visibility = (Visibility.Collapsed if self.txt_vg_search.Text
                             else Visibility.Visible)
        if self._current_vg_subtab() == "filt":
            return
        search = (self.txt_vg_search.Text or "").strip().lower()
        self._vg_items.Clear()
        for item in self._vg_all:
            if not search or search in item.Name.lower():
                self._vg_items.Add(item)

    def _on_vg_item_changed(self, sender, e):
        if self._suppress_vg_changes:
            return
        if e.PropertyName not in ("IsVisible", "IsHidden"):
            return
        vt = self._current_template
        if vt is None or self._checked_count() != 1:
            return
        item = sender
        t = None
        try:
            t = Transaction(doc, "VTM: VG Category Visibility")
            t.Start()
            vt.SetCategoryHidden(item.CatId, item.IsHidden)
            t.Commit()
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass
            self._suppress_vg_changes = True
            item.IsHidden = not item.IsHidden
            self._suppress_vg_changes = False

    def _on_filter_row_changed(self, sender, e):
        if self._suppress_filter_changes:
            return
        vt = self._current_template
        if vt is None or self._checked_count() != 1:
            return
        item = sender
        t = None
        try:
            t = Transaction(doc, "VTM: Filter " + e.PropertyName)
            t.Start()
            if e.PropertyName == "Visible":
                vt.SetFilterVisibility(item.FilterId, item.Visible)
            elif e.PropertyName == "Enabled":
                try:
                    vt.SetIsFilterEnabled(item.FilterId, item.Enabled)
                except Exception:
                    pass
            t.Commit()
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass

    def _vg_set_highlighted(self, visible):
        """Apply visibility to highlighted rows in the VG categories list."""
        vt = self._current_template
        if vt is None or self._checked_count() != 1:
            return
        if self._current_vg_subtab() == "filt":
            return
        rows = list(self.dg_vg_cats.SelectedItems)
        if not rows:
            self._set_status("No category rows highlighted.")
            return
        hide = not visible
        changed = 0
        t = None
        try:
            t = Transaction(doc, "VTM: VG Set Highlighted")
            t.Start()
            self._suppress_vg_changes = True
            for item in rows:
                try:
                    vt.SetCategoryHidden(item.CatId, hide)
                    item.IsHidden = hide
                    changed += 1
                except Exception:
                    pass
            self._suppress_vg_changes = False
            t.Commit()
        except Exception as ex:
            self._suppress_vg_changes = False
            try:
                if t: t.RollBack()
            except Exception: pass
        self._set_status("{} categor{} set to {} in '{}'.".format(
            changed, "ies" if changed != 1 else "y",
            "visible" if visible else "hidden", vt.Name))

    def _vg_set_all(self, visible):
        vt = self._current_template
        if vt is None or self._checked_count() != 1:
            return
        if self._current_vg_subtab() == "filt":
            return
        hide = not visible
        changed = 0
        t = None
        try:
            t = Transaction(doc, "VTM: VG Set All")
            t.Start()
            self._suppress_vg_changes = True
            for item in self._vg_all:
                try:
                    vt.SetCategoryHidden(item.CatId, hide)
                    item.IsHidden = hide
                    changed += 1
                except Exception:
                    pass
            self._suppress_vg_changes = False
            t.Commit()
        except Exception as ex:
            self._suppress_vg_changes = False
            try:
                if t: t.RollBack()
            except Exception: pass
        self._set_status("{} categories set to {} in '{}'.".format(
            changed, "visible" if visible else "hidden", vt.Name))

    def _on_filter_add_click(self, sender, e):
        vt = self._current_template
        if vt is None or self._checked_count() != 1:
            return
        result = self._prompt_pick_filter()
        if result is None:
            return
        name, fid = result
        t = None
        try:
            t = Transaction(doc, "VTM: Add Filter")
            t.Start()
            vt.AddFilter(fid)
            t.Commit()
            self._reload_template_filters()
            self._set_status("Added filter '{}' to '{}'.".format(name, vt.Name))
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass
            MessageBox.Show("Could not add filter:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _on_filter_remove_click(self, sender, e):
        vt = self._current_template
        if vt is None or self._checked_count() != 1:
            return
        sel = self.dg_vg_filters.SelectedItem
        if sel is None:
            MessageBox.Show("Select a filter row to remove.", "No Selection",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return
        t = None
        try:
            t = Transaction(doc, "VTM: Remove Filter")
            t.Start()
            vt.RemoveFilter(sel.FilterId)
            t.Commit()
            self._reload_template_filters()
            self._set_status("Removed filter '{}' from '{}'.".format(sel.Name, vt.Name))
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass
            MessageBox.Show("Could not remove filter:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _bulk_add_cat(self, hide=True):
        result = self._prompt_pick_category()
        if result is None:
            return
        sub = self._current_vg_subtab()  # "model"/"ann"/"imp"
        for name, cat_id in result:
            for existing in self._bulk_cat_items:
                if eid_int(existing.CatId) == eid_int(cat_id):
                    existing.Hide = hide
                    break
            else:
                self._bulk_cat_items.Add(BulkCatItem(name, cat_id, hide=hide, cat_type=sub))
        self._refresh_bulk_cat_view()

    def _bulk_add_all_cat(self, hide=True):
        """Add EVERY category of the current sub-tab as a Hide / Show override."""
        sub = self._current_vg_subtab()
        if sub == "filt":
            return
        if sub == "imp":
            cats = self._imp_cats
        elif sub == "ann":
            cats = self._ann_cats
        else:
            cats = self._model_cats
        added = 0
        existing_ids = set(eid_int(it.CatId) for it in self._bulk_cat_items)
        for name, cat_id in cats:
            cid = eid_int(cat_id)
            if cid in existing_ids:
                # Update existing entry's Hide instead of duplicating
                for it in self._bulk_cat_items:
                    if eid_int(it.CatId) == cid:
                        it.Hide = hide
                        break
                continue
            cat_type = self._cat_type_map.get(cid, sub)
            self._bulk_cat_items.Add(BulkCatItem(name, cat_id, hide=hide, cat_type=cat_type))
            existing_ids.add(cid)
            added += 1
        self._refresh_bulk_cat_view()
        sub_label = {"model":"model","ann":"annotation","imp":"imported"}[sub]
        action = "Hide" if hide else "Show"
        self.lbl_vg_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(72, 187, 120))
        self.lbl_vg_bulk_status.Text = "Added {} {} categor{} to {}.".format(
            added, sub_label, "ies" if added != 1 else "y", action)

    def _bulk_add_filter(self, action="Apply"):
        result = self._prompt_pick_filter()
        if result is None:
            return
        name, fid = result
        for existing in self._bulk_filt_items:
            if eid_int(existing.FilterId) == eid_int(fid):
                existing.Action = action
                return
        self._bulk_filt_items.Add(BulkFilterItem(name, fid, action=action))

    def _on_bulk_remove_row(self, sender, e):
        sub = self._current_vg_subtab()
        if sub == "filt":
            sel = self.dg_vg_bulk_filters.SelectedItem
            if sel is not None: self._bulk_filt_items.Remove(sel)
        else:
            sel = self.dg_vg_bulk_cats.SelectedItem
            if sel is not None:
                # remove from underlying list, then refresh view
                try: self._bulk_cat_items.Remove(sel)
                except Exception: pass
                self._refresh_bulk_cat_view()

    def _on_vg_bulk_apply(self, sender, e):
        targets = self._checked_templates()
        if len(targets) < 2:
            return
        cat_overrides    = list(self._bulk_cat_items)
        filter_overrides = list(self._bulk_filt_items)
        if not cat_overrides and not filter_overrides:
            self.lbl_vg_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_vg_bulk_status.Text = "No overrides defined — add some first."
            return

        applied, errors = 0, []
        try:
            tg = TransactionGroup(doc, "VTM: Bulk VG Overrides")
            tg.Start()
            for ti in targets:
                vt = ti.View
                t = Transaction(doc, "VTM: Bulk VG - " + vt.Name)
                try:
                    t.Start()
                    for co in cat_overrides:
                        try:
                            if category_can_be_hidden(vt, co.CatId):
                                vt.SetCategoryHidden(co.CatId, co.Hide)
                        except Exception as ex:
                            errors.append("{}/{}: {}".format(vt.Name, co.Name, ex))
                    for fo in filter_overrides:
                        try:
                            if fo.Action == "Apply":
                                applied_already = False
                                try:
                                    cur_ids = vt.GetFilters()
                                    applied_already = any(eid_int(fid) == eid_int(fo.FilterId)
                                                          for fid in cur_ids)
                                except Exception:
                                    pass
                                if not applied_already:
                                    vt.AddFilter(fo.FilterId)
                            elif fo.Action == "Detach":
                                try: vt.RemoveFilter(fo.FilterId)
                                except Exception: pass
                        except Exception as ex:
                            errors.append("{}/Filter {}: {}".format(vt.Name, fo.Name, ex))
                    t.Commit()
                    applied += 1
                except Exception as ex:
                    try: t.RollBack()
                    except Exception: pass
                    errors.append(vt.Name + ": " + str(ex))
            tg.Assimilate()
        except Exception as ex:
            self.lbl_vg_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
            self.lbl_vg_bulk_status.Text = "Failed: " + str(ex)
            return

        msg = "✓ Applied VG overrides to {} template{}.".format(applied, "s" if applied != 1 else "")
        if errors:
            msg += " ({} issues)".format(len(errors))
        self._set_status(msg)
        # Clear the override list now that it's been applied
        self._clear_bulk_overrides()
        self.lbl_vg_bulk_status.Foreground = SolidColorBrush(Color.FromRgb(72, 187, 120))
        self.lbl_vg_bulk_status.Text = msg

    # ─────────────────────────────────────────────
    # REVIT LINKS TAB
    # ─────────────────────────────────────────────

    def _refresh_links_tab(self):
        self._link_items.Clear()
        if not self._link_types:
            self.lbl_links_status.Text = "  No linked Revit models found in this project."
            return

        n = self._checked_count()
        if n == 1 and self._current_template is not None:
            vt = self._current_template
            for lt in self._link_types:
                first_inst = lt["first_instance"]
                if first_inst is None:
                    continue
                disp = get_link_display_type(vt, first_inst.Id)
                ht   = get_link_halftone(vt, first_inst.Id)
                self._link_items.Add(LinkRowItem(
                    lt["name"], lt["type_id"], lt["instance_ids"], disp, ht))
            self.lbl_links_status.Text = ""
        else:
            for lt in self._link_types:
                self._link_items.Add(LinkRowItem(
                    lt["name"], lt["type_id"], lt["instance_ids"], "—", False))
            if n == 0:
                self.lbl_links_status.Text = "  Check a template to view per-template settings."
            else:
                self.lbl_links_status.Text = "  Bulk mode — use the checkboxes and the action menu below."

    def _on_link_set_single(self, action):
        if self._checked_count() != 1:
            return
        vt = self._current_template
        if vt is None: return
        sel = self.dg_links.SelectedItem
        if sel is None:
            MessageBox.Show("Select a link row first.", "No Selection",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return
        if not _HAS_LINK_GRAPHICS:
            MessageBox.Show("RevitLinkGraphicsSettings is not available in this Revit version.",
                            "Not Supported", MessageBoxButton.OK, MessageBoxImage.Warning)
            return
        t = None
        try:
            t = Transaction(doc, "VTM: Link Override")
            t.Start()
            for inst_id in sel.InstanceIds:
                self._apply_link_override(vt, inst_id, action)
            t.Commit()
            self._refresh_links_tab()
            self.lbl_links_status.Text = "✓ Updated '{}'".format(sel.Name)
            self._set_status("Updated link '{}' in '{}'.".format(sel.Name, vt.Name))
        except Exception as ex:
            try:
                if t: t.RollBack()
            except Exception: pass
            MessageBox.Show("Could not update link:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    def _on_link_bulk_apply(self, sender, e):
        targets = self._checked_templates()
        if len(targets) < 2:
            return
        if not _HAS_LINK_GRAPHICS:
            MessageBox.Show("RevitLinkGraphicsSettings is not available in this Revit version.",
                            "Not Supported", MessageBoxButton.OK, MessageBoxImage.Warning)
            return
        sel_action_item = self.cmb_bulk_link_action.SelectedItem
        action_tag = sel_action_item.Tag if sel_action_item else "host"
        chosen = [li for li in self._bulk_link_items if li.Apply]
        if not chosen:
            self.lbl_links_status.Text = "Check at least one link row above."
            return

        applied, errors = 0, []
        try:
            tg = TransactionGroup(doc, "VTM: Bulk Link Overrides")
            tg.Start()
            for ti in targets:
                vt = ti.View
                t = Transaction(doc, "VTM: Bulk Link - " + vt.Name)
                try:
                    t.Start()
                    for li in chosen:
                        for inst_id in li.InstanceIds:
                            try:
                                self._apply_link_override(vt, inst_id, action_tag)
                            except Exception as ex:
                                errors.append("{}/{}: {}".format(vt.Name, li.Name, ex))
                    t.Commit()
                    applied += 1
                except Exception as ex:
                    try: t.RollBack()
                    except Exception: pass
                    errors.append(vt.Name + ": " + str(ex))
            tg.Assimilate()
        except Exception as ex:
            self.lbl_links_status.Text = "Failed: " + str(ex)
            return

        msg = "✓ Applied link override to {} template{}.".format(
            applied, "s" if applied != 1 else "")
        if errors:
            msg += " ({} issues)".format(len(errors))
        self.lbl_links_status.Text = msg
        self._set_status(msg)

    def _apply_link_override(self, view_template, link_inst_id, action):
        """action in: host, linked, ht_on, ht_off, toggle_ht, reset"""
        from Autodesk.Revit.DB import LinkedViewType
        if action == "reset":
            try:
                view_template.SetLinkOverrides(link_inst_id, None)
            except Exception:
                settings = RevitLinkGraphicsSettings()
                view_template.SetLinkOverrides(link_inst_id, settings)
            return

        try:
            cur = view_template.GetLinkOverrides(link_inst_id)
            if cur is None:
                cur = RevitLinkGraphicsSettings()
        except Exception:
            cur = RevitLinkGraphicsSettings()

        if action == "host":
            try: cur.SetLinkVisibilityType(LinkedViewType.ByHostView)
            except Exception:
                try: cur.SetLinkViewType(LinkedViewType.ByHostView)
                except Exception:
                    cur = RevitLinkGraphicsSettings()
        elif action == "linked":
            try: cur.SetLinkVisibilityType(LinkedViewType.ByLinkedView)
            except Exception:
                try: cur.SetLinkViewType(LinkedViewType.ByLinkedView)
                except Exception: pass
        elif action == "ht_on":
            try: cur.Halftone = True
            except Exception: pass
        elif action == "ht_off":
            try: cur.Halftone = False
            except Exception: pass
        elif action == "toggle_ht":
            try: cur.Halftone = not cur.Halftone
            except Exception: pass

        view_template.SetLinkOverrides(link_inst_id, cur)

    # ─────────────────────────────────────────────
    # USAGE TAB
    # ─────────────────────────────────────────────

    def _load_usage_tab(self):
        """Show usage for every checked template, plus the focused row.

        Falls back to the focused row when no checks are present so the user
        can preview before checking.
        """
        self._usage_items.Clear()
        # Determine which templates to report on
        checked = self._checked_templates()
        targets = [ti.View for ti in checked] if checked else (
            [self._current_template] if self._current_template is not None else [])
        if not targets:
            self.lbl_usage_header.Text = "Select (check) one or more templates to see which views use them."
            return

        # Build sheet_map: view-id -> "Sheet# - Sheet Name"
        sheet_map = {}
        for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
            try:
                num = ""
                name = ""
                try:
                    p = sheet.get_Parameter(BuiltInParameter.SHEET_NUMBER)
                    if p: num = p.AsString() or ""
                except Exception: pass
                try:
                    p = sheet.get_Parameter(BuiltInParameter.SHEET_NAME)
                    if p: name = p.AsString() or ""
                except Exception: pass
                ref_str = "{} - {}".format(num, name).strip(" -")
                for vp_id in sheet.GetAllViewports():
                    vp = doc.GetElement(vp_id)
                    if vp:
                        sheet_map[eid_int(vp.ViewId)] = ref_str
            except Exception:
                pass

        # Build target_id -> template name lookup
        target_ids = {eid_int(vt.Id): vt.Name for vt in targets}

        # Aggregate, grouped by template (so rows for the same template appear together)
        rows_by_tmpl = {}  # tid -> list of UsageRow
        for v in FilteredElementCollector(doc).OfClass(View):
            if v.IsTemplate:
                continue
            try:
                tid = eid_int(v.ViewTemplateId)
            except Exception:
                continue
            if tid in target_ids:
                sheet_ref = sheet_map.get(eid_int(v.Id), "-")
                row = UsageRow(target_ids[tid], v.Name,
                               viewtype_label(v.ViewType), sheet_ref)
                rows_by_tmpl.setdefault(tid, []).append(row)

        # Add rows in template-name order, with views sorted within each
        total = 0
        used_count = 0
        for tid in sorted(rows_by_tmpl.keys(), key=lambda k: target_ids[k].lower()):
            rows = sorted(rows_by_tmpl[tid], key=lambda r: r.ViewName.lower())
            for r in rows:
                self._usage_items.Add(r)
            total += len(rows)
            used_count += 1
        unused = len(targets) - used_count
        if len(targets) == 1:
            tmpl = targets[0]
            self.lbl_usage_header.Text = "{} view{} use '{}'.".format(
                total, "s" if total != 1 else "", tmpl.Name)
        else:
            self.lbl_usage_header.Text = (
                "{} view{} across {} of {} checked templates ({} unused).".format(
                    total, "s" if total != 1 else "",
                    used_count, len(targets), unused))

    # ─────────────────────────────────────────────
    # DIALOGS
    # ─────────────────────────────────────────────

    def _prompt_rename(self, title, default_name=""):
        try:
            win = Markup.XamlReader.Parse(RENAME_XAML)
            win.Title = title
            win.Owner = self
            txt  = win.FindName("txt_new_name")
            ok   = win.FindName("btn_ok")
            canc = win.FindName("btn_cancel")
            txt.Text = default_name
            txt.SelectAll()
            result = [None]

            def on_ok(s, e):
                result[0] = txt.Text.strip()
                win.DialogResult = True
                win.Close()

            def on_cancel(s, e):
                win.DialogResult = False
                win.Close()

            def on_key(s, e):
                if e.Key == System.Windows.Input.Key.Return:
                    on_ok(s, e)
                elif e.Key == System.Windows.Input.Key.Escape:
                    on_cancel(s, e)

            ok.Click   += on_ok
            canc.Click += on_cancel
            txt.KeyDown += on_key
            win.ShowDialog()
            return result[0]
        except Exception as ex:
            MessageBox.Show("Rename dialog error:\n" + str(ex), "Error",
                            MessageBoxButton.OK, MessageBoxImage.Error)
            return None

    def _prompt_pick_category(self):
        """Show category picker. Returns a list of (name, cat_id) tuples,
        or None if cancelled."""
        try:
            win = Markup.XamlReader.Parse(PICK_CAT_XAML)
            win.Owner = self
            lb        = win.FindName("lb_cats")
            txt_s     = win.FindName("txt_cat_search")
            rb_model  = win.FindName("rb_cat_model")
            rb_ann    = win.FindName("rb_cat_ann")
            rb_imp    = win.FindName("rb_cat_imp")
            btn_ok    = win.FindName("btn_cat_ok")
            btn_canc  = win.FindName("btn_cat_cancel")
            lbl_count = win.FindName("lbl_cat_count")

            # Pre-select picker radio to match current bulk sub-tab
            sub = self._current_vg_subtab()
            if sub == "imp":   rb_imp.IsChecked = True
            elif sub == "ann": rb_ann.IsChecked = True
            else:              rb_model.IsChecked = True

            result = [None]
            def populate():
                search = (txt_s.Text or "").strip().lower()
                if rb_imp.IsChecked:
                    source = self._imp_cats
                elif rb_ann.IsChecked:
                    source = self._ann_cats
                else:
                    source = self._model_cats
                lb.Items.Clear()
                for name, cat_id in source:
                    if search and search not in name.lower():
                        continue
                    item = ListBoxItem()
                    item.Content = name
                    item.Tag     = cat_id
                    lb.Items.Add(item)
                lbl_count.Text = "{} item{}".format(lb.Items.Count,
                                                    "s" if lb.Items.Count != 1 else "")

            def update_count(s=None, e=None):
                try:
                    n = lb.SelectedItems.Count
                except Exception:
                    n = 0
                lbl_count.Text = "{} selected".format(n)

            txt_s.TextChanged += lambda s, e: populate()
            rb_model.Checked  += lambda s, e: populate()
            rb_ann.Checked    += lambda s, e: populate()
            rb_imp.Checked    += lambda s, e: populate()
            lb.SelectionChanged += update_count

            def on_ok(s, e):
                items = list(lb.SelectedItems)
                if not items: return
                result[0] = [(it.Content, it.Tag) for it in items]
                win.DialogResult = True
                win.Close()

            btn_ok.Click   += on_ok
            btn_canc.Click += lambda s, e: win.Close()
            lb.MouseDoubleClick += on_ok
            populate()
            win.ShowDialog()
            return result[0]
        except Exception as ex:
            MessageBox.Show("Category picker error:\n" + str(ex), "Error",
                            MessageBoxButton.OK, MessageBoxImage.Error)
            return None

    def _prompt_pick_filter(self):
        if not self._proj_filters:
            MessageBox.Show("No filters defined in this project.\n\n"
                            "Create filters in Revit (View > Filters) first.",
                            "No Filters", MessageBoxButton.OK, MessageBoxImage.Information)
            return None
        try:
            win = Markup.XamlReader.Parse(PICK_FILTER_XAML)
            win.Owner = self
            lb       = win.FindName("lb_filters")
            txt_s    = win.FindName("txt_filt_search")
            btn_ok   = win.FindName("btn_filt_ok")
            btn_canc = win.FindName("btn_filt_cancel")
            result = [None]

            def populate():
                search = (txt_s.Text or "").strip().lower()
                lb.Items.Clear()
                for name, fid in self._proj_filters:
                    if search and search not in name.lower():
                        continue
                    item = ListBoxItem()
                    item.Content = name
                    item.Tag     = fid
                    lb.Items.Add(item)

            txt_s.TextChanged += lambda s, e: populate()
            def on_ok(s, e):
                sel = lb.SelectedItem
                if sel is None: return
                result[0] = (sel.Content, sel.Tag)
                win.DialogResult = True
                win.Close()
            btn_ok.Click   += on_ok
            btn_canc.Click += lambda s, e: win.Close()
            lb.MouseDoubleClick += on_ok
            populate()
            win.ShowDialog()
            return result[0]
        except Exception as ex:
            MessageBox.Show("Filter picker error:\n" + str(ex), "Error",
                            MessageBoxButton.OK, MessageBoxImage.Error)
            return None

    def _prompt_view_range(self, top, cut, bot, dep):
        try:
            win = Markup.XamlReader.Parse(VIEW_RANGE_XAML)
            win.Owner = self
            txt_top   = win.FindName("txt_vr_top")
            txt_cut   = win.FindName("txt_vr_cut")
            txt_bot   = win.FindName("txt_vr_bot")
            txt_depth = win.FindName("txt_vr_depth")
            btn_ok    = win.FindName("btn_vr_ok")
            btn_canc  = win.FindName("btn_vr_cancel")
            txt_top.Text   = "{:.4f}".format(top)
            txt_cut.Text   = "{:.4f}".format(cut)
            txt_bot.Text   = "{:.4f}".format(bot)
            txt_depth.Text = "{:.4f}".format(dep)
            result = [None]
            def on_ok(s, e):
                try:
                    nt = float(txt_top.Text)
                    nc = float(txt_cut.Text)
                    nb = float(txt_bot.Text)
                    nd = float(txt_depth.Text)
                except Exception:
                    MessageBox.Show("All offsets must be numeric (in feet).", "Bad Input",
                                    MessageBoxButton.OK, MessageBoxImage.Warning)
                    return
                result[0] = (nt, nc, nb, nd)
                win.DialogResult = True
                win.Close()
            btn_ok.Click   += on_ok
            btn_canc.Click += lambda s, e: win.Close()
            win.ShowDialog()
            return result[0]
        except Exception as ex:
            MessageBox.Show("View Range dialog error:\n" + str(ex),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)
            return None

    def _show_apply_to_views_dialog(self, template_item):
        try:
            win = Markup.XamlReader.Parse(APPLY_VIEWS_XAML)
            win.Owner = self
            win.FindName("lbl_title").Text = "Apply Template: " + template_item.Name

            dg       = win.FindName("dg_av_views")
            txt_s    = win.FindName("txt_av_search")
            ph_s     = win.FindName("ph_av_search")
            btn_all  = win.FindName("btn_av_all")
            btn_none = win.FindName("btn_av_none")
            btn_appl = win.FindName("btn_av_apply")
            btn_canc = win.FindName("btn_av_cancel")
            lbl_stat = win.FindName("lbl_av_status")

            tmpl_map = {}
            for vt in self._all_templates:
                tmpl_map[eid_int(vt.Id)] = vt.Name

            rows = ObservableCollection[object]()
            all_rows = []
            for v in FilteredElementCollector(doc).OfClass(View):
                if v.IsTemplate:
                    continue
                if v.ViewType in (ViewType.Schedule, ViewType.Legend,
                                  ViewType.DraftingView, ViewType.Walkthrough):
                    continue
                tid = eid_int(v.ViewTemplateId)
                cur_tmpl = tmpl_map.get(tid, "<None>") if tid != -1 else "<None>"
                row = ApplyViewRow(v, cur_tmpl)
                all_rows.append(row)
                rows.Add(row)
            dg.ItemsSource = rows
            lbl_stat.Text  = "{} views".format(len(all_rows))

            def filter_rows():
                search = (txt_s.Text or "").strip().lower()
                rows.Clear()
                for r in all_rows:
                    if not search or search in r.ViewName.lower():
                        rows.Add(r)
                lbl_stat.Text = "{} of {} views".format(rows.Count, len(all_rows))
                # Hide the gray placeholder hint while the user is typing.
                if ph_s is not None:
                    ph_s.Visibility = (Visibility.Collapsed if txt_s.Text
                                       else Visibility.Visible)

            txt_s.TextChanged += lambda s, e: filter_rows()
            btn_all.Click  += lambda s, e: [setattr(r, 'IsChecked', True)  for r in rows]
            btn_none.Click += lambda s, e: [setattr(r, 'IsChecked', False) for r in rows]
            btn_canc.Click += lambda s, e: win.Close()

            def on_apply(s, e):
                to_apply = [r for r in all_rows if r.IsChecked]
                if not to_apply:
                    MessageBox.Show("No views selected.", "Nothing to Do",
                                    MessageBoxButton.OK, MessageBoxImage.Information)
                    return
                applied, errors = 0, []
                t = None
                try:
                    t = Transaction(doc, "VTM: Apply Template to Views")
                    t.Start()
                    for row in to_apply:
                        try:
                            row.View.ViewTemplateId = template_item.Id
                            applied += 1
                        except Exception as ex:
                            errors.append("{}: {}".format(row.ViewName, str(ex)))
                    t.Commit()
                except Exception as ex:
                    try:
                        if t: t.RollBack()
                    except Exception: pass
                    MessageBox.Show("Apply failed:\n" + str(ex), "Error",
                                    MessageBoxButton.OK, MessageBoxImage.Error)
                    return
                self._load_all()
                msg = "Applied to {} view{}.".format(applied, "s" if applied != 1 else "")
                if errors:
                    msg += " ({} errors)".format(len(errors))
                lbl_stat.Text = msg
                self._set_status(msg)
                if not errors:
                    win.Close()

            btn_appl.Click += on_apply
            win.ShowDialog()
        except Exception as ex:
            MessageBox.Show("Apply dialog error:\n" + traceback.format_exc(),
                            "Error", MessageBoxButton.OK, MessageBoxImage.Error)

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    def _set_status(self, msg):
        self.lbl_status.Text = msg

    def ShowDialog(self):
        return super(ViewTemplateManagerWindow, self).ShowDialog()


# ═══════════════════════════════════════════════════════════════
# LAUNCH
# ═══════════════════════════════════════════════════════════════

try:
    win = ViewTemplateManagerWindow()
    win.ShowDialog()
except Exception:
    import traceback
    TaskDialog.Show("View Templates Manager - Error",
                    traceback.format_exc())
