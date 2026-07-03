# Clash Grouping (Layer C) — Design

**Date:** 2026-07-02 (day after the importance engine shipped)
**Status:** DESIGN — awaiting Nathan's sign-off before implementation.
**Method:** 13-agent workflow — 2 codebase audits, 5 research threads
(Navisworks ecosystem, Revizto/BIMcollab/ACC/Solibri, clustering
algorithms, coordination-meeting UX, issue-identity theory), 3 independent
designs forced through 8 churn scenarios, 3-lens judge panel (veteran
coordinator, maintaining engineer, product skeptic).
**Companion docs:** `CLASH_IMPORTANCE_RESEARCH.md` (scoring), section 6 of
`CLASH_REBUILD_SPEC.md` (to be updated to match this doc after sign-off).

---

## 1. The verdict in one paragraph

A group is an **issue** — a ticket a coordinator names, assigns, and
discusses in a meeting — not a query result. The whole industry converged
on the same trick after years of pain: **don't solve the group-matching
problem, design it out.** Once a clash joins a group, nothing but a human
action or the clash's own lifecycle ever moves it; the grouping algorithm
only ever proposes membership for currently-ungrouped clashes. Durable
identity lives on the member clashes (our immortal clash ids), and the
group is a persistent container of them (the BIMcollab "Smart Issues" /
iConstruct pattern). Automatic grouping runs on two axes — **spatial
clusters** for congested racks and an **element star** for "one bad duct =
12 hits" — but geometry never becomes identity. Two of three judges picked
this design outright; the third's winner differed only in how growth is
gated, and all three wrote nearly identical synthesis advice, which is
what this document records.

## 2. What the research established

**Everyone's real noise weapon is grouping.** 500-1000 raw clashes become
20-70 issues; the weekly meeting covers the top 10-40 group-level items,
5-10 minutes each, report distributed ~48h ahead. A good issue = one root
cause = one decision = one accountable owner.

**How the shipping tools keep group identity (and how they fail):**

| Tool | Mechanism | Lesson |
| --- | --- | --- |
| Navisworks native | Manual groups; per-clash GUID-pair matching on re-run; matched clashes keep status AND membership; new clashes land ungrouped | Group survival = member matching quality; grouping itself is never re-derived |
| Group Clashes plugin / iConstruct | "Group only ungrouped clashes" — the celebrated retention feature is literally just *never touching existing groups* | The sticky-roster trick is the industry answer, not a compromise |
| BIMcollab Smart Issues | Issue = named bag of conflict links; identity on each conflict; auto-close when ALL resolve; reopen on return; new clashes never silently join | The container model this design copies |
| Revizto | Match group by persistent id first, then by maximum member-overlap within the same test; reviewed groups frozen to rule changes | Overlap matching is the fallback, never the primary; curation freezes |
| ACC Model Coordination | Identity on element GUIDs alone; delete-and-remodel resurrects closed clashes; no auto-close | The cautionary tale |
| Solibri | Result identity includes rule + category path; decisions lost when categorization shifts | Never let derived values into identity |

**Algorithms:** at our scale (~2000 midpoints) DBSCAN degenerates into
single-linkage connected components, so a union-find over a fixed 6-ft
grid is the right spatial pass (same shape as the shipped `cluster_n`
grid, O(n), pure Python). Raw clusters are inherently unstable under
churn — one removed bridge point splits a cluster — and no clustering
variant fixes that well enough for *named* groups; identity must live on
members. Element-axis grouping must be **star-shaped around one
deterministic anchor** — never transitive union over shared elements,
which is how a slab merges an entire floor into one blob.

**Coordinators' documented hates:** groups reshuffling between runs,
duplicate issues after regrouping, context-free clash views (Mehrbod: a
contractor couldn't relocate an issue two meetings later and it never got
resolved — titles must carry system + level + grid/zone), and unfiltered
lists.

**Codebase facts that shaped the design (audited, cited):**
- Merge produces four member-churn events (new / persisting / reopened /
  auto-resolved) plus one compound one: an element moved past a
  fingerprint bucket surfaces as auto-resolved(old fp) + new(new fp) in
  the same run.
- **The fingerprint spatial bucket is 1.0 ft in code** (`identity.py:34`)
  while the docstring says 5 ft — identity churn is ~5x more frequent than
  documented. See decision D1.
