# -*- coding: utf-8 -*-
"""View Range Helper - web/glTF rebuild (Phase 1: pipeline skeleton).

The original tool previewed a plan's view range by repeatedly exporting Revit
PNGs (apply a temp view range, export at 3000px, trim white margins, roll back)
and overlaying draggable lines on the bitmap. That round-trip is the bottleneck:
fidgety, fragile pixel-alignment, and slow on big models.

This rebuild takes the 3D Viewer's approach instead. Revit's four view-range
planes (Top, Cut, Bottom, View Depth) map one-to-one onto GPU clipping planes,
so we export the building once and slice it live in a web panel: the plan is a
top-down orthographic camera, the section is an orthographic cut, and dragging a
plane is just a clipping-plane move (instant, dead accurate, no re-render).

Phase 1 builds ONLY the pipeline skeleton:
  * gate to a plan view,
  * export the active view's crop footprint to a small .glb (arch + structural
    only, coarse, low LOD) via the shared clash_export pipeline,
  * host Revit's bundled WebView2 and show the model in a top-down orthographic
    view.
No view-range editing or write-back yet -- that arrives in later phases.

See the project notes for the 8-phase plan. WebView2 is Revit's own bundled
engine (2025/2026 ship it next to Revit.exe), so there's nothing to install.
"""

__title__  = 'View Range\nHelper'
__author__ = 'Nathaniel'
__doc__    = ('Visualize and edit a plan view\'s view range in a live web '
              'plan + section, built on an exported model slice. Rebuild of '
              'the old PNG-preview tool.')

import base64
import json
import os
import shutil
import traceback

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

import System
from System.Windows import Visibility

from pyrevit import forms
import dbhms_ui
import dbhms_telemetry

from clash_export import custom_export
from clash_detect._compat import eid_int, make_eid

from Autodesk.Revit.DB import (
    ElementId, PlanViewPlane, Level, FilteredElementCollector, Transaction,
    ViewPlan,
)

# Revit document handles
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

PLANE_KEYS = ("top", "cut", "bot", "vd")
_PLANE_MAP = (
    ("top", PlanViewPlane.TopClipPlane),
    ("cut", PlanViewPlane.CutPlane),
    ("bot", PlanViewPlane.BottomClipPlane),
    ("vd",  PlanViewPlane.ViewDepthPlane),
)

# Special view-range level ids. When a plane is not pinned to a named level,
# Revit returns one of these non-positive ids from GetLevelId. The mapping is
# undocumented but stable since 2013 (confirmed against the Autodesk API forum
# and our own DIAG reads): -1 Unlimited, -2 Level Above, -3 Associated Level
# (a.k.a. "Current" / same as the plan level), -4 Level Below. These are the
# SAME ids Revit accepts back through SetLevelId, and they mean the same thing
# whether the plane is read from a view or a view template -- a template just
# tends to report the relative ids (esp. -3 Associated) where a concrete view
# resolves "Associated" to its real level id. We treat both identically.
SENTINEL_UNLIMITED  = -1
SENTINEL_ABOVE      = -2
SENTINEL_ASSOCIATED = -3
SENTINEL_BELOW      = -4


# --- Proven Revit view-range logic (ported verbatim from the old PNG tool) ----

def read_view_range(view_plan):
    """Read the four planes of a plan view (or template) into a dict of
    {key: {"level_id": ElementId, "offset": float}}."""
    pvr = view_plan.GetViewRange()
    out = {}
    for key, plane in _PLANE_MAP:
        try:
            lid = pvr.GetLevelId(plane)
        except Exception:
            lid = ElementId.InvalidElementId
        try:
            off = pvr.GetOffset(plane)
        except Exception:
            off = 0.0
        out[key] = {"level_id": lid, "offset": float(off)}
    return out


def lvl_elev(lv):
    """Internal-origin elevation (feet) of a level -- the frame the exported
    geometry lives in, used for ALL positioning. ProjectElevation is internal by
    definition; fall back to Elevation if it's somehow unavailable. Display code
    subtracts get_display_datum_ft from this to show height above the view's
    floor."""
    try:
        return float(lv.ProjectElevation)
    except Exception:
        return float(lv.Elevation)


def get_all_levels_sorted(doc):
    lvls = list(FilteredElementCollector(doc).OfClass(Level)
                .WhereElementIsNotElementType())
    return sorted(lvls, key=lvl_elev)


def get_display_datum_ft(doc, assoc_level):
    """Datum (feet) subtracted from an absolute internal elevation before display
    so the tool reads HEIGHT ABOVE THE FLOOR THIS VIEW IS ON.

    The associated level reads 0'-0", each plane reads its height above that floor
    (cut plane +4'-0", etc.), and other levels read relative to it (the level
    above at +14'-9"). This is project-independent on purpose: it depends only on
    the levels themselves, never on the Project Base Point, Survey Point, or shared
    coordinates, so it behaves identically on every model. The datum is simply the
    associated level's own internal elevation. Returns 0.0 if there's no associated
    level (then the tool shows raw internal elevations). `doc` is unused but kept
    for call-site symmetry.
    """
    if assoc_level is None:
        return 0.0
    return lvl_elev(assoc_level)


