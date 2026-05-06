# -*- coding: utf-8 -*-
"""Walkthrough launcher - pick mode + filter, then enter the full-screen walkthrough.

UI MOCKUP. The launcher dialog renders. "Enter Walkthrough" pops a "coming
soon" alert; the full-screen WPF window with XInput polling, clash markers,
and the Free-Fly camera will be built in its own iteration.

See Clash Detection.tab/README.md for the architecture.
"""

__title__  = 'Walk-\nthrough'
__author__ = 'Nathaniel'
__doc__    = ('Full-screen 3D walkthrough with clash markers; controller, mouse, and keyboard '
              'support. (UI mockup of the launcher.)')

import os

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from pyrevit import forms


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'WalkthroughForm.xaml')


class WalkthroughForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)
        self.btn_close.Click += self._on_close
        self.btn_enter.Click += self._on_enter

    def _on_enter(self, sender, args):
        mode = 'Clash Navigator' if self.rb_navigator.IsChecked else 'Free-Fly'
        forms.alert(
            "The full-screen walkthrough isn't wired up yet.\n\n"
            "Selected mode: {}\n\n"
            "When implemented, this will:\n"
            "  - Maximize a dedicated 'Clash Navigator' 3D view\n"
            "  - Hide Revit's project browser, properties palette, and ribbon\n"
            "  - Apply Consistent Colors with shadows and ambient occlusion\n"
            "  - Render colored sphere markers at every clash midpoint matching your filters\n"
            "  - Start polling Xbox controller input via XInput at 60 Hz\n"
            "  - Listen for keyboard/mouse input as a fallback\n\n"
            "Press Esc (or Back on the controller) to exit.".format(mode),
            title='Coming Soon - Enter Walkthrough',
        )

    def _on_close(self, sender, args):
        self.Close()


WalkthroughForm().ShowDialog()
