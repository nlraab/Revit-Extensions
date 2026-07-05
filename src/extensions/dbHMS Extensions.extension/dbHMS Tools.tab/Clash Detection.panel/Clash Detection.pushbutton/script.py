# -*- coding: utf-8 -*-
"""Clash Coordination - the ground-up rebuild (Phase 1 shell).

This is the first in-Revit piece of the new clash coordination tool. It hosts
the xeokit-based web front-end in a WebView2 pane (reusing the exact hosting
approach proven by the 3D Viewer button next door), exports the active model to
glTF on demand, and loads it in xeokit where every element is addressable by its
stable federation key (fed_key). The full coordination UI (clash grid, issue
tracker, dashboards) grows inside the web page over the coming phases; unwired
parts are clearly labelled as sample data for now.

See CLASH_REBUILD_SPEC.md (this panel) for the plan. The WebView2 hosting notes
live in the 3D Viewer button's script.py, which this deliberately mirrors so both
tools behave identically inside Revit (assembly loading, the Revit-2024
Core/Wpf version-match fix, env-var steering, the virtual-host mapping, and the
DPI bounds-nudge for scaled displays).
"""

__title__  = 'Clash\nDetection'
__author__ = 'Nathaniel'
__doc__    = ('dbHMS clash coordination (rebuild). Phase 1: hosts the xeokit '
              'viewer in Revit, exports the model, and picks/isolates elements '
              'by their stable id. Clash grid and issue tracking come next.')

import os
import shutil
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import System
from System.Windows import Thickness, TextWrapping, VerticalAlignment, PresentationSource
from System.Windows.Controls import TextBlock
from System.Windows.Media import SolidColorBrush, Colors

from pyrevit import forms, revit
import dbhms_telemetry


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'CoordinationForm.xaml')
WEB_DIR    = os.path.join(SCRIPT_DIR, 'web')
APP_PAGE   = 'coord.html'                 # the page served over the virtual host
WEB_INDEX  = os.path.join(WEB_DIR, APP_PAGE)

# Writable runtime root, separate from the 3D Viewer's so the two never interfere.
# The web assets are copied in from WEB_DIR on launch; exported models go under
# models/. Both are served under one WebView2 virtual host so the page can fetch
# large model files (the base64 message channel maxes out around 25 MB).
_DATA_ROOT = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or SCRIPT_DIR,
    'dbHMS', 'Coordination')
APP_DIR    = os.path.join(_DATA_ROOT, 'app')
MODELS_DIR = os.path.join(_DATA_ROOT, 'models')
VHOST      = 'dbhms.coord'
# Second virtual host: the team's SHARED clash-data root (clashes.json,
# viewpoints/). Mapped read-only-in-spirit so the page can display persisted
# clash thumbnails without the 3D model being loaded.
VHOST_DATA = 'dbhms.clashdata'

_WEBVIEW2_WPF  = 'Microsoft.Web.WebView2.Wpf.dll'
_WEBVIEW2_CORE = 'Microsoft.Web.WebView2.Core.dll'

# Software-composite escape hatch for the offset/black-box render seen on a few
# GPU drivers. Off by default (WebGL stays hardware-accelerated either way).
DISABLE_GPU_COMPOSITING = False

LOG_PATH = os.path.join(_DATA_ROOT, 'coord.log')

# Model-size guardrails for the WebView2 renderer. Peak renderer memory when
# xeokit loads a .glb is roughly 3-5x the file size (download buffer + decoded
# typed arrays + GPU copies + per-element scene objects). Above the hard cap we
# never hand the page a url: -- the renderer would run out of memory and Chromium
# would swap in its "Out of Memory" page, which reads as the tool vanishing. The
# clash DATA is never at risk either way (it's already on disk); only the
# optional 3D snapshot + clash images need the model loaded. THESE ARE
# UN-MEASURED PLACEHOLDERS: 580 MB was the observed OOM (a full Fine-detail
# federated model); 350 blocks it with margin. Raise once there's field data or
# after the leaner image-mode export lands.
GLB_HARD_MAX  = 350 * 1048576   # bytes: never load past this
GLB_SOFT_WARN = 220 * 1048576   # bytes: load, but warn the load may be slow


