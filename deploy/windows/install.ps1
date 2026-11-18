<#
.SYNOPSIS
  Installs the CX Nova Agent Orchestration Layer on a Windows server (first time, or after a fresh clone).

.DESCRIPTION
  Run from the repository folder in an elevated PowerShell:
      Set-ExecutionPolicy -Scope Process Bypass -Force
      .\deploy\windows\install.ps1

  What it does:
    1. Finds Python 3.11+ (installs Python 3.12 with winget when missing)
    2. Creates .venv and installs the framework with the Claude, MCP and browser extras
    3. Installs Chromium for Playwright (accessibility, heatmap and variation agents); skip with -SkipBrowser
    4. Creates .env from .env.example, generating CAF_API_TOKEN and CAF_SECRET_KEY if empty
    5. Creates the data folders and runs the offline health check of all 28 agents

  The web console ships prebuilt in git (console/dist) and is used as it is; pass -RebuildConsole to rebuild it
  here from console/src (needs Node.js). Afterwards: put your Foundry key in .env, then
  .\deploy\windows\service.ps1 to run it as a service.
#>
[CmdletBinding()]
param(
  [switch]$SkipBrowser,
  [switch]$RebuildConsole,
  [string]$PythonExe = ""
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
Write-Host "CX Nova Agent Orchestration Layer: installing in $root" -ForegroundColor Cyan
$env:PYTHONUTF8 = "1"

function Find-Python {
  param([string]$Preferred)
  $candidates = @()
  if ($Preferred) { $candidates += $Preferred }
  $candidates += @("py -3.13", "py -3.12", "py -3.11", "python3.12", "python3.11", "python", "python3")
  foreach ($c in $candidates) {
    try {
      $parts = $c.Split(" ")
      $v = & $parts[0] $parts[1..($parts.Length - 1)] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v) {
        $major, $minor = $v.Trim().Split(".")
        if ([int]$major -eq 3 -and [int]$minor -ge 11) { return $c }
      }
    } catch { }
  }
  return $null
}

$py = Find-Python -Preferred $PythonExe
if (-not $py) {
  Write-Host "Python 3.11+ not found. Installing Python 3.12 with winget..." -ForegroundColor Yellow
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
Write-Host "Installing the framework and its dependencies (a few minutes the first time)..."
& $venvPy -m pip install -e ".[anthropic,mcp,browser,dev]" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not $SkipBrowser) {
  Write-Host "Installing Chromium for Playwright..."
  & $venvPy -m playwright install chromium
  if ($LASTEXITCODE -ne 0) { Write-Host "Chromium install failed; the browser-based agents fall back to static checks. Re-run later with: .venv\Scripts\python -m playwright install chromium" -ForegroundColor Yellow }
}

# the built console ships in git (console/dist) and is used as committed; a git clone gives every file the same
# timestamp, so "sources newer than dist" is no signal here. Rebuild only on request.
if ($RebuildConsole) {
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "-RebuildConsole needs Node.js on PATH" }
  Write-Host "Rebuilding the console from console\src..."
  if (-not (Test-Path "console\node_modules\nunjucks")) { npm install --prefix console nunjucks@3.2.4 --silent }
  & $venvPy console\build.py | Out-Null
} elseif (-not (Test-Path "console\dist\console-local.html")) {
  Write-Host "console\dist is missing: restoring the committed build" -ForegroundColor Yellow
  git checkout -- console/dist 2>$null
}

function New-RandomToken { -join ((1..48) | ForEach-Object { "abcdefghijklmnopqrstuvwxyz0123456789"[(Get-Random -Maximum 36)] }) }
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example"
}
$envText = Get-Content ".env" -Raw
if ($envText -match "(?m)^CAF_API_TOKEN=\s*$") {
  $envText = $envText -replace "(?m)^CAF_API_TOKEN=\s*$", ("CAF_API_TOKEN=" + (New-RandomToken))
  Write-Host "Generated CAF_API_TOKEN (the operator token for the API and the shop admin)"
}
if ($envText -match "(?m)^CAF_SECRET_KEY=\s*$") {
  $envText = $envText -replace "(?m)^CAF_SECRET_KEY=\s*$", ("CAF_SECRET_KEY=" + (New-RandomToken))
  Write-Host "Generated CAF_SECRET_KEY (encrypts customers' model credentials at rest)"
}
if ($envText -notmatch "(?m)^CAF_HOST=") { $envText += "`nCAF_HOST=0.0.0.0`n" }
Set-Content ".env" $envText -NoNewline
New-Item -ItemType Directory -Force -Path "data", "runs" | Out-Null

Write-Host "`nOffline health check of all agents (no model cost)..." -ForegroundColor Cyan
& (Join-Path $root ".venv\Scripts\caf.exe") health
Write-Host "`nInstalled." -ForegroundColor Green
Write-Host @"

Next:
  1. Edit .env: set CAF_LLM_PROVIDER=anthropic_foundry, ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_BASE_URL
     (and CAF_PUBLIC_URL to the address people will use, e.g. https://cxnova2.centralindia.cloudapp.azure.com/agentshop).
  2. .\deploy\windows\check.ps1          live check of all 28 agents on your Foundry deployment
  3. .\deploy\windows\service.ps1        run it as a Windows service (port 8080) and open the firewall
"@
