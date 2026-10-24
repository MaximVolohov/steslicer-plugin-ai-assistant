<#
.SYNOPSIS
  Links (or copies) the AIAssistant plugin into the STE Slicer user plugins folder.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1            # junction: edits are live after a slicer restart
  powershell -ExecutionPolicy Bypass -File install.ps1 -Copy      # plain copy (as an end user would install)
  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param(
    [switch]$Copy,
    [switch]$Uninstall,
    [string]$Version = "7.10"
)

$ErrorActionPreference = "Stop"
$source = (Resolve-Path (Join-Path $PSScriptRoot "..\AIAssistant")).Path
$pluginsDir = Join-Path $env:APPDATA "steslicer\$Version\plugins"
$target = Join-Path $pluginsDir "AIAssistant"

if (Test-Path $target) {
    $item = Get-Item $target -Force
    if ($item.LinkType -eq "Junction" -or $item.LinkType -eq "SymbolicLink") {
        # Remove only the link itself, never the linked source folder.
        [System.IO.Directory]::Delete($target)
    } else {
        Remove-Item $target -Recurse -Force
    }
    Write-Host "Removed $target"
}

if ($Uninstall) { return }

New-Item -ItemType Directory -Force $pluginsDir | Out-Null
if ($Copy) {
    Copy-Item $source $target -Recurse -Exclude "__pycache__"
    Get-ChildItem $target -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    Write-Host "Copied plugin to $target"
} else {
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    Write-Host "Linked $target -> $source"
}
Write-Host "Restart STE Slicer, then open Extensions > AI Assistant > Open chat."
