Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Resolve-Path "$PSScriptRoot\.."
$outDir = Join-Path $root "artifacts"
$srcDir = Join-Path $root "src\extensions\dbHMS Extensions.extension"

if (!(Test-Path $srcDir)) {
    throw "Source extension folder not found: $srcDir"
}

if (!(Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zipPath = Join-Path $outDir "dbHMS-Extensions-$stamp.zip"

Compress-Archive -Path $srcDir -DestinationPath $zipPath -Force
Write-Host "Build complete: $zipPath"
