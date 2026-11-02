"""Check bounded offline references and durable spectator explanations through real interfaces."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from sts2_agent.dashboard import create_app
from sts2_agent.knowledge import KnowledgeBase
from test_dashboard_api import FakeSession
from test_provider import build, completion, DECISION, public_payload
from test_session_controls import make_session, FakeProvider


# The imported corpus is checked in and all provider requests below use local HTTP doubles.
ROOT = Path(__file__).resolve().parents[1]


def test_model_receives_selected_live_references_and_version_mismatch_is_explicit(monkeypatch):
    """A relevant Power reaches the actual model payload without injecting the full wiki."""
    provider, requests = build(monkeypatch, [completion(json.dumps(DECISION)) for _ in range(2)])
    obs = {"state": {"screen": "COMBAT", "combat": {"player": {"powers": ["SANDPIT_POWER 1"]}}}}
    provider.decide(obs, {})
    payload = public_payload(requests[0]["messages"])
    knowledge = payload["knowledge"]
    assert payload["observation"] == obs
    assert len(knowledge["entries"]) < 24
    sandpit = next(entry for entry in knowledge["entries"] if entry["entity_id"] == "SANDPIT")
    assert sandpit["observed_amounts"] == [1]
    assert "sample_description" not in sandpit
    assert provider.metadata[0]["knowledge_snapshot_id"] == knowledge["snapshot_id"]
    provider.knowledge_game_version = "v999.0"
    provider.decide(obs, {})
    latest = public_payload(requests[-1]["messages"])["knowledge"]
    assert latest["status"] == "version_mismatch" and latest["entries"] == []
    assert len(requests[-1]["messages"]) == 2


def test_offline_browser_reads_full_entity_without_policy_calls(tmp_path):
    """Human browsing uses the local corpus and never sends a gameplay command."""
    session = FakeSession()
    # Keep test controller locks away from a running local dashboard.
    session.provider = SimpleNamespace(knowledge=KnowledgeBase(ROOT))
    app = create_app({}, tmp_path, session=session)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/knowledge").status_code == 200
        result = client.get("/api/knowledge?q=Sandpit&kind=powers").json()
        assert result["metadata"]["status"] == "ready"
        assert any(entry["entity_id"] == "SANDPIT" for entry in result["results"])
        detail = client.get("/api/knowledge/powers/SANDPIT_POWER").json()
        assert detail["entity_id"] == "SANDPIT"
        assert detail["source"]["revision"]
        assert client.get("/api/knowledge/powers/DOES_NOT_EXIST").status_code == 404
        assert session.commands == {}


def test_decision_explanations_survive_token_flood_and_restart(tmp_path):
    """Full reasons remain after the SSE event window rolls over, with execution status intact."""
    session, bridge, _ = make_session(tmp_path)
    session.command("step", "step")
    session._tick()
    first = session.snapshot()["decision_history"][-1]
    assert first["reason"] == "Offline test decision" and first["status"] == "executed"
    for _index in range(2100):
        session._emit("model_reasoning_delta", {"text": "visible streamed text"}, persist=False)
    session._persist()
    assert not any(event["type"] == "decision" for event in session.snapshot()["events"])
    restored = type(session)(session.config, tmp_path, bridge=bridge, provider=FakeProvider())
    assert restored.snapshot()["decision_history"][-1] == first
    assert restored.snapshot()["metrics"]["requests_used"] == 1


def test_display_timestamps_follow_real_provider_lifecycle(monkeypatch):
    """Refreshable timing metadata accompanies the same request, never an extra API call."""
    provider, requests = build(monkeypatch, [completion(json.dumps(DECISION))])
    provider.decide({}, {})
    record = provider.metadata[0]
    assert record["started_utc"] <= record["first_delta_utc"] <= record["completed_utc"]
    assert len(requests) == provider.requests_used == 1
