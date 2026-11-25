<#
.SYNOPSIS
  Runs DocuInsights as a Windows service that starts with the machine.

.DESCRIPTION
  Elevated PowerShell, from the repository folder:
      .\deploy\windows\service.ps1              install (or update) and start the service
      .\deploy\windows\service.ps1 -Restart     restart after a code or .env change
      .\deploy\windows\service.ps1 -Status
      .\deploy\windows\service.ps1 -Uninstall

  With NSSM (https://nssm.cc, nssm.exe on PATH or in C:\nssm) it runs as a real Windows service
  "CXNovaDocuInsights" with log files under logs\. Without NSSM it runs as a scheduled task of the same name that
  starts at boot under SYSTEM and restarts if it stops.

  It listens on 127.0.0.1 (DOCUINSIGHTS_HOST and DOCUINSIGHTS_PORT in .env), for the machine's web server to
  forward to. Windows Firewall is opened only when DOCUINSIGHTS_HOST is set to another address.
#>
[CmdletBinding()]
param(
  [switch]$Restart,
  [switch]$Status,
  [switch]$Uninstall,
  [string]$Name = "CXNovaDocuInsights"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }

function Get-EnvValue([string]$key, [string]$default) {
  if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern "^\s*$key\s*=\s*(.+?)\s*$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"') }
  }
  return $default
}
$Port = [int](Get-EnvValue "DOCUINSIGHTS_PORT" "8090")
$bind = Get-EnvValue "DOCUINSIGHTS_HOST" "127.0.0.1"
$prefix = (Get-EnvValue "DOCUINSIGHTS_ROOT_PATH" "").TrimEnd("/")
$rule = "CX Nova DocuInsights ($Port)"
$nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source
if (-not $nssm -and (Test-Path "C:\nssm\nssm.exe")) { $nssm = "C:\nssm\nssm.exe" }
if (-not $nssm -and (Test-Path "C:\nssm\win64\nssm.exe")) { $nssm = "C:\nssm\win64\nssm.exe" }
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

