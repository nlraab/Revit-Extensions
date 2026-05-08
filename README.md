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
          dbHMS Tools.panel/
            AlignViews.pushbutton/
            Revisions Manager.pushbutton/
            Sheet Manager.pushbutton/
            SheetSetup.pushbutton/
            View Range Helper.pushbutton/
            View Templates Manager.pushbutton/
        Clash Detection.tab/
          Clash Detection.panel/
            Settings.pushbutton/
            Test Library.pushbutton/
            Run Clash Test.pushbutton/
            Clash Browser.pushbutton/
            Reports.pushbutton/
            Walkthrough.pushbutton/
        lib/
          clash_core/                    <- shared data model + persistence
          clash_detect/                  <- detection algorithms
          clash_report/                  <- BCF / XLSX / HTML builders
          clash_view/                    <- 3D view + walkthrough helpers
          dbhms_ui/                      <- shared friendly popup dialog
  tests/
    test_extension_integrity.py
    test_clash_*.py                       <- pure-data unit tests for clash modules
    test_walkthrough_*.py                 <- pure-data unit tests for walkthrough modules
  scripts/
    test.ps1
    build.ps1
    deploy.ps1
  run_tests.ps1
  CLAUDE.md                                <- instructions for AI-assisted development
  README.md
```

## Components

Current extension package: `dbHMS Extensions.extension`

### dbHMS Tools tab

- **Align Views**: align viewport/title positions to a master viewport
- **Sheet Setup**: generate sheets/views in bulk from discipline/level matrices
- **Sheet Manager**: browse/filter/rename/renumber/duplicate sheets and manage revisions
- **View Templates Manager**: edit and bulk-apply view template settings
- **Revisions Manager**: create/reorder/edit revisions and assign across sheets
- **View Range Helper**: visual editor for plan view range planes

### Clash Detection tab

A complete clash-coordination toolkit. Six pushbuttons working against a
shared per-project JSON database under a configurable shared root:

- **Settings**: per-machine + per-project config; link role mapping
  (Architectural / Structural / ignore).
- **Test Library**: firm-wide default tests + per-project overrides.
  Multi-source set support (host + linked-doc filtering).
- **Run Clash Test**: real Revit `InterferenceCheck` over the host doc
  + role-mapped linked models. Persistent identity via fingerprints —
  re-runs preserve comments, status, history; auto-resolves disappeared
  clashes; reopens reappearing ones.
- **Clash Browser**: live grid + filter card + saved filter presets.
  Status dropdown, trade reassign, comments, history all persist
  immediately. Show in 3D + Walkthrough Here per-clash actions.
- **Reports**: export filtered clashes as BCF 2.1 (.bcfzip) for
  consultants, Excel (.xlsx) for internal review, or HTML summary for
  client-facing handoffs (Save-as-PDF in any browser → PDF for free).
- **Walkthrough**: free-fly through the model with WASD + mouse-look
  (F-key toggle) for coordination meetings. Discipline visibility
  toggles, per-project saved camera bookmarks, 1920×1080 ray-traced
  PNG render export.

Architecture, data model, and full tool documentation are in
[Clash Detection.tab/README.md](src/extensions/dbHMS%20Extensions.extension/Clash%20Detection.tab/README.md).
Read that before making serious changes anywhere under `Clash Detection.tab/`
or `lib/clash_*/`.

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
