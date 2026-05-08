"""Tests for lib/clash_view/walkthrough_motion.py.

Pure-data — no Revit. Verifies WASD movement direction + magnitude,
diagonal-speed normalization, world-up vs camera-up Q/E, mouse-look
yaw + pitch + clamp, and Rodrigues rotation correctness.
"""

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_view import walkthrough_motion as wm  # noqa: E402


def _camera(pos, fwd, up):
    """Build the (position, forward, up) tuple from three lists."""
    return (list(pos), list(fwd), list(up))


# ---------------------------------------------------------------------------
# step() — WASD horizontal movement
# ---------------------------------------------------------------------------

class StepHorizontalTests(unittest.TestCase):

    def setUp(self):
        # Camera at origin, facing +X, world Z up.
        self.cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])

    def test_no_keys_no_movement(self):
        new = wm.step(self.cam, set(), 10.0, 1.0)
        self.assertEqual(new[0], [0, 0, 5])

    def test_zero_speed_no_movement(self):
        new = wm.step(self.cam, {wm.KEY_FORWARD}, 0.0, 1.0)
        self.assertEqual(new[0], [0, 0, 5])

    def test_zero_dt_no_movement(self):
        new = wm.step(self.cam, {wm.KEY_FORWARD}, 10.0, 0.0)
        self.assertEqual(new[0], [0, 0, 5])

    def test_forward_moves_along_camera_xy(self):
        new = wm.step(self.cam, {wm.KEY_FORWARD}, 10.0, 1.0)
        self.assertAlmostEqual(new[0][0], 10.0)
        self.assertAlmostEqual(new[0][1], 0.0)
        self.assertAlmostEqual(new[0][2], 5.0)  # vertical unchanged

    def test_backward_moves_opposite(self):
        new = wm.step(self.cam, {wm.KEY_BACKWARD}, 10.0, 1.0)
        self.assertAlmostEqual(new[0][0], -10.0)

    def test_right_moves_perpendicular(self):
        # Camera faces +X, right is -Y in Revit's coord system
        # (right = forward × up = (1,0,0) × (0,0,1) = (0,-1,0)).
        new = wm.step(self.cam, {wm.KEY_RIGHT}, 10.0, 1.0)
        self.assertAlmostEqual(new[0][0], 0.0)
        self.assertAlmostEqual(new[0][1], -10.0)

    def test_left_is_opposite_of_right(self):
        new = wm.step(self.cam, {wm.KEY_LEFT}, 10.0, 1.0)
        self.assertAlmostEqual(new[0][1], 10.0)

    def test_pitched_camera_w_follows_full_3d_forward(self):
        # Camera pitched 45° down — forward is (cos45, 0, -sin45).
        # Pressing W now follows the FULL 3D forward direction (the
        # natural "fly where I'm looking" feel), so the camera dives
        # forward-and-down instead of staying level.
        s = math.sqrt(0.5)
        cam = _camera([0, 0, 10], [s, 0, -s], [s, 0, s])
        new = wm.step(cam, {wm.KEY_FORWARD}, 10.0, 1.0)
        # forward unit · 10 ft = (s*10, 0, -s*10) ≈ (7.07, 0, -7.07)
        self.assertAlmostEqual(new[0][0], 10.0 * s, places=5)
        self.assertAlmostEqual(new[0][1], 0.0, places=5)
        self.assertAlmostEqual(new[0][2], 10.0 - 10.0 * s, places=5)

    def test_pitched_camera_strafe_stays_horizontal(self):
        # A/D should NOT track pitch — standard FPS convention. Camera
        # pitched 45° down, pressing D still strafes purely horizontal.
        s = math.sqrt(0.5)
        cam = _camera([0, 0, 10], [s, 0, -s], [s, 0, s])
        new = wm.step(cam, {wm.KEY_RIGHT}, 10.0, 1.0)
        # Forward XY = +X, so right_xy = (0,-1,0). Camera moves in -Y
        # at 10 ft, height unchanged.
        self.assertAlmostEqual(new[0][1], -10.0, places=5)
        self.assertAlmostEqual(new[0][2], 10.0, places=5)

    def test_diagonal_does_not_double_speed(self):
        # W+D simultaneously should travel `distance` total, not √2 ×
        # distance — otherwise diagonal movement is faster than straight.
        new = wm.step(self.cam, {wm.KEY_FORWARD, wm.KEY_RIGHT}, 10.0, 1.0)
        magnitude = math.sqrt(new[0][0]**2 + new[0][1]**2)
        self.assertAlmostEqual(magnitude, 10.0, places=5)

    def test_dt_scales_distance(self):
        # speed=10 ft/s, dt=0.5s → 5 ft.
        new = wm.step(self.cam, {wm.KEY_FORWARD}, 10.0, 0.5)
        self.assertAlmostEqual(new[0][0], 5.0)


# ---------------------------------------------------------------------------
# step() — Q/E vertical movement
# ---------------------------------------------------------------------------

