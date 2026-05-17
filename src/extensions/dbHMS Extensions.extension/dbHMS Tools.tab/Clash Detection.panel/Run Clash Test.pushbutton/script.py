# -*- coding: utf-8 -*-
"""Run Clash Test - real detection against the active project.

Reads the saved test library from <shared>/global/test_library.json,
enumerates live linked models via the per-project role mapping, runs the
selected tests through clash_detect.runner, merges with the project's
existing clashes.json (preserving comments / status / history on
persisting clashes, auto-resolving disappeared clashes), and writes the
new clashes.json.

Detection currently blocks the UI while it runs. Typical projects (under
a few thousand elements per side) finish in seconds.

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
"""

__title__  = 'Run\nClash Test'
__author__ = 'Nathaniel'
__doc__    = ('Pick saved tests, choose which linked models to include, and execute '
              'real clash detection against the active project.')

import os
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System import Action, TimeSpan
from System.Windows import (
    Visibility, Thickness, GridLength, GridUnitType,
    VerticalAlignment, FontWeights, CornerRadius,
    RoutedEventHandler,
)
from System.Windows.Controls import (
    Grid, ColumnDefinition, CheckBox, TextBlock, Border,
)
from System.Windows.Media import SolidColorBrush, Color, FontFamily
from System.Windows.Threading import (
    DispatcherTimer, Dispatcher, DispatcherFrame, DispatcherPriority,
)

from pyrevit import forms, script
import dbhms_ui
import dbhms_telemetry

from clash_core import config, persistence, project, users, merge, dedupe
from clash_core.models import _now_iso
from clash_detect import linked, runner

output = script.get_output()


# ---------------------------------------------------------------------------
# Doc census (debug helper)
# ---------------------------------------------------------------------------

def _log_doc_census(doc, label, log, max_categories=40):
    """List the top N categories in `doc` by element-instance count."""
    from Autodesk.Revit.DB import FilteredElementCollector
    from clash_detect._compat import eid_int

    log("---")
    log("**Doc census:** {}".format(label))
    try:
        log("  PathName: `{}`".format(doc.PathName or '(unsaved)'))
    except Exception:
        pass
    try:
        log("  Title: `{}`".format(doc.Title))
    except Exception:
        pass

    try:
        all_instances = list(
            FilteredElementCollector(doc)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception as ex:
        log("  **FilteredElementCollector failed**: {}".format(ex))
        return

    log("  Total element instances in doc: **{}**".format(len(all_instances)))

    counts = {}  # (cat_name, cat_id_int) -> count
    no_cat = 0
    cat_access_errs = 0
    for elem in all_instances:
        try:
            cat = elem.Category
        except Exception:
            cat_access_errs += 1
            continue
        if cat is None:
            no_cat += 1
            continue
        try:
            cat_name = cat.Name
        except Exception:
            cat_name = "?"
        try:
            cat_id = eid_int(cat.Id)
        except Exception:
            cat_id = 0
        key = (cat_name, cat_id)
        counts[key] = counts.get(key, 0) + 1

    if no_cat:
        log("  (uncategorized: {})".format(no_cat))
    if cat_access_errs:
        log("  (Category access errors: {})".format(cat_access_errs))

    items = sorted(counts.items(), key=lambda x: -x[1])[:max_categories]
    if not items:
        log("  *(no categorized elements)*")
        return

    log("  Top {} categories by count:".format(len(items)))
    for (name, cid), n in items:
        log("    - **{}**: {} (cat id `{}`)".format(name, n, cid))


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'RunClashTestForm.xaml')


def _brush(hex_str):
    h = hex_str.lstrip('#')
    return SolidColorBrush(Color.FromRgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))


def _flush_ui():
    """Process pending WPF events so the UI repaints before a blocking call.

    WPF normally batches updates; if we set status text then run a 5-second
    detection on the same thread, the user never sees the new status. This
    pumps the dispatcher once at Background priority to flush any pending
    layout/render work.
    """
    frame = DispatcherFrame()
    Dispatcher.CurrentDispatcher.BeginInvoke(
        DispatcherPriority.Background,
        Action(lambda: setattr(frame, 'Continue', False)),
    )
    Dispatcher.PushFrame(frame)


# ---------------------------------------------------------------------------
# View-model
# ---------------------------------------------------------------------------

