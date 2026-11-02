# Start exactly one isolated experimental client without touching other clients or saves.
param([Parameter(Mandatory=$true)][string]$ArmRoot)
$ErrorActionPreference = 'Stop'
# Resolve the shared launcher and each arm's independent process configuration.
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ResolvedArm = [IO.Path]::GetFullPath($ArmRoot)
$ArmMachine = Get-Content -LiteralPath (Join-Path $ResolvedArm 'configs/machine.json') -Raw | ConvertFrom-Json
$ArmClient = [string]$ArmMachine.client_id
$ArmPort = [int]$ArmMachine.api_port
if ($ArmClient -notmatch '^\d+$' -or $ArmClient -eq '2026102701') { throw 'Use a distinct numeric experiment client.' }
if (Get-NetTCPConnection -State Listen -LocalPort $ArmPort -ErrorAction SilentlyContinue) { throw 'Experiment bridge port already has a listener; inspect it before starting another process.' }
# Never terminate a running game to prepare the other arm.
$MatchingClient = Get-CimInstance Win32_Process -Filter "Name='SlayTheSpire2.exe'" | Where-Object {
    $_.CommandLine -match ('--clientId(?:=|\s+)' + [regex]::Escape($ArmClient) + '(?:\s|$)')
}
if ($MatchingClient) { throw 'This experimental client already has a game process.' }
# Debug commands remain disabled during forward policy evaluation.
$env:STS2_AGENT_SETTINGS_PATH = Join-Path $ResolvedArm 'configs/bridge.settings.json'
$env:STS2_AGENT_ROLE = 'human'
$env:STS2_AGENT_AUTOPLAY = '0'
$env:STS2_ENABLE_DEBUG_ACTIONS = $null
& (Join-Path $ProjectRoot 'vendor/STS2-Agent/scripts/start-game-session.ps1') `
    -ExePath (Join-Path $ArmMachine.game_root 'SlayTheSpire2.exe') `
    -ApiPort $ArmPort -KeepExistingProcesses -Attempts 90 -DelaySeconds 1 `
    -ExtraArguments ('--windowed --force-steam off --clientId ' + $ArmClient)