- Clash records are **immortal** — merge never deletes — so member ids can
  never dangle. This single fact de-risks the whole container model.
- Both run pipelines rebuild clashes.json's top level from a fixed literal
  and would **silently drop** an unknown `groups` key; the Browser's
  read-modify-write preserves it. Both literals must gain `'groups'` in
  the same change, guarded by an integrity test.
- There is **no status/comment write-back channel** in coord.html today;
  groups ship with the first one.
- The M4 hysteresis precedent (`prev_rule` read from the previous run's
  stamped importance) is exactly the reconcile-against-prior-state shape
  grouping needs.

## 3. The recommended design

Skeleton: **"Anchored Sticky Issues"** (identity-first, judges 86/81; the
engineer-judge preferred "Docket" at 85 with ASI at 77). Three grafts from
Docket and one from Hotspots — all three judges independently prescribed
the same ones.

### 3.1 Philosophy

Sticky rosters: every existing group keeps every member, every run,
unconditionally. No re-derivation, no overlap matching for anything a
human may have touched. Algorithms only assign **currently-ungrouped**
clashes. Views (regroup by level / system / discipline pair) are ephemeral
UI pivots, never identities. We accept coarser grouping, ungrouped
singletons, and stale-but-honest issues as the price of never dissolving,
duplicating, or silently absorbing a named issue.

### 3.2 The algorithm (merge-time, after `score_all`, both pipelines)

`clash_group.regroup_all(clashes, groups)` — pure dict-in/dict-out in a
new `lib/clash_group/`, IronPython-2.7-safe, zero per-project tuning.
Only unsuppressed clashes are ever assigned. Ordered passes:

**P1 — Sticky roster refresh.** Every existing group keeps every member id
(persisting, reopened, resolved alike). This is the whole trick: the
recluster-then-match problem never arises because grouped clashes are
never reclustered.

**P2 — Successor adoption cascade** (heals the compound rekey event),
strongest evidence first, one-to-one, nearest-midpoint-first,
deterministic order:
- *Tier 1 (silent adopt):* a member auto-resolved THIS run has a new-clash
  successor with the **same test_id + same unordered (source, element_id)
  pair** — that is literally the same two elements re-keyed by the 1-ft
  fingerprint bucket. Inherits the membership; history `member_rekeyed`.
- *Tier 2 (suggestion only):* same test + same category pair + midpoint
  within 10 ft of the vanished member (covers delete-and-recreate).
  Weaker evidence never silently enters a named group.

**P3 — Anchor-key claims.** Each group stores an `anchor` element key
(frozen at formation, see P5). An ungrouped clash touching that element
**auto-joins**; if the group's status is beyond Open, it joins with
`needs_review` + history. This is the one sanctioned silent join — the
"one bad duct" group keeps acquiring that duct's new clashes forever.
(Rejected: Docket's suggestion-only growth. A growing problem must never
read as shrinking because a suggestion queue went ungroomed.)

