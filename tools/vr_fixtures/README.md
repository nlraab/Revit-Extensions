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
  info, sentinels, template state. Schema in `REBUILD_SPEC.md` Section 4.1.
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
payload so you can confirm it matches `REBUILD_SPEC.md` Section 4.2. Detach /
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
