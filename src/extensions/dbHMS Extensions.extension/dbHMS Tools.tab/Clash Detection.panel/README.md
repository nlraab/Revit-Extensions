# Clash Detection

dbHMS's in-Revit clash coordination platform: find, rank, group, discuss,
assign, and resolve interferences without leaving Revit, built for the
firm's normal project shape (MEP host model + linked architectural model +
sometimes a linked structural model).

> **Read this file before making serious changes** to anything under
> `dbHMS Tools.tab/Clash Detection.panel/` or `lib/clash_*/`. The durable
> design rationale, contracts, and gotchas are in the **Design notes &
> decisions** appendix at the end of this file; the full build history is
> in git.

---

## The two tools (all that ships)

The panel contains exactly **two pushbuttons**, both WebView2 web apps.
The legacy WPF suite (Run Clash Test, Clash Browser, Test Library,
Reports, Settings) was deleted 2026-07 after the Clash Detection web app
absorbed every one of their jobs.

1. **Clash Detection** (`Clash Detection.pushbutton/`, page
   `web/coord.html`) - the coordination hub. Tabs: Home (health +
   meeting agenda), Test Library, Clash Browser (the "Quiet Ledger"
   issue/clash grid + inspector), 3D Viewer (federated view), Reports,
   Settings (folder + link roles). Firm default tests ship in
   `default_tests.json` next to its `script.py`.
2. **3D Viewer** (`3D Viewer.pushbutton/`, page `web/viewer3.html`) -
   the standalone federated model viewer (glTF snapshot export, category/
   workset visibility, share-to-browser, clash markers). Will
   increasingly interoperate with the Clash Detection app.

