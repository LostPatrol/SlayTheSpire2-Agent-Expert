"""Offline guarded-execution, public projection, and action-handle safety checks."""

from copy import deepcopy
import json

import httpx
import pytest

from sts2_agent.bridge import Bridge, BridgeError
from sts2_agent.public import ActionError, observation, validate_action


# Tests use only an in-process mock; no game or remote endpoint is contacted.
BRIDGE_CONFIG = {"bridge": {
    "base_url": "http://127.0.0.1:18765", "expected_pid": 123,
    "expected_game_version": "v0.107.1", "require_atomic": True,
}}


def envelope(data: dict, status: int = 200) -> httpx.Response:
    """Wrap one mocked bridge payload using its actual envelope contract."""
    return httpx.Response(status, json={"ok": True, "data": data})


def health(instance: str = "instance-one") -> dict:
    """Return the minimum correct health identity and fair protocol flags."""
    return {"process_id": 123, "game_version": "v0.107.1", "instance_role": "human",
            "play_running": False, "external_protocol": {
                "version": 1, "action_receipts": True, "instance_id": instance}}


def combat() -> dict:
    """Offer a legal zero-cost play while the player's energy is zero."""
    return {"protocol_version": 1, "supported": True, "screen": "COMBAT", "turn": 1,
            "available_actions": ["play_card", "end_turn", "save_and_quit", "continue_run"],
            "combat": {"action_readiness": {"actions_settled": True, "snapshot_stable": True,
                                              "can_use_combat_actions": True}, "player": {"energy": 0},
                       "hand": [{"i": 0, "card_id": "ZERO_COST", "energy_cost": 0,
                                 "playable": True, "targets": [0]},
                                {"i": 1, "card_id": "UNPLAYABLE", "energy_cost": 1,
                                 "playable": False, "targets": [0]}]}}


def bridge_with(handler) -> Bridge:
    """Replace the bridge HTTP client before any request reaches the network."""
    bridge = Bridge(deepcopy(BRIDGE_CONFIG))
    bridge.client.close()
    bridge.client = httpx.Client(base_url=BRIDGE_CONFIG["bridge"]["base_url"],
                                 transport=httpx.MockTransport(handler), trust_env=False)
    return bridge


def test_post_transport_loss_reads_receipt_without_repeating_post():
    """An accepted action with a lost response is reconciled by GET only."""
    calls = []

    def handle(request):
        """Simulate losing the action response after the native action occurred."""
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return envelope(health())
        if request.method == "POST":
            command = json.loads(request.content)
            assert command == {"action": "play_card", "card_index": 0, "target_index": 0,
                               "action_id": "action-one", "expected_observation_token": "token-one"}
            raise httpx.ReadTimeout("Response was lost after acceptance.")
        assert request.url.path == "/action-receipt"
        assert request.url.params["action_id"] == "action-one"
        return envelope({"status": "completed", "stable": True, "raw_state": {"seed": 42},
                         "public_state": combat(), "public_observation_token": "token-two"})

    bridge = bridge_with(handle)
    action = {"type": "play_card", "card_index": 0, "target_index": 0}
    result = bridge.execute(action, "action-one", "token-one")
    assert calls.count(("POST", "/action")) == 1
    assert calls == [("GET", "/health"), ("POST", "/action"),
                     ("GET", "/health"), ("GET", "/action-receipt")]
    assert result["status"] == "completed" and "raw_state" not in result
    assert action == {"type": "play_card", "card_index": 0, "target_index": 0}


def test_lost_response_and_changed_instance_stop_before_receipt():
    """Process restart loses in-memory receipts and must never cause action resubmission."""
    calls = []
    health_reads = 0

    def handle(request):
        """Return a restarted bridge instance after the action response is lost."""
        nonlocal health_reads
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            health_reads += 1
            return envelope(health("instance-one" if health_reads == 1 else "instance-two"))
        assert request.method == "POST"
        raise httpx.ReadTimeout("lost")

    bridge = bridge_with(handle)
    with pytest.raises(BridgeError, match="bridge_process_changed_receipts_lost"):
        bridge.execute({"type": "end_turn"}, "action-one", "token-one")
    assert calls.count(("POST", "/action")) == 1
    assert not any(path == "/action-receipt" for _, path in calls)


