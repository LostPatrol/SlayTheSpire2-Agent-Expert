<!-- Build and integration guide for the shared Mentor mirror/search service. -->
# Mentor simulation service

The service reuses Spire Mentor's native extractor, Python CombatMirror, Rust rules, full MCTS wrapper and original networks. The MIT source pin is `28429066d91ed05c10b7b57597fc624196d197a4`, targeting game v0.107.1.

The bridge links the original C# extraction source. The Python runtime carries the original `PyCombatState` across completed actions and calibrates it against actual game observations. `mentor_native.pyd` registers the upstream Python module and production search in one library, so searches clone complete objects without losing pending continuations to a serialization round trip.

Honest search blindfolds a clone before priors, filters and previews. Real draw order and RNG may synchronize past events in the truth mirror. The model receives only public results. See [the simulation contract](../../docs/combat-simulation.md) for the information boundary, controls and limitations.

## Build

Run from the project root in PowerShell:

```powershell
.\scripts\setup-mentor.ps1
.\scripts\build-simulation.ps1 -FetchDependencies
```

The build uses project-local Rust 1.99.1, the project Python environment, MSVC and Windows SDK. Keep the existing Cargo lockfiles. The first build downloads their locked dependencies; omit `-FetchDependencies` for later offline builds. Ordered source changes are checked through a temporary Git index; the build does not reset vendor checkouts.

The production package is `local/simulation/mentor-native/`: the extension, selected original Python modules, localization maps, verified models and `source-provenance.json`. To stage a candidate:

```powershell
.\experiments\mentor_simulation\build-native.ps1 -DeploymentDirectory agent/codex/<study>/candidate
```

Run the production protocol using:

```powershell
.\.venv\Scripts\python.exe -m sts2_agent.mentor_runtime --deployment local/simulation/mentor-native
```

Each JSONL `mirror_sync` supplies the current public root, its paired `internal_state.mentor_state`, encounter identity, and optionally one completed command. Search/evaluate/simulate requests identify that exact public root. Recent roots remain available for delayed action audits. Metadata and noncombat PPO use the same native library; PPO receives the package's explicit model directory.

## Code map

- `native/src/lib.rs`: register original Python types and clone them into the host library.
- `host/src/public.rs`: UI coordinate binding and public projections; it does not reconstruct game state.
- `host/src/mcts.rs`, `networks.rs`: original full search wrapper, statistics and network controls.
- `host/src/ppo.rs`: original noncombat policy integration.
- `host/src/alignment.rs`, `validation.rs`: original RunManager and continuous combat validation.
- `src/sts2_agent/mentor_runtime.py`: process lifecycle and original CombatMirror integration.

`build.ps1` retains a standalone Rust CLI for offline validation and synthetic probes. Its combat service accepts complete wire states; the retired public-history importer and finite search planners are no longer available. A Rust-world run is not native-game validation.

Retain Spire Mentor's MIT license and copyright notice when redistributing source or binaries.
