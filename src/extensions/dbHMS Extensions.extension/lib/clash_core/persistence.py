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
            "Open Settings (Clash Detection tab) and pick a folder."
        )
    return root


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _atomic_write(path, text):
    """Write `text` to <path>.tmp then rename onto path."""
    tmp = path + ".tmp"
    with codecs.open(tmp, "w", "utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def _read_json(path, default):
    """Read JSON from `path`; return `default` on missing-or-corrupt."""
    if not os.path.isfile(path):
        return default
    try:
        with codecs.open(path, "r", "utf-8") as f:
            data = json.load(f)
        return data
    except (IOError, ValueError):
        return default


def _write_json(path, data):
    _ensure_dir(os.path.dirname(path))
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Project-scoped paths
# ---------------------------------------------------------------------------

def project_dir(project_hash):
    """Return <shared_root>/<project-hash>/, creating it if missing."""
    return _ensure_dir(os.path.join(_require_shared_root(), project_hash))


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
    return os.path.join(project_dir(project_hash), "clashes.json")


def project_meta_path(project_hash):
    return os.path.join(project_dir(project_hash), "project.json")


def overrides_path(project_hash):
    return os.path.join(project_dir(project_hash), "test_overrides.json")


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
    """Return the clashes.json content; empty schema if missing."""
    return _read_json(clashes_path(project_hash), default={
        "schema_version": SCHEMA_VERSION,
        "project_hash":   project_hash,
        "last_run_at":    None,
        "tests_run":      [],
        "clashes":        [],
    })


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
