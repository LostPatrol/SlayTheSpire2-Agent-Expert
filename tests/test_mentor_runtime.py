"""Exercise the complete upstream mirror through native search and the real JSONL client."""

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from sts2_agent.mentor_runtime import MentorRuntime
from sts2_agent.public import observation
from sts2_agent.simulation import CombatSimulator, HOST_MENTOR
import sts2_agent.simulation as simulation


# Tests may target a candidate package before the default deployment is replaced.
ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = Path(os.environ.get("MENTOR_NATIVE_DEPLOYMENT", ROOT / HOST_MENTOR)).resolve().parent
# The fixture contains actual native observations and the original upstream board exports.
FRAMES = json.loads((ROOT / "tests/fixtures/mentor_choice_native.json").read_text(encoding="utf-8"))["frames"]
pytestmark = pytest.mark.skipif(not (DEPLOYMENT / "mentor_native.pyd").is_file(),
                                reason="The native Mentor package must be built separately")


def frame(index):
    """Rename the native choice UI envelope while keeping its actual cards and board."""
    public = deepcopy(FRAMES[index]["native"]["public_state"])
    public["protocol_version"] = 1
    public["action_revision"] = index
    pending = FRAMES[index]["native"].get("pending_choice")
    if pending:
        public["screen"] = "CARD_SELECTION"
        public["selection"] = {"kind": "combat_hand_select", "underlying_screen": "COMBAT",
            "source": "FromHand", "source_model": "BURNING_PACT", "prompt_key": "BURNING_PACT",
            "min": pending["min_select"], "max": pending["max_select"], "cancelable": False,
            "requires_confirmation": True, "selected": 0, "cards": deepcopy(pending["candidates"])}
        public["available_actions"] = ["select_deck_card", "confirm_selection"]
    else:
        public["available_actions"] = ["play_card", "end_turn"]
        public["combat"]["action_readiness"] = {"can_use_combat_actions": True, "actions_settled": True,
            "snapshot_stable": True, "is_paused": False, "modal_open": False}
    return public


def request(index, public=None, completion=None):
    """Bind one actual native calibration board to the corresponding public root."""
    return {"public_state": public or frame(index), "internal_state": deepcopy(FRAMES[index]["internal"]),
            "mirror_encounter": "native-choice", "completion": completion}


def completed(identity, action, before):
    """Represent a completed controller receipt without submitting any game action."""
    return {"action_id": identity, "receipt_status": "completed", "action": action, "before_state": before}


@pytest.mark.parametrize(("monster_id", "name", "expected"), [
    ("LEAF_SLIME_M", "树叶史莱姆（中）", "LeafSlimeM"),
    ("LEAF_SLIME_M", "Nibbit", "LeafSlimeM"),
    ("LeafSlimeM", "树叶史莱姆（中）", "LeafSlimeM"),
    ("DECIMILLIPEDE_SEGMENT_FRONT", "千足虫", "DecimillipedeSegmentFront"),
    (None, "Leaf Slime (M)", "LeafSlimeM"),
])
def test_live_monster_identity_survives_localized_display_name(tmp_path, monster_id, name, expected):
    """The original two adapters keep stable species ids and preserve legacy title-only input."""
    runtime = MentorRuntime(DEPLOYMENT)
    runtime.mirror.log_path = tmp_path / "mirror.jsonl"
    board = deepcopy(FRAMES[0]["internal"]["mentor_state"])
    board["enemies"][0].update(id=monster_id, name=name)
    headless = runtime.to_headless(board)
    assert headless["enemies"][0]["id"] == monster_id
    assert headless["enemies"][0]["name"] == name
    assert runtime._wire(board)["encounter"][0]["kind"] == expected


@pytest.mark.parametrize("knowledge", [
    {"observed": True, "groups": [], "inserted": [], "discard_groups": []},
    {"observed": True, "groups": [[1]], "inserted": [0], "discard_groups": [[0]]},
])
def test_perfect_fit_certificate_survives_reconcile_and_restores_search(tmp_path, knowledge):
    """Even an exhausted Perfect Fit card needs witnessed knowledge at the public search root."""
    runtime = MentorRuntime(DEPLOYMENT)
    runtime.mirror.log_path = tmp_path / "mirror.jsonl"
    bound = request(0)
    board = bound["internal_state"]["mentor_state"]
    perfect = deepcopy(board["draw_pile"]["cards"][0])
    perfect["enchantment"] = "PERFECT_FIT"
    board["exhaust_pile"]["cards"] = [perfect]
    board["exhaust_pile"]["count"] = 1
    search = {"op": "search", "read_access": "public", "simulations": 40, "scenario_seed": 29}
    failed = runtime.handle({**search, **bound})
    assert failed["errors"] == ["mcts_search:TerminalRoot"]

    if knowledge["discard_groups"]:
        board["discard_pile"]["cards"] = [deepcopy(board["draw_pile"]["cards"][0])]
        board["discard_pile"]["count"] = 1
    board["draw_knowledge"] = deepcopy(knowledge)
    bound["public_state"]["combat"]["draw_knowledge"] = {"available": True}
    assert runtime._wire(board)["deck"]["draw_knowledge"] == knowledge
    for revision in (1, 2):
        bound["public_state"]["action_revision"] = revision
        runtime.synchronize(bound)
        before = runtime.native.state_to_dict(runtime.mirror.state)
        carried = before["deck"]["draw_knowledge"]
        for key, value in knowledge.items():
            assert carried[key] == value
        answer = runtime.handle({**search, **bound})
        assert answer["ok"] and answer["best"]["actions"], answer
        assert runtime.native.state_to_dict(runtime.mirror.state) == before

    bound["public_state"]["combat"]["draw_knowledge"]["available"] = False
    refused = runtime.handle({**search, **bound})
    assert refused["reason"] == "public_draw_knowledge_unavailable"
    internal = runtime.handle({**search, **bound, "read_access": "internal"})
    assert internal["ok"], internal


