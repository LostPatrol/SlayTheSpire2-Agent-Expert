"""Exercise real public tool dispatch and native thinking/tool protocol without paid requests."""

from copy import deepcopy
import json

import pytest

from sts2_agent.model_tools import ModelTools, tool_result_content
from sts2_agent.provider import ProviderError
from sts2_agent.prompt_context import serialize
from test_provider import DECISION, ROOT, build, completion
from test_provider_stream_cache import frame, stream
from sts2_agent.knowledge import KnowledgeBase


def tool_stream(name="history_search", arguments='{"query":"old fact"}', *, done=True):
    """Split function arguments across actual SSE chunks to verify streaming assembly."""
    return stream([
        frame({"reasoning_content": "Need earlier public experience."}),
        frame({"tool_calls": [{"index": 0, "id": "call_old", "type": "function",
            "function": {"name": name, "arguments": arguments[:8]}}]}),
        frame({"tool_calls": [{"index": 0, "function": {"arguments": arguments[8:]}}]}),
        frame(finish="tool_calls"),
    ], done=done)


def test_streamed_tool_round_replays_required_reasoning_and_complete_result(monkeypatch):
    """Tool thinking is protocol state; every actual API subrequest consumes budget."""
    provider, requests = build(monkeypatch, [tool_stream(), completion(serialize(DECISION)),
                                            completion(serialize(DECISION))])
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    facts = [{"source": "experienced_action", "facts": {"run": {"floor": 2}, "note": "old fact"}}]
    provider.configure_tools(history=lambda: facts)
    provider.event_context = {"decision_id": "decision", "run_id": "same-run"}
    assert provider.decide({}, {}) == DECISION
    assert provider.requests_used == 2
    followup = requests[1]
    assert "tools" in followup and "response_format" not in followup
    assert followup["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"query":"old fact"}'
    assert followup["messages"][-2]["reasoning_content"] == "Need earlier public experience."
    result = json.loads(followup["messages"][-1]["content"])
    assert result["events"][0]["event"] == facts[0]
    completed = next(payload for kind, payload in events if kind == "tool_call_completed")
    assert completed["tool_call_id"] == "call_old"
    assert completed["arguments"] == {"query": "old fact"}
    assert completed["result"] == result and completed["status"] == "completed"
    assert provider.decide({"state": {"turn": 2}}, {}) == DECISION
    assert provider.requests_used == 3
    assert provider._history_turns == 2
    assert all("reasoning_content" in message for message in requests[2]["messages"]
               if message["role"] == "assistant")
    provider.reset_context("scene_changed")
    assert provider._history == [] and provider._history_turns == 0 and provider._definitions == {}


def test_disabled_thinking_tool_round_omits_reasoning_state(monkeypatch):
    """Replay of reasoning state is needed only while thinking mode is enabled."""
    provider, requests = build(monkeypatch, [tool_stream(), completion(serialize(DECISION))],
                               config_patch={"thinking": "disabled"})
    provider.configure_tools(history=[])
    provider.event_context = {"decision_id": "decision", "run_id": "same-run"}
    assert provider.decide({}, {}) == DECISION
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in requests[0]
    assert "reasoning_content" not in json.dumps(requests[1]["messages"])


def test_incomplete_tool_stream_never_executes_the_partial_call(monkeypatch):
    """A fragmented response is retried as transport work, never dispatched early."""
    provider, requests = build(monkeypatch, [tool_stream(done=False), completion(serialize(DECISION))])
    events = []
    provider.event_sink = lambda kind, payload: events.append(kind)
    provider.configure_tools(history=[])
    assert provider.decide({}, {}) == DECISION
    assert len(requests) == 2
    assert not any(kind.startswith("tool_call_") for kind in events)


def test_controller_budget_checked_before_every_tool_continuation(monkeypatch):
    """The single decision's second HTTP request cannot bypass cumulative controller limits."""
    provider, requests = build(monkeypatch, [tool_stream(), completion(serialize(DECISION))])
    checks = []

    def allow_one_request():
        """Permit only the first actual network attempt in this decision."""
        checks.append(True)
        return len(checks) == 1

    provider.configure_tools(history=[], before_request=allow_one_request)
    with pytest.raises(ProviderError, match="Controller request budget"):
        provider.decide({}, {})
    assert provider.requests_used == len(requests) == 1
    assert provider._history == []


def test_invalid_tool_arguments_return_error_then_allow_final_action(monkeypatch):
    """Model argument mistakes are visible recoverable tool results, not real actions."""
    provider, requests = build(monkeypatch, [tool_stream(arguments='{"limit":999}'),
                                            completion(serialize(DECISION))])
    provider.configure_tools(history=[])
    assert provider.decide({}, {}) == DECISION
    result = json.loads(requests[1]["messages"][-1]["content"])
    assert result["ok"] is False and "schema" in result["error"]


def test_last_tool_round_requires_final_answer(monkeypatch):
    """A finite loop explicitly requests a final decision after the local call allowance."""
    monkeypatch.setattr("sts2_agent.provider.MAX_TOOL_ROUNDS", 1)
    provider, requests = build(monkeypatch, [tool_stream(), completion(serialize(DECISION))])
    provider.configure_tools(history=[])
    provider.decide({}, {})
    assert requests[0]["tool_choice"] == "auto"
    assert requests[1]["tool_choice"] == "none"


def test_history_pagination_returns_complete_public_events_without_other_runs():
    """Older events remain retrievable after they leave the recent-memory window."""
    events = [{"facts": {"run": {"floor": index}, "screen": "COMBAT", "note": "old fact " + str(index)}}
              for index in range(50)]
    tools = ModelTools(KnowledgeBase(ROOT), history=lambda: events)
    call = {"id": "history", "function": {"name": "history_search",
        "arguments": '{"query":"old fact","limit":2,"offset":48}'}}
    result = tools.call(call, {}, {}, {})
    assert result["events"][0]["event"]["facts"]["run"]["floor"] == 1
    assert result["events"][1]["event"] == events[0]
    assert result["next_offset"] is None
    assert result["total_matches"] == 50


def test_simulator_tool_uses_exact_current_observation_and_declared_plans():
    """No tool can substitute an unobserved root or execute its plan against the game."""
    calls = []

    class Simulator:
        """Capture only the read-only evaluator interface."""

        def evaluate(self, observation, plans, **options):
            """Return a recognizable hypothetical result for contract verification."""
            calls.append((observation, plans, options))
            return {"status": "evaluated", "hypothetical": True}

    tools = ModelTools(KnowledgeBase(ROOT), simulator=Simulator())
    observation = {"state": {"screen": "COMBAT"}}
    plans = [{"id": "block", "actions": [{"type": "end_turn"}]}]
    result = tools.call({"id": "sim", "function": {"name": "simulation_evaluate", "arguments":
        serialize({"candidate_plans": plans, "samples": 2})}}, observation, {}, {})
    assert result["hypothetical"] is True
    assert calls == [(observation, plans, {"samples": 2, "rollout": True})]


def test_wiki_lookup_dispatch_returns_full_entry():
    """Detailed wiki reads retain the entity's source and unabridged rule data."""
    knowledge = KnowledgeBase(ROOT)
    tools = ModelTools(knowledge)
    result = tools.call({"id": "wiki", "function": {"name": "wiki_lookup", "arguments":
        '{"kind":"cards","entity_id":"BASH","include_related":false}'}}, {}, {}, {})
    assert "BASH" in serialize(result)
    assert "source" in serialize(result)


def evaluation_result():
    """Resemble a real audit: candidate metrics, stop reasons, coverage and full sample states."""
    state = {"player": {"hp": 40, "energy": 0, "stars": 2},
             "enemies": [{"i": 0, "enemy_id": "LeafSlimeM", "hp": {"current": 27, "max": 35},
                          "block": 0, "powers": [], "alive": True}],
             "hand": [{"card_handle": "h0", "card_id": "STRIKE_IRONCLAD", "cost": 1,
                       "description": "Static Strike description. " * 200,
                       "modifiers": {"$ref": "literal_public_value"}}],
             "score_support": {"meaning": "tactical_heuristic_not_a_win_rate"}}
    candidate = {"id": "attack_first", "status": "estimated", "sample_count": 2, "completed_samples": 2,
                 "metrics": {"mean": 1.2, "mean_stderr": 0.01, "p_win": 1.0, "p_loss": 0.0,
                             "p_unresolved": 0.0, "prefix_worlds": 2, "resolved_hp_samples": 24,
                             "cutoffs": 0, "meaning": "equal_weight_prefix_worlds_not_a_win_rate",
                             "probability_meaning": "empirical_terminal_mass_not_calibrated",
                             "hp_sq_sum": 3200.0, "value_sum": 96.0, "visit_weighted_mean": 1.19},
                 "mcts": {"visits": 500, "wins": 7},
                 "samples": [
                     {"status": "completed", "stop_reason": "unknown_draw_boundary:" + "detail" * 40,
                      "state": state, "mcts": {"visits": 250}},
                     {"status": "completed", "stop_reason": "unknown_draw_boundary",
                      "state": deepcopy(state), "mcts": {"visits": 250}}]}
    return {"status": "evaluated", "backend": "mentor", "rule_snapshot": "mentor-native-runtime",
            "diagnostics": [], "usage": {"prompt_tokens": 1},
            "result": {"scope": "current_turn",
                       "assumptions": ["declared_prefix_actions_are_never_replaced"],
                       "mcts": {"simulations_completed": 1000, "simulations_requested": 1000,
                                "cutoffs": 0, "visits": 1000},
                       "candidates": [candidate]}}




@pytest.mark.parametrize("compact", [True, False])
def test_simulation_tool_reply_carries_outcomes_not_sample_states(compact):
    """The model gets candidate values, coverage and one endpoint; never the per-sample states."""
    result = evaluation_result()
    raw = tool_result_content("simulation_evaluate", result)
    encoded = tool_result_content("simulation_evaluate", result, compact_simulation=compact)
    if not compact:
        assert encoded == raw
        return
    assert len(encoded) < len(raw) // 5
    for absent in ('"samples":', "card_handle", "score_support", "hp_sq_sum", "value_sum"):
        assert absent not in encoded
    summary = json.loads(encoded)
    candidate = summary["candidates"][0]
    assert candidate["id"] == "attack_first"
    assert candidate["sample_count"] == 2 and candidate["completed_samples"] == 2
    assert candidate["metrics"] == {"mean": 1.2, "mean_stderr": 0.01, "p_win": 1.0, "p_loss": 0.0,
                                    "p_unresolved": 0.0, "prefix_worlds": 2,
                                    "resolved_hp_samples": 24, "cutoffs": 0}
    assert candidate["stop_reasons"] == {"unknown_draw_boundary": 2}
    assert candidate["endpoint"]["player"] == {"hp": 40, "energy": 0, "stars": 2}
    assert candidate["endpoint"]["enemies"][0]["enemy_id"] == "LeafSlimeM"
    assert summary["search"] == {"simulations_completed": 1000, "simulations_requested": 1000,
                                 "cutoffs": 0}
    assert summary["metrics_meaning"]["meaning"] == "equal_weight_prefix_worlds_not_a_win_rate"
    assert summary["assumptions"] == ["declared_prefix_actions_are_never_replaced"]


def test_small_simulation_failure_and_other_tools_stay_verbatim():
    """A short failure or a non-simulation reply is clearer inline than summarized."""
    small = {"ok": False, "error": "No evaluation paths remain."}
    assert tool_result_content("simulation_evaluate", small, compact_simulation=True) == serialize(small)
    large = evaluation_result()
    assert tool_result_content("wiki_lookup", large, compact_simulation=True) == serialize(large)


@pytest.mark.parametrize("compact", [True, False])
def test_tool_reply_transport_keeps_raw_evidence_and_required_reasoning(monkeypatch, compact):
    """Only the model-facing text is compacted; the recorded tool event keeps the full audit."""
    result = evaluation_result()

    class Simulator:
        """Return one fixed evaluation without touching a game or a path ledger."""

        def evaluate(self, observation, plans, **options):
            """Supply a real-shaped audit for the provider tool continuation."""
            return result

    arguments = serialize({"candidate_plans": [{"id": "one", "actions": []}], "samples": 12})
    provider, requests = build(monkeypatch, [tool_stream("simulation_evaluate", arguments),
        completion(serialize(DECISION)), completion(serialize(DECISION))])
    provider.config["provider"]["compact_simulation_tool_results"] = compact
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    provider.configure_tools(simulator=Simulator())
    assert provider.decide({}, {}) == DECISION
    reply = requests[1]["messages"][-1]
    assert reply["role"] == "tool"
    if compact:
        assert '"samples":' not in reply["content"] and "candidates" in reply["content"]
        assert len(reply["content"]) < len(serialize(result)) // 5
    else:
        assert json.loads(reply["content"]) == result
    complete = next(payload for kind, payload in events if kind == "tool_call_completed")
    assert complete["result"] == result
    assert requests[1]["messages"][-2]["reasoning_content"] == "Need earlier public experience."
    assert provider.decide({"state": {"turn": 2}}, {}) == DECISION
    retained_reply = next(message for message in requests[2]["messages"] if message["role"] == "tool")
    assert retained_reply == reply
    assert provider.requests_used == 3
