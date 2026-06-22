# -*- coding: utf-8 -*-
"""3D Viewer - dbHMS's own 3D model viewer (Phase 1: foundation).

This is the first phase of a custom, web-tech 3D model viewer that lives
inside a pyRevit window. The long-term plan (see the Clash Detection panel
README and the project notes): export the Revit model to a lightweight 3D
file once, then fly through it smoothly with WASD / gamepad, toggle
categories / worksets / disciplines instantly, overlay clash markers, and
eventually open the same viewer in a plain browser so PMs and contractors
can review without Revit.

Phase 1 builds ONLY the foundation:
  * the dbHMS-styled window + side controls (mostly placeholders for now),
  * an embedded web panel powered by Revit's own bundled WebView2 engine,
  * a self-contained local page that renders a spinning cube with raw
    WebGL, proving we can host a GPU-rendered 3D view inside Revit with no
    external files and no internet.

Why WebView2: Revit 2025/2026 ship the WebView2 assemblies right next to
Revit.exe, so we can reference Revit's own copy (nothing to install, the
engine is guaranteed present whenever Revit is running). "Web" here means
local files rendered by the Edge engine that's already on the machine, not
the internet.

See dbHMS Tools.tab/Clash Detection.panel/README.md for the architecture.
"""

__title__  = '3D\nViewer'
__author__ = 'Nathaniel'
__doc__    = ('dbHMS 3D model viewer. Phase 1: proves the embedded WebGL '
              'panel renders inside Revit. Model export, navigation, '
              'category/workset toggles, and clash overlay come next.')

import base64
import os
import shutil
import time
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import System
from System.Windows import (
    TextWrapping, TextAlignment, Thickness, CornerRadius,
    HorizontalAlignment, VerticalAlignment, FontWeights, Visibility,
)
from System.Windows.Controls import (
    TextBlock, StackPanel, CheckBox, Border, Button, Grid as WpfGrid,
    ScrollViewer, ScrollBarVisibility, TextBox,
)
from System.Windows.Input import Mouse, Cursors
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_export import revit_geometry


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'ViewerForm.xaml')
MODEL_VIS_XAML = os.path.join(SCRIPT_DIR, 'ModelVisibilityForm.xaml')
WEB_DIR    = os.path.join(SCRIPT_DIR, 'web')
WEB_INDEX  = os.path.join(WEB_DIR, 'index.html')   # file:// init trigger + fallback
APP_PAGE   = 'viewer3.html'                          # the page served over the virtual host (three.js)

# Writable runtime root. We serve the viewer + exported models from here
# through a WebView2 virtual host so the panel can fetch large model files
# (the base64 message channel maxes out around 25 MB). The app assets are
# copied in from WEB_DIR on launch; models are written under models/.
_DATA_ROOT = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or SCRIPT_DIR,
    'dbHMS', '3DViewer')
APP_DIR    = os.path.join(_DATA_ROOT, 'app')
MODELS_DIR = os.path.join(_DATA_ROOT, 'models')
VHOST      = 'dbhms.viewer'   # virtual hostname mapped to _DATA_ROOT

# WebView2 assemblies we need from Revit's install directory.
_WEBVIEW2_WPF  = 'Microsoft.Web.WebView2.Wpf.dll'
_WEBVIEW2_CORE = 'Microsoft.Web.WebView2.Core.dll'

LOG_PATH = os.path.join(_DATA_ROOT, 'viewer.log')


