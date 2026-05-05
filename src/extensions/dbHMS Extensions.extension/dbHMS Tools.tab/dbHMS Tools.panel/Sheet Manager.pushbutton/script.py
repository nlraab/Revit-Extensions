# -*- coding: utf-8 -*-
"""Sheet Manager — Comprehensive sheet management for Revit.

Features:
  - View all sheets grouped by prefix (matches project browser)
  - Search / filter sheets
  - Inline selection with checkboxes
  - Group-level checkbox to select/deselect a whole trade group
  - Rename (single) or batch renumber with live preview
  - Duplicate sheets with views, preserving viewport alignment
      - Per-sheet: new number, new name, new view name, view template
      - Duplicate type: WithDetailing / Duplicate / AsDependent
      - New view name defaults to "<source view name> Copy"
  - Revision management: add / remove revisions from selected sheets
  - Navigate to sheet (open in canvas)
  - Delete sheets with confirmation
"""
__title__ = "Sheet\nManager"
__doc__ = "Manage sheets: group, rename, renumber, duplicate with views, revisions."

import clr
import re
import sys

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, Viewport, View, ViewType,
    ViewDuplicateOption, Transaction, BuiltInParameter, BuiltInCategory,
    ElementId, ViewSchedule, ScheduleSheetInstance, StorageType,
    InstanceBinding, Category
)
from Autodesk.Revit.UI import TaskDialog, TaskDialogCommonButtons, TaskDialogResult

import System
from System import String
from System.Windows import (
    Window, Application, Thickness, HorizontalAlignment, VerticalAlignment,
    Visibility, RoutedEventHandler
)
from System.Windows.Controls import (
    DataGrid, DataGridCheckBoxColumn, DataGridTextColumn,
    DataGridTemplateColumn, DataGridEditAction, DataGridLength,
    DataGridRow,
    ComboBox, ComboBoxItem, TextBox,
    ListBox, ListBoxItem, Button, CheckBox, RadioButton,
    StackPanel, Grid, Border, TextBlock, ScrollViewer,
    GroupStyle, Expander, TabControl, TabItem, WrapPanel,
    Orientation
)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Threading import DispatcherPriority
from System import Action
from System.Windows.Media import SolidColorBrush, Color, VisualTreeHelper
from System.Windows.Data import (
    CollectionViewSource, PropertyGroupDescription,
    Binding, BindingMode
)
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List
from System.ComponentModel import (
    INotifyPropertyChanged, PropertyChangedEventArgs, PropertyChangedEventHandler,
    SortDescription, ListSortDirection
)
import System.Windows.Markup as Markup
from System.Windows.Interop import WindowInteropHelper
import System.Diagnostics

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def get_prefix(sheet_number):
    """Return the FIRST alphabetic character only, so families share a group:
       E201, ED102 → 'E'   |   F101, FP201, FPD301 → 'F'.
    Falls back to the first character if the sheet number doesn't start with a letter."""
    s = (sheet_number or "").strip()
    if s and s[0].isalpha():
        return s[0].upper()
    return s[0] if s else "?"


def get_discipline(sheet):
    """Return Sheet Discipline or Discipline parameter, fallback 'Other'."""
    for param_name in ("Sheet Discipline", "Discipline"):
        p = sheet.LookupParameter(param_name)
        if p and p.AsString():
            return p.AsString()
    return "Other"


def get_current_revision(sheet):
    """Return the current revision string on a sheet."""
    try:
        p = sheet.get_Parameter(BuiltInParameter.SHEET_CURRENT_REVISION)
        if p:
            return p.AsString() or ""
    except Exception:
        pass
    return ""


def get_titleblock_id(doc, sheet):
    """Return ElementId of the title block family type on a sheet."""
    coll = FilteredElementCollector(doc, sheet.Id)
    coll = coll.OfCategory(BuiltInCategory.OST_TitleBlocks).FirstElement()
    if coll:
        return coll.GetTypeId()
    return ElementId.InvalidElementId


def get_titleblock_instance(doc, sheet):
    """Return the title block FamilyInstance on a sheet, or None."""
    try:
        return (FilteredElementCollector(doc, sheet.Id)
                .OfCategory(BuiltInCategory.OST_TitleBlocks)
                .WhereElementIsNotElementType()
                .FirstElement())
    except Exception:
        return None


# Sheet identity / Revit-managed parameters. We never copy these between
# sheets — the destination sheet manages its own number, name, revisions, etc.
_SHEET_IDENTITY_PARAM_NAMES = set([
    "Sheet Number", "Sheet Name", "Sheet Issue Date",
    "Drawn By", "Designed By", "Approved By", "Checked By",
    "Current Revision", "Current Revision Date",
    "Current Revision Description", "Current Revision Issued",
    "Current Revision Issued By", "Current Revision Issued To",
    "File Path", "Guide Grid", "Appears In Sheet List",
])


def _copy_param_value(src_param, dst_param):
    """Copy one Revit parameter value by storage type, ignoring failures."""
    try:
        st = src_param.StorageType
        if st == StorageType.String:
            dst_param.Set(src_param.AsString() or "")
        elif st == StorageType.Integer:
            dst_param.Set(src_param.AsInteger())
        elif st == StorageType.Double:
            dst_param.Set(src_param.AsDouble())
        elif st == StorageType.ElementId:
            dst_param.Set(src_param.AsElementId())
    except Exception:
        pass


def copy_writable_params(src_element, dst_element):
    """Copy every non-readonly, non-identity instance parameter value from
    src_element to dst_element, matched by parameter name. Used to clone the
    title block's Yes/No toggles & text fields onto a duplicated sheet so the
    new title block looks like the source's."""
    if src_element is None or dst_element is None:
        return
    try:
        params = src_element.Parameters
    except Exception:
        return
    for sp in params:
        try:
            if sp.IsReadOnly:
                continue
            name = sp.Definition.Name
            if name in _SHEET_IDENTITY_PARAM_NAMES:
                continue
            dp = dst_element.LookupParameter(name)
            if dp is None or dp.IsReadOnly:
                continue
            _copy_param_value(sp, dp)
        except Exception:
            continue


def get_first_view_name(sheet):
    """Return the name of the first viewport's view on the sheet, or ''."""
    try:
        vp_ids = list(sheet.GetAllViewports())
        if not vp_ids:
            return ""
        vp = doc.GetElement(vp_ids[0])
        if not vp:
            return ""
        view = doc.GetElement(vp.ViewId)
        if not view:
            return ""
        return view.Name or ""
    except Exception:
        return ""


def get_all_view_templates(doc):
    """Return sorted list of (name, ElementId) tuples for all view templates."""
    templates = [("<None>", ElementId.InvalidElementId)]
    for v in FilteredElementCollector(doc).OfClass(View):
        if v.IsTemplate:
            templates.append((v.Name, v.Id))
    templates[1:] = sorted(templates[1:], key=lambda x: x[0])
    return templates


# Maximum number of project-parameter slots exposed on SheetItem (ProjectParam1..N).
MAX_PROJECT_PARAM_SLOTS = 8


def _is_yesno_param(definition):
    """True when a parameter definition holds a Yes/No (boolean) value."""
    # Newer Revit (2022+): SpecTypeId.Boolean.YesNo
    try:
        from Autodesk.Revit.DB import SpecTypeId
        return definition.GetDataType() == SpecTypeId.Boolean.YesNo
    except Exception:
        pass
    # Older Revit: ParameterType.YesNo
    try:
        from Autodesk.Revit.DB import ParameterType
        return definition.ParameterType == ParameterType.YesNo
    except Exception:
        return False


def get_sheet_project_params(doc):
    """Return a list of dicts describing ISSUANCE project parameters bound to
    Sheets. Filtered to parameters whose name contains 'ISSUE' (case-insensitive)
    so we surface things like 'DO NOT ISSUE', 'ISSUED FOR CONSTRUCTION',
    'ISSUED FOR DD', 'ISSUED FOR PERMIT', 'ISSUED FOR SD' — and skip the
    dozens of other project params (DISCIPLINE, SP_PP_*, etc.) that just
    crowd the grid.
       [{"name": str, "definition": Definition, "kind": "yesno" | "string" | "other"}, ...]
       Limited to MAX_PROJECT_PARAM_SLOTS entries.
    """
    result = []
    sheet_cat_id = ElementId(BuiltInCategory.OST_Sheets)
    try:
        binding_map = doc.ParameterBindings
        it = binding_map.ForwardIterator()
        it.Reset()
        while it.MoveNext():
            definition = it.Key
            # Issuance filter: name must contain "ISSUE"
            try:
                name_upper = (definition.Name or "").upper()
            except Exception:
                continue
            if "ISSUE" not in name_upper:
                continue
            binding = binding_map.get_Item(definition)
            if binding is None:
                continue
            cats = getattr(binding, "Categories", None)
            if cats is None:
                continue
            in_sheets = False
            for cat in cats:
                try:
                    if cat.Id == sheet_cat_id:
                        in_sheets = True
                        break
                except Exception:
                    continue
            if not in_sheets:
                continue
            if _is_yesno_param(definition):
                kind = "yesno"
            else:
                kind = "string"
            result.append({
                "name":       definition.Name,
                "definition": definition,
                "kind":       kind,
            })
            if len(result) >= MAX_PROJECT_PARAM_SLOTS:
                break
    except Exception:
        pass
    return result


# Populated once per session, before SheetItem instances are created.
SHEET_PROJECT_PARAMS = []


def get_all_revisions(doc):
    """Return list of revision elements sorted by sequence number."""
    revs = list(
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Revisions)
        .ToElements()
    )
    def seq(r):
        try:
            return r.get_Parameter(BuiltInParameter.PROJECT_REVISION_SEQUENCE_NUM).AsInteger()
        except Exception:
            return 0
    return sorted(revs, key=seq)


# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class SheetItem(INotifyPropertyChanged):
    """Observable wrapper around a ViewSheet for WPF binding."""

    PropertyChanged = None

    def add_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, value)

    def remove_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, value)

    def _notify(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(self, PropertyChangedEventArgs(name))

    def __init__(self, sheet, doc):
        self._sheet     = sheet
        self._doc       = doc
        self._selected  = False
        self._number    = sheet.SheetNumber
        self._name      = sheet.Name
        self._prefix    = get_prefix(sheet.SheetNumber)
        self._discipline = get_discipline(sheet)
        self._revision  = get_current_revision(sheet)
        try:
            self._view_count = len(list(sheet.GetAllViewports()))
        except Exception:
            self._view_count = 0
        # Read project-parameter values into a fixed-length slot list.
        self._params = [None] * MAX_PROJECT_PARAM_SLOTS
        for idx, info in enumerate(SHEET_PROJECT_PARAMS):
            try:
                p = sheet.LookupParameter(info["name"])
                if not p:
                    continue
                if info["kind"] == "yesno":
                    self._params[idx] = bool(p.AsInteger())
                elif p.StorageType == StorageType.String:
                    self._params[idx] = p.AsString() or ""
                else:
                    self._params[idx] = p.AsValueString() or ""
            except Exception:
                self._params[idx] = None

    def _set_project_param(self, idx, new_val):
        """Commit a project-parameter change for one slot (Yes/No or String)."""
        if idx < 0 or idx >= len(SHEET_PROJECT_PARAMS):
            return
        info = SHEET_PROJECT_PARAMS[idx]
        try:
            with Transaction(self._doc, "Sheet Manager: Set Sheet Parameter") as t:
                t.Start()
                p = self._sheet.LookupParameter(info["name"])
                if p is not None and not p.IsReadOnly:
                    if info["kind"] == "yesno":
                        p.Set(1 if new_val else 0)
                    elif p.StorageType == StorageType.String:
                        p.Set(new_val or "")
                t.Commit()
            self._params[idx] = new_val
            self._notify("ProjectParam{0}".format(idx + 1))
        except Exception as ex:
            TaskDialog.Show(
                "Set Parameter Error",
                "Could not set '{0}':\n{1}".format(info["name"], str(ex)))
            self._notify("ProjectParam{0}".format(idx + 1))

    # -- Sheet reference --
    @property
    def Sheet(self):      return self._sheet
    @property
    def ElementId(self):  return self._sheet.Id

    # -- IsSelected (two-way) --
    def _get_sel(self):   return self._selected
    def _set_sel(self, v):
        self._selected = bool(v)
        self._notify("IsSelected")
    IsSelected = property(_get_sel, _set_sel)

    # -- SheetNumber (two-way: writes to Revit in a transaction) --
    def _get_num(self): return self._number
    def _set_num(self, v):
        v = (v or "").strip()
        if not v or v == self._number:
            return
        try:
            with Transaction(self._doc, "Sheet Manager: Rename Sheet Number") as t:
                t.Start()
                self._sheet.SheetNumber = v
                t.Commit()
            self._number = v
            new_prefix = get_prefix(v)
            if new_prefix != self._prefix:
                self._prefix = new_prefix
                self._notify("Prefix")
            self._notify("SheetNumber")
        except Exception as ex:
            TaskDialog.Show(
                "Rename Error",
                "Could not change number to '{0}':\n{1}".format(v, str(ex)))
            # Force the UI back to the old value.
            self._notify("SheetNumber")
    SheetNumber = property(_get_num, _set_num)

    # -- SheetName (two-way: writes to Revit in a transaction) --
    def _get_name(self): return self._name
    def _set_name(self, v):
        v = (v or "").strip()
        if not v or v == self._name:
            return
        try:
            with Transaction(self._doc, "Sheet Manager: Rename Sheet Name") as t:
                t.Start()
                self._sheet.Name = v
                t.Commit()
            self._name = v
            self._notify("SheetName")
        except Exception as ex:
            TaskDialog.Show(
                "Rename Error",
                "Could not change name to '{0}':\n{1}".format(v, str(ex)))
            self._notify("SheetName")
    SheetName = property(_get_name, _set_name)

    # -- Read-only display properties --
    @property
    def Prefix(self):      return self._prefix
    @property
    def Discipline(self):  return self._discipline
    @property
    def Revision(self):    return self._revision
    @property
    def ViewCount(self):   return self._view_count

    # Group key used by CollectionViewSource grouping
    @property
    def GroupKey(self):    return self._prefix  # overridden when group mode changes

    # -- Eight project-parameter slots, dynamically populated based on the
    #    project's parameter bindings to the Sheets category. Columns bind by
    #    these names (ProjectParam1..ProjectParam8). Empty slots return None. --
    def _g_pp1(self): return self._params[0]
    def _s_pp1(self, v): self._set_project_param(0, v)
    ProjectParam1 = property(_g_pp1, _s_pp1)
    def _g_pp2(self): return self._params[1]
    def _s_pp2(self, v): self._set_project_param(1, v)
    ProjectParam2 = property(_g_pp2, _s_pp2)
    def _g_pp3(self): return self._params[2]
    def _s_pp3(self, v): self._set_project_param(2, v)
    ProjectParam3 = property(_g_pp3, _s_pp3)
    def _g_pp4(self): return self._params[3]
    def _s_pp4(self, v): self._set_project_param(3, v)
    ProjectParam4 = property(_g_pp4, _s_pp4)
    def _g_pp5(self): return self._params[4]
    def _s_pp5(self, v): self._set_project_param(4, v)
    ProjectParam5 = property(_g_pp5, _s_pp5)
    def _g_pp6(self): return self._params[5]
    def _s_pp6(self, v): self._set_project_param(5, v)
    ProjectParam6 = property(_g_pp6, _s_pp6)
    def _g_pp7(self): return self._params[6]
    def _s_pp7(self, v): self._set_project_param(6, v)
    ProjectParam7 = property(_g_pp7, _s_pp7)
    def _g_pp8(self): return self._params[7]
    def _s_pp8(self, v): self._set_project_param(7, v)
    ProjectParam8 = property(_g_pp8, _s_pp8)


class DuplicateRowItem(INotifyPropertyChanged):
    """One row in the Duplicate dialog DataGrid."""

    PropertyChanged = None

    def add_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, value)

    def remove_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, value)

    def _notify(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(self, PropertyChangedEventArgs(name))

    def __init__(self, sheet_item, template_names):
        self._source         = sheet_item
        # Capture source view name once so naming rules can re-derive
        # NewViewName from it whenever rules are re-applied.
        self._source_view_name = get_first_view_name(sheet_item.Sheet)
        self._new_number     = sheet_item.SheetNumber + "-DUP"
        self._new_name       = sheet_item.SheetName + " (Copy)"
        if self._source_view_name:
            self._new_view_name = self._source_view_name + " Copy"
        else:
            self._new_view_name = ""
        self._template_name  = "<None>"
        # When True, the View Name column renders muted/italic — it just
        # mirrors NewName and isn't meant to be edited by the user.
        self._mirrored       = False
        self.TemplateNames   = template_names   # List[str] for ComboBox

    def reset_to_defaults(self):
        """Restore the auto-generated -DUP / (Copy) values."""
        self.NewNumber = self._source.SheetNumber + "-DUP"
        self.NewName   = self._source.SheetName + " (Copy)"
        if self._source_view_name:
            self.NewViewName = self._source_view_name + " Copy"
        else:
            self.NewViewName = ""

    @property
    def SourceNumber(self):   return self._source.SheetNumber
    @property
    def SourceName(self):     return self._source.SheetName
    @property
    def SourceSheet(self):    return self._source.Sheet
    @property
    def SourceViewName(self): return self._source_view_name

    def _g_num(self): return self._new_number
    def _s_num(self, v):
        self._new_number = v; self._notify("NewNumber")
    NewNumber = property(_g_num, _s_num)

    def _g_nm(self): return self._new_name
    def _s_nm(self, v):
        self._new_name = v; self._notify("NewName")
    NewName = property(_g_nm, _s_nm)

    def _g_vn(self): return self._new_view_name
    def _s_vn(self, v):
        self._new_view_name = v; self._notify("NewViewName")
    NewViewName = property(_g_vn, _s_vn)

    def _g_tn(self): return self._template_name
    def _s_tn(self, v):
        self._template_name = v; self._notify("TemplateName")
    TemplateName = property(_g_tn, _s_tn)

    # Set by the dialog when "View name = Sheet name" is on. The View Name
    # column's element style watches this flag via DataTrigger to show muted /
    # italic text so the user sees the field is auto-generated.
    def _g_mirr(self): return self._mirrored
    def _s_mirr(self, v):
        self._mirrored = bool(v); self._notify("IsViewNameMirrored")
    IsViewNameMirrored = property(_g_mirr, _s_mirr)


class FindReplaceRule(INotifyPropertyChanged):
    """One row in the rules editor: find text → replace text, with case toggle.
    Used by both the Duplicate dialog (re-naming on creation) and the Rename
    dialog (find/replace mode against existing sheets)."""

    PropertyChanged = None

    def add_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, value)

    def remove_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, value)

    def _notify(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(self, PropertyChangedEventArgs(name))

    def __init__(self, find="", replace="", case_sensitive=True):
        self._find = find or ""
        self._replace = replace or ""
        self._cs = bool(case_sensitive)

    def _g_f(self): return self._find
    def _s_f(self, v):
        self._find = v if v is not None else ""
        self._notify("Find")
    Find = property(_g_f, _s_f)

    def _g_r(self): return self._replace
    def _s_r(self, v):
        self._replace = v if v is not None else ""
        self._notify("Replace")
    Replace = property(_g_r, _s_r)

    def _g_cs(self): return self._cs
    def _s_cs(self, v):
        self._cs = bool(v)
        self._notify("CaseSensitive")
    CaseSensitive = property(_g_cs, _s_cs)


def apply_rules(text, rules):
    """Apply a list of FindReplaceRule entries in order to a string.
    Empty 'find' rules are skipped. Falls back to plain replace if regex fails."""
    if text is None:
        text = ""
    out = text
    for rule in rules:
        find = rule.Find or ""
        if not find:
            continue
        replace = rule.Replace or ""
        if rule.CaseSensitive:
            out = out.replace(find, replace)
        else:
            try:
                out = re.sub(re.escape(find), replace, out, flags=re.IGNORECASE)
            except Exception:
                out = out.replace(find, replace)
    return out


# ═══════════════════════════════════════════════════════════════
# SHARED LIGHT THEME (matches AlignViews / SheetSetup)
# ═══════════════════════════════════════════════════════════════
#   Background: #F7FAFC      Header bar: #2D3748
#   Cards: White / #E2E8F0   Primary btn: #2B6CB0
#   Text: #1A202C / #4A5568  Helper: #718096
# ═══════════════════════════════════════════════════════════════

SHARED_RESOURCES = """
    <!-- Section/field/helper text -->
    <Style x:Key="SectionHeader" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="14"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#1A202C"/>
      <Setter Property="Margin"     Value="0,0,0,6"/>
    </Style>
    <Style x:Key="FieldLabel" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="11"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#4A5568"/>
      <Setter Property="Margin"     Value="0,8,0,2"/>
    </Style>
    <Style x:Key="HelperText" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="11"/>
      <Setter Property="Foreground" Value="#718096"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>

    <!-- Primary (filled blue) button -->
    <Style x:Key="PrimaryButton" TargetType="Button">
      <Setter Property="Background"      Value="#2B6CB0"/>
      <Setter Property="Foreground"      Value="White"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
      <Setter Property="Padding"         Value="14,6"/>
      <Setter Property="MinWidth"        Value="100"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="Margin"          Value="6,0,0,0"/>
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
                <Setter Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- Secondary (outlined) button -->
    <Style x:Key="SecondaryButton" TargetType="Button">
      <Setter Property="Background"      Value="#EDF2F7"/>
      <Setter Property="Foreground"      Value="#2D3748"/>
      <Setter Property="Padding"         Value="12,6"/>
      <Setter Property="MinWidth"        Value="100"/>
      <Setter Property="BorderBrush"     Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="Margin"          Value="6,0,0,0"/>
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
                <Setter Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- Danger button (used for Delete) -->
    <Style x:Key="DangerButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
      <Setter Property="Foreground"  Value="#C53030"/>
      <Setter Property="BorderBrush" Value="#FEB2B2"/>
    </Style>

    <!-- TextBox -->
    <Style TargetType="TextBox">
      <Setter Property="Background"       Value="White"/>
      <Setter Property="Foreground"       Value="#1A202C"/>
      <Setter Property="BorderBrush"      Value="#CBD5E0"/>
      <Setter Property="BorderThickness"  Value="1"/>
      <Setter Property="Padding"          Value="6,4"/>
      <Setter Property="Height"           Value="28"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"       Value="#2B6CB0"/>
    </Style>

    <!-- ComboBox - dark text on white, dropdown items styled below -->
    <Style TargetType="ComboBox">
      <Setter Property="Background"      Value="White"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="BorderBrush"     Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="6,4"/>
      <Setter Property="Height"          Value="28"/>
      <Setter Property="FontSize"        Value="12"/>
    </Style>
    <Style TargetType="ComboBoxItem">
      <Setter Property="Background"  Value="White"/>
      <Setter Property="Foreground"  Value="#1A202C"/>
      <Setter Property="Padding"     Value="6,4"/>
      <Setter Property="FontSize"    Value="12"/>
    </Style>

    <!-- CheckBox / RadioButton text -->
    <Style TargetType="CheckBox">
      <Setter Property="Foreground"  Value="#1A202C"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Margin"      Value="0,3,0,3"/>
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
                         HorizontalAlignment="Center" VerticalAlignment="Center"
                         Stroke="#A0AEC0" StrokeThickness="1.5" Fill="White"/>
                <Ellipse x:Name="dot" Width="7" Height="7"
                         HorizontalAlignment="Center" VerticalAlignment="Center"
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
"""


# ═══════════════════════════════════════════════════════════════
# MAIN WINDOW XAML
# ═══════════════════════════════════════════════════════════════

MAIN_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Sheet Manager" Width="1000" Height="680"
        WindowStartupLocation="CenterScreen"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """

    <!-- DataGrid -->
    <Style TargetType="DataGrid">
      <Setter Property="Background"               Value="White"/>
      <Setter Property="Foreground"               Value="#1A202C"/>
      <Setter Property="BorderBrush"              Value="#E2E8F0"/>
      <Setter Property="BorderThickness"          Value="1"/>
      <Setter Property="RowBackground"            Value="White"/>
      <Setter Property="AlternatingRowBackground" Value="#F7FAFC"/>
      <Setter Property="GridLinesVisibility"      Value="None"/>
      <Setter Property="HeadersVisibility"        Value="Column"/>
      <Setter Property="SelectionMode"            Value="Extended"/>
      <Setter Property="SelectionUnit"            Value="FullRow"/>
      <Setter Property="CanUserAddRows"           Value="False"/>
      <Setter Property="CanUserDeleteRows"        Value="False"/>
      <Setter Property="AutoGenerateColumns"      Value="False"/>
      <Setter Property="CanUserResizeRows"        Value="False"/>
      <Setter Property="RowHeight"                Value="30"/>
    </Style>
    <Style TargetType="DataGridColumnHeader">
      <Setter Property="Background"        Value="#EDF2F7"/>
      <Setter Property="Foreground"        Value="#2D3748"/>
      <Setter Property="FontSize"          Value="11"/>
      <Setter Property="FontWeight"        Value="SemiBold"/>
      <Setter Property="Padding"           Value="10,8"/>
      <Setter Property="BorderBrush"       Value="#E2E8F0"/>
      <Setter Property="BorderThickness"   Value="0,0,1,1"/>
      <Setter Property="SeparatorVisibility" Value="Visible"/>
      <Setter Property="SeparatorBrush"    Value="#E2E8F0"/>
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
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
      <Style.Triggers>
        <Trigger Property="IsSelected" Value="True">
          <Setter Property="Background" Value="Transparent"/>
          <Setter Property="Foreground" Value="#1A202C"/>
        </Trigger>
      </Style.Triggers>
    </Style>

    <!-- Centered checkbox style for the row's IsSelected column -->
    <Style x:Key="RowCheck" TargetType="CheckBox">
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment"   Value="Center"/>
      <Setter Property="Margin" Value="0"/>
    </Style>

    <!-- Inline edit style for Sheet # / Sheet Name (white box, dark text). -->
    <Style x:Key="MainEditCell" TargetType="TextBox">
      <Setter Property="Background"        Value="White"/>
      <Setter Property="Foreground"        Value="#1A202C"/>
      <Setter Property="BorderBrush"       Value="#2B6CB0"/>
      <Setter Property="BorderThickness"   Value="1"/>
      <Setter Property="Padding"           Value="8,2"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"        Value="#2B6CB0"/>
      <Setter Property="SelectionBrush"    Value="#BEE3F8"/>
    </Style>

    <!-- Chip toggle: rounded pill that lights up blue when active. Used for
         the prefix and series filter buttons. -->
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

    <!-- Group header: contains a CheckBox (group selector) + an Expander toggle -->
    <Style x:Key="GroupHdr" TargetType="GroupItem">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="GroupItem">
            <Expander IsExpanded="True">
              <Expander.Style>
                <Style TargetType="Expander">
                  <Setter Property="Background"     Value="#EDF2F7"/>
                  <Setter Property="BorderBrush"    Value="#E2E8F0"/>
                  <Setter Property="BorderThickness" Value="0,0,0,1"/>
                  <Setter Property="Foreground"     Value="#2D3748"/>
                  <Setter Property="Template">
                    <Setter.Value>
                      <ControlTemplate TargetType="Expander">
                        <Border Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}"
                                BorderThickness="{TemplateBinding BorderThickness}">
                          <StackPanel>
                            <Grid>
                              <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="Auto"/>
                                <ColumnDefinition Width="*"/>
                              </Grid.ColumnDefinitions>
                              <!-- Group selector checkbox -->
                              <CheckBox Grid.Column="0" Tag="GroupSelector"
                                        Margin="14,0,4,0" VerticalAlignment="Center"
                                        Cursor="Hand" Focusable="False"
                                        ToolTip="Select / deselect all sheets in this group"/>
                              <!-- Clickable expander header -->
                              <ToggleButton Grid.Column="1" x:Name="Hdr"
                                            IsChecked="{Binding IsExpanded, RelativeSource={RelativeSource TemplatedParent}}"
                                            Background="Transparent" BorderThickness="0"
                                            Cursor="Hand" Padding="6,8"
                                            HorizontalContentAlignment="Left">
                                <ToggleButton.Template>
                                  <ControlTemplate TargetType="ToggleButton">
                                    <Border Background="{TemplateBinding Background}"
                                            BorderThickness="{TemplateBinding BorderThickness}"
                                            Padding="{TemplateBinding Padding}">
                                      <ContentPresenter/>
                                    </Border>
                                  </ControlTemplate>
                                </ToggleButton.Template>
                                <StackPanel Orientation="Horizontal">
                                  <TextBlock x:Name="arrow" Text="▾ " Foreground="#2B6CB0" FontSize="11"/>
                                  <TextBlock Text="{Binding Name}" Foreground="#2D3748"
                                             FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                  <TextBlock Foreground="#718096" FontSize="11" VerticalAlignment="Center">
                                    <Run Text="  "/>
                                    <Run Text="{Binding ItemCount, Mode=OneWay}"/>
                                    <Run Text=" sheets"/>
                                  </TextBlock>
                                </StackPanel>
                              </ToggleButton>
                            </Grid>
                            <ContentPresenter x:Name="body"/>
                          </StackPanel>
                        </Border>
                        <ControlTemplate.Triggers>
                          <Trigger Property="IsExpanded" Value="False">
                            <Setter TargetName="body" Property="Visibility" Value="Collapsed"/>
                            <Setter TargetName="arrow" Property="Text" Value="▸ "/>
                          </Trigger>
                        </ControlTemplate.Triggers>
                      </ControlTemplate>
                    </Setter.Value>
                  </Setter>
                </Style>
              </Expander.Style>
              <ItemsPresenter/>
            </Expander>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>  <!-- 0: header -->
      <RowDefinition Height="Auto"/>  <!-- 1: toolbar -->
      <RowDefinition Height="Auto"/>  <!-- 2: filter pane (collapsible) -->
      <RowDefinition Height="*"/>     <!-- 3: grid -->
      <RowDefinition Height="Auto"/>  <!-- 4: status -->
    </Grid.RowDefinitions>

    <!-- HEADER (dark blue bar) -->
    <Border Grid.Row="0" Background="#2D3748" Padding="20,14">
      <Grid>
        <StackPanel Orientation="Vertical" HorizontalAlignment="Left">
          <TextBlock Text="Sheet Manager" Foreground="White"
                     FontSize="20" FontWeight="Bold"/>
          <TextBlock x:Name="lbl_project"
                     Text="Group, rename, renumber, duplicate with views, and manage revisions."
                     Foreground="#CBD5E0" FontSize="12" Margin="0,2,0,0"/>
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

    <!-- TOOLBAR -->
    <Border Grid.Row="1" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,0,0,1"
            Padding="16,10">
      <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
        <!-- Search -->
        <Grid Width="220" Margin="0,0,8,0">
          <TextBox x:Name="txt_search" Height="30" FontSize="12"
                   VerticalContentAlignment="Center" Padding="8,0"/>
          <TextBlock x:Name="ph_search" Text="🔍  Search sheets…"
                     Foreground="#A0AEC0" FontSize="12"
                     IsHitTestVisible="False"
                     VerticalAlignment="Center" Margin="10,0"/>
        </Grid>
        <!-- Filter toggle (reveals the filter pane below) -->
        <ToggleButton x:Name="btn_filter"
                      Height="30" Padding="12,4" Margin="0,0,12,0"
                      Cursor="Hand" FontSize="12"
                      Background="#EDF2F7" Foreground="#2D3748"
                      BorderBrush="#CBD5E0" BorderThickness="1"
                      ToolTip="Show / hide the filter pane">
          <ToggleButton.Content>
            <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
              <TextBlock Text="▽" Foreground="#718096" FontSize="13"
                         Margin="0,0,6,0" VerticalAlignment="Center"/>
              <TextBlock Text="Filter" VerticalAlignment="Center"/>
            </StackPanel>
          </ToggleButton.Content>
          <ToggleButton.Template>
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
              </ControlTemplate.Triggers>
            </ControlTemplate>
          </ToggleButton.Template>
        </ToggleButton>
        <!-- Group by -->
        <TextBlock Text="Group:" Foreground="#4A5568" FontSize="12"
                   VerticalAlignment="Center" Margin="0,0,6,0"/>
        <ComboBox x:Name="cmb_group" Width="125" Height="30" SelectedIndex="0">
          <ComboBoxItem Content="Prefix"/>
          <ComboBoxItem Content="Discipline"/>
          <ComboBoxItem Content="All Flat"/>
        </ComboBox>
        <Border Width="1" Background="#E2E8F0" Margin="14,4"/>
        <!-- Action buttons -->
        <Button x:Name="btn_open"      Content="↗ Open"      Style="{StaticResource SecondaryButton}" Height="30" IsEnabled="False" ToolTip="Open selected sheet"/>
        <Button x:Name="btn_rename"    Content="✎ Rename"    Style="{StaticResource SecondaryButton}" Height="30" IsEnabled="False" ToolTip="Rename / renumber selected sheets"/>
        <Button x:Name="btn_duplicate" Content="⎘ Duplicate" Style="{StaticResource SecondaryButton}" Height="30" IsEnabled="False" ToolTip="Duplicate selected sheets with views"/>
        <Button x:Name="btn_revisions" Content="⏱ Revisions" Style="{StaticResource SecondaryButton}" Height="30" IsEnabled="False" ToolTip="Add or remove revisions on selected sheets"/>
        <Button x:Name="btn_delete"    Content="🗑 Delete"   Style="{StaticResource DangerButton}"    Height="30" IsEnabled="False" ToolTip="Delete selected sheets"/>
      </StackPanel>
    </Border>

    <!-- FILTER PANE (collapsible). Populated dynamically. -->
    <Border x:Name="filter_panel" Grid.Row="2"
            Background="#F7FAFC" BorderBrush="#E2E8F0" BorderThickness="0,0,0,1"
            Padding="16,10" Visibility="Collapsed">
      <StackPanel>
        <Grid Margin="0,0,0,4">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="80"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Text="Prefix:" FontWeight="SemiBold" Foreground="#4A5568"
                     FontSize="11" VerticalAlignment="Center"/>
          <WrapPanel x:Name="pnl_filter_prefixes" Grid.Column="1"/>
        </Grid>
        <Grid Margin="0,0,0,4">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="80"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Text="Series:" FontWeight="SemiBold" Foreground="#4A5568"
                     FontSize="11" VerticalAlignment="Center"/>
          <WrapPanel x:Name="pnl_filter_series" Grid.Column="1"/>
        </Grid>
        <Grid x:Name="pnl_filter_params_outer" Margin="0,0,0,4" Visibility="Collapsed">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="80"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Text="Parameters:" FontWeight="SemiBold" Foreground="#4A5568"
                     FontSize="11" VerticalAlignment="Center"/>
          <WrapPanel x:Name="pnl_filter_params" Grid.Column="1"/>
        </Grid>
        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,4,0,0">
          <TextBlock x:Name="lbl_filter_summary" Foreground="#718096" FontSize="11"
                     VerticalAlignment="Center" Margin="0,0,12,0"/>
          <Button x:Name="btn_filter_reset" Content="Reset Filters"
                  Style="{StaticResource SecondaryButton}" Height="30" MinWidth="110"/>
        </StackPanel>
      </StackPanel>
    </Border>

    <!-- MAIN — DataGrid -->
    <Border Grid.Row="3" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0"
            Padding="16,12">
      <DataGrid x:Name="sheet_grid"
                VirtualizingPanel.IsVirtualizingWhenGrouping="True"
                EnableRowVirtualization="True"
                ScrollViewer.VerticalScrollBarVisibility="Auto"
                ScrollViewer.HorizontalScrollBarVisibility="Auto">
        <DataGrid.GroupStyle>
          <GroupStyle ContainerStyle="{StaticResource GroupHdr}"/>
        </DataGrid.GroupStyle>
        <DataGrid.Columns>
          <!-- Real CheckBox in a template column → toggles on first click
               (DataGridCheckBoxColumn requires two clicks: enter edit, then toggle). -->
          <DataGridTemplateColumn Header="" Width="40" MinWidth="40" CanUserResize="False">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <CheckBox IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                          Style="{StaticResource RowCheck}"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <!-- Sheet # — click again on a selected row to edit. Commits via
               the SheetNumber setter (Revit transaction), then row re-sorts. -->
          <DataGridTextColumn Header="Sheet #"
                              Binding="{Binding SheetNumber, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                              Width="110" MinWidth="100"
                              EditingElementStyle="{StaticResource MainEditCell}">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="Padding"           Value="10,0"/>
                <Setter Property="VerticalAlignment" Value="Center"/>
                <Setter Property="Foreground"        Value="#2B6CB0"/>
                <Setter Property="FontWeight"        Value="SemiBold"/>
                <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
          <!-- Sheet Name — click again on a selected row to edit.
               MinWidth keeps it visible even when many param columns are present. -->
          <DataGridTextColumn Header="Sheet Name"
                              Binding="{Binding SheetName, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                              Width="*" MinWidth="240"
                              EditingElementStyle="{StaticResource MainEditCell}">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="Padding"           Value="10,0"/>
                <Setter Property="VerticalAlignment" Value="Center"/>
                <Setter Property="Foreground"        Value="#1A202C"/>
                <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
          <DataGridTextColumn Header="Views" Binding="{Binding ViewCount}"
                              Width="60" IsReadOnly="True">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="Padding"           Value="10,0"/>
                <Setter Property="VerticalAlignment" Value="Center"/>
                <Setter Property="Foreground"        Value="#718096"/>
                <Setter Property="TextAlignment"     Value="Center"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
          <DataGridTextColumn Header="Rev" Binding="{Binding Revision}"
                              Width="60" IsReadOnly="True">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="Padding"           Value="10,0"/>
                <Setter Property="VerticalAlignment" Value="Center"/>
                <Setter Property="Foreground"        Value="#718096"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
        </DataGrid.Columns>
      </DataGrid>
    </Border>

    <!-- STATUS BAR -->
    <Border Grid.Row="4" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0"
            Padding="16,8">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock x:Name="lbl_status" Foreground="#4A5568"
                   FontSize="12" VerticalAlignment="Center"/>
        <Button x:Name="btn_sel_all"   Grid.Column="1" Content="Select All"
                Style="{StaticResource SecondaryButton}" Height="28" MinWidth="100"/>
        <Button x:Name="btn_sel_none"  Grid.Column="2" Content="Select None"
                Style="{StaticResource SecondaryButton}" Height="28" MinWidth="100"/>
      </Grid>
    </Border>
  </Grid>
</Window>
"""

# ── Rename / Renumber dialog ──────────────────────────────────
RENAME_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Rename / Renumber Sheets" Width="720" Height="680"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """
    <Style x:Key="CardBorder" TargetType="Border">
      <Setter Property="Background"      Value="White"/>
      <Setter Property="BorderBrush"     Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius"    Value="4"/>
      <Setter Property="Padding"         Value="14"/>
    </Style>
  </Window.Resources>

  <DockPanel>
    <!-- Header bar -->
    <Border DockPanel.Dock="Top" Background="#2D3748" Padding="20,12">
      <Grid>
        <StackPanel HorizontalAlignment="Left">
          <TextBlock Text="Rename / Renumber Sheets" Foreground="White"
                     FontSize="18" FontWeight="Bold"/>
          <TextBlock Text="Edit a single sheet, batch-renumber a sequence, or run find/replace rules across the selection."
                     Foreground="#CBD5E0" FontSize="11" Margin="0,2,0,0"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- Footer -->
    <Border DockPanel.Dock="Bottom" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="20,12">
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <Button x:Name="btn_cancel" Content="Cancel" Style="{StaticResource SecondaryButton}"/>
        <Button x:Name="btn_apply"  Content="Apply"  Style="{StaticResource PrimaryButton}"/>
      </StackPanel>
    </Border>

    <Grid Margin="20,16">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
      </Grid.RowDefinitions>

      <!-- Mode tabs -->
      <StackPanel Grid.Row="0" Orientation="Horizontal" Margin="0,0,0,14">
        <RadioButton x:Name="rb_single"   Content="Single Rename"  IsChecked="True"
                     Margin="0,0,20,0" Cursor="Hand"/>
        <RadioButton x:Name="rb_batch"    Content="Batch Renumber"
                     Margin="0,0,20,0" Cursor="Hand"/>
        <RadioButton x:Name="rb_findrep"  Content="Find / Replace" Cursor="Hand"/>
      </StackPanel>

      <!-- Single rename panel -->
      <Border x:Name="pnl_single" Grid.Row="1" Style="{StaticResource CardBorder}" Margin="0,0,0,12">
        <StackPanel>
          <TextBlock Text="Edit the selected sheet's number and name."
                     Style="{StaticResource HelperText}" Margin="0,0,0,10"/>
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="120"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="8"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <TextBlock Text="Sheet Number:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_new_number" Grid.Column="1" Height="30"/>
            <TextBlock Grid.Row="2" Text="Sheet Name:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_new_name" Grid.Row="2" Grid.Column="1" Height="30"/>
          </Grid>
        </StackPanel>
      </Border>

      <!-- Batch renumber panel -->
      <Border x:Name="pnl_batch" Grid.Row="1" Style="{StaticResource CardBorder}"
              Visibility="Collapsed" Margin="0,0,0,12">
        <StackPanel>
          <TextBlock Text="Renumbers all selected sheets in order by sheet number."
                     Style="{StaticResource HelperText}" Margin="0,0,0,10"/>
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="120"/>
              <ColumnDefinition Width="100"/>
              <ColumnDefinition Width="20"/>
              <ColumnDefinition Width="120"/>
              <ColumnDefinition Width="100"/>
            </Grid.ColumnDefinitions>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="8"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="8"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <TextBlock Text="Prefix:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_prefix" Grid.Column="1" Height="30"/>
            <TextBlock Grid.Column="3" Text="Separator:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_sep" Grid.Column="4" Height="30"/>
            <TextBlock Grid.Row="2" Text="Start #:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_start" Grid.Row="2" Grid.Column="1" Height="30" Text="100"/>
            <TextBlock Grid.Row="2" Grid.Column="3" Text="Increment:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_inc" Grid.Row="2" Grid.Column="4" Height="30" Text="1"/>
            <TextBlock Grid.Row="4" Text="Suffix:" Foreground="#4A5568" FontSize="12" VerticalAlignment="Center"/>
            <TextBox x:Name="txt_suffix" Grid.Row="4" Grid.Column="1" Height="30"/>
            <Button x:Name="btn_preview" Grid.Row="4" Grid.Column="3" Grid.ColumnSpan="2"
                    Content="Preview Changes" Style="{StaticResource SecondaryButton}" Height="30"/>
          </Grid>
        </StackPanel>
      </Border>

      <!-- Find / Replace panel -->
      <Border x:Name="pnl_findrep" Grid.Row="1" Style="{StaticResource CardBorder}"
              Visibility="Collapsed" Margin="0,0,0,12">
        <StackPanel>
          <TextBlock Text="Apply find/replace rules to every selected sheet's number and/or name."
                     Style="{StaticResource HelperText}" Margin="0,0,0,8"/>
          <StackPanel Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="Apply to:" Foreground="#4A5568" FontSize="12"
                       VerticalAlignment="Center" Margin="0,0,10,0"/>
            <CheckBox x:Name="chk_fr_apply_num"  Content="Sheet #"    IsChecked="True" Margin="0,0,16,0"/>
            <CheckBox x:Name="chk_fr_apply_name" Content="Sheet Name" IsChecked="True"/>
          </StackPanel>
          <ItemsControl x:Name="fr_rules_list" Margin="0,4,0,4">
            <ItemsControl.ItemTemplate>
              <DataTemplate>
                <Grid Margin="0,2">
                  <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="22"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="14"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="14"/>
                    <ColumnDefinition Width="Auto"/>
                  </Grid.ColumnDefinitions>
                  <TextBox Grid.Column="0" Height="26"
                           Text="{Binding Find, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
                  <TextBlock Grid.Column="1" Text="→" Foreground="#718096"
                             FontSize="14" VerticalAlignment="Center"
                             HorizontalAlignment="Center"/>
                  <TextBox Grid.Column="2" Height="26"
                           Text="{Binding Replace, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
                  <CheckBox Grid.Column="4" Content="Aa"
                            IsChecked="{Binding CaseSensitive, Mode=TwoWay}"
                            ToolTip="Case-sensitive match"
                            VerticalAlignment="Center" Margin="0"/>
                  <Button Grid.Column="6" Tag="RemoveRule" Content="✕"
                          Width="26" Height="26" MinWidth="26"
                          Style="{StaticResource SecondaryButton}"
                          Margin="0" Padding="0" FontSize="11"
                          ToolTip="Remove rule"/>
                </Grid>
              </DataTemplate>
            </ItemsControl.ItemTemplate>
          </ItemsControl>
          <Grid Margin="0,6,0,0">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <Button Grid.Column="0" x:Name="btn_fr_add_rule" Content="+ Add rule"
                    Style="{StaticResource SecondaryButton}" Height="30" MinWidth="100"
                    Margin="0"/>
            <Button Grid.Column="2" x:Name="btn_fr_preview" Content="Preview Changes"
                    Style="{StaticResource SecondaryButton}" Height="30" MinWidth="140"/>
          </Grid>
        </StackPanel>
      </Border>

      <!-- Preview list -->
      <Border Grid.Row="2" Style="{StaticResource CardBorder}">
        <ScrollViewer>
          <ListBox x:Name="lst_preview" Background="Transparent" BorderThickness="0"
                   Foreground="#1A202C" FontSize="12"/>
        </ScrollViewer>
      </Border>
    </Grid>
  </DockPanel>
</Window>
"""

# ── Duplicate dialog ──────────────────────────────────────────
DUPLICATE_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Duplicate Sheets with Views" Width="960" Height="780"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """
    <Style x:Key="CardBorder" TargetType="Border">
      <Setter Property="Background"      Value="White"/>
      <Setter Property="BorderBrush"     Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius"    Value="4"/>
    </Style>
    <Style TargetType="DataGrid">
      <Setter Property="Background"               Value="White"/>
      <Setter Property="Foreground"               Value="#1A202C"/>
      <Setter Property="BorderThickness"          Value="0"/>
      <Setter Property="RowBackground"            Value="White"/>
      <Setter Property="AlternatingRowBackground" Value="#F7FAFC"/>
      <Setter Property="GridLinesVisibility"      Value="Horizontal"/>
      <Setter Property="HorizontalGridLinesBrush" Value="#EDF2F7"/>
      <Setter Property="HeadersVisibility"        Value="Column"/>
      <Setter Property="CanUserAddRows"           Value="False"/>
      <Setter Property="CanUserDeleteRows"        Value="False"/>
      <Setter Property="AutoGenerateColumns"      Value="False"/>
      <Setter Property="RowHeight"                Value="32"/>
    </Style>
    <Style TargetType="DataGridColumnHeader">
      <Setter Property="Background"      Value="#EDF2F7"/>
      <Setter Property="Foreground"      Value="#2D3748"/>
      <Setter Property="FontSize"        Value="11"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
      <Setter Property="Padding"         Value="8,7"/>
      <Setter Property="BorderBrush"     Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="0,0,1,1"/>
    </Style>
    <Style TargetType="DataGridRow">
      <Setter Property="Foreground" Value="#1A202C"/>
      <Style.Triggers>
        <Trigger Property="IsSelected" Value="True">
          <!-- Soft pale blue for selected rows so edit text stays readable -->
          <Setter Property="Background" Value="#EBF8FF"/>
          <Setter Property="Foreground" Value="#1A202C"/>
        </Trigger>
        <Trigger Property="IsMouseOver" Value="True">
          <Setter Property="Background" Value="#F7FAFC"/>
        </Trigger>
      </Style.Triggers>
    </Style>
    <Style TargetType="DataGridCell">
      <Setter Property="BorderThickness"   Value="0"/>
      <Setter Property="Foreground"        Value="#1A202C"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="FocusVisualStyle"  Value="{x:Null}"/>
      <Style.Triggers>
        <!-- Don't paint cell with the system blue when selected — let the
             row's pale-blue show through instead. -->
        <Trigger Property="IsSelected" Value="True">
          <Setter Property="Background" Value="Transparent"/>
          <Setter Property="Foreground" Value="#1A202C"/>
        </Trigger>
      </Style.Triggers>
    </Style>

    <!-- Editing TextBox style: white box with blue border, dark legible text. -->
    <Style x:Key="DupEditCell" TargetType="TextBox">
      <Setter Property="Background"        Value="White"/>
      <Setter Property="Foreground"        Value="#1A202C"/>
      <Setter Property="BorderBrush"       Value="#2B6CB0"/>
      <Setter Property="BorderThickness"   Value="1"/>
      <Setter Property="Padding"           Value="6,2"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"        Value="#2B6CB0"/>
      <Setter Property="SelectionBrush"    Value="#BEE3F8"/>
    </Style>

    <!-- Read-only "Source" cells: faint gray tint to set them apart. -->
    <Style x:Key="SourceCell" TargetType="DataGridCell" BasedOn="{StaticResource {x:Type DataGridCell}}">
      <Setter Property="Background" Value="#EDF2F7"/>
    </Style>
    <Style x:Key="SourceHeader" TargetType="DataGridColumnHeader" BasedOn="{StaticResource {x:Type DataGridColumnHeader}}">
      <Setter Property="Background"      Value="#CBD5E0"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
    </Style>
    <!-- Editable "New" cells: white background; the first one gets a strong
         left border to act as a visual divider between Source and New blocks. -->
    <Style x:Key="NewCell" TargetType="DataGridCell" BasedOn="{StaticResource {x:Type DataGridCell}}">
      <Setter Property="Background" Value="White"/>
    </Style>
    <Style x:Key="NewFirstCell" TargetType="DataGridCell" BasedOn="{StaticResource {x:Type DataGridCell}}">
      <Setter Property="Background"      Value="White"/>
      <Setter Property="BorderBrush"     Value="#2B6CB0"/>
      <Setter Property="BorderThickness" Value="2,0,0,0"/>
    </Style>
    <Style x:Key="NewHeader" TargetType="DataGridColumnHeader" BasedOn="{StaticResource {x:Type DataGridColumnHeader}}">
      <Setter Property="Background"      Value="#BEE3F8"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
    </Style>
    <Style x:Key="NewFirstHeader" TargetType="DataGridColumnHeader" BasedOn="{StaticResource {x:Type DataGridColumnHeader}}">
      <Setter Property="Background"      Value="#BEE3F8"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="BorderBrush"     Value="#2B6CB0"/>
      <Setter Property="BorderThickness" Value="2,0,1,1"/>
    </Style>
  </Window.Resources>

  <DockPanel>
    <!-- Header -->
    <Border DockPanel.Dock="Top" Background="#2D3748" Padding="20,12">
      <Grid>
        <StackPanel HorizontalAlignment="Left">
          <TextBlock Text="Duplicate Sheets with Views" Foreground="White"
                     FontSize="18" FontWeight="Bold"/>
          <TextBlock Text="Edit number, name, view name, and template for each sheet to duplicate."
                     Foreground="#CBD5E0" FontSize="11" Margin="0,2,0,0"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- Footer -->
    <Border DockPanel.Dock="Bottom" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="20,12">
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <Button x:Name="btn_cancel"    Content="Cancel"    Style="{StaticResource SecondaryButton}"/>
        <Button x:Name="btn_duplicate" Content="Duplicate" Style="{StaticResource PrimaryButton}"/>
      </StackPanel>
    </Border>

    <Grid Margin="20,16">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>  <!-- 0: naming rules card -->
        <RowDefinition Height="*"/>     <!-- 1: table -->
        <RowDefinition Height="Auto"/>  <!-- 2: options bar -->
      </Grid.RowDefinitions>

      <!-- Naming rules card -->
      <Border Grid.Row="0" Style="{StaticResource CardBorder}" Margin="0,0,0,12" Padding="14">
        <StackPanel>
          <TextBlock Text="Naming rules" Style="{StaticResource SectionHeader}"/>
          <TextBlock Text="Add find/replace pairs and apply them to the new sheet number, name, and view name. Rules transform the source sheet's values."
                     Style="{StaticResource HelperText}" Margin="0,0,0,8"/>

          <!-- Apply-to checkboxes -->
          <StackPanel Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="Apply to:" Foreground="#4A5568" FontSize="12"
                       VerticalAlignment="Center" Margin="0,0,10,0"/>
            <CheckBox x:Name="chk_apply_num"  Content="Sheet #"    IsChecked="True" Margin="0,0,16,0"/>
            <CheckBox x:Name="chk_apply_name" Content="Sheet Name" IsChecked="True" Margin="0,0,16,0"/>
            <CheckBox x:Name="chk_apply_view" Content="View Name"  IsChecked="True"/>
          </StackPanel>

          <!-- Rules list -->
          <ItemsControl x:Name="rules_list" Margin="0,4,0,4">
            <ItemsControl.ItemTemplate>
              <DataTemplate>
                <Grid Margin="0,2">
                  <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="22"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="14"/>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="14"/>
                    <ColumnDefinition Width="Auto"/>
                  </Grid.ColumnDefinitions>
                  <TextBox Grid.Column="0" Height="26"
                           Text="{Binding Find, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
                  <TextBlock Grid.Column="1" Text="→" Foreground="#718096"
                             FontSize="14" VerticalAlignment="Center"
                             HorizontalAlignment="Center"/>
                  <TextBox Grid.Column="2" Height="26"
                           Text="{Binding Replace, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
                  <CheckBox Grid.Column="4" Content="Aa"
                            IsChecked="{Binding CaseSensitive, Mode=TwoWay}"
                            ToolTip="Case-sensitive match"
                            VerticalAlignment="Center" Margin="0"/>
                  <Button Grid.Column="6" Tag="RemoveRule" Content="✕"
                          Width="26" Height="26" MinWidth="26"
                          Style="{StaticResource SecondaryButton}"
                          Margin="0" Padding="0" FontSize="11"
                          ToolTip="Remove rule"/>
                </Grid>
              </DataTemplate>
            </ItemsControl.ItemTemplate>
          </ItemsControl>

          <!-- Rule actions -->
          <Grid Margin="0,6,0,0">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <Button Grid.Column="0" x:Name="btn_add_rule" Content="+ Add rule"
                    Style="{StaticResource SecondaryButton}" Height="30" MinWidth="100"
                    Margin="0"/>
            <TextBlock Grid.Column="1" x:Name="lbl_rules_hint" Text=""
                       Foreground="#A0AEC0" FontSize="11"
                       VerticalAlignment="Center" Margin="14,0,0,0"/>
            <Button Grid.Column="2" x:Name="btn_reset_rules" Content="Reset Names"
                    Style="{StaticResource SecondaryButton}" Height="30" MinWidth="100"
                    ToolTip="Restore the auto-generated -DUP / (Copy) names."/>
            <Button Grid.Column="3" x:Name="btn_apply_rules" Content="Apply Rules"
                    Style="{StaticResource PrimaryButton}" Height="30" MinWidth="120"/>
          </Grid>
        </StackPanel>
      </Border>

      <!-- Table -->
      <Border Grid.Row="1" Style="{StaticResource CardBorder}">
        <DataGrid x:Name="dup_grid" Margin="0"
                  SelectionMode="Extended" SelectionUnit="FullRow">
          <DataGrid.Columns>
            <!-- Numeric columns stay fixed; the four text columns are star-sized
                 so they share extra space proportionally when the window grows. -->
            <!-- ─── FROM (read-only source columns, gray tint) ─── -->
            <DataGridTextColumn Header="From: #"
                                Binding="{Binding SourceNumber}"
                                Width="90" MinWidth="80" IsReadOnly="True"
                                CellStyle="{StaticResource SourceCell}"
                                HeaderStyle="{StaticResource SourceHeader}">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="Padding"           Value="8,0"/>
                  <Setter Property="VerticalAlignment" Value="Center"/>
                  <Setter Property="Foreground"        Value="#2B6CB0"/>
                  <Setter Property="FontWeight"        Value="SemiBold"/>
                  <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
            <DataGridTextColumn Header="From: Name"
                                Binding="{Binding SourceName}"
                                Width="1.5*" MinWidth="120" IsReadOnly="True"
                                CellStyle="{StaticResource SourceCell}"
                                HeaderStyle="{StaticResource SourceHeader}">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="Padding"           Value="8,0"/>
                  <Setter Property="VerticalAlignment" Value="Center"/>
                  <Setter Property="Foreground"        Value="#4A5568"/>
                  <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
            <!-- ─── TO (editable new columns, white + blue header) ─── -->
            <DataGridTextColumn Header="To: New #"
                                Binding="{Binding NewNumber, UpdateSourceTrigger=PropertyChanged}"
                                Width="110" MinWidth="100"
                                CellStyle="{StaticResource NewFirstCell}"
                                HeaderStyle="{StaticResource NewFirstHeader}"
                                EditingElementStyle="{StaticResource DupEditCell}">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="Padding"           Value="8,0"/>
                  <Setter Property="VerticalAlignment" Value="Center"/>
                  <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
            <DataGridTextColumn Header="To: New Name"
                                Binding="{Binding NewName, UpdateSourceTrigger=PropertyChanged}"
                                Width="2*" MinWidth="140"
                                CellStyle="{StaticResource NewCell}"
                                HeaderStyle="{StaticResource NewHeader}"
                                EditingElementStyle="{StaticResource DupEditCell}">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="Padding"           Value="8,0"/>
                  <Setter Property="VerticalAlignment" Value="Center"/>
                  <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
            <DataGridTextColumn Header="To: New View Name"
                                Binding="{Binding NewViewName, UpdateSourceTrigger=PropertyChanged}"
                                Width="2*" MinWidth="140"
                                CellStyle="{StaticResource NewCell}"
                                HeaderStyle="{StaticResource NewHeader}"
                                EditingElementStyle="{StaticResource DupEditCell}">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="Padding"           Value="8,0"/>
                  <Setter Property="VerticalAlignment" Value="Center"/>
                  <Setter Property="Foreground"        Value="#1A202C"/>
                  <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                  <Style.Triggers>
                    <!-- When "View name = Sheet name" is on, the dialog flips
                         IsViewNameMirrored on every row; show muted italic text
                         so the user sees this column is auto-derived. -->
                    <DataTrigger Binding="{Binding IsViewNameMirrored}" Value="True">
                      <Setter Property="Foreground" Value="#A0AEC0"/>
                      <Setter Property="FontStyle"  Value="Italic"/>
                    </DataTrigger>
                  </Style.Triggers>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
            <DataGridTemplateColumn Header="To: View Template"
                                    Width="1.3*" MinWidth="140"
                                    CellStyle="{StaticResource NewCell}"
                                    HeaderStyle="{StaticResource NewHeader}">
              <DataGridTemplateColumn.CellTemplate>
                <DataTemplate>
                  <TextBlock Text="{Binding TemplateName}" Padding="8,0"
                             VerticalAlignment="Center" Foreground="#1A202C"
                             TextTrimming="CharacterEllipsis"/>
                </DataTemplate>
              </DataGridTemplateColumn.CellTemplate>
              <DataGridTemplateColumn.CellEditingTemplate>
                <DataTemplate>
                  <ComboBox ItemsSource="{Binding TemplateNames}"
                            SelectedItem="{Binding TemplateName, UpdateSourceTrigger=PropertyChanged}"
                            FontSize="12"/>
                </DataTemplate>
              </DataGridTemplateColumn.CellEditingTemplate>
            </DataGridTemplateColumn>
          </DataGrid.Columns>
        </DataGrid>
      </Border>

      <!-- Options (two rows: duplicate type + per-sheet toggles) -->
      <StackPanel Grid.Row="2" Margin="0,14,0,0">
        <StackPanel Orientation="Horizontal" Margin="0,0,0,8">
          <TextBlock Text="Duplicate type:" Foreground="#4A5568" FontSize="12"
                     VerticalAlignment="Center" Margin="0,0,10,0"/>
          <RadioButton x:Name="rb_with_det"    Content="With Detailing"    IsChecked="True" Margin="0,0,16,0" Cursor="Hand"/>
          <RadioButton x:Name="rb_without_det" Content="Without Detailing" Margin="0,0,16,0" Cursor="Hand"/>
          <RadioButton x:Name="rb_dependent"   Content="As Dependent"      Cursor="Hand"/>
        </StackPanel>
        <StackPanel Orientation="Horizontal">
          <CheckBox x:Name="chk_align" Content="Preserve view position on sheet"
                    IsChecked="True" VerticalAlignment="Center" Cursor="Hand" Margin="0,0,24,0"/>
          <CheckBox x:Name="chk_copy_tb_params" Content="Copy title block &amp; sheet parameters"
                    IsChecked="True" VerticalAlignment="Center" Cursor="Hand" Margin="0,0,24,0"
                    ToolTip="Copy non-readonly instance parameters (Yes/No toggles, text fields) from each source title block and sheet to its duplicate."/>
          <CheckBox x:Name="chk_view_eq_name" Content="View name = Sheet name"
                    VerticalAlignment="Center" Cursor="Hand"
                    ToolTip="Force every duplicate's view name to match its sheet name. The View Name column becomes read-only when this is on."/>
        </StackPanel>
      </StackPanel>
    </Grid>
  </DockPanel>
</Window>
"""

# ── Revisions dialog ─────────────────────────────────────────
REVISION_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Manage Revisions" Width="520" Height="500"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """
    <Style x:Key="CardBorder" TargetType="Border">
      <Setter Property="Background"      Value="White"/>
      <Setter Property="BorderBrush"     Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius"    Value="4"/>
    </Style>
  </Window.Resources>

  <DockPanel>
    <!-- Header -->
    <Border DockPanel.Dock="Top" Background="#2D3748" Padding="20,12">
      <Grid>
        <StackPanel HorizontalAlignment="Left">
          <TextBlock Text="Manage Revisions" Foreground="White"
                     FontSize="18" FontWeight="Bold"/>
          <TextBlock Text="Check revisions to add to (or keep on) selected sheets. Uncheck to remove."
                     Foreground="#CBD5E0" FontSize="11" Margin="0,2,0,0"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- Footer -->
    <Border DockPanel.Dock="Bottom" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="20,12">
      <StackPanel Orientation="Vertical">
        <TextBlock x:Name="lbl_rev_info"
                   Foreground="#718096" FontSize="11"
                   TextWrapping="Wrap" Margin="0,0,0,10"/>
        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
          <Button x:Name="btn_cancel" Content="Cancel" Style="{StaticResource SecondaryButton}"/>
          <Button x:Name="btn_apply"  Content="Apply"  Style="{StaticResource PrimaryButton}"/>
        </StackPanel>
      </StackPanel>
    </Border>

    <Grid Margin="20,16">
      <Border Style="{StaticResource CardBorder}">
        <ScrollViewer>
          <ListBox x:Name="lst_revisions" Background="Transparent" BorderThickness="0"
                   Foreground="#1A202C" FontSize="12">
            <ListBox.ItemTemplate>
              <DataTemplate>
                <!-- IsThreeState=True so a "mixed" row (revision is on some
                     selected sheets but not all) renders as indeterminate.
                     Click cycles null → False → True → null. -->
                <CheckBox IsChecked="{Binding IsChecked, Mode=TwoWay,
                                              UpdateSourceTrigger=PropertyChanged,
                                              TargetNullValue={x:Null}}"
                          IsThreeState="True"
                          Foreground="#1A202C" Margin="6,4" Cursor="Hand">
                  <CheckBox.Content>
                    <StackPanel Orientation="Horizontal">
                      <TextBlock Text="{Binding SeqNum}" Foreground="#2B6CB0"
                                 FontWeight="SemiBold" Width="28"/>
                      <TextBlock Text="{Binding Date}" Foreground="#718096" Width="90"/>
                      <TextBlock Text="{Binding Description}" Foreground="#1A202C"/>
                    </StackPanel>
                  </CheckBox.Content>
                </CheckBox>
              </DataTemplate>
            </ListBox.ItemTemplate>
          </ListBox>
        </ScrollViewer>
      </Border>
    </Grid>
  </DockPanel>
