# Run the pinned upstream MCTS regression gates with only project-local toolchains.
param(
    [string]$Output = 'agent/codex/solver-validation-20261029/nextest-final.log',
    [string]$TargetDirectory = '.tools/solver-validation-mcts-unoptimized'
)
$ErrorActionPreference = 'Stop'
# Resolve toolchain state locally without changing persistent machine settings.
$ValidationRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$env:RUSTUP_HOME = Join-Path $ValidationRoot '.tools/mentor-rust/rustup'
$env:CARGO_HOME = Join-Path $ValidationRoot '.tools/mentor-rust/cargo'
$env:RUSTUP_TOOLCHAIN = '1.99.1'
$env:CARGO_TARGET_DIR = Join-Path $ValidationRoot $TargetDirectory
& (Join-Path $ValidationRoot 'scripts/set-rust-build-parallelism.ps1')
$env:PATH = (Join-Path $ValidationRoot '.tools/mentor-rust/cargo/bin') + ';' +
    (Join-Path $ValidationRoot '.tools/solver-validation/nextest') + ';' + $env:PATH
Push-Location $ValidationRoot
try {
    # Windows PowerShell 5 treats normal cargo stderr progress as nonterminating errors.
    $ErrorActionPreference = 'Continue'
    & (Join-Path $ValidationRoot '.tools/mentor-rust/cargo/bin/cargo.exe') nextest run --offline --locked `
        --config 'profile.dev.package.sts2-core.opt-level=0' `
        --config 'profile.dev.package.sts2-content.opt-level=0' `
        --config 'profile.dev.package.sts2-mcts.opt-level=0' `
        --manifest-path vendor/spire-mentor/sts2-rs/Cargo.toml -p sts2-mcts `
        --test toy_convergence --test root_blindfold --test draw_chance --test rollout_stats `
        --test final_hp --test potion_charge --test solver_lite --test suicide_q_collapse_0ljme `
        --test lethal_detector_integration --test intent_aware_scenarios 2>&1 |
        ForEach-Object { $_.ToString() } |
        Tee-Object -FilePath $Output
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) { throw "MCTS regression failed: exit $LASTEXITCODE" }
} finally { Pop-Location }
