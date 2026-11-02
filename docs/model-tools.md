# Model tools

`model_tools.py` is the central read-only tool registry. `Provider.configure_tools()` binds the combat simulator, a callback returning this run's complete public memory events, the strategy library, and the controller's request-budget check. `Provider.decide()` binds each call to its current public observation. Tools cannot execute real game actions or select a replacement action.

| Tool | Data source |
|---|---|
| `simulation_evaluate` | `CombatSimulator.evaluate`: declared fixed candidate plans and existing episode simulation allowance |
| `history_search` | Same-run experienced public events, newest first, complete records with pagination |
| `wiki_lookup` | Frozen `KnowledgeBase.lookup_detailed`, including explicit related rules and rewards |
| `wiki_search` | Frozen wiki search; incompatible game versions remain unavailable |
| `strategy_search` | Complete attributed `StrategyLibrary` sections |

To add a tool, add its function schema and description in `TOOL_SPECS`, dispatch it in `ModelTools._dispatch`, and verify the public-data boundary with an offline test. The provider serially answers model-generated tool calls, then requests one final action JSON. At most six tool rounds and eight calls per batch are accepted. Every HTTP attempt, including tool continuations and retries, passes the ordinary cumulative request/time limits. Simulation paths use the simulator's existing persistent allowance.

The provider emits `tool_call_started`, `tool_call_completed` and `tool_call_failed` with `tool_call_id`, name, complete arguments, result/error, timing and decision correlation. The session records these events for the dashboard and replay. A failed tool returns a visible error to the model; it does not silently replace the model's action.

Repeated Wiki/strategy queries may reuse an exact result only within the same decision and public root, with `cache_hit` recorded. Simulation calls retain their budget effects, and history searches remain fresh. The static schema validators and frozen knowledge dependencies are cached independently of tool results.

`provider.compact_simulation_tool_results` enables tool-local sharing (omission means false). Daily `a0-simulation.json` enables it with `simulation_tool_compression_mode=static_and_states`. Mode `static` shares only repeated static modeling descriptions/card text at reviewed evaluator paths, keeping full live state inline. Mode `static_and_states` additionally shares complete, exactly equal sample states of at least 1024 characters. Different HP, costs, handles or modifiers produce distinct states; card IDs alone never merge instances. All definitions are complete original values with no generated nested references, using the existing `public-shared-values-v1` wrapper; every lookup resolves in this message alone. Sample metrics, statuses, failures and stop reasons remain inline in both modes. Encoding is used only when it saves at least 512 characters and 2% including overhead. Complete raw results remain in logs, and the profile version/mode bind cached-proposal identity. Reversibility does not prove identical model decisions.

DeepSeek's [thinking-mode protocol](https://api-docs.deepseek.com/guides/thinking_mode/) requires complete `reasoning_content` on all retained assistant messages when tools are enabled. Tool mode therefore retains and resends that protocol field, including final-answer turns; it is not independently inserted as a fact or strategy. The previous no-tools path still omits reasoning from subsequent requests. Context reset discards tool messages, reasoning, public definitions and retained-event signatures together. An unfinished SSE stream never dispatches a tool.

Strategy balance is part of the fixed decision prompt: assess current/near-term threats before comparing growth and Boss needs, compare the immediate opportunity cost of preserved resources, and keep `build_plan` separate from the scene-limited `scene_plan`. Their lifecycle is managed by the session, not by tool dispatch.

The session persists only the build summary across rooms. Scene tactics bind to the observed act, floor and room screen; card-selection/view/modal overlays remain in their originating room. A room change clears the tactic and provider conversation together, preserving public facts, map, budgets and logs. An omitted plan field keeps its value; an explicit empty string clears it. Unscoped legacy mixed plans are not revived on upgrade.

`automatic.py` handles public housekeeping one action at a time, using the same token, pause and receipt gate as model decisions. It opens card rewards but never chooses a card, and delegates potion collection when an empty slot has a known relic tradeoff. The room's accepted `scene_actions` distinguishes first shop entry from a closed inventory, preventing reopen loops. Set `automatic.enabled=false` to disable it.
