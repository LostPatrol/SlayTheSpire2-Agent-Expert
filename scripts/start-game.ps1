# Launch the persistent offline save used across runs so unlock progress accumulates.
param([string]$ClientId)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$machine = Get-Content (Join-Path $projectRoot 'configs/machine.json') -Raw | ConvertFrom-Json
# Keep the configured client across runs; change it only when explicitly requested.
if (-not $ClientId) { $ClientId = $machine.client_id }
if ($ClientId -notmatch '^\d+$') { throw 'ClientId must contain only digits.' }
if (Get-Process -Name SlayTheSpire2 -ErrorAction SilentlyContinue) {
    throw 'A game is already running. Close it before starting the configured client.'
}
# Refuse to load a profile that lost the authorized full-unlock progress; the guard restores it.
& (Join-Path $projectRoot '.venv/Scripts/python.exe') -m sts2_agent.cli --root $projectRoot --config configs/a0-simulation.json ensure-save --client-id $ClientId
if ($LASTEXITCODE -ne 0) {
    throw 'Full-unlock save check failed. Game launch stopped.'
}
# Apply a prepared bridge update only between game processes, before loading any save.
if (Test-Path -LiteralPath (Join-Path $projectRoot 'vendor/STS2-Agent/build/mods/STS2AIAgent/STS2AIAgent.dll')) {
    & (Join-Path $PSScriptRoot 'install-built-bridge.ps1') -ProjectRoot $projectRoot -GameRoot $machine.game_root
}
$env:STS2_AGENT_SETTINGS_PATH = Join-Path $projectRoot 'configs/bridge.settings.json'
$env:STS2_ENABLE_DEBUG_ACTIONS = $null
# Seed the native main-branch schema; the upstream fallback uses beta schema v8.
$testSettings = Join-Path $env:APPDATA ('SlayTheSpire2/default/' + $ClientId + '/settings.save')
if (Test-Path -LiteralPath $testSettings) {
    $settings = Get-Content -LiteralPath $testSettings -Raw | ConvertFrom-Json -AsHashtable
} else {
    $settings = @{ schema_version = 5; mod_settings = @{ mods_enabled = $true }; vsync = 'adaptive' }
}
$settings.fullscreen = $false
$settings.window_size = @{ X = 1280; Y = 720 }
$settings.limit_fps_in_background = $false
$settings.mod_settings.mods_enabled = $true
# Preserve user choices and enable installed agent/animation helpers without replacing the list.
$modList = @($settings.mod_settings.mod_list | Where-Object { $null -ne $_ })
foreach ($modId in @('STS2AIAgent', 'ModConfig', 'SpeedX')) {
    $installed = $modId -eq 'STS2AIAgent' -or (Test-Path -LiteralPath (Join-Path $machine.game_root "mods/$modId/$modId.dll"))
    if (-not $installed) { continue }
    $existing = @($modList | Where-Object { $_.id -eq $modId })
    if ($existing.Count) {
        foreach ($entry in $existing) { $entry.is_enabled = $true }
    } else {
        $modList += @{ id = $modId; is_enabled = $true; source = 'mods_directory' }
    }
}
$settings.mod_settings.mod_list = $modList
New-Item -ItemType Directory -Path (Split-Path -Parent $testSettings) -Force | Out-Null
$settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $testSettings -Encoding utf8NoBOM
& (Join-Path $projectRoot 'vendor/STS2-Agent/scripts/start-game-session.ps1') `
    -ExePath (Join-Path $machine.game_root 'SlayTheSpire2.exe') `
    -ApiPort $machine.api_port -KeepExistingProcesses -Attempts 90 -DelaySeconds 1 `
    -ExtraArguments ("--windowed --force-steam off --clientId " + $ClientId)
