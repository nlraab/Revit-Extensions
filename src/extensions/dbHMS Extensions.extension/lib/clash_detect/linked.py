# -*- coding: utf-8 -*-
"""Linked-document element collection and coordinate-system handling.

The headache: a host doc can have N RevitLinkInstance objects, each pointing
at the same linked .rvt with a different transform (think "site model with
the same building copied three times"). When we collect "all walls in the
arch link", we have to enumerate every link instance, not just the link
type once.

The other headache: BoundingBoxIntersectsFilter against a link instance
returns wrong results when the link is moved or rotated relative to the
host. See README "Linked-model intersection notes" for the workaround
(applied later when detection lands).

Also contains helpers for the per-project linked-model role mapping
(see Settings tool / project.json's `link_role_map`).

Revit imports are inside function bodies so this module parses cleanly in
CPython 3 for the test suite — only the live functions need the API.
"""


# ---------------------------------------------------------------------------
# Role constants (canonical strings used in project.json link_role_map)
# ---------------------------------------------------------------------------

ROLE_ARCHITECTURAL = "Architectural"
ROLE_STRUCTURAL    = "Structural"
ROLE_IGNORE        = "ignore"

ROLE_OPTIONS = (ROLE_ARCHITECTURAL, ROLE_STRUCTURAL, ROLE_IGNORE)

# Display label for the (ignore) role - the canonical value is "ignore" but
# we show it as "(ignore)" in pickers so users see it as a deliberate choice
# rather than a missing value.
IGNORE_DISPLAY = "(ignore)"


# ---------------------------------------------------------------------------
# Live link enumeration (Revit-dependent)
# ---------------------------------------------------------------------------

def find_link_instances(doc):
    """Return every loaded RevitLinkInstance in `doc`.

    Unloaded links (where GetLinkDocument() is None) are filtered out. If
    the same .rvt is linked multiple times with different transforms, every
    instance appears separately; use `find_unique_link_titles` if you only
    want the distinct file titles.
    """
    from Autodesk.Revit.DB import (
        FilteredElementCollector, BuiltInCategory, RevitLinkInstance,
    )

    instances = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_RvtLinks)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    out = []
    for inst in instances:
        if not isinstance(inst, RevitLinkInstance):
            continue
        if inst.GetLinkDocument() is None:
            continue  # unloaded
        out.append(inst)
    return out


def link_title(link_instance):
    """Return the linked .rvt's Title (filename without folder, no .rvt)."""
    link_doc = link_instance.GetLinkDocument()
    if link_doc is None:
        return "(unloaded)"
    return link_doc.Title


def find_unique_link_titles(doc):
    """Return a sorted list of unique linked .rvt titles in `doc`.

    Used by Settings to populate the role-mapping rows: one row per unique
    title (not per instance), since users assign roles by file.
    """
    titles = set()
    for inst in find_link_instances(doc):
        titles.add(link_title(inst))
    return sorted(titles)


# ---------------------------------------------------------------------------
# Role-map helpers (combine live enumeration with the saved mapping)
# ---------------------------------------------------------------------------

def merged_link_view(doc, role_map):
    """Combine live link enumeration with the saved `role_map` dict.

    Returns a list of dicts, one per unique linked .rvt:
        {'title': filename, 'role': 'Architectural' | 'Structural' | 'ignore'}

    Order: alphabetical by title. Links present in the live doc but not in
    role_map default to 'ignore' (the safe default for new links).
    """
    out = []
    for title in find_unique_link_titles(doc):
        role = role_map.get(title, ROLE_IGNORE)
        if role not in ROLE_OPTIONS:
            role = ROLE_IGNORE
        out.append({"title": title, "role": role})
    return out


def links_for_role(doc, role_map, target_role):
    """Return the RevitLinkInstance objects whose linked .rvt is mapped to `target_role`.

    Used by the detection engine: e.g. `links_for_role(doc, role_map, "Architectural")`
    returns every link instance to feed into a `link:Architectural` clash test.
    """
    out = []
    for inst in find_link_instances(doc):
        title = link_title(inst)
        if role_map.get(title, ROLE_IGNORE) == target_role:
            out.append(inst)
    return out


# ---------------------------------------------------------------------------
# Geometry / element collection (placeholders - implemented in detection chunk)
# ---------------------------------------------------------------------------

