# Clash Coordination - Rebuild Spec (north star)

> This is the planning document for rebuilding dbHMS's clash detection
> into a Navisworks/Revizto-class coordination tool. It is the reference
> the rest of the work is shaped around. Read it before building anything
> under a new clash coordination tool. It pins down the navigation model,
> the full feature set, the data model, the element-identity contract, the
> importance-scoring design, the tech stack, and a phased plan that
> front-loads the risks that could actually sink the project.
>
> Status: **spec / pre-build.** No code is written against this yet. The
> current in-Revit tool (documented in `README.md`, this same folder) keeps
> running until the rebuild reaches parity.
>
> Companion docs: `README.md` (the current tool), and the memory note
> `clash-rebuild-direction` (the decisions and why).

---

## 1. What we're building

A single clash coordination application, custom to dbHMS, that feels like
Autodesk Navisworks Manage and Revizto but is built on top of the data
model, detection engine, and web viewer we already own. It absorbs every
capability of today's tool, adds the intelligence those tools are missing
(noise reduction, importance scoring, auto-grouping), and integrates both
ways with our 3D walkthrough viewer and with Revit itself.

The firm is standardizing this across every model, so the bar is: solid,
scalable, reliable, and genuinely easy to use.

**Non-goals for the first versions** (so scope stays honest):
- No full 2GB fly-through inside the clash tool. The clash tool shows a
  focused context view of the selected clash; full walkthrough stays in the
  3D Viewer and in Revit.
- No 4D / construction-sequence clashing (Navisworks TimeLiner). Out of
  scope.
- No real-time multi-user cloud. One coordinator at a time per project, on
  the shared folder, matching how we work today. Multi-user comes later
  only if the workflow demands it.
