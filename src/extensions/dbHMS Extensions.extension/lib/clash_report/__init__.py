# -*- coding: utf-8 -*-
"""clash_report - export clash data to BCF (and, later, PDF / HTML summaries).

Submodules:
    bcf - BCF 2.1 file builder (zip of project.bcfp + per-topic folders)

We target BCF 2.1, not 3.0, on purpose: 2.1 is the version Navisworks,
Solibri, BIMcollab, Newforma, and most other coordination tools read
without trouble. 3.0 adds nice-to-haves (extensions, fields) but isn't
universally supported as of this writing.
"""
