# View Range Helper — Web/glTF Rebuild: Build Spec for an Autonomous Agent

You are picking this up cold. Read this whole document before writing code. It
contains every decision already made, the proven Revit logic to reuse verbatim,
the exact data contract, the math, a real test fixture you can develop against
**without Revit**, and a phased plan with browser-verifiable acceptance criteria.

Your job: take this tool from its current Phase-1 skeleton to a finished v1 and
self-verify as much as possible in a browser. Work for as long as it takes. The
human (Nathan) will test the Revit-only parts in the morning.

---

## 0. TL;DR of what you're building

The old View Range Helper previewed a plan's view range by exporting Revit PNGs
on every edit (slow, fidgety, doesn't scale). This rebuild replaces that with the
3D Viewer's approach: **export the building once to a small glTF slice, then slice
it live in a web panel.** Revit's four view-range planes (Top, Cut, Bottom, View
Depth) map one-to-one onto GPU clipping planes, so:

- **Plan** = top-down orthographic camera, model clipped between the Bottom and
  Top planes. Re-slices instantly.
- **Section** = orthographic cut along a draggable section line, with the four
  planes drawn as draggable horizontal bars.
- **Apply** sends the four plane elevations back to Revit, which writes the view
  range with the same proven API the old tool used.

Everything interactive lives in the web page. Python is a thin host: it exports
the slice, sends metadata (levels, current view range, template state), and on
Apply converts elevations back to (level, offset) and writes them in a
transaction.

---

## 1. Hard constraints (do not violate)

1. **Two runtimes.** `script.py` and the `lib/` modules run under **IronPython
   2.7** inside Revit. Keep them **Python-2-compatible** (no f-strings, no
   `pathlib`, `.format()` only). The web page runs in **WebView2 (Edge/Chromium)**.
2. **You cannot run Revit.** There is no Revit in your environment. You CANNOT
   execute `script.py`, `export_region`, or any view-range read/write. Do not
   pretend to test them. They are validated three ways: (a) you reuse the
   **verbatim proven functions** in this spec, (b) the CPython integrity tests
   keep them parseable, (c) Nathan tests them live in the morning. Flag every
   Revit-only assumption in your final report.
3. **You CAN fully test the web app** in a browser against the included sample
   dataset (Section 8). Use the `preview_*` tools to self-verify: render, drag,
   clip, editor sync, validation. This is where most of your verification effort
   goes.
4. **Integrity tests must stay green:** `python -m unittest discover -s tests -p
   "test_*.py"` (run from repo root; the launcher is `python`, not `py`, in this
   environment). 303 tests today. They check: every `script.py` parses, every
   `.xaml` is valid XML, no merge markers, telemetry is wired, expected
   pushbuttons exist.
5. **Telemetry stays wired.** `script.py` must keep `import dbhms_telemetry` and
   the `with dbhms_telemetry.session(__title__, script_path=__file__):` wrapper.
6. **UI conventions.** The WPF window follows `CLAUDE.md`'s dbHMS design system
   (slate `#2D3748` header with the `db | HMS` wordmark, `#F7FAFC` surface). The
   in-page UI should echo the same palette (tokens in Section 5.6) so the web
   panel looks like the rest of the firm's tools.
7. **Writing style:** no em dashes anywhere in prose or comments. Keep it tight.
8. **Edit in place** in the working tree at the repo root. pyRevit loads directly
   from `src/extensions/...`; there is no separate deploy step. Do not create a
   parallel "v2" tool — Nathan chose to rebuild in place.
9. **Shared export lib.** This tool imports `lib/clash_export` (the 3D Viewer's
   export pipeline), which is a cross-panel use of that lib. That is intentional
   and already in motion; document it as a `lib/` exception in `CLAUDE.md` when
   you finish (see Section 11).

---

## 2. Current state (what Phase 1 already delivered)

