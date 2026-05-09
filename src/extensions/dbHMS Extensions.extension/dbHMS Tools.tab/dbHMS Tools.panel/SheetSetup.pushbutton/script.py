# -*- coding: utf-8 -*-
"""Sheet Setup - one-click MEP project sheet/view scaffolding.

Pick a title block, scope box, the levels in scope, and the disciplines/plan
types you want. The tool creates a floor or ceiling plan per (level x plan
type) combo, applies the scope box and the view template you've chosen, sets
browser organization parameters, creates a sheet for each (auto-numbered
e.g. E101, E102, E201, M101, P101, P201, FP101, T101...), and places the new
view as a viewport on its sheet at a consistent location.

Defaults live in config.json next to this script. Edit them in the form and
hit "Save as Default" to persist firm-standard settings.
"""

__title__ = 'Sheet\nSetup'
__author__ = 'Nathaniel'

import os
import re
import json
import copy
import codecs
import hashlib
import traceback

from pyrevit import revit, DB, forms, script
import dbhms_ui
import dbhms_telemetry

# WPF / .NET imports for the dynamic form controls
import clr  # noqa: F401
from System.Windows import Thickness, Visibility, HorizontalAlignment, VerticalAlignment, TextTrimming
from System.Windows.Controls import (
    CheckBox, ComboBox, ComboBoxItem, TextBox, TextBlock, StackPanel, Button,
    Border, Grid, ColumnDefinition, ScrollViewer, Orientation, ScrollBarVisibility
)
from System.Windows.Media import Brushes, SolidColorBrush, Color
from System.Windows import GridLength, GridUnitType


# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
FORM_XAML = os.path.join(SCRIPT_DIR, 'ModelSetupForm.xaml')
PREVIEW_XAML = os.path.join(SCRIPT_DIR, 'PreviewForm.xaml')
PLAN_SETTINGS_XAML = os.path.join(SCRIPT_DIR, 'PlanTypeSettingsForm.xaml')

doc = revit.doc
output = script.get_output()

NONE_TEMPLATE_LABEL = '<None - no template>'
NONE_SCOPEBOX_LABEL = '<None>'


# Per-project state is persisted to disk so the form's last-known config
# (disciplines, level labels, popup overrides, picker selections) survives
# across Revit sessions and engine resets. Files live under:
#   %APPDATA%\dbHMS\SheetSetup\states\<md5(doc-key)>.json
# An in-memory cache keeps reads fast within a single session.
_PROJECT_STATE_CACHE = {}


def _state_key():
    try:
        return doc.PathName or doc.Title
    except Exception:
        return None


def _state_dir():
    appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
    folder = os.path.join(appdata, 'dbHMS', 'SheetSetup', 'states')
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except Exception:
            return None
    return folder


def _state_file_path():
    key = _state_key()
    if not key:
        return None
    folder = _state_dir()
    if not folder:
        return None
    try:
        digest = hashlib.md5(key.encode('utf-8')).hexdigest()
    except Exception:
        digest = hashlib.md5(repr(key).encode('utf-8')).hexdigest()
    return os.path.join(folder, '{}.json'.format(digest))


def _save_project_state(cfg):
    key = _state_key()
    if key is None:
        return
    snapshot = copy.deepcopy(cfg)
    _PROJECT_STATE_CACHE[key] = snapshot
    path = _state_file_path()
    if not path:
        return
    try:
        # Tag the file so a human can identify which project it belongs to.
        snapshot.setdefault('_project_key', key)
        with codecs.open(path, 'w', 'utf-8') as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # never fail the run because persistence hiccupped


def _load_project_state():
    key = _state_key()
    if key is None:
        return None
    if key in _PROJECT_STATE_CACHE:
        return copy.deepcopy(_PROJECT_STATE_CACHE[key])
    path = _state_file_path()
    if path and os.path.isfile(path):
        try:
            with codecs.open(path, 'r', 'utf-8') as f:
                state = json.load(f)
            _PROJECT_STATE_CACHE[key] = copy.deepcopy(state)
            return state
        except Exception:
            return None
    return None


def _load_config():
    with codecs.open(CONFIG_PATH, 'r', 'utf-8') as f:
        return json.load(f)


def _save_config(cfg):
    with codecs.open(CONFIG_PATH, 'w', 'utf-8') as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def _minimal_default_disciplines():
    """The fresh-project starting layout: one sample discipline with two
    sample plan types. The "Full Setup" button restores the full multi-
    discipline default from config.json."""
    return [{
        'code': 'X',
        'name': 'Sample Discipline',
        'enabled': True,
        'plan_types': [
            {
                'name': 'SAMPLE PLAN 1',
                'view_family': 'FloorPlan',
                'view_template_name': None,
                'enabled': True,
                'level_filter_uniqueids': None,
                'sheet_number_prefix_override': None,
                'sheet_number_series_override': '1',
                'sheet_number_level_override': None,
                'sheet_number_suffix_override': None,
            },
            {
                'name': 'SAMPLE PLAN 2',
                'view_family': 'FloorPlan',
                'view_template_name': None,
                'enabled': True,
                'level_filter_uniqueids': None,
                'sheet_number_prefix_override': None,
                'sheet_number_series_override': '2',
                'sheet_number_level_override': None,
                'sheet_number_suffix_override': None,
            },
        ],
    }]


# --------------------------------------------------------------------------
# Project data gatherers
# --------------------------------------------------------------------------

def _get_levels():
    levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
    # Building-story levels first, then non-building-story
    def _is_story(l):
        p = l.get_Parameter(DB.BuiltInParameter.LEVEL_IS_BUILDING_STORY)
        return bool(p.AsInteger()) if p else True
    return sorted(levels, key=lambda l: (not _is_story(l), l.Elevation, l.Name))


def _get_title_blocks():
    return sorted(
        DB.FilteredElementCollector(doc)
          .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
          .WhereElementIsElementType()
          .ToElements(),
        key=lambda t: '{} : {}'.format(
            t.Family.Name, DB.Element.Name.GetValue(t))
    )


def _title_block_label(tb):
    return '{} : {}'.format(tb.Family.Name, DB.Element.Name.GetValue(tb))


def _get_scope_boxes():
    return sorted(
        DB.FilteredElementCollector(doc)
          .OfCategory(DB.BuiltInCategory.OST_VolumeOfInterest)
          .WhereElementIsNotElementType()
          .ToElements(),
        key=lambda s: s.Name
    )


def _get_view_family_types():
    """Return dict of {family_string: [ViewFamilyType, ...]}."""
    out = {'FloorPlan': [], 'CeilingPlan': []}
    for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
        if vft.ViewFamily == DB.ViewFamily.FloorPlan:
            out['FloorPlan'].append(vft)
        elif vft.ViewFamily == DB.ViewFamily.CeilingPlan:
            out['CeilingPlan'].append(vft)
    for k in out:
        out[k].sort(key=lambda v: DB.Element.Name.GetValue(v))
    return out


def _get_view_templates():
    """Return dict of {ViewType: [view_template, ...]}."""
    out = {}
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if not v.IsTemplate:
            continue
        out.setdefault(v.ViewType, []).append(v)
    for k in out:
        out[k].sort(key=lambda v: v.Name)
    return out


_FAMILY_TO_VIEWTYPE = {
    'FloorPlan': DB.ViewType.FloorPlan,
    'CeilingPlan': DB.ViewType.CeilingPlan,
}


def _get_room_tag_types():
    """Return all loaded RoomTag FamilySymbols sorted by family : type."""
    return sorted(
        DB.FilteredElementCollector(doc)
          .OfCategory(DB.BuiltInCategory.OST_RoomTags)
          .WhereElementIsElementType()
          .ToElements(),
        key=lambda t: '{} : {}'.format(
            t.Family.Name, DB.Element.Name.GetValue(t))
    )


def _room_tag_label(tag_type):
    return '{} : {}'.format(
        tag_type.Family.Name, DB.Element.Name.GetValue(tag_type))