# NSSM writes some of its messages to stderr, which PowerShell would treat as a terminating error under
# $ErrorActionPreference = "Stop"; every NSSM call goes through here and reports its exit code instead.
$script:NssmExit = 0
function Invoke-Nssm {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$NssmArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $nssm @NssmArgs 2>&1 | ForEach-Object { "$_" }
    $script:NssmExit = $LASTEXITCODE
    return (($out -join "`n") -replace "`0", "")   # NSSM prints UTF-16 on some consoles
  } finally { $ErrorActionPreference = $prev }
}
function Service-Exists { [bool](Get-Service -Name $Name -ErrorAction SilentlyContinue) }
# Only this installation's own processes: Python started from this folder with "docuinsights serve". Other Python
# programs on the machine are not touched.
function Stop-Own {
  $serving = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like "*docuinsights serve*" })
  $mine = @($serving | Where-Object { $_.CommandLine -like "*$root*" })
  $ids = @($mine | ForEach-Object { $_.ProcessId })
  $children = @($serving | Where-Object { $ids -contains $_.ParentProcessId })   # the interpreter the .venv launcher started
  foreach ($p in @($mine) + @($children)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Show-Status {
  if ($nssm) {
    if (Service-Exists) { "$Name service: $((Get-Service -Name $Name).Status)" } else { "$Name service is not installed" }
  } else {
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($t) { "$Name task: $($t.State)" } else { "$Name is not installed" }
  }
  try {
    $h = Invoke-RestMethod "http://127.0.0.1:$Port$prefix/health" -TimeoutSec 5
    "health: $($h.status), version $($h.version), model $($h.model), API key $($h.api_key)"
  }
  catch { "health: not answering on port $Port yet (give it 20 seconds after a start, then check logs\service.log)" }
}

if ($Status) { Show-Status; exit 0 }

if ($Uninstall) {
  if ($nssm -and (Service-Exists)) { Invoke-Nssm stop $Name | Out-Null; Invoke-Nssm remove $Name confirm | Out-Null }
  Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
  Stop-Own
  Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
  Write-Host "Removed $Name (the code, .env and data stay where they are)"
  exit 0
}

if ($Restart) {
  if (Service-Exists) {
    $svc = Get-Service -Name $Name
    if ($svc.Status -ne "Stopped") { Stop-Service -Name $Name -Force; $svc.WaitForStatus("Stopped", "00:00:40") }
    Start-Service -Name $Name
  }
  elseif (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Stop-Own
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $Name
  } else { Write-Host "$Name is not installed yet: run .\deploy\windows\service.ps1 without -Restart" -ForegroundColor Yellow; exit 1 }
  Start-Sleep -Seconds 8
  Show-Status
  exit 0
}

# ---- install: whatever ran before under this name is stopped first, then the port has to be free
if ($nssm -and (Service-Exists)) { Invoke-Nssm stop $Name | Out-Null; Invoke-Nssm remove $Name confirm | Out-Null }
Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
Stop-Own
Start-Sleep -Seconds 2
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  throw "Port $Port is already in use on this machine by another program. Choose another one: .\deploy\windows\install.ps1 -Port 8095, then run this script again and use the same port in the web server rule."
}
if ($nssm) {
  $out = Invoke-Nssm install $Name $py "-m" docuinsights serve   # "-m" in quotes: an argument for Python, not a parameter of this function
  if ($script:NssmExit -ne 0) { throw "NSSM could not install the service: $out" }
  Invoke-Nssm set $Name AppDirectory $root | Out-Null
  Invoke-Nssm set $Name DisplayName "CX Nova DocuInsights" | Out-Null
  Invoke-Nssm set $Name Description "DocuInsights: agentic document and case intelligence (workbench and API)" | Out-Null
  Invoke-Nssm set $Name Start SERVICE_AUTO_START | Out-Null
  Invoke-Nssm set $Name AppStdout (Join-Path $root "logs\service.log") | Out-Null
  Invoke-Nssm set $Name AppStderr (Join-Path $root "logs\service.log") | Out-Null
  Invoke-Nssm set $Name AppRotateFiles 1 | Out-Null
  Invoke-Nssm set $Name AppRotateBytes 10485760 | Out-Null
  Invoke-Nssm set $Name AppExit Default Restart | Out-Null
  Invoke-Nssm set $Name AppRestartDelay 5000 | Out-Null
  Invoke-Nssm set $Name AppEnvironmentExtra PYTHONUTF8=1 | Out-Null
  $out = Invoke-Nssm start $Name
  if ($script:NssmExit -ne 0) { Write-Host "Service installed but did not start: $out`nSee logs\service.log" -ForegroundColor Yellow }
  else { Write-Host "Installed Windows service $Name (NSSM); logs in logs\service.log" }
} else {
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"& '$root\deploy\windows\run.ps1' *>> '$root\logs\service.log'`"" `
    -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "DocuInsights: agentic document and case intelligence" | Out-Null
  Start-ScheduledTask -TaskName $Name
  Write-Host "Installed scheduled task $Name (starts at boot, restarts if it stops); logs in logs\service.log"
  Write-Host "Tip: install NSSM (https://nssm.cc) and re-run this script for a real Windows service." -ForegroundColor DarkGray
}

if ($bind -ne "127.0.0.1" -and $bind -ne "localhost" -and -not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
  Write-Host "Opened Windows Firewall for TCP $Port (also open it in the cloud network security group if people connect from outside)"
}
Start-Sleep -Seconds 10
Show-Status
Write-Host "`nOn this machine   http://127.0.0.1:$Port$prefix/`nFrom anywhere     https://<this server>$prefix/   once the web server rule in DEPLOY.md is in place`nAPI key           DOCUINSIGHTS_API_KEY in .env (the workbench asks for it once per browser)" -ForegroundColor Green
