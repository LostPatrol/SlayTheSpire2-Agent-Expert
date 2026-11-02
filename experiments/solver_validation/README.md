# Solver validation

These tools separate search regression, continuous simulated combat, native rule checks, and whole-run policy evaluation. A simulator playing against itself is not evidence that its rules match the game. Combat outcomes are not whole-run win rates.

Run from the repository root with the project Python. Use a newly built executable explicitly; the runner records its SHA256 and writes every attempted cell before publishing its summary.

```powershell
.venv/Scripts/python.exe experiments/solver_validation/benchmark.py --exe <mentor-simulation.exe> --output agent/codex/<new-study> --seeds 719 1223 --budgets 128 1000
```

`combat_trial` keeps one hypothetical `CombatState` alive across draws, enemy turns, selections and subsequent player decisions. Each MCTS decision uses a fresh public-information search and applies only its first action to that world. Random and one-step HCE baselines share the same world seeds. The HCE baseline scrambles hidden information independently before evaluating successors. All arms retain terminal wins/losses, decision/deadline cutoffs, illegal-action failures, actual rounds, rollouts and depth caps separately. The denominator includes failures and cutoffs. A small panel is diagnostic, not a statistical strength claim.

The synthetic corpus includes five characters with base/upgraded cards, repeated draw, multiple enemies, potions, exhaust choices and a long scaling fight. Unsupported cases remain in the results. These are synthetic public roots; they are not native reachable-state certificates.

`uncertainty.py --exe <host> --oracle-checkout <prepared-native-oracle> --output <new-study>` freezes the host and reuses `combat_simulation/uncertainty_validation.py`. It enumerates 33 native synthetic hidden orders across four draw families and compares independently sampled public-prefix outcomes, with exact binomial diagnostics. Every requested sample must finish; rejected samples make the family incomplete. Passing this finite uniform-mixture check does not establish arbitrary real-run conditional odds.

The upstream MCTS gates require the project-local Rust toolchain and `cargo-nextest` under `.tools/solver-validation/nextest`:

```powershell
powershell -ExecutionPolicy Bypass -File experiments/solver_validation/run-nextest.ps1 -Output agent/codex/<new-study>/nextest.log
```

The gates cover known lethal answers, convergence, terminal propagation, potion actions, hidden draw/RNG invariance, chance nodes, terminal HP bookkeeping and rollout cutoffs. The old exact tree snapshot in `final_hp_does_not_change_the_search` may detect deliberate rule/search changes; retain and investigate the failure instead of silently updating its expected numbers.

To build an isolated upstream native headless harness, provide an existing local directory containing the owned v0.107.1 DLLs and `localization_eng`. The original DLLs are copied and hashed without IL modification. No saves are loaded.

```powershell
.venv/Scripts/python.exe experiments/solver_validation/stage_headless.py --source-lib <local-native-lib> --output agent/codex/<new-native-stage>
.venv/Scripts/python.exe experiments/solver_validation/native_probe.py --assembly agent/codex/<new-native-stage>/src/Sts2Headless/bin/v0.107.1/Debug/net9.0/Sts2Headless.dll --output agent/codex/<new-native-probe> --turns 3
```

Strict mode disables upstream's `Neutralize.OnPlay` implementation replacement and rejects a stalled `end_turn` before cancellation/retry. Godot stubs, scheduling and localization patches remain. The probe records every request/response, round progress and forced-recovery markers. It deliberately reports `end_turn_validated=false`: successful multi-turn progress alone does not establish full-state equivalence or true-client behavior.

`native_differential.py --source <probe-directory> --exe <host> --output <new-directory>` replays each recorded native trajectory as one continuous core world. It explicitly restores only the recorded synthetic oracle's private RNG, ordered draw and starting enemy move into an internal-read diagnostic arm. The public projection removes those fields and pile indices. Every intermediate HP/block/energy/round, pile count, hand and enemy state is compared; unavailable fields and incomplete successors remain explicit. This is a partial comparison contract, not full-state equivalence.

`stage_headless.py --osty-fixture` adds a narrow setup-only fixture for an existing summoned Osty's HP and Strength. `osty_probe.py` summons through two native Bodyguard plays, verifies the fixture readback and measures upgraded Unleash damage. It grants Strength through native `PowerCmd`; attack implementation and hooks remain native.

For broader native differential checks, reuse the upstream `parity_fuzz`, `parity_audit` and `run_parity` tools with an actually built `sts2_rs` extension and the strict native harness. Preserve their first divergences and missing comparison fields. Whole-run Rust `run_arena` panels require their own pinned policy/budget/seed identities and must be labelled simulator outcomes. Position books and 50k-simulation “oracle” searches are fallible search references, not native truth.

