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
    BuiltInParameter,
)

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
# Helpers (kept lean for iter-1; expand in iter-2 when wiring API)
# ===========================================================================

def eid_int(eid):
    """Revit 2024+ safe ElementId int read."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


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

        self._populate_combos()
        self._build_chips()
        self._wire_events()
        self._load_templates()
        self._render_template_list()
        self._update_mode()

    # ---- populate value combos with mock options -------------------------

    def _populate_combos(self):
        def fill(combo, items, default_idx=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default_idx, combo.Items.Count - 1)

        fill(self.cmb_scale,         MOCK_SCALES,        default_idx=10)  # 1/8"=1'
        fill(self.cmb_display_model, MOCK_DISPLAY_MODEL, default_idx=0)
        fill(self.cmb_detail,        MOCK_DETAIL_LEVEL,  default_idx=0)
        fill(self.cmb_parts,         MOCK_PARTS_VIS,     default_idx=0)
        fill(self.cmb_phase_filter,  MOCK_PHASE_FILTERS, default_idx=0)
        fill(self.cmb_discipline,    MOCK_DISCIPLINE,    default_idx=0)
        fill(self.cmb_hidden_lines,  MOCK_HIDDEN_LINES,  default_idx=1)
        fill(self.cmb_color_loc,     MOCK_COLOR_LOC,     default_idx=0)
        fill(self.cmb_color_scheme,  MOCK_COLOR_SCHEMES, default_idx=0)
        fill(self.cmb_underlay,      MOCK_UNDERLAY,      default_idx=1)
        fill(self.cmb_orientation,   MOCK_ORIENTATION,   default_idx=0)
        fill(self.cmb_depth_clip,    MOCK_DEPTH_CLIP,    default_idx=0)

        # Default-include checkboxes
        for name in ("chk_inc_scale", "chk_inc_display_model", "chk_inc_detail",
                     "chk_inc_parts", "chk_inc_vg_model", "chk_inc_vg_annotation",
                     "chk_inc_vg_analytical", "chk_inc_vg_import",
                     "chk_inc_vg_filters", "chk_inc_vg_links",
                     "chk_inc_gd_model_display", "chk_inc_gd_shadows",
                     "chk_inc_gd_sketchy", "chk_inc_gd_lighting",
                     "chk_inc_gd_photo", "chk_inc_gd_background",
                     "chk_inc_phase_filter", "chk_inc_discipline",
                     "chk_inc_hidden_lines", "chk_inc_color_loc",
                     "chk_inc_color_scheme", "chk_inc_sys_color",
                     "chk_inc_underlay", "chk_inc_view_range",
                     "chk_inc_orientation", "chk_inc_depth_clip"):
            try:
                getattr(self, name).IsChecked = True
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
        self._render_template_list()

    # ---- wire events -----------------------------------------------------

    def _wire_events(self):
        self.btn_close.Click  += self._on_close
        self.btn_apply.Click  += self._on_apply

        self.btn_tpl_refresh.Click += self._on_tpl_refresh
        self.btn_tpl_all.Click     += self._on_tpl_all
        self.btn_tpl_none.Click    += self._on_tpl_none
        self.txt_tpl_search.TextChanged += self._on_tpl_search

        self.chk_show_analytical.Click += self._on_toggle_analytical

        # V/G Edit buttons - launch sub-dialogs
        self.btn_vg_model.Click      += lambda s, e: self._open_vg_categories("Model")
        self.btn_vg_annotation.Click += lambda s, e: self._open_vg_categories("Annotation")
        self.btn_vg_analytical.Click += lambda s, e: self._open_vg_categories("Analytical")
        self.btn_vg_import.Click     += lambda s, e: self._open_vg_imports()
        self.btn_vg_filters.Click    += lambda s, e: self._open_vg_filters()
        self.btn_vg_links.Click      += lambda s, e: self._open_vg_links()

        # Graphic Display Edit buttons - placeholders
        for btn_name, label in (
            ("btn_gd_model_display", "Model Display"),
            ("btn_gd_shadows",       "Shadows"),
            ("btn_gd_sketchy",       "Sketchy Lines"),
            ("btn_gd_lighting",      "Lighting"),
            ("btn_gd_photo",         "Photographic Exposure"),
            ("btn_gd_background",    "Background"),
            ("btn_view_range",       "View Range"),
            ("btn_sys_color",        "System Color Schemes"),
        ):
            try:
                btn = getattr(self, btn_name)
                btn.Click += self._make_placeholder_handler(label)
            except Exception:
                pass

        # When scale combo changes, update Scale Value: 1: derived
        self.cmb_scale.SelectionChanged += self._on_scale_changed

    def _make_placeholder_handler(self, name):
        def handler(sender, args):
            forms.alert(
                "Sub-dialog for {0} arrives in iteration 5 (Graphic Display "
                "bundle).\n\nIteration 1 only ships the V/G sub-dialogs "
                "(Model / Annotation / Analytical / Import / Filters / RVT "
                "Links) so we can lock down the layout for those first."
                .format(name),
                title="View Templates Manager - Iter 1 preview",
            )
        return handler

    def _on_scale_changed(self, sender, args):
        idx = self.cmb_scale.SelectedIndex
        if idx < 0 or idx >= len(MOCK_SCALES):
            self.txt_scale_value.Text = "-"
            return
        # Trivial mapping: index in MOCK_SCALES -> denominator hint
        denom_lookup = ['384', '192', '128', '96', '64', '48', '32', '24',
                        '16', '12', '8', '4', '2', '1', 'Custom']
        try:
            self.txt_scale_value.Text = denom_lookup[idx]
        except IndexError:
            self.txt_scale_value.Text = "-"

    # ---- footer ----------------------------------------------------------

    def _on_close(self, sender, args):
        self.Close()

    def _on_apply(self, sender, args):
        # Disabled in iter-1 but provide a friendly message just in case
        forms.alert("Iteration 1 is a UI preview - Apply lands in iteration 2.",
                    title="View Templates Manager")

    # ---- left-side template list ----------------------------------------

    def _on_tpl_refresh(self, sender, args):
        self._load_templates()
        self._render_template_list()
        self._update_mode()

    def _on_tpl_all(self, sender, args):
        for it in self._visible_items():
            if it.checkbox is not None:
                it.checkbox.IsChecked = True
        self._update_mode()

    def _on_tpl_none(self, sender, args):
        for it in self._all_templates:
            if it.checkbox is not None:
                it.checkbox.IsChecked = False
        self._update_mode()

    def _on_tpl_search(self, sender, args):
        self._search_text = (self.txt_tpl_search.Text or "").strip().lower()
        self._render_template_list()

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
        chk.IsChecked = False
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
        """Checkbox toggle: if the checkbox's row is part of a multi-row
        highlight set, propagate the new check state to every highlighted
        row. Otherwise behaves as a simple per-row toggle."""
        target_item = getattr(sender, "Tag", None)
        if target_item is None or not isinstance(target_item, TemplateItem):
            self._update_mode()
            return

        new_state = bool(sender.IsChecked)
        # Only propagate if the clicked row is itself highlighted AND the
        # highlight set has 2+ items. Avoids surprising behavior when the
        # user just wants to tick a single arbitrary row.
        if (target_item.eid_int in self._highlighted_eids and
                len(self._highlighted_eids) > 1):
            for it in self._all_templates:
                if (it.eid_int in self._highlighted_eids and
                        it is not target_item and
                        it.checkbox is not None):
                    it.checkbox.IsChecked = new_state

        self._update_mode()

    # ---- single / bulk mode plumbing ------------------------------------

    def _checked(self):
        return [it for it in self._all_templates
                if it.checkbox is not None and bool(it.checkbox.IsChecked)]

    def _update_mode(self):
        n = len(self._checked())
        self._set_apply_column_visible(n >= 2)
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
        else:
            checked = self._checked()
            self.bnr_bulk.Visibility = Visibility.Visible
            self.txt_bulk_banner.Text = (
                "Bulk mode: editing {0} templates - check the Apply column "
                "for each parameter you want to propagate.".format(n)
            )
            self.txt_right_title.Text = "Bulk edit ({0} templates)".format(n)
            self.txt_right_subtitle.Text = "Values shown are mock; check Apply boxes to mark which to propagate."
            total_usage = sum(it.usage_count for it in checked)
            self.txt_views_using.Text = "Total views affected: {0}".format(total_usage)
            self._set_param_table_enabled(True)
            self._toggle_plan_section(any(is_plan_view_type(it.view_type) for it in checked))
            self._show_varies_in_values()

    def _set_apply_column_visible(self, visible):
        # Toggle the Apply header column
        try:
            self.col_apply_hdr.Width = self._grid_length(80) if visible else self._grid_length(0)
        except Exception:
            pass
        try:
            self.hdr_apply.Visibility = Visibility.Visible if visible else Visibility.Collapsed
        except Exception:
            pass
        # Each row's Apply checkbox is in column 3 with fixed 80px width;
        # we just toggle visibility on each known checkbox.
        for chk_name in [n for n in dir(self) if n.startswith("chk_app_")]:
            try:
                chk = getattr(self, chk_name)
                chk.Visibility = Visibility.Visible if visible else Visibility.Collapsed
                chk.IsChecked = False
            except Exception:
                pass

    def _set_param_table_enabled(self, enabled):
        self.scr_params.IsEnabled = bool(enabled)
        opacity = 1.0 if enabled else 0.55
        self.scr_params.Opacity = opacity

    def _toggle_plan_section(self, is_plan):
        try:
            self.exp_plan.Visibility = Visibility.Visible if is_plan else Visibility.Collapsed
        except Exception:
            pass

    def _show_varies_in_values(self):
        # In bulk mode, paint Value combos with a subtle "(varies)" hint.
        # For iter-1 we tag with tooltip and keep the visible selection.
        for cmb_name in ("cmb_scale", "cmb_display_model", "cmb_detail",
                         "cmb_parts", "cmb_phase_filter", "cmb_discipline",
                         "cmb_hidden_lines", "cmb_color_loc", "cmb_color_scheme",
                         "cmb_underlay", "cmb_orientation", "cmb_depth_clip"):
            try:
                cmb = getattr(self, cmb_name)
                cmb.ToolTip = "Templates have differing values - pick one and tick Apply to propagate."
            except Exception:
                pass

    # ---- sub-dialog launchers -------------------------------------------

    def _selected_template_names(self):
        return [it.name for it in self._checked()]

    def _open_vg_categories(self, kind):
        names = self._selected_template_names()
        if not names:
            forms.alert("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        dlg = VgCategoriesDialog(kind=kind, template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

    def _open_vg_imports(self):
        names = self._selected_template_names()
        if not names:
            forms.alert("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        dlg = VgImportsDialog(template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

    def _open_vg_filters(self):
        names = self._selected_template_names()
        if not names:
            forms.alert("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        dlg = VgFiltersDialog(template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

    def _open_vg_links(self):
        names = self._selected_template_names()
        if not names:
            forms.alert("Pick a template (or check 2+ for bulk edit) first.",
                        title="View Templates Manager")
            return
        dlg = VgLinksDialog(template_names=names)
        dlg.Owner = self
        dlg.ShowDialog()

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
    def __init__(self, kind="Model", template_names=None):
        forms.WPFWindow.__init__(self, VG_CAT_XAML)
        self._kind  = kind
        self._names = template_names or []
        self._populate_title()
        self._populate_lookups()
        self._populate_categories()
        self._wire_events()

    def _populate_title(self):
        if self._kind == "Annotation":
            self.txt_dialog_title.Text = "V/G Overrides - Annotation Categories"
            self.txt_dialog_sub.Text = (
                "Toggle visibility and override graphics for each annotation "
                "category - same as Revit's V/G dialog.")
        elif self._kind == "Analytical":
            self.txt_dialog_title.Text = "V/G Overrides - Analytical Model Categories"
            self.txt_dialog_sub.Text = (
                "Analytical category overrides - typically only useful for "
                "structural workflows.")
        else:
            self.txt_dialog_title.Text = "V/G Overrides - Model Categories"
            self.txt_dialog_sub.Text = (
                "Toggle visibility and override graphics for each model "
                "category - same as Revit's V/G dialog.")
        # Status: hint at which templates this affects
        if len(self._names) > 1:
            self.txt_dialog_status.Text = (
                "Iter 1 preview - changes won't write back yet. Will affect "
                "{0} templates: {1}".format(
                    len(self._names),
                    ", ".join(self._names[:3]) +
                    (", ..." if len(self._names) > 3 else "")
                ))
        elif self._names:
            self.txt_dialog_status.Text = (
                "Iter 1 preview - changes won't write back yet. Editing "
                "template: {0}".format(self._names[0]))

    def _populate_lookups(self):
        def fill(combo, items, default=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default, combo.Items.Count - 1)

        fill(self.cmb_detail_level,    ["<By View>"] + MOCK_DETAIL_LEVEL)
        fill(self.cmb_proj_weight,     MOCK_LINE_WEIGHTS)
        fill(self.cmb_proj_pattern,    MOCK_LINE_PATTERNS)
        fill(self.cmb_cut_weight,      MOCK_LINE_WEIGHTS)
        fill(self.cmb_cut_pattern,     MOCK_LINE_PATTERNS)
        fill(self.cmb_surf_fg_pattern, MOCK_PATTERNS)
        fill(self.cmb_surf_bg_pattern, MOCK_PATTERNS)
        fill(self.cmb_cut_fg_pattern,  MOCK_PATTERNS)
        fill(self.cmb_cut_bg_pattern,  MOCK_PATTERNS)

        # Hook transparency slider value display
        self.sld_transparency.ValueChanged += self._on_transparency

    def _on_transparency(self, sender, args):
        self.txt_transparency_val.Text = "{0}%".format(int(self.sld_transparency.Value))

    def _populate_categories(self):
        if self._kind == "Annotation":
            cats = MOCK_ANNOTATION_CATEGORIES
        elif self._kind == "Analytical":
            cats = MOCK_ANALYTICAL_CATEGORIES
        else:
            cats = MOCK_MODEL_CATEGORIES
        self.pnl_categories.Children.Clear()
        for cat_name in cats:
            row = _build_category_row(cat_name, on_click=self._on_cat_click)
            self.pnl_categories.Children.Add(row)

    def _on_cat_click(self, cat_name):
        self.txt_selected_cat.Text = cat_name

    def _wire_events(self):
        self.btn_dlg_cancel.Click += lambda s, e: self.Close()
        self.btn_dlg_ok.Click     += lambda s, e: self.Close()


# ===========================================================================
# SUB-DIALOG: V/G Imports (CAD link/layer tree)
# ===========================================================================

class VgImportsDialog(forms.WPFWindow):
    def __init__(self, template_names=None):
        forms.WPFWindow.__init__(self, VG_IMP_XAML)
        self._names = template_names or []
        self._populate_lookups()
        self._populate_imports()
        self._wire_events()
        if len(self._names) > 1:
            self.txt_imp_status.Text = (
                "Iter 1 preview - bulk hide/show layers will affect {0} "
                "templates once iter 3 wires it.".format(len(self._names)))

    def _populate_lookups(self):
        def fill(combo, items, default=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default, combo.Items.Count - 1)
        fill(self.cmb_imp_weight,  MOCK_LINE_WEIGHTS)
        fill(self.cmb_imp_pattern, MOCK_LINE_PATTERNS)
        self.sld_imp_transparency.ValueChanged += self._on_transparency

    def _on_transparency(self, sender, args):
        self.txt_imp_transparency_val.Text = "{0}%".format(int(self.sld_imp_transparency.Value))

    def _populate_imports(self):
        self.pnl_imports.Children.Clear()
        for cad in MOCK_CAD_IMPORTS:
            cad_row = _build_import_link_row(
                cad["name"], on_click=lambda n=cad["name"]: self._on_imp_select(n))
            self.pnl_imports.Children.Add(cad_row["row"])
            # Layer rows (collapsed by default)
            layer_panel = StackPanel()
            layer_panel.Visibility = Visibility.Collapsed
            for layer_name in cad["layers"]:
                lr = _build_import_layer_row(
                    layer_name, parent_cad=cad["name"],
                    on_click=lambda n="{0} > {1}".format(cad["name"], layer_name): self._on_imp_select(n))
                layer_panel.Children.Add(lr)
            self.pnl_imports.Children.Add(layer_panel)
            cad_row["expander_btn"].Click += self._make_layer_toggle(cad_row["expander_btn"], layer_panel)

    def _make_layer_toggle(self, btn, panel):
        def handler(sender, args):
            if panel.Visibility == Visibility.Collapsed:
                panel.Visibility = Visibility.Visible
                btn.Content = u"▾"  # ▾
            else:
                panel.Visibility = Visibility.Collapsed
                btn.Content = u"▸"  # ▸
        return handler

    def _on_imp_select(self, name):
        self.txt_imp_selected.Text = name

    def _wire_events(self):
        self.btn_imp_cancel.Click       += lambda s, e: self.Close()
        self.btn_imp_ok.Click           += lambda s, e: self.Close()
        self.btn_imp_show_all.Click     += self._on_show_all
        self.btn_imp_hide_all.Click     += self._on_hide_all
        self.btn_imp_expand_all.Click   += self._on_expand_all
        self.btn_imp_collapse_all.Click += self._on_collapse_all

    def _on_show_all(self, sender, args):
        # Iter-1: visual feedback only
        self.txt_imp_status.Text = "Show All clicked - iter 3 will write to all selected templates."

    def _on_hide_all(self, sender, args):
        self.txt_imp_status.Text = "Hide All clicked - iter 3 will write to all selected templates."

    def _on_expand_all(self, sender, args):
        # Walk children: every StackPanel that's collapsed becomes visible
        for child in list(self.pnl_imports.Children):
            if isinstance(child, StackPanel):
                child.Visibility = Visibility.Visible

    def _on_collapse_all(self, sender, args):
        for child in list(self.pnl_imports.Children):
            if isinstance(child, StackPanel):
                child.Visibility = Visibility.Collapsed


# ===========================================================================
# SUB-DIALOG: V/G Filters
# ===========================================================================

class VgFiltersDialog(forms.WPFWindow):
    def __init__(self, template_names=None):
        forms.WPFWindow.__init__(self, VG_FLT_XAML)
        self._names = template_names or []
        self._populate_lookups()
        self._populate_filters()
        self._wire_events()
        if len(self._names) > 1:
            self.txt_flt_status.Text = (
                "Iter 1 preview - filter changes will apply to {0} templates "
                "once iter 3 wires it.".format(len(self._names)))

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
        for f in MOCK_FILTERS:
            row = _build_filter_row(
                f["name"], f["enabled"], f["visible"],
                on_click=lambda n=f["name"]: self._on_flt_select(n))
            self.pnl_filters.Children.Add(row)

    def _on_flt_select(self, name):
        self.txt_flt_selected.Text = name

    def _wire_events(self):
        self.btn_flt_cancel.Click += lambda s, e: self.Close()
        self.btn_flt_ok.Click     += lambda s, e: self.Close()
        self.btn_flt_add.Click    += self._on_add
        self.btn_flt_remove.Click += self._on_remove

    def _on_add(self, sender, args):
        forms.alert(
            "Filter picker arrives in iter 3. For now, the list shows mock "
            "filters so you can see how the layout works.",
            title="V/G Overrides - Filters")

    def _on_remove(self, sender, args):
        self.txt_flt_status.Text = "Remove clicked - iter 3 will detach the filter from selected templates."


# ===========================================================================
# SUB-DIALOG: RVT Link Display Settings
# ===========================================================================

class VgLinksDialog(forms.WPFWindow):
    def __init__(self, template_names=None):
        forms.WPFWindow.__init__(self, VG_LNK_XAML)
        self._names = template_names or []
        self._populate_link_picker()
        self._populate_inherit_combos()
        self._wire_events()

    def _populate_link_picker(self):
        self.cmb_link_picker.Items.Clear()
        for link in MOCK_RVT_LINKS:
            self.cmb_link_picker.Items.Add(link["name"])
        if self.cmb_link_picker.Items.Count > 0:
            self.cmb_link_picker.SelectedIndex = 0

    def _populate_inherit_combos(self):
        def fill(combo, items, default=0):
            combo.Items.Clear()
            for s in items:
                combo.Items.Add(s)
            if combo.Items.Count > 0:
                combo.SelectedIndex = min(default, combo.Items.Count - 1)
        fill(self.cmb_lnk_linkedview,  MOCK_LINK_VIEWS)
        fill(self.cmb_lnk_filters,     MOCK_LINK_INHERIT)
        fill(self.cmb_lnk_viewrange,   MOCK_LINK_INHERIT)
        fill(self.cmb_lnk_phase,       ['<By host view> (New Construction)', 'New Construction', 'Existing'])
        fill(self.cmb_lnk_phasefilter, ['<By host view> (Show All)'] + MOCK_PHASE_FILTERS)
        fill(self.cmb_lnk_detail,      ['<By host view> (Coarse)'] + MOCK_DETAIL_LEVEL)
        fill(self.cmb_lnk_discipline,  ['<By host view> (Architectural)'] + MOCK_DISCIPLINE)
        fill(self.cmb_lnk_colorfill,   MOCK_LINK_INHERIT)
        fill(self.cmb_lnk_objstyles,   MOCK_LINK_INHERIT_M)
        fill(self.cmb_lnk_nested,      MOCK_LINK_NESTED)

    def _wire_events(self):
        self.btn_lnk_cancel.Click  += lambda s, e: self.Close()
        self.btn_lnk_ok.Click      += lambda s, e: self.Close()
        self.btn_open_native.Click += self._on_open_native
        self.rb_link_byhost.Checked  += self._on_mode_changed
        self.rb_link_bylinked.Checked += self._on_mode_changed

    def _on_mode_changed(self, sender, args):
        # Iter-1 visual feedback only
        if self.rb_link_byhost.IsChecked:
            self.txt_lnk_status.Text = (
                "Iter 1 preview - 'By host view' selected. Per-aspect dropdowns will "
                "be greyed out and inherit from this template.")
        elif self.rb_link_bylinked.IsChecked:
            self.txt_lnk_status.Text = (
                "Iter 1 preview - 'By linked view' selected. Iter 4 will let you pick "
                "a specific view from the linked file.")

    def _on_open_native(self, sender, args):
        forms.alert(
            "Iter 4 will add an escape hatch that posts Revit's native View "
            "Template / RVT Link Display Settings command for the currently "
            "selected template, so you can configure Custom mode there.\n\n"
            "Custom mode (per-category checklists) cannot be set via the "
            "Revit API - that's a documented Autodesk limitation.",
            title="Open in Revit's native dialog")


# ===========================================================================
# Row builders for sub-dialog lists
# ===========================================================================

def _build_category_row(cat_name, on_click=None):
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

    return outer


def _build_import_link_row(cad_name, on_click=None):
    outer = Border()
    outer.Padding = Thickness(0, 2, 0, 2)
    outer.BorderBrush = SolidColorBrush(Color.FromRgb(0xE2, 0xE8, 0xF0))
    outer.BorderThickness = Thickness(0, 0, 0, 1)
    outer.Background = SolidColorBrush(Color.FromRgb(0xF7, 0xFA, 0xFC))
    outer.Cursor = Cursors.Hand

    grid = Grid()
    from System.Windows import GridLength, GridUnitType
    widths = [20, 40, None, 50, 50]
    for w in widths:
        c = ColumnDefinition()
        if w is None:
            c.Width = GridLength(1.0, GridUnitType.Star)
        else:
            c.Width = GridLength(float(w))
        grid.ColumnDefinitions.Add(c)

    expander_btn = Button()
    expander_btn.Content = u"▸"  # ▸
    expander_btn.Background = SolidColorBrush(Color.FromRgb(0xF7, 0xFA, 0xFC))
    expander_btn.BorderThickness = Thickness(0)
    expander_btn.Cursor = Cursors.Hand
    expander_btn.FontSize = 11
    expander_btn.Padding = Thickness(0)
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

    return {"row": outer, "expander_btn": expander_btn, "chk_show": chk_show}


def _build_import_layer_row(layer_name, parent_cad, on_click=None):
    outer = Border()
    outer.Padding = Thickness(0, 2, 0, 2)
    outer.BorderBrush = SolidColorBrush(Color.FromRgb(0xF1, 0xF5, 0xF9))
    outer.BorderThickness = Thickness(0, 0, 0, 1)
    outer.Background = SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF))
    outer.Cursor = Cursors.Hand

    grid = Grid()
    from System.Windows import GridLength, GridUnitType
    widths = [20, 40, None, 50, 50]
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

    if on_click is not None:
        def handler(sender, args):
            on_click()
        outer.MouseLeftButtonDown += handler

    return outer


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

    return outer


def _semi_bold():
    from System.Windows import FontWeights
    return FontWeights.SemiBold


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    if doc is None:
        forms.alert("No active Revit document.", exitscript=True)
        return
    ViewTemplatesManagerForm().ShowDialog()


if __name__ == "__main__":
    main()
