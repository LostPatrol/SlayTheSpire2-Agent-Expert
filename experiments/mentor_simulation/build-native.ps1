# Build and package the shared Mentor extension, original mirror modules, and verified models.
param(
    [string]$DeploymentDirectory = 'local/simulation/mentor-native',
    # Populate the local Cargo cache without changing the reviewed lockfile.
    [switch]$FetchDependencies
)
$ErrorActionPreference = 'Stop'
# Resolve every tool and artifact inside the project.
$MentorNativeProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
# Reuse the same isolated compiler and incremental cache as the JSONL host.
$MentorNativeRustRoot = Join-Path $MentorNativeProjectRoot '.tools/mentor-rust'
$MentorNativeCargo = Join-Path $MentorNativeRustRoot 'cargo/bin/cargo.exe'
$MentorNativePython = Join-Path $MentorNativeProjectRoot '.venv/Scripts/python.exe'
$MentorNativeManifest = Join-Path $PSScriptRoot 'native/Cargo.toml'
$MentorNativeLock = Join-Path $PSScriptRoot 'native/Cargo.lock'
if (-not (Test-Path -LiteralPath $MentorNativeCargo)) { throw 'Install the project-local Rust runtime first.' }
if (-not (Test-Path -LiteralPath $MentorNativePython)) { throw 'Create the project Python environment first.' }
# Restore and audit exactly the same ordered source series as the standalone validator.
. (Join-Path $PSScriptRoot 'prepare-source.ps1') -ProjectRoot $MentorNativeProjectRoot
$env:RUSTUP_HOME = Join-Path $MentorNativeRustRoot 'rustup'
$env:CARGO_HOME = Join-Path $MentorNativeRustRoot 'cargo'
$env:RUSTUP_TOOLCHAIN = '1.99.1'
$env:CARGO_TARGET_DIR = Join-Path $MentorNativeProjectRoot '.tools/mentor-build'
$env:PYO3_PYTHON = $MentorNativePython
& (Join-Path $MentorNativeProjectRoot 'scripts/set-rust-build-parallelism.ps1')
# Seed the independent lock with production versions; a fresh resolution can select newer Tract dependencies.
$MentorNativeBuildArguments = @('build', '--offline', '--release', '--manifest-path', $MentorNativeManifest)
if (-not (Test-Path -LiteralPath $MentorNativeLock)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'host/Cargo.lock') -Destination $MentorNativeLock
} else {
    $MentorNativeBuildArguments += '--locked'
}
if ($FetchDependencies) {
    & $MentorNativeCargo fetch --locked --manifest-path $MentorNativeManifest
    if ($LASTEXITCODE -ne 0) { throw 'Locked Mentor dependency download failed.' }
}
# Bind the compiled extension and copied Python modules to the same stable source snapshot.
$MentorNativeRuntimeScript = Join-Path $PSScriptRoot 'stage_mentor_runtime.py'
$MentorNativeRuntimeInputs = & $MentorNativePython $MentorNativeRuntimeScript --source $MentorCheckout --list-inputs | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot inventory the original Mentor runtime modules.' }
$MentorNativeInputPaths = @((Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'host/src') -Filter '*.rs' -Recurse).FullName)
$MentorNativeInputPaths += @((Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'native/src') -Filter '*.rs' -Recurse).FullName)
$MentorNativeInputPaths += @($MentorNativeManifest, $MentorNativeLock,
    (Join-Path $PSScriptRoot 'host/Cargo.toml'), (Join-Path $PSScriptRoot 'host/Cargo.lock'),
    $MentorNativeRuntimeScript, (Join-Path $PSScriptRoot 'stage_network_models.py'),
    (Join-Path $MentorNativeProjectRoot 'src/sts2_agent/mentor_runtime.py'),
    (Join-Path $MentorNativeProjectRoot 'src/sts2_agent/simulation.py'))
$MentorNativeInputPaths += @($MentorReviewedPaths | ForEach-Object { Join-Path $MentorCheckout $_ })
$MentorNativeInputPaths += @($MentorNativeRuntimeInputs)
$MentorNativeInputsBefore = @($MentorNativeInputPaths | Sort-Object -Unique | ForEach-Object {
    [ordered]@{ path = [IO.Path]::GetRelativePath($MentorNativeProjectRoot, $_); sha256 = (Get-FileHash -LiteralPath $_).Hash }
})
& $MentorNativeCargo @MentorNativeBuildArguments
if ($LASTEXITCODE -ne 0) { throw 'Mentor native extension build failed.' }
$MentorNativeInputsAfter = @($MentorNativeInputPaths | Sort-Object -Unique | ForEach-Object {
    [ordered]@{ path = [IO.Path]::GetRelativePath($MentorNativeProjectRoot, $_); sha256 = (Get-FileHash -LiteralPath $_).Hash }
})
if (($MentorNativeInputsBefore | ConvertTo-Json -Compress) -ne ($MentorNativeInputsAfter | ConvertTo-Json -Compress)) {
    throw 'Build inputs changed during compilation; rerun after source edits finish. Deployment was preserved.'
}
# An explicit destination lets integration checks finish before product deployment.
$MentorNativeDeployment = if ([IO.Path]::IsPathRooted($DeploymentDirectory)) {
    [IO.Path]::GetFullPath($DeploymentDirectory)
} else {
    [IO.Path]::GetFullPath((Join-Path $MentorNativeProjectRoot $DeploymentDirectory))
}
New-Item -ItemType Directory -Path $MentorNativeDeployment -Force | Out-Null
$MentorNativeBinary = Join-Path $env:CARGO_TARGET_DIR 'release/mentor_native.dll'
Copy-Item -LiteralPath $MentorNativeBinary -Destination (Join-Path $MentorNativeDeployment 'mentor_native.pyd') -Force
Copy-Item -LiteralPath (Join-Path $MentorNativeProjectRoot 'vendor/spire-mentor/LICENSE') -Destination (Join-Path $MentorNativeDeployment 'LICENSE.spire-mentor') -Force
# PPO needs an explicit package-relative model directory because its host executable is Python.
$MentorNativeAssets = & $MentorNativePython (Join-Path $PSScriptRoot 'stage_network_models.py') --source $MentorCheckout --destination (Join-Path $MentorNativeDeployment 'models') | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Network asset verification or staging failed.' }
$MentorNativeRuntime = & $MentorNativePython $MentorNativeRuntimeScript --source $MentorCheckout --destination $MentorNativeDeployment | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Original Mentor runtime staging or import validation failed.' }
$MentorNativeMetadata = $MentorNativeRuntime.native_metadata
$MentorNativeProvenance = [ordered]@{
    upstream_commit = $MentorActualCommit
    backend = 'mentor'
    game_version = $MentorNativeMetadata.game_version
    protocol_version = $MentorNativeMetadata.protocol_version
    rule_snapshot = $MentorNativeMetadata.rule_snapshot
    extension_sha256 = (Get-FileHash -LiteralPath $MentorNativeBinary).Hash
    native_cargo_lock_sha256 = (Get-FileHash -LiteralPath $MentorNativeLock).Hash
    source_inputs = $MentorNativeInputsBefore
    source_patches = @($MentorRepairs | ForEach-Object {
        @{ path = ('experiments/mentor_simulation/patches/' + $_); sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot ('patches/' + $_))).Hash }
    })
    runtime = $MentorNativeRuntime
    network_assets = $MentorNativeAssets
}
$MentorNativeProvenance | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $MentorNativeDeployment 'source-provenance.json') -Encoding utf8
Write-Output ('Mentor native extension: ' + (Join-Path $MentorNativeDeployment 'mentor_native.pyd'))
