# -*- coding: utf-8 -*-
"""Test Library - manage the global clash-test library + per-project overrides.

Reads / writes the firm-wide library at <shared>/global/test_library.json
and the active project's overrides at <shared>/<project-hash>/test_overrides.json.
First-run seeding from default_tests.json (next to this script) is handled
when the global file is missing or empty.

Iteration 2 wires the editor: pick a test on the left, change fields on
the right, click Save Changes - the result is persisted to the same store
the test came from. The bottom-left buttons add a new firm-wide test, fork
a project override off the current selection (or create a blank project
test), and delete the selected test (with a confirmation that names the
scope so a misclick can't wipe the firm library).

Disabled overrides (a global test_id added to disabled_test_ids for one
project) are still display-only - the broad firm defaults don't lend
themselves to per-project disabling so wiring that flow is deferred.

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
"""

__title__  = 'Test\nLibrary'
__author__ = 'Nathaniel'
__doc__    = "View, edit, and create clash tests in the global firm library and per-project overrides."

import codecs
import json
import os
import uuid

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import Thickness, Visibility
from System.Windows.Controls import CheckBox
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_core import config, persistence, project
from clash_core import categories as cat_module


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'TestLibraryForm.xaml')
SEED_FILE  = os.path.join(SCRIPT_DIR, 'default_tests.json')


# Display <-> JSON-value mappings for editor dropdowns
KIND_DISPLAY_TO_VALUE = {
    "Hard":                "hard",
    "Soft":                "soft",
    "Clearance (planned)": "clearance",
}
KIND_VALUE_TO_DISPLAY = {v: k for k, v in KIND_DISPLAY_TO_VALUE.items()}

# All allowed assignee trades — must match the cmb_assignee items in XAML.
ASSIGNEE_TRADES = (
    "Mechanical", "Electrical", "Plumbing", "Fire Protection",
    "Technology", "Architectural", "Structural",
)


# ---------------------------------------------------------------------------
# Display row classes (DataGrid binding objects)
# ---------------------------------------------------------------------------

class TestRow(object):
    def __init__(self, test_dict):
        self.Source = test_dict
        self.Name = test_dict.get('name', '<unnamed>')
        kind_value = test_dict.get('kind') or 'hard'
        self.Kind = kind_value.capitalize()
        self.Assignee = test_dict.get('default_assignee') or '-'


class OverrideRow(object):
    """Override grid row.

    `OverrideKind` is one of "Custom" or "Disabled". For Disabled rows we
    look up the underlying global test so the row shows a readable name and
    trade rather than a raw id; the editor then shows the global definition
    and a banner explaining the row is read-only.
    """
    def __init__(self, source_dict, kind_label, name=None, assignee=None):
        self.Source = source_dict
        self.Name = name or source_dict.get('name', '<unnamed>')
        self.OverrideKind = kind_label
        self.Assignee = assignee or source_dict.get('default_assignee') or '-'


# ---------------------------------------------------------------------------
# Library loading + first-run seeding
# ---------------------------------------------------------------------------

def _read_seed_tests():
    """Read default_tests.json shipped next to this script. Returns the tests list."""
    with codecs.open(SEED_FILE, 'r', 'utf-8') as f:
        seed = json.load(f)
    return seed.get('tests', [])


def _ensure_library_seeded():
    """Read the global library; if empty, seed it from default_tests.json
    and persist. Returns the (now-populated) library dict.

    Raises persistence.SharedFolderNotConfigured if the user hasn't set up
    Settings yet — caller should route them there.
    """
    library = persistence.read_global_test_library()
    if not library.get('tests'):
        try:
            seed_tests = _read_seed_tests()
        except (IOError, ValueError):
            seed_tests = []
        if seed_tests:
            library = {'$schema_version': 1, 'tests': seed_tests}
            persistence.write_global_test_library(library)
    return library


