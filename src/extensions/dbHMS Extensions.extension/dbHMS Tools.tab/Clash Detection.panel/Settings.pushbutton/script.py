# -*- coding: utf-8 -*-
"""Settings - per-machine and per-project configuration.

Per-machine fields (shared folder, display name, warn threshold) save to
%APPDATA%\\dbHMS_clash\\config.json.

Per-project fields (project display name, disciplines roster, linked-model
role mapping) save to <shared>/<project-hash>/project.json. The link-mapping
rows are built dynamically from the live RevitLinkInstance enumeration so
they always reflect what's loaded right now.

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
"""

__title__  = 'Settings'
__author__ = 'Nathaniel'
__doc__    = 'Configure the shared clash-data folder, link role mapping, and per-project settings.'

import datetime
import os
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")

from System.Windows import (
    Thickness, GridLength, GridUnitType, HorizontalAlignment, VerticalAlignment,
    FontStyles,
)
from System.Windows.Controls import (
    Grid, ColumnDefinition, ComboBox, ComboBoxItem, TextBlock,
)
from System.Windows.Forms import FolderBrowserDialog, DialogResult
from System.Windows.Media import SolidColorBrush, Color, FontFamily

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_core import config, persistence, project
from clash_detect import linked


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'SettingsForm.xaml')


def _brush(hex_str):
    h = hex_str.lstrip('#')
    return SolidColorBrush(Color.FromRgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))


class SettingsForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._project_hash = None
        self._link_rows = []  # list of (title, ComboBox) tuples for save-time collection

        # Per-machine config: load real values
        cfg = config.load()
        self.txt_shared_folder.Text  = cfg.get('shared_root') or ''
        self.txt_display_name.Text   = cfg.get('user_display_name') or ''
        self.txt_warn_threshold.Text = str(cfg.get('warn_threshold') or 2000)

        # Per-project: enumerate live links + load saved meta
        self._populate_project_section()

        if config.is_first_run():
            self.txt_status.Text = (
                'First-run setup: pick a shared folder for clash data, then click Save Settings.'
            )

        # Wire events
        self.btn_close.Click               += self._on_close
        self.btn_save.Click                += self._on_save
        self.btn_browse_shared.Click       += self._on_browse_shared
        self.btn_open_shared.Click         += self._on_open_shared
        self.btn_open_project_folder.Click += self._on_open_project_folder
        self.btn_open_readme.Click         += self._on_open_readme

    # --- Per-project section --------------------------------------------

    def _populate_project_section(self):
        """Resolve project hash, load project meta if any, render link rows + disciplines."""
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
            self.txt_project_name.Text = project.display_name_for(doc)
            self._populate_link_rows_empty(
                '(project must be saved before per-project settings can be stored)'
            )
            return

        self._project_hash = ph
        # Default the display-name field to the doc's filename
        self.txt_project_name.Text = project.display_name_for(doc)

        # Try to load saved meta. Falls through to mockup-empty if no shared folder.
        role_map = {}
        try:
            meta = persistence.read_project_meta(ph)
            saved_name = meta.get('display_name')
            if saved_name:
                self.txt_project_name.Text = saved_name
            saved_disciplines = meta.get('disciplines') or []
            if saved_disciplines:
                self._set_disciplines(saved_disciplines)
            role_map = meta.get('link_role_map') or {}
        except persistence.SharedFolderNotConfigured:
            pass  # Will save fresh once Settings is set up
        except Exception:
            pass  # Best-effort: missing/corrupt meta won't crash the form

        self._populate_link_rows(doc, role_map)

    def _set_disciplines(self, enabled_list):
        enabled_set = set(enabled_list)
        for child in self.wp_disciplines.Children:
            if hasattr(child, 'IsChecked') and hasattr(child, 'Content'):
                child.IsChecked = (str(child.Content) in enabled_set)

    def _collect_disciplines(self):
        out = []
        for child in self.wp_disciplines.Children:
            if hasattr(child, 'IsChecked') and child.IsChecked and hasattr(child, 'Content'):
                out.append(str(child.Content))
        return out

    def _populate_link_rows(self, doc, role_map):
        self.sp_link_map_rows.Children.Clear()
        self._link_rows = []
        try:
            view = linked.merged_link_view(doc, role_map)
        except Exception:
            self._populate_link_rows_empty('(unable to enumerate linked models)')
            return
        if not view:
            self._populate_link_rows_empty('(no linked models loaded in this project)')
            return
        for entry in view:
            self.sp_link_map_rows.Children.Add(
                self._build_role_row(entry['title'], entry['role'])
            )

    def _populate_link_rows_empty(self, message):
        self.sp_link_map_rows.Children.Clear()
        self._link_rows = []
        empty = TextBlock()
        empty.Text = message
        empty.FontSize = 11
        empty.FontStyle = FontStyles.Italic
        empty.Foreground = _brush('#A0AEC0')
        empty.HorizontalAlignment = HorizontalAlignment.Center
        empty.Margin = Thickness(0, 8, 0, 8)
        self.sp_link_map_rows.Children.Add(empty)

    def _build_role_row(self, title, current_role):
        """Build a Grid row: filename label + role-select ComboBox."""
        grid = Grid()
        grid.Margin = Thickness(0, 0, 0, 6)

        cd1 = ColumnDefinition()
        cd1.Width = GridLength(1, GridUnitType.Star)
        cd2 = ColumnDefinition()
        cd2.Width = GridLength(160)
        grid.ColumnDefinitions.Add(cd1)
        grid.ColumnDefinitions.Add(cd2)

        title_tb = TextBlock()
        title_tb.Text = title
        title_tb.FontFamily = FontFamily("Consolas")
        title_tb.FontSize = 11
        if current_role == linked.ROLE_IGNORE:
            title_tb.Foreground = _brush('#A0AEC0')
        else:
            title_tb.Foreground = _brush('#2D3748')
        title_tb.VerticalAlignment = VerticalAlignment.Center
        Grid.SetColumn(title_tb, 0)
        grid.Children.Add(title_tb)

        combo = ComboBox()
        combo.Padding = Thickness(6, 4, 6, 4)
        combo.Height = 26
        for value in (linked.ROLE_ARCHITECTURAL, linked.ROLE_STRUCTURAL, linked.ROLE_IGNORE):
            item = ComboBoxItem()
            item.Content = linked.IGNORE_DISPLAY if value == linked.ROLE_IGNORE else value
            item.Tag = value
            if value == current_role:
                item.IsSelected = True
            combo.Items.Add(item)
        Grid.SetColumn(combo, 1)
        grid.Children.Add(combo)

        self._link_rows.append((title, combo))
        return grid

    def _collect_link_role_map(self):
        out = {}
        for title, combo in self._link_rows:
            item = combo.SelectedItem
            if item is None:
                continue
            role = getattr(item, 'Tag', None)
            if role:
                out[title] = role
        return out

    # --- Save -----------------------------------------------------------

    def _on_save(self, sender, args):
        threshold_raw = self.txt_warn_threshold.Text.strip()
        try:
            threshold = int(threshold_raw)
            if threshold < 1:
                raise ValueError()
        except (ValueError, TypeError):
            dbhms_ui.info(
                'Warn threshold must be a positive whole number (e.g. 2000).\n\n'
                'Got: "{}"'.format(threshold_raw),
                title='Invalid value',
            )
            return

        # Per-machine
        cfg = config.load()
        cfg['shared_root']       = self.txt_shared_folder.Text.strip() or None
        cfg['user_display_name'] = self.txt_display_name.Text.strip() or None
        cfg['warn_threshold']    = threshold
        try:
            config.save(cfg)
        except Exception as ex:
            dbhms_ui.info(
                'Could not save per-machine config:\n\n{}\n\n{}'.format(
                    ex, traceback.format_exc()),
                title='Save failed',
            )
            return

        # Per-project (only if we have a hash AND a shared folder)
        per_project_saved = False
        if self._project_hash and cfg['shared_root']:
            try:
                meta = persistence.read_project_meta(self._project_hash)
                meta['project_hash']   = self._project_hash
                meta['display_name']   = self.txt_project_name.Text.strip() or None
                meta['disciplines']    = self._collect_disciplines()
                meta['link_role_map']  = self._collect_link_role_map()
                meta['warn_threshold'] = threshold
                persistence.write_project_meta(self._project_hash, meta)
                per_project_saved = True
            except Exception as ex:
                dbhms_ui.info(
                    'Per-machine settings saved, but per-project save failed:\n\n{}'.format(ex),
                    title='Partial save',
                )
                return

        # Quiet success: update the footer status bar instead of popping an alert.
        # Errors/partial-saves above DO still pop, since those need attention.
        now = datetime.datetime.now().strftime('%I:%M %p').lstrip('0')
        if per_project_saved:
            self.txt_status.Text = (
                'Saved at {} - per-machine + per-project settings updated.'.format(now)
            )
        elif self._project_hash and not cfg['shared_root']:
            self.txt_status.Text = (
                "Saved at {} - per-machine only (no shared folder set, so per-project "
                "settings weren't written).".format(now)
            )
        else:
            self.txt_status.Text = 'Saved at {}.'.format(now)

    # --- Browse / open helpers -----------------------------------------

    def _on_browse_shared(self, sender, args):
        dlg = FolderBrowserDialog()
        dlg.Description = (
            "Pick the firm's shared folder for clash data. "
            "Engineers will see clash reports here."
        )
        current = self.txt_shared_folder.Text.strip()
        if current and os.path.isdir(current):
            dlg.SelectedPath = current
        if dlg.ShowDialog() == DialogResult.OK:
            self.txt_shared_folder.Text = dlg.SelectedPath

    def _on_open_shared(self, sender, args):
        path = self.txt_shared_folder.Text.strip()
        if not path:
            dbhms_ui.info('No shared folder is set yet. Use Browse to pick one.',
                        title='Nothing to open')
            return
        if not os.path.isdir(path):
            dbhms_ui.info(
                "The shared folder doesn't exist on disk yet:\n\n{}\n\n"
                'Pick a different folder or create that one first.'.format(path),
                title='Folder missing',
            )
            return
        try:
            os.startfile(path)
        except Exception as ex:
            dbhms_ui.info("Couldn't open folder:\n\n{}".format(ex), title='Open failed')

    def _on_open_project_folder(self, sender, args):
        if not self._project_hash:
            dbhms_ui.info(
                "No active project, or the project hasn't been saved yet.",
                title='No project',
            )
            return
        try:
            folder = persistence.project_dir(self._project_hash)
        except persistence.SharedFolderNotConfigured as ex:
            dbhms_ui.info(str(ex), title='Shared folder not set')
            return
        try:
            os.startfile(folder)
        except Exception as ex:
            dbhms_ui.info("Couldn't open project folder:\n\n{}".format(ex),
                        title='Open failed')

    def _on_open_readme(self, sender, args):
        readme = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', 'README.md'))
        if os.path.isfile(readme):
            try:
                os.startfile(readme)
            except Exception as ex:
                dbhms_ui.info("Couldn't open README:\n\n{}".format(ex), title='Open failed')
        else:
            dbhms_ui.info('README not found at:\n\n{}'.format(readme), title='README missing')

    def _on_close(self, sender, args):
        self.Close()


with dbhms_telemetry.session(__title__, script_path=__file__):
    SettingsForm().ShowDialog()
