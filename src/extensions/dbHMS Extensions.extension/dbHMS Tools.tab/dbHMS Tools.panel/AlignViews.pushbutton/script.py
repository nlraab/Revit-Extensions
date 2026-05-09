# -*- coding: utf-8 -*-
"""Align Views - match viewport position and view title across sheets.

Pick a "master" viewport, pick the sheets you want to consider, and the tool
shows every other viewport whose properties match (same scale, same title
block, optionally same view type / scope box). Check the ones you want to
align, choose what to match (viewport position on sheet, view title position,
view title line length), and the tool snaps every selected viewport so the
view content lands at the same XY on its sheet as the master and the title
sits in the same spot.

Modeled after IDEATE Align Views, but lives entirely inside Revit/pyRevit.
"""

__title__ = 'Align\nViews'
__author__ = 'Nathaniel'

import os
import json
import codecs
import traceback

from pyrevit import revit, DB, forms, script
import dbhms_ui
import dbhms_telemetry

# WPF / .NET imports for the dynamic form controls
import clr  # noqa: F401
from System.Windows import Thickness, Visibility
from System.Windows.Controls import (
    CheckBox, ComboBoxItem, TextBlock, StackPanel
)
from System.Windows.Media import Brushes


# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
FORM_XAML = os.path.join(SCRIPT_DIR, 'AlignViewsForm.xaml')

doc = revit.doc
output = script.get_output()


def _load_config():
    try:
        with codecs.open(CONFIG_PATH, 'r', 'utf-8') as f:
            return json.load(f)
    except Exception:
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
        }


def _save_config(cfg):
    with codecs.open(CONFIG_PATH, 'w', 'utf-8') as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


# --------------------------------------------------------------------------
# Project data gathering
# --------------------------------------------------------------------------

def _scale_label(scale):
    """Render a Revit integer view scale (e.g. 96) as '1/8\" = 1'-0\"'."""
    if not scale or scale <= 0:
        return '<no scale>'
    # Map common architectural scales; fall back to "1\" = N\"".
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
    """Return a dict of sheet ElementId -> titleblock type ElementId,
    built from a SINGLE project-wide collector query (fast on large projects)."""
    cache = {}
    try:
        all_tbs = (DB.FilteredElementCollector(doc)
                     .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
                     .WhereElementIsNotElementType()
                     .ToElements())
        for tb in all_tbs:
            sid = tb.OwnerViewId   # for a title block placed on a sheet this IS the sheet Id
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
    """Return the scope box ElementId assigned to the view, or
    InvalidElementId if none."""
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
    # Compact human label for view type
    try:
        return str(vt)
    except Exception:
        return '?'


def _collect_records():
    """Walk every Viewport in the project and return a list of dicts
    describing each one, suitable for filtering and display.

    Performance notes
    -----------------
    * Title-block lookup: ONE project-wide collector query builds a
      sheet_id -> type_id dict.  The old approach did one
      FilteredElementCollector(doc, sheet.Id) call per sheet, which
      scaled terribly on large projects (300 sheets = 300 queries).
    * Label caches: titleblock and scope-box string labels are computed
      once per unique type/element and reused everywhere.
    """
    # --- single-shot title-block cache (the big win) ---
    sheet_titleblock_cache = _build_titleblock_cache()

    # --- lazy label caches so we only compute each string once ---
    tb_label_cache   = {}   # type_id  -> str
    scope_name_cache = {}   # scope_id -> str

    vps = (DB.FilteredElementCollector(doc)
             .OfClass(DB.Viewport)
             .ToElements())

    records = []
    for vp in vps:
        try:
            sheet = doc.GetElement(vp.SheetId)
            view  = doc.GetElement(vp.ViewId)
            if sheet is None or view is None:
                continue

            sheet_id   = sheet.Id
            tb_type_id = sheet_titleblock_cache.get(
                             sheet_id, DB.ElementId.InvalidElementId)

            if tb_type_id not in tb_label_cache:
                tb_label_cache[tb_type_id] = _titleblock_label(tb_type_id)

            scale    = view.Scale if hasattr(view, 'Scale') else 0
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
                'viewport_id':       vp.Id,
                'view_id':           view.Id,
                'sheet_id':          sheet_id,
                'sheet_no':          sheet_no or '',
                'sheet_name':        sheet_name or '',
                'view_name':         view_name or '',
                'scale':             scale,
                'scale_label':       _scale_label(scale),
                'view_type':         view_type,
                'view_type_label':   _viewtype_label(view_type),
                'scope_box_id':      scope_id,
                'scope_box_name':    scope_name_cache[scope_id],
                'titleblock_type_id': tb_type_id,
                'titleblock_label':  tb_label_cache[tb_type_id],
            })
        except Exception:
            continue

    records.sort(key=lambda r: (r['sheet_no'], r['view_name']))
    return records