- No architectural/code-rule model checking (Solibri's domain). We do MEP
  interference and clearance, not general rule-based model auditing.

---

## 2. Decisions locked

These were decided with Nathan on 2026-06-30 and are not open questions
anymore. Everything downstream assumes them.

| Decision | Choice | Consequence |
|---|---|---|
| Rendering engine | **xeokit SDK 2.x** | Purpose-built BIM viewer: double-precision coordinates, batched loading of 100k+ pickable objects, section planes, BCF viewpoint round-trip, per-object isolate/color. Fed by our existing glTF export. No renderer built from scratch. |
| Tool reach | **Internal only** | xeokit's AGPL license is a non-issue. If this ever needs to go to clients, the fallback is @thatopen/Fragments (MIT); we keep the engine swappable but do not pay that cost now. |
| Detection | **Stays in Revit for v1** | We keep our working `clash_detect/` engine (true solid intersection, correct linked-model transforms). A browser-side geometry engine is a later phase for clearance distance and penetration depth, not v1. |
| Element identity | **Revit `UniqueId` as the universal key** | Stamped onto every exported mesh and every clash element reference, so a pick in the web viewer joins clash to element to Revit. This closes the seam gap in today's viewer. |
| Model in the tool | **Focused context view + Show in Revit** | Click a clash, see just those two elements framed and colored. Full walkthrough delegated to the 3D Viewer and Revit. |
| Packaging | **One web front-end, three mounts** | Embedded in pyRevit via WebView2 (primary), Tauri standalone (later), self-contained HTML share (already have it). |
| Team model | **One coordinator at a time, shared-folder JSON** | No database to stand up. Graduate to a small local database (PocketBase) only when concurrent editors start clobbering each other. |
| Importance scoring | **Constraint-first tier rules, firm-standard config** (revised 2026-07-01 after the deep research pass; see `CLASH_IMPORTANCE_RESEARCH.md`) | Ordered tier rules assign Critical/Major/Minor directly with a one-sentence reason; a clamped sub-score orders within the band. Structure/architecture are fixed context, never ladder rungs. ONE firm-wide standard (no per-project tuning), so "Critical" means the same thing on every project. Band words appear internally and in external reports; the numeric 0-100 stays internal. |
| Assignment | **Trade-primary, optional per-person later** | Clashes route to a discipline's queue as today; a per-person assignee and watchers can layer on later. Engineers rotate through projects, so the trade is the durable owner. |
| Location (level / grid / zone) | **Nearest grid intersection + level; zone deferred** | Grid label from the nearest grid lines at the clash point (e.g. "B/3"); level from the element or the clash elevation. Zone deferred (rooms/spaces are inconsistent across projects). No grids falls back to level + coordinates. |

---

## 3. Grounded in reality: what we take from Navisworks and Revizto

The single most important rule for this rebuild: **the UI is not an
afterthought.** We do not build features and bolt a UI on at the end. Every
screen and interaction below is copied from a proven pattern in Navisworks
or Revizto, named here so we can point at the reference instead of guessing.
Where we deviate, it is a deliberate call, not an accident.

**From Navisworks Manage (the detection and review muscle memory):**
- A-vs-B test setup driven by reusable saved sets, not hand-picked elements
  (Navisworks Search Sets). This is what makes tests survive model updates.
- The Results grid with a right-side inspector split into Items / Details /
  Comments. This is the single most familiar surface for a coordinator and
  we copy it closely.
- Click a clash row to auto-select both elements, zoom to them, and apply an
  isolation mode. Navisworks' real options are Dim, Hide, or Transparent
  dimming of the other items; we copy those three. (Navisworks' "Auto Reveal"
  is a separate walk-through occlusion feature, not a clash isolation mode, so
  we do not borrow that name.) This is the click-to-focus loop that makes it
  feel powerful.
- Ignore rules as the primary noise filter (same file, same system,
  coincident points, insulation tolerance). We ship better defaults than
  Navisworks does out of the box.
- The clash status lifecycle (New / Active / Reviewed / Approved / Resolved),
  which we already implement and, in one respect, better (see below).
- SwitchBack: jump from a clash to the same elements selected in Revit. We
  run inside Revit, so this is easier for us than it is for Navisworks.
- Report outputs (HTML / tabular / XML / viewpoints). We already ship BCF,
  Excel, and self-contained HTML.

**From Revizto (the coordination and collaboration polish):**
- Automated clash grouping and auto-assignment to a trade. The two things
  Navisworks does badly. This is where a chunk of our new value lives.
- A clash is a trackable Issue with priority, assignee, watchers, tags, and
  a deadline, not just a row in a grid.
- Status and priority as a color system that propagates everywhere: grid
  chips, 3D markers, dashboard segments.
- Saved filter presets, including a "my open items" style preset, and
  click-a-value-to-filter.
- Live dashboards, not static reports: open vs closed over time, counts by
  discipline pair and level.
- A clean project home that surfaces every area, so nothing feels buried.

**From Solibri (just the prioritization idea):**
- Graded severity by magnitude of violation, not a binary clash / no-clash.
  This feeds our importance score.

**What we deliberately drop or defer** (and why):
- Real dockable / floating / tabbed panels (WPF-style). We approximate with
  collapsible regions. Full docking is huge effort for little MEP payoff.
- 4D / TimeLiner clashing. Out of scope.
- Solibri-grade rule authoring. Out of scope.
- A Kanban board. We use the grid plus dashboards instead.

**Where we already beat both tools:** our clash identity model. Navisworks
and Revizto lose status and comments when element GUIDs regenerate on
re-export. Our deterministic fingerprint (test id + sorted element refs +
midpoint bucket) plus the new / persisting / resolved / reopened lifecycle
preserves human work across runs. We make this visible as a headline banner
("since last run: 12 auto-closed, 3 reopened"). Keep this. It is the moat.

---

## 4. Navigation model and the home screen

Nathan's requirement: a clean home that gets you to every area, so you never
feel like you have to hunt for a tab. The model below makes every major area
reachable in one click from anywhere.

### 4.1 Persistent shell (always on screen)

- **Top header bar** (dbHMS slate `#2D3748`): tool title "Clash
  coordination", the active project and run subtitle, and the db|HMS
  wordmark on the right. This never changes.
- **Left navigation rail** (always visible, icon + label, collapsible to
  icons only): the list of top-level areas. This is the spine of the app.
  One click from any screen reaches any other. Modeled on Revizto's
  persistent left panel.
  - Home
  - Tests (run + library)
  - Clashes (the results grid, the hero screen)
  - 3D (open the model / context view)
  - Reports
  - Settings
- **Right inspector** (contextual, collapsible): the selected-item detail
  panel. Shared across the Clashes grid and the 3D view so it never
  disappears when you move between them.

Everything the current six pushbuttons do maps onto one of these six areas.
No feature lives somewhere you would not think to look.

### 4.2 Home (the landing screen)

Home is a project dashboard plus a set of launchers. When you open the tool,
you land here and immediately see the state of coordination and a clear door
into every area. It contains:

- **The "since last run" banner** (auto-closed / reopened / new counts). Our
  persistence advantage, front and center.
- **Health summary cards**: total open, open by importance bucket (Critical
  / Major / Minor), and open by discipline pair.
- **Live charts**: open vs closed over time (the coordination trend), counts
  by level, counts by trade. Each chart segment clicks through into a
  filtered Clashes grid.
- **Quick launchers**: large clear buttons for the common next actions. Run
  a clash test, open the clash grid, open the 3D model, export a report,
  open settings. This is the redundant, obvious path to every area for
  anyone who prefers big buttons over the nav rail.
- **Recent activity**: the last few status changes, comments, and runs, so a
  coordinator returning to a project sees what moved.

The point: the nav rail is the fast path for someone who knows the tool, and
the Home launchers are the obvious path for someone who does not. Both reach
everything.

---

## 5. The screens (full feature set)

Every screen below names the Navisworks/Revizto pattern it is grounded in
and lists which current-tool features it absorbs, so nothing gets lost.

### 5.1 Tests (run + library) - tests defined by saved sets (the Navisworks lesson); matrix layout is a dbHMS design

Unifies today's **Run Clash Test** and **Test Library** buttons into one
area. Three pieces: saved sets, the test library, and a matrix run surface.

**Saved sets** (first-class, reusable). A named, reusable selection defined by
a property query (source plus Revit categories: host / arch link / struct link
crossed with categories), for example "all supply ductwork" or "all gravity
drainage". You define a set once and reuse it across many tests. This is the
backbone of real Navisworks coordination (Search Sets and Selection Sets), and
defining tests from named sets rather than hand-picked elements is why a
re-run after a model update still resolves correctly. This is the Navisworks
Search Set lesson, and it is the one thing that must not be skipped.

**Test Library** (the definitions). The firm-wide library plus per-project
overrides, with the exact firm-vs-project safety model we already built:
firm defaults open read-only with a "customize for this project" fork; a
guarded "edit firm-wide" path; new firm / new project tests; delete scoped
to the right store; reset to firm default. Each test pits set A against set B.

**Discipline matrix** (the run surface). Note: this matrix layout is a
deliberate dbHMS design choice, not a Navisworks pattern. Navisworks presents
its tests as a flat list; the matrix idea is closer to Revizto's clash matrix
(and to the results matrix in Navisworks' own Report tab). We adopt it as the
run surface because a discipline-vs-discipline grid reads faster than a flat
list for our MEP work. Rows and columns are the firm's real disciplines
(Mechanical, Electrical, Plumbing, Fire Protection, Technology, Architectural,
Structural). Each cell is a test between those two disciplines and shows its
type chip (Hard / Clearance / Soft / Duplicate), its tolerance, its
pre-assigned owner, and its last-run count. Empty cells offer "add test". The
diagonal is self-intersect (default off). Run one cell, run a row, or run all.
The per-run trade filter and the per-run link include/exclude toggle from
today's Run form both carry over, and the run writes a diagnostics log
(per-model element census, per-test detection and dedupe counts) for debugging
a "why zero clashes" run.

**Cell editor** (modal): Set A and Set B pickers (choose a saved set or build
a query inline), the type dropdown, tolerance with MEP presets, independent
horizontal and vertical clearance offsets (a Revizto touch, valuable for ducts
over ceilings), the ignore-rules checklist (same file, same system, coincident
endpoints, insulation within thickness, previously approved), and the default
owner.

Absorbs: Run Clash Test (all of it, including the per-run trade filter, the
per-run link include/exclude toggle, and the run diagnostics log), Test
Library (all of it), the 4 broad default tests, and the auto-derived trade
assignment.

### 5.2 Clashes (the results grid) - grounded in the Navisworks Results tab

The hero screen, the surface coordinators live in. This is the screen the
mockup showed.

- **Left rail**: filter presets (ship "my open", "new this run",
  "unresolved criticals", "important, score at or above 70" - all new; today
  ships Active only / Mechanical only / Resolved only) plus a stackable
  group-by control (level, system, grid/zone, discipline pair, proximity
  cluster, or none). Note: level, grid, and zone do not exist in the data
  today (level is a hardcoded stub); the rebuild must compute them at
  detection time and store them on the clash record before these columns and
  group-by axes actually work. See section 7.
- **Center grid**: sortable columns. Importance (score chip, color-graded),
  number ("Clash 214"), status chip, type, discipline pair, element A,
  element B, level/grid, gap/penetration, assignee, first seen, comment
  count. Default sort is importance descending, so the top of the list is
  the meeting agenda. When grouped, rows collapse into parent group rows
  ("Duct DD-03 vs Beams B/3-4, 12 clashes, max score 84"); a group inherits
  the max child score and resolves only when all children are decided
  (Solibri rollup semantics). Multi-select uses the firm-standard
  click / shift-click / ctrl-click.
- **Grid toolbar**: new group, group, ungroup, assign, comment, mark
  resolved, show in 3D, show in Revit. Bulk operations apply to the
  highlighted set.
- **Right inspector** (three stacked cards, Navisworks layout): Items (the
  clashing pair, each with isolate / zoom / select-in-Revit), Details /
  Display (highlight colors, isolation mode, and the importance score
  breakdown), and Comments (threaded, with the audit history).

Absorbs: the entire Clash Browser (list, filter, group, status/trade/comment
edits with immediate persist and history, show in 3D, save viewpoint, filter
presets, bulk operations, history dialog), plus the 2000-clash warning and
list virtualization for performance.

### 5.3 Issue / coordination detail - grounded in Revizto's Issue Tracker

We model each clash as a BCF Topic, and the target is for its stable
fingerprint to become the topic id. (Today's BCF export mints a fresh random
GUID per topic on each export; making the fingerprint the id is a change, and
the reason issues will round-trip stably.) Selecting a clash and expanding it
shows the full issue card: title, status (Open / Reviewed / Approved /
Resolved, our existing vocabulary), priority (a manual Blocker-to-Trivial
field, distinct from the computed importance score), assignee, watchers, tags,
deadline, reporter, location (level / grid / zone), the saved viewpoint
thumbnail, markup, and the comment feed. Assignment stays trade-based by
default (routing to a discipline's queue, exactly as today), with an optional
per-person assignee and watchers layered on top for teams that want
Revizto-style individual accountability; this trade-vs-person choice is open
question 5. A lightweight automation-rules editor (ordered, first match wins,
with an "otherwise" fallback) auto-assigns owner, priority, and tags per
group. This is the biggest "feels like Revizto" win and it is cheap on the
JSON store.

### 5.4 3D - grounded in Navisworks scene view + Revizto single space

Two things live here:
- **Open the model**: load the exported model (or a section-boxed region)
  and navigate it. This is the existing 3D Viewer, re-based on xeokit when
  scale forces it.
- **Context view**: when a clash is selected in the grid, this frames just
  those two elements, colors A red and B green, section-boxes the
  neighborhood, and dims the rest. Restores the clash's saved viewpoint
  (camera plus section box). This is the focused-context decision.

Absorbs: the 3D Viewer (glTF export, category/workset visibility toggles,
fly-through, clash markers, click-to-fly, saved viewpoints, open-in-browser
share, the pop-out render window and borderless fullscreen, and the
environment/render controls: time-of-day, sun direction and strength, edges,
ground plane, minimap, render quality, and performance mode). Those
environment/render controls must carry over, or be explicitly dropped, when
the viewer is re-based on xeokit. Adds the element-highlight join that does
not exist today.

### 5.5 Reports - grounded in the Navisworks Report tab

Keep today's Reports area intact: BCF 2.1, Excel, and self-contained HTML,
with the live preview count, filter card (trade / status / test / date), and
filename templating, opening the result in Explorer. Additions: filter by
importance and by group; every clash carries a saved viewpoint so it
round-trips into the viewer; BCF viewpoints include the stable element id /
IFC GUID (the thing Revizto drops); and BCF import (we only export today), so
issues from an outside consultant's Navisworks or Solibri come back in.

Absorbs: Reports (all three formats and the whole filter/preview/template
flow).

### 5.6 Settings - grounded in Revizto project settings

Keep today's Settings: the per-machine shared-folder path, the per-project
link role mapping (each Revit link assigned Architectural / Structural /
ignore), and the disciplines roster. The importance engine deliberately has
NO per-project settings (section 6: firm standard); a firm-level rule/table
editor may come later. Add: the automation-rules editor entry point.

Absorbs: Settings (shared root, link role mapping, disciplines, the project
display-name override, and the open-README / open-project-folder /
open-shared-folder shortcuts).

---

## 6. Important clashes (the differentiator)

This is the feature the firm specifically wants and the reason the rebuild is
worth doing. Three layers, all built from data we already compute, all
transparent (no black box), all tunable. Grounded in Navisworks ignore rules,
the industry MEP priority matrix, Solibri's graded severity, and Revizto's
auto-grouping.

> **REVISED 2026-07-01.** The original Layer B here (a weighted sum over a
> six-rung "who moves" ladder with structure on rung 5) was replaced after
> the deep research pass. Full findings, sources, worked examples, and the
> adopted design: `CLASH_IMPORTANCE_RESEARCH.md` (this folder). The engine
> is BUILT: `lib/clash_score/` computes everything below at merge time;
> ref enrichment is captured in `clash_detect/enrich.py`.

**The core idea: a clash's importance is the cost of moving the thing that
must move.** dbHMS can only move its own MEP. Structure and architecture are
never on a movability ladder; they are fixed context that selects which rule
fires. Between two MEP elements, the lower-rigidity (cheaper) element is the
mover. Each movable element gets a rigidity 0-5 from its system + size
(grease duct and gravity mains at 5 down to small conduit and flex at 0),
grounded in code physics (IPC 704.1 slope, IMC 506.3.7, NEC bend budgets,
NFPA 13), with a category-only fallback so old records still classify.

**Layer A - noise suppression (ordered rules, first match wins).** R-SELF
(element vs itself), R-NOT-OURS (both elements in consultants' links: dbHMS
can move neither), R-SYS (same system name touching at a fitting seam),
R-FIELD (lone small-bore conduit vs conduit, field-routed per NBIMS-US V3
5.5.4.8 + Annex A 1.5.1, with a congestion escape), and R-GRAZE (dormant
until penetration depth ships). Deliberately narrow: suppress only on
proof, nulls fail open. A suppressed clash is NEVER dropped: it keeps its
fingerprint/status/comments, still gets a full score, and sits behind a
visible "Suppressed (n)" toggle. A per-clash `suppress_override`
(force-show / force-suppress) beats every rule.

**Layer B - tier rules assign the band; a clamped sub-score orders within
it.** Critical (70+, "meeting agenda, resolve this cycle"): gravity/grease
vs structure (C1, flagged `escalate_candidate` since the fix may be a
structural penetration request); 24 in+ movers vs structure (C2); two
no-slack systems, one rigidity 5 (C3); deep penetration (C4, dormant).
Major (40-69, "assign and coordinate this cycle"): clearance-test hits
(M-CODE), rigidity 2-4 vs structure (M2), congested zones (M4, with
hysteresis), large-obstruction reroutes (M1), nearly-consumed clearances
(M3). Minor (8-39, "detailing, no meeting time"): arch penetration
candidates (N1, flagged for a future sleeve-schedule pass), insulation
pinches (N2, never suppressed), and the fallback. Every rule carries a
one-sentence plain-English reason with the code citation; that sentence,
not the number, is the trust surface. The sub-score (mover constraint +
size + geometry + congestion + code risk) is clamped inside the band, so a
weight change can NEVER silently flip a chip across 70: band changes
require a rule-input change, which is a visible cause.

**Config is the firm standard.** One standard across all projects (decided
2026-07-01): the tables in `lib/clash_score/defaults.py` are the standard;
there is no per-project tuning surface. Band words (Critical/Major/Minor)
appear internally AND in external reports; the numeric score stays
internal-only. Fully implemented in IronPython-safe pure Python,
unit-tested in CPython (`tests/test_clash_score.py`).

**Layer C - sticky issue groups.** (REVISED + APPROVED 2026-07-02; full
design, research grounding, and worked scenarios: `CLASH_GROUPING_DESIGN.md`.
Engine SHIPPED in `lib/clash_group/`, wired into both run pipelines after
scoring; UI is the next phase.) A group is an ISSUE — a ticket a coordinator
names, assigns, and discusses — not a query result. Rosters are STICKY: the
algorithm never re-derives existing groups, it only assigns currently
ungrouped clashes (the iConstruct / BIMcollab Smart Issues pattern). Member
handles are the immortal clash ids, never fingerprints. Two automatic
formation axes: density-gated spatial clusters (core = stamped cluster_n >= 6,
component >= 10 members, 6-ft union, borders never bridge two components — so
merging two distinct nearby problems is structurally impossible) and a
participation-anchored element star (the element on the most clashes anchors;
the anchor key is frozen at formation; >= 3 members). Fingerprint re-key
churn is healed by a tiered successor cascade (same test + same element pair
= silent adopt; weaker evidence = suggestion only). New clashes touching a
group's anchor auto-join (flagged needs_review when the group is beyond
Open); spatial adjacency only ever suggests into curated groups. Splits and
merges are human acts only; manual groups are exclusive and invisible to
formation. Groups persist as the top-level `groups` key INSIDE clashes.json
(one atomic write; both run-writer literals carry it, integrity-tested).
Rollup: band/score = max open unsuppressed member; a group resolves only
when every member is decided, and reopens when any member does. Regroup by
level/system/discipline pair remains an ephemeral UI pivot, never identity.

**How the user sees and drives it:**
- The Importance column leads the grid and the grid default-sorts by it.
- "Important" is a first-class saved preset (score at or above 70) and a Home
  dashboard bucket.
- The inspector leads with the one-sentence reason (tier rule + code
  citation), then small bars for the within-band factors, so a coordinator
  reads WHY in one glance and can mark "not an issue".
- No per-project tuning surface (decided 2026-07-01): the firm standard
  lives in `lib/clash_score/defaults.py` so bands stay comparable across
  projects. A firm-level (not per-project) editor can come later.
- Two working norms: run detection after modeling sessions, not during (a
  half-routed duct scores as a transient Critical), and treat the
  `escalate_candidate` flag as the earliest-possible conversation with the
  structural engineer (longest external lead time).

**Machine learning is a later phase, not day one.** Every "not an issue" and
"real issue" verdict is stored as a labeled feature vector. After a few
projects that becomes a training set for an offline relevance model (the
research shows over 80% precision is achievable), which feeds one more input
into the score. This runs in CPython, not in Revit's IronPython, and humans
always stay in the loop. We design the schema for it now (persist the feature
vectors), but we do not build it until the labeled data exists.

---

## 7. Data model

Extends today's clash dict rather than replacing it, and shapes each clash as
a BCF Topic so export/import is native. Storage stays as JSON in the shared
folder, keyed by project hash, exactly as today.

**Keep** (from today): `id`, `test_id`, `kind`, `ref_a` / `ref_b`,
`midpoint`, `first_seen_run` / `last_seen_run`, `comments[]`, `history[]`, and
the whole new / persisting / resolved / reopened merge behavior. Note that
`fingerprint` and `seq` are added at merge time (not by the clash factory),
and `gap_inches` is present only on soft clashes; do not treat them as
universal factory fields.

**Add to each clash** (Revizto/Jira-shaped, all BCF-mappable): `priority`
(Blocker / Critical / Major / Minor / Trivial), `assignee`, `watchers[]`,
`tags[]`, `deadline`, `reporter`, `topic_type`, `group_id`, and the computed
location fields `level`, `grid`, and `zone` (level now arrives via ref
enrichment; grid/zone still to derive, see open question 6).

**SHIPPED 2026-07-01 (importance):** each clash carries an `importance`
block stamped at merge time by `lib/clash_score` (band, rule id, score,
one-sentence reason, per-factor `brk` bars, suppression state, confidence,
and a persisted `features` dict, which is the feature vector the future ML
phase trains on). Reserved per-run keys `penetration_depth_in` /
`overlap_volume_cf` are materialized as null via `merge._PER_RUN_FIELDS` so
depth/volume backfill later with no migration; `tolerance_inches` is
stamped per clash so scoring needs no test-library lookup. A per-clash
`suppress_override` (true/false) beats every suppression rule.

**Add to each element reference** (the load-bearing change): `unique_id`
(Revit `UniqueId`) and optionally `ifc_guid`. Today the element reference
carries only an integer `element_id`, which is not stable enough to join web
to Revit reliably. Linked-instance elements get `unique_id` plus a
link-transform hash, matching our fingerprint's need to tell link instances
apart. **SHIPPED 2026-07-01 (enrichment):** refs also carry `sys_class`,
`sys_name`, `sys_abbr`, `dims_in`, `ins_in`, `slope`, `level`, and
`discipline`, captured in `clash_detect/enrich.py` at detection time (all
nullable, fingerprint-safe, refreshed every run via the merge ref
replacement).

**Groups** (REVISED 2026-07-02: no separate groups.json — no cross-file
atomicity exists, so groups live as the top-level `groups` key inside
clashes.json): each group is `{id, created_at, created_by, axis
(element|cluster|manual), anchor, title, title_locked, status, assignee,
priority, member_ids[], suggested_ids[], needs_review, lineage, history[],
comments[], rep_clash_id, rollup}`. Shipped in `lib/clash_group/`; see
`CLASH_GROUPING_DESIGN.md` section 3.4 for the full schema. A group rolls up
the max open member band/score and resolves only when all members are
decided.

**Viewpoints** become BCF viewpoint shaped: camera (perspective or ortho),
the selected pair, the red/green coloring, visibility exceptions, and the
section box as clipping planes.

---

## 8. Element identity and the round-trip (the load-bearing seam)

This is the one contract everything depends on, and the one thing to prove
before building anything else.

Standardize on Revit **`UniqueId`** as the universal key. It equals the
industry-proven external id and is stable across re-exports. Stamp it in
three places at export time: the glTF node `extras`, the viewer's element
metadata, and any BCF viewpoint's element reference. Optionally also compute
the IFC GUID at export (from the UniqueId, no IFC export needed) for clean
interop with outside tools.

Today's export writes an integer `element_id` into the glTF node `extras`,
and the clash record's element references carry the same integer. That is not
enough. The fix: also write `UniqueId` on both sides. Then a pick in the web
viewer joins directly: clash to its two `UniqueId`s to viewer entities to
"color A red, B green, isolate the pair", and clash to `UniqueId` to Revit
element for Show in Revit. This join is the difference between "fly to a dot"
(what the viewer does today) and "isolate the actual clashing pair" (what
Navisworks does).

The three-way jump, all keyed on `UniqueId`:
- Clash to viewpoint: replay the stored BCF viewpoint in the embedded viewer.
- Viewpoint to Revit (SwitchBack): a message to the pyRevit host raises an
  external event that selects and zooms the elements in the active Revit view.
- Revit to clash: a selection hook posts the current element ids and opens
  the matching clash record.

Also preserve the existing coordinate contract exactly: clash midpoints are
in host feet, the glTF carries the offset and feet-to-meters scale, and the
viewer's host-to-viewer transform is the inverse of the export transform.
Lock this with a fixture test and reuse it verbatim.

---

## 9. Tech stack

- **Engine**: xeokit SDK 2.x (WebGL today; the engine adopts WebGPU later on
  its own timeline, which is a free future upgrade since we run inside a
  Chromium WebView2 we control). Fed by our existing glTF export converted to
  xeokit's XKT format, split per discipline/link into chunks with a manifest
  so a 2GB model does not blow the WebView2 memory. Re-export only changed
  links.
- **Detection**: stays in Revit. Keep `clash_detect/` (hard via
  `ElementIntersectsElementFilter`, host-vs-link and link-vs-link via the
  inverse-transform solid path, soft via AABB inflation). A later phase adds
  a browser-side geometry engine (three-mesh-bvh over the already-exported
  meshes) purely for clearance distance, penetration depth, and headless
  re-runs.
- **Export pipeline**: keep and extend `clash_export` (the CustomExporter to
  glTF). Add real geometry instancing (emit each family symbol's mesh once
  plus per-instance transforms, the biggest size/speed lever for MEP), stamp
  `UniqueId` on every node, and optimize with meshopt plus quantization (not
  Draco, which is lossy and wrong for clash fidelity).
- **Packaging**: one web front-end with a thin bridge that auto-detects its
  host. WebView2 present means embedded-in-pyRevit (primary). Otherwise HTTP
  to pyRevit Routes means Tauri standalone (same WebView2 engine as embedded,
  so zero rendering divergence, and far lighter than Electron). Otherwise a
  pure offline file (the self-contained HTML share we already ship).

---

## 10. What we reuse vs rebuild

From the code audit, the split is clean and most of the value transfers.

**Reuse directly (pure Python, already portable):** the data model and
factories (`clash_core/models.py`), the fingerprint (`identity.py`), the
merge logic (`merge.py`), dedupe, the browser filter predicate
(`browser_filters.py`), bulk edit, history formatting, filter presets, the
category table, the whole JSON persistence/config/project-hash layer, the
glTF writer (`clash_export/mesh.py` and `gltf.py`), all three report builders
(`bcf.py`, `excel_summary.py`, `html.py`), the self-contained HTML packer
(`clash_share/bundle.py`), and the entire three.js viewer plus its standalone
overlay (already a working browser clash-review UI today).

**Rebuild or keep inside Revit (Revit / IronPython bound):** all of
`clash_detect/` (detection stays in Revit anyway), the CustomExporter and
geometry/material resolvers (`clash_export/custom_export.py` and
`revit_geometry.py`), all of `clash_view/` (Revit views, section boxes,
image export, overrides), and the pushbutton WPF forms (replaced by the web
front-end).

**The one gap to close:** today the viewer receives only clash midpoints, not
element ids, so it cannot highlight the actual clashing geometry in the
browser. The glTF nodes already carry `element_id`; adding `UniqueId` and
building the join is the unlock (section 8).

---

## 11. Phased roadmap

The trap to avoid, in Nathan's words, is a loop of small fixes that never
converges. That comes from starting with UI polish. We start instead with the
two things that could actually sink the rebuild, prove them on a real model
in weeks, and only then build UI.

**Phase 0 - de-risk the two killers (spike, about 2 to 3 weeks).** Prove
(a) that `UniqueId` stamped into the export survives export to XKT to a web
pick and back to a Revit SwitchBack on a real federated project, and (b) that
`convert2xkt` split per discipline loads our real 2GB model in a WebView2
pane without running out of memory. If either fails, we learn it now, cheaply,
before building anything on top. Nothing else matters until these work.

**Phase 1 - parity on the new stack.** Port the pure-Python parts that
transfer (models, identity, merge, dedupe, filters, reports, glTF writer, the
standalone viewer overlay). Ship: run tests, merge into scored/grouped
`clashes.json`, the Navisworks-style Results grid (sortable, status chips,
assign/comment/history), the embedded context view with clash-isolate and
Show in Revit, and the existing BCF/Excel/HTML reports. This replaces the
current tool at parity plus the element-identity join.

**Phase 2 - the differentiators (the point of the rebuild).** The
noise-suppression rules layer, the discipline-matrix test UI on saved search
sets, the 0-to-100 importance score, two-axis auto-grouping, and the
"Important" tab and presets. This is where we beat Navisworks.

**Phase 3 - issue-tracker depth.** Priority / assignee / watchers / deadline /
tags, the automation-rules auto-assignment, BCF import, and "freeze
already-touched groups on re-run".

**Phase 4 - team and scale.** The Tauri standalone shell over pyRevit Routes,
graduating the shared JSON to PocketBase if concurrent editing demands it,
and the live dashboard.

**Phase 5 - machine-learning relevance.** Only after labeled data exists.
Train offline in CPython on the feature vectors we have been persisting, and
feed the probability back as one more score input.

Every phase ships a usable tool. Phases 0 and 1 reach parity; 2 and 3 reach
Navisworks/Revizto-class power; 4 and 5 are scale and polish.

---

## 12. Risks and how we de-risk them

1. **Element identity from a Revit glTF export.** Highest risk; the whole
   round-trip depends on it. De-risked first, in Phase 0.
2. **2GB scale in a WebView2 tab.** Achievable with tuning, not turnkey.
   De-risked in Phase 0 with the split-and-chunk load; the embedded pane is
   scoped to clash-neighborhood review, not full-federation walkthrough.
3. **The coordinate contract.** The host-to-viewer transform is the most
   load-bearing runtime contract. Lock it with a fixture test and reuse it
   verbatim.
4. **Prioritization credibility.** The differentiator is noise reduction, not
   math. Sequence it first among the new features and keep the score
   transparent so coordinators trust it.
5. **Engine license.** Settled: internal-only makes AGPL a non-issue, with
   @thatopen/Fragments held as the MIT fallback.

---

## 13. Feature parity checklist

Every current-tool capability and where it lands in the rebuild, so nothing
is lost. If it is not on this list, flag it.

> **COMPLETED 2026-07:** the legacy suite this table absorbs (Run Clash
> Test, Clash Browser, Test Library, Reports, Settings pushbuttons) has
> been deleted from the panel. The two shipping tools are the Clash
> Detection web app and the 3D Viewer. References to the old buttons in
> this spec are historical plan language.

| Current capability | Rebuild home |
|---|---|
| Run Clash Test (pick tests, run detection, merge) | Tests: discipline matrix, run |
| Per-run trade filter | Tests: run bar |
| Per-run link include/exclude toggle | Tests: run bar |
| Run diagnostics (element census, detection/dedupe counts) | Tests: run log |
| 4 broad default tests + auto-trade tagging | Tests: Test Library defaults |
| Test Library (firm + project overrides, safety model) | Tests: Test Library |
| Fingerprint identity + new/persisting/resolved/reopened merge | Data model (kept, made visible on Home) |
| Clash Browser list + virtualization + 2000 warning | Clashes: results grid |
| Trade / status / test / search filters | Clashes: left rail + presets |
| Group-by (trade/test/status/level) | Clashes: group-by control, extended with system/grid/proximity (level/grid/zone must be computed at detection time; they do not exist today) |
| Status / trade / comment edits with immediate persist + history | Clashes: inspector + issue detail |
| Bulk operations (status / reassign / mark resolved) | Clashes: grid toolbar |
| History dialog | Clashes: inspector Comments/History card |
| Filter presets (built-in + user) | Clashes: left rail presets |
| Show in 3D (Clash Navigator section box + red/blue) | 3D: context view (plus in-browser highlight join) |
| Save Viewpoint (camera + section box + PNG) | 3D + data model: BCF-shaped viewpoints |
| Reports: BCF 2.1 | Reports (plus element-id in viewpoints, plus import) |
| Reports: Excel | Reports |
| Reports: self-contained HTML | Reports |
| 3D Viewer (glTF export, fly-through) | 3D: open the model |
| Category / workset visibility toggles | 3D: visibility panel |
| Clash markers + click-to-fly | 3D: markers + context view |
| Open in Browser (self-contained share) | 3D: share, and the standalone mount |
| Viewer pop-out window + borderless fullscreen | 3D: open the model |
| Viewer environment/render controls (sun, edges, ground, minimap, quality, perf) | 3D: carry to xeokit or explicitly drop |
| Settings: shared-folder path | Settings |
| Settings: link role mapping | Settings |
| Settings: disciplines roster | Settings |
| Settings: display-name override + folder/README shortcuts | Settings |
| Soft / near-miss clashes with gap ranking | Tests + Clashes (kept) |
| Clearance clashes (stub today) | Detection phase 2 (independent H/V offsets) |
| new: importance score + noise rules + auto-grouping | Important clashes (section 6) |
| new: issue fields (priority/assignee/watchers/deadline/tags) | Issue detail |
| new: saved reusable sets (Search-Set style) | Tests: saved sets |
| new: level/grid/zone computed at detection time | Detection + data model |
| new: live dashboards | Home |
| new: clean home / navigation shell | Home + nav rail |
| new: element-highlight round-trip (clash to element to Revit) | Identity seam (section 8) |

---

## 14. Open questions (decide as we reach them, not blocking Phase 0)

These are deliberately deferred. None of them block starting Phase 0.

1. Clearance detection timing: is MEP-vs-MEP clearance (with independent
   horizontal and vertical offsets) a Phase 2 must-have or later?
2. BCF interop depth: do we actually round-trip BCF with outside consultants,
   or is BCF mostly an export nicety? This sets how much we invest in BCF
   import and IFC-GUID fidelity.
3. Exporter language: is a small compiled C# CustomExporter add-in acceptable
   for export speed and instancing, or do we keep everything in IronPython to
   avoid a build step?
4. DECIDED (2026-07-01): the importance config is ONE firm standard across
   all projects (`lib/clash_score/defaults.py`); no per-project tuning.
   Refinement happens by editing the standard after the calibration run
   (research doc section 13). Related decisions the same day: band words
   appear in external reports, numeric score stays internal; the
   `escalate_candidate` flag ships in v1 (C1 rule); "run after modeling
   sessions, not during" is the working norm for transient-Critical noise;
   the sprinkler-vs-conduit rung order ships at the Korman baseline.
5. DECIDED (2026-06-30): assignment stays trade-primary (routing to a
   discipline's queue), because engineers rotate through a project. An
   optional per-person assignee plus watchers can layer on later. See the
   decisions table (section 2).
6. DECIDED (2026-06-30): location is nearest grid intersection plus level.
   Grid is the nearest grid line on each axis at the clash point (e.g.
   "B/3"); level is the element's associated level, falling back to the clash
   elevation against the level planes. Zone is deferred (rooms and spaces are
   used inconsistently, sometimes faked, so we do not depend on them); a
   project with no grids falls back to coordinates plus level. See the
   decisions table (section 2).
