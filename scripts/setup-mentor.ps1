# Install the project-local Rust runtime required by the Mentor combat service.
param()
$ErrorActionPreference = 'Stop'
# Keep compiler state and downloads inside the project, without editing the system PATH.
$MentorRoot = Split-Path -Parent $PSScriptRoot
# Match the reviewed upstream toolchain rather than an automatically moving channel.
$MentorToolchain = '1.99.1'
# These homes affect this process and its children only.
$MentorRuntime = Join-Path $MentorRoot '.tools/mentor-rust'
$env:RUSTUP_HOME = Join-Path $MentorRuntime 'rustup'
$env:CARGO_HOME = Join-Path $MentorRuntime 'cargo'
$env:RUSTUP_TOOLCHAIN = $MentorToolchain
# Use the isolated rustup proxy instead of any unrelated global installation.
$MentorCargo = Join-Path $env:CARGO_HOME 'bin/cargo.exe'
if (-not (Test-Path -LiteralPath $MentorCargo)) {
    New-Item -ItemType Directory -Path $MentorRuntime -Force | Out-Null
    # The official installer downloads only the compiler; MSVC/Windows SDK are separate prerequisites.
    $MentorInstaller = Join-Path $MentorRuntime 'rustup-init.exe'
    Invoke-WebRequest -Uri 'https://win.rustup.rs/x86_64' -OutFile $MentorInstaller
    & $MentorInstaller -y --no-modify-path --profile minimal --default-toolchain $MentorToolchain
    if ($LASTEXITCODE -ne 0) { throw 'Project-local Rust installation failed.' }
}
& $MentorCargo --version
if ($LASTEXITCODE -ne 0) { throw 'Project-local Rust is unavailable; check the pinned toolchain installation.' }
Write-Output 'Mentor compiler is ready. Run scripts/build-simulation.ps1 to build the service.'
