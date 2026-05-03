param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path "$PSScriptRoot\.."
$sourceExtension = Join-Path $repoRoot "src\extensions\dbHMS Extensions.extension"

if (!(Test-Path $sourceExtension)) {
    throw "Source extension folder not found: $sourceExtension"
}

if (!(Test-Path $TargetRoot)) {
    throw "Target path does not exist: $TargetRoot"
}

$targetExtension = Join-Path $TargetRoot "dbHMS Extensions.extension"

if (Test-Path $targetExtension) {
    Remove-Item -Recurse -Force $targetExtension
}

Copy-Item -Recurse -Force $sourceExtension $targetExtension
Write-Host "Deploy complete: $targetExtension"