def _get_viewport_types():
    """Find every viewport type in the project. Tries several strategies
    because different Revit versions / project configurations expose viewport
    types differently."""
    out = {}

    def _add(t):
        if t is None:
            return False
        try:
            key = t.Id.IntegerValue
        except Exception:
            return False
        if key in out:
            return False
        out[key] = t
        return True

    OST_INT = int(DB.BuiltInCategory.OST_Viewports)

    def _try(fn):
        try:
            fn()
        except Exception:
            pass

    # Strategy 1: standard category collector
    def _s1():
        col = (DB.FilteredElementCollector(doc)
                 .OfCategory(DB.BuiltInCategory.OST_Viewports)
                 .WhereElementIsElementType())
        for t in col.ToElements():
            _add(t)
    _try(_s1)

    # Strategy 2: OfCategoryId variant
    def _s2():
        cat_id = DB.ElementId(OST_INT)
        col = (DB.FilteredElementCollector(doc)
                 .OfCategoryId(cat_id)
                 .WhereElementIsElementType())
        for t in col.ToElements():
            _add(t)
    _try(_s2)

    # Strategy 3: ElementCategoryFilter via LogicalAnd
    def _s3():
        cat_filter = DB.ElementCategoryFilter(DB.BuiltInCategory.OST_Viewports)
        col = (DB.FilteredElementCollector(doc)
                 .WherePasses(cat_filter)
                 .WhereElementIsElementType())
        for t in col.ToElements():
            _add(t)
    _try(_s3)

    # Strategy 4: scan ALL element types, post-filter by category id or family name
    def _s4():
        col = DB.FilteredElementCollector(doc).WhereElementIsElementType()
        for t in col.ToElements():
            try:
                cat = getattr(t, 'Category', None)
                if cat is not None and cat.Id.IntegerValue == OST_INT:
                    _add(t); continue
                fam = getattr(t, 'FamilyName', '') or ''
                if fam == 'Viewport':
                    _add(t)
            except Exception:
                pass
    _try(_s4)

    # Strategy 5: harvest from existing viewport instances
    def _s5():
        for vp in (DB.FilteredElementCollector(doc)
                     .OfClass(DB.Viewport)
                     .ToElements()):
            try:
                tid = vp.GetTypeId()
                if tid is not None and tid != DB.ElementId.InvalidElementId:
                    _add(doc.GetElement(tid))
            except Exception:
                pass
    _try(_s5)

    # Strategy 6: project default viewport type
    def _s6():
        try:
            eid = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.ViewportType)
        except Exception:
            eid = None
        if eid is not None and eid != DB.ElementId.InvalidElementId:
            _add(doc.GetElement(eid))
    _try(_s6)

    return sorted(out.values(),
                  key=lambda t: (DB.Element.Name.GetValue(t) or '').lower())


def _viewport_type_label(vp_type):
    return DB.Element.Name.GetValue(vp_type) or '<unnamed>'


# --------------------------------------------------------------------------
# Main configuration form
# --------------------------------------------------------------------------

