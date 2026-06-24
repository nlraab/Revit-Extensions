# -*- coding: utf-8 -*-
"""Clash Browser - the main UI for working through clashes.

Reads the project's clashes.json on open and renders every clash in the
DataGrid with status / trade pills. Status changes (via the detail
panel's dropdown), trade reassignments, and posted comments persist back
to clashes.json immediately, with a history entry recording who did
what and when.

"Show in 3D" navigates to the selected clash in Revit's own 3D view
(via the clash_view package).

If no clashes have been detected yet, the form shows an empty state with
a hint to open Run Clash Test.

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
"""

__title__  = 'Clash\nBrowser'
__author__ = 'Nathaniel'
__doc__    = ('Browse, filter, comment on, assign, and resolve every '
              'clash in the current project.')

import os
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import Visibility
from System.Windows.Data import PropertyGroupDescription, ListCollectionView
from System.Windows.Media import SolidColorBrush, Color
from System.Collections.Generic import List as NetList

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_core import config, persistence, project, users, models, browser_filters, bulk_edit, history_format, filter_presets
from clash_core.models import _now_iso


SCRIPT_DIR        = os.path.dirname(__file__)
FORM_XAML         = os.path.join(SCRIPT_DIR, 'ClashBrowserForm.xaml')
HISTORY_DIALOG_XAML = os.path.join(SCRIPT_DIR, 'HistoryDialog.xaml')


# ---------------------------------------------------------------------------
# Color tokens for status / trade pills
# ---------------------------------------------------------------------------

def _brush(hex_str):
    h = hex_str.lstrip('#')
    return SolidColorBrush(Color.FromRgb(
        int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    ))

STATUS_BRUSHES = {
    'Open':     _brush('#E53E3E'),
    'Reviewed': _brush('#D69E2E'),
    'Approved': _brush('#2B6CB0'),
    'Resolved': _brush('#38A169'),
}

TRADE_BG_BRUSHES = {
    'Mechanical':       _brush('#DBE5EE'),
    'Electrical':       _brush('#EDE2C6'),
    'Plumbing':         _brush('#D5E5E0'),
    'Fire Protection':  _brush('#EAD2D2'),
    'Technology':       _brush('#E0D5E8'),
    'Architectural':    _brush('#E5E0D5'),
    'Structural':       _brush('#D8DCE0'),
}

TRADE_FG_BRUSHES = {
    'Mechanical':       _brush('#3D5A75'),
    'Electrical':       _brush('#7A5C1F'),
    'Plumbing':         _brush('#3F6D6B'),
    'Fire Protection':  _brush('#7A4040'),
    'Technology':       _brush('#5E4878'),
    'Architectural':    _brush('#605A4D'),
    'Structural':       _brush('#3E454F'),
}

GRAY_BRUSH = _brush('#A0AEC0')
DARK_GRAY_BRUSH = _brush('#4A5568')


# ---------------------------------------------------------------------------
# View-model
# ---------------------------------------------------------------------------

class ClashRow(object):
    """One row in the clash grid. Wraps a real clash dict so changes write back."""
    def __init__(self, clash_dict, test_name_lookup):
        self.Source = clash_dict   # link back to underlying dict for in-place edits
        self.Id     = clash_dict.get('seq') or '?'
        self.TestName = test_name_lookup.get(clash_dict.get('test_id'), '(unknown test)')

        ref_a = clash_dict.get('ref_a') or {}
        ref_b = clash_dict.get('ref_b') or {}
        self.ElementA = (ref_a.get('name')
                         or _short_for_id(ref_a.get('element_id')))
        self.ElementB = (ref_b.get('name')
                         or _short_for_id(ref_b.get('element_id')))

        self.Level  = '-'  # level lookup is a future enhancement (needs Revit)
        self.Status = clash_dict.get('status') or 'Open'
        self.Trade  = clash_dict.get('assignee') or '-'
        self.Kind   = (clash_dict.get('kind') or 'hard').lower()

        comments = clash_dict.get('comments') or []
        self.CommentCount = len(comments)
        self.CommentCountStr = str(self.CommentCount) if self.CommentCount else ''

        self.StatusBrush  = STATUS_BRUSHES.get(self.Status, GRAY_BRUSH)
        self.TradeBgBrush = TRADE_BG_BRUSHES.get(self.Trade, GRAY_BRUSH)
        self.TradeFgBrush = TRADE_FG_BRUSHES.get(self.Trade, DARK_GRAY_BRUSH)

        self.ElemAId = ref_a.get('element_id') or 0
        self.ElemBId = ref_b.get('element_id') or 0
        self.ElemASource = ref_a.get('source') or 'host'
        self.ElemBSource = ref_b.get('source') or 'host'

        # Pre-computed lowercase search-haystack for the Search filter box —
        # rebuilt on every comment-add via refresh_from_source.
        self.SearchHaystack = browser_filters.build_search_haystack(
            clash_dict, self.TestName)

    def refresh_from_source(self):
        """Re-read computed display fields from the underlying clash dict."""
        self.Status = self.Source.get('status') or 'Open'
        self.Trade  = self.Source.get('assignee') or '-'
        self.StatusBrush  = STATUS_BRUSHES.get(self.Status, GRAY_BRUSH)
        self.TradeBgBrush = TRADE_BG_BRUSHES.get(self.Trade, GRAY_BRUSH)
        self.TradeFgBrush = TRADE_FG_BRUSHES.get(self.Trade, DARK_GRAY_BRUSH)
        comments = self.Source.get('comments') or []
        self.CommentCount = len(comments)
        self.CommentCountStr = str(self.CommentCount) if self.CommentCount else ''
        # Rebuild the haystack so search picks up newly-posted comments
        # and any status / trade reassignments.
        self.SearchHaystack = browser_filters.build_search_haystack(
            self.Source, self.TestName)


def _short_for_id(elem_id):
    if elem_id is None:
        return '<no id>'
    return 'Element {}'.format(elem_id)


