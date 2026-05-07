# -*- coding: utf-8 -*-
"""Walkthrough — free-fly through the Revit model with WASD + mouse-look.

Designed for coordination meetings: open the dedicated `dbHMS Walkthrough`
perspective view, fly around the entire model with all trades visible,
toggle disciplines on/off as you go, save bookmark camera positions,
render any view to a high-quality PNG.

Architecture:
  * Modeless WPF — Revit's viewport keeps redrawing as the camera updates
    each frame.
  * IExternalEventHandler — every Revit-side action (view-create,
    camera-move, visibility toggle, render) runs through one shared
    ExternalEvent so it lands on Revit's UI thread in a valid API
    context.
  * `__persistentengine__ = True` — keeps the IronPython engine alive
    after script.py exits so the handler class definition stays valid.
    Without this, Revit fatal-crashes when invoking Execute() on a
    torn-down class.
  * DispatcherTimer at ~33 ms (30 fps) drives the motion loop. Each tick
    reads the pressed-keys set + accumulated mouse-look delta, computes
    a new camera state via clash_view.walkthrough_motion, and queues a
    SetOrientation through the ExternalEvent.
  * Mouse capture inside the look pad: cursor is hidden, motion is
    measured as deltas, cursor is recentered every frame so the user
    can drag indefinitely without hitting screen edges.

See Clash Detection.tab/README.md for the architecture.
"""

__title__  = 'Walk-\nthrough'
__author__ = 'Nathaniel'
__doc__    = ('Free-fly through the model with WASD + mouse-look. '
              'Toggle disciplines, save bookmark views, render PNGs. '
              'For coordination meetings.')

# CRITICAL for modeless: tells pyRevit to keep this script's IronPython
# engine alive after script.py exits. Without it, Revit fatal-crashes
# when it tries to invoke our IExternalEventHandler on a torn-down class
# (ExceptionCode=0xe0434352, no Python output).
__persistentengine__ = True

import os
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

import System
from System import EventHandler
from System.Windows import Visibility, Input
from System.Windows.Input import (
    Key, Keyboard, Mouse, MouseButtonState, Cursors, CaptureMode,
)
from System.Windows.Threading import DispatcherTimer, DispatcherPriority
from System import TimeSpan

from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

from pyrevit import forms, revit

from clash_core import persistence, project, users
from clash_view import (
    walkthrough_view, walkthrough_motion, walkthrough_bookmarks,
    walkthrough_render,
)


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'WalkthroughForm.xaml')


# Movement key map. WPF Key enum → motion-module key name.
_KEY_MAP = {
    Key.W: walkthrough_motion.KEY_FORWARD,
    Key.S: walkthrough_motion.KEY_BACKWARD,
    Key.A: walkthrough_motion.KEY_LEFT,
    Key.D: walkthrough_motion.KEY_RIGHT,
    Key.Q: walkthrough_motion.KEY_DOWN,
    Key.E: walkthrough_motion.KEY_UP,
}

# DispatcherTimer interval — 33 ms ≈ 30 fps. Faster than this and we
# queue ExternalEvents Revit can't drain in time; slower and the camera
# motion feels laggy.
_TICK_MS = 33

# Speed multipliers when modifier keys are held.
_SHIFT_MULTIPLIER = 3.0
_CTRL_MULTIPLIER  = 0.25


# ---------------------------------------------------------------------------
# Bookmark view-model (binds to ListBox)
# ---------------------------------------------------------------------------

class _BookmarkRow(object):
    def __init__(self, bookmark):
        self.Bookmark = bookmark
        self.Display  = bookmark.get("name") or "(unnamed)"

    def __str__(self):
        return self.Display


# ---------------------------------------------------------------------------
# Revit-side action handler — runs on Revit's UI thread via ExternalEvent
# ---------------------------------------------------------------------------

