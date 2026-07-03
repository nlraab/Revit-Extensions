# Clash Detection

dbHMS's in-Revit clash coordination platform: find, rank, group, discuss,
assign, and resolve interferences without leaving Revit, built for the
firm's normal project shape (MEP host model + linked architectural model +
sometimes a linked structural model).

> **Read this file before making serious changes** to anything under
> `dbHMS Tools.tab/Clash Detection.panel/` or `lib/clash_*/`. Deep design
> rationale lives in the four design documents next to this file.

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
| `clash_score` | the importance engine: Layer A noise suppression + constraint-first tier rules -> Critical/Major/Minor + one-sentence reasons (firm standard in `defaults.py`; see CLASH_IMPORTANCE_RESEARCH.md) |
| `clash_group` | Layer C sticky issue groups: participation-anchored element stars, density-gated racks, successor adoption, group ops (see CLASH_GROUPING_DESIGN.md) |
| `clash_report` | BCF 2.1 export (per clash and one-topic-per-issue) + pre-meeting HTML agenda digest; consumed by the web app's Reports surface |
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
    viewpoints/<id>.png   # clash context thumbnails
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

CPython 3 unit tests (`.un_tests.ps1`) cover the pure engine: scoring
(tests/test_clash_score.py), grouping (test_clash_group.py), broad phase
(test_clash_broadphase.py), merge/identity/binding/BCF/digest, plus the
extension integrity suite (folder shape, bundle layout, telemetry, parse
checks). Revit-API paths (ES, detection filters, capture) are verified in
Revit by hand - see each module's docstring for its manual check.

## Design documents (this folder)

- `CLASH_REBUILD_SPEC.md` - the north-star spec (sections 2, 5, 6).
- `CLASH_IMPORTANCE_RESEARCH.md` - the scoring engine: research, the
  tier-rule design, calibration decisions (rev 3).
- `CLASH_GROUPING_DESIGN.md` - Layer C issue grouping design + S1-S8
  churn scenarios.
- `NIUHTC_CALIBRATION_FINDINGS.md` - the real-project audit that
  produced scoring revs 2-3 (band autopsy, retune packages, judges).
