# Offline game knowledge

The controller reads a frozen Spire Codex snapshot from `knowledge/snapshots/`. Runtime retrieval is fully offline. The dashboard's `/knowledge` page searches this corpus. The model receives all references selected from current public IDs and their explicit rule dependencies; repeated data is shared in the prompt rather than discarding related rules to meet an entry limit.

## Source and version

The source repository is `ptrlrd/spire-codex`, fixed at `2af6543ca7a6c76acfde6be582651c7c05e88e50`. `sources.lock.json` preserves source paths, SHA256 hashes, extraction-build evidence, coverage and license attribution. Original English and Chinese entity JSON, approved mechanics articles, the extraction manifest, README and license are retained under the snapshot's `raw/` directory.

The public stable extraction manifest declares Steam build **23811903**, matching the installed game's Steam manifest and local v0.107.1 baseline. Entity references carry `source_build_matched`; that label means their extraction build matches, and does not independently validate every parser result. Imported knowledge is allowed for retrieval and strategy reference. It is not an exact calculator or action-validation rule source.

Spire Codex uses **PolyForm Noncommercial 1.0.0**. Preserve `raw/LICENSE.md` and its required notice when copying snapshot content. Game content belongs to Mega Crit. The upstream API terms are retained for reference; runtime does not use that hosted API.

## Reproduce the import

Use the project's Python environment in PowerShell:

```powershell
git clone --filter=blob:none --no-checkout https://github.com/ptrlrd/spire-codex.git vendor/spire-codex
.\.venv\Scripts\python.exe -X utf8 scripts/import-knowledge.py
```

The importer reads fixed Git objects, rather than mutable source working-tree files. A source checkout is unnecessary after cloning. It creates a new content-addressed snapshot and writes `knowledge/current.json`. Existing snapshot directories are retained. The provider resolves the pointer once; a running provider never hot-reloads a changed corpus. Persist the selected snapshot ID with the run and use that ID when restoring it.

## Retrieval interface

`src/sts2_agent/knowledge.py` contains `KnowledgeBase(root, snapshot_id=None)`. `metadata()` returns provenance, coverage and status. `lookup(kind, entity_id)` returns one complete imported entity, and `search(query, kind=None, limit=20)` supports the local human browser. Kinds are `cards`, `characters`, `relics`, `potions`, `monsters`, `encounters`, `events`, `powers`, `keywords`, `glossary`, `orbs`, `enchantments`, `afflictions`, `intents`, `acts`, `ascensions`, `rest_site_options` and `mechanics`.

`context_for_model(public_observation)` selects explicit public IDs and approved compact fields, including the bridge's `event.id`, without a prior entry/character cutoff. Event `preview_relic_ids` are resolved as relic references, and explicit “add/gain/obtain/receive … card name” grants in relic descriptions add the named card (for example `JEWELRY_BOX → APOTHEOSIS`), so previewed rewards carry their own rules. Current hand afflictions, enchantments and keywords, active Powers and their dependencies precede full card descriptions, enemy references and background equipment/deck entries. A bounded four-level rule traversal follows explicit source properties and named mechanics, including Power → affliction and affliction/enchantment → keyword. It does not treat a referenced mechanic as currently applied. Ordinary prose words such as Enemy and Deck do not expand into glossary noise. Event choices can also link explicitly named rewards. The compatible `context(..., max_entries=24, max_chars=18000)` remains available for deliberately bounded callers; Provider uses `context_for_model`.

IDs, English names, Chinese names and compact Power suffixes such as `SANDPIT_POWER` resolve to the same source record. Older bridge snapshots can recover explicitly named modifiers from a visible card's `line`; arbitrary observation strings are not searched. The runtime only accepts already-reviewed public observations; it never reads the game, seeds, replay services or unknown future room instances.

Model delivery version 2 also supplies the reviewed Retain, Ethereal and Exhaust keyword definitions during combat, even when ordinary cards mention none of them. The fixed system prompt states normal hand cleanup and its visible exceptions: unplayed ordinary cards are discarded, simulation stop is not real turn end, and a historical plan is not evidence of Retain. These references do not grant keywords to current cards. Whole unversioned mechanics articles remain excluded. Plain `context(..., max_entries=None, max_chars=None)` retains the earlier complete related-entity contract for historical audits; `include_hand_rules=True` enables the baseline references explicitly.

