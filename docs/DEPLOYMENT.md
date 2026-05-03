# Deployment Notes

## Target

Deploy the extension folder `dbHMS Extensions.extension` into your pyRevit extensions root.

## Steps

1. Run tests:
   - `.\run_tests.ps1`
2. Optional build artifact:
   - `.\scripts\build.ps1`
3. Deploy to your pyRevit extension path:
   - `.\scripts\deploy.ps1 -TargetRoot "C:\Path\To\pyRevit\Extensions"`
4. Reload pyRevit in Revit (or restart Revit).

## Rollback

- Keep a backup copy of the previous extension folder in your deployment target.
- To rollback, replace the deployed folder with the backup.
