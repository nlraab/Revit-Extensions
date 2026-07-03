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
- `lib/clash_share/bundle.py` — packs the 3D Viewer into ONE self-contained
  `.html` so a project manager can double-click it in any browser (no Revit,
  no server, no internet) and walk the clashes. `build_share_html(web_dir,
  glb_path, clashes, viewpoints, project, generated, out_path)`: a
  double-clicked `file://` page can't fetch sibling files (browser CORS), so
  it inlines everything — each three.js engine module becomes a `data:` URL in
  the page's import map (relative `import`s rewritten to flat import-map keys,
  since relative specifiers can't resolve against a `data:` URL), the model is
  embedded as base64, and the clash list + saved viewpoints ride along as JSON.
  `web/viewer3.html` detects the absence of Revit's WebView2 host ("standalone
  mode") and builds an in-page overlay — nav help, the clash list with
  Trade/Status/Search filters + click-to-fly, markers, saved views, and
  model/category show-hide — wired to the SAME viewer functions the WPF panel
  drives in Revit. One renderer, two front ends. The 3D Viewer's **Open in
  Browser** button writes the file into `<shared>/<project-hash>/share/`.
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

Stubs / mockups still:

- ~~**Reports** — form renders; Export BCF pops "coming soon."~~ (Done — see Wired forms.)
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
| 6 | **3D Viewer** | Web-tech 3D model viewer in a pyRevit window: snapshot glTF export, in-tool category / workset toggles, fly through the model, share in a browser. |

---

## Folder layout

```
src/extensions/dbHMS Extensions.extension/
    dbHMS Tools.tab/
        BIM Tools.panel/                     <- general productivity tools
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
            3D Viewer.pushbutton/
                script.py
                icon.png
                web/                         <- web-tech 3D viewer assets
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
        clash_report/
            __init__.py
            bcf.py                           <- BCF 2.1 zip builder
            excel_summary.py                 <- native XLSX builder (no openpyxl dep)
            html.py                          <- self-contained HTML summary (Iter 14)
        clash_share/                         <- 3D Viewer browser-share packer
            __init__.py
            bundle.py                        <- inline engine + model -> 1 standalone .html
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

### One folder per project (the whole state model)

> **This supersedes the older shared-root design.** The two currently-shipping
> tools (**Clash Detection** / Coordination and **3D Viewer**) use the model
> below. The older per-machine "shared root" (`clash_core.config`) and the
> `<shared_root>/<hash>/` layout only remain for the being-retired buttons.

A project remembers exactly **one** thing: the absolute path of its clash-data
folder. The user points the tool at a folder (Coordination -> Settings ->
*Set up folder*, a plain folder browser), and that path is stored **inside the
Revit model** via Extensible Storage on `ProjectInformation`
(`lib/clash_core/binding.py`, `folder_for` / `write_binding`). Because it lives
in the .rvt, it travels with the file and is the same for every teammate who
opens it. All clash data is read and written **directly inside that folder**
via the `*_at(folder)` helpers in `lib/clash_core/persistence.py`:

```
<the folder the user picked>/     <- e.g. \\server\Projects\21112\BIM\Clash Data\
    clashes.json                  <- the clash database (+ groups) for this project
    project.json                  <- display name, disciplines, link role map
    test_overrides.json           <- this project's disabled/custom tests
    viewpoints/
        <clash-id>.png            <- on-demand clash context thumbnails
```

Key properties:

- **The folder is the truth.** Point the model at a folder -> you see that
  folder's data; an empty/new folder -> an empty tool; change the folder ->
  you see the new folder's data and nothing from before.
- **No folder set -> nothing shown.** A model with no stored path shows the
  "Set up folder" prompt and no data. Merely opening a tool never creates a
  folder (reads are non-creating).
- **No shared root, no hash, no subfolder naming, no migration.** The path is
  stored exactly as the user picked it; the tool does nothing about network vs.
  local. If a teammate can't reach that path they simply see no data.
- **Firm default tests are bundled** with the Coordination tool
  (`default_tests.json` next to its `script.py`); per-project tweaks live in
  the folder's `test_overrides.json`. There is no separate firm-wide library
  file.
- The schema is always built before reading (`_schema(create=True)`), because
  opening a .rvt does not reliably register an Extensible Storage schema in a
  fresh Revit session -- without this a reopened model (or a teammate, or the
  read-only 3D Viewer) would wrongly read "no folder set".

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
