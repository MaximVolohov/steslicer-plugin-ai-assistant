<#
.SYNOPSIS
  Builds the submission archive <team>.zip with plugin/ and results/ (case.md, section 7).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File package.ps1 -Team myteam
#>
param(
    [Parameter(Mandatory = $true)][string]$Team,
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path          # ai_assistant\
$stage = Join-Path $env:TEMP ("ste_ai_package_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $stage | Out-Null
try {
    Copy-Item (Join-Path $root "plugin") (Join-Path $stage "plugin") -Recurse
    Copy-Item (Join-Path $root "results") (Join-Path $stage "results") -Recurse
    Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    if (-not $Out) { $Out = Join-Path $root "$Team.zip" }
    if (Test-Path $Out) { Remove-Item $Out -Force }
    # Compress-Archive in Windows PowerShell 5.1 writes "\" into entry names (not zip-spec compliant),
    # so build the archive with forward-slash entries explicitly.
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::Open($Out, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in Get-ChildItem $stage -Recurse -File) {
            $entry = $file.FullName.Substring($stage.Length + 1).Replace("\", "/")
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $entry) | Out-Null
        }
    } finally {
        $archive.Dispose()
    }
    Write-Host "Created $Out"
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}