**P4 — Spatial cluster formation** (two-tier density gate, grafted from
Docket — fixes the winner's 10-19-neighbor coverage hole):
- Core = stamped `importance.features.cluster_n >= 6`.
- Union-find cores over a 6-ft grid; non-core clashes within 6 ft of
  exactly ONE component attach as borders — **borders never bridge two
  components**, so racks cannot chain-merge (this is what makes the
  false-merge scenario structurally impossible, not threshold-lucky).
- A component forms a group only with **>= 10 members**.
- Caps: span > 50 ft or members > 150 cut at the largest coordinate gap
  (deterministic, formation-time only).
- **M4 is untouched**: it keeps firing at 20 for *scoring*; formation and
  banding are separate questions. Layer C only reads the stamped feature.

**P5 — Element-star formation** over the remainder (participation
anchoring, grafted from Docket — replaces mover-anchoring and the separate
obstacle pass):
- Count each element's participation across the remaining pool. A clash's
  anchor is its **higher-participation element** (tie: lexicographically
  smaller unique_id). Star-shaped, never transitive.
- One mechanism now covers both directions: the mis-elevated duct anchors
  its 12 beam hits (12 vs 1), and the rated wall anchors its 14 crossings
  (14 vs 1) — no rigidity/mover dependency, so a future enrichment fix can
  never re-key group identity.
- `>= 3` members form a group; the anchor key is **stored on the group at
  formation and never recomputed** — participation decides only at
  creation, so later shifts cannot re-key a live group.
- Long-element bound: member span > 80 ft along the anchor splits into
  span segments at formation.

**P6 — Spatial adjacency suggestions.** An ungrouped clash within 6 ft of
a curated cluster group's open members becomes a `suggested_id` (badge,
explicit accept). For an untouched machine group it auto-joins (no human
state at risk — Revizto's frozen-reviewed asymmetry).

**P7 — Lifecycle.** A group with zero open unsuppressed members
auto-resolves (history); a Resolved group reopens when any member does.
When a member reopens under a human-decided status (Reviewed/Approved),
the human status is kept but the group is flagged `needs_review` with a
history entry — never silent, never stomped (build clarification,
2026-07-02). Groups are never deleted.

**P8 — Stamp.** Auto-title (only while `title_locked=false`), rollups,
per-clash `group_id` (derived read convenience), churn counters, `drifted`
flag when open members form 2+ spatial components (invites — never
performs — a split).

Residue stays ungrouped: an ungrouped clash is a degenerate single-member
issue on the agenda, never hidden.

### 3.3 Identity rules (the product)

- Group id = uuid4, immortal. Member handle = the **clash id**, never the
  fingerprint (ids survive; fingerprints are 1-ft volatile by design).
- What survives arbitrary churn: id, title (+lock), status, assignee,
  priority, comments, history, viewpoint, lineage — all live on the group
  record, never derived from membership.
- **Splits: never automatic.** `drifted` invites a manual split; the
  fragment with the most open members keeps id/name/status/assignee/
  comments; the other gets a new id + `lineage.split_from`, inherits the
  assignee only.
- **Merges: never automatic.** Survivor = the more-curated group (locked
  title / comments / assignment; tie: older). Absorbed group becomes a
  `MergedInto` tombstone so BCF Guids and history never dangle.
- **Manual groups** (axis=manual): maximally sticky — membership is
  exclusive, formation passes are blind to their members, no anchor key,
  nothing ever auto-joins. Only successor adoption and lifecycle touch
  them.
- Anchor keys use `(source, unique_id)` — never `fed_key` (embeds link
  placement origin; a link re-placement would re-key every anchor) and
  never enrichment values (sys_name/level are display facets; Solibri's
  decision-loss bug is what happens when derived values enter identity).
  Fallback when unique_id is None (old data): `source + element_id +
  link_doc_title`.

### 3.4 Data schema and flow

Groups live as a top-level `"groups"` key **inside clashes.json** — one
atomic write covers members + rosters + statuses (a second file has no
atomicity with the first; staleness between them is a bug class we can
simply not have). Preconditions handled in the same change: both run
pipelines' `new_data` literals gain `'groups'`, `read_clashes`' default
gains `"groups": []`, and an integrity test asserts the key round-trips
both pipelines (the silent-drop is the likeliest real-world data-loss
bug — all three judges flagged it).

```json
{"id": "uuid4", "created_at": "iso", "created_by": "system|user",
 "axis": "element|cluster|manual",
 "anchor": {"source": "...", "unique_id": "...", "element_id": 0,
             "name": "...", "category": "...", "link_doc_title": null},
 "title": "Duct 'DD-03' vs 12x Structural Framing — L3, B/3–F/3",
 "title_locked": false,
 "status": "Open|Reviewed|Approved|Resolved|MergedInto",
 "assignee": "Mechanical", "priority": null,
 "member_ids": ["..."], "suggested_ids": [], "needs_review": false,
 "lineage": {"split_from": null, "merged_from": [], "merged_into": null},
 "history": [], "comments": [], "rep_clash_id": "...",
 "rollup": {"band": "Critical", "score": 94, "reason": "...",
             "level": "L3", "n_open": 5, "n_resolved": 9, "n_suppressed": 0,
             "n_new_run": 2, "n_resolved_run": 9, "n_reopened_run": 0,
             "drifted": false, "congested": false, "diameter_ft": 0}}
```

Flow: `read_clashes → merge_runs → score_all → regroup_all → write_clashes`
in the existing best-effort try of both pipelines. Rollup: band = max open
unsuppressed member band, score = max open member score, reason = the
controlling member's reason (stamped host-side so coord.html gains no
fourth copy of the 70/40 thresholds). Suppressed members keep their roster
seat but never count toward band/open. Group assignee = modal member trade
at creation; a human assignment is never auto-reassigned.

