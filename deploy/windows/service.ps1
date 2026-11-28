<#
.SYNOPSIS
  Runs the CX Nova Agent Orchestration Layer as a Windows service that starts with the machine.

.DESCRIPTION
  Elevated PowerShell, from the repository folder:
      .\deploy\windows\service.ps1              install (or update) and start the service, open the firewall port
      .\deploy\windows\service.ps1 -Restart     restart after a code or .env change
      .\deploy\windows\service.ps1 -Status
      .\deploy\windows\service.ps1 -Uninstall

  With NSSM (https://nssm.cc, nssm.exe on PATH or in C:\nssm) the shop runs as a real Windows service
  "CXNovaAgentShop" with log files under logs\. Without NSSM it runs as a scheduled task of the same name
  that starts at boot under SYSTEM and restarts if it stops.
#>
[CmdletBinding()]
param(
  [switch]$Restart,
  [switch]$Status,
  [switch]$Uninstall,
  [int]$Port = 0,
  [string]$Name = "CXNovaAgentShop"
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$caf = Join-Path $root ".venv\Scripts\caf.exe"
if (-not (Test-Path $caf)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }

function Get-EnvValue([string]$key, [string]$default) {
  if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern "^\s*$key\s*=\s*(.+?)\s*$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"') }
  }
  return $default
}
if ($Port -le 0) { $Port = [int](Get-EnvValue "CAF_PORT" "8080") }
$bind = Get-EnvValue "CAF_HOST" "0.0.0.0"
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

function Show-Status {
  if ($nssm) {
    if (Service-Exists) { "$Name service: $((Get-Service -Name $Name).Status)" } else { "$Name service is not installed" }
  } else {
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($t) { "$Name task: $($t.State)" } else { "$Name is not installed" }
  }
  try {
    $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5
    $all = if ($h.agents_total -and $h.agents_total -gt $h.agents_registered) { " blueprint, $($h.agents_total) with agent packs" } else { "" }
    "health: $($h.status), agents $($h.agents_registered)/$($h.agents_expected)$all, provider $($h.llm.provider)"
    $a2a = if ($h.protocols -and $h.protocols.a2a) { "A2A $($h.protocols.a2a -join ' + ')" } else { "" }
    $mcp = if ($h.protocols -and $h.protocols.mcp) { ", MCP $($h.protocols.mcp.revision) (SDK $($h.protocols.mcp.sdk))" } else { "" }
    if ($a2a) { "protocols: $a2a$mcp" }
  }
  catch { "health: not answering on port $Port yet (give it 20 seconds after a start, then check logs\service.log)" }
}

if ($Status) { Show-Status; exit 0 }

if ($Uninstall) {
  if ($nssm -and (Service-Exists)) { Invoke-Nssm stop $Name | Out-Null; Invoke-Nssm remove $Name confirm | Out-Null }
  Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
  Get-Process -Name caf -ErrorAction SilentlyContinue | Stop-Process -Force
  Remove-NetFirewallRule -DisplayName "CX Nova Agent Orchestration Layer ($Port)" -ErrorAction SilentlyContinue
  Write-Host "Removed $Name"
  exit 0
}

if ($Restart) {
  if (Service-Exists) {
    # Stop-Service/Start-Service work for an NSSM service too and report failures (nssm restart is silent about them)
    $svc = Get-Service -Name $Name
    if ($svc.Status -ne "Stopped") { Stop-Service -Name $Name -Force; $svc.WaitForStatus("Stopped", "00:00:40") }
    Start-Service -Name $Name
  }
  elseif (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Get-Process -Name caf -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $Name
  } else { Write-Host "$Name is not installed yet: run .\deploy\windows\service.ps1 without -Restart" -ForegroundColor Yellow; exit 1 }
  Start-Sleep -Seconds 8
  Show-Status
  exit 0
}

# ---- install
if ($nssm) {
  if (Service-Exists) { Invoke-Nssm stop $Name | Out-Null; Invoke-Nssm remove $Name confirm | Out-Null; Start-Sleep -Seconds 2 }
  $out = Invoke-Nssm install $Name $caf serve --host $bind --port $Port
  if ($script:NssmExit -ne 0) { throw "NSSM could not install the service: $out" }
  Invoke-Nssm set $Name AppDirectory $root | Out-Null
  Invoke-Nssm set $Name DisplayName "CX Nova Agent Orchestration Layer" | Out-Null
  Invoke-Nssm set $Name Description "CX Nova Agent Orchestration Layer: storefront, agent console, API, MCP and A2A (Coforge Agentic Framework)" | Out-Null
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
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"& '$root\deploy\windows\start.ps1' -BindHost $bind -Port $Port *>> '$root\logs\service.log'`"" `
    -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "CX Nova Agent Orchestration Layer (Coforge Agentic Framework)" | Out-Null
  Start-ScheduledTask -TaskName $Name
  Write-Host "Installed scheduled task $Name (starts at boot, restarts if it stops); logs in logs\service.log"
  Write-Host "Tip: install NSSM (https://nssm.cc) and re-run this script for a real Windows service." -ForegroundColor DarkGray
}

if (-not (Get-NetFirewallRule -DisplayName "CX Nova Agent Orchestration Layer ($Port)" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "CX Nova Agent Orchestration Layer ($Port)" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
  Write-Host "Opened Windows Firewall for TCP $Port (also open it in the Azure network security group if people connect from outside)"
}
Start-Sleep -Seconds 10
Show-Status
$publicUrl = Get-EnvValue "CAF_PUBLIC_URL" "http://$($env:COMPUTERNAME):$Port"
Write-Host "`nStorefront  $publicUrl/shop`nConsole     $publicUrl/console`nAdmin       $publicUrl/shop/admin  (operator token = CAF_API_TOKEN in .env)" -ForegroundColor Green
