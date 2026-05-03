Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -m unittest discover -s "$PSScriptRoot\..\tests" -p "test_*.py" -v
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    python -m unittest discover -s "$PSScriptRoot\..\tests" -p "test_*.py" -v
    exit $LASTEXITCODE
}

Write-Error "No Python interpreter found. Install Python 3, then run scripts\test.ps1 again."