class ModelSetupForm(forms.WPFWindow):
    """Big WPF form: title block / scope box / levels / disciplines."""

    def __init__(self, cfg, project_data):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._cfg = copy.deepcopy(cfg)
        self._proj = project_data
        self.confirmed = False

        # State accumulators -------------------------------------------------
        self._level_rows = []                 # [{level, cb, label_box}, ...]
        self._discipline_widgets = []         # see _build_discipline_card

        # Populate ----------------------------------------------------------
        self._populate_title_blocks()
        self._populate_viewport_types()
        self._populate_scope_boxes()
        self._populate_room_tag_types()
        self._populate_levels()
        self._populate_disciplines()

        # Wire buttons -------------------------------------------------------
        self.btn_full_setup.Click += self._on_full_setup
        self.btn_clear.Click += self._on_clear
        self.btn_cancel.Click += self._on_cancel
        self.btn_run.Click += self._on_run
        self.btn_levels_all.Click += self._on_levels_all
        self.btn_levels_none.Click += self._on_levels_none

    # ---- Population ------------------------------------------------------

    def _populate_title_blocks(self):
        self.cmb_titleblock.Items.Clear()
        default = self._cfg.get('default_title_block_name')
        selected_index = 0
        for i, tb in enumerate(self._proj['title_blocks']):
            label = _title_block_label(tb)
            item = ComboBoxItem()
            item.Content = label
            item.Tag = tb
            self.cmb_titleblock.Items.Add(item)
            if default and default == label:
                selected_index = i
        if self.cmb_titleblock.Items.Count > 0:
            self.cmb_titleblock.SelectedIndex = selected_index

    def _populate_viewport_types(self):
        """Viewport type dropdown: <Default> + every viewport type found."""
        self.cmb_viewport_type.Items.Clear()
        types = self._proj.get('viewport_types', []) or []
        none_item = ComboBoxItem()
        none_item.Content = '<Default - whatever Revit uses>'
        none_item.Tag = None
        self.cmb_viewport_type.Items.Add(none_item)
        default = self._cfg.get('default_viewport_type_name')
        sel = 0
        for i, vt in enumerate(types):
            label = _viewport_type_label(vt)
            it = ComboBoxItem()
            it.Content = label
            it.Tag = vt
            self.cmb_viewport_type.Items.Add(it)
            if default and default == label:
                sel = i + 1
        self.cmb_viewport_type.SelectedIndex = sel

    def _populate_scope_boxes(self):
        self.cmb_scopebox.Items.Clear()
        none_item = ComboBoxItem()
        none_item.Content = NONE_SCOPEBOX_LABEL
        none_item.Tag = None
        self.cmb_scopebox.Items.Add(none_item)
        default = self._cfg.get('default_scope_box_name')
        selected_index = 0
        for i, sb in enumerate(self._proj['scope_boxes']):
            item = ComboBoxItem()
            item.Content = sb.Name
            item.Tag = sb
            self.cmb_scopebox.Items.Add(item)
            if default and default == sb.Name:
                selected_index = i + 1
        self.cmb_scopebox.SelectedIndex = selected_index

    def _populate_room_tag_types(self):
        """Room tag dropdown: <None> + every loaded room-tag type."""
        self.cmb_roomtag.Items.Clear()
        none_item = ComboBoxItem()
        none_item.Content = '<None - skip room tagging>'
        none_item.Tag = None
        self.cmb_roomtag.Items.Add(none_item)
        default = self._cfg.get('default_room_tag_type_name')
        sel = 0
        for i, tag_type in enumerate(self._proj['room_tag_types']):
            label = _room_tag_label(tag_type)
            it = ComboBoxItem()
            it.Content = label
            it.Tag = tag_type
            self.cmb_roomtag.Items.Add(it)
            if default and default == label:
                sel = i + 1
        self.cmb_roomtag.SelectedIndex = sel

    def _populate_levels(self):
        """One row per level: [checkbox] [Revit name (read-only)] [label override]."""
        self.pnl_levels.Children.Clear()
        self._level_rows = []
        for idx, level in enumerate(self._proj['levels'], start=1):
            row = Grid()
            row.Margin = Thickness(0, 2, 0, 2)
            for w in (22, None, 110):
                cd = ColumnDefinition()
                cd.Width = (GridLength(w, GridUnitType.Pixel)
                            if w else GridLength(1, GridUnitType.Star))
                row.ColumnDefinitions.Add(cd)

            cb = CheckBox()
            cb.VerticalAlignment = VerticalAlignment.Center
            p = level.get_Parameter(DB.BuiltInParameter.LEVEL_IS_BUILDING_STORY)
            cb.IsChecked = bool(p.AsInteger()) if p else True
            Grid.SetColumn(cb, 0)
            row.Children.Add(cb)

            name_tb = TextBlock()
            name_tb.Text = level.Name
            name_tb.ToolTip = level.Name
            name_tb.VerticalAlignment = VerticalAlignment.Center
            name_tb.TextTrimming = TextTrimming.CharacterEllipsis
            name_tb.Margin = Thickness(0, 0, 6, 0)
            Grid.SetColumn(name_tb, 1)
            row.Children.Add(name_tb)

            label_box = TextBox()
            # Default to the actual Revit level name. If we have a cached
            # label override for this level (from this Revit session), use it.
            cached_labels = self._cfg.get('level_labels', {}) or {}
            cached = cached_labels.get(level.UniqueId)
            label_box.Text = cached if cached else (level.Name or '')
            label_box.ToolTip = (
                'How this level reads on sheets and views. '
                'Defaults to the Revit level name; type anything to override '
                "(e.g. ROOF, UNDERGROUND).")
            label_box.HorizontalContentAlignment = HorizontalAlignment.Center
            Grid.SetColumn(label_box, 2)
            row.Children.Add(label_box)

            self.pnl_levels.Children.Add(row)
            self._level_rows.append({
                'level': level,
                'cb': cb,
                'label_box': label_box,
            })

    def _populate_disciplines(self):
        self.pnl_disciplines.Children.Clear()
        self._discipline_widgets = []
        for disc in self._cfg['disciplines']:
            w = self._build_discipline_card(disc)
            self.pnl_disciplines.Children.Add(w['root'])
            self._discipline_widgets.append(w)
        # "+ Add Discipline" button always at the bottom of the panel
        self._add_disc_btn = self._make_add_button(
            '+ Add discipline', self._on_add_discipline)
        self.pnl_disciplines.Children.Add(self._add_disc_btn)

    # ---- Discipline card builder -----------------------------------------

    def _build_discipline_card(self, disc):
        """Build one discipline card. Returns dict of widgets/state."""
        card = Border()
        card.Background = Brushes.White
        card.BorderBrush = SolidColorBrush(Color.FromRgb(226, 232, 240))
        card.BorderThickness = Thickness(1)
        card.CornerRadius = self._corner_radius(4)
        card.Padding = Thickness(10)
        card.Margin = Thickness(0, 0, 0, 8)

        outer = StackPanel()
        card.Child = outer

        # Header: [enabled cbx] [code] [name] [duplicate btn] [remove btn]
        header = Grid()
        header.Margin = Thickness(0, 0, 0, 6)
        for w in (24, 50, None, 28, 28):
            cd = ColumnDefinition()
            cd.Width = (GridLength(w, GridUnitType.Pixel)
                        if w else GridLength(1, GridUnitType.Star))
            header.ColumnDefinitions.Add(cd)

        disc_cb = CheckBox()
        disc_cb.IsChecked = bool(disc.get('enabled', True))
        disc_cb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(disc_cb, 0)
        header.Children.Add(disc_cb)

        code_box = TextBox()
        code_box.Text = disc['code']
        code_box.MaxLength = 4
        code_box.HorizontalContentAlignment = HorizontalAlignment.Center
        code_box.FontWeight = self._fw_bold()
        code_box.Margin = Thickness(0, 0, 8, 0)
        Grid.SetColumn(code_box, 1)
        header.Children.Add(code_box)

        name_box = TextBox()
        name_box.Text = disc['name']
        Grid.SetColumn(name_box, 2)
        header.Children.Add(name_box)

        duplicate_disc_btn = self._make_duplicate_button(
            'Duplicate this discipline (with all plan types and overrides)')
        Grid.SetColumn(duplicate_disc_btn, 3)
        header.Children.Add(duplicate_disc_btn)

        remove_disc_btn = self._make_remove_button('Remove this discipline')
        Grid.SetColumn(remove_disc_btn, 4)
        header.Children.Add(remove_disc_btn)

        outer.Children.Add(header)

        # Plan-type rows panel
        plans_panel = StackPanel()
        plans_panel.Margin = Thickness(24, 4, 0, 0)
        outer.Children.Add(plans_panel)

        # Widget dict (created early so plan-row remove handlers can mutate it)
        widget = {
            'root': card,
            'config': disc,
            'enabled_cb': disc_cb,
            'code_box': code_box,
            'name_box': name_box,
            'plans_panel': plans_panel,
            'plan_rows': [],
        }

        # Build initial plan rows
        for plan in list(disc['plan_types']):
            row_dict = self._build_plan_row(plan, widget)
            plans_panel.Children.Add(row_dict['row_grid'])
            widget['plan_rows'].append(row_dict)

        # "+ Add plan type" footer always at the bottom of plans_panel
        add_plan_btn = self._make_add_button(
            '+ Add plan type', None, small=True)
        def _on_add_plan(sender, e):
            new_plan = {
                'name': 'NEW PLAN TYPE',
                'view_family': 'FloorPlan',
                'view_template_name': None,
                'enabled': True,
            }
            disc['plan_types'].append(new_plan)
            row_dict = self._build_plan_row(new_plan, widget)
            # Insert before the add button (which is at the last index)
            insert_idx = plans_panel.Children.IndexOf(add_plan_btn)
            if insert_idx < 0:
                plans_panel.Children.Add(row_dict['row_grid'])
            else:
                plans_panel.Children.Insert(insert_idx, row_dict['row_grid'])
            widget['plan_rows'].append(row_dict)
        add_plan_btn.Click += _on_add_plan
        plans_panel.Children.Add(add_plan_btn)
        widget['add_plan_btn'] = add_plan_btn

        # Wire discipline duplicate + remove
        def _on_duplicate_disc(sender, e):
            self._duplicate_discipline(widget)
        duplicate_disc_btn.Click += _on_duplicate_disc

        def _on_remove_disc(sender, e):
            self._remove_discipline(widget)
        remove_disc_btn.Click += _on_remove_disc

        return widget

    def _build_plan_row(self, plan, parent_disc_widget):
        """Build one plan-type row. Caller is responsible for inserting
        the returned row_grid into the discipline's plans_panel."""
        row = Grid()
        row.Margin = Thickness(0, 2, 0, 2)
        # cols: cb(22) | name(*) | family(110) | template(180) | gear(26) | remove(26)
        widths = [22, None, 110, 180, 26, 26]
        for w in widths:
            cd = ColumnDefinition()
            if w is None:
                cd.Width = GridLength(1, GridUnitType.Star)
            else:
                cd.Width = GridLength(w, GridUnitType.Pixel)
            row.ColumnDefinitions.Add(cd)

        plan_cb = CheckBox()
        plan_cb.IsChecked = bool(plan.get('enabled', True))
        plan_cb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(plan_cb, 0)
        row.Children.Add(plan_cb)

        plan_name_box = TextBox()
        plan_name_box.Text = plan['name']
        plan_name_box.Margin = Thickness(0, 0, 6, 0)
        Grid.SetColumn(plan_name_box, 1)
        row.Children.Add(plan_name_box)

        family_combo = ComboBox()
        for fam in ('FloorPlan', 'CeilingPlan'):
            it = ComboBoxItem()
            it.Content = fam
            family_combo.Items.Add(it)
        family_combo.SelectedIndex = (
            0 if plan.get('view_family', 'FloorPlan') == 'FloorPlan' else 1)
        family_combo.Margin = Thickness(0, 0, 6, 0)
        Grid.SetColumn(family_combo, 2)
        row.Children.Add(family_combo)

        template_combo = ComboBox()
        template_combo.Margin = Thickness(0, 0, 6, 0)
        Grid.SetColumn(template_combo, 3)
        row.Children.Add(template_combo)

        # Populate templates based on family selection (and refresh on change)
        def refresh_templates(_sender=None, _e=None):
            current_family = (
                family_combo.SelectedItem.Content
                if family_combo.SelectedItem is not None else 'FloorPlan')
            view_type = _FAMILY_TO_VIEWTYPE[current_family]
            templates = self._proj['view_templates'].get(view_type, [])
            template_combo.Items.Clear()
            none_item = ComboBoxItem()
            none_item.Content = NONE_TEMPLATE_LABEL
            none_item.Tag = None
            template_combo.Items.Add(none_item)
            saved_name = plan.get('view_template_name')
            sel_index = 0
            for i, t in enumerate(templates):
                it = ComboBoxItem()
                it.Content = t.Name
                it.Tag = t
                template_combo.Items.Add(it)
                if saved_name and saved_name == t.Name:
                    sel_index = i + 1
            template_combo.SelectedIndex = sel_index

        family_combo.SelectionChanged += refresh_templates
        refresh_templates()

        # Gear button -> opens plan-type settings popup (level filter + sheet # override)
        gear_btn = Button()
        gear_btn.Content = '⚙'  # gear icon
        gear_btn.ToolTip = 'Levels this plan type applies to + sheet number override'
        gear_btn.Width = 24
        gear_btn.Height = 22
        gear_btn.Padding = Thickness(0)
        gear_btn.Margin = Thickness(2, 0, 0, 0)
        gear_btn.Cursor = self._cursor_hand()
        gear_btn.Background = Brushes.Transparent
        gear_btn.BorderBrush = SolidColorBrush(Color.FromRgb(203, 213, 224))
        gear_btn.Foreground = SolidColorBrush(Color.FromRgb(45, 55, 72))
        Grid.SetColumn(gear_btn, 4)
        row.Children.Add(gear_btn)

        # Remove button on this row
        remove_btn = self._make_remove_button('Remove this plan type')
        Grid.SetColumn(remove_btn, 5)
        row.Children.Add(remove_btn)

        row_dict = {
            'config': plan,
            'enabled_cb': plan_cb,
            'name_box': plan_name_box,
            'family_combo': family_combo,
            'template_combo': template_combo,
            'row_grid': row,
        }

        def _on_remove_plan(sender, e):
            disc_cfg = parent_disc_widget['config']
            if plan in disc_cfg['plan_types']:
                disc_cfg['plan_types'].remove(plan)
            if row_dict in parent_disc_widget['plan_rows']:
                parent_disc_widget['plan_rows'].remove(row_dict)
            parent_disc_widget['plans_panel'].Children.Remove(row)
        remove_btn.Click += _on_remove_plan

        def _on_gear(sender, e):
            self._open_plan_type_settings(plan)
        gear_btn.Click += _on_gear

        return row_dict

    # ---- Add/remove plumbing ---------------------------------------------

    def _make_duplicate_button(self, tooltip):
        b = Button()
        b.Content = '⎘'  # writing tablet / sheet copy glyph (legible)
        b.ToolTip = tooltip
        b.Width = 24
        b.Height = 22
        b.Padding = Thickness(0)
        b.Margin = Thickness(2, 0, 0, 0)
        b.Cursor = self._cursor_hand()
        b.Background = Brushes.Transparent
        b.BorderBrush = SolidColorBrush(Color.FromRgb(203, 213, 224))
        b.Foreground = SolidColorBrush(Color.FromRgb(43, 108, 176))
        b.FontWeight = self._fw_bold()
        return b

    def _make_remove_button(self, tooltip):
        b = Button()
        b.Content = '✕'  # multiplication X
        b.ToolTip = tooltip
        b.Width = 24
        b.Height = 22
        b.Padding = Thickness(0)
        b.Margin = Thickness(2, 0, 0, 0)
        b.Cursor = self._cursor_hand()
        b.Background = Brushes.Transparent
        b.BorderBrush = SolidColorBrush(Color.FromRgb(203, 213, 224))
        b.Foreground = SolidColorBrush(Color.FromRgb(197, 48, 48))
        b.FontWeight = self._fw_bold()
        return b

    def _make_add_button(self, label, click_handler, small=False):
        b = Button()
        b.Content = label
        b.HorizontalAlignment = HorizontalAlignment.Left
        b.Padding = Thickness(10, 4, 10, 4)
        b.Margin = Thickness(0, 6, 0, 0) if small else Thickness(0, 4, 0, 0)
        b.Cursor = self._cursor_hand()
        b.Background = SolidColorBrush(Color.FromRgb(237, 242, 247))
        b.BorderBrush = SolidColorBrush(Color.FromRgb(203, 213, 224))
        b.Foreground = SolidColorBrush(Color.FromRgb(45, 55, 72))
        if click_handler is not None:
            b.Click += click_handler
        return b

    def _cursor_hand(self):
        from System.Windows.Input import Cursors
        return Cursors.Hand

    def _remove_discipline(self, widget):
        if widget['config'] in self._cfg['disciplines']:
            self._cfg['disciplines'].remove(widget['config'])
        if widget in self._discipline_widgets:
            self._discipline_widgets.remove(widget)
        self.pnl_disciplines.Children.Remove(widget['root'])

    def _duplicate_discipline(self, source_widget):
        """Deep-copy a discipline (with all its plan types and overrides)
        and insert the copy directly after the source in the panel."""
        # Pull current values from the widgets first so the copy reflects any
        # in-flight edits the user has made but not yet saved.
        self._read_form_into_cfg()

        source_cfg = source_widget['config']
        new_cfg = copy.deepcopy(source_cfg)
        # Avoid an exact-duplicate name (cosmetic only - codes are user-editable)
        if new_cfg.get('name'):
            new_cfg['name'] = '{} (copy)'.format(new_cfg['name'])
        # Wipe any project-specific level filters so the copy is "fresh"
        for pt in new_cfg.get('plan_types', []):
            pt['level_filter_uniqueids'] = None

        # Insert into config list right after the source
        try:
            src_idx = self._cfg['disciplines'].index(source_cfg)
        except ValueError:
            src_idx = len(self._cfg['disciplines']) - 1
        self._cfg['disciplines'].insert(src_idx + 1, new_cfg)

        # Build the card and insert it directly after the source card
        new_widget = self._build_discipline_card(new_cfg)
        try:
            src_widget_idx = self._discipline_widgets.index(source_widget)
        except ValueError:
            src_widget_idx = len(self._discipline_widgets) - 1
        self._discipline_widgets.insert(src_widget_idx + 1, new_widget)

        try:
            src_panel_idx = self.pnl_disciplines.Children.IndexOf(source_widget['root'])
        except Exception:
            src_panel_idx = -1
        if src_panel_idx < 0:
            insert_idx = self.pnl_disciplines.Children.IndexOf(self._add_disc_btn)
            if insert_idx < 0:
                insert_idx = self.pnl_disciplines.Children.Count - 1
        else:
            insert_idx = src_panel_idx + 1
        self.pnl_disciplines.Children.Insert(insert_idx, new_widget['root'])

    def _on_add_discipline(self, sender, e):
        new_disc = {
            'code': 'X',
            'name': 'NEW DISCIPLINE',
            'enabled': True,
            'plan_types': [
                {
                    'name': 'NEW PLAN TYPE',
                    'view_family': 'FloorPlan',
                    'view_template_name': None,
                    'enabled': True,
                }
            ],
        }
        self._cfg['disciplines'].append(new_disc)
        widget = self._build_discipline_card(new_disc)
        self._discipline_widgets.append(widget)
        # Insert before the "+ Add discipline" button (the last child)
        insert_idx = self.pnl_disciplines.Children.IndexOf(self._add_disc_btn)
        if insert_idx < 0:
            self.pnl_disciplines.Children.Add(widget['root'])
        else:
            self.pnl_disciplines.Children.Insert(insert_idx, widget['root'])

    # ---- Plan-type settings popup ---------------------------------------

    def _open_plan_type_settings(self, plan):
        """Open the popup that edits per-plan-type level filter + sheet # override.

        Mutates the plan dict in place if user clicks Apply.
        """
        # Sync any in-flight form edits into cfg first so the popup's
        # default-help text reflects whatever's currently typed (live
        # discipline code/name etc).
        self._read_form_into_cfg()

        # Build the list of level options to show in the popup: only levels
        # currently CHECKED in the main form (so the filter is a subset).
        active_levels = []
        for r in self._level_rows:
            if not r['cb'].IsChecked:
                continue
            label = (r['label_box'].Text or '').strip() or (r['level'].Name or '')
            active_levels.append({
                'level': r['level'],
                'label': label,
                'uniqueid': r['level'].UniqueId,
            })
        if not active_levels:
            dbhms_ui.info(
                'Check at least one level in the main form '
                'before configuring per-plan-type filters.',
                title='No levels selected')
            return

        # Compute defaults the user would have gotten if they didn't override
        # anything for this plan type (used to render the live preview).
        parent_disc = None
        for w in self._discipline_widgets:
            if plan in w['config']['plan_types']:
                parent_disc = w
                break
        if parent_disc is None:
            disc_code = '?'; series_idx = 1
        else:
            # Read the LIVE textbox value so popup defaults reflect any
            # un-saved code edits the user has made in the main form.
            live_code = (parent_disc['code_box'].Text or '').strip()
            disc_code = live_code or parent_disc['config'].get('code', '?')
            try:
                series_idx = parent_disc['config']['plan_types'].index(plan) + 1
            except Exception:
                series_idx = 1
        # Pick a representative level for the preview (first selected, idx 1)
        first_lvl_num = 1
        if active_levels:
            m = re.search(r'\d+', active_levels[0]['level'].Name or '')
            if m:
                try:
                    first_lvl_num = int(m.group(0))
                except ValueError:
                    pass
        defaults = {
            'prefix': str(disc_code),
            'series': str(series_idx),
            'level':  '{:02d}'.format(first_lvl_num),
        }

        # Live discipline context for the popup header so the popup feels
        # tied to whatever the user just typed for the discipline letter/name.
        if parent_disc is None:
            disc_name_live = ''
        else:
            disc_name_live = (parent_disc['name_box'].Text or '').strip() \
                or parent_disc['config'].get('name', '')
        discipline_context = {
            'code': disc_code,
            'name': disc_name_live,
        }

        popup = PlanTypeSettingsWindow(
            plan, active_levels, defaults, discipline_context)
        popup.Owner = self
        popup.ShowDialog()
        # popup mutates plan dict directly on Apply

    # ---- WPF helpers ------------------------------------------------------

    def _corner_radius(self, r):
        from System.Windows import CornerRadius
        return CornerRadius(r)

    def _fw_bold(self):
        from System.Windows import FontWeights
        return FontWeights.SemiBold

    # ---- State extraction -------------------------------------------------

    def _read_form_into_cfg(self):
        """Update self._cfg in place from current form state."""
        # Title block
        sel = self.cmb_titleblock.SelectedItem
        self._cfg['default_title_block_name'] = (
            sel.Content if sel is not None else None)

        # Scope box
        sel = self.cmb_scopebox.SelectedItem
        self._cfg['default_scope_box_name'] = (
            None if sel is None or sel.Tag is None else sel.Tag.Name)

        # Viewport type
        sel = self.cmb_viewport_type.SelectedItem
        self._cfg['default_viewport_type_name'] = (
            None if sel is None or sel.Tag is None
            else _viewport_type_label(sel.Tag))

        # Room tag type
        sel = self.cmb_roomtag.SelectedItem
        self._cfg['default_room_tag_type_name'] = (
            None if sel is None or sel.Tag is None
            else _room_tag_label(sel.Tag))

        # Level label overrides (uniqueid -> label_text), so we can restore
        # what the user typed when the form is reopened in the same session.
        level_labels = self._cfg.get('level_labels')
        if not isinstance(level_labels, dict):
            level_labels = {}
            self._cfg['level_labels'] = level_labels
        for r in self._level_rows:
            try:
                level_labels[r['level'].UniqueId] = (r['label_box'].Text or '')
            except Exception:
                pass

        # Disciplines
        for w in self._discipline_widgets:
            disc = w['config']
            disc['enabled'] = bool(w['enabled_cb'].IsChecked)
            disc['code'] = w['code_box'].Text.strip() or disc['code']
            disc['name'] = w['name_box'].Text.strip() or disc['name']
            for pr in w['plan_rows']:
                pl = pr['config']
                pl['enabled'] = bool(pr['enabled_cb'].IsChecked)
                pl['name'] = pr['name_box'].Text.strip() or pl['name']
                fam_sel = pr['family_combo'].SelectedItem
                pl['view_family'] = (
                    fam_sel.Content if fam_sel is not None else 'FloorPlan')
                tmpl_sel = pr['template_combo'].SelectedItem
                pl['view_template_name'] = (
                    None if tmpl_sel is None or tmpl_sel.Tag is None
                    else tmpl_sel.Tag.Name)

    def get_run_settings(self):
        """Return the runtime settings the script will act on."""
        self._read_form_into_cfg()

        title_block = None
        sel = self.cmb_titleblock.SelectedItem
        if sel is not None:
            title_block = sel.Tag

        scope_box = None
        sel = self.cmb_scopebox.SelectedItem
        if sel is not None and sel.Tag is not None:
            scope_box = sel.Tag

        levels = []
        for r in self._level_rows:
            if not r['cb'].IsChecked:
                continue
            label = (r['label_box'].Text or '').strip()
            if not label:
                label = _level_label(r['level'], len(levels) + 1)
            levels.append({'level': r['level'], 'label': label})

        disciplines_runtime = []
        for w in self._discipline_widgets:
            disc = w['config']
            if not disc.get('enabled', True):
                continue
            plan_runtime = []
            for pr in w['plan_rows']:
                pl = pr['config']
                if not pl.get('enabled', True):
                    continue
                tmpl_sel = pr['template_combo'].SelectedItem
                template_obj = (
                    None if tmpl_sel is None else tmpl_sel.Tag)
                plan_runtime.append({
                    'name': pl['name'],
                    'view_family': pl['view_family'],
                    'view_template': template_obj,
                    'view_template_name': pl.get('view_template_name'),
                    'level_filter_uniqueids': pl.get('level_filter_uniqueids'),
                    'sheet_number_prefix_override': pl.get('sheet_number_prefix_override'),
                    'sheet_number_series_override': pl.get('sheet_number_series_override'),
                    'sheet_number_level_override':  pl.get('sheet_number_level_override'),
                    'sheet_number_suffix_override': pl.get('sheet_number_suffix_override'),
                })
            if plan_runtime:
                disciplines_runtime.append({
                    'code': disc['code'],
                    'name': disc['name'],
                    'plan_types': plan_runtime,
                })

        # Room tag type (None = skip tagging)
        room_tag_type = None
        sel = self.cmb_roomtag.SelectedItem
        if sel is not None and sel.Tag is not None:
            room_tag_type = sel.Tag

        # Viewport type (None = Revit default)
        viewport_type = None
        sel = self.cmb_viewport_type.SelectedItem
        if sel is not None and sel.Tag is not None:
            viewport_type = sel.Tag

        return {
            'title_block': title_block,
            'scope_box': scope_box,
            'room_tag_type': room_tag_type,
            'viewport_type': viewport_type,
            'levels': levels,
            'disciplines': disciplines_runtime,
            'cfg': self._cfg,  # for patterns/options
        }

    # ---- Button handlers --------------------------------------------------

    def _on_full_setup(self, sender, e):
        """Replace the discipline list with the full firm-standard layout
        from config.json. Leaves all other settings (title block, scope box,
        level overrides, etc.) untouched so the user keeps their context."""
        # Capture in-flight edits so we don't lose user picks elsewhere
        self._read_form_into_cfg()
        full_cfg = _load_config()
        self._cfg['disciplines'] = copy.deepcopy(full_cfg.get('disciplines', []))
        self._populate_disciplines()

    def _on_clear(self, sender, e):
        """Empty the discipline list. Use this to start a setup from scratch
        in the current project."""
        self._read_form_into_cfg()
        self._cfg['disciplines'] = []
        self._populate_disciplines()

    def _on_save_default(self, sender, e):
        self._read_form_into_cfg()
        try:
            _save_config(self._cfg)
            dbhms_ui.info('Defaults saved to config.json.', title='Saved')
        except Exception as ex:
            dbhms_ui.info('Failed to save: {}'.format(ex), title='Error')

    def _on_cancel(self, sender, e):
        self.confirmed = False
        self.Close()

    def _on_run(self, sender, e):
        self.confirmed = True
        self.Close()

    def _on_levels_all(self, sender, e):
        for r in self._level_rows:
            r['cb'].IsChecked = True

    def _on_levels_none(self, sender, e):
        for r in self._level_rows:
            r['cb'].IsChecked = False