Both host their page in WebView2 with the shared init-retry pattern
(fresh profile on 0x8007139F-class failures) and draw their own
resize-grip dots (the browser surface covers WPF's native grip).

## The engine (`lib/clash_*`)

| Lib | Job |
| --- | --- |
| `clash_core` | models, merge (fingerprint identity, statuses survive re-runs), persistence (`*_at(folder)` helpers), **binding** (the folder-in-model state), categories, users |
| `clash_detect` | detection: hard/soft engines, grid AABB broad phase (`broadphase.py`), tessellation + mesh distance, per-ref MEP enrichment (`enrich.py`), test runner |
| `clash_score` | the importance engine: Layer A noise suppression + constraint-first tier rules -> Critical/Major/Minor + one-sentence reasons (firm standard in `defaults.py`; see Design notes below) |
| `clash_group` | Layer C sticky issue groups: participation-anchored element stars, density-gated racks, successor adoption, group ops (see Design notes below) |
| `clash_report` | the export deliverables, all band-aware off one shared `report_model` (summary + rows so every format and the on-tab preview agree): **BCF 2.1** (industry exchange; band → Priority + reason in the description), formatted **Excel** (`excel_summary`), an interactive + print-clean **HTML** report (`html`, also the source the host prints to **PDF**), and the pre-meeting agenda **digest**. Driven by the web app's **Reports tab** (`report:` bridge → native Save As; PDF via a hidden off-screen WebView2 `PrintToPdfAsync`, falling back to HTML+browser) |
| `clash_export` | Revit -> glTF snapshot export (shared by both tools) |
| `clash_identity` | the federation key joining clash refs to exported glTF nodes |
| `clash_view` | Revit-side viewpoint/thumbnail capture helpers |
| `clash_share` | share-to-browser packaging for the 3D Viewer |

Detection -> `merge.merge_runs` -> `clash_score.score_all` ->
`clash_group.regroup_all` -> one atomic write of `clashes.json`.

## State model: one folder per project

A project remembers exactly **one** thing: the absolute path of its
clash-data folder, stored INSIDE the model via Extensible Storage on
`ProjectInformation` (`lib/clash_core/binding.py`). It travels with the
.rvt, so every teammate who opens the saved/synced model gets the same
folder, and changing it changes it for everyone. All data is read and
written directly in that folder via `persistence.*_at(folder)`:

```
<the folder the user picked>/
    clashes.json          # clash database + issue groups
    project.json          # display name, disciplines, link role map
    test_overrides.json   # per-project test tweaks
    viewpoints/           # web-viewer captures (JPEG; legacy captures are .png)
        <clash-id>.jpg        # per-clash image: A red / B blue, ghosted context
        issue_<group-id>.jpg  # per-issue aggregate photo
        issues.json           # issue photo manifest (member-roster hash per group)
```

Resilience layer: every binding this machine sees is mirrored to
`%APPDATA%/dbHMS_clash/bindings.json` (keyed by hashes of ALL the model's
identity paths - cloud, workshare central, PathName). Reads fall back to
the registry when the model copy is missing (closed without saving), and
the tool re-heals the model binding on open. ES only persists on SAVE /
Sync with Central - the Settings tab warns until then. The schema is
always built before reading (`_schema(create=True)`): opening a .rvt does
not reliably register an ES schema in a fresh session.

Key properties: the folder is the truth; no folder set -> nothing shown;
no shared root, no hash folders, no migration; reads never create.

## Testing

CPython 3 unit tests (`.\run_tests.ps1`) cover the pure engine: scoring
(tests/test_clash_score.py), grouping (test_clash_group.py), broad phase
(test_clash_broadphase.py), merge/identity/binding/BCF/digest, plus the
extension integrity suite (folder shape, bundle layout, telemetry, parse
checks). Revit-API paths (ES, detection filters, capture) are verified in
Revit by hand - see each module's docstring for its manual check.

## Design notes & decisions

This appendix pins the durable contracts, invariants, and hard-won gotchas
behind `lib/clash_*` and the 3D Viewer tab. It is the merged, deduped
distillation of the former design documents; the point-in-time build
narratives, calibration measurements, and resolved "decisions for Nathan"
that produced these rules live in git history. Everything under `lib/clash_*`
must parse and run under **both IronPython 2.7 (Revit) and CPython 3 (tests)**;
`defaults.py` files are pure data. Editing anything under `lib/` requires a full
pyRevit reload / Revit restart to take effect.

### Identity: the fingerprint (`clash_core/identity.py`)

A clash is keyed by a 16-char (`FINGERPRINT_LENGTH=16`) SHA-1 digest over
`test_id | sorted (source:element_id) element pair | 1-ft-bucketed midpoint`.
The pair is sorted so ref-swap is invariant. The midpoint is bucketed at
`SPATIAL_BUCKET_FT = 1.0` ft/axis using round-**half-away-from-zero**
(`_round_half_away`, mirrored by the scorer's own rounder). This is deliberate:
IronPython 2.7 rounds half-away, CPython 3 rounds half-even, and the built-in
`round()` would detach ~1% of clashes sitting on a .5-ft coordinate.

- **Never change `SPATIAL_BUCKET_FT`, the bucket, the midpoint, or anything the
  fingerprint hashes on a live project** — every stored clash would re-fingerprint
  and lose its status/comments/history. Better geometry goes in NEW fields (e.g.
  `overlap_centroid`), never by reusing `midpoint`.
- **Clearance rows** (`kind == 'clearance'`, Phase 4) call
  `clash_fingerprint(..., include_midpoint=False)`: the pair + test_id already
  identify one intruder-vs-zone violation, so dropping the midpoint stops a nudged
  intruder from re-keying as it slides along a large zone. The default
  (`include_midpoint=True`) path stays byte-identical to legacy fingerprints.
- `UniqueId` is the universal join key across a clash ref, its exported glTF node,
  and its Revit element. Element refs also carry nullable, fingerprint-safe
  enrichment (`sys_class`, `sys_name`, `dims_in`, `ins_in`, `slope`, `level`,
  `discipline`) captured in `clash_detect/enrich.py`.

### Merge lifecycle & data-loss guards (`clash_core/merge.py`)

`merge.merge_runs` preserves human work across runs via four outcomes:
`new` / `persisting` / `auto_resolved` / `reopened` (a re-detected resolved clash
reopens with a history entry). `fingerprint` and `seq` are stamped at merge, not
by the clash factory.

- **Classify every new field into exactly one bucket at review time.** Per-run
  DERIVED fields (`merge._PER_RUN_FIELDS`: midpoint, gap, contact points,
  `tolerance_inches`, `penetration_depth_in`, `overlap_bbox_in`,
  `overlap_volume_cf`, `overlap_centroid`, `pen_class`, `geom_method`, and the
  clearance fields `clearance_rule`/`spr_clearance_in`/`zone_cap_ft`/
  `intrusion_depth_in`/`xref_hard`) refresh unconditionally each run and backfill
  re-appearing clashes with no migration. Durable human state (`status`,
  `comments`, `thumb`) must **NOT** be in that tuple — membership would null it
  every run. Auto-resolved rows never gain per-run keys, so readers must use `.get()`.
- A **suppressed clash is never dropped**: it keeps fingerprint/status/comments,
  still scores a full would-be score, and hides behind a reversible "Suppressed (n)"
  toggle; per-clash `suppress_override` beats every rule.

### Importance scoring (`clash_score`) — fully deterministic

`score_all(clashes, config=None)` (alias `rescore_all`) runs AFTER `merge.merge_runs`
on the MERGED list, stamping `clash['importance']` in place and returning a
band-count summary. **Never call it on raw detection rows** (merge's new-clash literal
drops a raw-row score). Pure data, zero Revit imports; re-runs in milliseconds from
stored features after any config change with no detection pass. There is **no AI in
scoring** — an advisory-AI layer was designed but deferred; doctrine forbids any model
from mutating band/score/rule.

**Band / score doctrine (violating any is a bug):**
- **A named tier rule decides the band; the 0-100 score only maps it onto the UI scale.**
  Evaluation is ordered, first-match-wins: Layer A suppression
  (`R-SELF → R-NEST → R-NOT-OURS → R-SYS → R-FIELD → R-GRAZE`; suppressed rows are
  still fully scored) then the tier ladder into Critical/Major/Minor. Clearance rules
  (`C-NEC` / `C-NEC-W` / `M-NEC-PROT` / `M-SPR`, keyed off the test id via
  `clearance_rule`) dispatch at the top of `_tier`. The M1 Major band is partitioned
  into 8 **frozen** sub-rule ids (`M1-EQ-EQ`, `M1-FIX-EQ`, `M1-FIX-CURVE`, `M1-FP`,
  `M1-SLOPE`, `M1-EQ-SYS`, `M1-RIGID`, `M1-XING`) dispatched mover-class-first; ids are
  frozen because they key M4 hysteresis (`prev_rule`) and every migration.
- Bands: **Critical `(70,99)` / Major `(40,69)` / Minor `(8,39)`**. The **70/40 cutoffs
  are also hardcoded in `coord.html` (four sites)** — move them in both. Sub-scores map
  proportionally within a band via `min(1, raw / raw_realistic_max)`,
  `raw_realistic_max = 48.0`; **tune only that constant against the calibration
  histogram, never the cutoffs.** A clamped sub-score orders WITHIN a band and can never
  walk a clash across a cutoff. Promotion pressure earns a new named rule, not a cutoff move.
- **The firm standard is `defaults.py`, not a per-project knob** — one standard so
  "Critical" means the same everywhere; `config` overrides exist for tests only. Code
  editions are pinned in comments (NEC 2023, IMC 2021, IPC 2021, NFPA 13 2022).
- **A band change between runs must have a visible cause.** Cluster thresholds use
  hysteresis (`cluster_major` 20 fires, `cluster_major_release` 12 releases) so a
  resolved neighbor can't flap a band; the reason names the rule. Frozen invariant:
  **no row moves UP a band on a retune.**
- `DEFAULTS['rev']` (config rev) is currently **8**, stamped as `config_rev`. A band
  shift across a rev bump is a **rule change, not resolved work** — rescore at a run
  boundary with a before/after report. **Skew guard:** the engine never restamps a record
  whose stored `config_rev` exceeds its own `rev` (warns instead); a rescore-to-current
  runs on tool open when stored rev < engine rev.

**The mover model.** Importance is the cost of moving whatever must move. Structure and
architecture are **never on the movability ladder** — they are fixed context flags
(`_fixed_kind` decides by CATEGORY first, then link role) that select which tier rule
fires. Between two movable MEP elements the lower-rigidity (0–5 ladder) one moves.
Structural elements delivered inside the arch link, and arch-modeled walls/floors flagged
`is_structural`, take the structural path, not the sleeve demotion.

**Mounted vs. equipment split (the big domain fix).** Heavy `EQUIPMENT_CATS`
(Mechanical/Electrical Equipment) are rigidity 4; hosted fixtures (`MOUNTED_CATS`:
Plumbing/Electrical/Lighting Fixtures, Air Terminals) are rigidity 2, class `fixture`.
Rule **N3** demotes a fixture/equipment mover merely mounting in/on its host surface
(`MOUNTING_SURFACE_CATS`) to Minor `mounting_check`; **N4** demotes gravity fittings
seated at their `DRAIN_SURFACE_CATS` slab (gap 0 by design) to Minor. Switchgear
(`SWITCHGEAR_NAME_WORDS`) is held OUT of N3 — a panel buried in a wall is a real layout
problem. **C3** (Critical) requires BOTH sides in `NO_SLACK_KLASSES`, so a rigid-but-
reroutable big duct vs. gravity is Major, not Critical.

**Suppression & reasons.** Suppression demands proof; demotion is the default for
suspicion. A suppression rule fires only when every feature it tests is non-null;
missing data degrades to a defensible mid-band with `confidence='degraded'` (never
silence, never a false Critical). `R-NEST` suppresses only on `super_root_id` proof;
name heuristics (`N-PT`, `N-DUP`) demote with a flag and never suppress. `R-GRAZE`
suppresses only a MEASURED boolean overlap (`geom_method=='boolean'`) that is BOTH
shallow (`penetration_depth_in < graze_floor_in = 0.375`) and small
(`overlap_volume_cf < graze_max_vol_cf = 0.02`) with mover rigidity ≤ 3 and not
structural — never a bbox proxy. Reasons are **composed, never concatenated**:
`_compose(rule_id, ctx)` returns 3 sentences (WHAT / WHY / ACT) plus ≤1 qualifier;
a null fact drops its clause (never renders "None"); headline ≤ 90 chars; a code
citation appears **only when the triggering fact was measured** (unmeasured cases use
"verify…" prose, no clause number). That sentence, not the number, is the trust surface.
`relevance_class ∈ {error, deliberate, pseudo, artifact, field}` is derived from the rule id.

**The `importance` block** carries `v: 1`, `config_rev`, tier/rule/band/score, the composed
reason, a `brk` array of `{k, v}` sub-terms (Mover constraint / Size / Geometry / Volume /
Congestion / Code), suppression fields, `flags`, `confidence`, and a persisted `features`
vector (the fired rule is itself a stored feature) reserved for a future offline-CPython ML
relevance model. Band words appear in external reports; the numeric score stays internal.

### Grouping (`clash_group`) — sticky issue containers

`regroup_all(clashes, groups, run_iso=...)` runs after `score_all` in both pipelines,
returns a NEW groups list (existing dicts deep-copied, never aliased), and stamps
`group_id` on members. It reads only stamped `importance` blocks; `clash_score`/M4 are
never touched. Pure data, zero Revit imports.

**A group is an issue** — a coordinator-named, assignable ticket — not a re-derived query
result.
- **Durable identity lives on member clash ids** (immortal — merge never deletes records,
  so member ids can never dangle); the group id is an immortal `uuid4`. Member handles are
  clash ids, **never fingerprints**.
- **Sticky rosters:** existing groups keep every member every run, unconditionally —
  grouped clashes are never reclustered, so the recluster-then-rematch problem never
  arises. Formation only ever assigns **currently-ungrouped, unsuppressed, open** clashes.
- **Splits and merges are human acts only**, never automatic; manual groups are exclusive
  and invisible to formation. The `drifted` flag (a spatial/manual group whose open members
  form 2+ components) only *invites* a manual split.
- Element/anchor keys use `(source, unique_id)` — never `fed_key` (embeds link-placement
  origin) and never display facets (sys_name/level). Fallback for pre-`unique_id` data:
  `source|eid|element_id|link_doc_title`. **A group's anchor key is frozen at formation and
  never recomputed** — later geometry/scoring shifts can never re-key a live group.

**Formation passes (ordered, deterministic bar new uuid4s):** P1 sticky refresh → P2
successor adoption (Tier 1 = same `test_id` + same unordered element pair silently re-adopts
a member re-keyed across the 1-ft bucket; Tier 2 = same test + same category pair within
`adopt_radius_ft`, suggestion-only) → P3 anchor-key claims (the one sanctioned silent join;
joins a beyond-Open group with `needs_review`) → P4 density-gated spatial clusters (cores by
stamped `cluster_n`, union-find over the grid; borders attach to exactly one component and
**never bridge two**, which is what makes false rack-merges structurally impossible, not
threshold-lucky) → P5 participation-anchored element stars (star-shaped around the
higher-participation element, **never transitive**) → P6 adjacency (suggest into curated
groups, auto-join untouched ones) → P7 lifecycle → P8 stamp. Ungrouped residue is a
degenerate single-member issue on the agenda, never hidden.

**Lifecycle.** A group with zero open unsuppressed members auto-resolves and reopens when
any member reopens; groups never auto-close (status is human-owned), they just leave the
band-driven agenda when members demote. A member reopening under a human status
(Reviewed/Approved) sets `needs_review` rather than stomping the decision. Suppressed
members keep their roster seat but stop counting toward band/open rollups. **Rollup band =
MAX band among open unsuppressed members** (never inferred from scores); the group `reason`
is its own composed narrative (`rollup.reason`/`score`/`band`, rebuilt on every rescore),
not a copy of one member's.

**Churn identity note.** `run_iso=None` (a Browser-side rescore after a group edit)
intentionally skips churn detection so the adoption cascade never re-fires; do not synthesize
a fallback run stamp. Tier-1 rekeys are excluded from the "+N new" badge — a member merely
drifting across the 1-ft bucket is the false-alarm noise the successor cascade exists to
suppress.

**Storage & data-loss guard.** Groups persist as the top-level `"groups"` key **inside
clashes.json** (one atomic write covers members + rosters + statuses; a separate file would
have no cross-file atomicity — there is no `groups.json`). Both run pipelines' `new_data`
literals must carry `'groups'`, and `read_clashes` defaults it to `[]`; the silent-drop of an
unknown top-level key on rebuild is the real data-loss bug class here, and an integrity test
asserts the key round-trips both pipelines.

**Constants** (`clash_group/defaults.py`, firm-standard, frozen until real-project
calibration): `core_cluster_n = 6` (neighbors within **5 ft**), `cluster_eps_ft = 6.0`,
`cluster_min_members = 10`, `anchor_min = 3`, `adopt_radius_ft = 10.0`,
`anchor_span_segment_ft = 80.0`, cluster caps `max_span_ft`/`max_members` = 50/150,
`congested_cluster_n = 20` (mirrors clash_score's M4 fire threshold — keep in sync). Note
`cluster_n` itself counts neighbors within **5 ft** (`clash_score`'s `cluster_radius_ft`),
distinct from the 6-ft grouping grid. **The member cap is formation-only**, so adjacency
growth past 150 is unbounded across runs by design — watch it.

### Boolean pair geometry, dedupe & other gotchas

- **Silent zero is a bug class.** The link-role misconfiguration that silently stored zero
  arch/structure clashes is why every pass that can produce zero reports counts in run
  diagnostics and the run modal shows a loud alarm; the suppressed-link warning fires even
  when all links are unloaded, and a `link:` test with an empty `link_role_map` blocks with a
  "map your links first" prompt.
- Rated assembly = `fire_rating_hr >= rated_wall_min_hr` (**1.0**); a penetration is `full`
  vs `partial` at `pen_full_frac` (**0.85**) of captured thickness (NOT a fixed tolerance).
  Sleeve-size limits `sleeve_rect_max_in` / `sleeve_round_max_in` = **16.0** in.
- Score-term saturations: `size_full_in` = **30.0**, `vol_full_cf` = **1.0**,
  `congest_full_n` = **20.0**. `N-PT` scope is option A
  (`n_pt_include_wall_devices=True`, `n_pt_include_typical=False`).
- Boolean pair geometry (`clash_detect/pairgeom.py`) is bounded by `GEOM_SOLID_CAP=8`/side,
  `GEOM_TIME_BUDGET_S=300.0` s run-wide, and a `_SOLID_CACHE_MAX=1200` solid cache; any
  failure/over-budget falls back to the free `overlap_bbox_in` tier.
- Layered penetrations are collapsed pre-merge
  (`clash_core/dedupe.collapse_layered_penetrations`) so one physical run through a stacked
  floor/roof assembly is one row (keeping the most significant layer), while a riser through
  separate storeys stays multiple penetrations.
- Clearance rows also assign to the test's `default_assignee` (the zone owner), not the
  intruding trade.

### 3D Viewer tab: the review cockpit (`web/coord.html`)

The **3D Viewer tab** is the meeting cockpit for reviewing clashes: flip the Browser's
filtered list, see each clash in context, record the decision in place. It is distinct from
the standalone **3D Viewer** button (`web/viewer3.html`, the walkthrough/share tool) and
deliberately does not grow fly-through, sun studies, or render tiers. Headline invariant: the
live view uses the **same visual recipe as the saved clash photos** (element A solid red, B
solid blue, surroundings ghosted), so card image and on-screen model always match.

**Scene state machine (load-bearing).** Three layers:
- **Baseline** — the user's own state (visibility, manual hides, sections, camera); the state
  every Esc / Show all returns to.
- **Review overlay** — what clash focus paints (pair colors, two-tier ghost, hidden set, auto
  section box). Recomputed per selection via batch ops on precomputed id sets, and **fully
  removed on Esc / Show all / deselect — never persisted into baseline.**
- **Capture mode** — the existing enterCap/exitCap batch.

Arbitration invariants: (1) a capture batch may not start or continue while the 3D tab is
active (pauses/resumes on tab-leave/idle); (2) the inspector's live one-shot capture is
disabled while the tab is active (the live view *is* the context view); (3) **enterCap is
illegal while a review overlay is painted** — flatten to baseline first, so a batch can never
restore overlay state as if the user owned it; (4) data re-pushes are diff-applied — never
rebuild the list DOM wholesale, reset selection/scroll, or touch the camera.

**Bridge contracts** (same string envelope as the rest of the app):
- `clashop:{op: status|assign|deadline|comment, clash_id, ...}` → host applies via
  `clash_core.bulk_edit` (`apply_status`/`apply_trade`/`apply_deadline`, un-assign supported)
  or `models.make_comment`, one atomic read-modify-write of `clashes.json` →
  `clashopdone:{ok, clash_id}` → delta re-push. Hardening: **busy-nack**
  (`clashopdone:{ok:false, busy:true}` while an op is in flight; the page queues and retries so
  no meeting decision is lost during the post-run export tail), **optimistic UI** (page mutates
  its local row and reconciles by id on the ack — never a full `loadClashes` reset that clears
  selection), and **coalescing** (rapid edits debounce into sequential writes, since each is a
  full RMW of a file that may live on a share).
- `showinrevit:{keys:[fed_key...]}` → resolve via UniqueId, select + zoom in the active Revit
  view, then minimize the tool window. Host elements select directly; linked elements zoom-to
  and select the *link* (Revit cannot select inside a link). No ExternalEvent needed while the
  window is modal.
- `viewpoint:{clash_id, camera}` → camera saved on decide so reopening lands in the view it was
  decided in; full BCF-shaped viewpoint JSON (camera + section box + view mode + see-through)
  restores on select via a session-vs-saved split so a restored view never leaks into the next
  clash.
- The WebView2 default context menu is suppressed app-wide so a stray right-click can't hit
  Chromium's "Reload" and restart the app mid-meeting.

**Coordinate round-trip.** Clash midpoints are in host feet; the glTF carries the offset and
feet→meters scale, and the viewer's host→viewer transform is the exact inverse of the export
transform (locked by a fixture test — reuse verbatim).

**Integration with the Clash Browser (one app, five "one"s):** one **selection** (select
anywhere selects everywhere; Show in 3D is a tab switch), one **list** (the 3D list is the
Browser's filtered view, same rows/order, plus a 3D-only Proximity sort; quick filters sync
both ways, advanced filter editing stays in the Browser), one **card** (same inspector in both;
3D adds a prev/next review header; editable in both), one **keyboard** (j/k, `[` `]`, Esc, digit
decide-and-advance identical), one **visual language** (band chips, status dots, red/blue pair,
ghost style identical in grid, card, viewport, and photos). The keyboard handler and inspector
renderer are shared across tabs so the two never drift.

### Reports tab & the export pipeline (`clash_report`, `web/coord.html`)

The **Reports tab** is a live report preview beside an export panel: pick a scope
(Meeting set / Everything / Current Browser filter) and a format (PDF / Excel / BCF /
HTML). Headline contract: **what you see is what exports.** The page sends the scope's
clash **ids** (not a re-derived query) with the `report:` message, so the host builds
the file from exactly the rows the preview showed. The preview and every printed cover
compute their counts from the SAME `report_model.summarize`, so they can never disagree.

- **`report_model` is the single source of truth.** Band/score/trade/pair/status
  accessors + `summarize` + `report_rows`. Pure data, no Revit/WPF, parses under both
  interpreters. The 70/40 band cutoffs are duplicated here (documented alongside the
  four `coord.html` sites); move them in lockstep. Every builder (Excel, HTML, BCF)
  reads through it, so importance is first-class in every deliverable.
- **BCF 2.1 is the industry format** (Navisworks / ACC / Solibri / Revizto). Band maps
  to Priority (High/Normal/Low) and leads the topic Description; snapshots come from the
  same `viewpoints/<clash-id>.{jpg,png}` the app captures.
- **PDF renders the HTML report.** `_print_html_to_pdf` drives a hidden, off-screen
  WebView2 (`ShowActivated=False`, shares the app's `CoreWebView2.Environment` so no
  second profile locks) → `PrintToPdfAsync`, pumped via the same `_do_events` frame the
  export uses. It is **fully guarded**: any failure (old runtime with no PrintToPdf, a
  timeout) degrades to saving the HTML next to the PDF and opening it for Ctrl+P. So the
  one HTML layout (`@media print` hides the interactive toolbar) is the source for the
  HTML export, the PDF, and the on-tab preview.
- **Files land wherever a native Save As says** (defaulting into the clash-data folder);
  `reveal:` opens Explorer on the result. Every outcome is reported back
  (`reportdone` / `reportfail` / `reportcancel`), and `_report_busy` blocks re-entrancy.
- The host-side PDF/dialog paths are Revit-only and **not exercised by the CPython
  tests** (which cover the pure builders + `report_model`); verify them in Revit by hand.
