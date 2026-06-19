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

import os
import time
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import System
from System.Windows import (
    TextWrapping, TextAlignment, Thickness,
    HorizontalAlignment, VerticalAlignment,
)
from System.Windows.Controls import TextBlock
from System.Windows.Input import Mouse, Cursors
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_export import revit_geometry, gltf


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML  = os.path.join(SCRIPT_DIR, 'ViewerForm.xaml')
WEB_INDEX  = os.path.join(SCRIPT_DIR, 'web', 'index.html')

# WebView2 assemblies we need from Revit's install directory.
_WEBVIEW2_WPF  = 'Microsoft.Web.WebView2.Wpf.dll'
_WEBVIEW2_CORE = 'Microsoft.Web.WebView2.Core.dll'


def _safe_title(doc):
    """Sanitize the document title into a filename-safe stem."""
    try:
        title = doc.Title or 'model'
    except Exception:
        title = 'model'
    keep = [c for c in title if c.isalnum() or c in (' ', '_', '-')]
    return ''.join(keep).strip() or 'model'


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
        forms.WPFWindow.__init__(self, FORM_XAML)
        self._webview = None

        self.btn_close.Click  += self._on_close
        self.btn_export.Click += self._on_export

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
            wv = WebView2()
            # WebView2 writes a cache/user-data folder; default sits next to
            # the host exe (Program Files, read-only). Point it somewhere
            # writable so initialization can't fail on permissions.
            props = CoreWebView2CreationProperties()
            base = os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or SCRIPT_DIR
            props.UserDataFolder = os.path.join(base, 'dbHMS', '3DViewer', 'WebView2')
            wv.CreationProperties = props
            # Surface async init failures (missing Evergreen runtime, locked
            # user-data folder, etc.) instead of leaving a blank panel.
            wv.CoreWebView2InitializationCompleted += self._on_webview_init
            self.brd_viewport.Child = wv
            self._webview = wv
            # Setting Source kicks off implicit initialization and navigates
            # to our local page once the core is ready.
            wv.Source = System.Uri(WEB_INDEX)
        except Exception:
            self._show_viewport_message(
                "The 3D panel failed to start:\n\n{}".format(traceback.format_exc()))

    def _on_webview_init(self, sender, args):
        # args.IsSuccess is False if the WebView2 core couldn't start.
        try:
            if not args.IsSuccess:
                self._show_viewport_message(
                    "WebView2 failed to initialize:\n\n{}".format(
                        args.InitializationException))
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

    def _on_export(self, sender, args):
        """Phase 2: export a small slice of the model to a .glb and open it
        so Nathan can confirm the geometry came out right. In-panel loading
        comes next phase."""
        from pyrevit import revit
        doc = revit.doc
        if doc is None:
            dbhms_ui.info("No active Revit document to export.", title='3D Viewer')
            return
        try:
            view = doc.ActiveView
        except Exception:
            view = None

        Mouse.OverrideCursor = Cursors.Wait
        result = None
        path = None
        size_bytes = 0
        error = None
        t0 = time.time()
        try:
            result = revit_geometry.extract_slice(doc, view)
            if result["meshes"]:
                path = self._export_path(doc)
                size_bytes = gltf.write_glb(
                    path, result["meshes"], asset_extras=result["asset_extras"])
        except Exception:
            error = traceback.format_exc()
        finally:
            Mouse.OverrideCursor = None
        seconds = time.time() - t0

        if error is not None:
            dbhms_ui.info("Export failed:\n\n{}".format(error),
                          title='3D Viewer export failed')
            return
        if not result["meshes"]:
            dbhms_ui.info(
                "No 3D geometry found to export.\n\n"
                "Open a 3D view with model elements and try again.",
                title='3D Viewer')
            return

        stats = result["stats"]
        size_mb = size_bytes / (1024.0 * 1024.0)
        note = " (capped slice)" if stats.get("capped") else ""
        host_n = stats.get("host_elements", 0)
        link_n = stats.get("link_elements", 0)
        models_n = stats.get("models", 0)
        self.txt_status.Text = (
            "Exported {0} host + {1} linked elements, {2:,} triangles, "
            "{3:.1f} MB in {4:.1f}s{5}.".format(
                host_n, link_n, stats["triangles"], size_mb, seconds, note))
        dbhms_ui.info(
            "Exported a slice of the model to glTF (.glb).\n\n"
            "Host elements: {0}\nLinked elements: {1} (from {2} linked "
            "model(s))\nTriangles: {3:,}\nFile size: {4:.1f} MB\n"
            "Time: {5:.1f}s{6}\n\nFile:\n{7}\n\n"
            "Opening it now so you can confirm the geometry. In-panel "
            "loading arrives in the next phase.".format(
                host_n, link_n, models_n, stats["triangles"], size_mb,
                seconds, note, path),
            title='3D Viewer export complete')
        try:
            os.startfile(path)
        except Exception:
            try:
                os.startfile(os.path.dirname(path))
            except Exception:
                pass

    def _export_path(self, doc):
        base = (os.environ.get('LOCALAPPDATA')
                or os.environ.get('TEMP') or SCRIPT_DIR)
        out_dir = os.path.join(base, 'dbHMS', '3DViewer', 'models')
        try:
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
        except Exception:
            out_dir = base
        return os.path.join(out_dir, _safe_title(doc) + '.glb')

    def _on_close(self, sender, args):
        try:
            if self._webview is not None:
                self._webview.Dispose()
        except Exception:
            pass
        self.Close()


with dbhms_telemetry.session(__title__, script_path=__file__):
    ViewerForm().ShowDialog()
