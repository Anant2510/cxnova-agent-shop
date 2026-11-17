<#
.SYNOPSIS
  Updates a running installation from GitHub: pull, reinstall dependencies, rebuild the console if needed, restart.
      .\deploy\windows\update.ps1
#>
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
Write-Host "Pulling the latest code..." -ForegroundColor Cyan
git pull --ff-only
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
& $venvPy -m pip install -e ".[anthropic,mcp,browser,dev]" --quiet
if ((Get-Command node -ErrorAction SilentlyContinue) -and (Test-Path "console\build.py")) {
  $dist = Get-Item "console\dist\index.html" -ErrorAction SilentlyContinue
  $newest = Get-ChildItem "console\src" -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $dist -or ($newest -and $newest.LastWriteTime -gt $dist.LastWriteTime)) {
    if (-not (Test-Path "console\node_modules\nunjucks")) { npm install --prefix console nunjucks@3.2.4 --silent }
    & $venvPy console\build.py | Out-Null
  }
}
& (Join-Path $PSScriptRoot "service.ps1") -Restart
