# -*- coding: utf-8 -*-
"""Saved filter presets for the Clash Browser.

A preset is a snapshot of the Browser's filter card state — which Trade
checkboxes are on, which Status checkboxes are on, what's in the Test
dropdown, what's in the Search box. Click a saved preset → the Browser
restores those filter settings.

Two kinds of presets:
  * Built-in (BUILT_IN_PRESETS, defined below) — always present, can't
    be edited or deleted, ship with the tool. Cover common workflows
    (active clashes, single-trade focus, resolved view).
  * User-saved — named snapshots the user creates via the "+ Save
    current as preset" button. Persisted per-machine at
    %APPDATA%/dbHMS_clash/filter_presets.json (matches config.py's
    location pattern), so they follow the user across projects rather
    than being tied to one model.

Pure data — no Revit / WPF imports. The Browser script handles the
WPF side (reading checkbox states into a preset dict, writing a preset
dict back into the checkboxes); this module just owns the preset shape
+ persistence.

Schema (preset dict):
    {
        "id":         "preset-<10char-hex>",     # synthetic id, stable across saves
        "name":       "Plumbing only — open",    # user-visible label
        "trades":     ["Plumbing"] or None,      # None means "select all"
        "statuses":   ["Open", "Reviewed"] or None,
        "test":       "MEP vs Architecture" or "(All tests)",
        "search":     "duct" or "",
        "created_at": "2026-05-06T14:30:00Z",
        "builtin":    False,                     # True for BUILT_IN_PRESETS
    }
"""

import codecs
import json
import os
import uuid as _uuid

from clash_core import models  # _now_iso
from clash_core.config import _appdata_root, CONFIG_DIR_NAME


PRESETS_FILE_NAME = "filter_presets.json"
SCHEMA_VERSION    = 1


# ---------------------------------------------------------------------------
# Built-in presets — always available, can't be deleted
# ---------------------------------------------------------------------------

# Note: `trades=None` and `statuses=None` mean "set every checkbox on" — the
# Browser interprets this as "no filter on this dimension." Useful for
# presets that only constrain one filter (e.g. "Mechanical only" doesn't
# care about status, so leaves it default).
BUILT_IN_PRESETS = [
    {
        "id":         "builtin-active",
        "name":       "Active only (Open + Reviewed)",
        "trades":     None,
        "statuses":   ["Open", "Reviewed"],
        "test":       "(All tests)",
        "search":     "",
        "builtin":    True,
    },
    {
        "id":         "builtin-mechanical",
        "name":       "Mechanical only",
        "trades":     ["Mechanical"],
        "statuses":   ["Open", "Reviewed"],
        "test":       "(All tests)",
        "search":     "",
        "builtin":    True,
    },
    {
        "id":         "builtin-resolved",
        "name":       "Resolved only",
        "trades":     None,
        "statuses":   ["Resolved"],
        "test":       "(All tests)",
        "search":     "",
        "builtin":    True,
    },
]


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

def make_preset(name, trades=None, statuses=None, test=None, search=None,
                created_at=None):
    """Build a fresh preset dict from plain values.

    `trades` / `statuses` accept None (no filter on this dimension), an
    empty list (filter everything out — unusual but valid), or a list of
    selected values. `test` defaults to "(All tests)" sentinel; `search`
    defaults to empty.
    """
    return {
        "id":         "preset-" + _uuid.uuid4().hex[:10],
        "name":       (name or "Untitled preset").strip() or "Untitled preset",
        "trades":     list(trades) if trades is not None else None,
        "statuses":   list(statuses) if statuses is not None else None,
        "test":       test or "(All tests)",
        "search":     search or "",
        "created_at": created_at or models._now_iso(),
        "builtin":    False,
    }


# ---------------------------------------------------------------------------
# Persistence — per-machine JSON at %APPDATA%/dbHMS_clash/filter_presets.json
# ---------------------------------------------------------------------------

def presets_path():
    """Full path to the user-saved presets file."""
    d = os.path.join(_appdata_root(), CONFIG_DIR_NAME)
    if not os.path.isdir(d):
        os.makedirs(d)
    return os.path.join(d, PRESETS_FILE_NAME)


def read_user_presets():
    """Return the list of user-saved preset dicts (empty list if none).

    Missing or corrupt file returns []. We don't raise — the Browser
    treats "no saved presets" and "couldn't read presets" the same way
    (just shows the built-ins).
    """
    path = presets_path()
    if not os.path.isfile(path):
        return []
    try:
        with codecs.open(path, "r", "utf-8") as f:
            data = json.load(f)
    except (IOError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    presets = data.get("presets") or []
    if not isinstance(presets, list):
        return []
    return [p for p in presets if isinstance(p, dict)]


def write_user_presets(presets):
    """Atomically write the list of user presets to disk.

    Writes to <path>.tmp then renames onto the real path so a crash
    mid-write can't leave truncated JSON behind. Same pattern as
    clash_core/config.py / clash_core/persistence.py.
    """
    presets = [p for p in (presets or []) if isinstance(p, dict)]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "presets":        presets,
    }
    path = presets_path()
    tmp = path + ".tmp"
    text = json.dumps(payload, indent=2, sort_keys=True)
    with codecs.open(tmp, "w", "utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def append_user_preset(preset):
    """Convenience: read, append, write. Returns the updated full list."""
    presets = read_user_presets()
    presets.append(preset)
    write_user_presets(presets)
    return presets


def delete_user_preset(preset_id):
    """Remove the preset with `preset_id` from the user file. Returns True
    if a preset was removed, False if not found (or if the id was a
    built-in)."""
    if not preset_id:
        return False
    presets = read_user_presets()
    keep = [p for p in presets if p.get("id") != preset_id]
    if len(keep) == len(presets):
        return False
    write_user_presets(keep)
    return True


def all_presets():
    """Built-in presets first, then user-saved. The Browser populates its
    button list from this single combined order so built-ins always
    appear at the top."""
    return list(BUILT_IN_PRESETS) + read_user_presets()
