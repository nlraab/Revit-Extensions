# 3D Viewer tab: design + build plan

> The plan for turning the Clash Detection app's 3D Viewer tab from a
> bare canvas into the firm's clash-review cockpit: Navisworks-class
> review power with Revizto-class speed, on the xeokit stack we
> already ship. Grounded in a deep research pass (2026-07-03) over
> Navisworks, Revizto, ACC Model Coordination, Solibri, BIMcollab
> Zoom, Trimble Connect, Dalux, 3D-navigation HCI research, real
> coordination-meeting workflow, the xeokit SDK, and our own code.
> The plan then survived an adversarial review against the codebase
> and the research; the fixes are folded in.
>
> Status: **ALL phases (A, B1, B2, C, D) are BUILT** (2026-07-03/04, all
> decisions in section 11 resolved with the recommendations; both tools
> stay separate per Nathan). Browser-verified against sample data + the
> test model; awaiting Nathan's in-Revit test. Phase D shipped: full
> saved viewpoints (camera + section box + view mode + see-through,
> restored on select via a session-vs-saved split so a restored view
> never leaks into the next clash), a "Save this view" button, clash
> pins (AnnotationsPlugin, numbered + band-colored + occludable, click
> to open), a presentation text-size toggle, view-undo (separate from
> data Ctrl+Z), and a level navigator (isolate a floor + top-down
> ortho). Deferred: the visual section-cut floor-plan minimap render
> (needs a metaModel export or a canvas-disrupting snapshot; level
> navigator ships instead per research) and numbered redline tag
> markups (design's "maybe").
> Phase C added: visibility panel finished (Categories/Worksets/Levels
> mode toggle), face-click section-plane Cut tool with chips (flip/
> remove/clear), Measure with vertex/edge snapping and ft-in straight/
> horizontal/vertical readout, Ortho + Plan toggles, and click-to-inspect
> Items rows. All tools coordinate (mutually exclusive, teardown on
> clash change / model reload / tab-leave) and are transparent to the
> image-capture pipeline (enterCap suspends section planes + measurements
> instead of tearing the overlay down). Adversarial review found + fixed:
> the capture-batch/lastFocusKey bug that wiped cuts on a same-clash tab
> round-trip, measurement lines bleeding into saved thumbnails, a
> foot-boundary rounding bug (11.98in reading 12"), and a hide-mode
> visibility-edit that looked broken.
> Sections 1 to 6 are the product; section 7 is the engineering.
> Post-build additions from the adversarial review: a `clashupd:`/
> `groupupd:` per-edit delta channel (instead of full re-pushes per
> keystroke), group rollup refresh on member status edits, busy-nack
> for groupop too, a bak-swap atomic write in persistence (crash can
> no longer vaporize clashes.json), and un-assign support in
> bulk_edit.apply_trade.

---

## 1. What this tab is (and is not)

**The place you look at and decide clashes.** The Clash Browser is
the ledger: filter, sort, group, bulk edit, audit. The 3D tab is the
meeting cockpit: flip through the same list, see each clash in
context, and record the decision (status, owner, due date, comment)
without leaving the view. The research is unambiguous that this loop
IS the product in every serious tool: click a result, the camera
flies, the pair lights up, the context ghosts, you decide, next.

| Surface | Job | Unique powers |
| --- | --- | --- |
| Clash Browser | triage + audit ledger | filter presets, group ops, bulk edit, history, search |
| 3D Viewer tab | look + decide | live context view, sections, visibility, measure, element info |
| 3D Viewer button | full-model walkthrough + share | render quality, sun, minimap, share-to-browser |

The two tabs share one selection, one filter set, and one inspector
card (section 8), so moving between them never loses your place. The
standalone 3D Viewer stays the walkthrough tool; this tab does not
grow fly-through, sun studies, or render tiers (its long-term fate is
decision 5).

**Headline differentiator:** the live view uses the exact same visual
recipe as the saved clash photos (red A, blue B, ghosted
surroundings). The picture on the card and the model on screen always
match. No competitor does this.

**Deliberately not building:** see section 10.

---

## 2. Layout

The layout every coordinator already knows from ACC and Navisworks,
and the one you sketched: list left, decision card right, view tools
bottom center, orientation top right. All dbHMS design tokens.

```
+------------------------------------------------------------------+
| tab header strip: [Model v]  status text                          |
+--------+-----------------------------------------------+---------+
|        |        context chip (what am I seeing)        |         |
| REVIEW |                                               | CLASH   |
| LIST   |                VIEWPORT                       | CARD    |
| (left, |                                    [NavCube]  | (right, |
| collap-|                                    [Home/Fit] | 420px,  |
| sible) |                                               | resiz-  |
|        |      [bottom-center floating toolbar]         | able)   |
|        |      hint bar (tiny, dismissible)             |         |
+--------+-----------------------------------------------+---------+
```

- **Review list** (left, ~264px, collapsible to a thin tab): the same
  rows the Clash Browser currently shows under its active filters, in
  compact form (band chip, number, short title, status dot, level).
  Issues are parents with chevrons, identical row language to the
  Browser grid. The header names the active preset ("Meeting set,
  41") and carries a **search box**, **status and trade quick-chips**
  (live, synced with the Browser rail), and a **sort toggle**:
  Importance / Level / **Proximity** (spatial walk order, so the
  camera tours one area at a time instead of teleporting across the
  building; Navisworks calls this Sort By Proximity and meetings run
  on it). Complex filter editing stays in the Browser.
- **Viewport**: the xeokit canvas. Overlays:
  - **Context chip**, top center: always states what you are seeing.
    "Clash 214, A red / B blue, others ghosted" or "Issue G12, 12
    clashes" or "Free view", with a [Show all] link. This is the
    answer to "is this a group or a clash" at a glance.
  - **NavCube** + Home + Fit, top right.
  - **Bottom-center floating toolbar** (white pill card): Fit, view
    mode segmented control (**Ghost others / Hide others / Show
    all**), Section box, Section plane, Measure, Visibility, Pins
    (later), "?". Buttons appear with their phase; we never ship a
    dead placeholder. Overflows into a "..." menu on narrow windows.
  - **Hint bar**, very bottom, one line: "Drag: orbit. Right-drag:
    pan. Scroll: zoom. Click: select. Double-click: focus." Dismiss X
    (remembered); afterwards it lives inside "?".
- **Clash card** (right, 420px, draggable splitter, min 360): the
  same inspector the Clash Browser shows, plus a review header:
  "Clash 3 of 41" with large previous/next buttons. Status, owner,
  due date, and comment are editable right on the card (section 4).
- The current toolbar buttons (Load last export / Update from Revit /
  Recapture images) collapse into a small **Model menu** in the tab
  header strip; the status line stays.
- **Clean screen**: one toolbar button (and F9) hides both side
  panels for screen sharing. Ships with the review loop, not later:
  meetings run on laptops over Teams and need the pixels.
- Sizing rules (firm resize standard): app window min width ~1100;
  the review list auto-collapses below ~1250px of window width; no
  clipped controls at any size.

---

## 3. Controls

### The mouse (recommendation, and decision 0)

Research verdict on the left-click complaint: every mouse-first web
viewer (ACC/Forge, Sketchfab, xeokit) ships left-drag = orbit AND
left-click = select, separated by a small movement threshold (ours
already has one: a click only registers if the mouse moved 3px or
less). What is missing today is any signal telling you which will
happen. The recommended fix keeps the web-standard mapping, adds
Revit's middle-button grammar so Revit muscle memory works unchanged,
and adds strong visual feedback so the behavior is obvious. If it
still feels wrong in use, a "Revit-strict" preference (left-click
select only, orbit on Shift+middle) is a cheap toggle later. Your
call: decision 0.

| Input | Action |
| --- | --- |
| Left click | select element (empty space: deselect) |
| Left drag | orbit, pivoting at the point under the cursor; up axis locked, no roll, model can never flip upside down |
| Double-click element | fly to and frame it |
| Double-click empty | zoom to fit |
| Scroll wheel | zoom toward the cursor |
| Middle drag | pan (Revit) |
| Shift + middle drag | orbit (Revit) |
| Right drag | pan |
| Right click (no drag) | context menu: Focus, Ghost others, Hide, Section box here, Properties, Show all |

Feedback that makes it obvious:

- **Hover glow, always on**: whatever the cursor is over lights up
  faintly with a pointer cursor. When things glow under the mouse,
  "click selects, drag orbits" explains itself. This is the Forge
  viewer default and the strongest single cue.
- **Pivot marker**: a small dbHMS-blue circle shows the orbit point
  while orbiting.
- **Safe orbit**: orbiting from empty space pivots sensibly (never
  flings the model off screen, the classic novice disaster named in
  Autodesk's own navigation research).
- **First-run hint**: a one-time animated "drag to orbit, click to
  select" overlay (Sketchfab pattern), dismissed forever.
- **"?" cheat sheet**: a mouse-diagram dialog, dbHMS styled.

### The keyboard

Guarded whenever a text input has focus. One language across both
tabs.

| Key | Action |
| --- | --- |
| j / k or Down / Up | next / previous item (camera follows; a new press cancels the in-flight camera move) |
| [ / ] | previous / next clash within the selected issue |
| Enter | re-frame the current clash |
| Esc | one step back, in order: close active tool (measure/section), clear element selection, member up to issue, issue up to list, review view back to your own view |
| F | frame selection |
| Home | fit whole model |
| C | section box around the current clash on/off |
| T | swap which pair element is see-through (for pipe-inside-duct cases) |
| X | ghost others (selection) |
| H | hide selection (toast with undo) |
| V (hold) | view in context: zoom out to the whole building, release to fly back; temporarily shows everything even in Hide mode |
| 1 / 2 | status Open / Reviewed, then advance to the next item |
| 3 | Approved: opens a small reason picker (reason dropdown + optional note, Enter commits, current view image attached), then advances |
| 4 | Resolved, then advance; toast reads "Resolved, will be confirmed on the next run" (the merge engine reopens it if it still clashes, same safety net as Navisworks) |
| Shift + 1..4 | same, but stay on this clash |
| Ctrl+Z | undo the last status/edit (single step, data edits only) |
| F9 | clean screen (hide side panels) |

Advance-on-decide is the default because that is the meeting rhythm
(coordinators literally write AutoHotkey scripts to get "1 = Reviewed
and next" in Navisworks); Shift holds position. The digit shortcuts
are printed on the card's status buttons so they teach themselves.
Approved deliberately costs one extra step: an unexplained acceptance
is a compliance problem on record, so the reason is mandatory (ACC
pattern).

Camera feel: 1:1 like Revit, near-zero glide. Perspective by default,
orthographic and top-down plan as toolbar afterthoughts. Walk mode is
deferred (decision 6): orbit plus fly-to covers clash review per the
HCI studies, and the "stand in the corridor, look up into the plenum"
move is covered by the section box; the standalone 3D Viewer remains
the walkthrough tool.

---

## 4. The review loop (the core feature)

### Selecting a clash

From a list row, j/k, the card arrows, or "Show in 3D" in the
Browser:

1. The camera flies (about 400ms) to the clash neighborhood, framed
   the same way the clash photos are framed.
2. A renders solid red, B solid blue (the firm standard; safer for
   color-blind reviewers than Navisworks red/green). When one element
   swallows the other, the big one automatically goes see-through so
   both read (already built for the photos); T swaps which one.
3. The rest of the model ghosts in two tiers: the neighborhood around
   the clash at a readable ghost, everything farther at a whisper
   ghost. **Nothing is hidden by default**, so you never lose the
   building (the top disorientation complaint with every tool). A
   "Context size" slider adjusts the readable-ghost radius.
4. The context chip, card, and list row all update together.

The **view mode** segmented control (Ghost others / Hide others /
Show all) is persistent: Ghost is the default recipe above; Hide
others blanks everything outside the neighborhood for the messiest
plenums; Show all turns the recipe off. Esc or Show all always
returns to your own view, exactly as you left it (section 7's state
machine guarantees flipping through clashes can never wreck your
visibility setup).

### Issues vs clashes, always obvious

- Issue selected: every member pair lights red/blue, the camera
  frames the whole issue, the chip says "Issue G12, 12 clashes", and
  the card is the issue card (composition, progress, members,
  suggestions).
- Clash inside an issue: the card carries the crumb ("Issue G12,
  clash 3 of 12"), [ and ] step through the roster, Esc goes up.
- List rows keep the Browser's parent/child structure language
  (chevron, weight, pill), never color tint.

### Deciding from the card

The meeting decides four things per item: status, owner, due date,
and the comment that records the action. All four are editable on the
card, in both tabs (the Browser card becomes editable as part of this
work; today it is read-only). Edits persist immediately with a toast
and Ctrl+Z, append to the clash history, and never move your
selection or scroll. When you decide (status change or comment), the
current camera is saved with the clash, so reopening it later lands
in the view you decided in (camera first; full saved-view state with
sections arrives in phase D).

### Section box around the clash

C or the toolbar button drops a box around the clash with a padding
slider (Revizto's signature move, and Dalux's C key). Recommended
default: off, because the ghost recipe already declutters and matches
our photos; the box is one keypress away and there is an "Auto box on
focus" toggle for Revizto-style taste. Decision 1.

You said "scope boxes": we read that as the 3D section box around
the clash. If you also meant importing the model's named Revit Scope
Boxes as ready-made cutting presets, say so, that is a different and
doable feature.

### Show in Revit (honest version)

A button on the card selects and zooms the clash elements in Revit
itself. Because the tool is a full-screen window that Revit sits
behind, the flow is: click, the tool minimizes, Revit is in front
with the elements selected and framed; bring the tool back up when
done. Host elements select directly; elements inside linked models
zoom-to and select the link (Revit cannot select inside a link).
Making the whole tool live side-by-side with Revit (a modeless
window) is a real architectural change we can price separately if
this gesture proves popular.

---

## 5. Model tools

- **Visibility panel** (toolbar flyout): Models, then categories,
  then worksets, with search and All/None, in the same blue
  model-box pattern as the standalone 3D Viewer so the two read
  identically. A levels row filters by level. What you set here is
  your **baseline**: the state every Esc / Show all returns to.
  Models + categories ship with the review loop (B2); worksets +
  levels complete in C.
- **Sections**: the clash auto-box (above), "Section box around
  selection" on the context menu, and single planes created by
  clicking a face (the plane aligns to the face you clicked), dragged
  by a fat arrow that glows on hover; clicking the arrow flips the
  cut direction (made visually obvious; ACC users file support
  tickets over this exact confusion). Active planes show as chips
  above the toolbar with eye/remove; Clear all. No cut-surface
  hatching (a documented performance killer in ACC).
- **Measure**: distance with snapping to corners/edges, showing the
  direct distance plus the X/Y/Z offsets (the offsets are the "how
  far do I drop this duct" number), in feet and fractional inches.
  Esc exits; measurements clear when you move to another clash.
- **Element info, click anything**: clicking any element (clashing
  or not) shows: Category, Family, Name, System, Size, Level,
  Workset, Model, Created by, Id, with Ghost others / Hide / Show in
  Revit actions. Clicking a red/blue pair element mid-review shows
  its info inline on the clash card without leaving the clash; Esc
  returns. System and Size ride the export from phase A. Full
  parameter browsing is decision 3.
- **Later (phase D)**: clash pins in the model (numbered, colored by
  importance band, only for the current filtered list, hidden behind
  geometry properly, one toolbar toggle because ACC's unhideable pins
  generated support tickets), a floor-plan minimap with level picker,
  and view-state undo (the "unbreak my view" button).

---

## 6. Learning it, trusting it

- Hint bar + first-run hint + "?" cheat sheet (section 3).
- Everything is reversible and says so: status toasts with undo, Show
  all one key away, Esc always retreats one step, H shows a toast
  with undo so nothing silently vanishes.
- Degraded gracefully: if a clash's elements are missing from the
  loaded export, the card shows the saved photo and the 3D actions
  gray out with the existing "not in this export" note. Never an
  error dialog.
- Empty states teach: no model loaded shows a friendly "Load last
  export / Update from Revit" card in the viewport, not a gray void.

---

## 7. How it is built (the engineering section)

### Scene state machine (the load-bearing invariant)

Three layers, generalizing what the capture pipeline already does:

- **Baseline**: the user's own state (visibility panel choices,
  manual hides, sections, camera). Owned by the user.
- **Review overlay**: what clash focus paints (pair colors, ghost
  tiers, hidden set, auto box). Recomputed per selection, fully
  removed on Esc / Show all / deselect, never persisted into
  baseline. Applied via batch ops (setObjectsVisible / Xrayed /
  Colorized) on precomputed id sets. Honest cost: the neighborhood
  set is one cached-AABB scan over the object list per selection
  (fine; already how capShow works); the Context size slider
  re-scans, so it is throttled on drag.
- **Capture mode**: the existing enterCap/exitCap batch.

Named arbitration rules (from the adversarial review; these are
invariants, not details):

1. A capture batch may not start or continue while the 3D tab is the
   active view, including forced batches (post-run, Update from
   Revit, Recapture). It pauses and resumes when the user leaves the
   tab or goes idle. Post-run images may finish after the meeting;
   that is fine.
2. The inspector's live one-shot capture is disabled while the 3D
   tab is active (the live view IS the context view there).
3. enterCap is illegal while a review overlay is painted: flatten to
   baseline first, so a batch can never "restore" overlay state as
   if the user owned it.
4. Data re-pushes are diff-applied: they never rebuild the list DOM
   wholesale, never reset selection/scroll, never touch the camera.

### Bridge additions (same string envelope as today)

- `clashop:{op: "status"|"assign"|"deadline"|"comment", clash_id,
  ...}` -> host applies via clash_core.bulk_edit (apply_status /
  apply_trade, history included), models.make_comment, one atomic
  write -> `clashopdone:{ok, clash_id}` -> delta re-push.
  Hardening required (found in review):
  - **Busy nack**: while `_op_busy` (detection/export in flight) the
    host currently drops messages silently; clashop must get
    `clashopdone:{ok:false, busy:true}` and the page queues and
    retries, so no decision made during the post-run export tail is
    lost.
  - **Optimistic UI**: the page mutates its local row immediately and
    reconciles by id on the ack; the current full loadClashes reset
    (which clears selection) is not acceptable on this path.
  - **Coalescing**: rapid-fire ops debounce into sequential writes;
    each write is a full read-modify-write of clashes.json, which on
    a network share must not run per keystroke.
  - Deadline: add the `deadline` field to the clash record (already
    planned in the rebuild spec's data model) and an op for it.
- `showinrevit:{keys:[fed_key...]}` -> resolve via UniqueId, select +
  zoom in the active Revit view, then minimize the tool window. No
  ExternalEvent needed while the window stays modal (bridge handlers
  already run in Revit API context; `_run_tests` proves it).
- `viewpoint:{clash_id, camera}` (B2: camera-only floats piggybacked
  on decide ops; D: full BCF-shaped viewpoint JSON with
  defaultInvisible=true so exception lists stay small).
- Suppress the WebView2 default context menu app-wide (host
  `AreDefaultContextMenusEnabled=false` or page-level preventDefault
  outside text inputs): today a right-click on the grid offers
  Chromium's "Reload", which restarts the app mid-meeting.

### xeokit configuration

- **Bundle bump first**: vendored 2.6.0 -> latest 2.6.x (drop-in
  file). The real reasons: about 110 patches of section / measure /
  BCF fixes, and the DTX + logarithmic-depth + edges fix (2.6.109),
  which matters because our ghost look uses x-ray edges and we load
  with dtxEnabled. Verify on the big test model.
- Corrections from the review: middle-drag pan already works on
  2.6.0 (hard-coded); Shift+middle orbit is NOT expressible via
  keyMap in any 2.6.x and is a small custom input handler (budgeted
  in phase A); the right-click threshold and canvas context-menu
  suppression are already built into 2.6.0's rightClick event.
- Viewer: logarithmicDepthBufferEnabled, FastNavPlugin (degrades
  detail while moving), dtxEnabled stays on.
- CameraControl: followPointer (default), smartPivot on, styled
  pivotElement, doublePickFlyTo (default), panRightClick on, hover
  events -> highlighted state (throttled ~30Hz, suspended during
  camera motion).
- NavCubePlugin on its own corner canvas, fitVisible on (fits what
  is visible, not the hidden building).
- Section caps: only after the bump, needs readableGeometryEnabled
  at load (real memory cost on big models, DTX compatibility
  unverified). Default off; likely skip.

### Export / data changes

- Phase A: add `sys_name`, `sys_abbr`, `dims_in`, `ins_in` to glTF
  extras for MEP categories (mirrors enrich.py), and use the
  enrich-style level fallback for the `level` extra (LevelId alone
  misses many MEP elements). Measure and report the size delta.
- `deadline` on clash records (clashop above).
- Grid intersection stays a detection-time enrichment (rebuild spec
  open item); when it lands, the queue and card pick it up for free.
- Phase D: BCF-shaped viewpoints on clash records.

### Shared inspector extraction

selectClash / selectGroup are ~300 lines of template with side
effects (direct #cl-detail writes, row highlight sync, SEL globals,
crumb wiring, capture kicks). Extraction into {html, bind(container)}
renderers is feasible, not a rewrite, but the keyboard layer moves
with it: the document keydown handler is currently gated to the
Clashes tab, so Esc / j/k / [ ] must be lifted into the shared
selection store or the two tabs drift.

### Performance guardrails

- Review list renders a capped window of rows (the Browser's
  innerHTML-everything approach is fine to ~2000; there is no
  existing virtualization to reuse, so cap + "show more" first,
  windowing only if needed).
- Pins capped to the filtered list (phase D).
- No per-frame overlay work; measurement/section state cleared on
  model reload; GLB size caps unchanged.
- Ship checks run on three real project exports of different sizes,
  at least one with links, not just the test fixture.

---

## 8. One app: integration rules with the Clash Browser

1. **One selection.** Selecting a clash or issue anywhere selects it
   everywhere. Show in 3D is just "switch tab"; switching back lands
   on the same row.
2. **One list.** The 3D review list is the Browser's filtered view,
   same rows, same order (plus the 3D-side sort toggle for spatial
   order). Quick filters (search, status, trade) are editable on
   both sides and stay in sync; preset and advanced filter editing
   lives in the Browser.
3. **One card.** The same inspector component renders in both tabs;
   the 3D tab adds the review header (position + previous/next).
   Editing works in both.
4. **One keyboard.** j/k, [ ], Esc, digits behave identically in
   both tabs.
5. **One visual language.** Band chips, status dots, row structure,
   red/blue pair colors, ghost style: identical in grid, card,
   viewport, and photos.

---

## 9. Build order

Every phase ships alone and is testable in a plain browser (sample
data + test model) before Revit.

**A. Controls + selection foundation.** Bundle bump; camera config
(pivot marker, safe orbit, Revit middle-button grammar including the
custom Shift+middle handler, right-click menu, WebView2 menu
suppression); hover glow; NavCube + Home/Fit; hint bar + first-run +
cheat sheet; element info card (with System/Size via the export
enrichment) + ghost/hide/show-all + context menu; Esc/F/Home; empty
state card. Ship check: a Revit user orbits, pans, zooms, selects,
and reads element info with zero instruction, and hover stays smooth
on the big model.

**B1. Flip and see (first real clash value).** Review list bound to
Browser filters (+ search, quick chips, sort incl. Proximity); shared
card extraction with review header and previous/next; the live ghost
focus recipe with the two-tier ghost and Context size; view mode
trio; issue vs clash presentation; context chip; j/k [ ] Enter Esc;
clean screen (F9); baseline/overlay state machine skeleton. Ship
check: flip through a real project's meeting set end to end, groups
and members clearly distinguished, without touching the Browser tab.

**B2. Decide and edit (the meeting cockpit).** clashop channel with
busy-queue + optimistic UI + coalescing; editable status / owner /
due date / comment on BOTH tabs' cards; digit keys with
advance-on-decide, Approved reason picker, undo toast; camera-only
viewpoint save on decide; auto section box (C) + padding + "auto box
on focus" toggle; T swap; V view-in-context; Show in Revit
(minimize-and-select); visibility panel (models + categories). Ship
check: run a full mock meeting, decide twenty clashes with the
keyboard alone, close the tool, reopen, everything stuck.

**C. Model tools complete.** Visibility panel finishes (worksets +
levels); face-aligned section planes + chips; measure with axis
offsets in ft-in; ortho/plan toggle; section box around any
selection; richer element card polish. Ship check: answer "what is
this, how big, how far, what if I cut here" entirely in-tab.

**D. Persistence + spatial polish.** Full BCF-shaped saved viewpoints
(restore sections + visibility, not just camera); clash pins overlay
with toggle; presentation type-size toggle; view-state undo/redo;
floor-plan minimap with level picker (evaluate xeokit storeys vs
porting the 3D Viewer minimap); numbered tag markups (maybe). Ship
check: close a clash Tuesday, reopen Thursday, land in the exact view
you decided in.

Relative sizes: A medium, B1 medium, B2 large, C medium, D medium.

---

## 10. Explicitly not building (and why)

- **Walk/fly mode in v1**: clash review is orbit-around-the-problem
  plus fly-to; two HCI studies and the tool survey agree. The MEP
  "stand in the corridor and look up" case is covered by the section
  box and plan view; the standalone 3D Viewer walks. Revisit only if
  meetings actually miss it (decision 6).
- **Nine nav modes / SteeringWheels / mode toolbar**: the thing that
  makes Navisworks feel dated. Two schemes on different buttons need
  no mode switch at all.
- **Full parameter dumps on the card**: the card is a decision
  surface; every tool keeps deep properties one click away (decision
  3 picks our one-click path).
- **Freehand redlines in v1**: numbered tags that create comments are
  the valuable 20% (phase D maybe).
- **Presenter / follow-me / multi-user live sync**: even Revizto
  meetings are one driver on a shared screen.
- **Section cut-face hatching by default**: documented ACC
  performance killer.
- **xeokit v3 alpha, XKT conversion, metamodel TreeView**: our glTF
  extras already carry what we need.

---

## 11. Decisions for Nathan

0. **The mouse (the left-click question).** Recommendation:
   web-standard (left-drag orbit, left-click select) + Revit's
   middle-button grammar working simultaneously + strong feedback
   (hover glow, pivot marker, hint bar). Alternative: Revit-strict
   mode (left-click select only, orbit on Shift+middle only), which
   no web viewer ships and feels dead on trackpads, but is truer to
   "emulate Revit". Escape hatch either way: a preference toggle
   later is cheap. Which default?
1. **Auto section box on clash focus**: off (recommended; ghost is
   our signature and matches the photos, box is one keypress) or on
   (Revizto style)?
2. **Single-key decisions**: comfortable with 1/2/4 as bare
   keystrokes that advance (undo toast as guardrail, Approved always
   asks for a reason)? Or require clicks on the card?
3. **Deep element properties**: where should "everything about this
   element" live? (a) Show in Revit is the deep-dive path
   (recommended for now), (b) grow the export to carry full
   parameters for a proper properties panel (real export size/time
   cost), or (c) live parameter fetch from Revit over the bridge for
   the selected element only (small bridge addition, host does the
   lookup on demand).
4. **Standalone 3D Viewer endgame**: as this tab absorbs visibility,
   minimap, and clash context, the standalone tool's unique value
   shrinks to walkthrough quality (sun, AO, edges) and
   share-to-browser. Keep two tools indefinitely, or plan to fold
   walkthrough + share into this app later and retire the button?
   (No action needed now; sets direction.)
5. **Walk mode**: agree to defer?