def get_associated_level(view_plan):
    try:
        return view_plan.GenLevel
    except Exception:
        return None


def get_level_by_id(doc, lvl_id):
    if lvl_id is None or eid_int(lvl_id) <= 0:
        return None
    el = doc.GetElement(lvl_id)
    return el if isinstance(el, Level) else None


def absolute_z_for_plane(view_plan, level_id, offset_feet, all_levels):
    """Resolve a plane's (level_id, offset) to an absolute elevation in feet.
    Returns None for an Unlimited plane (off-canvas)."""
    base = get_associated_level(view_plan)
    if base is None:
        return None
    base_z = lvl_elev(base)
    iid = eid_int(level_id)
    if iid <= 0:
        if iid == SENTINEL_UNLIMITED:
            return None  # Unlimited -> off-canvas
        sorted_lvls = sorted(all_levels, key=lvl_elev)
        idx = None
        for i, l in enumerate(sorted_lvls):
            if l.Id == base.Id:
                idx = i
                break
        if idx is None:
            return base_z + (offset_feet or 0.0)
        if iid == SENTINEL_ABOVE and idx + 1 < len(sorted_lvls):     # Level Above
            return lvl_elev(sorted_lvls[idx + 1]) + (offset_feet or 0.0)
        if iid == SENTINEL_BELOW and idx - 1 >= 0:                   # Level Below
            return lvl_elev(sorted_lvls[idx - 1]) + (offset_feet or 0.0)
        return base_z + (offset_feet or 0.0)
    lvl = get_level_by_id(doc, level_id)
    if lvl is None:
        return base_z + (offset_feet or 0.0)
    return lvl_elev(lvl) + (offset_feet or 0.0)


def sentinel_name_for(level_id):
    """Map a level-id int to a sentinel name, or None for a real level."""
    iid = eid_int(level_id)
    if iid == SENTINEL_ABOVE:
        return "above"
    if iid == SENTINEL_BELOW:
        return "below"
    if iid == SENTINEL_UNLIMITED:
        return "unlimited"
    if iid == SENTINEL_ASSOCIATED:
        return "associated"
    return None


_SENTINEL_GLYPH = {"above": u"Level Above", "below": u"Level Below",
                   "unlimited": u"Unlimited", "associated": u"Associated Level"}


def write_view_range(view_plan, state, skip_planes=None):
    """Write a state dict {key: {"level_id": ElementId, "offset": float}} back to
    the plan view (or template). Skips keys in skip_planes (RCP Bottom)."""
    skip = skip_planes or set()
    pvr = view_plan.GetViewRange()
    for key, plane in _PLANE_MAP:
        if key in skip:
            continue
        s = state[key]
        try:
            pvr.SetLevelId(plane, s["level_id"])
        except Exception:
            pass
        try:
            pvr.SetOffset(plane, float(s["offset"]))
        except Exception:
            pass
    t = Transaction(doc, "Edit View Range")
    try:
        t.Start()
        view_plan.SetViewRange(pvr)
        t.Commit()
        return True, ""
    except Exception as ex:
        try:
            t.RollBack()
        except Exception:
            pass
        return False, str(ex)


def read_hidden_category_names(view):
    """Display names of the model categories the active plan hides in its V/G
    (directly or via its view template). The web tool starts these categories
    toggled OFF so the plan/section open matching what Revit shows, while leaving
    them present in the .glb so the engineer can switch any of them back on.

    Host document only: a linked model's per-link category visibility is a
    separate API surface we don't read here, so linked categories start ON. Names
    match the `category` tag stamped into the .glb (Category.Name)."""
    from Autodesk.Revit.DB import CategoryType
    hidden = []
    try:
        cats = doc.Settings.Categories
    except Exception:
        return hidden
    for cat in cats:
        try:
            if cat.CategoryType != CategoryType.Model:
                continue
            if not cat.get_AllowsVisibilityControl(view):
                continue
            if view.GetCategoryHidden(cat.Id):
                nm = cat.Name
                if nm:
                    hidden.append(nm)
        except Exception:
            continue
    return sorted(set(hidden))


def read_hidden_worksets(view):
    """Names of the user worksets the active plan hides in its V/G. The web tool
    starts these toggled OFF (mirroring the plan) while keeping their geometry in
    the .glb so they can be switched on. Host document only; empty if the model
    is not workshared. Names match the `workset` tag stamped into the .glb."""
    from Autodesk.Revit.DB import (FilteredWorksetCollector, WorksetKind,
                                   WorksetVisibility)
    hidden = []
    try:
        if not doc.IsWorkshared:
            return hidden
    except Exception:
        return hidden
    try:
        for ws in FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset):
            try:
                vis = view.GetWorksetVisibility(ws.Id)
                if vis == WorksetVisibility.Hidden:
                    hidden.append(ws.Name)
            except Exception:
                continue
    except Exception:
        pass
    return sorted(set(hidden))


def get_disabled_planes(view_plan):
    """RCP locks Bottom to the Cut plane; Revit won't let it be edited."""
    try:
        vt_str = str(view_plan.ViewType)
    except Exception:
        return set()
    return set(["bot"]) if vt_str == "CeilingPlan" else set()