Bounded contexts record `omitted_fact_ids` and `omitted_entries` with `entry_limit`/`character_limit`; model contexts retain the entire selected set. `unresolved_references` separately records recognized public IDs absent from the snapshot, rather than misclassifying them as budget omissions. Provider request metadata records selected fact IDs, omissions and unresolved IDs. Exact outgoing `messages` contain a reversible shared-value representation: expand the latest payload using definitions present in that request's retained user messages to inspect the complete selected rules. Inspect those messages when validating delivery, rather than relying on lookup success or entity counts.

With `provider.model_view="safe"`, each model entry keeps rule content (`name/kind/description/properties/dynamic_values/observed_amounts`) and no longer carries per-entry `source/fact_id/entity_id/verification/admissibility/allowed_uses/conditions`; one top-level `knowledge.scope` carries snapshot, game build/channel, source revision and verification. Full provenance remains in the source snapshot, dashboard and journals. Static enemy state machines describe public possibilities. The model must use public history to determine a transition; missing premises mean uncertainty. Normal/ascension variants remain explicit and are never silently collapsed into one damage value.

## Dynamic values and unresolved data

Power templates can include runtime counters, owner names and conditional phrases. Their static sample descriptions may contain a default count, such as Sandpit's sample two turns. Samples remain available in the human browser; model context includes the original template and observed counter instead. It never substitutes the sample for current state, invents missing values or fills them with zero. Live card text and resource costs take precedence over static base/upgrade references. Card upgrade dictionaries preserve source deltas, while `upgrade_description` provides the resolved upgraded text.

`conflicts.jsonl` records unresolved placeholders and known contradictions. Calcified Cultist's inconsistent `attack_pattern` is omitted from normalized references, while its original JSON remains preserved. Mechanics articles are shared stable/beta prose without independent build evidence; they are available in the human browser as `unversioned_reference` and are not automatically injected into model context. Developer console, AutoSlay and The Deprived articles are excluded. Runs, same-seed lookups, live presence and replay datasets are never imported.

Missing snapshots or a corpus hash mismatch produce `unavailable` plus a visible diagnostic and empty reference context. Base gameplay remains available. Restoration with an unavailable frozen snapshot does not fall back to another corpus. A game update needs a newly reviewed snapshot; do not relabel the existing one as the new version.

## Public observation bridge

The live bridge supplies `power_details` with native tooltip text, names, counters and UI display amounts alongside the compatible compact Power strings. Applied Power text uses native `SmartDescription` when available, then ordinary `Description`; templates or unknown values remain explicit on formatting failure. Ordinary descriptions can contain one-stack defaults (as observed with Rupture), so they must not replace the native applied counter. This path is shared by players, enemies and pets; localized smart text still needs in-game acceptance after deploying a new bridge.

Every visible card surface carries nullable `affliction` and `enchantment` objects with their public IDs, names, native dynamic rule text and amounts; enchantments also retain their display counter. This includes hands, grouped piles/decks, selection, reward, bundle and shop previews. Grouping keys include overlays so cards with different restrictions cannot merge. `unplayable_reason_text` explains `blocked_by_hook` using only its already-visible card overlay, visible combat Power or owned relic; unknown preventers remain unexplained. It does not guess hidden quotas or expose internal type names. All new paths pass the explicit public whitelist and affect observation tokens.

Event prose, card dynamic values, potion occupancy, orb slots, pet Powers and glossary explanations also pass explicit paths in `FairDecisionProtocol.cs`. Unknown draw-pile order remains grouped and sorted. Unrevealed crystal-sphere item geometry is filtered; revealed items and visible board cover coordinates may pass.

`derived_combat_risks` contains limited warnings based on public intents, HP, block, Sandpit/Doom/Constrict/Magic Bomb counters and the native poison preview. The user approved the poison preview as public information. This existing warning is emitted only when the player's current poison preview reaches current HP; it is not a general enemy poison projection. It is approximate reference material, not complete simulated settlement, and an empty warning list does not imply safety. Seeds, internal enemy move IDs and hidden instance values remain excluded.

