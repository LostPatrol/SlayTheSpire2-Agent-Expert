"""Verify complete public history, stable pagination and incremental journal reads."""

import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from sts2_agent.dashboard import create_app
from sts2_agent.history import HistoryStore


def journal(path: Path, rows: list) -> None:
    """Append complete test records using the controller's JSONL framing."""
    with path.open("ab") as stream:
        for row in rows:
            stream.write((json.dumps(row) + "\n").encode())


def transition(kind: str, payload: dict, *, decision="d1", number=1, **context) -> dict:
    """Build a real transition envelope with explicit correlation identities."""
    return {"schema_version": 2, "event_id": f"event-{number}", "event_type": kind,
            "timestamp_utc": f"2026-11-01T00:00:{number:02d}+00:00", "run_id": "run-a",
            **({"decision_id": decision} if decision else {}), **context, "data": payload}


def archive(tmp_path: Path, rows: list):
    """Create one independently readable public run archive."""
    directory = tmp_path / "runs/run-a"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({"run_id": "run-a", "config": {"secret": "not-returned"}}))
    journal(directory / "transitions.jsonl", rows)
    return HistoryStore(tmp_path).get("run-a"), directory


def ready(history, *, before=None, limit=30):
    """Wait briefly for the asynchronous read-only index used by real HTTP requests."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        page = history.page(before, limit)
        if not page.get("indexing"):
            return page
        time.sleep(0.005)
    pytest.fail("history index did not become ready")


def test_history_restores_full_request_tools_and_native_status(tmp_path):
    """Only completed actions become executed; full request text is sought outside snapshots."""
    rows = [
        transition("model_request", {"provider_event_type": "model_request_started", "payload": {
            "model_request_id": "r1", "decision_id": "d1", "started_utc": "2026-11-01T00:00:01Z"}},
            model_request_id="r1"),
        transition("model_request", {"provider_event_type": "model_request_input", "payload": {
            "model_request_id": "r1", "messages": [{"role": "user", "content": "public input"}]}},
            number=2, model_request_id="r1"),
        transition("model_request", {"provider_event_type": "model_request_completed", "payload": {
            "model_request_id": "r1", "metadata": {"usage": {"completion_tokens": 42}}},
            "response": {"model_request_id": "r1", "reasoning": "full original reasoning",
                         "content": "full original answer", "status": "completed"}}, number=3, model_request_id="r1"),
        transition("tool_call_started", {"tool_call_id": "t1", "decision_id": "d1", "name": "wiki_lookup",
            "arguments": {"id": "CARD"}, "status": "started"}, number=4),
        transition("tool_call_completed", {"tool_call_id": "t1", "decision_id": "d1", "name": "wiki_lookup",
            "result": {"text": "actual result"}, "status": "completed"}, number=5),
        transition("decision", {"decision": {"decision_id": "d1", "action": {"type": "end_turn"},
            "reason": "actual reason"}}, number=6),
        transition("action_submitted", {"action_id": "a1", "decision_id": "d1"}, number=7, action_id="a1"),
        transition("action_receipt", {"receipt": {"action_id": "a1", "status": "completed"}},
                   number=8, action_id="a1"),
    ]
    history, directory = archive(tmp_path, rows)
    assert ready(history)["decisions"][0]["status"] == "submitted"
    detail = history.detail("d1")
    assert detail["complete"] is True
    assert detail["model_requests"][0]["reasoning"] == "full original reasoning"
    assert detail["model_requests"][0]["input"]["messages"][0]["content"] == "public input"
    assert detail["tool_calls"][0]["arguments"] == {"id": "CARD"}
    assert detail["tool_calls"][0]["result"] == {"text": "actual result"}
    assert ready(history)["decisions"][0]["counts"] == {"request": 1, "tool": 1, "event": 8}
    journal(directory / "transitions.jsonl", [transition("action_completed", {"decision_id": "d1", "action_id": "a1"}, number=9)])
    assert ready(history)["decisions"][0]["status"] == "executed"
    assert "not-returned" not in json.dumps(history.detail("d1"))
    assert sorted(path.name for path in directory.iterdir()) == ["manifest.json", "transitions.jsonl"]


@pytest.mark.parametrize("usage", [{"completion_tokens": 12}, {}])
def test_history_restores_cancelled_partial_response_without_decision(tmp_path, usage):
    """A cancelled request retains partial text and real usage without an executed decision."""
    history, directory = archive(tmp_path, [
        transition("model_request", {"provider_event_type": "model_request_started", "payload": {
            "model_request_id": "r1", "decision_id": "d1"}}, model_request_id="r1"),
        transition("model_request", {"provider_event_type": "model_request_cancelled", "payload": {
            "model_request_id": "r1", "decision_id": "d1", "metadata": {"usage": usage}},
            "response": {"model_request_id": "r1", "reasoning": "partial reasoning",
                         "content": "partial answer", "status": "cancelling"}}, number=2, model_request_id="r1"),
    ])
    journal(directory / "dashboard-events.jsonl", [{"seq": 1, "type": "model_request_cancelled",
        "payload": {"model_request_id": "r1", "decision_id": "d1"}}])
    page = ready(history)
    assert page["decisions"][0]["status"] == "pending"
    assert page["decisions"][0]["counts"]["event"] == 2
    detail = history.detail("d1")
    request = detail["model_requests"][0]
    assert request["status"] == "cancelled"
    assert request["reasoning"] == "partial reasoning"
    assert request["content"] == "partial answer"
    assert request["usage"] == usage
    assert "action" not in detail["decision"]


def test_history_pages_beyond_snapshot_window_with_stable_cursor(tmp_path):
    """New decisions do not cause an older-page cursor to skip or repeat archived groups."""
    history, directory = archive(tmp_path, [transition("decision", {"decision": {
        "decision_id": f"d{number}", "action": {"type": "end_turn"}}}, decision=f"d{number}", number=number)
        for number in range(1, 51)])
    first = ready(history, limit=30)
    assert first["total_decisions"] == 50
    assert first["decisions"][0]["decision_id"] == "d50"
    journal(directory / "transitions.jsonl", [transition("decision", {"decision": {"decision_id": "d51"}}, decision="d51", number=51)])
    second = ready(history, before=first["next_before"], limit=30)
    assert [row["decision_id"] for row in second["decisions"]] == [f"d{n}" for n in range(20, 0, -1)]
    assert not second["has_more"]


def test_history_uses_explicit_ids_and_skips_duplicate_lifecycle(tmp_path):
    """Unassociated session events stay independent and mirrored facts are not counted twice."""
    history, directory = archive(tmp_path, [
        transition("decision", {"decision": {"decision_id": "d1"}}, number=1),
        transition("action_submitted", {"action_id": "a1", "decision_id": "d1"}, action_id="a1", number=2),
        transition("action_receipt", {"receipt": {"action_id": "a1"}}, action_id="a1", number=3),
    ])
    journal(directory / "dashboard-events.jsonl", [
        {"seq": 1, "type": "decision", "payload": {"run_id": "run-a", "decision_id": "d1"}},
        {"seq": 2, "type": "action_submitted", "payload": {"decision_id": "d1", "action_id": "a1"}},
        {"seq": 3, "type": "action_receipt", "payload": {"action_id": "a1"}},
        {"seq": 4, "type": "control_command", "payload": {"command": "pause"}},
        {"seq": 5, "type": "model_reasoning_delta", "payload": {"text": "partial"}},
    ])
    page = ready(history)
    assert page["through_seq"] == 5
    assert page["decisions"][0]["counts"]["event"] == 3
    session = history.events(None, 50)
    assert len(session["events"]) == 1
    assert session["events"][0]["type"] == "control_command"
    assert "decision_id" not in session["events"][0]["payload"]


def test_history_retries_partial_tail_and_reports_corrupt_lines(tmp_path):
    """Incomplete appends are retried without rewriting evidence or losing the next valid record."""
    history, directory = archive(tmp_path, [transition("decision", {"decision": {"decision_id": "d1"}})])
    ready(history)
    path = directory / "transitions.jsonl"
    raw = (json.dumps(transition("decision", {"decision": {"decision_id": "d2"}}, decision="d2", number=2)) + "\n").encode()
    with path.open("ab") as stream:
        stream.write(b'bad-json\n')
        stream.write(raw[:40])
    partial = ready(history)
    assert partial["total_decisions"] == 1
    assert partial["pending_tail"] == ["transitions.jsonl"]
    assert partial["issue_count"] == 1
    with path.open("ab") as stream:
        stream.write(raw[40:])
    finished = ready(history)
    assert finished["total_decisions"] == 2
    assert not finished["pending_tail"]
    assert finished["issue_count"] == 1


def test_history_routes_validate_paths_and_are_read_only(tmp_path):
    """GET history cannot select arbitrary files or mutate controller state."""
    archive(tmp_path, [transition("decision", {"decision": {"decision_id": "d1"}})])
    app = create_app({}, tmp_path, session=object())
    client = TestClient(app, base_url="http://127.0.0.1:18766")
    assert client.get("/api/history", params={"run_id": "../secrets"}).status_code == 422
    assert client.get("/api/history", params={"run_id": "unknown"}).status_code == 404
    assert client.get("/api/history", params={"run_id": "run-a", "limit": 0}).status_code == 422
    assert client.post("/api/history", json={"run_id": "run-a"}).status_code == 405
    for _ in range(100):
        page = client.get("/api/history", params={"run_id": "run-a"}).json()
        if not page.get("indexing"):
            break
        time.sleep(0.005)
    assert page["decisions"][0]["decision_id"] == "d1"
    assert client.get("/api/history/d1", params={"run_id": "run-a"}).json()["complete"]
    assert client.get("/api/history/missing", params={"run_id": "run-a"}).status_code == 404
    assert client.get("/api/history-events", params={"run_id": "run-a"}).json()["events"] == []
