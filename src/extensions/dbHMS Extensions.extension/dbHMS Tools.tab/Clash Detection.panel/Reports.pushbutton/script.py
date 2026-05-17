# -*- coding: utf-8 -*-
"""Reports - export the active project's clashes as a BCF 2.1 file.

Reads the project's clashes.json, applies the user's filter selections,
calls clash_report.bcf.build_bcf_zip to produce a .bcfzip with one topic
per clash (markup.bcf + viewpoint.bcfv + snapshot.png), and writes it
to the chosen output folder. The clash_report module handles the actual
file-format work; this script is the form glue.

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
"""

__title__  = 'Reports'
__author__ = 'Nathaniel'
__doc__    = "Export the active project's clashes as a BCF 2.1 file for sharing with consultants."

import codecs
import os
import re
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
# System.Diagnostics.Process lives in the System assembly — without this
# AddReference, importing Process at module load time fails with
# "Cannot import name Process" before the Reports form even shows.
clr.AddReference("System")

from System.Diagnostics import Process

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_core import config, persistence, project
from clash_report import bcf, excel_summary
from clash_report import html as html_report


# Internal format keys.
_FORMAT_BCF   = 'bcf'
_FORMAT_XLSX  = 'xlsx'
_FORMAT_HTML  = 'html'

_FORMAT_EXTENSIONS = {
    _FORMAT_BCF:  '.bcfzip',
    _FORMAT_XLSX: '.xlsx',
    _FORMAT_HTML: '.html',
}


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'ReportsForm.xaml')


# Names of the trade checkboxes (their .Content) — used by the
# filename-template logic to detect "all trades checked" so it can
# render that as "All-Trades" rather than a long hyphenated list.
_TRADE_NAMES = ('Mechanical', 'Electrical', 'Plumbing', 'Fire Protection',
                'Technology', 'Architectural', 'Structural')


class ReportsForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)

        self._project_hash = None
        self._project_meta = {}
        self._clashes = []
        self._reports_dir = None  # default output dir; resolved on first paint

        self._resolve_project()
        self._load_clashes()
        self._populate_test_filter()
        self._set_default_output_folder()
        self._wire_filter_events()

        self.btn_close.Click         += self._on_close
        self.btn_export.Click        += self._on_export
        self.btn_browse_folder.Click += self._on_browse

        # Format change: swap the filename extension to match the new
        # format so the user doesn't accidentally save a CSV with a
        # .bcfzip extension.
        self.cmb_format.SelectionChanged += self._on_format_changed

        # Initial preview / status reflect the default filter state.
        self._update_preview()

    # --- Setup --------------------------------------------------------------

    def _resolve_project(self):
        try:
            from pyrevit import revit
            doc = revit.doc
            if doc is None:
                return
            ph = project.project_hash_for(doc)
            if not ph:
                return
            self._project_hash = ph
            self._project_meta = persistence.read_project_meta(ph) or {}
        except Exception:
            pass

    def _load_clashes(self):
        if not self._project_hash:
            self._clashes = []
            return
        try:
            data = persistence.read_clashes(self._project_hash)
            self._clashes = data.get('clashes') or []
        except Exception:
            self._clashes = []

    def _populate_test_filter(self):
        """Replace the XAML mockup test items with the actual loaded test names."""
        from System.Windows.Controls import ComboBoxItem
        unique_names = sorted({
            self._test_name_for(c) for c in self._clashes
        })
        self.cmb_test_filter.Items.Clear()
        all_item = ComboBoxItem()
        all_item.Content = "(All tests)"
        self.cmb_test_filter.Items.Add(all_item)
        for name in unique_names:
            if not name:
                continue
            item = ComboBoxItem()
            item.Content = name
            self.cmb_test_filter.Items.Add(item)
        self.cmb_test_filter.SelectedIndex = 0

    def _set_default_output_folder(self):
        """Default output folder = <shared>/<project-hash>/reports/.
        Falls back to user's Desktop if shared root isn't configured."""
        if self._project_hash:
            try:
                pdir = persistence.project_dir(self._project_hash)
                self._reports_dir = os.path.join(pdir, 'reports')
                self.txt_output_folder.Text = self._reports_dir
                return
            except Exception:
                pass
        # Fallback: user's Desktop.
        self._reports_dir = os.path.join(
            os.path.expanduser('~'), 'Desktop')
        self.txt_output_folder.Text = self._reports_dir

    def _wire_filter_events(self):
        """Hook every filter control so the preview count updates live."""
        for chk in self._iter_checkboxes(self.wp_trade_filter):
            chk.Checked   += self._on_filter_changed
            chk.Unchecked += self._on_filter_changed
        for chk in self._iter_checkboxes(self.wp_status_filter):
            chk.Checked   += self._on_filter_changed
            chk.Unchecked += self._on_filter_changed
        self.cmb_test_filter.SelectionChanged += self._on_filter_changed
        self.cmb_date_filter.SelectionChanged  += self._on_filter_changed

    @staticmethod
    def _iter_checkboxes(panel):
        """Yield CheckBox children of `panel`. Uses the logical tree
        (panel.Children) which is available immediately after XAML load
        — unlike the visual tree, which doesn't exist until the window
        renders."""
        from System.Windows.Controls import CheckBox
        for child in panel.Children:
            if isinstance(child, CheckBox):
                yield child

    # --- Filtering ----------------------------------------------------------

    def _on_filter_changed(self, sender, args):
        self._update_preview()

    def _build_filter_predicate(self):
        """Build a callable that takes a clash dict and returns True if it
        passes the current filter selections."""
        allowed_trades = self._checked_set(self.wp_trade_filter)
        allowed_statuses = self._checked_set(self.wp_status_filter)
        test_filter = self._combo_text(self.cmb_test_filter)
        date_filter = self._combo_text(self.cmb_date_filter)
        cutoff_iso = _date_filter_cutoff(date_filter)

        def predicate(clash):
            if allowed_trades is not None:
                if (clash.get('assignee') or '-') not in allowed_trades:
                    return False
            if allowed_statuses is not None:
                if (clash.get('status') or 'Open') not in allowed_statuses:
                    return False
            if test_filter and test_filter != "(All tests)":
                if self._test_name_for(clash) != test_filter:
                    return False
            if cutoff_iso:
                first_seen = clash.get('first_seen_run') or ''
                if first_seen < cutoff_iso:
                    return False
            return True
        return predicate

    def _checked_set(self, panel):
        """Return the set of CheckBox.Content values currently checked
        in `panel`. None if the panel has no checkboxes (defensive)."""
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

    def _combo_text(self, combo):
        item = combo.SelectedItem
        if item is None:
            return None
        return getattr(item, 'Content', None)

    def _test_name_for(self, clash):
        """Resolve a clash's test friendly name. Test names live in the
        global library + project overrides; for simplicity we just use
        the test_id directly when we don't have a lookup."""
        # If clash dicts already carry a test_name (added at run time),
        # use that. Otherwise fall back to the test_id.
        return clash.get('test_name') or clash.get('test_id') or '(unknown)'

    # --- Preview ------------------------------------------------------------

    def _update_preview(self):
        try:
            predicate = self._build_filter_predicate()
            count = sum(1 for c in self._clashes if predicate(c))
        except Exception:
            count = len(self._clashes)
        self.txt_preview.Text = '{} clash(es) will be exported.'.format(count)
        self.txt_status.Text = 'Ready - {} clash(es) match the current filters.'.format(count)

    # --- Browse / Export ----------------------------------------------------

    def _on_browse(self, sender, args):
        chosen = forms.pick_folder(
            title='Pick the output folder',
            owner=self,
        )
        if chosen:
            self.txt_output_folder.Text = chosen

    def _on_format_changed(self, sender, args):
        """Swap the filename's extension when the user picks a different
        format — saves them having to manually edit the textbox."""
        new_ext = _FORMAT_EXTENSIONS[self._selected_format()]
        current = (self.txt_filename.Text or '').strip()
        if not current:
            return
        # Strip any of our known extensions, replace with the new one.
        for known in _FORMAT_EXTENSIONS.values():
            if current.lower().endswith(known):
                current = current[:-len(known)]
                break
        else:
            # If the user hand-typed some other extension, only replace
            # things that actually look like one (last token after a dot).
            base, dot, _ = current.rpartition('.')
            if dot and len(base) > 0 and len(_) <= 8:
                current = base
        self.txt_filename.Text = current + new_ext

    def _selected_format(self):
        """Return the internal format key for the currently-selected
        cmb_format item. Defaults to BCF if unrecognized."""
        text = (self._combo_text(self.cmb_format) or '').lower()
        if 'html' in text:
            return _FORMAT_HTML
        if 'xlsx' in text or 'excel' in text:
            return _FORMAT_XLSX
        return _FORMAT_BCF

    def _on_export(self, sender, args):
        fmt = self._selected_format()
        title_label = {
            _FORMAT_BCF:  'Export BCF',
            _FORMAT_XLSX: 'Export Excel',
            _FORMAT_HTML: 'Export HTML',
        }[fmt]

        if not self._project_hash:
            dbhms_ui.info(
                "Couldn't determine the active project. Open Settings and "
                "confirm the active project is set up.",
                title=title_label,
            )
            return

        out_dir = (self.txt_output_folder.Text or '').strip()
        if not out_dir:
            dbhms_ui.info("Pick an output folder first.", title=title_label)
            return
        try:
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
        except Exception as ex:
            dbhms_ui.info("Couldn't create the output folder:\n\n{}".format(ex),
                        title=title_label)
            return

        # Resolve filename + extension. _fill_filename_template handles
        # the template tokens; then we make sure the extension matches
        # the selected format.
        filename = (self.txt_filename.Text or '').strip()
        if not filename:
            filename = 'clashes' + _FORMAT_EXTENSIONS[fmt]
        filename = self._fill_filename_template(filename)
        filename = _ensure_extension(filename, fmt)
        out_path = os.path.join(out_dir, filename)

        predicate = self._build_filter_predicate()

        try:
            if fmt == _FORMAT_BCF:
                viewpoints_dir = None
                try:
                    viewpoints_dir = persistence.viewpoints_dir(self._project_hash)
                except Exception:
                    pass
                written = bcf.build_bcf_zip(
                    project_meta=self._project_meta,
                    clashes=self._clashes,
                    viewpoints_dir=viewpoints_dir,
                    out_path=out_path,
                    filter_predicate=predicate,
                    project_name=self._project_meta.get('display_name'),
                )
            elif fmt == _FORMAT_XLSX:
                written = excel_summary.build_xlsx(
                    clashes=self._clashes,
                    out_path=out_path,
                    filter_predicate=predicate,
                )
            else:  # HTML
                viewpoints_dir = None
                try:
                    viewpoints_dir = persistence.viewpoints_dir(self._project_hash)
                except Exception:
                    pass
                # Build the test name lookup so each clash card can show
                # its test name (the clash dict only stores the test id).
                test_name_lookup = {}
                try:
                    library = persistence.read_global_test_library()
                    for t in library.get('tests') or []:
                        tid = t.get('id')
                        if tid:
                            test_name_lookup[tid] = t.get('name', '<unnamed>')
                except Exception:
                    pass
                # Resolve current user inline — Reports form doesn't
                # cache an _author like the Browser does. Best-effort.
                generated_by = None
                try:
                    from clash_core import users as _users
                    from pyrevit import revit as _revit
                    generated_by = _users.current_user(_revit.uiapp)
                except Exception:
                    pass
                written = html_report.build_html(
                    clashes=self._clashes,
                    out_path=out_path,
                    filter_predicate=predicate,
                    project_name=self._project_meta.get('display_name'),
                    viewpoints_dir=viewpoints_dir,
                    generated_by=generated_by,
                    test_name_lookup=test_name_lookup,
                )
        except Exception as ex:
            dbhms_ui.info(
                "Export failed:\n\n{}\n\n{}".format(ex, traceback.format_exc()),
                title=title_label + ' failed',
            )
            return

        self.txt_status.Text = 'Exported {} clash(es) to {}'.format(written, filename)

        # Show the file in Explorer so the user can grab it.
        try:
            Process.Start('explorer.exe', '/select,"{}"'.format(out_path))
        except Exception:
            pass

        dbhms_ui.info(
            "Exported {} clash(es) to:\n\n{}".format(written, out_path),
            title='Export complete',
        )

    def _fill_filename_template(self, template):
        """Replace {date}, {project}, {filter} tokens in the filename."""
        import datetime
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
        project_name = self._project_meta.get('display_name') or 'Project'
        # Sanitize project name for a filename
        project_name = re.sub(r'[\\/:*?"<>|]', '_', project_name)
        # Filter token = active trade list, joined with hyphens.
        trades = self._checked_set(self.wp_trade_filter)
        if trades is None or len(trades) == len(_TRADE_NAMES):
            filter_str = 'All-Trades'
        elif not trades:
            filter_str = 'No-Trades'
        else:
            filter_str = '-'.join(sorted(trades)).replace(' ', '')
        out = template
        out = out.replace('{date}', date_str)
        out = out.replace('{project}', project_name)
        out = out.replace('{filter}', filter_str)
        return out

    def _on_close(self, sender, args):
        self.Close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_extension(filename, fmt):
    """Force `filename` to end with the right extension for `fmt`. If it
    already has the right extension we leave it alone; if it has a
    different known extension we swap it; otherwise we append."""
    target = _FORMAT_EXTENSIONS[fmt]
    if filename.lower().endswith(target):
        return filename
    # Strip any other known extension first so we don't end up with
    # "thing.bcfzip.csv".
    for ext in _FORMAT_EXTENSIONS.values():
        if filename.lower().endswith(ext):
            filename = filename[:-len(ext)]
            break
    return filename + target


def _date_filter_cutoff(date_filter_text):
    """Map the date-filter dropdown text to an ISO cutoff. Returns the
    earliest first_seen_run that should pass the filter, or None for
    'All time'.

    Uses naive UTC (matches how clashes.json stores ISO dates)."""
    if not date_filter_text or date_filter_text == 'All time':
        return None
    import datetime
    now = datetime.datetime.utcnow()
    if 'last 7' in date_filter_text:
        cutoff = now - datetime.timedelta(days=7)
    elif 'last 30' in date_filter_text:
        cutoff = now - datetime.timedelta(days=30)
    else:
        return None  # Custom range not yet supported
    return cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')


with dbhms_telemetry.session(__title__, script_path=__file__):
    ReportsForm().ShowDialog()
