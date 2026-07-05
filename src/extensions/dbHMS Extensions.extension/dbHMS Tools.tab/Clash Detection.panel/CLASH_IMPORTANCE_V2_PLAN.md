# Clash Importance Engine v2 — Design and Implementation Plan

Status: **approved-pending-Nathan's-decisions** (section 15). Written 2026-07-04.
Provenance: deep-research pass (102 agents, 20 sources, 24/25 claims verified 3-0 or 2-1),
NIUHTC v1 data audit (1,604 clashes), code-grounding + 3-lens design + 2-critic workflow
(9 agents). Full working papers in the session scratchpad (`v2design/*.md`); the verified
research citations are summarized in section 2. This document supersedes nothing: it EXTENDS
`CLASH_IMPORTANCE_RESEARCH.md` and the rev-3 engine; v1 rules that survive are listed in place.

Companion docs: `README.md` (panel architecture), `defaults.py` (the firm standard tables).

---

## 1. Why v2 (what the NIUHTC run proved)

1. **Three of four tests stored zero rows, silently.** All 1,604 clashes came from
   `default-mep-internal`. `project.json` had `"link_role_map": {}`; `links_for_role`
   resolved nothing; the only warning went to coord.log, and the unloaded-link warning in
   `script.py` is suppressed exactly when ALL links are unloaded. Consequence: C1, C2, M2,
   N1, N3, N4-arch have NEVER fired on real data. M-CODE has also never fired anywhere:
   `runner.py:67-70` returns `[]` for clearance kind (stub).
   **WHERE THIS IS FIXED:** the loud-failure alarm (red run-modal state + blocking
   "map your links first" prompt) ships in **Phase 1** (section 5.7) so it can never
   silently recur; the arch/structure clashes populate as soon as the links are mapped and
   detection re-runs; the full arch/structure rule set (M-RATED, M-PEN, N1 grades, wall
   facts) fires on that data in **Phase 3**. This is the highest-priority item in the plan.
2. **Major is 69% of the job** (1,114 of 1,604). Only four rules fired: M1 (766), M4 (348),
   C3 (11), FB (479). A band that catches two-thirds of everything is a to-do list.
3. **M1 stamped one identical sentence on 572 rows.** 60% of M1 rows were scored from
   class/category fallback; 365/766 have no dims on either side.
4. **Geometry is never measured.** `penetration_depth_in`, `overlap_volume_cf`,
   `gap_inches`, closest points, `is_contact`: 0/1,604 populated on hard rows. The Geometry
   bar is a flat placeholder 4. R-GRAZE and C4 are dormant.
5. **Data starvation sits exactly where the over-firing is.** Pipes/Ducts: 100% dims,
   system, level. Mechanical Equipment: 0% dims, 34% level. Lighting/Electrical Fixtures:
   0% everything except the TYPE name (`elem.Name`; the `family` field itself is a Phase 2
   capture). Equipment `sys_class` is a comma-joined multi-value string that defeats every
   exact match.
6. **The score is a band label, not a ranking.** Histogram humps at 20-29 and 50-69, the
   40-49 decade is empty, 620 rows pile at 50-59 (33 distinct score values total).

## 2. Evidence base (verified research, abbreviated)

- **Owner standards rank by element labels + named pairs, with stage-gated deadlines**
  (Ashghal S0301 2022; Indiana University 2015 — IU Level One includes duct/pipe vs RATED
  walls for damper coordination, vs structure, vs ceilings, equipment clearances). Bands
  legitimately carry WHEN-it-must-die semantics.
- **~Half of raw clashes are irrelevant** (51.2% true-positive high/medium of 1,000 sampled,
  ASCE JCEM 2026; 58.3% irrelevant, Lin & Huang 2019). Taxonomy: errors / deliberate
  (intended penetrations) / pseudo (permissible overlaps).
- **Explainable rules beat black-box ML; rules-first hybrid beats both** (JRip > five ML
  families; 5 crude rules fed to ML as a feature: f1 0.96). **AI resolution advice caps
  ~80%** in the one published study. AI stays advisory.
- **Penetration TYPE is the top geometry feature**: partial penetrations (Intersected /
  Inserted / Contained) are NOT sleeve-resolvable and must be resolved in design; full
  pass-throughs under size thresholds (~250 mm rect / ~400 mm round) are sleeve candidates.
- **Element volume is the dominant relevance predictor; risk level has no dominant
  feature** — capture volume plus broad context.
- **NEC 110.26 provides per-se triggers citable verbatim** (dedicated space (E)(1)(a),
  foreign-systems-above (E)(1)(b), 30-in working width (A)(2)); caveats: cap at 6 ft above
  equipment top OR structural ceiling whichever is LOWER, suspended ceilings permitted,
  above-cap crossings must not flag.
- **Research gap:** no verified trade-priority ladder or cost-to-move dollars exist in
  print. The rigidity ladder stays grounded in code physics; no fake dollars.

## 3. Doctrine (invariants — violating any of these is a design bug)

1. **Band is decided by a named rule; sub-scores are clamped inside the band.**
   coord.html derives band FROM score client-side (L2579 + three more sites); the clamp is
   UI correctness, not taste. Promotion pressure gets a new named rule, never a cutoff move.
2. **A code citation appears only when the triggering fact was MEASURED.** Unmeasured
   situations get "verify ..." wording with no clause number.
3. **Suppression demands proof; demotion is the default for suspicion.** (R-NEST suppresses
   only on `super_root_id` proof; name heuristics demote to N-DUP with a flag.)
4. **Every new field is nullable; every consumer degrades to v1 behavior on None.**
   New PER-RUN DERIVED clash-level fields (geometry, context, diagnostics) MUST be appended
   to `_PER_RUN_FIELDS` (`merge.py:111-114`) so they refresh each run. Durable cross-run
   state (`status`, `comments`, `thumb`) must NOT be listed there — `_PER_RUN_FIELDS`
   membership would null it every run. Classify every new field into one of these two
   buckets explicitly at review time.
5. **Fingerprint identity is untouchable.** Never change `midpoint`, `SPATIAL_BUCKET_FT`,
   or anything `clash_core/identity.py` hashes. Better geometry goes in NEW fields
   (`overlap_centroid`).
6. **The reason is composed, never concatenated.** Three sentences (WHAT / WHY / ACT) plus
   at most one qualifier; confidence and near-threshold notes move inside the composer.
7. **AI never mutates band, score, rule, suppression, grouping, or capture priority.**
   The advisory AI layer is DEFERRED out of v2 (section 10), so there is no AI in the
   engine at all right now; this invariant governs it if and when it is ever built.
8. **One firm standard.** Tuning = editing `defaults.py`. Code editions pinned in comments
   (NEC 2023, IMC 2021, IPC 2021, NFPA 13 2022). No per-project knobs.
9. **Silent zero is a bug class.** Every pass that can produce zero (tests, capture,
   fire-rating scan, gear zones, room lookups) reports counts in run diagnostics.
