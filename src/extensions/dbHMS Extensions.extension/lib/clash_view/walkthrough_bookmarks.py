# -*- coding: utf-8 -*-
"""Saved camera bookmarks for the Walkthrough.

A bookmark is a named camera state — position, forward, up. Click a
bookmark in the Walkthrough form → the camera jumps to that pose
(eased flight via clash_view.walkthrough_camera if the source and
destination differ enough; otherwise instant snap).

Persistence is **per-project, on the shared root** — same idea as
clashes.json. The whole team sees the same bookmark set, so a meeting
moderator can prep "RTU-1", "Lobby", "Mech room corridor" the night
before and click through them on the call.

Path: `<shared>/<project-hash>/walkthrough_bookmarks.json`. Atomic-
rename write so a crash mid-write can't truncate the file.

Pure data — no Revit, no WPF. Lives in clash_view because conceptually
it's view-side state; the persistence helpers reuse `clash_core.persistence`
for path resolution.

Schema (single bookmark):
    {
        "id":         "bm-<10char-hex>",     # synthetic, stable across renames
        "name":       "RTU-1 east",          # user-visible
        "camera": {
            "position": [x, y, z],            # world coords, feet
            "forward":  [x, y, z],            # unit vector
            "up":       [x, y, z],            # unit vector
        },
        "created_by": "nathaniel",
        "created_at": "2026-05-06T14:30:00Z",
    }
"""

import codecs
import json
import os
import uuid as _uuid

from clash_core import models, persistence


BOOKMARKS_FILE_NAME = "walkthrough_bookmarks.json"
SCHEMA_VERSION       = 1


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

def make_bookmark(name, position, forward, up, created_by=None,
                  created_at=None, bookmark_id=None):
    """Build a fresh bookmark dict from plain values.

    `position` / `forward` / `up` are 3-element float lists.
    `name` is stripped + falls back to "Untitled bookmark" if empty.
    """
    return {
        "id":         bookmark_id or "bm-" + _uuid.uuid4().hex[:10],
        "name":       (name or "Untitled bookmark").strip() or "Untitled bookmark",
        "camera": {
            "position": [float(position[0]), float(position[1]),
                         float(position[2])],
            "forward":  [float(forward[0]),  float(forward[1]),
                         float(forward[2])],
            "up":       [float(up[0]),       float(up[1]),
                         float(up[2])],
        },
        "created_by": created_by,
        "created_at": created_at or models._now_iso(),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def bookmarks_path(project_hash):
    """Full path to the bookmarks JSON file for `project_hash`."""
    return os.path.join(persistence.project_dir(project_hash),
                        BOOKMARKS_FILE_NAME)


def read_bookmarks(project_hash):
    """Return the list of bookmark dicts for this project (empty list if
    none / corrupt / missing). Defensive — same "missing == empty"
    behavior as clash_core.filter_presets.
    """
    if not project_hash:
        return []
    path = bookmarks_path(project_hash)
    if not os.path.isfile(path):
        return []
    try:
        with codecs.open(path, "r", "utf-8") as f:
            data = json.load(f)
    except (IOError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    bookmarks = data.get("bookmarks") or []
    if not isinstance(bookmarks, list):
        return []
    return [b for b in bookmarks if isinstance(b, dict)]


def write_bookmarks(project_hash, bookmarks):
    """Atomically write the bookmark list to disk. Same pattern as
    clash_core.filter_presets.write_user_presets — write to .tmp then
    rename onto the real path so a crash mid-write can't truncate.
    """
    if not project_hash:
        return
    bookmarks = [b for b in (bookmarks or []) if isinstance(b, dict)]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "bookmarks":      bookmarks,
    }
    path = bookmarks_path(project_hash)
    tmp = path + ".tmp"
    text = json.dumps(payload, indent=2, sort_keys=True)
    with codecs.open(tmp, "w", "utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def append_bookmark(project_hash, bookmark):
    """Read, append, write. Returns the updated full list."""
    bookmarks = read_bookmarks(project_hash)
    bookmarks.append(bookmark)
    write_bookmarks(project_hash, bookmarks)
    return bookmarks


def delete_bookmark(project_hash, bookmark_id):
    """Remove the bookmark with `bookmark_id`. Returns True if removed,
    False if not found."""
    if not project_hash or not bookmark_id:
        return False
    bookmarks = read_bookmarks(project_hash)
    keep = [b for b in bookmarks if b.get("id") != bookmark_id]
    if len(keep) == len(bookmarks):
        return False
    write_bookmarks(project_hash, keep)
    return True


def rename_bookmark(project_hash, bookmark_id, new_name):
    """Update the name of the bookmark with `bookmark_id`. Returns True
    if updated, False if not found / new_name empty.

    Same name-handling rules as `make_bookmark`: strip, fall back to
    "Untitled bookmark" if blank.
    """
    if not project_hash or not bookmark_id:
        return False
    cleaned = (new_name or "").strip() or "Untitled bookmark"
    bookmarks = read_bookmarks(project_hash)
    found = False
    for b in bookmarks:
        if b.get("id") == bookmark_id:
            b["name"] = cleaned
            found = True
            break
    if not found:
        return False
    write_bookmarks(project_hash, bookmarks)
    return True
