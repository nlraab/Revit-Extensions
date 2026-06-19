# Clash Detection

A pyRevit clash-detection toolkit for an MEP firm's daily coordination
work. Built to live entirely inside Revit, against the firm's normal
project structure (MEP host model + linked architectural model + sometimes
a linked structural model), so engineers don't have to leave Revit to find,
discuss, assign, and resolve interferences.

> **Read this file before making serious changes** to anything under
> `dbHMS Tools.tab/Clash Detection.panel/` or `lib/clash_*/`. It
> describes the architecture the rest of the code is shaped around.

---

## Status

**End-to-end loop is live, default test library is now broad-tests + auto-trade-tagging (Option B).**

The library shrunk from 17 narrow tests to **4 broad tests**:

1. **MEP Internal Coordination** — host MEP vs host MEP (all MEP categories on both sides)
2. **MEP vs Architecture** — host MEP vs host or linked architectural elements (walls, floors, ceilings, doors, windows, roofs, stairs)
3. **MEP vs Structure** — host MEP vs host or linked structural elements (framing, columns, foundations)
4. **MEP Soft Clearance (1 in.)** — soft-clash version of (2)+(3) at 1″ tolerance, for "near miss" review

The trade assignee for each clash is **auto-derived from the primary element's category** at detection time (`clash_core.categories.discipline_for_category_id`). A duct-vs-pipe clash inside the Internal Coord test still tags as Mechanical (the trade that owns the duct) — no fidelity loss vs the old narrow tests.

Per-run trade filter and Browser group-by handle the slicing that 17 narrow tests used to provide:

- **Run Clash Test → Trade Filter card** — checkboxes for the 5 MEP trades. Unchecked trades get filtered OUT of the test's set_a before detection, so you can do a quick "just check what I changed in Mechanical" run.
- **Clash Browser → Group by dropdown** — group the result list by Trade, Test, Status, or Level. Headers show counts per group.

Custom tests for special projects (medical gas, MRI clearances, etc.) go in **per-project overrides** via Test Library — same architecture as before, just with a smaller default library to start from.

What's implemented and runs against real Revit + persistence:

- `lib/clash_core/config.py` — per-machine config (load/save) via `%APPDATA%\dbHMS_clash\config.json`
- `lib/clash_core/project.py` — central-model path hashing (stable per-project ID)
- `lib/clash_core/persistence.py` — atomic JSON read/write for `clashes.json`,
  `project.json`, `test_library.json`, `test_overrides.json`