# --------------------------------------------------------------------------
# Preview window
# --------------------------------------------------------------------------

class PlanTypeSettingsWindow(forms.WPFWindow):
    """Popup for one plan type: pick which levels apply + override sheet # pattern."""

    def __init__(self, plan, active_levels, defaults, discipline_context=None):
        forms.WPFWindow.__init__(self, PLAN_SETTINGS_XAML)
        self._plan = plan
        self._active_levels = active_levels
        # defaults = {'prefix': 'E', 'series': '1', 'level': '01'} for previewing
        self._defaults = defaults
        self._level_checkboxes = []  # [(uniqueid, CheckBox)]

        # Live discipline context: shows the user that this popup is tied to
        # the discipline code/name they just typed in the main form. Updates
        # each time the popup is opened from the main form's gear button.
        ctx = discipline_context or {}
        ctx_code = (ctx.get('code') or '').strip()
        ctx_name = (ctx.get('name') or '').strip()
        if ctx_code or ctx_name:
            label = ctx_code
            if ctx_name:
                label = '{} - {}'.format(label, ctx_name) if label else ctx_name
            self.txt_discipline_context.Text = label.upper()
        else:
            self.txt_discipline_context.Text = ''

        self.txt_plan_name.Text = plan.get('name', '')

        # Default-help line above the three boxes
        self.txt_default_help.Text = (
            'Default for this plan type: {}{}{} (e.g. {}{}{}). '
            'Leave any box blank to keep the default; type a value to override just that part.'
            .format(defaults['prefix'], defaults['series'], defaults['level'],
                    defaults['prefix'], defaults['series'], defaults['level']))

        # Pre-fill the four override textboxes
        self.txt_prefix_override.Text = plan.get('sheet_number_prefix_override') or ''
        self.txt_series_override.Text = plan.get('sheet_number_series_override') or ''
        self.txt_level_override.Text  = plan.get('sheet_number_level_override')  or ''
        self.txt_suffix_override.Text = plan.get('sheet_number_suffix_override') or ''

        # Hint text under each box
        self.txt_prefix_hint.Text = 'blank = ' + defaults['prefix']
        self.txt_series_hint.Text = 'blank = ' + defaults['series']
        self.txt_level_hint.Text  = 'blank = ' + defaults['level']
        self.txt_suffix_hint.Text = 'blank = (none)'

        # Wire live preview
        self.txt_prefix_override.TextChanged += self._update_preview
        self.txt_series_override.TextChanged += self._update_preview
        self.txt_level_override.TextChanged  += self._update_preview
        self.txt_suffix_override.TextChanged += self._update_preview
        self._update_preview(None, None)

        # Build levels list. Pre-check from saved filter (None = all checked).
        saved_filter = plan.get('level_filter_uniqueids')
        for li in active_levels:
            row = Grid()
            row.Margin = Thickness(0, 2, 0, 2)
            for w in (22, None):
                cd = ColumnDefinition()
                cd.Width = (GridLength(w, GridUnitType.Pixel)
                            if w else GridLength(1, GridUnitType.Star))
                row.ColumnDefinitions.Add(cd)
            cb = CheckBox()
            cb.VerticalAlignment = VerticalAlignment.Center
            cb.IsChecked = (
                True if saved_filter is None else li['uniqueid'] in saved_filter)
            Grid.SetColumn(cb, 0)
            row.Children.Add(cb)
            label_tb = TextBlock()
            label_tb.Text = '{}    [{}]'.format(li['label'], li['level'].Name)
            label_tb.VerticalAlignment = VerticalAlignment.Center
            label_tb.TextTrimming = TextTrimming.CharacterEllipsis
            label_tb.ToolTip = li['level'].Name
            Grid.SetColumn(label_tb, 1)
            row.Children.Add(label_tb)
            self.pnl_levels.Children.Add(row)
            self._level_checkboxes.append((li['uniqueid'], cb))

        self.btn_levels_all.Click += self._on_all
        self.btn_levels_none.Click += self._on_none
        self.btn_apply.Click += self._on_apply
        self.btn_cancel.Click += self._on_cancel

    def _on_all(self, sender, e):
        for _, cb in self._level_checkboxes:
            cb.IsChecked = True

    def _on_none(self, sender, e):
        for _, cb in self._level_checkboxes:
            cb.IsChecked = False

    def _on_apply(self, sender, e):
        # Levels: store list of uniqueids; if every active level is checked,
        # store None to mean "all (whatever's checked in main form)".
        checked = [uid for uid, cb in self._level_checkboxes if cb.IsChecked]
        if len(checked) == len(self._level_checkboxes):
            self._plan['level_filter_uniqueids'] = None
        else:
            self._plan['level_filter_uniqueids'] = checked
        # Sheet number part overrides (blank = default)
        for key, box in (
            ('sheet_number_prefix_override', self.txt_prefix_override),
            ('sheet_number_series_override', self.txt_series_override),
            ('sheet_number_level_override',  self.txt_level_override),
            ('sheet_number_suffix_override', self.txt_suffix_override),
        ):
            val = (box.Text or '').strip()
            self._plan[key] = val if val else None
        self.Close()

    def _update_preview(self, sender, e):
        d = self._defaults
        pre = (self.txt_prefix_override.Text or '').strip() or d['prefix']
        ser = (self.txt_series_override.Text or '').strip() or d['series']
        lvl = (self.txt_level_override.Text  or '').strip() or d['level']
        suf = (self.txt_suffix_override.Text or '').strip()
        self.txt_preview.Text = '{}{}{}{}'.format(pre, ser, lvl, suf)

    def _on_cancel(self, sender, e):
        self.Close()


