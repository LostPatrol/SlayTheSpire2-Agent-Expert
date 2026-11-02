# Restore and verify the ordered upstream sources without resetting a developer checkout.
param([string]$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..')))
$ErrorActionPreference = 'Stop'
# Both native and standalone builds use the same reviewed source series.
$MentorProjectRoot = $ProjectRoot
# This commit is the source audit baseline, rather than the moving upstream default branch.
$MentorCommit = '28429066d91ed05c10b7b57597fc624196d197a4'
# Keep the pinned dependency separate from dated experiment evidence.
$MentorCheckout = Join-Path $MentorProjectRoot 'vendor/spire-mentor'
if (-not (Test-Path -LiteralPath (Join-Path $MentorCheckout 'sts2-rs/Cargo.toml'))) {
    & git clone https://github.com/tfriedel/spire-mentor.git $MentorCheckout
    if ($LASTEXITCODE -ne 0) { throw 'Mentor dependency clone failed.' }
    & git -C $MentorCheckout checkout --detach $MentorCommit
    if ($LASTEXITCODE -ne 0) { throw 'Mentor dependency checkout failed.' }
}
$MentorActualCommit = & git -C $MentorCheckout rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $MentorActualCommit -ne $MentorCommit) { throw 'Mentor checkout differs from the reviewed source pin.' }
# These direct source repairs are reviewed independently and remain individually reproducible.
$MentorRepairs = @('held-potion-availability.patch', 'manual-replay-choice.patch',
    'brilliant-scarf-fifth-hand-play.patch', 'imported-monster-power-registration.patch',
    'observed-current-cost.patch', 'deterministic-rng-domain-tracking.patch',
    'manual-scarf-history.patch', 'pre-draw-turn-boundary.patch', 'scarf-hand-only-cost.patch',
    'scarf-completed-play-counter.patch', 'scarf-non-auto-completed-counter.patch',
    'observed-quote-expiry-and-potion-targets.patch', 'restored-monster-move-history.patch',
    'mcts-exact-rng-and-sound-chance-proofs.patch', 'starter-completed-counters.patch',
    'mcts-fixture-completed-counters.patch', 'public-monster-ai-belief.patch',
    'monster-ai-semantic-hash.patch', 'pet-power-hook-listeners.patch',
    'chance-complete-player-semantics.patch', 'wire-completed-counters-and-quotes.patch',
    'nibbit-slice-public-telegraph.patch', 'py-completed-counters.patch',
    'run-parity-terminal-outcomes.patch', 'mcts-native-legacy88-value-loader.patch',
    'runai-search-component-and-execution-hooks.patch', 'ppo-public-probability-decode.patch',
    'runai-production-combat-entry.patch', 'boss-ancient-native-entry-stage.patch',
    'ppo-darv-ancient-surface.patch', 'perfect-fit-native-priority-belief.patch',
    'held-potion-native-contract-test.patch', 'public-combat-history-recorder.patch',
    'unmovable-empty-bus-history.patch', 'fixture-completed-history-shape.patch',
    'held-potion-content-native-contract-tests.patch', 'observed-quote-nonzero-mask-gates.patch',
    'witnessed-cross-refill-draw-outcomes.patch', 'panache-public-display.patch',
    'ppo-public-row-zero-coordinate.patch', 'witnessed-hatch-hp-outcomes.patch',
    'witnessed-random-orb-outcomes.patch', 'public-play-resource-snapshots.patch',
    'local-creator-generated-card-history.patch', 'osty-attack-command-history.patch',
    'decision-history-search-keys.patch', 'nibbit-slice-ascension-telegraph.patch',
    'corrected-search-identity-and-chance-reuse-tests.patch',
    'witnessed-generated-character-card-outcomes.patch', 'witnessed-random-target-outcomes.patch',
    'feral-live-return-budget-and-finished-zero-quotes.patch',
    'weighted-witnessed-history-root-beliefs.patch',
    'upstream-shared-mirror-runtime.patch', 'live-monster-stable-id.patch',
    'localized-potion-orb-id.patch')