- `lib/clash_core/users.py` — current-user resolution (config override → `Application.Username`)
- `lib/clash_core/models.py` — factory functions for every domain dict + ISO-8601 timestamps
- `lib/clash_core/categories.py` — Revit `OST_*` ↔ friendly-name mapping
- `lib/clash_core/identity.py` — **clash fingerprint** (stable hash so re-runs recognize the same clash)
- `lib/clash_core/merge.py` — **diff/merge** of a fresh detection run with the previous one
- `lib/clash_core/dedupe.py` — pre-merge dedupe: drop soft clashes whose pair already
  has a hard clash in the same run (a pair that actually intersects isn't a "near miss")
- `lib/clash_core/browser_filters.py` — pure-data filter predicate for the Browser's
  Trade × Status × Test × Search filter card; built so the rule can be unit-tested
  in CPython independently of WPF / Revit
- `lib/clash_core/bulk_edit.py` — single-clash mutators (`apply_status`, `apply_trade`)
  used by the Browser's bulk-action handlers. Each appends a history entry matching
  the shape of the single-row handlers (audit log looks identical regardless of bulk
  vs single-row edit), skips no-op changes, and accepts an optional uniform
  timestamp so a whole batch shares one ISO time
- `lib/clash_core/history_format.py` — friendly-display formatters for clash history
  entries: `format_action` ("Status changed: Open → Reviewed"), `format_when`
  ("2026-05-06 05:11 UTC"), `format_author`. Used by the Browser's history sub-dialog;
  pure data so the formatting rules are testable in CPython.
- `lib/clash_detect/linked.py` — live link enumeration + role-map + cross-doc geometry helpers
- `lib/clash_detect/hard.py` — real `ElementIntersectsElementFilter` / `ElementIntersectsSolidFilter`
  detection (host vs host, host vs link, link vs link)
- `lib/clash_detect/soft.py` — bounding-box-inflation "near miss" detection with tolerance
- `lib/clash_detect/runner.py` — orchestrator that turns a saved test definition into raw clashes
- `lib/clash_view/geometry.py` — bounding-box math: transform link-local boxes into host
  coordinates (refits an AABB around all 8 transformed corners under rotation), union
  multiple boxes, pad / box-around-point fallbacks
- `lib/clash_view/threed_view.py` — find or create the persistent **Clash Navigator**
  3D view (single named view we own and reuse across every Show in 3D click — keeps
  the project file clean instead of accumulating one view per click)
- `lib/clash_view/highlights.py` — view-scoped element color overrides: paint clash
  element A red and element B blue in the Clash Navigator view only, clear on
  next click / Browser close. Module-level state tracks the last-painted IDs so
  the view never accumulates stale highlights across clicks.
- `lib/clash_view/navigate.py` — `show_clash(uidoc, clash_dict, role_map)` resolves
  the clash's two element refs back to live elements (host + linked), centers a 10 ft
  cube section box on the clash midpoint, applies red/blue color overrides to the
  host elements, switches to the navigator view, selects the host elements, zooms
  tight on the section box. Plus `clear_highlights(uidoc)` for explicit cleanup.
- `lib/clash_view/snapshot.py` — `export(doc, view, out_path, pixel_size)`:
  Document.ExportImage wrapper for clash thumbnails (PNG, FitToPage zoom,
  800px longer-edge default, 150 DPI). OFFSCREEN render — doesn't depend on
  the view being active or visible, doesn't capture overlapping windows
  (the Browser, dialogs, tooltips). Pairs with `threed_view.set_section_box`,
  which sets both the section box AND a matching CropBox on the view —
  the CropBox is what makes ExportImage's FitToPage zoom frame the clash
  region tightly instead of zooming to project extents.
- `lib/clash_report/bcf.py` — BCF 2.1 file builder. `build_bcf_zip(project_meta,
  clashes, viewpoints_dir, out_path, filter_predicate)` writes a `.bcfzip`
  containing `bcf.version` + `project.bcfp` + per-topic folders with `markup.bcf`
  (topic title / status / comments / assignee), `viewpoint.bcfv` (orthogonal
  camera + 6 clipping planes from the section box), and `snapshot.png` (the
  thumbnail captured by `clash_view.viewpoint`). Pure-Python (zipfile +
  xml.etree.ElementTree, no external deps), atomic-rename writes (no half-zip
  on failure), feet → meters coordinate conversion. Receivers tested:
  Solibri, BIMcollab, Newforma.
- `lib/clash_report/excel_summary.py` — `build_xlsx(clashes, out_path,
  filter_predicate)` writes a formatted XLSX workbook for the internal-team
  review path (open in Excel, sort, filter, save-as PDF if needed). 22
  columns × N rows. Manual XLSX construction (zipfile + xml.sax.saxutils)
  so there's no openpyxl dependency in IronPython. Includes:
    - dbHMS-styled header row (white bold on dark slate, frozen)
    - Status pills colored to match the Browser (Open=red, Reviewed=amber,
      Approved=blue, Resolved=green)
    - Trade pills with the same muted-tint backgrounds as the Browser
    - Numeric columns right-aligned with thin borders
    - Auto-filter dropdowns on every column header
    - Per-column widths tuned for typical content
  Atomic-rename writes, Unicode-safe (XML escaping). Validated end-to-end
  against openpyxl in the test suite — the file opens cleanly in Excel.
- `lib/clash_view/viewpoint.py` — two capture entry points:
  `generate_for_all(uidoc, clash_dicts, role_map, project_hash, captured_by,
  log, only_missing=True)` — batch-generates viewpoint thumbnails for every
  clash that needs one. Used by Run Clash Test (post-detection auto-gen for
  new clashes) and the Browser (catch-up on open for older data). Per-clash
  transaction stages section box + crop box + element color overrides on the
  navigator view, then ExportImages to the deterministic PNG path. Doesn't
  switch views or depend on UI state — pure offscreen render.
  `capture_for_clash(uidoc, clash_dict, role_map, project_hash, captured_by)`
  — single-clash capture used by the Browser's manual Save Viewpoint button.
  Re-stages section box + highlights for the clash (so the manual save always
  captures THAT clash, not whatever happened to be on screen) before exporting.
  Plus `viewpoint_image_for(clash_dict, project_hash)` for the detail panel
  to look up the image path on row selection.

Wired forms:

- **Settings** — per-machine fields save to `config.json`. Per-project section
  enumerates live `RevitLinkInstance`s, lets the user assign each to
  Architectural / Structural / `(ignore)`, and saves the mapping to
  `project.json`. Per-project disciplines roster also persists.
- **Test Library** — reads the global library from `<shared>/global/test_library.json`.
  Auto-seeds from `default_tests.json` on first launch. Shows project overrides.
  **Editor is fully wired** with a deliberate firm-vs-project safety model:
    - Selecting a **firm-wide (global) default** opens the editor in
      **read-only** mode with a blue locked banner. The prominent CTA is
      **Customize for this Project** — forks the selected global into the
      active project's `custom_tests` and switches the editor to that
      override. A small grey **Edit firm-wide…** link in the bottom-right
      of the editor unlocks the global for that one test (after a
      confirmation dialog), turning the banner yellow with a "Cancel
      firm-wide edit" out. Switching tests always relocks — firm-wide edit
      is per-test, not session-wide, so a stray Save can't mutate the firm
      library.
    - Selecting a **project override** opens the editor fully editable
      with a purple "Project override" badge and no banner. This is the
      common path.
    - **+ New Project Test** (footer) creates a blank in the active
      project's overrides — the easy create path. **+ New Firm Test**
      creates a blank in the global library and asks for confirmation
      first; the new firm-wide test opens unlocked so the user can fill it
      in without an extra unlock step.
    - **Delete** removes the selection from whichever store it came from
      (with a confirmation that names the scope so a misclick can't wipe
      the firm library). **Reset to firm default** overwrites the global
      with the shipped `default_tests.json` (project overrides untouched).
  Multi-source sets (`["host", "link:Architectural"]` style) are edited via
  three independent source checkboxes per set — the editor saves a single
  string when one is checked and a list when 2+ are.
  *Not yet wired:* the "Disabled override" flow — selecting a Disabled row
  shows a read-only banner instead of letting you re-enable from here. The
  4 broad firm defaults don't lend themselves to per-project disabling so
  this is deferred.
- **Run Clash Test** — runs **real detection** against the active doc + role-mapped
  linked models, merges with the existing `clashes.json`, and writes the result.
  Each new clash gets a per-project sequential number (`Clash #N`). On re-run:
  comments, status, history, and viewpoints on persisting clashes are preserved;
  disappeared clashes auto-mark Resolved with a history entry; Resolved clashes
  that reappear get reopened with a history entry.
- **Clash Browser** — reads real `clashes.json` and renders every clash. Status
  dropdown changes, trade reassignments, and posted comments persist back to
  disk **immediately** with a history entry recording who did what and when.
  **Live filters** (Iteration 5): the Trade and Status checkboxes, Test
  dropdown (populated from the actual loaded tests, not XAML mockup), and
  Search box (case-insensitive substring match across element names, IDs,
  trade, comment authors and bodies) all filter the visible grid in real
  time. The Test, Trade, and Status filters compose with the existing
  Group-by control. Reset returns to the firm-default coordination view
  (all trades on, Open + Reviewed only, all tests, no search). Status /
  trade reassignments and new comments re-apply the active filter set
  immediately so a row changing status to one that's currently hidden
  drops out of view without waiting for refresh.
  **Show in 3D** (Iteration 3) frames the selected clash in the persistent
  *Clash Navigator* 3D view. The section box is built **around the clash
  midpoint** with a 5 ft half-size (10 ft cube) — not around the union of
  the elements' bounding boxes, which would produce a section box the size
  of whichever element is largest (a 40 ft duct that clashes a wall at one
  end would otherwise produce a 40-ft section box rather than a 10-ft
  window on the actual clash). The midpoint comes from detection time,
  computed as the center of the bbox-overlap region. If that midpoint is
  missing or malformed the show falls back to padded element bboxes so the
  view still lands somewhere sensible. Host element(s) get selected so
  Properties shows them; the view zooms tight to the section box (via
  `ZoomAndCenterRectangle`, not `ZoomToFit` — `ZoomToFit` zooms to the
  view's CROP region which for a 3D view is project extents).
  **Element color overrides** (Iter 4): in the navigator view only, the
  clash's element A is painted red, element B blue. The next click clears
  the previous pair and paints the new one. Linked elements appear inside
  the section box but aren't selected or color-overridden — Revit's
  host-element APIs don't accept link refs cleanly and reference-based
  link work is finicky enough to defer.
  **Viewpoints** (Iter 6) are generated automatically and stored as PNG
  thumbnails (offscreen ExportImage render, ~30-80 KB each) at
  `<shared>/<project-hash>/viewpoints/<clash-id>.png` plus camera + section
  box + author + timestamp on the clash dict. Single viewpoint per clash
  (v1 design — every save overwrites the previous one in place). Three
  trigger paths:
    - **Post-detection batch** (Run Clash Test): every newly-detected clash
      gets a viewpoint generated immediately after merge. By the time the
      user opens the Browser, all thumbnails are already on disk.
    - **Browser-open catch-up**: any clash without a viewpoint (older data,
      detection paths that didn't auto-generate) gets one when the Browser
      opens. Usually a no-op on a healthy project; status bar reports
      progress when there are missing thumbnails to fill in.
    - **Manual Save Viewpoint button**: re-captures with whatever angle the
      user has rotated the navigator view to. Useful when the auto-generated
      isometric angle isn't ideal for a specific clash.
  Capture is **offscreen render via Document.ExportImage** (not a screenshot)
  — works without the navigator view being active or visible, doesn't capture
  overlapping windows like the Browser, and uses the view's CropBox to frame
  tightly on the clash (without the matching CropBox, ExportImage's FitToPage
  zooms to project extents — way zoomed out from where the user wants).
  The detail panel's image area shows the saved thumbnail immediately on
  selection; the placeholder text (with instructions) returns when no
  viewpoint has been saved.
  **Filter presets** (Iter 11): the "Filter Presets" card under the filter
  panel renders one button per preset. Three **built-ins** ship and can't
  be deleted: *Active only (Open + Reviewed)*, *Mechanical only*, and
  *Resolved only* — covering the common "what needs attention" / "single-
  trade focus" / "what got cleared" workflows. The **+ Save current as
  preset** button at the bottom snapshots the live filter state (trades,
  statuses, test, search) under a user-supplied name. **Right-click** a
  user-saved preset to delete it (with confirmation); built-ins have no
  context menu. User saves persist per-machine at
  `%APPDATA%/dbHMS_clash/filter_presets.json` (atomic-rename JSON write,
  same pattern as `config.py`) so they follow the user across projects
  rather than living in any one model. Pure-data persistence layer is
  `clash_core.filter_presets`; the script.py side just calls
  `make_preset` / `append_user_preset` / `delete_user_preset` and rebuilds
  the button list. Applying a preset suspends filter events while it
  re-checks every checkbox, picks the matching Test combo item (falling
  back to "(All tests)" if the saved name no longer exists in the
  project), and rewrites the search box, then runs `_apply_filters` once.
  Empty state shown if no clashes yet.
- **Reports** (Iter 7, +14) — exports the project's clashes in one of
  three formats for sharing. Filter card mirrors the Browser (Trade /
  Status / Test / Date range) and live-updates a preview count. Output
  folder defaults to `<shared>/<project-hash>/reports/` and is
  browse-able. Filename template supports `{date}`, `{project}`,
  `{filter}` tokens; the file extension auto-swaps when the user
  changes format.
    - **BCF 2.1 (.bcfzip)** — for outside coordination tools. Each topic
      carries the full clash data (title / status / comments / assignee),
      the section box as 6 clipping planes, the camera state, and the
      saved thumbnail PNG. Opens in Solibri, BIMcollab, Newforma, BIM
      Track, etc.
    - **Excel (.xlsx)** — for internal team review. Native XLSX (manually
      constructed; no openpyxl dependency) with full dbHMS-styled
      formatting: dark-slate header (frozen), colored Status pills,
      colored Trade pills, auto-filter dropdowns on every column, tuned
      column widths. 22 columns per row; sortable / filterable / save-as
      PDF directly from Excel.
    - **HTML summary (.html)** (Iter 14) — for sharing with non-Revit
      users (clients, GCs, project managers). Self-contained single
      file: inline CSS, inline base64-encoded thumbnails (~50 KB each),
      no external assets. dbHMS-branded header (slate bar + "db | HMS"
      wordmark) + run metadata + summary cards (counts by status, counts
      by trade) + one detail block per clash with thumbnail, element
      refs, status pill, trade pill, comments, and history snippet.
      Save-as-PDF works from any browser (Ctrl+P → Save as PDF) — gives
      us PDF for free without an IronPython PDF library, with
      `page-break-inside: avoid` on each clash so per-clash pages come
      out cleanly. HTML escaping on every user-supplied string defends
      against ill-formed clash data + accidental script content in
      element names. Pure-data builder at `lib/clash_report/html.py`,
      24 unit tests.
  All three formats pop up the file in Explorer (selected, ready to
  right-click → Send to) after a successful export.
- **Walkthrough** (Iter 12) — free-fly through the building with WASD +
  mouse-look, for coordination meetings. The first cut shipped a
  guided clash-tour and was wrong scope — clash review belongs in the
  Browser. The current build is a real free-fly walkthrough where you
  fly through walls and ceilings looking for equipment, toggle
  disciplines on/off, and save bookmark camera positions for the
  meeting moderator to click through.
  **Click "Open Walkthrough View"** → creates / activates the
  dedicated `dbHMS Walkthrough` perspective view. **Click into the
  Look Pad** (the dark area on the right of the form) to capture
  mouse + keyboard. Cursor hides; the green "● ACTIVE" pill appears
  in the header. Then:
    - **WASD** — walk forward / back / strafe (always horizontal,
      regardless of camera pitch — pressing W while looking at the
      floor doesn't drive you into it)
    - **Q / E** — move down / up along world Z (so "up" always means
      "toward the sky", not "toward where the camera is looking")
    - **Mouse drag** — yaw + pitch (yaw rotates around world Z so the
      horizon stays level; pitch is clamped to ±88° so you can't flip
      upside down)
    - **Shift / Ctrl** — speed up (3×) / slow down (¼×) on the fly
    - **Esc** — release mouse capture
  **Speed slider** sets the base movement speed (2–60 ft/s, 15 default).
  Modifier keys multiply on top.
  **Visibility checkboxes** in the left card — Mechanical / Electrical /
  Plumbing / Fire Protection / Architectural / Structural — toggle each
  discipline's full set of categories together so during a meeting you
  can hide architectural and see all the MEP, then turn arch back on for
  context. The discipline → category map lives in
  `walkthrough_view.DISCIPLINE_CATEGORIES` and is editable.
  **Bookmarks** save the current camera (position + forward + up) under
  a name. Persisted at `<shared>/<project-hash>/walkthrough_bookmarks.json`
  via the same atomic-rename pattern as filter_presets. Per-project, on
  the shared root — the whole team sees the same set, so a moderator
  can prep "RTU-1 east", "Lobby", "Mech room corridor" the night before
  and click through them on the call. Double-click a bookmark to snap
  the camera to it; Delete key removes the selected one (with confirm).
  **Render** captures the current view to a 1920×1080 PNG at 300 DPI
  (`walkthrough_render.render_stop`) at
  `<shared>/<project-hash>/walkthrough_renders/clash-view-<timestamp>.png`.
  Same module the clash-stop renders used.
  **Architecture:** modeless WPF (`__persistentengine__ = True` keeps
  the IronPython engine alive after script.py exits — without this,
  Revit fatal-crashes when invoking `Execute()` on a torn-down handler
  class with `ExceptionCode=0xe0434352`). All Revit-API calls (view
  create, camera set, discipline toggle, render) route through one
  shared `IExternalEventHandler` so they land on Revit's UI thread in
  a valid API context. WPF click handlers fire on the WPF thread and
  can't legally call `Transaction.Start()` directly.
  **Motion loop:** a `DispatcherTimer` ticks every ~33 ms (30 fps).
  Each tick reads the pressed-keys set + accumulated mouse-look delta,
  runs `walkthrough_motion.step` + `walkthrough_motion.look` to
  compute a new camera state, and queues an ExternalEvent to apply
  it. Mouse capture inside the look pad hides the cursor and recenters
  it after every move so the user can drag indefinitely without
  hitting screen edges.
  **Honest limit:** the dedicated view is currently a minimum-viable
  config (Realistic + Fine detail + cleared template). Sun, shadows,
  ambient lighting, gradient background, and category-noise hiding
  were stripped after they fatal-crashed Revit 2026 on first
  view-create — they need to be added back one at a time, each with
  testing, once we have a stable bones. A per-step debug log at
  `%TEMP%\\dbhms_walkthrough.log` records every config call so the
  next crash leaves a clear last-call-attempted line. None of this
  reaches Enscape fidelity (the Revit viewport is Autodesk's own
  rasterizer; without an external real-time renderer we can't hit
  PBR / GI / SSR) — the goal is "much better than default Revit on a
  projector," not photoreal.
- **Walkthrough Here — Browser → Walkthrough handoff** (Iter 13) — the
  Browser's **Walkthrough Here** button is now wired. Click a clash row
  → click the button → the Walkthrough flies to that clash's saved
  viewpoint. Bridges the two tools: Browser owns clash review, Walkthrough
  owns spatial navigation.
  Cross-script handoff is file-based — the Browser writes a small
  `walkthrough_pending.json` at `<shared>/<project-hash>/` containing
  the target's camera state; the Walkthrough form reads-and-clears it.
  This works regardless of whether the Walkthrough form is currently
  open: the form picks the file up either via its `_open_view`
  completion path (cold-launch case) OR via a slow polling timer
  (every ~2s while running) for the case where it was already open.
  Last-write-wins — if the user clicks Walkthrough Here on multiple
  clashes in quick succession, the most recent one is where the camera
  lands.
  Pure-data persistence layer is `clash_view.walkthrough_handoff`,
  symmetric with `filter_presets` and `walkthrough_bookmarks`:
  atomic-rename JSON write, defensive read (corrupt / missing →
  None), no Revit imports. The Walkthrough form's polling timer is
  separate from the 30 fps motion tick — it ticks at 0.5 Hz so we're
  not hitting disk 30×/sec for a feature that triggers maybe a few
  times per meeting.
  Pre-condition the Browser enforces: the clash must have a saved
  viewpoint already. If missing, the button alerts "Click Save
  Viewpoint first" and refuses to queue. Walkthrough Here has no
  meaning without a target camera state.

Stubs / mockups still:

- ~~**Reports** — form renders; Export BCF pops "coming soon."~~ (Done — see Wired forms.)
- ~~**Walkthrough** — launcher renders; Enter Walkthrough pops "coming soon."~~
  (Done — see Walkthrough entry under Wired forms.)
- ~~**Clash Browser filter presets** — visual-only mockup buttons.~~ (Done — see
  Filter presets entry under Wired forms below.)
- **Bulk operations in Browser** (Iter 9): the bulk-action bar appears
  when 2+ rows are selected. **Change Status** and **Reassign** open a
  picker for the new value; **Mark Resolved** applies directly. All
  three loop `clash_core.bulk_edit.apply_*` over the selection, share
  one ISO timestamp across the batch's history entries, skip no-op
  changes (clashes already at the target value), persist once at the
  end (single write, not N), and re-apply filters so rows that changed
  to a now-hidden status drop out of view immediately. A confirmation
  dialog appears when 5+ rows are selected so a misclick can't quietly
  reassign 100 clashes. **Group** is still a placeholder — needs its
  own data-model design pass.
- **History panel** (Iter 10): clicking the **History** button in the
  Browser's detail panel opens a modal sub-dialog (`HistoryDialog.xaml`)
  that renders the selected clash's full audit trail — every status
  change, reassign, comment, viewpoint save, and auto-resolve is one
  row showing the formatted timestamp + author on the left and the
  action label (with `before → after` arrow where relevant) on the
  right. Read-only; data comes straight from `clash_dict['history']`,
  formatted via `clash_core.history_format`. Empty state shows a hint
  when a clash has no recorded history yet.
- ~~**Walkthrough Here** button in Browser — pops "coming soon".~~ (Done
  in Iter 13 — see Browser → Walkthrough handoff entry under Wired
  forms.)
- **Walkthrough Xbox controller (XInput)** — deferred. P/Invoke against
  `xinput1_4.dll`, DispatcherTimer at 60 Hz; own iteration. WASD +
  mouselook covers the use case for now.
- **Clearance clashes** — not started; folders + stubs in place.

### Identity / merge model (the key new bit)

Each clash gets a deterministic `fingerprint` computed from:

- the test ID (clashes from different tests are distinct)
- the two element refs, sorted (swap of A/B → same fingerprint)
- the midpoint rounded to a 1 ft spatial bucket (small geometry shifts → same
  clash; same pair clashing in two distant spots → two distinct clashes)

When `merge_runs` writes a new run:

- **Persisting** (matched fingerprint) — keep id, comments, status, history,
  viewpoints; refresh midpoint and refs.
- **Reopened** — was Resolved, fingerprint reappears → flip to Open with a
  history entry.
- **Auto-resolved** — was Open/Reviewed/Approved, fingerprint did not appear
  in this run → flip to Resolved with a history entry.
- **New** — fingerprint not seen before → fresh clash with a per-project `seq`
  number (1, 2, 3...) for nice "Clash #42" display.

This matches Navisworks behavior closely enough that engineers used to that
tool should not need retraining.

---

## What's in this tab

The tab is `Clash Detection`, a single panel `Clash Detection`, and six
pushbuttons in this order (controlled by the `layout:` key in
`Clash Detection.panel/bundle.yaml`):

| # | Button | What it will do |
|---|---|---|
| 1 | **Run Clash Test** | Pick saved tests, run hard / soft detection, write results to the project's clash database. |
| 2 | **Clash Browser** | The main UI: list, filter, comment on, assign, change status, save viewpoints, jump-to-3D for every clash. |
| 3 | **Test Library** | Edit the firm-wide library of clash test definitions plus per-project overrides. |
| 4 | **Reports** | Export filtered clashes as a BCF 2.1 file. |
| 5 | **Settings** | First-run wizard, shared-folder path, per-project preferences. |
| 6 | **Walkthrough** | Full-screen 3D, two modes: clash-by-clash navigator (with section box) and free-fly with Xbox controller / mouse + keyboard. |

---

## Folder layout

```
src/extensions/dbHMS Extensions.extension/
    dbHMS Tools.tab/
        dbHMS Tools.panel/                   <- general productivity tools
            (AlignViews, Sheet Manager, etc. — not part of this README)
        Clash Detection.panel/
            README.md                        <- this file
            bundle.yaml                      <- controls toolbar button order via `layout:` key
            Run Clash Test.pushbutton/
                script.py                    <- entry point
                icon.png                     <- 96x96 toolbar icon
            Clash Browser.pushbutton/
                script.py
                icon.png
            Test Library.pushbutton/
                script.py
                icon.png
                default_tests.json           <- firm-wide clash test seed data
            Reports.pushbutton/
                script.py
                icon.png
            Settings.pushbutton/
                script.py
                icon.png
            Walkthrough.pushbutton/
                script.py
                icon.png
    lib/                                     <- extension-level shared modules
        clash_core/
            __init__.py
            config.py                        <- per-machine config (shared folder, etc.)
            models.py                        <- Discipline / Status / Clash / Test / Comment dicts
            persistence.py                   <- JSON read / write, atomic-rename
            project.py                       <- central-model path -> stable project hash
            users.py                         <- current user (Application.Username)
        clash_detect/
            __init__.py
            hard.py                          <- Revit InterferenceCheck driver
            soft.py                          <- bounding-box-inflation overlap test
            linked.py                        <- linked-doc element collection + transforms
            clearance/                       <- STUB: future clearance-rule engine
                __init__.py
                rules.py
                volumes.py
        clash_view/
            __init__.py
            geometry.py                      <- bbox math (transform / union / pad)
            navigate.py                      <- "show this clash" entry point
            highlights.py                    <- per-element red/blue overrides
            threed_view.py                   <- find or create the navigator view
            viewpoint.py                     <- batch + single viewpoint capture
            snapshot.py                      <- thumbnail PNG export (~800 px)
            walkthrough_motion.py            <- WASD step + mouse-look math (pure data)
            walkthrough_bookmarks.py         <- per-project saved camera positions (pure data)
            walkthrough_handoff.py           <- Browser→Walkthrough fly-here handoff (pure data)
            walkthrough_view.py              <- create/configure dbHMS Walkthrough view
            walkthrough_render.py            <- 1920px high-quality render export
        clash_report/
            __init__.py
            bcf.py                           <- BCF 2.1 zip builder
            excel_summary.py                 <- native XLSX builder (no openpyxl dep)
            html.py                          <- self-contained HTML summary (Iter 14)
        dbhms_ui/                            <- shared friendly popup (Iter 15)
            __init__.py
            dialogs.py                       <- info(message, title=...)
            InfoDialog.xaml                  <- dbHMS-branded modal markup
```

### Why an extension-level `lib/` exists for *this* tool

The repo's `CLAUDE.md` says *"Tools do not share helper modules - duplication
across pushbuttons is intentional so each one can be deployed standalone."*
That convention holds for every other tool in the repo.

This tool is the documented exception: clash detection is a coherent system
where six pushbuttons all read and write the same on-disk data and share
the same data model and detection algorithms. Duplicating thousands of
lines across six buttons would be unworkable and would let the buttons
drift out of sync. pyRevit's standard answer is an extension-level `lib/`
folder that gets added to `sys.path` automatically; we use that, scoped
under `clash_*` namespaces so other tools' scripts can ignore it.

If you build a future tool that wants the same treatment, document it in
`CLAUDE.md` first.

---

## How clash data is stored

All clash data lives **outside** the Revit model, in a configurable shared
folder. Why outside?

* **Survives model corruption.** If the central .rvt corrupts, the clash
  database is untouched.
* **Inspectable.** Engineers can open `clashes.json` in Notepad to see
  what's there, recover from a bad write, or grep for an element ID.
* **Doesn't require Revit to read.** Reports, dashboards, and any future
  cross-project rollup can read the JSON directly.
* **No Revit-side schema migration.** Changing the data shape later is a
  matter of editing JSON, not migrating Extensible Storage in every model.

### Layout under the shared root

```
<shared_root>/                            <- e.g. T:\_clash_data\
    global/
        test_library.json                 <- firm-wide test definitions (editable)
    <project-hash>/                       <- one folder per project
        project.json                      <- display name, disciplines, central path
        clashes.json                      <- the clash database for this project
        test_overrides.json               <- per-project tweaks to global library
        viewpoints/
            <clash-id>__<viewpoint-id>.png
    <another-project-hash>/
        ...
```

`<project-hash>` is a short hex digest (first 12 chars of SHA-1) of the
normalized central-model path. It's stable across users on the same
project, and unique enough that two projects won't collide. Computed in
`lib/clash_core/project.py`.

### Per-machine config

```
%APPDATA%/dbHMS_clash/config.json
```

Contains the path to the shared root and a few per-user preferences. The
**only** thing an engineer has to set up the first time they use the tool
is point this at the firm's shared folder. After that, every project's
data is found automatically by the central-model path -> hash lookup.

---

## One-time setup for the team

Hand this section to your engineers verbatim:

1. **Find or create the shared clash folder.** Pick a location that
   everyone on the team can already reach. Examples that work:
   * a network share like `\\dbhms-server\projects\_clash_data\`
   * a mapped drive like `T:\_clash_data\`
   * a OneDrive / SharePoint synced folder, as long as everyone syncs it
2. **Make sure that folder exists.** It needs to be writable by everyone
   who'll run clash tests; read-only is fine for view-only roles.
3. **Open Revit and click any button on the Clash Detection panel
   (under the dbHMS Tools tab).** The first time, you'll be asked to
   point at the shared folder.
4. **Done.** Every project you open will get its own subfolder under that
   shared root automatically; you never have to set anything per project.

The choice is saved per-machine in `%APPDATA%\dbHMS_clash\config.json`,
so you only do this once. New laptops repeat the one-time prompt.

If you ever need to move the shared folder, change the path in **Settings**
and (the harder part) move the existing `<project-hash>` subfolders to the
new location - the hashes don't change, so the data lights up again as
soon as the path is right.

---

## End-to-end flow: a clash from "Run" to "Resolved"

A reader unfamiliar with the tool should be able to follow what happens.

1. **Engineer opens the central .rvt and clicks Run Clash Test.**
   The button reads the global test library + per-project overrides
   (`clash_core.persistence.read_global_test_library` +
   `read_overrides`), shows a checklist, and the engineer picks one or
   more tests to run.
2. **Detection runs (`clash_detect.hard`, `clash_detect.soft`).**
   For each test, set A and set B are resolved into element lists - host
   elements via `FilteredElementCollector(doc)`, link elements via
   `clash_detect.linked.collect_link_elements`. Linked-element collisions
   handle the link transform issue documented below.
3. **Results merge with the previous run.** The engine compares against
   the existing `clashes.json` and tags each clash as `new`,
   `persisting`, or `resolved` (an old clash that no longer fires). Open
   clashes that the engineer has commented on are preserved across runs;
   we never blow away history.
4. **`clashes.json` is rewritten atomically.** Write goes to a temp file,
   then `os.rename` swaps it in. Mid-write Revit crashes leave the old
   data intact.
5. **Engineer opens Clash Browser.** Loads `clashes.json`, shows a
   virtualized list grouped by trade. Filters: trade, status, test,
   level, free-text. Warns first if the project has > 2,000 clashes.
6. **Engineer picks a clash, clicks Show in 3D.** `clash_view.navigate.
   show_clash` finds or creates the dedicated `Clash Navigator` 3D view,
   applies a section box around both elements, isolates them, zooms.
7. **Engineer comments, changes status, optionally saves a viewpoint.**
   Each change appends a `HistoryEntry` to the clash. `Application.
   Username` (or the configured override) is the author. A viewpoint
   captures camera + section box + a PNG snapshot.
8. **Reports button -> BCF.** When the engineer wants to ship issues to
   a non-Revit consultant, Reports filters the clash list and writes a
   BCF 2.1 zip. Camera / section box / snapshots become BCF viewpoints,
   comments become BCF comments, status maps to BCF topic status.

---

## Disciplines and assignment

Clashes are assigned **by trade**, not by individual person. A clash
between two elements is assigned by default to the trade most likely to
resolve it (e.g. host MEP element vs linked architectural beam -> goes
to the host trade's queue, tagged "Arch link"). Engineers filter the
Clash Browser to "everything assigned to Mechanical" and work through
the list.

We capture *who created* a clash run and *who authored* each comment
(via `Application.Username`), but those are audit-log fields, not
assignment.

The disciplines we ship with:

* Mechanical
* Electrical
* Plumbing
* Fire Protection
* Technology
* Architectural
* Structural

Architectural and Structural are typically linked-only. The default
test library treats them as linked sources (`"source": "link:Architectural"`,
`"source": "link:Structural"`).

**Linked-model role mapping is per-project, configured by the user — not
inferred from filenames.** Linked .rvt files have unpredictable names that
vary by project (`ArchModel-2026-04-15.rvt`, `STR-Building-A_v2.rvt`,
`Civil-Site-Plan.rvt`, etc.) and a single project may have multiple
linked models in the same role (one architectural link per building, for
example). Filename heuristics like "ends with `_ARCH`" are too fragile.

The **Settings** tool exposes a per-project "Linked model role mapping"
UI: every loaded `RevitLinkInstance` is listed with a role dropdown
(Architectural / Structural / `(ignore)`), and the user assigns roles
explicitly. The mapping is stored in `<shared>/<project-hash>/project.json`
so it's persistent and team-wide.

Schema fragment in `project.json`:

```json
{
  "link_role_map": {
    "ArchModel-2026-04-15.rvt":      "Architectural",
    "STR-Building-A_v2.rvt":         "Structural",
    "Civil-Site-Plan.rvt":           "ignore",
    "ArchModel-Phase2-Lobby.rvt":    "Architectural"
  }
}
```

When a clash test specifies `"source": "link:Architectural"`, the
detection engine pulls the **union** of every link instance currently
mapped to "Architectural" in this project. Links not in the map (or
mapped to `"ignore"`) contribute nothing.

When a project doesn't have a structural link assigned, tests targeting
`link:Structural` simply produce zero results — they don't error. New
links loaded after the mapping was set default to `(ignore)` until the
user explicitly assigns them a role.

Adding a discipline later: extend `clash_core.models.Discipline`, add it
to the project.json roster, add an icon color, and update any UI lists.

---

## Detection algorithms

### Hard clashes (`clash_detect.hard`)

Wraps Revit's built-in `InterferenceCheck` and `ElementIntersectsElementFilter`.
For every pair of elements (one from set A, one from set B), test whether
their solids intersect. Returns a list of `(id_a, id_b, midpoint_xyz)`
tuples that the upper layer turns into Clash dicts.

For host-vs-link, see "Linked-model intersection notes" below.

### Soft clashes (`clash_detect.soft`)

Algorithm:

1. Compute each element's tight bounding box in shared coordinates.
2. Inflate each box by `tolerance_inches` on every face.
3. `BoundingBoxIntersectsFilter` to get candidate pairs (cheap).
4. For each candidate, use the actual geometry (with inflation, either
   via `Solid.Offset` or a swept bounding box - the latter is faster and
   accurate enough for typical "near miss" use) to confirm overlap.
5. Subtract any pair that *also* triggers a hard clash. Soft clashes are
   meant to surface near-misses, not duplicate hard ones.

Also reports the actual minimum gap so the Browser can rank by "almost
touching" first.

### Clearance clashes (planned, not built)

Stubbed under `lib/clash_detect/clearance/`. The plan: a JSON-driven
rules engine ("electrical panels need 36 in. front clearance," "VAV
boxes need 24 in. service clearance below," etc.). Each rule generates
a clearance Solid for matching elements; clashes fire when anything else
(including same-trade items) intersects the clearance Solid.

Why deferred: clearance rules are their own mini-project and require
real input from the engineering team on what the rules actually are.
Better to ship hard + soft first and add clearance once we know what
rules to start with.

When implemented, also:

* extend `clash_core.models.ClashKind.CLEARANCE` use sites in the Browser
  and Reports filters
* extend `default_tests.json` with a `clearance_rules` section
* update this README's algorithm section

---

## Linked-model intersection notes

The headache: `BoundingBoxIntersectsFilter` and `ElementIntersectsSolidFilter`
do not behave correctly when the `RevitLinkInstance` has been moved or
rotated relative to the host (they compare against link-local coordinates
without the transform). The Building Coder and the pyRevit forums have
extensive threads on this; our handling lives in `clash_detect.linked`.

The approach we'll use:

1. Get the host element's solid in **host** coordinates.
2. Get `link_instance.GetTransform()`, invert it.
3. Apply the inverse to the host solid -> now we have a Solid that lives
   in the link's coordinate system.
4. Run an `ElementIntersectsSolidFilter` against the link's document
   using that transformed Solid.

The reverse direction (link element -> host space) is also valid; we use
whichever has fewer elements on the "transform every solid" side.

Edge case: a host doc can have multiple `RevitLinkInstance` rows pointing
at the *same* linked .rvt with different transforms (e.g. site model with
the same building copied N times). `linked.find_link_instances` enumerates
each instance, not just the link type once.

---

## Walkthrough mode

Free-fly through the building with WASD + mouse-look, for coordination
meetings where the team needs to walk the model and inspect equipment.
Clash review belongs in the Browser; the Walkthrough is intentionally
**not** a clash-stop tour.

The first build (Iter 12) shipped a guided clash-stop tour and was
wrong scope — it duplicated Browser functionality without adding the
free-fly capability the team actually needs. The current build is the
free-fly redesign.

### Architecture (modeless)

This is the only modeless WPF window in the extension. The Browser,
Reports, Settings, etc. are all modal (`ShowDialog`); the Walkthrough
must be modeless because Revit's viewport keeps redrawing while the
camera updates each frame, and a modal window would block Revit's UI
thread.

Modeless requires three load-bearing pieces:

1. **`__persistentengine__ = True`** at the top of script.py. Without
   this, pyRevit tears down the IronPython engine when script.py
   exits, the `_WalkthroughHandler` class definition disappears, and
   when the user clicks any button Revit fatal-crashes invoking
   `Execute()` on a now-dead class. Symptom is `ExceptionCode=0xe0434352`
   in the journal a second or two after the click, with no Python
   output. This is the single most important line in the script.

2. **Pin the wrapper alive.** A module-level `_ACTIVE_WINDOW = win`
   prevents Python GC from collecting the WPF wrapper (and its event
   handlers) after `Show()` returns. WPF keeps the underlying Window
   alive on its own; the pin is for the Python side.

3. **Marshal Revit-API calls onto the API thread.** WPF click handlers
   and DispatcherTimer ticks fire on the WPF UI thread, which is NOT
   a valid Revit API context. One shared `IExternalEventHandler`
   (`_WalkthroughHandler`) services every Revit-touching action;
   handlers set `pending_action` + `kwargs` on it then call
   `external_event.Raise()`, and Revit invokes `Execute(app)` on its
   own thread when free. Completion callbacks marshal back onto the
   WPF dispatcher (`Dispatcher.BeginInvoke(System.Action(...))`) so
   any UI updates run on the WPF thread.

### Motion: WASD + mouse-look

Pure-data math lives in `clash_view.walkthrough_motion`:

* `step(camera, keys, speed_fps, dt_seconds, world_up=True)` — given
  the current camera tuple `(position, forward, up)` + the set of
  pressed motion keys + speed + elapsed time, returns the new camera.
  W/S walk along the camera's forward projected onto XY (so pressing
  W while looking at the floor doesn't drive you into it). A/D strafe
  along the right vector projected onto XY. Q/E by default move along
  world +Z/-Z (so "up" always means "toward the sky", not "toward
  where the camera is looking"); pass `world_up=False` for camera-up
  vertical. Diagonal motion is normalized so W+D doesn't travel √2×
  faster than W alone.
* `look(camera, dx_pixels, dy_pixels, sensitivity)` — applies mouse-
  drag deltas. Yaw rotates forward around world Z (no roll, horizon
  stays level). Pitch is around the right vector and clamped to ±88°
  so the camera can't flip upside down. Up vector is recomputed after
  pitch so the orientation stays a valid rigid frame.
* Rotations use Rodrigues' formula, vectors are renormalized after
  each operation so numerical drift doesn't compound.

Pure-data, fully unit-tested under CPython.

### The motion loop

The form runs a `DispatcherTimer` at ~33 ms (30 fps). Each tick:

1. Detect whether any movement input is active (`has_input` = keys
   pressed OR a non-zero mouse delta).
2. **No input:** update the last-tick clock and, if we're currently in
   the cheap "fast navigation" display style and the camera has been
   still for longer than `_STOP_DEBOUNCE` (0.3 s), queue an `end_motion`
   to snap the view back to full quality. Then return.
3. **Input:** record `_last_input_time`; measure `dt` from the last
   tick's wall-clock time (so movement is distance-per-second regardless
   of frame-rate hiccups).
4. Read `Keyboard.Modifiers` for Shift / Ctrl → multiply base speed by
   3× / ¼× respectively.
5. Run `walkthrough_motion.step` with the pressed keys + speed + dt.
6. Run `walkthrough_motion.look` with the accumulated mouse deltas;
   reset the deltas to zero.
7. Update the cached camera state and queue a `set_camera` ExternalEvent,
   passing `enter_fast=True` on the *first* frame of a movement burst.

The Revit-side `_set_camera` handler opens a transaction, optionally
drops the view to the fast display style (`enter_fast`), calls
`SetOrientation(ViewOrientation3D(...))`, commits, then forces one
`uidoc.RefreshActiveView()`. The transaction commit alone is cheap
(~1 ms even on a large model).

**The repaint, and the LOD fix (the big lag fix).** A committed
transaction that changes the active view doesn't reliably repaint it on
its own while a 30 fps timer is flooding the UI thread — WM_PAINT is the
lowest-priority Win32 message, so it gets starved and the picture only
updates when motion stops. So we must force the repaint every frame with
`uidoc.RefreshActiveView()` — an *immediate, synchronous* repaint on the
UI thread.

The catch: at full presentation quality (Realistic + Fine) that forced
repaint measured **~1073 ms per frame** on a large model (the commit was
~1 ms — the redraw was 99.7% of every frame), profiled via the per-frame
timings in `walkthrough.log`. Because a pyRevit modeless window shares
Revit's UI thread, that 1 s repaint froze the keyboard, mouse, and the
30 fps timer — the "1-2 fps, unusable, controls feel dead" symptom.
Native orbit stays smooth on the same model because Revit renders a
*simplified* model while you move and snaps back to full quality when you
stop. There is no public API for Revit's interactive LOD render, so we
reproduce the behavior ourselves.

**What we do (LOD / "do what native orbit does"):** while the camera is
actively moving, the view runs in a cheap display *style* — `Shaded`,
while keeping `Fine` detail (`walkthrough_view.enter_fast_navigation`).
Detail stays `Fine` on purpose: this is a clash-detection view, so the
user must see real pipe/duct wall thickness, which `Coarse`/`Medium`
collapse to single lines; `Fine` measured no noticeable cost over
`Medium` on the heavy model, and the display style is where the real
savings are. Every motion frame is then cheap, so the forced
`RefreshActiveView` stays well inside
the frame budget and the controls stay smooth. The moment movement stops
(debounced 0.3 s), `end_motion` →
`walkthrough_view.exit_fast_navigation` → `configure_for_first_run`
restores full presentation quality (firm template, or the Realistic +
Fine fallback). So the simplified look is visible *only while you're
actively flying*; the instant you stop, the view goes back to full
quality.

**Critically, `end_motion` does NOT force a repaint.** Committing the
display-style change marks the view dirty, and Revit then repaints it at
full quality on its own next-idle cycle — the same asynchronous,
non-blocking path native orbit uses when you let go of the mouse. An
earlier version forced a synchronous `RefreshActiveView()` here; on a
heavy model that immediate full-quality redraw is ~1 s and runs on the
shared UI thread, so it locked the user out of moving again until it
finished ("I stop and can't move for a second while it renders").
Handing the snap-back repaint to Revit removes that lockout: the
simplified frame lingers a beat, then Revit sharpens it on its own,
exactly like native, and the controls never freeze. We force the repaint
only *during* motion (where it's cheap in the fast style and needed to
beat WM_PAINT starvation), never on stop.

Two cases for restoring quality:

- **No template (fallback projects, incl. the heavy model that exposed
  the bug):** the display style is freely settable, so it's a clean
  `Realistic ↔ Shaded` swap each burst. No template work, no flicker
  beyond the style change.
- **Template projects (`dbHMS Walkthrough` applied):** if the template
  locks the display style, `enter_fast_navigation` detaches the template
  for the duration of the motion and `configure_for_first_run` re-applies
  it on stop. That re-attach is a Visibility/Graphics recompute, so there
  is a brief flicker at the start and end of a movement burst on template
  projects. Acceptable tradeoff — those projects weren't the laggy ones,
  and full fidelity is restored the instant you stop.

`Shaded` (display style) + `Fine` (detail level) is the current
navigate-quality choice. The display style is what's cheapened for speed;
detail stays `Fine` so pipe/duct thickness is visible for clash review.
The display style can be dialed lighter (e.g. `Wireframe`) if more
smoothness is ever needed — that's a one-line change in
`enter_fast_navigation` — but do not drop the detail level below `Fine`
without a reason, since that's the geometry a clash review depends on.

**Continuous-fly mode (the `chk_continuous_fly` toggle).** Even with the
snap-back handed to Revit, on a very heavy model the brief churn each time
you pause-and-resume can still feel like friction. The "Smooth fly mode
(stay simplified)" checkbox removes it entirely: while it's on, the form
sets `self._continuous_fly = True`, which makes the motion loop *skip the
`end_motion` snap-back* — the view stays in the cheap `Shaded` display
style (still at `Fine` detail) the whole time you're in the walkthrough. Flying is continuously
smooth and stopping is instant, because there is never a full-quality
re-render to wait for. The tradeoff is that the model always looks
simplified (Shaded) while the toggle is on. Toggling it ON drops to the
cheap style immediately (`set_camera` with `enter_fast=True`); toggling it
OFF restores full quality at once (`end_motion`) and the normal LOD
behavior resumes. Aimed at heavy models where smoothness beats fidelity;
off by default so light models keep full-quality stills on stop.

This exists because the underlying platform limit is real: a pyRevit tool
can only move the camera via `View3D.SetOrientation`, which is a
transactional document edit that forces a repaint. Revit's own native
orbit/walk uses a separate interactive navigation engine (no transaction,
GPU LOD) that is **not exposed to the API**, so a tool-driven camera can
never be quite as smooth as native at full quality. Continuous-fly mode is
the closest we get from inside Revit: keep every frame cheap, all the
time. The genuinely native-smooth alternatives are all outside a pyRevit
tool — a 3Dconnexion SpaceMouse (drives native nav, stays in the Revit
viewport, full quality) or a real-time engine like Twinmotion / D5 Render
(WASD + gamepad, but a separate synced window).

The hot path also uses a cached view reference (no per-frame
`FilteredElementCollector`) and does no per-frame logging, both of which
were adding avoidable cost to every frame.

Frame rate target is 30 fps because faster (60 fps tick) queues
ExternalEvents faster than Revit can drain them on dense MEP models —
the queue backs up, the camera lags input, and users feel like they're
fighting the controls. 30 fps + light per-tick work keeps up cleanly.

### Mouse capture inside the look pad

Click into the dark "Look Pad" area on the right of the form:
1. The look pad calls `CaptureMouse()` so MouseMove events keep flowing
   even when the cursor leaves the look pad's bounds.
2. The cursor is hidden (`Mouse.OverrideCursor = getattr(Cursors, 'None')`
   — `Cursors.None` is a syntax error in Python because `None` is a
   keyword).
3. The window grabs keyboard focus so WASD KeyDown fires.
4. On every MouseMove, the delta from the captured-origin point is
   accumulated, then the system cursor is recentered to the origin via
   `System.Windows.Forms.Cursor.Position`. This gives the user
   unlimited dragging room.
5. Esc, mouse-up, or LostMouseCapture releases everything cleanly.

The "● ACTIVE" pill in the header turns green while captured so the
user can see the mode at a glance.

### Visibility — discipline buckets

`walkthrough_view.DISCIPLINE_CATEGORIES` maps each discipline name
(Mechanical / Electrical / Plumbing / Fire Protection / Architectural /
Structural) to a list of `BuiltInCategory` name strings. Toggling a
checkbox calls `set_discipline_visible(doc, view, name, visible)` which
loops the categories and calls `view.SetCategoryHidden` on each (gated
by `view.CanCategoryBeHidden`).

Strings instead of direct `BuiltInCategory` references keep the module
parsing in CPython for the test suite — the enum lookup happens at
runtime via `getattr(BuiltInCategory, name, None)`. A category that
doesn't exist in the current Revit version is silently skipped.

The bucket map is editable; if a category needs to move (e.g. cable
tray feels like Electrical but ends up under Mechanical in some
firms' templates), it's a one-line fix.

### Bookmarks

`clash_view.walkthrough_bookmarks` is a pure-data persistence layer
mirroring `clash_core.filter_presets`:

* `make_bookmark(name, position, forward, up, ...)` — builds a fresh
  bookmark dict with a synthetic `bm-<10char-hex>` id, ISO timestamp,
  and the camera state.
* `read_bookmarks(project_hash)` — list of dicts; empty on missing /
  corrupt file (defensive — same "missing == empty" behavior as
  filter_presets).
* `write_bookmarks` / `append_bookmark` / `delete_bookmark` /
  `rename_bookmark` — atomic-rename writes (write to .tmp, rename
  onto path) so a crash mid-write can't truncate the JSON.

Path: `<shared>/<project-hash>/walkthrough_bookmarks.json` — per-project
on the shared root, so the whole team sees the same bookmark set. A
meeting moderator can prep "RTU-1 east", "Lobby", "Mech room corridor"
the night before and click through them on the call. Double-click in
the Bookmarks ListBox jumps the camera; Delete key removes the selected
one (with a yes/no confirm).

Saved bookmarks are full camera state (position + forward + up); the
jump action snaps the camera to that pose. v1 doesn't do eased
interpolation between bookmarks — instant snap is fine for meeting use
and the tour-style flight code from the previous iteration was deleted
when the tour itself got removed.

### Render export

`clash_view.walkthrough_render.render_stop` wraps `Document.ExportImage`
with high-quality settings (`PixelSize = 1920`,
`ImageResolution.DPI_300`, `ZoomFitType.FitToPage`,
`ExportRange.SetOfViews` pointed at the Walkthrough view). Output goes
to `<shared>/<project-hash>/walkthrough_renders/clash-view-<timestamp>.png`.

Timestamped filenames (not keyed by anything else) because the user
often renders the same view multiple times during a meeting at
different camera angles — each capture lands as a new file rather than
overwriting.

ExportImage occasionally appends a suffix to the filename it actually
writes; `_resolve_actual_path` finds and renames it onto the intended
path so the caller gets a stable result.

### The dedicated view (and the Revit 2026 view-config crash)

`clash_view.walkthrough_view.get_or_create_walkthrough_view` finds or
creates a `dbHMS Walkthrough` perspective View3D in a single
transaction. Falls back to isometric if `View3D.CreatePerspective`
fails (some Revit 2026 ViewFamilyType configurations reject perspective).

**Currently a minimum-viable config:** clear view template + Realistic
display style (with ShadingWithEdges fallback) + Fine detail. That's
it. Sun + shadows + ambient lighting + gradient background + ~20
noise-category hides were stripped after they native-crashed Revit 2026
on first view-create. They need to come back one at a time, each
verified, once we have a stable bones.

A per-step debug log at `%TEMP%\\dbhms_walkthrough.log` records every
configuration call so the next crash leaves a clear "last call
attempted" line. `walkthrough_view.log_path()` exposes the path.

Deleting the view from the Project Browser is a clean reset — the
next Open Walkthrough View re-creates it with these settings.

### What's still deferred

* **Eased bookmark transitions.** v1 snaps; eased flight (the math from
  the deleted `walkthrough_camera`) could come back as a one-time
  optional smooth-jump.
* **XInput / Xbox controller polling.** P/Invoke against
  `xinput1_4.dll`, `DispatcherTimer` at 60 Hz. The motion math doesn't
  care about input source, so adding controller support is just an
  input-binding layer.
* **Visual fidelity restoration** — sun, shadows, AO, gradient sky,
  category noise hiding. Each needs a Revit-2026 verification pass
  with the debug log in hand.
* **Photoreal rendering.** The Revit viewport is rasterized by
  Autodesk's engine; without an external real-time renderer (Enscape /
  Twinmotion / D5) we can't reach Enscape-level fidelity. The 1920px
  Render export is the slide-deck path.

---

## BCF reports

Targeting **BCF 2.1**, not 3.0. Justification:

* 2.1 is the version Navisworks, Solibri, BIMcollab, Newforma Konekt,
  Vrex, Catenda Bimsync, ACCA usBIM, and most other coordination tools
  read out of the box.
* 3.0 adds nice-to-haves (extensions, richer fields) but tool support
  is uneven as of this writing.
* Building 2.1 first, adding 3.0 later if a downstream consultant asks
  for it, is the path of least pain.

A BCF 2.1 file is a `.zip` with this layout:

```
bcf.version            <- xml; declares "2.1"
project.bcfp           <- xml; project name + project_id GUID
<topic-guid>/
    markup.bcf         <- xml; the issue (title, status, comments, refs)
    viewpoint.bcfv     <- xml; camera + clipping planes + components
    snapshot.png       <- the rendered viewpoint thumbnail
<another-topic-guid>/
    ...
```

Built by hand using `xml.etree.ElementTree` (no external dependency).
Implementation in `lib/clash_report/bcf.py`.

PDF / HTML summaries are not in this version; if requested later, the
data is already structured in a way that makes generating either
straightforward.

---

## Per-project storage layout

### `project.json`

```json
{
  "schema_version": 1,
  "display_name": "Sample Project",
  "central_model_path": "T:\\Projects\\Sample\\Sample-Central.rvt",
  "project_hash": "9c1f3a8b2e0d",
  "disciplines": ["Mechanical", "Electrical", "Plumbing", "Fire Protection", "Technology", "Architectural", "Structural"],
  "warn_threshold": 2000,
  "created_at": "2026-05-05T10:30:00Z",
  "created_by": "Nathan Raab"
}
```

### `clashes.json`

```json
{
  "schema_version": 1,
  "project_hash": "9c1f3a8b2e0d",
  "last_run_at": "2026-05-05T14:22:00Z",
  "tests_run": ["default-mech-vs-plumbing", "default-mech-equip-vs-walls"],
  "clashes": [
    {
      "id": "<uuid>",
      "test_id": "default-mech-vs-plumbing",
      "kind": "hard",
      "status": "Open",
      "assignee": "Mechanical",
      "ref_a": {
        "source": "host",
        "element_id": 123456,
        "category": "OST_DuctCurves",
        "name": "Round Duct - 12\""
      },
      "ref_b": {
        "source": "host",
        "element_id": 789012,
        "category": "OST_PipeCurves",
        "name": "PVC Pipe - 4\""
      },
      "midpoint": [12.5, 8.25, 9.0],
      "first_seen_run": "2026-05-05T14:22:00Z",
      "last_seen_run": "2026-05-05T14:22:00Z",
      "comments": [
        {
          "author": "Nathan Raab",
          "at": "2026-05-05T15:01:00Z",
          "body": "Will reroute duct above pipe."
        }
      ],
      "viewpoints": [
        {
          "id": "<uuid>",
          "captured_by": "Nathan Raab",
          "captured_at": "2026-05-05T15:02:00Z",
          "camera": {
            "position": [10, 10, 10],
            "target": [12.5, 8.25, 9.0],
            "up": [0, 0, 1]
          },
          "section_box": {
            "min": [10, 5, 7],
            "max": [15, 11, 11]
          },
          "snapshot_relpath": "viewpoints/<clash-id>__<viewpoint-id>.png"
        }
      ],
      "history": [
        {"author": "Nathan Raab", "at": "...", "action": "created"},
        {"author": "Nathan Raab", "at": "...", "action": "comment_added"},
        {"author": "Nathan Raab", "at": "...", "action": "status_changed", "before": "Open", "after": "Reviewed"}
      ]
    }
  ]
}
```

---

## Performance considerations

* **2,000-clash warning.** Before loading the full Browser grid, warn
  the user with a count and let them filter first.
* **WPF list virtualization.** Build the Browser list with virtualization
  from day one (`VirtualizingStackPanel`); without it, 5,000+ items
  becomes molasses.
* **Detection batching.** When a test enumerates host vs link, build the
  link-side index once per test (not per host element) and reuse it.
* **Snapshots are lazy.** PNGs only generate when the user explicitly
  saves a viewpoint or when Reports needs them - never on every clash
  found.
* **Atomic JSON writes.** Already covered, but worth restating: long-form
  JSON serialization happens on a temp file; the rename is atomic.

---

## Future expansion

In rough priority order, things this skeleton is shaped to absorb later
without restructuring:

1. **Clearance clash rules** (folder + stubs already in place).
2. **PDF / HTML summary reports** alongside BCF.
3. **Per-person notifications** when a clash is assigned (Slack / Teams
   webhook, email). Currently out of scope - assignment is by trade only.
4. **Cross-project rollups** - read every `<project-hash>/clashes.json`
   under the shared root and produce firm-wide stats.
5. **BCF 3.0** if a consultant asks for it.
6. **External-renderer integration** for the walkthrough (e.g. Enscape /
   Twinmotion live link with our clash markers overlaid).

---

## Testing

The repo's `tests/test_extension_integrity.py` is extended to cover this
tab: it asserts the six pushbuttons exist, every `script.py` parses, every
JSON file is valid, and the seed `default_tests.json` has the required
keys. The test suite runs in CPython 3 and only validates static structure
(parsing, schema shape) - it does not exercise Revit APIs.

The lib/ stubs are written to be importable in CPython 3 for the syntax
test, with all Revit API imports kept inside function bodies (added when
those functions are actually implemented). Runtime is IronPython 2.7
inside Revit.

---

## Glossary for non-programmer readers

* **Pushbutton** - a single button on a pyRevit toolbar; a folder with
  `.pushbutton` suffix containing `script.py` and `icon.png`.
* **Panel** - a group of buttons inside a tab; a folder with `.panel` suffix.
* **Tab** - a top-level Revit ribbon tab; a folder with `.tab` suffix.
* **lib/** - Python modules shared across the pushbuttons in this tab.
* **JSON** - a plain-text data format (key-value pairs and lists). All
  the clash data is stored as JSON files you can open in Notepad.
* **BCF** - BIM Collaboration Format; an industry-standard file for
  exchanging coordination issues between BIM tools.
* **Hard clash** - two elements physically intersect.
* **Soft clash** - two elements are within some configurable distance
  of each other (a "near miss").
* **Clearance clash** - something encroaches on a code-required clear
  zone around an element (planned, not built yet).
* **Section box** - a box you place in 3D that hides everything outside it.
* **Viewpoint** - a saved camera position + section box + screenshot.
