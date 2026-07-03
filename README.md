# Revit Extensions

Collection of custom pyRevit tools maintained in a structured, testable repository.

## Purpose

This repository provides a release-ready source of Revit productivity extensions with:

- organized source layout for one or more extensions
- repeatable local testing before deployment
- build and deployment scripts for packaging/release workflows
- project documentation for users and maintainers

## Repository Structure

```text
Revit Extensions/
  src/
    extensions/
      dbHMS Extensions.extension/
        dbHMS Tools.tab/
          BIM Tools.panel/                  <- general productivity tools
            AlignViews.pushbutton/
            Revisions Manager.pushbutton/
            Sheet Manager.pushbutton/
            SheetSetup.pushbutton/
            View Range Helper.pushbutton/
            View Templates Manager.pushbutton/
          Clash Detection.panel/            <- MEP clash coordination platform
            Clash Detection.pushbutton/     <- the coordination web app (WebView2)
            3D Viewer.pushbutton/           <- federated model viewer web app
            README.md                       <- panel-specific architecture
            CLASH_*.md                      <- design documents
        lib/
          clash_core/                       <- data model, merge, persistence, folder binding
          clash_detect/                     <- detection engines + broad phase + enrichment
          clash_score/                      <- importance engine (Critical/Major/Minor)
          clash_group/                      <- issue grouping engine
          clash_report/                     <- BCF + meeting-agenda digest builders
          clash_export/                     <- Revit -> glTF snapshot export
          clash_identity/                   <- clash <-> glTF federation keys
          clash_view/                       <- Revit-side viewpoint capture
          clash_share/                      <- 3D Viewer share-to-browser packaging
          dbhms_ui/                         <- shared friendly popup dialog
  tests/
    test_extension_integrity.py
    test_clash_*.py                         <- pure-data unit tests for clash modules
  scripts/
    test.ps1
    build.ps1
    deploy.ps1
  run_tests.ps1
  CLAUDE.md                                  <- instructions for AI-assisted development
  README.md
```

## Components

The extension ships a single Revit ribbon tab — **dbHMS Tools** — with
two panels.

### dbHMS Tools panel

General productivity tools:

- **Align Views**: align viewport/title positions to a master viewport
- **Sheet Setup**: generate sheets/views in bulk from discipline/level matrices
- **Sheet Manager**: browse/filter/rename/renumber/duplicate sheets and manage revisions
- **View Templates Manager**: edit and bulk-apply view template settings
- **Revisions Manager**: create/reorder/edit revisions and assign across sheets
- **View Range Helper**: visual editor for plan view range planes

### Clash Detection panel

The firm's clash-coordination platform: two WebView2 web-app tools over a
shared pure-Python engine, working against a per-project clash-data
folder stored inside the Revit model:

- **Clash Detection**: the coordination hub. Runs detection (hard/soft
  with a grid broad phase), ranks every clash Critical/Major/Minor with a
  one-sentence reason (constraint-first importance engine), groups
  clashes into persistent issues (racks / element runs) that survive
  re-runs, and fronts it all with the "Quiet Ledger" browser: meeting
  agenda, issue inspector, filters, comments, BCF-by-issue and
  pre-meeting digest exports.
- **3D Viewer**: federated glTF viewer for the whole model - category /
  workset visibility, clash markers, share-to-browser (self-contained
  HTML), used side-by-side with the coordination app.

Deep design docs live in the panel folder (spec, importance research,
grouping design, real-project calibration findings).

Architecture, data model, and full tool documentation are in
[Clash Detection.panel/README.md](src/extensions/dbHMS%20Extensions.extension/dbHMS%20Tools.tab/Clash%20Detection.panel/README.md).
Read that before making serious changes anywhere under
`Clash Detection.panel/` or `lib/clash_*/`.

### Shared UI helper

The `lib/dbhms_ui/` module is a small set of cross-tool UI utilities so
every dbHMS tool looks identical when it puts up a popup. Currently:

- `dbhms_ui.info(message, title=...)` — friendly dbHMS-branded modal
  dialog (slate header bar, blue ⓘ glyph, OK button) used in place of
  pyRevit's `forms.alert(...)` for informational popups. The default
  pyRevit alert uses the Windows yellow-warning-triangle icon even on
  success messages, which reads as failure; this replacement drops
  that and matches the rest of the firm's UI.

Yes/no confirmations and fatal-precondition gates still use
`forms.alert(...)`. See **CLAUDE.md** for the full UI-conventions
guidance new tools should follow.

## Testing

Run all tests from repo root:

```powershell
.\run_tests.ps1
```

Or directly:

```powershell
.\scripts\test.ps1
```

Current suite checks:

- expected pushbutton folders exist
- all `script.py` files parse (syntax check)
- no git conflict markers in `.py/.xaml/.json`
- all `.xaml` files are well-formed XML
- required `config.json` shape for key tools

## Build

Create a deployable zip package:

```powershell
.\scripts\build.ps1
```

Output is written to `artifacts/` as a timestamped zip.

## Deploy

Copy the extension folder to a pyRevit extensions location:

```powershell
.\scripts\deploy.ps1 -TargetRoot "C:\Path\To\pyRevit\Extensions"
```

The script replaces `dbHMS Extensions.extension` in the target path with the repo version.

## Typical Release Flow

1. Make code changes in `src/extensions/...`.
2. Run tests: `.\run_tests.ps1`.
3. Build package: `.\scripts\build.ps1`.
4. Deploy to target extension path: `.\scripts\deploy.ps1 -TargetRoot ...`.
5. Reload pyRevit (or restart Revit) and smoke test in a project.
