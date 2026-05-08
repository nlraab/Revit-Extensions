# -*- coding: utf-8 -*-
"""Friendly information dialog — drop-in replacement for
`pyrevit.forms.alert(message, title=title)`.

Why this exists: pyRevit's `forms.alert` uses the Windows MessageBox,
which displays a yellow warning triangle even for purely informational
messages ("Export complete: 13 clashes saved to..."). That looks like
something failed. The firm's preference is for popups to look friendly
and on-brand — slate dbHMS header bar, blue accent, simple OK button —
so users don't get the failure-vibes from a successful action.

Public API:
    info(message, title="dbHMS")
        — show a modal info dialog. Returns when the user clicks OK
          (or presses Enter / Esc). No return value.

Implementation:
    Loads `InfoDialog.xaml` as a pyRevit `forms.WPFWindow` so the
    window cleanly inherits Revit-window ownership (renders in front
    of Revit's main window, modal-blocks like other pyRevit dialogs).
    XAML is in a sibling file rather than inlined as a string so the
    designer-friendly markup stays readable.

    All Revit/WPF imports are deferred inside the function so this
    module parses cleanly under CPython 3 for the test suite.
"""

import os


_INFO_XAML = os.path.join(os.path.dirname(__file__), "InfoDialog.xaml")


def info(message, title="dbHMS"):
    """Show a friendly modal info dialog.

    Args:
        message - the message to display. Strings only — caller should
                  format any data into the string.
        title   - the title shown in the slate header bar. Defaults to
                  "dbHMS".

    Returns None. Always blocks until dismissed.
    """
    # Lazy import — keeps this module CPython-parseable for tests.
    from pyrevit import forms

    class _InfoDialog(forms.WPFWindow):
        def __init__(self, msg, ttl):
            forms.WPFWindow.__init__(self, _INFO_XAML)
            self.txt_title.Text = ttl or "dbHMS"
            self.txt_message.Text = msg or ""
            self.btn_ok.Click += self._on_ok
            # Esc dismisses too — common WPF expectation.
            from System.Windows import Input
            self.PreviewKeyDown += self._on_keydown

        def _on_ok(self, sender, args):
            self.Close()

        def _on_keydown(self, sender, args):
            from System.Windows.Input import Key
            if args.Key == Key.Escape:
                self.Close()

    dialog = _InfoDialog(message, title)
    dialog.ShowDialog()
