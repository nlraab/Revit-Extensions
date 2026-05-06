# Clash Detection

A pyRevit clash-detection toolkit for an MEP firm's daily coordination
work. Built to live entirely inside Revit, against the firm's normal
project structure (MEP host model + linked architectural model + sometimes
a linked structural model), so engineers don't have to leave Revit to find,
discuss, assign, and resolve interferences.

> **Read this file before making serious changes** to anything under
> `Clash Detection.tab/` or `lib/clash_*/`. It describes the architecture
> the rest of the code is shaped around.

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
- `lib/clash_detect/linked.py` — live link enumeration + role-map + cross-doc geometry helpers
- `lib/clash_detect/hard.py` — real `ElementIntersectsElementFilter` / `ElementIntersectsSolidFilter`
  detection (host vs host, host vs link, link vs link)
- `lib/clash_detect/soft.py` — bounding-box-inflation "near miss" detection with tolerance
- `lib/clash_detect/runner.py` — orchestrator that turns a saved test definition into raw clashes

Wired forms:

- **Settings** — per-machine fields save to `config.json`. Per-project section
  enumerates live `RevitLinkInstance`s, lets the user assign each to
  Architectural / Structural / `(ignore)`, and saves the mapping to
  `project.json`. Per-project disciplines roster also persists.
- **Test Library** — reads the global library from `<shared>/global/test_library.json`.
  Auto-seeds from `default_tests.json` on first launch. Shows project overrides.
  Editor view is read-only (writing edits to disk lands next iteration).
- **Run Clash Test** — runs **real detection** against the active doc + role-mapped
  linked models, merges with the existing `clashes.json`, and writes the result.
  Each new clash gets a per-project sequential number (`Clash #N`). On re-run:
  comments, status, history, and viewpoints on persisting clashes are preserved;
  disappeared clashes auto-mark Resolved with a history entry; Resolved clashes
  that reappear get reopened with a history entry.
- **Clash Browser** — reads real `clashes.json` and renders every clash. Status
  dropdown changes, trade reassignments, and posted comments persist back to
  disk **immediately** with a history entry recording who did what and when.
  Empty state shown if no clashes yet.

Stubs / mockups still:

- **Reports** — form renders; Export BCF pops "coming soon."
- **Walkthrough** — launcher renders; Enter Walkthrough pops "coming soon."
- **Clash Browser filters** — checkboxes and dropdowns render but don't filter
  the live grid yet.
- **Bulk operations in Browser** (Change Status / Reassign / Group / Mark
  Resolved on multi-select) — pop "coming soon."
- **Show in 3D / Save Viewpoint / Walkthrough Here / History buttons** in
  Browser — pop "coming soon"; depend on viewport navigation modules.
- **Editing tests in Test Library** — pops "coming soon."
- **BCF export, Walkthrough full-screen + XInput, clearance clashes** —
  not started; folders + stubs in place.

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
    Clash Detection.tab/
        README.md                            <- this file
        Clash Detection.panel/
            bundle.yaml                      <- controls toolbar button order via `layout:` key
            Run Clash Test.pushbutton/
                script.py                    <- entry point (currently "coming soon")
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
            navigate.py                      <- "show this clash" entry point
            viewpoint.py                     <- save / restore camera + section box
            snapshot.py                      <- PNG export
            threed_view.py                   <- find or create the dedicated nav view
        clash_report/
            __init__.py
            bcf.py                           <- BCF 2.1 zip builder
        clash_walkthrough/
            __init__.py
            xinput.py                        <- Win32 XInput bindings (Xbox controller)
            camera.py                        <- input deltas -> View camera updates
            modes.py                         <- ClashNavigator + FreeFly mode controllers
            render.py                        <- enter / exit full-screen, set visual style
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
3. **Open Revit and click any button on the Clash Detection tab.** The
   first time, you'll be asked to point at the shared folder.
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

One full-screen window, two modes, switchable on the fly (gamepad START
or keyboard Tab).

### Clash Navigator mode

* Step through the filtered clash list one at a time.
* Each step: `clash_view.navigate.show_clash` zooms + section-boxes the
  view to the current clash and isolates the elements.
* Bindings:
  * **A button / Right arrow / N** - next clash
  * **B button / Left arrow / P** - previous clash
  * **X button / R** - mark Reviewed
  * **Y button / Enter** - mark Resolved
  * **Back button / Esc** - exit walkthrough

### Free-Fly mode

* WASD + mouse-look (or Q/E for vertical) for keyboard.
* Left stick translate, right stick rotate, LB/RB for vertical, A button
  speed-up, B button slow down for gamepad.
* Clash markers: colored spheres at each clash midpoint.
  * **Red** = Open
  * **Yellow** = Reviewed
  * **Green** = Resolved
* **Right shoulder / G** - jump to nearest clash.
* **Left shoulder / N** - jump to next clash by id.

### Rendering

`clash_walkthrough.render.set_pretty_visual_style` sets:

* Visual Style: Consistent Colors
* Shadows: ON (sun-driven)
* Ambient Occlusion: ON
* Sketchy Lines: OFF
* Show edges: hidden detail level

This is the best Revit can do without an external renderer. It looks
like nice CAD with shading - clearer than the default Revit workspace
view, but not photoreal. If the firm needs photoreal walkthroughs,
the right answer is fixing Enscape (or another real-time renderer);
we cannot match GPU raytracing inside Revit.

`enter_walkthrough` and `exit_walkthrough` hide / restore Revit's UI
chrome (project browser, properties palette, ribbon). `exit_walkthrough`
wraps every restore step in try/except so a failure mid-restore can't
leave Revit in a half-broken UI state.

### Xbox controller (XInput)

`clash_walkthrough.xinput` does P/Invoke against `xinput1_4.dll`
directly via `clr` + `System.Runtime.InteropServices.DllImport`. No
external Python package needed. We poll on a `DispatcherTimer` at ~16 ms
intervals (60 Hz). Wireless and wired controllers both work the same
way - XInput presents them identically.

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
