# -*- coding: utf-8 -*-
"""Mapping between Revit BuiltInCategory OST names and friendly display names.

Used by the Test Library editor to render category checkboxes with human
labels while persisting the canonical `OST_*` name in `test_library.json`.
Only includes categories that show up in clash tests; extend as needed.

Order in `CATEGORIES` controls display order in the editor (grouped by
discipline so the editor reads top-to-bottom by trade).
"""


# (OST_name, friendly_name, discipline_group)
CATEGORIES = [
    # Mechanical (HVAC)
    ("OST_DuctCurves",            "Duct Curves",            "Mechanical"),
    ("OST_DuctFitting",           "Duct Fittings",          "Mechanical"),
    ("OST_DuctAccessory",         "Duct Accessories",       "Mechanical"),
    ("OST_FlexDuctCurves",        "Flex Duct Curves",       "Mechanical"),
    ("OST_DuctTerminal",          "Air Terminals",          "Mechanical"),
    ("OST_MechanicalEquipment",   "Mechanical Equipment",   "Mechanical"),

    # Plumbing / Piping
    ("OST_PipeCurves",            "Pipe Curves",            "Plumbing"),
    ("OST_PipeFitting",           "Pipe Fittings",          "Plumbing"),
    ("OST_PipeAccessory",         "Pipe Accessories",       "Plumbing"),
    ("OST_FlexPipeCurves",        "Flex Pipe Curves",       "Plumbing"),
    ("OST_PlumbingFixtures",      "Plumbing Fixtures",      "Plumbing"),

    # Fire Protection
    ("OST_Sprinklers",            "Sprinklers",             "Fire Protection"),

    # Electrical
    ("OST_Conduit",               "Conduit",                "Electrical"),
    ("OST_ConduitFitting",        "Conduit Fittings",       "Electrical"),
    ("OST_CableTray",             "Cable Tray",             "Electrical"),
    ("OST_CableTrayFitting",      "Cable Tray Fittings",    "Electrical"),
    ("OST_ElectricalEquipment",   "Electrical Equipment",   "Electrical"),
    ("OST_ElectricalFixtures",    "Electrical Fixtures",    "Electrical"),
    ("OST_LightingFixtures",      "Lighting Fixtures",      "Electrical"),

    # Technology
    ("OST_DataDevices",           "Data Devices",           "Technology"),
    ("OST_CommunicationDevices",  "Communication Devices",  "Technology"),
    ("OST_TelephoneDevices",      "Telephone Devices",      "Technology"),
    ("OST_NurseCallDevices",      "Nurse Call Devices",     "Technology"),
    ("OST_SecurityDevices",       "Security Devices",       "Technology"),

    # Architectural
    ("OST_Walls",                 "Walls",                  "Architectural"),
    ("OST_Floors",                "Floors",                 "Architectural"),
    ("OST_Ceilings",              "Ceilings",               "Architectural"),
    ("OST_Doors",                 "Doors",                  "Architectural"),
    ("OST_Windows",               "Windows",                "Architectural"),
    ("OST_Roofs",                 "Roofs",                  "Architectural"),
    ("OST_Stairs",                "Stairs",                 "Architectural"),

    # Structural
    ("OST_StructuralFraming",     "Structural Framing",     "Structural"),
    ("OST_StructuralColumns",     "Structural Columns",     "Structural"),
    ("OST_StructuralFoundation",  "Structural Foundations", "Structural"),
]


# Lookups derived from CATEGORIES - kept as module-level dicts so callers
# don't have to scan the list for every check.
FRIENDLY_BY_OST = {ost: friendly for ost, friendly, _ in CATEGORIES}
OST_BY_FRIENDLY = {friendly: ost for ost, friendly, _ in CATEGORIES}
GROUP_BY_OST    = {ost: group    for ost, _, group    in CATEGORIES}


def friendly_for(ost_name):
    """Return the friendly name for an OST_ category; falls back to the OST
    name itself if unknown (so unrecognized categories still render)."""
    return FRIENDLY_BY_OST.get(ost_name, ost_name)


def ost_for(friendly_name):
    """Return the OST_ name for a friendly name; falls back to the friendly
    name unchanged if unknown."""
    return OST_BY_FRIENDLY.get(friendly_name, friendly_name)


def categories_grouped():
    """Return categories grouped by discipline, in display order.

    Returns a list of (group_name, [(ost, friendly), ...]) tuples so callers
    can render section headers between groups while preserving the order
    declared in CATEGORIES.
    """
    out = []
    last_group = None
    current = None
    for ost, friendly, group in CATEGORIES:
        if group != last_group:
            current = (group, [])
            out.append(current)
            last_group = group
        current[1].append((ost, friendly))
    return out


# ---------------------------------------------------------------------------
# Revit-aware lookup: BuiltInCategory id -> discipline
# ---------------------------------------------------------------------------
#
# Used by the merge layer to auto-derive a clash's assignee trade from the
# element's actual Category Id. Stable across Revit versions because the
# integer values of the BuiltInCategory enum members are part of Revit's
# public API contract.
#
# Lazy-initialized on first call so this module can still be imported in
# CPython 3 (the test runtime) without a Revit API in scope.
# ---------------------------------------------------------------------------

_BIC_INT_TO_DISCIPLINE = None


def discipline_for_category_id(category_id_int):
    """Return the dbHMS discipline group for a Revit Category Id integer,
    or None if we don't know a mapping.

    `category_id_int` is the integer underlying `Category.Id` (which is a
    Revit ElementId; use the version-agnostic `eid_int()` helper to extract
    it). Negative integers like -2008132 correspond to BuiltInCategory enum
    values and are stable across Revit versions.

    On first call, builds the BIC-int -> discipline map by walking
    `CATEGORIES` and resolving each `OST_*` name through `BuiltInCategory`
    via `System.Enum.Parse`. Subsequent calls hit the cache.

    Requires Revit API at first-call time; do not call from non-Revit
    contexts (the test suite, for example).
    """
    global _BIC_INT_TO_DISCIPLINE
    if _BIC_INT_TO_DISCIPLINE is None:
        _BIC_INT_TO_DISCIPLINE = _build_bic_int_to_discipline()
    return _BIC_INT_TO_DISCIPLINE.get(category_id_int)


def _build_bic_int_to_discipline():
    """Walk CATEGORIES and resolve each OST_ name to its BuiltInCategory
    integer value. Lazy-imports the Revit API."""
    from Autodesk.Revit.DB import BuiltInCategory
    from System import Enum

    out = {}
    for ost, _friendly, discipline in CATEGORIES:
        try:
            bic = Enum.Parse(BuiltInCategory, ost)
            out[int(bic)] = discipline
        except Exception:
            continue
    return out