class PreviewWindow(forms.WPFWindow):

    def __init__(self, plan):
        forms.WPFWindow.__init__(self, PREVIEW_XAML)
        self.confirmed = False
        self._plan = plan

        self.txt_summary.Text = (
            '{} sheet(s) and {} view(s) will be created.'.format(
                len(plan), len(plan)))

        for op in plan:
            self.pnl_rows.Children.Add(self._build_row(op))

        self.btn_back.Click += self._on_back
        self.btn_confirm.Click += self._on_confirm

    def _build_row(self, op):
        row = Border()
        row.BorderBrush = SolidColorBrush(Color.FromRgb(237, 242, 247))
        row.BorderThickness = Thickness(0, 0, 0, 1)
        row.Padding = Thickness(10, 6, 10, 6)

        g = Grid()
        for w in (80, None, 220, 180):
            cd = ColumnDefinition()
            cd.Width = (GridLength(w, GridUnitType.Pixel)
                        if w else GridLength(1, GridUnitType.Star))
            g.ColumnDefinitions.Add(cd)
        row.Child = g

        def _t(text, col, bold=False):
            tb = TextBlock()
            tb.Text = text or ''
            tb.TextTrimming = self._text_trim()
            if bold:
                from System.Windows import FontWeights
                tb.FontWeight = FontWeights.SemiBold
            Grid.SetColumn(tb, col)
            g.Children.Add(tb)
            return tb

        _t(op['sheet_number'], 0, bold=True)
        _t(op['sheet_name'], 1)
        _t(op['view_name'], 2)
        _t(op.get('template_name') or NONE_TEMPLATE_LABEL, 3)
        return row

    def _text_trim(self):
        from System.Windows import TextTrimming
        return TextTrimming.CharacterEllipsis

    def _on_back(self, sender, e):
        self.confirmed = False
        self.Close()

    def _on_confirm(self, sender, e):
        self.confirmed = True
        self.Close()


