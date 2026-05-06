# -*- coding: utf-8 -*-
"""Convert input deltas into Revit 3D View camera updates.

The 3D View's orientation is controlled via View3D.SetOrientation(ViewOrientation3D),
which takes a ViewOrientation3D(EyePosition, UpDirection, ForwardDirection).

We apply small per-frame deltas to that orientation:
  * Translation:    WASD or left stick + Q/E or LB/RB for vertical
  * Rotation:       Mouse drag or right stick (yaw + pitch)
  * Speed modifier: Shift / left trigger to speed up, Ctrl / right trigger
                    to slow down

A camera state dict is mutated in-place and pushed to the View via
apply_to_view() at the end of each frame.
"""

# Defaults; tunable later from Settings
DEFAULT_MOVE_SPEED_FT_PER_SEC = 8.0
DEFAULT_ROT_SPEED_DEG_PER_SEC = 90.0


def empty_camera_state():
    """Return a fresh mutable camera state with sensible defaults."""
    raise NotImplementedError


def apply_keyboard_input(state, keys_held, dt_seconds):
    """Mutate `state` based on which keys are currently held.
    `keys_held` is a set of System.Windows.Input.Key values."""
    raise NotImplementedError


def apply_mouse_delta(state, dx_pixels, dy_pixels):
    """Apply a mouse-look delta to yaw/pitch in `state`."""
    raise NotImplementedError


def apply_gamepad_state(state, gamepad, dt_seconds):
    """Apply Xbox controller input (stick + trigger + button held state)
    to `state`. `gamepad` is the dict returned by xinput.poll()."""
    raise NotImplementedError


def apply_to_view(view, state):
    """Push `state` into the 3D View by calling SetOrientation()."""
    raise NotImplementedError


def look_at(state, target_xyz, distance_ft):
    """Reposition the camera to look at `target_xyz` from `distance_ft`
    away, preserving the current viewing direction. Used by clash navigator
    mode to jump to a clash."""
    raise NotImplementedError
