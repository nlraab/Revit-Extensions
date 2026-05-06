# -*- coding: utf-8 -*-
"""Cross-version Revit API compatibility shims.

Revit 2024 widened `ElementId` from a 32-bit int to a 64-bit long, deprecated
the `IntegerValue` property, and added a new `Value` property. Revit 2026
appears to have actually broken `IntegerValue` access (it throws), so any
code path that does `eid.IntegerValue` silently drops every element on
2026 if the access is wrapped in a try/except.

Use `eid_int(elem.Id)` instead of `elem.Id.IntegerValue` everywhere in the
detection pipeline.

Also: `BuiltInCategory` enum values are sometimes flaky to pass directly to
`OfCategory` from IronPython (the implicit unbox to the .NET enum type can
mis-resolve). Wrap the BIC in an `ElementId` and use `OfCategoryId` instead -
that's the canonical filter.
"""


def eid_int(eid):
    """Get the integer value of a Revit ElementId, working on any Revit version.

    Revit 2024+ exposes `ElementId.Value` (long).
    Pre-2024 exposes `ElementId.IntegerValue` (int).
    Revit 2026 has `Value` but its `IntegerValue` may throw.

    Returns 0 if both accessors fail (defensive - shouldn't happen on a
    valid ElementId).
    """
    if eid is None:
        return 0
    # Try the modern long-based property first.
    try:
        return int(eid.Value)
    except AttributeError:
        pass
    except Exception:
        pass
    # Fall back to the legacy property.
    try:
        return int(eid.IntegerValue)
    except Exception:
        return 0


def bic_to_id(bic):
    """Convert a BuiltInCategory enum value to an ElementId for OfCategoryId().

    OfCategoryId(ElementId) is more reliable than OfCategory(BuiltInCategory)
    in IronPython contexts where the enum boxing can mis-resolve.
    """
    from Autodesk.Revit.DB import ElementId
    return ElementId(bic)
