# -*- coding: utf-8 -*-
"""Plain-Python data structures for the clash detection domain.

Everything is a JSON-serializable dict under the hood. Factory functions
build the dicts so the schema stays in one place. IronPython 2.7 doesn't
have dataclasses, so we stay simple.

Concept map:
    Discipline      -> a trade ("Mechanical", "Electrical", ...)
    ElementRef      -> identifier for one Revit element (host or linked)
    ClashTest       -> a saved definition: name, type, set A, set B, defaults
    Clash           -> one detected intersection; refs to two elements + state
    Comment         -> one entry in a clash's discussion thread
    Viewpoint       -> camera + section box + optional snapshot path
    HistoryEntry    -> audit log of a clash's state changes
"""

import datetime
import uuid


# ---------------------------------------------------------------------------
# Enumerations (string constants - IronPython-compatible)
# ---------------------------------------------------------------------------

class Discipline(object):
    MECHANICAL      = "Mechanical"
    ELECTRICAL      = "Electrical"
    PLUMBING        = "Plumbing"
    FIRE_PROTECTION = "Fire Protection"
    TECHNOLOGY      = "Technology"
    ARCHITECTURAL   = "Architectural"
    STRUCTURAL      = "Structural"

    ALL = (
        MECHANICAL, ELECTRICAL, PLUMBING, FIRE_PROTECTION,
        TECHNOLOGY, ARCHITECTURAL, STRUCTURAL,
    )


class ClashStatus(object):
    OPEN     = "Open"
    REVIEWED = "Reviewed"
    APPROVED = "Approved"
    RESOLVED = "Resolved"

    ALL = (OPEN, REVIEWED, APPROVED, RESOLVED)


class ClashKind(object):
    HARD      = "hard"
    SOFT      = "soft"
    CLEARANCE = "clearance"


class ElementSource(object):
    HOST                  = "host"
    LINK_ARCHITECTURAL    = "link:Architectural"
    LINK_STRUCTURAL       = "link:Structural"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso():
    """UTC timestamp in ISO-8601 with trailing Z (no microseconds).

    Uses tz-aware `datetime.now(timezone.utc)` on CPython 3.x (the test runtime),
    falls back to `datetime.utcnow()` on IronPython 2.7 (the Revit runtime,
    which doesn't have `datetime.timezone`). Both paths produce the same
    `YYYY-MM-DDTHH:MM:SSZ` string."""
    try:
        from datetime import timezone
        now = datetime.datetime.now(timezone.utc)
    except ImportError:
        now = datetime.datetime.utcnow()
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id():
    return str(uuid.uuid4())


def _xyz_to_list(xyz):
    """Accept a tuple/list of 3 numbers OR a Revit XYZ; return a 3-list."""
    if xyz is None:
        return None
    if hasattr(xyz, "X") and hasattr(xyz, "Y") and hasattr(xyz, "Z"):
        return [float(xyz.X), float(xyz.Y), float(xyz.Z)]
    return [float(c) for c in xyz]


# ---------------------------------------------------------------------------
# Factories - dicts in / dicts out
# ---------------------------------------------------------------------------

def make_element_ref(source, element_id, category=None, name=None,
                     link_doc_title=None, category_id=None):
    """Build an ElementRef dict.

    `source` is one of ElementSource.* (or any "link:<role>" string).
    For host elements, set source=ElementSource.HOST and leave link_doc_title=None.
    For linked elements, link_doc_title should be the linked .rvt's title so
    the element can be re-resolved on the next session.

    `category` is the human-readable Category.Name (e.g. "Pipes", "Walls").
    `category_id` is the integer BuiltInCategory id (e.g. -2008132 for OST_PipeCurves);
    used by the merge layer to auto-derive the clash assignee from the element's
    discipline. Stored in JSON so re-runs don't have to re-resolve through Revit.
    """
    return {
        "source":         source,
        "element_id":     int(element_id) if element_id is not None else None,
        "category":       category,
        "category_id":    int(category_id) if category_id is not None else None,
        "name":           name,
        "link_doc_title": link_doc_title,
    }


def make_clash_test(name, kind, set_a, set_b, tolerance_inches=0.0,
                    default_assignee=None, test_id=None):
    """Build a ClashTest dict ready to be persisted in the test library."""
    return {
        "id":                test_id or _new_id(),
        "name":              name,
        "kind":              kind,
        "tolerance_inches":  float(tolerance_inches),
        "set_a":             set_a,
        "set_b":             set_b,
        "default_assignee":  default_assignee,
    }


def make_clash(test_id, ref_a, ref_b, midpoint_xyz, kind=None,
               status=None, assignee=None, run_at=None, clash_id=None):
    """Build a Clash dict for a freshly detected intersection."""
    if kind is None:
        kind = ClashKind.HARD
    if status is None:
        status = ClashStatus.OPEN
    when = run_at or _now_iso()
    return {
        "id":              clash_id or _new_id(),
        "test_id":         test_id,
        "kind":            kind,
        "status":          status,
        "assignee":        assignee,
        "ref_a":           ref_a,
        "ref_b":           ref_b,
        "midpoint":        _xyz_to_list(midpoint_xyz),
        "first_seen_run": when,
        "last_seen_run":   when,
        "comments":        [],
        "viewpoints":      [],
        "history":         [],
    }


def make_comment(author, body, at=None):
    """Build a Comment dict with an automatic timestamp (UTC)."""
    return {
        "author": author,
        "at":     at or _now_iso(),
        "body":   body,
    }


def make_viewpoint(camera_position, target, up_vector,
                   section_box=None, snapshot_relpath=None,
                   captured_by=None, viewpoint_id=None):
    """Build a Viewpoint dict capturing camera + section box + optional PNG path.

    `section_box` is (min_xyz, max_xyz) or None.
    """
    vp = {
        "id":           viewpoint_id or _new_id(),
        "captured_by":  captured_by,
        "captured_at":  _now_iso(),
        "camera": {
            "position": _xyz_to_list(camera_position),
            "target":   _xyz_to_list(target),
            "up":       _xyz_to_list(up_vector),
        },
    }
    if section_box is not None:
        vp["section_box"] = {
            "min": _xyz_to_list(section_box[0]),
            "max": _xyz_to_list(section_box[1]),
        }
    if snapshot_relpath:
        vp["snapshot_relpath"] = snapshot_relpath
    return vp


def make_history_entry(author, action, before=None, after=None):
    """Build a HistoryEntry dict for the audit log."""
    entry = {
        "author": author,
        "at":     _now_iso(),
        "action": action,
    }
    if before is not None:
        entry["before"] = before
    if after is not None:
        entry["after"] = after
    return entry