10. **Rounding:** one shared half-away-from-zero helper (IronPython 2 `round` vs CPython 3
    banker's rounding); the harness uses the same helper or exact-stamp acceptance breaks.

## 4. Phase map and rev table (single source of truth)

| Rev | Phase | Needs | Content |
|-----|-------|-------|---------|
| 4 | P1 words+rules | rescore only | Composer + stamps, M1 partition (8 rules), N-PT, N-DUP, sys_class sets, score remap, group reasons, zero-row alarm CODE, all survivor templates recomposed |
| 5 | P2 capture | detection re-run | Ref facts (bbox/family/host/mount/level), pair geometry (`pairgeom.py`), context lookups, geometry+volume score terms, R-GRAZE/C4 gated activation |
| 6 | P3 arch payload | link roles fixed + re-run | Wall/floor facts, M-RATED, M-PEN, N1 wording grades, over-modeled-partition flag, structural-usage rerouting to structural rules |
| 7 | V3 detection cleanup | detection re-run | Layered-penetration collapse (one row per physical penetration, not per modeled layer), `raw_realistic_max` retune 30->48, geom budget 90s->300s, pen_class surfaced in facts. Scores shift within bands only; no band cutoff moved |
| 8 | P4 clearance | new subsystem | Clearance detection engine, NEC 110.26 zones (C-NEC, C-NEC-W, M-NEC-PROT), dedicated sprinkler-obstruction test + numeric M-SPR |

The advisory AI layer is **DEFERRED** (Nathan, 2026-07-04) and is part of NO phase above:
v2 ships as a fully deterministic engine. Its design is parked intact in section 10 for a
possible future pickup. Each rev gets a `defaults.py` history comment: "band movement
across this bump is a rule change, not resolved work." **Skew guard:** engine never restamps records whose stored
`config_rev` is HIGHER than its own rev (warn instead); rescore-to-current runs on tool
open when stored rev < engine rev, announced once in the UI ("scores remapped, rev N").

## 5. Phase 1 (rev 4) — pure rescore, validated offline on a COPY of NIUHTC

> **IMPLEMENTATION STATUS (2026-07-04): the scoring ENGINE is done and verified.**
> Shipped in `lib/clash_score/` (defaults + `__init__`) and `lib/clash_group/`: the
> M1 8-way split, N-PT + N-DUP, sys_class set-matching, the composer + all rule
> templates (headline / code_ref / resolve_by / resolve_by_label / facts /
> relevance_class), the score remap, and the group narrative. 553 unit tests pass
> (21 new). Offline harness on the real NIUHTC file confirms: Critical held at 11;
> band migration is exactly the 57 demotions (17 N-PT + 40 N-DUP), nothing else;
> every v1 M1 row lands in one named sub-rule; distinct reasons 78 -> 739, worst
> identical run 572 -> 56; deterministic; zero fallbacks.
> **Backward-compatible:** `score_all` signature unchanged and every new field is
> additive, so the current tool keeps working; the coord.html polish just renders
> richer data when it lands.
> **Honest calibration finding:** two spread gates (fill the 40-49 decade,
> distinct >= 55) are NOT reachable on a pure rescore — with geometry uncaptured
> the sub-score raw is clustered integers. What P1 delivers is the 50-59 pile eased
> (620 -> 576) and distinct scores 33 -> 47 (+42%). Full de-piling and the 40-49
> fill arrive with measured geometry in Phase 2 (the 5.4 sim assumed proxy
> geometry). `raw_realistic_max` calibrated to 24.
> **P1 alarm + UI: DONE (2026-07-04).** Zero-row alarm: `runner.run_test` now
> returns `(rows, diag)` with per-test element/role counts; `script.py` collects
> and persists `last_run_summary.test_diagnostics`, flags suspect tests, adds a
> suspect count to the run message, and the suppressed-link-warning bug is fixed
> (it now warns even when links are unloaded — the exact silent failure that
> produced this dataset). coord.html: the "Why it ranks" card now shows the
> composed reason + rule/code/deadline chips + the measured-facts table ("(not
> captured)" for uncaptured geometry) + score bars; the Home agenda uses the short
> headline; the group card renders the composed group narrative + governing
> deadline. All **browser-verified** against the dev-fallback sample (updated to
> v2). Backend parses + 562 tests pass. The alarm's loud warning is live; the only
> deferred bit is the visual per-test TABLE in the run modal (data is persisted).

## 6-status. Phase 2 (rev 5) IMPLEMENTATION STATUS (2026-07-04)

**Started — the capture side that kills the no-dims problem is in and consumed.**
`enrich.mep_facts` now captures (all nullable, try/excepted): `bbox_in` [dx,dy,dz],
`top_ft`/`bot_ft`, `family`, `host_id`/`host_cat`, `mount`, `super_root_id`,
`sys_class_list`. `clash_score._max_dim_in` falls back to `bbox_in` for
equipment/fixture categories (gated so routed curves never inflate), so equipment
and fixtures finally get a real Size term. 5 new tests cover the consumption side;
562 tests pass. **Needs a Revit run to verify the capture itself** (parameter/geometry
reads can't run in CPython).

**Pair geometry DONE (2026-07-05).** `clash_detect/pairgeom.py` captures, at detection
time in `hard.py` (both element handles + link transforms in scope, no re-resolution):
`overlap_bbox_in` (free AABB-overlap extents — the depth proxy on every hard row) and the
boolean tier `overlap_volume_cf` / `penetration_depth_in` (intersection min extent) /
`overlap_centroid`, guarded by a per-pair solid cap (8) + a run-wide 90 s wall-clock budget
+ a cached raw-solid extraction; on any failure/over-budget the row keeps the bbox tier.
Wired `hard.py` -> `runner.py` -> `script.py` (run-scope `geom_cache`) -> `merge.py`
(`_PER_RUN_FIELDS` extended). Engine consumption + the two guards are done and OFFLINE-TESTED
(8 new tests): geometry term uses the bbox proxy then the boolean depth; **R-GRAZE** wakes but
only on a MEASURED boolean overlap that is shallow AND small (never a bbox proxy, never a
wide shallow face-contact); **C4** wakes but is GUARDED so a duct fully through an arch
wall/floor stays N1 (its depth is just the assembly thickness) and a recessed fixture stays
N3 — C4 is reserved for deep MEP-vs-MEP / into-structure. `pen_class` (full/partial) is
Phase 3 (needs wall thickness). 570 tests pass. **Needs one Revit re-run to activate**
(same as the ref facts, which the 2026-07-05 run proved work). This fills the empty 40-49
band, separates graze from impale, and suppresses modeling-noise grazes.

## 7-status. Phase 3 (rev 6) IMPLEMENTATION STATUS (2026-07-05)

**DONE.** `enrich.py` captures the rated-assembly facts (`fire_rating_raw` +
parsed `fire_rating_hr`, `thickness_in`, `wall_function`, `is_structural`, `is_rated`
with a type-name fallback so ratings typed into the type name still count) on
wall/floor/roof/ceiling/framing refs. New engine rules, all offline-TESTED (10 new tests,
580 pass) and SMOKE-TESTED on the real 5,690-clash V2 data with zero exceptions / zero ERR
/ bands stable:
- **M-RATED** (Major): a duct through a rated wall/floor/roof -> fire/smoke damper +
  access door, IMC 607.5.1, resolve_by duct_fab.
- **M-PEN** (Major): a partial penetration (stops inside) or an oversized full penetration
  -> not sleeve-resolvable, design work. `pen_class` computed from penetration depth +
  wall thickness.
- **N1 grades**: a pipe/conduit through a rated assembly gets firestop wording (IBC
  714.4.1); the sentence names the assembly thickness when captured.
- **M-STRUCT-ZONE** (Major, escalate flag on the flexural zone): a routed run crossing a
  beam, classified into the top/middle/bottom third from the beam elevations + clash Z.
  Already FIRES on the V2 data (60 rows -- beam elevations were captured last run), so it
  needs no re-run to see.
- **Structural-wall reroute**: an arch-modeled wall/floor flagged structural takes the
  C1/C2/M2 structural path, never the N1 sleeve demotion.
config_rev bumped to 6 (covers Phase 2 geometry + Phase 3). M-RATED / M-PEN activate on the
next run (they need the fire-rating + thickness capture). raw_realistic_max recalibrated to
30 on the full run.

**REMAINING (small, deferrable):** room/space + nearest-ceiling context lookups (finicky
GetRoomAtPoint; enriches sentences and enables the over-modeled-partition flag + a future
NEC rule), the `pt_slab` coring-hazard flag (floor type-name heuristic), and the per-test
alarm TABLE render (alarm data is already persisted + surfaced in the run message). None
blocks the boss review.

## 7b-status. Rev 7 — V3 detection cleanup DONE (2026-07-05)

After the V3 deep dive (full run, 5,690 clashes, everything activated: 1,630 boolean
geoms, arch facts, M-RATED/M-PEN/M-STRUCT-ZONE all fired) fixed the three over-fire
issues (C4 restricted to vs-structure, M-PEN finish-floor gate, M-STRUCT-ZONE beam
penetration gate — all shipped in rev 6 code), four cleanup items landed as **rev 7**.
Nathan directed all of these; the rated-wall lookup and Phase 4/5 were explicitly
deferred (see Open decisions).

- **Layered-penetration collapse** (`clash_core/dedupe.collapse_layered_penetrations`,
  wired in `script.py` right after `drop_soft_overlapping_hard`, PRE-merge so no history
  churn). One MEP run through a stacked floor/roof/ceiling assembly clashed with EVERY
  modeled layer (structural slab + topping + finish + rendering) — four agenda rows for
  one physical penetration. Collapses each spatial cluster (same mover, same layered
  category, midpoints within 3 ft via single-linkage) to ONE row, keeping the most
  significant layer (structural, then thickest, then lowest element id for a stable
  fingerprint). A riser through three floor LEVELS stays three penetrations (clusters are
  storeys apart). Pure Python, 11 new tests. **On the V3 data: 239 rows dropped (4.2% of
  all clashes; 43% of the 553 layered-penetration candidates were redundant stacked
  layers), zero Critical touched (36 Major + 203 Minor duplicates removed).**
- **`raw_realistic_max` retune 30 -> 48.** Once pairgeom filled the geometry/volume score
  terms on the real run, 30 re-piled ~80% of the Major band into 60-69. Swept on the
  collapsed V3 data: 48 drains the pile into a heavy 50-59 middle (1,415, ~90%), reserves
  60-69 for a genuine top ~8% (121 near-critical Majors), and starts a 40-49 tail (40),
  at the best distinct-score granularity (21). Bands are unchanged (rmax only orders
  within a band): Critical 29 / Major 1,576 / Minor 3,733.
- **Boolean geom budget 90s -> 300s** (`pairgeom.GEOM_TIME_BUDGET_S`) + solid-cache cap
  600 -> 1,200. 90s covered only 1,630 of 5,248 hard rows; 300s covers essentially all of
  them and still lands the whole run under 30 min.
- **pen_class surfaced in facts.** The engine's own read of the depth (the same
  `partial`/`full` class that drives M-PEN) now annotates the Penetration facts row
  ("2.0 in (partial - stops inside)"). Transparency only; nothing reads it back. On V3:
  185 rows annotated (38 partial, 147 full).

config_rev bumped 6 -> 7. Full-pipeline re-verified offline on the V3 file (collapse +
score with shipped defaults): 5,451 final clashes, 0 scoring ERR, bands stable, Major
spread on target. **593 tests pass.** Activates on Nathan's next detection re-run.

### 5.0 The v2 evaluation ladder (consolidated; order is NORMATIVE)

Layer A, first match wins: R-SELF -> R-NEST (P2, proof only) -> R-NOT-OURS -> R-SYS ->
R-FIELD -> R-GRAZE (activates P2, guarded). Suppressed rows are still fully scored.

Tier ladder, first match wins (phase in parentheses; unmarked = live at rev 4):

```
 1. FB-no-mover
 2. C-NEC          (P4)      5. C2                    8. M-CODE (dead until P4 engine)
 3. C-NEC-W        (P4)      6. C3                    9. N3
 4. C1                       7. M-NEC-PROT (P4)
10. M-RATED        (P3)     13. C4 (activates P2, guarded; see 6.4)
11. M-PEN          (P3)     14. M2
12. N1 (graded P3)          15. M4 (hysteresis intact)
16. N2
17. M-SPR numeric  (P4; fires only on the dedicated sprinkler-test rows)
18. N4 / M3
19. N-DUP  — placed AFTER M4 by construction: the 44 measured rows were all v1-M1
    (none reached M4), so this position preserves the measured set exactly
20. M1 dispatch (outer gate byte-identical to v1 :782-783), internal order:
    N-PT -> M1-EQ-EQ -> M1-FIX-EQ -> M1-FIX-CURVE -> M1-FP -> M1-SLOPE ->
    M1-EQ-SYS -> M1-RIGID -> M1-XING
21. FB residual
```

N-PT lives INSIDE the M1 dispatch (critic ruling: placing it above M4 would steal
congested-zone rows and silently expand the measured band-change set).

### 5.1 M1 partition — 8 named Major sub-rules

Dispatch is MOVER-CLASS FIRST, matching how the counterfactual measured the populations
(the earlier draft's "exactly one side equipment" placed second would have swallowed the
fixture/FP/vent-vs-equipment rows and made the measured numbers unreachable). Populations
below are measured on NIUHTC and sum to 766 before the N-PT/N-DUP intercepts.

| # | Rule | Trigger (inside old M1 gate, after N-PT) | n measured | resolve_by | code_ref |
|---|------|-------------------------------------------|-----------|-----------|----------|
| 1 | M1-EQ-EQ | both sides klass equipment | 142 (~98 after N-DUP takes 44) | gear_setting | none ("verify working clearance" wording; zone unmeasured until P4) |
| 2 | M1-FIX-EQ | mover fixture, other in EQUIPMENT_CATS | 173 (~165 after strict N-PT) | ceiling_close | none |
| 3 | M1-FIX-CURVE | mover fixture, residual | 24 | ceiling_close | none |
| 4 | M1-FP | mover klass fp_wet | 43 | next_cycle | none — NFPA obligations stated as prose ("relocations re-trigger spacing and obstruction checks"), clause number stamped only when the P4 test MEASURES distance (doctrine 2) |
| 5 | M1-SLOPE | mover klass gravity / gravity_vent / condensate / medgas | 23 | next_cycle | IPC 704.1 / 905.2 (slope class IS measured, from system classification) |
| 6 | M1-EQ-SYS | exactly one side equipment (residual: equipment-mover vs curve 19 + curve-mover vs equipment 89) | 108 | gear_setting | IPC 704.1 in the gravity-other variant only |
| 7 | M1-RIGID | min(rig) >= 4, residual | ~0 on this run (guard rule; contained in the residuals) | next_cycle | none |
| 8 | M1-XING | residual curve-vs-curve | 253 | next_cycle | none |

Sum check: 142 + 173 + 24 + 43 + 23 + 108 + 253 = 766. Rule ids are frozen HERE (stored in
records, key M4 hysteresis via `prev_rule`, key every migration matrix). Templates: one per
rule, 3-sentence grammar (5.5); full texts in the design working papers; final wording
arbitrated at implementation against the composer test.

### 5.2 N-PT and N-DUP (the measured demotions)

- **N-PT** point-mounted fixture demotion, Minor, flag `field_fix`. Head of the M1
  dispatch (see 5.0). Phase 1 signal: name patterns, regexes precompiled at import, all
  matching case-insensitive: strict = `EXIT` word or `^#\d+` -> **8 measured rows** (all
  EXIT signs). Extension option A adds wall-device TOKEN matches (GFI / CONVENIENCE /
  SWITCHED / WP / COUNTERTOP) -> **~17 rows** (the tokens reach ~9 of the 23 unknown-class
  fixture rows). Extension option B additionally treats an Electrical Fixture NAMED
  "TYPICAL" as a wall device -> **~27 rows**. `S1 BATHROOM` / `S3` stay Major in every
  option (the counterfactual flags S-prefix as likely strip/surface LIGHTING, not devices).
  The earlier "31" figure was wrong: it counted all 23 unknown-class rows as reachable by
  the five tokens, which they are not. Exact demotion sets come from the harness; every
  demoted row appears in the spot-check sheet. **Decision D1** (section 15) picks the
  option. Phase 2 replaces patterns with the captured `mount` fact.
- **N-DUP** duplicate/placeholder family suspects, Minor, flag `family_artifact_suspect`.
  Trigger FROZEN to the measured rule: hard + same source + same category in
  EQUIPMENT_CATS | MOUNTED_CATS + (names equal and non-null, OR either name in
  {TYPICAL, STANDARD}, case-insensitive) -> **44 measured rows**, all currently Major;
  dissolves 2-4 placeholder-anchored Major groups outright. `TYPE CATALOG` and `none` are
  CANDIDATE placeholder names (72 and 62 side-occurrences in the equipment pile) but were
  NOT in the measured rule; the harness re-measures with them included and the expanded
  set ships only if Nathan approves those rows in the spot-check, with the gate number
  taken from the harness, never assumed. NOT applied to duct/pipe categories. Template
  must NOT promise a band override (no such mechanism exists); wording says "verify in the
  model this cycle; comment here if these are two genuine units." Phase 2: provable nesting
  (`super_root_id`) moves up to Layer A **R-NEST** true suppression; N-DUP narrows to the
  placeholder branch.

Combined P1 band movement with the frozen triggers: Major 1,114 -> ~1,062 (strict N-PT)
to ~1,043 (option B). Critical stays exactly 11 (gate).

### 5.3 sys_class set matching (gated)

`_sys_class_set(ref)` splits the stored comma-joined string. Replacement at the four exact
match sites (`:356-357, :406, :434-435, :439`). Gates (both critics' traps):
- Gravity/FP signals only when category is in PIPE_CATS | FLEX_CATS.
- For any non-Pipe category that passes (fittings/accessories), gravity requires the split
  set to be a SUBSET of GRAVITY_CLASSES (all values gravity), not any-match — otherwise
  multi-system fittings mint new C3 Criticals inside a "band-neutral" rescore.
- Equipment's class set is template/facts context only, never a rigidity signal.

### 5.4 Score composition v2 (spreads scores honestly; clamp preserved)

| Term | Range | Formula (float literals; Py2 division) | Degrade |
|------|-------|----------------------------------------|---------|
| constraint | 0-15 | `2.0*rig_mover + 1.0*rig_other`; **rig_other = 5 when other side is fixed** (wall/structure must outweigh a mid duct) | mover None -> 4.0 |
| size | 0-8 | `min(8.0, 8.0*bulk_in/SIZE_FULL_IN)`, SIZE_FULL_IN=30.0; bulk falls back to bbox dims where captured | both None -> 2.0 |
| geometry hard | 0-8 | pen: `min(8.0, 2.0*pen_in)`; else bbox proxy `min(6.0, min(overlap_bbox_in))`; else 4.0 (v1 legacy) | old records -> 4.0 |
| geometry soft | 0-6 | unchanged | unchanged |
| volume (new) | 0-6 | `min(6.0, 6.0*(vol_cf/VOL_FULL_CF)**0.5)`, VOL_FULL_CF=1.0 | None -> 0.0 |
| congestion | 0-6 | `min(6.0, cluster_n*6.0/20.0)` continuous | unchanged |
| code | 0/6 | 6 iff `code_ref` non-null (bar and citation can never disagree) | — |

Assembly: proportional band mapping `score = base + round_half_away(width * min(1.0,
raw/RAW_REALISTIC_MAX))`, RAW_REALISTIC_MAX=36.0 re-derived after the volume/code terms
settle (single tuning constant; calibration harness owns it). `features.sub_raw` stays.
Sub-scores still cannot cross 70/40. `brk` gains a sixth `volume` bar (builder handles
arbitrary lists, verified). Soft rows structurally earn less raw than hard rows under this
table; the harness prints soft-vs-hard within-band distributions so the P4 soft flood is
judged with eyes open, and per-test scoring semantics are revisited when the sprinkler test
ships (section 8).

Simulated P1 effect (real dims + proxy geometry): 40-49 decade 0 -> 39 rows, 50-59 share
38.7% -> ~34%, distinct scores 33 -> 55+.

### 5.5 Sentence composer and importance stamps

`_compose(rule_id, ctx)` -> `(headline, reason, facts, anchor, resolve_by)`; one template
function per rule id (`_TEMPLATES` dict, `ERR` included). Grammar: S1 WHAT (physical event
with sizes/systems/location; verb by kind+geometry), S2 WHY (one code or cost anchor), S3
ACT (action + deadline vocabulary), plus at most one qualifier sentence replacing the v1
suffix concatenation (`:964-967` retired). Null policy: a null fact drops its clause;
never renders "None"; `_fmt_desc` degrades `"24x12 in supply duct"` -> `"duct (size not
captured)"` -> bare category. Headline = S1, enforced <= 90 chars by SLOT TRUNCATION
(40-char desc cap with ellipsis — long real names like "dbHMS - PFP - Jockey Pump
Controller" exist; the CPython template test uses real NIUHTC names plus an all-None ctx).

New stamps inside `importance` (rewritten wholesale by `score_all`, no migration):
`headline`, `code_ref` (bare citation string; there is NO separate `anchor` field — the
composer returns `(headline, reason, facts, code_ref, resolve_by)` and ASCE-style cost
anchors are template prose, never stamped), `resolve_by` (token), `resolve_by_label`
(display string stamped so coord.html stays dumb), `facts` (ordered
`{'k','v','unit','method'}` rows, DERIVED-ONLY: no engine code ever reads facts[] back),
and `relevance_class` in {error, deliberate, pseudo, artifact, field} derived from the
rule id (N1 / N4-penetration -> deliberate; N2 -> pseudo; N-DUP / R-SELF / R-NEST ->
artifact; R-FIELD / N-PT / field_fix -> field; else error = genuine coordination work) —
this stamps the research taxonomy explicitly and feeds the ~50% noise benchmark. Deadline
vocabulary (A's token set) in `defaults.py`: pre_pour, steel_fab, duct_fab, gear_setting,
sleeve_pkg, ceiling_close, next_cycle, field.

Geometry-grade template variants (cite measured pen/volume/gap) are WRITTEN in P1, dormant
behind null checks; they activate automatically when capture ships (merge backfill).

### 5.6 Group explanations

`clash_group` authors `rollup.reason` with the same 3-sentence grammar (composition /
governing constraint from the most severe member's code_ref / action + governing
resolve_by), plus group facts (worst member, band mix, trades, span, artifact-suspect
count). The rescore path MUST refresh rollups, including `rollup.score`/`rollup.band`
(verify `clash_group` rebuild is wired into the rescore call site, not only detection
runs). Group lifecycle under band migration, explicit: groups NEVER auto-close (status is
human-owned); a group whose members all demote below Major leaves the Home meeting agenda
automatically because the agenda is band-driven, but the group, its thumb, and its history
persist; the harness lists every group whose rollup band changes (expected: the 2-4
dissolving placeholder groups) and the rack-retention delta is a P1 acceptance gate
(section 5.8). UI: `rollup.reason` becomes the lead paragraph of the Composition icard
(it exists in the schema today and is never displayed); `whyTogether` stays as the
fallback lead for manual/legacy groups with no engine rollup.

### 5.7 Zero-result alarm (code lands in P1; effect visible on next run)

Three layers plus hardening (the fix for the disaster that produced this dataset):
1. `run_test` returns `(rows, diag)`: side element counts, roles requested/resolved,
   mapped vs loaded link titles, rows stored, skipped_reason. Soft tests add phase
   counters (candidates, mesh-measured, bbox-fallback, within-tolerance) and a RED state
   for `tolerance <= 0` (soft.py silently returns `[]` today).
2. `last_run_summary.test_diagnostics` persisted forever; first run after upgrade renders
   "no baseline", not green.
3. Run-modal per-test table; red when rows==0 AND (a side collected 0, a requested role
   resolved 0 instances, or the prior diag stored > 0).
Hardening: fix the suppressed-warning bug (`script.py:1569` — warn on missing mapped titles
regardless of loaded_titles); blocking "map your links first" prompt on bind when a `link:`
test is enabled, `link_role_map` is empty, AND the doc actually contains RevitLinkInstances;
title-drift listing by name. Later passes add their own counters here: fire-rating coverage
("N walls scanned / M rated" per link, red at 0/many), gear-zones-tested, room/space
hit-rate, pairgeom timings + `geom_method` mix + boolean-failure count.

### 5.8 P1 acceptance gates (offline harness, CPython, COPY of NIUHTC clashes.json)

1. Band-migration matrix EXACT: off-diagonal cells equal the frozen-trigger demotion sets
   AS RE-MEASURED BY THE HARNESS (current estimates: N-PT 8 strict / ~17 option A / ~27
   option B; N-DUP 44); Critical row and column both 11; suppression deltas zero. The gate
   number is the harness's output for the frozen triggers, never a hand-assumed count.
2. Rule-migration matrix: every v1 M1 row lands in exactly one M1-*/N-PT/N-DUP bucket;
   zero fall to FB; full matrix printed.
3. Ranking: 40-49 decade > 0; 50-59 share < 35%; distinct scores >= 55; within-band
   Kendall tau reported; top-20 overlap v1-vs-v2 printed with deltas for review.
4. Sentences: distinct >= 600; largest identical run <= 99; 100% non-null headline +
   resolve_by; headline <= 90 chars; 2-4 sentences per reason; no `{` leftovers.
5. Determinism: rescore twice -> byte-identical stamps (shared rounding helper);
   `band(score)` == stamped band for every row (clamp proof).
6. Noise benchmark (trend, not gate): (suppressed + Minor + artifact-flagged)/total vs the
   literature's ~50% irrelevant baseline, split by `relevance_class`.
7. Group gate: the set of groups whose rollup band changes equals the predicted dissolving
   set (2-4 placeholder-anchored groups, listed by name); rack-retention delta explained
   entirely by that set.

## 6. Phase 2 (rev 5) — capture re-run

**Capture philosophy for Phases 2-3:** pull every fact a mechanical OR structural engineer
would actually check, across ALL MEP, architectural, and structural categories — the score
is only as smart as the facts behind it. Scope is set by the full data audit and the
research (volume, penetration type, rated assemblies, room context, structural penetration
zones), NOT by any single example clash. Breadth in capture; the reason surfaces only the
one decisive fact (doctrine 6), so more data makes the ranking smarter without burying the
signal. What follows is the prioritized list, cheapest first.

### 6.1 Ref facts (`enrich.py` + `_make_ref`, run-scope memo keyed `(link_ns, eid_int)`)

`bbox_in [dx,dy,dz]`, `top_ft`, `bot_ft` (world bbox; **bbox-derived dims feed rigidity
ONLY for non-LocationCurve elements** — equipment/fixtures, exactly the starved
populations; a diagonal duct's world AABB is fiction. New `rig_src='bbox'`: confidence
full, facts row shows `method: bbox`, `near` flag applies), `family`
(FamilyInstance.Symbol.FamilyName), `super_root_id` (SuperComponent walk), `host_id`/
`host_cat` (None when host is a RevitLinkInstance), `mount`
{ceiling,wall,floor,face,free,None}, `sys_class_list`, **element solid volume** `vol_cf`
(free during solid extraction; the research's dominant relevance predictor), level
fallbacks (FAMILY_LEVEL_PARAM, reference/schedule level, bbox-bottom vs level table).

### 6.2 Pair geometry (`clash_detect/pairgeom.py`, post-dedupe pass, hard rows only)

Ladder per pair, cheapest first: (1) `overlap_bbox_in` — free, dx/dy/dz already computed
and discarded in `hard.py:421-426`; (2) boolean intersect (solids at FINE detail, link
transform via GetTotalTransform, per-pair bare `except Exception`): `overlap_volume_cf`,
`overlap_centroid` (new field; NEVER `midpoint`), `penetration_depth_in` (exact along
`Wall.Orientation` vs wall/floor of known thickness — wrap: Orientation THROWS on curved
walls, fall back to generic; bbox min-extent proxy for MEP-vs-MEP), `pen_class`
{full, partial, contained, None} — `contained` only vs wall/floor; full requires depth
within `PEN_FULL_TOL_IN = 0.25` of thickness AND bbox crossing both faces; (3) fallback
tag `geom_method='bbox'` (host-vs-link bbox proxies are link-frame; facts table labels
them, never presents as exact). Guards: `GEOM_SOLID_CAP=8`/side, `GEOM_TIME_BUDGET_S=60`
ordered by overlap size desc, solid cache FIFO-capped 400 and CLEARED at pass end.
Cost: ~5-30 s at 1.6k rows, bounded.

### 6.3 Context lookups (same pass, memoized by 1-ft midpoint bucket)

`space`/`room` — name, number, AND `department` + `occupancy` (the richest arch-side
severity signal: an OR / isolation room / imaging suite is not a mechanical room, and
occupancy modulates both severity and action wording; GetRoomAtPoint WITH explicit phase —
pick the phase with most rooms; run diag reports hit-rate because plenum points often sit
outside room volumes), `clg_below_ft` (one ceiling collector pass per arch link + XY grid),
`level_ctx` (level table; link elevations shifted only when BasisZ vertical). All appended
to `_PER_RUN_FIELDS`.

### 6.4 Activation

Geometry + volume score terms go live as fields populate. **R-GRAZE** wakes with proof
guards: `geom_method=='boolean'` only, depth < 0.375, `overlap_volume_cf < GRAZE_MAX_VOL_CF
(0.02)`, mover rig <= 3, not structural, other side not rated (tri-state: None passes).
**C4** keeps its ladder position with B's guard set; the truth table IS the spec:

| Deep pen (>= 6 in) situation | Outcome |
|------------------------------|---------|
| other side structural, pen_class partial or None | **C4 Critical** (a rig-2 pipe 6 in inside a beam is exactly the C4 story) |
| other side in PENETRABLE_ARCH_CATS (wall/floor/ceiling/roof) | never C4 — M-RATED / M-PEN / N1 own it |
| pen_class == 'full' (through-penetration) anywhere | never C4 — depth ~ thickness is a pass-through, not systemic overlap |
| mounting-shaped pair (mover hosted on / seated on the other side) | never C4 — N3 owns it (recessed panelboard case) |
| MEP-vs-MEP deep overlap | **C4 Critical** |

R-GRAZE/C4 fire counts are harness output reviewed by a human BEFORE the rev ships.

**P2 acceptance gates:** geometry bar non-flat on >= 95% of hard rows; largest identical
sentence run <= 56 (family-name slot live); R-GRAZE and C4 fire lists human-reviewed;
fact-coverage dashboard reviewed (dims/family/mount/level percentages per category);
pairgeom pass inside `GEOM_TIME_BUDGET_S` with boolean-failure rate and `geom_method` mix
reported in diagnostics.

## 7. Phase 3 (rev 6) — the FULL architecture + structure payload (first run with fixed link roles)

Walls are ONE slice. This phase captures every arch and structural fact that changes a
ranking or an action, across all penetrable and fixed categories. Scope was set by what a
mechanical/structural engineer actually checks plus the research, NOT by the wall example.

**Rated-assembly facts — walls, FLOORS, ROOFS, and ceilings, same treatment for each:**
`fire_rating_raw` + parsed `fire_rating_hr` (unit table: "2 HR" / "120 min" / "2-hour";
type-name regex fallback; parsed param wins, regex fills gaps), thickness, structural flag,
absolute base/top elevations, room-bounding, `wall_function`. A rated FLOOR or ROOF
penetration is a firestop/damper item exactly like a rated wall, not a lesser case.

**Structural depth (reverses v1's "bbox only" for framing — the biggest breadth gain):**
- **Beams (Structural Framing):** member depth, and compute WHICH THIRD of the depth the
  clash crosses (top / middle / bottom) from the beam solid + overlap centroid. A pipe
  through the middle third of a web is often an engineerable opening; bottom/top third or
  flange usually is not. New rule **M-STRUCT-ZONE** flags the zone and routes to the
  structural engineer — the tool never approves an opening, it surfaces the decisive fact.
- **Columns (Structural Columns):** never-penetrate + a clearance-zone flag.
- **Beam role + bracing** (transfer / moment / braced-frame): read from type name / comments
  where present. HONEST: rarely a clean parameter, so a name heuristic + human confirm,
  captured as a flag, never a hard claim.

**Slab-coring hazard (flag, not a precise check):** `pt_slab` flag when a floor type name
matches post-tensioned patterns ("PT", "post-tension"). You cannot field-core a PT slab
without locating tendons, and tendon paths are NOT in the model — so the tool flags
"post-tensioned slab: coordinate before coring", it never pretends to know the cable layout.

**Room occupancy** (department + occupancy, captured in 6.3): a clash in an OR / isolation
room / imaging suite is not a mechanical-room clash; occupancy modulates severity and
wording.

**Shafts:** MEP entering an elevator / stair / mechanical shaft is a hard error, derived
from `wall_function` = Shaft/Coreshaft + room/space name; flagged distinctly from an
ordinary partition crossing.

**New/updated scoring rules, placed before N1:**
- **M-RATED** (Major): duct through a rated wall/floor/roof (`fire_rating_hr >= 1.0`) ->
  fire/smoke damper + access door, IMC 607.5.1, resolve_by duct_fab. Pipe/conduit/tray stays
  N1 with firestop wording (IBC 714.4.1). Fail-loud fire-rating coverage diag (shared-param
  firms would otherwise read as "no rated assemblies").
- **M-PEN** (Major): `pen_class` partial/contained, or full over sleeve thresholds
  (SLEEVE_RECT_MAX_IN=10.0, SLEEVE_ROUND_MAX_IN=16.0) -> not sleeve-resolvable, design work.
- **M-STRUCT-ZONE** (Major, Critical vs a column or bottom-third beam web): the beam-third /
  column penetration above.
- **N1 grades**: full pen within thresholds gets proof-grade sleeve wording; all-null keeps
  today's generic wording. `over_modeled_partition` flag when clash Z sits above the wall's
  captured top constraint (flag, never suppression) — one item on this list, not the phase.
- `structural_usage` / `wall_function` flips arch-modeled bearing/shear walls onto the
  C1/C2/M2 structural path.

**Feasibility ladder (honest about each fact):** measurable (fire rating, thickness, beam
depth + third, column clearance, room department/occupancy, shaft function); flag /
name-heuristic + human confirm (PT slab, beam role, bracing); NOT in the model and never
faked (tendon paths, rebar, engineered-opening approvals).

Comparison discipline: persisting rows get the full matrix treatment; NEW arch/struct rows
get first-contact review only (sample 20 per new rule) — never presented as comparable to
v1 totals (population roughly triples per the 5,651-row calibration run).

## 8. Phase 4 (rev 8) — clearance engine + NEC zones + sprinkler test

> **IMPLEMENTATION STATUS (2026-07-05): BUILT, awaiting a Revit run to
> validate the geometry.** The clearance engine ships as
> `lib/clash_detect/clearance_engine.py` (a new peer of hard/soft/pairgeom),
> dispatched from `runner._run_clearance` (the old `kind=='clearance'` stub is
> gone). It SYNTHESIZES a zone Solid per owner and flags intruders via
> `ElementIntersectsSolidFilter`, emitting rows with ref_a=intruder /
> ref_b=owner (both real elements, so fingerprints/`_make_ref` are unchanged).
> - **Four firm tests** in `default_tests.json` (`C-NEC`, `C-NEC-W`,
>   `M-NEC-PROT`, `M-SPR`); the test id IS the scoring discriminator
>   (`clearance_rule`). set_b = owner gear, set_a = intruders.
> - **Zones**: C-NEC = footprint extruded from gear top to min(+6 ft,
>   structural ceiling); C-NEC-W = working-space box in front (facing +
>   voltage depth, built only when the facing is trustworthy + axis-aligned,
>   else skipped — never guess a direction); M-NEC-PROT = the band between +6
>   ft and a REAL structural ceiling (empty when there is no headroom, so no
>   next-floor false-flags); M-SPR = NFPA radius box around a head (dormant —
>   zero heads in the model). Housekeeping pads are excluded as zone owners.
> - **Identity**: `clash_fingerprint` gained `include_midpoint`; `merge_runs`
>   keys clearance rows on (test id + element pair) with the midpoint EXCLUDED
>   so a nudged intruder keeps status/comments. Every hard/soft row hashes
>   byte-identically (regression-tested). The five clearance fields are in
>   `_PER_RUN_FIELDS`. Clearance rows take the test's `default_assignee`
>   (Electrical / Fire Protection), not the intruder's trade.
> - **Scoring**: the four rules dispatch at the TOP of `_tier` (above C1, above
>   the mover-None guard) so C2/C4 can never steal a zone violation and an arch
>   intruder with no mover still routes correctly; C-NEC-W with an arch/unmapped
>   intruder demotes to Minor + `flag_design_team`. Composed sentences +
>   `CLEARANCE_CODE_BY_RULE` citations; clearance facts show Intrusion / Zone
>   cap instead of bare "(not captured)" pen/overlap. rev 7 -> 8.
> - **Tested**: 640 CPython tests pass. Testable = the NEC/NFPA math + owner /
>   leak / facing / axis classification (`test_clash_clearance.py`), the
>   fingerprint + merge identity (`test_clash_merge.py`), and the scoring rules
>   (`test_clash_score.py`). RUNTIME-ONLY (needs a real run): solid extrusion,
>   `ElementIntersectsSolidFilter`, `FacingOrientation`, linked transforms, the
>   structural-cap scan. **Validation gate**: run the four tests on a model
>   with electrical gear; confirm C-NEC/C-NEC-W emit sane rows on the 46 gear
>   elements, M-SPR emits 0 without error, no existing hard/soft row re-keys.
>   The sprinkler half cannot be validated until a sprinklered model exists.


**The clearance detection engine does not exist** (`runner.py:67-70` stub; M-CODE is dead
code today). Scope it as a subsystem, built once, shared by everything below:
zone solids via GeometryCreationUtilities, `ElementIntersectsSolidFilter` over broadphase
candidates, ref_a=intruder / ref_b=owner element (both real, so fingerprints work), plus an
explicit identity rule for synthetic rows: fingerprint on (owner element, intruder element,
test id) — midpoint EXCLUDED so a nudged duct does not detach status/comments. Dedupe rule
for "duct both hard-clashes the gear and violates its zone" (keep both, cross-reference
flag). Then:
- **C-NEC** (Critical): foreign duct/pipe inside dedicated space (footprint extruded
  equip-top to min(+6 ft, structural ceiling)). Guards: skip OST_Ceilings members, skip
  above-cap crossings, sprinkler piping routes to M-NEC-PROT. NEC 110.26(E)(1)(a).
- **C-NEC-W** (Critical): working space box; front face from FacingOrientation with an
  ambiguity fallback (skip zone when facing unreliable); arch-side hits route to an
  FB-style "flag to design team" instead of Critical (unactionable by MEP).
- **M-NEC-PROT** (Major): leak-capable systems above the cap, 110.26(E)(1)(b).
- **M-SPR numeric** (Major): NFPA 13 10.2.7 three-times rule (capped 24 in) via a DEDICATED
  head-radius test (sprinkler heads only vs everything within 24 in — cheap because the
  A-set is tiny; never raise general soft tolerance). Until then, hard sprinkler contacts
  get M1-FP wording with NO numeric distance claims (a citation the tool cannot back is
  forbidden by doctrine 2). Per-test scoring semantics: sprinkler-test rows judged by the
  NFPA formula, not generic gap_ratio (tolerance-as-denominator coupling documented).
Diag: "N gear elements zone-tested", listed by name.

## 9. UI changes (coord.html, a handful of localized edits)

Hybrid "Why it ranks" card (option 3): reason paragraph (2-3 sentences; panel already
scrolls) -> chip row (rule id, band-colored; code anchor in Consolas; resolve_by_label
chip; gray `estimated` chip when degraded) -> Measured-facts KV block (renderElementCard
pattern; null v renders `(not captured)` in #A0AEC0) -> existing brk bars under a micro
caption "score components (ordering within band)". Old records: paragraph + bars, exactly
today. Home agenda renders `headline || reason` (fixes the L4418 truncation). Group card:
`rollup.reason` lead paragraph + group facts block. NO touches to `band()`, the four 70/40
sites, PIN_COLORS, or band names.

## 10. DEFERRED (not in v2 scope): advisory AI layer

**Status: deferred at Nathan's direction, 2026-07-04.** The AI layer does not improve the
ranking itself — the engine ranks and explains deterministically without it — so it is
explicitly OUT of scope for v2 and belongs to no phase. Nothing below gets built until
Nathan chooses to revisit it. The design is preserved intact so it can be picked up later
without re-deriving it; when that happens it stays advisory (research caps resolution
advice ~80%; doctrine 7 governs it). Everything from here to the end of section 10 is
future design only.

- **Storage: top-level `c['ai']`** (NOT inside `importance` — `score_all` rebuilds that
  dict wholesale every run/rescore and would erase advice; top-level fields survive merge's
  deep-copy, like status/comments/thumb; NOT in `_PER_RUN_FIELDS`). Grep-test targets the
  new location.
- Unit of work: the ISSUE (95 groups + ~200 Critical/Major ungrouped = ~300 calls/full
  pass), per-clash on-demand via "Ask AI". Delta-only batches (input_hash over facts + rule
  + band ONLY — hashing reason text would invalidate all advice on every wording rev).
- Runtime: reuses Chatbot plumbing (HttpWebRequest, TLS 1.2/1.3, DPAPI key store — no
  second key location). Model constant default `claude-opus-4-8`; ~$0.02/call, ~$7 full
  pass, ~$1 delta pass; Haiku fallback constant documented. Batch runs on a
  `System.Threading.Thread` worker (module is Revit-API-free), results marshaled back via
  the existing Dispatcher/WebView2 post pattern; budget 5-8 s/call (25-40 min cold pass in
  background; never on the UI thread; never blocks the run modal). Sequenced AFTER the
  importance-first thumbnail phase so images exist. System prompt is static; note in code:
  it is under the 4096-token Opus cache minimum, so do not expect cache hits.
- Bundle: engine block + refs + members + context + last-3 comments + human-action history
  + thumb JPEG when present (`"image": false` otherwise; prompt says reason from facts
  alone; red=A/blue=B convention stated). Output: strict JSON via `output_config.format`
  json_schema: verdict concur/push_back/insufficient_facts, suggested_band (push_back
  only), basis <= 2 sentences referencing a supplied fact, 1-3 resolutions, confidence.
- UI: separate icard below "Why it ranks" (and below Composition on groups), permanent
  subcaption "Advisory only. Does not change the score.", distinct inset background,
  offline/no-key static states, Helpful/Not-helpful buttons.
- Logging: per-user `ai_advice_<USER>.jsonl` in the bound folder (multi-machine append
  interleaving; telemetry precedent), merged at read time. Record-write collisions on
  `c['ai']` (two machines advising the same issue): idempotent by `input_hash` — equal
  hashes produce equivalent advice, last-writer-wins is acceptable; the batch skips rows
  whose stored hash already matches, so races only cost duplicate tokens, never data.
  Governance (future): pilot would be manual-trigger only; a firm sign-off on sending model
  facts + images to the API is a prerequisite whenever this is revisited; spend control is a
  real cap, not just key removal:
  `AI_MONTHLY_CAP_USD` in `clash_ai/defaults.py` (default 50.0) — the batch computes the
  month's spend from the merged logs and refuses to start past the cap (per-clash Ask AI
  still works, with a warning).
- Feedback loop: `calibration_report` gains an ai_agreement block (concur rate per rule,
  push-back precision vs status transitions + stored labels, insufficient_facts
  concentration vs the fact-coverage dashboard). Push-back patterns above ~70% precision
  over 2+ runs become PROPOSALS for named deterministic rules (rev bump). AI never touches
  scores at runtime.

## 11. Diagnostics and calibration (standing instruments)

- `calibration_snapshot.json` persisted per run beside the markdown report: band counts,
  per-rule counts + deltas vs previous snapshot, score histogram, sentence metrics,
  fact-coverage percentages per field per category, per-test diag echo with red markers,
  config_rev mix (skew banner), pass timings + geom_method mix + boolean-failure counts.
- `labels.jsonl`: Nathan's spot-check verdicts persisted (clash id + rev + verdict) so
  demotion-rule precision accumulates across cycles; bottom-sample stride seeded from run
  seq so the same 30 rows do not recur forever.
- Doctrine unchanged: > ~25 open Criticals or a rack-retention increase after a rule change
  triggers a tuning session; tune rules, never cutoffs.

## 12. v1-vs-v2 comparison (how we prove improvement on THIS data)

Freeze the current NIUHTC `clashes.json` + v1 calibration report as fixtures. One offline
diff script (scratchpad, not shipped) produces: exact band-migration matrix, full
rule-migration matrix, decade histogram, Kendall tau per band, top-20 delta list, sentence
metrics, noise-share vs ~50% baseline, double-rescore determinism proof. Spot-check
protocol with Nathan (readable HTML sheet, not JSON): all 11 Criticals; 10 rows per new
sub-rule; every N-PT demotion; 15 N-DUP rows with thumbnails; 10 biggest within-band
movers; 10 rows from the newly-filled 40-49 decade; the dissolving groups in full. Blind
sentence A/B on 20 rows (target: v2 preferred >= 80%); demotion-safety question per
N-PT/N-DUP row (target >= 90% "comfortable if this never hits an agenda"; every no becomes
a tuning note). Answers persist to labels.jsonl.

## 13. Critique resolutions (why the merged plan says what it says)

| Conflict | Resolution |
|----------|-----------|
| M1 taxonomy (A 8 rules vs C 6 vs counterfactual 5) | A's 8 rule IDS frozen, but dispatch order corrected to MOVER-CLASS FIRST and populations re-derived from the counterfactual crosstab (verify pass caught A's table double-counting: its "EQ-SYS 250" included the both-equipment rows, and its listed order would have swallowed the fixture/FP/vent-vs-equipment populations); C templates re-pointed |
| N-DUP trigger vs measured 44 (verify pass) | Trigger frozen to the measured rule (names equal OR TYPICAL/STANDARD); TYPE CATALOG and 'none' are harness-measured candidates, never assumed |
| N-PT "31 rows" (verify pass) | Corrected: the five tokens reach ~17; the TYPICAL-electrical branch reaches ~27; "31" was unreachable as specified |
| N-PT ladder position | Inside M1 dispatch (below M4) — critic caught silent band-set expansion |
| Artifact suspects band | N-DUP Minor demotion (A) over keep-Major-with-wording (C) |
| C4 guards | B's set (partial-vs-structural stays Critical); A's "both movable" over-corrected |
| facts[] duplication | Stamp it (A/C); derived-only doctrine; unified row schema |
| code sub-score | 6 iff code_ref non-null (A) |
| resolve_by vocabulary | A's tokens + stamped display label (kills the client-side mirror) |
| sys_class gate | B's strict gate + subset-match for non-pipes |
| bbox rigidity | KILLED as-written; gated to non-LocationCurve elements only |
| importance.ai placement | KILLED; moved to top-level c['ai'] (now moot — AI deferred, section 10; kept for the future pickup) |
| M-SPR as specced | Split: wording now, numeric rule only with dedicated test (P4) |
| NEC "synthetic rows" | Re-scoped as the clearance-engine subsystem (P4) with its own identity rule |
| rev numbering | Single table, section 4 |

## 14. What v2 will NOT do

1. No cutoff moves; no sub-score crossing 70/40; no band renames.
2. No LLM-written ranking reasons; the reasons are deterministic templates, period
   (identical wording for identical fact-sets is a feature for meeting scanning). The whole
   AI layer is deferred (section 10); v2 has no AI at all.
3. No suppression on name heuristics; proof only.
4. No code citations without measured facts.
5. No trade-priority ladders or dollar cost-to-move estimates (nothing survived
   verification; fake dollars poison the real citations).
6. No per-project or per-code-edition knobs.
7. No midpoint/fingerprint changes; no reuse of midpoint for overlap centroid.
8. No ML relevance model on firm data (one project, zero arch rows: hopeless dataset;
   revisit only if a future labeled dataset ever exists).
9. No full 15-type penetration taxonomy or image classifier; four pen_class values cover
   every rule need.
10. No deletion of the brk bars (only explanation of within-band ordering; old records
    have nothing else).
11. No date-anchored deadlines (event-anchored vocabulary only; the tool has no schedule).

## 15. Open decisions (Nathan)

- **D1 — N-PT scope:** strict (8 EXIT rows), option A (+wall-device tokens, ~17 rows), or
  option B (+Electrical Fixtures named TYPICAL, ~27 rows)? Recommendation: option A now,
  option B only if the spot-check sheet confirms every TYPICAL-named row; exact counts
  come from the harness, and every demoted row appears in the sheet before rev 4 ships.
- **D2 — Spot-check session:** ~1 hour with the readable sheet + blind A/B after the P1
  harness passes. Scheduling it is the P1 ship gate.
- **D3 — P4 priority:** the clearance engine (NEC zones + sprinkler test) is the largest
  new subsystem for the smallest measured population. Recommendation: keep it last; revisit
  after P3's first real arch/struct run shows what the model actually contains.

(The former AI-model and API-governance decisions are removed: the advisory AI layer is
deferred out of v2 — section 10. They return if and when Nathan revisits AI.)
