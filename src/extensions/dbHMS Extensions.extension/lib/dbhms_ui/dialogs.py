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
    info(message, title="dbHMS", kind="info")
        — show a modal dialog. Returns when the user clicks OK (or presses
          Enter / Esc). No return value. `kind` picks the header glyph:
          "info"/"success" = green check, "error" = red X, "warn" = amber !.
          A FAILURE must never show the green check, so use error()/warn().
    error(message, title="dbHMS")  — info(..., kind="error"): red X.
    warn(message, title="dbHMS")   — info(..., kind="warn"): amber !.

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

# Header glyph + color per dialog kind. Success/info keep the friendly green
# check; error and warn get their own mark so a FAILURE never reads as "this
# worked". (u"..." literals stay valid under both IronPython 2.7 and CPython 3.)
_ICONS = {
    "info":    (u"✓", "#48BB78"),   # heavy check      - dbHMS green
    "success": (u"✓", "#48BB78"),
    "error":   (u"✕", "#F56565"),   # multiplication X - red
    "warn":    (u"⚠", "#ECC94B"),   # warning sign     - amber
}


def info(message, title="dbHMS", kind="info"):
    """Show a modal dbHMS dialog.

    Args:
        message - the message to display. Strings only — caller should
                  format any data into the string.
        title   - the title shown in the slate header bar. Defaults to
                  "dbHMS".
        kind    - "info"/"success" (green check, default), "error" (red X),
                  or "warn" (amber !). A failure must not use the default.

    Returns None. Always blocks until dismissed.
    """
    # Lazy import — keeps this module CPython-parseable for tests.
    from pyrevit import forms

    class _InfoDialog(forms.WPFWindow):
        def __init__(self, msg, ttl, knd):
            forms.WPFWindow.__init__(self, _INFO_XAML)
            self.txt_title.Text = ttl or "dbHMS"
            self.txt_message.Text = msg or ""
            self._apply_icon(knd)
            self.btn_ok.Click += self._on_ok
            # Esc dismisses too — common WPF expectation.
            from System.Windows import Input
            self.PreviewKeyDown += self._on_keydown

        def _apply_icon(self, knd):
            try:
                from System.Windows.Media import SolidColorBrush, Color
                glyph, hex6 = _ICONS.get(knd, _ICONS["info"])
                h = hex6.lstrip("#")
                self.txt_icon.Text = glyph
                self.txt_icon.Foreground = SolidColorBrush(Color.FromRgb(
                    int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
            except Exception:
                pass   # fall back to the XAML default glyph; never break a popup

        def _on_ok(self, sender, args):
            self.Close()

        def _on_keydown(self, sender, args):
            from System.Windows.Input import Key
            if args.Key == Key.Escape:
                self.Close()

    dialog = _InfoDialog(message, title, kind)
    dialog.ShowDialog()


def error(message, title="dbHMS"):
    """Show a failure dialog (red X). Use for exceptions and failed actions."""
    info(message, title, kind="error")


def warn(message, title="dbHMS"):
    """Show a caution dialog (amber !). Use for "can't do that yet" / not-ready."""
    info(message, title, kind="warn")
