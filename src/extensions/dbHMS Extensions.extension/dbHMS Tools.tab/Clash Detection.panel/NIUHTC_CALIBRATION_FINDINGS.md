# NIUHTC Calibration Findings (first real-project run)

**Date:** 2026-07-02. **Project:** MEP_21112_NIUHTC_R26 (hospital; MEP host
+ linked arch + linked structural). **Data:** 5,651 clashes (5,624 active),
349 groups, in the project's clashes.json.

> **STAGE 1 SHIPPED + RESCORED, 2026-07-02 evening (approved by Nathan).**
> Engine at config rev 2; the real NIUHTC data was rescored offline
> (backup: `clashes.backup-pre-rev2.json`; rev-1 report archived as
> `calibration_report.rev1.md`). Measured outcome: Critical 39 (untouched),
> Major 3,989 -> **1,747**, Minor 3,838 as named buckets (1,925
> mounting_check, 1,512 penetration_candidate, 48 field_fix). Agenda
> 740 -> **331**. Duplicate group titles 105 -> 1 (the coincident
> duplicated-floor pair, a model issue). Rack-retention check: 10 racks
> went hollow, all sleeve-dense wall zones (N1/N3-dominated), the benign
> case predicted in section 4; real racks kept their Major members.
> **STAGE 2 SHIPPED + RESCORED, 2026-07-03 (approved by Nathan).** Config
> rev 3 (backup: `clashes.backup-pre-rev3.json`; rev-2 report archived).
> Measured: Critical 39 -> **36** (the three audit-flagged arguable C3s:
> two 2-in vents, one equipment row - all now Major M1), Major 1,747 ->
> **1,551**, M3 210 -> 62 with **148 N4** drain/imminent-penetration
> rows, agenda 331 -> **294**. Hollow racks 10 -> 18 (the drain-dense
> zones joining the sleeve-dense ones; same benign class, tracked as a
> trend in calibration_report). All numbers within a few rows of the
> deep-dive simulation.
**Method:** 9-agent forensic workflow: full engine recompute of all 5,651
rows, rule-by-rule autopsy, grouping integrity audit, industry/code
research, three retuning proposals SIMULATED against the real data, two
judges. This document is the Phase-5 calibration artifact for both the
scoring engine (CLASH_IMPORTANCE_RESEARCH.md) and grouping
(CLASH_GROUPING_DESIGN.md).

---

## 1. Verdict

**The engine is correct; the calibration has one big domain gap.** A full
recompute reproduced every stamped rule, band, suppression, and cluster
count exactly (0 mismatches in 5,651). Grouping rollups, membership
partition, and per-clash stamps audited perfectly clean (0 mismatches in
349 groups). The 3,989 Majors are real behavior of the shipped rules; the
problem is that the rules treat **hosted fixtures as routing conflicts**.

## 2. The Major autopsy (3,989)

- **~2,400-2,600 are by-design mounting/bearing adjacency**, dominated by
  M1 (1,766 of 2,516 = 70%): recessed light troffers inside the acoustic
  ceilings they are hosted by (461), radiant ceiling panels in ceiling
  grids (429), floor-mounted equipment standing on slabs (327), wall-hung
  lavatories/WCs in their carrier walls (154), floor boxes in slab (100),
  air terminals in ceilings. These take rigidity 4 via EQUIPMENT_CATS,
  fail N1's curve-category gate, and land M1 Major.
- **M4 (1,051):** 542 genuine multi-trade rack congestion; 269 wall/floor
  penetrations promoted out of N1 Minor by congestion; 336 promoted out of
  FB.
- **M3 (210):** 93 are floor/roof drains at gap exactly 0.0 to the slab
  they drain (by design); ~12 genuinely useful near-misses to steel.
- **M2 (212):** roughly half is equipment bearing on foundation slabs and
  recessed fixtures within beam depth.
- **Genuine, defensible Major is roughly 1,300-1,500.**
- All 591 degraded-confidence rows have exactly one cause: pipe/duct
  FITTINGS carry no dims_in (enrichment gap, capturable later).
- Bonus find: the arch model contains at least one duplicated floor
  element, so 7 real conflicts appear as 14 rows in two coincident groups
  (model-quality signal worth reporting to the architect).

## 3. The Criticals (39) are largely real

- **C1 (13):** all genuinely gravity vs the structural link: storm leaders
  and waste mains (4 in) against W-shape framing and the foundation slab.
  One mispositioned waste main alone generates 7 of the 39 (4 beams in a
  row plus 3 return-duct crossings).
- **C2 (12):** clean; 30-60 in roof ducts vs steel framing.
- **C3 (14):** ~11 real (big ducts vs storm leaders under the roof);
  ~3 arguable, caused by 2-in vents carrying rigidity 4 regardless of
  size and equipment class rigidity making equipment-vs-waste "dual
  no-slack."

## 4. The retune (RECOMMENDED-PACKAGE, both judges' winner: 86 and 90)

