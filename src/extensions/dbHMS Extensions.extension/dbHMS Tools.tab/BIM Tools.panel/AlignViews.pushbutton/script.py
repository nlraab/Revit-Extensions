# -*- coding: utf-8 -*-
"""Align Views - match viewport position and view title across sheets.

Pick a MASTER SHEET.  Every view placed on it becomes a numbered "position".
Pick the target sheets you want to bring into line; for each one the tool
pairs its views into the master's positions (automatically when that is
unambiguous, or via a small per-position picker when a sheet carries several
similar views).  Choose what to match (viewport position on the sheet, view
title position, view title line length) and the tool snaps every paired view
so its content and title land at the same spot as the master.

A "Titles only" mode aligns view titles WITHOUT moving the views, with a
choice between lining the titles up on the sheet (absolute) or giving every
title the same offset from its own view (relative).

Modeled after IDEATE Align Views, but lives entirely inside Revit/pyRevit.
"""

__title__ = 'Align\nViews'
__author__ = 'Nathaniel'

import os
import re
import json
import codecs
import traceback

from pyrevit import revit, DB, forms, script
import dbhms_ui
import dbhms_telemetry

# WPF / .NET imports for the dynamic form controls
import clr  # noqa: F401
from System.Windows import (
    Thickness, Visibility, VerticalAlignment, HorizontalAlignment,
    TextTrimming, FontWeights, CornerRadius)
from System.Windows.Controls import (
    CheckBox, ComboBox, ComboBoxItem, TextBlock, StackPanel, DockPanel,
    Orientation, Dock, Border, Button)
from System.Windows.Media import Brushes, BrushConverter
from System.Windows.Input import Cursors, Keyboard, ModifierKeys


# --------------------------------------------------------------------------
# Small brush / control helpers (module level so the row class can use them)
# --------------------------------------------------------------------------

_BC = BrushConverter()
_brush_cache = {}


def _B(hexs):
    """Cached brush from a hex string; 'transparent' maps to Brushes.Transparent.
    Returns None only if the string is somehow unparseable."""
    if hexs in _brush_cache:
        return _brush_cache[hexs]
    try:
        if hexs == 'transparent':
            br = Brushes.Transparent
        else:
            br = _BC.ConvertFromString(hexs)
    except Exception:
        br = None
    _brush_cache[hexs] = br
    return br


_BRUSH_NAVY = _B('#1A365D')
_BRUSH_META = _B('#A0AEC0')
_ELLIPSIS = TextTrimming.CharacterEllipsis


def _num_badge(n):
    """A small navy circle carrying a position number."""
    b = Border()
    b.Width = 17
    b.Height = 17
    b.CornerRadius = CornerRadius(9)
    b.Background = _BRUSH_NAVY
    b.VerticalAlignment = VerticalAlignment.Center
    t = TextBlock()
    t.Text = str(n)
    t.Foreground = Brushes.White
    t.FontSize = 10
    t.FontWeight = FontWeights.Bold
    t.HorizontalAlignment = HorizontalAlignment.Center
    t.VerticalAlignment = VerticalAlignment.Center
    b.Child = t
    return b


def _make_check_glyph():
    """White check mark, hidden until the row is selected."""
    t = TextBlock()
    t.Text = u'✓'
    t.Foreground = Brushes.White
    t.FontSize = 11
    t.FontWeight = FontWeights.Bold
    t.HorizontalAlignment = HorizontalAlignment.Center
    t.VerticalAlignment = VerticalAlignment.Center
    t.Visibility = Visibility.Collapsed
    return t


def _make_chip(clean, nviews):
    """Green 'auto-paired' chip, or amber 'N views - map them' chip."""
    b = Border()
    b.CornerRadius = CornerRadius(10)
    b.BorderThickness = Thickness(1)
    b.Padding = Thickness(8, 1, 8, 1)
    b.VerticalAlignment = VerticalAlignment.Center
    b.Margin = Thickness(6, 0, 0, 0)
    t = TextBlock()
    t.FontSize = 10.5
    t.VerticalAlignment = VerticalAlignment.Center
    if clean:
        b.Background = _B('#F0FFF4')
        b.BorderBrush = _B('#C6F6D5')
        t.Foreground = _B('#276749')
        t.Text = u'auto-paired'
    else:
        b.Background = _B('#FFFBEA')
        b.BorderBrush = _B('#F0D8A8')
        t.Foreground = _B('#975A16')
        t.Text = u'{} views — map them'.format(nviews)
    b.Child = t
    return b


def _mini_button(content):
    """A compact grey button that reads correctly under the default template."""
    btn = Button()
    btn.Content = content
    btn.Background = _B('#EDF2F7')
    btn.Foreground = _B('#4A5568')
    btn.BorderBrush = _B('#CBD5E0')
    btn.BorderThickness = Thickness(1)
    btn.Padding = Thickness(6, 0, 6, 0)
    btn.Margin = Thickness(6, 0, 0, 0)
    btn.FontSize = 11
    btn.Cursor = Cursors.Hand
    btn.VerticalAlignment = VerticalAlignment.Center
    return btn


# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
FORM_XAML = os.path.join(SCRIPT_DIR, 'AlignViewsForm.xaml')

doc = revit.doc
output = script.get_output()