def test_stale_observation_rejection_is_not_retried_or_replaced():
    """The bridge's atomic token rejection leaves the model's action unchanged."""
    commands = []

    def handle(request):
        """Reject the one submitted command with a stale-token protocol error."""
        if request.url.path == "/health":
            return envelope(health())
        commands.append(json.loads(request.content))
        return httpx.Response(409, json={"ok": False, "error": {"code": "stale_observation"}})

    bridge = bridge_with(handle)
    action = {"type": "play_card", "card_index": 0, "target_index": 0}
    with pytest.raises(BridgeError, match="stale_observation"):
        bridge.execute(action, "action-one", "expired")
    assert len(commands) == 1 and commands[0]["card_index"] == 0
    assert action["card_index"] == 0


def test_snapshot_discards_raw_siblings_and_copies_public_state():
    """Only public_state and its public observation token leave snapshot()."""
    payload = {"public_state": combat(), "public_observation_token": "token-one",
               "raw": {"seed": 42}, "compact": {"hidden": "private"}, "rng": "private"}
    bridge = bridge_with(lambda request: envelope(payload))
    state, token = bridge.snapshot()
    assert state == payload["public_state"] and token == "token-one"
    assert "raw" not in state and "rng" not in state and "compact" not in state
    state["combat"]["player"]["energy"] = 99
    assert payload["public_state"]["combat"]["player"]["energy"] == 0


def test_unknown_screen_is_rejected_by_settled(monkeypatch):
    """A screen outside the bridge whitelist cannot become a decision point."""
    monkeypatch.setattr("sts2_agent.bridge.time.sleep", lambda delay: None)
    bridge = bridge_with(lambda request: envelope({
        "public_state": {"protocol_version": 1, "supported": False, "screen": "TIMELINE",
                         "available_actions": []}, "public_observation_token": "token-one"}))
    with pytest.raises(BridgeError, match="unsupported_public_screen:TIMELINE"):
        bridge.settled(timeout=1)


def test_scene_loading_waits_without_exposing_unknown_state(monkeypatch):
    """A transient loading frame is waited out before the next public decision."""
    monkeypatch.setattr("sts2_agent.bridge.time.sleep", lambda delay: None)
    states = iter([
        {"protocol_version": 1, "supported": False, "screen": "UNKNOWN", "available_actions": []},
        combat(), combat(),
    ])
    bridge = bridge_with(lambda request: envelope({
        "public_state": next(states), "public_observation_token": "next-frame"}))
    state, _ = bridge.settled(timeout=1)
    assert state["screen"] == "COMBAT"


def test_settled_requires_two_equal_settled_public_frames(monkeypatch):
    """A changed public token requires a second equal frame before returning."""
    monkeypatch.setattr("sts2_agent.bridge.time.sleep", lambda delay: None)
    tokens = iter(["first", "second", "second"])
    reads = []

    def handle(request):
        """Supply one changing state followed by two stable public frames."""
        token = next(tokens)
        reads.append(token)
        return envelope({"public_state": combat(), "public_observation_token": token})

    bridge = bridge_with(handle)
    _, token = bridge.settled(timeout=1)
    assert reads == ["first", "second", "second"] and token == "second"


def test_end_turn_transition_waits_for_native_player_gate(monkeypatch):
    """A settled queue alone does not authorize decisions during enemy resolution."""
    monkeypatch.setattr("sts2_agent.bridge.time.sleep", lambda delay: None)
    transitioning = combat()
    transitioning["available_actions"] = ["save_and_quit"]
    transitioning["combat"]["action_readiness"].update(
        snapshot_stable=False, can_use_combat_actions=False)
    states = iter([transitioning, transitioning, combat(), combat()])
    bridge = bridge_with(lambda request: envelope({
        "public_state": next(states), "public_observation_token": "frame"}))
    state, _ = bridge.settled(timeout=1)
    assert state["combat"]["action_readiness"]["can_use_combat_actions"] is True
    assert bridge.last_settle_metrics["polls"] == 4
    assert bridge.last_settle_metrics["not_ready_polls"] == 2
    assert bridge.last_settle_metrics["settled"] is True


@pytest.mark.parametrize("screen,action", [("CARD_SELECTION", "select_deck_card"),
                                           ("MODAL", "confirm_modal")])
