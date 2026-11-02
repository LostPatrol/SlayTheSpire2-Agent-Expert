# Public policy evaluation

This experiment freezes candidate identities, collects real public decision roots, audits shorter histories, and runs explicitly bounded same-model effort pilots. It never calls the game bridge, loads a save, or executes a game action.

Run with the project Python from the repository root:

```powershell
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py freeze --config configs/a0-simulation.json --out agent/codex/my-study/freeze.json
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py verify --input agent/codex/my-study/freeze.json --out agent/codex/my-study/drift.json
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py collect --run runs/<run-id> --count 60 --out agent/codex/my-study/corpus.json
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py summarize --run runs/<run-id> --out agent/codex/my-study/run-summary.json
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py window --run runs/<run-id> --out agent/codex/my-study/windows.json
```

Each output path must be new. `freeze` hashes current decision sources, prompt/tools, deployed host and installed bridge/game files. An optional `--run` includes historical process manifests and observed model identities separately. This is a disk snapshot, not proof that the live process loaded those files. A complete unlock pool is explicitly unknown unless a future public export supplies it; no save is read. Use `verify` to report drift for experiment grouping, not as an ordinary gameplay admission gate.

`collect` balances screens and floors and preserves source line/request IDs. Accepted actions are the production public legality contract: multiple actions may be acceptable. These labels do **not** score tactical optimality. The optional `--witnessed <review-probes.json>` adds the reviewed strict death-versus-block sentinel and labels its simulator-only evidence explicitly. It does not fabricate a native full-turn oracle. New strategic cases should carry independently checked outcomes and an acceptable action set; model self-grading is insufficient.

`window` explores a rolling suffix of complete prior turns within each original scene boundary, rebuilds every reference, and checks current expanded facts for equality. The character budget applies after reintroducing dropped shared definitions. All tool reasoning and tool-call/result pairs in retained turns remain complete. This is an alternative history ablation, not an exact replay of production's full-history reset at its budget limit. Results are character-based adjacent-prefix opportunities, not API cache hits, token costs, or quality evidence.

To make real API requests, use an explicit limit after reviewing the corpus and freezing the candidate:

```powershell
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py live --corpus agent/codex/my-study/corpus.json --count 12 --request-limit 24 --out agent/codex/my-study/pilot.json
.\.venv\Scripts\python.exe experiments/policy_evaluation/benchmark.py pilot-summary --input agent/codex/my-study/pilot.json --out agent/codex/my-study/pilot-summary.json
```

The runner alternates low/high order, uses the configured model with thinking enabled, disables tools, retries and format repair, and records full public request inputs, returned decisions, usage and model fingerprints. A per-request JSONL journal preserves completed attempts if interrupted. The total request cap is 1–40; the default is zero and cannot make calls. Legality and strict tactical sentinel results are summarized separately. A small single-turn, tools-disabled pilot cannot establish production win-rate non-inferiority or justify changing production defaults.

`summarize` keeps native victory flags, assistance, version freezing and technical/budget stops separate. Repeated error events are not independent interruption counts. Uniform current off-peak prices are an explicit repricing assumption, never the historical bill. API generation and request timing overlap and must not be added together.
