"""Central read-only model tools over public experience, knowledge and hypothetical simulation."""

from collections import Counter
from copy import deepcopy
import json
import time
from datetime import datetime, timezone
from hashlib import sha256

from jsonschema import Draft202012Validator


# A model can refine a decision without making the local tool loop unbounded.
MAX_TOOL_ROUNDS = 6
# One batch cannot bypass the decision's finite local work allowance.
MAX_TOOL_CALLS_PER_ROUND = 8
# History pages are explicit, so complete events never need silent truncation.
MAX_HISTORY_PAGE = 30


def _object(properties: dict, required: tuple = ()) -> dict:
    """Build the common non-strict function parameter contract."""
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


# Keep declarations next to dispatch; adding a tool requires one reviewable registry entry.
TOOL_SPECS = {
    "simulation_evaluate": (
        "Compare model-proposed action prefixes in shared hypothetical worlds, optionally continuing combat rollouts. "
        "No real actions are executed. Use current card_index/card_id or simulator card_handle, "
        "target_index, potion_index, and select_card choice_index. rollout defaults true: apply the declared "
        "prefix literally then search combat continuations including potions and later turns. rollout=false "
        "requests the explicit legacy fixed-plan current-turn audit (4 plans,12 samples,8 actions). "
        "Configured rollout_policy, action_prior, value_model and search_profile apply to continuations; "
        "a learned scalar value evaluates leaves and is not an action policy. "
        "Inspect coverage, cutoffs and unfinished samples; adaptive MCTS values are not live win probabilities. "
        "Every plan action needs a type from play_card, use_potion, select_card, select_deck_card, "
        "confirm_selection, skip_selection or end_turn; stop_projection is not a simulated action.",
        _object({"candidate_plans": {"type": "array", "minItems": 1, "maxItems": 64,
            "items": _object({"id": {"type": "string"}, "actions": {"type": "array", "maxItems": 64,
                "items": {"type": "object", "required": ["type"],
                    "properties": {"type": {"type": "string", "enum": [
                        "play_card", "use_potion", "select_card", "select_deck_card",
                        "confirm_selection", "skip_selection", "end_turn"]}}}}},
                ("id", "actions"))},
            "samples": {"type": "integer", "minimum": 1, "maximum": 256},
            "rollout": {"type": "boolean"}}, ("candidate_plans",))),
    "history_search": (
        "Search this run's already experienced public events, including older events outside chat history. "
        "Returns complete matching events with stable indices and pagination, never hidden state or other runs.",
        _object({"query": {"type": "string"}, "floor": {"type": "integer"},
            "screen": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HISTORY_PAGE}})),
    "wiki_lookup": (
        "Read one full frozen wiki entity and its explicit related rules/rewards. "
        "Use kind such as cards, monsters, events, relics, powers or potions and an ID/name. "
        "Linked outcomes are possibilities, not confirmation they occurred.",
        _object({"kind": {"type": "string"}, "entity_id": {"type": "string"},
            "include_related": {"type": "boolean"}}, ("kind", "entity_id"))),
    "wiki_search": (
        "Find frozen public wiki entities by ID, English/Chinese name or description; "
        "use wiki_lookup for full related details.",
        _object({"query": {"type": "string"}, "kind": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ("query",))),
    "strategy_search": (
        "Find complete attributed strategy guide sections about card choices, skipping, deck size, "
        "potions, routes or an enemy. Advice is author opinion and must fit current state/version.",
        _object({"query": {"type": "string"}, "character": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ("query",))),
}
# Only immutable reference queries may bypass repeated local work, never simulation budgets.
CACHEABLE_QUERIES = frozenset({"wiki_lookup", "wiki_search", "strategy_search"})
# Compile fixed schemas once rather than once per tool invocation.
TOOL_VALIDATORS = {name: Draft202012Validator(spec[1]) for name, spec in TOOL_SPECS.items()}
# These declarations are immutable; callers receive isolated copies.
TOOL_DEFINITIONS = [{"type": "function", "function": {
    "name": name, "description": description, "parameters": schema}}
    for name, (description, schema) in TOOL_SPECS.items()]
# Only these outcome numbers decide between candidate prefixes; the rest stay in the local audit.
SIMULATION_TOOL_SUMMARY_PROFILE = "candidate-outcomes-v1"
SIMULATION_METRIC_FIELDS = (
    "mean", "mean_stderr", "value_variance", "p_win", "p_loss", "p_unresolved",
    "mean_final_hp", "final_hp_sd", "prefix_worlds", "resolved_hp_samples",
    "resolved_probability_mass", "cutoffs", "depth_cap_hits", "estimates_complete",
)
# These sentences explain what the numbers mean and are hoisted once for the whole reply.
SIMULATION_METRIC_MEANINGS = ("meaning", "probability_meaning", "final_hp_meaning", "counts_meaning")
# Search coverage tells the model how far the evaluation actually ran.
SIMULATION_SEARCH_FIELDS = ("simulations_completed", "simulations_requested", "rollout_paths", "cutoffs")
# One illustrative endpoint per candidate reports resources without replaying any other sample.
SIMULATION_ENDPOINT_PLAYER_FIELDS = ("hp", "block", "energy", "stars", "powers")
SIMULATION_ENDPOINT_ENEMY_FIELDS = ("i", "enemy_id", "hp", "block", "powers", "alive")
# Only reasons this short describe a boundary; longer free text is local diagnostic material.
MAX_TOOL_STOP_REASON_CHARACTERS = 80
# Multi-megabyte audits belong in the journal; the ledger keeps at most this much per tool result.
TOOL_STATE_RESULT_CHARACTERS = 32_000


def _canonical(value: object) -> str:
    """Produce a stable cache identity without changing any public values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _simulation_endpoint(samples: list) -> dict | None:
    """Report the resources of one illustrative successor without replaying any other sample."""
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        state = sample.get("state")
        if not isinstance(state, dict):
            continue
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        enemies = state.get("enemies") if isinstance(state.get("enemies"), list) else []
        return {
            "meaning": ("after_hypothetical_end_turn_resolution" if state.get("end_turn_executed")
                        else "after_supported_card_plays_before_turn_end_or_enemy_actions"),
            "horizon": state.get("horizon", "current_player_turn_before_end_turn_effects"),
            "sample_status": sample.get("status"),
            "player": {key: deepcopy(player[key]) for key in SIMULATION_ENDPOINT_PLAYER_FIELDS if key in player},
            "enemies": [{key: deepcopy(enemy[key]) for key in SIMULATION_ENDPOINT_ENEMY_FIELDS if key in enemy}
                        for enemy in enemies if isinstance(enemy, dict)],
        }
    return None


def _simulation_stop_reasons(samples: list) -> dict:
    """Count why sampled worlds stopped; a boundary label is public, free text is not."""
    counts = Counter()
    for sample in samples:
        reason = sample.get("stop_reason") if isinstance(sample, dict) else None
        if not isinstance(reason, str) or not reason:
            continue
        label = reason.split(":", 1)[0].strip()[:MAX_TOOL_STOP_REASON_CHARACTERS]
        if label:
            counts[label] += 1
    return dict(counts)


def _simulation_tool_summary(result: dict) -> dict:
    """Keep candidate outcomes and coverage; drop every per-sample internal state."""
    audit = result if isinstance(result, dict) else {}
    native = audit.get("result") if isinstance(audit.get("result"), dict) else {}
    summary = {"status": audit.get("status"), "backend": audit.get("backend"),
               "rule_snapshot": audit.get("rule_snapshot"), "scope": native.get("scope"),
               "assumptions": native.get("assumptions") or [],
               "diagnostics": audit.get("diagnostics") or []}
    if audit.get("notice"):
        summary["notice"] = audit["notice"]
    search = {key: native.get("mcts", {}).get(key) for key in SIMULATION_SEARCH_FIELDS
              if isinstance(native.get("mcts"), dict) and isinstance(native["mcts"].get(key), int)}
    if search:
        summary["search"] = search
    meanings = {}
    candidates = []
    for candidate in native.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
        for key in SIMULATION_METRIC_MEANINGS:
            if isinstance(metrics.get(key), str) and key not in meanings:
                meanings[key] = metrics[key]
        samples = candidate.get("samples") if isinstance(candidate.get("samples"), list) else []
        entry = {"id": candidate.get("id"), "status": candidate.get("status"),
                 "sample_count": candidate.get("sample_count"),
                 "completed_samples": candidate.get("completed_samples"),
                 "metrics": {key: metrics[key] for key in SIMULATION_METRIC_FIELDS if key in metrics},
                 "stop_reasons": _simulation_stop_reasons(samples)}
        endpoint = _simulation_endpoint(samples)
        if endpoint is not None:
            entry["endpoint"] = endpoint
        candidates.append(entry)
    if candidates:
        summary["candidates"] = candidates
    if meanings:
        summary["metrics_meaning"] = meanings
    return summary


def bounded_tool_result(result: object) -> object:
    """Keep a compact, still-readable result for the durable ledger and the dashboard."""
    if not isinstance(result, dict):
        return result
    raw = _canonical(result)
    if len(raw) <= TOOL_STATE_RESULT_CHARACTERS:
        return result
    summary = _simulation_tool_summary(result)
    summary["truncated"] = True
    summary["result_characters"] = len(raw)
    return summary


def tool_result_content(name: str, result: dict, *, compact_simulation: bool = False) -> str:
    """Return only what a decision needs: candidate outcomes, coverage and one endpoint."""
    raw = _canonical(result)
    if name != "simulation_evaluate" or not compact_simulation:
        return raw
    encoded = _canonical(_simulation_tool_summary(result))
    # A short failure or an already-small audit is clearer verbatim than summarized.
    return encoded if len(encoded) < len(raw) else raw


class ModelTools:
    """Bind reusable services while each invocation receives the current public decision root."""

    def __init__(self, knowledge, *, simulator=None, history=None, strategies=None, event_sink=None):
        """Keep services local; no credentials, filesystem paths or executable tools are exposed."""
        self.knowledge = knowledge
        self.simulator = simulator
        self.history = history
        self.strategies = strategies
        self.event_sink = event_sink
        self.knowledge_status = knowledge.status
        self._query_scope = None
        self._query_cache = {}

    def clear_query_cache(self) -> None:
        """Discard reference memoization at decision and conversation boundaries."""
        self._query_scope = None
        self._query_cache.clear()

    def definitions(self) -> list[dict]:
        """Return the centralized OpenAI-compatible function declarations."""
        return deepcopy(TOOL_DEFINITIONS)

    def _query_key(self, name: str, arguments: dict, observation: dict, context: dict) -> str | None:
        """Bind immutable reference reads to the exact root, decision, and loaded corpora."""
        if name not in CACHEABLE_QUERIES:
            return None
        manifest = getattr(self.strategies, "manifest", {})
        scope = _canonical({"root": observation, "decision": {key: context.get(key) for key in (
            "run_id", "decision_id", "decision_episode_id", "decision_epoch")},
            "knowledge": [getattr(self.knowledge, "snapshot_id", None), self.knowledge_status],
            "strategy": [manifest.get("source_revision"), manifest.get("corpus_sha256")]})
        digest = sha256(scope.encode("utf-8")).hexdigest()
        if digest != self._query_scope:
            self._query_scope = digest
            self._query_cache.clear()
        return _canonical([name, arguments])

    def call(self, call: dict, observation: dict, memory: dict, context: dict) -> dict:
        """Validate, execute and journal one read-only tool without replacing the final decision."""
        function = call.get("function", {})
        name = function.get("name", "")
        raw = function.get("arguments", "")
        started = time.perf_counter()
        event = {**context, "tool_call_id": call["id"], "name": name, "arguments": raw,
                 "status": "running", "started_utc": datetime.now(timezone.utc).isoformat()}
        try:
            arguments = json.loads(raw)
            event["arguments"] = arguments
        except (ValueError, TypeError):
            arguments = None
        self._emit("tool_call_started", event)
        try:
            if name not in TOOL_SPECS:
                raise ValueError("Unknown model tool. Game actions belong only in the final action JSON; "
                                 "available read-only tools: " + ", ".join(TOOL_SPECS) + ".")
            if not isinstance(arguments, dict) or not TOOL_VALIDATORS[name].is_valid(arguments):
                raise ValueError("Arguments do not match this tool's schema.")
            cache_key = self._query_key(name, arguments, observation, context)
            cached = cache_key is not None and cache_key in self._query_cache
            if cached:
                result = deepcopy(self._query_cache[cache_key])
            else:
                result = self._dispatch(name, arguments, observation, memory)
            # Serializability is required by both the protocol and the durable event stream.
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            if cache_key is not None and not cached and result.get("ok") is not False:
                self._query_cache[cache_key] = deepcopy(result)
            event["cache_hit"] = cached
        except Exception as error:
            # Local service failures are recoverable information; they never trigger a real action.
            result = {"ok": False, "error": str(error)[:1000], "tool": name}
            event.update({"status": "failed", "error": result["error"], "result": result})
            kind = "tool_call_failed"
        else:
            event.update({"status": "completed", "result": result})
            kind = "tool_call_completed"
        event["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self._emit(kind, event)
        return result

    def _emit(self, kind: str, event: dict) -> None:
        """Publish complete tool evidence through the provider's existing journal hook."""
        if self.event_sink:
            self.event_sink(kind, deepcopy(event))

    def _dispatch(self, name: str, args: dict, observation: dict, memory: dict) -> dict:
        """Route only the reviewed public tool set to its bound implementation."""
        if name.startswith("wiki_") and self.knowledge_status != "ready":
            return {"ok": False, "status": self.knowledge_status,
                    "error": "The frozen wiki is unavailable or incompatible with this live game."}
        if name == "simulation_evaluate":
            if self.simulator is None:
                return {"ok": False, "error": "Simulation service is unavailable."}
            return self.simulator.evaluate(observation, args["candidate_plans"], samples=args.get("samples", 6),
                                           rollout=args.get("rollout", True))
        if name == "history_search":
            events = self.history() if callable(self.history) else self.history
            return self._history_search(events if events is not None else memory.get("events", []), args)
        if name == "wiki_lookup":
            if hasattr(self.knowledge, "lookup_detailed"):
                return self.knowledge.lookup_detailed(args["kind"], args["entity_id"],
                    include_related=args.get("include_related", True))
            return {"entry": self.knowledge.lookup(args["kind"], args["entity_id"])}
        if name == "wiki_search":
            return {"entries": self.knowledge.search(args["query"], kind=args.get("kind"), limit=args.get("limit", 10))}
        if name == "strategy_search":
            if self.strategies is None:
                return {"ok": False, "error": "Strategy library is unavailable."}
            return {"entries": self.strategies.search(args["query"], character=args.get("character"),
                                                       limit=args.get("limit", 5))}
        raise ValueError("Unknown model tool.")

    @staticmethod
    def _history_search(events: list, args: dict) -> dict:
        """Search complete public event records with stable newest-first result indices."""
        query = args.get("query", "").casefold()
        matches = []
        for index, event in reversed(list(enumerate(events))):
            facts = event.get("facts", {}) if isinstance(event, dict) else {}
            if "floor" in args and facts.get("run", {}).get("floor") != args["floor"]:
                continue
            if "screen" in args and str(facts.get("screen", "")).upper() != args["screen"].upper():
                continue
            if query and query not in json.dumps(event, ensure_ascii=False).casefold():
                continue
            matches.append({"event_index": index, "event": deepcopy(event)})
        offset, limit = args.get("offset", 0), args.get("limit", 10)
        page = matches[offset:offset + limit]
        return {"source": "experienced_public_events_this_run", "total_matches": len(matches),
                "events": page, "next_offset": offset + limit if offset + limit < len(matches) else None}
