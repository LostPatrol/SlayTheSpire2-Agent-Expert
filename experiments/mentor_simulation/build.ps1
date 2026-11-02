# Build and deploy the isolated Mentor service without accessing the installed game.
param(
    [string]$DeploymentDirectory = 'local/simulation/mentor'
)
$ErrorActionPreference = 'Stop'
# Resolve all tools and output relative to this project, without changing system settings.
$MentorProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
# Rust was installed with --no-modify-path and project-local homes.
$MentorRustRoot = Join-Path $MentorProjectRoot '.tools/mentor-rust'
$MentorCargo = Join-Path $MentorRustRoot 'cargo/bin/cargo.exe'
if (-not (Test-Path -LiteralPath $MentorCargo)) { throw 'Install the project-local Rust 1.99.1 runtime described in README.md.' }
. (Join-Path $PSScriptRoot 'prepare-source.ps1') -ProjectRoot $MentorProjectRoot
$env:RUSTUP_HOME = Join-Path $MentorRustRoot 'rustup'
$env:CARGO_HOME = Join-Path $MentorRustRoot 'cargo'
$env:RUSTUP_TOOLCHAIN = '1.99.1'
$env:CARGO_TARGET_DIR = Join-Path $MentorProjectRoot '.tools/mentor-build'
& (Join-Path $MentorProjectRoot 'scripts/set-rust-build-parallelism.ps1')
# Bind provenance to one stable source revision even when parallel tasks share this checkout.
$MentorBuildInputs = @((Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'host/src') -Filter '*.rs').FullName)
$MentorBuildInputs += @((Join-Path $PSScriptRoot 'host/Cargo.toml'), (Join-Path $PSScriptRoot 'host/Cargo.lock'))
$MentorBuildInputs += @($MentorReviewedPaths | ForEach-Object { Join-Path $MentorCheckout $_ })
$MentorSourcesBefore = @($MentorBuildInputs | Sort-Object -Unique | ForEach-Object { (Get-FileHash -LiteralPath $_).Hash }) | ConvertTo-Json -Compress
& $MentorCargo build --locked --release --manifest-path (Join-Path $PSScriptRoot 'host/Cargo.toml')
if ($LASTEXITCODE -ne 0) { throw 'Mentor service build failed.' }
$MentorSourcesAfter = @($MentorBuildInputs | Sort-Object -Unique | ForEach-Object { (Get-FileHash -LiteralPath $_).Hash }) | ConvertTo-Json -Compress
if ($MentorSourcesBefore -ne $MentorSourcesAfter) { throw 'Build inputs changed during compilation; rerun after source edits finish. Deployment was preserved.' }
# An explicit staging directory lets validation finish before replacing the active service.
$MentorDeployment = if ([IO.Path]::IsPathRooted($DeploymentDirectory)) {
    [IO.Path]::GetFullPath($DeploymentDirectory)
} else {
    [IO.Path]::GetFullPath((Join-Path $MentorProjectRoot $DeploymentDirectory))
}
New-Item -ItemType Directory -Path $MentorDeployment -Force | Out-Null
# Verify and stage trained assets before replacing the service executable.
$MentorPython = Join-Path $MentorProjectRoot '.venv/Scripts/python.exe'
$MentorAssetJson = & $MentorPython (Join-Path $PSScriptRoot 'stage_network_models.py') --source $MentorCheckout --destination (Join-Path $MentorDeployment 'models')
if ($LASTEXITCODE -ne 0) { throw 'Network asset verification or staging failed.' }
$MentorAssets = $MentorAssetJson | ConvertFrom-Json
$MentorExecutable = Join-Path $env:CARGO_TARGET_DIR 'release/mentor-simulation.exe'
Copy-Item -LiteralPath $MentorExecutable -Destination (Join-Path $MentorDeployment 'mentor-simulation.exe') -Force
Copy-Item -LiteralPath (Join-Path $MentorCheckout 'LICENSE') -Destination (Join-Path $MentorDeployment 'LICENSE.spire-mentor') -Force
# Ask the compiled service for its actual rule identity before recording deployment provenance.
$MentorMetadata = '{"op":"metadata"}' | & $MentorExecutable | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $MentorMetadata.ok -or -not $MentorMetadata.rule_snapshot) {
    throw 'Compiled Mentor service did not report its rule identity.'
}
# Bind the actual modified source and compiled executable, not only the upstream commit.
$MentorRecordedSourcePaths = @($MentorReviewedPaths) + @(& git -C $MentorCheckout diff --name-only HEAD)
$MentorProvenance = @{
    upstream_commit = $MentorActualCommit
    backend = 'mentor'
    game_version = 'v0.107.1'
    protocol_version = $MentorMetadata.protocol_version
    rule_snapshot = $MentorMetadata.rule_snapshot
    source_hashes = $MentorMetadata.source_hashes
    network_assets = $MentorAssets
    source_patch = 'experiments/mentor_simulation/patches/held-potion-availability.patch'
    source_patch_sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'patches/held-potion-availability.patch')).Hash
    modified_core_sha256 = (Get-FileHash -LiteralPath (Join-Path $MentorCheckout 'sts2-rs/crates/sts2-core/src/lib.rs')).Hash
    executable_sha256 = (Get-FileHash -LiteralPath $MentorExecutable).Hash
    cargo_lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'host/Cargo.lock')).Hash
    host_sources = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'host/src') -Filter '*.rs' | Sort-Object Name | ForEach-Object {
        @{ path = $_.Name; sha256 = (Get-FileHash -LiteralPath $_.FullName).Hash }
    })
    source_patches = @($MentorRepairs | ForEach-Object {
        @{ path = ('experiments/mentor_simulation/patches/' + $_); sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot ('patches/' + $_))).Hash }
    })
    # Reviewed patches may introduce new files that the checkout has not added to its normal index.
    modified_core_files = @($MentorRecordedSourcePaths | Sort-Object -Unique | ForEach-Object {
        @{ path = $_; sha256 = (Get-FileHash -LiteralPath (Join-Path $MentorCheckout $_)).Hash }
    })
}
$MentorProvenance | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $env:CARGO_TARGET_DIR 'source-provenance.json') -Encoding utf8
Copy-Item -LiteralPath (Join-Path $env:CARGO_TARGET_DIR 'source-provenance.json') -Destination (Join-Path $MentorDeployment 'source-provenance.json') -Force
Write-Output ('Mentor service deployed: ' + (Join-Path $MentorDeployment 'mentor-simulation.exe'))
