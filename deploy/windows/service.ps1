<#
.SYNOPSIS
  Runs the CX Nova Agent Shop as a Windows service that starts with the machine.

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

function Show-Status {
  if ($nssm) { & $nssm status $Name } else {
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($t) { "$Name task: $($t.State)" } else { "$Name is not installed" }
  }
  try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5; "health: $($h.status), agents $($h.agents_registered)/$($h.agents_expected), provider $($h.llm.provider)" }
  catch { "health: not answering on port $Port yet" }
}

if ($Status) { Show-Status; exit 0 }

if ($Uninstall) {
  if ($nssm) { & $nssm stop $Name 2>$null; & $nssm remove $Name confirm } else {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Get-Process -Name caf -ErrorAction SilentlyContinue | Stop-Process -Force
  }
  Remove-NetFirewallRule -DisplayName "CX Nova Agent Shop ($Port)" -ErrorAction SilentlyContinue
  Write-Host "Removed $Name"
  exit 0
}

if ($Restart) {
  if ($nssm) { & $nssm restart $Name } else {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Get-Process -Name caf -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $Name
  }
  Start-Sleep -Seconds 4
  Show-Status
  exit 0
}

# ---- install
if ($nssm) {
  & $nssm stop $Name 2>$null | Out-Null
  & $nssm remove $Name confirm 2>$null | Out-Null
  & $nssm install $Name $caf serve --host $bind --port $Port
  & $nssm set $Name AppDirectory $root
  & $nssm set $Name DisplayName "CX Nova Agent Shop"
  & $nssm set $Name Description "CX Nova Agent Shop: storefront, agent console, API, MCP and A2A (Coforge Agentic Framework)"
  & $nssm set $Name Start SERVICE_AUTO_START
  & $nssm set $Name AppStdout (Join-Path $root "logs\service.log")
  & $nssm set $Name AppStderr (Join-Path $root "logs\service.log")
  & $nssm set $Name AppRotateFiles 1
  & $nssm set $Name AppRotateBytes 10485760
  & $nssm set $Name AppExit Default Restart
  & $nssm set $Name AppRestartDelay 5000
  & $nssm set $Name AppEnvironmentExtra PYTHONUTF8=1
  & $nssm start $Name
  Write-Host "Installed Windows service $Name (NSSM); logs in logs\service.log"
} else {
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"& '$root\deploy\windows\start.ps1' -BindHost $bind -Port $Port *>> '$root\logs\service.log'`"" `
    -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "CX Nova Agent Shop (Coforge Agentic Framework)" | Out-Null
  Start-ScheduledTask -TaskName $Name
  Write-Host "Installed scheduled task $Name (starts at boot, restarts if it stops); logs in logs\service.log"
  Write-Host "Tip: install NSSM (https://nssm.cc) and re-run this script for a real Windows service." -ForegroundColor DarkGray
}

if (-not (Get-NetFirewallRule -DisplayName "CX Nova Agent Shop ($Port)" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "CX Nova Agent Shop ($Port)" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
  Write-Host "Opened Windows Firewall for TCP $Port (also open it in the Azure network security group if people connect from outside)"
}
Start-Sleep -Seconds 5
Show-Status
$publicUrl = Get-EnvValue "CAF_PUBLIC_URL" "http://$($env:COMPUTERNAME):$Port"
Write-Host "`nStorefront  $publicUrl/shop`nConsole     $publicUrl/console`nAdmin       $publicUrl/shop/admin  (operator token = CAF_API_TOKEN in .env)" -ForegroundColor Green
