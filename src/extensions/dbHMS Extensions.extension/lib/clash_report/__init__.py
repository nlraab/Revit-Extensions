# -*- coding: utf-8 -*-
"""clash_report - export the clash database to shareable deliverables.

Submodules:
    report_model   - shared band/score/trade/pair accessors + the
                     coordination summary; the single source of truth the
                     other builders and the Reports tab all compute from.
    bcf            - BCF 2.1 file builder (the industry clash-exchange
                     format: Navisworks / ACC / Solibri / Revizto read it).
    excel_summary  - formatted .xlsx workbook, one row per clash.
    html           - interactive, print-clean HTML report (also the source
                     the host prints to PDF via WebView2).
    digest         - pre-meeting agenda handout (top issues only).

We target BCF 2.1, not 3.0, on purpose: 2.1 is the version Navisworks,
Solibri, BIMcollab, Newforma, and most other coordination tools read
without trouble. 3.0 adds nice-to-haves (extensions, fields) but isn't
universally supported as of this writing.
"""
