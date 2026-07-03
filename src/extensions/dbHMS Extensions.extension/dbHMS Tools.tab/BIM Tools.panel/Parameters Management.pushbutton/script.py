# -*- coding: utf-8 -*-
"""Parameters Management - bulk-edit, admin, transform and exchange parameters.

Iteration 2: Tab 1 (Bulk Edit) wired end-to-end against the active document.
  - Sheet list loads every ViewSheet in the project, sorted by sheet number
    (matches Revit's project browser by-trade-letter order). Filter chips
    for prefix + series (Sheet Manager style).
  - Right-side parameter editor rebuilds on every selection change. Each
    parameter shows the current value across the selection - tri-state
    checkbox for Yes/No, italic <mixed> placeholder for text/number when
    values differ. Read-only and ElementId (Phase, Title Block) parameters
    display only.
  - Editing marks the row's left stripe blue. Apply runs every dirty edit
    inside a single revit.Transaction("Parameters Management - bulk edit"),
    then re-reads values and reports per-sheet failures.

Tabs 2-4 (Project Parameters / Transform Values / Export-Import) are still
the iter-1 preview - mock rows, disabled actions.
"""

__title__  = 'Parameters\nManagement'
__author__ = 'Nathaniel'
__doc__    = ('Bulk-edit instance/type parameter values across many '
              'elements (sheets first), administer project parameters, '
              'transform values, and export/import parameter definitions. '
              'Iteration 2 wires Tab 1 (Bulk Edit) end-to-end; Tabs 2-4 '
              'are still preview only.')

import os
import re
import clr  # noqa: F401

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")
clr.AddReference("System.Xaml")

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, Transaction, StorageType,
)

from System.Windows import (
    Visibility, Thickness, HorizontalAlignment, VerticalAlignment,
    TextAlignment, TextTrimming, TextWrapping,
    FontWeights, FontStyles, CornerRadius,
    GridLength, GridUnitType,
)
from System.Windows.Controls import (
    Border, StackPanel, Grid, ColumnDefinition, RowDefinition,
    CheckBox, TextBlock, TextBox, Button, ComboBox, ComboBoxItem,
    Orientation, ScrollViewer, ScrollBarVisibility, WrapPanel,
)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Input import Cursors, Keyboard, ModifierKeys
from System import EventHandler

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

# Revit document handles (unused in iter 1 but keeps the standard shape)
try:
    doc   = __revit__.ActiveUIDocument.Document
    uidoc = __revit__.ActiveUIDocument
except Exception:
    doc = None
    uidoc = None

SCRIPT_DIR = os.path.dirname(__file__)
MAIN_XAML  = os.path.join(SCRIPT_DIR, 'ParametersManagementForm.xaml')
NEW_PARAM_XAML = os.path.join(SCRIPT_DIR, 'NewParameterDialog.xaml')


# ===========================================================================
# Helpers
# ===========================================================================

def _set_legend_mixed(form):
    """Push the legend's IsThreeState checkbox to indeterminate. WPF starts
    IsThreeState=True at False; we have to set IsChecked = None in code to
    render the middle state. CLAUDE.md UI conventions name the control
    `chk_legend_mixed`."""
    try:
        chk = getattr(form, "chk_legend_mixed", None)
        if chk is not None:
            chk.IsChecked = None
    except Exception:
        pass


def _brush(hex_str):
    h = hex_str.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return SolidColorBrush(Color.FromRgb(r, g, b))
    if len(h) == 8:
        a = int(h[0:2], 16)
        r, g, b = int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
        return SolidColorBrush(Color.FromArgb(a, r, g, b))
    return SolidColorBrush(Color.FromRgb(0, 0, 0))


def _text(value, size=12, weight=None, color="#2D3748", margin=None,
          italic=False, wrap=False):
    tb = TextBlock()
    tb.Text = value
    tb.FontSize = size
    tb.Foreground = _brush(color)
    tb.VerticalAlignment = VerticalAlignment.Center
    if weight == "bold":
        tb.FontWeight = FontWeights.Bold
    elif weight == "semibold":
        tb.FontWeight = FontWeights.SemiBold
    if italic:
        tb.FontStyle = FontStyles.Italic
    if margin is not None:
        tb.Margin = Thickness(*margin)
    if wrap:
        tb.TextWrapping = TextWrapping.Wrap
    return tb


# ===========================================================================
# Mock data
# ===========================================================================
#
# Shape matches what real Revit data will look like once iter 2 wires it up,
# so the UI populating code doesn't have to change much.

MOCK_SHEETS = [
    {"number": "G-001", "name": "Cover Sheet"},
    {"number": "G-002", "name": "Sheet Index"},
    {"number": "G-003", "name": "General Notes"},
    {"number": "M-100", "name": "Mechanical Symbols and Abbreviations"},
    {"number": "M-101", "name": "Mechanical Plan - Level 1"},
    {"number": "M-102", "name": "Mechanical Plan - Level 2"},
    {"number": "M-103", "name": "Mechanical Plan - Roof"},
    {"number": "M-201", "name": "Mechanical Schedules"},
    {"number": "M-301", "name": "Mechanical Details"},
    {"number": "M-501", "name": "Mechanical Diagrams"},
    {"number": "E-100", "name": "Electrical Symbols and Abbreviations"},
    {"number": "E-101", "name": "Electrical Power Plan - Level 1"},
    {"number": "E-102", "name": "Electrical Power Plan - Level 2"},
    {"number": "E-201", "name": "Electrical Lighting Plan - Level 1"},
    {"number": "E-301", "name": "Electrical Panel Schedules"},
    {"number": "E-501", "name": "Electrical Risers"},
    {"number": "P-101", "name": "Plumbing Plan - Level 1"},
    {"number": "P-201", "name": "Plumbing Schedules"},
    {"number": "P-501", "name": "Plumbing Risers"},
    {"number": "FP-101", "name": "Fire Protection Plan - Level 1"},
]

