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
    """Return the stable identity path for `doc`'s central model.

    Priority order matters for identity stability:
    1. ACC / BIM 360 cloud models: the CLOUD path ("BIM 360://Project/
       Model.rvt" style) - identical for every teammate on every machine,
       unlike doc.PathName which points at the machine-local cloud cache.
    2. Workshared file-based models: GetWorksharingCentralModelPath, which
       matches what Revit shows in the title bar (UNC/mapped path everyone
       shares).
    3. Everything else (or any lookup failure): doc.PathName.
    """
    from Autodesk.Revit.DB import ModelPathUtils  # noqa: F401

    try:
        if getattr(doc, "IsModelInCloud", False):
            mp = doc.GetCloudModelPath()
            if mp is not None:
                p = ModelPathUtils.ConvertModelPathToUserVisiblePath(mp) or ""
                if p:
                    return p
    except Exception:
        pass
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


def all_identity_paths(doc):
    """Every identity path this doc could hash to, most-stable first: cloud
    path, workshare central path, and PathName. Used by the binding registry
    so a change in how we pick the 'primary' path can never orphan a mapping
    that was written under a different pick - the registry tries them all."""
    if doc is None:
        return []
    out = []

    def _add(p):
        if p and p not in out:
            out.append(p)

    try:
        from Autodesk.Revit.DB import ModelPathUtils
        try:
            if getattr(doc, "IsModelInCloud", False):
                mp = doc.GetCloudModelPath()
                if mp is not None:
                    _add(ModelPathUtils.ConvertModelPathToUserVisiblePath(mp))
        except Exception:
            pass
        try:
            if getattr(doc, "IsWorkshared", False):
                mp = doc.GetWorksharingCentralModelPath()
                if mp is not None:
                    _add(ModelPathUtils.ConvertModelPathToUserVisiblePath(mp))
        except Exception:
            pass
    except Exception:
        pass
    _add(getattr(doc, "PathName", "") or "")
    return out


def display_name_for(doc):
    """Return a human-readable project name (file basename minus .rvt by default)."""
    p = getattr(doc, "PathName", "") or ""
    if not p:
        return "Untitled Project"
    base = os.path.basename(p)
    if base.lower().endswith(".rvt"):
        base = base[:-4]
    return base or "Untitled Project"
