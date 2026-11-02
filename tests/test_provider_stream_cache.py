"""Offline SSE completion, request accounting, and reusable public-prefix checks."""

import json

import httpx
import pytest

from sts2_agent.memory import PublicMemory
from sts2_agent.prompt_context import policy_data, prompt_payload, serialize
from sts2_agent.provider import ProviderError
from sts2_agent.public import ActionError, validate_action
from test_provider import DECISION, TEST_KEY, build, completion, public_payload


def frame(delta: dict | None = None, *, finish=None, usage=None, choices=None) -> dict:
    """Build one ordinary DeepSeek stream chunk with optional final usage."""
    result = {"model": "deepseek-flash", "system_fingerprint": "stream-test",
              "choices": choices if choices is not None else [{
                  "index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        result["usage"] = usage
    return result


def stream(chunks: list[dict], *, done: bool = True) -> httpx.Response:
    """Encode SSE without network access, optionally simulating an early EOF."""
    body = "".join("data: " + serialize(chunk) + "\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)


def test_reasoning_content_usage_and_events_stay_separate(monkeypatch):
    """Deliver reasoning and JSON deltas while accounting only final reported usage."""
    content = serialize(DECISION)
    usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
             "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
    response = stream([frame({"reasoning_content": "Compare visible attacks."}),
                       frame({"content": content[:17]}), frame({"content": content[17:]}),
                       frame(finish="stop"), frame(usage=usage, choices=[])])
    provider, requests = build(monkeypatch, [response])
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    provider.event_context = {"run_id": "run", "decision_id": "decision", "decision_epoch": 2}
    assert provider.decide({}, {}) == DECISION
    assert requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert events[0][0] == "model_request_started"
    assert events[-1][0] == "model_request_completed"
    request_id = events[0][1]["model_request_id"]
    assert all(payload["model_request_id"] == request_id for _, payload in events)
    assert all(payload["decision_epoch"] == 2 for _, payload in events)
    assert events[0][1]["usage"]["total_tokens"] == "unknown"
    assert events[-1][1]["usage"] == provider.metadata[0]["usage"] == usage
    assert provider.metadata[0]["duration_ms"] >= 0
    assert "Compare visible" not in serialize(provider.metadata)
    assert "reasoning_content" not in serialize(provider._history)
    assert "".join(payload["text"] for kind, payload in events
                   if kind == "model_content_delta") == content


def test_usage_chunk_can_repeat_choice_and_finish_metadata(monkeypatch):
    """Accept final usage with a nonempty choices list and repeated identity."""
    provider, _ = build(monkeypatch, [stream([
        frame({"content": serialize(DECISION)}, finish="stop"),
        frame(finish="stop", usage={"total_tokens": 25, "prompt_cache_hit_tokens": 0}),
    ])])
    assert provider.decide({}, {}) == DECISION
    assert len(provider.metadata) == provider.requests_used == 1
    assert provider.metadata[0]["usage"]["total_tokens"] == 25
    assert provider.metadata[0]["usage"]["prompt_tokens"] == "unknown"


@pytest.mark.parametrize("finish", [None, "stop"])
def test_eof_never_executes_even_a_complete_json_fragment(monkeypatch, finish):
    """Require DONE as well as stop; partial streams never become valid decisions."""
    provider, requests = build(monkeypatch, [stream([
        frame({"content": serialize(DECISION)}, finish=finish)], done=False)])
    provider.config["provider"]["transient_retries"] = 0
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    with pytest.raises(ProviderError, match="stream ended"):
        provider.decide({}, {})
    assert len(requests) == provider.requests_used == 1
    assert provider._history == []
    assert provider.metadata[0]["usage"]["total_tokens"] == "unknown"
    assert events[-1][0] == "model_request_failed"
    assert not any(kind == "model_request_completed" for kind, _ in events)


def test_interrupted_stream_retry_has_independent_ids_and_real_counts(monkeypatch):
    """Retry an incomplete stream on the same model within the existing request budget."""
    provider, requests = build(monkeypatch, [
        stream([frame({"content": '{"action":'})], done=False),
        stream([frame({"content": serialize(DECISION)}, finish="stop")]),
    ])
    assert provider.decide({}, {}) == DECISION
    assert provider.requests_used == len(provider.metadata) == len(requests) == 2
    assert provider.metadata[0]["error"] == "incomplete_stream"
    assert len({record["model_request_id"] for record in provider.metadata}) == 2
    assert [record["attempt_index"] for record in provider.metadata] == [0, 1]
    assert {request["model"] for request in requests} == {"deepseek-flash"}
    assert requests[0]["messages"] == requests[1]["messages"]


def test_explicit_nonstream_path_omits_stream_options(monkeypatch):
    """Allow deliberate JSON mode without silently changing the model."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION))])
    provider.config["provider"]["stream"] = False
    assert provider.decide({}, {}) == DECISION
    assert requests[0]["stream"] is False
    assert "stream_options" not in requests[0]


def test_generation_clock_excludes_prefill_and_usage_tail(monkeypatch):
    """Measure text arrival with a monotonic clock, independent of request waiting."""
    provider, _ = build(monkeypatch, [])
    provider._request_started_monotonic = 100.0
    record = {"model_request_id": "clock-test", "response_streamed": True}
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    monkeypatch.setattr("sts2_agent.provider.time.monotonic", lambda: 102.0)
    provider._delta("reasoning", "first text", record, TEST_KEY)
    monkeypatch.setattr("sts2_agent.provider.time.monotonic", lambda: 105.0)
    provider._delta("content", '{"ok":true}', record, TEST_KEY)
    monkeypatch.setattr("sts2_agent.provider.time.monotonic", lambda: 110.0)
    provider._delta("content", "", record, TEST_KEY, flush=True)
    assert record["generation_ms"] == 3000
    assert record["first_delta_elapsed_ms"] == 2000
    assert record["last_delta_elapsed_ms"] == 5000
    assert events[-1][1]["generation_ms"] == 3000
    assert events[0][1]["first_delta_utc"] == events[-1][1]["first_delta_utc"]
    nonstream = {"model_request_id": "json-test", "response_streamed": False}
    provider._delta("content", '{"ok":true}', nonstream, TEST_KEY)
    assert "generation_ms" not in nonstream


def test_controller_metadata_does_not_change_serialized_policy_input():
    """Changing observation IDs, revisions, and times cannot perturb the policy prefix."""
    first = {"run_id": "run-one", "observation_id": "obs-one", "timestamp_utc": "today",
             "decision_episode_id": "episode-one", "state": {"action_revision": 1, "hp": 50}}
    second = {"state": {"hp": 50, "action_revision": 99}, "run_id": "run-two",
              "observation_id": "obs-two", "timestamp_utc": "tomorrow",
              "decision_episode_id": "episode-two"}
    assert serialize(policy_data(first)) == serialize(policy_data(second))
    assert serialize(policy_data(first)) == '{"state":{"hp":50}}'


def test_next_request_reuses_prior_user_and_assistant_prefix(monkeypatch):
    """Append new public facts after the exact prior request rather than rewriting it."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION)) for _ in range(2)])
    provider.decide({"observation_id": "first", "state": {"hp": 50}}, {})
    provider.decide({"observation_id": "second", "state": {"hp": 40}}, {})
    first, second = (request["messages"] for request in requests)
    assert second[:len(first)] == first
    assert second[-2] == {"role": "assistant", "content": serialize(DECISION)}
    assert public_payload(second)["observation"] == {"state": {"hp": 40}}
    assert "observation_id" not in serialize(requests)
    assert provider.metadata[0]["prefix_sha256"] == provider.metadata[1]["prefix_sha256"]


def test_current_memory_fact_payload_is_not_repeated():
    """Preserve the complete current observation without a duplicate memory snapshot."""
    observation = {"run_id": "run", "state": {"screen": "COMBAT", "turn": 1,
                   "run": {"floor": 3}, "combat": {"player": {"hp": 50}}}}
    memory = PublicMemory("run")
    memory.observe(observation)
    payload, _ = prompt_payload(observation, memory.context(), [])
    assert payload["memory"]["events"] == []
    assert payload["observation"]["state"] == observation["state"]
    memory.observe(observation, {"type": "end_turn"})
    payload, signatures = prompt_payload(observation, memory.context(), [])
    assert len(payload["memory"]["events"]) == 1
    again, _ = prompt_payload(observation, memory.context(), [], signatures)
    assert again["memory"]["events"] == []


def test_history_rollover_retains_current_public_facts_and_emits_reason(monkeypatch):
    """Bound retained turns and rebuild context transparently at the configured limit."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION)) for _ in range(4)])
    provider.config["provider"]["context_max_turns"] = 2
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    for hp in range(50, 46, -1):
        provider.decide({"state": {"hp": hp}}, {"plan": "Keep the current build."})
        assert len(provider._history) <= 4
    assert len(requests[2]["messages"]) == 2
    current = public_payload(requests[2]["messages"])
    assert current["observation"]["state"]["hp"] == 48
    assert current["memory"]["plan"] == "Keep the current build."
    assert [(kind, payload["reason"]) for kind, payload in events if kind == "context_reset"] == [
        ("context_reset", "history_turn_limit")]
    assert provider.requests_used == 4


def test_character_limit_never_truncates_current_observation(monkeypatch):
    """Overlarge current public facts remain complete while history is discarded."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION)) for _ in range(2)])
    provider.config["provider"]["context_max_characters"] = 100
    observation = {"state": {"description": "public fact " * 200}}
    provider.decide(observation, {})
    provider.decide(observation, {})
    assert provider._history == []
    assert all(len(request["messages"]) == 2 for request in requests)
    assert public_payload(requests[-1]["messages"])["observation"] == observation


def test_history_budget_excludes_current_and_shared_definitions_reset_together(monkeypatch):
    """Reuse complete prior messages, and rebuild all referenced facts at an epoch boundary."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION)) for _ in range(4)])
    provider.config["provider"]["context_max_turns"] = 2
    provider.config["provider"]["context_max_characters"] = 10000
    rule = {"description": "Visible, instance-specific public rule. " * 30}
    observations = [{"state": {"turn": index, "rules": [rule, rule]}} for index in range(4)]
    for observation in observations:
        provider.decide(observation, {})
        assert public_payload(requests[-1]["messages"])["observation"] == observation
    assert requests[1]["messages"][:2] == requests[0]["messages"]
    assert len(requests[2]["messages"]) == 2
    assert provider.metadata[2]["prior_shared_definitions"] == 0
    assert provider.metadata[1]["current_chars"] < provider.metadata[0]["current_chars"]
    assert provider.metadata[1]["prior_shared_definitions"] > 0


def test_large_current_facts_do_not_evict_an_in_budget_history(monkeypatch):
    """A full current observation has a separate allowance from retained conversation."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION)) for _ in range(2)])
    provider.decide({"state": {"hp": 50}}, {})
    retained = sum(len(message["content"]) for message in provider._history)
    provider.config["provider"]["context_max_characters"] = retained + 1
    observation = {"state": {"description": "A complete, large public observation. " * 200}}
    provider.decide(observation, {})
    assert len(requests[-1]["messages"]) == 4
    assert public_payload(requests[-1]["messages"])["observation"] == observation


def test_revoke_removes_instruction_and_all_old_model_paraphrases(monkeypatch):
    """Revocation clears historical user guidance and assistant echoes without a refund."""
    first = {**DECISION, "reason": "Keep all potions because the user asked."}
    provider, requests = build(monkeypatch, [completion(serialize(first)), completion(serialize(DECISION))])
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    provider.decide({}, {}, instructions=[{"id": "one", "text": "Keep all potions", "status": "injected"}])
    provider.decide({}, {}, instructions=[])
    assert len(requests[-1]["messages"]) == 2
    assert "Keep all potions" not in serialize(requests[-1])
    assert any(kind == "context_reset" and payload["reason"] == "active_instructions_changed"
               for kind, payload in events)
    assert provider.requests_used == 2


def test_instruction_lifecycle_metadata_alone_keeps_history(monkeypatch):
    """Bookkeeping changes cannot reset an otherwise identical active instruction."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION)) for _ in range(2)])
    provider.decide({}, {}, instructions=[{"id": "one", "text": "Prefer attacks", "status": "queued"}])
    provider.decide({}, {}, instructions=[{"id": "two", "text": "Prefer attacks", "status": "injected"}])
    assert len(requests[-1]["messages"]) == 4
    assert "queued" not in serialize(requests)


def test_start_is_counted_and_published_before_http_and_reset_cannot_resurrect_old_history(monkeypatch):
    """Request accounting precedes I/O and a reset invalidates late history writes."""
    provider, requests = build(monkeypatch, [completion(serialize(DECISION))])
    events = []

    def sink(kind, payload):
        """Inspect actual request ordering and imitate redecision during the HTTP call."""
        events.append((kind, payload))
        if kind == "model_request_started":
            assert provider.requests_used == 1
            assert requests == []
            provider.reset_context("redecision")

    provider.event_sink = sink
    assert provider.decide({}, {}) == DECISION
    assert provider._history == []
    assert provider.requests_used == len(requests) == 1


def test_stream_format_repair_keeps_actual_attempts_and_rejects_extra_fields(monkeypatch):
    """A complete SSE response still needs schema validation and bounded repair."""
    provider, requests = build(monkeypatch, [
        stream([frame({"content": serialize({**DECISION, "unexpected": True})}, finish="stop")]),
        stream([frame({"content": serialize(DECISION)}, finish="stop")]),
    ])
    assert provider.decide({}, {}) == DECISION
    assert provider.requests_used == len(requests) == len(provider.metadata) == 2
    assert [record["repair_index"] for record in provider.metadata] == [0, 1]
    assert [record["format_repair"] for record in provider.metadata] == [False, True]
    assert "Repair the previous answer" in requests[-1]["messages"][-1]["content"]
    assert all("unexpected" not in message["content"] for message in provider._history)


def test_transport_exception_text_never_reaches_event_sink(monkeypatch):
    """Failures expose a safe generic message while preserving unknown usage."""
    provider, _ = build(monkeypatch, [httpx.ReadError(TEST_KEY)])
    provider.config["provider"]["transient_retries"] = 0
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    with pytest.raises(ProviderError):
        provider.decide({}, {})
    assert TEST_KEY not in serialize(events)
    assert events[-1][0] == "model_request_failed"
    assert events[-1][1]["usage"]["total_tokens"] == "unknown"


def test_split_credentials_are_redacted_before_text_events(monkeypatch):
    """Do not reconstruct a credential in the UI when it crosses SSE chunk boundaries."""
    provider, _ = build(monkeypatch, [stream([
        frame({"reasoning_content": "echo " + TEST_KEY[:9]}),
        frame({"reasoning_content": TEST_KEY[9:] + " done"}),
        frame({"content": serialize(DECISION)}, finish="stop"),
    ])])
    events = []
    provider.event_sink = lambda kind, payload: events.append((kind, payload))
    assert provider.decide({}, {}) == DECISION
    reasoning = "".join(payload["text"] for kind, payload in events
                        if kind == "model_reasoning_delta")
    assert reasoning == "echo [REDACTED] done"
    assert TEST_KEY not in serialize(events)


def test_length_still_collects_later_usage_without_retry(monkeypatch):
    """Capture billed usage after truncation while stopping instead of repairing."""
    provider, requests = build(monkeypatch, [stream([
        frame({"content": '{"action":'}, finish="length"),
        frame(usage={"total_tokens": 16000}, choices=[]),
    ])])
    with pytest.raises(ProviderError, match="truncated"):
        provider.decide({}, {})
    assert provider.requests_used == len(requests) == 1
    assert provider.metadata[0]["usage"]["total_tokens"] == 16000


def test_repeated_http_errors_and_format_repair_all_have_one_metadata_record(monkeypatch):
    """Count real HTTP attempts across retry and repair without double-counting chunks."""
    provider, requests = build(monkeypatch, [httpx.Response(503), completion("bad"),
                                          completion(serialize(DECISION))])
    assert provider.decide({}, {}) == DECISION
    assert provider.requests_used == len(requests) == len(provider.metadata) == 3
    assert [record["request_index"] for record in provider.metadata] == [1, 2, 3]
    assert [record["attempt_index"] for record in provider.metadata] == [0, 1, 0]
    assert [record["repair_index"] for record in provider.metadata] == [0, 0, 1]


def targetless_observation() -> dict:
    """Expose two legal targetless public cards so changing the selected card is detectable."""
    return {"available_actions": ["play_card", "end_turn"], "state": {
        "screen": "COMBAT", "combat": {"hand": [
            {"i": 0, "card_id": "THUNDERCLAP", "playable": True, "targets": []},
            {"i": 1, "card_id": "DEFEND", "playable": True, "targets": []},
        ]}}}


def test_targetless_card_repairs_extra_target_once_and_preserves_selected_card(monkeypatch):
    """Ask the same model to omit a forbidden target and store only its valid repaired answer."""
    invalid = {"action": {"type": "play_card", "card_index": 0, "target_index": 0}, "reason": "Use Thunderclap"}
    repaired = {"action": {"type": "play_card", "card_index": 0}, "reason": "Use Thunderclap"}
    provider, requests = build(monkeypatch, [completion(serialize(invalid)), completion(serialize(repaired))])
    observation = targetless_observation()
    assert provider.decide(observation, {}) == repaired
    validate_action(observation, repaired["action"])
    assert provider.requests_used == len(requests) == 2
    assert [record["repair_index"] for record in provider.metadata] == [0, 1]
    assert "deleting only target_index" in requests[-1]["messages"][-1]["content"]
    assert "Preserve type, card_index and option_index exactly" in requests[-1]["messages"][-1]["content"]
    assert json.loads(provider._history[-1]["content"]) == repaired
    assert all(request["model"] == "deepseek-flash" for request in requests)


@pytest.mark.parametrize("changed_action", [
    {"type": "play_card", "card_index": 1}, {"type": "end_turn"},
    {"type": "play_card", "card_index": 0, "option_index": 1},
])
def test_target_parameter_repair_cannot_change_action_selection(monkeypatch, changed_action):
    """Reject a model that uses a parameter repair to change its chosen card or action."""
    invalid = {"action": {"type": "play_card", "card_index": 0, "target_index": 0}, "reason": "Use Thunderclap"}
    changed = {"action": changed_action, "reason": "Changed selection"}
    provider, requests = build(monkeypatch, [completion(serialize(invalid)), completion(serialize(changed))])
    with pytest.raises(ProviderError, match="repair changed the selected action"):
        provider.decide(targetless_observation(), {})
    assert provider.requests_used == len(requests) == 2
    assert provider._history == []


@pytest.mark.parametrize("repair_limit,first_content,expected_requests", [(0, None, 1), (1, "bad JSON", 2)])
def test_target_parameter_repair_shares_existing_format_limit(monkeypatch, repair_limit, first_content, expected_requests):
    """A prior JSON repair or a zero allowance leaves no extra request for target correction."""
    invalid = {"action": {"type": "play_card", "card_index": 0, "target_index": 0}, "reason": "Use Thunderclap"}
    responses = [completion(first_content)] if first_content is not None else []
    responses.append(completion(serialize(invalid)))
    provider, requests = build(monkeypatch, responses)
    provider.config["provider"]["format_repair_attempts"] = repair_limit
    with pytest.raises(ProviderError, match="bounded repair"):
        provider.decide(targetless_observation(), {})
    assert provider.requests_used == len(requests) == expected_requests
    assert provider._history == []


@pytest.mark.parametrize("failure", ["invalid_card", "disabled"])
def test_other_semantic_action_errors_do_not_trigger_target_parameter_repair(monkeypatch, failure):
    """Leave invalid card choices to the session's explicit rejection."""
    observation = targetless_observation()
    action = {"type": "play_card", "card_index": 0, "target_index": 0}
    if failure == "invalid_card":
        action["card_index"] = 99
    elif failure == "disabled":
        observation["state"]["combat"]["hand"][0]["playable"] = False
    else:
        observation["state"]["combat"]["hand"][0]["targets"] = [1]
    decision = {"action": action, "reason": "A semantic mistake"}
    provider, requests = build(monkeypatch, [completion(serialize(decision))])
    assert provider.decide(observation, {}) == decision
    with pytest.raises(ActionError):
        validate_action(observation, action)
    assert provider.requests_used == len(requests) == 1


@pytest.mark.parametrize("target", [None, 1])
def test_required_target_repairs_missing_or_stale_enemy_index(monkeypatch, target):
    """An enemy death can renumber targets; the same model repairs only that parameter."""
    observation = targetless_observation()
    observation["state"]["combat"]["hand"][0]["targets"] = [0]
    action = {"type": "play_card", "card_index": 0}
    if target is not None:
        action["target_index"] = target
    repaired = {"action": {**action, "target_index": 0}, "reason": "Use the current target"}
    provider, requests = build(monkeypatch, [completion(serialize({"action": action, "reason": "Attack"})),
                                            completion(serialize(repaired))])
    assert provider.decide(observation, {}) == repaired
    assert len(requests) == 2
    assert "current valid targets: [0]" in requests[-1]["messages"][-1]["content"]


def test_required_target_repair_rejects_a_different_card(monkeypatch):
    """Target correction cannot become a local or model-selected replacement card."""
    observation = targetless_observation()
    observation["state"]["combat"]["hand"][0]["targets"] = [0]
    invalid = {"action": {"type": "play_card", "card_index": 0, "target_index": 1}, "reason": "Attack"}
    changed = {"action": {"type": "play_card", "card_index": 1}, "reason": "Defend instead"}
    provider, _ = build(monkeypatch, [completion(serialize(invalid)), completion(serialize(changed))])
    with pytest.raises(ProviderError, match="repair changed the selected action"):
        provider.decide(observation, {})
