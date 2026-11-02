"""Verify deferred audits never delay admission, change actions, or weaken durable boundaries."""

from copy import deepcopy
import threading
from unittest.mock import Mock

from sts2_agent.telemetry import read_jsonl
from test_session_controls import make_session


def test_background_prediction_uses_frozen_root_without_blocking_native_action(tmp_path):
    """A blocked audit cannot delay a real POST or overwrite a newer decision's display."""
    session, bridge, _ = make_session(tmp_path)
    session.config["simulation"] = {"prediction_mode": "background"}
    started, release = threading.Event(), threading.Event()
    captured = []

    def predict(observed, action):
        """Hold an audit after capturing the independent pre-action observation."""
        captured.append((deepcopy(observed), deepcopy(action)))
        started.set()
        assert release.wait(3)
        return {"status": "predicted", "state": {"player": {"hp": 80}}}

    session.simulator = Mock()
    session.simulator.settings = {"read_access": "public", "candidate_source": "mcts", "search_strength": 3}
    session.simulator.advise.return_value = {"status": "skipped"}
    session.simulator.synchronize.return_value = {"status": "disabled"}
    session.simulator.predict.side_effect = predict
    session.command("run", "resume")
    try:
        session._tick()
        assert started.wait(1)
        assert len(bridge.executions) == session.snapshot()["metrics"]["actions_completed"] == 1
        assert captured[0][0]["state"]["turn"] == 1
        assert captured[0][1] == bridge.executions[0]["action"] == {"type": "end_turn"}
        session._state["decision"] = {"decision_id": "newer-decision"}
        session._state["simulation"] = {"notice": "newer-advice"}
    finally:
        release.set()
        session._close_prediction_worker()
    assert session._state["simulation"] == {"notice": "newer-advice"}
    events = list(read_jsonl(session._journal.directory / "transitions.jsonl"))
    audit = next(event for event in events if event.get("data", {}).get("audit_mode") == "background")
    assert audit["action_id"] == bridge.executions[0]["action_id"]
    assert audit["data"]["prediction"]["status"] == "predicted"
    timings = list(read_jsonl(session._journal.directory / "stage-timings.jsonl"))
    assert {"native_dispatch", "background_prediction", "admission_observation"} <= {
        row["stage"] for row in timings}


def test_display_events_coalesce_ledger_writes_but_submission_stays_durable(tmp_path):
    """Derived UI refreshes retain journals while only semantic boundaries replace the ledger."""
    session, _, provider = make_session(tmp_path)
    session._persist = Mock(wraps=session._persist)
    session._emit("simulation", {"status": "partial"})
    session._emit("calculation", {"result": {"status": "partial"}})
    assert session._persist.call_count == 0
    session._emit("action_submitted", {"action_id": "reserved"})
    assert session._persist.call_count == 1
    provider.close = Mock()
    session.shutdown()
    provider.close.assert_called_once()
    assert session.snapshot()["performance"]["ledger_persist"]["count"] >= 1