The ordered source repairs correct `run_parity` terminal outcomes in both live and cached paths. A `game_over` phase never implies a win. Explicit win flags take precedence; death can establish a loss, and an unknown outcome remains a harness error instead of a clean terminal match. This preserves useful partial field comparisons without inventing terminal equivalence.

`batch_diagnostics.py` lets the original upstream policy drive a full run while checking public imports, support and meta action mapping at each observed root. Adapter failures are collected without stopping the driver. It retains the first complete public fixture per exact error and encounter, indexes every occurrence, and groups handle-independent symptoms separately from confirmed root causes. Diagnostic combat checks omit full public search; driver wins are not public-policy wins.

The runner defaults to logical processors minus two workers and expands its own CPU affinity. `--jobs` overrides concurrency; Windows CPU time, working set and wall time are measured separately. Use a new output directory for each batch. `--replay-from <diagnostic-output>` rechecks every saved public fixture against a new executable with input hashes preserved; this checks import/meta availability. `--mode public` runs the independent strict production loop, retaining every unavailable action, halt and timeout. The collected host stage timings overlap and must not be summed as batch wall time.

Fixture `kind` selects the original import or meta endpoint. Legacy requests without it infer the endpoint from the combat object; an ambiguous meta fixture needs a separately recorded derived request with its original kind, preserving the archived request and projection.

```powershell
.venv/Scripts/python.exe experiments/solver_validation/batch_diagnostics.py --host <host.exe> --output agent/codex/<new-diagnostic-panel> --jobs 14 --full-unlock
```

`replay_frontiers.py --host <host> --sources <frontier-json> <frontier-json> --output <new-dir>` checks literal public roots through production `search`. Each requested source gets its own ordinal, source version and SHA in the output name, so identically named failures from different runs cannot overwrite one another. The input is copied unchanged. Its declared smaller search budget checks root admission; it does not reproduce the original full-run outcome or original search statistics.

The network alignment runner freezes its executable, verifies all five exact champion ONNX hashes against `champions.toml`, clears inherited `STS2*` policy overrides, and retains every timeout/error/nonterminal cell. All modes keep one persistent `RunManager`. `raw` uses the original `MctsCombatPolicy<PpoPolicy>` wrapper and observations; `audit` also records reachable public imports. `public` calls the actual production `search` and `policy_evaluate` endpoints, then maps their selected native actions to current legal engine edges. An unavailable import or unmappable choice stops that cell. No private state is substituted after an import failure.

```powershell
.venv/Scripts/python.exe experiments/solver_validation/validation.py run --host <host.exe> --models vendor/spire-mentor/sts2-pycli/data/ppo --output agent/codex/<new-network-panel> --modes raw,public --profile corrected --seeds 1458832210 --jobs 2
```

For the raw wrapper, `--profile pristine` means its original construction in the **current patched vendor**, without embedding hooks; it does not claim untouched upstream source. `corrected` applies the production search components, terminal-rollout/exact-RNG behavior and quote-aware cache settings through explicit wrapper hooks, preserving original counters, forced moves and event vetoes. The public mode uses production root decisions and public event guards. Its offline comparison supplies the known benchmark seed and decision counter explicitly; live production does not receive the actual hidden run seed. Record source hashes when comparing either arm with the independently cloned clean pin. The public projection excludes all RNG cursors, unrevealed draw order, undisplayed accumulators and unwitnessed history; import gaps and structural observation differences remain visible.

The stopping limits default to the original full-run helper's 100000 advanced steps and 256 unchanged-state iterations, with a strict 600-second process timeout. Requested simulations and actual rollouts are separate; a forced legal action spends zero rollouts. Trace actions include a terminal action even when the original helper's advanced-step counter excludes it.

`compare_full_runs.py --current <panel-dir> --reference-panels <panel-dir> --stage-profile native_ui --output <new-dir>` verifies the frozen program, champion bytes, requested primary controls and observed stage flags before pairing every role/seed/ascension. It keeps incomplete pairs and both original outcomes. Historical arena JSONL may instead be supplied with `--reference-jsonl` and its `--legacy-control-evidence`, including the original invocation with the executable and five champion hashes, for `served` only. Public priors and original full-state observations can differ; equal seeds and budgets do not make repaired engines or every decision equivalent.

