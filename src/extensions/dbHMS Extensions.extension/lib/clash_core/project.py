# -*- coding: utf-8 -*-
"""Project-identity helpers.

A clash database needs a stable identifier for "this project" that is
independent of which user is opening it. We derive it from the central
model's full path (which everyone on the team sees as the same string when
opening from BIM Workshare / shared Revit Server / a UNC path).

The hash is a short hex digest, used as a folder name under the shared root.
Display name is stored in project.json so we never have to show the hash to
the user.

The pure path/hash helpers (`normalize_path`, `hash_path`) are
Revit-independent and unit-tested. The doc-aware helpers
(`central_model_path`, `project_hash_for`, `display_name_for`) import the
Revit API lazily inside the function body so this module also parses cleanly
in CPython 3 for the test suite.
"""

import hashlib
import os


HASH_LENGTH = 12


# ---------------------------------------------------------------------------
# Pure helpers (Revit-independent, testable)
# ---------------------------------------------------------------------------

def normalize_path(path):
    """Normalize a model path so two equivalent forms hash the same.

    Lowercases, switches backslashes to forward slashes, strips whitespace.
    `T:\\Foo.rvt`, `t:/foo.rvt`, and `T:/FOO.RVT  ` all collapse to the same
    canonical string.
    """
    if not path:
        return ""
    return path.replace("\\", "/").strip().lower()


def hash_path(path):
    """Return the first HASH_LENGTH hex chars of SHA-1 of the normalized path.

    Empty paths return "" (callers can detect "no project" with `not hash_path(...)`).
    """
    norm = normalize_path(path)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:HASH_LENGTH]


# ---------------------------------------------------------------------------
# Doc-aware helpers (require the Revit API; lazily imported inside)
# ---------------------------------------------------------------------------

def central_model_path(doc):
    """Return the full path to the central .rvt for `doc`.

    For workshared documents, uses GetWorksharingCentralModelPath +
    ModelPathUtils.ConvertModelPathToUserVisiblePath so the result matches
    what Revit shows in the title bar. For non-workshared documents (or if
    workshare lookup fails), falls back to `doc.PathName`.
    """
    from Autodesk.Revit.DB import ModelPathUtils  # noqa: F401

    if getattr(doc, "IsWorkshared", False):
        try:
            mp = doc.GetWorksharingCentralModelPath()
            if mp is not None:
                return ModelPathUtils.ConvertModelPathToUserVisiblePath(mp) or ""
        except Exception:
            pass
    return getattr(doc, "PathName", "") or ""


def project_hash_for(doc):
    """Return the stable per-project hash for `doc`. May be "" for an
    unsaved/untitled document - callers should treat that as "no project."""
    return hash_path(central_model_path(doc))


def display_name_for(doc):
    """Return a human-readable project name (file basename minus .rvt by default)."""
    p = getattr(doc, "PathName", "") or ""
    if not p:
        return "Untitled Project"
    base = os.path.basename(p)
    if base.lower().endswith(".rvt"):
        base = base[:-4]
    return base or "Untitled Project"
