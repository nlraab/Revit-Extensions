# -*- coding: utf-8 -*-
"""Free-fly motion math for the Walkthrough.

Pure data. Operates on plain camera-state tuples — no Revit, no WPF. The
Walkthrough script polls keyboard / mouse on a DispatcherTimer (~30 Hz)
and feeds the deltas into `step()` / `look()` here; the resulting
orientation gets pushed to the Revit View3D via SetOrientation.

Camera state is a tuple of three 3-vectors:
  (position, forward, up)

All vectors are lists of three floats. Forward and up are kept unit-
length; the right vector is computed on demand as `forward × up` (and
re-normalized — for perspective views the user's `up` may not be
exactly perpendicular to forward).

Coordinate convention: Revit's world is Z-up. By default Q/E move
along world +Z / -Z (so "up" always means "toward the sky" regardless
of where the camera is pointing — what feels right when flying around
a building). Optional camera-relative vertical movement (Q/E moves
along the camera's local up) is available via `world_up=False` in
`step()`.

Yaw rotates the forward vector around world Z (so the horizon stays
horizontal — no roll). Pitch rotates forward + up around the right
vector and is clamped to ±88° so the camera can't flip upside down.
"""

import math


# Movement keys → unit direction in camera-local space.
# Rebound at runtime if we ever support customization.
KEY_FORWARD  = "forward"
KEY_BACKWARD = "backward"
KEY_LEFT     = "left"
KEY_RIGHT    = "right"
KEY_UP       = "up"
KEY_DOWN     = "down"

# Pitch clamp — Revit accepts straight-up / straight-down orientations
# but the resulting view loses its frame of reference (the world spins
# around you). 88° leaves enough headroom to look at a ceiling without
# losing the horizon.
MAX_PITCH_DEG = 88.0


# ---------------------------------------------------------------------------
# Vector helpers (3-vectors as plain Python lists of floats)
# ---------------------------------------------------------------------------

