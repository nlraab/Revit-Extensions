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

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
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
from System.Windows.Controls import ListBoxItem
from System.Windows.Input import (
    Key, Keyboard, Mouse, MouseButtonState, Cursors, CaptureMode,
)
from System.Windows.Threading import DispatcherTimer, DispatcherPriority
from System import TimeSpan

from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

from pyrevit import forms, revit
import dbhms_ui
import dbhms_telemetry

from clash_core import persistence, project, users
from clash_view import (
    walkthrough_view, walkthrough_motion, walkthrough_bookmarks,
    walkthrough_render, walkthrough_handoff,
)


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'WalkthroughForm.xaml')


# Mark every script execution so we know definitively whether the
# Walkthrough script is running. If this line doesn't appear in the log
# after a Walkthrough button click, the script literally isn't being
# invoked (pyRevit cache, persistent engine staleness, etc.).
walkthrough_view._log("====== script.py imported ======")
walkthrough_view._log("log path: {}".format(walkthrough_view.log_path()))


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

# How long the camera must sit still (no keys, no mouse) before we snap
# the view back to full presentation quality. Short enough that a brief
# pause restores fidelity promptly; long enough that the rapid start/stop
# of normal flying doesn't thrash the display style. Seconds.
_STOP_DEBOUNCE = 0.3