def _read_clashes_safe(project_hash):
    """Return (clashes_list, test_name_lookup, banner_or_None)."""
    if not project_hash:
        return ([], {}, '(no active project)')
    try:
        data = persistence.read_clashes(project_hash)
        clashes = data.get('clashes') or []
    except persistence.SharedFolderNotConfigured as ex:
        return ([], {}, str(ex))
    except Exception as ex:
        return ([], {}, 'Could not read clashes.json: {}'.format(ex))

    # Test name lookup
    name_lookup = {}
    try:
        library = persistence.read_global_test_library()
        for t in library.get('tests') or []:
            tid = t.get('id')
            if tid:
                name_lookup[tid] = t.get('name', '<unnamed>')
    except Exception:
        pass

    return (clashes, name_lookup, None)


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------

class ClashBrowserForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._project_hash = None
        self._suppress_change_events = False  # set True when programmatically updating dropdowns
        # True once the filter events are wired AND _apply_filters is safe to
        # call. _load_clashes uses this to decide whether to re-apply filters
        # itself (refresh case) vs. leaving that to __init__ (first-load case).
        self._filters_wired = False
        self._author = self._resolve_author()

        self._resolve_project_hash()
        self._load_clashes()

        # Wire events
        self.dg_clashes.SelectionChanged += self._on_selection_changed
        self.btn_close.Click             += self._on_close
        self.btn_refresh.Click           += self._on_refresh
        self.btn_post_comment.Click      += self._on_post_comment
        self.btn_reset_filters.Click     += self._on_reset_filters
        self.cmb_detail_status.SelectionChanged += self._on_status_changed
        self.cmb_detail_trade.SelectionChanged  += self._on_trade_changed
        self.cmb_group_by.SelectionChanged       += self._on_group_changed

        # "Show in 3D" navigates to the clash in Revit's own 3D view, and
        # Save Viewpoint are wired against the clash_view package.
        self.btn_show_3d.Click       += self._on_show_in_3d
        self.btn_history.Click        += self._on_show_history
        # Bulk action bar (visible when 2+ rows selected) — Iteration 9.
        # Group is left on the placeholder; "group these clashes as a
        # unit" needs its own data-model design pass.
        self.btn_bulk_status.Click   += self._on_bulk_status
        self.btn_bulk_reassign.Click += self._on_bulk_reassign
        self.btn_bulk_resolve.Click  += self._on_bulk_resolve
        for btn_name in ('btn_bulk_group',):
            getattr(self, btn_name).Click += self._on_coming_soon
        # Filter presets (Iteration 11). Built-ins always show; user
        # saves go under them. Right-click a user preset to delete it.
        self.btn_save_preset.Click += self._on_save_preset

        # Filter wiring (Iteration 5). Trade and Status checkboxes don't have
        # individual x:Name attributes — we walk the WrapPanels' Children
        # collection and hook every CheckBox the same way. Search filter is
        # immediate (TextChanged); for normal-sized clash lists this is fast
        # enough that debouncing isn't worth the complexity.
        for chk in self._iter_checkboxes(self.wp_trade_filter):
            chk.Checked   += self._on_filter_changed
            chk.Unchecked += self._on_filter_changed
        for chk in self._iter_checkboxes(self.wp_status_filter):
            chk.Checked   += self._on_filter_changed
            chk.Unchecked += self._on_filter_changed
        self.cmb_test_filter.SelectionChanged += self._on_filter_changed
        self.txt_search.TextChanged           += self._on_filter_changed
        # First filter pass against the data we just loaded.
        self._filters_wired = True
        self._apply_filters()

        # Populate the filter-preset button list (built-ins + saved).
        self._build_preset_buttons()

        # Catch-up viewpoint generation: any clash without a viewpoint
        # (older data from before viewpoint generation existed, or new
        # clashes detected via a path that didn't auto-generate) gets
        # one now. Run Clash Test already auto-generates for new
        # clashes, so on a healthy project this is usually a no-op.
        # Wrapped defensively — viewpoint generation failure must NOT
        # block opening the Browser.
        try:
            self._catch_up_viewpoints()
        except Exception:
            pass

        # The first-thumbnail-doesn't-show bug: when __init__ sets
        # SelectedIndex = 0 (or WPF's auto-sync does it from the new
        # ItemsSource), SelectionChanged fires synchronously and
        # _render_viewpoint runs against an Image control that's been
        # constructed but not yet laid out / displayed. WPF doesn't
        # repaint that Image when the window finally shows, so the
        # first thumbnail stays blank until the user clicks a different
        # row and back. Hooking Loaded — which fires AFTER the window
        # has been measured + arranged + shown — and re-rendering the
        # current selection at that moment forces WPF to paint the
        # thumbnail with the now-fully-laid-out image.
        self.Loaded += self._on_loaded

        if len(self._clashes) > 0:
            self.dg_clashes.SelectedIndex = 0
        else:
            self._show_empty_detail()

    # --- Loading --------------------------------------------------------

    def _resolve_project_hash(self):
        try:
            from pyrevit import revit
            doc = revit.doc
            if doc is None:
                return
            ph = project.project_hash_for(doc)
            if ph:
                self._project_hash = ph
        except Exception:
            pass

    def _resolve_author(self):
        try:
            from pyrevit import revit
            return users.current_user(revit.uiapp)
        except Exception:
            return 'unknown'

    def _load_clashes(self):
        clash_dicts, name_lookup, banner = _read_clashes_safe(self._project_hash)
        self._clash_dicts = clash_dicts
        self._test_names  = name_lookup

        self._clashes = [ClashRow(c, name_lookup) for c in clash_dicts]

        # Wrap rows in an explicit ListCollectionView. This gives grouping a
        # known-stable .NET source instead of relying on WPF building an
        # implicit default view over a Python list, which has been the source
        # of crashes when GroupDescriptions change at runtime under
        # IronPython-hosted WPF.
        backing = NetList[object]()
        for row in self._clashes:
            backing.Add(row)
        self._view = ListCollectionView(backing)
        self.dg_clashes.ItemsSource = self._view

        # Refresh the Test dropdown to match the actual loaded data — the
        # XAML-baked items are mockup placeholders. Done with event
        # suppression so populating the combobox doesn't itself trigger
        # _on_filter_changed and double-apply.
        self._populate_test_filter()
        # Re-apply any active grouping after a reload.
        self._apply_current_grouping()
        # Re-apply current filter state against the freshly-loaded rows.
        # On first load (called from __init__ before filter handlers are
        # wired), skip — __init__ calls _apply_filters explicitly once
        # everything's wired. On refresh, apply now.
        if self._filters_wired:
            self._apply_filters()
        else:
            self.txt_grid_header.Text = 'Clashes ({})'.format(len(self._clashes))

        if banner:
            self.txt_status.Text = banner
        elif not self._clashes:
            self.txt_status.Text = (
                'No clashes detected yet. Open Run Clash Test to scan the project.'
            )
        else:
            self.txt_status.Text = '{} clash(es) loaded.'.format(len(self._clashes))

    # --- Filter wiring (Iteration 5) ------------------------------------

    @staticmethod
    def _iter_checkboxes(panel):
        """Yield the CheckBox children of a WrapPanel/StackPanel.

        Trade and Status filter checkboxes don't have individual x:Names
        in the XAML; we iterate the parent panel and treat any CheckBox
        child as a filter. CheckBox.Content (string) is the filter key.
        """
        from System.Windows.Controls import CheckBox
        for child in panel.Children:
            if isinstance(child, CheckBox):
                yield child

    def _populate_test_filter(self):
        """Replace the XAML's mockup placeholder items with one item per
        unique test name in the loaded clashes, plus the "(All tests)"
        sentinel.

        Done with event suppression so populating the dropdown doesn't
        itself fire SelectionChanged → _apply_filters and double-apply.
        """
        from System.Windows.Controls import ComboBoxItem
        previous = self._suppress_change_events
        self._suppress_change_events = True
        try:
            self.cmb_test_filter.Items.Clear()
            all_item = ComboBoxItem()
            all_item.Content = "(All tests)"
            self.cmb_test_filter.Items.Add(all_item)
            unique_names = sorted({row.TestName for row in self._clashes})
            for name in unique_names:
                item = ComboBoxItem()
                item.Content = name
                self.cmb_test_filter.Items.Add(item)
            self.cmb_test_filter.SelectedIndex = 0
        finally:
            self._suppress_change_events = previous

    def _checked_set(self, panel):
        """Return the set of checked checkbox Contents in a panel.

        Used to drive trade/status filtering. None if the panel has no
        checkboxes (defensive — shouldn't happen).
        """
        out = set()
        had_any = False
        for chk in self._iter_checkboxes(panel):
            had_any = True
            try:
                if chk.IsChecked:
                    out.add(str(chk.Content))
            except Exception:
                pass
        return out if had_any else None

    def _collect_filter_args(self):
        """Snapshot the current UI filter state into the kwargs that
        browser_filters.row_passes expects."""
        allowed_trades   = self._checked_set(self.wp_trade_filter)
        allowed_statuses = self._checked_set(self.wp_status_filter)
        test_filter = None
        item = self.cmb_test_filter.SelectedItem
        if item is not None:
            test_filter = getattr(item, 'Content', None)
        search_text = self.txt_search.Text or ""
        return allowed_trades, allowed_statuses, test_filter, search_text

    def _apply_filters(self):
        """Re-apply the current UI filter state to self._view and refresh
        the displayed count. Called on every filter event AND after data
        reload (refresh)."""
        if self._view is None:
            return
        allowed_trades, allowed_statuses, test_filter, search_text = (
            self._collect_filter_args())

        def predicate(row):
            return browser_filters.row_passes(
                row,
                allowed_trades, allowed_statuses,
                test_filter, search_text,
            )

        # Need a .NET Predicate-typed delegate for ListCollectionView.Filter.
        # IronPython auto-coerces a Python callable to Predicate[Object] when
        # the property setter expects it, but being explicit keeps the
        # interop predictable.
        from System import Predicate, Object
        try:
            self._view.Filter = Predicate[Object](predicate)
        except Exception:
            # Fallback for IronPython versions where the explicit Predicate
            # construction trips: assign the raw callable.
            try:
                self._view.Filter = predicate
            except Exception:
                pass
        try:
            self._view.Refresh()
        except Exception:
            pass

        visible = 0
        try:
            for _ in self._view:
                visible += 1
        except Exception:
            visible = len(self._clashes)
        self.txt_grid_header.Text = 'Clashes ({} of {})'.format(
            visible, len(self._clashes))

    def _on_filter_changed(self, sender, args):
        if self._suppress_change_events:
            return
        self._apply_filters()

    # --- Selection / detail panel ---------------------------------------

    def _on_selection_changed(self, sender, args):
        selected = list(self.dg_clashes.SelectedItems)
        count = len(selected)
        if count > 1:
            self.brd_bulk_actions.Visibility = Visibility.Visible
            self.txt_bulk_count.Text = '{} selected:'.format(count)
        else:
            # Hidden (not Collapsed) keeps the bar's layout slot reserved
            # so the DataGrid below doesn't jump up/down as the bar
            # appears/disappears with selection changes.
            self.brd_bulk_actions.Visibility = Visibility.Hidden

        # Only re-render the detail panel when exactly one row is
        # selected. Re-rendering on every multi-select adjustment
        # (ctrl-click adding/removing rows, even when the FIRST selected
        # row hasn't changed) caused visible glitching as the BitmapImage
        # reloaded on each click. In multi-select mode the bulk-action
        # bar is the relevant info; the detail panel keeps whatever was
        # last shown until the user drops back to a single selection.
        if count == 1:
            self._populate_detail(selected[0])
        elif count == 0 and not self._clashes:
            self._show_empty_detail()

    def _show_empty_detail(self):
        self.txt_clash_title.Text  = '(no clashes)'
        self.txt_clash_test.Text   = ''
        self.txt_clash_kind.Text   = ''
        self.txt_elem_a_name.Text  = ''
        self.txt_elem_a_id.Text    = ''
        self.txt_elem_b_name.Text  = ''
        self.txt_elem_b_id.Text    = ''
        self.ic_comments.ItemsSource = []
        self.txt_comments_header.Text = 'Comments (0)'
        self._render_viewpoint(None)

    def _populate_detail(self, row):
        self._suppress_change_events = True
        try:
            self.txt_clash_title.Text = 'Clash #{}'.format(row.Id)
            self.txt_clash_test.Text  = row.TestName
            self.txt_clash_kind.Text  = row.Kind.upper()

            def _id_line(eid, source):
                if not source or source == 'host':
                    return 'ID {} - host model'.format(eid)
                return 'ID {} - {}'.format(eid, source)

            self.txt_elem_a_name.Text = row.ElementA
            self.txt_elem_a_id.Text   = _id_line(row.ElemAId, row.ElemASource)
            self.txt_elem_b_name.Text = row.ElementB
            self.txt_elem_b_id.Text   = _id_line(row.ElemBId, row.ElemBSource)

            self._set_combo_by_text(self.cmb_detail_status, row.Status)
            self._set_combo_by_text(self.cmb_detail_trade,  row.Trade)

            comments = row.Source.get('comments') or []
            self.ic_comments.ItemsSource = [
                _CommentRow(c) for c in comments
            ]
            self.txt_comments_header.Text = 'Comments ({})'.format(len(comments))
            self._render_viewpoint(row.Source)
        finally:
            self._suppress_change_events = False

    @staticmethod
    def _set_combo_by_text(combo, text):
        for item in combo.Items:
            content = getattr(item, 'Content', None)
            if content == text:
                combo.SelectedItem = item
                return

    # --- Status / trade changes (write back) ----------------------------

    def _selected_row(self):
        sel = list(self.dg_clashes.SelectedItems)
        return sel[0] if sel else None

    def _on_status_changed(self, sender, args):
        if self._suppress_change_events:
            return
        row = self._selected_row()
        if row is None:
            return
        new_status = self._get_combo_text(self.cmb_detail_status)
        old_status = row.Source.get('status') or 'Open'
        if not new_status or new_status == old_status:
            return
        row.Source['status'] = new_status
        history = row.Source.setdefault('history', [])
        history.append(models.make_history_entry(
            self._author, 'status_changed',
            before=old_status, after=new_status,
        ))
        row.refresh_from_source()
        self.dg_clashes.Items.Refresh()
        # Re-apply filters: changing status from Open to Resolved with the
        # Resolved checkbox unchecked should hide the row immediately.
        if self._filters_wired:
            self._apply_filters()
        self._restore_selection(row)
        if self._save_clashes(action_label="Status -> {}".format(new_status)):
            pass

    def _on_trade_changed(self, sender, args):
        if self._suppress_change_events:
            return
        row = self._selected_row()
        if row is None:
            return
        new_trade = self._get_combo_text(self.cmb_detail_trade)
        old_trade = row.Source.get('assignee') or '-'
        if not new_trade or new_trade == old_trade:
            return
        row.Source['assignee'] = new_trade
        history = row.Source.setdefault('history', [])
        history.append(models.make_history_entry(
            self._author, 'reassigned',
            before=old_trade, after=new_trade,
        ))
        row.refresh_from_source()
        self.dg_clashes.Items.Refresh()
        # Re-apply filters: reassigning a clash to a trade whose checkbox
        # is unchecked should hide the row immediately.
        if self._filters_wired:
            self._apply_filters()
        self._restore_selection(row)
        self._save_clashes(action_label="Reassigned to {}".format(new_trade))

    @staticmethod
    def _get_combo_text(combo):
        item = combo.SelectedItem
        if item is None:
            return None
        return getattr(item, 'Content', None)

    def _restore_selection(self, row):
        try:
            self.dg_clashes.SelectedItems.Clear()
            self.dg_clashes.SelectedItems.Add(row)
        except Exception:
            pass

    # --- Comment posting -----------------------------------------------

    def _on_post_comment(self, sender, args):
        body = self.txt_new_comment.Text.strip()
        if not body:
            return
        row = self._selected_row()
        if row is None:
            return
        new_comment = models.make_comment(self._author, body)
        comments = row.Source.setdefault('comments', [])
        comments.append(new_comment)
        history = row.Source.setdefault('history', [])
        history.append(models.make_history_entry(self._author, 'comment_added'))
        row.refresh_from_source()
        # Refresh the detail comments list
        self.ic_comments.ItemsSource = [_CommentRow(c) for c in comments]
        self.txt_comments_header.Text = 'Comments ({})'.format(len(comments))
        self.txt_new_comment.Text = ''
        # Refresh grid (comment count column)
        self.dg_clashes.Items.Refresh()
        # Re-apply filters so any active search picks up text in the new
        # comment immediately.
        if self._filters_wired:
            self._apply_filters()
        self._restore_selection(row)
        self._save_clashes(action_label="Comment added")

    # --- Save ----------------------------------------------------------

    def _save_clashes(self, action_label=None):
        """Write the current clash list back to clashes.json."""
        if not self._project_hash:
            dbhms_ui.info(
                "No project hash - can't save changes. Open Settings first.",
                title='Save failed',
            )
            return False
        try:
            existing = persistence.read_clashes(self._project_hash)
            existing['clashes'] = self._clash_dicts  # in-place edits already applied
            persistence.write_clashes(self._project_hash, existing)
            if action_label:
                self.txt_status.Text = '{} - saved.'.format(action_label)
            return True
        except Exception as ex:
            dbhms_ui.info(
                "Couldn't save clashes.json:\n\n{}\n\n{}".format(
                    ex, traceback.format_exc()),
                title='Save failed',
            )
            return False

    # --- Refresh / misc -------------------------------------------------

    def _on_refresh(self, sender, args):
        self._load_clashes()
        if len(self._clashes) > 0:
            self.dg_clashes.SelectedIndex = 0
        else:
            self._show_empty_detail()

    _GROUP_PROP_FOR_CHOICE = {
        'Group by trade':  'Trade',
        'Group by test':   'TestName',
        'Group by level':  'Level',
        'Group by status': 'Status',
    }

    def _on_group_changed(self, sender, args):
        """Apply or clear group descriptions when the dropdown changes."""
        self._apply_current_grouping()

    def _apply_current_grouping(self):
        """Sync the DataGrid view's GroupDescriptions with the dropdown.

        Operates on the explicit ListCollectionView we own (self._view) rather
        than poking the DataGrid's Items collection. Both row virtualization
        and column virtualization are off in XAML, and the GroupStyle.Panel
        forces a non-virtualizing StackPanel for the group containers, which
        together avoid the layout crash that happens when WPF tries to swap a
        virtualized panel under an active group.
        """
        view = getattr(self, '_view', None)
        if view is None:
            return
        choice = self._get_combo_text(self.cmb_group_by) or 'No grouping'
        prop = self._GROUP_PROP_FOR_CHOICE.get(choice)
        try:
            if view.GroupDescriptions.Count:
                view.GroupDescriptions.Clear()
            if prop:
                view.GroupDescriptions.Add(PropertyGroupDescription(prop))
        except Exception:
            # Never let a grouping hiccup take down the whole form.
            pass

    def _on_reset_filters(self, sender, args):
        """Reset every filter back to the form's default coordination state:

        Trade  — all checked
        Status — all four checked (Open, Reviewed, Approved, Resolved).
                 Defaulting Approved + Resolved on too — most coordination
                 reviews want to see the full picture including resolved
                 items, and unchecking a status to focus is a one-click
                 operation when needed.
        Test   — "(All tests)"
        Search — empty
        """
        previous = self._suppress_change_events
        self._suppress_change_events = True
        try:
            for chk in self._iter_checkboxes(self.wp_trade_filter):
                chk.IsChecked = True
            for chk in self._iter_checkboxes(self.wp_status_filter):
                chk.IsChecked = True
            self.cmb_test_filter.SelectedIndex = 0  # "(All tests)"
            self.txt_search.Text = ''
        finally:
            self._suppress_change_events = previous
        self._apply_filters()

    # --- Filter presets (Iteration 11) ----------------------------------

    # Sentinel string the Test combo's "(All tests)" item carries; preset
    # records use the same string so we can round-trip without special-
    # casing.
    _ALL_TESTS_SENTINEL = "(All tests)"

    def _build_preset_buttons(self):
        """Populate sp_presets with one Button per preset.

        Built-ins first (always present, can't delete), then user-saved.
        Each button click applies the preset to the filter UI; right-
        click on a user-saved button opens a context menu to delete it.
        """
        from System.Windows.Controls import (
            Button, ContextMenu, MenuItem)
        from System.Windows import HorizontalAlignment, Thickness
        sp = self.sp_presets
        sp.Children.Clear()
        for preset in filter_presets.all_presets():
            btn = Button()
            btn.Content = preset["name"]
            btn.Style = self.Resources["MiniButton"]
            btn.HorizontalContentAlignment = HorizontalAlignment.Left
            btn.Margin = Thickness(0, 0, 0, 4)
            # Bind preset via default-arg closure (IronPython late-binding
            # bites the same way CPython does — without `p=preset`, every
            # button would apply the LAST preset in the loop).
            btn.Click += (lambda s, a, p=preset: self._on_apply_preset(p))
            if not preset.get("builtin"):
                # User-saved presets get a right-click "Delete preset"
                # menu. Built-ins are immutable.
                cm = ContextMenu()
                mi = MenuItem()
                mi.Header = "Delete preset"
                mi.Click += (lambda s, a, pid=preset["id"], pname=preset["name"]:
                             self._on_delete_preset(pid, pname))
                cm.Items.Add(mi)
                btn.ContextMenu = cm
            sp.Children.Add(btn)

    def _on_apply_preset(self, preset):
        """Restore the filter UI to the state captured in `preset`.

        trades=None / statuses=None means "no filter on this dimension"
        — every checkbox in that panel goes on. test defaults to the
        "(All tests)" sentinel; search defaults to empty.
        """
        previous = self._suppress_change_events
        self._suppress_change_events = True
        try:
            trades   = preset.get("trades")
            statuses = preset.get("statuses")
            test     = preset.get("test") or self._ALL_TESTS_SENTINEL
            search   = preset.get("search") or ""

            for chk in self._iter_checkboxes(self.wp_trade_filter):
                content = str(getattr(chk, 'Content', ''))
                chk.IsChecked = (trades is None) or (content in trades)
            for chk in self._iter_checkboxes(self.wp_status_filter):
                content = str(getattr(chk, 'Content', ''))
                chk.IsChecked = (statuses is None) or (content in statuses)

            # Test filter: if the saved name isn't in the dropdown
            # (project changed, test deleted), fall back to "(All tests)"
            # rather than leaving the combo blank.
            matched = False
            for item in self.cmb_test_filter.Items:
                if getattr(item, 'Content', None) == test:
                    self.cmb_test_filter.SelectedItem = item
                    matched = True
                    break
            if not matched:
                self.cmb_test_filter.SelectedIndex = 0

            self.txt_search.Text = search
        finally:
            self._suppress_change_events = previous
        self._apply_filters()
        self.txt_status.Text = "Preset applied: {}".format(preset.get("name", ""))

    def _on_save_preset(self, sender, args):
        """Prompt for a name, snapshot the current filter UI state into a
        preset, persist, and refresh the button list."""
        name = forms.ask_for_string(
            default='My filter',
            prompt='Save current filters as a preset. Name:',
            title='Save filter preset',
        )
        if not name:
            return
        allowed_trades, allowed_statuses, test_filter, search_text = (
            self._collect_filter_args())
        # _collect_filter_args returns sets; convert to sorted lists for
        # stable JSON output. allowed_trades/statuses being None means
        # the panel had no checkboxes (defensive); persist as None too.
        trades_list = (sorted(allowed_trades)
                       if allowed_trades is not None else None)
        statuses_list = (sorted(allowed_statuses)
                         if allowed_statuses is not None else None)
        preset = filter_presets.make_preset(
            name,
            trades=trades_list,
            statuses=statuses_list,
            test=test_filter or self._ALL_TESTS_SENTINEL,
            search=search_text,
        )
        try:
            filter_presets.append_user_preset(preset)
        except Exception as ex:
            dbhms_ui.info(
                "Couldn't save preset:\n\n{}".format(ex),
                title='Save preset failed',
            )
            return
        self._build_preset_buttons()
        self.txt_status.Text = "Preset saved: {}".format(preset["name"])

    def _on_delete_preset(self, preset_id, preset_name):
        """Confirm + delete a user-saved preset, then refresh the list."""
        ok = forms.alert(
            "Delete the preset '{}'? This can't be undone.".format(preset_name),
            title='Delete preset',
            yes=True, no=True,
        )
        if not ok:
            return
        try:
            removed = filter_presets.delete_user_preset(preset_id)
        except Exception as ex:
            dbhms_ui.info(
                "Couldn't delete preset:\n\n{}".format(ex),
                title='Delete preset failed',
            )
            return
        if removed:
            self._build_preset_buttons()
            self.txt_status.Text = "Preset deleted: {}".format(preset_name)

    def _catch_up_viewpoints(self):
        """Generate viewpoint thumbnails for any clashes that don't have one.

        Called once when the Browser opens. New clashes from Run Clash
        Test already get viewpoints auto-generated post-detection, so
        on a healthy project this is usually a no-op. The catch-up path
        is here to handle older clash data from before viewpoint
        auto-generation existed (or any case where it failed silently).

        Skips fully if there's no project hash or no clashes. Runs
        synchronously — for typical clash counts (10-100) this is fast
        enough to not be visible, and we want the thumbnails ready
        before the user starts clicking through.
        """
        if not self._project_hash or not self._clash_dicts:
            return

        from pyrevit import revit
        from clash_view import viewpoint as vp_module

        # Quick scan — bail out fast if every clash already has a viewpoint
        # PNG on disk. Checking the disk (not just the dict) means deleting
        # the viewpoints folder is a complete reset — every clash regenerates
        # because the file's gone, regardless of what the dict says.
        missing = [c for c in self._clash_dicts
                   if vp_module.viewpoint_image_for(c, self._project_hash) is None]
        if not missing:
            return

        try:
            meta = persistence.read_project_meta(self._project_hash)
            role_map = meta.get('link_role_map') or {}
        except Exception:
            role_map = {}

        prev_status = self.txt_status.Text
        self.txt_status.Text = (
            "Generating thumbnails for {} clash(es) — one moment...".format(
                len(missing)))
        try:
            generated = vp_module.generate_for_all(
                revit.uidoc, self._clash_dicts, role_map,
                self._project_hash, captured_by=self._author,
                only_missing=True,
            )
            if generated > 0:
                # Persist updated clash dicts so the viewpoints[] entries
                # we just added stick across sessions.
                try:
                    existing = persistence.read_clashes(self._project_hash)
                    existing['clashes'] = self._clash_dicts
                    persistence.write_clashes(self._project_hash, existing)
                except Exception:
                    pass
                # The DataGrid auto-selects the first row when ItemsSource
                # is set in _load_clashes — earlier in __init__, BEFORE
                # this catch-up runs. That auto-selection's SelectionChanged
                # fires _render_viewpoint while the PNG file is still
                # missing, so the panel shows the "no viewpoint" placeholder.
                # Force a re-render of whatever's currently selected so
                # the just-generated PNG actually appears without the user
                # having to click away and back.
                current = self._selected_row()
                if current is not None:
                    self._render_viewpoint(current.Source)
            self.txt_status.Text = prev_status
        except Exception as ex:
            self.txt_status.Text = (
                "Thumbnail generation failed: {}. Clashes still usable.".format(ex))

    def _on_show_in_3d(self, sender, args):
        """Frame the selected clash in the dbHMS Clash Navigator 3D view.

        Reads the project's link role-map fresh on each click so a Settings
        change (re-mapping a link to a different role) is picked up without
        having to reopen the Browser.
        """
        selected = list(self.dg_clashes.SelectedItems)
        if not selected:
            dbhms_ui.info("Select a clash from the list first.",
                        title='Nothing selected')
            return
        if len(selected) > 1:
            dbhms_ui.info(
                "Show in 3D works on a single clash. Select just one row, "
                "then try again.",
                title='Pick one clash',
            )
            return

        clash_dict = selected[0].Source  # ClashRow.Source IS the clash dict
        try:
            meta = persistence.read_project_meta(self._project_hash)
            role_map = meta.get('link_role_map') or {}
        except Exception:
            role_map = {}

        from pyrevit import revit
        from clash_view import navigate
        # Viewpoints are generated in batch (post-detection in Run Clash
        # Test, and on Browser open as catch-up). Show in 3D's only job
        # here is to navigate the user to the clash — the thumbnail is
        # already on disk by the time the Browser is open.
        success, message, _view = navigate.show_clash(
            revit.uidoc, clash_dict, role_map)
        self.txt_status.Text = message
        if not success:
            dbhms_ui.info(message, title='Show in 3D')

    # _on_save_viewpoint removed — the manual Save Viewpoint button is
    # gone from the detail panel. Viewpoints are auto-generated on Run
    # Clash Test (post-detection batch) and on Browser open (catch-up
    # for any clashes missing one), so there's no need for the manual
    # save flow. `clash_view.viewpoint.capture_for_clash` is still
    # available as a library function if a future tool needs it.

    def _on_show_history(self, sender, args):
        """Pop a modal sub-window listing the selected clash's audit
        trail (status changes, reassigns, comments, viewpoint saves).

        Read-only display. Data is whatever's already in
        `clash_dict['history']`.
        """
        selected = list(self.dg_clashes.SelectedItems)
        if not selected:
            dbhms_ui.info("Select a clash from the list first.",
                        title='History')
            return
        if len(selected) > 1:
            dbhms_ui.info(
                "History is a single-clash view. Select just one row, "
                "then try again.",
                title='History',
            )
            return
        row = selected[0]
        dialog = _HistoryDialog(row.Source)
        dialog.Owner = self
        dialog.ShowDialog()

    # --- Bulk actions (Iteration 9) -------------------------------------

    # Allowed values for the bulk-pickers — same set the detail panel
    # offers, so bulk edits stay in lockstep with single-row edits.
    _BULK_STATUSES = ('Open', 'Reviewed', 'Approved', 'Resolved')
    _BULK_TRADES = ('Mechanical', 'Electrical', 'Plumbing', 'Fire Protection',
                    'Technology', 'Architectural', 'Structural')

    # Above this many rows we ask the user to confirm. Picked so a
    # double-click misclick can't quietly reassign 100 clashes, but a
    # deliberate 5-row selection isn't pestered.
    _BULK_CONFIRM_THRESHOLD = 5

    def _on_bulk_status(self, sender, args):
        """Pick a new status, apply to every selected clash."""
        selected = list(self.dg_clashes.SelectedItems)
        if len(selected) < 2:
            return
        new_status = forms.SelectFromList.show(
            list(self._BULK_STATUSES),
            title='Change status for {} clash(es)'.format(len(selected)),
            button_name='Apply',
            multiselect=False,
        )
        if not new_status:
            return
        if not self._confirm_bulk_if_large(
                selected, "Change status to '{}'".format(new_status)):
            return
        self._apply_bulk(
            selected,
            lambda clash, at: bulk_edit.apply_status(
                clash, new_status, self._author, at=at),
            action_label="Status set to {}".format(new_status))

    def _on_bulk_reassign(self, sender, args):
        """Pick a new trade, apply to every selected clash."""
        selected = list(self.dg_clashes.SelectedItems)
        if len(selected) < 2:
            return
        new_trade = forms.SelectFromList.show(
            list(self._BULK_TRADES),
            title='Reassign {} clash(es) to trade'.format(len(selected)),
            button_name='Apply',
            multiselect=False,
        )
        if not new_trade:
            return
        if not self._confirm_bulk_if_large(
                selected, "Reassign to '{}'".format(new_trade)):
            return
        self._apply_bulk(
            selected,
            lambda clash, at: bulk_edit.apply_trade(
                clash, new_trade, self._author, at=at),
            action_label="Reassigned to {}".format(new_trade))

    def _on_bulk_resolve(self, sender, args):
        """Mark every selected clash as Resolved — no picker needed."""
        selected = list(self.dg_clashes.SelectedItems)
        if len(selected) < 2:
            return
        if not self._confirm_bulk_if_large(selected, "Mark as Resolved"):
            return
        self._apply_bulk(
            selected,
            lambda clash, at: bulk_edit.apply_status(
                clash, 'Resolved', self._author, at=at),
            action_label="Marked Resolved")

    def _confirm_bulk_if_large(self, selected, action_text):
        """Pop a yes/no confirmation when many rows are selected.

        Returns True if the user confirmed (or selection is small enough
        to skip the prompt), False if they cancelled.
        """
        if len(selected) < self._BULK_CONFIRM_THRESHOLD:
            return True
        return bool(forms.alert(
            "{} for {} clash(es)?".format(action_text, len(selected)),
            title='Bulk action — confirm',
            yes=True, no=True,
        ))

    def _apply_bulk(self, selected, mutator, action_label):
        """Loop `mutator` over each selected row, persist once at the end,
        refresh the grid + detail panel + filters.

        `mutator(clash_dict, at) -> bool` should return True when an
        actual change was made (so we can report a meaningful count).
        Uses one ISO timestamp for the whole batch so the audit log
        groups the entries cleanly.
        """
        batch_at = _now_iso()
        changed = 0
        for row in selected:
            try:
                if mutator(row.Source, batch_at):
                    row.refresh_from_source()
                    changed += 1
            except Exception:
                # One bad row shouldn't kill the whole batch.
                continue

        if changed == 0:
            self.txt_status.Text = (
                'No changes — every selected clash was already at the target value.')
            return

        self.dg_clashes.Items.Refresh()
        # Re-apply filters: a row whose status changed to one whose
        # checkbox is unchecked should drop from the grid immediately.
        if self._filters_wired:
            self._apply_filters()
        # Restore selection on the still-visible rows so the user can
        # see what they just changed (re-render of detail panel for the
        # current selection too).
        try:
            current = self._selected_row()
            if current is not None:
                self._populate_detail(current)
        except Exception:
            pass

        if self._save_clashes(action_label=action_label):
            self.txt_status.Text = '{} on {}/{} clash(es).'.format(
                action_label, changed, len(selected))

    def _on_loaded(self, sender, args):
        """Window-shown hook: force a re-render of the current selection's
        thumbnail.

        Without this, the very first auto-selected clash on Browser open
        shows the placeholder until the user clicks away and back. The
        cause: SelectionChanged fires synchronously during __init__ —
        before WPF has actually laid out + shown the Image control — so
        the BitmapImage we set as the Source isn't painted by WPF when
        the window eventually appears. Loaded fires AFTER the window has
        been measured / arranged / shown, so a re-render here lands at
        a moment when WPF will actually paint it.

        One-shot — unsubscribe immediately so re-shows of the form (which
        in pyRevit's modal flow shouldn't happen, but defensively...)
        don't re-fire and waste work.
        """
        try:
            self.Loaded -= self._on_loaded
        except Exception:
            pass
        try:
            current = self._selected_row()
            if current is not None:
                self._render_viewpoint(current.Source)
        except Exception:
            pass

    def _render_viewpoint(self, clash_dict):
        """Update the viewpoint thumbnail panel for the given clash.

        Loads the PNG (if one exists) into img_viewpoint and toggles
        the placeholder accordingly. Uses BitmapCacheOption.OnLoad so
        the file isn't held open after loading — important because the
        next save needs to overwrite it in place.
        """
        from System.Windows import Visibility
        # No clash selected, or no project hash → just show placeholder
        if clash_dict is None or not self._project_hash:
            self.img_viewpoint.Source = None
            self.img_viewpoint.Visibility = Visibility.Collapsed
            self.txt_viewpoint_placeholder.Visibility = Visibility.Visible
            return

        from clash_view import viewpoint as vp_module
        image_path = vp_module.viewpoint_image_for(clash_dict, self._project_hash)
        if not image_path:
            self.img_viewpoint.Source = None
            self.img_viewpoint.Visibility = Visibility.Collapsed
            self.txt_viewpoint_placeholder.Visibility = Visibility.Visible
            return

        try:
            from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
            from System import Uri
            img = BitmapImage()
            img.BeginInit()
            img.UriSource = Uri(image_path)
            # Critical: load fully into memory so we don't lock the
            # underlying file. Without this, re-saving the viewpoint
            # for the same clash would fail with "file is in use."
            img.CacheOption = BitmapCacheOption.OnLoad
            img.EndInit()
            self.img_viewpoint.Source = img
            self.img_viewpoint.Visibility = Visibility.Visible
            self.txt_viewpoint_placeholder.Visibility = Visibility.Collapsed
        except Exception:
            # Couldn't decode the PNG (corrupt? wrong format?) — fall
            # back to the placeholder rather than silently leaving a
            # stale image up.
            self.img_viewpoint.Source = None
            self.img_viewpoint.Visibility = Visibility.Collapsed
            self.txt_viewpoint_placeholder.Visibility = Visibility.Visible

    def _on_coming_soon(self, sender, args):
        dbhms_ui.info(
            "This action isn't wired up yet. It depends on viewport navigation / "
            "viewpoint capture / BCF export, which are the next chunks.",
            title='Coming Soon',
        )

    def _on_close(self, sender, args):
        # No close-time cleanup. The navigator view's highlights persist
        # between Browser sessions (acceptable — minor visual artifact)
        # and are cleared automatically by the next Show in 3D click via
        # highlights._clear_last. Keeping this handler trivial means the
        # Browser closes as fast as any other tool — there's no extra
        # transaction in the close path to slow it down.
        self.Close()