def _log(msg):
    """Append a timestamped line to the viewer log. Best-effort; never raises."""
    try:
        if not os.path.isdir(_DATA_ROOT):
            os.makedirs(_DATA_ROOT)
        from datetime import datetime
        with open(LOG_PATH, 'a') as f:
            f.write("{0} {1}\n".format(
                datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _safe_title(doc):
    """Sanitize the document title into a filename-safe, URL-safe stem
    (spaces become underscores so it needs no URL escaping)."""
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
    """Return the directory holding Revit's bundled WebView2 assemblies, or
    None. Revit 2025/2026 ship them in the install root, next to Revit.exe;
    the running process IS Revit.exe, so its folder is where they live."""
    try:
        exe = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName
        d = os.path.dirname(exe)
        if os.path.isfile(os.path.join(d, _WEBVIEW2_WPF)):
            return d
    except Exception:
        pass
    return None


def _load_webview2_type():
    """Reference Revit's WebView2 assemblies and return the WebView2 WPF
    control type. Returns (type, None) on success or (None, reason) on
    failure so the caller can surface a clear message."""
    d = _revit_dir_with_webview2()
    if d is None:
        return None, ("WebView2 assemblies were not found next to Revit.exe. "
                      "They ship with Revit 2025/2026; this Revit may be "
                      "older, or installed somewhere unexpected.")
    try:
        clr.AddReferenceToFileAndPath(os.path.join(d, _WEBVIEW2_CORE))
        clr.AddReferenceToFileAndPath(os.path.join(d, _WEBVIEW2_WPF))
        from Microsoft.Web.WebView2.Wpf import WebView2
        return WebView2, None
    except Exception:
        return None, "Failed to load WebView2:\n\n{}".format(traceback.format_exc())


class ViewerForm(forms.WPFWindow):
    def __init__(self):
        # handle_esc=False: pyRevit's WPFWindow otherwise wires PreviewKeyDown ->
        # self.Close() on Escape, which was closing the whole tool. We want Escape
        # to only deselect (handled inside the viewer page), never close.
        forms.WPFWindow.__init__(self, FORM_XAML, handle_esc=False)
        self._webview = None
        self._vhost_ok = False     # True once the virtual host is mapped
        self._model_version = 0     # cache-buster for re-exports
        self._popout = None         # the pop-out render window, when detached
        self._fs = False            # pop-out fullscreen state

        self.btn_close.Click      += self._on_close
        self.btn_export.Click     += self._on_export
        self.btn_load_last.Click  += self._on_load_last
        self.btn_popout.Click     += self._on_popout
        self.btn_fullscreen.Click += self._on_fullscreen
        self.sl_speed.ValueChanged += self._on_speed_changed
        self.sl_look.ValueChanged  += self._on_look_changed
        self.sl_time_of_day.ValueChanged  += self._on_sun_changed
        self.sl_sun_strength.ValueChanged += self._on_sun_changed
        self.sl_sun_direction.ValueChanged += self._on_sun_changed
        self.chk_edges.Checked   += self._on_edges_changed
        self.chk_edges.Unchecked += self._on_edges_changed
        self.chk_clash_markers.Checked   += self._on_clash_filter_changed
        self.chk_clash_markers.Unchecked += self._on_clash_filter_changed
        self.txt_clash_search.TextChanged += self._on_clash_filter_changed
        self.btn_saved_views.Click += self._on_save_viewpoint
        self.cmb_quality.SelectionChanged += self._on_quality_changed
        self._last_cam = None    # latest camera reported by the viewer
        self._clash_rows = []    # every clash for this project (row dicts)
        self._trade_chks = []    # dynamic filter checkboxes, by dimension
        self._status_chks = []
        self._kind_chks = []
        self._refresh_tuning_labels()

        # Attach the web panel after the window is laid out, so the host
        # Border has a real size for WebView2 to initialize into.
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
            self._show_viewport_message("Viewer page is missing:\n\n{}".format(WEB_INDEX))
            return
        try:
            from Microsoft.Web.WebView2.Wpf import CoreWebView2CreationProperties
            self._sync_app_assets()
            wv = WebView2()
            # WebView2 writes a cache/user-data folder; default sits next to
            # the host exe (Program Files, read-only). Point it somewhere
            # writable so initialization can't fail on permissions.
            props = CoreWebView2CreationProperties()
            props.UserDataFolder = os.path.join(_DATA_ROOT, 'WebView2')
            wv.CreationProperties = props
            wv.CoreWebView2InitializationCompleted += self._on_webview_init
            self.brd_viewport.Child = wv
            self._webview = wv
            # Init the core WITHOUT navigating anywhere. Previously we set Source
            # to a file:// page to trigger init, but that file:// navigation fired
            # late and ABORTED our virtual-host navigation to viewer3
            # (ConnectionAborted), leaving the old page on screen. EnsureCoreWebView2Async
            # starts the core with no competing navigation; _on_webview_init then
            # maps the vhost and navigates to viewer3 exactly once.
            try:
                wv.EnsureCoreWebView2Async(None)
                _log("attach_viewer: webview created, EnsureCoreWebView2Async called")
            except Exception:
                _log("attach_viewer: EnsureCoreWebView2Async failed; falling back to Source\n{0}"
                     .format(traceback.format_exc()))
                wv.Source = System.Uri(WEB_INDEX)
        except Exception:
            _log("attach_viewer: EXCEPTION\n{}".format(traceback.format_exc()))
            self._show_viewport_message(
                "The 3D panel failed to start:\n\n{}".format(traceback.format_exc()))

    def _sync_app_assets(self):
        """Copy the viewer's web assets into the served app folder so the
        virtual host can serve them (and models) from one origin."""
        try:
            if not os.path.isdir(APP_DIR):
                os.makedirs(APP_DIR)
            # Recursive copy so subfolders (e.g. the vendored three.js under
            # lib/three/) reach the served app folder, not just top-level files.
            n = 0
            for dirpath, _dirs, files in os.walk(WEB_DIR):
                rel = os.path.relpath(dirpath, WEB_DIR)
                dst_dir = APP_DIR if rel == "." else os.path.join(APP_DIR, rel)
                if not os.path.isdir(dst_dir):
                    os.makedirs(dst_dir)
                for name in files:
                    # Per-file guard: a file locked by a still-alive WebView2
                    # (e.g. a prior session serving index.html) must not abort the
                    # whole sync and leave viewer3.html / lib uncopied.
                    try:
                        shutil.copy2(os.path.join(dirpath, name), os.path.join(dst_dir, name))
                        n += 1
                    except Exception:
                        _log("sync_app_assets: skip {0} ({1})".format(
                            name, traceback.format_exc().splitlines()[-1]))
            _log("sync_app_assets: copied {0} file(s); {1} present={2}".format(
                n, APP_PAGE, os.path.isfile(os.path.join(APP_DIR, APP_PAGE))))
        except Exception:
            _log("sync_app_assets: EXCEPTION\n{}".format(traceback.format_exc()))

    def _on_webview_init(self, sender, args):
        try:
            ok = args.IsSuccess
            _log("init: IsSuccess={0}".format(ok))
            if not ok:
                self._show_viewport_message(
                    "WebView2 failed to initialize:\n\n{}".format(
                        args.InitializationException))
                return
            core = self._webview.CoreWebView2
            # Reverse channel: the page posts messages back (e.g. "escape" to
            # drop full screen, element picks later).
            try:
                core.WebMessageReceived += self._on_web_message
            except Exception:
                _log("init: WebMessageReceived wire failed\n{0}".format(
                    traceback.format_exc()))
            # Navigation tracing: log every page load + its outcome so we can see
            # exactly which page ends up displayed and why a navigation failed.
            try:
                core.NavigationStarting += self._on_nav_starting
                core.NavigationCompleted += self._on_nav_completed
            except Exception:
                _log("init: nav-trace wire failed\n{0}".format(traceback.format_exc()))
            # Resolve the access-kind enum from the SAME assembly instance the
            # core object came from. Importing the type directly can bind to a
            # different loaded copy of WebView2.Core (Revit/Dynamo also load
            # it), producing the IronPython "expected X, got X" identity error.
            ak_type = core.GetType().Assembly.GetType(
                "Microsoft.Web.WebView2.Core.CoreWebView2HostResourceAccessKind")
            allow = System.Enum.Parse(ak_type, "Allow")
            app_index = os.path.join(APP_DIR, APP_PAGE)
            if os.path.isfile(app_index):
                # Serve _DATA_ROOT (app/ + models/) under one https origin so
                # the page can fetch large model files past the message limit.
                core.SetVirtualHostNameToFolderMapping(VHOST, _DATA_ROOT, allow)
                # Defeat WebView2's aggressive vhost cache with a versioned
                # FILENAME (a real file -> no query string for the host to choke
                # on, and a fresh name each update so a stale copy can't be served).
                page = self._versioned_page(app_index)
                core.Navigate("https://{0}/app/{1}".format(VHOST, page))
                self._vhost_ok = True
                _log("init: vhost mapped, navigated to app/{0}".format(page))
            else:
                _log("init: app index missing at {0}, staying on file://"
                     .format(app_index))
        except Exception:
            # Virtual host unavailable: stay on the local file. Small models
            # still load via the base64 path.
            _log("init: vhost EXCEPTION\n{}".format(traceback.format_exc()))

    def _versioned_page(self, app_index):
        """Copy the served page to a per-version filename (viewer3.<mtime>.html)
        and return that name, so each update navigates to a brand-new URL that no
        WebView2 cache can satisfy with a stale copy. Relative imports in the page
        resolve against /app/ regardless of the filename. Falls back to the plain
        page name if the copy fails."""
        try:
            stem = APP_PAGE.rsplit('.', 1)[0]
            ver = int(os.path.getmtime(app_index))
            for f in os.listdir(APP_DIR):   # drop older versioned copies
                if f.startswith(stem + '.') and f.endswith('.html') and f != APP_PAGE:
                    try:
                        os.remove(os.path.join(APP_DIR, f))
                    except Exception:
                        pass
            versioned = "{0}.{1}.html".format(stem, ver)
            shutil.copy2(app_index, os.path.join(APP_DIR, versioned))
            return versioned
        except Exception:
            _log("versioned_page: {0}".format(traceback.format_exc().splitlines()[-1]))
            return APP_PAGE

    def _on_nav_starting(self, sender, args):
        try:
            _log("nav start: {0}".format(args.Uri))
        except Exception:
            pass

    def _on_nav_completed(self, sender, args):
        try:
            _log("nav done: success={0} err={1}".format(
                args.IsSuccess, args.WebErrorStatus))
        except Exception:
            pass

    def _show_viewport_message(self, msg):
        """Replace the viewport content with a readable message (used when
        WebView2 can't load, so the window still opens and we get a clear
        diagnostic instead of a blank panel)."""
        tb = TextBlock()
        tb.Text = msg
        tb.TextWrapping = TextWrapping.Wrap
        tb.TextAlignment = TextAlignment.Center
        tb.Foreground = SolidColorBrush(Color.FromRgb(0xA0, 0xAE, 0xC0))
        tb.HorizontalAlignment = HorizontalAlignment.Center
        tb.VerticalAlignment = VerticalAlignment.Center
        tb.Margin = Thickness(24)
        tb.MaxWidth = 520
        try:
            self.brd_viewport.Child = tb
        except Exception:
            pass

    # --- Actions ------------------------------------------------------

    def _active_3d_view(self, doc, view):
        """A non-template 3D view for CustomExporter: the active view if it's 3D,
        else the first usable 3D view in the document, else None (-> caller falls
        back to the geometry-API exporter)."""
        from Autodesk.Revit.DB import View3D, FilteredElementCollector
        try:
            if isinstance(view, View3D) and not view.IsTemplate:
                return view
        except Exception:
            pass
        try:
            for v in FilteredElementCollector(doc).OfClass(View3D):
                if v is not None and not v.IsTemplate:
                    return v
        except Exception:
            pass
        return None

    def _on_export(self, sender, args):
        """Export the whole model (host + linked) to a .glb and load it into
        the panel. Streams to disk so large models stay memory-safe; models
        too big for the in-panel loader are saved and their size measured."""
        from pyrevit import revit
        doc = revit.doc
        if doc is None:
            dbhms_ui.info("No active Revit document to export.", title='3D Viewer')
            return
        if not forms.alert(
                "Export the full model (your model plus loaded links) to the "
                "3D viewer?\n\nOn very large models this can take a while, and "
                "Revit will be busy until it finishes.",
                title='3D Viewer', yes=True, no=True):
            return
        try:
            view = doc.ActiveView
        except Exception:
            view = None

        Mouse.OverrideCursor = Cursors.Wait
        result = None
        error = None
        t0 = time.time()
        try:
            path = self._export_path(doc)
            # Prefer the high-fidelity CustomExporter (smooth curves, full model
            # via its own all-on view, per-vertex normals); fall back to the
            # geometry-API exporter on ANY failure so an export always succeeds.
            try:
                from clash_export import custom_export
                ce = custom_export.export_view(doc, path)
                if ce.get("elements", 0) > 0 and ce.get("bytes", 0) > 0:
                    result = {"path": path, "asset_extras": None, "stats": {
                        "elements": ce["elements"], "host_elements": ce["elements"],
                        "link_elements": 0, "triangles": ce["triangles"],
                        "models": 1, "bytes": ce["bytes"], "capped": False}}
                    _log("export: used CustomExporter ({0} elements)".format(ce["elements"]))
                else:
                    _log("export: CustomExporter produced no geometry, falling back")
                    result = None
            except Exception:
                _log("export: CustomExporter failed, falling back to geometry API\n{0}"
                     .format(traceback.format_exc()))
                result = None
            if result is None:
                result = revit_geometry.export_model(doc, path, view=view)
                _log("export: used geometry-API exporter")
        except Exception:
            error = traceback.format_exc()
        finally:
            Mouse.OverrideCursor = None
        seconds = time.time() - t0

        if error is not None:
            dbhms_ui.info("Export failed:\n\n{}".format(error),
                          title='3D Viewer export failed')
            return

        stats = result["stats"]
        if stats["elements"] == 0 or stats["bytes"] == 0:
            dbhms_ui.info(
                "No 3D geometry found to export.\n\n"
                "Open a 3D view with model elements and try again.",
                title='3D Viewer')
            return

        size_bytes = stats["bytes"]
        size_mb = size_bytes / (1024.0 * 1024.0)
        host_n = stats.get("host_elements", 0)
        link_n = stats.get("link_elements", 0)
        cap = (" Hit the safety ceiling, so this is a partial export."
               if stats.get("capped") else "")

        # With the virtual host active the panel fetches the file directly, so
        # any size can load. Without it (fallback) we're on the base64 message
        # channel, which maxes out around 25 MB.
        _log("export done: bytes={0}, vhost_ok={1}".format(
            size_bytes, self._vhost_ok))
        self.txt_stats.Text = (
            "{0} host + {1} linked elements\n{2:,} triangles  -  {3:.1f} MB  -  "
            "{4:.0f}s{5}".format(host_n, link_n, stats["triangles"], size_mb,
                                 seconds, cap))
        can_load = self._vhost_ok or size_bytes <= 25 * 1024 * 1024
        if can_load and self._load_into_panel(path):
            self.txt_status.Text = "Model loaded. Press F to fly, or drag to look."
            return

        self.txt_status.Text = (
            "Exported {0} host + {1} linked elements, {2:,} triangles, "
            "{3:.1f} MB in {4:.1f}s.".format(
                host_n, link_n, stats["triangles"], size_mb, seconds))
        dbhms_ui.info(
            "Exported the full model to glTF (.glb), but the 3D panel "
            "wasn't available to display it (the viewer engine may not have "
            "started). It's saved to disk.\n\n"
            "Host elements: {0}\nLinked elements: {1}\nTriangles: {2:,}\n"
            "File size: {3:.1f} MB\nTime: {4:.1f}s{5}\n\nFile:\n{6}".format(
                host_n, link_n, stats["triangles"], size_mb, seconds, cap, path),
            title='3D Viewer - exported')
        try:
            os.startfile(os.path.dirname(path))
        except Exception:
            pass

    def _on_load_last(self, sender, args):
        """Load the most recent export for this document without re-exporting."""
        from pyrevit import revit
        doc = revit.doc
        if doc is None:
            dbhms_ui.info("No active Revit document.", title='3D Viewer')
            return
        path = self._export_path(doc)
        if not os.path.isfile(path):
            dbhms_ui.info(
                "No previous export found for this model.\n\nRun "
                "Export & Load Model first.", title='3D Viewer')
            return
        if self._load_into_panel(path):
            try:
                mb = os.path.getsize(path) / (1024.0 * 1024.0)
                self.txt_stats.Text = "Loaded last export ({0:.1f} MB).".format(mb)
            except Exception:
                pass
            self.txt_status.Text = "Model loaded. Press F to fly, or drag to look."
        else:
            dbhms_ui.info(
                "The 3D panel isn't ready yet. Give it a moment after opening "
                "the tool, then try again.", title='3D Viewer')

    def _load_into_panel(self, path):
        """Tell the panel to load the exported .glb. With the virtual host
        active, post a model URL the page fetches (handles any size). Without
        it, fall back to base64 over the message channel (small models only).
        Returns False if the panel isn't ready."""
        try:
            wv = self._webview
            if wv is None or wv.CoreWebView2 is None:
                _log("load_into_panel: core not ready (vhost_ok={0})".format(
                    self._vhost_ok))
                return False
            self._model_version += 1
            if self._vhost_ok:
                url = "https://{0}/models/{1}?v={2}".format(
                    VHOST, os.path.basename(path), self._model_version)
                wv.CoreWebView2.PostWebMessageAsString("url:" + url)
                _log("load_into_panel: posted URL {0}".format(url))
            else:
                with open(path, 'rb') as f:
                    data = f.read()
                b64 = base64.b64encode(data)
                if isinstance(b64, bytes):
                    b64 = b64.decode('ascii')
                wv.CoreWebView2.PostWebMessageAsString("b64:" + b64)
                _log("load_into_panel: posted base64 ({0} bytes)".format(len(data)))
            self._push_tuning()
            return True
        except Exception:
            _log("load_into_panel: EXCEPTION\n{0}".format(traceback.format_exc()))
            return False

    # --- Navigation tuning (panel -> render) --------------------------

    def _post(self, msg):
        """Send a control message to the render page. Best-effort."""
        try:
            wv = self._webview
            if wv is not None and wv.CoreWebView2 is not None:
                wv.CoreWebView2.PostWebMessageAsString(msg)
        except Exception:
            pass

    def _push_tuning(self):
        """Push the current speed/look slider values to the render."""
        try:
            self._post("speed:{0:.2f}".format(self.sl_speed.Value))
            self._post("look:{0:.3f}".format(self.sl_look.Value))
        except Exception:
            pass

    def _refresh_tuning_labels(self):
        try:
            self.txt_speed_val.Text = "{0:.1f} m/s".format(self.sl_speed.Value)
            self.txt_look_val.Text = "{0:.2f}".format(self.sl_look.Value)
        except Exception:
            pass

    def _on_speed_changed(self, sender, args):
        self.txt_speed_val.Text = "{0:.1f} m/s".format(self.sl_speed.Value)
        self._post("speed:{0:.2f}".format(self.sl_speed.Value))

    def _on_look_changed(self, sender, args):
        self.txt_look_val.Text = "{0:.2f}".format(self.sl_look.Value)
        self._post("look:{0:.3f}".format(self.sl_look.Value))

    def _push_sun(self):
        """Push the environment sun (from Time of day / direction / strength sliders)
        to the render as 'sun:<elevation>,<azimuth>,<strength0..1>'. Time of day drives
        a real arc: the sun rises on one side (~6am), peaks high at noon, and sets on
        the OTHER side (~6pm), going below the horizon at night. 'Sun direction' sets the
        noon bearing (which way the building faces), and time sweeps +/-90 deg around it."""
        try:
            import math
            t = self.sl_time_of_day.Value
            frac = (t - 6.0) / 12.0                      # 0 at 6am, 1 at 6pm; <0 / >1 = night
            el = math.sin(frac * math.pi) * 78.0         # negative at night (sun below horizon)
            az = self.sl_sun_direction.Value + (frac - 0.5) * 180.0   # sweep E -> S -> W
            strength = self.sl_sun_strength.Value / 100.0
            self._post("sun:{0:.1f},{1:.1f},{2:.3f}".format(el, az, strength))
        except Exception:
            pass

    def _on_sun_changed(self, sender, args):
        self._push_sun()

    def _push_edges(self):
        try:
            self._post("edges:" + ("1" if self.chk_edges.IsChecked else "0"))
        except Exception:
            pass

    def _on_edges_changed(self, sender, args):
        self._push_edges()

    # --- Pop-out / full screen ----------------------------------------

    def _on_popout(self, sender, args):
        if self._popout is not None:
            self._dock_render()
        else:
            self._pop_out_render()

    def _pop_out_render(self):
        """Move the render into its own window for a second screen."""
        if self._webview is None:
            return
        from System.Windows import Window, WindowStartupLocation
        try:
            self.brd_viewport.Child = None
            win = Window()
            win.Title = "dbHMS 3D Viewer"
            win.Width = 1280
            win.Height = 800
            win.WindowStartupLocation = WindowStartupLocation.CenterScreen
            win.Background = SolidColorBrush(Color.FromRgb(0x1A, 0x20, 0x2C))
            win.Content = self._webview
            win.Closed += self._on_popout_closed
            self._popout = win
            win.Show()
            win.Activate()
            self._show_viewport_message(
                "Render is in its own window.\n\nDrag it to your share screen, "
                "then click Full Screen. Click \"Dock Render\" to bring it back.")
            self.btn_popout.Content = "Dock Render"
            self.btn_fullscreen.IsEnabled = True
        except Exception:
            _log("pop_out: EXCEPTION\n{0}".format(traceback.format_exc()))
            self._reclaim_webview()

    def _dock_render(self):
        win = self._popout
        if win is None:
            return
        self._popout = None
        try:
            win.Closed -= self._on_popout_closed
        except Exception:
            pass
        try:
            win.Content = None
        except Exception:
            pass
        self._reclaim_webview()
        try:
            win.Close()
        except Exception:
            pass
        self._fs = False
        self.btn_popout.Content = "Pop Out Render"
        self.btn_fullscreen.Content = "Full Screen"
        self.btn_fullscreen.IsEnabled = False

    def _on_popout_closed(self, sender, args):
        # User closed the pop-out window directly: bring the render home.
        if self._popout is None:
            return
        try:
            self._popout.Content = None
        except Exception:
            pass
        self._popout = None
        self._reclaim_webview()
        self._fs = False
        self.btn_popout.Content = "Pop Out Render"
        self.btn_fullscreen.Content = "Full Screen"
        self.btn_fullscreen.IsEnabled = False

    def _reclaim_webview(self):
        try:
            if self._webview is not None:
                self.brd_viewport.Child = self._webview
        except Exception:
            _log("reclaim_webview: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _on_fullscreen(self, sender, args):
        if self._fs:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        win = self._popout
        if win is None or self._fs:
            return
        from System.Windows import WindowState, WindowStyle, ResizeMode
        try:
            win.WindowStyle = getattr(WindowStyle, 'None')   # borderless
            win.ResizeMode = ResizeMode.NoResize
            win.WindowState = WindowState.Maximized
            self._fs = True
            self.btn_fullscreen.Content = "Exit Full Screen"
        except Exception:
            _log("enter_fullscreen: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _exit_fullscreen(self):
        """Drop full screen back to the windowed pop-out. Reachable by the
        button, by Esc inside the render, and by Esc on the window, so a
        single-screen user is never trapped in a borderless window."""
        win = self._popout
        if win is None or not self._fs:
            return
        from System.Windows import WindowState, WindowStyle, ResizeMode
        try:
            win.WindowState = WindowState.Normal
            win.WindowStyle = WindowStyle.SingleBorderWindow
            win.ResizeMode = ResizeMode.CanResize
            self._fs = False
            self.btn_fullscreen.Content = "Full Screen"
            try:
                win.Activate()
            except Exception:
                pass
        except Exception:
            _log("exit_fullscreen: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _on_web_message(self, sender, args):
        try:
            msg = args.TryGetWebMessageAsString()
        except Exception:
            msg = None
        if not msg:
            return
        if msg.startswith("diag:"):
            _log("viewer {0}".format(msg))
            return
        if msg == "minimize":
            # The viewer's bottom-left "minimize" button: step out of the enlarged
            # view. Full screen -> windowed; popped out -> docked; embedded ->
            # minimize the tool window. (Escape no longer does this; it unselects.)
            try:
                if self._fs:
                    self._exit_fullscreen()
                elif self._popout is not None:
                    self._dock_render()
                else:
                    from System.Windows import WindowState
                    self.WindowState = WindowState.Minimized
            except Exception:
                _log("minimize: EXCEPTION\n{0}".format(traceback.format_exc()))
            return
        if msg.startswith("cam:"):
            try:
                import json
                self._last_cam = json.loads(msg[len("cam:"):])
            except Exception:
                pass
            return
        if msg.startswith("filters:"):
            # Model finished loading (offset is known); build the tree and load
            # the project's clashes + saved viewpoints now that transforms work.
            self._build_filter_ui(msg[len("filters:"):])
            self._load_clashes()
            self._build_viewpoints_list()
            self._push_quality()   # re-sync the tier in case it changed pre-load
            self._push_sun()       # apply the current time-of-day / sun settings
            self._push_edges()     # apply the current edges toggle
            return

    # --- render quality -----------------------------------------------

    def _quality_name(self):
        try:
            item = self.cmb_quality.SelectedItem
            return str(item.Content).strip().lower() if item is not None else "shaded"
        except Exception:
            return "shaded"

    def _push_quality(self):
        self._post("quality:" + self._quality_name())

    def _on_quality_changed(self, sender, args):
        self._push_quality()

    # --- clashes ------------------------------------------------------

    # Clash kinds in display order (stored lowercase in the data).
    _KIND_LABELS = [("hard", "Hard"), ("soft", "Soft"), ("clearance", "Clearance")]
    # Cap on how many list rows we render at once (markers still show all).
    _CLASH_LIST_CAP = 500

    def _load_clashes(self):
        """Read every clash for this project into memory, build the
        trade/status/type filter checkboxes, and render the filtered list.
        Markers are pushed to the viewer only when the user turns them on."""
        self._clash_rows = []
        folder_ok = True
        try:
            from pyrevit import revit
            from clash_core import persistence, project, browser_filters
            doc = revit.doc
            ph = project.project_hash_for(doc) if doc is not None else None
            data = persistence.read_clashes(ph) if ph else {"clashes": []}
            # Test-name lookup so the search box can also match by test name.
            names = {}
            try:
                lib = persistence.read_global_test_library()
                for t in (lib.get("tests") or []):
                    if t.get("id"):
                        names[t["id"]] = t.get("name", "")
            except Exception:
                pass
            for i, c in enumerate(data.get("clashes") or []):
                mp = c.get("midpoint")
                if not mp or len(mp) < 3:
                    continue
                a = (c.get("ref_a") or {}).get("category") or "?"
                b = (c.get("ref_b") or {}).get("category") or "?"
                seq = c.get("seq") or (i + 1)
                status = c.get("status") or "Open"
                label = "#{0}  {1} x {2}  -  {3}".format(seq, a, b, status)
                self._clash_rows.append({
                    "label":   label,
                    "point":   [float(mp[0]), float(mp[1]), float(mp[2])],
                    "trade":   c.get("assignee") or "-",
                    "status":  status,
                    "kind":    (c.get("kind") or "hard").lower(),
                    "haystack": browser_filters.build_search_haystack(
                        c, names.get(c.get("test_id"), "")),
                })
        except persistence.SharedFolderNotConfigured:
            folder_ok = False
        except Exception:
            _log("load_clashes: {0}".format(traceback.format_exc()))

        if not folder_ok:
            self._show_clash_message(
                "Set the shared clash folder in Settings to see this "
                "project's clashes here.")
            return
        if not self._clash_rows:
            self._show_clash_message(
                "No clashes for this project yet (run a clash test).")
            return

        self._build_clash_filter_checkboxes()
        self.sp_clash_filters.Visibility = Visibility.Visible
        self._apply_clash_filters()

    def _show_clash_message(self, text):
        """Collapse the filters and show one explanatory line in the list box."""
        try:
            self.sp_clash_filters.Visibility = Visibility.Collapsed
        except Exception:
            pass
        tb = TextBlock()
        tb.Text = text
        tb.Foreground = SolidColorBrush(Color.FromRgb(0x71, 0x80, 0x96))
        tb.FontSize = 11
        tb.TextWrapping = TextWrapping.Wrap
        try:
            self.brd_clashes.Child = tb
        except Exception:
            pass

    def _build_clash_filter_checkboxes(self):
        """Fill the Trade / Status / Type groups with one (checked) checkbox per
        value that actually occurs in this project's clashes."""
        trade_order = ["Mechanical", "Electrical", "Plumbing", "Fire Protection",
                       "Technology", "Architectural", "Structural", "-"]
        status_order = ["Open", "Reviewed", "Approved", "Resolved"]
        present_trades = set(r["trade"] for r in self._clash_rows)
        present_status = set(r["status"] for r in self._clash_rows)
        present_kinds  = set(r["kind"] for r in self._clash_rows)

        def ordered(order, present):
            return ([v for v in order if v in present]
                    + sorted(v for v in present if v not in order))

        self._trade_chks = self._fill_filter_group(
            self.sp_clash_trades,
            [(v, ("Unassigned" if v == "-" else v))
             for v in ordered(trade_order, present_trades)])
        self._status_chks = self._fill_filter_group(
            self.sp_clash_status,
            [(v, v) for v in ordered(status_order, present_status)])
        self._kind_chks = self._fill_filter_group(
            self.sp_clash_kind,
            [(k, lbl) for k, lbl in self._KIND_LABELS if k in present_kinds])

    def _fill_filter_group(self, container, value_label_pairs):
        """Reset `container`, add a checked CheckBox per (value, label), wire the
        re-filter handler, and return the list of checkboxes."""
        container.Children.Clear()
        chks = []
        for value, label in value_label_pairs:
            chk = CheckBox()
            chk.Content = label
            chk.Tag = value
            chk.IsChecked = True
            chk.FontSize = 11
            chk.Margin = Thickness(0, 1, 0, 1)
            chk.Checked += self._on_clash_filter_changed
            chk.Unchecked += self._on_clash_filter_changed
            container.Children.Add(chk)
            chks.append(chk)
        return chks

    def _checked_values(self, chks):
        return set(c.Tag for c in chks if c.IsChecked)

    def _filtered_clashes(self):
        """Apply the current trade/status/type/search filters (AND across
        dimensions) to the in-memory clash rows."""
        trades   = self._checked_values(self._trade_chks)
        statuses = self._checked_values(self._status_chks)
        kinds    = self._checked_values(self._kind_chks)
        try:
            needle = (self.txt_clash_search.Text or "").lower().strip()
        except Exception:
            needle = ""
        out = []
        for r in self._clash_rows:
            if r["trade"] not in trades:
                continue
            if r["status"] not in statuses:
                continue
            if r["kind"] not in kinds:
                continue
            if needle and needle not in r["haystack"]:
                continue
            out.append(r)
        return out

    def _on_clash_filter_changed(self, sender, args):
        self._apply_clash_filters()

    def _apply_clash_filters(self):
        """Re-render the list from the current filter state, and (only when the
        markers checkbox is on) push the filtered midpoints to the viewer."""
        filtered = self._filtered_clashes()
        self._build_clash_list(filtered)
        markers_on = False
        try:
            markers_on = bool(self.chk_clash_markers.IsChecked)
        except Exception:
            pass
        if markers_on:
            try:
                import json as _json
                self._post("clashes:" + _json.dumps([r["point"] for r in filtered]))
                self._post("showmarkers:1")
            except Exception:
                _log("apply_clash_filters: {0}".format(traceback.format_exc()))
        else:
            self._post("showmarkers:0")

    def _build_clash_list(self, rows):
        panel = StackPanel()
        shown = rows[:self._CLASH_LIST_CAP]
        for r in shown:
            btn = Button()
            btn.Content = r["label"]
            btn.HorizontalAlignment = HorizontalAlignment.Stretch
            btn.HorizontalContentAlignment = HorizontalAlignment.Left
            btn.Background = SolidColorBrush(Color.FromRgb(0xED, 0xF2, 0xF7))
            btn.Foreground = SolidColorBrush(Color.FromRgb(0x2D, 0x37, 0x48))
            btn.BorderBrush = SolidColorBrush(Color.FromRgb(0xCB, 0xD5, 0xE0))
            btn.BorderThickness = Thickness(1)
            btn.Padding = Thickness(8, 4, 8, 4)
            btn.Margin = Thickness(0, 0, 0, 3)
            btn.FontSize = 11
            btn.Cursor = Cursors.Hand
            p = r["point"]
            btn.Click += (lambda s, a, pt=p:
                          self._post("flytopoint:{0},{1},{2}".format(pt[0], pt[1], pt[2])))
            panel.Children.Add(btn)
        if not shown:
            tb = TextBlock()
            tb.Text = "No clashes match the current filters."
            tb.Foreground = SolidColorBrush(Color.FromRgb(0x71, 0x80, 0x96))
            tb.FontSize = 11
            tb.TextWrapping = TextWrapping.Wrap
            panel.Children.Add(tb)
        sv = ScrollViewer()
        sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        sv.MaxHeight = 240
        sv.Content = panel
        try:
            self.brd_clashes.Child = sv
        except Exception:
            _log("build_clash_list: {0}".format(traceback.format_exc()))
        try:
            total = len(self._clash_rows)
            n = len(rows)
            if n > self._CLASH_LIST_CAP:
                self.lbl_clash_count.Text = (
                    "Showing first {0} of {1} matching ({2} total) - "
                    "refine filters to narrow.".format(self._CLASH_LIST_CAP, n, total))
            else:
                self.lbl_clash_count.Text = "Showing {0} of {1} clashes.".format(n, total)
        except Exception:
            pass

    # --- saved viewpoints ---------------------------------------------

    def _viewpoints_path(self):
        try:
            from pyrevit import revit
            title = _safe_title(revit.doc) if revit.doc is not None else "model"
        except Exception:
            title = "model"
        d = os.path.join(_DATA_ROOT, "viewpoints")
        try:
            if not os.path.isdir(d):
                os.makedirs(d)
        except Exception:
            pass
        return os.path.join(d, title + ".json")

    def _read_viewpoints(self):
        import json
        p = self._viewpoints_path()
        if not os.path.isfile(p):
            return []
        try:
            with open(p, 'r') as f:
                return (json.load(f) or {}).get("viewpoints") or []
        except Exception:
            return []

    def _write_viewpoints(self, vps):
        import json
        try:
            with open(self._viewpoints_path(), 'w') as f:
                json.dump({"viewpoints": vps}, f, indent=2)
        except Exception:
            _log("write_viewpoints: {0}".format(traceback.format_exc()))

    def _on_save_viewpoint(self, sender, args):
        if self._last_cam is None:
            dbhms_ui.info("Load a model and move the view a moment first, "
                          "then save.", title='Viewpoints')
            return
        vps = self._read_viewpoints()
        name = forms.ask_for_string(
            default='View {0}'.format(len(vps) + 1),
            prompt='Name this viewpoint:', title='Save viewpoint')
        if not name:
            return
        vps.append({"name": name, "pos": self._last_cam.get("pos"),
                    "yaw": self._last_cam.get("yaw"),
                    "pitch": self._last_cam.get("pitch")})
        self._write_viewpoints(vps)
        self._build_viewpoints_list()

    def _build_viewpoints_list(self):
        import json
        vps = self._read_viewpoints()
        panel = StackPanel()
        if not vps:
            tb = TextBlock()
            tb.Text = "No saved viewpoints yet."
            tb.Foreground = SolidColorBrush(Color.FromRgb(0x71, 0x80, 0x96))
            tb.FontSize = 11
            panel.Children.Add(tb)
        else:
            for vp in vps:
                btn = Button()
                btn.Content = vp.get("name") or "View"
                btn.HorizontalAlignment = HorizontalAlignment.Stretch
                btn.HorizontalContentAlignment = HorizontalAlignment.Left
                btn.Background = SolidColorBrush(Color.FromRgb(0xED, 0xF2, 0xF7))
                btn.Foreground = SolidColorBrush(Color.FromRgb(0x2D, 0x37, 0x48))
                btn.BorderBrush = SolidColorBrush(Color.FromRgb(0xCB, 0xD5, 0xE0))
                btn.BorderThickness = Thickness(1)
                btn.Padding = Thickness(8, 4, 8, 4)
                btn.Margin = Thickness(0, 0, 0, 3)
                btn.FontSize = 11
                btn.Cursor = Cursors.Hand
                payload = json.dumps({"pos": vp.get("pos"), "yaw": vp.get("yaw"),
                                      "pitch": vp.get("pitch")})
                btn.Click += (lambda s, a, pl=payload: self._post("viewpose:" + pl))
                panel.Children.Add(btn)
        try:
            self.brd_viewpoints.Child = panel
        except Exception:
            _log("build_viewpoints_list: {0}".format(traceback.format_exc()))

    def _build_filter_ui(self, json_text):
        """Build the model -> categories visibility tree from what the render
        reports after a model loads. Runs on the UI thread."""
        import json
        try:
            data = json.loads(json_text)
        except Exception:
            _log("build_filter_ui: bad json\n{0}".format(traceback.format_exc()))
            return
        self._build_models_tree(data.get("models") or [])

    def _build_models_tree(self, models):
        """models = [{"name", "categories":[...], "worksets":[...]}, ...].
        Each model gets its own blue box: a whole-model on/off checkbox plus
        an Edit button that opens a popup to toggle that model's categories
        and worksets. The host model is marked '(this model)'."""
        self._models = {}
        self._hidden_models = set()
        self._hidden_cats = set()
        self._hidden_ws = set()

        host_title = None
        try:
            from pyrevit import revit
            host_title = revit.doc.Title if revit.doc is not None else None
        except Exception:
            host_title = None

        panel = StackPanel()
        if not models:
            tb = TextBlock()
            tb.Text = "No models reported."
            tb.Foreground = SolidColorBrush(Color.FromRgb(0x71, 0x80, 0x96))
            tb.FontSize = 11
            panel.Children.Add(tb)
        for entry in models:
            name = entry.get("name") or "Model"
            self._models[name] = {
                "categories": entry.get("categories") or [],
                "worksets": entry.get("worksets") or [],
            }
            panel.Children.Add(self._model_box(name, name == host_title))
        try:
            self.brd_models.Child = panel
        except Exception:
            _log("build_models_tree: failed\n{0}".format(traceback.format_exc()))

    def _model_box(self, name, is_host):
        """One per-model blue box: [on/off checkbox] [name]  [Edit...]."""
        box = Border()
        # dbHMS highlight blue (matches the multi-select highlight used elsewhere)
        box.Background = SolidColorBrush(Color.FromRgb(0xEB, 0xF8, 0xFF))
        box.BorderBrush = SolidColorBrush(Color.FromRgb(0x31, 0x82, 0xCE))
        box.BorderThickness = Thickness(1)
        box.CornerRadius = CornerRadius(4)
        box.Padding = Thickness(8)
        box.Margin = Thickness(0, 0, 0, 6)

        grid = WpfGrid()
        from System.Windows.Controls import ColumnDefinition
        from System.Windows import GridLength
        c0 = ColumnDefinition(); c0.Width = self._star_width()
        c1 = ColumnDefinition(); c1.Width = GridLength(72)
        grid.ColumnDefinitions.Add(c0)
        grid.ColumnDefinitions.Add(c1)

        chk = CheckBox()
        chk.Content = name + (" (this model)" if is_host else "")
        chk.IsChecked = True
        chk.FontWeight = FontWeights.SemiBold
        chk.VerticalAlignment = VerticalAlignment.Center
        chk.Checked   += (lambda s, a, n=name: self._on_model_toggle(n, True))
        chk.Unchecked += (lambda s, a, n=name: self._on_model_toggle(n, False))
        WpfGrid.SetColumn(chk, 0)
        grid.Children.Add(chk)

        edit = Button()
        edit.Content = "Edit..."
        edit.Padding = Thickness(8, 2, 8, 2)
        edit.Background = SolidColorBrush(Color.FromRgb(0xED, 0xF2, 0xF7))
        edit.BorderBrush = SolidColorBrush(Color.FromRgb(0xCB, 0xD5, 0xE0))
        edit.Cursor = self._hand_cursor()
        edit.VerticalAlignment = VerticalAlignment.Center
        edit.Click += (lambda s, a, n=name: self._open_model_dialog(n))
        WpfGrid.SetColumn(edit, 1)
        grid.Children.Add(edit)

        box.Child = grid
        return box

    @staticmethod
    def _star_width():
        from System.Windows import GridLength, GridUnitType
        return GridLength(1, GridUnitType.Star)

    @staticmethod
    def _hand_cursor():
        try:
            from System.Windows.Input import Cursors as _C
            return _C.Hand
        except Exception:
            return None

    def _on_model_toggle(self, name, shown):
        if shown:
            self._hidden_models.discard(name)
        else:
            self._hidden_models.add(name)
        self._post("vis:model:{0}:{1}".format(1 if shown else 0, name))

    def _open_model_dialog(self, name):
        info = self._models.get(name)
        if not info:
            return
        cats = info["categories"]
        ws = info["worksets"]
        cat_hidden = set(c for c in cats if (name + "||" + c) in self._hidden_cats)
        ws_hidden = set(w for w in ws if (name + "||" + w) in self._hidden_ws)
        dlg = ModelVisibilityForm(name, cats, ws, cat_hidden, ws_hidden)
        try:
            dlg.Owner = self
        except Exception:
            pass
        if not dlg.ShowDialog():
            return
        for cat, checked in dlg.cat_states.items():
            key = name + "||" + cat
            hidden = key in self._hidden_cats
            if checked and hidden:
                self._hidden_cats.discard(key)
                self._post("vis:cat:1:" + key)
            elif (not checked) and (not hidden):
                self._hidden_cats.add(key)
                self._post("vis:cat:0:" + key)
        for w, checked in dlg.ws_states.items():
            key = name + "||" + w
            hidden = key in self._hidden_ws
            if checked and hidden:
                self._hidden_ws.discard(key)
                self._post("vis:ws:1:" + key)
            elif (not checked) and (not hidden):
                self._hidden_ws.add(key)
                self._post("vis:ws:0:" + key)

    def _export_path(self, doc):
        try:
            if not os.path.isdir(MODELS_DIR):
                os.makedirs(MODELS_DIR)
        except Exception:
            pass
        return os.path.join(MODELS_DIR, _safe_title(doc) + '.glb')

    def _on_close(self, sender, args):
        if self._popout is not None:
            win = self._popout
            self._popout = None
            try:
                win.Closed -= self._on_popout_closed
            except Exception:
                pass
            try:
                win.Content = None
            except Exception:
                pass
            try:
                win.Close()
            except Exception:
                pass
        try:
            if self._webview is not None:
                self._webview.Dispose()
        except Exception:
            pass
        self.Close()


class ModelVisibilityForm(forms.WPFWindow):
    """Popup editor for one model's category + workset visibility. Collects
    checkbox states on Apply; the caller reads cat_states / ws_states and
    pushes the changes to the render."""

    def __init__(self, model_name, categories, worksets, cat_hidden, ws_hidden):
        forms.WPFWindow.__init__(self, MODEL_VIS_XAML)
        self.cat_states = {}
        self.ws_states = {}
        self._cat_checks = {}
        self._ws_checks = {}
        try:
            self.txt_title.Text = model_name
        except Exception:
            pass
        self._populate(self.sp_cats, categories, cat_hidden, self._cat_checks,
                       "No model categories.")
        self._populate(self.sp_ws, worksets, ws_hidden, self._ws_checks,
                       "No worksets (model not workshared).")
        self.btn_cats_all.Click  += (lambda s, a: self._set_all(self._cat_checks, True))
        self.btn_cats_none.Click += (lambda s, a: self._set_all(self._cat_checks, False))
        self.btn_ws_all.Click    += (lambda s, a: self._set_all(self._ws_checks, True))
        self.btn_ws_none.Click   += (lambda s, a: self._set_all(self._ws_checks, False))
        self.btn_apply.Click  += self._on_apply
        self.btn_cancel.Click += self._on_cancel

    def _populate(self, panel, items, hidden, store, empty_msg):
        if not items:
            tb = TextBlock()
            tb.Text = empty_msg
            tb.Foreground = SolidColorBrush(Color.FromRgb(0xA0, 0xAE, 0xC0))
            tb.FontSize = 11
            panel.Children.Add(tb)
            return
        for name in items:
            chk = CheckBox()
            chk.Content = name
            chk.IsChecked = (name not in hidden)
            panel.Children.Add(chk)
            store[name] = chk

    @staticmethod
    def _set_all(store, state):
        for chk in store.values():
            chk.IsChecked = state

    def _on_apply(self, sender, args):
        self.cat_states = dict((n, bool(c.IsChecked))
                               for n, c in self._cat_checks.items())
        self.ws_states = dict((n, bool(c.IsChecked))
                              for n, c in self._ws_checks.items())
        self.DialogResult = True
        self.Close()

    def _on_cancel(self, sender, args):
        self.DialogResult = False
        self.Close()


with dbhms_telemetry.session(__title__, script_path=__file__):
    ViewerForm().ShowDialog()