class StepVerticalTests(unittest.TestCase):

    def test_up_moves_along_world_z_by_default(self):
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.step(cam, {wm.KEY_UP}, 10.0, 1.0)
        self.assertAlmostEqual(new[0][2], 15.0)

    def test_down_opposite_of_up(self):
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.step(cam, {wm.KEY_DOWN}, 10.0, 1.0)
        self.assertAlmostEqual(new[0][2], -5.0)

    def test_world_up_respected_when_camera_pitched(self):
        # Camera pitched downward — Q should still move along world +Z,
        # not along the camera's local up.
        s = math.sqrt(0.5)
        cam = _camera([0, 0, 10], [s, 0, -s], [s, 0, s])
        new = wm.step(cam, {wm.KEY_UP}, 10.0, 1.0, world_up=True)
        self.assertAlmostEqual(new[0][2], 20.0)
        self.assertAlmostEqual(new[0][0], 0.0)

    def test_camera_up_when_world_up_false(self):
        # With world_up=False, Q/E follows the camera's tilted up.
        s = math.sqrt(0.5)
        cam = _camera([0, 0, 10], [s, 0, -s], [s, 0, s])
        new = wm.step(cam, {wm.KEY_UP}, 10.0, 1.0, world_up=False)
        # Up moves along (s, 0, s) → +x and +z by 10*s ≈ 7.07.
        self.assertAlmostEqual(new[0][0], 10.0 * s, places=5)
        self.assertAlmostEqual(new[0][2], 10.0 + 10.0 * s, places=5)


# ---------------------------------------------------------------------------
# look() — yaw + pitch
# ---------------------------------------------------------------------------

class LookTests(unittest.TestCase):

    def test_no_delta_no_change(self):
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 0, 0)
        self.assertEqual(new[1], [1.0, 0.0, 0.0])

    def test_yaw_rotates_forward_around_z(self):
        # Default sensitivity 0.15 deg/pixel; dx=600 pixels → -90° yaw
        # (negative because dx>0 means look-right which is -yaw in
        # standard math convention).
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 600, 0, sensitivity_deg_per_pixel=0.15)
        # Yaw of -90° around Z takes (1,0,0) → (0,-1,0).
        self.assertAlmostEqual(new[1][0], 0.0, places=5)
        self.assertAlmostEqual(new[1][1], -1.0, places=5)
        self.assertAlmostEqual(new[1][2], 0.0, places=5)

    def test_yaw_does_not_change_z_component(self):
        # Pure yaw must keep the camera level (no roll).
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 200, 0)
        self.assertAlmostEqual(new[1][2], 0.0, places=5)

    def test_pitch_rotates_forward_around_right(self):
        # dy=300 with sens 0.15 → -45° pitch (negative because dy>0
        # means look-down, which decreases the forward vector's z).
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 0, 300, sensitivity_deg_per_pixel=0.15)
        # Forward should tilt downward by 45°.
        self.assertAlmostEqual(new[1][0], math.cos(math.radians(45)), places=5)
        self.assertAlmostEqual(new[1][2], -math.sin(math.radians(45)), places=5)

    def test_pitch_clamped_to_max(self):
        # Try to pitch up by 200° in one step. Should clamp at +88°.
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 0, -2000, sensitivity_deg_per_pixel=0.15)
        # At max pitch, forward.z = sin(88°) ≈ 0.9994.
        self.assertAlmostEqual(new[1][2], math.sin(math.radians(88.0)),
                               places=4)

    def test_pitch_clamped_negative(self):
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 0, 2000, sensitivity_deg_per_pixel=0.15)
        self.assertAlmostEqual(new[1][2], -math.sin(math.radians(88.0)),
                               places=4)

    def test_up_remains_perpendicular_to_forward(self):
        # After any yaw + pitch, up should be perpendicular to forward
        # (otherwise the orientation isn't a valid rigid frame).
        cam = _camera([0, 0, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 137, 89)
        dot = (new[1][0] * new[2][0]
               + new[1][1] * new[2][1]
               + new[1][2] * new[2][2])
        self.assertAlmostEqual(dot, 0.0, places=5)

    def test_position_unchanged(self):
        cam = _camera([3, 4, 5], [1, 0, 0], [0, 0, 1])
        new = wm.look(cam, 100, 50)
        self.assertEqual(new[0], [3.0, 4.0, 5.0])


# ---------------------------------------------------------------------------
# right_vector + helpers
# ---------------------------------------------------------------------------

class RightVectorTests(unittest.TestCase):

    def test_standard_right_vector(self):
        # forward=+X, up=+Z → right = forward × up = (-Y).
        right = wm.right_vector([1, 0, 0], [0, 0, 1])
        self.assertAlmostEqual(right[0], 0.0)
        self.assertAlmostEqual(right[1], -1.0)
        self.assertAlmostEqual(right[2], 0.0)

    def test_right_vector_is_unit_length(self):
        right = wm.right_vector([2, 0, 0], [0, 0, 5])  # non-unit inputs
        magnitude = math.sqrt(sum(x*x for x in right))
        self.assertAlmostEqual(magnitude, 1.0, places=5)


# ---------------------------------------------------------------------------
# default_eye_pose
# ---------------------------------------------------------------------------

class DefaultEyePoseTests(unittest.TestCase):

    def test_returns_position_at_eye_height(self):
        cam = wm.default_eye_pose(eye_height_feet=5.5)
        self.assertAlmostEqual(cam[0][2], 5.5)

    def test_forward_is_normalized(self):
        cam = wm.default_eye_pose(facing_xy=(3, 4))
        magnitude = math.sqrt(cam[1][0]**2 + cam[1][1]**2 + cam[1][2]**2)
        self.assertAlmostEqual(magnitude, 1.0, places=5)

    def test_up_is_world_z(self):
        cam = wm.default_eye_pose()
        self.assertEqual(cam[2], [0.0, 0.0, 1.0])


if __name__ == "__main__":
    unittest.main()
