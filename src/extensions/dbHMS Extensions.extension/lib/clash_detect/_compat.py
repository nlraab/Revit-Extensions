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


def make_eid(value):
    """Construct a Revit ElementId from an integer-like value, version-tolerant.

    Revit 2024+ widened ElementId from Int32 to Int64; the Int32 constructor
    is deprecated and on Revit 2026 has been observed to silently route through
    the wrong overload (ElementId(BuiltInCategory) takes priority in IronPython
    overload resolution because BuiltInCategory's underlying type is Int32, so
    `ElementId(1250111)` produces an ElementId encoding the BIC value 1250111
    rather than the element ID 1250111 — and `doc.GetElement(...)` then quietly
    returns None instead of the intended element).

    Going through `System.Int64` explicitly forces the Int64 constructor so we
    get the element-id-as-id semantics we actually want. Falls back to the
    Int32 constructor on Revit versions that don't have the Int64 overload.

    Returns None if `value` is None or can't be converted.
    """
    if value is None:
        return None
    from Autodesk.Revit.DB import ElementId
    try:
        from System import Int64
        return ElementId(Int64(int(value)))
    except Exception:
        pass
    try:
        return ElementId(int(value))
    except Exception:
        return None