class TestRow(object):
    def __init__(self, test_dict, selected=False, last_run='-'):
        self.Source = test_dict
        self.Name = test_dict.get('name', '<unnamed>')
        kind_value = test_dict.get('kind') or 'hard'
        self.Kind = kind_value.capitalize()
        self.Assignee = test_dict.get('default_assignee') or '-'
        self.LastRun = last_run
        self.Selected = selected


# Sample fallback for first-run / unconfigured state - keeps the layout
# visible even before the user sets up Settings + Test Library.
_SAMPLE_TESTS = [
    {'name': 'Mechanical Ducts vs Plumbing Pipes',          'kind': 'hard', 'default_assignee': 'Mechanical'},
    {'name': 'Mechanical Ducts vs Electrical Conduit',      'kind': 'hard', 'default_assignee': 'Mechanical'},
    {'name': 'Mechanical Equipment vs Architectural Walls', 'kind': 'hard', 'default_assignee': 'Mechanical'},
    {'name': 'Plumbing Pipes vs Structural Framing',        'kind': 'hard', 'default_assignee': 'Plumbing'},
    {'name': 'All MEP near Architectural Ceilings',         'kind': 'soft', 'default_assignee': 'Mechanical'},
]


def _read_test_rows():
    """Return (TestRow list, fallback_message_or_None, is_real_data)."""
    try:
        library = persistence.read_global_test_library()
    except persistence.SharedFolderNotConfigured as ex:
        return (
            [TestRow(t, selected=True) for t in _SAMPLE_TESTS],
            'Sample tests shown - {}'.format(ex),
            False,
        )

    tests = library.get('tests') or []
    if not tests:
        return (
            [TestRow(t, selected=True) for t in _SAMPLE_TESTS],
            'No saved tests yet - open Test Library to seed defaults from the firm library.',
            False,
        )

    return (
        [TestRow(t, selected=True, last_run=t.get('last_run') or 'never')
         for t in tests],
        None,
        True,
    )


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------

class RunClashTestForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._run_link_rows = []  # list of (title, role, CheckBox)
        self._timer = None
        self._tick_count = 0
        self._project_hash = None
        self._role_map = {}

        # Tests
        self._tests, banner, self._real_data = _read_test_rows()
        self.dg_tests.ItemsSource = self._tests
        self.txt_tests_header.Text = 'Saved Clash Tests ({})'.format(len(self._tests))

        # Per-machine warn threshold prefill
        cfg = config.load()
        self.txt_warn_threshold.Text = str(cfg.get('warn_threshold') or 2000)

        # Project + link rows
        self._populate_link_section()

        # Banner / status text
        if banner:
            self.txt_status.Text = banner

        # Wire events
        self.btn_close.Click         += self._on_close
        self.btn_run.Click           += self._on_run
        self.btn_select_all.Click    += self._on_select_all
        self.btn_select_none.Click   += self._on_select_none
        self.btn_open_browser.Click  += self._on_open_browser
        self.btn_open_settings.Click += self._on_open_settings

        # Excel-style bulk toggle for the test checkboxes
        self.dg_tests.AddHandler(
            CheckBox.ClickEvent,
            RoutedEventHandler(self._on_test_checkbox_click),
        )

    # --- Project / link section -----------------------------------------

    def _populate_link_section(self):
        try:
            from pyrevit import revit
            doc = revit.doc
        except Exception:
            doc = None
        if doc is None:
            self._populate_link_rows_empty('(no active Revit document)')
            return

        try:
            ph = project.project_hash_for(doc)
        except Exception:
            ph = ''

        if not ph:
            self._populate_link_rows_empty('(project not yet saved)')
            return

        self._project_hash = ph
        try:
            meta = persistence.read_project_meta(ph)
            self._role_map = meta.get('link_role_map') or {}
        except persistence.SharedFolderNotConfigured:
            self._role_map = {}
        except Exception:
            self._role_map = {}

        try:
            view = linked.merged_link_view(doc, self._role_map)
        except Exception:
            self._populate_link_rows_empty('(unable to enumerate linked models)')
            return

        self.sp_run_link_rows.Children.Clear()
        self._run_link_rows = []
        if not view:
            self._populate_link_rows_empty('(no linked models loaded in this project)')
            return
        for entry in view:
            self.sp_run_link_rows.Children.Add(
                self._build_run_link_row(entry['title'], entry['role'])
            )

    def _populate_link_rows_empty(self, message):
        self.sp_run_link_rows.Children.Clear()
        self._run_link_rows = []
        from System.Windows import FontStyles
        empty = TextBlock()
        empty.Text = message
        empty.FontSize = 11
        empty.FontStyle = FontStyles.Italic
        empty.Foreground = _brush('#A0AEC0')
        empty.Margin = Thickness(0, 6, 0, 6)
        self.sp_run_link_rows.Children.Add(empty)

    def _build_run_link_row(self, title, role):
        grid = Grid()
        grid.Margin = Thickness(0, 2, 0, 2)

        cd1 = ColumnDefinition(); cd1.Width = GridLength.Auto
        cd2 = ColumnDefinition(); cd2.Width = GridLength(1, GridUnitType.Star)
        cd3 = ColumnDefinition(); cd3.Width = GridLength.Auto
        grid.ColumnDefinitions.Add(cd1)
        grid.ColumnDefinitions.Add(cd2)
        grid.ColumnDefinitions.Add(cd3)

        chk = CheckBox()
        chk.IsChecked = (role != linked.ROLE_IGNORE)
        Grid.SetColumn(chk, 0)
        grid.Children.Add(chk)

        title_tb = TextBlock()
        title_tb.Text = title
        title_tb.FontFamily = FontFamily("Consolas")
        title_tb.FontSize = 11
        title_tb.VerticalAlignment = VerticalAlignment.Center
        title_tb.Margin = Thickness(6, 0, 6, 0)
        title_tb.Foreground = _brush('#A0AEC0' if role == linked.ROLE_IGNORE else '#2D3748')
        Grid.SetColumn(title_tb, 1)
        grid.Children.Add(title_tb)

        badge_border = Border()
        badge_border.CornerRadius = CornerRadius(3)
        badge_border.Padding = Thickness(6, 1, 6, 1)
        badge_border.VerticalAlignment = VerticalAlignment.Center
        badge_border.BorderThickness = Thickness(1)
        badge_tb = TextBlock()
        badge_tb.FontSize = 10
        badge_tb.FontWeight = FontWeights.SemiBold
        if role == linked.ROLE_ARCHITECTURAL:
            badge_border.Background = _brush('#EBF8FF')
            badge_border.BorderBrush = _brush('#90CDF4')
            badge_tb.Text = 'Architectural'
            badge_tb.Foreground = _brush('#2A4365')
        elif role == linked.ROLE_STRUCTURAL:
            badge_border.Background = _brush('#FAF5FF')
            badge_border.BorderBrush = _brush('#D6BCFA')
            badge_tb.Text = 'Structural'
            badge_tb.Foreground = _brush('#553C9A')
        else:
            badge_border.Background = _brush('#F7FAFC')
            badge_border.BorderBrush = _brush('#CBD5E0')
            badge_tb.Text = '(unassigned)'
            badge_tb.Foreground = _brush('#718096')
        badge_border.Child = badge_tb
        Grid.SetColumn(badge_border, 2)
        grid.Children.Add(badge_border)

        self._run_link_rows.append((title, role, chk))
        return grid

    # --- Selection helpers ----------------------------------------------

    def _selected_count(self):
        return sum(1 for t in self._tests if t.Selected)

    def _selected_test_dicts(self):
        return [t.Source for t in self._tests if t.Selected]

    def _enabled_link_titles(self):
        """Titles of linked .rvt files the user wants included this run."""
        return [t for t, role, chk in self._run_link_rows
                if chk.IsChecked and role != linked.ROLE_IGNORE]

    def _collect_trade_filter(self):
        """Return a set of trade names from the trade filter checkboxes,
        or None if every trade is checked (= no filter, save the work).
        """
        wanted = []
        for chk_name, trade in (
            ('chk_trade_mech',  'Mechanical'),
            ('chk_trade_elec',  'Electrical'),
            ('chk_trade_plumb', 'Plumbing'),
            ('chk_trade_fp',    'Fire Protection'),
            ('chk_trade_tech',  'Technology'),
        ):
            chk = getattr(self, chk_name, None)
            if chk is not None and chk.IsChecked:
                wanted.append(trade)
        # If all five are checked, no filter.
        if len(wanted) == 5:
            return None
        return set(wanted)

    def _filtered_role_map(self):
        """Role map filtered to only the link titles currently checked-in.

        Run Clash Test's per-run inclusion overrides the saved role map for
        this run only. Unchecked links effectively become 'ignore' for the
        duration of the run.
        """
        enabled = set(self._enabled_link_titles())
        out = {}
        for title, role in self._role_map.items():
            out[title] = role if title in enabled else linked.ROLE_IGNORE
        # Also include enabled links not in the saved map (defensive)
        for title, role, chk in self._run_link_rows:
            if chk.IsChecked and role != linked.ROLE_IGNORE and title not in out:
                out[title] = role
        return out

    def _on_select_all(self, sender, args):
        for t in self._tests:
            t.Selected = True
        self.dg_tests.Items.Refresh()

    def _on_select_none(self, sender, args):
        for t in self._tests:
            t.Selected = False
        self.dg_tests.Items.Refresh()

    def _on_test_checkbox_click(self, sender, args):
        clicked = args.OriginalSource
        if not isinstance(clicked, CheckBox):
            return
        new_state = bool(clicked.IsChecked)
        clicked_row = clicked.DataContext
        selected = list(self.dg_tests.SelectedItems)
        if clicked_row not in selected or len(selected) <= 1:
            return
        for row in selected:
            row.Selected = new_state
        self.dg_tests.Items.Refresh()
        try:
            self.dg_tests.SelectedItems.Clear()
            for row in selected:
                self.dg_tests.SelectedItems.Add(row)
        except Exception:
            pass

    # --- Run flow (real detection now) ----------------------------------

    def _on_run(self, sender, args):
        n_selected = self._selected_count()
        if n_selected == 0:
            dbhms_ui.info('No tests selected. Check the Run column for at least one test.',
                        title='Nothing to run')
            return

        # Hard prerequisites
        if not self._real_data:
            dbhms_ui.info(
                "Can't run - no saved test library yet.\n\n"
                "Open Test Library (Clash Detection panel on the dbHMS Tools tab) once. It will auto-seed "
                "the firm-default tests from the shipped defaults file.",
                title='Setup needed',
            )
            return

        try:
            from pyrevit import revit
            doc = revit.doc
        except Exception:
            doc = None
        if doc is None:
            dbhms_ui.info('No active Revit document.', title='No project')
            return

        if not self._project_hash:
            dbhms_ui.info(
                "The project has no central-model path yet. Save the project, "
                "then come back and run.",
                title='Project not saved',
            )
            return

        if not config.shared_root():
            dbhms_ui.info(
                "No shared folder is set. Open Settings and pick one first.",
                title='Setup needed',
            )
            return

        selected_tests = self._selected_test_dicts()
        n_links = len([1 for _t, role, chk in self._run_link_rows
                       if chk.IsChecked and role != linked.ROLE_IGNORE])

        # Author resolution for history entries
        try:
            from pyrevit import revit
            uiapp = revit.uiapp
            author = users.current_user(uiapp)
        except Exception:
            author = 'unknown'

        # Lock UI + show progress
        self.btn_run.IsEnabled = False
        self.btn_close.IsEnabled = False
        self.pb_run.Visibility = Visibility.Visible
        self.txt_status.Text = (
            'Running {} test(s) against host + {} linked model(s)... '
            'UI may pause briefly.'
        ).format(len(selected_tests), n_links)
        self.brd_result.Visibility = Visibility.Collapsed
        _flush_ui()

        # Open the output panel so the user can see the per-test diagnostics.
        # This is critical for understanding 0-clash results: the log will
        # show whether elements were collected at all, vs collected but
        # no intersections found, vs error.
        output.print_md(
            "## Clash detection run\n"
            "Project: `{}` (hash `{}`)  \n"
            "Active links included: **{}**".format(
                doc.PathName or '<unsaved>', self._project_hash, n_links)
        )

        # Pre-flight: census the host doc + each loaded link doc, so we
        # can immediately tell whether the elements the user expects are
        # actually present in the docs we're querying.
        _log_doc_census(doc, label='HOST', log=output.print_md)
        try:
            for inst in linked.find_link_instances(doc):
                link_doc = inst.GetLinkDocument()
                if link_doc is not None:
                    _log_doc_census(link_doc,
                                    label='LINK `{}`'.format(link_doc.Title),
                                    log=output.print_md)
        except Exception as ex:
            output.print_md("Census of links failed: `{}`".format(ex))
        output.print_md("---")

        try:
            run_role_map = self._filtered_role_map()
            trade_filter = self._collect_trade_filter()
            if trade_filter is not None:
                output.print_md(
                    "**Trade filter active:** {}".format(", ".join(sorted(trade_filter)))
                )
            raw_clashes = []
            error_count = 0
            for test_dict in selected_tests:
                try:
                    raw_clashes.extend(
                        runner.run_test(doc, test_dict, run_role_map,
                                        log=output.print_md,
                                        trade_filter=trade_filter)
                    )
                except Exception as ex:
                    # One bad test shouldn't kill the whole run, but log it
                    # loudly so the user can tell us what broke.
                    error_count += 1
                    output.print_md(
                        "**ERROR in test '{}':** `{}`".format(
                            test_dict.get('name', '?'), ex)
                    )
                    output.print_md(
                        "```\n{}\n```".format(traceback.format_exc())
                    )
            output.print_md(
                "\n**Detection finished. Raw clashes: {}** (errors during run: {})".format(
                    len(raw_clashes), error_count)
            )

            # Dedupe across tests: drop soft clashes whose pair already
            # has a hard clash in this run. The same physical contact
            # otherwise produces both a hard hit and a soft "near miss"
            # row in the Browser, which is duplicate noise.
            if raw_clashes:
                raw_clashes, dropped = dedupe.drop_soft_overlapping_hard(raw_clashes)
                if dropped:
                    output.print_md(
                        "**Deduped {} soft clash(es)** (same pair already detected as hard).".format(dropped)
                    )
        except Exception as ex:
            self._restore_ui()
            dbhms_ui.info(
                'Detection failed:\n\n{}\n\n{}'.format(ex, traceback.format_exc()),
                title='Detection error',
            )
            return

        # Merge with existing
        try:
            existing_data = persistence.read_clashes(self._project_hash)
            old_clashes = existing_data.get('clashes') or []
            run_iso = _now_iso()
            merged_clashes, summary = merge.merge_runs(
                old_clashes, raw_clashes, run_iso=run_iso, author=author,
            )
            new_data = {
                'schema_version': existing_data.get('schema_version', 1),
                'project_hash':   self._project_hash,
                'last_run_at':    run_iso,
                'tests_run':      [t.get('id') for t in selected_tests],
                'clashes':        merged_clashes,
            }
            persistence.write_clashes(self._project_hash, new_data)
        except Exception as ex:
            self._restore_ui()
            dbhms_ui.info(
                'Detection finished but saving the result failed:\n\n{}\n\n{}'.format(
                    ex, traceback.format_exc()),
                title='Save error',
            )
            return

        # Auto-generate viewpoints for any new clashes (and for any
        # existing clashes that don't already have one — catches up older
        # data that was detected before this iteration shipped). Done
        # AFTER the persist above so even if viewpoint generation fails,
        # the clash data itself is safely on disk.
        try:
            from pyrevit import revit
            from clash_view import viewpoint as vp_module
            generated = vp_module.generate_for_all(
                revit.uidoc, merged_clashes, run_role_map,
                self._project_hash, captured_by=author,
                log=output.print_md, only_missing=True,
            )
            if generated > 0:
                # Re-persist now that viewpoints[] entries have been
                # added to the clash dicts in-place.
                new_data['clashes'] = merged_clashes
                persistence.write_clashes(self._project_hash, new_data)
        except Exception as ex:
            output.print_md(
                "**Viewpoint generation failed** (clashes are saved; "
                "thumbnails will populate on first Show in 3D): `{}`".format(ex)
            )

        # Show result
        self._restore_ui()
        self._show_results(summary, len(merged_clashes))

        # Update each row's last-run column
        for t in self._tests:
            if t.Selected:
                t.LastRun = 'just now'
        self.dg_tests.Items.Refresh()

    def _restore_ui(self):
        self.pb_run.Visibility = Visibility.Collapsed
        self.btn_run.IsEnabled = True
        self.btn_close.IsEnabled = True

    def _show_results(self, summary, total_clashes):
        new = summary.get('new', 0)
        persisting = summary.get('persisting', 0)
        auto_resolved = summary.get('auto_resolved', 0)
        reopened = summary.get('reopened', 0)
        active_total = new + persisting

        self.txt_status.Text = (
            'Run complete. {} active clashes ({} new, {} persisting, '
            '{} auto-resolved, {} reopened).'
        ).format(active_total, new, persisting, auto_resolved, reopened)

        lines = [
            "Detection finished. Results saved to clashes.json.",
            "",
            "  - {} new clash(es)".format(new),
            "  - {} persisting clash(es) (kept their comments / status)".format(persisting),
            "  - {} auto-resolved (no longer firing)".format(auto_resolved),
            "  - {} reopened (was Resolved, came back)".format(reopened),
            "",
            "Total in database: {}".format(total_clashes),
            "Open the Clash Browser to review them.",
        ]
        self.txt_result_summary.Text = "\n".join(lines)
        self.brd_result.Visibility = Visibility.Visible

    # --- Open Browser button --------------------------------------------

    def _on_open_browser(self, sender, args):
        """Close this form and launch the Clash Browser pushbutton's script.

        We resolve the sibling pushbutton by relative path and execfile it
        with a globals dict that inherits this script's pyRevit injection
        (so `from pyrevit import ...` works inside the launched script).
        """
        browser_script = os.path.abspath(os.path.join(
            SCRIPT_DIR, '..', 'Clash Browser.pushbutton', 'script.py',
        ))
        if not os.path.isfile(browser_script):
            dbhms_ui.info(
                "Clash Browser script not found at:\n\n{}\n\n"
                "Close this window and click the Clash Browser button in the "
                "toolbar instead.".format(browser_script),
                title='Could not open Browser',
            )
            return
        self.Close()
        try:
            ns = dict(globals())
            ns['__file__'] = browser_script
            ns['__name__'] = '__main__'
            execfile(browser_script, ns)
        except Exception as ex:
            dbhms_ui.info(
                "Couldn't launch Clash Browser:\n\n{}\n\n"
                "Click the Clash Browser button in the toolbar instead.".format(ex),
                title='Open failed',
            )

    def _on_open_settings(self, sender, args):
        """Close this Run Clash Test form, then auto-launch the
        Settings toolbar button via Revit's ribbon. Same AdWindows
        approach as Browser → Walkthrough Here.
        """
        launched = self._post_settings_command()
        self.Close()
        if not launched:
            dbhms_ui.info(
                "Click the Settings button on the dbHMS Clash Detection "
                "toolbar to manage role mapping.",
                title='Settings',
            )

    @staticmethod
    def _post_settings_command():
        """Walk the Revit ribbon and click our **Clash Detection >
        Settings** button. The text-only match in the original was too
        greedy — pyRevit's own Settings button (Core Settings /
        Environment Variables / etc.) shares the text "Settings" and
        was being invoked instead, opening pyRevit's settings dialog
        rather than our Clash Detection settings.

        Fix: scope the search to the tab whose title contains "Clash
        Detection". Skip every other tab — pyRevit, dbHMS Tools,
        anything else — so we can only possibly land on the right
        button.
        """
        try:
            import clr
            clr.AddReference("AdWindows")
            from Autodesk.Windows import ComponentManager
        except Exception:
            return False
        try:
            ribbon = ComponentManager.Ribbon
        except Exception:
            return False
        if ribbon is None:
            return False
        try:
            for tab in ribbon.Tabs:
                try:
                    for panel in tab.Panels:
                        # Scope to the Clash Detection PANEL by name
                        # (Iter 16 — the panel lives inside dbHMS
                        # Tools tab now, so a tab-title filter would
                        # miss it). Panel-level scoping ALSO keeps
                        # us out of pyRevit's own "Settings" button,
                        # which lives in a different panel.
                        try:
                            source = panel.Source
                            if source is None:
                                continue
                            panel_title = str(getattr(source, "Title", "") or "")
                        except Exception:
                            continue
                        if "Clash Detection" not in panel_title:
                            continue
                        try:
                            for item in source.Items:
                                text = None
                                for attr in ("Text", "AutomationName",
                                             "Cookie", "Description"):
                                    try:
                                        v = getattr(item, attr, None)
                                        if v:
                                            text = str(v)
                                            break
                                    except Exception:
                                        continue
                                if not text:
                                    continue
                                normalized = text.replace("\n", "") \
                                                  .replace("\r", "") \
                                                  .replace("-", "") \
                                                  .replace(" ", "") \
                                                  .lower()
                                if normalized == "settings":
                                    handler = getattr(item, "CommandHandler", None)
                                    if handler is None:
                                        continue
                                    try:
                                        handler.Execute(item)
                                        return True
                                    except Exception:
                                        try:
                                            handler.Execute(None)
                                            return True
                                        except Exception:
                                            continue
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _on_close(self, sender, args):
        self.Close()


with dbhms_telemetry.session(__title__, script_path=__file__):
    RunClashTestForm().ShowDialog()