**BCF:** one topic per group — Guid = group id, Title = group title,
AssignedTo = assignee, Description = member roster (seqs + one-line
reasons), snapshot = `rep_clash_id`'s PNG.

### 3.5 UI (Phase 3)

- `_send_clashes` posts a parallel `groups:` message; rows join on
  `group_id`.
- **First write-back channel**: `groupop:{op, group_id, ...}` (rename /
  assign / status / accept-suggestion / split / merge / new-from-selection
  / ungroup), host does read-modify-write + atomic write, re-sends, and
  acks with `groupdone:` so a stale host surfaces "host does not support
  groups yet" instead of silence.
- Grid: real collapsible parent rows (collapse state keyed by group id;
  selection becomes a Set of clash ids; firm-standard click/shift/ctrl
  multi-select). Parent row: title, band chip, `14/22` progress, churn
  badge ("+2 new"), needs_review dot, assignee. The cosmetic GROUP_KEY
  pivots remain as the ephemeral regroup-on-demand lens.
- Inspector: group variant of selectClash — editable title with lock
  indicator, controlling reason, suggested-additions accept/dismiss,
  member mini-list drilling into the clash inspector, comments, history.
  "Show group in 3D" generalizes the two-fed-key isolate to the member
  union with union-AABB framing.
- Home: a live **"Meeting agenda"** card — top 10-20 open issues (groups +
  ungrouped singletons) sorted band/score with a stable created_at
  tie-break so ordering never churns between meetings.
- Titles carry what a meeting needs to relocate the issue (the #1
  documented failure): facet + systems + level + grid span, e.g.
  "Congested rack — L2 mech room, C/5 (35 clashes, 4 trades)". Facet
  vocabulary from Hotspots: *element-run / rack / penetration-set*.
  (Build note 2026-07-02: the grid span is blocked upstream — clash
  records carry no grid data; needs a detection-time enrichment stamping
  the nearest grid intersection. Titles ship with level + count until
  then.)

### 3.6 Constants (firm standard, `lib/clash_group/defaults.py`)

| Constant | Value | Meaning |
| --- | --- | --- |
| CORE_CLUSTER_N | 6 | stamped cluster_n at/above this = spatial core |
| CLUSTER_EPS_FT | 6.0 | union / border-attach radius |
| CLUSTER_MIN_MEMBERS | 10 | component floor to form a group |
| ANCHOR_MIN | 3 | element-star formation floor |
| ADOPT_RADIUS_FT | 10.0 | tier-2 successor suggestion radius |
| ANCHOR_SPAN_SEGMENT_FT | 80 | long-element star segmenting |
| MAX_SPAN_FT / MAX_MEMBERS | 50 / 150 | cluster caps, deterministic cut |

All frozen until the Phase-5 calibration run on a real project, then
adjusted once, firm-wide.

## 4. The eight scenarios under the final design

| # | Scenario | Outcome |
| --- | --- | --- |
| S1 | Mis-elevated duct vs 12 beams | No spatial cores (neighbors ~2-3). Element star: duct participation 12 vs 1 → ONE group, anchor = duct. "Duct 'DD-03' vs 12x Structural Framing — L3, B/3–F/3", Critical. |
| S2 | 35-clash rack, 9 elements, 4 trades | cluster_n high → cores → one component ≥10 → ONE rack group; borders attach; M4 still scores members. |
| S3 | 14 penetrations of one rated wall | Wall participation 14 vs 1 → the wall anchors ONE penetration-set group (same mechanism as S1, no special pass). Minor band, one row, feeds the future sleeve-schedule pass. |
| S4 | Named+assigned duct group; 9 resolve, 3 persist, 2 new grazes | Roster keeps all 12; rekeyed members silently adopted (tier 1); the 2 new grazes touch the stored anchor duct → auto-join with needs_review; name/assignee/comments untouched; churn line "9 resolved, 2 new". Id never changes. |
| S5 | Rack half-resolves, would split spatially | No split. Progress "18 of 35 resolved"; open members form 2 clumps → `drifted` flag invites a manual split; identity loss only ever a human act. |
| S6 | Two distinct problems 4 ft apart | No cores (density gate), different anchors below ANCHOR_MIN → two singleton issues. Structurally cannot merge: spatial formation is density-gated and borders never bridge. |
| S7 | Manual group of 3 pulled out | Manual membership is exclusive and invisible to formation; survives verbatim; 1 auto-resolving leaves "2 open / 1 resolved", resolves only when all decided. |
| S8 | 500 raw clashes, first run | Layer A suppresses ~30-40%; stars + clusters collapse the rest to ~25-60 issues; agenda card shows top 20; singletons below the fold, visible. |

