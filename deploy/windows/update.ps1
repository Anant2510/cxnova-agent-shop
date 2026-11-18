<#
.SYNOPSIS
  Updates a running installation from GitHub: pull, reinstall dependencies, restart.
      .\deploy\windows\update.ps1                    use the console build that ships in git (the normal case)
      .\deploy\windows\update.ps1 -RebuildConsole    rebuild the console from console\src first (needs Node.js)
#>
param([switch]$RebuildConsole)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
# a console built on this machine would make the pull fail ("local changes would be overwritten"): the committed build wins
git checkout -- console/dist 2>$null
Write-Host "Pulling the latest code..." -ForegroundColor Cyan
git pull --ff-only
$env:PYTHONUTF8 = "1"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
& $venvPy -m pip install -e ".[anthropic,mcp,browser,dev]" --quiet
if ($RebuildConsole) {
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "-RebuildConsole needs Node.js on PATH" }
  if (-not (Test-Path "console\node_modules\nunjucks")) { npm install --prefix console nunjucks@3.2.4 --silent }
  & $venvPy console\build.py | Out-Null
}
& (Join-Path $PSScriptRoot "service.ps1") -Restart
