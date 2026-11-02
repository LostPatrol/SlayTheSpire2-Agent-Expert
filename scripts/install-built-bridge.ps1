# Install the locally built bridge before a new game launch, retaining the previous files.
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$GameRoot
)
$ErrorActionPreference = 'Stop'
if (-not $GameRoot) {
    $BridgeMachine = Get-Content -LiteralPath (Join-Path $ProjectRoot 'configs/machine.json') -Raw | ConvertFrom-Json
    $GameRoot = $BridgeMachine.game_root
}
# Installation is deferred while the actual target executable is still loaded.
$BridgeGameExecutable = [IO.Path]::GetFullPath((Join-Path $GameRoot 'SlayTheSpire2.exe'))
if (Get-Process -Name SlayTheSpire2 -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $BridgeGameExecutable }) {
    throw 'Close the running game before installing the built bridge.'
}
# These three files form the existing mod package; validate all of them before copying.
$BridgeFiles = @('STS2AIAgent.dll', 'STS2AIAgent.pck', 'mod_id.json')
$BridgeStage = Join-Path $ProjectRoot 'vendor/STS2-Agent/build/mods/STS2AIAgent'
$BridgeInstalled = Join-Path $GameRoot 'mods'
foreach ($BridgeFile in $BridgeFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $BridgeStage $BridgeFile))) {
        throw 'Built bridge package is incomplete. Run scripts/build-bridge.ps1 -SkipInstall first.'
    }
}
$BridgeDifferent = @($BridgeFiles | Where-Object {
    $BridgeTarget = Join-Path $BridgeInstalled $_
    -not (Test-Path -LiteralPath $BridgeTarget) -or
        (Get-FileHash -LiteralPath $BridgeTarget).Hash -ne (Get-FileHash -LiteralPath (Join-Path $BridgeStage $_)).Hash
})
if ($BridgeDifferent.Count -eq 0) { Write-Output 'The installed bridge matches the built package.'; return }
# Keep an exact recoverable copy, without changing saves, settings, client identity or unlocks.
$BridgeBackup = Join-Path $ProjectRoot ('local/mod-backups/' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $BridgeBackup -Force | Out-Null
New-Item -ItemType Directory -Path $BridgeInstalled -Force | Out-Null
foreach ($BridgeFile in $BridgeFiles) {
    $BridgeTarget = Join-Path $BridgeInstalled $BridgeFile
    if (Test-Path -LiteralPath $BridgeTarget) { Copy-Item -LiteralPath $BridgeTarget -Destination (Join-Path $BridgeBackup $BridgeFile) }
}
foreach ($BridgeFile in $BridgeFiles) {
    Copy-Item -LiteralPath (Join-Path $BridgeStage $BridgeFile) -Destination (Join-Path $BridgeInstalled $BridgeFile) -Force
}
Write-Output ('Installed the built bridge; previous package: ' + $BridgeBackup)
