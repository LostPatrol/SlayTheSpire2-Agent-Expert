# Attach the dashboard launcher to this arm's explicit native process configuration.
$ErrorActionPreference = 'Stop'
# The preparation manifest locates the shared launcher without assuming a fixed checkout path.
$ArmRoot = Split-Path -Parent $PSScriptRoot
$RuntimeIdentity = Get-Content -LiteralPath (Join-Path $ArmRoot 'runtime-freeze.json') -Raw | ConvertFrom-Json
& (Join-Path $RuntimeIdentity.source_root 'experiments/dual_client/start-client.ps1') -ArmRoot $ArmRoot
