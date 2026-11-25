<#
.SYNOPSIS
  Installs DocuInsights on a Windows server (first time, or after a fresh clone).

.DESCRIPTION
  Elevated PowerShell, from the repository folder:
      Set-ExecutionPolicy -Scope Process Bypass -Force
      .\deploy\windows\install.ps1

  What it does:
    1. Finds Python 3.10+ (installs Python 3.12 with winget when missing)
    2. Creates .venv and installs DocuInsights with its server and PDF parts
    3. Creates .env from .env.example and sets what a server needs: a generated API key, the address prefix,
       the port and the data folder. Lines you already set are left as they are.
    4. Takes the model settings from the CX Nova Agent Orchestration Layer on this machine when it is there (the
       same key, endpoint and deployments), unless .env already names a model. No value is shown.
    5. Runs a bundled sample without a model, then asks the configured model once

      -Prefix /docuinsights                       the address prefix behind the web server ("" for a host name or a port of its own)
      -Port 8090
      -LayerFolder C:\cxnova\cxnova-agent-shop    where the layer's .env is looked for ("" to skip)

  Afterwards: .\deploy\windows\service.ps1 to run it as a service, then the web server rule in DEPLOY.md.
#>
[CmdletBinding()]
param(
  [string]$Prefix = "/docuinsights",
  [int]$Port = 8090,
  [string]$LayerFolder = "C:\cxnova\cxnova-agent-shop",
  [string]$PythonExe = ""
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
Write-Host "DocuInsights: installing in $root" -ForegroundColor Cyan
$env:PYTHONUTF8 = "1"

function Find-Python {
  param([string]$Preferred)
  $candidates = @()
  if ($Preferred) { $candidates += $Preferred }
  $candidates += @("py -3.13", "py -3.12", "py -3.11", "py -3.10", "python3.12", "python3.11", "python", "python3")
  foreach ($c in $candidates) {
    try {
      $parts = $c.Split(" ")
      $v = & $parts[0] $parts[1..($parts.Length - 1)] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v) {
        $major, $minor = $v.Trim().Split(".")
        if ([int]$major -eq 3 -and [int]$minor -ge 10) { return $c }
      }
    } catch { }
  }
  return $null
}

$py = Find-Python -Preferred $PythonExe
if (-not $py) {
  Write-Host "Python 3.10+ not found. Installing Python 3.12 with winget..." -ForegroundColor Yellow
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements --override "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
  }
  if (-not $py) { throw "Install Python 3.12 from https://www.python.org/downloads/windows/ (tick 'Add to PATH'), then run this script again." }
}
Write-Host "Using Python: $py"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  $parts = $py.Split(" ")
  & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
}
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip wheel --quiet
Write-Host "Installing DocuInsights and what it needs..."
& $venvPy -m pip install -e ".[server,pdf]" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ---- .env: one KEY=value per line. Written as UTF-8 without a byte-order mark.
$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) {
  Copy-Item (Join-Path $root ".env.example") $envPath
  Write-Host "Created .env from .env.example"
}
$script:EnvLines = [System.Collections.Generic.List[string]]::new()
foreach ($line in [System.IO.File]::ReadAllLines($envPath)) { $script:EnvLines.Add($line) }

function Get-Line([System.Collections.Generic.List[string]]$Lines, [string]$Key) {
  for ($n = 0; $n -lt $Lines.Count; $n++) { if ($Lines[$n] -match ("^\s*" + [regex]::Escape($Key) + "\s*=")) { return $n } }
  return -1
}
function Get-Value([System.Collections.Generic.List[string]]$Lines, [string]$Key) {
  $n = Get-Line $Lines $Key
  if ($n -lt 0) { return "" }
  $v = ($Lines[$n] -split "=", 2)[1]
  $v = ($v -split "\s+#", 2)[0]   # a comment after the value is not part of it
  return $v.Trim().Trim('"').Trim("'")
}
# Sets KEY=value. With -Keep a value that is already there stays. Returns $true when the line was written.
function Set-Line([string]$Key, [string]$Value, [switch]$Keep) {
  $n = Get-Line $script:EnvLines $Key
  if ($n -ge 0) {
    if ($Keep -and (Get-Value $script:EnvLines $Key)) { return $false }
    $script:EnvLines[$n] = "$Key=$Value"
  } else { $script:EnvLines.Add("$Key=$Value") }
  return $true
}
function New-RandomKey {   # 32 bytes from the system's cryptographic generator, as 64 hex characters
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLower()
}