# --------------------------------------------------------------------------
# Plan builder
# --------------------------------------------------------------------------

def _format_pattern(pattern, **values):
    """Safe pattern format with int padding tokens."""
    # Allow {level:02d} style with int values
    return pattern.format(**values)


def _level_number(level, fallback_idx):
    """Parse first integer from level.Name; fall back to selection index."""
    m = re.search(r'\d+', level.Name or '')
    if m:
        try:
            return int(m.group(0))
        except ValueError:
            pass
    return fallback_idx


def _level_label(level, fallback_idx):
    """Spelled-out 'LEVEL N' standardized across whatever the Revit name is."""
    return 'LEVEL {}'.format(_level_number(level, fallback_idx))


def _build_plan(settings):
    cfg = settings['cfg']
    patterns = cfg['patterns']
    levels = settings['levels']
    auto_suffix = bool(cfg['options'].get('auto_suffix_on_conflict', True))

    # Existing-name index for collision avoidance
    existing_view_names = set(
        v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View))
    existing_sheet_numbers = set(
        s.SheetNumber for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet))

    plan = []
    used_view_names = set()
    used_sheet_numbers = set()

    def _unique(base, existing, used):
        if base not in existing and base not in used:
            return base
        if not auto_suffix:
            return base  # caller should detect
        n = 2
        while True:
            cand = '{} ({})'.format(base, n)
            if cand not in existing and cand not in used:
                return cand
            n += 1

    for disc in settings['disciplines']:
        for series_idx, plan_type in enumerate(disc['plan_types'], start=1):
            # Per-plan-type level filter (None = use all selected levels)
            level_filter = plan_type.get('level_filter_uniqueids')
            if level_filter:
                applicable = [li for li in levels
                              if li['level'].UniqueId in level_filter]
                if not applicable:
                    # Filter doesn't match anything in this run -> fall back to all
                    applicable = levels
            else:
                applicable = levels

            # Per-plan-type part overrides (blank = use default)
            prefix_ovr = plan_type.get('sheet_number_prefix_override')
            series_ovr = plan_type.get('sheet_number_series_override')
            level_ovr  = plan_type.get('sheet_number_level_override')
            suffix_ovr = plan_type.get('sheet_number_suffix_override')

            for level_idx, level_info in enumerate(applicable, start=1):
                level = level_info['level']
                level_label = level_info['label']  # user-supplied (or default)
                level_num = _level_number(level, level_idx)
                token_values = dict(
                    prefix=disc['code'],
                    series=series_idx,
                    discipline_code=disc['code'],
                    discipline_name=disc['name'],
                    plan_type=plan_type['name'],
                    level_name=level.Name,    # raw Revit level name
                    level_label=level_label,  # standardized 'LEVEL N'
                    level=level_num,          # int, supports {level:02d}
                )
                if prefix_ovr or series_ovr or level_ovr or suffix_ovr:
                    # Compose from parts when ANY override is set
                    pre = prefix_ovr or disc['code']
                    ser = series_ovr or str(series_idx)
                    lvl = level_ovr or '{:02d}'.format(level_num)
                    suf = suffix_ovr or ''
                    sheet_number = '{}{}{}{}'.format(pre, ser, lvl, suf)
                else:
                    sheet_number = _format_pattern(
                        patterns['sheet_number'], **token_values)
                sheet_name = _format_pattern(
                    patterns['sheet_name'], **token_values)
                view_name = _format_pattern(
                    patterns['view_name'], **token_values)

                final_view_name = _unique(view_name, existing_view_names, used_view_names)
                final_sheet_num = _unique(sheet_number, existing_sheet_numbers, used_sheet_numbers)
                used_view_names.add(final_view_name)
                used_sheet_numbers.add(final_sheet_num)

                plan.append({
                    'discipline_code': disc['code'],
                    'discipline_name': disc['name'],
                    'plan_type_name': plan_type['name'],
                    'level': level,
                    'level_name': level.Name,
                    'view_family': plan_type['view_family'],
                    'view_template': plan_type['view_template'],
                    'template_name': plan_type.get('view_template_name'),
                    'sheet_number': final_sheet_num,
                    'sheet_name': sheet_name,
                    'view_name': final_view_name,
                })
    return plan