def _pick_default_master(records):
    """Default master = a viewport on the currently active sheet, or the
    viewport that hosts the currently active view, or the first record."""
    if not records:
        return None
    try:
        active = doc.ActiveView
    except Exception:
        active = None
    if active is None:
        return records[0]

    # Case 1: the active view IS a sheet -> any viewport on that sheet
    try:
        if isinstance(active, DB.ViewSheet):
            for r in records:
                if r['sheet_id'] == active.Id:
                    return r
    except Exception:
        pass

    # Case 2: the active view is a regular view that's placed on a sheet
    try:
        for r in records:
            if r['view_id'] == active.Id:
                return r
    except Exception:
        pass

    return records[0]


# --------------------------------------------------------------------------
# Filter & alignment logic
# --------------------------------------------------------------------------

def _matches_filter(rec, master, filters):
    """Does `rec` match `master` under the given filter checkbox state?"""
    if rec['viewport_id'] == master['viewport_id']:
        return False  # never include the master in the candidates list

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


def _try_set(obj, attr, value):
    """Set obj.attr = value if writable; return True/False."""
    try:
        setattr(obj, attr, value)
        return True
    except Exception:
        return False


def _align_one(master_vp, target_vp, match_opts):
    """Apply the chosen match options from master_vp onto target_vp.
    Returns a list of (op, ok, msg) tuples for the report."""
    results = []

    if match_opts.get('viewport_position'):
        try:
            master_center = master_vp.GetBoxCenter()
            target_vp.SetBoxCenter(master_center)
            results.append(('viewport position', True, ''))
        except Exception as e:
            results.append(('viewport position', False, str(e)))

    if match_opts.get('title_position'):
        try:
            offset = master_vp.LabelOffset  # XYZ in newer API, UV in older
            ok = _try_set(target_vp, 'LabelOffset', offset)
            if ok:
                results.append(('title position', True, ''))
            else:
                results.append(('title position', False,
                                'LabelOffset is not settable in this Revit version'))
        except Exception as e:
            results.append(('title position', False, str(e)))

    if match_opts.get('title_line_length'):
        try:
            length = master_vp.LabelLineLength
            ok = _try_set(target_vp, 'LabelLineLength', length)
            if ok:
                # also try to copy the line offset if the API exposes it
                try:
                    line_offset = master_vp.LabelLineOffset
                    _try_set(target_vp, 'LabelLineOffset', line_offset)
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
# Main configuration form
# --------------------------------------------------------------------------