class _WalkthroughHandler(IExternalEventHandler):
    """One handler instance services every Revit-side action — view
    create/activate, camera set, discipline visibility toggle, render.

    pending_action is set by the WPF side then external_event.Raise()'d.
    Single-handler-many-actions serializes all Revit work through one
    queue (ExternalEvent only allows one pending Raise at a time
    anyway, so this prevents accidental ordering bugs).
    """

    def __init__(self):
        self.pending_action = None
        self.kwargs         = {}
        self._completion_callback = None

    def set_completion_callback(self, cb):
        """Register a callable invoked on the WPF thread after each
        action runs. Receives (action_name, success, message, payload)."""
        self._completion_callback = cb

    def Execute(self, app):
        action = self.pending_action
        kwargs = self.kwargs
        walkthrough_view._log("Execute: action={}".format(action))
        self.pending_action = None
        self.kwargs = {}
        success, message, payload = False, "(no action)", None
        try:
            if action == "open_view":
                success, message, payload = self._open_view(app)
            elif action == "set_camera":
                success, message, payload = self._set_camera(app, **kwargs)
            elif action == "set_discipline":
                success, message, payload = self._set_discipline(app, **kwargs)
            elif action == "render":
                success, message, payload = self._render(app, **kwargs)
            elif action == "read_camera":
                success, message, payload = self._read_camera(app)
            else:
                message = "Unknown action: {}".format(action)
        except Exception:
            message = "{} crashed: {}".format(action, traceback.format_exc())
            walkthrough_view._log(message)
        cb = self._completion_callback
        if cb is not None:
            try:
                cb(action, success, message, payload)
            except Exception:
                pass

    def GetName(self):
        return "dbHMS Walkthrough action handler"

    # -- Actions --------------------------------------------------------

    def _open_view(self, app):
        """Find or create the Walkthrough view and switch to it. Returns
        (success, message, current_camera) so the form can seed its
        motion state from the view's existing orientation."""
        from Autodesk.Revit.DB import Transaction
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = walkthrough_view.find_walkthrough_view(doc)
        if view is None:
            txn = Transaction(doc, "dbHMS Create walkthrough view")
            try:
                txn.Start()
                view = walkthrough_view.get_or_create_walkthrough_view(doc)
                if view is None:
                    txn.RollBack()
                    return False, "No 3D ViewFamilyType available.", None
                txn.Commit()
            except Exception as ex:
                if txn.HasStarted() and not txn.HasEnded():
                    txn.RollBack()
                return False, "View create failed: {}".format(ex), None
        try:
            uidoc.ActiveView = view
        except Exception:
            pass
        cam = walkthrough_view.get_camera(view)
        return True, "Walkthrough view ready.", cam

    def _read_camera(self, app):
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        view = walkthrough_view.find_walkthrough_view(uidoc.Document)
        if view is None:
            return False, "Walkthrough view not found.", None
        return True, "OK", walkthrough_view.get_camera(view)

    def _set_camera(self, app, camera=None):
        """Apply the camera tuple inside a transaction + refresh the view."""
        from Autodesk.Revit.DB import Transaction
        if camera is None:
            return False, "No camera passed.", None
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = walkthrough_view.find_walkthrough_view(doc)
        if view is None:
            return False, "Walkthrough view not found.", None
        txn = Transaction(doc, "dbHMS Walkthrough camera step")
        try:
            txn.Start()
            walkthrough_view.set_camera(view, camera)
            txn.Commit()
        except Exception as ex:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
            return False, "Camera set failed: {}".format(ex), None
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        return True, "OK", None

    def _set_discipline(self, app, discipline=None, visible=True):
        from Autodesk.Revit.DB import Transaction
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = walkthrough_view.find_walkthrough_view(doc)
        if view is None:
            return False, "Walkthrough view not found.", None
        txn = Transaction(doc, "dbHMS Walkthrough toggle {}"
                          .format(discipline))
        try:
            txn.Start()
            walkthrough_view.set_discipline_visible(
                doc, view, discipline, visible)
            txn.Commit()
        except Exception as ex:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
            return False, "Toggle failed: {}".format(ex), None
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        return True, "{} {}.".format(
            discipline, "shown" if visible else "hidden"), None

    def _render(self, app, project_hash=None):
        """Render current Walkthrough view to high-quality PNG. The
        clash_dict argument is None — bookmark renders aren't tied to a
        specific clash, so the filename uses 'view' as the seq stand-in."""
        if not project_hash:
            return False, "No project hash.", None
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = walkthrough_view.find_walkthrough_view(doc)
        if view is None:
            return False, "Walkthrough view not found.", None
        success, path, message = walkthrough_render.render_stop(
            doc, view, {"seq": "view"}, project_hash)
        if not success:
            return False, message, None
        return True, "Render saved:\n{}".format(path), path


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------