function Test-MentorSourceIndex {
    # Compare reviewed files with a temporary index; never reset a developer's checkout.
    param([string[]]$Paths)
    foreach ($MentorPath in $Paths) {
        if (-not (Test-Path -LiteralPath (Join-Path $MentorCheckout $MentorPath))) { return $false }
        $MentorExpectedBlob = & git -C $MentorCheckout rev-parse (':' + $MentorPath)
        if ($LASTEXITCODE -ne 0) { return $false }
        $MentorActualBlob = & git -C $MentorCheckout hash-object --path $MentorPath $MentorPath
        if ($LASTEXITCODE -ne 0 -or $MentorExpectedBlob -ne $MentorActualBlob) { return $false }
    }
    return $true
}

# Later repairs can edit earlier hunks, so validate the ordered series rather than reverse each patch.
$MentorIndexRoot = Join-Path $MentorProjectRoot '.tools/mentor-build'
New-Item -ItemType Directory -Path $MentorIndexRoot -Force | Out-Null
$MentorTemporaryIndex = Join-Path $MentorIndexRoot ('source-check-' + [guid]::NewGuid().ToString('N') + '.index')
$MentorPreviousIndex = $env:GIT_INDEX_FILE
try {
    $env:GIT_INDEX_FILE = $MentorTemporaryIndex
    & git -C $MentorCheckout read-tree $MentorCommit
    if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare the pinned source index.' }
    foreach ($MentorRepair in $MentorRepairs) {
        & git -C $MentorCheckout apply --cached (Join-Path $PSScriptRoot ('patches/' + $MentorRepair))
        if ($LASTEXITCODE -ne 0) { throw "Reviewed source series failed: $MentorRepair" }
    }
    $MentorReviewedPaths = @(& git -C $MentorCheckout diff --cached --name-only $MentorCommit)
    if (-not (Test-MentorSourceIndex -Paths $MentorReviewedPaths)) {
        # An older reviewed prefix can be upgraded without discarding unrelated local edits.
        & git -C $MentorCheckout read-tree $MentorCommit
        $MentorPrefix = -1
        for ($MentorPosition = 0; $MentorPosition -le $MentorRepairs.Count; $MentorPosition++) {
            if (Test-MentorSourceIndex -Paths $MentorReviewedPaths) { $MentorPrefix = $MentorPosition; break }
            if ($MentorPosition -lt $MentorRepairs.Count) {
                & git -C $MentorCheckout apply --cached (Join-Path $PSScriptRoot ('patches/' + $MentorRepairs[$MentorPosition]))
                if ($LASTEXITCODE -ne 0) { throw 'Cannot reconstruct the reviewed source prefix.' }
            }
        }
        if ($MentorPrefix -lt 0) { throw 'Reviewed source files contain other edits; no checkout files were replaced.' }
        for ($MentorPosition = $MentorPrefix; $MentorPosition -lt $MentorRepairs.Count; $MentorPosition++) {
            $MentorPatch = Join-Path $PSScriptRoot ('patches/' + $MentorRepairs[$MentorPosition])
            & git -C $MentorCheckout apply $MentorPatch
            if ($LASTEXITCODE -ne 0) { throw 'Source upgrade stopped; the existing checkout was preserved.' }
            & git -C $MentorCheckout apply --cached $MentorPatch
            if ($LASTEXITCODE -ne 0) { throw 'Cannot validate the upgraded source.' }
        }
        if (-not (Test-MentorSourceIndex -Paths $MentorReviewedPaths)) { throw 'Upgraded source differs from the reviewed series.' }
    }
} finally {
    $env:GIT_INDEX_FILE = $MentorPreviousIndex
    Remove-Item -LiteralPath $MentorTemporaryIndex -Force -ErrorAction SilentlyContinue
}
