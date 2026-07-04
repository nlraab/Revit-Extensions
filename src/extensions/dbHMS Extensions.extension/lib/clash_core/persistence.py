# -*- coding: utf-8 -*-
"""JSON read / write for clash data, with atomic-rename semantics.

Layout under the configured shared root:

    <shared_root>/
        global/
            test_library.json       # firm-wide clash test definitions
        <project-hash>/
            project.json            # display name, disciplines, central-model path,
                                    # link role map
            clashes.json            # the project's full clash database
            test_overrides.json     # per-project tweaks to the global library
            viewpoints/
                <clash-id>__<viewpoint-id>.png

Every write goes via _atomic_write: write to a sibling temp file, then rename.
This survives a Revit crash mid-save without leaving truncated JSON behind.

`shared_root` is read from `clash_core.config` on demand. If it's not
configured (first run), every read/write helper raises a clear error so the
caller can route the user to Settings.
"""

import codecs
import json
import os

from clash_core import config


SCHEMA_VERSION = 1


class SharedFolderNotConfigured(Exception):
    """Raised when persistence is called before the shared folder is set up.

    Callers (Run Clash Test, Browser, etc.) should catch this and route the
    user to the Settings tool instead of letting the traceback escape.
    """
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_shared_root():
    root = config.shared_root()
    if not root:
        raise SharedFolderNotConfigured(
            "Shared clash-data folder is not configured. "
            "Open Settings (Clash Detection panel on the dbHMS Tools tab) and pick a folder."
        )
    return root


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _atomic_write(path, text):
    """Write `text` to <path>.tmp then swap it onto path.

    The swap prefers os.replace (atomic on Windows; CPython 3, the test
    runtime). IronPython 2.7 has no os.replace, so it falls back to a
    bak-swap: the old file is renamed ASIDE before the new one lands. The
    old remove-then-rename had a window where a crash left NO file at all
    (the data invisible in .tmp), and the per-keystroke clash edits from
    the 3D review cockpit multiply how often that window is open. With the
    bak-swap, the worst case leaves <path>.bak, which _read_json recovers
    from."""
    tmp = path + ".tmp"
    with codecs.open(tmp, "w", "utf-8") as f:
        f.write(text)
    replace = getattr(os, "replace", None)
    if replace is not None:
        replace(tmp, path)
        return
    bak = path + ".bak"
    try:
        if os.path.exists(bak):
            os.remove(bak)
    except OSError:
        pass
    if os.path.exists(path):
        os.rename(path, bak)
    os.rename(tmp, path)
    try:
        if os.path.exists(bak):
            os.remove(bak)
    except OSError:
        pass


def _read_json(path, default):
    """Read JSON from `path`; return `default` on missing-or-corrupt.

    A file that parses but has the wrong top-level shape (e.g. a top-level list,
    string, or number in a hand-edited or foreign file dropped into the now
    user-picked clash folder) is treated as absent when a dict is expected, so
    the readers below can safely do `data["..."]` / `data.get(...)`."""
    if not os.path.isfile(path):
        # Recover from an interrupted bak-swap write: the main file vanished
        # mid-swap but the previous version survives as .bak.
        bak = path + ".bak"
        if os.path.isfile(bak):
            path = bak
        else:
            return default
    try:
        with codecs.open(path, "r", "utf-8") as f:
            data = json.load(f)
    except (IOError, ValueError):
        return default
    if isinstance(default, dict) and not isinstance(data, dict):
        return default
    return data


def _write_json(path, data):
    # ensure_ascii=False: IronPython 2.7's json.dumps raises on non-ASCII
    # under the default (the documented repo gotcha). Refs now carry
    # user-typed system/level names, so unicode is expected, and
    # _atomic_write / _read_json already round-trip UTF-8.
    _ensure_dir(os.path.dirname(path))
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True,
                                   ensure_ascii=False))


# ---------------------------------------------------------------------------
# Project-scoped paths
# ---------------------------------------------------------------------------

def project_dir(project_hash):
    """Return <shared_root>/<project-key>/, creating it if missing.

    Use this for WRITE / asset paths (viewpoints, share, reports) that
    legitimately need the folder to exist. The read helpers below go through
    `_project_dir_path` (non-creating) instead, so merely opening a tool on an
    unlinked project never litters the shared drive with an empty folder."""
    return _ensure_dir(os.path.join(_require_shared_root(), project_hash))


def _project_dir_path(project_hash):
    """Compute <shared_root>/<project-key>/ WITHOUT creating it. The read
    path-builders use this: `_read_json` already returns the default when the
    file is missing regardless of whether the parent dir exists, and every
    writer self-ensures the directory via `_write_json`, so nothing needs the
    side-effecting mkdir on a read."""
    return os.path.join(_require_shared_root(), project_hash)


def viewpoints_dir(project_hash):
    return _ensure_dir(os.path.join(project_dir(project_hash), "viewpoints"))


