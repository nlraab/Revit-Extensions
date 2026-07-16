# -*- coding: utf-8 -*-
"""dbhms_ui — shared UI helpers for the dbHMS Extensions toolkit.

A small set of cross-tool UI utilities that give every dbHMS tool a
consistent look. The primary export is `dialogs.info(message, title=...)`
— a friendly, dbHMS-branded replacement for `pyrevit.forms.alert(...)`
that drops the Windows-style yellow warning triangle and adopts the
firm's dark-slate header bar instead.

Why a shared lib (deviation from the "duplicate per tool" rule):
    The repo's CLAUDE.md says tools generally don't share helpers, on
    purpose — each pyRevit pushbutton ships standalone. This module is
    the second documented exception (alongside `clash_*` for clash
    detection): all dbHMS tools should look identical when they put up
    a popup, and duplicating the WPF-Window + XAML loader across every
    pushbutton would make a "make all popups blue" tweak a 30-file
    edit. Lives in `lib/` (auto-added to `sys.path` by pyRevit) and is
    scoped under the `dbhms_ui` namespace so other tools' scripts can
    ignore it. See CLAUDE.md for the full rule.

Public API:
    dbhms_ui.info(message, title="dbHMS", kind="info")
        — friendly OK-only dialog with the dbHMS slate header bar.
          Use this in place of `forms.alert(message, title=title)` for
          informational popups (export complete, X was queued, etc.).
    dbhms_ui.error(message, title="dbHMS")
        — same dialog with a red X glyph. Use for failures/exceptions so
          a failed action never shows the friendly green check.
    dbhms_ui.warn(message, title="dbHMS")
        — same dialog with an amber ! glyph, for "can't do that / not ready".
          Yes/no confirmations still use `forms.alert(..., yes=True, no=True)`.

Lazy WPF imports — this module's top level is pure Python so the test
suite parses it under CPython 3 without needing PresentationFramework.
"""

from .dialogs import info, error, warn  # noqa: F401
