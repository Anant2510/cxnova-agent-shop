<#
.SYNOPSIS
  Starts the CX Nova Agent Orchestration Layer in the foreground (what the service runs).
  Settings come from .env (CAF_HOST, CAF_PORT, CAF_ROOT_PATH, the model provider, tokens).
#>
param([string]$BindHost = "", [int]$Port = 0)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$env:PYTHONUTF8 = "1"   # UTF-8 everywhere: reports carry curly quotes and em dashes
$caf = Join-Path $root ".venv\Scripts\caf.exe"
if (-not (Test-Path $caf)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }
$args = @("serve")
if ($BindHost) { $args += @("--host", $BindHost) }
if ($Port -gt 0) { $args += @("--port", "$Port") }
& $caf @args