def _default_config():
    return {
        'filters': {
            'same_scale': True,
            'same_titleblock': True,
            'same_view_type': False,
            'same_scope_box': False,
        },
        'match': {
            'viewport_position': True,
            'title_position': True,
            'title_line_length': True,
        },
        'title_mode': 'absolute',
    }


def _load_config():
    try:
        with codecs.open(CONFIG_PATH, 'r', 'utf-8') as f:
            cfg = json.load(f)
        base = _default_config()
        base.update(cfg or {})
        return base
    except Exception:
        return _default_config()


def _save_config(cfg):
    try:
        with codecs.open(CONFIG_PATH, 'w', 'utf-8') as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Project data gathering
# --------------------------------------------------------------------------

def _scale_label(scale):
    """Render a Revit integer view scale (e.g. 96) as '1/8\" = 1'-0\"'."""
    if not scale or scale <= 0:
        return '<no scale>'
    common = {
        1536: '1/128" = 1\'-0"',
        768:  '1/64" = 1\'-0"',
        384:  '1/32" = 1\'-0"',
        192:  '1/16" = 1\'-0"',
        96:   '1/8" = 1\'-0"',
        64:   '3/16" = 1\'-0"',
        48:   '1/4" = 1\'-0"',
        32:   '3/8" = 1\'-0"',
        24:   '1/2" = 1\'-0"',
        16:   '3/4" = 1\'-0"',
        12:   '1" = 1\'-0"',
        8:    '1-1/2" = 1\'-0"',
        6:    '2" = 1\'-0"',
        4:    '3" = 1\'-0"',
        2:    '6" = 1\'-0"',
        1:    '12" = 1\'-0"',
    }
    return common.get(scale, '1:{}'.format(scale))


def _build_titleblock_cache():
    """sheet ElementId -> titleblock type ElementId, from one collector query."""
    cache = {}
    try:
        all_tbs = (DB.FilteredElementCollector(doc)
                     .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
                     .WhereElementIsNotElementType()
                     .ToElements())
        for tb in all_tbs:
            sid = tb.OwnerViewId
            if sid not in cache:
                cache[sid] = tb.GetTypeId()
    except Exception:
        pass
    return cache


def _titleblock_label(type_id):
    if type_id is None or type_id == DB.ElementId.InvalidElementId:
        return '<no title block>'
    sym = doc.GetElement(type_id)
    if sym is None:
        return '<unknown>'
    try:
        fam = sym.Family.Name
    except Exception:
        fam = '?'
    try:
        tname = DB.Element.Name.GetValue(sym)
    except Exception:
        tname = '?'
    return '{} : {}'.format(fam, tname)


