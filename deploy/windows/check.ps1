<#
.SYNOPSIS
  Proves the deployment: the model key, then every agent live on the configured model, then the service URLs.
      .\deploy\windows\check.ps1            all 28 agents on their routed tiers (Sonnet and Opus)
      .\deploy\windows\check.ps1 -Fast      every model step on the fast tier (cheaper, quicker)
      .\deploy\windows\check.ps1 -Offline   no model calls at all
#>
param([switch]$Fast, [switch]$Offline)
$ErrorActionPreference = "Continue"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$env:PYTHONUTF8 = "1"
$caf = Join-Path $root ".venv\Scripts\caf.exe"
if (-not (Test-Path $caf)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }
Write-Host "Model routing on this server:" -ForegroundColor Cyan
& $caf models | Select-Object -First 3
Write-Host ""
if ($Offline) { & $caf health }
elseif ($Fast) { & $caf health --live --fast }
else { & $caf health --live }
$code = $LASTEXITCODE
$port = 8080
if (Test-Path ".env") { $m = Select-String -Path ".env" -Pattern "^\s*CAF_PORT\s*=\s*(\d+)" | Select-Object -First 1; if ($m) { $port = [int]$m.Matches[0].Groups[1].Value } }
try {
  $h = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 5
  Write-Host "`nService on port ${port}: $($h.status), $($h.agents_registered)/$($h.agents_expected) agents registered, provider $($h.llm.provider)" -ForegroundColor Green
} catch { Write-Host "`nThe service is not answering on port $port (start it with .\deploy\windows\service.ps1)" -ForegroundColor Yellow }
exit $code
