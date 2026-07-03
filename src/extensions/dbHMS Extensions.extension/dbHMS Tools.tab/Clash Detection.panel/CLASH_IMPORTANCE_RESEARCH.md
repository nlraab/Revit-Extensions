# Importance Scoring: Research Findings and Recommended Design

> Companion to `CLASH_REBUILD_SPEC.md`, section 6. Written 2026-07-01 after a
> deep research pass that Nathan requested before committing the importance
> engine design: 30 agents, ~5.5 hours of wall-clock research across ten
> parallel threads (Navisworks, Revizto, Solibri, ACC/BIMcollab, the wider
> market, academic literature, MEP coordination practice, US codes, published
> BEP severity conventions, and coordinator-trust research), three independent
> competing designs, a three-judge panel, adversarial verification of the
> load-bearing claims, and a completeness critique.
>
> Status: **recommendation, awaiting Nathan's decisions.** The spec has NOT
> been edited. Section 14 lists what needs his call. If adopted, spec section
> 6 and parts of the section 2 decisions table should be rewritten to match
> section 9 below.

---

## 1. The verdict in one paragraph

The three-layer shape in the spec (noise rules, deterministic transparent
score, grouping) survives the research fully intact; it is the right
architecture and it is genuinely novel, because no shipping tool computes any
per-clash importance score. But the Layer B formula as written should not be
built. The "who moves" ladder with structure on rung 5 is wrong (Nathan's
challenge is confirmed from five independent directions), the flat weighted
sum has a documented trust failure mode (band assignments silently drift when
weights change or neighbors resolve), penetration depth and overlap volume
are the weakest signals in the formula rather than the strongest (the "CVSS
trap"), and the shipped ladder ordering itself contradicts published
practice in places (sprinkler vs conduit). The replacement: a short ordered
list of tier rules that assign Critical/Major/Minor directly, each with a
one-sentence plain-English reason, with a small clamped sub-score that only
orders clashes within a band. Structure and architecture never appear on any
ladder; they are context that selects which rule fires.

---

## 2. Nathan's structure question, answered

Nathan asked: "We're an MEP firm. We can't move structure. If a pipe
conflicts with structure, the clash should be judged by the pipe's
constraints, because the pipe is what must move. So why is structure on the
who-moves ladder?"

He is right, and this is now one of the best-supported conclusions in the
whole dossier. Five independent confirmations:

1. **BIMForum BXP 2020**: hierarchy is "established with the objects that are
   most difficult or expensive to move as having precedence." Structure tops
   published matrices precisely because it never moves; it is the frozen
   boundary others route around, not a participant.
2. **Revizto's own guidance**: "the most moveable element of the clash
   becomes accountable for solving it."
3. **Solibri's severity engine** (the only shipping rule-graded severity):
   never asks who moves. It grades by what the components are, and
   architectural involvement DEMOTES severity (walls get openings; routine
   penetrations), the exact opposite polarity of the spec's structure rung.
4. **Academic who-moves research** (Harode/Thabet 2024, ~80% accuracy
   predicting which element moves): the moving element is predicted from
   per-instance flexibility features (free space, connectivity, system,
   size). No published model puts immovable structure on a priority ladder.
5. **Field ethnography** (Mehrbod, 90+ real coordination meetings):
   structural teams resolve design discrepancies, not clashes; the flexible
   trade accommodates.

The refinement that keeps what the old rung was trying to express: a clash
with structure IS usually more serious than the same MEP pair clash, but not
because structure "ranks." Two reasons with data behind them:

- **No meet-in-the-middle.** Between two MEP elements, dbHMS owns both sides
  and can split the difference. Against steel or concrete, 100% of the fix
  lands on the MEP element.
- **The fix window closes.** Sleeves must be cast before the pour, steel
  penetrations must be in the fabricator's shops drawings. Missing that
  window means x-ray and coring (AU CS125677). MEP-vs-structure clashes are
  schedule-critical, which is why practice coordinates them first.

So structure enters the model as a context flag (fixed, kind=structural) that
selects a more severe tier rule for the mover, and the severity within that
tier comes from the mover's own constraints. A gravity main vs a beam is
Critical because slope is code-fixed and steel will not move. A 3/4 in
conduit vs the same beam is minor because the conduit bends around it for the
cost of two offsets.

One addition the research surfaced that the original framing missed
completely: sometimes the MEP element CANNOT move either (no fall left, shaft
locked), and the real resolution is a negotiated slab opening or beam web
penetration owned by another firm. Those clashes have the longest external
lead time and must reach the meeting earliest. The recommended design's top
tier (gravity vs structure) captures most of these, and the reason string
should say "may require a structural penetration request."

---

## 3. What the market actually does (nobody scores)

Verified against primary documentation for every product:

| Tool | Importance mechanism | Noise mechanism |
|---|---|---|
| Navisworks Manage | None. "Severity" = signed distance (penetration depth), sort the column. | 6 binary ignore rules + 5 templates (same file/composite/insulation...), tolerance floor on \|distance\|, Approved status persists across runs |
| Revizto | Priority enum (Blocker..Trivial) set by user-authored IF/THEN/OTHERWISE rules, never computed | Ignore rules (same model, room/space objects, search sets), grouping (proximity 15 m default, level, grid, zone), "Don't create issue" |
| Solibri | 3 severity classes (Critical/Moderate/Low) assigned per rule; hard-clash severity from orientation + arch involvement + component smallest dimension (25 mm / 200 mm cutoffs), NOT from depth/volume | Volume/horizontal/vertical tolerances as pass/fail gates; per-cell matrix severity + ignore |
| ACC Model Coordination | None. Cell heat-color by group count | Depth tolerance filter (below/above/within), auto-grouping by primary object + multi-property, "Not an issue" |
| BIMcollab Zoom | None. Manual priority field | Clash-box size/volume minimums, grouping (component + grid + level), Smart Issues identity across re-runs |
| Bentley iTwin | None (no severity field at all) | Suppression rule templates down to freeform ECSql |
| Newforma Konekt | Manual color-coded priority; precedence encoded in test NAMES ("F: HVAC vs STR") | Status sync, grouping by selection then grid/level |
| ClashMEP (BuildingSP) | Published a score ROADMAP in 2017 (heuristics + congestion + ML feedback); never published accuracy; niche | Attribute/volume filters |
| AI startups (Firmus, Kreo, BIMLOGIQ, Sortdesk) | None do 3D clash prioritization (Firmus scores 2D drawings; others pivoted) | n/a |

Three lessons beyond "the 0-100 score is unoccupied ground":

- **Everyone's real noise weapon is grouping, not scoring.** 500-1000 raw
  clashes collapse to ~20 root causes; the meeting agenda is the top 10-20
  GROUPS. iConstruct charges money specifically for retaining groups across
  re-runs. A perfectly scored flat grid of 500 rows still fails the meeting.
- **Everyone's triage vocabulary is binary plus bands.** Not-an-issue /
  Approved plus at most 3-4 ordinal severity classes. Coordinators filter by
  bucket; nobody sorts by a number. The bands are the interface; the number
  is a tiebreaker within a band.
- **Suppression is always visible and reversible** in the tools people
  trust (ACC Closed tab, Suppressed counts, audit views).

## 4. What practice and published standards say

- The canonical trade priority ordering (Korman/Tatum, Stanford CIFE WP054,
  Table 3) is a constraint ladder with explicit reasons: gravity systems
  ("slope essential, graded 1/8 in per foot"), then size, then flexibility,
  with electrical "most flexible routing, especially small diameter conduit."
  Practitioner sources DISAGREE on details the spec treated as settled:
  Helonic puts fire sprinkler LAST (below conduit); the spec's draft ladder
  puts sprinkler above conduit. Bus duct and racked conduit banks are
  documented as nearly immovable despite being "electrical" (Brett Young:
  "one conduit is flexible; multiple conduits are not"). Conclusion: ship the
  ladder as an editable table with the Korman baseline documented, and treat
  rack-vs-single-run as a real signal, not trade labels.
- Published owner standards define severity as ordinal bands whose real
  meaning is a RESOLVE-BY DEADLINE, never a number. Ashghal (Qatar PWA
  S0301): element classes A/B/C, severity from the pair (AxA/AxB = Level 1,
  "resolved before detailed design stage"). Indiana University: Level
  One/Two/Three as explicit pair lists (Level One: duct/pipe vs structure, vs
  ceilings, vs rated walls, equipment + clearances vs walls/structure).
  Nothing numeric anywhere. Our Critical/Major/Minor chips should carry
  action semantics ("meeting agenda this cycle" / "assign this cycle" /
  "detailing, no meeting time").
- Tolerance conventions: hard-clash noise floor 0-10 mm, 25 mm (~1 in) as
  the "meeting-worthy rule of thumb"; clearance defaults by pair (duct vs
  structure 50 mm, tray vs duct 100 mm, sprinkler vs beam 150 mm, maintenance
  access 600 mm). IU/LACCD require equipment access zones modeled as
  invisible solids and clash-checked as first-class geometry.
- Meetings: top 10-20 groups, 20-40 issues max, 5-10 minutes per major item,
  and the meeting ratifies pre-vetted solutions. Coordination sign-off
  drawings are contractual (NBIMS 5.5.4.11: install off-drawing, relocate at
  your own expense).

## 5. What the codes say (movability is physics plus code, not trade)

The code research produced an objective movability ordering, least movable
first, with the governing constraint for each:

1. **Kitchen grease duct**: IMC 506.3.7, 2% slope toward hood (8.3% over
   75 ft), welded liquid-tight, rated enclosure.
2. **Sanitary/storm gravity mains**: IPC Table 704.1 (1/4 in/ft at 2.5 in
   and under, 1/8 in/ft at 3-6 in, 1/16 in/ft at 8 in+). A 100 ft run of 6 in
   main has ~12.5 in of total fall to spend, ever.
3. **Condensate**: IMC 307.2.1, 1% slope.
4. **Dry/preaction sprinkler pipe**: pitched to drain per NFPA 13.
5. **Vents**: IPC 905, graded to drip, 6 in above flood rim before offset.
6. **Large duct**: no code slope, but space is space, insulation adds up to
   100 mm per side, and seismic bracing (NFPA 13 / ASCE 7) raises the cost of
   moving braced mains.
7. **Pressurized pipe**: reroutes with offsets, costs fittings and head.
8. **Sprinkler branch/heads**: heads slide in plan within spacing envelopes
   (max 15 ft, min 6 ft), but deflectors are vertically locked and OTHER
   trades entering the head's obstruction envelope (NFPA 13 three-times rule,
   18 in rule) is a code violation with zero geometric contact.
