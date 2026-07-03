# -*- coding: utf-8 -*-
"""Per-project clash-data folder, stored INSIDE the Revit model.

The whole state model of the clash tool is deliberately tiny: **a project
remembers exactly one thing, the absolute path of its clash-data folder.**
Everything the tool reads and writes (clashes, settings, viewpoints) lives
directly in that folder. Point the model at a different folder and you see that
folder's data; an empty folder shows an empty tool; nothing is remembered
across a folder change except the path itself.

The path is stored via Extensible Storage on the model's ProjectInformation
element, so it travels with the .rvt and is the same for every teammate who
opens it. We store the path exactly as the user picked it and do nothing clever
about network vs. local: if a teammate can't reach that path, they simply see
no data. A brand-new model has no stored path, so the tool shows nothing until
the user points it at a folder.

All Revit API use is imported lazily inside function bodies so this module still
parses under CPython 3 for the test suite (same pattern as project.py).
"""


# Fixed schema identity. NEVER change this GUID: it is how we find the folder
# path written by earlier tool versions. (This is a NEW guid, distinct from the
# retired subfolder-name schema, so any half-written old binding is simply
# ignored and the user re-points the folder once.)
_SCHEMA_GUID_STR = "3F2A9B71-6C4D-4E88-A1F3-2D5B7C0E9A46"
_SCHEMA_NAME     = "dbHMSClashFolder"
_VENDOR_ID       = "dbHMS"
_FIELD_FOLDER    = "folder_path"


# ---------------------------------------------------------------------------
# Extensible Storage schema (Revit; lazily imported)
# ---------------------------------------------------------------------------

def _schema(create=False):
    """Look up (or, when create=True, build) our Extensible Storage schema.
    Building a schema does NOT require a transaction; only writing an entity to
    an element does. Returns the Schema, or None when absent and create=False."""
    import clr  # noqa: F401
    import System
    from Autodesk.Revit.DB.ExtensibleStorage import (
        Schema, SchemaBuilder, AccessLevel)
    gid = System.Guid(_SCHEMA_GUID_STR)
    existing = Schema.Lookup(gid)
    if existing is not None:
        return existing
    if not create:
        return None
    b = SchemaBuilder(gid)
    b.SetSchemaName(_SCHEMA_NAME)
    b.SetVendorId(_VENDOR_ID)
    b.SetReadAccessLevel(AccessLevel.Public)
    b.SetWriteAccessLevel(AccessLevel.Public)
    b.AddSimpleField(_FIELD_FOLDER, clr.GetClrType(System.String))
    return b.Finish()


def model_folder(doc):
    """Return the folder path stored INSIDE `doc` (Extensible Storage), or
    None. Never raises.

    Builds the schema (create=True) before reading: opening a .rvt that already
    contains our entity does NOT reliably register the schema in a fresh Revit
    session, so Schema.Lookup can return None even though the entity is there.
    Building is idempotent (Lookup short-circuits once registered), needs no
    transaction, and our definition is fixed, so it's safe on every read. This
    is what makes a reopened model -- and every teammate, and the read-only 3D
    Viewer -- actually see the stored folder."""
    if doc is None:
        return None
    try:
        import System
        schema = _schema(create=True)
        if schema is None:
            return None
        pinfo = getattr(doc, "ProjectInformation", None)
        if pinfo is None:
            return None
        ent = pinfo.GetEntity(schema)
        if ent is None or not ent.IsValid():
            return None
        path = ent.Get[System.String](_FIELD_FOLDER)
        return path or None
    except Exception:
        return None


def folder_for(doc):
    """Return this project's clash-data folder, or None. The tool's ONE
    "is this project set up?" test. Never raises.

    Two layers, model first:
      1. The Extensible Storage binding inside the model - the team truth
         (travels with the saved .rvt to every user).
      2. A per-machine registry (%APPDATA%/dbHMS_clash/bindings.json keyed
         by the central model path) - the resilience layer. ES only becomes
         permanent when the model is SAVED, so a close-without-save or a
         mid-session pyRevit reload on an unsaved model would otherwise
         "forget" the folder and flood Nathan with why-is-my-data-gone
         questions. The registry remembers every binding this machine has
         seen and answers when the model can't.
    A model hit is written through to the registry; callers that can open a
    transaction should heal the model when `needs_heal` says so."""
    folder = model_folder(doc)
    if folder:
        remember_local(doc, folder)
        return folder
    return _local_folder(doc)


def needs_heal(doc):
    """Return the folder to re-write into the model when the model has no
    binding but this machine remembers one (the close-without-save case).
    None when the model is already bound or nothing is remembered."""
    if model_folder(doc):
        return None
    return _local_folder(doc)


# ---------------------------------------------------------------------------
# Per-machine binding registry (pure file I/O; CPython-testable)
# ---------------------------------------------------------------------------

def _doc_key(doc):
    """Stable per-model key: hash of the central model path. None when the
    document is unsaved/untitled or unreadable."""
    if doc is None:
        return None
    try:
        from clash_core import project
        try:
            path = project.central_model_path(doc)
        except Exception:
            path = getattr(doc, "PathName", "") or ""
        key = project.hash_path(path)
        return key or None
    except Exception:
        return None


def _registry_path():
    from clash_core import config
    import os
    return os.path.join(config.config_dir(), "bindings.json")


def _read_registry():
    import io
    import json
    try:
        with io.open(_registry_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def remember_local(doc, folder):
    """Record doc->folder in the per-machine registry. Best-effort; a failed
    write must never break a read path."""
    key = _doc_key(doc)
    if not key or not folder:
        return
    try:
        import io
        import json
        reg = _read_registry()
        if (reg.get(key) or {}).get("folder") == folder:
            return
        reg[key] = {"folder": folder}
        with io.open(_registry_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(reg, indent=2, sort_keys=True,
                               ensure_ascii=False))
    except Exception:
        pass


def _local_folder(doc):
    key = _doc_key(doc)
    if not key:
        return None
    return (_read_registry().get(key) or {}).get("folder") or None


def write_binding(doc, folder_path):
    """Store `folder_path` on `doc`. MUST be called inside an open Revit
    transaction (the caller owns it, because writing an entity mutates the
    document). Raises on failure (e.g. ProjectInformation not editable in a
    workshared model); the caller should surface a friendly message.

    Also writes through to the per-machine registry immediately: the model
    copy only survives a SAVE, and the registry is what keeps the folder
    across a close-without-save or pyRevit reload."""
    import System
    from Autodesk.Revit.DB.ExtensibleStorage import Entity
    schema = _schema(create=True)
    ent = Entity(schema)
    ent.Set[System.String](_FIELD_FOLDER, folder_path)
    doc.ProjectInformation.SetEntity(ent)
    remember_local(doc, folder_path)
    return folder_path


def clear_binding(doc):
    """Remove any stored folder path from `doc` (used if the user ever wants to
    un-set a project). MUST be called inside an open transaction. Best-effort.
    create=True for the same reason as folder_for: a fresh session may not have
    the schema registered, and DeleteEntity needs a valid schema object."""
    try:
        schema = _schema(create=True)
        if schema is None:
            return
        doc.ProjectInformation.DeleteEntity(schema)
    except Exception:
        pass
