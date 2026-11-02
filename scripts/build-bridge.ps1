# Build and install the pinned local bridge against the installed Steam main build.
param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$machine = Get-Content (Join-Path $projectRoot 'configs/machine.json') -Raw | ConvertFrom-Json
$gameRoot = $machine.game_root
$env:PATH = (Join-Path $projectRoot '.tools/dotnet') + ';' + $env:PATH
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:STS2_DATA_DIR = Join-Path $gameRoot 'data_sts2_windows_x86_64'
$bridge = Join-Path $projectRoot 'vendor/STS2-Agent'
# The bridge links the same pinned Mentor exporter used by the simulation runtime.
. (Join-Path $projectRoot 'experiments/mentor_simulation/prepare-source.ps1') -ProjectRoot $projectRoot
if (-not $SkipInstall -and (Get-Process -Name SlayTheSpire2 -ErrorAction SilentlyContinue)) {
    throw 'Close the game before replacing the bridge files.'
}
if (-not $SkipInstall) {
    $backup = Join-Path $projectRoot ('local/mod-backups/' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    foreach ($file in @('STS2AIAgent.dll', 'STS2AIAgent.pck', 'mod_id.json')) {
        $installed = Join-Path $gameRoot ('mods/' + $file)
        if (Test-Path -LiteralPath $installed) {
            New-Item -ItemType Directory -Path $backup -Force | Out-Null
            Copy-Item -LiteralPath $installed -Destination (Join-Path $backup $file)
        }
    }
}
Push-Location $bridge
try {
    & ./scripts/build-mod.ps1 -Configuration Release -GameRoot $gameRoot -GodotExe (Join-Path $gameRoot 'SlayTheSpire2.exe') -SkipInstall:$SkipInstall
} finally { Pop-Location }