def test_choice_screens_settle_while_combat_is_waiting(monkeypatch, screen, action):
    """A native action waiting for a choice must not block the choice's own screen."""
    monkeypatch.setattr("sts2_agent.bridge.time.sleep", lambda delay: None)
    choice = combat()
    choice["screen"] = screen
    choice["available_actions"] = [action]
    choice["combat"]["action_readiness"].update(
        actions_settled=False, snapshot_stable=False, can_use_combat_actions=False,
        player_action_phase=False)
    bridge = bridge_with(lambda request: envelope({
        "public_state": choice, "public_observation_token": "choice-frame"}))
    state, _ = bridge.settled(timeout=1)
    assert state["screen"] == screen
    assert bridge.last_settle_metrics["polls"] == 2
    assert bridge.last_settle_metrics["not_ready_polls"] == 0


def test_unready_interruption_requires_a_new_settled_pair(monkeypatch):
    """Even an unchanged token cannot bridge an intervening unready native frame."""
    monkeypatch.setattr("sts2_agent.bridge.time.sleep", lambda delay: None)
    unready = combat()
    unready["combat"]["action_readiness"]["snapshot_stable"] = False
    states = iter([combat(), unready, combat(), combat()])
    bridge = bridge_with(lambda request: envelope({
        "public_state": next(states), "public_observation_token": "same-token"}))
    before = deepcopy(combat())
    state, _ = bridge.settled(timeout=1)
    assert state == before
    assert bridge.last_settle_metrics["polls"] == 4
    assert bridge.last_settle_metrics["not_ready_polls"] == 1
    assert bridge.last_settle_metrics["token_changes"] == 0
    assert bridge.last_settle_metrics["elapsed_ms"] >= 0
    assert bridge.last_snapshot_metrics["elapsed_ms"] >= 0
    assert "elapsed_ms" not in state


def test_settled_records_failed_wait_without_public_state_mutation(monkeypatch):
    """A failed wait keeps diagnostics separate from its public observation contract."""
    clock = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr("sts2_agent.bridge.time.monotonic", lambda: next(clock))
    bridge = bridge_with(lambda request: envelope({}))
    with pytest.raises(BridgeError, match="settled_state_timeout"):
        bridge.settled(timeout=1)
    assert bridge.last_settle_metrics == {
        "polls": 0, "not_ready_polls": 0, "token_changes": 0,
        "settled": False, "elapsed_ms": 2000.0}


def test_nosl_observation_hides_reload_controls_and_keeps_zero_cost_action():
    """NoSL exposes neither save/quit nor continue_run while allowing normal zero-cost play."""
    state = combat()
    obs = observation(state, "run-one", "episode-one")
    assert "save_and_quit" not in obs["available_actions"]
    assert "continue_run" not in obs["available_actions"]
    assert obs["state"]["available_actions"] == obs["available_actions"]
    validate_action(obs, {"type": "play_card", "card_index": 0, "target_index": 0})
    assert "save_and_quit" in state["available_actions"]


@pytest.mark.parametrize("action", [
    {"type": "play_card", "card_index": 0, "target_index": 7},
    {"type": "play_card", "card_index": 9, "target_index": 0},
    {"type": "play_card", "card_index": 1, "target_index": 0},
    {"type": "play_card", "card_index": 0, "target_index": True},
    {"type": "save_and_quit"},
    {"type": "continue_run"},
])
def test_illegal_actions_are_rejected_without_mutation(action):
    """Bad targets, disabled cards, stale handles, and reloads receive no substitute."""
    obs = observation(combat(), "run-one", "episode-one")
    original = deepcopy(action)
    with pytest.raises(ActionError):
        validate_action(obs, action)
    assert action == original


def test_selection_cards_use_option_index_namespace():
    """The native select_deck_card handler consumes option_index, not card_index."""
    obs = observation({"screen": "CARD_SELECTION", "available_actions": ["select_deck_card"],
                       "selection": {"cards": [{"i": 2, "card_id": "STRIKE"}]}},
                      "run-one", "episode-one")
    assert "option_index" in obs["action_parameters"]["select_deck_card"]
    validate_action(obs, {"type": "select_deck_card", "option_index": 2})
    with pytest.raises(ActionError):
        validate_action(obs, {"type": "select_deck_card", "card_index": 2})
