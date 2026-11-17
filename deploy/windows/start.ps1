<#
.SYNOPSIS
  Starts the CX Nova Agent Shop in the foreground (what the service runs).
  Settings come from .env (CAF_HOST, CAF_PORT, CAF_ROOT_PATH, the model provider, tokens).
#>
param([string]$BindHost = "", [int]$Port = 0)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$caf = Join-Path $root ".venv\Scripts\caf.exe"
if (-not (Test-Path $caf)) { throw "Not installed: run .\deploy\windows\install.ps1 first" }
$args = @("serve")
if ($BindHost) { $args += @("--host", $BindHost) }
if ($Port -gt 0) { $args += @("--port", "$Port") }
& $caf @args