def collect_link_elements(link_instance, of_ost_names):
    """Collect elements in `link_instance.GetLinkDocument()` matching the
    given OST_ category names.

    Returns a list of Element objects (in link-local coordinates - caller
    is responsible for applying `link_transform(link_instance)` when
    computing geometry in host coordinates).

    Unknown OST_ names are silently skipped (so a category that exists in
    one Revit version but not another doesn't break the call).
    """
    link_doc = link_instance.GetLinkDocument()
    if link_doc is None:
        return []
    return collect_doc_elements(link_doc, of_ost_names)


def collect_doc_elements(doc, of_ost_names, log=None):
    """Collect elements in `doc` matching any of the OST_ category names.

    Uses `OfCategoryId(ElementId(bic))` rather than `OfCategory(bic)` because
    the latter has flaky behavior in some IronPython/Revit-version combos
    where the BuiltInCategory enum doesn't unbox correctly through the .NET
    interop. Element IDs are extracted via the version-agnostic `eid_int`
    helper from `_compat` (Revit 2024+ uses `ElementId.Value`).
    """
    from Autodesk.Revit.DB import FilteredElementCollector, BuiltInCategory
    from System import Enum
    from clash_detect._compat import eid_int, bic_to_id

    out = []
    seen_ids = set()
    for ost in of_ost_names or []:
        bic = _resolve_bic(BuiltInCategory, Enum, ost)
        if bic is None:
            if log:
                log("      `{}`: could NOT resolve to BuiltInCategory "
                    "(unknown / misspelled / unsupported in this Revit version)"
                    .format(ost))
            continue
        try:
            cat_id = bic_to_id(bic)
            elements = (
                FilteredElementCollector(doc)
                .OfCategoryId(cat_id)
                .WhereElementIsNotElementType()
                .ToElements()
            )
        except Exception as ex:
            if log:
                log("      `{}`: collection raised - {}".format(ost, ex))
            continue
        added = 0
        skipped_no_id = 0
        for e in elements:
            ei = eid_int(e.Id)
            if ei == 0:
                skipped_no_id += 1
                continue
            if ei in seen_ids:
                continue
            seen_ids.add(ei)
            out.append(e)
            added += 1
        if log:
            tail = ""
            if skipped_no_id:
                tail = " (skipped {} with unreadable id)".format(skipped_no_id)
            log("      `{}`: {} element(s) in this doc{}".format(ost, added, tail))
    return out


def _resolve_bic(BuiltInCategory, Enum, ost_name):
    """Try multiple paths to convert an `OST_*` string to a BuiltInCategory enum value.

    `getattr` works in most IronPython/Revit version combos but has been seen
    to silently return a non-enum value on Revit 2026; `Enum.Parse` is the
    canonical .NET path and always returns the boxed enum value correctly.
    """
    # Prefer Enum.Parse - it's the canonical .NET string-to-enum conversion
    # and consistently returns a value that .NET methods accept.
    try:
        return Enum.Parse(BuiltInCategory, ost_name)
    except Exception:
        pass
    # Fall back to getattr (older Revit / IronPython where Parse may be slow).
    bic = getattr(BuiltInCategory, ost_name, None)
    return bic


def link_transform(link_instance):
    """Return the Transform from link coordinates to host coordinates.
    Identity if the link was placed at origin without rotation."""
    return link_instance.GetTransform()


def host_solid_in_link_space(solid, link_instance):
    """Return a copy of `solid` transformed so it lives in the link's
    coordinate system.

    Use this when testing a host-side solid against a linked element via
    `ElementIntersectsSolidFilter` scoped to the linked document: the
    filter compares geometry in the link's own coordinate frame.

    Math: link.GetTransform() maps link->host, so we apply its inverse
    to map host->link.
    """
    from Autodesk.Revit.DB import SolidUtils
    inverse = link_instance.GetTransform().Inverse
    return SolidUtils.CreateTransformed(solid, inverse)


def link_solid_in_host_space(solid, link_instance):
    """Return a copy of `solid` (which lives in link coordinates) transformed
    into host coordinates. Useful for cross-doc clash midpoint computation
    in shared coords."""
    from Autodesk.Revit.DB import SolidUtils
    return SolidUtils.CreateTransformed(solid, link_instance.GetTransform())