def _length(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _normalize(v):
    n = _length(v)
    if n < 1e-9:
        return [0.0, 0.0, 1.0]
    return [v[0] / n, v[1] / n, v[2] / n]


def _add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _scale(v, s):
    return [v[0] * s, v[1] * s, v[2] * s]


def _cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def right_vector(forward, up):
    """Return the right vector for a camera with the given forward + up.

    Right = forward × up, normalized. For a well-formed orientation
    (forward perpendicular to up) this gives a unit vector; for the
    "near-vertical" case where forward ≈ up we still get a sensible
    direction because the cross product of nearly-parallel vectors is
    small but the normalize step forces it to unit length (or falls
    back to +Z if zero).
    """
    return _normalize(_cross(forward, up))


# ---------------------------------------------------------------------------
# Movement (WASD + Q/E)
# ---------------------------------------------------------------------------

def step(camera, keys, speed_fps, dt_seconds, world_up=True):
    """Apply movement keys for one timestep, returning a new camera tuple.

    Args:
        camera        - (position, forward, up). All 3-element float lists.
        keys          - iterable of pressed-key names (KEY_FORWARD,
                        KEY_BACKWARD, etc.). A set is typical.
        speed_fps     - camera speed in feet-per-second. The script holds
                        this value at the user's slider setting; SHIFT /
                        CTRL multipliers are applied by the caller before
                        passing in (so this function just does linear
                        speed × dt).
        dt_seconds    - elapsed time since last step, in seconds. Caller
                        measures from the DispatcherTimer interval; using
                        a measured dt instead of a fixed assumption means
                        the camera moves the right *distance* per second
                        regardless of frame-rate hiccups.
        world_up      - True (default): Q/E moves along world +Z/-Z. False:
                        Q/E moves along the camera's local up. World-up
                        feels right for "fly around a building"; local-up
                        is what you'd want for a free orbital camera.

    Returns:
        (new_position, forward, up)  — forward + up unchanged; only
        position is updated. Look operations (look()) are what change
        forward + up.

    Empty `keys` returns the input position unchanged. Unknown key
    names are ignored.
    """
    position, forward, up = camera
    if not keys or speed_fps <= 0 or dt_seconds <= 0:
        return (list(position), list(forward), list(up))

    distance = float(speed_fps) * float(dt_seconds)

    # Movement vector accumulator in WORLD coordinates.
    move = [0.0, 0.0, 0.0]

    forward_xy = None  # cached forward projected onto XY plane (no pitch)

    if KEY_FORWARD in keys or KEY_BACKWARD in keys:
        # W/S follows the FULL 3D forward direction — looking up + W
        # flies up, looking down + W dives forward. Feels more natural
        # for free-fly inspection; Q/E is still available for pure
        # world-up/down motion when you don't want to tilt.
        forward_unit = _normalize(forward)
        if KEY_FORWARD in keys:
            move = _add(move, _scale(forward_unit, 1.0))
        if KEY_BACKWARD in keys:
            move = _add(move, _scale(forward_unit, -1.0))

    if KEY_LEFT in keys or KEY_RIGHT in keys:
        # Strafe stays HORIZONTAL — A/D following pitch produces weird
        # diagonal motion that's confusing in tight spaces. Standard FPS
        # convention: strafe along the right vector projected onto XY.
        forward_xy = _flatten_xy(forward)
        right_xy = _flatten_xy(_cross(forward_xy, [0.0, 0.0, 1.0]))
        if KEY_RIGHT in keys:
            move = _add(move, _scale(right_xy, 1.0))
        if KEY_LEFT in keys:
            move = _add(move, _scale(right_xy, -1.0))

    if KEY_UP in keys or KEY_DOWN in keys:
        if world_up:
            vertical = [0.0, 0.0, 1.0]
        else:
            vertical = list(up)
        if KEY_UP in keys:
            move = _add(move, _scale(vertical, 1.0))
        if KEY_DOWN in keys:
            move = _add(move, _scale(vertical, -1.0))

    # Normalize the combined direction so diagonal movement (W+D) doesn't
    # travel √2× faster than straight movement. Then scale by distance.
    move_len = _length(move)
    if move_len > 1e-9:
        move = _scale(move, distance / move_len)

    new_position = _add(position, move)
    return (new_position, list(forward), list(up))


def _flatten_xy(v):
    """Project a 3-vector onto the XY plane and renormalize. Used to
    keep WASD strictly horizontal regardless of the camera's pitch.
    """
    flat = [v[0], v[1], 0.0]
    n = _length(flat)
    if n < 1e-9:
        # Forward is straight up/down — no horizontal direction. Pick
        # +X arbitrarily; better than freezing the user's WASD.
        return [1.0, 0.0, 0.0]
    return [flat[0] / n, flat[1] / n, 0.0]


# ---------------------------------------------------------------------------
# Look (mouse drag → yaw + pitch)
# ---------------------------------------------------------------------------

def look(camera, dx_pixels, dy_pixels, sensitivity_deg_per_pixel=0.15):
    """Apply mouse-look deltas, returning a new camera tuple.

    Args:
        camera                    - (position, forward, up).
        dx_pixels                 - horizontal mouse delta (positive →
                                    look right). Caller is responsible
                                    for inverting if they want
                                    inverted-X mice.
        dy_pixels                 - vertical mouse delta (positive →
                                    look down, screen-space convention).
        sensitivity_deg_per_pixel - degrees of rotation per pixel of
                                    mouse movement. 0.15 ≈ Enscape default.
                                    Exposed so a future preference can
                                    tune it.

    Returns:
        (position, new_forward, new_up).  Position unchanged.

    Yaw is around world Z (so horizon stays level). Pitch is around the
    camera's right vector and is clamped so we can't flip upside down.
    Up vector is recomputed after pitch so it stays perpendicular to
    the new forward.
    """
    position, forward, up = camera
    if dx_pixels == 0 and dy_pixels == 0:
        return (list(position), list(forward), list(up))

    yaw_deg   = -float(dx_pixels) * float(sensitivity_deg_per_pixel)
    pitch_deg = -float(dy_pixels) * float(sensitivity_deg_per_pixel)

    # Yaw — rotate forward around world Z. Up isn't rotated (it stays
    # along world Z since we're a level-horizon camera).
    new_forward = _rotate_around_axis(forward, [0.0, 0.0, 1.0], yaw_deg)

    # Compute the right vector AFTER yaw so it's the post-yaw right.
    right = right_vector(new_forward, [0.0, 0.0, 1.0])

    # Pitch — clamp the requested pitch to the allowable range. Compute
    # current pitch (angle between forward and the XY plane) so we can
    # honor the clamp on the cumulative angle, not the per-frame delta.
    current_pitch = math.degrees(math.asin(
        max(-1.0, min(1.0, new_forward[2]))))
    desired_pitch = current_pitch + pitch_deg
    if desired_pitch > MAX_PITCH_DEG:
        pitch_deg = MAX_PITCH_DEG - current_pitch
    elif desired_pitch < -MAX_PITCH_DEG:
        pitch_deg = -MAX_PITCH_DEG - current_pitch

    if abs(pitch_deg) > 1e-9:
        new_forward = _rotate_around_axis(new_forward, right, pitch_deg)

    # Recompute up so it's perpendicular to new_forward.
    new_up = _normalize(_cross(right, new_forward))
    return (list(position), _normalize(new_forward), new_up)


def _rotate_around_axis(v, axis, angle_deg):
    """Rodrigues rotation: rotate vector v around unit axis by angle.

    Standard form. Axis must already be normalized; we don't re-normalize
    inside the hot path. For our two callers — world Z (always unit)
    and the freshly-computed right vector (unit by construction) — this
    is safe.
    """
    if abs(angle_deg) < 1e-9:
        return list(v)
    angle_rad = math.radians(angle_deg)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    one_minus_cos = 1.0 - cos_a
    k = axis
    dot_kv = _dot(k, v)
    cross_kv = _cross(k, v)
    return [
        v[0] * cos_a + cross_kv[0] * sin_a + k[0] * dot_kv * one_minus_cos,
        v[1] * cos_a + cross_kv[1] * sin_a + k[1] * dot_kv * one_minus_cos,
        v[2] * cos_a + cross_kv[2] * sin_a + k[2] * dot_kv * one_minus_cos,
    ]


# ---------------------------------------------------------------------------
# Initial camera helpers (used when entering walkthrough at "human eye" pose)
# ---------------------------------------------------------------------------

def default_eye_pose(building_center_xy=(0.0, 0.0), eye_height_feet=5.5,
                     facing_xy=(1.0, 0.0)):
    """Return a sensible starting camera tuple at human eye height.

    Used only as a fallback when the active view doesn't already have
    a perspective camera set up (fresh view, first launch). In normal
    use the existing view's GetOrientation seeds the camera state and
    we never call this.
    """
    cx, cy = building_center_xy
    fx, fy = facing_xy
    forward = _normalize([fx, fy, 0.0])
    up      = [0.0, 0.0, 1.0]
    position = [cx, cy, float(eye_height_feet)]
    return (position, forward, up)