def _build_override_rows(overrides_dict, library_tests):
    out = []
    library_by_id = {t.get('id'): t for t in library_tests}
    for tid in overrides_dict.get('disabled_test_ids', []):
        ref = library_by_id.get(tid, {})
        out.append(OverrideRow(
            ref or {'name': tid, 'id': tid},
            kind_label='Disabled',
            name=ref.get('name', tid),
            assignee=ref.get('default_assignee', '-'),
        ))
    for ct in overrides_dict.get('custom_tests', []):
        out.append(OverrideRow(ct, 'Custom'))
    return out


# ---------------------------------------------------------------------------
# Helpers used by both edit/save and new/delete
# ---------------------------------------------------------------------------

def _new_test_id():
    """Short, collision-resistant id for a user-created test."""
    return 'custom-' + uuid.uuid4().hex[:10]


def _blank_test(name):
    return {
        'id':               _new_test_id(),
        'name':             name,
        'kind':             'hard',
        'tolerance_inches': 0.0,
        'set_a':            {'source': 'host', 'categories': []},
        'set_b':            {'source': 'host', 'categories': []},
        'default_assignee': 'Mechanical',
    }


def _upsert_by_id(items, item):
    """Replace the dict in `items` whose id matches `item['id']`, or append
    if not present. Mutates and returns `items`."""
    for i, t in enumerate(items):
        if t.get('id') == item.get('id'):
            items[i] = item
            return items
    items.append(item)
    return items


def _remove_by_id(items, item_id):
    return [t for t in items if t.get('id') != item_id]


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------

def _brush(hex_str):
    h = hex_str.lstrip('#')
    return SolidColorBrush(Color.FromRgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))


class TestLibraryForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)

        # Build the comprehensive category checkbox list (same for both Set A and Set B)
        self._populate_category_list(self.sp_categories_a)
        self._populate_category_list(self.sp_categories_b)

        # Selection state — which store the editor is currently bound to.
        # Values: None / 'global' / 'override:custom' / 'override:disabled'
        self._current_store = None
        self._current_id    = None
        # When a global default is selected, the editor is read-only by
        # default and must be deliberately unlocked via "Edit firm-wide…".
        # Resets to False on every selection change so the protection is
        # per-test, not session-wide.
        self._firmwide_unlocked = False

        # Library + project state
        self._global_tests  = []     # list[TestRow]
        self._overrides     = []     # list[OverrideRow]
        self._project_hash  = None
        self._setup_message = None

        try:
            library = _ensure_library_seeded()
            self._global_tests = [TestRow(t) for t in library.get('tests', [])]
            try:
                from pyrevit import revit
                doc = revit.doc
                if doc is not None:
                    ph = project.resolve_key(doc)
                    if ph:
                        self._project_hash = ph
                        ov = persistence.read_overrides(ph)
                        self._overrides = _build_override_rows(ov, library.get('tests', []))
            except Exception:
                pass
        except persistence.SharedFolderNotConfigured as ex:
            self._setup_message = str(ex)

        self.dg_global.ItemsSource    = self._global_tests
        self.dg_overrides.ItemsSource = self._overrides
        self.txt_global_count.Text   = '{} tests'.format(len(self._global_tests))
        self.txt_override_count.Text = '{} overrides'.format(len(self._overrides))

        # Wire events
        self.btn_close.Click                  += self._on_close
        self.btn_save.Click                   += self._on_save
        self.btn_new_project_test.Click       += self._on_new_project_test
        self.btn_new_firm_test.Click          += self._on_new_firm_test
        self.btn_delete_test.Click            += self._on_delete
        self.btn_reset_default.Click          += self._on_reset_default
        self.btn_customize_for_project.Click  += self._on_customize_for_project
        self.btn_edit_firmwide.Click          += self._on_edit_firmwide
        self.btn_cancel_firmwide_edit.Click   += self._on_cancel_firmwide_edit
        self.dg_global.SelectionChanged       += self._on_global_selected
        self.dg_overrides.SelectionChanged    += self._on_override_selected
        self.cmb_kind.SelectionChanged        += self._on_kind_changed

        # Pre-select first global test, or show first-run alert
        if self._setup_message:
            self.txt_editor_header.Text = "No global library yet"
            self._apply_banners()
            self.Loaded += self._show_first_run_alert
        elif self._global_tests:
            self.dg_global.SelectedIndex = 0
        else:
            self._apply_banners()

    def _show_first_run_alert(self, sender, args):
        self.Loaded -= self._show_first_run_alert
        dbhms_ui.info(
            "The Test Library can't load:\n\n{}\n\n"
            "Open Settings (Clash Detection panel on the dbHMS Tools tab) and pick a shared "
            "clash-data folder, then come back.".format(self._setup_message),
            title='Setup needed',
        )

    # --- Category list construction -------------------------------------

    def _populate_category_list(self, panel):
        panel.Children.Clear()
        for ost, friendly, _group in cat_module.CATEGORIES:
            chk = CheckBox()
            chk.Content = friendly
            chk.Tag = ost
            chk.Margin = Thickness(0, 3, 0, 3)
            panel.Children.Add(chk)

    def _set_categories_checked(self, panel, ost_list):
        ost_set = set(ost_list)
        for child in panel.Children:
            if hasattr(child, 'Tag') and hasattr(child, 'IsChecked'):
                child.IsChecked = (child.Tag in ost_set)

    def _collect_categories(self, panel):
        out = []
        for child in panel.Children:
            if hasattr(child, 'Tag') and getattr(child, 'IsChecked', False):
                out.append(child.Tag)
        return out

    # --- Source check-box helpers ---------------------------------------

    def _set_sources(self, source_value, chk_host, chk_arch, chk_struct):
        """Drive the three source checkboxes from a string OR list value."""
        if isinstance(source_value, list):
            sources = set(source_value)
        elif source_value:
            sources = set([source_value])
        else:
            sources = set()
        chk_host.IsChecked   = ('host' in sources)
        chk_arch.IsChecked   = ('link:Architectural' in sources)
        chk_struct.IsChecked = ('link:Structural' in sources)

    def _collect_sources(self, chk_host, chk_arch, chk_struct):
        """Return a string when exactly one source is checked, a list when 2+,
        or [] when none. Storage shape mirrors what the runner already accepts."""
        out = []
        if chk_host.IsChecked:   out.append('host')
        if chk_arch.IsChecked:   out.append('link:Architectural')
        if chk_struct.IsChecked: out.append('link:Structural')
        if len(out) == 1:
            return out[0]
        return out

    # --- Selection handlers ---------------------------------------------

    def _on_global_selected(self, sender, args):
        items = list(self.dg_global.SelectedItems)
        if not items:
            return
        self.dg_overrides.UnselectAll()
        # Every selection change relocks the editor — firm-wide edit is a
        # per-test deliberate choice, not a session mode.
        self._firmwide_unlocked = False
        self._populate_editor(
            items[0].Source,
            store='global',
            source_label='Global default — read-only',
            bg='#EBF8FF', border='#90CDF4', fg='#2A4365',
        )
        self._apply_banners()
        self.txt_editor_status.Text = ""

    def _on_override_selected(self, sender, args):
        items = list(self.dg_overrides.SelectedItems)
        if not items:
            return
        self.dg_global.UnselectAll()
        self._firmwide_unlocked = False
        row = items[0]
        is_disabled = (row.OverrideKind == 'Disabled')
        store = 'override:disabled' if is_disabled else 'override:custom'
        self._populate_editor(
            row.Source,
            store=store,
            source_label=('Disabled (read-only)' if is_disabled else 'Project override'),
            bg='#FFFBEA' if is_disabled else '#FAF5FF',
            border='#D69E2E' if is_disabled else '#D6BCFA',
            fg='#744210' if is_disabled else '#553C9A',
        )
        self._apply_banners()
        self.txt_editor_status.Text = (
            "This row disables a global test for the active project. "
            "Editing/removing the disable lands in a future iteration."
            if is_disabled else ""
        )

    def _populate_editor(self, test_dict, store, source_label, bg, border, fg):
        self._current_store = store
        self._current_id    = test_dict.get('id')

        self.txt_editor_header.Text  = 'Editing: {}'.format(test_dict.get('name', ''))
        self.txt_source_badge.Text   = source_label
        self.brd_source_badge.Background = _brush(bg)
        self.brd_source_badge.BorderBrush = _brush(border)
        self.txt_source_badge.Foreground = _brush(fg)

        self.txt_test_name.Text  = test_dict.get('name', '')
        self.txt_tolerance.Text  = '{}'.format(test_dict.get('tolerance_inches', 0.0))
        self._set_combo_by_text(
            self.cmb_kind,
            KIND_VALUE_TO_DISPLAY.get(test_dict.get('kind', 'hard'), 'Hard'),
        )
        self._sync_tolerance_enabled()

        set_a = test_dict.get('set_a') or {}
        set_b = test_dict.get('set_b') or {}
        self._set_sources(set_a.get('source'),
                          self.chk_src_a_host, self.chk_src_a_arch, self.chk_src_a_struct)
        self._set_sources(set_b.get('source'),
                          self.chk_src_b_host, self.chk_src_b_arch, self.chk_src_b_struct)
        self._set_categories_checked(self.sp_categories_a, set_a.get('categories', []))
        self._set_categories_checked(self.sp_categories_b, set_b.get('categories', []))

        self._set_combo_by_text(self.cmb_assignee,
                                test_dict.get('default_assignee') or 'Mechanical')

    def _clear_editor(self):
        self._current_store    = None
        self._current_id       = None
        self._firmwide_unlocked = False
        self.txt_editor_header.Text = "(no test selected)"
        self.txt_source_badge.Text  = "—"
        self.brd_source_badge.Background = _brush('#EDF2F7')
        self.brd_source_badge.BorderBrush = _brush('#CBD5E0')
        self.txt_source_badge.Foreground = _brush('#4A5568')
        self.txt_test_name.Text = ''
        self.txt_tolerance.Text = '0.0'
        self._set_combo_by_text(self.cmb_kind, 'Hard')
        self._sync_tolerance_enabled()
        for chk in (self.chk_src_a_host, self.chk_src_a_arch, self.chk_src_a_struct,
                    self.chk_src_b_host, self.chk_src_b_arch, self.chk_src_b_struct):
            chk.IsChecked = False
        self._set_categories_checked(self.sp_categories_a, [])
        self._set_categories_checked(self.sp_categories_b, [])
        self._set_combo_by_text(self.cmb_assignee, 'Mechanical')
        self.txt_editor_status.Text = ""
        self._apply_banners()

    def _set_editor_enabled(self, enabled):
        """Enable/disable every editable widget in the bottom card."""
        for ctrl in (self.txt_test_name, self.cmb_kind, self.txt_tolerance,
                     self.chk_src_a_host, self.chk_src_a_arch, self.chk_src_a_struct,
                     self.chk_src_b_host, self.chk_src_b_arch, self.chk_src_b_struct,
                     self.sp_categories_a, self.sp_categories_b,
                     self.cmb_assignee, self.btn_save, self.btn_delete_test):
            ctrl.IsEnabled = enabled
        # Tolerance is special: even when the editor is enabled, it's only
        # meaningful for soft clashes. _sync_tolerance_enabled() refines it.
        if enabled:
            self._sync_tolerance_enabled()

    def _apply_banners(self):
        """Sync banner visibility + editor lock state with the current store.

        State machine, driven entirely by `self._current_store` and
        `self._firmwide_unlocked`:

        - global, locked: blue locked banner + small "Edit firm-wide…" link;
          editor read-only; Save/Delete disabled. The Customize button is the
          obvious next click.
        - global, unlocked: yellow firm-wide warning banner with Cancel
          firm-wide edit; editor editable; Save enabled. Locked link hidden.
        - override:custom: no banners; editor editable; Save enabled.
        - override:disabled (or no selection): no banners; editor read-only;
          Save/Delete disabled.
        """
        store     = self._current_store
        is_global = (store == 'global')
        editable  = (
            store == 'override:custom'
            or (is_global and self._firmwide_unlocked)
        )

        # Banners
        self.brd_locked_banner.Visibility = (
            Visibility.Visible if (is_global and not self._firmwide_unlocked)
            else Visibility.Collapsed
        )
        self.brd_firmwide_warning.Visibility = (
            Visibility.Visible if (is_global and self._firmwide_unlocked)
            else Visibility.Collapsed
        )
        self.dp_firmwide_link.Visibility = self.brd_locked_banner.Visibility

        # Customize button: only meaningful if a project is open. Visually
        # disabled when not, so the locked banner still explains what's going
        # on instead of just hiding the button.
        self.btn_customize_for_project.IsEnabled = bool(self._project_hash)

        # Editor + Save/Delete
        self._set_editor_enabled(editable)

    def _sync_tolerance_enabled(self):
        kind = self._get_combo_text(self.cmb_kind) or 'Hard'
        self.txt_tolerance.IsEnabled = (kind == 'Soft')
        if kind != 'Soft':
            try:
                if float(self.txt_tolerance.Text or '0') != 0.0:
                    self.txt_tolerance.Text = '0.0'
            except ValueError:
                self.txt_tolerance.Text = '0.0'

    def _on_kind_changed(self, sender, args):
        self._sync_tolerance_enabled()

    @staticmethod
    def _set_combo_by_text(combo, text):
        for item in combo.Items:
            content = getattr(item, 'Content', None)
            if content == text:
                combo.SelectedItem = item
                return

    @staticmethod
    def _get_combo_text(combo):
        item = combo.SelectedItem
        if item is None:
            return None
        return getattr(item, 'Content', None)

    # --- Editor -> dict ------------------------------------------------

    def _collect_editor_values(self, base_id):
        """Build a JSON-shaped test dict from the current form state.

        Raises ValueError with a user-readable message if anything required
        is missing or malformed.
        """
        name = (self.txt_test_name.Text or '').strip()
        if not name:
            raise ValueError("Test name is required.")

        kind_display = self._get_combo_text(self.cmb_kind) or 'Hard'
        kind = KIND_DISPLAY_TO_VALUE.get(kind_display, 'hard')

        tolerance = 0.0
        if kind == 'soft':
            raw = (self.txt_tolerance.Text or '0').strip()
            try:
                tolerance = float(raw)
            except ValueError:
                raise ValueError("Tolerance must be a number (got '{}').".format(raw))
            if tolerance <= 0.0:
                raise ValueError("Soft clashes need a tolerance greater than 0 inches.")

        set_a_sources = self._collect_sources(
            self.chk_src_a_host, self.chk_src_a_arch, self.chk_src_a_struct)
        set_b_sources = self._collect_sources(
            self.chk_src_b_host, self.chk_src_b_arch, self.chk_src_b_struct)
        if not set_a_sources:
            raise ValueError("Set A needs at least one source (Host or a linked model).")
        if not set_b_sources:
            raise ValueError("Set B needs at least one source (Host or a linked model).")

        set_a_categories = self._collect_categories(self.sp_categories_a)
        set_b_categories = self._collect_categories(self.sp_categories_b)
        if not set_a_categories:
            raise ValueError("Set A needs at least one category checked.")
        if not set_b_categories:
            raise ValueError("Set B needs at least one category checked.")

        assignee = self._get_combo_text(self.cmb_assignee) or 'Mechanical'

        return {
            'id':               base_id,
            'name':             name,
            'kind':             kind,
            'tolerance_inches': tolerance,
            'set_a':            {'source': set_a_sources, 'categories': set_a_categories},
            'set_b':            {'source': set_b_sources, 'categories': set_b_categories},
            'default_assignee': assignee,
        }

    # --- Persistence wrappers -------------------------------------------

    def _persist_global(self, new_test):
        library = persistence.read_global_test_library()
        tests = library.get('tests', [])
        _upsert_by_id(tests, new_test)
        library['tests'] = tests
        persistence.write_global_test_library(library)

    def _persist_override(self, new_test):
        if not self._project_hash:
            raise persistence.SharedFolderNotConfigured(
                "No active project — open a Revit project before saving overrides.")
        ov = persistence.read_overrides(self._project_hash)
        custom = ov.get('custom_tests', [])
        _upsert_by_id(custom, new_test)
        ov['custom_tests'] = custom
        persistence.write_overrides(self._project_hash, ov)

    def _delete_global(self, test_id):
        library = persistence.read_global_test_library()
        library['tests'] = _remove_by_id(library.get('tests', []), test_id)
        persistence.write_global_test_library(library)

    def _delete_override(self, test_id):
        if not self._project_hash:
            return
        ov = persistence.read_overrides(self._project_hash)
        ov['custom_tests'] = _remove_by_id(ov.get('custom_tests', []), test_id)
        ov['disabled_test_ids'] = [tid for tid in ov.get('disabled_test_ids', [])
                                   if tid != test_id]
        persistence.write_overrides(self._project_hash, ov)

    # --- Refresh + re-select --------------------------------------------

    def _reload(self, select_id=None, select_in='global'):
        """Re-read both stores from disk, refresh the grids, and re-select
        `select_id` in the named grid ('global' or 'override') if provided."""
        try:
            library = _ensure_library_seeded()
            self._global_tests = [TestRow(t) for t in library.get('tests', [])]
        except persistence.SharedFolderNotConfigured:
            self._global_tests = []
            library = {'tests': []}

        if self._project_hash:
            try:
                ov = persistence.read_overrides(self._project_hash)
                self._overrides = _build_override_rows(ov, library.get('tests', []))
            except Exception:
                self._overrides = []
        else:
            self._overrides = []

        self.dg_global.ItemsSource    = self._global_tests
        self.dg_overrides.ItemsSource = self._overrides
        self.txt_global_count.Text   = '{} tests'.format(len(self._global_tests))
        self.txt_override_count.Text = '{} overrides'.format(len(self._overrides))

        if select_id is None:
            self._clear_editor()
            return
        target_grid = self.dg_global if select_in == 'global' else self.dg_overrides
        for i, row in enumerate(target_grid.ItemsSource or []):
            src = getattr(row, 'Source', None)
            if src and src.get('id') == select_id:
                target_grid.SelectedIndex = i
                return
        # Couldn't find it — clear instead of leaving stale editor state.
        self._clear_editor()

    # --- Action handlers -------------------------------------------------

    def _on_save(self, sender, args):
        if not self._current_id or self._current_store not in ('global', 'override:custom'):
            dbhms_ui.info(
                "Pick a test on the left first, or click + New Test to start a fresh one.",
                title='Nothing to save',
            )
            return
        try:
            new_test = self._collect_editor_values(base_id=self._current_id)
        except ValueError as ex:
            dbhms_ui.info(str(ex), title='Invalid input')
            return
        try:
            if self._current_store == 'global':
                self._persist_global(new_test)
                store_label = 'global library'
                select_in   = 'global'
            else:
                self._persist_override(new_test)
                store_label = 'project overrides'
                select_in   = 'override'
        except persistence.SharedFolderNotConfigured as ex:
            dbhms_ui.info(str(ex), title='Setup needed')
            return
        except Exception as ex:
            dbhms_ui.info(
                "Could not save the test:\n\n{}".format(ex),
                title='Save failed',
            )
            return

        self._reload(select_id=new_test['id'], select_in=select_in)
        self.txt_editor_status.Text = "Saved to {}.".format(store_label)

    def _on_new_project_test(self, sender, args):
        """Create a blank test in the active project's overrides (the easy path)."""
        if not self._project_hash:
            dbhms_ui.info(
                "Open a Revit project first so the new test has a project to live under.\n\n"
                "If you want a firm-wide test that applies to every project, use "
                "+ New Firm Test instead.",
                title='No active project',
            )
            return
        blank = _blank_test('New Project Test')
        try:
            self._persist_override(blank)
        except persistence.SharedFolderNotConfigured as ex:
            dbhms_ui.info(str(ex), title='Setup needed'); return
        except Exception as ex:
            dbhms_ui.info("Could not create:\n\n{}".format(ex), title='Create failed'); return
        self._reload(select_id=blank['id'], select_in='override')
        self.txt_editor_status.Text = (
            "New project test created. Edit name + categories, then Save Changes."
        )

    def _on_new_firm_test(self, sender, args):
        """Create a blank firm-wide test (with a confirmation, since this affects every project)."""
        confirmed = forms.alert(
            "Create a new FIRM-WIDE test?\n\n"
            "This adds a new test to the global library, which every project on every "
            "computer will use from now on.\n\n"
            "If you only want a test for the active project, click Cancel and use "
            "+ New Project Test instead.",
            title='Add a firm-wide test?',
            yes=True, no=True,
        )
        if not confirmed:
            return
        blank = _blank_test('New Firm Test')
        try:
            self._persist_global(blank)
        except persistence.SharedFolderNotConfigured as ex:
            dbhms_ui.info(str(ex), title='Setup needed'); return
        except Exception as ex:
            dbhms_ui.info("Could not create:\n\n{}".format(ex), title='Create failed'); return
        # Newly created firm-wide tests are immediately editable so the user
        # can fill in name + categories without an extra unlock step. Any
        # later edit of an existing firm-wide test still requires unlocking.
        self._firmwide_unlocked = True
        self._reload(select_id=blank['id'], select_in='global')
        self.txt_editor_status.Text = (
            "New firm-wide test created — editor is unlocked for this test. "
            "Edit name + categories, then Save Changes."
        )

    def _on_customize_for_project(self, sender, args):
        """Fork the currently-selected global test into a project override.

        This is the primary path for "I want this test, but tweaked for this
        one project." Replaces the old + Add Project Override button.
        """
        if not self._project_hash:
            dbhms_ui.info(
                "Open a Revit project first so the override has a project to live under.",
                title='No active project',
            )
            return
        items_global = list(self.dg_global.SelectedItems)
        if not items_global:
            dbhms_ui.info(
                "Select a firm-wide test on the left first, then click "
                "Customize for this Project.",
                title='Nothing selected',
            )
            return

        # Deep-clone the source dict so the override owns its own data and
        # editing it never accidentally mutates the in-memory global.
        src = dict(items_global[0].Source)
        src['set_a'] = dict(src.get('set_a') or {})
        src['set_b'] = dict(src.get('set_b') or {})
        src['set_a']['categories'] = list(src['set_a'].get('categories') or [])
        src['set_b']['categories'] = list(src['set_b'].get('categories') or [])
        new_test = src
        new_test['id']   = _new_test_id()
        new_test['name'] = src.get('name', 'Test') + ' (project)'

        try:
            self._persist_override(new_test)
        except persistence.SharedFolderNotConfigured as ex:
            dbhms_ui.info(str(ex), title='Setup needed'); return
        except Exception as ex:
            dbhms_ui.info(
                "Could not create the project override:\n\n{}".format(ex),
                title='Create failed')
            return
        self._reload(select_id=new_test['id'], select_in='override')
        self.txt_editor_status.Text = (
            "Project override created from the firm default. Edit fields above, then Save Changes."
        )

    def _on_edit_firmwide(self, sender, args):
        """Unlock the editor for firm-wide editing of the selected global test.

        Always asks for explicit confirmation — accidentally hitting Save
        Changes on a global test is the failure mode this whole locked-mode
        UX is designed to prevent.
        """
        if self._current_store != 'global' or not self._current_id:
            return
        confirmed = forms.alert(
            "Unlock this test for FIRM-WIDE editing?\n\n"
            "While unlocked, Save Changes will overwrite the firm default, affecting "
            "every project on every computer that uses the global library.\n\n"
            "If you only want to change this test for the active project, click "
            "Cancel and use Customize for this Project instead.",
            title='Edit firm-wide library?',
            yes=True, no=True,
        )
        if not confirmed:
            return
        self._firmwide_unlocked = True
        self._apply_banners()
        self.txt_editor_status.Text = (
            "Firm-wide editor unlocked for this test. Save Changes will overwrite the firm default."
        )

    def _on_cancel_firmwide_edit(self, sender, args):
        """Re-lock the firm-wide editor for the current global test."""
        if self._current_store != 'global':
            return
        self._firmwide_unlocked = False
        self._apply_banners()
        self.txt_editor_status.Text = "Firm-wide edit canceled. The editor is locked again."

    def _on_delete(self, sender, args):
        if not self._current_id or self._current_store not in ('global', 'override:custom'):
            dbhms_ui.info(
                "Pick a test on the left first. (Disabled-override rows can't be deleted "
                "from this iteration.)",
                title='Nothing to delete',
            )
            return
        test_label = self.txt_test_name.Text or self._current_id
        if self._current_store == 'global':
            confirmed = forms.alert(
                "Delete the firm-wide test '{}'?\n\n"
                "This affects EVERY project that uses the global library. "
                "Project-specific overrides are not touched."
                .format(test_label),
                title='Delete firm-wide test?',
                yes=True, no=True,
            )
            if not confirmed:
                return
            try:
                self._delete_global(self._current_id)
            except persistence.SharedFolderNotConfigured as ex:
                dbhms_ui.info(str(ex), title='Setup needed'); return
            except Exception as ex:
                dbhms_ui.info("Delete failed:\n\n{}".format(ex), title='Delete failed'); return
            self._reload()
            self.txt_editor_status.Text = "Deleted from the firm-wide library."
        else:  # override:custom
            confirmed = forms.alert(
                "Delete the project override '{}'?\n\n"
                "This affects only the active project."
                .format(test_label),
                title='Delete project override?',
                yes=True, no=True,
            )
            if not confirmed:
                return
            try:
                self._delete_override(self._current_id)
            except Exception as ex:
                dbhms_ui.info("Delete failed:\n\n{}".format(ex), title='Delete failed'); return
            self._reload()
            self.txt_editor_status.Text = "Deleted from project overrides."

    def _on_reset_default(self, sender, args):
        """Overwrite the global test_library.json with the shipped defaults
        (default_tests.json next to this script). Project overrides aren't touched."""
        try:
            seed_tests = _read_seed_tests()
        except Exception as ex:
            dbhms_ui.info(
                "Couldn't read default_tests.json:\n\n{}".format(ex),
                title='Reset failed',
            )
            return
        if not seed_tests:
            dbhms_ui.info(
                "default_tests.json had no tests; nothing to reset to.",
                title='Reset failed',
            )
            return

        confirmed = forms.alert(
            "Reset the firm-wide test library to the shipped defaults?\n\n"
            "This OVERWRITES <shared>/global/test_library.json with the {} "
            "tests from default_tests.json. Any custom edits you've made to "
            "the global library will be lost.\n\n"
            "Per-project overrides are NOT affected.\n\n"
            "Use this after a tool update to pick up new firm-default tests, "
            "or to undo accidental changes.".format(len(seed_tests)),
            title='Reset library to firm defaults?',
            yes=True, no=True,
        )
        if not confirmed:
            return

        try:
            new_library = {'$schema_version': 1, 'tests': seed_tests}
            persistence.write_global_test_library(new_library)
        except persistence.SharedFolderNotConfigured as ex:
            dbhms_ui.info(str(ex), title='Setup needed')
            return
        except Exception as ex:
            dbhms_ui.info(
                "Couldn't write the global library:\n\n{}".format(ex),
                title='Reset failed',
            )
            return

        self._reload(select_id=(seed_tests[0].get('id') if seed_tests else None),
                     select_in='global')
        self.txt_editor_status.Text = (
            "Library reset. {} firm-default tests are now active.".format(len(seed_tests))
        )

    def _on_close(self, sender, args):
        self.Close()


with dbhms_telemetry.session(__title__, script_path=__file__):
    TestLibraryForm().ShowDialog()