# ---------------------------------------------------------------------------
# Comment view-model (binds in the ic_comments ItemsControl)
# ---------------------------------------------------------------------------

class _CommentRow(object):
    def __init__(self, comment_dict):
        self.Author = comment_dict.get('author') or 'unknown'
        when = comment_dict.get('at') or ''
        # ISO 8601 strings like "2026-05-05T14:30:00Z" - show date + time only
        if 'T' in when:
            d, t = when.split('T', 1)
            t = t.rstrip('Z')[:5]
            when = '{} {}'.format(d, t)
        self.When = when
        self.Body = comment_dict.get('body') or ''


# ---------------------------------------------------------------------------
# History dialog (Iteration 10) — sub-window listing one clash's audit trail
# ---------------------------------------------------------------------------

class _HistoryRow(object):
    """View-model row for the history ItemsControl. Properties bind to
    {Binding When|Author|Action} in HistoryDialog.xaml."""
    def __init__(self, history_entry):
        self.When   = history_format.format_when(history_entry)
        self.Author = history_format.format_author(history_entry)
        self.Action = history_format.format_action(history_entry)


class _HistoryDialog(forms.WPFWindow):
    """Sub-dialog that renders one clash's history list.

    Read-only — no edits, no actions beyond Close. Modal so the user
    finishes reading before going back to the main Browser. Entries
    show in chronological order (oldest first) so the audit reads
    top-to-bottom as the clash's life story.
    """
    def __init__(self, clash_dict):
        forms.WPFWindow.__init__(self, HISTORY_DIALOG_XAML)

        seq = (clash_dict or {}).get('seq') or '?'
        self.txt_dialog_title.Text = 'History — Clash #{}'.format(seq)

        # Build view-model rows from the clash's history list. The
        # underlying list is already chronological (we always append),
        # so display in original order.
        history = (clash_dict or {}).get('history') or []
        rows = [_HistoryRow(e) for e in history]
        self.ic_history.ItemsSource = rows

        # Empty-state placeholder vs the rendered list.
        from System.Windows import Visibility
        if not rows:
            self.txt_empty_state.Visibility = Visibility.Visible
            self.ic_history.Visibility = Visibility.Collapsed
        else:
            self.txt_empty_state.Visibility = Visibility.Collapsed

        # Footer count.
        self.txt_history_count.Text = (
            '{} entry'.format(len(rows)) if len(rows) == 1
            else '{} entries'.format(len(rows)))
        self.txt_dialog_subtitle.Text = (
            "Audit trail for the selected clash. Read-only.")

        self.btn_close.Click += self._on_close

    def _on_close(self, sender, args):
        self.Close()


with dbhms_telemetry.session(__title__, script_path=__file__):
    ClashBrowserForm().ShowDialog()
