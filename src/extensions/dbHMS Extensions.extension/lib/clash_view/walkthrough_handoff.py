# -*- coding: utf-8 -*-
"""Cross-script handoff for "Walkthrough Here" — Browser → Walkthrough.

Browser is modal (blocks while the user reviews a clash); Walkthrough is
modeless (a separate form that may or may not be open at any moment).
We can't directly call methods on the Walkthrough's WPFWindow from the
Browser (they're in different IronPython script scopes), so we use a
file as the communication channel:

  1. Browser's "Walkthrough Here" handler writes a tiny JSON command
     file at `<shared>/<project-hash>/walkthrough_pending.json` with
     the target clash's camera state.
  2. Walkthrough form, on `_open_view` completion AND via a slow
     polling timer (every ~2s while running), reads + consumes the
     file. On consume it queues a `set_camera` action and deletes
     the file.

The dual entry point handles both states cleanly:
  * **Form not yet open** — user clicks Walkthrough Here, gets prompted
    to open the Walkthrough button, on launch the file is consumed.
  * **Form already open** — user clicks Walkthrough Here, the running
    form's polling timer picks up the file within ~2s and flies there.

Last-write-wins: if the user clicks Walkthrough Here on multiple
clashes in succession, the file gets overwritten each time. Whoever
the most recent click was for is where the camera lands. Intuitive.

Pure-data — no Revit, no WPF. Atomic-rename writes (write to .tmp,
rename onto path) so a crash mid-write can't truncate.

Schema (single command):
    {
        "schema_version": 1,
        "queued_at":  "2026-05-07T12:00:00Z",
        "clash_id":   "clash-abc",
        "clash_seq":  42,
        "camera": {
            "position": [x, y, z],   # world coords, feet
            "target":   [x, y, z],   # eye + forward unit, world coords
            "up":       [x, y, z],   # unit vector
        }
    }
"""

import codecs
import json
import os

from clash_core import models, persistence


PENDING_FILE_NAME = "walkthrough_pending.json"
SCHEMA_VERSION    = 1


def pending_path(project_hash):
    """Full path to the pending-command file for `project_hash`."""
    return os.path.join(persistence.project_dir(project_hash),
                        PENDING_FILE_NAME)


def make_pending_fly_to(clash_dict, viewpoint_dict):
    """Build the command dict the Browser writes when the user clicks
    Walkthrough Here.

    `viewpoint_dict` is the saved viewpoint shape from
    `clash_core.models.make_viewpoint` — has `camera.position`,
    `camera.target`, `camera.up`. We copy those through verbatim;
    the Walkthrough side translates target → forward unit at consume
    time.

    Returns None if the clash has no usable viewpoint (no save yet,
    or malformed). Caller treats None as "can't fly there — tell the
    user to save a viewpoint first."
    """
    if not viewpoint_dict:
        return None
    cam = viewpoint_dict.get('camera') or {}
    pos = cam.get('position')
    tgt = cam.get('target')
    up  = cam.get('up')
    if not (_is_xyz(pos) and _is_xyz(tgt) and _is_xyz(up)):
        return None
    clash = clash_dict or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "queued_at":      models._now_iso(),
        "clash_id":       clash.get('id'),
        "clash_seq":      clash.get('seq'),
        "camera": {
            "position": [float(pos[0]), float(pos[1]), float(pos[2])],
            "target":   [float(tgt[0]), float(tgt[1]), float(tgt[2])],
            "up":       [float(up[0]),  float(up[1]),  float(up[2])],
        },
    }


def write_pending(project_hash, command_dict):
    """Atomically write the pending command. Overwrites any existing
    file (last-write-wins).

    Returns True on success, False on any I/O failure.
    """
    if not project_hash or not command_dict:
        return False
    path = pending_path(project_hash)
    tmp  = path + ".tmp"
    try:
        text = json.dumps(command_dict, indent=2, sort_keys=True)
        with codecs.open(tmp, "w", "utf-8") as f:
            f.write(text)
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp, path)
        return True
    except Exception:
        return False


def read_pending(project_hash):
    """Return the pending command dict, or None if no file / corrupt /
    malformed. Defensive — same "missing == None" behavior as
    filter_presets.
    """
    if not project_hash:
        return None
    path = pending_path(project_hash)
    if not os.path.isfile(path):
        return None
    try:
        with codecs.open(path, "r", "utf-8") as f:
            data = json.load(f)
    except (IOError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cam = data.get('camera') or {}
    if not (_is_xyz(cam.get('position'))
            and _is_xyz(cam.get('target'))
            and _is_xyz(cam.get('up'))):
        return None
    return data


def clear_pending(project_hash):
    """Delete the pending command file. Called by the Walkthrough side
    after consuming. Safe to call even if no file exists — silently
    no-ops.
    """
    if not project_hash:
        return
    path = pending_path(project_hash)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


def viewpoint_to_camera_tuple(command_dict):
    """Translate a pending command's viewpoint shape to the
    (position, forward, up) tuple `walkthrough_motion` and
    `walkthrough_view.set_camera` expect.

    Viewpoints store TARGET (a world-coord look-at point); the motion
    module uses FORWARD (a unit direction). forward = normalize(target - position).

    Returns (position, forward, up) lists of floats, or None if the
    command's geometry is degenerate (target == position, so forward
    would be zero-length).
    """
    if not command_dict:
        return None
    cam = command_dict.get('camera') or {}
    pos = cam.get('position')
    tgt = cam.get('target')
    up  = cam.get('up')
    if not (_is_xyz(pos) and _is_xyz(tgt) and _is_xyz(up)):
        return None
    fx = float(tgt[0]) - float(pos[0])
    fy = float(tgt[1]) - float(pos[1])
    fz = float(tgt[2]) - float(pos[2])
    flen = (fx * fx + fy * fy + fz * fz) ** 0.5
    if flen < 1e-9:
        return None
    return (
        [float(pos[0]), float(pos[1]), float(pos[2])],
        [fx / flen, fy / flen, fz / flen],
        [float(up[0]),  float(up[1]),  float(up[2])],
    )


def _is_xyz(v):
    if v is None:
        return False
    try:
        return len(v) >= 3
    except TypeError:
        return False
