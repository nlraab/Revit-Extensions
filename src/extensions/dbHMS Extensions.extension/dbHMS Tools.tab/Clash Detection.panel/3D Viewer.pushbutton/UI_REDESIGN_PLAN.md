# 3D Viewer UI redesign — implementation plan

Status: **approved design, implementation not started.** Written 2026-07-11 from the locked
design (mockups: https://claude.ai/code/artifact/caf10b9d-536a-40b9-9df0-273b7fc5d8c6) after
per-phase planning against the live code and an adversarial constraint review.

## The locked design (do not relitigate)

One page (`web/viewer3.html`), three modes, all UI in-page; the WPF window becomes bare chrome
hosting the WebView2 control. Firm navy dotted header + 3px orange baseline + real logo
(coord.html `#hdr` recipe), labeled tabs **Explore / Clashes / Markups**, one docked-collapsible
300px left panel reskinned per mode, full-bleed canvas + floating bottom toolbar, selection-scoped
380px right inspector, bottom filmstrip in Clashes+Markups, **Present** mode (chrome gone, capsule
+ caption, keyboard driving). Markups are **QA/QC sessions**: name + date + the markups from one
review walk; engineers open a session later, flip through, set status; sessions are
created/renamed/deleted from an All sessions list.

**Hard rules (Nathan):**
1. **The render pipeline is untouched.** No ghosting, no pair recoloring, no scene overrides.
   Clash focus = fly-to + the existing yellow selection highlight + info card. Every new feature
   is chrome, a camera move, or an overlay (DOM canvas / existing highlight-box idiom).
2. Ink never lives in 3D space; it is locked to a markup's saved viewpoint.
3. Deep visual clash review (red/blue pair, ghosted context) stays in the Clash Detection app.

**Two sanctioned engine-adjacent edits** (each is one line extending an existing mechanism, and
each needs explicit sign-off in review): (a) a `hiddenLevel` clause added to
`recomputeVisibility()`'s predicate for level visibility (extends the existing `_vis` path);
(b) adding the clash pair's two `Box3Helper` clones to `pickAtMerged`'s hidden-overlay list
(exactly what `hlBox` does today). Nothing else touches engine code.

## Phase order

| # | Phase | Size | Ships |
|---|-------|------|-------|
| 1 | Shell + Explore parity | L | New in-page UI, WPF sidebar retires, Explore fully works |
| 2 | Clashes mode | L | Flip-through synced to the rebuilt clash DB, pins, write-back |
| 3 | Present mode | M | The client-tour surface |
| 4 | Markups + QA/QC sessions | XL | The new markup system |
| 5 | Share parity + cleanup | M-L | Standalone share file becomes the new shell, read-only |
| 6 | Measure tool (approved: Measure only) | S-M | Two-pick distance measure; Cut/Box dropped |
| D | (parallel track) corner-render DPI bug | — | Diagnosis + fix for the 150% hybrid-GPU laptop |

Clashes lands **before** Present so the clash workflow is only absent from this tool for one
phase (during Phase 1, clash review lives in the Clash Detection app; the WPF clash sidebar
retires with the sidebar). Present's deck registry moves with it into Phase 3.

Lib batching (pyRevit reload discipline): Phases 1-3 touch **no lib/** (page + script.py refresh
each launch, tight iteration). Phase 4 lands its lib work as one Batch A drop first. Phase 5 has
one lib drop (`bundle.py`). Never edit lib/ mid-phase without planning a Revit restart.

---

## Frozen bridge vocabulary

The bridge stays plain `prefix:` strings over `postMessage`. This table is the single owner of
every new message; phases must not redefine shapes. **Never change:** `filters:` (exact
`{models:[{name,categories,worksets}]}` shape; the host uses it as the load-complete trigger),
`cam:`, `diag:`, `minimize`, `url:`/`b64:`, `kd:`/`ku:` (vocabulary may grow, prefix may not),
`viewpose:`, `flytopoint:`, `vis:`, `quality:/sun:/edges:/perf:/ground:/minimap:`. Legacy
`clashes:`/`showmarkers:` handlers stay in the page for the share overlay; the host simply stops
emitting them in hosted mode.

| Message | Dir | Payload | Phase |
|---|---|---|---|
| `ready` | page→host | none; host replies `prefs:`, `views:`, `vispresets:`, `project:` | 1 |
| `export:` | page→host | `{"picksubcats":0\|1}` | 1 |
| `loadlast` / `popout` / `fullscreen` | page→host | none | 1 |
| `share:` | page→host | `{"images":"auto"\|"none"}` (Phase 5 shape, defined now) | 1 |
| `saveviews:` | page→host | `{"viewpoints":[{id,name,pos,yaw,pitch,thumb?}],"home":<id\|null>}` (page authoritative, host persists) | 1 |
| `views:` | host→page | same schema as `saveviews:` | 1 |
| `vispresets:` | both | `{"presets":[{name,hiddenModels,hiddenCats,hiddenWs,hiddenLevels}]}` | 1 |
| `prefs:` | both | `{quality,edges,ground,perf,minimap,speed,look,gamepad,panelCollapsed}` | 1 |
| `project:` | host→page | `{"title":doc.Title}` | 1 |
| `stats:` / `exportstate:` | host→page | export stats JSON / `started\|done\|fail:<line>` | 1 |
| `debug:` | host→page | `1` shows the diag ribbon | 1 |
| `clashdata:` | host→page | `{"v":1,"ok":bool,"msg":str\|null,"rows":[full row…]}` | 2 |
| `clashupd:` | host→page | `[row]` single-row reconcile by id | 2 |
| `clashop:` | page→host | `{op_id, clash_id, op:status\|assign\|deadline\|comment, …}` | 2 |
| `clashopdone:` | host→page | `{ok, error, clash_id, op_id, busy?}` (busy-nack + page requeue) | 2 |
| `showinrevit:` | page→host | `{"keys":[fed keys],"mid":[x,y,z]}` — the ONE select-and-zoom message; markups reuse it with a single key | 2 |
| `refreshclashes` | page→host | bare | 2 |
| `present:1/0` | page→host | none; host maximizes/restores | 3 |
| `presentend:1` | host→page | host had to abort present (popout closed / tool closing) | 3 |
| `markups:` | host→page | `{"user":name,"sessions":[…full markups.json…]}` | 4 |
| `sessionop:` / `sessionopdone:` | page→host / back | create/rename/**archive** + ack (busy-nack) | 4 |
| `markupop:` / `markupopdone:` | page→host / back | add/status/assign/due/note/comment/delete + ack | 4 |
| `markupshot:<sid>:<guid>:<dataurl>` | page→host | JPEG data-URL, host atomic-writes | 4 |
| `mkreport:` (+`mkreportbusy/done/fail:`) | page→host / back | `{"session_id"}` → PDF via hidden WebView2 print | 4 |
| `sharedone:` / `sharefail:` | host→page | `{path,mb,rows,thumbs,skipped_thumbs}` / `{error}` | 5 |

---

## Phase 1 — Shell + Explore parity (L)

**Goal:** the entire UI moves in-page; Explore mode fully works; `ViewerForm.xaml` becomes bare
chrome; render engine untouched; the share bundle keeps building.

Page-side (`web/viewer3.html`):
- **P1. Three-way mode gate first**: `HOSTED` (chrome.webview) / `BUNDLE` (`__DBHMS_BUNDLE`) /
  `DEV` (neither). BUNDLE keeps calling `bootStandalone()` verbatim AND a CSS gate
  (`body[data-mode="bundle"] .shell{display:none}`) hides all new shell DOM there, so an
  old-style share file rebuilt from the new page shows one UI, not two. DEV gets a
  `?model=<url>` + drag-drop loader so everything is browser-testable.
- **P2. Shell markup/CSS**: port coord.html's `#hdr` (gradient + dot tile + orange border) with
  `<img src="dbhms_logo.png" onerror=hide>`; delete the `#badge` wordmark; header tabs; 300px
  left overlay panel with collapse chevron; 380px inspector skeleton; bottom pill toolbar
  (Home, Fit, Visibility, `?` live; Orbit/Walk, Measure, Pins disabled with tooltips; **no Cut or
  Box buttons — dropped by ruling 2026-07-11**);
  **an empty docked filmstrip mount** (Phase 2 fills it). Keep `#c` at `100vw/100vh` and panels
  as fixed overlays — do NOT reflow the canvas into flex/grid (DPI corner-render bug, see
  memory `webview2_dpi_corner_render`). Keep `#winGrip`, `#props`, `#mm` (restyle chrome only).
- **P3.** Diag ribbon behind `?debug` / `debug:1`. Default off.
- **P4. Explore cards** wired to existing engine functions: Model (export/loadlast + stats),
  Visibility (tree from `reportFilters()` data, worksets get real rows, levels via the sanctioned
  `hiddenLevel` clause, named presets), Appearance (quality/edges/ground/perf/sun; port the
  time-of-day remap from `_push_sun` into JS), Navigation (speed/look, key legend, gamepad
  checkbox wiring `padUseEnabled`; delete the stale `'g'` comment), Saved views (fly/rename/
  delete/reorder/Home; mutations post `saveviews:`).
- **P5.** Inspector shows the pinned element card (hover keeps the small tooltip).
- **P6. Input hygiene**: keydown handler bails on input/textarea/select/contenteditable targets;
  blur toolbar buttons after click.
- **P7.** `ready` → host pushes `prefs:`/`views:`/`vispresets:`/`project:`; page posts edits back
  debounced. DEV mode uses localStorage instead.

Host-side (`script.py` + XAML, IronPython 2.7):
- **H1.** Strip `ViewerForm.xaml` to a window holding only `brd_viewport` (must stay valid XML).
- **H2.** Delete `ModelVisibilityForm` (XAML + class + helpers). Keep `SubcategoryPickerForm`.
- **H3.** Remove all WPF control wiring and `_push_*` methods; keep verbatim: WebView2
  init/retry/versioned-page/vhost, bounds-nudge timer, pop-out/fullscreen/dock, kd:/ku:
  forwarding with its guards, telemetry wrapper, `_collect_clash_rows` + `_on_share`.
  **DPI fix baked in here (Track D, diagnosis confirmed):** wrap the window/WebView2 host
  creation in per-monitor DPI hosting (`SetThreadDpiHostingBehavior(MIXED)` +
  `SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)` via ctypes, restored right after),
  re-asserted on the pop-out re-parent and the fresh-profile retry; fully guarded (no-op unless
  M11 != 1.0, try/excepted). Add `M11` + `RasterizationScale` to the diag ribbon. This is the
  one Phase 1 item that must be **verified live at 150%**, not in the browser.
- **H4.** `export:`/`loadlast`/`share:`/`popout`/`fullscreen` handlers around the existing
  bodies; export bracketed by `exportstate:` posts; page disables its Export button while busy
  (the dispatcher blocks during export).
- **H5.** New persistence IO: `vispresets\<title>.json`, global `prefs.json`, extended
  viewpoints records (`id`, `home`, later `thumb`). **All JSON writes `ensure_ascii=False` +
  utf-8** — preset and view names are user-typed (IronPython non-ASCII gotcha).
- **H6.** Copy `dbhms_logo.png` from the Clash Detection web folder into this tool's `web/`.

Tests: **T1** — CPython test runs the real `clash_share.bundle.build_share_html` against the repo
`web/` dir with a tiny temp glb (output under the system temp dir, never under `src/`), locking
the importmap + single-module-script invariants the bundler string-matches on. **T2** — existing
suite stays green (XAML parses, AST, telemetry references).

**bundle.py invariants the page rebuild must keep** (verified against bundle.py source): exactly
one `<script type="importmap">`; exactly one `<script type="module">` with that byte-identical
attribute-free tag (no second module script anywhere — the bundler rewrites only the first); no
literal `</script>` substring inside JS strings/templates; no lines beginning `import`/`export`
inside template strings. All new JS lives inside the one existing module script.

Known cost, flagged: **the WPF clash list retires here and Clashes mode arrives in Phase 2** —
for one phase, clash review happens in the Clash Detection app only.

Revit checklist: new header/tabs/no-badge; export + subcat picker + stats; visibility incl.
worksets/levels/presets surviving relaunch; appearance + nav sliders surviving relaunch; saved
views + Home surviving restart; WASD/F forwarding still works and typing in inputs does not fly
the camera; pop-out/fullscreen/dock; share still builds and opens; minimap fine; diag gone.
**On a 150% high-DPI machine:** full-bleed render (no top-left corner / dark border), popped out
too; diag (`?debug`) shows `M11=1.5 rasterScale=1.5`; 100% and RDP still render fine (no
regression). This closes Track D.

## Phase 2 — Clashes mode (L)

**Goal:** the full clash workflow in-page with zero render changes: rich list + inspector +
keyboard triage + pins + filmstrip + status write-back to the same `clashes.json` the
Coordination app uses.

Page-side: `clashdata:`/`clashupd:`/`clashopdone:` handling with `normalizeRow`/`band()` ported
from coord.html; DEV fixture rows so the mode runs in a plain browser. Left panel: presets
("Meeting set" default), band count rows, status/trade chips, search, order select (Score /
Newest / Level / Proximity, greedy chain **capped to the filtered set**, host-ft midpoints).
**Windowed list rendering** (5-10k rows). Inspector card ported from `renderClashCard` minus the
red/blue swatches and live-capture branches; prev/next header. **Select-by-fed_key**: lazy
`fedMap` from `elements[]` meta (exporter stamps fed_key into node extras); `focusClash` = fly to
`mid` + two yellow `Box3Helper` clones on the pair (the sanctioned edit) with graceful
degradation when an element is missing from the export. **Pin engine (the ONE pin
implementation, Markups reuses it parameterized by data source + colors):** a DOM `<canvas>`
over the WebGL canvas, zero scene objects; project filtered midpoints on camera change,
grid-cluster (~48px) to count badges, band-colored numbered circles, click hit-test, toolbar
toggle. Filmstrip fed by `row.thumb` URLs with lazy loading + band-colored placeholders.
Keyboard: J/K, digits decide-and-advance (Shift stays), Ctrl+Z single-slot undo toast. clashop
optimistic queue ported (busy-nack requeue). Register the **clash deck provider** in the
`Decks` registry (interface defined in Phase 3's engine but the registry object ships here as a
plain module-level stub: `Decks.register(id,{label,available,build})`).

Host-side: **unify** `_load_clashes` + `_collect_clash_rows` into one `_clash_rows_full(doc)`
whose rows are a subset of coord's `_clash_row` (id, seq, score/band, reason, headline, status,
kind, owner, pair/category/source labels, level, mid, fedA/fedB, gap, first_seen, deadline,
comments, thumb, haystack) plus the legacy `label/point/trade` keys so the Phase-5-era share
projection stays byte-identical. `ensure_ascii=False` on every dumps. **Second vhost**
`https://dbhms.clashdata/` → the bound clash folder (port `_map_data_host`/`_thumb_url` from the
Clash Detection host) — thumbnails are served, never copied; **the mapping must be re-applied in
the fresh-profile retry path** (the retry recreates CoreWebView2) and on folder rebind.
Port `_handle_clashop` (bulk_edit + atomic write + rollup refresh + ack + one-row `clashupd:`)
and `_handle_showinrevit`/`_zoom_active_view_to`. Delete the orphaned WPF clash methods.
Optionally forward J/K/digits while WPF chrome has focus. **No lib changes.**

Revit checklist: counts match the Coordination app on the same folder; filters/search/order
instant; click = fly + yellow pair highlight + card; J/K + digit decide-and-advance + undo; an
edit here shows in the Coordination app with a proper history entry; pins numbered/band-colored/
clustered/toggleable; filmstrip shows the app-captured thumbnails; Show in Revit selects + zooms
+ minimizes; Explore + share + Coordination app all regress clean; concurrent edits do not
corrupt `clashes.json`.

## Phase 3 — Present mode (M)

**Goal:** one key (H) or one click; chrome gone; orange top edge + logo badge + caption card +
prev/play/next capsule; keyboard driving with chrome hidden; exact shell restore on exit.

Page-side: `Decks` engine (`build()` → `{title, stops:[{id,title,subtitle,thumb,go(animMs)}]}`);
saved-views provider (+ the Phase 2 clash provider now lights up in the picker; session provider
arrives in Phase 4); `startFly` gets an optional duration arg (default 600, present uses ~1500);
present chrome DOM; `enterPresent()`/`exitPresent()` snapshot-and-restore of the full shell
state; keyboard (arrows/PageUp-Down step, Space autoplay driven from the frame loop with ~6s
dwell, H chrome toggle, Esc exit — present-Esc runs before the existing deselect branch and
consumes the event; same keys honored via `kd:` forwarding); 4s idle auto-hide; one-time hint
toast (localStorage, loss-tolerant); thumbnail capture on view save (offscreen 320x180 JPEG via
`toDataURL`, `preserveDrawingBuffer` is already true) added to the `saveviews:` schema's
existing `thumb?` field. **Laser pen (L, fades ~3s, never persisted): committed for v1**
(ruling 2026-07-11) — transparent overlay canvas, timestamped points, per-frame redraw with
alpha decaying over ~3s; zero interaction with markup storage.

Host-side: `present:` handler — popped out: reuse `_enter/_exit_fullscreen`; docked: borderless
maximize with the Normal→style→Maximized hop, reversed on exit; post-transition
`webview.Focus()`. **Add H to the always-forwarded nav-key map** (not just while presenting, or
the entry keystroke never arrives when WPF chrome has focus); extend forwarding with
arrows/Space/Esc while `_presenting`. Teardown safety: `_on_popout_closed`, `_dock_render`, and
`_on_close` post `presentend:1` and restore window state if presenting.

Revit checklist: enter/exit from docked and popped-out states restores everything exactly;
arrows/Space/H/Esc work with all chrome hidden; autoplay pauses on manual input; idle fade;
hint shows once ever; popout closed mid-present recovers cleanly; deck picker offers Saved views
and the current clash filter.

## Phase 4 — Markups + QA/QC sessions (XL)

**Goal:** session-based markups end-to-end: create/rename/delete sessions, pin + ink + note +
auto screenshot, statuses, filmstrip review, session report PDF.

**Batch A, lib-side first (one drop, needs pyRevit reload, fully CPython-tested):**
- `lib/clash_core/markups.py`: `read_markups_at(folder)` / `write_markups_at(folder, data)`
  (default `{"schema_version":1,"sessions":[]}`), `markups_path_at`, `session_shot_dir_at`
  (`<folder>/markups/<sid>/`), factories `make_session`/`make_markup` (uuid4), pure mutators
  mirroring `bulk_edit` (status/assignee/due/note/comment/rename, no-op-skipping, history
  appending), `is_safe_id` (uuid whitelist — session ids and guids become path segments), and
  **`archive_session_at(folder, session_id)`** implementing the ruled delete-is-archive
  behavior: create `<folder>/3D Review Markups/` on first use, then
  `<folder>/3D Review Markups/<safe session name> (<session date>)/` (numeric suffix on
  collision) containing `session.json` (the full session record), the session's screenshot
  JPEGs moved from `markups/<sid>/`, and a self-contained `report.html` (via
  `markup_report.build_session_html`) so the archived record is browsable from Explorer with
  no tool. The session is then removed from `markups.json`. There is no hard delete.
  Py2+py3 dual parse. Tests exercise ONLY the module's public API (not persistence privates).
- `lib/clash_report/markup_report.py`: `build_session_html(...)` following `html.py` conventions
  (self-contained, base64 thumbs, `@media print`): cover + summary table + full-screenshot
  appendix.
- `tests/test_clash_markups.py` + `tests/test_clash_markup_report.py`: round-trip incl.
  non-ASCII note, atomic-write recovery, guid stability, no-op mutators, `is_safe_id` rejects
  traversal; report builds, escapes HTML, tolerates missing thumbs.

**Batch B, host-side:** `_push_markups()` from the `filters:` branch and after each op;
`sessionop:`/`markupop:` handlers through the lib mutators (atomic write, always ack, busy-nack
during export); `markupshot:` decode + atomic JPEG write with `is_safe_id` validation (copy
`_decode_dataurl`/`_write_image` from the Clash Detection host); session archive moves its shot
folder (never deletes); `mkreport:` → Save As + `markup_report` + the hidden-WebView2 `PrintToPdfAsync` pattern
(share the live CoreWebView2 Environment; HTML+browser fallback). **Markup screenshots are
served through the Phase 2 `dbhms.clashdata` vhost** (`/markups/<sid>/<guid>.jpg` + mtime
cache-buster) — no mirror copies. Open in Revit posts **`showinrevit:`** with a single key.

**Batch C, page-side:** All sessions view + session view + in-page dbHMS-styled dialogs
(create / rename / **Archive** with a confirm naming the destination folder — no delete verb
anywhere in the UI);
optimistic op queue (clashop pattern); **pin drop**: raycast (small models: `pickAt` hit point;
merged models: raycast `mergedMeshes`, read `_elemId`/`_vis` at the hit face) storing host-ft
Z-up point (exact inverse of `hostToViewer`) + `unique_id`/`fed_key`/`element_id`/`source` from
element meta (null-guard old exports); BCF-shaped viewpoint (camera point/direction/up/FOV,
host-ft Z-up, + capture aspect); **ink overlay**: 2D canvas, pointer events only while armed,
palette (Select/Pin/Pen/Highlighter/Cloud/Arrow/Text, 5 swatches #E53E3E default, 2 weights,
undo/redo, Done), strokes normalized against a letterboxed fit of the capture aspect; arming an
ink tool sets `viewLocked` gating mouse/wheel/nav keys AND host-forwarded `kd:/ku:` (+ gamepad),
with the "View locked" pill; ink replays only when the camera matches the viewpoint (epsilon
scaled by model radius), fades on departure with a "Return to markup view" button; **snapshot**:
force one synchronous render through the RAF code path, compose WebGL canvas + ink at ~1040x640,
`toDataURL(jpeg, .85)`, post `markupshot:`; pins reuse the Phase 2 DOM-canvas pin engine
(status colors, hide-resolved toggle); inspector (note/status/assignee/due/comments,
Snapshot vs Live 3D toggle, Open in Revit, report button); filmstrip deck; register the
**session deck provider**; STANDALONE shows a read-only "available inside Revit" state.
**This phase also ships the final unified Esc ladder**, in writing: present → dialog/lightbox →
ink-armed (cancel stroke, then drop tool, then unlock view) → clash focus → mouselook →
selection. Esc never closes the tool.

Revit checklist: empty/unbound states; session create/rename writes `markups.json`; archiving a
session produces `<folder>/3D Review Markups/<name> (<date>)/` with `session.json` + shots +
`report.html` and removes it from the tool (first archive creates the parent folder, later ones
only add session folders); pin + lock + ink +
Done → JPEG on disk under the session folder; orbit away fades ink with return button; full
relaunch rehydrates everything; second user on the same folder sees edits after relaunch;
filmstrip + prev/next; Open in Revit; report PDF with fallback.

## Phase 5 — Share parity + cleanup (M-L)

**Goal:** the standalone share file is the new shell in read-only mode; legacy share overlay and
dead weight deleted; docs/tests catch up; whole-redesign regression sweep.

Lib (one drop): `bundle.py` v2 — **new params as optional kwargs with defaults, never a signature
change** (script.py refreshes per launch but the lib does not; a stale lib must not turn Share
into a TypeError — host also wraps the call in try/except TypeError → `sharefail:` "restart
Revit to finish updating"). Adds full clash rows, `sessions`, `tests` map, and `thumbs` emitted
as a separate `window.__DBHMS_THUMBS` global via string concatenation (never through
`json.dumps`); stamps `bundle_v: 2`; asserts loudly if the page ever grows a second module
script. Non-ASCII text flows through the payload — cover it with a CPython test AND a Revit
build containing a "±10°" comment.

Host: `_collect_clash_rows` becomes a thin projection of Phase 2's `_clash_rows_full`;
`_collect_markups`; `_collect_thumbs` with downscale (~520x320 q60 via System.Drawing, disposed
in `finally`, per-image try/except) + priority cap (markup shots first, then Critical→Major→
Minor, ~25MB non-model budget; report skipped counts in `sharedone:`); the `share:` handler
posts `sharedone:`/`sharefail:` instead of popping dialogs.

Page: `bootStandalone()` rewritten to feed the new shell's own ingest functions from
`__DBHMS_BUNDLE` v2 + `__DBHMS_THUMBS`; version gate (v1 bundle → loud banner + degraded fly-to
list, never a blank page); one `applyStandaloneChrome()` entry point (hide Update from
Revit/Show in Revit/Share, editors become read-only chips; Present/pins/filmstrip/minimap stay
live); delete the legacy `#share` panel and all `share*` functions **retargeting the
`onLoaded` hook in the same edit** (it calls `shareOnModelLoaded` today; leaving the call
dangling kills model load); fold the stray trailing `#winGrip` style block into the head.

Docs/tests: README tool-#2 bullet updated (share = same page read-only, bundle_v, caps policy;
old share files are self-contained, never migrated; `web/index.html` stays as the host
init-trigger/fallback, excluded from the redesign); `tests/test_clash_share_bundle.py` (fixture
build + real-page invariants + non-ASCII round-trip); integrity test asserts
`ModelVisibilityForm.xaml` stays gone and `web/index.html` still exists.

Full-redesign regression sweep (manual, in Revit): 150% DPI laptop, RDP, pop-out + fullscreen
re-parent, Revit 2024 and 2026, share round-trip with non-ASCII, H: offline (unmapped bound
folder), fresh-profile retry path (both vhosts re-applied).

## Phase 6 — Measure tool (S-M)

**Ruled 2026-07-11: ship Measure only. Cut and Box are dropped** (no clipping planes; the render
rule stays absolute; revisit only if a real need appears). Phase 1's toolbar therefore carries
no Cut/Box buttons at all, and Measure sits disabled until this lands. Measure = two raycast
picks (reuse the existing pick paths), a DOM-overlay line + distance label in host feet with
fractional inches (coord's `fracInches` formatting), M key parity with coord, a half-done
measure cancels on Esc (it gets a rung in the Esc ladder). Pure overlay + camera math; zero
renderer state. Can land any time after Phase 1; the natural slot is alongside Phase 2 or 3.

---

## Track D — corner-render bug: **DIAGNOSIS CONFIRMED (2026-07-11)**

**D1 result: setting the 150% laptop to 100% Windows scaling makes the tool render perfectly.**
That is decisive. **The bug is a host-side DPI desync (theory 1). The GPU/driver-compositing
theory (theory 2) is dead** — a driver bug would not vanish at 100% scaling. So the fix is
knowable code, not driver roulette, and every earlier failure is explained: the surface renders
at scale 1.0 while the WPF control is laid out at M11=1.5, so it fills exactly 1/1.5 = 66% of the
control from the top-left. (The one earlier attempt that showed a *wrong position* was
`BoundsMode=UseRasterizationScale` double-counting; not a driver misplacement.)

**Reframe: this is no longer a risky side-experiment — it is a design requirement of the Phase 1
host rework.** Phase 1 already rebuilds `ViewerForm` + the WebView2 hosting (H1/H3). We build
that new window **DPI-correct from the start** instead of bolting a fix onto the old one, so the
corner bug never exists in the rebuilt tool. Because the browser preview can't reproduce a Revit
host-DPI issue, this one item is verified **live on a high-DPI machine**, not in the browser.

**Why the old fix failed and what changes:** the previous attempts wrote `RasterizationScale`
while the Wpf wrapper's own DPI *detection* still reported 1.0, so the wrapper reset the value
within a fraction of a second and won the tug-of-war. The durable fix makes the wrapper *detect*
the right scale instead of fighting it after the fact. Root cause of detection=1.0 is almost
certainly the DPI-awareness context of the HWND at WebView2-init time (worsened by re-parenting
the single control for pop-out). Approach, in order, all fully guarded (no-op unless `M11 != 1.0`,
try/excepted so a failure can never break the tool, and irrelevant on the 100% path that already
works):

- **E-root (build into Phase 1 H3):** wrap the WebView2 host window/control creation in
  per-monitor DPI hosting: `SetThreadDpiHostingBehavior(DPI_HOSTING_BEHAVIOR_MIXED)` +
  `SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)` immediately before creating the window /
  initializing `CoreWebView2`, restore the prior context right after (ctypes p/invoke to
  user32). This lets the WebView2 child HWND detect the real monitor scale, so the wrapper
  computes `RasterizationScale` correctly on its own and stays there. Re-assert on the pop-out
  re-parent (that path recreates the hosting relationship) and in the fresh-profile init retry.
- **E-fallback (only if E-root doesn't fully hold live):** with detection now correct, a
  one-shot `RasterizationScale = M11` set after init should stick (the reset loop that beat the
  old attempt only ran because detection disagreed); the stashed reflection code
  ("abandoned WebView2 DPI experiment") is the reference. `ShouldDetectMonitorScaleChanges`
  stays **true** (we want the wrapper detecting), the opposite of the old dead-end.
- **Instrumentation:** the diag ribbon (kept behind `?debug`/`debug:1` in Phase 1) already
  prints dpr/canvas/buffer sizes; add M11 + RasterizationScale to it so the live test reads
  pass/fail at a glance.

**Sanity note on scope (Nathan's priority = work computers):** the tool already renders fine on
the work machines (they are effectively 100%, incl. over RDP which presents at 100%), so nobody
is blocked today. Office 4K monitors and high-DPI laptops are the ones this fix unlocks. It costs
almost nothing to build correctly into the new window, and "Open in Browser" (which renders
perfectly at any DPI) stays the guaranteed fallback if a specific machine still misbehaves.

**Verify live at 150%** (the one non-browser Phase 1 check): open the rebuilt tool at 150% →
full-bleed render, no top-left corner, no dark border; pop out → still full; diag ribbon shows
`M11=1.5 rasterScale=1.5 dpr=1.5`; then confirm 100% and RDP still fine (no regression).

---

## Resolved decisions (Nathan, 2026-07-11)

1. **Measure only** — Cut/Box dropped (Phase 6 above).
2. **Session delete = archive** into `<bound clash folder>/3D Review Markups/<session name>
   (<date>)/` with `session.json`, screenshots, and a browsable `report.html`; the parent folder
   is created on first archive only; no hard delete anywhere (Phase 4 above).
3. **Laser pen ships in Present v1** (Phase 3 above).
   - **Pop out vs Present (raised 2026-07-11, undecided):** Nathan questioned whether Pop Out
     is still needed once Present exists. Keep both for now — Pop Out = render in its own
     window for a second monitor (Revit still usable beside it); Present = fullscreen tour
     chrome. Decide whether to fold them (e.g. Present just fullscreens the current window, and
     Pop Out becomes "open on second screen") once Present is actually built in Phase 3.
4. **One-phase clash gap accepted**: during Phase 1, clash review lives only in the Clash
   Detection app; Phase 2 brings names + flip-through back into this tool (that is the whole
   Clashes mode). No throwaway interim list.
5. **Corner-render bug = confirmed DPI desync** (100% scaling fixes it; GPU/driver theory dead).
   The fix is built into Phase 1's new window (Track D, E-root), verified live at 150%. Work
   computers are unaffected today; this unlocks 4K/high-DPI machines. Not a blocker.
