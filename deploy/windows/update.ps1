<#
.SYNOPSIS
  Updates a running installation from GitHub: pull, reinstall dependencies, restart.
      .\deploy\windows\update.ps1                    use the console build that ships in git (the normal case)
      .\deploy\windows\update.ps1 -RebuildConsole    rebuild the console from console\src first (needs Node.js)

  The service is stopped while pip runs: Windows keeps .venv\Scripts\caf.exe locked while it is in use, and pip
  rewrites that file. The service is started again at the end (20 seconds of downtime, usually less).
#>
param([switch]$RebuildConsole, [string]$Name = "CXNovaAgentShop")
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
# a console built on this machine would make the pull fail ("local changes would be overwritten"): the committed build wins
git checkout -- console/dist 2>$null
Write-Host "Pulling the latest code..." -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull failed: see the message above (git status shows what is in the way), then run update.ps1 again" }
$env:PYTHONUTF8 = "1"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"

$svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
$task = $null
if (-not $svc) { $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue }
if ($svc -and $svc.Status -ne "Stopped") {
  Write-Host "Stopping $Name while the dependencies update..."
  Stop-Service -Name $Name -Force
  $svc.WaitForStatus("Stopped", "00:00:40")
}
if ($task) { Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue }
Get-Process -Name caf -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

& $venvPy -m pip install -e ".[anthropic,mcp,browser,dev]" --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip reported a problem (see above); starting the service with what is installed" -ForegroundColor Yellow }
if ($RebuildConsole) {
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "-RebuildConsole needs Node.js on PATH" }
  if (-not (Test-Path "console\node_modules\nunjucks")) { npm install --prefix console nunjucks@3.2.4 --silent }
  & $venvPy console\build.py | Out-Null
}

if ($svc) { Start-Service -Name $Name }
elseif ($task) { Start-ScheduledTask -TaskName $Name }
else { Write-Host "$Name is not installed as a service yet: run .\deploy\windows\service.ps1" -ForegroundColor Yellow; exit 0 }
Start-Sleep -Seconds 12
& (Join-Path $PSScriptRoot "service.ps1") -Status
