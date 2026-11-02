# Dual-client high/low comparison

`prepare.py` freezes two independent controller roots using the existing CLI `--root`. It does not launch a game, call a model, read credentials, or copy save progress. The main profile and its controller ledger remain separate.

Run from the project directory after the intended Python/Mentor build is complete:

```powershell
.\.venv\Scripts\python.exe experiments/dual_client/prepare.py --destination local/experiments/dual-live-20261029
```

The destination may already contain setup evidence, but `high/`, `low/` and `experiment.json` must not exist. Preparation preserves existing setup files and never overwrites prior arms. Defaults are high: client `2026102901`, bridge `18865`, dashboard `18866`; low: client `2026102902`, bridge `18965`, dashboard `18966`. Override these with the corresponding CLI flags if needed. The operator must separately verify that the client IDs are unused and copy the authorized fully unlocked progress before native launch.

Each arm receives copied Python/web sources, configuration/schema, the active knowledge snapshot and strategies, Mentor executable/provenance and relevant source records. Its `local/`, `runs/`, controller/dashboard locks and simulation budget files are separate. Both use the project's Python environment; generated entrypoints insert the frozen arm source first so the environment's editable install cannot import the main package. Do not update that shared environment during the experiment.

The policy configurations differ only in `provider.reasoning_effort` (`high`/`low`) and the process-specific bridge URL. API credentials are referenced by absolute original filename and never copied. `experiment.json` records roots and ports; each arm's `runtime-freeze.json` records copied artifact hashes. Preparation validates actual package import, schema, knowledge/strategy loading, dashboard assets and Mentor artifact identity without starting a session or host.

Start dashboards with each arm's `scripts/start-dashboard.ps1`. Both start paused. The default arm `scripts/start-game.ps1` deliberately reports that native launch belongs to the external experiment launcher. Once the correct native client is healthy, the existing dashboard game-preparation flow can attach normally. An optional `--game-launcher` copies an explicitly supplied root-aware PowerShell launcher into both arms.

Keep game-build, mods, card/relic unlock pool, source/executable hashes and all other policy settings equal. Concurrent sessions can share account-level API capacity; compare request latency separately from reasoning length. A single pair is functional acceptance and an exploratory comparison, not a statistically reliable win-rate conclusion.

The current authorized experiment uses a native `unlock all` baseline copied to both independent clients; the main profile remains unlocked. Runtime updates must stop only paused/settled controllers, preserve their ledgers and spending, and record new source hashes in a separate `runtime-updates/` segment. Keep the original freeze and source evidence. The dashboard's starting-policy controls can select the character/difficulty before embark; both comparison arms currently use Ironclad/A0. A previously persisted A1 is adjusted by ordinary guarded actions before embark.

`monitor.py` reads explicit public controller roots and journals without issuing provider/game calls. Use distinct output paths for initial errors and repaired segments; actual tool compression requires request/response evidence, and untriggered features stay unverified.