class WalkthroughForm(forms.WPFWindow):
    """Modeless Walkthrough launcher + control surface.

    State machine:
      "idle"     — view not open yet. WASD does nothing. Open Walkthrough
                   View button is the only meaningful action.
      "armed"    — view open, look pad shows hint. Click look pad → captured.
      "captured" — mouse captured + cursor hidden. WASD + mouse drive
                   the camera. Esc / click again releases.
    """

    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)

        self._project_hash = None
        self._author       = self._resolve_author()
        self._resolve_project_hash()

        self._camera = None  # (position, forward, up); seeded when view opens
        self._pressed_keys = set()
        self._mouse_dx = 0.0
        self._mouse_dy = 0.0
        self._drag_origin = None  # (x, y) WPF-local point during active drag
        self._dragging = False    # True only while a mouse button is held
        self._last_tick_time = None

        # Revit-side ExternalEvent — same pattern as the previous tour
        # build but action set is different.
        self._handler = _WalkthroughHandler()
        self._handler.set_completion_callback(self._on_handler_done)
        self._external_event = ExternalEvent.Create(self._handler)

        # Wire UI
        self.btn_close.Click          += self._on_close
        self.btn_open_view.Click      += self._on_open_view
        self.btn_render.Click         += self._on_render
        self.btn_save_bookmark.Click  += self._on_save_bookmark
        self.lb_bookmarks.MouseDoubleClick += self._on_bookmark_double_click
        self.lb_bookmarks.KeyDown     += self._on_bookmarks_key

        # Visibility checkboxes
        for chk_name, disc in [
            ("chk_vis_mechanical",     "Mechanical"),
            ("chk_vis_electrical",     "Electrical"),
            ("chk_vis_plumbing",       "Plumbing"),
            ("chk_vis_fireprotection", "Fire Protection"),
            ("chk_vis_architectural",  "Architectural"),
            ("chk_vis_structural",     "Structural"),
        ]:
            chk = getattr(self, chk_name)
            chk.Checked   += (lambda s, a, d=disc:
                              self._on_visibility_changed(d, True))
            chk.Unchecked += (lambda s, a, d=disc:
                              self._on_visibility_changed(d, False))

        # Speed slider
        self.sl_speed.ValueChanged += self._on_speed_changed
        self._refresh_speed_label()

        # Look pad — captures mouse + keyboard when clicked
        self.brd_lookpad.MouseLeftButtonDown += self._on_lookpad_mousedown
        self.brd_lookpad.MouseMove           += self._on_lookpad_mousemove
        self.brd_lookpad.MouseLeftButtonUp   += self._on_lookpad_mouseup
        self.brd_lookpad.LostMouseCapture    += self._on_capture_lost

        # Window-level keyboard so WASD works whenever the form has focus.
        self.PreviewKeyDown += self._on_preview_keydown
        self.PreviewKeyUp   += self._on_preview_keyup
        # Drop pressed-keys + active drag when the user alt-tabs or
        # clicks into Revit — avoids the "still flying after I came
        # back" bug.
        self.Deactivated += self._on_window_deactivated

        # DispatcherTimer for the motion loop. Created stopped; started
        # when the view opens.
        self._timer = DispatcherTimer()
        self._timer.Interval = TimeSpan.FromMilliseconds(_TICK_MS)
        self._timer.Tick += self._on_tick

        # Bookmarks
        self._refresh_bookmarks()

    # --- Setup --------------------------------------------------------

    def _resolve_project_hash(self):
        try:
            doc = revit.doc
            if doc is None:
                return
            ph = project.project_hash_for(doc)
            if ph:
                self._project_hash = ph
        except Exception:
            pass

    def _resolve_author(self):
        try:
            return users.current_user(revit.uiapp)
        except Exception:
            return "unknown"

    def _refresh_speed_label(self):
        self.txt_speed_value.Text = "{:.0f} ft/s".format(self.sl_speed.Value)

    def _refresh_bookmarks(self):
        self.lb_bookmarks.Items.Clear()
        if not self._project_hash:
            return
        try:
            for b in walkthrough_bookmarks.read_bookmarks(self._project_hash):
                self.lb_bookmarks.Items.Add(_BookmarkRow(b))
        except Exception as ex:
            self.txt_status.Text = "Couldn't load bookmarks: {}".format(ex)

    # --- Open view + camera seeding -----------------------------------

    def _on_open_view(self, sender, args):
        if not self._project_hash:
            forms.alert("No active project.", title='Walkthrough')
            return
        self.txt_status.Text = "Opening Walkthrough view..."
        self._queue_action("open_view")

    # --- Visibility toggles -------------------------------------------

    def _on_visibility_changed(self, discipline, visible):
        # Don't queue toggles before the view is open — they'd silently
        # fail in the handler. The user can pre-set the checkboxes; we
        # apply them once the view exists. v1 just no-ops; v2 could
        # remember pending state.
        if self._camera is None:
            return
        self._queue_action("set_discipline",
                           discipline=discipline, visible=visible)

    def _apply_initial_visibility(self):
        """After the view opens for the first time, push every checkbox
        state into the view so what the user sees matches the form."""
        for chk_name, disc in [
            ("chk_vis_mechanical",     "Mechanical"),
            ("chk_vis_electrical",     "Electrical"),
            ("chk_vis_plumbing",       "Plumbing"),
            ("chk_vis_fireprotection", "Fire Protection"),
            ("chk_vis_architectural",  "Architectural"),
            ("chk_vis_structural",     "Structural"),
        ]:
            chk = getattr(self, chk_name)
            visible = bool(chk.IsChecked)
            # Only push hides — Revit defaults to all visible, so no
            # need to push True for the all-checked default. But push
            # False for any unchecked-on-launch.
            if not visible:
                self._queue_action("set_discipline",
                                   discipline=disc, visible=False)

    # --- Speed --------------------------------------------------------

    def _on_speed_changed(self, sender, args):
        self._refresh_speed_label()

    def _current_speed_fps(self):
        base = float(self.sl_speed.Value)
        # Modifier keys read directly from Keyboard so they apply even
        # if the user hasn't released the previous tick's check.
        if Keyboard.Modifiers & Input.ModifierKeys.Shift:
            base *= _SHIFT_MULTIPLIER
        if Keyboard.Modifiers & Input.ModifierKeys.Control:
            base *= _CTRL_MULTIPLIER
        return base

    # --- Look pad: click + drag for mouse-look ------------------------
    #
    # Simplified UX (no persistent capture): user click+drags inside the
    # look pad to rotate the camera. Cursor hides only DURING the drag.
    # Releasing the button stops the drag. WASD is independent — it
    # works any time the window has keyboard focus + the view is open.

    def _on_lookpad_mousedown(self, sender, args):
        walkthrough_view._log("lookpad mousedown fired")
        if self._camera is None:
            self.txt_status.Text = ("Open the Walkthrough view first "
                                    "(big blue button above).")
            return
        pt = args.GetPosition(self.brd_lookpad)
        self._drag_origin = (pt.X, pt.Y)
        self._dragging = True
        try:
            self.brd_lookpad.CaptureMouse()
        except Exception:
            pass
        Mouse.OverrideCursor = getattr(Cursors, 'None')  # 'None' is a Python keyword; can't use dot syntax
        self.brd_active_indicator.Visibility = Visibility.Visible
        self.txt_lookpad_active.Visibility = Visibility.Visible
        self.sp_lookpad_hint.Visibility = Visibility.Collapsed
        # Window needs keyboard focus for WASD; grab it now in case it's
        # drifted somewhere else.
        self.Focus()

    def _on_lookpad_mousemove(self, sender, args):
        if not self._dragging or self._drag_origin is None:
            return
        pt = args.GetPosition(self.brd_lookpad)
        dx = pt.X - self._drag_origin[0]
        dy = pt.Y - self._drag_origin[1]
        if dx == 0 and dy == 0:
            return
        self._mouse_dx += dx
        self._mouse_dy += dy
        # Recenter the system cursor to the drag origin so the user can
        # keep dragging in any direction without hitting screen edges.
        # The recenter posts a synthetic MouseMove with dx=dy=0 back to
        # us, filtered out by the guard above.
        self._recenter_cursor()

    def _on_lookpad_mouseup(self, sender, args):
        self._stop_drag()

    def _on_capture_lost(self, sender, args):
        # WPF/Revit can steal the capture (alt-tab, modal dialog).
        # Reset cleanly.
        self._stop_drag()

    def _stop_drag(self):
        if not self._dragging:
            return
        self._dragging = False
        self._drag_origin = None
        self._mouse_dx = 0.0
        self._mouse_dy = 0.0
        try:
            if self.brd_lookpad.IsMouseCaptured:
                self.brd_lookpad.ReleaseMouseCapture()
        except Exception:
            pass
        Mouse.OverrideCursor = None
        self.brd_active_indicator.Visibility = Visibility.Collapsed
        self.txt_lookpad_active.Visibility = Visibility.Collapsed
        self.sp_lookpad_hint.Visibility = Visibility.Visible

    def _recenter_cursor(self):
        """Move the system cursor back to the drag-origin point so the
        user has unlimited dragging room. WPF doesn't expose cursor
        positioning directly — we go through System.Windows.Forms.Cursor."""
        try:
            from System.Windows.Forms import Cursor
            from System.Drawing import Point
            from System.Windows import Point as WPoint
            origin_local = WPoint(self._drag_origin[0],
                                  self._drag_origin[1])
            screen_pt = self.brd_lookpad.PointToScreen(origin_local)
            Cursor.Position = Point(int(screen_pt.X), int(screen_pt.Y))
        except Exception as ex:
            walkthrough_view._log("recenter_cursor failed: {}".format(ex))

    # --- Keyboard ------------------------------------------------------
    #
    # WASD works whenever the form has keyboard focus AND the view is
    # open. No "capture" requirement — clicking the look pad is only for
    # mouse-look, not for keys.

    def _on_preview_keydown(self, sender, args):
        if self._camera is None:
            return
        movement_key = _KEY_MAP.get(args.Key)
        if movement_key is not None:
            if movement_key not in self._pressed_keys:
                walkthrough_view._log("keydown: {}".format(movement_key))
            self._pressed_keys.add(movement_key)
            args.Handled = True

    def _on_preview_keyup(self, sender, args):
        movement_key = _KEY_MAP.get(args.Key)
        if movement_key is not None:
            self._pressed_keys.discard(movement_key)
            args.Handled = True

    def _on_window_deactivated(self, sender, args):
        # Window lost focus (alt-tab, click in Revit). Drop any pressed
        # keys so the camera doesn't keep flying when we come back.
        self._pressed_keys.clear()
        self._stop_drag()

    # --- Motion tick (DispatcherTimer) --------------------------------

    def _on_tick(self, sender, args):
        if self._camera is None:
            return
        # No active input → don't burn cycles queueing identical states.
        if not self._pressed_keys and self._mouse_dx == 0 and self._mouse_dy == 0:
            return
        from datetime import datetime
        now = datetime.now()
        if self._last_tick_time is None:
            dt = _TICK_MS / 1000.0
        else:
            dt = max(0.001, (now - self._last_tick_time).total_seconds())
        self._last_tick_time = now

        new_camera = self._camera
        if self._pressed_keys:
            new_camera = walkthrough_motion.step(
                new_camera, self._pressed_keys,
                self._current_speed_fps(), dt)
        if self._mouse_dx != 0 or self._mouse_dy != 0:
            new_camera = walkthrough_motion.look(
                new_camera, self._mouse_dx, self._mouse_dy)
            self._mouse_dx = 0.0
            self._mouse_dy = 0.0
        self._camera = new_camera
        self._queue_action("set_camera", camera=new_camera)

    # --- Bookmarks -----------------------------------------------------

    def _on_save_bookmark(self, sender, args):
        if self._camera is None:
            forms.alert("Open the Walkthrough view first.",
                        title='Save bookmark')
            return
        if not self._project_hash:
            forms.alert("No active project.", title='Save bookmark')
            return
        name = forms.ask_for_string(
            default='New bookmark',
            prompt='Name this bookmark:',
            title='Save bookmark',
        )
        if not name:
            return
        position, forward, up = self._camera
        bm = walkthrough_bookmarks.make_bookmark(
            name, position, forward, up,
            created_by=self._author,
        )
        try:
            walkthrough_bookmarks.append_bookmark(self._project_hash, bm)
        except Exception as ex:
            forms.alert("Couldn't save bookmark:\n\n{}".format(ex),
                        title='Save bookmark failed')
            return
        self._refresh_bookmarks()
        self.txt_status.Text = "Bookmark saved: {}".format(bm["name"])

    def _on_bookmark_double_click(self, sender, args):
        item = self.lb_bookmarks.SelectedItem
        if item is None:
            return
        bm = item.Bookmark
        cam = bm.get("camera") or {}
        position = cam.get("position") or [0, 0, 5]
        forward  = cam.get("forward")  or [1, 0, 0]
        up       = cam.get("up")       or [0, 0, 1]
        new_camera = (list(position), list(forward), list(up))
        self._camera = new_camera
        self._queue_action("set_camera", camera=new_camera)
        self.txt_status.Text = "Jumped to: {}".format(bm.get("name"))

    def _on_bookmarks_key(self, sender, args):
        if args.Key != Key.Delete:
            return
        item = self.lb_bookmarks.SelectedItem
        if item is None:
            return
        bm = item.Bookmark
        if not forms.alert(
                "Delete bookmark '{}'?".format(bm.get("name")),
                title='Delete bookmark', yes=True, no=True):
            return
        try:
            walkthrough_bookmarks.delete_bookmark(
                self._project_hash, bm.get("id"))
        except Exception as ex:
            forms.alert("Couldn't delete: {}".format(ex),
                        title='Delete bookmark failed')
            return
        self._refresh_bookmarks()

    # --- Render --------------------------------------------------------

    def _on_render(self, sender, args):
        if not self._project_hash:
            forms.alert("No active project.", title='Render')
            return
        if self._camera is None:
            forms.alert("Open the Walkthrough view first.", title='Render')
            return
        self.txt_status.Text = "Rendering current view..."
        self._queue_action("render", project_hash=self._project_hash)

    # --- Handler completion (marshals to WPF thread) ------------------

    def _on_handler_done(self, action, success, message, payload):
        def update():
            if action == "open_view":
                if success:
                    self._camera = payload  # seeded camera from view
                    self.txt_status.Text = (
                        "Active. WASD to move. Click+drag the look pad "
                        "to look around. Shift = faster, Ctrl = slower.")
                    self._timer.Start()
                    self._apply_initial_visibility()
                    # Window must have keyboard focus for WASD to fire.
                    # The Open View button stole it on click; grab it
                    # back here so the user can press W immediately.
                    try:
                        self.Activate()
                        self.Focus()
                        Keyboard.Focus(self)
                    except Exception:
                        pass
                else:
                    self.txt_status.Text = message
                return
            if action == "set_camera":
                # Per-tick camera sets are too noisy to update status for.
                if not success:
                    self.txt_status.Text = message
                return
            if not success:
                self.txt_status.Text = message
                return
            self.txt_status.Text = message
        try:
            self.Dispatcher.BeginInvoke(
                DispatcherPriority.Normal, System.Action(update))
        except Exception:
            pass

    def _queue_action(self, action, **kwargs):
        self._handler.pending_action = action
        self._handler.kwargs = kwargs
        try:
            self._external_event.Raise()
        except Exception as ex:
            self.txt_status.Text = "Couldn't queue action: {}".format(ex)

    def _on_close(self, sender, args):
        try:
            self._timer.Stop()
        except Exception:
            pass
        try:
            self._release_capture()
        except Exception:
            pass
        try:
            self._external_event.Dispose()
        except Exception:
            pass
        self.Close()


# ---------------------------------------------------------------------------
# Entry — modeless Show, NOT ShowDialog
# ---------------------------------------------------------------------------

def _start():
    win = WalkthroughForm()
    global _ACTIVE_WINDOW
    _ACTIVE_WINDOW = win
    win.Show()


_ACTIVE_WINDOW = None
_start()
