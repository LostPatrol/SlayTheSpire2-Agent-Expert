# Restore the pinned upstream and project-local SDK/Python environment.
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
# Initialize private machine settings without overwriting an existing installation.
foreach ($settings in @('machine', 'bridge.settings')) {
    $settingsPath = Join-Path $projectRoot ("configs/$settings.json")
    if (-not (Test-Path -LiteralPath $settingsPath)) {
        Copy-Item -LiteralPath (Join-Path $projectRoot ("configs/$settings.example.json")) -Destination $settingsPath
        Write-Warning "Created configs/$settings.json; review local paths and profile settings before building or launching."
    }
}
$machine = Get-Content (Join-Path $projectRoot 'configs/machine.json') -Raw | ConvertFrom-Json
$sdkRoot = Join-Path $projectRoot '.tools/dotnet'
if (-not (Test-Path -LiteralPath (Join-Path $sdkRoot 'dotnet.exe'))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $sdkRoot) -Force | Out-Null
    $installer = Join-Path $projectRoot '.tools/dotnet-install.ps1'
    Invoke-WebRequest -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installer
    & $installer -Version '9.0.320' -InstallDir $sdkRoot -NoPath
}
$bridge = Join-Path $projectRoot 'vendor/STS2-Agent'
if (-not (Test-Path -LiteralPath (Join-Path $bridge '.git'))) {
    git clone https://github.com/CharTyr/STS2-Agent.git $bridge
    if ($LASTEXITCODE -ne 0) { throw 'Upstream clone failed.' }
    git -C $bridge checkout --detach $machine.upstream_commit
    if ($LASTEXITCODE -ne 0) { throw 'Pinned upstream commit was not available.' }
    git -C $bridge apply (Join-Path $projectRoot 'patches/bridge-main-public-v1.patch')
    if ($LASTEXITCODE -ne 0) { throw 'Local bridge changes could not be restored.' }
} else {
    $actual = git -C $bridge rev-parse HEAD
    if ($actual -ne $machine.upstream_commit) {
        throw 'Existing vendor checkout differs from the pinned source; preserve and review it first.'
    }
}
Push-Location $projectRoot
try {
    uv sync --locked --extra dev --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Local Python dependency setup failed.' }
} finally { Pop-Location }
Write-Output 'Project environment is ready. Build the bridge with scripts/build-bridge.ps1.'
