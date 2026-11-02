# Build the public combat simulator without touching the installed game or bridge.
param(
    [ValidateSet('mentor', 'evangambit', 'all')]
    [string]$Backend = 'mentor',
    # Download locked crates on the first build; later builds remain offline.
    [switch]$FetchDependencies
)
$ErrorActionPreference = 'Stop'
# Resolve the bundled SDK and managed host from the project directory.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
# Use the project SDK instead of relying on a global installation.
$DotnetPath = Join-Path $ProjectRoot '.tools/dotnet/dotnet.exe'
# Keep the public simulator in the same isolated host used by offline checks.
$HostProject = Join-Path $ProjectRoot 'experiments/combat_simulation/host/CombatSimulation.Host.csproj'
if ($Backend -in @('evangambit', 'all')) {
    & $DotnetPath build $HostProject -c Release --nologo
    if ($LASTEXITCODE -ne 0) { throw 'Legacy combat simulation build failed.' }
}
if ($Backend -in @('mentor', 'all')) {
    & (Join-Path $ProjectRoot 'experiments/mentor_simulation/build-native.ps1') -FetchDependencies:$FetchDependencies
}