class AlignViewsForm(forms.WPFWindow):
    """Master view + sheet picker + filters + viewport list."""

    def __init__(self, cfg, records):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._cfg = cfg
        self._records = records
        self.confirmed = False

        # Master record (mutable). Default to a viewport on the active
        # sheet/view if possible, otherwise the first record.
        self._master = _pick_default_master(records)

        # Per-viewport checkbox state: ElementId -> bool
        self._view_checks = {}

        # ---- Wire static controls ----------------------------------------
        self._populate_master_combo(records)
        self._populate_filter_checks()
        self._populate_match_checks()

        # Events
        self.cmb_master.SelectionChanged += self._on_master_changed
        self.txt_master_search.TextChanged += self._on_master_search_changed
        self.txt_views_search.TextChanged += self._on_views_search_changed

        for chk in (self.chk_filter_scale, self.chk_filter_titleblock,
                    self.chk_filter_viewtype, self.chk_filter_scopebox):
            chk.Checked += self._on_filter_changed
            chk.Unchecked += self._on_filter_changed

        self.btn_views_all.Click += self._on_views_all
        self.btn_views_none.Click += self._on_views_none
        self.btn_views_refresh.Click += lambda s, e: self._refresh_views()
        self.btn_cancel.Click += self._on_cancel
        self.btn_run.Click += self._on_run

        # Initial population of the right-hand viewport list
        self._refresh_views()
        self._update_master_summary()
        self._update_status()

    # ------------------------------------------------------------------
    # Population helpers
    # ------------------------------------------------------------------

    def _populate_master_combo(self, records, search_text=None):
        self.cmb_master.Items.Clear()
        st = (search_text or '').strip().lower()
        target_index = 0
        master_id = self._master['viewport_id'] if self._master else None
        for i, r in enumerate(records):
            label = '{}  -  {}  ({})'.format(
                r['sheet_no'], r['view_name'], r['scale_label'])
            if st and st not in label.lower():
                continue
            item = ComboBoxItem()
            item.Content = label
            item.Tag = r
            self.cmb_master.Items.Add(item)
            if master_id is not None and r['viewport_id'] == master_id:
                target_index = self.cmb_master.Items.Count - 1
        if self.cmb_master.Items.Count > 0:
            self.cmb_master.SelectedIndex = target_index

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

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_master_changed(self, sender, e):
        item = self.cmb_master.SelectedItem
        if item is None:
            return
        new_master = item.Tag
        # Skip the no-op selection events that fire when the combo is rebuilt
        # by the search box — only react when the actual viewport changes.
        if self._master is not None and \
                new_master['viewport_id'] == self._master['viewport_id']:
            return
        self._master = new_master
        # Reset per-viewport checkboxes — the candidate set has changed
        self._view_checks = {}
        self._refresh_views()
        self._update_master_summary()
        self._update_status()

    def _on_master_search_changed(self, sender, e):
        self._populate_master_combo(self._records, self.txt_master_search.Text)

    def _on_views_search_changed(self, sender, e):
        self._refresh_views()

    def _on_filter_changed(self, sender, e):
        self._refresh_views()
        self._update_status()

    def _on_view_check_changed(self, sender, e):
        cb = sender
        self._view_checks[cb.Tag] = bool(cb.IsChecked)
        self._update_status()

    def _on_views_all(self, sender, e):
        for cb in list(self.pnl_views.Children):
            if isinstance(cb, CheckBox):
                cb.IsChecked = True
        self._update_status()

    def _on_views_none(self, sender, e):
        for cb in list(self.pnl_views.Children):
            if isinstance(cb, CheckBox):
                cb.IsChecked = False
        self._update_status()

    def _on_cancel(self, sender, e):
        self.confirmed = False
        self.Close()

    def _on_run(self, sender, e):
        if self._master is None:
            dbhms_ui.info('Pick a master viewport first.')
            return
        if not any(self._view_checks.values()):
            dbhms_ui.info('Check at least one viewport to align.')
            return
        if not (self.chk_match_position.IsChecked or
                self.chk_match_title_pos.IsChecked or
                self.chk_match_title_line.IsChecked):
            dbhms_ui.info('Choose at least one thing to match.')
            return
        self.confirmed = True
        self.Close()

    # ------------------------------------------------------------------
    # Refresh logic
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

    def _candidate_records(self):
        if self._master is None:
            return []
        filters = self._current_filters()
        st = (self.txt_views_search.Text or '').strip().lower() \
            if hasattr(self, 'txt_views_search') else ''
        out = []
        for r in self._records:
            if not _matches_filter(r, self._master, filters):
                continue
            if st:
                hay = '{} {} {}'.format(
                    r['sheet_no'], r['view_name'], r['titleblock_label']).lower()
                if st not in hay:
                    continue
            out.append(r)
        return out

    def _refresh_views(self):
        self.pnl_views.Children.Clear()
        candidates = self._candidate_records()
        for r in candidates:
            cb = CheckBox()
            cb.Content = '{}  -  {}    ({}, {})'.format(
                r['sheet_no'], r['view_name'],
                r['scale_label'], r['titleblock_label'])
            cb.Tag = r['viewport_id']
            cb.IsChecked = self._view_checks.get(r['viewport_id'], True)
            # Remember default-checked state so the running set stays in sync
            self._view_checks[r['viewport_id']] = bool(cb.IsChecked)
            cb.Checked += self._on_view_check_changed
            cb.Unchecked += self._on_view_check_changed
            self.pnl_views.Children.Add(cb)

        # Drop any stale keys that aren't candidates anymore
        live_ids = set(r['viewport_id'] for r in candidates)
        self._view_checks = {k: v for k, v in self._view_checks.items()
                             if k in live_ids}

        self.txt_views_header.Text = 'Views to Align ({})'.format(
            len(candidates))
        self._update_status()

    def _update_master_summary(self):
        if self._master is None:
            self.txt_master_summary.Text = ''
            return
        m = self._master
        self.txt_master_summary.Text = (
            'Scale: {}   |   Title block: {}   |   '
            'Scope box: {}   |   View type: {}'
        ).format(m['scale_label'], m['titleblock_label'],
                 m['scope_box_name'], m['view_type_label'])

    def _update_status(self):
        n = sum(1 for v in self._view_checks.values() if v)
        if self._master is None:
            self.txt_status.Text = 'No viewports in this project.'
        else:
            self.txt_status.Text = '{} viewport(s) selected to align.'.format(n)

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    def get_run_settings(self):
        target_ids = [vid for vid, on in self._view_checks.items() if on]
        return {
            'master': self._master,
            'target_ids': target_ids,
            'match': self._current_match_opts(),
            'filters': self._current_filters(),
        }


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _execute(settings):
    master_id = settings['master']['viewport_id']
    master_vp = doc.GetElement(master_id)
    if master_vp is None:
        dbhms_ui.info('Master viewport could not be loaded; aborting.')
        return

    match_opts = settings['match']
    target_ids = settings['target_ids']

    summary = []        # (sheet_no, view_name, [(op, ok, msg), ...])
    failures = 0

    with revit.Transaction('Align Views'):
        for tid in target_ids:
            vp = doc.GetElement(tid)
            if vp is None:
                continue
            try:
                view = doc.GetElement(vp.ViewId)
                sheet = doc.GetElement(vp.SheetId)
                view_name = view.Name if view else '<?>'
                sheet_no = sheet.SheetNumber if sheet else '?'
            except Exception:
                view_name, sheet_no = '<?>', '?'

            results = _align_one(master_vp, vp, match_opts)
            summary.append((sheet_no, view_name, results))
            for _op, ok, _msg in results:
                if not ok:
                    failures += 1

    # Report
    output.print_md('## Align Views complete')
    output.print_md(
        '- Master: **{} - {}**'.format(
            settings['master']['sheet_no'],
            settings['master']['view_name']))
    output.print_md('- Targets aligned: **{}**'.format(len(summary)))
    if failures:
        output.print_md(
            '- Operations that could not be applied: **{}** '
            '(usually because the running Revit version does not expose a '
            'setter for that property)'.format(failures))

    output.print_md('### Per-viewport results')
    for sno, vname, results in summary:
        ops = ', '.join(
            ('{}{}'.format(op, '' if ok else ' (FAILED)'))
            for op, ok, _ in results
        )
        output.print_md('- {} - {}: {}'.format(sno, vname, ops))


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
    if not settings['target_ids']:
        return

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
