# -*- coding: utf-8 -*-
"""Clash Browser - the main UI for working through clashes.

Reads the project's clashes.json on open and renders every clash in the
DataGrid with status / trade pills. Status changes (via the detail
panel's dropdown), trade reassignments, and posted comments persist back
to clashes.json immediately, with a history entry recording who did
what and when.

Buttons that need viewport navigation (Show in 3D, Save Viewpoint,
Walkthrough Here, History) still pop "coming soon" - those depend on
clash_view modules that haven't landed yet.

If no clashes have been detected yet, the form shows an empty state with
a hint to open Run Clash Test.

See Clash Detection.tab/README.md for the architecture.
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

from clash_core import config, persistence, project, users, models


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'ClashBrowserForm.xaml')


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

        # Buttons that depend on the not-yet-implemented clash_view modules
        for btn_name in ('btn_show_3d', 'btn_save_viewpoint', 'btn_walkthrough_here',
                         'btn_history', 'btn_save_preset',
                         'btn_bulk_status', 'btn_bulk_reassign',
                         'btn_bulk_group', 'btn_bulk_resolve'):
            getattr(self, btn_name).Click += self._on_coming_soon

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
        self._comment_lookup = {}  # not used anymore; comments live on each clash dict

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
        # Re-apply any active grouping after a reload.
        self._apply_current_grouping()

        if banner:
            self.txt_status.Text = banner
        elif not self._clashes:
            self.txt_status.Text = (
                'No clashes detected yet. Open Run Clash Test to scan the project.'
            )
        else:
            self.txt_status.Text = '{} clash(es) loaded.'.format(len(self._clashes))

        self.txt_grid_header.Text = 'Clashes ({})'.format(len(self._clashes))

    # --- Selection / detail panel ---------------------------------------

    def _on_selection_changed(self, sender, args):
        selected = list(self.dg_clashes.SelectedItems)
        if len(selected) > 1:
            self.brd_bulk_actions.Visibility = Visibility.Visible
            self.txt_bulk_count.Text = '{} selected:'.format(len(selected))
        else:
            self.brd_bulk_actions.Visibility = Visibility.Collapsed

        if len(selected) >= 1:
            self._populate_detail(selected[0])
        elif not self._clashes:
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
        self._restore_selection(row)
        self._save_clashes(action_label="Comment added")

    # --- Save ----------------------------------------------------------

    def _save_clashes(self, action_label=None):
        """Write the current clash list back to clashes.json."""
        if not self._project_hash:
            forms.alert(
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
            forms.alert(
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
        forms.alert(
            "Filter wiring isn't done yet - the filter checkboxes/dropdowns are "
            "currently visual only. Filtering of the live clash list lands in the "
            "next iteration.",
            title='Coming Soon',
        )

    def _on_coming_soon(self, sender, args):
        forms.alert(
            "This action isn't wired up yet. It depends on viewport navigation / "
            "viewpoint capture / BCF export, which are the next chunks.",
            title='Coming Soon',
        )

    def _on_close(self, sender, args):
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


ClashBrowserForm().ShowDialog()
