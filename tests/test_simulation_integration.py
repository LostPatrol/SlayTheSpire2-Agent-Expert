"""Verify public advice stays advisory and every accepted action has replay evidence."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from sts2_agent.session import DashboardSession, SessionError, archive_terminal_session
from sts2_agent.simulation import CombatSimulator, HOST_MENTOR
import sts2_agent.simulation as simulation
from sts2_agent.telemetry import Journal, read_jsonl, write_json
from test_session_controls import FakeProvider, make_session


def test_default_mentor_child_roundtrips_chinese_json_with_utf8_disabled_in_environment(tmp_path, monkeypatch):
    """The real production launcher must preserve non-ASCII JSON in both subprocess directions."""
    artifact = tmp_path / "mentor_native.pyd"
    artifact.write_bytes(b"transport-only-test")
    monkeypatch.setattr(simulation, "HOST_MENTOR", artifact)
    monkeypatch.setenv("PYTHONUTF8", "0")
    # Replace only the native service body, retaining the production interpreter flags and real pipes.
    service = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"ok": True, "backend": "mentor", "rule_snapshot": "transport-test",
                      "echo": request["label"], "utf8_mode": sys.flags.utf8_mode}, ensure_ascii=False), flush=True)
'''
    original_popen = subprocess.Popen

    def start_echo(command, **kwargs):
        """Run a minimal JSON service using exactly the production child's encoding flags."""
        assert command[command.index("-m") + 1] == "sts2_agent.mentor_runtime"
        flags = command[1:command.index("-m")]
        return original_popen([sys.executable, *flags, "-c", service], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start_echo)
    client = CombatSimulator({"simulation": {"enabled": True, "backend": "mentor"}}, tmp_path)
    try:
        for label in ("铁甲战士 · 旋风斩", "尖塔观测台 ⚔️ 中文 JSON"):
            response = client._request({"op": "metadata", "label": label})
            assert response["echo"] == label
            assert response["utf8_mode"] == 1
    finally:
        client.close()


def test_simulation_advice_precedes_model_and_records_model_selected_action(tmp_path):
    """A suggested play never replaces the model's end-turn decision or its public before frame."""
    session, bridge, provider = make_session(tmp_path)
    advice = {"scope": "current_turn_only", "first_action": {"type": "play_card", "card_index": 0}}
    session.simulator = Mock()
    session.simulator.synchronize.return_value = {"status": "disabled"}
    session.simulator.advise.return_value = {"status": "partial", "simulation_advice": advice}
    session.simulator.predict.return_value = {"status": "unsupported", "action": {"type": "end_turn"}}
    session.command("run", "resume")
    session._tick()
    assert provider.calls[0]["observation"]["simulation_advice"] == advice
    assert bridge.executions[0]["action"] == {"type": "end_turn"}
    predicted_obs, predicted_action = session.simulator.predict.call_args.args
    assert predicted_action == {"type": "end_turn"}
    assert "simulation_advice" not in predicted_obs
    events = list(read_jsonl(session._journal.directory / "transitions.jsonl"))
    submitted = next(item for item in events if item["event_type"] == "action_submitted")
    completed = next(item for item in events if item["event_type"] == "action_completed")
    assert submitted["action_id"] == completed["action_id"]
    assert submitted["bridge_identity"] == {"pid": 123, "instance_id": "offline-instance"}
    assert submitted["data"]["observation"]["state"]["turn"] == 1
    assert completed["data"]["observation"]["state"]["turn"] == 2
    assert next(item for item in events if item["event_type"] == "simulation")["decision_id"] == submitted["decision_id"]


def test_unavailable_simulator_keeps_basic_model_decision(tmp_path):
    """An optional host failure remains visible without stopping ordinary policy execution."""
    session, bridge, provider = make_session(tmp_path)
    session.simulator = Mock()
    session.simulator.synchronize.return_value = {"status": "disabled"}
    session.simulator.advise.return_value = {"status": "unavailable", "diagnostics": ["host_missing"]}
    session.simulator.predict.return_value = {"status": "unavailable"}
    session.command("run", "resume")
    session._tick()
    assert "simulation_advice" not in provider.calls[0]["observation"]
    assert len(bridge.executions) == 1
    assert session.snapshot()["simulation"]["diagnostics"] == ["host_missing"]