Files that already exist and work (Phase 1 was validated against the integrity
tests; the Revit path is pending Nathan's live test):

- `lib/clash_export/custom_export.py` — added `export_region(doc, out_path,
  src_view, lod=3)` and helpers `_make_region_view`, `shell_category_names`,
  `_model_z_extent_ft`. Builds a throwaway 3D view section-boxed to the active
  plan's crop footprint x building height and exports a low-LOD `.glb` of ALL
  model categories in that footprint (annotation/datum dropped at the element
  level). The web tool controls per-category visibility; `shell_category_names`
  lists the arch/structural categories the page renders with the clay/poche look
  (everything else in flat Revit colour). **Only added functions; the 3D
  Viewer's `export_view` is untouched.**
- `View Range Helper.pushbutton/script.py` — gates to a plan view, runs
  `export_region`, hosts WebView2 with its own data root
  (`%LOCALAPPDATA%/dbHMS/ViewRange`) and virtual host `dbhms.viewrange`. Uses a
  `ready` handshake before pushing the model. Has `_load_model`, `_post`,
  `_on_web_message` (handles `diag:` and `ready`).
- `View Range Helper.pushbutton/ViewRangeHelperForm.xaml` — dbHMS-chromed host
  window: header bar + left placeholder card + right viewport `Border`
  (`x:Name="brd_viewport"`). **You will simplify this** (Section 6.1): drop the
  left card, let the viewport fill, because the editor moves into the page.
- `View Range Helper.pushbutton/web/viewrange.html` — Phase-1 page: loads the
  `.glb` from `url:`/`b64:` messages, frames it top-down (orthographic), clean
  shaded lighting, posts `ready`. **You will rewrite this into the full app.**
- `View Range Helper.pushbutton/web/lib/three/...` — vendored three.js (r-ish
  with addons: GLTFLoader, OrbitControls, postprocessing). Reuse as-is.
- `View Range Helper.pushbutton/web/sample/sample_building.glb` +
  `sample_meta.json` — **your test fixture** (Section 8).
- `tools/vr_fixtures/make_sample_glb.py` — regenerates the fixture.

---

## 3. Architecture

```
┌─ Revit (IronPython 2.7) ───────────────┐        ┌─ WebView2 (Chromium) ─────────┐
│ script.py                              │        │ web/viewrange.html + vr/app.js │
│  • gate to plan view                   │        │  • plan view (top-down ortho)  │
│  • export_region -> small .glb         │        │  • section view (ortho cut)    │
│  • read levels + view range + template │  msgs  │  • 4-plane editor + validation │
│  • build meta -> post "meta:{json}"    │ <════> │  • clipping planes = VR planes │
│  • post "url:<glb>" / "b64:<data>"     │        │  • drag planes / section line  │
│  • on "apply:{json}" -> write VR       │        │  • Apply/Revert/Detach buttons │
│  • on "detach"/"edittemplate" -> Revit │        │  • DEV FALLBACK: load sample/  │
└────────────────────────────────────────┘        └────────────────────────────────┘
```

**Single source of truth for view-range state = the JS app.** Python reads the
initial state, sends it once as `meta`, and only acts again on explicit commands
(`apply`, `detach`, `edittemplate`). This keeps the WPF side tiny and makes the
whole editor testable in a browser.

### 3.1 Message bridge contract

WebView2 strings, `cmd:payload` form. Receive in the page via
`window.chrome.webview.addEventListener('message', e => ...)`; send via
`window.chrome.webview.postMessage(string)`. On the Python side, send via
`CoreWebView2.PostWebMessageAsString(...)` and receive in `_on_web_message`.

**Python → Web**

| Message | Payload | Meaning |
|---|---|---|
| `meta:` | JSON (Section 4) | levels, current view range, view info, template state. Send once after `ready`, and again after detach/edit-template. |
| `url:` | `https://dbhms.viewrange/models/<file>?v=N` | load the glb over the vhost (preferred; handles big files). |
| `b64:` | base64 of the glb | fallback when vhost unavailable (<25 MB). |
| `applied:` | JSON `{"ok":true}` or `{"ok":false,"err":"..."}` | result of an Apply. |

**Web → Python**

| Message | Payload | Meaning |
|---|---|---|
| `ready` | (none) | page wired + three.js imported; safe to push meta + model. |
| `apply:` | JSON `{"planes":{"top":{...},...}}` (Section 4.2) | write these planes to Revit. |
| `detach` | (none) | detach this view's view range from its template. |
| `edittemplate` | (none) | switch the write target to the template itself. |
| `diag:` | text | log line (already handled). |

Order independence: the page must apply `meta` and the loaded model whenever
**both** have arrived, regardless of which came first.

---

## 4. The data contract (meta + apply)

### 4.1 `meta` schema (Python → Web)

This is exactly what `web/sample/sample_meta.json` contains. Build the same dict
in Python. Field reference:

```jsonc
{
  "schema": "dbhms.viewrange.meta/1",
  "units": "meters",
  "ft_to_m": 0.3048,
  "offset_ft": [30.0, 20.0, 18.0],     // export centering offset (Revit feet). Y-up Z is offset_ft[2].
  "model_z_extent_ft": [-15.0, 66.0],  // section vertical guide range
  "view": {
    "name": "L2 - Office  Floor Plan",
    "view_type": "FloorPlan",          // FloorPlan | CeilingPlan | AreaPlan | EngineeringPlan
    "is_ceiling_plan": false,
    "associated_level_id": 102,        // the view's GenLevel id (anchor for sentinels)
    "associated_level_name": "L2 - Office",
    "crop_ft": {"min_x":0,"max_x":60,"min_y":0,"max_y":40}  // Revit-feet crop footprint
  },
  "levels": [                          // ALL levels, sorted by elevation ascending
    {"id":101,"name":"L1 - Ground","elev_ft":0.0},
    {"id":102,"name":"L2 - Office","elev_ft":12.0},
    {"id":103,"name":"L3 - Office","elev_ft":24.0},
    {"id":104,"name":"Roof","elev_ft":36.0}
  ],
  "view_range": {                      // current state of the 4 planes
    "top": {"level_id":102,"level_name":"L2 - Office","offset_ft":7.5,"abs_ft":19.5,"sentinel":null},
    "cut": {"level_id":102,"level_name":"L2 - Office","offset_ft":4.0,"abs_ft":16.0,"sentinel":null},
    "bot": {"level_id":102,"level_name":"L2 - Office","offset_ft":0.0,"abs_ft":12.0,"sentinel":null},
    "vd":  {"level_id":102,"level_name":"L2 - Office","offset_ft":0.0,"abs_ft":12.0,"sentinel":null}
  },
  "sentinels": {                       // which extra options each plane's level picker exposes
    "top": {"above":true, "below":false,"unlimited":false},
    "cut": {"above":false,"below":false,"unlimited":false},
    "bot": {"above":false,"below":true, "unlimited":false},
    "vd":  {"above":false,"below":true, "unlimited":true}
  },
  "sentinel_ids": {"above":-3,"below":-2,"unlimited":-1},
  "disabled_planes": [],               // ["bot"] for a ceiling plan
  "template_lock": {"locked":false,"template_name":null},
  "snap": {"enabled":false,"distance_ft":0.5}
}
```

**`abs_ft` is pre-resolved by Python** using `absolute_z_for_plane` (Section 7),
so the page never has to do sentinel math to *display* a plane. The page derives
each plane's reference elevation as `ref_ft = abs_ft - offset_ft`; on drag it
keeps the level and sets `offset = new_abs_ft - ref_ft`. `sentinel` is `null`,
`"above"`, `"below"`, or `"unlimited"` (the page shows the sentinel name in the
level dropdown; `"unlimited"` planes have no finite elevation — draw at the far
frustum edge and label "Unlimited", non-draggable).

### 4.2 `apply` payload (Web → Python)

```jsonc
{
  "planes": {
    "top": {"level_id":102,"offset_ft":7.5,"sentinel":null},
    "cut": {"level_id":102,"offset_ft":4.0,"sentinel":null},
    "bot": {"level_id":102,"offset_ft":0.0,"sentinel":null},
    "vd":  {"level_id":102,"offset_ft":0.0,"sentinel":null}
  }
}
```

`level_id` is the integer the plane is bound to: a real positive level id, or a
sentinel (`-3`/`-2`/`-1`). Python skips `disabled_planes` (RCP Bottom) on write.

---

## 5. The web app (the bulk of the work)

Rewrite `web/viewrange.html` into the full app. Put the application logic in a
single ES module `web/vr/app.js` imported by the page (keep the three.js
importmap in the HTML). One module file keeps integration simple; do not
over-split.

### 5.1 Units & coordinate system (get this exactly right)

The glb is **Y-up, meters, centered on `offset_ft`**. The export maps Revit
coords like this (from `lib/clash_export/custom_export.py`):

```
glTF_X =  (revit_X - offset_ft[0]) * ft_to_m
glTF_Y =  (revit_Z - offset_ft[2]) * ft_to_m      // UP = Revit Z
glTF_Z = -(revit_Y - offset_ft[1]) * ft_to_m      // Revit North (+Y) -> glTF -Z
```

Therefore, to place a horizontal plane at Revit **absolute elevation `E` (feet)**:

```
glTF_world_Y = (E - offset_ft[2]) * ft_to_m
E = offset_ft[2] + glTF_world_Y / ft_to_m
```

Use **Box3 of the loaded model** for horizontal spatial extents (section-line
default span, level-guide width, framing). Use **meta** for elevations, levels,
view range, units, template state. Keep one `feetToY(E)` / `yToFeet(Y)` helper.

North maps to glTF `-Z`; for the plan, set `camera.up = (0,0,-1)` so north points
up on screen (Phase 1 already does this). Do not try to honor project-vs-true
north in v1 — flag it for Nathan if it looks rotated.

### 5.2 Two-view rendering (one renderer, scissor split)

Use **one** `WebGLRenderer` + **one** `Scene` containing the model. Two
orthographic cameras. In the render loop, draw twice with viewport/scissor:

```
renderer.setScissorTest(true);
// PLAN (left half)
renderer.clippingPlanes = planClips;
renderer.setViewport(0,0,wL,h); renderer.setScissor(0,0,wL,h);
renderer.render(scene, camPlan);
// SECTION (right half)
renderer.clippingPlanes = sectionClips;
renderer.setViewport(wL,0,wR,h); renderer.setScissor(wL,0,wR,h);
renderer.render(scene, camSection);
```

`renderer.clippingPlanes` is global GL state; changing it between the two
`render()` calls gives each view its own clip set in the same frame. (Do NOT use
per-material clipping; global is simpler here.) Split 50/50 to start; a draggable
divider is a nice-to-have, not required.

### 5.3 Clipping planes = the view-range planes (the core math)

`THREE.Plane(normal, constant)` keeps fragments where `normal·p + constant >= 0`.

**Plan view** — keep geometry between Bottom and Top, looking straight down:

```
const topY = feetToY(state.top.abs_ft);
const botY = feetToY(state.bot.abs_ft);
planClips = [
  new THREE.Plane(new THREE.Vector3(0,-1,0),  topY),   // keep y <= topY
  new THREE.Plane(new THREE.Vector3(0, 1,0), -botY),   // keep y >= botY
];
```

(View Depth and the Cut plane do not clip the plan in v1. The plan is the slice
between Bottom and Top. You may optionally fade geometry above the Cut plane to
hint at "beyond cut", but it is not required for v1.)

**Section view** — keep a thin slab around the section line, full height. Let the
section line have endpoints `A`, `B` in glTF world (X/Z plane, any Y). Horizontal
direction `d = normalize(B - A)` (zero the Y). The cut plane is vertical
containing the line; its horizontal normal `n = normalize(cross(d, up))` where
`up=(0,1,0)`. Slab half-depth `hd` (meters, e.g. `0.6` = ~2 ft):

```
const c = n.dot(A);                  // signed distance of the cut plane from origin
sectionClips = [
  new THREE.Plane(n.clone(),          -(c - hd)),   // keep n·p >= c - hd
  new THREE.Plane(n.clone().negate(),  (c + hd)),   // keep n·p <= c + hd
];
```

The **plane bars** (Top/Cut/Bottom/Vd) are NOT clips in the section; they are
overlay lines drawn at `feetToY(abs_ft)` (Section 5.4). The section shows the
whole building height so the user sees the planes relative to all levels.

**Section camera.** Orthographic, looking along `+n` (or `-n` when flipped),
`up=(0,1,0)`. Width = section line length, height = building height (+pad). Frame
it from the model Box3 projected onto the (d, up) basis. Position the camera back
along `-lookDir` from the cut-plane center by more than the model depth; set
`near/far` to bracket the slab. Since it is orthographic, exact distance does not
affect size, only near/far clipping.

> **Known v1 limitation (decided):** clipped cut faces are hollow (open shells),
> so you see into wall cavities. Materials are `DoubleSide` so you see the back
> interior, not a void. Solid-cap poché is **deferred to a later pass** — do not
> attempt stencil capping in v1. A clean shaded hollow slice is the v1 target.

### 5.4 Overlays (SVG/HTML synced to the cameras each frame)

Render the model in WebGL; draw all UI lines/handles/labels as an **SVG overlay**
(or absolutely-positioned HTML) on top of each scissor region, recomputed every
frame by projecting world points through the matching camera. This mirrors the
old tool's "bitmap underneath, vector overlay on top" model — now it is "WebGL
underneath, SVG on top." Project with `vec.clone().project(camera)` then map NDC
to the region's pixel rect.

**Plan overlay** (left region):
- the **section line** A→B with draggable endpoint handles and a body you can
  drag to translate; a small arrow showing the look direction (flip on click).
- optional far-clip / view-depth handle (not required v1).

**Section overlay** (right region):
- four horizontal **plane bars** at `feetToY(abs_ft)`, colored per Section 5.6,
  each with a draggable round handle at the right edge and a label
  (`Top  19'-6"`). Dragging updates that plane's offset (Section 5.5).
- faint **level guide** lines + names at each level elevation from `meta.levels`.
- the "unlimited" View-Depth plane (if `sentinel=="unlimited"`) drawn pinned to
  the bottom frustum edge, labeled "Unlimited", non-draggable.

Hit-testing: put the handles in the SVG as real elements and use pointer events.
That is far easier than raycasting and gives crisp UI.

### 5.5 The four-plane editor + drag math

A compact panel (HTML overlay, top-left of the page or above the section) lists
Top / Cut / Bottom / View Depth. Each row: color swatch, **level dropdown**
(populated from `meta.levels` plus the sentinel options allowed for that plane
per `meta.sentinels`), an **offset input** (feet-inches text), and a live
**abs elevation** readout.

State per plane (JS): `{ level_id, offset_ft, sentinel, ref_ft }` where
`ref_ft = abs_ft - offset_ft` at load. Derived `abs_ft = ref_ft + offset_ft`.

- **Typing an offset** → parse (Section 9, port `parse_offset`) → snap if enabled
  → set `offset_ft` → recompute `abs_ft` → update clips + overlays + validation.
- **Dragging a plane bar** in the section → convert handle Y (pixels) to world Y
  to `abs_ft` via `yToFeet` → `offset_ft = abs_ft - ref_ft` → snap → update editor
  + clips + validation. **Keep `level_id`/`sentinel` unchanged on drag** (this is
  exactly the old tool's behavior — no nearest-level snapping ever).
- **Picking a different level** in the dropdown → set `level_id` and recompute
  `ref_ft` from that level's `elev_ft` (or the resolved sentinel base, Section
  7.3); keep `offset_ft`; recompute `abs_ft`.
- Disabled planes (`meta.disabled_planes`, i.e. RCP Bottom) render greyed and are
  not draggable/editable.

### 5.6 Look & palette

Match the firm tokens. Plane colors are fixed (also used by the old tool and the
icon):

| Plane | Hex |
|---|---|
| Top | `#38A169` green |
| Cut Plane | `#E53E3E` red |
| Bottom | `#3182CE` blue |
| View Depth | `#805AD5` purple |
| Level guide | `#A0AEC0` gray |

UI chrome tokens: surface `#F7FAFC`, card `#FFFFFF`, card border `#E2E8F0`, header
`#2D3748`, primary `#2B6CB0`, body text `#2D3748`, helper `#718096`, wordmark
accent `#00BFFF`. Section background a soft light slate so the shaded model reads
clean (Phase 1 uses `#e9eef3`).

Clean shaded lighting only: hemisphere fill + 1-2 directional lights, **no env
map, no textures, no AO** in v1 (that is the "keep it reasonable" decision).

### 5.7 Apply / Revert / template banner (in-page)

- **Apply to Revit** button → build the apply payload (Section 4.2) → `post('apply:'+json)`.
  Disable the button + show a spinner until `applied:` returns. On `{ok:true}`
  set the current state as the new baseline and toast "View range applied." On
  `{ok:false}` toast the error.
- **Revert** → reset state to the original `meta.view_range`, re-render. Pure JS.
- **Template lock.** If `meta.template_lock.locked`, show a warn banner across the
  top (tokens: bg `#FFFBEA`, border `#D69E2E`, text `#744210`): "View range is
  controlled by template '<name>'." with two buttons — **Detach view range**
  (`post('detach')`) and **Edit template instead** (`post('edittemplate')`). Grey
  out the editor + Apply while locked. After Python does the Revit work it sends a
  fresh `meta`; re-render from it (unlocked, or showing "[Template] <name>"
  context when editing the template).

### 5.8 DEV FALLBACK MODE (this is how you test without Revit)

When `!(window.chrome && window.chrome.webview)` (i.e. a plain browser, not
WebView2), the page must **auto-load the sample fixture**:

```
fetch('./sample/sample_meta.json')  -> apply as meta
loader.load('./sample/sample_building.glb', ...) -> the model
```

In dev mode, **Apply** does not reach Revit — just toast "Apply (dev): " + the
JSON payload and log it, so you can verify the payload is correct. Detach/edit-
template in dev mode: toast a stub. This single fallback makes the entire UI
exercisable in a browser. Keep it for good (it is also a handy demo mode).

Serve over HTTP (fetch + module scripts are blocked on `file://`). From the
pushbutton's `web/` folder:

```
python -m http.server 8123
# open http://localhost:8123/viewrange.html
```

Use the `preview_*` tools to start a server, screenshot, snapshot the DOM, click
and drag handles via `preview_eval`, and confirm the clip updates. Verify:
model renders in both views, planes drag and the section bars move, the plan
re-slices when Top/Bottom change, editor and drag stay in sync, validation banner
appears when you push Top below Cut, Apply payload is correct.

---

## 6. The Python side (Revit-only; reuse verbatim logic)

You cannot run this. Get it right by reusing the proven functions in Section 7
verbatim and wiring them carefully.

### 6.1 Simplify the host window

In `ViewRangeHelperForm.xaml`, drop the left settings card; let `brd_viewport`
fill the body under the header (the editor now lives in the page). Keep the dbHMS
header bar and wordmark. Keep `x:Name="brd_viewport"`. The window can stay
1500x950, resizable.

### 6.2 Build and post `meta`

After the page posts `ready`, send `meta` then the model. Add a `_build_meta(doc,
view)` that uses the Section 7 functions to assemble the Section 4.1 dict, then:

```python
import json
self._post("meta:" + json.dumps(meta))   # IronPython json is fine; keep it ASCII-safe
self._load_model()
```

`offset_ft` and the model_z_extent must match the export. `export_region` already
computes the offset via `_view_offset`; have it **return the offset** (extend the
stats dict with `"offset_ft"`) so `script.py` can put the same numbers in meta.
The plane `abs_ft` values come from `absolute_z_for_plane`. `crop_ft` comes from
the same world-XY crop computation `_make_region_view` already does (factor it out
so both the export and meta use one helper, or recompute in `_build_meta`).

### 6.3 Handle `apply`, `detach`, `edittemplate`

Extend `_on_web_message`:

```python
if msg.startswith("apply:"):
    self._apply_planes(json.loads(msg[len("apply:"):]))
    return
if msg == "detach":
    self._do_detach(); return
if msg == "edittemplate":
    self._do_edit_template(); return
```

`_apply_planes` rebuilds a state dict and calls `write_view_range` (Section 7.4)
with `skip_planes=self._disabled_planes`, then posts `applied:{...}`. Mapping the
incoming integer `level_id` back to an `ElementId`: keep the **originally read**
`PlanViewRange` level ids in a dict; if the incoming int equals the original int
for that plane, reuse the original `ElementId` object (safest for sentinels);
otherwise `make_eid(level_id)` (from `clash_detect._compat`, the repo's
version-safe constructor). `_do_detach` / `_do_edit_template` call the Section 7.5
functions, then rebuild + re-post `meta`.

> Note: `write_view_range`/`read_view_range` work on a **view template** too (a
> template responds to `GetViewRange`/`SetViewRange`), which is how "edit template
> instead" works — you swap the write target from the view to the template.

---

## 7. Proven Revit logic to port verbatim (source of truth)

These are lifted from the old tool (git history). Reuse them as-is. `eid_int`,
`make_eid`, `doc`, `PLANE_KEYS=("top","cut","bot","vd")`, and the `PlanViewPlane`
imports must be present.

### 7.1 Read view range

```python
def read_view_range(view_plan):
    pvr = view_plan.GetViewRange()
    out = {}
    mapping = (
        ("top", PlanViewPlane.TopClipPlane),
        ("cut", PlanViewPlane.CutPlane),
        ("bot", PlanViewPlane.BottomClipPlane),
        ("vd",  PlanViewPlane.ViewDepthPlane),
    )
    for key, plane in mapping:
        try:    lid = pvr.GetLevelId(plane)
        except Exception: lid = ElementId.InvalidElementId
        try:    off = pvr.GetOffset(plane)
        except Exception: off = 0.0
        out[key] = {"level_id": lid, "offset": float(off)}
    return out
```

### 7.2 Levels + helpers

```python
def get_all_levels_sorted(doc):
    lvls = list(FilteredElementCollector(doc).OfClass(Level).WhereElementIsNotElementType())
    return sorted(lvls, key=lambda l: l.Elevation)

def get_associated_level(view_plan):
    try: return view_plan.GenLevel
    except Exception: return None

def get_level_by_id(doc, lvl_id):
    if lvl_id is None or eid_int(lvl_id) <= 0:
        return None
    el = doc.GetElement(lvl_id)
    return el if isinstance(el, Level) else None
```

### 7.3 Plane (level_id, offset) -> absolute elevation, with sentinels

Sentinels: **-3 = Level Above, -2 = Level Below, -1 = Unlimited** (Unlimited
returns `None` = off-canvas). These integers are a heuristic, not a documented
API constant — keep the fallback.

```python
def absolute_z_for_plane(view_plan, level_id, offset_feet, all_levels):
    base = get_associated_level(view_plan)
    if base is None:
        return None
    base_z = base.Elevation
    iid = eid_int(level_id)
    if iid <= 0:
        if iid == -1:
            return None  # Unlimited -> off-canvas
        sorted_lvls = sorted(all_levels, key=lambda l: l.Elevation)
        idx = None
        for i, l in enumerate(sorted_lvls):
            if l.Id == base.Id:
                idx = i; break
        if idx is None:
            return base_z + (offset_feet or 0.0)
        if iid == -3 and idx + 1 < len(sorted_lvls):     # Level Above
            return sorted_lvls[idx + 1].Elevation + (offset_feet or 0.0)
        if iid == -2 and idx - 1 >= 0:                   # Level Below
            return sorted_lvls[idx - 1].Elevation + (offset_feet or 0.0)
        return base_z + (offset_feet or 0.0)
    lvl = get_level_by_id(doc, level_id)
    if lvl is None:
        return base_z + (offset_feet or 0.0)
    return lvl.Elevation + (offset_feet or 0.0)
```

For meta, set each plane's `sentinel` from its `level_id` int (`-3`->above,
`-2`->below, `-1`->unlimited, else null) and `level_name` to the resolved level
name or the sentinel glyph (`<Above>`/`<Below>`/`<Unlimited>`).