def viewpoint_image_path(project_hash, clash_id):
    """Deterministic on-disk path for a clash's viewpoint thumbnail.

    Single viewpoint per clash for v1 — file name is just the clash id,
    so saving a new viewpoint overwrites the previous one in place. If
    we ever go multi-viewpoint, the convention should change to include
    the viewpoint id (and a small migration would copy the existing
    file to its new name).
    """
    return os.path.join(viewpoints_dir(project_hash), "{}.png".format(clash_id))


def clashes_path(project_hash):
    return os.path.join(_project_dir_path(project_hash), "clashes.json")


def project_meta_path(project_hash):
    return os.path.join(_project_dir_path(project_hash), "project.json")


def overrides_path(project_hash):
    return os.path.join(_project_dir_path(project_hash), "test_overrides.json")


# ---------------------------------------------------------------------------
# Global-scoped paths
# ---------------------------------------------------------------------------

def global_dir():
    """Return <shared_root>/global/, creating it if missing."""
    return _ensure_dir(os.path.join(_require_shared_root(), "global"))


def global_test_library_path():
    return os.path.join(global_dir(), "test_library.json")


# ---------------------------------------------------------------------------
# Read / write API
# ---------------------------------------------------------------------------

def read_clashes(project_hash):
    """Return the clashes.json content; empty schema if missing.

    `groups` is the Layer C issue list (lib/clash_group). It lives INSIDE
    clashes.json so one atomic write covers members + rosters + statuses.
    WARNING to future writers: both run pipelines rebuild this file's top
    level from a literal - any writer that drops the `groups` key silently
    wipes every named group (integrity-tested in tests/test_clash_group)."""
    data = _read_json(clashes_path(project_hash), default={
        "schema_version": SCHEMA_VERSION,
        "project_hash":   project_hash,
        "last_run_at":    None,
        "tests_run":      [],
        "clashes":        [],
        "groups":         [],
    })
    if "groups" not in data:
        data["groups"] = []
    return data


def write_clashes(project_hash, data):
    _write_json(clashes_path(project_hash), data)


def read_global_test_library():
    """Return the firm-wide test library; empty if missing.

    The first-run seeding (copying default_tests.json into this file) is
    the Test Library tool's responsibility, not ours.
    """
    return _read_json(global_test_library_path(), default={
        "$schema_version": SCHEMA_VERSION,
        "tests":           [],
    })


def write_global_test_library(data):
    _write_json(global_test_library_path(), data)


def read_overrides(project_hash):
    return _read_json(overrides_path(project_hash), default={
        "schema_version":    SCHEMA_VERSION,
        "disabled_test_ids": [],
        "custom_tests":      [],
    })


def write_overrides(project_hash, data):
    _write_json(overrides_path(project_hash), data)


def read_project_meta(project_hash):
    return _read_json(project_meta_path(project_hash), default={
        "schema_version": SCHEMA_VERSION,
        "project_hash":   project_hash,
        "display_name":   None,
        "disciplines":    [],
        "link_role_map":  {},
        "warn_threshold": None,
    })


def write_project_meta(project_hash, data):
    _write_json(project_meta_path(project_hash), data)


# ---------------------------------------------------------------------------
# Folder-path API (the kept tools: Coordination + 3D Viewer)
# ---------------------------------------------------------------------------
#
# The current model stores ONE absolute clash-data folder path in the Revit
# model (clash_core.binding). These helpers read/write directly inside that
# folder -- no shared root, no per-project subfolder, no hash. `_read_json`
# returns the default when a file is missing (so an empty/new folder reads as an
# empty project) and `_write_json` ensures the folder exists on write, so a read
# never creates anything on disk. Pass the absolute folder path the user picked.

def clashes_path_at(folder):
    return os.path.join(folder, "clashes.json")


def project_meta_path_at(folder):
    return os.path.join(folder, "project.json")


def overrides_path_at(folder):
    return os.path.join(folder, "test_overrides.json")


def viewpoints_dir_at(folder):
    """<folder>/viewpoints/, created on demand (a write path)."""
    return _ensure_dir(os.path.join(folder, "viewpoints"))


def read_clashes_at(folder):
    """clashes.json inside `folder`; empty schema (incl. `groups`) if missing.
    See read_clashes for why `groups` must never be dropped."""
    data = _read_json(clashes_path_at(folder), default={
        "schema_version": SCHEMA_VERSION,
        "last_run_at":    None,
        "tests_run":      [],
        "clashes":        [],
        "groups":         [],
    })
    if "groups" not in data:
        data["groups"] = []
    return data


def write_clashes_at(folder, data):
    _write_json(clashes_path_at(folder), data)


def read_project_meta_at(folder):
    return _read_json(project_meta_path_at(folder), default={
        "schema_version": SCHEMA_VERSION,
        "display_name":   None,
        "disciplines":    [],
        "link_role_map":  {},
        "warn_threshold": None,
    })


def write_project_meta_at(folder, data):
    _write_json(project_meta_path_at(folder), data)


def read_overrides_at(folder):
    return _read_json(overrides_path_at(folder), default={
        "schema_version":    SCHEMA_VERSION,
        "disabled_test_ids": [],
        "custom_tests":      [],
    })


def write_overrides_at(folder, data):
    _write_json(overrides_path_at(folder), data)