</Window>
"""


# ═══════════════════════════════════════════════════════════════
# REVISION ROW (for revision dialog)
# ═══════════════════════════════════════════════════════════════

class RevisionRow(INotifyPropertyChanged):
    """A revision row with tri-state: True=on every selected sheet,
    False=on none, None=mixed (on some but not all). The original state
    is captured separately so Apply can detect actual user changes."""

    PropertyChanged = None

    def add_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, value)

    def remove_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, value)

    def _notify(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(self, PropertyChangedEventArgs(name))

    def __init__(self, rev_element, initial_state):
        # initial_state is True / False / None  (True=all, False=none, None=mixed)
        self._rev = rev_element
        self._checked  = initial_state
        self._original = initial_state
        try:
            self._seq = str(rev_element.get_Parameter(
                BuiltInParameter.PROJECT_REVISION_SEQUENCE_NUM).AsInteger())
        except Exception:
            self._seq = "?"
        try:
            self._date = rev_element.get_Parameter(
                BuiltInParameter.PROJECT_REVISION_REVISION_DATE).AsString() or ""
        except Exception:
            self._date = ""
        try:
            self._desc = rev_element.get_Parameter(
                BuiltInParameter.PROJECT_REVISION_REVISION_DESCRIPTION).AsString() or ""
        except Exception:
            self._desc = ""

    @property
    def RevElement(self): return self._rev
    @property
    def SeqNum(self):     return self._seq
    @property
    def Date(self):       return self._date
    @property
    def Description(self): return self._desc
    @property
    def OriginalState(self): return self._original

    def _g_chk(self): return self._checked
    def _s_chk(self, v):
        # Accept True / False / None (Nullable<bool>).
        self._checked = v
        self._notify("IsChecked")
    IsChecked = property(_g_chk, _s_chk)


# ═══════════════════════════════════════════════════════════════
# RENAME DIALOG
# ═══════════════════════════════════════════════════════════════

class RenameDialog(object):
    def __init__(self, owner, selected_items):
        self._items   = selected_items
        self._applied = False
        w = Markup.XamlReader.Parse(RENAME_XAML)
        self._w = w
        w.Owner = owner

        self._rb_single  = w.FindName("rb_single")
        self._rb_batch   = w.FindName("rb_batch")
        self._rb_findrep = w.FindName("rb_findrep")
        self._pnl_single = w.FindName("pnl_single")
        self._pnl_batch  = w.FindName("pnl_batch")
        self._pnl_findrep = w.FindName("pnl_findrep")
        self._txt_num    = w.FindName("txt_new_number")
        self._txt_name   = w.FindName("txt_new_name")
        self._txt_prefix = w.FindName("txt_prefix")
        self._txt_sep    = w.FindName("txt_sep")
        self._txt_start  = w.FindName("txt_start")
        self._txt_inc    = w.FindName("txt_inc")
        self._txt_suffix = w.FindName("txt_suffix")
        self._lst        = w.FindName("lst_preview")
        self._btn_prev   = w.FindName("btn_preview")
        self._btn_apply  = w.FindName("btn_apply")
        self._btn_cancel = w.FindName("btn_cancel")
        # Find/replace controls
        self._fr_rules_list   = w.FindName("fr_rules_list")
        self._chk_fr_apply_num  = w.FindName("chk_fr_apply_num")
        self._chk_fr_apply_name = w.FindName("chk_fr_apply_name")
        self._btn_fr_add_rule = w.FindName("btn_fr_add_rule")
        self._btn_fr_preview  = w.FindName("btn_fr_preview")

        # Pre-fill single fields
        if selected_items:
            self._txt_num.Text  = selected_items[0].SheetNumber
            self._txt_name.Text = selected_items[0].SheetName

        # Single Rename only makes sense for a single sheet — when the user
        # has multiple sheets selected, hide that radio entirely and switch
        # the default to Batch Renumber.
        if len(selected_items) > 1:
            self._rb_single.Visibility = Visibility.Collapsed
            self._rb_single.IsChecked  = False
            self._rb_batch.IsChecked   = True
            self._pnl_single.Visibility = Visibility.Collapsed
            self._pnl_batch.Visibility  = Visibility.Visible

        # Find/replace rules collection
        self._fr_rules = ObservableCollection[FindReplaceRule]()
        self._fr_rules.Add(FindReplaceRule())
        self._fr_rules_list.ItemsSource = self._fr_rules

        # Events
        self._rb_single.Checked   += self._on_mode
        self._rb_batch.Checked    += self._on_mode
        self._rb_findrep.Checked  += self._on_mode
        self._btn_prev.Click      += self._on_preview
        self._btn_fr_preview.Click += self._on_fr_preview
        self._btn_fr_add_rule.Click += self._on_fr_add_rule
        self._btn_apply.Click     += self._on_apply
        self._btn_cancel.Click    += lambda s, e: w.Close()
        # Bubble class handler for the per-row "Remove rule" buttons.
        self._fr_rules_list.AddHandler(
            Button.ClickEvent,
            RoutedEventHandler(self._on_fr_rules_button_click))
        w.ShowDialog()

    def _on_mode(self, sender, e):
        # Hide all panels, then show the one matching the active radio.
        self._pnl_single.Visibility  = Visibility.Collapsed
        self._pnl_batch.Visibility   = Visibility.Collapsed
        self._pnl_findrep.Visibility = Visibility.Collapsed
        if self._rb_single.IsChecked:
            self._pnl_single.Visibility  = Visibility.Visible
        elif self._rb_batch.IsChecked:
            self._pnl_batch.Visibility   = Visibility.Visible
        else:
            self._pnl_findrep.Visibility = Visibility.Visible

    def _on_preview(self, sender, e):
        self._lst.Items.Clear()
        rows = self._build_batch_rows()
        for old, new in rows:
            lbi = System.Windows.Controls.ListBoxItem()
            lbi.Content = "{0}  →  {1}".format(old, new)
            lbi.Foreground = SolidColorBrush(Color.FromRgb(0x1A, 0x20, 0x2C))
            self._lst.Items.Add(lbi)

    def _build_batch_rows(self):
        prefix = self._txt_prefix.Text or ""
        sep    = self._txt_sep.Text or ""
        try:    start = int(self._txt_start.Text)
        except: start = 100
        try:    inc   = int(self._txt_inc.Text)
        except: inc   = 1
        suffix = self._txt_suffix.Text or ""
        sorted_items = sorted(self._items, key=lambda x: x.SheetNumber)
        rows = []
        for i, item in enumerate(sorted_items):
            new_num = "{0}{1}{2}{3}{4}".format(
                prefix, sep, start + i * inc, sep, suffix).strip("-_")
            rows.append((item.SheetNumber + "  " + item.SheetName, new_num))
        return rows

    # ── Find/replace mode ────────────────────────────────────

    def _on_fr_add_rule(self, sender, e):
        self._fr_rules.Add(FindReplaceRule())

    def _on_fr_rules_button_click(self, sender, e):
        src = e.OriginalSource
        if isinstance(src, Button) and str(src.Tag) == "RemoveRule":
            rule = src.DataContext
            if rule is not None and rule in self._fr_rules:
                self._fr_rules.Remove(rule)
                e.Handled = True

    def _build_findrep_rows(self):
        """Return list of (old_label, new_label, new_number_or_None, new_name_or_None)
        for every selected sheet, after applying the active rules."""
        apply_num  = bool(self._chk_fr_apply_num.IsChecked)
        apply_name = bool(self._chk_fr_apply_name.IsChecked)
        rules = list(self._fr_rules)
        rows = []
        for item in self._items:
            new_num  = apply_rules(item.SheetNumber, rules) if apply_num else None
            new_name = apply_rules(item.SheetName,   rules) if apply_name else None
            old_lbl = "{0}  •  {1}".format(item.SheetNumber, item.SheetName)
            disp_num  = new_num  if new_num  is not None else item.SheetNumber
            disp_name = new_name if new_name is not None else item.SheetName
            new_lbl = "{0}  •  {1}".format(disp_num, disp_name)
            rows.append((old_lbl, new_lbl, new_num, new_name))
        return rows

    def _on_fr_preview(self, sender, e):
        self._lst.Items.Clear()
        for old_lbl, new_lbl, new_num, new_name in self._build_findrep_rows():
            lbi = System.Windows.Controls.ListBoxItem()
            unchanged = (old_lbl == new_lbl)
            lbi.Content = "{0}  →  {1}{2}".format(
                old_lbl, new_lbl, "    (no change)" if unchanged else "")
            color = (0x71, 0x80, 0x96) if unchanged else (0x1A, 0x20, 0x2C)
            lbi.Foreground = SolidColorBrush(Color.FromRgb(*color))
            self._lst.Items.Add(lbi)

    # ── Apply ────────────────────────────────────────────────

    def _on_apply(self, sender, e):
        with Transaction(doc, "Sheet Manager: Rename/Renumber Sheets") as t:
            t.Start()
            try:
                if self._rb_single.IsChecked:
                    # Single rename — first selected item
                    if self._items:
                        sheet = self._items[0].Sheet
                        new_num  = self._txt_num.Text.strip()
                        new_name = self._txt_name.Text.strip()
                        if new_num:  sheet.SheetNumber = new_num
                        if new_name: sheet.Name = new_name
                elif self._rb_batch.IsChecked:
                    # Batch renumber
                    prefix = self._txt_prefix.Text or ""
                    sep    = self._txt_sep.Text or ""
                    try:    start = int(self._txt_start.Text)
                    except: start = 100
                    try:    inc   = int(self._txt_inc.Text)
                    except: inc   = 1
                    suffix = self._txt_suffix.Text or ""
                    sorted_items = sorted(self._items, key=lambda x: x.SheetNumber)
                    # Pass 1: temp numbers to avoid duplicates
                    for i, item in enumerate(sorted_items):
                        item.Sheet.SheetNumber = "__tmp_{0}__".format(i)
                    # Pass 2: real numbers
                    for i, item in enumerate(sorted_items):
                        new_num = "{0}{1}{2}{3}{4}".format(
                            prefix, sep, start + i * inc, sep, suffix).strip("-_")
                        item.Sheet.SheetNumber = new_num
                else:
                    # Find / replace across selection
                    rows = self._build_findrep_rows()
                    # Pass 1: stage temp numbers if numbers change, to avoid
                    # collisions with existing sheets in the same selection.
                    has_num_changes = any(
                        nn is not None and nn != it.SheetNumber
                        for (_old, _new, nn, _nm), it in zip(rows, self._items))
                    if has_num_changes:
                        for i, item in enumerate(self._items):
                            item.Sheet.SheetNumber = "__tmp_fr_{0}__".format(i)
                    for item, (_old, _new, new_num, new_name) in zip(self._items, rows):
                        sheet = item.Sheet
                        if new_num is not None and new_num.strip():
                            try:
                                sheet.SheetNumber = new_num
                            except Exception:
                                # Append a uniqueness suffix if collision.
                                try:
                                    sheet.SheetNumber = new_num + "-1"
                                except Exception:
                                    pass
                        elif has_num_changes:
                            # Number wasn't being changed — restore original.
                            try:
                                sheet.SheetNumber = item.SheetNumber
                            except Exception:
                                pass
                        if new_name is not None and new_name.strip():
                            try:
                                sheet.Name = new_name
                            except Exception:
                                pass
                t.Commit()
                self._applied = True
            except Exception as ex:
                t.RollBack()
                TaskDialog.Show("Rename Error", str(ex))
        self._w.Close()

    @property
    def Applied(self): return self._applied


# ═══════════════════════════════════════════════════════════════
# DUPLICATE DIALOG
# ═══════════════════════════════════════════════════════════════

class DuplicateDialog(object):
    def __init__(self, owner, selected_items, template_map):
        """
        template_map: list of (name, ElementId) tuples
        """
        self._items       = selected_items
        self._template_map = template_map
        self._success     = False
        template_names    = [t[0] for t in template_map]

        w = Markup.XamlReader.Parse(DUPLICATE_XAML)
        self._w = w
        w.Owner = owner

        self._grid      = w.FindName("dup_grid")
        self._rb_with   = w.FindName("rb_with_det")
        self._rb_without = w.FindName("rb_without_det")
        self._rb_dep    = w.FindName("rb_dependent")
        self._chk_align = w.FindName("chk_align")
        self._chk_copy_tb_params = w.FindName("chk_copy_tb_params")
        self._btn_dup   = w.FindName("btn_duplicate")
        self._btn_cancel = w.FindName("btn_cancel")
        # Naming-rules controls
        self._rules_list      = w.FindName("rules_list")
        self._btn_add_rule    = w.FindName("btn_add_rule")
        self._btn_apply_rules = w.FindName("btn_apply_rules")
        self._btn_reset_rules = w.FindName("btn_reset_rules")
        self._chk_apply_num   = w.FindName("chk_apply_num")
        self._chk_apply_name  = w.FindName("chk_apply_name")
        self._chk_apply_view  = w.FindName("chk_apply_view")
        self._chk_view_eq_name = w.FindName("chk_view_eq_name")
        self._lbl_rules_hint  = w.FindName("lbl_rules_hint")

        # Build rows; subscribe to row PropertyChanged so the
        # "View name = Sheet name" toggle can mirror NewName → NewViewName live.
        # Wrap the bound method as a PropertyChangedEventHandler delegate —
        # SheetItem/DuplicateRowItem combine via System.Delegate.Combine, which
        # only accepts a real Delegate (not a Python instancemethod).
        self._suppress_mirror = False
        # Used while broadcasting a per-row template change to other selected
        # rows — without it, each propagated assignment would re-trigger the
        # broadcast and recurse.
        self._suppress_template_propagation = False
        # Snapshot of the grid's selection captured the moment the user
        # mouse-downs into a cell. WPF resets multi-selection to one row when
        # the user clicks into the per-row template combo, so we save it
        # here and restore it after the template change so the user doesn't
        # lose their shift/ctrl selection.
        self._last_multi_selection = []
        self._row_pc_handler = PropertyChangedEventHandler(self._on_row_property_changed)
        self._rows = ObservableCollection[DuplicateRowItem]()
        for item in selected_items:
            row = DuplicateRowItem(item, template_names)
            row.add_PropertyChanged(self._row_pc_handler)
            self._rows.Add(row)
        self._grid.ItemsSource = self._rows

        # Cache the View Name column so the "View name = Sheet name" toggle
        # can flip its IsReadOnly state. Column order is fixed in the XAML:
        # [From #][From Name][New #][New Name][New View Name][View Template].
        try:
            self._col_view_name = self._grid.Columns[4]
        except Exception:
            self._col_view_name = None

        # Rules collection (start with one empty rule for affordance)
        self._rules = ObservableCollection[FindReplaceRule]()
        self._rules.Add(FindReplaceRule())
        self._rules_list.ItemsSource = self._rules
        self._update_rules_hint()

        # Wire events
        self._btn_dup.Click          += self._on_duplicate
        self._btn_cancel.Click       += lambda s, e: w.Close()
        self._btn_add_rule.Click     += self._on_add_rule
        self._btn_apply_rules.Click  += self._on_apply_rules
        self._btn_reset_rules.Click  += self._on_reset_rules
        self._chk_view_eq_name.Checked   += self._on_view_eq_name
        self._chk_view_eq_name.Unchecked += self._on_view_eq_name
        # Snapshot the selection BEFORE the click hits the cell — WPF reduces
        # multi-row selection to a single row once the click lands on a cell,
        # so we need PreviewMouseLeftButtonDown (the bubbling mouse event
        # would already be too late).
        self._grid.PreviewMouseLeftButtonDown += self._on_grid_preview_mouse_down
        # Bubble class handler for the per-row "Remove rule" buttons.
        self._rules_list.AddHandler(
            Button.ClickEvent,
            RoutedEventHandler(self._on_rules_button_click))

        # Establish initial mirror state (toggle starts OFF so this is mostly
        # a no-op, but keeps everything consistent).
        self._on_view_eq_name(None, None)

        w.ShowDialog()

    def _get_dup_option(self):
        if self._rb_without.IsChecked:
            return ViewDuplicateOption.Duplicate
        if self._rb_dep.IsChecked:
            return ViewDuplicateOption.AsDependent
        return ViewDuplicateOption.WithDetailing

    def _template_id(self, name):
        for n, eid in self._template_map:
            if n == name:
                return eid
        return ElementId.InvalidElementId

    # ── Rules wiring ─────────────────────────────────────────

    def _update_rules_hint(self):
        n = sum(1 for r in self._rules if (r.Find or "").strip())
        if n == 0:
            self._lbl_rules_hint.Text = "Add rules then click Apply Rules to preview."
        else:
            self._lbl_rules_hint.Text = "{0} active rule(s).".format(n)

    def _on_add_rule(self, sender, e):
        self._rules.Add(FindReplaceRule())
        self._update_rules_hint()

    def _on_rules_button_click(self, sender, e):
        src = e.OriginalSource
        if isinstance(src, Button) and str(src.Tag) == "RemoveRule":
            rule = src.DataContext
            if rule is not None and rule in self._rules:
                self._rules.Remove(rule)
                self._update_rules_hint()
                e.Handled = True

    def _on_apply_rules(self, sender, e):
        apply_num    = bool(self._chk_apply_num.IsChecked)
        apply_name   = bool(self._chk_apply_name.IsChecked)
        apply_view   = bool(self._chk_apply_view.IsChecked)
        view_eq_name = bool(self._chk_view_eq_name.IsChecked)
        rules        = list(self._rules)

        # Suppress NewName→NewViewName mirroring while we set values explicitly.
        self._suppress_mirror = True
        try:
            for row in self._rows:
                if apply_num:
                    row.NewNumber = apply_rules(row.SourceNumber, rules)
                if apply_name:
                    row.NewName = apply_rules(row.SourceName, rules)
                if view_eq_name:
                    row.NewViewName = row.NewName
                elif apply_view:
                    row.NewViewName = apply_rules(row.SourceViewName, rules)
        finally:
            self._suppress_mirror = False
        self._update_rules_hint()

    def _on_reset_rules(self, sender, e):
        self._suppress_mirror = True
        try:
            for row in self._rows:
                row.reset_to_defaults()
        finally:
            self._suppress_mirror = False

    def _on_view_eq_name(self, sender, e):
        """Sync UI state to the "View name = Sheet name" toggle. When ON:
          - the View Name column becomes read-only and renders muted/italic
            (via the IsViewNameMirrored DataTrigger),
          - the rules-card "Apply to: View Name" checkbox is disabled (since
            rules can't target a mirrored field), and
          - every row's NewViewName is immediately mirrored to NewName.
        When OFF, all of those revert."""
        on = bool(self._chk_view_eq_name.IsChecked)
        # Disable the Apply-to View Name chip when mirroring is on.
        try:
            self._chk_apply_view.IsEnabled = not on
        except Exception:
            pass
        # Flip the column to read-only when mirroring is on.
        if self._col_view_name is not None:
            try:
                self._col_view_name.IsReadOnly = on
            except Exception:
                pass
        # Update each row's mirrored flag (drives the muted-italic style) and,
        # if on, copy NewName into NewViewName so the cell reflects the lock.
        self._suppress_mirror = True
        try:
            for row in self._rows:
                row.IsViewNameMirrored = on
                if on:
                    row.NewViewName = row.NewName
        finally:
            self._suppress_mirror = False

    # ── Multi-row template propagation ───────────────────────

    def _hit_test_row(self, e):
        """Walk up from e.OriginalSource to find the DataGridRow it belongs
        to, and return the bound row item (DuplicateRowItem) - or None if
        the click wasn't on a row (header, scrollbar, empty space, etc.)."""
        node = e.OriginalSource
        while node is not None:
            if isinstance(node, DataGridRow):
                try:
                    return node.Item
                except Exception:
                    return None
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                return None
        return None

    def _on_grid_preview_mouse_down(self, sender, e):
        """Maintain the multi-selection snapshot used by the per-row template
        dropdown to broadcast a template pick to every highlighted row.

        WPF collapses multi-row selection to a single row as soon as a click
        lands on a cell. To survive that, we snapshot the selection here
        (Preview events tunnel down before the click is processed) and only
        clear the snapshot when the user clicks a row that is NOT in the
        existing multi-selection (which signals a fresh single-select)."""
        try:
            current = list(self._grid.SelectedItems)
        except Exception:
            current = []

        # Capture a fresh multi-selection if the live selection has more than
        # one row. This handles the initial shift/ctrl-click sequence that
        # built the multi-selection.
        if len(current) > 1:
            self._last_multi_selection = current
            return

        # Otherwise: WPF may be about to collapse the selection. Decide
        # whether to keep the prior snapshot (user clicked inside their own
        # multi-selection - probably opening a dropdown) or clear it (user
        # clicked outside - making a fresh single-select).
        if not self._last_multi_selection:
            return  # nothing snapshotted yet

        clicked_item = self._hit_test_row(e)
        if clicked_item is None:
            return  # click on header/scrollbar/etc. - leave snapshot alone

        if clicked_item in self._last_multi_selection:
            # Click inside the multi-selection -> preserve snapshot so the
            # template broadcast still has the rows it needs.
            return

        # Click on a row outside the multi-selection -> user is making a
        # fresh selection, drop the snapshot so we don't broadcast to
        # stale rows.
        self._last_multi_selection = []

    def _restore_multi_selection(self):
        """Re-apply the snapshotted multi-selection to the grid so the user
        keeps their shift/ctrl context after a per-row template change."""
        if not self._last_multi_selection:
            return
        try:
            self._grid.SelectedItems.Clear()
            for row in self._last_multi_selection:
                self._grid.SelectedItems.Add(row)
        except Exception:
            pass

    def _on_row_property_changed(self, sender, args):
        if self._suppress_mirror:
            return

        # Broadcast a per-row template pick to every row that was part of the
        # multi-selection at click-time, so the per-row ComboBox feels like an
        # in-cell version of the "Apply to selected" toolbar.
        if args.PropertyName == "TemplateName":
            if (not self._suppress_template_propagation
                    and self._last_multi_selection
                    and sender in self._last_multi_selection):
                new_value = sender.TemplateName
                targets = [r for r in self._last_multi_selection if r is not sender]
                if targets:
                    self._suppress_template_propagation = True
                    try:
                        for row in targets:
                            try:
                                if row.TemplateName != new_value:
                                    row.TemplateName = new_value
                            except Exception:
                                pass
                    finally:
                        self._suppress_template_propagation = False
                    # Defer the selection restore to the next dispatcher tick
                    # so it happens after WPF finishes closing the cell editor.
                    try:
                        self._w.Dispatcher.BeginInvoke(
                            DispatcherPriority.Background,
                            Action(self._restore_multi_selection))
                    except Exception:
                        # If deferring fails (older WPF/IronPython), restore
                        # inline. WPF tolerates this in most cases.
                        self._restore_multi_selection()
            return

        if args.PropertyName != "NewName":
            return
        if not bool(self._chk_view_eq_name.IsChecked):
            return
        # Live-mirror sheet name into view name when the toggle is on.
        try:
            sender.NewViewName = sender.NewName
        except Exception:
            pass

    # ── Duplicate execution ──────────────────────────────────

    def _on_duplicate(self, sender, e):
        dup_option   = self._get_dup_option()
        preserve_pos = bool(self._chk_align.IsChecked)
        copy_params  = bool(self._chk_copy_tb_params.IsChecked)
        view_eq_name = bool(self._chk_view_eq_name.IsChecked)

        errors = []
        with Transaction(doc, "Sheet Manager: Duplicate Sheets") as t:
            t.Start()
            try:
                for row in self._rows:
                    src_sheet  = row.SourceSheet
                    tb_id      = get_titleblock_id(doc, src_sheet)
                    new_sheet  = ViewSheet.Create(doc, tb_id)
                    # Apply number and name (guard against duplicates)
                    try:
                        new_sheet.SheetNumber = row.NewNumber
                    except Exception:
                        new_sheet.SheetNumber = row.NewNumber + "-1"
                    new_sheet.Name = row.NewName

                    # Copy non-readonly sheet & title-block instance parameters
                    # so the duplicate's title block looks like the source's
                    # (Yes/No toggles, custom text fields, etc.) — per source.
                    if copy_params:
                        try:
                            copy_writable_params(src_sheet, new_sheet)
                        except Exception as ex:
                            errors.append("Sheet params {0}: {1}".format(
                                row.NewNumber, str(ex)))
                        try:
                            src_tb = get_titleblock_instance(doc, src_sheet)
                            dst_tb = get_titleblock_instance(doc, new_sheet)
                            copy_writable_params(src_tb, dst_tb)
                        except Exception as ex:
                            errors.append("Title block params {0}: {1}".format(
                                row.NewNumber, str(ex)))

                    tmpl_id = self._template_id(row.TemplateName)
                    # Final view name for this row (mirror toggle wins).
                    final_view_name = row.NewName if view_eq_name else row.NewViewName

                    # Duplicate each viewport
                    vp_ids = list(src_sheet.GetAllViewports())
                    for vp_id in vp_ids:
                        vp  = doc.GetElement(vp_id)
                        if not vp:
                            continue
                        view = doc.GetElement(vp.ViewId)
                        if not view:
                            continue
                        center = vp.GetBoxCenter() if preserve_pos else None

                        # Determine new view
                        new_view_id = None
                        try:
                            if view.ViewType == ViewType.Legend:
                                # Place same legend (legends can appear on multiple sheets)
                                new_view_id = view.Id
                            elif hasattr(view, "Duplicate"):
                                dup_id = view.Duplicate(dup_option)
                                new_view_id = dup_id
                                new_view = doc.GetElement(dup_id)
                                # Set name
                                if final_view_name:
                                    try:
                                        new_view.Name = final_view_name
                                    except Exception:
                                        pass
                                # Set template
                                if tmpl_id != ElementId.InvalidElementId:
                                    try:
                                        new_view.ViewTemplateId = tmpl_id
                                    except Exception:
                                        pass
                        except Exception as ex:
                            errors.append("View '{0}': {1}".format(
                                view.Name if hasattr(view, "Name") else "?", str(ex)))
                            continue

                        if new_view_id:
                            try:
                                new_vp = Viewport.Create(
                                    doc, new_sheet.Id, new_view_id,
                                    center if center else System.Windows.Point())
                                # Restore alignment
                                if preserve_pos and center:
                                    new_vp.SetBoxCenter(center)
                            except Exception as ex:
                                errors.append("Viewport: " + str(ex))

                t.Commit()
                self._success = True
            except Exception as ex:
                t.RollBack()
                TaskDialog.Show("Duplicate Error", str(ex))
                self._w.Close()
                return

        if errors:
            TaskDialog.Show("Duplicate — Warnings",
                            "Completed with warnings:\n" + "\n".join(errors[:10]))
        self._w.Close()

    @property
    def Success(self): return self._success


# ═══════════════════════════════════════════════════════════════
# REVISION DIALOG
# ═══════════════════════════════════════════════════════════════

class RevisionDialog(object):
    def __init__(self, owner, selected_items):
        self._items   = selected_items
        self._applied = False
        revisions     = get_all_revisions(doc)

        def rev_on_sheet(rev_id, sheet):
            """True iff the revision is on the sheet (manually added OR via a
            revision cloud in a placed view). GetAllRevisionIds is the union."""
            try:
                ids = list(sheet.GetAllRevisionIds())
                return rev_id in ids
            except Exception:
                return False

        w = Markup.XamlReader.Parse(REVISION_XAML)
        self._w = w
        w.Owner = owner

        self._lst   = w.FindName("lst_revisions")
        self._lbl   = w.FindName("lbl_rev_info")
        btn_apply   = w.FindName("btn_apply")
        btn_cancel  = w.FindName("btn_cancel")

        self._lbl.Text = (
            "Applying to {0} sheet(s).  ☑ = on every sheet,  ☐ = on none,  "
            "▣ = mixed.".format(len(selected_items)))

        self._rows = ObservableCollection[RevisionRow]()
        for rev in revisions:
            states = [rev_on_sheet(rev.Id, it.Sheet) for it in selected_items]
            if all(states):
                init = True       # on every selected sheet
            elif not any(states):
                init = False      # on no selected sheet
            else:
                init = None       # mixed
            self._rows.Add(RevisionRow(rev, init))
        self._lst.ItemsSource = self._rows

        btn_apply.Click  += self._on_apply
        btn_cancel.Click += lambda s, e: w.Close()
        w.ShowDialog()

    def _on_apply(self, sender, e):
        # Track sheets we couldn't fully un-apply because the revision is
        # tied to a revision cloud in a placed view (can only be removed by
        # deleting the cloud — outside this dialog's scope).
        cloud_pinned = []   # list of "sheet_number  •  revision desc"
        changed_any = False

        with Transaction(doc, "Sheet Manager: Update Revisions") as t:
            t.Start()
            try:
                for row in self._rows:
                    target = row.IsChecked
                    # Only act if the user actually changed this row away from
                    # its original mixed/all/none state.
                    if target == row.OriginalState:
                        continue
                    if target is None:
                        # User cycled back to indeterminate — treat as no change.
                        continue
                    rid = row.RevElement.Id
                    for item in self._items:
                        sheet = item.Sheet
                        try:
                            addl    = list(sheet.GetAdditionalRevisionIds())
                            all_ids = list(sheet.GetAllRevisionIds())
                            on_via_cloud = (rid in all_ids) and (rid not in addl)

                            if target:
                                # Want this revision ON every selected sheet.
                                if rid not in all_ids:
                                    addl.append(rid)
                                    sheet.SetAdditionalRevisionIds(List[ElementId](addl))
                                    changed_any = True
                                # Already on (manually or via cloud) — nothing to do.
                            else:
                                # Want this revision OFF every selected sheet.
                                if rid in addl:
                                    addl.remove(rid)
                                    sheet.SetAdditionalRevisionIds(List[ElementId](addl))
                                    changed_any = True
                                if on_via_cloud:
                                    cloud_pinned.append(
                                        "{0}  •  {1}".format(
                                            sheet.SheetNumber, row.Description or "(no desc)"))
                        except Exception as inner:
                            TaskDialog.Show(
                                "Revision Error",
                                "Sheet {0}: {1}".format(sheet.SheetNumber, str(inner)))
                t.Commit()
                self._applied = changed_any
            except Exception as ex:
                t.RollBack()
                TaskDialog.Show("Revision Error", str(ex))
                self._w.Close()
                return

        if cloud_pinned:
            preview = "\n".join(cloud_pinned[:10])
            extra = "" if len(cloud_pinned) <= 10 else "\n…and {0} more.".format(
                len(cloud_pinned) - 10)
            TaskDialog.Show(
                "Revisions — could not remove",
                "These revisions stayed on their sheets because a revision "
                "cloud in a placed view is forcing them. To remove, delete "
                "the cloud (or hide its view).\n\n" + preview + extra)
        self._w.Close()

    @property
    def Applied(self): return self._applied


# ═══════════════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════════════

class SheetManagerWindow(object):
    def __init__(self):
        self._all_items     = []       # All SheetItem objects
        self._filtered      = []       # After search filter + filter pane
        self._template_map  = get_all_view_templates(doc)
        self._group_prop    = "Prefix" # Current grouping property
        # Filter state (filter pane). Empty set → no filtering on that axis.
        self._filter_prefixes = set()  # set[str], e.g. {'E', 'F'}
        self._filter_series   = set()  # set[int], e.g. {100, 200}
        self._filter_params   = {}     # {slot_index: True | False}, missing means Any

        # Discover sheet-bound project parameters BEFORE any SheetItem is built
        # so each SheetItem reads its slot values during construction.
        global SHEET_PROJECT_PARAMS
        SHEET_PROJECT_PARAMS = get_sheet_project_params(doc)

        w = Markup.XamlReader.Parse(MAIN_XAML)
        self._w = w

        # Parent to Revit's main window so it doesn't spawn as a separate taskbar entry
        try:
            hwnd = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
            WindowInteropHelper(w).Owner = hwnd
        except Exception:
            pass

        # Find controls
        self._lbl_project  = w.FindName("lbl_project")
        self._txt_search   = w.FindName("txt_search")
        self._ph_search    = w.FindName("ph_search")
        self._cmb_group    = w.FindName("cmb_group")
        self._grid         = w.FindName("sheet_grid")
        self._lbl_status   = w.FindName("lbl_status")
        self._btn_open     = w.FindName("btn_open")
        self._btn_rename   = w.FindName("btn_rename")
        self._btn_dup      = w.FindName("btn_duplicate")
        self._btn_rev      = w.FindName("btn_revisions")
        self._btn_del      = w.FindName("btn_delete")
        self._btn_all      = w.FindName("btn_sel_all")
        self._btn_none     = w.FindName("btn_sel_none")
        # Filter pane controls
        self._btn_filter         = w.FindName("btn_filter")
        self._filter_panel       = w.FindName("filter_panel")
        self._pnl_fil_prefixes   = w.FindName("pnl_filter_prefixes")
        self._pnl_fil_series     = w.FindName("pnl_filter_series")
        self._pnl_fil_params     = w.FindName("pnl_filter_params")
        self._pnl_fil_params_out = w.FindName("pnl_filter_params_outer")
        self._lbl_fil_summary    = w.FindName("lbl_filter_summary")
        self._btn_fil_reset      = w.FindName("btn_filter_reset")

        # Project name in subtitle
        try:
            proj_info = doc.ProjectInformation
            proj_name = proj_info.Name or doc.Title
            if proj_name:
                self._lbl_project.Text = (
                    "Group, rename, renumber, duplicate with views, and manage revisions.    —    "
                    + proj_name)
        except Exception:
            pass

        # Build dynamic project-parameter columns BEFORE loading sheets so the
        # grid is fully shaped on first render.
        self._build_project_param_columns()

        # Load sheets
        self._load_sheets()
        self._populate_filter_chips()
        self._apply_view()
        self._update_status()
        self._update_filter_summary()

        # Wire events
        self._txt_search.TextChanged += self._on_search
        self._txt_search.TextChanged += self._on_search_placeholder
        self._cmb_group.SelectionChanged += self._on_group_changed
        self._grid.SelectionChanged      += self._on_grid_selection
        # Re-sort / re-group the row that was just renamed inline.
        self._grid.CellEditEnding        += self._on_cell_edit_ending
        self._btn_open.Click    += self._on_open
        self._btn_rename.Click  += self._on_rename
        self._btn_dup.Click     += self._on_duplicate
        self._btn_rev.Click     += self._on_revisions
        self._btn_del.Click     += self._on_delete
        self._btn_all.Click     += self._on_select_all
        self._btn_none.Click    += self._on_clear_selection
        # Filter pane events
        self._btn_filter.Checked   += self._on_filter_toggle
        self._btn_filter.Unchecked += self._on_filter_toggle
        self._btn_fil_reset.Click  += self._on_filter_reset

        # Class-level CheckBox.Click handler so the group-header checkbox
        # (Tag="GroupSelector") can toggle every item in its group.
        self._grid.AddHandler(
            CheckBox.ClickEvent,
            RoutedEventHandler(self._on_any_checkbox_click))

        w.ShowDialog()

    # ── Dynamic project-parameter columns ────────────────────

    def _build_project_param_columns(self):
        """For each project parameter bound to Sheets, append a column to the
        DataGrid (Yes/No → CheckBox column, otherwise → text column).
        Bindings target ProjectParam1..ProjectParamN slots on SheetItem."""
        if not SHEET_PROJECT_PARAMS:
            return
        # Insert each new column just before the trailing "Rev" column.
        rev_index = self._grid.Columns.Count - 1  # "Rev" is currently last
        if rev_index < 0:
            rev_index = self._grid.Columns.Count

        for slot, info in enumerate(SHEET_PROJECT_PARAMS):
            slot_prop = "ProjectParam{0}".format(slot + 1)
            header_text = info["name"]
            # XAML-escape the header (ampersands, quotes)
            header_xml = (header_text.replace("&", "&amp;")
                                     .replace("<", "&lt;")
                                     .replace('"', "&quot;"))
            if info["kind"] == "yesno":
                col_xaml = """
<DataGridTemplateColumn xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
                        Header="{HDR}" Width="Auto" MinWidth="80" CanUserResize="True">
  <DataGridTemplateColumn.CellTemplate>
    <DataTemplate>
      <CheckBox Tag="{TAG}"
                IsChecked="{Binding {SLOT}, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Cursor="Hand"/>
    </DataTemplate>
  </DataGridTemplateColumn.CellTemplate>
</DataGridTemplateColumn>
""".replace("{HDR}", header_xml).replace("{TAG}", slot_prop) \
   .replace("{Binding {SLOT},", "{Binding " + slot_prop + ",")
            else:
                col_xaml = """
<DataGridTextColumn xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
                    Header="{HDR}"
                    Binding="{Binding {SLOT}, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                    Width="Auto" MinWidth="100">
  <DataGridTextColumn.ElementStyle>
    <Style TargetType="TextBlock">
      <Setter Property="Padding"           Value="10,0"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="Foreground"        Value="#1A202C"/>
      <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
    </Style>
  </DataGridTextColumn.ElementStyle>
</DataGridTextColumn>
""".replace("{HDR}", header_xml) \
   .replace("{Binding {SLOT},", "{Binding " + slot_prop + ",")
            try:
                col = Markup.XamlReader.Parse(col_xaml)
                # Reference the same MainEditCell style from the live window.
                if info["kind"] != "yesno":
                    style = self._w.TryFindResource("MainEditCell")
                    if style is not None:
                        col.EditingElementStyle = style
                self._grid.Columns.Insert(rev_index, col)
                rev_index += 1
            except Exception as ex:
                # If parsing fails for an exotic param name, just skip it
                # rather than blowing up the whole window.
                try:
                    TaskDialog.Show(
                        "Sheet Manager",
                        "Couldn't add column for parameter '{0}':\n{1}".format(
                            info["name"], str(ex)))
                except Exception:
                    pass

    # ── Data loading ─────────────────────────────────────────

    def _load_sheets(self):
        sheets = (FilteredElementCollector(doc)
                  .OfClass(ViewSheet)
                  .ToElements())
        self._all_items = []
        for s in sheets:
            try:
                self._all_items.append(SheetItem(s, doc))
            except Exception:
                pass
        self._all_items.sort(key=lambda x: x.SheetNumber)
        self._filtered = list(self._all_items)

    def _apply_view(self):
        """Push filtered list into DataGrid with current grouping."""
        col = ObservableCollection[SheetItem]()
        for item in self._filtered:
            col.Add(item)

        view = CollectionViewSource.GetDefaultView(col)
        view.GroupDescriptions.Clear()
        if self._group_prop:
            view.GroupDescriptions.Add(PropertyGroupDescription(self._group_prop))
        view.SortDescriptions.Clear()
        view.SortDescriptions.Add(SortDescription("SheetNumber", ListSortDirection.Ascending))
        view.Refresh()

        self._grid.ItemsSource = view

    def _update_status(self):
        selected = [i for i in self._filtered if i.IsSelected]
        self._lbl_status.Text = "{0} selected  |  {1} shown  |  {2} total".format(
            len(selected), len(self._filtered), len(self._all_items))
        has_sel = len(selected) > 0
        self._btn_open.IsEnabled    = (len(selected) == 1)
        self._btn_rename.IsEnabled  = has_sel
        self._btn_dup.IsEnabled     = has_sel
        self._btn_rev.IsEnabled     = has_sel
        self._btn_del.IsEnabled     = has_sel

    # ── Events ───────────────────────────────────────────────

    def _on_search(self, sender, e):
        self._apply_filters()

    # ── Filter pane ──────────────────────────────────────────

    @staticmethod
    def _series_bucket(sheet_number):
        """E.g. 'E201' → 200, 'A1.2' → 0 (no bucket since <100). None if no digits."""
        m = re.search(r"\d+", sheet_number or "")
        if not m:
            return None
        n = int(m.group(0))
        return (n // 100) * 100

    def _populate_filter_chips(self):
        """Build the chip toggle buttons for prefixes / series / params,
        based on what actually exists in the project."""
        # PREFIXES
        self._pnl_fil_prefixes.Children.Clear()
        prefixes = sorted({i.Prefix for i in self._all_items if i.Prefix})
        chip_style = self._w.TryFindResource("ChipToggle")
        for p in prefixes:
            tb = ToggleButton()
            tb.Content = p
            tb.Tag = "PFX:" + p
            if chip_style is not None:
                tb.Style = chip_style
            tb.Checked   += self._on_filter_chip
            tb.Unchecked += self._on_filter_chip
            self._pnl_fil_prefixes.Children.Add(tb)

        # SERIES
        self._pnl_fil_series.Children.Clear()
        buckets = sorted({self._series_bucket(i.SheetNumber) for i in self._all_items
                          if self._series_bucket(i.SheetNumber) is not None})
        for b in buckets:
            tb = ToggleButton()
            tb.Content = "{0}s".format(b)
            tb.Tag = "SER:" + str(b)
            if chip_style is not None:
                tb.Style = chip_style
            tb.Checked   += self._on_filter_chip
            tb.Unchecked += self._on_filter_chip
            self._pnl_fil_series.Children.Add(tb)

        # PROJECT PARAMETERS — show one tri-state group per Yes/No param
        self._pnl_fil_params.Children.Clear()
        yesno_slots = [(slot, info) for slot, info in enumerate(SHEET_PROJECT_PARAMS)
                       if info["kind"] == "yesno"]
        if not yesno_slots:
            self._pnl_fil_params_out.Visibility = Visibility.Collapsed
        else:
            self._pnl_fil_params_out.Visibility = Visibility.Visible
            for slot, info in yesno_slots:
                # Outer chip group: [Param Name: ✓ Yes  ✗ No]
                grp = StackPanel()
                grp.Orientation = Orientation.Horizontal
                grp.Margin = Thickness(0, 0, 12, 4)
                lbl = TextBlock()
                lbl.Text = info["name"] + ":"
                lbl.Foreground = SolidColorBrush(Color.FromRgb(0x4A, 0x55, 0x68))
                lbl.FontSize = 11
                lbl.VerticalAlignment = VerticalAlignment.Center
                lbl.Margin = Thickness(0, 0, 6, 0)
                grp.Children.Add(lbl)
                for label, want in (("Yes", True), ("No", False)):
                    tb = ToggleButton()
                    tb.Content = label
                    tb.Tag = "PRM:{0}:{1}".format(slot, 1 if want else 0)
                    if chip_style is not None:
                        tb.Style = chip_style
                    tb.Checked   += self._on_filter_chip
                    tb.Unchecked += self._on_filter_chip
                    grp.Children.Add(tb)
                self._pnl_fil_params.Children.Add(grp)

    def _on_filter_toggle(self, sender, e):
        self._filter_panel.Visibility = (
            Visibility.Visible if self._btn_filter.IsChecked else Visibility.Collapsed)

    def _on_filter_chip(self, sender, e):
        tag = str(sender.Tag) if sender.Tag is not None else ""
        is_on = bool(sender.IsChecked)

        if tag.startswith("PFX:"):
            val = tag[4:]
            if is_on:
                self._filter_prefixes.add(val)
            else:
                self._filter_prefixes.discard(val)

        elif tag.startswith("SER:"):
            try:
                val = int(tag[4:])
            except Exception:
                return
            if is_on:
                self._filter_series.add(val)
            else:
                self._filter_series.discard(val)

        elif tag.startswith("PRM:"):
            # PRM:<slot>:<0|1>. Yes and No are mutually exclusive within a param;
            # turning one on auto-turns its sibling off so the filter stays sensible.
            try:
                _, slot_s, want_s = tag.split(":")
                slot = int(slot_s)
                want = bool(int(want_s))
            except Exception:
                return
            if is_on:
                self._filter_params[slot] = want
                # Untoggle the sibling chip (the opposite Yes/No for this slot)
                for child in self._pnl_fil_params.Children:
                    # child is a StackPanel with [label, yes-toggle, no-toggle]
                    for sub in child.Children:
                        if isinstance(sub, ToggleButton) and sub is not sender:
                            sub_tag = str(sub.Tag) if sub.Tag is not None else ""
                            if sub_tag.startswith("PRM:" + str(slot) + ":"):
                                if sub.IsChecked:
                                    # avoid recursive event by setting via .IsChecked
                                    sub.IsChecked = False
            else:
                # Only clear the slot if no chip for this slot is now checked.
                still_on = False
                for child in self._pnl_fil_params.Children:
                    for sub in child.Children:
                        if isinstance(sub, ToggleButton):
                            sub_tag = str(sub.Tag) if sub.Tag is not None else ""
                            if (sub_tag.startswith("PRM:" + str(slot) + ":")
                                    and sub.IsChecked):
                                still_on = True
                                break
                    if still_on:
                        break
                if not still_on and slot in self._filter_params:
                    del self._filter_params[slot]

        self._apply_filters()

    def _on_filter_reset(self, sender, e):
        self._filter_prefixes.clear()
        self._filter_series.clear()
        self._filter_params.clear()
        # Untick every chip
        for panel in (self._pnl_fil_prefixes, self._pnl_fil_series):
            for child in panel.Children:
                if isinstance(child, ToggleButton):
                    child.IsChecked = False
        for child in self._pnl_fil_params.Children:
            for sub in child.Children:
                if isinstance(sub, ToggleButton):
                    sub.IsChecked = False
        self._apply_filters()

    def _apply_filters(self):
        """Combine the search query with every active filter, then refresh."""
        q = (self._txt_search.Text or "").strip().lower()
        items = list(self._all_items)
        if q:
            items = [i for i in items
                     if q in i.SheetNumber.lower() or q in i.SheetName.lower()]
        if self._filter_prefixes:
            items = [i for i in items if i.Prefix in self._filter_prefixes]
        if self._filter_series:
            items = [i for i in items
                     if self._series_bucket(i.SheetNumber) in self._filter_series]
        for slot, want in self._filter_params.items():
            items = [i for i in items if bool(i._params[slot]) == bool(want)]
        self._filtered = items
        self._apply_view()
        self._update_status()
        self._update_filter_summary()

    def _update_filter_summary(self):
        active = (len(self._filter_prefixes) + len(self._filter_series)
                  + len(self._filter_params))
        if active == 0:
            self._lbl_fil_summary.Text = "No filters active."
        else:
            self._lbl_fil_summary.Text = "{0} filter(s) active.".format(active)

    def _on_search_placeholder(self, sender, e):
        self._ph_search.Visibility = (
            Visibility.Collapsed if self._txt_search.Text else Visibility.Visible)

    def _on_group_changed(self, sender, e):
        idx = self._cmb_group.SelectedIndex
        if idx == 0:   self._group_prop = "Prefix"
        elif idx == 1: self._group_prop = "Discipline"
        else:          self._group_prop = None
        self._apply_view()

    def _on_grid_selection(self, sender, e):
        # Just refresh status. Don't force IsSelected=True on row-selection,
        # because that fights the checkbox toggle (uncheck would get reverted
        # to checked when the same click also selects the row).
        self._update_status()

    def _on_cell_edit_ending(self, sender, e):
        """When the user commits an inline edit on Sheet # or Sheet Name,
        the SheetItem setter writes through to Revit. We then defer a Refresh
        on the CollectionView so the row jumps to its new sorted position
        (and into a new prefix group if the first letter changed)."""
        if e.EditAction == DataGridEditAction.Commit:
            # Defer: the binding pushes the value on LostFocus AFTER this event.
            self._w.Dispatcher.BeginInvoke(
                DispatcherPriority.Background,
                Action(self._refresh_view))

    def _refresh_view(self):
        view = self._grid.ItemsSource
        if view is not None and hasattr(view, "Refresh"):
            try:
                view.Refresh()
            except Exception:
                pass

    def _on_any_checkbox_click(self, sender, e):
        """Class-level CheckBox.Click handler. Three behaviors:
          1. Group-header checkbox (Tag='GroupSelector')  → toggle every item in that group.
          2. Row checkbox on a multi-selected row         → propagate to all selected rows.
          3. Project-parameter Yes/No checkbox            → commit to Revit on the row only.
        """
        src = e.OriginalSource
        if not isinstance(src, CheckBox):
            self._update_status()
            return

        tag = src.Tag
        tag_str = str(tag) if tag is not None else ""

        # 1. Group selector
        if tag_str == "GroupSelector":
            grp = src.DataContext
            new_state = bool(src.IsChecked) if src.IsChecked is not None else False
            if grp is not None and hasattr(grp, "Items"):
                for item in grp.Items:
                    if isinstance(item, SheetItem):
                        item.IsSelected = new_state
                self._update_status()
                e.Handled = True
                return

        # 3. Project-parameter checkbox carries the slot tag "ProjectParam<n>"
        if tag_str.startswith("ProjectParam"):
            # Two-way binding has already pushed the new value into SheetItem.
            # Nothing else to do; status is unaffected.
            return

        # 2. Plain row IsSelected checkbox
        clicked_item = src.DataContext
        if isinstance(clicked_item, SheetItem):
            new_state = bool(src.IsChecked)
            sel_items = [i for i in self._grid.SelectedItems if isinstance(i, SheetItem)]
            if clicked_item in sel_items and len(sel_items) > 1:
                # Bulk-toggle: every multi-selected row matches the click.
                for it in sel_items:
                    if it is not clicked_item:
                        it.IsSelected = new_state
        self._update_status()

    def _get_selected(self):
        return [i for i in self._filtered if i.IsSelected]

    def _on_select_all(self, sender, e):
        for i in self._filtered:
            i.IsSelected = True
        self._update_status()

    def _on_select_group(self, sender, e):
        """Select sheets in the same group as the first selected item."""
        sel = self._get_selected()
        if not sel:
            return
        group_val = getattr(sel[0], self._group_prop, None) if self._group_prop else None
        for i in self._filtered:
            if group_val is None or getattr(i, self._group_prop, None) == group_val:
                i.IsSelected = True
        self._update_status()

    def _on_clear_selection(self, sender, e):
        for i in self._filtered:
            i.IsSelected = False
        self._update_status()

    def _on_open(self, sender, e):
        sel = self._get_selected()
        if not sel:
            return
        try:
            uidoc.ActiveView = sel[0].Sheet
            self._w.Close()
        except Exception as ex:
            TaskDialog.Show("Open Sheet", str(ex))

    def _on_rename(self, sender, e):
        sel = self._get_selected()
        if not sel:
            return
        dlg = RenameDialog(self._w, sel)
        if dlg.Applied:
            self._load_sheets()
            self._apply_view()
            self._update_status()

    def _on_duplicate(self, sender, e):
        sel = self._get_selected()
        if not sel:
            return
        dlg = DuplicateDialog(self._w, sel, self._template_map)
        if dlg.Success:
            self._load_sheets()
            self._apply_view()
            self._update_status()

    def _on_revisions(self, sender, e):
        sel = self._get_selected()
        if not sel:
            return
        dlg = RevisionDialog(self._w, sel)
        if dlg.Applied:
            self._load_sheets()
            self._apply_view()
            self._update_status()

    def _on_delete(self, sender, e):
        sel = self._get_selected()
        if not sel:
            return
        count = len(sel)
        msg = "Permanently delete {0} sheet{1}?\n\nThis cannot be undone.".format(
            count, "s" if count > 1 else "")
        res = TaskDialog.Show(
            "Delete Sheets",
            msg,
            TaskDialogCommonButtons.Yes | TaskDialogCommonButtons.No)
        if res != TaskDialogResult.Yes:
            return
        failed = []
        try:
            with Transaction(doc, "Delete Sheets") as t:
                t.Start()
                for item in sel:
                    try:
                        doc.Delete(item.Sheet.Id)
                    except Exception as inner:
                        failed.append("{0}: {1}".format(item.SheetNumber, str(inner)))
                t.Commit()
        except Exception as ex:
            TaskDialog.Show("Delete Sheets", str(ex))
            return
        if failed:
            TaskDialog.Show("Delete Sheets",
                "Could not delete:\n" + "\n".join(failed))
        self._load_sheets()
        self._apply_view()
        self._update_status()


# ═══════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════
SheetManagerWindow()