The installed v0.107.1 `PoisonPower.CalculateTotalDamageNextTurn()` was invoked in an isolated model-only harness through public APIs, without decompilation or changing the running game. Sixty-four combinations covered poison 1/2/5/10, opposing Accelerant 0/1/2/5, both combat sides and Intangible. All matched sequential diminishing ticks: poison 5 with Accelerant 1/2 previews 9/12; ticks stop when poison runs out, and Intangible caps each tick at 1. Six Buffer cases confirmed that this API returns damage **before HP-loss prevention**, not actual HP lost. The public warning states this limitation explicitly and excludes the raw certainty flag. These checks validate the preview in the tested snapshots, not all actual turn-start triggers. Unplayed cards, later poison application, intervening healing, changes to powers, death prevention and complete turn settlement are outside this preview. Application effects such as Snecko Skull are reflected in the observed poison counter after application, rather than added again by the bridge.

The provider pins `knowledge_snapshot_id` in the persistent session, logs the actual per-request reference count and size, and records metadata under the run manifest's `current_knowledge`. Those current settings do not retroactively attribute references to earlier requests. A changed live game version suppresses snapshot entries and clears stale conversational context; it does not stop the base controller.

## Coverage and limits

Runtime compatibility currently checks the live game version. Full build/channel/rules-mod/mode/difficulty filtering and persistent known-top/known-bottom tracking are not implemented. The probability helper is not wired into production decisions. Only Spire Codex has been imported; the additional cross-check sources proposed in the original whitelist guide remain future work.

The initial snapshot contains 1,576 references across 18 categories: 556 cards, 257 Powers, 296 relics, 63 potions, 115 monsters, 87 encounters, 66 events, plus characters, keywords, glossary, orbs, enchantments, afflictions, intents, acts, ascensions, rest-site choices and 24 mechanics articles. English descriptions are used, with Chinese names retained as aliases. Multiplayer-only cards and deprecated/developer character entries are excluded.

The mechanics corpus and strategy prose remain separate. The local read-only browser serves the offline reference need without importing the upstream site's run databases or infrastructure.

## Strategy references and detailed relationships

`scripts/import-strategies.py` reads `data/guides.json` from the same fixed Git revision and imports eight guides into `knowledge/strategies/`: new-player decisions, encounter reading, character builds, and all five character tier guides. It preserves 81 complete level-two sections, nested headings, introductory qualifications, author, publication date, tags, reader difficulty, character, source URL/revision and hashes. Reproduce it with the project Python; no network service is used at runtime. The original PolyForm notice and license under the mechanics snapshot still apply.

`StrategyLibrary.context_for_model(observation)` selects complete relevant sections for reward, shop, map, rest and combat scenes. It does not inject every tier list into every decision. General advice covers skipping, deck size, current weaknesses, path risk, resting/upgrading and using potions; class build summaries apply only to the observed character. `search(query, character=None, limit=10)` and `lookup(section_or_guide_id)` supply optional detailed retrieval. Metadata explicitly marks these articles `unversioned_strategy_reference`: reader difficulty is not ascension, community win-rate samples are not causal predictions, and prose may refer to other patches or multiplayer. Live mechanics take priority and archetypes are suggestions, not obligations.

`KnowledgeBase.lookup_detailed(kind, entity_id, include_related=True, depth=3)` returns a full entry and its public related entities. `search_related` traverses up to four levels. It can expand encounter membership on demand without automatically injecting future Boss move tables. `knowledge/strategies/relations.json` maintains separately attributed reference associations, so frozen entities remain unchanged. The Byrdonis Nest query reaches Byrdonis Egg, the Hatch rest-site interaction, the associated Byrdpip transformation reward, and Byrd Swoop with its actual card text. The egg-to-Byrdpip association uses the fixed Mentor public wiki notes as a separately identified reference; Codex provides the egg/rest-site text and relic/card reward text. These associations do not assert that the player owns a reward or that an event option has already been chosen.
