# -*- coding: utf-8 -*-
"""High-level "show me this clash" entry point.

Used by Clash Browser, Walkthrough's clash navigator mode, and Reports
(when generating BCF viewpoints).
"""


def show_clash(uidoc, clash, isolate=True, fit_padding_feet=2.0):
    """Navigate the active 3D view to `clash`.

    Steps:
      1. Resolve element_a and element_b from clash refs (host or linked).
      2. Compute a combined bounding box around both elements + fit_padding.
      3. Switch to or create a dedicated "Clash Navigator" 3D view (see
         threed_view.get_or_create_navigator_view).
      4. Apply a section box equal to the combined bounding box.
      5. If isolate=True, temporarily isolate the two elements (or the
         host element + a categories filter for the linked one).
      6. Zoom the view to fit the section box.

    Returns the View used (so the caller can capture a viewpoint from it).
    """
    raise NotImplementedError


def clear_isolation(uidoc):
    """Restore the active view from the temporary isolation applied by show_clash."""
    raise NotImplementedError
