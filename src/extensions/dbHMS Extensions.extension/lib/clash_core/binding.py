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

TWO HARD RULES (an adversarial multi-machine review found the tool violating
both, silently corrupting the team's shared binding):

  1. The MODEL is written ONLY by an explicit user action (_set_folder ->
     write_binding, inside a transaction the user then saves/syncs). NOTHING
     writes the model automatically on open. The old "heal on open" that
     re-published the local cache into the model is GONE - it could clobber a
     teammate's synced change on the next sync.

  2. A FAILED read is never treated as "not set". read_model() returns a
     three-state status (BOUND / UNSET / CLEARED / UNKNOWN); only a clean
     BOUND caches to the registry, and UNKNOWN never drives any write. This
     stops a transient ES read (routine on ACC reload) from serving, and
     cementing, a stale folder.

The per-machine registry (%APPDATA%/dbHMS_clash/bindings.json) is a pure
LOCAL convenience: it lets a close-without-save session still resolve the
folder on this machine. It can never reach the shared model.

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


# Three-state result of reading the model's binding. The whole multi-machine
# correctness of the tool hinges on keeping these DISTINCT (an adversarial
# review found that collapsing UNKNOWN into "not set" let a transient ES
# read on one machine silently serve, and re-publish, a stale folder over a
# teammate's synced change):
#   BOUND   - the model carries a real folder path. Authoritative; team truth.
#   UNSET   - the model was read cleanly and has NO binding (never set).
#   CLEARED - the model was read cleanly and carries an explicit "cleared"
#             tombstone (empty path). Distinct from UNSET so a deliberate
#             team-wide clear is never resurrected from a stale local cache.
#   UNKNOWN - the read FAILED (schema race, ProjectInformation mid-load on an
#             ACC reload, borrow contention). We know NOTHING; we must never
#             treat this as "not set", never write the model, never cement a
#             cache from it.
BOUND   = "bound"
UNSET   = "unset"
CLEARED = "cleared"
UNKNOWN = "unknown"


# Optional diagnostic logger. script.py sets this to its coord.log writer via
# set_logger() so the folder-resolution path is fully visible in one place.
# Default is a no-op so the module stays import-clean under CPython tests.
_LOG = None


def set_logger(fn):
    global _LOG
    _LOG = fn


def _log(msg):
    if _LOG is not None:
        try:
            _LOG("binding: " + msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Extensible Storage schema (Revit; lazily imported)
# ---------------------------------------------------------------------------

def _schema(create=False):
    """Look up (or, when create=True, build) our Extensible Storage schema.
    Building a schema does NOT require a transaction; only writing an entity to
    an element does. Returns the Schema, or None when absent and create=False.

    Hardened against the register race (review finding): SchemaBuilder.Finish()
    throws if the GUID was registered between our Lookup and our Finish (a
    concurrent read on another doc in the same session, or the ES-registration
    timing the read path already fights). On any Finish failure we re-Lookup
    and use whatever is now registered, so a lost race becomes a success, not
    an exception that the caller has to read as UNKNOWN."""
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
    try:
        b = SchemaBuilder(gid)
        b.SetSchemaName(_SCHEMA_NAME)
        b.SetVendorId(_VENDOR_ID)
        b.SetReadAccessLevel(AccessLevel.Public)
        b.SetWriteAccessLevel(AccessLevel.Public)
        b.AddSimpleField(_FIELD_FOLDER, clr.GetClrType(System.String))
        return b.Finish()
    except Exception:
        # Lost the register race (or a foreign schema shares the GUID): use
        # whatever is registered now rather than raising.
        return Schema.Lookup(gid)


def _read_entity(doc):
    """The Revit-facing half of read_model: return (status, path). All ES /
    Revit API contact lives here so read_model's decision logic stays pure
    and unit-testable (tests monkeypatch this). Under CPython (no Revit) the
    ES import fails -> UNKNOWN, which is the correct "can't read" answer."""
    import System
    try:
        schema = _schema(create=True)
    except Exception:
        import traceback
        _log("read: schema build FAILED\n" + traceback.format_exc())
        return (UNKNOWN, None)
    if schema is None:
        return (UNKNOWN, None)
    try:
        pinfo = doc.ProjectInformation
    except Exception:
        _log("read: ProjectInformation FAILED -> UNKNOWN")
        return (UNKNOWN, None)
    if pinfo is None:
        return (UNKNOWN, None)
    try:
        ent = pinfo.GetEntity(schema)
    except Exception:
        import traceback
        _log("read: GetEntity FAILED -> UNKNOWN\n" + traceback.format_exc())
        return (UNKNOWN, None)
    if ent is None or not ent.IsValid():
        return (UNSET, None)          # clean absence
    try:
        path = ent.Get[System.String](_FIELD_FOLDER)
    except Exception:
        import traceback
        _log("read: field read FAILED -> UNKNOWN\n" + traceback.format_exc())
        return (UNKNOWN, None)
    if path:
        return (BOUND, path)
    return (CLEARED, None)             # empty entity = explicit tombstone


def read_model(doc):
    """Read the model's binding as a (status, path) pair, status one of
    BOUND / UNSET / CLEARED / UNKNOWN. Never raises. This is the low-level
    truth; higher-level callers branch on the status so a FAILED read is
    never mistaken for a clean "not set"."""
    if doc is None:
        return (UNKNOWN, None)
    try:
        status, path = _read_entity(doc)
    except Exception:
        import traceback
        _log("read_model: unexpected\n" + traceback.format_exc())
        return (UNKNOWN, None)
    _log("read_model: {0} {1!r}".format(status, path))
    return (status, path)


def read_model_retry(doc, attempts=3):
    """read_model with a few retries, because the schema-registration/ACC
    reload transient that produces UNKNOWN is momentary. Returns the first
    non-UNKNOWN result, or the last UNKNOWN. No sleeps (IronPython/WPF UI
    thread) - just re-attempts, which re-runs Lookup now that a prior
    _schema(create=True) may have registered it."""
    status, path = read_model(doc)
    tries = 1
    while status == UNKNOWN and tries < attempts:
        status, path = read_model(doc)
        tries += 1
    return (status, path)


def model_folder(doc):
    """Back-compat convenience: the model's folder path or None. Returns the
    path ONLY on a confirmed BOUND read; UNSET/CLEARED/UNKNOWN all give None.
    Callers that must act on the distinction use read_model()."""
    status, path = read_model_retry(doc)
    return path if status == BOUND else None


def folder_for(doc):
    """Return this project's clash-data folder for DISPLAY/DATA, or None.
    Never raises. Never writes the model (see _set_folder for the only
    model-writing path).

    Precedence, driven by the three-state read:
      BOUND   -> the model's folder wins, always (team truth). Cache it.
      UNSET   -> the model genuinely has no binding: fall back to this
                 machine's registry (the close-without-save resilience case).
      CLEARED -> the team deliberately cleared it: show nothing, and never
                 let a stale local cache resurrect it.
      UNKNOWN -> the read FAILED. Fall back to the registry so the tool isn't
                 blank, but this is DISPLAY-ONLY: we do not cache it and no
                 code path ever writes it back to the model. Self-corrects on
                 the next good read."""
    status, path = read_model_retry(doc)
    if status == BOUND:
        remember_local(doc, path)
        _log("folder_for: -> {0!r} (model, BOUND)".format(path))
        return path
    if status == CLEARED:
        _log("folder_for: model CLEARED -> None")
        return None
    local = _local_folder(doc)
    _log("folder_for: model {0} -> registry {1!r}".format(status, local))
    return local


# ---------------------------------------------------------------------------
# Per-machine binding registry (pure file I/O; CPython-testable)
# ---------------------------------------------------------------------------

def _doc_keys(doc):
    """ALL candidate registry keys for this model, most-stable first (cloud,
    workshare-central, PathName). Returns [] for an unsaved/untitled doc.

    Reading tries every key so a binding written under one identity path
    still resolves after the 'primary' path pick changes - the exact way an
    earlier ACC-path change orphaned a saved mapping. Writing uses the first
    (most stable) key but ALSO back-fills the others."""
    if doc is None:
        return []
    try:
        from clash_core import project
        paths = project.all_identity_paths(doc)
        keys = []
        for p in paths:
            k = project.hash_path(p)
            if k and k not in keys:
                keys.append(k)
        return keys
    except Exception:
        try:
            from clash_core import project
            k = project.hash_path(getattr(doc, "PathName", "") or "")
            return [k] if k else []
        except Exception:
            return []


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
    """Record doc->folder in the per-machine registry under EVERY candidate
    key, so the mapping resolves no matter which identity path a later read
    uses. Best-effort; a failed write must never break a read path."""
    keys = _doc_keys(doc)
    if not keys or not folder:
        return
    try:
        import io
        import json
        reg = _read_registry()
        changed = False
        for key in keys:
            if (reg.get(key) or {}).get("folder") != folder:
                reg[key] = {"folder": folder}
                changed = True
        if not changed:
            return
        with io.open(_registry_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(reg, indent=2, sort_keys=True,
                               ensure_ascii=False))
    except Exception:
        pass


def forget_local(doc):
    """Drop this doc's registry entries (all candidate keys). Used when the
    binding is explicitly cleared, so the local cache stops answering."""
    keys = set(_doc_keys(doc))
    if not keys:
        return
    try:
        import io
        import json
        reg = _read_registry()
        removed = [k for k in list(reg.keys()) if k in keys]
        if not removed:
            return
        for k in removed:
            reg.pop(k, None)
        with io.open(_registry_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(reg, indent=2, sort_keys=True,
                               ensure_ascii=False))
    except Exception:
        pass


def _local_folder(doc):
    reg = _read_registry()
    for key in _doc_keys(doc):
        folder = (reg.get(key) or {}).get("folder")
        if folder:
            return folder
    return None


def write_binding(doc, folder_path):
    """Store `folder_path` on `doc`'s ProjectInformation. MUST be called
    inside an open Revit transaction (the caller owns it, because writing an
    entity mutates the document). Raises on failure (e.g. ProjectInformation
    borrowed by a teammate on a workshared model); the caller catches it and
    surfaces a friendly message.

    Deliberately does NOT touch the per-machine registry: a transaction can
    still roll back after this returns, which would poison the registry with
    a folder the model never actually got (review finding). The caller writes
    the registry with remember_local() only AFTER the transaction commits and
    a read-back confirms the write landed."""
    import System
    from Autodesk.Revit.DB.ExtensibleStorage import Entity
    schema = _schema(create=True)
    ent = Entity(schema)
    ent.Set[System.String](_FIELD_FOLDER, folder_path)
    doc.ProjectInformation.SetEntity(ent)
    return folder_path


def clear_binding(doc):
    """Explicitly clear this project's folder, team-wide. Writes an empty-path
    TOMBSTONE entity rather than deleting it, so a re-read is CLEARED (not
    UNSET): teammates whose local registry still holds the old folder will
    NOT resurrect it. MUST be called inside an open transaction (mutates the
    doc); the caller saves/syncs to publish. Also clears this machine's
    registry entries so the local cache can't answer for it either."""
    import System
    from Autodesk.Revit.DB.ExtensibleStorage import Entity
    schema = _schema(create=True)
    ent = Entity(schema)
    ent.Set[System.String](_FIELD_FOLDER, "")
    doc.ProjectInformation.SetEntity(ent)
    forget_local(doc)
    return None