9. **Conduit/tray**: most reroutable, but each dodge spends bend budget (max
   360 degrees between pull points) and tray must stay exposed/accessible
   (NEC 392.18; 12 in clear above tray in the 2026 NEC).

Two further findings that should shape the roadmap rather than the score:

- Several constraints are keep-out ZONES, not intersections: NEC 110.26
  working space and dedicated space above panels, sprinkler obstruction
  halos, damper access (IMC 607.4, 12 in square), equipment service space
  (IMC 306.1, 30x30 in), 80 in headroom / 4 in protrusion on egress paths.
  These are future default CLEARANCE TESTS (spec 5.1's cell editor), not
  score inputs. A clearance-test hit should carry a code-risk term in the
  score, because it predicts an inspection failure, not a graze.
- Code-required insulation is real geometry (chilled water ~1-1.5 in, heating
  hot water 2 in per ASHRAE 90.1/IECC). An uninsulated-model near miss of
  1 in against chilled water is a field hard clash.

## 6. What academia says

- The field splits the problem exactly as our Layer A / Layer B does, and
  finds the halves differ in difficulty: binary relevance filtering reaches
  80-88% precision, graded RISK prediction only ~70% (Koo et al., JCEM 152(7)
  2026). A deterministic transparent Layer B is therefore defensible; no
  paper publishes a 0-100 score either.
- Noise fractions: 40-60% of raw clashes are irrelevant (Lin & Huang's
  labeled set: 58%). The published clash taxonomy is errors / pseudo
  (tolerable) / deliberate (intended penetrations) / duplicates.
- Rules-first is validated: 5 expert rules at only 60% standalone accuracy
  still lifted downstream ML by 6-17% when their verdicts were stored as
  features (Lin & Huang). Store which rule fired in the feature vector.
- The consistent published feature set, which is our feature_vector
  checklist: element category pair, system types, sizes, materials,
  penetration distance, overlap dimensions/volume, clash XYZ + level/zone,
  host structural member type and position-within-member, connected-element
  count, clash-group membership, free space around the elements, duplicate
  cluster size (one real clash repeated 24 times in Parn's data).
- 93% of real MEP-vs-structure overlaps fall in 30-299 mm (Parn & Edwards),
  so when penetration depth ships, its thresholds belong in that range.

## 7. The trust research (why the weighted sum loses)

- **Google Tricorder** (the best-documented automated-review adoption study):
  automated findings survive only when understandable, actionable, and under
  a 10% "effective false positive" budget, where a false positive is any
  finding the user takes NO ACTION on. Private/invisible suppression
  destroyed trust and hid real bugs; suppression must be shared and visible.
- **Algorithm aversion** (Dietvorst et al.): users abandon a ranker after one
  visible absurd result, faster than they abandon a human making the same
  mistake. The coordinator will audit the top 10 in week one and judge the
  entire engine by it.
- **The CVSS lesson** from security triage: a severity score built on
  intrinsic magnitude (how big is the hole) rather than predicted action
  (will this force rework) becomes a number everyone learns to ignore. Only
  5-6% of CVSS-Critical vulnerabilities are ever exploited; action-predicting
  models displaced it. Penetration depth and overlap volume are our CVSS
  trap: real signals, but weak ones, and they must never carry the score.
- Direct design consequences: every clash needs a one-line reason; a clash's
  BAND must never change without a visible cause (a weighted sum fails this
  by construction: any weight tweak or a resolved neighbor can walk a chip
  across 69/70 silently); and re-run stability is the single most documented
  trust killer in existing tools (Navisworks losing Approved status is its
  most-complained failure; BIMcollab's whole marketing pitch is identity
  persistence, which our fingerprints already beat).

---

## 8. How we got the recommended design

Three complete competing designs were built independently, each grounded in
the full dossier: (A) resolution-cost weighted factors, (B) field-consequence
weighted factors with a code-risk term, (C) constraint-first tier rules.
Three judges (veteran-coordinator lens, universality lens, explainability
lens) scored all three. All three judges independently picked C as the
winner (84/100 from each), for the same reason: it is the only design where a
band can NEVER change because a weight slider nudged a sum across a cutoff.
Bands move only when a rule input changes, which is a visible cause by
construction. They then specified the same grafts from A and B, which are
folded in below.

## 9. Recommended design

### 9.1 Shape

- **Layer A**: ordered, first-match-wins suppression rules (Revizto's
  IF/THEN/OTHERWISE evaluation model). A suppressed clash is NEVER dropped:
  it keeps its fingerprint, status, comments, history, gets a full computed
  score anyway, and is stamped `suppressed` + rule id + reason. The UI hides
  suppressed rows behind a visible "Suppressed (n)" count with one-click
  reveal and per-rule counts.
- **Layer B**: ordered tier rules assign the band (Critical/Major/Minor)
  directly. Each rule carries a one-sentence reason template with the code
  citation baked in. A small sub-score, clamped so it cannot cross a band
  boundary, orders clashes within the band.
- Both layers run as pure functions in a new `lib/clash_score/` module
  (zero Revit imports, IronPython 2.7 + CPython 3 compatible, tested in
  `tests/test_clash_score.py` with the standard `_LIB` shim), executed at
  merge time AND re-executable at any time from stored features
  (`rescore_all`), so a Settings change re-ranks in milliseconds without a
  detection run.

### 9.2 Participants and the mover

Each ref gets two derived facts:

- **fixed**: true when the element is not dbHMS's to move. Decide by
  CATEGORY first (the `categories.py` table maps OST_StructuralFraming etc.
  to Structural regardless of which link it came from, which handles
  structure delivered inside the arch link), falling back to link role.
  `fixed_kind` = structural or architectural.
- **rigidity 0-5** for movable MEP, from system classification + system name
  + size, with a category-only fallback so old records still classify
  (`rigidity_src` records which path fired):
  - 5 no-slack: grease/kitchen exhaust duct (name match); gravity drainage
    3 in and larger (classification Sanitary, plus storm; see 9.5)
  - 4 sloped/huge: gravity under 3 in, Vent, condensate, FP dry/preaction,
    duct with max dimension 24 in+, med-gas
  - 3 bulky/racked: duct 12-24 in, pressurized pipe 4 in+, cable tray
  - 2 standard: duct under 12 in, pressurized pipe under 4 in, FP wet branch
  - 1 slideable: conduit over 1 in, sprinkler heads
  - 0 field-flex: conduit 1 in and under, flex, MC
  - category fallback: duct 3, pipe 2, tray 3, conduit 1, sprinkler 2, MEP
    equipment 4, unknown 2

**Mover selection**: fixed elements are never the mover. MEP vs fixed: the
MEP element moves. MEP vs MEP: the lower-rigidity element moves. The
sprinkler-vs-conduit rung order is disputed in the sources; the table ships
editable with the Korman baseline documented.

### 9.3 Layer A rules (ordered)

1. **R-SELF**: same element on both sides (same source + element_id, or same
   unique_id). "Element intersecting itself: modeling artifact."
2. **R-NOT-OURS**: both refs from links (neither is host). "Both elements
   belong to linked consultants; dbHMS can move neither. Flag to the design
   team outside the clash grid." This is the firm-identity rule: the grid
   contains only what dbHMS can act on.
3. **R-SYS**: hard clash, both host, both system names present and equal,
   AND at least one participant is a fitting/accessory (the fitting
   requirement protects a genuine routing error where two branches of one
   system cross). "Same {system} run touching itself at a fitting seam."
4. **R-FIELD**: both refs are conduit with known diameter 1 in and under
   (single runs, not racks), and local cluster count < 10. "Single small-bore
   conduit: field-routed by the installing trade." Deliberately NARROW:
   conduit-vs-anything-else stays visible and scores low via rigidity
   instead, because suppression that backfires once (a cast-in conduit, a
   panel feeder) discredits the whole layer. Cite NBIMS-US V3 sec 5.5.4.8 +
   Annex A Note 1 (1.5.1) for the conduit floor ONLY; note the same Annex
   requires racks of 2+ conduits of any size to be modeled (hence the narrow
   rule + cluster escape). Flex/MC exemptions are BEP practice, not NBIMS;
   ship as a tunable toggle grounded in the project BEP.
5. **R-GRAZE** (DORMANT until penetration depth ships): hard clash, depth
   under 3/8 in (the sourced 10 mm practitioner noise floor), mover rigidity
   3 or less, never against structure. Ships now with config key and tests;
   activates automatically when the runner emits depth.

### 9.4 Layer B tier rules (ordered, first match wins)

Critical (band base 70):
- **C1**: hard, other side fixed structural, mover rigidity 5. "Gravity
  {sys} vs structure: slope is code-fixed (IPC 704.1), no vertical freedom.
  May require a structural penetration request."
- **C2**: hard, other side fixed structural, mover rigidity 4, AND (mover
  max dimension 24 in+ OR clearance/code-test kind). Otherwise falls to M2.
  Gating C2 keeps Critical stingy on duct-heavy jobs.
- **C3**: hard, both movable, both rigidity 4+, at least one rigidity 5.
  "Two no-slack systems: no cheap mover." (Both-rigidity-4 without a 5 is
  Major, not Critical.)
- **C4** (DORMANT): penetration depth 6 in+. "Deep systemic overlap."

Major (band base 40):
- **M-CODE**: clearance-kind test result (a code-driven test: NEC working
  space, sprinkler halo, access zone). "Code clearance consumed: predicts an
  inspection failure, not a graze." Adds the code-risk sub-term.
- **M2**: hard vs fixed structural, mover rigidity 2-3. "{elem} must
  drop/rise around steel; pre-pour/fabrication deadline."
- **M4**: cluster_n at or over 20, with hysteresis (fires at 20, releases
  below 12, so neighbor resolution cannot flap the band). "Congested zone:
  {n} clashes within 5 ft: rack-level rework."
- **M1**: hard, any participant rigidity 4+, or mover rigidity 3. "Reroute
  around a large obstruction needs a coordinated path."
- **M3**: soft, gap 25% of tolerance or less, mover rigidity 2+. "Clearance
  nearly consumed."

Minor (band base 8):
- **N1**: other side fixed architectural (wall/floor/ceiling/roof), mover an
  MEP curve. Minor + `penetration_candidate` flag. "Likely intended
  penetration: confirm sleeve/damper (IMC 607.4)." The flag makes these
  groupable as a sleeve-schedule pass, which is how sleeves actually get
  coordinated. When wall fire-rating capture ships, rated walls promote to
  Major (damper + firestop scope is real money).
- **N2**: insulation category involved, or soft gap smaller than combined
  insulation thickness. Minor, deliberately NOT suppressed (chilled-water
  vapor barrier pinches are real field clashes).
- **FB**: mandatory otherwise, Minor. Old/degraded records append
  "(estimated from categories; re-run for system/size detail)".

### 9.5 Gravity/storm detection (verified correction)

Revit has no usable Storm classification through 2026 (the API enum value
exists but is documented "Reserved for future use"). Storm systems typically
inherit Sanitary when duplicated, so CLASSIFICATION-PRIMARY detection
(classification in {Sanitary, Vent} = gravity) already catches most storm
mains with no name matching. The configurable name/abbreviation regex
('storm', 'ST', 'RD', 'OVFL'...) is a BACKSTOP for storm modeled under
Other or Domestic Cold Water (a documented practitioner alternative), and
nonzero RBS_PIPE_SLOPE is a third OR-able signal. Pipe mains classified
Other/Undefined should surface as "unclassified gravity candidate" rather
than silently demoting. A misread here flips a band, so this classifier
needs its own unit tests more than anything else in the module.

### 9.6 Within-band sub-score and the breakdown UI

`score = band_base + clamp(constraint + bulk + geometry + congestion + code,
0, band_width - 1)`

- constraint = 3 x mover rigidity (0-15)
- bulk = largest participant max dimension: 24 in+ = 8, 12+ = 5, 6+ = 3,
  else 1, unknown 2
- geometry = hard: flat 4 until depth ships, then min(8, 2 x depth_in);
  soft: round(6 x (1 - gap/tol)); contact 6
- congestion = cluster_n 20+ = 6, 10+ = 4, 5+ = 2 (hysteresis on thresholds)
- code = 6 for clearance-kind or gravity/grease mover, 4 for rated
  counterpart, else 0

Inspector layout: tier chip + the reason sentence FIRST (that is what gets
read aloud in the meeting), then bars for the sub-terms only, all capped at
comparable ranges. Do not render the tier itself as a 70-point bar; the bars
explain ordering within the band, the sentence explains the band. `brk`
becomes an array of `{k, v}` objects; coord.html reads brk at exactly one
site (selectClash, ~lines 547-549), so this is a one-site change plus the
provisional-score fallback that already exists. The 70/40 cutoffs live in
four places in coord.html (band(), renderHome(), the static card sublabels,
and the "Important (>= 70)" preset) and stay numerically the same; the
preset's semantics become "Critical tier."

Near-threshold honesty: when a dimension read sits within 15% of a rigidity
class boundary, append "(near size threshold: verify)" to the reason rather
than pretending precision.

### 9.7 Data capture and schema (verified against the code)

Captured at detect time inside each ref (in `runner._make_ref`, where the
live Element, its Document, and its RevitLinkInstance are all in scope):
`sys_class`, `sys_name`, `sys_abbr`, `dims_in` ([dia] or [w, h]), `ins_in`,
`level`. All nullable, all try/excepted. Refs are wholesale-replaced on
persisting rows at merge (merge.py:162-165), so enrichment refreshes every
run with zero fingerprint impact: the fingerprint hashes only test_id + the
(source, element_id) pair + the 1 ft bucketed midpoint (identity.py:40-83,
verified). Add the regression test that persisting rows pick up enriched
refs and that fingerprints ignore extra ref keys (currently untested).

**Insulation (verified correction)**: do NOT read
`RBS_REFERENCE_INSULATION_THICKNESS`. It is officially obsolete in Revit
2025/2026 (replaced by `DUCT_INSULATION_THICKNESS` /
`PIPE_INSULATION_THICKNESS`, which do not exist in 2024), and the host-level
read returns 0 on insulated fittings even where live. The correct,
version-stable path (API stable since 2012, covers curves + fittings +
accessories + linked docs): one `FilteredElementCollector` pass per document
over `DuctInsulation` + `PipeInsulation`, building a HostElementId ->
Thickness map. One query per document, cheap.

**Per-run pair fields**: add `penetration_depth_in` and `overlap_volume_cf`
to the `_PER_RUN_FIELDS` tuple in merge.py (that tuple edit is the actual
enabling step; the runner does not need to emit explicit nulls, since
`raw.get(k)` supplies None and merge materializes the keys on every
persisting and new record). When hard.py later computes depth (meshdist
extension) and volume (BooleanOperationsUtils on the already-collected
solids, gated to confirmed hard pairs), values flow into re-appearing
records with no migration. Auto-resolved records never gain the keys, so all
rules read them with .get().

**Importance block** stamped on each clash at merge time by
`clash_score.score_all(clashes, config)` (cluster_n is pure midpoint math
over the merged list):

```json
"importance": {
  "v": 1, "config_rev": 1,
  "tier": "critical", "rule": "C1", "band": "Critical", "score": 91,
  "reason": "Gravity sanitary main vs structural beam: slope is code-fixed (IPC 704.1) - no vertical reroute freedom.",
  "brk": [{"k": "Mover constraint", "v": 15}, {"k": "Size", "v": 2},
           {"k": "Geometry", "v": 4}, {"k": "Congestion", "v": 0}, {"k": "Code", "v": 6}],
  "suppressed": false, "suppress_rule": null, "suppress_override": null,
  "flags": [], "confidence": "full",
  "features": {"rigidity_a": 5, "rigidity_b": null, "fixed_b": "structural",
                "mover": "a", "rigidity_src": "system", "cluster_n": 0,
                "gap_ratio": null, "bulk_in": 4.0}
}
```

`features` is the persisted feature vector for the future ML phase (spec
section 6 requirement); which rule fired is itself a stored feature (the
Lin & Huang lesson). `suppress_override` (force-show / force-suppress per
clash, persisted via fingerprint, beats all rules) is the coordinator escape
hatch and doubles as labeled training data. Any fallback path sets
`confidence: "degraded"` and the UI appends a re-run note.

**Null principle** (stated as a tested invariant): a rule fires only when
every feature it tests is non-null. Suppress only on proof. Old records can
never be suppressed by data they lack, and an all-unknown clash against
structure lands at the Major floor, flagged, never buried, never falsely
Critical.

### 9.8 Bands mean deadlines

- **Critical (70+)**: meeting agenda; resolve before the next coordination
  cycle. Healthy steady state is 20 or fewer open Criticals (the documented
  agenda size). Too many means tune tier RULES, never cutoffs.
- **Major (40-69)**: assign and coordinate this cycle; a decision between
  trades is needed; known fix pattern exists.
- **Minor (under 40)**: detailing/field; close out before shop drawings; no
  meeting time.
- **Suppressed** is not a band; suppressed rows keep their computed would-be
  score and re-enter instantly if a rule is toggled off.

### 9.9 Trust plumbing

- One-click "not important" on any clash, monitored per tier rule. The
  Tricorder budget: if more than ~10% of Critical items get no action, the
  responsible rule gets revisited. This is the feature's health metric.
- Suppression always visible, attributed, reversible (see 9.1).
- A band change between runs must have a visible cause; the clamp guarantees
  weight edits cannot cause one, hysteresis guards the cluster thresholds,
  and the reason sentence names the rule so a change reads as "the input
  changed."

### 9.10 Worked examples (the sanity row)

| Scenario | Result | Why |
|---|---|---|
| 4 in sanitary gravity main vs steel beam | Critical 91 (C1) | Slope is code-fixed; steel will not move |
| 6 in storm vs deep beam, bay-length overlap | Critical 96 (C1 + congestion) | Systemic beats point clash |
| 24x20 duct main vs 4 in domestic (both host) | Major 58 (M1) | The pipe moves; real coordinated reroute, not agenda |
| Cable tray vs beam | Major 61 (M2) | Trays drop under steel routinely, but access implications |
| Corridor rack graze, 30 clashes in 5 ft | Major 55 (M4) | Congestion converts field noise to rack rework |
| Duct through arch wall | Minor 26 + penetration_candidate (N1) | Sleeve-schedule item; promotes to Major when rated-wall capture ships |
| Duct touching arch ceiling | Minor 28 (N1) | Ceiling coordination, never meeting time |
| Insulation pinch | Minor 23 (N2) | Real but detailing; never suppressed |
| Sprinkler branch 1/2 in from tray (1 in test) | Minor 19 (FB) | Half the clearance remains; branch slides |
| 3/4 in conduit grazing a duct | Minor ~17, visible | Narrow R-FIELD keeps conduit-vs-other visible; rigidity keeps it low |
| Same-system elbow self-clash | Suppressed (R-SYS), scored, visible in audit | Navisworks/Revizto-proven noise class |
| Old record, categories only | Major 52, confidence degraded | Ranks sensibly, confesses uncertainty, prompts re-run |

---

## 10. What must change in the spec if adopted

- Section 6 Layer B: replace the weighted-sum formula and the six-rung
  ladder with the tier-rule model (section 9 here). The ladder becomes the
  movable-MEP rigidity table; structure and architecture come off it.
- Section 6 Layer A: replace the representative rules with the five rules in
  9.3, and add the disposition contract (never dropped, always scored,
  visible count).
- Section 2 decisions table, "Importance scoring" row: "industry-standard
  MEP defaults, tunable" stands, but tuning means editing rules/tables, and
  the sub-score weights are the only sliders.
- Section 5.6 Settings: the "weight sliders + discipline-pair severity
  table" become: rigidity table editor, tier rule toggles/thresholds, Layer A
  rule toggles, band semantics text.
- Section 7 data model: add the ref enrichment fields, the importance block,
  and the two reserved per-run keys.

## 11. Verified engineering facts (for whoever builds this)

All line-verified this session:

- Fingerprint = SHA1(test_id | sorted (source:element_id) pair | 1 ft
  bucketed midpoint), first 16 hex (identity.py:60-83). Additive ref keys and
  new clash blocks can never reset history. Merge preserves unknown keys via
  deepcopy; persistence is schemaless JSON.
- merge.py:162-165 wholesale-replaces refs on persisting rows (guarded by
  truthiness). _PER_RUN_FIELDS (merge.py:104-105) is copied via raw.get(k) on
  persisting AND new branches; the auto-resolved branch never touches it.
  Score/importance must be stamped AFTER merge over the merged list (or added
  to _PER_RUN_FIELDS); a raw-row score would be silently dropped by the
  new-record literal.
- The top-level clashes.json envelope is rebuilt with a fixed key set by
  both run tools; a config block at file top level would be dropped. Keep
  score config in its own settings file.
- coord.html: brk is read at exactly one site (selectClash ~547-549,
  positional array with hardcoded labels + v*3 bar widths); 70/40 cutoffs at
  four sites; status wiring at ~five sites (a Suppressed toggle touches
  those); rows without importance fall back to the existing "Provisional
  score" note.
- Insulation: use the DuctInsulation/PipeInsulation collector map, not the
  obsolete BIP (see 9.7).
- Storm: classification-primary, regex backstop, slope as third signal (9.5).
- NBIMS-US V3 5.5: conduit-under-2-in floor is real (Annex A Note 1, 1.5.1,
  via 5.5.4.8) but racks of 2+ any size must be modeled, and flex/MC appears
  nowhere in it; it is also a legacy practice document (not carried into
  NBIMS V4), so frame it as industry guidance.

## 12. Known gaps in v1 (accepted, with the plan)

- Hard clashes have a flat geometry term until penetration depth ships; a
  nick and a bury of the same beam tie. The dormant rules (R-GRAZE, C4) ship
  with tests and activate automatically when depth lands.
- Rigidity is a class ladder, not measured slack (remaining fall, remaining
  bend budget, sprinkler spacing margin). The features dict reserves room
  for per-element slack later.
- Vertical runs: a riser has no slope constraint in plan and is often easy
  to shift, but classifies as gravity rigidity 5. Orientation
  (vertical/horizontal, parallel/crossing) is Solibri-validated, nearly free
  from geometry we already have, and is the first post-v1 refinement to
  make. Until then expect the occasional over-ranked riser; the "not
  important" click is the detector for it.
- Prefab/spool lock-in and first-installed advantage have no data source.
- cluster_n sees only detected clashes, not actual congestion.
- Phasing/design options are not modeled; running on an in-progress design
  model will produce transient Criticals (see open question 5).

## 13. Before this ships: the calibration gate

Every number above (tier gates, cluster thresholds, the 20-Critical target)
rests on constructed scenarios, not on dbHMS data. The single highest-value
step, and the recommended acceptance gate for the whole feature:

1. Implement enrichment capture + `lib/clash_score/` + tests (no UI).
2. Re-run detection on one real dbHMS project to enrich refs, score it, and
   dump the band histogram, the top 20, and 30 random Minor/suppressed rows.
3. Nathan blind-labels them: agenda-worthy or not.
4. Tune tier rules against that labeling; only then wire the UI.

That one exercise simultaneously tests capture cost on a real model,
measures the real system-data-quality failure rate (how many pipes have no
classification), populates the bands with a true distribution, and gives the
Tricorder budget its baseline, all before trust is on the line.

## 13.5 Stage-1 retune (2026-07-02, after the first real-project calibration)

The NIUHTC run (5,651 clashes) validated the engine (zero recompute
mismatches) and exposed one domain gap: hosted fixtures. Changes at config
rev 2 (full analysis: `NIUHTC_CALIBRATION_FINDINGS.md`):

- **Mounted-class split:** EQUIPMENT_CATS is now only Mechanical/Electrical
  Equipment (rig 4). Plumbing/Electrical/Lighting Fixtures + Air Terminals
  are MOUNTED_CATS: rigidity 2, class 'fixture' (checked before the gravity
  branch so a Sanitary-classified WC never becomes a rigidity-5 main).
- **New rule N3 "mounting adjacency"** (before M2/M4): hard,
  equipment/fixture mover vs its plausible mounting/bearing surface (arch
  wall/floor/ceiling/roof; structural foundations; fixture-in-framing) ->
  Minor + `mounting_check`. **Demoted, never suppressed:** with no
  penetration depth captured yet, flush mounting and fully-buried are
  indistinguishable, so rows stay visible and flagged; suppression waits
  for positive FamilyInstance.Host capture (future R-HOSTED). Switchgear
  name-gate keeps electrical gear vs walls at M1 (electrical-room layout).
- **N1 before M4:** wall/floor crossings inside congested zones stay on
  the sleeve schedule; congestion stays visible through MEP-vs-MEP members
  and cluster groups. Do NOT fold fixtures into N1 - penetration wording
  on a troffer is wrong (rejected-alternative analysis, deep-dive sims).
- **Fixture clause on M1:** fixture vs a rigidity-3+ curve stays Major
  (RCP-locked troffer vs duct = the duct moves); fixture-vs-device residue
  falls to FB with a `field_fix` flag.
- **_fixed_kind:** everything in the structural link reads structural
  (its Walls/Floors are concrete decks/shear walls; an embed or stray MEP
  category there is still the structural engineer's context), so
  gravity-through-deck is C1, not N1. Deliberately broader than the
  approved Walls/Floors-only patch; review-flagged and pinned by test.

Stage 2 (SHIPPED 2026-07-03, config rev 3): the M3 drain/penetration
split — rule N4 sends drains seated at their slab (mounting_check) and
tight soft near-misses to arch surfaces (imminent penetrations,
penetration_candidate) to Minor, while M3 stays Major for structure,
both-movable pairs, and unknowns — and the C3 tightening (equipment never
satisfies C3; vents with a known diameter under 3 in drop to rigidity 2,
unknown stays 4). Measured on NIUHTC: Critical 39 -> 36 (exactly the
three audit-flagged arguable C3s), M3 210 -> 62, N4 148, Major 1,551,
agenda 294.

## 14. Open questions for Nathan

> **ALL DECIDED 2026-07-01:** (1) grouping is the next milestone after
> scoring lands; (2) Critical/Major/Minor words appear internally and in
> external reports, the numeric score stays internal; (3) one firm-wide
> standard for the bands and rules, no per-project tuning
> (`lib/clash_score/defaults.py` is the standard); (4) the
> escalate-to-consultant flag ships in v1 (the C1 rule stamps
> `escalate_candidate`); (5) "run after modeling sessions, not during" is
> the working norm; (6) sprinkler-vs-conduit ships at the Korman baseline.
> The engine was built the same day: `lib/clash_score/` +
> `clash_detect/enrich.py` + merge wiring + the calibration report
> generator. The original questions are kept below for the record.

1. **Grouping is the real deliverable and has no design yet.** Every research
   thread converged on "the agenda is the top 10-20 GROUPS." Scoring a flat
   grid re-creates the 500-row problem competitors already solved. Group
   roll-up (group band = max open member) is specified, but the grouping
   engine itself (axes, identity across re-runs, interaction with
   fingerprints) needs its own design pass. Recommend: make it the milestone
   right after scoring lands.
2. **External exposure of scores.** BCF/Excel/HTML reports leave the firm.
   Should bands/scores appear in exports the architect or GC sees (with the
   relationship implications of labeling their wall clash "Critical"), or
   stay internal-only by default?
3. **Config governance.** Rules/tables are per-project tunable. Without a
   firm-level default + explicit per-project override model, every project
   drifts and "Critical" stops meaning the same thing across projects. How
   locked-down should project tuning be?
4. **Escalate-to-consultant as a first-class outcome?** A flag (and maybe a
   filter preset) for "dbHMS cannot resolve unilaterally: needs a structural
   opening / arch change request," since those have the longest lead time.
5. **In-progress-model noise.** This runs on the live design model, not a
   frozen snapshot. A half-routed duct will generate transient Criticals. Is
   a norm of "run after modeling sessions, not during" enough, or do we want
   a "coordination-ready" scoping mechanism eventually?
6. **Sprinkler-vs-conduit rung order** in the rigidity table (sources
   disagree; ships editable, Korman baseline).

## 15. Primary sources (selection)

- Korman/Tatum, Stanford CIFE WP054, Table 3 (trade priority with reasons)
- BIMForum BXP 2020 (precedence = difficulty to move)
- NBIMS-US V3 sec 5.5 + Annex A (MEP spatial coordination; conduit floor)
- Ashghal S0301 Clash Detection Matrix Template Guide (A/B/C pair severity)
- Indiana University BIM Guidelines sec 4.2.6 (Level One/Two/Three pairs)
- Solibri SMC Severity Table + rule 245 Clash Detection Matrix docs
- Navisworks Clash Detective official help (rules, tolerance, statuses)
- Revizto help center (clash test settings, issue automation, glossary)
- ACC Model Coordination help (tolerance filters, grouping, limits)
- IPC 704.1 / 905; IMC 306.1 / 307.2.1 / 506.3.7 / 607.4; NEC 110.26 /
  392.18 / bend limits; NFPA 13 obstruction + seismic; IBC 1003.3 / ADA 307
- Hu & Castro-Lacouture 2019; Lin & Huang 2019; Koo et al. JCEM 2026;
  Harode/Thabet 2024; Parn & Edwards 2018; Mehrbod et al. ITcon 2019;
  Wang & Leite 2016
- Google Tricorder (SWE book ch. 20); Johnson et al. ICSE 2013; Dietvorst
  et al. 2015 (algorithm aversion); EPSS/CVSS prioritization literature
