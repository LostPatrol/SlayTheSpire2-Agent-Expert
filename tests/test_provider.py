"""Offline provider protocol, model identity, and actual-request budget checks."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from sts2_agent.config import ConfigError, load_config
from sts2_agent.provider import Provider, ProviderError
from sts2_agent.prompt_projection import expand_public


# Tests read the checked-in sample and never access credential files.
ROOT = Path(__file__).resolve().parents[1]
# This credential exists only in the mocked process environment.
TEST_KEY = "offline-test-credential"
# A model-selected action used to verify that parsing preserves all parameters.
DECISION = {"action": {"type": "play_card", "card_index": 3, "target_index": 1}, "reason": "Use the available attack."}


def public_payload(messages: list[dict]) -> dict:
    """Expand the last public request using only definitions present in its actual history."""
    definitions = {}
    payload = None
    for message in messages:
        if message["role"] != "user":
            continue
        projection = json.loads(message["content"])
        payload = expand_public(projection, known_definitions=definitions)
        definitions.update(projection["definitions"])
    return payload


def completion(content: str, *, model: str = "deepseek-flash", finish: str = "stop") -> httpx.Response:
    """Build an envelope with an ignored reasoning field and missing usage."""
    return httpx.Response(200, json={
        "model": model,
        "system_fingerprint": "test-fingerprint",
        "choices": [{"finish_reason": finish, "message": {
            "content": content, "reasoning_content": "PRIVATE TRACE MUST NOT BE RETAINED",
        }}],
    })


def build(monkeypatch, responses: list, *, request_limit: int = 60,
          config_patch: dict | None = None) -> tuple[Provider, list]:
    """Create a provider using only deterministic in-process HTTP responses."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    monkeypatch.setattr("sts2_agent.provider.time.sleep", lambda delay: None)
    config = copy.deepcopy(load_config("configs/smoke.json", ROOT))
    config["limits"]["max_provider_requests_per_run"] = request_limit
    config["provider"].update(config_patch or {})
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Capture outgoing protocol settings and return the next mock result."""
        requests.append(json.loads(request.content))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = httpx.Client(transport=httpx.MockTransport(handle))
    return Provider(config, ROOT, client=client), requests


def test_request_protocol_preserves_model_action_and_discards_reasoning(monkeypatch):
    """Use the configured no-tools protocol and retain only safe metadata."""
    provider, requests = build(monkeypatch, [completion(json.dumps(DECISION))])
    assert provider.decide({"available_actions": []}, {}) == DECISION
    assert provider.requests_used == 1
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert requests[0]["reasoning_effort"] == "high"
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["max_tokens"] == 16384
    assert "tools" not in requests[0] and "tool_choice" not in requests[0]
    assert provider.metadata[0]["usage"]["total_tokens"] == "unknown"
    assert "PRIVATE TRACE" not in json.dumps(provider.metadata)
    assert TEST_KEY not in json.dumps(provider.metadata)


def test_disabled_thinking_omits_effort_and_records_toggle(monkeypatch):
    """A disabled request must not send a thinking effort for a mode that ignores it."""
    provider, requests = build(monkeypatch, [completion(json.dumps(DECISION))],
                               config_patch={"thinking": "disabled"})
    assert provider.decide({"available_actions": []}, {}) == DECISION
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in requests[0]
    assert provider.metadata[0]["thinking"] == "disabled"


def test_format_repair_is_bounded_and_uses_same_model(monkeypatch):
    """Allow one format repair without locally choosing an alternative action."""
    provider, requests = build(monkeypatch, [completion("bad"), completion(json.dumps(DECISION))])
    assert provider.decide({}, {}) == DECISION
    assert provider.requests_used == 2
    assert all(request["model"] == "deepseek-flash" for request in requests)
    assert "reasoning_content" not in json.dumps(requests)
    provider, requests = build(monkeypatch, [completion("bad"), completion("still bad")])
    with pytest.raises(ProviderError, match="bounded repair"):
        provider.decide({}, {})
    assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 403, 404])
def test_configuration_http_errors_stop_immediately(monkeypatch, status):
    """Never retry authentication, authorization, or model endpoint failures."""
    provider, requests = build(monkeypatch, [httpx.Response(status, text=TEST_KEY)])
    with pytest.raises(ProviderError) as result:
        provider.decide({}, {})
    assert len(requests) == provider.requests_used == 1
    assert TEST_KEY not in str(result.value)


def test_truncation_stops_without_format_repair(monkeypatch):
    """Treat token-limit truncation as an explicit terminal provider error."""
    provider, requests = build(monkeypatch, [completion("{}", finish="length")])
    with pytest.raises(ProviderError, match="truncated"):
        provider.decide({}, {})
    assert len(requests) == provider.requests_used == 1


def test_transient_retries_consume_actual_request_budget(monkeypatch):
    """Stop before a third HTTP attempt when only two requests are budgeted."""
    provider, requests = build(monkeypatch, [httpx.Response(503), httpx.Response(503)], request_limit=2)
    with pytest.raises(ProviderError, match="budget exhausted"):
        provider.decide({}, {})
    assert len(requests) == provider.requests_used == 2
    assert len(provider.metadata) == 2


def test_probe_retries_network_failure_and_returns_safe_answer(monkeypatch):
    """Probe the same real protocol while suppressing unsafe transport details."""
    provider, requests = build(monkeypatch, [httpx.ConnectError(TEST_KEY), completion('{"ok":true}')])
    result = provider.probe()
    assert result["answer"] == {"ok": True}
    assert result["requests_used"] == len(requests) == 2
    assert TEST_KEY not in json.dumps(provider.metadata)


@pytest.mark.parametrize("model", ["deepseek-pro", "unexpected", None])
def test_unapproved_returned_model_stops_without_retry(monkeypatch, model):
    """Do not silently accept a fallback or a missing model identity."""
    provider, requests = build(monkeypatch, [completion(json.dumps(DECISION), model=model)])
    with pytest.raises(ProviderError, match="model identity"):
        provider.decide({}, {})
    assert len(requests) == provider.requests_used == 1


def test_explicit_flash_version_is_allowed(monkeypatch):
    """Accept the documented concrete Flash version as well as its alias."""
    provider, _ = build(monkeypatch, [completion(json.dumps(DECISION), model="deepseek-v4.1-flash-1017")])
    assert provider.decide({}, {}) == DECISION


def test_unsupported_mode_is_rejected_without_echoing_input(tmp_path):
    """The configuration loader refuses modes outside the implemented baseline."""
    config = load_config("configs/smoke.json", ROOT)
    config["policy"]["mode"] = TEST_KEY
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError) as result:
        load_config(path, ROOT)
    assert TEST_KEY not in str(result.value)
