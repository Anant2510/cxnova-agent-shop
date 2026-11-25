<#
.SYNOPSIS
  Updates a running installation from GitHub: pull, reinstall what it needs, restart.
      .\deploy\windows\update.ps1

  .env, data\ and logs\ are not in git and are not touched. The service is stopped while pip runs and started
  again at the end (a few seconds of downtime).
#>
param([string]$Name = "CXNovaDocuInsights")
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
Write-Host "Pulling the latest code..." -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull failed: see the message above (git status shows what is in the way), then run update.ps1 again" }
$env:PYTHONUTF8 = "1"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }

$svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
$task = $null
if (-not $svc) { $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue }
if ($svc -and $svc.Status -ne "Stopped") {
  Write-Host "Stopping $Name while it updates..."
  Stop-Service -Name $Name -Force
  $svc.WaitForStatus("Stopped", "00:00:40")
}
if ($task) { Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue }
# only this installation's own processes: Python started from this folder with "docuinsights serve", and the interpreter its launcher started
$serving = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like "*docuinsights serve*" })
$mine = @($serving | Where-Object { $_.CommandLine -like "*$root*" })
$ids = @($mine | ForEach-Object { $_.ProcessId })
$children = @($serving | Where-Object { $ids -contains $_.ParentProcessId })
foreach ($p in @($mine) + @($children)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

& $venvPy -m pip install -e ".[server,pdf]" --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip reported a problem (see above); starting the service with what is installed" -ForegroundColor Yellow }

if ($svc) { Start-Service -Name $Name }
elseif ($task) { Start-ScheduledTask -TaskName $Name }
else { Write-Host "$Name is not installed as a service yet: run .\deploy\windows\service.ps1" -ForegroundColor Yellow; exit 0 }
Start-Sleep -Seconds 10
& (Join-Path $PSScriptRoot "service.ps1") -Status