# Mock parameters for Sheets category.  group_label: collapsible section.
# type:  yesno / text / number / date / choice
# read_only: dim the editor
# mixed_in_selection: True => render with <mixed> placeholder
MOCK_SHEET_PARAMS = [
    # Identity Data
    {"name": "Sheet Number",         "group": "Identity Data", "kind": "text",   "read_only": False, "value": "M-101",                 "mixed": True},
    {"name": "Sheet Name",           "group": "Identity Data", "kind": "text",   "read_only": False, "value": "Mechanical Plan - Level 1", "mixed": True},
    {"name": "Sheet Issue Date",     "group": "Identity Data", "kind": "text",   "read_only": False, "value": "2026-05-15",            "mixed": False},
    {"name": "Drawn By",             "group": "Identity Data", "kind": "text",   "read_only": False, "value": "NLR",                   "mixed": False},
    {"name": "Checked By",           "group": "Identity Data", "kind": "text",   "read_only": False, "value": "",                      "mixed": True},
    {"name": "Designed By",          "group": "Identity Data", "kind": "text",   "read_only": False, "value": "DBH",                   "mixed": False},
    {"name": "Approved By",          "group": "Identity Data", "kind": "text",   "read_only": False, "value": "",                      "mixed": False},
    {"name": "Scale",                "group": "Identity Data", "kind": "text",   "read_only": True,  "value": "As indicated",          "mixed": False},
    {"name": "Sheet Width",          "group": "Identity Data", "kind": "text",   "read_only": True,  "value": "36''",                  "mixed": False},
    {"name": "Sheet Height",         "group": "Identity Data", "kind": "text",   "read_only": True,  "value": "24''",                  "mixed": False},

    # Other (built-in flags)
    {"name": "Appears In Sheet List","group": "Other",         "kind": "yesno",  "read_only": False, "value": True,                    "mixed": False},
    {"name": "Current Revision",     "group": "Other",         "kind": "text",   "read_only": True,  "value": "3",                     "mixed": False},
    {"name": "Current Revision Description","group": "Other",  "kind": "text",   "read_only": True,  "value": "100% Construction Documents", "mixed": False},

    # Project + shared (firm)
    {"name": "Permit Set",           "group": "Project Params","kind": "yesno",  "read_only": False, "value": True,                    "mixed": True},
    {"name": "Bid Set",              "group": "Project Params","kind": "yesno",  "read_only": False, "value": False,                   "mixed": False},
    {"name": "Issue 50% CD",         "group": "Project Params","kind": "yesno",  "read_only": False, "value": True,                    "mixed": False},
    {"name": "Issue 90% CD",         "group": "Project Params","kind": "yesno",  "read_only": False, "value": True,                    "mixed": True},
    {"name": "Issue 100% CD",        "group": "Project Params","kind": "yesno",  "read_only": False, "value": False,                   "mixed": False},
    {"name": "Original Sheet Number","group": "Project Params","kind": "text",   "read_only": False, "value": "",                      "mixed": False},
    {"name": "Discipline Code",      "group": "Project Params","kind": "choice", "read_only": False, "value": "M", "choices": ["G", "M", "E", "P", "FP", "T"], "mixed": True},
    {"name": "Sheet Sequence",       "group": "Project Params","kind": "number", "read_only": False, "value": 101,                     "mixed": False},
]

# Mock inventory for tab 2.  Real implementation will read this from
# doc.ParameterBindings + the shared parameter file.
MOCK_PROJECT_PARAMS = [
    {"name": "Permit Set",           "group": "Project Params", "type": "Yes/No", "inst": "Instance", "categories": ["Sheets"],                                        "shared": True,  "in_use": 84},
    {"name": "Bid Set",              "group": "Project Params", "type": "Yes/No", "inst": "Instance", "categories": ["Sheets"],                                        "shared": True,  "in_use": 0},
    {"name": "Issue 50% CD",         "group": "Project Params", "type": "Yes/No", "inst": "Instance", "categories": ["Sheets"],                                        "shared": True,  "in_use": 62},
    {"name": "Issue 90% CD",         "group": "Project Params", "type": "Yes/No", "inst": "Instance", "categories": ["Sheets"],                                        "shared": True,  "in_use": 78},
    {"name": "Issue 100% CD",        "group": "Project Params", "type": "Yes/No", "inst": "Instance", "categories": ["Sheets"],                                        "shared": True,  "in_use": 0},
    {"name": "Discipline Code",      "group": "Project Params", "type": "Text",   "inst": "Instance", "categories": ["Sheets", "Views"],                               "shared": True,  "in_use": 84},
    {"name": "Sheet Sequence",       "group": "Project Params", "type": "Integer","inst": "Instance", "categories": ["Sheets"],                                        "shared": False, "in_use": 84},
    {"name": "Original Sheet Number","group": "Project Params", "type": "Text",   "inst": "Instance", "categories": ["Sheets"],                                        "shared": False, "in_use": 12},
    {"name": "dbHMS Equipment Tag",  "group": "Identity Data",  "type": "Text",   "inst": "Instance", "categories": ["Mechanical Equipment", "Electrical Equipment"],  "shared": True,  "in_use": 421},
    {"name": "Service Disconnect",   "group": "Electrical",     "type": "Text",   "inst": "Instance", "categories": ["Mechanical Equipment"],                          "shared": True,  "in_use": 156},
    {"name": "Air Changes Per Hour", "group": "Mechanical",     "type": "Number", "inst": "Instance", "categories": ["Spaces"],                                        "shared": True,  "in_use": 248},
    {"name": "Room Function",        "group": "Identity Data",  "type": "Text",   "inst": "Instance", "categories": ["Rooms", "Spaces"],                               "shared": True,  "in_use": 196},
    {"name": "FAR Calc",             "group": "Identity Data",  "type": "Area",   "inst": "Type",     "categories": ["Sheets"],                                        "shared": False, "in_use": 0},
]


# ===========================================================================
# Row helpers
# ===========================================================================

def _make_card(child, margin=None):
    """Wrap a control in a white card border."""
    b = Border()
    b.Background = _brush("#FFFFFF")
    b.BorderBrush = _brush("#E2E8F0")
    b.BorderThickness = Thickness(1)
    b.CornerRadius = CornerRadius(4)
    b.Padding = Thickness(12)
    if margin is not None:
        b.Margin = Thickness(*margin)
    else:
        b.Margin = Thickness(0, 0, 0, 10)
    b.Child = child
    return b


