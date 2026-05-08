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


# How far (feet) to back the perspective camera off from the clash
# midpoint. Picked to give a "framed" view of typical MEP clashes —
# duct/pipe-scale geometry reads cleanly at 15 ft, you can see the
# clashing parts plus enough surrounding context to navigate.
_PERSPECTIVE_OFFSET_FEET = 15.0


def make_pending_fly_to(clash_dict, viewpoint_dict):
    """Build the command dict the Browser writes when the user clicks
    Walkthrough Here.

    The saved viewpoint was captured from an ISOMETRIC navigator view
    where the camera position is conceptually "far away in iso
    projection space" — geometrically valid for that iso view but
    visually disorienting when applied verbatim to a PERSPECTIVE
    walkthrough camera (you teleport into a weird spot looking at the
    iso angle, often inside a wall or looking away from the clash).

    Instead, we derive a perspective-friendly camera from:
      * The clash midpoint (where the action is) — used as the look-at
        point.
      * The saved viewpoint's direction (target - position normalized)
        — used as the *approach angle* so the camera sees the clash
        from roughly the same orientation the iso view did.
      * `_PERSPECTIVE_OFFSET_FEET` standoff distance — places the camera
        back from the midpoint along that approach direction.

    Result: the user lands ~15 ft from the clash, looking at it from a
    sensible angle. They can then WASD/look around to inspect.

    Falls back to copying the saved viewpoint verbatim if the clash
    has no midpoint (older data shouldn't happen — every detected clash
    gets one — but defend against it). Returns None if the saved
    viewpoint is missing/malformed.
    """
    if not viewpoint_dict:
        return None
    cam = viewpoint_dict.get('camera') or {}
    saved_pos = cam.get('position')
    saved_tgt = cam.get('target')
    saved_up  = cam.get('up')
    if not (_is_xyz(saved_pos) and _is_xyz(saved_tgt) and _is_xyz(saved_up)):
        return None

    clash = clash_dict or {}
    midpoint = clash.get('midpoint')

    if _is_xyz(midpoint):
        # Saved viewpoint's direction = (target - position) normalized.
        # That's the direction the iso camera was looking IN. We want
        # the perspective camera to look the same direction, so we
        # back the camera off ALONG THE OPPOSITE direction from the
        # midpoint — i.e., midpoint - offset * direction.
        dx = float(saved_tgt[0]) - float(saved_pos[0])
        dy = float(saved_tgt[1]) - float(saved_pos[1])
        dz = float(saved_tgt[2]) - float(saved_pos[2])
        dlen = (dx * dx + dy * dy + dz * dz) ** 0.5
        if dlen < 1e-9:
            # Saved camera + target collapsed (shouldn't happen, but
            # guard). Use a default approach from +X.
            dx, dy, dz = 1.0, 0.0, 0.0
        else:
            dx, dy, dz = dx / dlen, dy / dlen, dz / dlen
        new_pos = [
            float(midpoint[0]) - _PERSPECTIVE_OFFSET_FEET * dx,
            float(midpoint[1]) - _PERSPECTIVE_OFFSET_FEET * dy,
            float(midpoint[2]) - _PERSPECTIVE_OFFSET_FEET * dz,
        ]
        new_tgt = [float(midpoint[0]), float(midpoint[1]), float(midpoint[2])]
    else:
        new_pos = [float(saved_pos[0]), float(saved_pos[1]), float(saved_pos[2])]
        new_tgt = [float(saved_tgt[0]), float(saved_tgt[1]), float(saved_tgt[2])]

    return {
        "schema_version": SCHEMA_VERSION,
        "queued_at":      models._now_iso(),
        "clash_id":       clash.get('id'),
        "clash_seq":      clash.get('seq'),
        "camera": {
            "position": new_pos,
            "target":   new_tgt,
            "up":       [float(saved_up[0]), float(saved_up[1]),
                         float(saved_up[2])],
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
