# -*- coding: utf-8 -*-
"""Single source of truth for the federation key that joins a clash record to a
glTF node to a Revit element.

BOTH the exporter (`clash_export`) and the clash detector (`clash_detect`) build
the key through here, so a linked-element clash always maps to the exact same
geometry the exporter stamped. A raw Revit UniqueId is unique only WITHIN one
document, so across a federation (host + links) it collides; namespacing a linked
element by its link instance makes the key globally unique.

CPython-safe: no Revit import at module scope, so the test suite can parse and
unit-test the pure string builders. The one Revit-touching helper lazy-imports
inside the function (same pattern as revit_geometry.py)."""


def link_ns_from_origin(doc_name_str, origin_x, origin_y, origin_z):
    """Per-link-instance namespace from a link's document name + its placement
    origin (Revit feet). Pure / testable. MUST stay byte-for-byte identical to
    what the exporter stamps: ``name@x,y,z`` at 3 decimals, comma-separated, no
    spaces."""
    name = doc_name_str or "link"
    return "{0}@{1:.3f},{2:.3f},{3:.3f}".format(name, origin_x, origin_y, origin_z)


def fed_key(unique_id, link_ns=None):
    """The globally-unique federation key.

    Host element   -> the bare Revit UniqueId.
    Linked element -> ``link_ns + '|' + UniqueId``.

    ``unique_id`` is the element's UniqueId within its OWN document. ``link_ns``
    is None for host elements, else the string from ``link_ns_from_origin`` /
    ``link_ns_for_instance``. Returns None if unique_id is missing.
    Pure / testable."""
    if not unique_id:
        return None
    if link_ns is None:
        return unique_id
    return link_ns + "|" + unique_id


def doc_name(doc):
    """The exporter's name choice for a link namespace: PathName, else Title,
    else 'link'. Never raises."""
    try:
        name = doc.PathName or doc.Title or None
    except Exception:
        name = None
    return name or "link"


def link_ns_for_instance(link_instance):
    """Build the link namespace for a RevitLinkInstance held by the DETECTOR,
    matching the exporter's LinkNode-derived namespace.

    Uses ``GetTotalTransform().Origin`` -- the TOTAL placement (instance plus the
    linked document's shared-coordinate transform) -- because that is what the
    CustomExporter ``LinkNode.GetTransform()`` reports and what the exported
    geometry actually used. Using ``GetTransform()`` here would drop any
    shared-coordinate offset and silently break the linked-element join.

    Revit-only; the lazy access keeps this module CPython-parseable for tests."""
    doc = None
    try:
        doc = link_instance.GetLinkDocument()
    except Exception:
        doc = None
    name = doc_name(doc) if doc is not None else "link"
    try:
        o = link_instance.GetTotalTransform().Origin
        return link_ns_from_origin(name, o.X, o.Y, o.Z)
    except Exception:
        return name