# --------------------------------------------------------------------------
# Plan executor
# --------------------------------------------------------------------------

def _pick_view_family_type(family_string, project_data):
    vfts = project_data['view_family_types'].get(family_string, [])
    return vfts[0] if vfts else None


def _viewport_center_for_sheet(sheet):
    """Return XYZ for viewport placement: center of title block bbox, or origin."""
    tbs = (DB.FilteredElementCollector(doc, sheet.Id)
           .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
           .WhereElementIsNotElementType()
           .ToElements())
    if tbs:
        bb = tbs[0].get_BoundingBox(sheet)
        if bb is not None:
            return DB.XYZ(
                (bb.Min.X + bb.Max.X) / 2.0,
                (bb.Min.Y + bb.Max.Y) / 2.0,
                0.0)
    return DB.XYZ(0, 0, 0)


def _room_is_placed(room):
    """Best-effort check: is this room actually placed (has a location and
    non-zero area)? Survives accessing .Area on rooms in linked docs where
    that property occasionally raises."""
    try:
        if room.Location is None:
            return False
    except Exception:
        return False
    try:
        return room.Area > 0
    except Exception:
        # No usable Area - fall back to "has a location point"
        try:
            return room.Location.Point is not None
        except Exception:
            return False


# Tolerance for matching a linked room's Z elevation to the host view's level
# (in feet). Generous enough for floor-thickness offsets between disciplines.
_LEVEL_MATCH_TOL_FT = 1.0


def _view_level_elevation(view):
    """Return the Z elevation (host coords) of the host view's associated
    level, or None if it can't be determined."""
    try:
        gen_level = view.GenLevel
    except Exception:
        gen_level = None
    if gen_level is None:
        return None
    try:
        return gen_level.Elevation
    except Exception:
        return None


def _tag_rooms_on_view(view, room_tag_type):
    """Tag every room (host + linked) that lies on the host view's level.

    Returns (tagged_count, skipped_count, error_messages). Error messages are
    captured (instead of silently swallowed) so the run report can surface
    real reasons tags didn't land - the most common being a view that doesn't
    intersect any rooms on its level.
    """
    tagged = 0
    skipped = 0
    errors = []
    if room_tag_type is None:
        return (0, 0, errors)
    if not room_tag_type.IsActive:
        try:
            room_tag_type.Activate()
            doc.Regenerate()
        except Exception:
            pass

    view_z = _view_level_elevation(view)

    # Host-doc rooms
    try:
        host_rooms = (DB.FilteredElementCollector(doc)
                      .OfCategory(DB.BuiltInCategory.OST_Rooms)
                      .WhereElementIsNotElementType()
                      .ToElements())
        for room in host_rooms:
            if not _room_is_placed(room):
                continue
            try:
                pt = room.Location.Point
                # Only tag rooms whose Z matches the view's level (avoids
                # tagging level 1 rooms onto the level 3 view, etc.)
                if view_z is not None and abs(pt.Z - view_z) > _LEVEL_MATCH_TOL_FT:
                    continue
                ref = DB.LinkElementId(room.Id)
                tag = DB.RoomTag.Create(
                    doc, view.Id, ref, DB.UV(pt.X, pt.Y))
                if tag.GetTypeId() != room_tag_type.Id:
                    try:
                        tag.ChangeTypeId(room_tag_type.Id)
                    except Exception:
                        pass
                tagged += 1
            except Exception as ex:
                skipped += 1
                errors.append('host room {}: {}'.format(
                    getattr(room, 'Id', '?'), ex))
    except Exception as ex:
        errors.append('host rooms collector: {}'.format(ex))

    # Linked-doc rooms - the common case for MEP setups where Architecture
    # is a linked model.
    try:
        link_instances = (DB.FilteredElementCollector(doc)
                          .OfClass(DB.RevitLinkInstance)
                          .ToElements())
    except Exception as ex:
        link_instances = []
        errors.append('link instances collector: {}'.format(ex))

    for inst in link_instances:
        link_doc = inst.GetLinkDocument()
        if link_doc is None:
            continue
        try:
            transform = inst.GetTotalTransform()
            rooms = (DB.FilteredElementCollector(link_doc)
                     .OfCategory(DB.BuiltInCategory.OST_Rooms)
                     .WhereElementIsNotElementType()
                     .ToElements())
            for room in rooms:
                if not _room_is_placed(room):
                    continue
                try:
                    pt = room.Location.Point
                    host_pt = transform.OfPoint(pt)
                    # Skip rooms whose level (after the link's transform)
                    # doesn't match the host view's level. Without this, we
                    # try to tag every linked room on every plan view and
                    # nearly all of them fail silently because the room
                    # doesn't intersect the view's plan range.
                    if view_z is not None and abs(host_pt.Z - view_z) > _LEVEL_MATCH_TOL_FT:
                        continue
                    ref = DB.LinkElementId(inst.Id, room.Id)
                    tag = DB.RoomTag.Create(
                        doc, view.Id, ref,
                        DB.UV(host_pt.X, host_pt.Y))
                    if tag.GetTypeId() != room_tag_type.Id:
                        try:
                            tag.ChangeTypeId(room_tag_type.Id)
                        except Exception:
                            pass
                    tagged += 1
                except Exception as ex:
                    skipped += 1
                    errors.append('linked room {} (link {}): {}'.format(
                        getattr(room, 'Id', '?'),
                        getattr(inst, 'Name', '?'), ex))
        except Exception as ex:
            errors.append('link {} rooms: {}'.format(
                getattr(inst, 'Name', '?'), ex))

    return (tagged, skipped, errors)


