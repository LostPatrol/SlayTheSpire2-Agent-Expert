"""Keep reviewed same-act maps available across prompt and controller history resets."""

from copy import deepcopy
import json

import pytest

from sts2_agent.memory import PublicMemory
from test_provider import build, completion, public_payload
from test_session_controls import make_session


def map_observation(run_id="run", act_id="ACT_ONE"):
    """Supply a visible graph with private-looking siblings that must not enter map memory."""
    return {"run_id": run_id, "available_actions": ["choose_map_node"], "state": {
        "screen": "MAP", "run": {"act_id": act_id, "floor": 2},
        "map": {"current": "0,1", "boss_node": "3,1", "second_boss_node": None,
                "options": [{"i": 7, "line": "RestSite (1,1)"}], "hidden_seed": "PRIVATE",
                "nodes": [
                    {"coord": "0,1", "node_type": "Monster", "visited": True, "children": ["1,1"],
                     "hidden_encounter": "PRIVATE"},
                    {"coord": "1,1", "node_type": "RestSite", "visited": False, "children": ["2,1"]},
                    {"coord": "2,1", "node_type": "Unknown", "visited": False, "children": ["3,1"]},
                    {"coord": "3,1", "node_type": "Boss", "visited": False, "children": []}]},
        "raw": {"map": {"hidden": "PRIVATE"}}}}


def room_observation(run_id="run", act_id="ACT_ONE", floor=6):
    """Supply a later same-act room where the native map is not currently open."""
    return {"run_id": run_id, "available_actions": ["end_turn"], "state": {
        "screen": "COMBAT", "run": {"act_id": act_id, "floor": floor},
        "combat": {"hand": [], "player": {"energy": 3}}}}


def test_public_map_keeps_historical_coordinates_and_filters_private_siblings():
    """Retain complete approved graph fields without old legal options or hidden siblings."""
    memory = PublicMemory("run")
    original = map_observation()
    memory.observe(original)
    memory.observe(room_observation())
    stored = memory.context()["public_map"]
    assert stored["last_observed_current"] == "0,1"
    assert stored["observed_floor"] == 2
    assert stored["act_id"] == "ACT_ONE"
    assert stored["status"] == "historical_public_map"
    assert "Live observation.state.map always takes priority" in stored["usage"]
    assert "not the current position" in stored["usage"]
    assert "Unknown nodes remain unknown" in stored["usage"]
    assert stored["nodes"] == [{key: node[key] for key in ("coord", "node_type", "visited", "children")}
                              for node in original["state"]["map"]["nodes"]]
    assert "PRIVATE" not in json.dumps(stored)
    assert "options" not in stored
    original["state"]["map"]["nodes"][0]["children"].clear()
    stored["nodes"].clear()
    assert len(memory.public_map["nodes"]) == 4
    assert memory.public_map["nodes"][0]["children"] == ["1,1"]


def test_act_transition_clears_map_and_new_live_map_replaces_history():
    """Avoid carrying a prior act's route into a new act before its map is observed."""
    memory = PublicMemory("run")
    memory.observe(map_observation())
    memory.observe(room_observation(act_id="ACT_TWO"))
    assert "public_map" not in memory.context()
    latest = map_observation(act_id="ACT_TWO")
    latest["state"]["map"]["current"] = "1,1"
    memory.observe(latest)
    assert memory.context()["public_map"]["last_observed_current"] == "1,1"
    assert memory.context()["public_map"]["act_id"] == "ACT_TWO"
    with pytest.raises(ValueError, match="mix controller runs"):
        memory.remember_public_map(map_observation(run_id="another-run"))


def test_map_reaches_actual_messages_after_32_turn_provider_history_reset(monkeypatch):
    """A fresh epoch still supplies the old observed graph during a current combat room."""
    action = {"action": {"type": "end_turn"}, "reason": "Offline public map delivery test"}
    provider, requests = build(monkeypatch, [completion(json.dumps(action)) for _ in range(33)])
    provider.config["provider"].update(context_max_turns=32, context_max_characters=640000)
    memory = PublicMemory("run")
    memory.observe(map_observation())
    for index in range(33):
        current = room_observation(floor=index + 3)
        memory.observe(current)
        provider.decide(current, memory.context())
    last = public_payload(requests[-1]["messages"])
    assert provider.metadata[-1]["history_turns"] == 0
    assert "map" not in last["observation"]["state"]
    assert last["memory"]["public_map"]["nodes"] == memory.public_map["nodes"]
    assert last["memory"]["public_map"]["last_observed_current"] == "0,1"
    assert len(requests[-1]["messages"]) == 2


def test_controller_restart_restores_map_without_refunding_or_appending_events(tmp_path):
    """Persist a graph independently of Provider history while keeping the ledger unchanged."""
    session, _, _ = make_session(tmp_path)
    run_id = session._state["run_id"]
    session.memory.observe(map_observation(run_id))
    session.memory.set_plan("Keep the existing model plan")
    session._state["plan"] = "Keep the existing model plan"
    session._state["observation"] = room_observation(run_id)
    session._state["metrics"].update(requests_used=8, actions_completed=7)
    session._persist()
    events = deepcopy(session.memory.events)
    restored, _, _ = make_session(tmp_path)
    assert restored.memory.public_map == session.memory.public_map
    assert restored.memory.events == events
    assert restored._state["metrics"]["requests_used"] == 8
    assert restored._state["metrics"]["actions_completed"] == 7
    assert restored.memory.context()["plan"] == "Keep the existing model plan"


def test_old_ledger_recovers_only_same_run_public_graph_without_replaying_events(tmp_path):
    """Migrate a missing map field from reviewed journal observations, not another run."""
    session, _, _ = make_session(tmp_path)
    run_id = session._state["run_id"]
    session.memory.observe(room_observation(run_id))
    session._state["observation"] = room_observation(run_id)
    session._journal.event("public-events", {"observation": map_observation(run_id)})
    foreign = map_observation("foreign-run")
    foreign["state"]["map"]["nodes"][0]["coord"] = "99,99"
    session._journal.event("public-events", {"observation": foreign})
    session._journal.event("public-events", {"observation": room_observation(run_id)})
    session._persist()
    ledger = json.loads(session._state_file.read_text(encoding="utf-8"))
    ledger.pop("public_map")
    session._state_file.write_text(json.dumps(ledger), encoding="utf-8")
    restored, _, _ = make_session(tmp_path)
    assert restored.memory.public_map["last_observed_current"] == "0,1"
    assert len(restored.memory.public_map["nodes"]) == 4
    assert "99,99" not in json.dumps(restored.memory.public_map)
    assert restored.memory.events == session.memory.events
    assert restored._state["metrics"] == session._state["metrics"]
    restored._state["observation"] = room_observation(run_id, act_id="ACT_TWO")
    restored.memory.remember_public_map(restored._state["observation"])
    restored._persist()
    another, _, _ = make_session(tmp_path)
    assert another.memory.public_map is None
    assert another.memory.events == session.memory.events