# Speed multipliers when modifier keys are held.
_SHIFT_MULTIPLIER = 3.0
_CTRL_MULTIPLIER  = 0.25


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
        # Cached walkthrough view — set on open, reused every motion frame
        # so the per-frame camera path skips a FilteredElementCollector scan.
        self._view = None

    def set_completion_callback(self, cb):
        """Register a callable invoked on the WPF thread after each
        action runs. Receives (action_name, success, message, payload)."""
        self._completion_callback = cb

    def Execute(self, app):
        action = self.pending_action
        kwargs = self.kwargs
        # set_camera / read_camera fire ~30x/sec during free-fly; logging
        # them means a file append per frame on the UI thread. Skip those;
        # log everything else.
        if action not in ("set_camera", "read_camera"):
            walkthrough_view._log("Execute: action={}".format(action))
        self.pending_action = None
        self.kwargs = {}
        success, message, payload = False, "(no action)", None
        try:
            if action == "open_view":
                success, message, payload = self._open_view(app)
            elif action == "set_camera":
                success, message, payload = self._set_camera(app, **kwargs)
            elif action == "end_motion":
                success, message, payload = self._end_motion(app)
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
        """Find or create the Walkthrough view, (re-)apply the visual
        quality settings, and switch to it.

        Re-applies `configure_for_first_run` on EVERY open — not just
        on first-create — so existing views from previous sessions
        pick up new settings the next time the user launches. The
        property setters in configure_for_first_run are all idempotent,
        so re-applying is a safe no-op when nothing's changed.

        Returns (success, message, current_camera) so the form can
        seed its motion state from the view's orientation.
        """
        from Autodesk.Revit.DB import Transaction
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = walkthrough_view.find_walkthrough_view(doc)

        txn = Transaction(doc, "dbHMS Open walkthrough view")
        try:
            txn.Start()
            if view is None:
                view = walkthrough_view.get_or_create_walkthrough_view(doc)
                if view is None:
                    txn.RollBack()
                    return False, "No 3D ViewFamilyType available.", None
            else:
                # Existing view from a previous session — re-apply the
                # quality settings in case the firm-default tuning has
                # been bumped since the view was created.
                walkthrough_view.configure_for_first_run(view, doc)
            txn.Commit()
        except Exception as ex:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
            return False, "View setup failed: {}".format(ex), None
        self._view = view  # cache for the per-frame camera hot path
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

    def _set_camera(self, app, camera=None, enter_fast=False):
        """Apply the camera tuple inside a transaction. Runs ~30x/sec
        during free-fly, so this is THE performance-critical path.

        Why this used to be unusable: the method forces a synchronous
        repaint (uidoc.RefreshActiveView). At full presentation quality on
        a heavy model that repaint measured ~1073 ms — 99.7% of every
        frame. Because a pyRevit modeless window shares Revit's UI thread,
        that 1 s repaint froze the keyboard, mouse, and 30 fps timer: the
        "1-2 fps, unusable" symptom. Native orbit dodges this by rendering
        a SIMPLIFIED model while the camera is moving, then snapping back
        to full quality when it stops.

        The fix (Nathan chose "do what native orbit does"): on the first
        motion frame of a movement burst the form passes enter_fast=True,
        and we drop the view to a fast display style (Shaded + Medium
        detail) via walkthrough_view.enter_fast_navigation. Every fast-mode
        frame is then cheap, so the forced refresh stays well under the
        frame budget and the controls stay smooth. When the form detects
        the camera has been still past the debounce window it queues
        "end_motion", which restores full quality. We force the refresh on
        EVERY frame now (it's cheap in fast mode) so the picture tracks the
        camera in real time instead of only updating when motion stops.

        Uses the cached view (no per-frame collector scan) and does no
        per-frame logging.
        """
        from Autodesk.Revit.DB import Transaction
        if camera is None:
            return False, "No camera passed.", None
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = self._view
        if view is None:
            view = walkthrough_view.find_walkthrough_view(doc)
            self._view = view
        if view is None:
            return False, "Walkthrough view not found.", None
        txn = Transaction(doc, "dbHMS Walkthrough camera step")
        try:
            txn.Start()
            if enter_fast:
                # First frame of a movement burst — switch to the cheap
                # navigation display style for the duration of the motion.
                walkthrough_view.enter_fast_navigation(view, doc)
            walkthrough_view.set_camera(view, camera)
            txn.Commit()
        except Exception as ex:
            # The view may have been closed / deleted out from under us —
            # drop the cache so the next frame re-finds it.
            self._view = None
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
            return False, "Camera set failed: {}".format(ex), None
        # Force the repaint so the picture tracks the camera every frame.
        # Cheap now that we're in the fast display style during motion.
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        return True, "OK", None

    def _end_motion(self, app):
        """Movement stopped (debounced) — restore full presentation
        quality. Re-applies the firm template (or the Realistic fallback).

        Deliberately does NOT force a repaint. Committing the display-style
        change already marks the view dirty; Revit then repaints it at full
        quality on its own next-idle cycle — the SAME asynchronous, non-
        blocking path native orbit uses when you let go. A forced
        uidoc.RefreshActiveView() here would do an immediate synchronous
        full-quality redraw on the shared UI thread, which on a heavy model
        is ~1 s and locks the user out of moving again until it finishes
        (the "I stop and can't move for a second" symptom). Handing the
        snap-back repaint to Revit removes that lockout: the simplified
        frame lingers a beat, then Revit sharpens it on its own, exactly
        like native — and the controls never freeze.
        """
        from Autodesk.Revit.DB import Transaction
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return False, "No active document.", None
        doc = uidoc.Document
        view = self._view
        if view is None:
            view = walkthrough_view.find_walkthrough_view(doc)
            self._view = view
        if view is None:
            return False, "Walkthrough view not found.", None
        txn = Transaction(doc, "dbHMS Walkthrough restore quality")
        try:
            txn.Start()
            walkthrough_view.exit_fast_navigation(view, doc)
            txn.Commit()
        except Exception as ex:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
            return False, "Quality restore failed: {}".format(ex), None
        # No RefreshActiveView — let Revit repaint full quality on idle.
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
        walkthrough_view._log("WalkthroughForm.__init__ start")
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
        # LOD ("do what native orbit does") state. While the camera is
        # actively moving the view runs in a cheap display style (Shaded +
        # Medium); when it's been still past _STOP_DEBOUNCE we snap back to
        # full quality. _fast_active tracks whether we're currently in the
        # cheap style; _last_input_time is when we last saw movement input.
        self._fast_active = False
        self._last_input_time = None
        # Continuous-fly mode (the chk_continuous_fly toggle): when on, we
        # stay in the cheap style the WHOLE time instead of snapping back
        # to full quality on stop. Trades the glossy look for zero re-render
        # pauses — flying stays smooth and stopping is instant. Best on
        # heavy models. Driven by the checkbox; default off.
        self._continuous_fly = False
        # When the user clicks Save bookmark we first queue a live camera
        # read (so we capture wherever the view actually is now, including
        # native orbit/zoom), then finalize the save once that read lands.
        # Holds the pending bookmark name between those two steps.
        self._pending_bookmark_name = None
        # Global mouselook mode (F-key toggle): cursor hidden + locked
        # to an anchor screen point, mouse motion rotates camera, all
        # clicks blocked from reaching Revit while active.
        self._mouselook_mode = False
        self._mouselook_anchor_screen = None  # (x, y) screen px to recenter to
        self._mouselook_origin_cursor = None  # (x, y) where cursor was on enter

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

        # Sliders — Move speed (ft/s) and Look speed (deg/pixel).
        self.sl_speed.ValueChanged += self._on_speed_changed
        self.sl_look_sensitivity.ValueChanged += self._on_look_sensitivity_changed
        self._refresh_speed_label()
        self._refresh_look_sensitivity_label()

        # Smooth-fly toggle — stay in the cheap style permanently while on.
        self.chk_continuous_fly.Checked   += self._on_continuous_fly_changed
        self.chk_continuous_fly.Unchecked += self._on_continuous_fly_changed

        # Look pad — captures mouse + keyboard when clicked
        self.brd_lookpad.MouseLeftButtonDown += self._on_lookpad_mousedown
        self.brd_lookpad.MouseMove           += self._on_lookpad_mousemove
        self.brd_lookpad.MouseLeftButtonUp   += self._on_lookpad_mouseup
        self.brd_lookpad.LostMouseCapture    += self._on_capture_lost

        # Window-level keyboard so WASD works whenever the form has focus.
        self.PreviewKeyDown += self._on_preview_keydown
        self.PreviewKeyUp   += self._on_preview_keyup
        # Window-level MouseMove: feeds the F-key mouselook mode (which
        # captures the mouse so events come here from anywhere on screen).
        self.MouseMove += self._on_window_mousemove
        # Drop pressed-keys + active drag when the user alt-tabs or
        # clicks into Revit — avoids the "still flying after I came
        # back" bug.
        self.Deactivated += self._on_window_deactivated
        # Window-activated: also a trigger to pick up any pending
        # Walkthrough Here handoff. Catches the case where the form
        # is already open and the user brings it to front from the
        # taskbar / Revit toolbar.
        self.Activated += self._on_window_activated

        # DispatcherTimer for the motion loop. Created stopped; started
        # when the view opens.
        self._timer = DispatcherTimer()
        self._timer.Interval = TimeSpan.FromMilliseconds(_TICK_MS)
        self._timer.Tick += self._on_tick

        # Slow polling timer (every ~2s) that checks for "fly to here"
        # commands queued by the Browser's Walkthrough Here button.
        # Separate from the motion tick because (a) we don't want to do
        # disk I/O 30×/sec, (b) it has to keep ticking even when there's
        # no movement input.
        self._handoff_timer = DispatcherTimer()
        self._handoff_timer.Interval = TimeSpan.FromMilliseconds(2000)
        self._handoff_timer.Tick += self._on_handoff_poll

        # Bookmarks
        self._refresh_bookmarks()

        # Auto-open the Walkthrough view on launch. Saves a click in
        # the common case (especially Walkthrough Here from Browser)
        # where the user always wants to start by opening the view.
        # Idempotent — find_walkthrough_view returns the existing view
        # on subsequent calls.
        # Hooked to Loaded (not done from __init__) so the WPF window
        # is fully laid out before we kick off the Revit-side work.
        self.Loaded += self._on_window_loaded

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
                # Use a real WPF ListBoxItem with the name as Content and
                # the bookmark dict stashed on Tag. IronPython's __str__ is
                # NOT picked up by WPF's item rendering (it falls back to
                # the .NET type name, e.g. "IronPython.NewTypes..."), so we
                # set the displayed string explicitly. Tag carries the data
                # back to the double-click / delete handlers.
                lbi = ListBoxItem()
                lbi.Content = b.get("name") or "(unnamed)"
                lbi.Tag = b
                self.lb_bookmarks.Items.Add(lbi)
        except Exception as ex:
            self.txt_status.Text = "Couldn't load bookmarks: {}".format(ex)

    # --- Open view + camera seeding -----------------------------------

    def _on_open_view(self, sender, args):
        walkthrough_view._log("btn_open_view clicked, project_hash={}"
                              .format(self._project_hash))
        if not self._project_hash:
            dbhms_ui.info("No active project.", title='Walkthrough')
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

    def _refresh_look_sensitivity_label(self):
        self.txt_look_sensitivity_value.Text = "{:.2f}".format(
            self.sl_look_sensitivity.Value)

    def _on_look_sensitivity_changed(self, sender, args):
        self._refresh_look_sensitivity_label()

    def _on_continuous_fly_changed(self, sender, args):
        """Smooth-fly toggle. ON: drop to the cheap style now and stay
        there (the motion loop stops snapping back). OFF: restore full
        quality immediately and let the normal LOD behavior resume."""
        self._continuous_fly = bool(self.chk_continuous_fly.IsChecked)
        walkthrough_view._log("continuous_fly = {}".format(self._continuous_fly))
        if self._camera is None:
            return
        if self._continuous_fly:
            # Drop to the light style right away so the change is visible
            # even before the next movement. Subsequent frames keep it.
            self._fast_active = True
            self._queue_action("set_camera", camera=self._camera,
                               enter_fast=True)
        else:
            # Back to full quality now (async repaint, no lockout).
            self._fast_active = False
            self._queue_action("end_motion")

    def _current_look_sensitivity(self):
        return float(self.sl_look_sensitivity.Value)

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
        # Refresh camera cache from the live view — the user may have
        # rotated Revit manually between the last walkthrough action
        # and this click. Without this, the first drag move starts from
        # a stale position and the view "jumps back" to where the
        # walkthrough last left it.
        self._queue_action("read_camera")
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

    # --- Global mouselook (F-key toggle) ------------------------------
    #
    # Press F → cursor hidden, locked to anchor, mouse motion rotates
    # the camera regardless of where on screen the cursor sits. All
    # clicks captured by our window so they can't reach Revit. Press F
    # or Esc to exit. Equivalent UX to Enscape's right-click look mode.
    #
    # Mechanism: Window.CaptureMouse() routes ALL mouse events to us
    # while the cursor is anywhere on screen — even over Revit's
    # viewport. We hide the cursor with System.Windows.Forms.Cursor.Hide()
    # and recenter to an anchor screen-point on every MouseMove, so the
    # cursor never visually "drifts" off-screen.

    def _enter_mouselook(self):
        if self._mouselook_mode:
            return
        # Refresh camera cache before starting (user may have rotated
        # Revit manually).
        self._queue_action("read_camera")
        # Save current cursor position so we can put it back on exit.
        self._mouselook_origin_cursor = self._cursor_position()
        # Anchor at the center of our window in screen coordinates —
        # always on the same monitor as our window, no multi-monitor
        # weirdness.
        try:
            from System.Windows import Point as WPoint
            local = WPoint(self.ActualWidth / 2.0, self.ActualHeight / 2.0)
            screen_pt = self.PointToScreen(local)
            self._mouselook_anchor_screen = (int(screen_pt.X), int(screen_pt.Y))
        except Exception:
            # Fallback: keep cursor where it is.
            self._mouselook_anchor_screen = self._mouselook_origin_cursor
        # Capture mouse on the window — events flow to us even when
        # cursor is over Revit. Critical for "look anywhere on screen."
        try:
            self.CaptureMouse()
        except Exception:
            pass
        # Hide the system cursor entirely (not just within our window).
        try:
            from System.Windows.Forms import Cursor as WFCursor
            WFCursor.Hide()
        except Exception:
            pass
        # Move cursor to anchor.
        if self._mouselook_anchor_screen is not None:
            self._set_cursor_position(self._mouselook_anchor_screen)
        self._mouselook_mode = True
        self.brd_active_indicator.Visibility = Visibility.Visible
        self.txt_status.Text = (
            "MOUSELOOK ON. Move mouse to look. WASD to move. "
            "Press F or Esc to exit.")
        walkthrough_view._log("mouselook: ENTERED")

    def _exit_mouselook(self):
        if not self._mouselook_mode:
            return
        self._mouselook_mode = False
        try:
            if self.IsMouseCaptured:
                self.ReleaseMouseCapture()
        except Exception:
            pass
        # Show cursor again.
        try:
            from System.Windows.Forms import Cursor as WFCursor
            WFCursor.Show()
        except Exception:
            pass
        # Restore cursor to where it was when we entered (so the user
        # finds their cursor where they left it, not at the anchor).
        if self._mouselook_origin_cursor is not None:
            self._set_cursor_position(self._mouselook_origin_cursor)
        self._mouselook_origin_cursor = None
        self._mouselook_anchor_screen = None
        self._mouse_dx = 0.0
        self._mouse_dy = 0.0
        self.brd_active_indicator.Visibility = Visibility.Collapsed
        self.txt_status.Text = "Mouselook OFF. Press F to re-enter."
        walkthrough_view._log("mouselook: EXITED")

    def _on_window_mousemove(self, sender, args):
        """Window-level mouse move. Only feeds the F-key mouselook
        mode — the look-pad drag has its own MouseMove handler scoped
        to the look pad."""
        if not self._mouselook_mode or self._mouselook_anchor_screen is None:
            return
        cur = self._cursor_position()
        if cur is None:
            return
        ax, ay = self._mouselook_anchor_screen
        dx = cur[0] - ax
        dy = cur[1] - ay
        if dx == 0 and dy == 0:
            return
        self._mouse_dx += dx
        self._mouse_dy += dy
        # Yank the cursor back to anchor — combined with hidden cursor,
        # this lets the user drag indefinitely in any direction.
        self._set_cursor_position(self._mouselook_anchor_screen)

    @staticmethod
    def _cursor_position():
        try:
            from System.Windows.Forms import Cursor as WFCursor
            p = WFCursor.Position
            return (p.X, p.Y)
        except Exception:
            return None

    @staticmethod
    def _set_cursor_position(xy):
        try:
            from System.Windows.Forms import Cursor as WFCursor
            from System.Drawing import Point
            WFCursor.Position = Point(int(xy[0]), int(xy[1]))
        except Exception:
            pass

    # --- Keyboard ------------------------------------------------------
    #
    # WASD works whenever the form has keyboard focus AND the view is
    # open. No "capture" requirement — clicking the look pad is only for
    # mouse-look, not for keys.

    def _on_preview_keydown(self, sender, args):
        if self._camera is None:
            return

        # F → toggle global mouselook mode. The mouse becomes a
        # "look-around mouse"; all clicks are blocked from Revit.
        # F again (or Esc) exits.
        if args.Key == Key.F:
            if self._mouselook_mode:
                self._exit_mouselook()
            else:
                self._enter_mouselook()
            args.Handled = True
            return

        # Esc exits mouselook only (not WASD or anything else).
        if args.Key == Key.Escape and self._mouselook_mode:
            self._exit_mouselook()
            args.Handled = True
            return

        movement_key = _KEY_MAP.get(args.Key)
        if movement_key is not None:
            was_empty = not self._pressed_keys
            if movement_key not in self._pressed_keys:
                walkthrough_view._log("keydown: {}".format(movement_key))
            self._pressed_keys.add(movement_key)
            # First key of a new input session → refresh camera cache
            # in case the user rotated Revit's view manually since the
            # last input. Without this, the next tick uses a stale
            # cached position and the camera "jumps back" to where it
            # was before the manual rotation.
            if was_empty:
                self._queue_action("read_camera")
            args.Handled = True

    def _on_preview_keyup(self, sender, args):
        movement_key = _KEY_MAP.get(args.Key)
        if movement_key is not None:
            self._pressed_keys.discard(movement_key)
            args.Handled = True

    def _on_window_deactivated(self, sender, args):
        # Window lost focus (alt-tab, click in Revit). Drop any pressed
        # keys + drop active drag + exit mouselook so we don't end up
        # in a stuck state when we come back.
        self._pressed_keys.clear()
        self._stop_drag()
        if self._mouselook_mode:
            self._exit_mouselook()

    # --- Motion tick (DispatcherTimer) --------------------------------

    def _on_tick(self, sender, args):
        if self._camera is None:
            return
        from datetime import datetime
        now = datetime.now()
        has_input = bool(self._pressed_keys) or self._mouse_dx != 0 or self._mouse_dy != 0
        # ALWAYS update last-tick-time, even when there's no input.
        # Otherwise after a quiet period, the first input tick computes
        # `dt = now - last_input_tick` which can be many seconds —
        # multiplied by speed that's a huge teleport. Symptom Nathan
        # reported: pressing E after idling jumps the camera way up
        # before settling at the correct speed.
        if not has_input:
            self._last_tick_time = now
            # No movement this tick. If we're in the cheap navigation
            # display style and the camera has been still long enough,
            # snap back to full presentation quality. The debounce keeps
            # the rapid start/stop of normal flying from thrashing the
            # display style on every brief pause.
            #
            # Skipped entirely in continuous-fly mode: there we deliberately
            # stay in the cheap style so there's never a re-render pause.
            if (not self._continuous_fly and self._fast_active
                    and self._last_input_time is not None):
                idle = (now - self._last_input_time).total_seconds()
                if idle >= _STOP_DEBOUNCE:
                    self._fast_active = False
                    self._queue_action("end_motion")
            return
        # There IS movement input this tick.
        self._last_input_time = now
        if self._last_tick_time is None:
            dt = _TICK_MS / 1000.0
        else:
            dt = max(0.001, (now - self._last_tick_time).total_seconds())
        # Cap dt at ~3 frames worth (100ms) — defends against frame
        # stutter / alt-tab pauses creating sudden bursts of movement
        # when input resumes.
        dt = min(dt, _TICK_MS / 1000.0 * 3.0)
        self._last_tick_time = now

        new_camera = self._camera
        if self._pressed_keys:
            new_camera = walkthrough_motion.step(
                new_camera, self._pressed_keys,
                self._current_speed_fps(), dt)
        if self._mouse_dx != 0 or self._mouse_dy != 0:
            new_camera = walkthrough_motion.look(
                new_camera, self._mouse_dx, self._mouse_dy,
                sensitivity_deg_per_pixel=self._current_look_sensitivity())
            self._mouse_dx = 0.0
            self._mouse_dy = 0.0
        self._camera = new_camera
        # First frame of a movement burst → tell the Revit side to drop to
        # the cheap navigation display style (what native orbit does). Once
        # we're in fast mode, subsequent frames just move the camera.
        enter_fast = not self._fast_active
        if enter_fast:
            self._fast_active = True
        self._queue_action("set_camera", camera=new_camera, enter_fast=enter_fast)

    # --- Handoff: Walkthrough Here from Browser (Iter 13) -------------
    #
    # Browser writes a "fly to here" command file when the user clicks
    # its Walkthrough Here button. We pick it up two ways:
    #  1. On _open_view completion (covers the case where the form was
    #     closed when the user clicked Walkthrough Here).
    #  2. Via _handoff_timer (every ~2s while running) — covers the
    #     case where the form was already open.
    # Either way, the same _consume_pending_handoff() reads + clears the
    # file and queues a set_camera to the target.

    def _on_handoff_poll(self, sender, args):
        # Log every tick so we can confirm the timer is alive — if this
        # line isn't appearing in the log, the timer was never started
        # or has been stopped.
        walkthrough_view._log(
            "handoff_poll: tick (camera={}, ph={})".format(
                "set" if self._camera is not None else "None",
                self._project_hash))
        if self._camera is None or not self._project_hash:
            return
        self._consume_pending_handoff()

    def _on_window_activated(self, sender, args):
        """Bring-to-front trigger — also consume pending if there is
        one. Catches the case where the user clicks the Walkthrough
        toolbar button and the persistent-engine pyRevit just brings
        the existing form to the front instead of creating a fresh
        one (script body never re-runs, so _open_view's consume path
        doesn't fire either)."""
        walkthrough_view._log("window_activated")
        if self._camera is not None and self._project_hash:
            self._consume_pending_handoff()

    def _on_window_loaded(self, sender, args):
        """Window is fully shown — conditionally auto-open the
        Walkthrough view.

        Behavior:
          * **Pending Walkthrough Here command on disk** (Browser just
            queued one): auto-open the view + consume → camera flies.
            Truly one-click from the Browser.
          * **No pending command** (manual Walkthrough toolbar click):
            do nothing. The user wants to come up to the form to
            adjust sliders / browse bookmarks / read the controls
            cheatsheet without the walkthrough view being forced open.
            They'll click Open Walkthrough View when ready.

        One-shot — unsubscribe immediately so a second Loaded raise
        (shouldn't happen, but safe) doesn't re-fire the auto-open.
        """
        try:
            self.Loaded -= self._on_window_loaded
        except Exception:
            pass
        if not self._project_hash or self._camera is not None:
            return
        # Only auto-open if there's a queued fly-here command waiting.
        try:
            pending = walkthrough_handoff.read_pending(self._project_hash)
        except Exception:
            pending = None
        if pending is not None:
            walkthrough_view._log(
                "auto-open: pending handoff present — queueing open_view")
            self._queue_action("open_view")
        else:
            walkthrough_view._log(
                "auto-open: no pending handoff — waiting for manual Open")

    def _consume_pending_handoff(self):
        walkthrough_view._log("consume_pending_handoff: enter, ph={}"
                              .format(self._project_hash))
        if not self._project_hash:
            return
        try:
            cmd = walkthrough_handoff.read_pending(self._project_hash)
        except Exception as ex:
            walkthrough_view._log(
                "consume_pending_handoff: read raised {}".format(ex))
            cmd = None
        if cmd is None:
            walkthrough_view._log("consume_pending_handoff: no pending file")
            return
        walkthrough_view._log(
            "consume_pending_handoff: read pending for clash_seq={}"
            .format(cmd.get("clash_seq")))
        target_camera = walkthrough_handoff.viewpoint_to_camera_tuple(cmd)
        try:
            walkthrough_handoff.clear_pending(self._project_hash)
        except Exception as ex:
            walkthrough_view._log(
                "consume_pending_handoff: clear failed {}".format(ex))
        if target_camera is None:
            walkthrough_view._log(
                "consume_pending_handoff: translation returned None")
            self.txt_status.Text = (
                "Couldn't fly to queued clash — viewpoint is malformed.")
            return
        walkthrough_view._log(
            "consume_pending_handoff: queueing set_camera to pos={}, fwd={}"
            .format(target_camera[0], target_camera[1]))
        self._camera = target_camera
        self._queue_action("set_camera", camera=target_camera)
        seq = cmd.get("clash_seq")
        seq_part = "Clash #{}".format(seq) if seq is not None else "queued clash"
        self.txt_status.Text = "Flying to {}.".format(seq_part)

    # --- Bookmarks -----------------------------------------------------

    def _on_save_bookmark(self, sender, args):
        if self._camera is None:
            dbhms_ui.info("Open the Walkthrough view first.",
                        title='Save bookmark')
            return
        if not self._project_hash:
            dbhms_ui.info("No active project.", title='Save bookmark')
            return
        name = forms.ask_for_string(
            default='New bookmark',
            prompt='Name this bookmark:',
            title='Save bookmark',
        )
        if not name:
            return
        # Capture the LIVE camera before saving. self._camera only tracks
        # WASD / mouse-look moves; if the user orbited or zoomed natively
        # in Revit since their last keypress, the cache is stale and we'd
        # bookmark the old pose. Queue a fresh read_camera and finalize the
        # save in _on_handler_done once it lands.
        self._pending_bookmark_name = name
        self.txt_status.Text = "Saving bookmark..."
        self._queue_action("read_camera")

    def _finalize_bookmark_save(self, name):
        """Write the bookmark using the freshly-read self._camera. Called
        from the read_camera completion once _on_save_bookmark has queued
        the read."""
        if self._camera is None:
            self.txt_status.Text = "Couldn't save bookmark — no camera."
            return
        if not self._project_hash:
            return
        position, forward, up = self._camera
        bm = walkthrough_bookmarks.make_bookmark(
            name, position, forward, up,
            created_by=self._author,
        )
        try:
            walkthrough_bookmarks.append_bookmark(self._project_hash, bm)
        except Exception as ex:
            dbhms_ui.info("Couldn't save bookmark:\n\n{}".format(ex),
                        title='Save bookmark failed')
            return
        self._refresh_bookmarks()
        self.txt_status.Text = "Bookmark saved: {}".format(bm["name"])

    def _on_bookmark_double_click(self, sender, args):
        item = self.lb_bookmarks.SelectedItem
        if item is None:
            return
        bm = item.Tag
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
        bm = item.Tag
        if not forms.alert(
                "Delete bookmark '{}'?".format(bm.get("name")),
                title='Delete bookmark', yes=True, no=True):
            return
        try:
            walkthrough_bookmarks.delete_bookmark(
                self._project_hash, bm.get("id"))
        except Exception as ex:
            dbhms_ui.info("Couldn't delete: {}".format(ex),
                        title='Delete bookmark failed')
            return
        self._refresh_bookmarks()

    # --- Render --------------------------------------------------------

    def _on_render(self, sender, args):
        if not self._project_hash:
            dbhms_ui.info("No active project.", title='Render')
            return
        if self._camera is None:
            dbhms_ui.info("Open the Walkthrough view first.", title='Render')
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
                        "Active. WASD to move. F = mouselook mode. "
                        "Click+drag the look pad for quick rotates. "
                        "Shift/Ctrl = faster/slower.")
                    self._timer.Start()
                    self._handoff_timer.Start()
                    walkthrough_view._log("timers started (motion + handoff)")
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
                    # If the user hit "Walkthrough Here" in the Browser
                    # before opening this form, there's a pending fly
                    # command waiting for us. Consume it now.
                    self._consume_pending_handoff()
                else:
                    self.txt_status.Text = message
                return
            if action == "read_camera":
                # Refresh the cached camera from the live view (user may
                # have rotated Revit manually). This keeps WASD/look
                # operating against current state instead of stale cache.
                if success and payload is not None:
                    self._camera = payload
                # If a Save bookmark click is waiting on this fresh read,
                # finalize it now so the bookmark stores the true current
                # pose, not the stale cache.
                pending = self._pending_bookmark_name
                if pending is not None:
                    self._pending_bookmark_name = None
                    self._finalize_bookmark_save(pending)
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
        walkthrough_view._log("WalkthroughForm closing")
        try:
            self._timer.Stop()
        except Exception:
            pass
        try:
            self._handoff_timer.Stop()
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
# Telemetry note: this is the only modeless tool. Show() returns
# immediately, so we can't use the dbhms_telemetry.session() context
# manager (it would close the session before the user actually finishes
# walking through). Instead, we open the session manually, attach its
# end() to the form's Closed event, and fail-fast on construction
# errors. See lib/dbhms_telemetry/__init__.py for the rule.

def _on_walkthrough_closed(sender, args):
    try:
        dbhms_telemetry.end(_TELEMETRY_SESSION, status='completed')
    except Exception:
        pass


def _start():
    win = WalkthroughForm()
    global _ACTIVE_WINDOW
    _ACTIVE_WINDOW = win
    win.Closed += _on_walkthrough_closed
    win.Show()


_ACTIVE_WINDOW = None
_TELEMETRY_SESSION = dbhms_telemetry.start(__title__, script_path=__file__)
try:
    _start()
except Exception:
    import sys as _sys
    _et, _ev, _ = _sys.exc_info()
    dbhms_telemetry.end(
        _TELEMETRY_SESSION,
        status='failed',
        error=(
            getattr(_et, '__name__', None),
            str(_ev) if _ev is not None else '',
            traceback.format_exc(),
        ),
    )
    raise