def _find_view_range_param_id(template):
    try:
        params = template.Parameters
    except Exception:
        return None
    target_names = ("view range", "plan view range")
    for p in params:
        try:
            d = p.Definition
            if d is None:
                continue
            if (d.Name or "").strip().lower() in target_names:
                return p.Id
        except Exception:
            continue
    return None


def is_view_range_template_locked(view_plan):
    """Return (locked, template) for a plan view. Locked means the view's
    template controls View Range and it is NOT in the non-controlled list."""
    try:
        tpl_id = view_plan.ViewTemplateId
    except Exception:
        return False, None
    if tpl_id is None or eid_int(tpl_id) == -1:
        return False, None
    tpl = doc.GetElement(tpl_id)
    if tpl is None:
        return False, None
    vr_pid = _find_view_range_param_id(tpl)
    if vr_pid is None:
        return False, tpl
    try:
        non_ctrl = list(tpl.GetNonControlledTemplateParameterIds())
    except Exception:
        non_ctrl = []
    for eid in non_ctrl:
        if eid_int(eid) == eid_int(vr_pid):
            return False, tpl     # in non-controlled list => NOT locked
    return True, tpl


def detach_view_range_from_template(view_plan):
    """Add the View Range parameter to the template's non-controlled list so this
    view's view range can be edited independently."""
    tpl_id = view_plan.ViewTemplateId
    if eid_int(tpl_id) == -1:
        return False, "View has no template."
    tpl = doc.GetElement(tpl_id)
    if tpl is None:
        return False, "Template element could not be loaded."
    vr_pid = _find_view_range_param_id(tpl)
    if vr_pid is None:
        return False, "Could not find a 'View Range' parameter on the template."
    try:
        non_ctrl = list(tpl.GetNonControlledTemplateParameterIds())
    except Exception:
        non_ctrl = []
    for eid in non_ctrl:
        if eid_int(eid) == eid_int(vr_pid):
            return True, "Already detached."
    non_ctrl.append(vr_pid)
    from System.Collections.Generic import List as NetList
    eid_list = NetList[ElementId]()
    for eid in non_ctrl:
        eid_list.Add(eid)
    t = Transaction(doc, "Detach view from template view range")
    try:
        t.Start()
        tpl.SetNonControlledTemplateParameterIds(eid_list)
        t.Commit()
        return True, ""
    except Exception as ex:
        try:
            t.RollBack()
        except Exception:
            pass
        return False, str(ex)

SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML     = os.path.join(SCRIPT_DIR, 'ViewRangeHelperForm.xaml')
LAUNCHER_XAML = os.path.join(SCRIPT_DIR, 'LauncherForm.xaml')
WEB_DIR    = os.path.join(SCRIPT_DIR, 'web')
APP_PAGE   = 'viewrange.html'        # served over the virtual host (three.js)

# Writable runtime root: the viewer assets + the exported model slice are served
# from here through a WebView2 virtual host so the page can fetch a large .glb
# (the base64 message channel maxes out around 25 MB).
_DATA_ROOT = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or SCRIPT_DIR,
    'dbHMS', 'ViewRange')
APP_DIR    = os.path.join(_DATA_ROOT, 'app')
MODELS_DIR = os.path.join(_DATA_ROOT, 'models')
VHOST      = 'dbhms.viewrange'        # virtual hostname mapped to _DATA_ROOT

_WEBVIEW2_WPF  = 'Microsoft.Web.WebView2.Wpf.dll'
_WEBVIEW2_CORE = 'Microsoft.Web.WebView2.Core.dll'

LOG_PATH = os.path.join(_DATA_ROOT, 'viewrange.log')


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


def _safe_title(title):
    """Sanitize a string into a filename-safe, URL-safe stem (spaces -> _)."""
    keep = []
    for c in (title or 'model'):
        if c.isalnum() or c in ('_', '-'):
            keep.append(c)
        elif c == ' ':
            keep.append('_')
    return ''.join(keep).strip('_') or 'model'


def _revit_dir_with_webview2():
    """Directory holding Revit's bundled WebView2 assemblies, or None. Revit
    2025/2026 ship them next to Revit.exe; the running process IS Revit.exe."""
    try:
        exe = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName
        d = os.path.dirname(exe)
        if os.path.isfile(os.path.join(d, _WEBVIEW2_WPF)):
            return d
    except Exception:
        pass
    return None


def _load_webview2_type():
    """Reference Revit's WebView2 assemblies and return (WebView2 type, None) on
    success or (None, reason) on failure."""
    d = _revit_dir_with_webview2()
    if d is None:
        return None, ("WebView2 assemblies were not found next to Revit.exe. "
                      "They ship with Revit 2025/2026; this Revit may be older "
                      "or installed somewhere unexpected.")
    try:
        clr.AddReferenceToFileAndPath(os.path.join(d, _WEBVIEW2_CORE))
        clr.AddReferenceToFileAndPath(os.path.join(d, _WEBVIEW2_WPF))
        from Microsoft.Web.WebView2.Wpf import WebView2
        return WebView2, None
    except Exception:
        return None, "Failed to load WebView2:\n\n{}".format(traceback.format_exc())