def _log(msg):
    """Append a timestamped line to the tool log. Best-effort; never raises."""
    try:
        if not os.path.isdir(_DATA_ROOT):
            os.makedirs(_DATA_ROOT)
        from datetime import datetime
        with open(LOG_PATH, 'a') as f:
            f.write("{0} {1}\n".format(datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _safe_title(doc):
    """Sanitize the document title into a filename-safe, URL-safe stem (spaces
    become underscores so it needs no URL escaping)."""
    try:
        title = doc.Title or 'model'
    except Exception:
        title = 'model'
    keep = []
    for c in title:
        if c.isalnum() or c in ('_', '-'):
            keep.append(c)
        elif c == ' ':
            keep.append('_')
    return ''.join(keep).strip('_') or 'model'


def _revit_dir_with_webview2():
    """Directory holding Revit's bundled WebView2 assemblies (next to Revit.exe),
    or None. The running process IS Revit.exe, so its folder is where they live."""
    try:
        exe = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName
        d = os.path.dirname(exe)
        if os.path.isfile(os.path.join(d, _WEBVIEW2_WPF)):
            return d
    except Exception:
        pass
    return None


def _revit_version_tag():
    """Best-effort Revit major version ('2024'/'2025'/'2026'), to give each Revit
    its OWN WebView2 user-data folder (two processes must not share one with
    different options). Falls back to 'x'."""
    try:
        import re
        exe = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName
        m = re.search(r'Revit\s+(\d{4})', exe or '')
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        v = getattr(__revit__.Application, 'VersionNumber', None)   # noqa: F821
        if v:
            return str(v)
    except Exception:
        pass
    return 'x'


def _log_webview2_versions():
    """Log the version + location of every loaded WebView2 Core/Wpf, so the
    Revit-2024 Core/Wpf skew is diagnosable in one shot."""
    try:
        seen = []
        for asm in System.AppDomain.CurrentDomain.GetAssemblies():
            n = asm.GetName()
            if n.Name in ('Microsoft.Web.WebView2.Core', 'Microsoft.Web.WebView2.Wpf'):
                seen.append("{0} v{1} @ {2}".format(n.Name, n.Version, asm.Location))
        _log("webview2 loaded: {0}".format(" | ".join(seen) or "none"))
    except Exception:
        pass


def _loaded_webview2_core_dir():
    """If WebView2.Core is ALREADY loaded in-process, return its folder -- else
    None. The Revit-2024 fix: pair the Wpf wrapper with whatever Core version is
    already live, so the wrapper never calls a ctor the older Core lacks."""
    try:
        for asm in System.AppDomain.CurrentDomain.GetAssemblies():
            try:
                if asm.GetName().Name == 'Microsoft.Web.WebView2.Core':
                    loc = asm.Location
                    if loc and os.path.isfile(loc) and \
                       os.path.isfile(os.path.join(os.path.dirname(loc), _WEBVIEW2_WPF)):
                        return os.path.dirname(loc)
            except Exception:
                continue
    except Exception:
        pass
    return None


def _load_webview2_type():
    """Reference Revit's WebView2 assemblies and return (WebView2 type, None) on
    success or (None, reason) on failure."""
    d = _loaded_webview2_core_dir()
    if d is not None:
        _log("webview2: matching wrapper to already-loaded Core at {0}".format(d))
    else:
        d = _revit_dir_with_webview2()
    if d is None:
        return None, ("WebView2 assemblies were not found next to Revit.exe. "
                      "They ship with Revit 2025/2026; this Revit may be older.")
    try:
        clr.AddReferenceToFileAndPath(os.path.join(d, _WEBVIEW2_CORE))
        clr.AddReferenceToFileAndPath(os.path.join(d, _WEBVIEW2_WPF))
        from Microsoft.Web.WebView2.Wpf import WebView2
        return WebView2, None
    except Exception:
        return None, "Failed to load WebView2:\n\n{}".format(traceback.format_exc())


class CoordinationForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML, handle_esc=False)
        # Parent the window to Revit's main HWND. Without an owner, the window
        # falls BEHIND the Revit window whenever a Revit API call activates
        # Revit (during detection or a glTF export, and while WebView2 spins up
        # on open) -- the tool vanishes behind Revit. Owning it to Revit keeps
        # it reliably in front and off the taskbar as a separate entry. Prefer
        # the authoritative UIApplication.MainWindowHandle (Revit 2019+); a raw
        # Process.MainWindowHandle can resolve to the wrong top-level window.
        try:
            from System.Windows.Interop import WindowInteropHelper
            hwnd = None
            try:
                hwnd = __revit__.MainWindowHandle   # noqa: F821
            except Exception:
                hwnd = None
            if hwnd is None or int(hwnd) == 0:
                import System.Diagnostics
                hwnd = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
            WindowInteropHelper(self).Owner = hwnd
            _log("owner: parented to Revit hwnd {0}".format(int(hwnd)))
        except Exception:
            _log("owner: failed to parent to Revit\n{0}".format(
                traceback.format_exc()))
        self._webview = None
        self._vhost_ok = False
        self._model_version = 0
        self._pushed_initial = False
        # Run/export progress + abort state. `_op_busy` gates re-entrancy: while
        # a blocking op (detection, export) pumps the message loop to keep the
        # page's Abort button live, only `abortrun` is honored, so a stray
        # double-click can't start a second run mid-flight. `_abort_run` is the
        # flag the pumped `abortrun` sets; `_cancel_check` reads it. `_last_pump`
        # throttles the pump so it never slows the export to a crawl.
        self._op_busy = False
        self._abort_run = False
        self._last_pump = 0
        # Route the binding module's diagnostics into coord.log so folder
        # resolution is fully traceable in one place.
        try:
            from clash_core import binding
            binding.set_logger(_log)
        except Exception:
            pass
        self.Closed += self._on_closed
        # Attach the web panel after layout so the host Border has a real size.
        self.Loaded += self._on_loaded

    # --- Web panel ----------------------------------------------------

    def _on_loaded(self, sender, args):
        try:
            self.Loaded -= self._on_loaded
        except Exception:
            pass
        self._attach_viewer()

    def _attach_viewer(self):
        WebView2, reason = _load_webview2_type()
        if WebView2 is None:
            self._show_viewport_message(reason)
            return
        if not os.path.isfile(WEB_INDEX):
            self._show_viewport_message("Coordination page is missing:\n\n{}".format(WEB_INDEX))
            return
        try:
            self._sync_app_assets()
            self._WebView2 = WebView2
            # Steer WebView2 through process ENV VARS (not CoreWebView2CreationProperties,
            # which trips a MissingMethodException against Revit-2024's older Core).
            browser_args = []
            if DISABLE_GPU_COMPOSITING:
                browser_args.append('--disable-gpu-compositing')
            System.Environment.SetEnvironmentVariable(
                'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', ' '.join(browser_args))
            # WebView2 forbids two instances sharing a user-data folder with
            # DIFFERENT browser args (COMException 0x8007139F), and a stale
            # msedgewebview2.exe from a crashed session can hold the folder
            # with OLD args. Like the 3D Viewer's '-s3', the folder name
            # encodes an args-scheme tag ('cd1') - bump it whenever the
            # browser args change so old holders can never conflict.
            udf_base = 'WebView2-cd1-swc' if DISABLE_GPU_COMPOSITING else 'WebView2-cd1'
            udf_path = os.path.join(_DATA_ROOT, udf_base, _revit_version_tag())
            try:
                if not os.path.isdir(udf_path):
                    os.makedirs(udf_path)
            except Exception:
                udf_path = os.path.join(os.environ.get('TEMP') or _DATA_ROOT,
                                        'dbHMS_Coordination', udf_base)
                try:
                    if not os.path.isdir(udf_path):
                        os.makedirs(udf_path)
                except Exception:
                    pass
            self._udf_path = udf_path
            self._wv_retried = False
            self._cleanup_retry_udfs(udf_path)
            self._create_webview(udf_path)
        except Exception:
            _log("attach: EXCEPTION\n{}".format(traceback.format_exc()))
            self._show_viewport_message(
                "The coordination panel failed to start:\n\n{}".format(traceback.format_exc()))

    def _create_webview(self, udf_path):
        """Create the WebView2 control against a specific user-data folder.
        Called once normally, and a second time by the init-failure retry
        with a fresh per-process folder."""
        System.Environment.SetEnvironmentVariable('WEBVIEW2_USER_DATA_FOLDER', udf_path)
        _log("attach: udf={0}".format(udf_path))
        wv = self._WebView2()
        wv.CoreWebView2InitializationCompleted += self._on_webview_init
        self.brd_viewport.Child = wv
        self._webview = wv
        try:
            wv.EnsureCoreWebView2Async(None)
            _log("attach: webview created, EnsureCoreWebView2Async called")
        except Exception:
            _log("attach: EnsureCoreWebView2Async failed\n{0}".format(traceback.format_exc()))
            wv.Source = System.Uri(WEB_INDEX)

    def _cleanup_retry_udfs(self, udf_path):
        """Best-effort removal of stale retry profiles (<udf>-r*) left by
        previous crashed sessions; a locked one just stays."""
        try:
            parent = os.path.dirname(udf_path)
            base = os.path.basename(udf_path) + '-r'
            for name in os.listdir(parent):
                if name.startswith(base):
                    try:
                        shutil.rmtree(os.path.join(parent, name))
                        _log("attach: removed stale retry profile {0}".format(name))
                    except Exception:
                        pass
        except Exception:
            pass

    def _sync_app_assets(self):
        """Recursively copy the web assets into the served app folder so the
        virtual host serves them (and models) from one origin. Per-file guard so a
        file locked by a prior WebView2 doesn't abort the whole sync."""
        try:
            if not os.path.isdir(APP_DIR):
                os.makedirs(APP_DIR)
            n = 0
            for dirpath, _dirs, files in os.walk(WEB_DIR):
                rel = os.path.relpath(dirpath, WEB_DIR)
                dst_dir = APP_DIR if rel == "." else os.path.join(APP_DIR, rel)
                if not os.path.isdir(dst_dir):
                    os.makedirs(dst_dir)
                for name in files:
                    try:
                        shutil.copy2(os.path.join(dirpath, name), os.path.join(dst_dir, name))
                        n += 1
                    except Exception:
                        _log("sync: skip {0} ({1})".format(
                            name, traceback.format_exc().splitlines()[-1]))
            _log("sync: copied {0} file(s); {1} present={2}".format(
                n, APP_PAGE, os.path.isfile(os.path.join(APP_DIR, APP_PAGE))))
        except Exception:
            _log("sync: EXCEPTION\n{}".format(traceback.format_exc()))

    def _log_dpi_state(self):
        try:
            src = PresentationSource.FromVisual(self.brd_viewport)
            m11 = src.CompositionTarget.TransformToDevice.M11 if src else None
            _log("dpi: border={0}x{1} wpf_M11={2}".format(
                getattr(self.brd_viewport, 'ActualWidth', '?'),
                getattr(self.brd_viewport, 'ActualHeight', '?'), m11))
        except Exception:
            pass

    def _nudge_webview_bounds(self):
        """Toggle a 2px margin to force the WebView2 child-HWND to recompute its
        Bounds, self-correcting the offset render seen on >100% display scaling."""
        try:
            wv = self._webview
            if wv is None:
                return
            wv.Margin = Thickness(0, 0, 0, 2)
            wv.UpdateLayout()
            wv.Margin = Thickness(0)
            wv.UpdateLayout()
        except Exception:
            pass

    def _schedule_bounds_nudge(self):
        try:
            from System.Windows.Threading import DispatcherTimer
            self._nudge_n = 0
            timer = DispatcherTimer()
            timer.Interval = System.TimeSpan.FromMilliseconds(320)

            def _tick(s, e):
                self._nudge_webview_bounds()
                self._nudge_n += 1
                if self._nudge_n >= 4:
                    timer.Stop()
            timer.Tick += _tick
            timer.Start()
            self._nudge_timer = timer   # pin so it isn't garbage-collected
        except Exception:
            pass

    def _on_webview_init(self, sender, args):
        try:
            ok = args.IsSuccess
            _log("init: IsSuccess={0}".format(ok))
            _log_webview2_versions()
            if not ok:
                _log("init: FAILED\n{0}".format(args.InitializationException))
                # One-shot self-heal: 0x8007139F ("group or resource is not
                # in the correct state") and friends usually mean the shared
                # profile folder is held by a stale msedgewebview2.exe from
                # a crashed session, or the WebView2 runtime updated under
                # Revit. A fresh per-process profile sidesteps both.
                if not getattr(self, '_wv_retried', False):
                    self._wv_retried = True
                    fresh = '{0}-r{1}'.format(
                        getattr(self, '_udf_path', os.path.join(_DATA_ROOT, 'WebView2')),
                        System.Diagnostics.Process.GetCurrentProcess().Id)
                    _log("init: retrying once with fresh profile {0}".format(fresh))
                    try:
                        if not os.path.isdir(fresh):
                            os.makedirs(fresh)
                    except Exception:
                        pass
                    try:
                        self.brd_viewport.Child = None
                    except Exception:
                        pass
                    self._create_webview(fresh)
                    return
                self._show_viewport_message(
                    "The embedded browser (WebView2) could not start, even "
                    "after retrying with a fresh profile.\n\n"
                    "This is almost always one of these:\n\n"
                    "1. A leftover browser process from a previous or crashed "
                    "Revit session is holding the profile. Fix: close ALL "
                    "Revit windows, open Task Manager, end every "
                    "'msedgewebview2.exe' process (or simply restart the "
                    "computer), then reopen the tool.\n\n"
                    "2. Windows updated the WebView2 runtime while Revit was "
                    "open. Fix: restart Revit.\n\n"
                    "Technical details:\n{0}".format(args.InitializationException))
                return
            core = self._webview.CoreWebView2
            self._log_dpi_state()
            self._schedule_bounds_nudge()
            try:
                # Kill Chromium's own right-click menu everywhere in the app:
                # its "Reload" entry restarts the whole tool mid-meeting, and
                # the 3D tab ships its own right-click menu.
                core.Settings.AreDefaultContextMenusEnabled = False
            except Exception:
                pass
            try:
                core.WebMessageReceived += self._on_web_message
            except Exception:
                _log("init: WebMessageReceived wire failed\n{0}".format(traceback.format_exc()))
            try:
                # The single hole that made the OOM crash invisible: with no
                # ProcessFailed handler, a dead renderer just shows Chromium's
                # "Out of Memory" page and the tool looks like it closed itself.
                core.ProcessFailed += self._on_process_failed
            except Exception:
                _log("init: ProcessFailed wire failed\n{0}".format(traceback.format_exc()))
            try:
                core.NavigationStarting += self._on_nav_starting
                core.NavigationCompleted += self._on_nav_completed
            except Exception:
                pass
            # Resolve the access-kind enum from the SAME assembly the core came
            # from (avoids IronPython "expected X got X" identity errors).
            ak_type = core.GetType().Assembly.GetType(
                "Microsoft.Web.WebView2.Core.CoreWebView2HostResourceAccessKind")
            allow = System.Enum.Parse(ak_type, "Allow")
            app_index = os.path.join(APP_DIR, APP_PAGE)
            if os.path.isfile(app_index):
                core.SetVirtualHostNameToFolderMapping(VHOST, _DATA_ROOT, allow)
                # Second host -> this project's clash-data folder, so persisted
                # clash thumbnails render without the 3D model loaded. Remapped
                # whenever the folder changes (_map_data_host).
                self._allow = allow
                self._datahost_ok = False
                try:
                    from clash_core import binding
                    folder = binding.folder_for(revit.doc) if revit.doc else None
                    self._map_data_host(folder)
                except Exception:
                    _log("init: data vhost failed\n{0}".format(traceback.format_exc()))
                page = self._versioned_page(app_index)
                core.Navigate("https://{0}/app/{1}".format(VHOST, page))
                self._vhost_ok = True
                _log("init: vhost mapped, navigated to app/{0}".format(page))
            else:
                _log("init: app index missing at {0}".format(app_index))
        except Exception:
            _log("init: EXCEPTION\n{}".format(traceback.format_exc()))

    def _versioned_page(self, app_index):
        """Copy the page to coord.<mtime>.html and return that name, so each update
        navigates to a fresh URL no WebView2 cache can satisfy with a stale copy."""
        try:
            stem = APP_PAGE.rsplit('.', 1)[0]
            ver = int(os.path.getmtime(app_index))
            for f in os.listdir(APP_DIR):
                if f.startswith(stem + '.') and f.endswith('.html') and f != APP_PAGE:
                    try:
                        os.remove(os.path.join(APP_DIR, f))
                    except Exception:
                        pass
            versioned = "{0}.{1}.html".format(stem, ver)
            shutil.copy2(app_index, os.path.join(APP_DIR, versioned))
            return versioned
        except Exception:
            return APP_PAGE

    def _on_nav_starting(self, sender, args):
        try:
            _log("nav start: {0}".format(args.Uri))
        except Exception:
            pass

    def _pf_kind_name(self, args):
        """Name of the ProcessFailedKind ('RenderProcessExited', ...); '?' if
        it can't be read. Kept as a string so version skew never throws."""
        try:
            return str(args.ProcessFailedKind)
        except Exception:
            return "?"

    def _pf_reason_name(self, args):
        """Name of the ProcessFailedReason ('OutOfMemory', 'Crashed', ...).
        Newer property, absent on Revit-2024's older Core where READING it
        throws -- hence its own guard."""
        try:
            return str(args.Reason)
        except Exception:
            return "?"

    def _on_process_failed(self, sender, args):
        """A WebView2 process died. Without this the big-model renderer OOM
        silently swaps the page for Chromium's error screen and the tool looks
        like it "just closed". We log it, keep the clash data safe (it was never
        in the renderer), and for a recoverable render-process crash reload the
        page and re-push the project feed.

        Recovery by kind:
          RenderProcessExited / RenderProcessUnresponsive -> the CONTROL is
              alive, the PAGE is dead: Reload() + re-map vhosts + re-push.
          BrowserProcessExited -> the control is dead, Reload() would throw:
              show a message telling the user to reopen the tool.
          GPU / Utility / other support processes -> Chromium auto-restarts
              them and the page keeps running: do NOTHING (a reload would
              destroy a healthy page)."""
        kind = self._pf_kind_name(args)
        reason = self._pf_reason_name(args)
        exit_code = None
        try:
            exit_code = args.ExitCode
        except Exception:
            pass
        _log("ProcessFailed: kind={0} reason={1} exit={2}".format(
            kind, reason, exit_code))

        if kind not in ("RenderProcessExited", "RenderProcessUnresponsive",
                        "BrowserProcessExited"):
            _log("ProcessFailed: {0} is auto-recovered by Chromium; "
                 "no action".format(kind))
            return

        # Clear the busy gate so the reloaded page can drive again (any in-flight
        # export runs on the UI thread, unaffected by the renderer dying).
        self._op_busy = False
        self._abort_run = False

        if kind == "BrowserProcessExited":
            self._show_viewport_message(
                "The embedded browser stopped unexpectedly.\n\n"
                "Your clash data is saved -- nothing was lost. Please close "
                "this window and reopen the Clash Detection tool.\n\n"
                "(Technical: WebView2 browser process exited, "
                "reason={0}.)".format(reason))
            return

        # Render-process crash (the big-model OOM case). The control is alive;
        # the page is dead. Reload to a blank page, then re-establish everything
        # it needs, because a reload starts it from scratch.
        oom = (reason == "OutOfMemory")
        _log("ProcessFailed: render process gone (oom={0}); reloading".format(oom))
        self._pushed_initial = False   # let _push_initial run again
        self._crash_oom = oom          # read by _on_nav_completed after reload
        try:
            core = self._webview.CoreWebView2
            # Re-map BOTH virtual hosts before the reloaded page fetches assets:
            # the app host is stable (_DATA_ROOT); the data host follows the
            # bound folder. Mapping is idempotent, so re-adding is safe.
            try:
                ak_type = core.GetType().Assembly.GetType(
                    "Microsoft.Web.WebView2.Core.CoreWebView2HostResourceAccessKind")
                allow = System.Enum.Parse(ak_type, "Allow")
                self._allow = allow
                core.SetVirtualHostNameToFolderMapping(VHOST, _DATA_ROOT, allow)
                from clash_core import binding
                folder = binding.folder_for(revit.doc) if revit.doc else None
                self._map_data_host(folder)
            except Exception:
                _log("ProcessFailed: vhost re-map failed\n{0}".format(
                    traceback.format_exc()))
            core.Reload()
            _log("ProcessFailed: Reload() issued")
        except Exception:
            _log("ProcessFailed: recovery failed\n{0}".format(
                traceback.format_exc()))
            self._show_viewport_message(
                "The 3D view ran out of memory and couldn't be restarted "
                "automatically.\n\nYour clash data is saved. Please close and "
                "reopen the tool. If it was a very large model, skip loading "
                "the 3D snapshot for images -- your clashes are all there "
                "without it.")

    def _on_nav_completed(self, sender, args):
        try:
            _log("nav done: success={0}".format(getattr(args, 'IsSuccess', '?')))
        except Exception:
            pass
        # Belt-and-suspenders: the page normally posts "ready" once its
        # message listener is up, which triggers _push_initial. But if that
        # single handshake message is ever dropped (timing, a reopened
        # WebView2, a focus race), the tool would open blank and the user
        # would think the project "lost its folder". So we ALSO arm a
        # one-shot timer that pushes the data ~2.5s after navigation if the
        # page never said ready. _push_initial is idempotent (guarded), so
        # whichever fires first wins and the other is a no-op.
        try:
            from System.Windows.Threading import DispatcherTimer
            from System import TimeSpan
            t = DispatcherTimer()
            t.Interval = TimeSpan.FromMilliseconds(2500)

            def _fallback(s, e):
                try:
                    t.Stop()
                except Exception:
                    pass
                if not getattr(self, "_pushed_initial", False):
                    _log("ready: page never said ready in 2.5s - "
                         "pushing data anyway (fallback)")
                    self._push_initial("nav-fallback")
            t.Tick += _fallback
            t.Start()
            self._ready_timer = t   # pin so it isn't GC'd
        except Exception:
            _log("nav done: fallback timer failed\n{0}".format(
                traceback.format_exc()))
        # If this navigation is the recovery reload after a renderer OOM, tell
        # the page so it shows a "model too large for 3D" banner instead of
        # silently re-loading the same monster .glb.
        if getattr(self, "_crash_oom", False):
            self._crash_oom = False
            try:
                self._post("hostcrash:oom")
            except Exception:
                pass

    def _push_initial(self, why):
        """Push the whole project feed to the page (settings, tests, clashes,
        model info). Runs on both the page-ready handshake and the nav
        fallback, so it MUST be idempotent and MUST NOT let one failing feed
        block the others - a blank tool is exactly the symptom we're killing.
        Every step is logged so a single reproduction shows precisely what
        the tool saw."""
        if getattr(self, "_pushed_initial", False):
            _log("push_initial({0}): already pushed - skipping".format(why))
            return
        self._pushed_initial = True
        # Loud folder diagnostic: the #1 confusion is "did the tool find the
        # project's folder?". Answer it explicitly, with the source.
        try:
            from clash_core import binding
            doc = revit.doc
            model_f = binding.model_folder(doc) if doc is not None else None
            resolved = binding.folder_for(doc) if doc is not None else None
            src = ("model" if model_f else
                   ("machine-registry" if resolved else "none"))
            _log("push_initial({0}): doc={1} folder={2!r} source={3}".format(
                why, getattr(doc, "Title", None), resolved, src))
        except Exception:
            _log("push_initial({0}): folder probe failed\n{1}".format(
                why, traceback.format_exc()))
        # NOTE: nothing writes the model on open. The folder binding is only
        # ever written by an explicit user action (_set_folder). Auto-heal
        # was removed - it could re-publish a stale local cache over a
        # teammate's synced change (multi-machine review).
        # Each feed independent: one failure can't blank the others.
        for name, fn in (("settings", self._send_settings),
                         ("tests", self._send_tests),
                         ("clashes", self._send_clashes),
                         ("model_info", self._send_model_info)):
            try:
                fn()
            except Exception:
                _log("push_initial({0}): {1} feed failed\n{2}".format(
                    why, name, traceback.format_exc()))
        # Pull the window back to the front once the data has loaded: WebView2
        # spinning up can let Revit's window activate over ours during open, so
        # a belt-and-suspenders Activate() (on top of the Revit owner) ensures
        # the tool is in front when the user first sees it.
        try:
            self.Activate()
        except Exception:
            pass

    # --- host <-> page ------------------------------------------------

    def _post(self, msg):
        try:
            wv = self._webview
            if wv is not None and wv.CoreWebView2 is not None:
                wv.CoreWebView2.PostWebMessageAsString(msg)
        except Exception:
            _log("post failed: {0}".format(traceback.format_exc().splitlines()[-1]))

    def _do_events(self):
        """Pump the WPF dispatcher once so queued input -- crucially the page's
        Abort button posting `abortrun` -- gets processed while a blocking op
        (detection / model export) holds the UI thread. Same mechanism the View
        Range Helper uses for its abortable region export."""
        try:
            from System.Windows.Threading import (
                Dispatcher, DispatcherFrame, DispatcherPriority)
            from System import Action, Object
            frame = DispatcherFrame()

            def _exit(f):
                f.Continue = False
            Dispatcher.CurrentDispatcher.BeginInvoke(
                DispatcherPriority.Background, Action[Object](_exit), frame)
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _cancel_check(self):
        """Hook handed to the exporter (and polled between detection tests).
        Pumps the UI a few times a second -- NOT on every poll, which would
        crawl the export -- so the Abort button stays clickable, then reports
        whether the user asked to stop. The flag read itself is instant."""
        try:
            tc = System.Environment.TickCount
            if tc - self._last_pump >= 150:    # ms; ~6-7 pumps/sec is plenty
                self._last_pump = tc
                self._do_events()
        except Exception:
            pass
        return self._abort_run

    def _on_web_message(self, sender, args):
        try:
            msg = args.TryGetWebMessageAsString()
        except Exception:
            msg = None
        if not msg:
            return
        # Abort is honored at any time (it's the whole point of pumping the
        # loop mid-op). While a blocking op runs, every OTHER message is
        # dropped: it arrived via a re-entrant pump, and starting a second
        # run / export / group-write on top of the first would corrupt state.
        if msg == "abortrun":
            self._abort_run = True
            _log("abortrun: user requested stop")
            return
        if self._op_busy:
            # A clash edit made while detection/export holds the thread must
            # NOT vanish silently (the page shows an optimistic "saved" state):
            # nack it so the page queues the op and retries after the run.
            if msg.startswith("clashop:"):
                try:
                    import json
                    op = json.loads(msg[8:])
                    self._post("clashopdone:" + json.dumps(
                        {"ok": False, "busy": True,
                         "op_id": op.get("op_id"),
                         "clash_id": op.get("clash_id")}))
                except Exception:
                    _log("busy: clashop nack itself failed\n{0}".format(
                        traceback.format_exc()))
                _log("busy: nacked clashop (an operation is already running)")
                return
            # Group edits get an explicit failure too - a silent drop leaves
            # the page's "Applying group change..." status stuck forever.
            if msg.startswith("groupop:"):
                try:
                    import json
                    self._post("groupdone:" + json.dumps(
                        {"ok": False,
                         "error": "The tool is busy running detection or an "
                                  "export. Make the change again when it "
                                  "finishes."}))
                except Exception:
                    pass
                _log("busy: nacked groupop")
                return
            _log("busy: ignoring '{0}' (an operation is already running)".format(
                msg[:40]))
            return
        try:
            if msg == "export":
                self._export_flow(manual=True)
            elif msg == "ready":
                _log("ready: received from page")
                self._push_initial("page-ready")
            elif msg == "loadlast":
                self._load_last_export()
            elif msg.startswith("snapshot:"):
                self._save_snapshot(msg[9:])
            elif msg.startswith("gsnapshot:"):
                self._save_group_snapshot(msg[10:])
            elif msg == "clearthumbs":
                self._clear_thumbs()
            elif msg.startswith("setrole:"):
                import json
                try:
                    self._set_link_role(json.loads(msg[8:]))
                except Exception:
                    _log("setrole failed\n{0}".format(traceback.format_exc()))
            elif msg == "setfolder":
                self._set_folder()
            elif msg.startswith("runtests:"):
                import json
                try:
                    ids = json.loads(msg[9:])
                except Exception:
                    ids = []
                self._run_tests(ids)
            elif msg.startswith("groupop:"):
                self._handle_groupop(msg[8:])
            elif msg.startswith("clashop:"):
                self._handle_clashop(msg[8:])
            elif msg.startswith("showinrevit:"):
                self._handle_showinrevit(msg[12:])
            elif msg.startswith("diag:"):
                _log("page: {0}".format(msg[5:]))
            # cam:/filters:/other page messages are ignored for now
        except Exception:
            _log("on_web_message: {0}".format(traceback.format_exc()))

    def _export_flow(self, manual=False):
        """Run one model export behind the page's run modal, abortable throughout.
        `manual=True` is the '3D Viewer > Update from Revit' button; otherwise
        it's the auto-export tail after a clash run. Sets the busy gate (so the
        Abort button's pumped `abortrun` is the only message honored mid-export)
        and translates the export outcome into a page stage message."""
        self._abort_run = False
        self._op_busy = True
        try:
            self._post("runstage:export" if not manual else "runstage:exportmanual")
            code = self._do_export(is_canceled=self._cancel_check)
        finally:
            self._op_busy = False
            # The export drives the Revit API and pumps the message loop, which
            # can let Revit's window activate over ours; pull the tool back to
            # the front when it finishes (the Revit owner keeps it there after).
            try:
                self.Activate()
            except Exception:
                pass
        if code == "canceled":
            self._post("runstage:canceled")
        elif code == "fail":
            self._post("runstage:exportfail")
        # "ok": _do_export already posted url:, the page moves itself to the
        # image-capture stage on receiving it.
        return code

    def _do_export(self, is_canceled=None):
        """Export the active Revit model to glTF and hand its URL to the page.
        Returns "ok" | "canceled" | "fail". `is_canceled` is polled by the
        CustomExporter so the user can abort a multi-minute export; a clean
        abort never touches user data (the export runs in a throwaway view)."""
        from clash_export import custom_export
        try:
            doc = revit.doc
        except Exception:
            doc = None
        if doc is None:
            self._post("status:No active Revit document to export.")
            return "fail"
        try:
            if not os.path.isdir(MODELS_DIR):
                os.makedirs(MODELS_DIR)
            path = os.path.join(MODELS_DIR, _safe_title(doc) + '.glb')
            _log("export: starting -> {0}".format(path))
            self._post("status:Exporting the model snapshot from Revit... "
                       "(large models take a few minutes)")
            stats = None
            try:
                # image_mode: a leaner snapshot tuned for clash images -- drops
                # pure-context clutter categories (furniture, casework, ...),
                # per-vertex normals, and textures, so a huge federated model
                # fits the renderer. keep_category_ids protects any category a
                # clash test actually uses (incl. custom tests) from the prune.
                ce = custom_export.export_view(
                    doc, path, is_canceled=is_canceled,
                    image_mode=True, keep_category_ids=self._test_category_ids())
                if ce.get("elements", 0) > 0 and ce.get("bytes", 0) > 0:
                    stats = ce
                    _log("export: CustomExporter ok ({0} elements)".format(ce.get("elements")))
            except custom_export.ExportCanceled:
                # Clean user abort: the throwaway view is already gone and the
                # partial file removed. Do NOT fall back (that restarts a full
                # export the user just cancelled).
                _log("export: canceled by user")
                return "canceled"
            except Exception:
                _log("export: CustomExporter failed\n{0}".format(traceback.format_exc()))
                stats = None
            if stats is None:
                # Geometry-API fallback has no cancel hook; a stop request here
                # bails before we relaunch a long export.
                if is_canceled is not None and is_canceled():
                    return "canceled"
                try:
                    from clash_export import revit_geometry
                    revit_geometry.export_model(doc, path)
                    _log("export: geometry-API fallback ok")
                except Exception:
                    _log("export: fallback failed\n{0}".format(traceback.format_exc()))
                    self._post("status:Export failed. See coord.log.")
                    return "fail"
            self._model_version += 1
            try:
                size_bytes = os.path.getsize(path)
            except Exception:
                size_bytes = 0
            size_mb = int(round(size_bytes / 1048576.0))
            if size_bytes > GLB_HARD_MAX:
                # Loading this in the renderer would OOM (peak ~= 3-5x file).
                # Do NOT hand it to the page. The clashes are already saved; the
                # images are the only thing skipped, and the page says so.
                _log("export: {0} MB exceeds hard cap {1} MB -- NOT loading "
                     "(would OOM); posting toobig".format(
                         size_mb, GLB_HARD_MAX // 1048576))
                self._post("runstage:toobig:{0}".format(size_mb))
                self._send_clashes()
                return "ok"
            if size_bytes > GLB_SOFT_WARN:
                self._post("modelwarn:{0}".format(size_mb))
            url = "https://{0}/models/{1}?v={2}".format(
                VHOST, os.path.basename(path), self._model_version)
            _log("export: done ({0} MB), posting {1}".format(size_mb, url))
            self._post("url:" + url)
            self._send_clashes()
            return "ok"
        except Exception:
            _log("export: EXCEPTION\n{0}".format(traceback.format_exc()))
            self._post("status:Export error. See coord.log.")
            return "fail"

    def _test_category_ids(self):
        """The set of BuiltInCategory ints referenced by the project's effective
        clash tests (set_a/set_b category lists). Handed to the image-mode
        exporter so its context-clutter prune NEVER drops a category a clash
        could involve -- including any custom test's categories. Empty set on
        any failure (which just means the exporter prunes its default blocklist
        outright -- still safe, those categories are never clash participants)."""
        ids = set()
        try:
            from Autodesk.Revit.DB import BuiltInCategory
            names = set()
            for t in self._effective_tests():
                for side in ('set_a', 'set_b'):
                    for c in ((t.get(side) or {}).get('categories') or []):
                        names.add(c)
            for n in names:
                try:
                    ids.add(int(getattr(BuiltInCategory, n)))
                except Exception:
                    pass
        except Exception:
            _log("test_category_ids failed\n{0}".format(traceback.format_exc()))
        return ids

    # --- model persistence + clash snapshots -------------------------------

    def _send_model_info(self):
        """Tell the page whether a previous export of THIS model exists on
        disk, so it can offer 'Load last export' without touching Revit."""
        import json
        info = {"exists": False}
        try:
            doc = revit.doc
            if doc is not None:
                path = os.path.join(MODELS_DIR, _safe_title(doc) + '.glb')
                if os.path.isfile(path):
                    st = os.stat(path)
                    from datetime import datetime
                    info = {
                        "exists": True,
                        "file": os.path.basename(path),
                        "mb": round(st.st_size / 1048576.0, 1),
                        "when": datetime.fromtimestamp(st.st_mtime).strftime("%m/%d %I:%M %p"),
                    }
        except Exception:
            pass
        try:
            self._post("model:" + json.dumps(info, ensure_ascii=False))
        except Exception:
            pass

    def _load_last_export(self):
        """Hand the page the EXISTING .glb (no Revit export) -- the fast path
        for reopening the tool on a model that was already exported."""
        try:
            doc = revit.doc
            if doc is None:
                self._post("status:No active Revit document.")
                return
            path = os.path.join(MODELS_DIR, _safe_title(doc) + '.glb')
            if not os.path.isfile(path):
                self._post("status:No previous export found. Use 'Update from Revit'.")
                return
            try:
                size_bytes = os.path.getsize(path)
            except Exception:
                size_bytes = 0
            if size_bytes > GLB_HARD_MAX:
                # Same guard as _do_export: the reopen path can hit the same
                # oversized file. Refuse it rather than OOM the renderer.
                size_mb = int(round(size_bytes / 1048576.0))
                _log("loadlast: {0} MB exceeds hard cap -- not loading".format(size_mb))
                self._post("status:The last export ({0} MB) is too large to show "
                           "in 3D here. Your clash data is unaffected.".format(size_mb))
                self._post("toobiglast:{0}".format(size_mb))
                return
            self._model_version += 1
            url = "https://{0}/models/{1}?v={2}".format(
                VHOST, os.path.basename(path), self._model_version)
            _log("loadlast: posting {0}".format(url))
            self._post("url:" + url)
        except Exception:
            _log("loadlast: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _viewpoints_dir(self):
        """The bound folder's viewpoints/ dir (created), or None if unbound."""
        from clash_core import binding
        doc = revit.doc
        folder = binding.folder_for(doc) if doc is not None else None
        if not folder:
            return None
        vp_dir = os.path.join(folder, 'viewpoints')
        if not os.path.isdir(vp_dir):
            os.makedirs(vp_dir)
        return vp_dir

    @staticmethod
    def _safe_image_id(image_id):
        """Ids are uuids from merge/grouping; never let one walk the path."""
        return bool(image_id) and "/" not in image_id and "\\" not in image_id \
            and ".." not in image_id

    @staticmethod
    def _decode_dataurl(dataurl):
        """(bytes, ext) from a page data URL. The page captures JPEG now;
        PNG is kept for compatibility with anything older."""
        import base64
        ext = '.jpg' if dataurl.startswith('data:image/jpeg') else '.png'
        b64 = dataurl.split(",", 1)[1] if "," in dataurl else dataurl
        return base64.b64decode(b64), ext

    @staticmethod
    def _write_image(vp_dir, stem, raw, ext):
        """Atomic image write; drops the other-extension leftover so a JPEG
        recapture replaces a legacy PNG instead of shadowing it."""
        out = os.path.join(vp_dir, stem + ext)
        tmp = out + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(raw)
        if os.path.isfile(out):
            os.remove(out)
        os.rename(tmp, out)
        other = os.path.join(vp_dir, stem + ('.png' if ext == '.jpg' else '.jpg'))
        try:
            if os.path.isfile(other):
                os.remove(other)
        except Exception:
            pass
        return out

    def _save_snapshot(self, payload):
        """Persist one clash's context image: 'snapshot:<clash-id>:<dataurl>'
        -> <folder>/viewpoints/<clash-id>.jpg, directly inside this project's
        clash-data folder. Images survive across sessions, so a clash can show
        its context view without the 3D model being loaded."""
        try:
            clash_id, dataurl = payload.split(":", 1)
            if not self._safe_image_id(clash_id):
                return
            raw, ext = self._decode_dataurl(dataurl)
            vp_dir = self._viewpoints_dir()
            if not vp_dir:
                return
            self._write_image(vp_dir, clash_id, raw, ext)
        except Exception:
            _log("save_snapshot: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _save_group_snapshot(self, payload):
        """Persist one issue's aggregate photo:
        'gsnapshot:<group-id>:<member-hash>:<dataurl>'
        -> <folder>/viewpoints/issue_<group-id>.jpg plus a row in
        viewpoints/issues.json recording the member-roster hash, so a
        membership change on a later run invalidates the photo."""
        import json
        try:
            group_id, rest = payload.split(":", 1)
            member_hash, dataurl = rest.split(":", 1)
            if not self._safe_image_id(group_id) \
                    or not self._safe_image_id(member_hash):
                return
            raw, ext = self._decode_dataurl(dataurl)
            vp_dir = self._viewpoints_dir()
            if not vp_dir:
                return
            fname = 'issue_' + group_id + ext
            self._write_image(vp_dir, 'issue_' + group_id, raw, ext)
            manifest = self._read_issue_manifest(vp_dir)
            manifest[group_id] = {'hash': member_hash, 'file': fname}
            mpath = os.path.join(vp_dir, 'issues.json')
            tmp = mpath + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
            if os.path.isfile(mpath):
                os.remove(mpath)
            os.rename(tmp, mpath)
        except Exception:
            _log("save_group_snapshot: EXCEPTION\n{0}".format(
                traceback.format_exc()))

    @staticmethod
    def _read_issue_manifest(vp_dir):
        """viewpoints/issues.json -> {group_id: {hash, file}}; {} on any
        failure (a corrupt manifest just means photos re-capture)."""
        import json
        try:
            mpath = os.path.join(vp_dir, 'issues.json')
            if not os.path.isfile(mpath):
                return {}
            with open(mpath, 'rb') as f:
                data = json.loads(f.read().decode('utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _clear_thumbs(self):
        """Delete every saved clash/issue image (viewpoints/ only ever holds
        tool-written captures) so the page can rebuild them in the current
        style. Fired by the page's 'Recapture images' button, which confirms
        with the user first."""
        try:
            from clash_core import binding
            doc = revit.doc
            folder = binding.folder_for(doc) if doc is not None else None
            if not folder:
                return
            vp_dir = os.path.join(folder, 'viewpoints')
            if not os.path.isdir(vp_dir):
                return
            n = 0
            for name in os.listdir(vp_dir):
                low = name.lower()
                if low.endswith('.png') or low.endswith('.jpg') \
                        or name == 'issues.json':
                    try:
                        os.remove(os.path.join(vp_dir, name))
                        n += 1
                    except Exception:
                        pass
            _log("clear_thumbs: removed {0} file(s)".format(n))
            self._send_clashes()
        except Exception:
            _log("clear_thumbs: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _map_data_host(self, folder):
        """(Re)point the clash-data virtual host at `folder` so the page can load
        persisted thumbnails from '<folder>/viewpoints/'. Called at init and
        whenever the folder changes. Clears any prior mapping first."""
        self._datahost_ok = False
        try:
            wv = self._webview
            core = wv.CoreWebView2 if wv is not None else None
            allow = getattr(self, '_allow', None)
            if core is None or allow is None:
                return
            try:
                core.ClearVirtualHostNameToFolderMapping(VHOST_DATA)
            except Exception:
                pass
            if folder and os.path.isdir(folder):
                core.SetVirtualHostNameToFolderMapping(VHOST_DATA, folder, allow)
                self._datahost_ok = True
                _log("data vhost -> {0}".format(folder))
        except Exception:
            _log("map_data_host failed\n{0}".format(traceback.format_exc()))

    def _thumb_url(self, folder, clash_id):
        """URL of a clash's persisted thumbnail under this project's folder, or
        None. The data vhost maps to `folder`, so the path is a flat
        '/viewpoints/<id>.jpg' (new captures) or '.png' (legacy)."""
        try:
            if not getattr(self, '_datahost_ok', False) or not folder or not clash_id:
                return None
            for ext in ('.jpg', '.png'):
                p = os.path.join(folder, 'viewpoints', clash_id + ext)
                if os.path.isfile(p):
                    return "https://{0}/viewpoints/{1}{2}?v={3}".format(
                        VHOST_DATA, clash_id, ext, int(os.path.getmtime(p)))
        except Exception:
            pass
        return None

    def _group_thumb(self, folder, manifest, group_id):
        """(url, member-hash) of an issue's persisted aggregate photo, from the
        issues.json manifest. (None, None) when there's no fresh photo."""
        try:
            info = manifest.get(group_id) or {}
            fname = info.get('file')
            if not getattr(self, '_datahost_ok', False) or not folder \
                    or not fname or not self._safe_image_id(fname):
                return None, None
            p = os.path.join(folder, 'viewpoints', fname)
            if os.path.isfile(p):
                url = "https://{0}/viewpoints/{1}?v={2}".format(
                    VHOST_DATA, fname, int(os.path.getmtime(p)))
                return url, info.get('hash')
        except Exception:
            pass
        return None, None

    # --- settings ---------------------------------------------------------

    def _send_settings(self):
        """Post the settings snapshot: this project's clash-data folder (the
        tool's only piece of state) and the linked-model role mapping.

        `linked` = the model has a folder set (clash_core.binding); `folder` =
        that absolute path. When not set, the tool shows nothing until the user
        points it at a folder."""
        import json
        payload = {"linked": False, "folder": None, "project": None, "links": []}
        try:
            from clash_core import binding, persistence
            from clash_detect import linked
            doc = revit.doc
            folder = binding.folder_for(doc) if doc is not None else None
            if folder:
                payload["linked"] = True
                payload["folder"] = folder
                # False when the binding lives only in this machine's
                # registry (model not saved since it was set): the page
                # shows a save-the-model hint so teammates get it too.
                payload["folderInModel"] = bool(binding.model_folder(doc))
                meta = persistence.read_project_meta_at(folder)
                payload["project"] = {
                    "folder": folder,
                    "display_name": meta.get("display_name") or (doc.Title if doc else None),
                }
                role_map = meta.get("link_role_map") or {}
                payload["links"] = linked.merged_link_view(doc, role_map)
            elif doc is not None:
                # Not set up yet: show the live links (role editing waits until
                # a folder is set, since there's nowhere to store the mapping).
                payload["links"] = linked.merged_link_view(doc, {})
                payload["project"] = {"folder": None,
                                      "display_name": getattr(doc, "Title", None)}
        except Exception:
            _log("send_settings: project part failed\n{0}".format(traceback.format_exc()))
        try:
            self._post("settings:" + json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _refresh_all(self):
        """Re-push every project feed after the folder changes."""
        self._send_settings()
        self._send_tests()
        self._send_clashes()
        self._send_model_info()

    def _set_folder(self):
        """Point this project at a clash-data folder. Opens a folder browser,
        stores the chosen absolute path INSIDE the model (Extensible Storage) so
        it travels with the file and is the same for every teammate, then
        reloads everything from that folder. This is the tool's ONLY setup step:
        the folder is the project's entire state. Whatever clash data is in the
        folder is what shows; an empty folder shows an empty tool."""
        from clash_core import binding, persistence
        doc = revit.doc
        if doc is None:
            self._post("status:No active Revit document.")
            return
        # Browse for the folder (the user makes/points at it -- e.g. a "Clash
        # Data" folder in the firm's project structure). We don't create or
        # rename anything and don't care whether it's local or on the network.
        try:
            clr.AddReference('System.Windows.Forms')
            from System.Windows.Forms import FolderBrowserDialog, DialogResult
            dlg = FolderBrowserDialog()
            dlg.Description = ("Pick this project's clash data folder "
                               "(everyone who opens the model uses this same path)")
            dlg.ShowNewFolderButton = True
            cur = binding.folder_for(doc)
            if cur and os.path.isdir(cur):
                dlg.SelectedPath = cur
            if dlg.ShowDialog() != DialogResult.OK:
                return
            folder = dlg.SelectedPath
        except Exception:
            _log("set_folder: browser failed\n{0}".format(traceback.format_exc()))
            self._post("status:Could not open the folder picker. See coord.log.")
            return
        if not folder:
            return
        # Store the path in the model (transaction; travels with the file).
        # Then VERIFY it actually landed by reading it back: on a workshared
        # model where a teammate owns Project Information, SetEntity can fail
        # to persist without a hard throw, and we must NOT report a false
        # success (review finding). The per-machine registry is written ONLY
        # after a confirmed commit + read-back, so a rolled-back transaction
        # can never poison the local cache.
        model_ok = False
        try:
            with revit.Transaction("dbHMS: set clash-data folder"):
                binding.write_binding(doc, folder)
            status, path = binding.read_model(doc)
            model_ok = (status == binding.BOUND and path == folder)
            _log("set_folder: wrote {0}; read-back status={1} ok={2}".format(
                folder, status, model_ok))
        except Exception:
            _log("set_folder: write_binding failed\n{0}".format(traceback.format_exc()))
        # Always remember locally so THIS machine resolves the folder even if
        # the model write couldn't persist (borrowed element, read-only).
        try:
            binding.remember_local(doc, folder)
        except Exception:
            pass
        if not model_ok:
            # The folder works on this machine (registry), but it's not in the
            # model yet, so teammates won't get it. Tell the user plainly.
            self._post("status:Folder set on your machine. It could NOT be "
                       "saved into the model yet (another user may be editing "
                       "Project Information, or the model is read-only) - "
                       "teammates won't see it until you set it again with "
                       "edit access and save.")
        # Seed project.json with a display name so the folder isn't anonymous.
        try:
            meta = persistence.read_project_meta_at(folder)
            if not meta.get('display_name'):
                meta['display_name'] = getattr(doc, 'Title', None)
                persistence.write_project_meta_at(folder, meta)
        except Exception:
            pass
        # Repoint the thumbnail vhost at the new folder, then reload everything
        # from it (or empty). Nothing from the previous folder survives.
        self._map_data_host(folder)
        self._post("status:Clash data folder set: {0}".format(folder))
        self._refresh_all()

    def _set_link_role(self, req):
        """Persist one link's role (Architectural / Structural / ignore) to the
        project's link_role_map. Requires a clash-data folder to be set first
        (there's nowhere to store per-project data until then)."""
        from clash_core import binding, persistence
        title = (req or {}).get("title")
        role = (req or {}).get("role")
        if not title or role not in ("Architectural", "Structural", "ignore"):
            return
        doc = revit.doc
        ph = binding.folder_for(doc) if doc is not None else None
        if not ph:
            self._post("status:Link this project to a clash-data folder first "
                       "(Settings), then set link roles.")
            return
        meta = persistence.read_project_meta_at(ph)
        role_map = meta.get("link_role_map") or {}
        role_map[title] = role
        meta["link_role_map"] = role_map
        if not meta.get("display_name"):
            try:
                meta["display_name"] = doc.Title
            except Exception:
                pass
        persistence.write_project_meta_at(ph, meta)
        _log("settings: link '{0}' -> {1}".format(title, role))
        self._send_settings()

    # --- tests: library + run --------------------------------------------

    def _effective_tests(self):
        """The effective test list for this project: the firm default tests that
        ship with the tool (default_tests.json next to this script) minus any
        disabled overrides, plus the project's custom tests. Project overrides
        live in the project's clash-data folder (test_overrides.json); with no
        folder set, only the firm defaults show. There is no separate firm-wide
        library file anymore -- the defaults are bundled with the tool."""
        import json
        try:
            tests = []
            seed_path = os.path.join(SCRIPT_DIR, 'default_tests.json')
            try:
                with open(seed_path, 'r') as f:
                    seed = json.load(f)
                tests = seed.get('tests') or []
            except Exception:
                _log("tests: default_tests.json read failed\n{0}".format(
                    traceback.format_exc()))
            disabled = []
            custom = []
            try:
                from clash_core import binding, persistence
                doc = revit.doc
                ph = binding.folder_for(doc) if doc is not None else None
                if ph:
                    ov = persistence.read_overrides_at(ph)
                    disabled = ov.get('disabled_test_ids') or []
                    custom = ov.get('custom_tests') or []
            except Exception:
                pass
            effective = [t for t in tests if t.get('id') not in disabled]
            for t in effective:
                t['_scope'] = 'firm'
            for ct in custom:
                ct['_scope'] = 'project'
                effective.append(ct)
            return effective
        except Exception:
            _log("tests: read failed\n{0}".format(traceback.format_exc()))
            return []

    @staticmethod
    def _humanize_side(side):
        """Short human summary of one test side ({source, categories})."""
        try:
            cats = [c.replace('OST_', '') for c in (side.get('categories') or [])]
            # split CamelCase-ish names lightly: DuctCurves -> Ducts is too clever;
            # keep the raw names but cap the list so the row stays scannable.
            shown = ', '.join(cats[:3]) + (' +{0} more'.format(len(cats) - 3) if len(cats) > 3 else '')
            src = side.get('source')
            if isinstance(src, list):
                labels = ['host' if s == 'host' else s.split(':', 1)[-1] for s in src]
                src_label = ' + '.join(labels)
            else:
                src_label = 'host' if (src or 'host') == 'host' else str(src).split(':', 1)[-1]
            return "{0} ({1})".format(shown or '?', src_label)
        except Exception:
            return "?"

    def _send_tests(self):
        """Post the effective test list to the page's Tests view."""
        import json
        tests = self._effective_tests()
        last_run_at = None
        tests_run = []
        try:
            from clash_core import binding, persistence
            doc = revit.doc
            ph = binding.folder_for(doc) if doc is not None else None
            if ph:
                meta = persistence.read_clashes_at(ph)
                last_run_at = meta.get('last_run_at')
                tests_run = meta.get('tests_run') or []
        except Exception:
            pass
        rows = []
        for t in tests:
            try:
                rows.append({
                    "id":        t.get('id'),
                    "name":      t.get('name') or t.get('id'),
                    "kind":      t.get('kind') or 'hard',
                    "tolerance": t.get('tolerance_inches') or 0.0,
                    "a":         self._humanize_side(t.get('set_a') or {}),
                    "b":         self._humanize_side(t.get('set_b') or {}),
                    "scope":     t.get('_scope') or 'firm',
                    "last_run":  last_run_at if t.get('id') in tests_run else None,
                })
            except Exception:
                continue
        try:
            payload = json.dumps(rows, ensure_ascii=False)
        except Exception:
            payload = "[]"
        _log("send_tests: {0} tests".format(len(rows)))
        self._post("tests:" + payload)

    def _detect_status(self, prefix, test_index, test_count):
        """Build the per-test status callback the detector calls on every
        heartbeat. It (1) pumps the UI so Abort stays live, (2) streams the
        text to the page, and (3) parses the '<done>/<total> elements' count
        into an overall run percentage for the modal's progress bar."""
        import re
        elem_re = re.compile(r'(\d+)\s*/\s*(\d+)\s+elements')

        def _cb(m):
            self._cancel_check()   # throttled pump; keeps Abort responsive
            self._post("status:" + prefix + m)
            try:
                mo = elem_re.search(m)
                if mo:
                    done_n = float(mo.group(1))
                    total_n = float(mo.group(2)) or 1.0
                    frac = done_n / total_n
                    if frac < 0:
                        frac = 0.0
                    if frac > 1:
                        frac = 1.0
                    pct = (test_index + frac) / max(1, test_count) * 100.0
                    self._post("runprog:{0:.1f}".format(pct))
            except Exception:
                pass
        return _cb

    def _run_tests(self, test_ids):
        """Run the selected clash tests through the real detection pipeline --
        the same steps as the Run Clash Test button (runner -> dedupe -> merge ->
        atomic write), minus the WPF form and minus viewpoint PNG generation
        (this tool renders clashes in its own 3D pane; the Browser's catch-up
        still fills PNGs for the old tools). Synchronous on the UI thread, same
        as the model export; status posts keep the page informed."""
        import json
        from clash_core import binding, persistence, users, dedupe, merge
        from clash_core.models import _now_iso
        from clash_detect import runner, pairgeom

        def done(ok, message, summary=None):
            payload = {"ok": ok, "message": message}
            if summary:
                payload["summary"] = summary
            try:
                self._post("rundone:" + json.dumps(payload, ensure_ascii=False))
            except Exception:
                pass

        try:
            doc = revit.doc
        except Exception:
            doc = None
        if doc is None:
            done(False, "No active Revit document.")
            return
        try:
            ph = binding.folder_for(doc)
            if not ph:
                done(False, "This project isn't pointed at a clash-data folder yet. "
                            "Open Settings and click \"Set up clash data folder\", then re-run.")
                return
        except Exception:
            done(False, "Could not resolve the project folder. See coord.log.")
            _log("run_tests: project resolve failed\n{0}".format(traceback.format_exc()))
            return

        tests = [t for t in self._effective_tests() if t.get('id') in set(test_ids or [])]
        if not tests:
            done(False, "No matching tests to run.")
            return

        try:
            meta = persistence.read_project_meta_at(ph)
            role_map = meta.get('link_role_map') or {}
            # First run on a new folder: seed the display name so project.json
            # isn't anonymous (Settings normally does this).
            if not meta.get('display_name'):
                try:
                    meta['display_name'] = doc.Title
                    persistence.write_project_meta_at(ph, meta)
                except Exception:
                    pass
        except Exception:
            role_map = {}
        try:
            author = users.current_user(revit.uiapp)
        except Exception:
            author = "Unknown"

        # Loud warning when the selected tests reference linked models but this
        # project has no usable role mapping -- otherwise linked clashes are
        # silently skipped and the run looks "done" while missing real hits
        # (exactly what a fresh project folder does before Settings is opened).
        link_warning = None
        try:
            needed = set()
            for t in tests:
                for side in ('set_a', 'set_b'):
                    src = (t.get(side) or {}).get('source')
                    srcs = src if isinstance(src, list) else [src]
                    for s in srcs:
                        if s and str(s).startswith('link:'):
                            needed.add(str(s).split(':', 1)[1])
            if needed:
                from clash_detect import linked
                view = linked.merged_link_view(doc, role_map)
                mapped_roles = set(r.get('role') for r in view
                                   if r.get('role') and r.get('role') != 'ignore')
                missing = sorted(needed - mapped_roles)
                loaded_titles = [r.get('title') for r in view]
                # Warn whenever a needed role is unmapped -- REGARDLESS of
                # whether any links are loaded. The old `and loaded_titles`
                # guard swallowed the warning on a fresh folder with unloaded
                # links, which is exactly the silent failure that produced the
                # NIUHTC "arch/structure never ran" dataset (v2 plan 5.7).
                if missing:
                    if loaded_titles:
                        link_warning = (
                            "Warning: linked model(s) {0} are loaded but not mapped to a role, "
                            "so the {1} side(s) of these tests found nothing. Open Settings on "
                            "the panel and set the link role mapping, then re-run."
                            .format(", ".join("'{0}'".format(t) for t in loaded_titles if t),
                                    " / ".join(missing)))
                    else:
                        link_warning = (
                            "Warning: these tests need linked model role(s) {0}, but no linked "
                            "models are mapped to them -- architecture/structure clashes were "
                            "NOT checked. Open Settings on the panel, set the link role "
                            "mapping, then re-run.".format(" / ".join(missing)))
        except Exception:
            link_warning = None

        raw = []
        errors = []
        diags = []        # per-test zero-row-alarm diagnostics (v2 plan 5.7)
        tess_cache = {}   # one cache for the whole run: shared elements tessellate once
        ins_cache = {}    # one insulation map per document per run
        geom_cache = pairgeom.new_cache()   # Phase 2 pair geometry: run-wide
                                            # boolean time budget + solid cache
        # Detection blocks the UI thread. Set the busy gate and pump the loop
        # through the status callback so the modal's Abort button stays live;
        # the abort is acted on between tests (a single test can't be stopped
        # mid-pass) and discards everything -- nothing is written on cancel.
        self._abort_run = False
        self._op_busy = True
        canceled = False
        n = len(tests)
        try:
            for i, t in enumerate(tests):
                if self._abort_run:
                    canceled = True
                    break
                name = t.get('name') or t.get('id')
                self._post("status:Running {0}/{1}: {2}...".format(i + 1, n, name))
                _log("run_tests: {0}/{1} {2}".format(i + 1, n, name))
                # Live heartbeat inside long tests: the page status line shows
                # "N/M elements checked, K clashes" and the modal bar advances.
                prefix = "Running {0}/{1}: {2} - ".format(i + 1, n, name)
                status = self._detect_status(prefix, i, n)
                try:
                    _rows, _diag = runner.run_test(doc, t, role_map, log=_log,
                                                   tess_cache=tess_cache,
                                                   ins_cache=ins_cache,
                                                   status=status,
                                                   geom_cache=geom_cache)
                    raw.extend(_rows)
                    diags.append(_diag)
                except Exception:
                    errors.append(name)
                    _log("run_tests: test '{0}' FAILED\n{1}".format(name, traceback.format_exc()))
        finally:
            self._op_busy = False
        if canceled:
            _log("run_tests: canceled by user before saving (nothing written)")
            done(False, "Run canceled before saving. Nothing was changed -- "
                        "your previous clash data is untouched.")
            self._post("runstage:detectcancel")
            return
        try:
            if raw:
                raw, _dropped = dedupe.drop_soft_overlapping_hard(raw)
                raw, _layered = dedupe.collapse_layered_penetrations(raw)
                raw, _protband = dedupe.drop_redundant_protected_band(raw)
                if _layered:
                    self._post("status:Collapsed {0} stacked-layer "
                               "penetrations...".format(_layered))
            self._post("status:Merging with previous results...")
            existing = persistence.read_clashes_at(ph)
            run_iso = _now_iso()
            merged, summary = merge.merge_runs(
                existing.get('clashes') or [], raw, run_iso=run_iso, author=author)
            # Importance engine: stamp band/score/reason on every merged
            # clash (pure math over the merged list; see lib/clash_score).
            # Best-effort: a scoring failure must never fail a run.
            try:
                import clash_score
                self._post("status:Scoring importance...")
                clash_score.score_all(merged)
            except Exception:
                _log("run_tests: scoring FAILED (non-fatal)\n{0}".format(
                    traceback.format_exc()))
            # Layer C grouping: reconcile sticky issue groups and form new
            # ones (lib/clash_group). regroup_all deep-copies the incoming
            # groups, so a failure here safely falls back to the previous
            # run's groups untouched.
            groups = existing.get('groups') or []
            try:
                import clash_group
                self._post("status:Grouping...")
                groups, _gsummary = clash_group.regroup_all(
                    merged, groups, run_iso=run_iso)
            except Exception:
                _log("run_tests: grouping FAILED (non-fatal)\n{0}".format(
                    traceback.format_exc()))
            # Zero-row alarm: flag suspect tests (rows==0 with an empty side
            # or an unresolved link role) and ride the diagnostics into
            # last_run_summary. Expected-empty clearance tests are never
            # suspect: a clearance test with no owner gear (e.g. M-SPR on a
            # model with no modeled sprinkler heads) is dormant by design, not
            # a silent failure (v2 plan 5.7 + Phase 4).
            for d in diags:
                rr = d.get('roles_resolved') or {}
                d['suspect'] = bool(
                    d.get('rows', 0) == 0
                    and d.get('skipped_reason') not in (
                        'clearance_stub', 'no_owner_elements')
                    and (d.get('skipped_reason') in ('side_a_empty', 'side_b_empty')
                         or any(v == 0 for v in rr.values())))
            try:
                summary['test_diagnostics'] = diags
            except Exception:
                pass
            new_data = {
                'schema_version': existing.get('schema_version', 1),
                'last_run_at':    run_iso,
                # Real since-last-run numbers for the Home banner.
                'last_run_summary': summary,
                'tests_run':      [t.get('id') for t in tests],
                'clashes':        merged,
                # Layer C groups live INSIDE clashes.json (atomicity).
                # Never drop this key: named groups die silently.
                'groups':         groups,
            }
            persistence.write_clashes_at(ph, new_data)
        except Exception:
            _log("run_tests: merge/write FAILED\n{0}".format(traceback.format_exc()))
            done(False, "Detection ran but saving results failed. See coord.log.")
            return
        # No Revit-side image rendering on run: detection writes results and
        # the grid shows them immediately. Images come from the web viewer:
        # the auto-export below hands the page a fresh model snapshot and the
        # page captures clash/issue photos in the background while the user
        # reviews (the old per-clash temp-3D-view pipeline took hours).
        msg = "Run complete: {0} new, {1} persisting, {2} auto-resolved, {3} reopened.".format(
            summary.get('new', 0), summary.get('persisting', 0),
            summary.get('auto_resolved', 0), summary.get('reopened', 0))
        if errors:
            msg += " ({0} test(s) failed: {1})".format(len(errors), ", ".join(errors))
        if link_warning:
            msg += " " + link_warning
        n_suspect = len([d for d in diags if d.get('suspect')])
        if n_suspect:
            msg += (" {0} test(s) stored zero rows and look wrong (a linked role "
                    "resolved nothing, or a side collected nothing) -- see the "
                    "per-test breakdown in the run summary.".format(n_suspect))
        _log("run_tests: " + msg)
        # Safe point: clash data is on disk. Everything after this is the
        # optional image tail -- fully abortable, and its failure never risks
        # the saved run.
        done(True, msg, summary)
        self._send_clashes()
        self._send_tests()
        # Auto-refresh the 3D snapshot so images capture right away and match
        # the model state that was just tested. Skipped when there is nothing
        # to photograph. _export_flow owns the abort + stage messaging; the
        # page's modal reacts to the stages it posts.
        if merged:
            try:
                self._export_flow(manual=False)
            except Exception:
                _log("run_tests: auto-export failed (non-fatal)\n{0}".format(
                    traceback.format_exc()))
                self._post("runstage:exportfail")
        else:
            # No clashes to photograph -- tell the page so its modal closes out
            # instead of hanging on "preparing images".
            self._post("runstage:noimages")

    def _handle_groupop(self, payload):
        """Apply one human group operation (the tool's first write-back
        channel). Read-modify-write of clashes.json: the read preserves
        every top-level key, only clashes/groups are touched, one atomic
        write. Always acks with groupdone: so a stale host surfaces as an
        error instead of silence."""
        import json
        ok, error = False, None
        try:
            op = json.loads(payload)
            from clash_core import binding, persistence
            from clash_group import ops as group_ops
            doc = revit.doc
            ph = binding.folder_for(doc) if doc is not None else None
            if not ph:
                error = 'This project is not pointed at a clash-data folder yet.'
            else:
                data = persistence.read_clashes_at(ph)
                clashes = data.get('clashes') or []
                groups = data.get('groups') or []
                try:
                    from clash_core import users
                    user = users.current_user(revit.uiapp)
                except Exception:
                    user = 'user'
                changed, error = group_ops.apply_op(
                    clashes, groups, op, user=user)
                if error is None:
                    data['clashes'] = clashes
                    data['groups'] = groups
                    persistence.write_clashes_at(ph, data)
                    ok = True
        except Exception:
            _log("groupop failed\n{0}".format(traceback.format_exc()))
            error = 'The operation failed. See coord.log.'
        try:
            self._post("groupdone:" + json.dumps(
                {"ok": ok, "error": error}, ensure_ascii=False))
        except Exception:
            pass
        if ok:
            self._send_clashes()

    def _handle_clashop(self, payload):
        """Apply one per-clash edit (status / trade / deadline / comment):
        the per-clash sibling of _handle_groupop, and the channel that makes
        the clash card editable in both tabs. Read-modify-write of
        clashes.json, one atomic write, always acks with clashopdone: (the
        page runs optimistically and reconciles on the ack). The op may
        carry 'camera' (host feet {position,target,up}) which is stored as
        the clash's viewpoint so reopening the clash lands in the view the
        decision was made in."""
        import json
        ok, error, clash_id, op_id = False, None, None, None
        ph, delta, rollup_gid = None, None, None
        try:
            op = json.loads(payload)
            clash_id = op.get('clash_id')
            op_id = op.get('op_id')
            kind = op.get('op')
            from clash_core import binding, persistence, bulk_edit, models
            doc = revit.doc
            ph = binding.folder_for(doc) if doc is not None else None
            if not ph:
                error = 'This project is not pointed at a clash-data folder yet.'
            elif not clash_id:
                error = 'No clash id in the request.'
            else:
                data = persistence.read_clashes_at(ph)
                clashes = data.get('clashes') or []
                target = None
                for c in clashes:
                    if c.get('id') == clash_id:
                        target = c
                        break
                if target is None:
                    error = 'That clash is not in the database any more.'
                else:
                    try:
                        from clash_core import users
                        user = users.current_user(revit.uiapp)
                    except Exception:
                        user = 'user'
                    changed = False
                    if kind == 'status':
                        st = op.get('status')
                        if not st:
                            error = 'No status value in the request.'
                        else:
                            changed = bulk_edit.apply_status(target, st, user)
                            # The Approve flow rides its reason along as a
                            # note: one write, one ack, and the "why it was
                            # accepted" lands in the comment record.
                            note = (op.get('note') or '').strip()
                            if note:
                                target.setdefault('comments', []).append(
                                    models.make_comment(user, note))
                                changed = True
                    elif kind == 'assign':
                        changed = bulk_edit.apply_trade(
                            target, op.get('assignee'), user)
                    elif kind == 'deadline':
                        changed = bulk_edit.apply_deadline(
                            target, op.get('deadline'), user)
                    elif kind == 'comment':
                        body = (op.get('body') or '').strip()
                        if body:
                            target.setdefault('comments', []).append(
                                models.make_comment(user, body))
                            changed = True
                        else:
                            error = 'Empty comment.'
                    elif kind == 'saveview':
                        # No data change - just persist the current 3D view on
                        # the clash (the "Save this view" button). The view is
                        # stamped below; mark changed so it writes + re-pushes.
                        changed = bool(op.get('view'))
                        if not changed:
                            error = 'No view to save.'
                    else:
                        error = 'Unknown clash operation: {0}'.format(kind)
                    if error is None:
                        # The full saved view (Phase D): camera + section box +
                        # view mode + see-through. Falls back to the legacy
                        # camera-only payload for older pages.
                        view = op.get('view')
                        cam = op.get('camera')
                        if view and changed:
                            self._stamp_clash_view(target, view, user)
                        elif cam and changed:
                            self._stamp_clash_view(target, {'camera': cam}, user)
                        # Status changes ripple into the group's rollup so
                        # the agenda/queue chips stay honest between runs.
                        if changed and kind == 'status' and target.get('group_id'):
                            rollup_gid = self._refresh_group_rollup(
                                data, target.get('group_id'))
                        if changed:
                            data['clashes'] = clashes
                            persistence.write_clashes_at(ph, data)
                        # A no-op edit (already at the value) still acks ok:
                        # the page state and disk already agree.
                        ok = True
                        delta = target
        except Exception:
            _log("clashop failed\n{0}".format(traceback.format_exc()))
            error = 'The edit failed. See coord.log.'
        try:
            self._post("clashopdone:" + json.dumps(
                {"ok": ok, "error": error, "clash_id": clash_id,
                 "op_id": op_id}, ensure_ascii=False))
        except Exception:
            pass
        if ok and delta is not None:
            self._post_clash_delta(ph, data, delta, rollup_gid)
        elif not ok and clash_id and ph:
            # A failed edit resyncs the page: the optimistic UI must never
            # keep showing state that is not on disk.
            try:
                self._send_clashes()
            except Exception:
                pass

    def _stamp_clash_view(self, clash, view, user):
        """Overwrite the clash's single viewpoint with the full 3D view the
        page sent. `view` = {camera:{position,target,up} host feet Z-up,
        box_on, box_pad_ft, mode, fade, ortho}. Keeps the existing snapshot
        path so the saved photo stays paired. The extra web state lives under
        a `web` key so the BCF exporter (camera + section_box) is unaffected.
        Never raises: a bad view just isn't saved."""
        try:
            cam = (view or {}).get('camera') or {}
            pos, tgt = cam.get('position'), cam.get('target')
            if (not pos or not tgt or len(pos) != 3 or len(tgt) != 3):
                return
            from clash_core import models
            old = None
            try:
                old = (clash.get('viewpoints') or [None])[0]
            except Exception:
                old = None
            vp = models.make_viewpoint(
                pos, tgt, cam.get('up') or [0.0, 0.0, 1.0],
                snapshot_relpath=(old or {}).get('snapshot_relpath'),
                captured_by=user)
            vp['source'] = 'web-decide'
            # Keep a legacy viewpoint's section box: BCF export emits it as
            # ClippingPlanes, and a decide must not strip it.
            sb = (old or {}).get('section_box')
            if sb:
                vp['section_box'] = sb
            # The web-viewer restore state (mode/box/fade/ortho); ignored by BCF.
            web = {}
            for k in ('box_on', 'box_pad_ft', 'mode', 'fade', 'ortho'):
                if view.get(k) is not None:
                    web[k] = view.get(k)
            if web:
                vp['web'] = web
            clash['viewpoints'] = [vp]
        except Exception:
            _log("stamp view failed\n{0}".format(traceback.format_exc()))

    def _handle_showinrevit(self, payload):
        """Select + zoom the clash elements in Revit itself, then minimize
        this window so Revit is actually visible (the tool is modal: Revit
        can be LOOKED at but not edited until the window closes). Host
        elements select directly via UniqueId; linked elements cannot be
        selected, so the zoom falls back to a box around the clash midpoint,
        which works for both cases."""
        import json
        try:
            req = json.loads(payload)
        except Exception:
            return
        try:
            doc = revit.doc
            uidoc = revit.uidoc
            if doc is None or uidoc is None:
                return
            from Autodesk.Revit.DB import ElementId
            from System.Collections.Generic import List
            ids = []
            for key in (req.get('keys') or []):
                if not key or '|' in key:
                    continue    # linked element: lives in the link's document
                try:
                    el = doc.GetElement(key)   # UniqueId lookup
                except Exception:
                    el = None
                if el is not None:
                    ids.append(el.Id)
            shown = False
            if ids:
                sel = List[ElementId]()
                for i in ids:
                    sel.Add(i)
                try:
                    uidoc.Selection.SetElementIds(sel)
                except Exception:
                    pass
                try:
                    uidoc.ShowElements(sel)
                    shown = True
                except Exception:
                    pass
            if not shown and req.get('mid'):
                self._zoom_active_view_to(req.get('mid'))
            try:
                from System.Windows import WindowState
                self.WindowState = WindowState.Minimized
            except Exception:
                pass
            self._post("status:Showing in Revit. Bring this window back up "
                       "from the taskbar when you're done looking.")
        except Exception:
            _log("showinrevit failed\n{0}".format(traceback.format_exc()))

    def _zoom_active_view_to(self, mid, half_ft=8.0):
        """Zoom the active Revit view's UIView to a box around a point
        (host feet). The linked-element fallback for Show in Revit."""
        try:
            from Autodesk.Revit.DB import XYZ, View3D, ViewPlan, ViewSection
            uidoc = revit.uidoc
            av = uidoc.ActiveView
            # Model-space coordinates only make sense in a model view: on a
            # sheet or schedule the zoom would land in nonsense paper space.
            if av is None or not isinstance(av, (View3D, ViewPlan, ViewSection)):
                return
            for uv in uidoc.GetOpenUIViews():
                if uv.ViewId == av.Id:
                    uv.ZoomAndCenterRectangle(
                        XYZ(mid[0] - half_ft, mid[1] - half_ft, mid[2] - half_ft),
                        XYZ(mid[0] + half_ft, mid[1] + half_ft, mid[2] + half_ft))
                    break
        except Exception:
            _log("zoom failed\n{0}".format(traceback.format_exc()))

    def _view_row(self, c):
        """Flatten the clash's saved viewpoint into the page's `vp` shape:
        {camera, box_on, box_pad_ft, mode, fade, ortho} or None. camera is
        the host-feet {position,target,up}; the rest is the web restore
        state stored under the viewpoint's `web` key."""
        vp0 = (c.get("viewpoints") or [None])[0] or {}
        cam = vp0.get("camera")
        if not cam:
            return None
        out = {"camera": cam}
        web = vp0.get("web") or {}
        for k in ("box_on", "box_pad_ft", "mode", "fade", "ortho"):
            if web.get(k) is not None:
                out[k] = web.get(k)
        return out

    def _clash_row(self, ph, c, tol_by_test):
        """One clash record -> the page's row dict. Shared by the full
        clashes: push and the per-edit clashupd: delta."""
        a = c.get("ref_a") or {}
        b = c.get("ref_b") or {}
        comments = [{
            "author": cm.get("author") or "?",
            "at":     cm.get("at") or "",
            "body":   cm.get("body") or "",
        } for cm in (c.get("comments") or [])]
        # Real importance when the engine has stamped it; otherwise the
        # provisional placeholder (pre-scoring records).
        imp = c.get("importance") or {}
        return {
            "id":     c.get("id"),
            "thumb":  self._thumb_url(ph, c.get("id")),
            "mid":    c.get("midpoint"),
            "seq":    c.get("seq"),
            "score":  imp.get("score") if imp.get("score") is not None
                      else self._placeholder_score(c),
            "reason":     imp.get("reason"),
            "headline":   imp.get("headline"),
            "brk":        imp.get("brk"),
            "rule":       imp.get("rule"),
            "codeRef":    imp.get("code_ref"),
            "resolveBy":  imp.get("resolve_by"),
            "resolveByLabel": imp.get("resolve_by_label"),
            "facts":      imp.get("facts"),
            "relClass":   imp.get("relevance_class"),
            "suppressed": bool(imp.get("suppressed")),
            "supReason":  imp.get("suppress_reason"),
            "conf":       imp.get("confidence"),
            "flags":      imp.get("flags") or [],
            "status": c.get("status") or "Open",
            "kind":   c.get("kind") or "hard",
            "pair":   self._pair_label(a, b),
            "a":      a.get("name") or a.get("category") or "?",
            "b":      b.get("name") or b.get("category") or "?",
            "catA":   a.get("category") or "?",
            "catB":   b.get("category") or "?",
            "srcA":   a.get("source") or "host",
            "srcB":   b.get("source") or "host",
            "level":  a.get("level") or b.get("level") or "",
            "owner":  c.get("assignee") or "",
            "gap":    c.get("gap_inches"),
            "contact":    c.get("is_contact"),
            "gapMethod":  c.get("gap_method"),
            # Prefer the tolerance stamped on the record at run time (what
            # the score used); library lookup covers old rows.
            "tol":        c.get("tolerance_inches")
                          if c.get("tolerance_inches") is not None
                          else tol_by_test.get(c.get("test_id")),
            "first_seen": c.get("first_seen_run"),
            "deadline":   c.get("deadline"),
            # The saved 3D view from the last decide-in-3D: camera (host feet)
            # plus the restore state (mode/box/fade/ortho). The page restores
            # all of it on select.
            "vp": self._view_row(c),
            "comments":   comments,
            "fedA":   a.get("fed_key"),
            "fedB":   b.get("fed_key"),
            "gid":    c.get("group_id"),
            "scored": bool(imp),
        }

    def _group_row(self, ph, issue_manifest, g):
        """One group record -> the page's group dict. Shared by the full
        groups: push and the per-edit groupupd: delta."""
        gthumb, ghash = self._group_thumb(ph, issue_manifest, g.get("id"))
        return {
            "id":         g.get("id"),
            "seq":        g.get("seq"),
            "axis":       g.get("axis"),
            # anchor element name + representative clash id: the issue
            # inspector's "why together" sentence and its fallback photo.
            "anchor":     (g.get("anchor") or {}).get("name"),
            "rep":        g.get("rep_clash_id"),
            # Aggregate issue photo + the member-roster hash it was captured
            # against; the page recaptures on mismatch.
            "thumb":      gthumb,
            "thumbHash":  ghash,
            "title":      g.get("title") or "",
            "titleLocked": bool(g.get("title_locked")),
            "status":     g.get("status") or "Open",
            "assignee":   g.get("assignee"),
            "members":    g.get("member_ids") or [],
            "suggested":  g.get("suggested_ids") or [],
            "needsReview": bool(g.get("needs_review")),
            "created":    g.get("created_at"),
            "comments": [{
                "author": cm.get("author") or "?",
                "at":     cm.get("at") or "",
                "body":   cm.get("body") or "",
            } for cm in (g.get("comments") or [])],
            "rollup":     g.get("rollup") or {},
        }

    def _refresh_group_rollup(self, data, gid):
        """Recompute one group's rollup after a member edit, so the Home
        agenda and the queue chips don't sit stale until the next run.
        Returns the gid on success, None otherwise."""
        try:
            import clash_group
            for g in (data.get('groups') or []):
                if g.get('id') == gid:
                    by_id = dict((c.get('id'), c)
                                 for c in (data.get('clashes') or []))
                    g['rollup'] = clash_group.rollup(g, by_id)
                    return gid
        except Exception:
            _log("rollup refresh failed\n{0}".format(traceback.format_exc()))
        return None

    def _post_clash_delta(self, ph, data, clash, gid):
        """Tiny per-edit reconcile: ONE clashupd: row (server authorship and
        timestamps replacing the page's optimistic guesses) plus the touched
        group via groupupd:. The full clashes:+groups: re-push is a few MB of
        JSON per decide keystroke on big projects; this is a few KB."""
        import json
        tol_by_test = {}
        try:
            for t in self._effective_tests():
                tol_by_test[t.get('id')] = t.get('tolerance_inches')
        except Exception:
            pass
        try:
            row = self._clash_row(ph, clash, tol_by_test)
            self._post("clashupd:" + json.dumps([row], ensure_ascii=False))
        except Exception:
            _log("clash delta failed\n{0}".format(traceback.format_exc()))
            self._send_clashes()
            return
        if gid:
            try:
                manifest = self._read_issue_manifest(
                    os.path.join(ph, 'viewpoints'))
                for g in (data.get('groups') or []):
                    if g.get('id') == gid and g.get('status') != 'MergedInto':
                        grow = self._group_row(ph, manifest, g)
                        self._post("groupupd:" + json.dumps(
                            [grow], ensure_ascii=False))
                        break
            except Exception:
                _log("group delta failed\n{0}".format(traceback.format_exc()))

    def _send_clashes(self):
        """Read the active project's clash database and hand the grid its rows.
        Fully defensive: a missing / unconfigured / corrupt database just sends
        an empty list (the page shows a real empty state, never sample rows).
        Every empty path pairs clashes:[] with groups:[] so no stale group can
        linger in the Home agenda. fed_key is present on refs only for clashes
        detected by the upgraded engine; older data sends null, which leaves
        'Show in 3D' disabled for that row until a re-run."""
        import json
        try:
            doc = revit.doc
        except Exception:
            doc = None
        if doc is None:
            self._post("clashes:[]")
            self._post("groups:[]")
            return
        try:
            from clash_core import binding, persistence
            ph = binding.folder_for(doc)
            if not ph:
                # No folder set: no data, no sample fallback. Clear groups too
                # so nothing stale lingers on the page.
                self._post("clashes:[]")
                self._post("groups:[]")
                return
            data = persistence.read_clashes_at(ph)
        except Exception:
            _log("send_clashes: read failed\n{0}".format(traceback.format_exc()))
            self._post("clashes:[]")
            self._post("groups:[]")
            return
        # test_id -> tolerance, so a near-miss row can say "needs 1 in".
        tol_by_test = {}
        try:
            for t in self._effective_tests():
                tol_by_test[t.get('id')] = t.get('tolerance_inches')
        except Exception:
            pass
        rows = []
        for c in data.get("clashes", []):
            try:
                rows.append(self._clash_row(ph, c, tol_by_test))
            except Exception:
                continue
        try:
            # ensure_ascii=False: IronPython json.dumps raises on non-ASCII
            # under the default, and refs now carry typed system/level names.
            payload = json.dumps(rows, ensure_ascii=False)
        except Exception:
            _log("send_clashes: json failed\n{0}".format(traceback.format_exc()))
            payload = "[]"
        _log("send_clashes: {0} clashes".format(len(rows)))
        self._post("clashes:" + payload)
        # Layer C groups ride along AFTER the rows (the page joins members
        # by gid). Trimmed to what the grid/inspector render; comments ride
        # so the group inspector can show them.
        gout = []
        issue_manifest = self._read_issue_manifest(
            os.path.join(ph, 'viewpoints'))
        for g in (data.get("groups") or []):
            try:
                if g.get("status") == "MergedInto":
                    continue
                gout.append(self._group_row(ph, issue_manifest, g))
            except Exception:
                continue
        try:
            gpayload = json.dumps(gout, ensure_ascii=False)
        except Exception:
            _log("send_clashes: groups json failed\n{0}".format(
                traceback.format_exc()))
            gpayload = "[]"
        _log("send_clashes: {0} groups".format(len(gout)))
        self._post("groups:" + gpayload)
        # Real project name + since-last-run numbers for the Home banner
        # (replaces the page's hardcoded sample text).
        try:
            meta = persistence.read_project_meta_at(ph) or {}
            self._post("runinfo:" + json.dumps({
                "project": meta.get("display_name") or "",
                "last_run_at": data.get("last_run_at"),
                "summary": data.get("last_run_summary") or None,
            }, ensure_ascii=False))
        except Exception:
            _log("send_clashes: runinfo failed\n{0}".format(
                traceback.format_exc()))

    def _placeholder_score(self, c):
        """Provisional 1-99 importance until the detector computes a real score:
        base by clash kind, nudged up slightly for a tighter gap."""
        base = {"hard": 60, "clearance": 45, "soft": 30}.get(c.get("kind"), 40)
        try:
            gap = c.get("gap_inches")
            if gap is not None:
                base += max(0, min(15, int(round(6.0 - float(gap) * 2.0))))
        except Exception:
            pass
        return max(1, min(99, base))

    def _pair_label(self, a, b):
        """Short 'A / B' discipline label from the two element refs."""
        def side(ref):
            src = ref.get("source") or "host"
            if src.startswith("link:"):
                return src.split(":", 1)[1]
            return ref.get("category") or "Host"
        try:
            return "{0} / {1}".format(side(a), side(b))
        except Exception:
            return ""

    # --- misc ---------------------------------------------------------

    def _show_viewport_message(self, msg):
        try:
            tb = TextBlock()
            tb.Text = msg
            tb.TextWrapping = TextWrapping.Wrap
            tb.Foreground = SolidColorBrush(Colors.White)
            tb.Margin = Thickness(24)
            tb.VerticalAlignment = VerticalAlignment.Center
            self.brd_viewport.Child = tb
        except Exception:
            pass

    def _on_closed(self, sender, args):
        try:
            if self._webview is not None:
                self._webview.Dispose()
        except Exception:
            pass


with dbhms_telemetry.session(__title__, script_path=__file__):
    CoordinationForm().ShowDialog()