@pytest.mark.parametrize("pending_native", [False, True])
@pytest.mark.parametrize("changed_rule", ["executable", "tool_encoding"])
def test_restart_checks_cached_rule_revision_without_refunding_or_losing_native_work(tmp_path, pending_native, changed_rule):
    """Updated rules retire only unsubmitted proposals; an accepted native action retains its evidence."""
    initial, bridge, provider = make_session(tmp_path)
    config = initial.config
    if changed_rule == "tool_mode":
        config["provider"]["compact_simulation_tool_results"] = True
    config["simulation"] = {"enabled": True, "backend": "mentor"}
    artifact = tmp_path / HOST_MENTOR
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rules-before")
    session = DashboardSession(config, tmp_path, bridge=bridge, provider=provider)
    session.command("prepare", "redecide")
    session._tick()
    before = session.snapshot()
    assert session._cached is not None
    if pending_native:
        session._state["inflight"] = {"action_id": "already-submitted", "accepted": True}
        write_json(tmp_path / "local/pending-action.json", {"action_id": "already-submitted"})
        session._persist()
    if changed_rule == "executable":
        artifact.write_bytes(b"rules-after")
    else:
        config["provider"]["compact_simulation_tool_results"] = True
    replacement = FakeProvider()
    restored = DashboardSession(config, tmp_path, bridge=bridge, provider=replacement)
    after = restored.snapshot()
    assert after["run_id"] == before["run_id"]
    assert after["decision_episode_id"] == before["decision_episode_id"]
    assert after["decision_epoch"] == before["decision_epoch"]
    assert after["metrics"]["requests_used"] == replacement.requests_used == 1
    assert bridge.executions == [] and replacement.calls == []
    if pending_native:
        assert restored._cached is not None
        assert after["inflight"]["action_id"] == "already-submitted"
        assert json.loads((tmp_path / "local/pending-action.json").read_text())["action_id"] == "already-submitted"
    else:
        assert restored._cached is None and after["decision"] is None
        assert "decision_rules_changed" in replacement.resets


def test_new_session_archives_only_terminal_ledger_without_pending_work(tmp_path):
    """The explicit new-run operation preserves old accounting and cannot discard pending actions."""
    ledger = tmp_path / "local/dashboard-session.json"
    original = {"run_id": "finished-run", "metrics": {"requests_used": 42}}
    write_json(ledger, original)
    with pytest.raises(SessionError, match="terminal"):
        archive_terminal_session(tmp_path)
    original["terminal_evidence"] = {"victory": False}
    write_json(ledger, original)
    pending = tmp_path / "local/pending-action.json"
    write_json(pending, {"action_id": "unresolved"})
    with pytest.raises(SessionError, match="pending"):
        archive_terminal_session(tmp_path)
    pending.unlink()
    result = archive_terminal_session(tmp_path)
    assert not ledger.exists()
    assert json.loads(Path(result["archive"]).read_text()) == original


def test_jsonl_recovery_preserves_fragment_and_reads_later_records(tmp_path):
    """A crash tail is reported instead of corrupting every later event or blocking dashboard restore."""
    journal = Journal(tmp_path / "run")
    path = journal.directory / "transitions.jsonl"
    path.write_bytes(b'{"event_type":"first"}\n{"broken":')
    with pytest.warns(RuntimeWarning, match="unterminated"):
        journal.event("transitions", {"event_type": "after_restart"})
    issues = []
    records = list(read_jsonl(path, issues))
    assert [item["event_type"] for item in records] == ["first", "after_restart"]
    assert issues == [{"file": "transitions.jsonl", "line": 2, "error": "invalid_or_incomplete_jsonl_record"}]