## 5. What was rejected, and why

- **Recompute-and-match for untouched groups** (Hotspots): overlap-tau
  matching is the graveyard everyone else climbed out of; ids/splits churn
  exactly during the first weeks when trust is being formed.
- **Absorbing M4 into grouping** (Hotspots): rewires the scorer that
  shipped this week — regression surface on freshly calibrated ground,
  duplicated across two pipelines. Formation reads stamped features only.
- **Suggestion-only growth for curated groups** (Docket): an ungroomed
  suggestion queue makes a growing problem read as shrinking; the docket
  quietly lies. Deterministic anchor-key joins are automatic (badged
  needs_review when the group is beyond Open); only geometric evidence is
  suggestion-only.
- **Mover-based anchoring** (original winner): couples group identity to
  the importance engine's rigidity pick; a scoring improvement could
  silently spawn duplicate sibling groups. Participation anchoring severs
  the coupling.
- **A separate groups.json**: no cross-file atomicity exists; staleness
  between the two files is a whole bug class avoided by one key in one
  atomically-written file.

## 6. Judge panel results

| Design | Coordinator | Engineer | Product |
| --- | --- | --- | --- |
| Anchored Sticky Issues (winner) | **86** | 77 | **81** |
| Docket (curated) | 81 | **85** | 73 |
| Hotspots (spatial-first) | 76 | 64 | 61 |

All three synthesis notes converged on: ASI skeleton + Docket's
participation anchoring (frozen at formation) + Docket's tiered successor
cascade + the two-tier spatial gate + Hotspots' titles/facets/card-facts —
which is exactly section 3.

## 7. Build plan

1. **Phase 1 — pure lib + tests.** `lib/clash_group/` (defaults, regroup_all,
   rollup, auto_title, union-find). `tests/test_clash_group.py` encoding
   S1-S8 as fixtures plus adoption/needs_review/lifecycle/title-lock cases.
   Fix the `identity.py` docstring (see D1).
2. **Phase 2 — persistence + wiring.** `"groups": []` in the read default;
   `regroup_all` after `score_all` in both pipelines; `'groups'` added to
   BOTH writer literals in the same commit; per-clash group_id stamp; the
   round-trip integrity test.
3. **Phase 3 — host + web UI.** `groups:` message, `groupop:`/`groupdone:`
   channel, collapsible grid, group inspector, agenda card, SAMPLE_GROUPS.
4. **Phase 4 — periphery.** Browser read-only group column; BCF
   group-topics; reports group filter; pre-meeting digest export.
5. **Phase 5 — calibration** on a real project; adjust constants once,
   firm-wide, freeze.

## 8. Decisions for Nathan

- **D1 — fingerprint bucket doc bug (recommend: keep 1.0 ft, fix the
  docstring).** The code buckets clash identity at 1 ft; the docs claim
  5 ft. Changing the constant would re-fingerprint every existing clash on
  the next run (all statuses/comments would detach), so the constant
  stays; the successor cascade is designed for the 1-ft churn rate. This
  is a documentation fix unless you feel strongly otherwise.
- **D2 — concurrent edits (recommend: accept for now).** clashes.json is
  last-writer-wins with no locking (pre-existing). Fine while one person
  drives the tool per project; needs an ops-log or field-merge before a
  true multi-user live-meeting workflow. Decide at Phase 4, not now.
- **D3 — BCF granularity (recommend: one topic per group).** Member-level
  topics can come later via RelatedTopics if a consultant asks.
- **D4 — approve the build plan** (Phases 1-5) and this design as spec
  section 6 Layer C.
