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
  tests/
    test_extension_integrity.py
  scripts/
    test.ps1
    build.ps1
    deploy.ps1
  run_tests.ps1
  README.md
```

## Components

Current extension package: `dbHMS Extensions.extension`

Included tools:

- **Align Views**: align viewport/title positions to a master viewport
- **Sheet Setup**: generate sheets/views in bulk from discipline/level matrices
- **Sheet Manager**: browse/filter/rename/renumber/duplicate sheets and manage revisions
- **View Templates Manager**: edit and bulk-apply view template settings
- **Revisions Manager**: create/reorder/edit revisions and assign across sheets
- **View Range Helper**: visual editor for plan view range planes

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
