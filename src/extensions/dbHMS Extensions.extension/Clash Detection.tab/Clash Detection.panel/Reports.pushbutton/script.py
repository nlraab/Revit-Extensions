# -*- coding: utf-8 -*-
"""Reports - export the active project's clashes as a BCF 2.1 file.

UI MOCKUP. Form renders with sample filters and output options. Clicking
Export pops a "coming soon" alert; nothing is actually written.

See Clash Detection.tab/README.md for the architecture.
"""

__title__  = 'Reports'
__author__ = 'Nathaniel'
__doc__    = "Export the active project's clashes as a BCF 2.1 file. (UI mockup.)"

import os

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from pyrevit import forms


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'ReportsForm.xaml')


class ReportsForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self.btn_close.Click         += self._on_close
        self.btn_export.Click        += self._on_export
        self.btn_browse_folder.Click += self._on_browse

    def _on_export(self, sender, args):
        forms.alert(
            "BCF export isn't wired up yet.\n\n"
            "When implemented, this will:\n"
            "  1. Read the project's clashes.json filtered by your selections above\n"
            "  2. Build a BCF 2.1 zip with project.bcfp + per-topic markup.bcf, "
            "viewpoint.bcfv, and snapshot.png files\n"
            "  3. Write it to the output folder using the filename template\n"
            "  4. Open the folder in Explorer for you to share with consultants.",
            title='Coming Soon - Export BCF',
        )

    def _on_browse(self, sender, args):
        forms.alert(
            "Folder picker isn't wired up yet.\n\n"
            "It'll use the standard Windows folder picker (FolderBrowserDialog) "
            "to let you pick where the BCF goes; defaults to the project's "
            "shared clash-data folder.",
            title='Coming Soon - Browse Folder',
        )

    def _on_close(self, sender, args):
        self.Close()


ReportsForm().ShowDialog()