Simulated end state on this real project: **Critical 39 -> 36, Major
3,989 -> 1,528, Minor 1,596 -> 4,060** where Minor is three NAMED buckets:
2,023 mounting checks (flag `mounting_check`), 1,568 sleeve/penetration
items (`penetration_candidate`), 469 plain. Agenda items (groups plus
Critical/Major singletons) drop **740 -> 300**. No row moves UP a band;
nothing new is suppressed (suppression stays 27; visible-Minor beats
suppression until positive host-relationship capture ships).

**Stage 1 (the consensus core; never touches Critical; lands Major ~1,700):**
- P1 mounted-class split: EQUIPMENT_CATS shrinks to Mechanical/Electrical
  Equipment (rig 4). New MOUNTED_CATS (Plumbing Fixtures, Electrical
  Fixtures, Lighting Fixtures, Air Terminals) at rigidity 2, class
  'fixture'. Explicit DISCIPLINE_BY_CAT entries added.
- P2 new tier rule N3 "mounting adjacency" (hard; mover class
  equipment/fixture; counterpart arch wall/floor/ceiling/roof, structural
  FOUNDATIONS bearing, or fixture-vs-framing recessed) -> Minor +
  `mounting_check`. Placed before M2/M4.
- P3 N1 evaluates before M4 (penetrations go to the sleeve schedule even
  inside congested zones; racks keep their 542 both-MEP Major members;
  verified no rack loses its last Major/Critical member).
- P6 _fixed_kind override, SHIPPED BROADER than approved (deliberate,
  review-flagged): EVERYTHING in link:Structural reads structural, not
  just Walls/Floors. Rationale: nothing in a consultant's link is dbHMS's
  to move, and an embed/sleeve modeled in the structural link is still
  structural context; pinned by
  test_mep_category_in_structural_link_keeps_c1. Fixes the 97
  structural-deck sleeve-wording rows either way. If a narrow
  Walls/Floors-only rule is preferred, it is a one-line gate plus a
  rescore.
- Judge patches: switchgear name-gate (MSB/CT/TX/switchboard vs walls
  stays M1); hard fixture-vs-MEP-curve rows keep M1 when the counterpart
  rigidity >= 3 (RCP-locked troffer vs duct: the duct moves); `field_fix`
  flag on the M1->FB residue; Roofs included in the surface gate.

**Stage 2 (next run boundary, after a calibration re-check; moves Critical):**
- P4 M3 split: drain-at-its-slab shapes -> Minor N4 + mounting_check;
  vs-structure and both-movable stay Major.
- P5 C3 tightening: equipment class never satisfies C3; vents with known
  diameter under 3 in drop to rigidity 2.

**Process requirements (from the engineer judge):** rescore at a run
boundary with config rev 2 and a run-history note ("step-down is a rule
change, not resolved work"); archive before/after calibration reports;
freeze ~25 representative rows as CPython fixture tests plus a
no-row-moves-up invariant; re-run calibration on the NEXT project before
declaring the firm standard settled, watching N3's equipment-vs-walls
slice (75 rows) by name.

## 5. Grouping audit results

Working as designed: rollups/stamps/partition perfectly clean; the 103
cluster groups are coherent room-scale racks (median 15.5 ft diagonal,
78% multi-trade); 91% of actives grouped; 6.6x compression to 851 agenda
items. The 155-member group = formation at the 150 cap + 5 legal P6
adjacency joins.

Improvements queued (approved as part of this calibration):
1. **Titles:** 30% of groups share a duplicate title (worst: nine
   identical ceiling-vs-lights titles ~70 ft apart). Fix: append a short
   centroid-derived locator; separates 40 of 41 duplicate sets (the 41st
   is the duplicated-floor model issue).
2. **Agenda cards hide the reason a rack is Critical** (1-4 critical
   members buried in 100+ rosters). Fix: surface rollup.reason on the
   card; consider listing Critical members individually even when grouped.
3. **drifted flag:** structural noise on element stars (151/246 flagged on
   day one). Fix: compute only for cluster/manual axes.
4. **Agenda tie-break degenerate:** all 349 groups share one second-level
   created_at. Fix: mint a formation seq for stable ordering.
5. Note: P6 growth past the member cap is unbounded across runs
   (formation-only cap is by design; watch it).

## 6. UI defects noticed on real data (Phase-3 fix list)

- Home banner is hardcoded sample text ("Central Hospital (sample)",
  "12 auto-closed, 3 reopened, 41 new") while real data is loaded; wire it
  to the real project name + merge summary.
- Window subtitle still says "rebuild - phase 1 (sample data)".
- Rules N3/N4 and flags mounting_check/field_fix need UI strings once the
  retune ships.

## 7. The story for engineers

"The tool found 5,651 raw overlaps. It filtered and ranked them into 851
issues; your trade owns its share of ~300 assigned coordination issues,
36 Criticals first. The other ~3,500 rows are two named schedules, 1,568
sleeve penetrations and 2,023 mounting checks, handled as batch passes,
not as your queue." Research grounding: hospital-scale jobs normally see
raw-to-issue compressions of this order; mature teams exclude or
auto-close hosted-fixture pairs before anyone reads a row.
