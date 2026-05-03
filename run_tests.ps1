Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

& "$PSScriptRoot\scripts\test.ps1"
exit $LASTEXITCODE
