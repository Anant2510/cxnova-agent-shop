<#
.SYNOPSIS
  Runs DocuInsights in the foreground (what the service runs). The address, the port, the prefix, the data folder,
  the API key and the model come from .env in the repository folder.
#>
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$env:PYTHONUTF8 = "1"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }
& $py -m docuinsights serve