class ViewRangeForm(forms.WPFWindow):
    def __init__(self, view, model_path, stats):
        # handle_esc=False: don't let pyRevit close the whole tool on Escape.
        forms.WPFWindow.__init__(self, FORM_XAML, handle_esc=False)
        self._webview = None
        self._vhost_ok = False
        self._page_ready = False
        self._model_path = model_path     # the exported .glb to display
        self._model_version = 0

        self._view = view                 # the active plan view (spatial context)
        self._stats = stats or {}         # export stats (carries offset_ft)
        self._target = view               # read/write target: the view, or its
                                          # template when "edit template instead"
        self._disabled_planes = get_disabled_planes(view)
        self._orig_levelids = {}          # key -> original ElementId (reused on
                                          # write-back when a plane is unchanged,
                                          # so the exact sentinel encoding round-trips)

        self.btn_close.Click += self._on_close
        # Attach the web panel after layout so the host Border has a real size.
        self.Loaded += self._on_loaded

        # Brand logo in the header (loaded from the sibling PNG)
        self._load_logo()

    def _load_logo(self):
        """Load the dbHMS wordmark PNG into the header Image.

        Decoded down to header height so the 5950px master never sits in
        memory at full size; wrapped in try/except so a missing or broken
        file can never break the tool."""
        try:
            from System import Uri, UriKind
            from System.Windows.Media.Imaging import (
                BitmapImage, BitmapCacheOption)
            path = os.path.join(SCRIPT_DIR, 'dbhms_logo.png')
            if not os.path.exists(path):
                return
            bmp = BitmapImage()
            bmp.BeginInit()
            bmp.CacheOption = BitmapCacheOption.OnLoad
            bmp.UriSource = Uri(path, UriKind.Absolute)
            bmp.DecodePixelHeight = 96
            bmp.EndInit()
            self.img_logo.Source = bmp
        except Exception:
            pass

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
        try:
            self._sync_app_assets()
            self._WebView2 = WebView2
            self._udf_path = os.path.join(_DATA_ROOT, 'WebView2')
            self._wv_retried = False
            self._cleanup_retry_udfs(self._udf_path)
            self._create_webview(self._udf_path)
        except Exception:
            _log("attach_viewer: EXCEPTION\n{}".format(traceback.format_exc()))
            self._show_viewport_message(
                "The 3D panel failed to start:\n\n{}".format(traceback.format_exc()))

    def _create_webview(self, udf_path):
        """Create the WebView2 control against a specific user-data folder.
        Called once normally, and again by the init-failure retry with a
        fresh per-process folder."""
        from Microsoft.Web.WebView2.Wpf import CoreWebView2CreationProperties
        wv = self._WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = udf_path
        wv.CreationProperties = props
        wv.CoreWebView2InitializationCompleted += self._on_webview_init
        self.brd_viewport.Child = wv
        self._webview = wv
        wv.EnsureCoreWebView2Async(None)
        _log("attach_viewer: webview created (udf={0})".format(udf_path))

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
                        _log("attach_viewer: removed stale retry profile {0}".format(name))
                    except Exception:
                        pass
        except Exception:
            pass

    def _sync_app_assets(self):
        """Copy the viewer's web assets into the served app folder so the virtual
        host can serve them (and the model) from one origin."""
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
                        shutil.copy2(os.path.join(dirpath, name),
                                     os.path.join(dst_dir, name))
                        n += 1
                    except Exception:
                        _log("sync_app_assets: skip {0} ({1})".format(
                            name, traceback.format_exc().splitlines()[-1]))
            _log("sync_app_assets: copied {0} file(s)".format(n))
        except Exception:
            _log("sync_app_assets: EXCEPTION\n{}".format(traceback.format_exc()))

    def _on_webview_init(self, sender, args):
        try:
            if not args.IsSuccess:
                _log("init: FAILED\n{0}".format(args.InitializationException))
                # One-shot self-heal for 0x8007139F-class profile conflicts
                # (stale msedgewebview2.exe holding the folder, or a runtime
                # update under Revit): retry with a fresh per-process folder.
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
            try:
                core.WebMessageReceived += self._on_web_message
            except Exception:
                _log("init: WebMessageReceived wire failed\n{0}".format(
                    traceback.format_exc()))
            # Resolve the access-kind enum from the SAME assembly instance the
            # core object came from, to dodge IronPython's type-identity error.
            ak_type = core.GetType().Assembly.GetType(
                "Microsoft.Web.WebView2.Core.CoreWebView2HostResourceAccessKind")
            allow = System.Enum.Parse(ak_type, "Allow")
            app_index = os.path.join(APP_DIR, APP_PAGE)
            if os.path.isfile(app_index):
                core.SetVirtualHostNameToFolderMapping(VHOST, _DATA_ROOT, allow)
                page = self._versioned_page(app_index)
                core.Navigate("https://{0}/app/{1}".format(VHOST, page))
                self._vhost_ok = True
                _log("init: vhost mapped, navigated to app/{0}".format(page))
            else:
                self._show_viewport_message(
                    "Viewer page is missing:\n\n{}".format(app_index))
        except Exception:
            _log("init: EXCEPTION\n{}".format(traceback.format_exc()))

    def _versioned_page(self, app_index):
        """Copy the served page to a per-version filename so each update navigates
        to a fresh URL no WebView2 cache can satisfy with a stale copy."""
        try:
            stem = APP_PAGE.rsplit('.', 1)[0]
            ver = int(os.path.getmtime(app_index))
            for f in os.listdir(APP_DIR):
                if (f.startswith(stem + '.') and f.endswith('.html')
                        and f != APP_PAGE):
                    try:
                        os.remove(os.path.join(APP_DIR, f))
                    except Exception:
                        pass
            versioned = "{0}.{1}.html".format(stem, ver)
            shutil.copy2(app_index, os.path.join(APP_DIR, versioned))
            return versioned
        except Exception:
            return APP_PAGE

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
        if msg == "ready":
            # The page's message listener is wired and three.js is imported.
            # Safe to push meta + the model now (no race against module load).
            self._page_ready = True
            self._post_meta()
            self._load_model()
            return
        if msg.startswith("apply:"):
            try:
                self._apply_planes(json.loads(msg[len("apply:"):]))
            except Exception:
                _log("apply: EXCEPTION\n{0}".format(traceback.format_exc()))
                self._post("applied:" + json.dumps({"ok": False, "err": "internal error"}))
            return
        if msg == "detach":
            self._do_detach()
            return
        if msg == "edittemplate":
            self._do_edit_template()
            return

    # --- view-range meta + apply --------------------------------------

    def _diag_view_range(self, view, target, vr, all_levels, assoc):
        """One-off diagnostic: dump the raw level ids + offsets a template (or
        view) reports, so the edit-template level mapping can be made exact.
        Logging only; never affects behavior. Safe to remove once the
        template-mode editor is finalized."""
        try:
            editing_template = (target is not view)
            _log("DIAG ===== view-range read ({0}) =====".format(
                "TEMPLATE" if editing_template else "view"))
            try:
                tname = getattr(target, "Name", "?")
            except Exception:
                tname = "?"
            _log("DIAG target='{0}' editing_template={1}".format(
                tname, editing_template))
            datum = get_display_datum_ft(doc, assoc)
            _log("DIAG display_datum_ft (assoc-floor elevation) = {0}".format(datum))
            _log("DIAG active-view assoc level: id={0} name='{1}' elev={2}".format(
                (eid_int(assoc.Id) if assoc is not None else None),
                (assoc.Name if assoc is not None else None),
                (assoc.Elevation if assoc is not None else None)))
            for lv in all_levels:
                try:
                    proj_elev = lv.ProjectElevation
                except Exception:
                    proj_elev = None
                _log("DIAG   level id={0} name='{1}' Elevation={2} "
                     "ProjectElevation={3} displayed(Elevation-datum)={4}".format(
                         eid_int(lv.Id), lv.Name, lv.Elevation, proj_elev,
                         (lv.Elevation - datum)))
            for key in PLANE_KEYS:
                lid = vr[key]["level_id"]
                off = vr[key]["offset"]
                _log("DIAG   plane {0}: raw_level_id_int={1} offset_ft={2} "
                     "sentinel={3} get_level_by_id={4}".format(
                         key, eid_int(lid), off, sentinel_name_for(lid),
                         (lambda l: l.Name if l is not None else None)(
                             get_level_by_id(doc, lid))))
            # Also dump the VIEW's own read, for side-by-side comparison even
            # while editing the template.
            if editing_template:
                vvr = read_view_range(view)
                _log("DIAG ----- same view, read from the VIEW itself -----")
                for key in PLANE_KEYS:
                    lid = vvr[key]["level_id"]
                    off = vvr[key]["offset"]
                    _log("DIAG   plane {0}: raw_level_id_int={1} offset_ft={2} "
                         "sentinel={3}".format(
                             key, eid_int(lid), off, sentinel_name_for(lid)))
            _log("DIAG ===== end view-range read =====")
        except Exception:
            _log("DIAG: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _build_meta(self):
        """Assemble the Section 4.1 meta dict from the read target's view range,
        the document levels, and the export offset. Read/written against
        self._target (the view, or its template in edit-template mode); spatial
        info (crop, name) always comes from self._view."""
        view = self._view
        target = self._target
        all_levels = get_all_levels_sorted(doc)
        vr = read_view_range(target)
        self._orig_levelids = dict((k, vr[k]["level_id"]) for k in PLANE_KEYS)

        assoc = get_associated_level(view)
        editing_template = (target is not view)
        self._diag_view_range(view, target, vr, all_levels, assoc)

        levels_meta = []
        for lv in all_levels:
            levels_meta.append({
                "id": eid_int(lv.Id), "name": lv.Name, "elev_ft": lvl_elev(lv),
            })

        vr_meta = {}
        for key in PLANE_KEYS:
            lid = vr[key]["level_id"]
            off = vr[key]["offset"]
            raw_int = eid_int(lid)
            # One code path for views and templates: classify the plane's level
            # id into a relative sentinel (associated / above / below / unlimited)
            # or a real named level, and resolve it to an absolute elevation
            # against the active view's associated level for the canvas.
            sent = sentinel_name_for(lid)
            abs_ft = absolute_z_for_plane(view, lid, off, all_levels)
            if sent:
                lname = _SENTINEL_GLYPH.get(sent, sent)
            else:
                lvl = get_level_by_id(doc, lid)
                lname = lvl.Name if lvl is not None else ""
            vr_meta[key] = {
                "level_id": raw_int, "level_name": lname,
                "offset_ft": float(off),
                "abs_ft": (None if abs_ft is None else float(abs_ft)),
                "sentinel": sent,
            }

        crop = custom_export._view_crop_world(view)
        if crop is None:
            crop = custom_export._view_xy_extent(doc, view)
        if crop is not None:
            crop_ft = {"min_x": crop[0], "min_y": crop[1],
                       "max_x": crop[2], "max_y": crop[3]}
        else:
            crop_ft = {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0}

        zmin, zmax = custom_export._model_z_extent_ft(doc)
        offset_ft = self._stats.get("offset_ft") or [0.0, 0.0, 0.0]

        try:
            vt_str = str(view.ViewType)
        except Exception:
            vt_str = "FloorPlan"
        is_rcp = (vt_str == "CeilingPlan")

        # The plan's screen-up in world XY, so the web plan can square up a
        # building that is rotated in world coordinates (project/true north).
        try:
            ud = view.UpDirection
            up_dir = [float(ud.X), float(ud.Y)]
        except Exception:
            up_dir = [0.0, 1.0]

        locked, tpl = is_view_range_template_locked(view)
        # In edit-template mode the banner is gone; we're deliberately editing
        # the template, so report unlocked.
        tpl_name = None
        try:
            if tpl is not None:
                tpl_name = tpl.Name
        except Exception:
            tpl_name = None

        view_name = getattr(view, "Name", "")
        if editing_template and tpl is not None:
            view_name = "[Template] " + (tpl_name or view_name)

        meta = {
            "schema": "dbhms.viewrange.meta/1",
            "units": "meters", "ft_to_m": custom_export.FT_TO_M,
            "offset_ft": [offset_ft[0], offset_ft[1], offset_ft[2]],
            "model_z_extent_ft": [float(zmin), float(zmax)],
            # Subtract from any ABSOLUTE elevation before display so numbers read
            # as height above the floor this view is on (associated level = 0).
            # Positioning stays in internal feet; this is display-only.
            "display_datum_ft": get_display_datum_ft(doc, assoc),
            "view": {
                "name": view_name, "view_type": vt_str,
                "is_ceiling_plan": is_rcp,
                "associated_level_id": (eid_int(assoc.Id) if assoc is not None else -1),
                "associated_level_name": (assoc.Name if assoc is not None else ""),
                "crop_ft": crop_ft,
                "up_dir": up_dir,
            },
            "levels": levels_meta,
            "view_range": vr_meta,
            "sentinels": {
                "top": {"above": True,  "below": False, "unlimited": False},
                "cut": {"above": False, "below": False, "unlimited": False},
                "bot": {"above": False, "below": True,  "unlimited": False},
                "vd":  {"above": False, "below": True,  "unlimited": True},
            },
            "sentinel_ids": {"above": SENTINEL_ABOVE, "below": SENTINEL_BELOW,
                             "unlimited": SENTINEL_UNLIMITED,
                             "associated": SENTINEL_ASSOCIATED},
            "disabled_planes": sorted(self._disabled_planes),
            # Category show/hide tree (web left rail). Categories the source plan
            # hides start toggled OFF; shell categories render with the clay/poche
            # look, everything else in its Revit colors. All categories are present
            # in the .glb regardless, so any can be switched on in the tool.
            "hidden_categories": read_hidden_category_names(view),
            "hidden_worksets": read_hidden_worksets(view),
            "shell_categories": custom_export.shell_category_names(doc),
            # The host model's title, so the visibility panel can tag its row
            # "(this model)" and list links separately, like the 3D Viewer.
            "host_model": getattr(doc, "Title", "") or "",
            # In template mode the page mirrors Revit's template View Range dialog:
            # each non-Cut plane offers Associated Level / Level Above / Level Below
            # (+ Unlimited for View Depth) and every named level, and the Cut Plane
            # is locked to Associated Level (offset only). All four options write
            # back the real -1/-2/-3/-4 sentinel ids.
            "template_mode": bool(editing_template),
            "template_lock": {
                "locked": bool(locked and not editing_template),
                "template_name": tpl_name,
            },
            "snap": {"enabled": True, "distance_ft": 0.5},
        }
        return meta

    def _post_meta(self):
        try:
            meta = self._build_meta()
            self._post("meta:" + json.dumps(meta))
            _log("posted meta ({0} levels, target={1})".format(
                len(meta["levels"]), "template" if self._target is not self._view else "view"))
        except Exception:
            _log("_post_meta: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _level_eid_for(self, key, incoming_int):
        """Map an incoming integer level_id back to an ElementId. The meta ids
        the page sends back are the SAME ids Revit uses (positive real levels and
        the -1/-2/-3/-4 sentinels), so this is a direct construction. When a
        plane is unchanged we reuse the exact ElementId we read, which guarantees
        a byte-perfect round-trip of whatever Revit originally stored."""
        incoming_int = int(incoming_int)
        orig = self._orig_levelids.get(key)
        if orig is not None and eid_int(orig) == incoming_int:
            return orig
        return make_eid(incoming_int)

    def _apply_planes(self, payload):
        planes = (payload or {}).get("planes", {})
        state = {}
        for key in PLANE_KEYS:
            p = planes.get(key) or {}
            lid_int = p.get("level_id", 0)
            state[key] = {
                "level_id": self._level_eid_for(key, lid_int),
                "offset": float(p.get("offset_ft", 0.0)),
            }
        ok, err = write_view_range(self._target, state,
                                   skip_planes=self._disabled_planes)
        self._post("applied:" + json.dumps({"ok": bool(ok), "err": err}))
        _log("apply -> ok={0} err={1}".format(ok, err))

    def _do_detach(self):
        ok, err = detach_view_range_from_template(self._view)
        if ok:
            # re-read now that the view controls its own view range
            self._post_meta()
            _log("detach ok ({0})".format(err or "detached"))
        else:
            self._post("applied:" + json.dumps({"ok": False, "err": err}))
            _log("detach failed: {0}".format(err))

    def _do_edit_template(self):
        locked, tpl = is_view_range_template_locked(self._view)
        if tpl is None:
            self._post("applied:" + json.dumps(
                {"ok": False, "err": "This view has no template to edit."}))
            return
        self._target = tpl
        self._post_meta()
        _log("edit-template: target switched to '{0}'".format(
            getattr(tpl, "Name", "?")))

    def _load_model(self):
        """Post the exported model URL to the page for display."""
        try:
            wv = self._webview
            path = self._model_path
            if wv is None or wv.CoreWebView2 is None or not path:
                return
            if not os.path.isfile(path):
                _log("load_model: file missing {0}".format(path))
                return
            self._model_version += 1
            if self._vhost_ok:
                url = "https://{0}/models/{1}?v={2}".format(
                    VHOST, os.path.basename(path), self._model_version)
                wv.CoreWebView2.PostWebMessageAsString("url:" + url)
                _log("load_model: posted URL {0}".format(url))
            else:
                with open(path, 'rb') as f:
                    data = f.read()
                b64 = base64.b64encode(data)
                if isinstance(b64, bytes):
                    b64 = b64.decode('ascii')
                wv.CoreWebView2.PostWebMessageAsString("b64:" + b64)
                _log("load_model: posted base64 ({0} bytes)".format(len(data)))
        except Exception:
            _log("load_model: EXCEPTION\n{0}".format(traceback.format_exc()))

    def _post(self, msg):
        """Send a control message to the page. Best-effort."""
        try:
            wv = self._webview
            if wv is not None and wv.CoreWebView2 is not None:
                wv.CoreWebView2.PostWebMessageAsString(msg)
        except Exception:
            pass

    def _show_viewport_message(self, text):
        """Replace the viewport with a plain text message (init failure path)."""
        try:
            from System.Windows.Controls import TextBlock
            from System.Windows import TextWrapping, Thickness
            tb = TextBlock()
            tb.Text = text
            tb.TextWrapping = TextWrapping.Wrap
            tb.Margin = Thickness(16)
            self.brd_viewport.Child = tb
        except Exception:
            pass

    def _on_close(self, sender, args):
        try:
            self.Close()
        except Exception:
            pass


def _active_plan_view(doc):
    """Return the active plan view, or None if the active view isn't a plan."""
    from Autodesk.Revit.DB import ViewPlan
    try:
        v = doc.ActiveView
    except Exception:
        return None
    return v if isinstance(v, ViewPlan) else None


def _do_events():
    """Pump pending WPF UI events once, so the launcher's Abort button stays
    clickable while the (main-thread, blocking) export runs. CustomExporter polls
    IsCanceled() frequently during Export(); the launcher's cancel hook calls this
    on each poll, letting a queued Abort click run and flip the cancel flag.
    Best-effort; never raises."""
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


class LauncherForm(forms.WPFWindow):
    """Lightweight gate shown the instant the tool is launched. NOTHING heavy runs
    until the user clicks Launch Helper, so pressing the toolbar button never locks
    Revit by surprise. Launch runs the export inline (it must be on the Revit main
    thread) but keeps an Abort button live via _do_events(), so a user can bail out
    of a long/laggy export without killing Revit. On success it stashes the stats +
    output path and closes; main() then opens the viewer."""

    def __init__(self, view, out_path):
        forms.WPFWindow.__init__(self, LAUNCHER_XAML, handle_esc=False)
        self._view = view
        self.out_path = out_path
        self.launched = False        # set True only on a successful export
        self.stats = None
        self._abort = False
        self._busy = False
        self._last_pump = 0          # TickCount of the last UI pump (throttle)
        try:
            self.txt_view.Text = "Active view: {0}".format(
                getattr(view, 'Name', '?'))
        except Exception:
            pass
        self.btn_launch.Click += self._on_launch
        self.btn_abort.Click += self._on_abort
        self.btn_close.Click += self._on_close
        self.Closing += self._on_closing

        # Brand logo in the header (loaded from the sibling PNG)
        self._load_logo()

    def _load_logo(self):
        """Load the dbHMS wordmark PNG into the header Image.

        Decoded down to header height so the 5950px master never sits in
        memory at full size; wrapped in try/except so a missing or broken
        file can never break the tool."""
        try:
            from System import Uri, UriKind
            from System.Windows.Media.Imaging import (
                BitmapImage, BitmapCacheOption)
            path = os.path.join(SCRIPT_DIR, 'dbhms_logo.png')
            if not os.path.exists(path):
                return
            bmp = BitmapImage()
            bmp.BeginInit()
            bmp.CacheOption = BitmapCacheOption.OnLoad
            bmp.UriSource = Uri(path, UriKind.Absolute)
            bmp.DecodePixelHeight = 96
            bmp.EndInit()
            self.img_logo.Source = bmp
        except Exception:
            pass

    # ---- state toggles ----------------------------------------------------
    def _show_progress(self, on):
        self.pnl_intro.Visibility = (
            Visibility.Collapsed if on else Visibility.Visible)
        self.pnl_progress.Visibility = (
            Visibility.Visible if on else Visibility.Collapsed)
        self.btn_launch.Visibility = (
            Visibility.Collapsed if on else Visibility.Visible)
        self.btn_close.Visibility = (
            Visibility.Collapsed if on else Visibility.Visible)
        self.btn_abort.Visibility = (
            Visibility.Visible if on else Visibility.Collapsed)

    def _cancel_check(self):
        """Hook handed to export_region, polled thousands of times during a big
        export. Pump the UI at most a few times a second (NOT every poll, which
        slows the export to a crawl) so the Abort button stays clickable, then
        report whether the user asked to stop. The flag read itself is instant."""
        tc = System.Environment.TickCount
        if tc - self._last_pump >= 150:    # ms; ~6-7 pumps/sec is plenty
            self._last_pump = tc
            _do_events()
        return self._abort

    # ---- events -----------------------------------------------------------
    def _on_launch(self, sender, args):
        if self._busy:
            return
        self._busy = True
        self._abort = False
        self._show_progress(True)
        self.txt_status.Text = "Exporting..."
        _do_events()    # paint the progress state before the export blocks
        _log("launcher: export start, view '{0}'".format(
            getattr(self._view, 'Name', '?')))
        try:
            stats = custom_export.export_region(
                doc, self.out_path, self._view, is_canceled=self._cancel_check)
        except custom_export.ExportCanceled:
            _log("launcher: export aborted by user")
            self._busy = False
            self._show_progress(False)
            self.txt_status.Text = "Aborted. Nothing was changed in your model."
            return
        except Exception:
            _log("launcher: export FAILED\n{0}".format(traceback.format_exc()))
            self._busy = False
            self._show_progress(False)
            self.txt_status.Text = "Export failed."
            dbhms_ui.info(
                "Couldn't export the model slice for this view.\n\n{0}".format(
                    traceback.format_exc().splitlines()[-1]),
                title="Export failed")
            return
        try:
            _log("export by model: {0}".format(stats.get("by_model")))
            _log("export by category: {0}".format(stats.get("by_category")))
        except Exception:
            pass
        self.stats = stats
        self.launched = True
        self._busy = False
        self.Close()

    def _on_abort(self, sender, args):
        self._abort = True
        try:
            self.txt_status.Text = "Aborting..."
            self.txt_progress.Text = "Stopping the export..."
        except Exception:
            pass

    def _on_close(self, sender, args):
        if self._busy:
            self._abort = True
            return
        self.Close()

    def _on_closing(self, sender, args):
        # X-button during an export: turn it into an abort and keep the window up
        # until the export unwinds, so we never tear down mid-export.
        if self._busy:
            self._abort = True
            try:
                args.Cancel = True
                self.txt_status.Text = "Aborting..."
            except Exception:
                pass


def main():
    view = _active_plan_view(doc)
    if view is None:
        forms.alert(
            "View Range Helper works on a plan view (floor plan, ceiling plan, "
            "area plan, etc.).\n\nOpen a plan view and run the tool again.",
            title="No plan view active", exitscript=True)
        return

    for d in (_DATA_ROOT, MODELS_DIR):
        if not os.path.isdir(d):
            os.makedirs(d)

    stem = _safe_title(getattr(doc, 'Title', 'model'))
    out_path = os.path.join(MODELS_DIR, "{0}_vr.glb".format(stem))

    # Gate: show the launcher first. The export (which can lock Revit for minutes)
    # only runs if the user clicks Launch, and stays abortable while it does.
    launcher = LauncherForm(view, out_path)
    launcher.ShowDialog()
    if not launcher.launched:
        _log("launcher: closed without launching")
        return

    stats = launcher.stats
    if not stats or not stats.get("triangles"):
        dbhms_ui.info(
            "The scoped export found no architectural or structural geometry in "
            "this view's crop region. The viewer will open empty.",
            title="Nothing to show")

    ViewRangeForm(view, out_path, stats).ShowDialog()


if __name__ == '__main__':
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