def test_draw_certificate_positions_are_rejected_if_translation_drops_a_card():
    """Soft-dropping an unknown card cannot shift a certified membership onto another card."""
    runtime = MentorRuntime(DEPLOYMENT)
    board = deepcopy(FRAMES[0]["internal"]["mentor_state"])
    board["draw_knowledge"] = {"observed": True, "groups": [[1]]}
    board["draw_pile"]["cards"][0].update(id=None, name="Unknown Fixture Card")
    with pytest.raises(ValueError, match="mirror_draw_knowledge_pile_translation_changed"):
        runtime._wire(board)


def test_choice_click_waits_for_confirmation_and_resumes_original_tail():
    """Selection UI toggles leave the truth mirror intact; confirmation advances exactly once."""
    runtime = MentorRuntime(DEPLOYMENT, strict=True)
    runtime.synchronize(request(0))
    play = completed("play", {"type": "play_card", "card_index": 0}, frame(0))
    runtime.synchronize(request(1, completion=play))
    before = runtime.native.state_to_dict(runtime.mirror.state)
    selected = frame(1)
    selected["selection"]["cards"][0]["selected"] = True
    selected["selection"]["selected"] = 1
    click = completed("click", {"type": "select_deck_card", "option_index": 0}, frame(1))
    runtime.synchronize(request(1, selected, click))
    assert runtime.native.state_to_dict(runtime.mirror.state) == before
    answer = runtime.handle({"op": "search", **request(1, selected), "read_access": "public",
                             "simulations": 40, "scenario_seed": 29})
    assert answer["ok"], answer
    assert answer["best"]["actions"][0] == {"type": "confirm_selection"}
    assert runtime.native.state_to_dict(runtime.mirror.state) == before
    confirm = completed("confirm", {"type": "confirm_selection"}, selected)
    diagnostics = runtime.synchronize(request(2, completion=confirm))
    assert diagnostics["differences"] == 0
    after = runtime.native.state_to_dict(runtime.mirror.state)
    assert after != before
    runtime.synchronize(request(2, completion=confirm))
    assert runtime.native.state_to_dict(runtime.mirror.state) == after


def test_random_prefix_is_hypothetical_and_never_changes_truth():
    """Drawing through a suspended choice must not be presented as a deterministic prediction."""
    runtime = MentorRuntime(DEPLOYMENT, strict=True)
    runtime.synchronize(request(0))
    before = runtime.native.state_to_dict(runtime.mirror.state)
    result = runtime.handle({"op": "simulate", **request(0), "read_access": "public", "scenario_seed": 29,
        "actions": [{"type": "play_card", "card_index": 0}, {"type": "select_card", "choice_index": 0}]})
    assert result["ok"], result
    assert result["hypothetical"] is True
    assert any(event["stochastic"] for event in result["events"])
    assert runtime.native.state_to_dict(runtime.mirror.state) == before


def test_packaged_client_preserves_mcts_totals_and_fixed_prefix_evaluation(tmp_path, monkeypatch):
    """Use the default production process with isolated budgets and complete native calibration."""
    monkeypatch.setattr(simulation, "HOST_MENTOR", DEPLOYMENT / "mentor_native.pyd")
    client = CombatSimulator({"simulation": {"enabled": True, "search_strength": 1,
        "timeout_seconds": 30, "read_access": "public"}}, ROOT)
    public = frame(0)
    observed = observation(public, "runtime-test", "runtime-test-1")
    try:
        client.set_internal_state(public, FRAMES[0]["internal"])
        assert client.synchronize(observed, encounter="runtime-test")["status"] == "synchronized"
        client.root = tmp_path
        assert client._request({"op": "metadata"})["protocol_version"] == 3
        advice = client.advise(observed)
        result = advice["results"]["mcts"]
        summary = advice["simulation_advice"]["strategies"]["mcts"]
        assert summary["search_mcts"] == result["mcts"]
        assert summary["mcts"] == result["best"]["mcts"]
        assert summary["search_mcts"]["visits"] == summary["mcts"]["visits"] + sum(
            candidate["mcts"]["visits"] for candidate in summary["alternatives"])
        assert "mentor_state" not in json.dumps(advice)
        evaluated = client.evaluate(observed, [{"id": "pass", "actions": [{"type": "end_turn"}]}], samples=2)
        assert evaluated["status"] == "evaluated", evaluated
        candidate = evaluated["result"]["candidates"][0]
        assert candidate["completed_samples"] == 2
        assert candidate["mcts"]["visits"] > 0
        assert evaluated["work"]["simulations_used"] > 0
    finally:
        client.close()
