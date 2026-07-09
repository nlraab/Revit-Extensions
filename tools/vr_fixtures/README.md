# View Range Helper test fixtures

A synthetic building used to develop the View Range Helper web app **without
Revit**. The page's dev-fallback mode (plain browser, no WebView2 host) loads
these so the whole plan + section + editor UI is exercisable in a browser.

## Files

Generated into the tool's web folder so the page can fetch them with relative
paths (`.../View Range Helper.pushbutton/web/sample/`):

- `sample_building.glb` — synthetic 3-storey + roof office box (33 elements,
  ~400 tris). Y-up, meters, centered on an export `offset_ft`, with per-element
  node `extras` (`element_id`, `category`, `level`, `name`) and `asset.extras`
  (`offset_ft`, `ft_to_m`) — exactly the conventions the real Revit export writes.
- `sample_meta.json` — default L2 **floor plan** meta (the `meta:` message the
  Python tool posts to the page): levels, current four-plane view range, view
  info, sentinels, template state. Schema in the **View range meta & Apply schema** section below.
- `sample_meta_ceiling.json` — a **reflected ceiling plan** variant
  (`is_ceiling_plan: true`, `disabled_planes: ["bot"]`, View Depth above Cut).
  Exercises the greyed-out Bottom plane and the RCP-specific validation message.
- `sample_meta_locked.json` — a **template-locked** variant
  (`template_lock.locked: true`). Exercises the warn banner with Detach /
  Edit-template actions.

## Regenerate

From the repo root (the launcher is `python` in this environment):

```
python tools/vr_fixtures/make_sample_glb.py
```

It reuses the repo's own pure-Python glTF writer (`lib/clash_export/mesh.py` +
`gltf.py`), so the fixture stays in lockstep with the real export format.

## Serve / test in a browser

`fetch()` and ES modules are blocked on `file://`, so serve over HTTP. From the
tool's `web/` folder:

```
cd "src/extensions/dbHMS Extensions.extension/dbHMS Tools.tab/BIM Tools.panel/View Range Helper.pushbutton/web"
python -m http.server 8123
```

Then open:

- `http://localhost:8123/viewrange.html` — default floor plan
- `http://localhost:8123/viewrange.html?fixture=ceiling` — ceiling plan
- `http://localhost:8123/viewrange.html?fixture=locked` — template-locked

In dev mode, **Apply** does not reach Revit; it toasts and logs the exact JSON
payload so you can confirm it matches the Apply payload schema below (and `_apply_planes` in the tool's `script.py`). Detach /
Edit-template toast a stub.

## Coordinate convention (keep in sync with the real export)

`lib/clash_export/custom_export.py` `OnPolymesh` maps Revit feet to glTF meters:

```
glTF_X =  (revit_X - offset_ft[0]) * 0.3048
glTF_Y =  (revit_Z - offset_ft[2]) * 0.3048      # UP comes from Revit Z
glTF_Z = -(revit_Y - offset_ft[1]) * 0.3048      # Revit North (+Y) -> glTF -Z
```

So a Revit absolute elevation `E` (feet) sits at `glTF_world_Y =
(E - offset_ft[2]) * 0.3048`. The app's `feetToY` / `yToFeet` helpers are the
single source of this conversion.

## Swap in REAL geometry

After running the tool once in Revit, a real export lands at
`%LOCALAPPDATA%\dbHMS\ViewRange\models\<model>_vr.glb`. Copy it over
`web/sample/sample_building.glb` and hand-edit `sample_meta.json` (levels,
view_range, offset_ft, crop_ft) to match that model, to retest the harness with
production data.

## View range meta & Apply schema

The `meta:` message (Python → page, built by `_build_meta` in the tool's `script.py`) and the `apply:` payload (page → Python, consumed by `_apply_planes`). This is the authoritative contract; `sample_meta*.json` are instances of it.

**`meta` (`schema: "dbhms.viewrange.meta/1"`)** — key fields:

- `units: "meters"`, `ft_to_m` (0.3048), `offset_ft: [x,y,z]` (export centering, Revit feet; Y-up world Z is `offset_ft[2]`), `model_z_extent_ft: [zmin,zmax]`.
- `display_datum_ft` — subtract from any absolute internal elevation to display height above the view's floor (associated level reads 0). Positioning stays in internal feet; display-only.
- `view` — `{ name, view_type ("FloorPlan"|"CeilingPlan"|"AreaPlan"|"EngineeringPlan"), is_ceiling_plan, associated_level_id, associated_level_name, crop_ft:{min_x,min_y,max_x,max_y}, up_dir:[x,y] }` (plan screen-up in world XY, for rotated/true-north buildings).
- `levels` — all levels sorted ascending: `[{ id, name, elev_ft }]`.
- `view_range` — the four planes, keyed `top`/`cut`/`bot`/`vd`, each `{ level_id (int), level_name, offset_ft, abs_ft (pre-resolved absolute elevation, or null for Unlimited), sentinel ("above"|"below"|"associated"|"unlimited"|null) }`. Page derives `ref_ft = abs_ft - offset_ft`.
- `sentinels` — per plane, which extra picker options are allowed: `{ above, below, unlimited }` bools.
- `sentinel_ids` — **`{ "unlimited": -1, "above": -2, "associated": -3, "below": -4 }`**. These are the real ids Revit's `GetLevelId`/`SetLevelId` use for non-level-pinned planes; they round-trip byte-for-byte on Apply.
- `disabled_planes` — e.g. `["bot"]` on a ceiling plan (RCP Bottom is locked to Cut; skipped on write).
- `hidden_categories`, `hidden_worksets` (start toggled OFF in the web tree), `shell_categories` (rendered with the clay/poché look), `host_model` (doc title).
- `template_mode` (bool; true when editing the view's template) and `template_lock: { locked, template_name }`.
- `snap: { enabled: true, distance_ft: 0.5 }`.

**`apply` payload (page → Python):**

```jsonc
{ "planes": {
    "top": { "level_id": 102, "offset_ft": 7.5 },
    "cut": { "level_id": 102, "offset_ft": 4.0 },
    "bot": { "level_id": 102, "offset_ft": 0.0 },
    "vd":  { "level_id": -1,  "offset_ft": 0.0 }   // sentinel example: Unlimited
} }
```

Each plane sends `level_id` (a real positive level id, or a sentinel `-1`/`-2`/`-3`/`-4`) and `offset_ft`. Python maps unchanged planes back to the original `ElementId` (exact sentinel round-trip), else builds one via `make_eid`, and skips `disabled_planes` on write.

Note: the `apply` plane object uses key **`offset_ft`** (matching `meta`), not `offset`.