if (Set-Line "DOCUINSIGHTS_API_KEY" (New-RandomKey) -Keep) { Write-Host "Generated DOCUINSIGHTS_API_KEY (the workbench asks for it once per browser; it is in .env)" }
[void](Set-Line "DOCUINSIGHTS_ROOT_PATH" $Prefix)
[void](Set-Line "DOCUINSIGHTS_PORT" "$Port")
[void](Set-Line "DOCUINSIGHTS_HOST" "127.0.0.1" -Keep)
[void](Set-Line "DOCUINSIGHTS_DATA_DIR" (Join-Path $root "data") -Keep)

# ---- the model: the layer's settings on this machine, when DocuInsights has none of its own
if (-not (Get-Value $script:EnvLines "DOCUINSIGHTS_MODEL_PROVIDER")) {
  $layerEnv = if ($LayerFolder) { Join-Path $LayerFolder ".env" } else { "" }
  if ($layerEnv -and (Test-Path $layerEnv)) {
    $layer = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($layerEnv)) { $layer.Add($line) }
    $provider = Get-Value $layer "CAF_LLM_PROVIDER"
    if ($provider -eq "anthropic" -or $provider -eq "anthropic_foundry") {
      $copied = 0
      foreach ($k in @("ANTHROPIC_API_KEY", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
                       "FOUNDRY_DEPLOYMENT_FAST", "FOUNDRY_DEPLOYMENT_BALANCED", "FOUNDRY_DEPLOYMENT_PRO", "FOUNDRY_DEPLOYMENT_COMPLEX")) {
        $v = Get-Value $layer $k
        if ($v) { [void](Set-Line $k $v); $copied++ }
      }
      [void](Set-Line "DOCUINSIGHTS_MODEL_PROVIDER" $provider)
      Write-Host "Model: the same as the layer in $LayerFolder ($provider, $copied settings copied; no value is shown)"
    } else { Write-Host "The layer in $LayerFolder names no Claude provider: set DOCUINSIGHTS_MODEL_PROVIDER in .env yourself" -ForegroundColor Yellow }
  } else { Write-Host "No model set: DocuInsights will read with patterns and rules. To connect Claude, set DOCUINSIGHTS_MODEL_PROVIDER and its key in .env (see .env.example)" -ForegroundColor Yellow }
}
[System.IO.File]::WriteAllLines($envPath, $script:EnvLines, (New-Object System.Text.UTF8Encoding($false)))
New-Item -ItemType Directory -Force -Path "data", "logs" | Out-Null

Write-Host "`nA bundled sample, end to end, without a model..." -ForegroundColor Cyan
$env:DOCUINSIGHTS_MODEL_PROVIDER = "offline"
& $venvPy -m docuinsights assess --profile supplier_invoice --sample mismatch | Select-Object -Last 1
$sampleExit = $LASTEXITCODE
Remove-Item Env:DOCUINSIGHTS_MODEL_PROVIDER
if ($sampleExit -ne 0) { throw "the sample did not run: see the message above" }
Write-Host "`nThe model..." -ForegroundColor Cyan
& $venvPy -m docuinsights check
if ($LASTEXITCODE -ne 0) { Write-Host "The model did not answer on every tier (see above). DocuInsights still runs: it falls back to patterns and says so on screen. Fix the model lines in .env, then: .venv\Scripts\python -m docuinsights check" -ForegroundColor Yellow }

Write-Host "`nInstalled." -ForegroundColor Green
Write-Host @"

Next:
  1. .\deploy\windows\service.ps1        run it as a Windows service on 127.0.0.1:$Port
  2. Add the web server rule from DEPLOY.md, so https://<this server>$Prefix/ reaches it
  3. Open that address. It asks for the API key once: the value of DOCUINSIGHTS_API_KEY in .env
"@