def _execute_plan(plan, settings, project_data):
    cfg = settings['cfg']
    options = cfg['options']
    title_block = settings['title_block']
    scope_box = settings['scope_box']
    set_browser_org = bool(options.get('set_browser_organization', False))
    browser_param_name = options.get(
        'browser_sub_discipline_param', 'Sub-Discipline')

    # Reusable: shared viewport XY (computed from first sheet)
    shared_viewport_xy = [None]  # mutable container

    placed_count = 0
    skipped = []
    log_lines = []
    total_rooms_tagged = 0
    total_rooms_skipped = 0
    tag_errors = []

    with revit.Transaction('Sheet Setup'):
        # Make sure the title block FamilySymbol is active before sheet creation
        if title_block is not None and not title_block.IsActive:
            title_block.Activate()
            doc.Regenerate()
        # Activate room tag type if needed
        room_tag_type = settings.get('room_tag_type')
        if room_tag_type is not None and not room_tag_type.IsActive:
            try:
                room_tag_type.Activate()
                doc.Regenerate()
            except Exception:
                pass

        for op in plan:
            try:
                vft = _pick_view_family_type(op['view_family'], project_data)
                if vft is None:
                    skipped.append('{}  (no {} ViewFamilyType in project)'.format(
                        op['sheet_number'], op['view_family']))
                    continue

                # 1. Create the view
                new_view = DB.ViewPlan.Create(doc, vft.Id, op['level'].Id)

                # 2. Name it
                try:
                    new_view.Name = op['view_name']
                except Exception:
                    pass  # Revit may reject some chars

                # 3. Apply scope box (must be set BEFORE view template if template
                #    locks it). Setting the param works for both plan and rcp.
                if scope_box is not None:
                    p = new_view.get_Parameter(
                        DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
                    if p is not None and not p.IsReadOnly:
                        p.Set(scope_box.Id)

                # 4. Apply view template
                if op['view_template'] is not None:
                    try:
                        new_view.ViewTemplateId = op['view_template'].Id
                    except Exception:
                        pass

                # 5. Browser organization parameter (Sub-Discipline)
                if set_browser_org:
                    p = new_view.LookupParameter(browser_param_name)
                    if p is not None and not p.IsReadOnly:
                        try:
                            p.Set(op['plan_type_name'])
                        except Exception:
                            pass

                # 6. Create the sheet
                new_sheet = DB.ViewSheet.Create(doc, title_block.Id)
                try:
                    new_sheet.SheetNumber = op['sheet_number']
                except Exception:
                    skipped.append('{}  (sheet number rejected)'.format(
                        op['sheet_number']))
                    continue
                try:
                    new_sheet.Name = op['sheet_name']
                except Exception:
                    pass

                # 7. Place viewport at consistent XY
                if shared_viewport_xy[0] is None:
                    shared_viewport_xy[0] = _viewport_center_for_sheet(new_sheet)
                place_at = shared_viewport_xy[0]

                if DB.Viewport.CanAddViewToSheet(
                        doc, new_sheet.Id, new_view.Id):
                    new_vp = DB.Viewport.Create(
                        doc, new_sheet.Id, new_view.Id, place_at)
                    # Apply viewport type if user picked one
                    vp_type = settings.get('viewport_type')
                    if vp_type is not None:
                        try:
                            if new_vp.GetTypeId() != vp_type.Id:
                                new_vp.ChangeTypeId(vp_type.Id)
                        except Exception:
                            pass
                else:
                    skipped.append('{}  (Revit refused to add view to sheet)'.format(
                        op['sheet_number']))
                    continue

                # 8. Auto-tag rooms (if user picked a room tag type). Tags
                #    rooms in the host doc and in every linked Revit model
                #    that intersect this view's level.
                room_tag_type = settings.get('room_tag_type')
                if room_tag_type is not None:
                    try:
                        t_tagged, t_skipped, t_errors = _tag_rooms_on_view(
                            new_view, room_tag_type)
                        total_rooms_tagged += t_tagged
                        total_rooms_skipped += t_skipped
                        # Cap the errors we keep so a misconfigured project
                        # doesn't flood the log with thousands of lines.
                        for msg in t_errors[:5]:
                            tag_errors.append('{} | {}'.format(
                                op['sheet_number'], msg))
                    except Exception as ex:
                        tag_errors.append('{} | tagging crashed: {}'.format(
                            op['sheet_number'], ex))

                placed_count += 1
                log_lines.append('{} | {} | {}'.format(
                    op['sheet_number'], op['sheet_name'], op['view_name']))
            except Exception as ex:
                tb = traceback.format_exc()
                skipped.append('{}  (error: {})'.format(op['sheet_number'], ex))
                output.print_md('```\n' + tb + '\n```')

    # Report
    output.print_md('## Sheet Setup - Result')
    output.print_md('**Created {} sheets/views.**'.format(placed_count))
    for line in log_lines:
        output.print_md('- ' + line)
    if skipped:
        output.print_md('**Skipped {}:**'.format(len(skipped)))
        for line in skipped:
            output.print_md('- ' + line)
    if settings.get('room_tag_type') is not None:
        output.print_md(
            '**Room tags placed: {}** (skipped: {})'.format(
                total_rooms_tagged, total_rooms_skipped))
        if total_rooms_tagged == 0 and total_rooms_skipped == 0 and not tag_errors:
            output.print_md(
                '- No rooms found on the host doc or in any linked model. '
                'Make sure the linked architectural model is loaded.')
        for line in tag_errors[:20]:
            output.print_md('- ' + line)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def main():
    if not os.path.isfile(CONFIG_PATH):
        forms.alert(
            'config.json is missing next to script.py. '
            'Reinstall the tool from the source folder.',
            exitscript=True)

    cfg = _load_config()
    cached = _load_project_state()
    if cached:
        # Use cached project state as the starting point so the user's edits
        # (renamed levels, popup overrides, picked title block, etc.) survive
        # form close/reopen and Revit restarts.
        cfg = cached
    else:
        # Fresh project: start with a single sample discipline + two sample
        # plan types instead of the full firm-standard list. The user can
        # click "Full Setup" to load the full default at any time.
        cfg['disciplines'] = _minimal_default_disciplines()

    project_data = {
        'levels': _get_levels(),
        'title_blocks': _get_title_blocks(),
        'scope_boxes': _get_scope_boxes(),
        'view_family_types': _get_view_family_types(),
        'view_templates': _get_view_templates(),
        'room_tag_types': _get_room_tag_types(),
        'viewport_types': _get_viewport_types(),
    }

    if not project_data['title_blocks']:
        forms.alert(
            'No title block families are loaded in this project. '
            'Load a title block first, then re-run.',
            exitscript=True)

    if not project_data['levels']:
        forms.alert('No levels found in this project.', exitscript=True)

    # Loop: form -> preview -> (back)? -> form -> ...
    while True:
        form = ModelSetupForm(cfg, project_data)
        form.ShowDialog()
        if not form.confirmed:
            # User cancelled - still capture their in-flight edits so opening
            # the form again brings everything back.
            try:
                form._read_form_into_cfg()
                _save_project_state(form._cfg)
            except Exception:
                pass
            return

        settings = form.get_run_settings()
        cfg = settings['cfg']  # carry edits forward across the loop
        _save_project_state(cfg)

        # Validation
        if settings['title_block'] is None:
            dbhms_ui.info('Pick a title block before continuing.')
            continue
        if not settings['levels']:
            dbhms_ui.info('Select at least one level.')
            continue
        if not settings['disciplines']:
            dbhms_ui.info('Enable at least one discipline + plan type.')
            continue

        plan = _build_plan(settings)
        if not plan:
            dbhms_ui.info('Nothing to create with the current selection.')
            continue

        if cfg['options'].get('show_preview', True):
            preview = PreviewWindow(plan)
            preview.ShowDialog()
            if not preview.confirmed:
                continue  # back to form

        _execute_plan(plan, settings, project_data)
        return


if __name__ == '__main__':
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
