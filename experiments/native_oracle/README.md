# Native combat oracle

Offline synthetic combat fixtures executed by the locally owned `sts2.dll`, using the MIT-licensed [sts2-cli](https://github.com/wuhao21/sts2-cli) adapter. This is a validation tool, independent of the live policy controller.

## Prepare

Run from the project directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe experiments/native_oracle/setup.py
.\.venv\Scripts\python.exe experiments/native_oracle/smoke.py
.\.venv\Scripts\python.exe experiments/native_oracle/nested_choice.py
.\.venv\Scripts\python.exe experiments/native_oracle/validate_contract.py --output agent/codex/native-contract-validation.json
```

The setup pins sts2-cli `d11aa883b582dd68bd39b331f3370746b30d447e`, copies the local v0.107.1 dependencies, adds the small native fixture adapter and builds with the project's .NET9 SDK. The inspected upstream default branch targets v0.111.0 and does not compile unchanged against the installed v0.107.1 API. `--game-root` and `--checkout` override local paths. Downloaded source, binaries, stub runtime files and new evidence remain under `agent/codex/priority-fixes-20261029/native/sts2-cli/`; earlier checkouts and their evidence are preserved. Game DLLs must not be distributed.

The installed game DLL is unmodified. Setup records the original/copy hashes in `evidence/provenance.json`. Godot is replaced by the upstream stubs, and existing scheduling/localization Harmony patches remain. Oracle mode disables the upstream Neutralize effect replacement and turns failed/forced potion consumption into an error.

The selector adapter detaches the old pending prompt before completing its task. A native continuation can synchronously open the next prompt; the old completion must preserve that new state. `nested_choice.py` verifies Duplicator followed by Burning Pact requires two explicit selections and settles the remaining effects once.

Synthetic carried relics receive their native public `Owner` before combat entry; this fixes the upstream `set_player` insertion path without replacing any relic hooks. This initialization does not reproduce arbitrary earlier pickup/room history, which fixtures must treat separately.

## Interface

```python
from native_oracle import NativeOracle

with NativeOracle() as oracle:
    initial = oracle.init({
        "hand": ["ARMAMENTS", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"],
        "draw": [], "discard": [], "exhaust": [], "potions": [],
        "energy": 3, "encounter": "NIBBITS_WEAK",
    })
    pending = oracle.step({"type": "play_card", "card_index": 0})
    after = oracle.step({"type": "select_card", "choice_index": 0})
```

Import this directory on the Python path. Card specs may be strings or `{ "card_id": "TRUE_GRIT", "upgraded": true }`. `setup` also accepts synthetic `seed`, `hp`, `max_hp`, `relics`, `potions` and an optional underlying `deck`. Each `init` creates a fresh process and native run in memory. No real save file or live-game snapshot is loaded.

Snapshots contain `public_state`, `endpoint` and `pending_choice`. Schema 2 exports unordered full pile descriptors, current modified energy/star costs, native damage previews, effective keywords, modifiers, public power counters, potion slots, explicit choices, and completed native play history. `endpoint.pile_cards` complements the legacy counts in `endpoint.piles`. Optional unavailable values stay null or absent; they are not replaced with zero. The top-level `pending_choice` is always explicit, including null. Native RNG, actual draw order and internal move identity remain excluded.

`play_card/card_index` is the current hand index; `target_index` is the stable public enemy `i`, translated to the native current index before dispatch. Dead slots are rejected instead of retargeted. `use_potion/option_index` is a stable belt slot; `select_card` accepts `choice_index` or an explicit `choice_indices` array. All four fixture piles are replaced together; omitted piles are empty, and each registered instance is assigned at most once. Optional `deck` supplies native registered instances but does not implicitly fill an omitted pile.

`combat.public_card_play_history` copies completed native plays including automatic and replay plays. Root `public_history` derives total and manual-first-series counts from those records. Legacy player counters remain labeled as manual oracle dispatches; consumers should prefer complete native history. No internal trigger counter is inferred from an absent public event.

For JSONL batches, run `native_oracle.py`; each line is `{ "id": "case", "setup": {...}, "actions": [...] }`, and returns `{ "ok": true, "id": ..., "initial": ..., "steps": [...] }`. Setup or action failures return `ok=false`; do not count them as matches.

## Validation scope

The smoke suite exercises native damage, block, Strength, Armaments/True Grit+/Burning Pact pending choices and recovery, and two potions. The parent parity runner can submit `initial.public_state` to an emulator and compare resulting resources, powers, hand and piles against the native successor.

This is a patched headless adapter around native game rules, not an unconditional substitute for the full game. Snapshots include current visible enemy intents, orbs and pets; exporting them does not prove emulator support. The supplied fixtures focus on the current player turn. Ordinary `step(end_turn)` is rejected. The repaired original upstream handler now fails in oracle mode before cancellation/retry or forced-terminal recovery; `probe_end_turn.py --output <path>` can explore single-dispatch progress, but explicitly reports `end_turn_validated=false`. Successful probes do not establish native draw-transaction completion or true-client equivalence. Arbitrary Power injection, full hidden-history restoration, UI-dependent mechanics, multiplayer and modded rules remain outside the contract. A still-running action executor yields an explicit error; pending selection is distinct.

Random draws require separate deterministic-order or distribution tests; an independently sampled emulator hand need not equal the test engine's randomly drawn hand. Unrecognized IDs, missing native support, failed initialization and unfinished actions must remain separate from numeric mismatches. Some superficially plausible card IDs such as `METALLICIZE` do not exist in the installed v0.107.1 registry.