def _row_border(highlight=False):
    """One row inside a vertical list. Click to toggle highlight."""
    b = Border()
    b.BorderThickness = Thickness(1)
    b.CornerRadius = CornerRadius(3)
    b.Padding = Thickness(8, 6, 8, 6)
    b.Margin = Thickness(0, 0, 0, 4)
    if highlight:
        b.Background  = _brush("#EBF8FF")
        b.BorderBrush = _brush("#3182CE")
    else:
        b.Background  = _brush("#FFFFFF")
        b.BorderBrush = _brush("#E2E8F0")
    return b


def _sheet_prefix(sheet_number):
    """First alphabetic character of the sheet number, uppercased, so
    families share a group (E201, ED102 -> 'E'; F101, FP201, FPD301 -> 'F').
    Matches Sheet Manager's grouping. Falls back to first char if the
    sheet number doesn't start with a letter."""
    s = (sheet_number or "").strip()
    if s and s[0].isalpha():
        return s[0].upper()
    return s[0] if s else "?"


def _sheet_series(sheet_number):
    """Series bucket from the first run of digits in the number.
    'E201' -> 200, 'M101' -> 100, 'A1.2' -> 0. None if no digits."""
    m = re.search(r"\d+", sheet_number or "")
    if not m:
        return None
    return (int(m.group(0)) // 100) * 100


# ---- Revit parameter helpers -----------------------------------------------

def _param_group_label(param):
    """Return a human-friendly group label for a parameter. Falls back
    through several APIs because the underlying enum / ForgeTypeId path
    changed between Revit versions."""
    try:
        from Autodesk.Revit.DB import LabelUtils
        try:
            return LabelUtils.GetLabelForGroup(param.Definition.ParameterGroup)
        except Exception:
            pass
        try:
            return LabelUtils.GetLabelForGroup(param.Definition.GetGroupTypeId())
        except Exception:
            pass
    except Exception:
        pass
    try:
        return str(param.Definition.ParameterGroup)
    except Exception:
        return "Other"


def _param_kind(param):
    """Map a Revit Parameter to one of:
       'yesno' / 'text' / 'integer' / 'number' / 'choice' / 'unknown'.
    'choice' covers ElementId references (Phase, Title Block type) which
    iter 2 treats as read-only display."""
    # Yes/No detection - try the legacy enum first, then the new ForgeTypeId.
    try:
        from Autodesk.Revit.DB import ParameterType
        if param.Definition.ParameterType == ParameterType.YesNo:
            return "yesno"
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB import SpecTypeId
        if param.Definition.GetDataType() == SpecTypeId.Boolean.YesNo:
            return "yesno"
    except Exception:
        pass

    st = param.StorageType
    if st == StorageType.String:
        return "text"
    if st == StorageType.Integer:
        return "integer"
    if st == StorageType.Double:
        return "number"
    if st == StorageType.ElementId:
        return "choice"
    return "unknown"


def _read_param_value(param, kind):
    """Native Python value for storage and equality comparison. None on error."""
    try:
        if kind == "yesno":
            return bool(param.AsInteger())
        if kind == "integer":
            return param.AsInteger()
        if kind == "number":
            return param.AsDouble()
        if kind == "text":
            return param.AsString() or ""
        if kind == "choice":
            s = param.AsValueString()
            return s if s else ""
    except Exception:
        return None
    return None


def _read_param_display(param, kind):
    """Human-readable string of the parameter's current value."""
    try:
        s = param.AsValueString()
        if s is not None and s != "":
            return s
    except Exception:
        pass
    if kind == "yesno":
        try:
            return "Yes" if param.AsInteger() else "No"
        except Exception:
            return ""
    if kind == "integer":
        try:
            return str(param.AsInteger())
        except Exception:
            return ""
    if kind == "number":
        try:
            return str(param.AsDouble())
        except Exception:
            return ""
    if kind == "text":
        return param.AsString() or ""
    return ""


def _write_param_value(param, kind, new_value):
    """Push new_value back into the Parameter. Returns (ok, error_message)."""
    if param.IsReadOnly:
        return False, "read-only"
    try:
        if kind == "yesno":
            param.Set(1 if new_value else 0)
        elif kind == "integer":
            param.Set(int(new_value))
        elif kind == "number":
            param.Set(float(new_value))
        elif kind == "text":
            param.Set("" if new_value is None else str(new_value))
        else:
            return False, "unsupported type"
        return True, None
    except Exception as exc:
        return False, str(exc)


# ===========================================================================
# Sub-dialog: New Project Parameter
# ===========================================================================

class NewParameterDialog(forms.WPFWindow):
    def __init__(self, owner=None):
        forms.WPFWindow.__init__(self, NEW_PARAM_XAML)
        if owner is not None:
            try:
                self.Owner = owner
            except Exception:
                pass

        # Toggle source-specific sub-panels
        self.rb_src_existing.Checked    += self._on_src_changed
        self.rb_src_project_only.Checked += self._on_src_changed

        # Buttons
        self.btn_np_cancel.Click += self._on_cancel
        self.btn_np_create.Click += self._on_create_disabled

        # Populate category list (mock: small set)
        cats = [
            ("Sheets", True),
            ("Views", False),
            ("Mechanical Equipment", False),
            ("Electrical Equipment", False),
            ("Plumbing Fixtures", False),
            ("Pipes", False),
            ("Ducts", False),
            ("Rooms", False),
            ("Spaces", False),
            ("Title Blocks", False),
            ("Walls", False),
            ("Floors", False),
        ]
        for cat_name, default_checked in cats:
            cb = CheckBox()
            cb.Content = cat_name
            cb.IsChecked = default_checked
            cb.Margin = Thickness(0, 2, 0, 2)
            self.np_category_list.Children.Add(cb)

    def _on_src_changed(self, sender, args):
        if self.rb_src_existing.IsChecked:
            self.panel_src_existing.Visibility = Visibility.Visible
            self.panel_src_project.Visibility  = Visibility.Collapsed
        elif self.rb_src_project_only.IsChecked:
            self.panel_src_existing.Visibility = Visibility.Collapsed
            self.panel_src_project.Visibility  = Visibility.Visible

    def _on_cancel(self, sender, args):
        self.Close()

    def _on_create_disabled(self, sender, args):
        # Disabled in iter 1, but stub the handler so future enabling works.
        dbhms_ui.info(
            "Parameter creation lands in iteration 2. "
            "The layout and validation are in place for review.",
            title='Iteration 1')


# ===========================================================================
# Main form
# ===========================================================================

class ParametersManagementForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, MAIN_XAML)

        # ---- Wire global buttons ----
        self.btn_close.Click += self._on_close

        # ---- Bulk Edit tab ----
        self._sheet_rows = []   # [{"row": Border, "data": dict, "selected": bool}]
        self._param_rows = []   # [{"row": Border, "stripe": Border, "editor": ..., "param": dict, "dirty": bool, "new_value": ...}]
        self._group_headers = []  # [(group_name, TextBlock)] - for hiding empty groups on search
        self._last_clicked_idx = -1
        # Filter state. Empty set on either axis means "no filter on that axis".
        self._filter_prefixes = set()
        self._filter_series   = set()
        self._populate_sheet_list()
        self._populate_filter_chips()
        self._refresh_param_list_for_selection()   # shows placeholder while nothing is selected
        self.txt_sheet_search.TextChanged += self._on_sheet_filter_changed
        self.btn_select_all.Click  += self._on_select_all
        self.btn_select_none.Click += self._on_select_none
        self.btn_filter_toggle.Checked   += self._on_filter_panel_toggle
        self.btn_filter_toggle.Unchecked += self._on_filter_panel_toggle
        self.btn_filter_reset.Click      += self._on_filter_reset
        self.txt_param_search.TextChanged += self._on_param_search
        self.btn_reset_changes.Click      += self._on_reset_changes
        self.btn_apply_bulk.Click         += self._on_apply_bulk

        # ---- Project Parameters tab ----
        self._populate_admin_list()
        self.btn_new_param.Click += self._on_new_param_clicked

        # ---- Transform Values tab ----
        # Default pattern is set here (not in XAML) because braces in an
        # XAML attribute value get parsed as markup extensions.
        self.txt_concat_pattern.Text = "{Discipline}-{Sequence} {Sheet Name}"
        self.cmb_transform_op.SelectionChanged += self._on_transform_op_changed
        self._populate_transform_preview()

        # ---- Export / Import tab ----
        self._populate_export_list()
        self._populate_import_list()

        # Final touches
        _set_legend_mixed(self)
        self._update_selection_count()

    # ===== Close =====
    def _on_close(self, sender, args):
        self.Close()

    # ===== Bulk Edit: sheet list =====
    def _collect_sheets(self):
        """Return sheet dicts sorted by sheet number (by-trade-letter order,
        matches Revit's project browser default). When there's no active
        document we fall back to MOCK_SHEETS so the layout is still
        navigable for review.

        Each dict has:
            number, name : str
            id           : ElementId (None for mocks)
            sheet        : ViewSheet (None for mocks)
        """
        if doc is None:
            return [dict(s, id=None, sheet=None) for s in MOCK_SHEETS]
        try:
            collector = FilteredElementCollector(doc).OfClass(ViewSheet)
            sheets = []
            for s in collector:
                try:
                    sheets.append({
                        "number": s.SheetNumber or "",
                        "name":   s.Name or "",
                        "id":     s.Id,
                        "sheet":  s,
                    })
                except Exception:
                    continue
            sheets.sort(key=lambda d: d["number"])
            return sheets
        except Exception:
            return [dict(s, id=None, sheet=None) for s in MOCK_SHEETS]

    def _populate_sheet_list(self):
        self.sheet_list.Children.Clear()
        self._sheet_rows = []
        sheets = self._collect_sheets()
        for idx, sheet in enumerate(sheets):
            row = _row_border(highlight=False)

            grid = Grid()
            # Three cols: checkbox / sheet number / sheet name
            chk_col  = ColumnDefinition(); chk_col.Width  = GridLength(24)
            num_col  = ColumnDefinition(); num_col.Width  = GridLength(70)
            name_col = ColumnDefinition(); name_col.Width = GridLength(1, GridUnitType.Star)
            grid.ColumnDefinitions.Add(chk_col)
            grid.ColumnDefinitions.Add(num_col)
            grid.ColumnDefinitions.Add(name_col)

            chk = CheckBox()
            chk.IsChecked = False
            chk.IsHitTestVisible = False   # row click is the source of truth
            chk.Focusable = False
            chk.VerticalAlignment = VerticalAlignment.Center
            Grid.SetColumn(chk, 0)
            grid.Children.Add(chk)

            num = _text(sheet["number"], weight="semibold", color="#2D3748")
            num.VerticalAlignment = VerticalAlignment.Center
            Grid.SetColumn(num, 1)
            grid.Children.Add(num)

            name = _text(sheet["name"], color="#4A5568")
            name.VerticalAlignment = VerticalAlignment.Center
            name.TextTrimming = TextTrimming.CharacterEllipsis
            name.Margin = Thickness(8, 0, 0, 0)
            Grid.SetColumn(name, 2)
            grid.Children.Add(name)

            row.Child = grid
            row.Cursor = Cursors.Hand

            entry = {"row": row, "chk": chk, "data": sheet,
                     "selected": False, "idx": idx}
            row.MouseLeftButtonDown += self._make_sheet_click(entry)

            self._sheet_rows.append(entry)
            self.sheet_list.Children.Add(row)

    def _make_sheet_click(self, entry):
        def handler(sender, args):
            mods = Keyboard.Modifiers
            shift = bool(mods & ModifierKeys.Shift)
            ctrl  = bool(mods & ModifierKeys.Control)
            idx = entry["idx"]

            if shift and self._last_clicked_idx >= 0:
                lo, hi = sorted([self._last_clicked_idx, idx])
                for i, e in enumerate(self._sheet_rows):
                    self._set_sheet_selected(e, lo <= i <= hi)
            elif ctrl:
                self._set_sheet_selected(entry, not entry["selected"])
                self._last_clicked_idx = idx
            else:
                for e in self._sheet_rows:
                    self._set_sheet_selected(e, e is entry)
                self._last_clicked_idx = idx

            self._update_selection_count()
            self._refresh_param_list_for_selection()
        return handler

    def _set_sheet_selected(self, entry, selected):
        entry["selected"] = selected
        entry["chk"].IsChecked = selected
        if selected:
            entry["row"].Background  = _brush("#EBF8FF")
            entry["row"].BorderBrush = _brush("#3182CE")
        else:
            entry["row"].Background  = _brush("#FFFFFF")
            entry["row"].BorderBrush = _brush("#E2E8F0")

    def _update_selection_count(self):
        total = len(self._sheet_rows)
        sel = sum(1 for e in self._sheet_rows if e["selected"])
        self.lbl_selection_count.Text = "%d of %d selected" % (sel, total)

    def _on_select_all(self, sender, args):
        for e in self._sheet_rows:
            self._set_sheet_selected(e, True)
        self._update_selection_count()
        self._refresh_param_list_for_selection()

    def _on_select_none(self, sender, args):
        for e in self._sheet_rows:
            self._set_sheet_selected(e, False)
        self._update_selection_count()
        self._refresh_param_list_for_selection()

    def _on_sheet_filter_changed(self, sender, args):
        self._apply_sheet_filters()

    def _apply_sheet_filters(self):
        """Combine the search query with the active prefix/series chips,
        then hide non-matching rows."""
        q = (self.txt_sheet_search.Text or "").lower().strip()
        self.txt_sheet_search_hint.Visibility = (
            Visibility.Collapsed if q else Visibility.Visible
        )
        visible = 0
        for e in self._sheet_rows:
            d = e["data"]
            num = d["number"] or ""
            nm  = d["name"]   or ""
            ok = True
            if q and q not in num.lower() and q not in nm.lower():
                ok = False
            if ok and self._filter_prefixes and _sheet_prefix(num) not in self._filter_prefixes:
                ok = False
            if ok and self._filter_series:
                s = _sheet_series(num)
                if s is None or s not in self._filter_series:
                    ok = False
            e["row"].Visibility = Visibility.Visible if ok else Visibility.Collapsed
            if ok:
                visible += 1
        self._update_filter_summary(visible)

    # ===== Bulk Edit: filter chips =====
    def _populate_filter_chips(self):
        """Build prefix and series chips from whichever sheets are loaded."""
        chip_style = self.TryFindResource("ChipToggle")

        self.pnl_filter_prefixes.Children.Clear()
        prefixes = sorted({_sheet_prefix(e["data"]["number"])
                           for e in self._sheet_rows
                           if e["data"]["number"]})
        for p in prefixes:
            tb = ToggleButton()
            tb.Content = p
            tb.Tag = "PFX:" + p
            if chip_style is not None:
                tb.Style = chip_style
            tb.Checked   += self._on_chip_toggled
            tb.Unchecked += self._on_chip_toggled
            self.pnl_filter_prefixes.Children.Add(tb)

        self.pnl_filter_series.Children.Clear()
        buckets = sorted({s for s in
                          (_sheet_series(e["data"]["number"]) for e in self._sheet_rows)
                          if s is not None})
        for b in buckets:
            tb = ToggleButton()
            tb.Content = "%ds" % b
            tb.Tag = "SER:%d" % b
            if chip_style is not None:
                tb.Style = chip_style
            tb.Checked   += self._on_chip_toggled
            tb.Unchecked += self._on_chip_toggled
            self.pnl_filter_series.Children.Add(tb)

        self._update_filter_summary(len(self._sheet_rows))

    def _on_filter_panel_toggle(self, sender, args):
        self.filter_panel.Visibility = (
            Visibility.Visible if self.btn_filter_toggle.IsChecked
            else Visibility.Collapsed)

    def _on_chip_toggled(self, sender, args):
        tag = str(sender.Tag) if sender.Tag is not None else ""
        on = bool(sender.IsChecked)
        if tag.startswith("PFX:"):
            v = tag[4:]
            if on:
                self._filter_prefixes.add(v)
            else:
                self._filter_prefixes.discard(v)
        elif tag.startswith("SER:"):
            try:
                v = int(tag[4:])
            except Exception:
                return
            if on:
                self._filter_series.add(v)
            else:
                self._filter_series.discard(v)
        self._apply_sheet_filters()

    def _on_filter_reset(self, sender, args):
        self._filter_prefixes.clear()
        self._filter_series.clear()
        for panel in (self.pnl_filter_prefixes, self.pnl_filter_series):
            for child in panel.Children:
                if isinstance(child, ToggleButton):
                    child.IsChecked = False
        self._apply_sheet_filters()

    def _update_filter_summary(self, visible_count):
        total = len(self._sheet_rows)
        parts = []
        if self._filter_prefixes:
            parts.append("Prefix: " + ", ".join(sorted(self._filter_prefixes)))
        if self._filter_series:
            parts.append("Series: " + ", ".join("%ds" % s for s in sorted(self._filter_series)))
        if parts:
            self.lbl_filter_summary.Text = "%d of %d shown - %s" % (
                visible_count, total, "; ".join(parts))
        else:
            self.lbl_filter_summary.Text = "%d sheets" % total

    # ===== Bulk Edit: parameter list =====
    def _selected_sheet_objs(self):
        """ViewSheet objects for currently selected rows."""
        return [e["data"]["sheet"] for e in self._sheet_rows
                if e["selected"] and e["data"].get("sheet") is not None]

    def _collect_params_for_selection(self, selected_sheets):
        """Build the per-row dicts that drive the parameter editor for the
        union of parameters on the selected sheets. Falls back to
        MOCK_SHEET_PARAMS when no real document is available."""
        if not selected_sheets:
            if doc is None and any(e["selected"] for e in self._sheet_rows):
                return list(MOCK_SHEET_PARAMS)
            return []
        if doc is None:
            return list(MOCK_SHEET_PARAMS)

        # Use the first sheet's ordered parameter set as the template.
        first = selected_sheets[0]
        try:
            ordered = list(first.GetOrderedParameters())
        except Exception:
            try:
                ordered = list(first.Parameters)
            except Exception:
                ordered = []

        rows = []
        for p in ordered:
            try:
                name = p.Definition.Name
            except Exception:
                continue
            kind = _param_kind(p)
            if kind == "unknown":
                continue

            group = _param_group_label(p)

            # Gather values across the whole selection.
            all_read_only = p.IsReadOnly
            values = []
            for sh in selected_sheets:
                sp = sh.LookupParameter(name)
                if sp is None:
                    continue
                if not sp.IsReadOnly:
                    all_read_only = False
                values.append(_read_param_value(sp, kind))

            mixed = (len({v for v in values}) > 1) if values else False
            display = _read_param_display(p, kind)

            rows.append({
                "name":      name,
                "group":     group,
                "kind":      kind,
                "read_only": all_read_only,
                "value":     None if mixed else (values[0] if values else None),
                "display":   "" if mixed else display,
                "mixed":     mixed,
            })

        return rows

    def _refresh_param_list_for_selection(self):
        """Recompute the parameter list from the current sheet selection.
        Called on every selection change and after a successful Apply."""
        rows = self._collect_params_for_selection(self._selected_sheet_objs())
        self._populate_param_list(rows)

    def _populate_param_list(self, rows):
        self.param_list.Children.Clear()
        self._param_rows = []
        self._group_headers = []

        any_selected = any(e["selected"] for e in self._sheet_rows)
        if not rows:
            ph = _text(
                "Select one or more sheets on the left to edit parameters."
                if not any_selected
                else "No editable parameters on the selected sheets.",
                color="#718096", italic=True, wrap=True,
                margin=(0, 20, 0, 0))
            ph.HorizontalAlignment = HorizontalAlignment.Center
            self.param_list.Children.Add(ph)
            self._update_pending_label()
            return

        # Group rows, preserving order of first appearance.
        seen_groups = []
        groups = {}
        for p in rows:
            g = p["group"]
            if g not in groups:
                groups[g] = []
                seen_groups.append(g)
            groups[g].append(p)

        for g in seen_groups:
            header = _text(g, size=12, weight="semibold", color="#1A202C",
                           margin=(0, 10, 0, 4))
            self.param_list.Children.Add(header)
            self._group_headers.append((g, header))

            for p in groups[g]:
                row_dict = self._make_param_row(p)
                row_dict["group"] = g
                self.param_list.Children.Add(row_dict["row"])
                self._param_rows.append(row_dict)

        self._update_pending_label()
        # Re-apply search filter (if any) to the newly built rows
        self._on_param_search(None, None)

    def _make_param_row(self, p):
        row = Border()
        row.BorderBrush = _brush("#E2E8F0")
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(2, 6, 2, 6)

        grid = Grid()
        stripe_col = ColumnDefinition(); stripe_col.Width = GridLength(4)
        name_col   = ColumnDefinition(); name_col.Width   = GridLength(220)
        editor_col = ColumnDefinition(); editor_col.Width = GridLength(1, GridUnitType.Star)
        grid.ColumnDefinitions.Add(stripe_col)
        grid.ColumnDefinitions.Add(name_col)
        grid.ColumnDefinitions.Add(editor_col)

        stripe = Border()
        stripe.Background = _brush("#2B6CB0")
        stripe.Visibility = Visibility.Collapsed
        stripe.Margin = Thickness(0, 2, 4, 2)
        Grid.SetColumn(stripe, 0)
        grid.Children.Add(stripe)

        name_block = _text(p["name"], color="#2D3748")
        if p["read_only"]:
            name_block.Foreground = _brush("#A0AEC0")
        Grid.SetColumn(name_block, 1)
        grid.Children.Add(name_block)

        row_dict = {
            "row":    row,
            "stripe": stripe,
            "param":  p,
            "dirty":  False,
            "new_value": None,
            "_user_typed": False,
        }

        editor = self._make_param_editor(p, row_dict)
        Grid.SetColumn(editor, 2)
        grid.Children.Add(editor)

        row.Child = grid
        row_dict["editor"] = editor
        return row_dict

    def _make_param_editor(self, p, row_dict):
        kind = p["kind"]

        # Read-only -> display only, not an input.
        if p["read_only"]:
            tb = _text(p["display"] if not p["mixed"] else "<mixed>",
                       color="#A0AEC0", italic=True)
            tb.TextTrimming = TextTrimming.CharacterEllipsis
            return tb

        if kind == "yesno":
            cb = CheckBox()
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.HorizontalAlignment = HorizontalAlignment.Left
            if p["mixed"]:
                cb.IsThreeState = True
                cb.IsChecked = None
            else:
                cb.IsChecked = bool(p["value"])
            cb.Click += self._make_yesno_handler(row_dict, cb)
            return cb

        if kind == "choice":
            # v2: choice/ElementId params are display-only (e.g. Phase).
            tb = _text(p["display"] if not p["mixed"] else "<mixed>",
                       color="#A0AEC0", italic=True)
            tb.TextTrimming = TextTrimming.CharacterEllipsis
            return tb

        # text / integer / number all use a TextBox
        tb = TextBox()
        if p["mixed"]:
            tb.Text = "<mixed>"
            tb.FontStyle = FontStyles.Italic
            tb.Foreground = _brush("#A0AEC0")
        else:
            v = p.get("value", "")
            tb.Text = "" if v is None else str(v)
        tb.GotFocus    += self._make_text_focus_handler(row_dict, tb)
        tb.TextChanged += self._make_text_handler(row_dict, tb)
        return tb

    # ----- editor change handlers -----
    def _make_yesno_handler(self, row_dict, cb):
        def handler(sender, args):
            # WPF Click on IsThreeState=True cycles checked -> unchecked ->
            # indeterminate. Once the user commits Yes or No, drop tri-state.
            if cb.IsThreeState and cb.IsChecked is not None:
                cb.IsThreeState = False
            self._mark_dirty(row_dict, cb.IsChecked)
        return handler

    def _make_text_focus_handler(self, row_dict, tb):
        def handler(sender, args):
            if row_dict["param"].get("mixed") and not row_dict["_user_typed"]:
                tb.Text = ""
                tb.FontStyle = FontStyles.Normal
                tb.Foreground = _brush("#2D3748")
            row_dict["_user_typed"] = True
        return handler

    def _make_text_handler(self, row_dict, tb):
        def handler(sender, args):
            # Ignore the TextChanged that fires during initial programmatic
            # population (before the user has focused the box).
            if not row_dict["_user_typed"]:
                return
            self._mark_dirty(row_dict, tb.Text)
        return handler

    def _mark_dirty(self, row_dict, new_value):
        row_dict["dirty"] = True
        row_dict["new_value"] = new_value
        row_dict["stripe"].Visibility = Visibility.Visible
        self._update_pending_label()

    def _update_pending_label(self):
        dirty_n  = sum(1 for r in self._param_rows if r.get("dirty"))
        sheets_n = sum(1 for e in self._sheet_rows if e["selected"])
        has_doc  = doc is not None

        if dirty_n == 0:
            self.lbl_pending_changes.Text = "No pending changes."
            self.btn_apply_bulk.IsEnabled = False
            self.btn_reset_changes.IsEnabled = False
            self.btn_apply_bulk.Content = "Apply changes"
            return

        self.lbl_pending_changes.Text = "%d pending change%s on %d sheet%s." % (
            dirty_n,  "" if dirty_n  == 1 else "s",
            sheets_n, "" if sheets_n == 1 else "s",
        )
        self.btn_apply_bulk.IsEnabled = has_doc and sheets_n > 0
        self.btn_reset_changes.IsEnabled = True
        self.btn_apply_bulk.Content = "Apply changes to %d sheet%s" % (
            sheets_n, "" if sheets_n == 1 else "s")

    def _on_param_search(self, sender, args):
        q = (self.txt_param_search.Text or "").lower().strip()
        # Track which groups have at least one visible row so we can
        # hide group headers whose rows are all filtered out.
        visible_groups = set()
        for r in self._param_rows:
            name = r["param"]["name"].lower()
            match = (not q) or (q in name)
            r["row"].Visibility = Visibility.Visible if match else Visibility.Collapsed
            if match:
                visible_groups.add(r.get("group"))
        for grp, header in self._group_headers:
            header.Visibility = (
                Visibility.Visible if grp in visible_groups else Visibility.Collapsed)

    def _on_reset_changes(self, sender, args):
        self._refresh_param_list_for_selection()

    def _on_apply_bulk(self, sender, args):
        if doc is None:
            dbhms_ui.info("No active Revit document.", title="Cannot apply")
            return
        dirty_rows = [r for r in self._param_rows if r.get("dirty")]
        if not dirty_rows:
            return
        selected = self._selected_sheet_objs()
        if not selected:
            dbhms_ui.info("No sheets selected.", title="Nothing to apply")
            return

        successes = 0
        failures = []   # list of (param_name, sheet_label, error_message)
        tx = Transaction(doc, "Parameters Management - bulk edit")
        try:
            tx.Start()
            for r in dirty_rows:
                p_info = r["param"]
                pname  = p_info["name"]
                kind   = p_info["kind"]
                raw    = r["new_value"]
                try:
                    coerced = self._coerce_for_kind(raw, kind)
                except ValueError as ve:
                    failures.append((pname, "(all selected)", str(ve)))
                    continue
                for sh in selected:
                    sp = sh.LookupParameter(pname)
                    label = sh.SheetNumber or "(unknown)"
                    if sp is None:
                        failures.append((pname, label, "no such parameter on sheet"))
                        continue
                    ok, err = _write_param_value(sp, kind, coerced)
                    if ok:
                        successes += 1
                    else:
                        failures.append((pname, label, err))
            tx.Commit()
        except Exception as exc:
            try:
                if tx.HasStarted() and not tx.HasEnded():
                    tx.RollBack()
            except Exception:
                pass
            dbhms_ui.info("Transaction failed: %s" % str(exc),
                          title="Apply failed")
            return

        # Reload values from the doc and clear dirty state.
        self._refresh_param_list_for_selection()

        if failures:
            preview = []
            for pname, label, err in failures[:10]:
                preview.append("  - %s on %s: %s" % (pname, label, err))
            extra = ""
            if len(failures) > 10:
                extra = "\n  ... and %d more" % (len(failures) - 10)
            dbhms_ui.info(
                "Applied %d writes.\n%d failure%s:\n%s%s" % (
                    successes,
                    len(failures), "" if len(failures) == 1 else "s",
                    "\n".join(preview), extra,
                ),
                title="Apply complete with errors")
        else:
            dbhms_ui.info("Applied %d writes across %d sheet%s." % (
                successes, len(selected), "" if len(selected) == 1 else "s"),
                title="Apply complete")

    @staticmethod
    def _coerce_for_kind(value, kind):
        if kind == "yesno":
            if value is None:
                raise ValueError(
                    "Yes/No must be Yes or No (not mixed) to apply.")
            return bool(value)
        if kind == "integer":
            try:
                return int(value)
            except Exception:
                raise ValueError("not a valid integer: %r" % value)
        if kind == "number":
            try:
                return float(value)
            except Exception:
                raise ValueError("not a valid number: %r" % value)
        if kind == "text":
            return "" if value is None else str(value)
        raise ValueError("unsupported parameter kind: %s" % kind)

    # ===== Project Parameters tab =====
    def _populate_admin_list(self):
        self.admin_param_list.Children.Clear()
        for p in MOCK_PROJECT_PARAMS:
            self.admin_param_list.Children.Add(self._make_admin_row(p))

    def _make_admin_row(self, p):
        row = Border()
        row.BorderBrush = _brush("#E2E8F0")
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(10, 8, 10, 8)
        row.Background = _brush("#FFFFFF")

        grid = Grid()
        widths = [(2.5, 180), (1.2, 110), (0.9, 80), (0.8, 70), (2.5, 180), (0.7, 60), (0.7, 70)]
        for star, minw in widths:
            col = ColumnDefinition()
            col.Width = GridLength(star, GridUnitType.Star)
            col.MinWidth = minw
            grid.ColumnDefinitions.Add(col)

        cats_text = ", ".join(p["categories"])
        cells = [
            (p["name"], "semibold", "#2D3748"),
            (p["group"], None, "#4A5568"),
            (p["type"], None, "#4A5568"),
            (p["inst"], None, "#4A5568"),
            (cats_text, None, "#4A5568"),
            ("Yes" if p["shared"] else "No", None, "#4A5568"),
            (str(p["in_use"]) if p["in_use"] > 0 else "-", None, "#4A5568"),
        ]
        for i, (val, weight, color) in enumerate(cells):
            tb = _text(val, weight=weight, color=color)
            tb.TextTrimming = TextTrimming.CharacterEllipsis
            Grid.SetColumn(tb, i)
            grid.Children.Add(tb)

        row.Child = grid
        return row

    def _on_new_param_clicked(self, sender, args):
        dlg = NewParameterDialog(owner=self)
        dlg.ShowDialog()

    # ===== Transform Values tab =====
    def _on_transform_op_changed(self, sender, args):
        idx = self.cmb_transform_op.SelectedIndex
        panels = [
            (0, self.panel_copy),
            (1, self.panel_replace),
            (2, self.panel_concat),
            (3, self.panel_math),
        ]
        for want_idx, panel in panels:
            panel.Visibility = Visibility.Visible if want_idx == idx else Visibility.Collapsed

    def _populate_transform_preview(self):
        # Pre-fill a small mock preview for the default operation (Copy A->B)
        self.transform_preview_list.Children.Clear()
        mocks = [
            ("M-101", "M-101", "M-101"),
            ("M-102", "M-102", "M-102"),
            ("M-103", "M-103", "M-103"),
            ("E-101", "E-101", "E-101"),
            ("E-102", "E-102", "E-102"),
        ]
        for elem, before, after in mocks:
            self.transform_preview_list.Children.Add(self._make_preview_row(elem, before, after))
        self.lbl_transform_status.Text = "Preview shows %d elements (mock)." % len(mocks)

    def _make_preview_row(self, elem, before, after):
        row = Border()
        row.BorderBrush = _brush("#E2E8F0")
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(8, 6, 8, 6)
        row.Background = _brush("#FFFFFF")

        grid = Grid()
        for _ in range(3):
            col = ColumnDefinition()
            col.Width = GridLength(1, GridUnitType.Star)
            col.MinWidth = 120
            grid.ColumnDefinitions.Add(col)

        cells = [
            (elem,   "semibold", "#2D3748"),
            (before, None,       "#4A5568"),
            (after,  None,       "#4A5568"),
        ]
        for i, (val, weight, color) in enumerate(cells):
            tb = _text(val, weight=weight, color=color)
            Grid.SetColumn(tb, i)
            grid.Children.Add(tb)

        row.Child = grid
        return row

    # ===== Export tab =====
    def _populate_export_list(self):
        self.export_param_list.Children.Clear()
        for p in MOCK_PROJECT_PARAMS:
            self.export_param_list.Children.Add(self._make_export_row(p))

    def _make_export_row(self, p):
        row = Border()
        row.BorderBrush = _brush("#E2E8F0")
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(8, 4, 8, 4)
        row.Background = _brush("#FFFFFF")

        grid = Grid()
        col_cb = ColumnDefinition(); col_cb.Width = GridLength(28)
        col_name = ColumnDefinition(); col_name.Width = GridLength(2, GridUnitType.Star)
        col_meta = ColumnDefinition(); col_meta.Width = GridLength(3, GridUnitType.Star)
        for col in (col_cb, col_name, col_meta):
            grid.ColumnDefinitions.Add(col)

        cb = CheckBox()
        cb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(cb, 0)
        grid.Children.Add(cb)

        name_block = _text(p["name"], weight="semibold", color="#2D3748")
        Grid.SetColumn(name_block, 1)
        grid.Children.Add(name_block)

        meta = "%s, %s, %s, %s%s" % (
            p["type"], p["inst"], p["group"],
            ", ".join(p["categories"]),
            "  (shared)" if p["shared"] else "",
        )
        meta_block = _text(meta, color="#718096")
        meta_block.TextTrimming = TextTrimming.CharacterEllipsis
        Grid.SetColumn(meta_block, 2)
        grid.Children.Add(meta_block)

        row.Child = grid
        return row

    # ===== Import tab =====
    def _populate_import_list(self):
        self.import_param_list.Children.Clear()
        # Mock "loaded from JSON" rows with status badges
        mocks = [
            {"name": "Permit Set",            "status": "Already exists"},
            {"name": "Bid Set",               "status": "Already exists"},
            {"name": "Issue Schematic Design","status": "New"},
            {"name": "Issue DD",              "status": "New"},
            {"name": "Discipline Code",       "status": "Conflict (Text vs Yes/No)"},
            {"name": "PM Initials",           "status": "New"},
            {"name": "QC Initials",           "status": "New"},
        ]
        for m in mocks:
            self.import_param_list.Children.Add(self._make_import_row(m))
        self.lbl_import_count.Text = "Mock: %d parameters in file (3 new, 2 existing, 1 conflict)." % len(mocks)

    def _make_import_row(self, m):
        row = Border()
        row.BorderBrush = _brush("#E2E8F0")
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(8, 4, 8, 4)
        row.Background = _brush("#FFFFFF")

        grid = Grid()
        col_cb = ColumnDefinition(); col_cb.Width = GridLength(28)
        col_name = ColumnDefinition(); col_name.Width = GridLength(2, GridUnitType.Star)
        col_status = ColumnDefinition(); col_status.Width = GridLength(2, GridUnitType.Star); col_status.MinWidth = 160
        for col in (col_cb, col_name, col_status):
            grid.ColumnDefinitions.Add(col)

        cb = CheckBox()
        cb.VerticalAlignment = VerticalAlignment.Center
        cb.IsChecked = (m["status"] == "New")
        Grid.SetColumn(cb, 0)
        grid.Children.Add(cb)

        name_block = _text(m["name"], weight="semibold", color="#2D3748")
        Grid.SetColumn(name_block, 1)
        grid.Children.Add(name_block)

        # Color the status badge to match its meaning
        status = m["status"]
        if status == "New":
            badge_color, text_color = "#C6F6D5", "#22543D"
        elif status.startswith("Already"):
            badge_color, text_color = "#EDF2F7", "#4A5568"
        else:
            badge_color, text_color = "#FED7D7", "#742A2A"

        badge = Border()
        badge.Background = _brush(badge_color)
        badge.CornerRadius = CornerRadius(3)
        badge.Padding = Thickness(8, 2, 8, 2)
        badge.HorizontalAlignment = HorizontalAlignment.Left
        badge.Child = _text(status, color=text_color, size=11, weight="semibold")
        Grid.SetColumn(badge, 2)
        grid.Children.Add(badge)

        row.Child = grid
        return row


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    ParametersManagementForm().ShowDialog()


if __name__ == "__main__":
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