### 7.4 Write view range (skip RCP Bottom)

```python
def write_view_range(view_plan, state, skip_planes=None):
    skip = skip_planes or set()
    pvr = view_plan.GetViewRange()
    mapping = (
        ("top", PlanViewPlane.TopClipPlane),
        ("cut", PlanViewPlane.CutPlane),
        ("bot", PlanViewPlane.BottomClipPlane),
        ("vd",  PlanViewPlane.ViewDepthPlane),
    )
    for key, plane in mapping:
        if key in skip:
            continue
        s = state[key]
        try: pvr.SetLevelId(plane, s["level_id"])
        except Exception: pass
        try: pvr.SetOffset(plane, float(s["offset"]))
        except Exception: pass
    t = Transaction(doc, "Edit View Range")
    try:
        t.Start(); view_plan.SetViewRange(pvr); t.Commit()
        return True, ""
    except Exception as ex:
        try: t.RollBack()
        except Exception: pass
        return False, str(ex)
```

`state[key]["level_id"]` must be an `ElementId`. See Section 6.3 for mapping the
incoming int back to an `ElementId` (reuse original for sentinels; `make_eid`
otherwise).

### 7.5 Ceiling-plan disabled planes + template lock/detach/edit

```python
def get_disabled_planes(view_plan):
    try: vt_str = str(view_plan.ViewType)
    except Exception: return set()
    return {"bot"} if vt_str == "CeilingPlan" else set()

def _find_view_range_param_id(template):
    try: params = template.Parameters
    except Exception: return None
    target_names = ("view range", "plan view range")
    for p in params:
        try:
            d = p.Definition
            if d is None: continue
            if (d.Name or "").strip().lower() in target_names:
                return p.Id
        except Exception: continue
    return None

def is_view_range_template_locked(view_plan):
    try: tpl_id = view_plan.ViewTemplateId
    except Exception: return False, None
    if tpl_id is None or eid_int(tpl_id) == -1:
        return False, None
    tpl = doc.GetElement(tpl_id)
    if tpl is None: return False, None
    vr_pid = _find_view_range_param_id(tpl)
    if vr_pid is None: return False, tpl
    try: non_ctrl = list(tpl.GetNonControlledTemplateParameterIds())
    except Exception: non_ctrl = []
    for eid in non_ctrl:
        if eid_int(eid) == eid_int(vr_pid):
            return False, tpl     # in non-controlled list => NOT locked
    return True, tpl

def detach_view_range_from_template(view_plan):
    # add the View Range param to the template's non-controlled list
    tpl_id = view_plan.ViewTemplateId
    if eid_int(tpl_id) == -1: return False, "View has no template."
    tpl = doc.GetElement(tpl_id)
    if tpl is None: return False, "Template element could not be loaded."
    vr_pid = _find_view_range_param_id(tpl)
    if vr_pid is None: return False, "Could not find a 'View Range' parameter on the template."
    try: non_ctrl = list(tpl.GetNonControlledTemplateParameterIds())
    except Exception: non_ctrl = []
    for eid in non_ctrl:
        if eid_int(eid) == eid_int(vr_pid):
            return True, "Already detached."
    non_ctrl.append(vr_pid)
    from System.Collections.Generic import List as NetList
    eid_list = NetList[ElementId]()
    for eid in non_ctrl: eid_list.Add(eid)
    t = Transaction(doc, "Detach view from template view range")
    try:
        t.Start(); tpl.SetNonControlledTemplateParameterIds(eid_list); t.Commit()
        return True, ""
    except Exception as ex:
        try: t.RollBack()
        except Exception: pass
        return False, str(ex)
```

