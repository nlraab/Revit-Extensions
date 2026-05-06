# -*- coding: utf-8 -*-
"""Test Library - manage the global clash-test library + per-project overrides.

Now reads real data from `clash_core.persistence`:
  * Global library: <shared>/global/test_library.json
    First time it's read on a new shared folder, the file is automatically
    seeded with the firm-default tests shipped in `default_tests.json` next
    to this script.
  * Project overrides: <shared>/<project-hash>/test_overrides.json
    Loaded when an active Revit doc is available.

Selecting a test in either grid populates the editor card below: name,
kind, tolerance, both sets' source + categories, and default assignee
trade.

Editing tests + writing changes to disk lands in the next iteration. For
this iteration the editor is a viewer; Save Changes pops a "coming soon"
alert with that note.

See Clash Detection.tab/README.md for the architecture.
"""

__title__  = 'Test\nLibrary'
__author__ = 'Nathaniel'
__doc__    = "View and (next iteration) edit the global clash-test library and project overrides."

import codecs
import json
import os

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import Thickness
from System.Windows.Controls import CheckBox
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms

from clash_core import config, persistence, project
from clash_core import categories as cat_module


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'TestLibraryForm.xaml')
SEED_FILE  = os.path.join(SCRIPT_DIR, 'default_tests.json')


# Display <-> JSON-value mappings for editor dropdowns
SOURCE_DISPLAY_TO_VALUE = {
    "Host (active project)": "host",
    "Linked Architectural":  "link:Architectural",
    "Linked Structural":     "link:Structural",
}
SOURCE_VALUE_TO_DISPLAY = {v: k for k, v in SOURCE_DISPLAY_TO_VALUE.items()}