Public combat histories retain completed Touch of Insanity, Heirloom Hammer and observed Boost Away sources, their selection surfaces and public successors. The simulator stages its full public hand with unknown eligibility; this does not certify a native UI holder's visibility. Previously seen opaque card IDs may reappear in unordered pile groups; unseen cards receive no identity. The importer replays the original rules and verifies the successor before restoring card effects. Until-played effects require the same observed instance's completed-play history for expiry. A zero displayed price alone cannot establish a permanent discount, especially when a global cost modifier masks a temporary local discount.

The public run arm enables the existing Core event recorder before combat creation. It exports actual completed manual, automatic and replay iterations, with block gains joined to those iterations; unfinished or missing joins make the summary incomplete. Raw reference runs keep recording disabled, and MCTS clones discard recorders. The native roster removes ordinary dead enemies and renumbers current slots, but retains a Reattach corpse. The simulator's actor translates those public slots to its internal slots when applying actions and writing witnesses.

Draw-order certificates use ordered bands with unordered members, separate random insertions and witnessed discard chronology. Keep the actual C# observer's output separate from the simulator's public-rule knowledge export. Neither certificate may copy a hidden physical draw order. An unrepresentable conditional order is a nonterminal belief cutoff; its count and reason are reported separately from depth caps and wins/losses.

`--stage-profile served` retains original arena TestMode reward grants and defaults potion swapping off. `--stage-profile native_ui` disables direct reward grants and enables interactive potion swapping; this is the default for public mode. These profiles are different experimental conditions. Compare each with a reference configured for the same flags. Public reward continuations preserve complete PPO row traces and expand only explicitly selected declines into legal engine edges. Core potion replacement is atomic: its virtual discard-followup frame tests the consumer contract but does not prove a captured native intermediate UI transition. Native commands, internal engine edges and macro scope are recorded separately.

`prepare_reference_probe.py --reference <clean-pinned-clone> --output <new-project>` creates an independent diagnostic executable without editing the reference tree. Build it with the same release profile as the host (opt-level 2, LTO off, 16 codegen units). The executable reuses the actual pristine encoder and ONNX inference. Compare selected reachable boundaries with:

```powershell
.venv/Scripts/python.exe experiments/solver_validation/validation.py diagnostics --host <host.exe> --reference-probe <reference-probe.exe> --models vendor/spire-mentor/sts2-pycli/data/ppo --runs <network-panel> --output agent/codex/<new-diagnostics> --per-surface 1
```

For a clean upstream whole-run comparator under native-UI reward flags, prepare `prepare_reference_native_ui.py --reference <clean-pinned-clone> --output <new-project>` and build that independent Cargo project with the same release profile and a separate target directory. Run `run_reference_native_ui.py --host <reference-exe> --reference <clean-pinned-clone> --build-project <new-project> --output <new-panel> --seeds 1458832210 1458832211 1458832212 1458832213 1458832214`. It reuses the original complete policy and FullRun driver, with direct reward grants disabled and interactive potion swapping enabled. It freezes the executable, verifies clean source/model identities and preserves every real process timeout. Original advanced steps and recorded decisions are available; the shared production `action_count` key is an absent-field placeholder for this reference.

Diagnostics compare candidate order, exact encoder arrays, active logits/probabilities/value and grouped decoding. Full observation structure is reported separately because upstream wire and engine observations already have unused optional-field differences. The per-surface selection rule and every failed comparison are saved. These checks establish an explicit adapter boundary contract, not native game correctness or a native whole-run win rate.

For explicitly declared hidden Neow outcomes, add `--uncertainty-worlds 32 --full-unlock` to a simulator panel. This assumes the original single-player full-unlock catalogue and an independent public prior. Every sampled world stays in the response; diagnostics independently execute each world's ONNX payload and verify the complete probability/value mean. This checks neural inference and averaging. Independent reconstruction of the counterfactual rules is a separate gate.

`validation.py contracts --host <host> --models <champions-dir> --fixtures <CSharp-contracts-dir> --output <new-dir>` consumes fixtures serialized by the actual production C# DTO/compact pipeline. Add `--scenes map-complete shop-complete` to select explicitly named `contract-*.json` cases. Every input is copied with its producer metadata and SHA. These are typed synthetic contract objects, not native gameplay captures; malformed or incomplete fixture contexts remain failures. The checked-in producer reward fixtures also exercise real PPO choices through the production macro planner and legal Core reward edges. The full-belt fixture explicitly controls button readiness; its follow-up frame is virtual and compares inventory and the remaining relic queue only.

`validation.py components --host <host> --reference-perf <pristine-perf-probe> --fixture <wire-json> --output <new-dir>` compares five characters in three room contexts using original rollout features/logits, the gated move prior, wide features and TexelWide values. The wire matrix tests component arithmetic; it does not establish search-trajectory or engine-rule equivalence.