def _get_scope_box_id(view):
    try:
        p = view.get_Parameter(DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
        if p is not None:
            eid = p.AsElementId()
            if eid is not None:
                return eid
    except Exception:
        pass
    return DB.ElementId.InvalidElementId


def _scope_box_name(eid):
    if eid is None or eid == DB.ElementId.InvalidElementId:
        return '<none>'
    el = doc.GetElement(eid)
    if el is None:
        return '<none>'
    try:
        return el.Name
    except Exception:
        return '<?>'


def _viewtype_label(vt):
    try:
        return str(vt)
    except Exception:
        return '?'


def _collect_records():
    """Walk every Viewport in the project; return one dict per viewport."""
    sheet_titleblock_cache = _build_titleblock_cache()
    tb_label_cache = {}
    scope_name_cache = {}

    vps = (DB.FilteredElementCollector(doc)
             .OfClass(DB.Viewport)
             .ToElements())

    records = []
    for vp in vps:
        try:
            sheet = doc.GetElement(vp.SheetId)
            view = doc.GetElement(vp.ViewId)
            if sheet is None or view is None:
                continue

            sheet_id = sheet.Id
            tb_type_id = sheet_titleblock_cache.get(
                sheet_id, DB.ElementId.InvalidElementId)
            if tb_type_id not in tb_label_cache:
                tb_label_cache[tb_type_id] = _titleblock_label(tb_type_id)

            scale = view.Scale if hasattr(view, 'Scale') else 0
            scope_id = _get_scope_box_id(view)
            if scope_id not in scope_name_cache:
                scope_name_cache[scope_id] = _scope_box_name(scope_id)

            try:
                view_type = view.ViewType
            except Exception:
                view_type = None
            try:
                sheet_no = sheet.SheetNumber
            except Exception:
                sheet_no = '?'
            try:
                sheet_name = sheet.Name
            except Exception:
                sheet_name = ''
            try:
                view_name = view.Name
            except Exception:
                view_name = '<?>'

            records.append({
                'viewport_id':        vp.Id,
                'view_id':            view.Id,
                'sheet_id':           sheet_id,
                'sheet_no':           sheet_no or '',
                'sheet_name':         sheet_name or '',
                'view_name':          view_name or '',
                'scale':              scale,
                'scale_label':        _scale_label(scale),
                'view_type':          view_type,
                'view_type_label':    _viewtype_label(view_type),
                'scope_box_id':       scope_id,
                'scope_box_name':     scope_name_cache[scope_id],
                'titleblock_type_id': tb_type_id,
                'titleblock_label':   tb_label_cache[tb_type_id],
            })
        except Exception:
            continue

    records.sort(key=lambda r: (r['sheet_no'], r['view_name']))
    return records


def _vp_center_xy(vp_id):
    """(x, y) of a viewport's box center on its sheet, for ordering positions."""
    try:
        vp = doc.GetElement(vp_id)
        c = vp.GetBoxCenter()
        return (c.X, c.Y)
    except Exception:
        return (0.0, 0.0)


def _order_positions(records):
    """Order a sheet's viewports in reading order: top-left first."""
    def key(r):
        x, y = _vp_center_xy(r['viewport_id'])
        return (-round(y, 4), round(x, 4), r['view_name'])
    return sorted(records, key=key)


_TOKEN_RE = re.compile(r'[^a-z0-9]+')


def _tokens(s):
    return [t for t in _TOKEN_RE.split((s or '').lower()) if t]


# --------------------------------------------------------------------------
# Filter, pairing & alignment logic
# --------------------------------------------------------------------------

def _matches_filter(rec, master, filters):
    """Can `rec` fill the position whose master viewport is `master`?"""
    if rec['viewport_id'] == master['viewport_id']:
        return False
    if filters.get('same_scale') and rec['scale'] != master['scale']:
        return False
    if filters.get('same_titleblock') and \
            rec['titleblock_type_id'] != master['titleblock_type_id']:
        return False
    if filters.get('same_view_type') and rec['view_type'] != master['view_type']:
        return False
    if filters.get('same_scope_box') and \
            rec['scope_box_id'] != master['scope_box_id']:
        return False
    return True


def _score(v, m):
    """How well does target view `v` fit master position view `m`?"""
    s = 0
    if v.get('view_type') == m.get('view_type'):
        s += 100
    vn = (v['view_name'] or '').strip().lower()
    mn = (m['view_name'] or '').strip().lower()
    if vn and vn == mn:
        s += 60
    else:
        tv = set(_tokens(vn))
        tm = set(_tokens(mn))
        if tv and tm:
            s += 6 * len(tv & tm)
    if v.get('scale') == m.get('scale'):
        s += 10
    return s


def _try_set(obj, attr, value):
    try:
        setattr(obj, attr, value)
        return True
    except Exception:
        return False


def _outline_min(outline):
    """MinimumPoint of a Revit Outline (absolute sheet coordinates)."""
    return outline.MinimumPoint


def _align_one(master_vp, target_vp, match_opts, title_mode):
    """Apply the chosen match options from master_vp onto target_vp.
    Returns a list of (op, ok, msg) tuples for the report."""
    results = []
    do_pos = bool(match_opts.get('viewport_position'))
    do_tpos = bool(match_opts.get('title_position'))
    do_tlen = bool(match_opts.get('title_line_length'))

    # Absolute title line-up only makes sense when the viewport itself is NOT
    # being moved; read both label anchors BEFORE we mutate anything.
    want_abs = do_tpos and (title_mode == 'absolute') and (not do_pos)
    master_label_abs = None
    target_label_abs = None
    if want_abs:
        try:
            master_label_abs = _outline_min(master_vp.GetLabelOutline())
            target_label_abs = _outline_min(target_vp.GetLabelOutline())
        except Exception:
            want_abs = False

    if do_pos:
        try:
            target_vp.SetBoxCenter(master_vp.GetBoxCenter())
            results.append(('viewport position', True, ''))
        except Exception as e:
            results.append(('viewport position', False, str(e)))

    if do_tpos:
        try:
            if want_abs and master_label_abs is not None \
                    and target_label_abs is not None:
                cur = target_vp.LabelOffset
                dx = master_label_abs.X - target_label_abs.X
                dy = master_label_abs.Y - target_label_abs.Y
                if hasattr(cur, 'Z'):
                    new_off = DB.XYZ(cur.X + dx, cur.Y + dy, cur.Z)
                else:
                    new_off = DB.UV(cur.U + dx, cur.V + dy)
                ok = _try_set(target_vp, 'LabelOffset', new_off)
                label = 'title line-up'
            else:
                ok = _try_set(target_vp, 'LabelOffset', master_vp.LabelOffset)
                label = 'title position'
            if ok:
                results.append((label, True, ''))
            else:
                results.append((label, False,
                                'LabelOffset is not settable in this Revit version'))
        except Exception as e:
            results.append(('title position', False, str(e)))

    if do_tlen:
        try:
            length = master_vp.LabelLineLength
            ok = _try_set(target_vp, 'LabelLineLength', length)
            if ok:
                try:
                    _try_set(target_vp, 'LabelLineOffset',
                             master_vp.LabelLineOffset)
                except Exception:
                    pass
                results.append(('title line length', True, ''))
            else:
                results.append(('title line length', False,
                                'LabelLineLength is not settable'))
        except Exception as e:
            results.append(('title line length', False, str(e)))

    return results


# --------------------------------------------------------------------------
# One row in the target-sheet list
# --------------------------------------------------------------------------

class _TargetRow(object):
    """A selectable target sheet.  Clean sheets show an 'auto-paired' chip;
    ambiguous sheets expand to one combo per position so the user can choose
    which view fills each slot."""

    def __init__(self, sheet, positions, mapping, clean, cand,
                 on_click, on_change):
        self.sheet = sheet
        self.positions = positions          # already the CHECKED positions
        self.mapping = dict(mapping)         # pos index -> target vp id / None
        self.clean = clean
        self.cand = cand                     # pos index -> [candidate records]
        self.on_click = on_click            # (row, shift_held) -> None
        self.on_change = on_change          # combo edits -> refresh status
        self.selected = False
        self.combos = {}
        self._suppress = False
        self._build()

    def _nviews(self):
        return len(self.sheet['viewports'])

    def _build(self):
        self.container = Border()
        self.container.BorderThickness = Thickness(1)
        self.container.CornerRadius = CornerRadius(6)
        self.container.Margin = Thickness(0, 0, 0, 7)
        self.container.BorderBrush = _B('transparent')
        self.container.Background = _B('transparent')

        root = StackPanel()
        self.container.Child = root

        # ---- header (clickable to select) --------------------------------
        self.header = Border()
        self.header.Padding = Thickness(9, 7, 9, 7)
        self.header.Background = _B('transparent')
        self.header.Cursor = Cursors.Hand

        dp = DockPanel()
        dp.LastChildFill = True

        self.box = Border()
        self.box.Width = 16
        self.box.Height = 16
        self.box.CornerRadius = CornerRadius(3)
        self.box.BorderBrush = _B('#CBD5E0')
        self.box.BorderThickness = Thickness(1.5)
        self.box.Background = _B('#FFFFFF')
        self.box.VerticalAlignment = VerticalAlignment.Center
        self.box.Margin = Thickness(0, 0, 10, 0)
        self.check_glyph = _make_check_glyph()
        self.box.Child = self.check_glyph
        DockPanel.SetDock(self.box, Dock.Left)
        dp.Children.Add(self.box)

        right = StackPanel()
        right.Orientation = Orientation.Horizontal
        right.VerticalAlignment = VerticalAlignment.Center
        DockPanel.SetDock(right, Dock.Right)
        right.Children.Add(_make_chip(self.clean, self._nviews()))
        if not self.clean:
            self.btn_expand = _mini_button(u'▾')
            self.btn_expand.Click += self._on_expand
            right.Children.Add(self.btn_expand)
        dp.Children.Add(right)

        mid = StackPanel()
        mid.Orientation = Orientation.Horizontal
        mid.VerticalAlignment = VerticalAlignment.Center
        sn = TextBlock()
        sn.Text = self.sheet['sheet_no']
        sn.FontWeight = FontWeights.Bold
        sn.MinWidth = 46
        sn.Foreground = _BRUSH_NAVY
        nm = TextBlock()
        nm.Text = self.sheet['sheet_name']
        nm.Margin = Thickness(8, 0, 0, 0)
        nm.TextTrimming = _ELLIPSIS
        nm.Foreground = _B('#2D3748')
        mid.Children.Add(sn)
        mid.Children.Add(nm)
        dp.Children.Add(mid)

        self.header.Child = dp
        self.header.MouseLeftButtonDown += self._on_header_click
        root.Children.Add(self.header)

        # ---- pairing panel (only when ambiguous) -------------------------
        if not self.clean:
            self.pairing = StackPanel()
            self.pairing.Margin = Thickness(35, 0, 9, 8)
            self.pairing.Visibility = Visibility.Collapsed
            self._build_pairing()
            root.Children.Add(self.pairing)

        self._update_visual()

    def _build_pairing(self):
        for p in self.positions:
            row = DockPanel()
            row.Margin = Thickness(0, 3, 0, 0)
            row.LastChildFill = True

            badge = _num_badge(p['index'])
            badge.Margin = Thickness(0, 0, 8, 0)
            DockPanel.SetDock(badge, Dock.Left)
            row.Children.Add(badge)

            lbl = TextBlock()
            lbl.Text = p['record']['view_name']
            lbl.Width = 116
            lbl.TextTrimming = _ELLIPSIS
            lbl.VerticalAlignment = VerticalAlignment.Center
            lbl.FontSize = 11
            lbl.Foreground = _B('#4A5568')
            DockPanel.SetDock(lbl, Dock.Left)
            row.Children.Add(lbl)

            arr = TextBlock()
            arr.Text = u'→'
            arr.Foreground = _B('#A0AEC0')
            arr.Margin = Thickness(6, 0, 6, 0)
            arr.VerticalAlignment = VerticalAlignment.Center
            DockPanel.SetDock(arr, Dock.Left)
            row.Children.Add(arr)

            combo = ComboBox()
            combo.Height = 24
            combo.FontSize = 11
            combo.VerticalAlignment = VerticalAlignment.Center
            for v in self.cand[p['index']]:
                it = ComboBoxItem()
                it.Content = v['view_name']
                it.Tag = v['viewport_id']
                combo.Items.Add(it)
            skip = ComboBoxItem()
            skip.Content = u'— skip —'
            skip.Tag = None
            combo.Items.Add(skip)

            # preselect the auto guess (falls back to skip = last item)
            combo.SelectedIndex = combo.Items.Count - 1
            sel_id = self.mapping.get(p['index'])
            if sel_id is not None:
                for idx in range(combo.Items.Count):
                    tag = combo.Items[idx].Tag
                    if tag is not None and tag.Equals(sel_id):
                        combo.SelectedIndex = idx
                        break

            combo.Tag = p['index']
            combo.SelectionChanged += self._on_combo_changed
            self.combos[p['index']] = combo
            row.Children.Add(combo)
            self.pairing.Children.Add(row)

    # -- interaction -------------------------------------------------------

    def _on_header_click(self, sender, e):
        shift = False
        try:
            shift = (Keyboard.Modifiers & ModifierKeys.Shift) == ModifierKeys.Shift
        except Exception:
            shift = False
        if self.on_click:
            self.on_click(self, shift)

    def _on_expand(self, sender, e):
        try:
            e.Handled = True
        except Exception:
            pass
        showing = self.pairing.Visibility == Visibility.Visible
        self.pairing.Visibility = \
            Visibility.Collapsed if showing else Visibility.Visible
        self.btn_expand.Content = u'▾' if showing else u'▴'

    def _on_combo_changed(self, sender, e):
        if self._suppress:
            return
        combo = sender
        idx = combo.Tag
        item = combo.SelectedItem
        val = item.Tag if item is not None else None
        self.mapping[idx] = val
        # a target view can only sit in one place: if another position holds
        # this same view, bump it back to skip.
        if val is not None:
            for k, other in self.combos.items():
                if k == idx:
                    continue
                oi = other.SelectedItem
                ov = oi.Tag if oi is not None else None
                if ov is not None and ov.Equals(val):
                    self._suppress = True
                    other.SelectedIndex = other.Items.Count - 1
                    self._suppress = False
                    self.mapping[k] = None
        if self.on_change:
            self.on_change()

    def _update_visual(self):
        if self.selected:
            self.container.Background = _B('#EBF8FF')
            self.container.BorderBrush = _B('#9FD8F1')
            self.box.Background = _B('#2B6CB0')
            self.box.BorderBrush = _B('#2B6CB0')
            self.check_glyph.Visibility = Visibility.Visible
        else:
            self.container.Background = _B('transparent')
            self.container.BorderBrush = _B('transparent')
            self.box.Background = _B('#FFFFFF')
            self.box.BorderBrush = _B('#CBD5E0')
            self.check_glyph.Visibility = Visibility.Collapsed

    def set_selected(self, state):
        self.selected = bool(state)
        self._update_visual()

    def get_pairs(self):
        """[(master_vp_id, target_vp_id, sheet, position), ...] for this row."""
        pairs = []
        if not self.selected:
            return pairs
        pos_by_index = {}
        for p in self.positions:
            pos_by_index[p['index']] = p
        seen = set()
        for idx in self.mapping:
            tgt = self.mapping[idx]
            if tgt is None or tgt in seen:
                continue
            p = pos_by_index.get(idx)
            if p is None:
                continue
            seen.add(tgt)
            pairs.append((p['record']['viewport_id'], tgt, self.sheet, p))
        return pairs


# --------------------------------------------------------------------------
# Main configuration form
# --------------------------------------------------------------------------

class AlignViewsForm(forms.WPFWindow):

    def __init__(self, cfg, records):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._cfg = cfg
        self._records = records
        self.confirmed = False

        self._load_logo()

        self._build_sheets()
        self._master_sheet_id = self._default_master_sheet()
        self._positions = []
        self._target_rows = []
        self._anchor_index = None            # for shift-click range selection

        # ---- static controls ---------------------------------------------
        self._populate_sheet_combo()
        self._populate_filter_checks()
        self._populate_match_checks()
        self._populate_title_mode()

        # ---- events ------------------------------------------------------
        self.cmb_master_sheet.SelectionChanged += self._on_master_changed
        self.txt_master_search.TextChanged += self._on_master_search_changed
        self.txt_targets_search.TextChanged += self._on_targets_search_changed
        for chk in (self.chk_filter_scale, self.chk_filter_titleblock,
                    self.chk_filter_viewtype, self.chk_filter_scopebox):
            chk.Checked += self._on_filter_changed
            chk.Unchecked += self._on_filter_changed
        self.cmb_title_mode.SelectionChanged += self._on_title_mode_changed
        for chk in (self.chk_match_position, self.chk_match_title_pos):
            chk.Checked += self._on_match_changed
            chk.Unchecked += self._on_match_changed
        self.btn_targets_all.Click += self._on_targets_all
        self.btn_targets_none.Click += self._on_targets_none
        self.btn_targets_refresh.Click += lambda s, e: self._refresh_targets()
        self.btn_cancel.Click += self._on_cancel
        self.btn_run.Click += self._on_run

        # ---- initial population ------------------------------------------
        self._rebuild_positions()
        self._update_master_summary()
        self._update_title_mode_visibility()
        self._refresh_targets()
        self._update_status()

    # ------------------------------------------------------------------
    # Branding
    # ------------------------------------------------------------------

    def _load_logo(self):
        try:
            from System import Uri, UriKind
            from System.Windows.Media.Imaging import (
                BitmapImage, BitmapCacheOption)
            path = os.path.join(SCRIPT_DIR, 'dbhms_logo.png')
            if not os.path.exists(path):
                return
            bmp = BitmapImage()
            bmp.BeginInit()
            bmp.CacheOption = BitmapCacheOption.OnLoad
            bmp.UriSource = Uri(path, UriKind.Absolute)
            bmp.DecodePixelHeight = 96
            bmp.EndInit()
            self.img_logo.Source = bmp
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sheet model
    # ------------------------------------------------------------------

    def _build_sheets(self):
        sheets = {}
        for r in self._records:
            sid = r['sheet_id']
            g = sheets.get(sid)
            if g is None:
                g = {
                    'sheet_id': sid,
                    'sheet_no': r['sheet_no'],
                    'sheet_name': r['sheet_name'],
                    'tb_type_id': r['titleblock_type_id'],
                    'tb_label': r['titleblock_label'],
                    'viewports': [],
                }
                sheets[sid] = g
            g['viewports'].append(r)
        self._sheets = sheets
        self._sheet_list = sorted(
            sheets.values(), key=lambda g: (g['sheet_no'], g['sheet_name']))

    def _default_master_sheet(self):
        if not self._sheet_list:
            return None
        try:
            active = doc.ActiveView
        except Exception:
            active = None
        if active is not None:
            try:
                if isinstance(active, DB.ViewSheet) and active.Id in self._sheets:
                    return active.Id
            except Exception:
                pass
            try:
                for r in self._records:
                    if r['view_id'] == active.Id:
                        return r['sheet_id']
            except Exception:
                pass
        return self._sheet_list[0]['sheet_id']

    # ------------------------------------------------------------------
    # Population helpers
    # ------------------------------------------------------------------

    def _populate_sheet_combo(self, search_text=None):
        self.cmb_master_sheet.Items.Clear()
        st = (search_text or '').strip().lower()
        target_index = 0
        for g in self._sheet_list:
            label = '{}  -  {}'.format(g['sheet_no'], g['sheet_name'])
            if st and st not in label.lower():
                continue
            item = ComboBoxItem()
            item.Content = label
            item.Tag = g['sheet_id']
            self.cmb_master_sheet.Items.Add(item)
            if g['sheet_id'] == self._master_sheet_id:
                target_index = self.cmb_master_sheet.Items.Count - 1
        if self.cmb_master_sheet.Items.Count > 0:
            self.cmb_master_sheet.SelectedIndex = target_index

    def _populate_filter_checks(self):
        f = self._cfg.get('filters', {})
        self.chk_filter_scale.IsChecked = bool(f.get('same_scale', True))
        self.chk_filter_titleblock.IsChecked = bool(f.get('same_titleblock', True))
        self.chk_filter_viewtype.IsChecked = bool(f.get('same_view_type', False))
        self.chk_filter_scopebox.IsChecked = bool(f.get('same_scope_box', False))

    def _populate_match_checks(self):
        m = self._cfg.get('match', {})
        self.chk_match_position.IsChecked = bool(m.get('viewport_position', True))
        self.chk_match_title_pos.IsChecked = bool(m.get('title_position', True))
        self.chk_match_title_line.IsChecked = bool(m.get('title_line_length', True))

    def _populate_title_mode(self):
        self.cmb_title_mode.Items.Clear()
        opt_abs = ComboBoxItem()
        opt_abs.Content = 'Line up on the sheet'
        opt_abs.Tag = 'absolute'
        opt_rel = ComboBoxItem()
        opt_rel.Content = 'Same offset from each view'
        opt_rel.Tag = 'relative'
        self.cmb_title_mode.Items.Add(opt_abs)
        self.cmb_title_mode.Items.Add(opt_rel)
        mode = self._cfg.get('title_mode', 'absolute')
        self.cmb_title_mode.SelectedIndex = 1 if mode == 'relative' else 0
        self._update_title_mode_hint()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_master_changed(self, sender, e):
        item = self.cmb_master_sheet.SelectedItem
        if item is None:
            return
        new_id = item.Tag
        if self._master_sheet_id is not None and new_id == self._master_sheet_id:
            return
        self._master_sheet_id = new_id
        self._rebuild_positions()
        self._update_master_summary()
        self._refresh_targets()

    def _on_master_search_changed(self, sender, e):
        self._populate_sheet_combo(self.txt_master_search.Text)

    def _on_targets_search_changed(self, sender, e):
        self._refresh_targets()

    def _on_filter_changed(self, sender, e):
        self._refresh_targets()

    def _on_position_changed(self, sender, e):
        chk = sender
        pos = chk.Tag
        pos['checked'] = bool(chk.IsChecked)
        self._update_positions_count()
        self._refresh_targets()

    def _on_title_mode_changed(self, sender, e):
        self._update_title_mode_hint()

    def _on_match_changed(self, sender, e):
        # The title line-up / offset chooser only matters when the titles move
        # but the views do not; show it exactly in that state.
        self._update_title_mode_visibility()

    def _on_targets_all(self, sender, e):
        for row in self._target_rows:
            row.set_selected(True)
        self._anchor_index = None
        self._update_status()

    def _on_targets_none(self, sender, e):
        for row in self._target_rows:
            row.set_selected(False)
        self._anchor_index = None
        self._update_status()

    def _on_target_changed(self):
        self._update_status()

    def _on_target_click(self, row, shift):
        """Plain click toggles one sheet; shift-click selects the whole range
        from the last-clicked sheet to this one (File-Explorer style)."""
        try:
            idx = self._target_rows.index(row)
        except ValueError:
            return
        if shift and self._anchor_index is not None and \
                0 <= self._anchor_index < len(self._target_rows):
            lo = min(self._anchor_index, idx)
            hi = max(self._anchor_index, idx)
            for j in range(lo, hi + 1):
                self._target_rows[j].set_selected(True)
        else:
            row.set_selected(not row.selected)
        self._anchor_index = idx
        self._update_status()

    def _on_cancel(self, sender, e):
        self.confirmed = False
        self.Close()

    def _on_run(self, sender, e):
        if not any(p['checked'] for p in self._positions):
            dbhms_ui.info('Check at least one position on the master sheet.',
                          title='Align Views')
            return
        match = self._current_match_opts()
        if not (match['viewport_position'] or match['title_position'] or
                match['title_line_length']):
            dbhms_ui.info('Choose at least one thing to match.',
                          title='Align Views')
            return
        if not self._collect_pairs():
            dbhms_ui.info('Select at least one target sheet to align.',
                          title='Align Views')
            return
        self.confirmed = True
        self.Close()

    # ------------------------------------------------------------------
    # Positions (master sheet)
    # ------------------------------------------------------------------

    def _rebuild_positions(self):
        self._positions = []
        self.pnl_positions.Children.Clear()
        g = self._sheets.get(self._master_sheet_id)
        if g is None:
            self._update_positions_count()
            return
        for i, rec in enumerate(_order_positions(g['viewports'])):
            pos = {'index': i + 1, 'record': rec, 'checked': True, 'chk': None}
            chk = CheckBox()
            try:
                chk.Content = self._make_position_content(pos)
            except Exception:
                chk.Content = '{}  {}'.format(i + 1, rec['view_name'])
            chk.IsChecked = True
            chk.Tag = pos
            chk.Checked += self._on_position_changed
            chk.Unchecked += self._on_position_changed
            pos['chk'] = chk
            self._positions.append(pos)
            self.pnl_positions.Children.Add(chk)
        self._update_positions_count()

    def _make_position_content(self, pos):
        r = pos['record']
        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal
        badge = _num_badge(pos['index'])
        name = TextBlock()
        name.Text = r['view_name']
        name.VerticalAlignment = VerticalAlignment.Center
        name.Margin = Thickness(8, 0, 0, 0)
        name.TextTrimming = _ELLIPSIS
        meta = TextBlock()
        meta.Text = u'  ·  {}'.format(r['scale_label'])
        meta.FontSize = 10.5
        meta.VerticalAlignment = VerticalAlignment.Center
        if _BRUSH_META is not None:
            meta.Foreground = _BRUSH_META
        sp.Children.Add(badge)
        sp.Children.Add(name)
        sp.Children.Add(meta)
        return sp

    def _update_positions_count(self):
        n = sum(1 for p in self._positions if p['checked'])
        self.txt_positions_count.Text = str(n)

    # ------------------------------------------------------------------
    # Target sheets
    # ------------------------------------------------------------------

    def _current_filters(self):
        return {
            'same_scale': bool(self.chk_filter_scale.IsChecked),
            'same_titleblock': bool(self.chk_filter_titleblock.IsChecked),
            'same_view_type': bool(self.chk_filter_viewtype.IsChecked),
            'same_scope_box': bool(self.chk_filter_scopebox.IsChecked),
        }

    def _current_match_opts(self):
        return {
            'viewport_position': bool(self.chk_match_position.IsChecked),
            'title_position': bool(self.chk_match_title_pos.IsChecked),
            'title_line_length': bool(self.chk_match_title_line.IsChecked),
        }

    def _current_title_mode(self):
        item = self.cmb_title_mode.SelectedItem
        if item is not None and item.Tag:
            return item.Tag
        return 'absolute'

    def _pair_target(self, sheet, positions, filters):
        """Return (mapping, clean, cand) for one target sheet.

        clean == the pairing is forced (each checked position has 0 or 1
        candidate and none compete), so no user choice is needed."""
        cand = {}
        for p in positions:
            cand[p['index']] = [v for v in sheet['viewports']
                                if _matches_filter(v, p['record'], filters)]
        clean = True
        for p in positions:
            if len(cand[p['index']]) >= 2:
                clean = False
        used = set()
        mapping = {}
        for p in positions:
            best = None
            best_score = -1
            for v in cand[p['index']]:
                if v['viewport_id'] in used:
                    continue
                sc = _score(v, p['record'])
                if sc > best_score:
                    best_score = sc
                    best = v
            if best is not None:
                mapping[p['index']] = best['viewport_id']
                used.add(best['viewport_id'])
            else:
                mapping[p['index']] = None
        # two positions competing for a single candidate -> needs a choice
        for p in positions:
            if mapping[p['index']] is None and len(cand[p['index']]) > 0:
                clean = False
        return mapping, clean, cand

    def _refresh_targets(self):
        self.pnl_targets.Children.Clear()
        self._target_rows = []
        self._anchor_index = None
        positions = [p for p in self._positions if p['checked']]
        if not positions or self._master_sheet_id is None:
            self.txt_targets_count.Text = '0'
            self._update_status()
            return
        filters = self._current_filters()
        st = (self.txt_targets_search.Text or '').strip().lower() \
            if hasattr(self, 'txt_targets_search') else ''
        count = 0
        for g in self._sheet_list:
            if g['sheet_id'] == self._master_sheet_id:
                continue
            if st:
                hay = '{} {}'.format(g['sheet_no'], g['sheet_name']).lower()
                if st not in hay:
                    continue
            mapping, clean, cand = self._pair_target(g, positions, filters)
            if not any(mapping[k] is not None for k in mapping):
                continue
            row = _TargetRow(g, positions, mapping, clean, cand,
                             self._on_target_click, self._on_target_changed)
            self._target_rows.append(row)
            self.pnl_targets.Children.Add(row.container)
            count += 1
        self.txt_targets_count.Text = str(count)
        self._update_status()

    # ------------------------------------------------------------------
    # Summaries / status
    # ------------------------------------------------------------------

    def _update_master_summary(self):
        g = self._sheets.get(self._master_sheet_id)
        if g is None:
            self.txt_master_summary.Text = ''
            return
        self.txt_master_summary.Text = (
            'Title block: {}   |   Views on sheet: {}'
        ).format(g['tb_label'], len(g['viewports']))

    def _update_title_mode_hint(self):
        mode = self._current_title_mode()
        if mode == 'relative':
            self.txt_title_mode_hint.Text = (
                'Each title keeps the same distance from its own view. Titles '
                'line up only where the views already do.')
        else:
            self.txt_title_mode_hint.Text = (
                'Titles land at the same spot on the sheet, so they line up '
                'across sheets even when the views themselves do not.')

    def _update_title_mode_visibility(self):
        show = bool(self.chk_match_title_pos.IsChecked) and \
            not bool(self.chk_match_position.IsChecked)
        self.pnl_title_mode.Visibility = \
            Visibility.Visible if show else Visibility.Collapsed

    def _update_status(self):
        n = sum(1 for r in self._target_rows if r.selected)
        self.txt_status_count.Text = str(n)
        if not self._sheet_list:
            self.txt_status.Text = 'No viewports in this project.'
        else:
            self.txt_status.Text = 'sheet(s) selected to align.'

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    def _collect_pairs(self):
        pairs = []
        for row in self._target_rows:
            pairs.extend(row.get_pairs())
        return pairs

    def _master_label(self):
        g = self._sheets.get(self._master_sheet_id)
        if g is None:
            return '<none>'
        return '{} - {}'.format(g['sheet_no'], g['sheet_name'])

    def get_run_settings(self):
        return {
            'pairs': self._collect_pairs(),
            'match': self._current_match_opts(),
            'title_mode': self._current_title_mode(),
            'filters': self._current_filters(),
            'master_label': self._master_label(),
        }


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _execute(settings):
    match = settings['match']
    title_mode = settings['title_mode']
    pairs = settings['pairs']

    by_sheet = {}       # (sheet_no, sheet_name) -> [(position, results), ...]
    order = []
    failures = 0

    with revit.Transaction('Align Views'):
        for (master_id, target_id, sheet, pos) in pairs:
            master_vp = doc.GetElement(master_id)
            target_vp = doc.GetElement(target_id)
            if master_vp is None or target_vp is None:
                continue
            results = _align_one(master_vp, target_vp, match, title_mode)
            key = (sheet['sheet_no'], sheet['sheet_name'])
            if key not in by_sheet:
                by_sheet[key] = []
                order.append(key)
            by_sheet[key].append((pos, results))
            for _op, ok, _msg in results:
                if not ok:
                    failures += 1

    # ---- report ----------------------------------------------------------
    output.print_md('## Align Views complete')
    output.print_md('- Master sheet: **{}**'.format(settings['master_label']))
    n_pairs = sum(len(v) for v in by_sheet.values())
    output.print_md('- Views aligned: **{}** across **{}** sheet(s)'.format(
        n_pairs, len(by_sheet)))
    mode_word = 'line up on the sheet' if title_mode == 'absolute' \
        else 'same offset from each view'
    if match['title_position'] and not match['viewport_position']:
        output.print_md('- Title alignment: **{}**'.format(mode_word))
    if failures:
        output.print_md(
            '- Operations that could not be applied: **{}** (usually because '
            'the running Revit version does not expose a setter for that '
            'property)'.format(failures))

    for key in order:
        sheet_no, sheet_name = key
        output.print_md('### {} - {}'.format(sheet_no, sheet_name))
        for pos, results in by_sheet[key]:
            ops = ', '.join(
                ('{}{}'.format(op, '' if ok else ' (FAILED)'))
                for op, ok, _ in results) or 'nothing to do'
            output.print_md('- position {} ({}): {}'.format(
                pos['index'], pos['record']['view_name'], ops))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    cfg = _load_config()

    records = _collect_records()
    if not records:
        forms.alert(
            'No viewports were found in this project. '
            'Place at least one view on a sheet first.',
            exitscript=True)

    form = AlignViewsForm(cfg, records)
    form.ShowDialog()
    if not form.confirmed:
        return

    settings = form.get_run_settings()
    if not settings['pairs']:
        return

    # remember the user's choices for next time
    cfg['filters'] = settings['filters']
    cfg['match'] = settings['match']
    cfg['title_mode'] = settings['title_mode']
    _save_config(cfg)

    try:
        _execute(settings)
    except Exception:
        output.print_md('### Align Views failed')
        output.print_md('```\n{}\n```'.format(traceback.format_exc()))
        dbhms_ui.info(
            'Align Views ran into an error - see the pyRevit output window '
            'for the traceback.', title='Align Views')


if __name__ == '__main__':
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