KIND_VALUE_TO_DISPLAY = {
    "hard":      "Hard",
    "soft":      "Soft",
    "clearance": "Clearance (planned)",
}


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
    def __init__(self, source_dict, kind_label, name=None, assignee=None):
        self.Source = source_dict
        self.Name = name or source_dict.get('name', '<unnamed>')
        self.OverrideKind = kind_label  # 'Disabled' / 'Custom'
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
    """Build OverrideRow objects from the overrides JSON shape.

    For "Disabled" overrides we look up the test name from the library so
    the row shows something readable instead of just an id.
    """
    out = []
    library_by_id = {t.get('id'): t for t in library_tests}
    for tid in overrides_dict.get('disabled_test_ids', []):
        ref = library_by_id.get(tid, {})
        out.append(OverrideRow(
            ref or {'name': tid},
            kind_label='Disabled',
            name=ref.get('name', tid),
            assignee=ref.get('default_assignee', '-'),
        ))
    for ct in overrides_dict.get('custom_tests', []):
        out.append(OverrideRow(ct, 'Custom'))
    return out


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

        # Load library + overrides
        self._global_tests = []
        self._overrides   = []
        self._project_hash = None
        self._setup_message = None

        try:
            library = _ensure_library_seeded()
            self._global_tests = [TestRow(t) for t in library.get('tests', [])]

            # Per-project overrides (only if there's an active doc with a saved central path)
            try:
                from pyrevit import revit
                doc = revit.doc
                if doc is not None:
                    ph = project.project_hash_for(doc)
                    if ph:
                        self._project_hash = ph
                        ov = persistence.read_overrides(ph)
                        self._overrides = _build_override_rows(ov, library.get('tests', []))
            except Exception:
                pass

        except persistence.SharedFolderNotConfigured as ex:
            self._setup_message = str(ex)

        self.dg_global.ItemsSource = self._global_tests
        self.dg_overrides.ItemsSource = self._overrides
        self.txt_global_count.Text   = '{} tests'.format(len(self._global_tests))
        self.txt_override_count.Text = '{} overrides'.format(len(self._overrides))

        # Wire events
        self.btn_close.Click               += self._on_close
        self.btn_save.Click                += self._on_save
        self.btn_new_test.Click            += self._on_coming_soon
        self.btn_delete_test.Click         += self._on_coming_soon
        self.btn_reset_default.Click       += self._on_reset_default
        self.btn_add_override.Click        += self._on_coming_soon
        self.dg_global.SelectionChanged    += self._on_global_selected
        self.dg_overrides.SelectionChanged += self._on_override_selected

        # Pre-select first global test, or show first-run alert
        if self._setup_message:
            self.txt_editor_header.Text = "No global library yet"
            self.Loaded += self._show_first_run_alert
        elif self._global_tests:
            self.dg_global.SelectedIndex = 0

    def _show_first_run_alert(self, sender, args):
        # Once - detach so reopening doesn't fire it again
        self.Loaded -= self._show_first_run_alert
        forms.alert(
            "The Test Library can't load:\n\n{}\n\n"
            "Open Settings (Clash Detection tab) and pick a shared "
            "clash-data folder, then come back.".format(self._setup_message),
            title='Setup needed',
        )

    # --- Category list construction -------------------------------------

    def _populate_category_list(self, panel):
        """Add a CheckBox per category from clash_core.categories.

        Each CheckBox.Tag = OST_ name; Content = friendly name.
        """
        panel.Children.Clear()
        for ost, friendly, _group in cat_module.CATEGORIES:
            chk = CheckBox()
            chk.Content = friendly
            chk.Tag = ost
            chk.Margin = Thickness(0, 3, 0, 3)
            panel.Children.Add(chk)

    def _set_categories_checked(self, panel, ost_list):
        """Walk the panel's CheckBox children; check those whose Tag is in ost_list."""
        ost_set = set(ost_list)
        for child in panel.Children:
            if hasattr(child, 'Tag') and hasattr(child, 'IsChecked'):
                child.IsChecked = (child.Tag in ost_set)

    # --- Selection handlers ---------------------------------------------

    def _on_global_selected(self, sender, args):
        items = list(self.dg_global.SelectedItems)
        if not items:
            return
        self.dg_overrides.UnselectAll()
        self._populate_editor(
            items[0].Source,
            source_label='Global default',
            bg='#EBF8FF', border='#90CDF4', fg='#2A4365',
        )

    def _on_override_selected(self, sender, args):
        items = list(self.dg_overrides.SelectedItems)
        if not items:
            return
        self.dg_global.UnselectAll()
        self._populate_editor(
            items[0].Source,
            source_label='Project override',
            bg='#FAF5FF', border='#D6BCFA', fg='#553C9A',
        )

    def _populate_editor(self, test_dict, source_label, bg, border, fg):
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

        set_a = test_dict.get('set_a') or {}
        set_b = test_dict.get('set_b') or {}
        self._set_combo_by_text(
            self.cmb_source_a,
            SOURCE_VALUE_TO_DISPLAY.get(set_a.get('source'), 'Host (active project)'),
        )
        self._set_combo_by_text(
            self.cmb_source_b,
            SOURCE_VALUE_TO_DISPLAY.get(set_b.get('source'), 'Host (active project)'),
        )
        self._set_categories_checked(self.sp_categories_a, set_a.get('categories', []))
        self._set_categories_checked(self.sp_categories_b, set_b.get('categories', []))

        self._set_combo_by_text(self.cmb_assignee,
                                test_dict.get('default_assignee') or 'Mechanical')

    @staticmethod
    def _set_combo_by_text(combo, text):
        for item in combo.Items:
            content = getattr(item, 'Content', None)
            if content == text:
                combo.SelectedItem = item
                return

    # --- Action handlers -------------------------------------------------

    def _on_save(self, sender, args):
        forms.alert(
            "Editing clash tests isn't wired up yet.\n\n"
            "This iteration: the Test Library reads your real shipped library "
            "(seeded automatically from default_tests.json on first launch) and "
            "the editor displays whatever's in the selected test. Edits you make "
            "here are visible but not persisted.\n\n"
            "If you want to refresh the global library with shipped defaults "
            "(after a tool update), use the **Reset to firm default** button "
            "at the bottom-left.",
            title='Coming Soon - Save changes to library',
        )

    def _on_reset_default(self, sender, args):
        """Overwrite the global test_library.json with the shipped defaults
        (default_tests.json next to this script). Project overrides aren't touched."""
        try:
            seed_tests = _read_seed_tests()
        except Exception as ex:
            forms.alert(
                "Couldn't read default_tests.json:\n\n{}".format(ex),
                title='Reset failed',
            )
            return
        if not seed_tests:
            forms.alert(
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
            forms.alert(str(ex), title='Setup needed')
            return
        except Exception as ex:
            forms.alert(
                "Couldn't write the global library:\n\n{}".format(ex),
                title='Reset failed',
            )
            return

        # Refresh the form in place so the new tests show up immediately.
        self._global_tests = [TestRow(t) for t in seed_tests]
        self.dg_global.ItemsSource = self._global_tests
        self.txt_global_count.Text = '{} tests'.format(len(self._global_tests))
        if self._global_tests:
            self.dg_global.SelectedIndex = 0

        forms.alert(
            "Library reset. {} tests now in the global library.\n\n"
            "Open Run Clash Test next to use them.".format(len(seed_tests)),
            title='Reset complete',
        )

    def _on_coming_soon(self, sender, args):
        forms.alert(
            "Adding/deleting tests and creating project overrides lands in the next "
            "iteration (alongside the Save Changes wiring).",
            title='Coming Soon',
        )

    def _on_close(self, sender, args):
        self.Close()


TestLibraryForm().ShowDialog()