"Edit template instead" = set the write/read target to `tpl` for the rest of the
session (rebuild meta from `read_view_range(tpl)`; the plan/section preview still
uses the active view for spatial context). Confirm with the user first.

---

## 8. The sample dataset (your test bed)

- `web/sample/sample_building.glb` — a synthetic 3-storey + roof office box (33
  elements, ~400 tris, 44 KB): floor slabs at L1/L2/L3, a roof slab + parapet,
  perimeter + partition walls per storey, four full-height columns. Same node
  `extras` keys (`element_id`, `category`, `level`, `name`) and `asset.extras`
  (`offset_ft`, `ft_to_m`) as the real export.
- `web/sample/sample_meta.json` — the Section 4.1 `meta`, modelled on an L2 floor
  plan (view range references L2). Plane abs elevations: Top 19.5, Cut 16.0,
  Bottom 12.0, Vd 12.0 ft.
- `tools/vr_fixtures/make_sample_glb.py` — regenerates both. Run from repo root:
  `python tools/vr_fixtures/make_sample_glb.py`. It reuses the repo's own
  `lib/clash_export/{mesh,gltf}.py` writer (pure Python, CPython-safe).

Use the fixture for all browser development. The geometry is deliberately blocky
so you can eyeball whether the plan slice and section cut are correct (e.g. cut at
16 ft on L2 should slice through L2's walls; the section should show 3 storeys +
roof + columns).

**To test against REAL geometry later:** Nathan runs the tool in Revit once; it
writes a real glb to `%LOCALAPPDATA%/dbHMS/ViewRange/models/<model>_vr.glb`. Copy
that over `web/sample/sample_building.glb` (and hand-edit `sample_meta.json`
levels/view-range to match) to retest the harness with production data. Document
this in `tools/vr_fixtures/README.md` (create it).

### Variant fixtures worth generating (optional, helps cover edge cases)
If time allows, extend `make_sample_glb.py` to also emit:
- a **ceiling-plan** meta (`is_ceiling_plan:true`, `disabled_planes:["bot"]`,
  view-range with Vd above Cut) to test the greyed-Bottom + flipped validation.
- a **template-locked** meta (`template_lock.locked:true`) to test the banner.
You can switch these via a CLI arg and write `sample_meta_ceiling.json` etc.; have
the dev fallback accept `?fixture=ceiling` to load a variant.

---

## 9. Feet-inch formatting, parsing, snapping, validation (port to JS)

Port these from the old tool. Match behavior exactly so the readouts feel native.

**Format** decimal feet -> `7'-6"` (1/16" precision):
```python
def fmt_feet_in(feet):
    if feet is None: return "-"
    sign = '-' if feet < 0 else ''
    f = abs(feet)
    whole_feet = int(math.floor(f))
    inches = (f - whole_feet) * 12.0
    whole_in = int(round(inches * 16.0)) / 16.0
    if whole_in >= 12.0 - 1e-6:
        whole_feet += 1; whole_in = 0.0
    return "{}{}'-{:g}\"".format(sign, whole_feet, whole_in)
```

**Parse** user text -> decimal feet. Accepts `7.5`, `-3.25`, `7'-6"`, `7' 6`, `6"`.
Try `float(s)` first (decimal feet); else regex feet/inches. Return null on
failure (revert the field to the last valid value).

**Snap** (snap-to-increment only; never snap-to-level): `snap(feet, d) = round(feet/d)*d`
when `d>0` and snapping enabled, else `feet`.

**Validation** (non-blocking warning banner). Floor plan ordering (descending):
`Top >= Cut >= Bottom >= ViewDepth`. Ceiling plan (Bottom locked to Cut): check
`Top >= Cut` and `ViewDepth >= Cut`. Messages, using `fmt_feet_in`:
- Top below Cut: "Top (x) is below Cut Plane (y)." (all view types)
- Floor plan: Cut below Bottom; Bottom below View Depth.
- Ceiling plan: View Depth below Cut ("on a Ceiling Plan, View Depth is normally
  above the cut...").
Compare with a `1e-6` epsilon. Show the banner only when there is at least one
message.

---

## 10. Phased build plan (each phase browser-verifiable in dev mode)

Do these in order; verify each in the browser against the sample before moving on.

**P2 — Plan view + live clipping.** Replace the Phase-1 top-down render with the
two-view scissor split (plan left, section placeholder right). Apply the Plan
clip planes from `meta.view_range`. Acceptance: the plan shows only geometry
between Bottom (12') and Top (19.5'); editing those abs values in a temporary
debug input re-slices live.

**P3 — Section view + plane bars.** Add the section camera + slab clip along a
fixed default section line (mid-building, east-west). Draw the four colored plane
bars + level guides as SVG overlay. Acceptance: section shows 3 storeys + roof +
columns; four bars sit at the right elevations with labels; dragging a bar moves
it and updates its abs readout.

**P4 — Section line on the plan.** Draw the section line on the plan with
draggable endpoints + body translate + flip arrow. The section re-cuts to follow.
Acceptance: moving the line changes what the section shows; rotating/translating
updates the slab.

**P5 — Plane editor + two-way sync + validation.** Build the four-row editor
(level dropdown + offset field + abs readout), wire it to drag and vice-versa,
add snap control and the validation banner. Acceptance: typing an offset moves
the bar and re-slices the plan; dragging updates the field; pushing Top below Cut
shows the warning; RCP fixture greys Bottom.

**P6 — Apply / Revert / template (web side) + Python wiring.** In-page Apply
builds the payload and posts `apply:`; Revert resets; template banner posts
`detach`/`edittemplate`. On the Python side, add `_build_meta`, the `apply`/
`detach`/`edittemplate` handlers, post `meta`, and simplify the XAML. Acceptance
(browser/dev): Apply toasts the exact JSON payload (verify it matches Section
4.2); Revert restores; banner appears for the locked fixture. Acceptance
(Python): integrity tests green; code reviewed against Section 7. Flag for
Nathan's live test.

**P7 — Polish.** dbHMS palette throughout, labels, snap default, keyboard nudge
(optional), empty-model and single-level edge cases, loading states, error
toasts. Tidy `viewrange.html`/`app.js`. Update docs (Section 11).

**Deferred (do NOT do in v1):** solid-cap poché / cut-fill, higher render quality
(AO, env, textures), batch multi-view editing, reusing an existing 3D-Viewer
export, project/true-north rotation, draggable view-split divider.

---

## 11. Documentation + housekeeping (do before finishing)

- `CLAUDE.md`: add a short note that View Range Helper is now a web/glTF tool that
  imports `lib/clash_export` (a documented cross-panel `lib/` use), and that its
  web UI lives in `web/` with a browser dev-fallback mode + sample fixture. Add to
  the "documented exceptions" list.
- `tools/vr_fixtures/README.md`: how to regenerate the fixture and how to swap in
  a real export (Section 8).
- Update the memory file `view_range_web_rebuild.md` (mark phases done) and the
  `clash_3d_viewer_backlog.md` if you touch shared export code.
- Keep `import dbhms_ui` available in `script.py` for any popups (use
  `dbhms_ui.info(...)` for success/info, `forms.alert(..., yes/no)` for
  confirmations, `forms.alert(..., exitscript=True)` for fatal gates).

---

## 12. Definition of done (v1)

1. In a browser (dev fallback, sample fixture): plan + section both render; the
   four planes drag and type-edit with two-way sync; the plan re-slices live from
   Top/Bottom; the section shows the building with correctly-placed plane bars +
   level guides; the section line moves the cut; validation warns on bad ordering;
   the RCP fixture greys Bottom; Apply produces the correct JSON payload; Revert
   works; the template-locked fixture shows the banner. Self-verified with
   `preview_*` screenshots/snapshots.
2. `python -m unittest discover -s tests -p "test_*.py"` is green.
3. `script.py` carries the Section 7 functions, `_build_meta`, and the
   `apply`/`detach`/`edittemplate` handlers; it parses (integrity test) and is
   reviewed against this spec. The XAML is simplified and valid.
4. Docs updated (Section 11).
5. A clear **final report** listing: what is browser-verified, and every
   Revit-only path Nathan must test live in the morning (export speed/size on a
   real model, north orientation, the actual view-range write, template
   detach/edit, ceiling plans). Be honest about what you could not run.

---

## 13. Questions already answered (so you don't have to ask)

- Export scope: the active view's crop footprint only (often a whole level; fine).
  Always its own fresh scoped export; never reuse a 3D-Viewer export.
- Categories: arch + structural shell only; no MEP, no textures.
- What changes: only the active view. If view-range is template-controlled, warn +
  offer detach or edit-template (never touch other views/templates).
- Section finish v1: clean shaded hollow slice. Solid-cap poché + quality deferred.
- One view at a time (no batch in v1).
- Rebuild in place (no parallel v2 button). Old PNG code lives in git history.

If you hit a genuinely new fork not covered here, make the smallest reasonable
decision, implement it, and flag it in your final report rather than stalling.
```
