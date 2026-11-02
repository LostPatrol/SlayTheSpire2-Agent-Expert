"""Focused conditional-probability and public-memory boundary checks."""

import math

import pytest

from sts2_agent.memory import PublicMemory
from sts2_agent.rules import draw_at_least_one, verify_selected_plan


def observation(energy=3, cost=1):
    """Create the actual reviewed combat shape with deliberate private distractions."""
    return {"run_id": "run-one", "observation_id": "read-one", "raw": {"seed": 42},
            "available_actions": ["play_card", "end_turn"], "state": {
                "screen": "COMBAT", "turn": 1, "seed": 42,
                "combat": {"player": {"hp": "80/80", "energy": energy, "hidden": 42},
                           "hand": [{"i": 0, "card_id": "STRIKE_IRONCLAD", "energy_cost": cost,
                                     "playable": True, "targets": [0, 1], "instance_id": "private"}],
                           "draw": [{"card_id": "SECRET_TOP"}], "hidden_order": [1, 2]},
                "run": {"floor": 1, "gold": 99, "rng": "private"}}}


def test_hypergeometric_probability_and_conditioning():
    """Known misses shrink the population; a known hit makes success certain."""
    assert draw_at_least_one(10, 2, 3)["probability"] == pytest.approx(1 - math.comb(8, 3) / math.comb(10, 3))
    assert draw_at_least_one(10, 2, 3, False)["probability"] == pytest.approx(1 - math.comb(7, 2) / math.comb(9, 2))
    assert draw_at_least_one(10, 2, 3, True)["probability"] == 1
    assert draw_at_least_one(0, 0, 0)["probability"] == 0
    assert draw_at_least_one(10, 2, 9)["probability"] == 1


@pytest.mark.parametrize("args", [(3, 4, 1), (3, 1, 4), (-1, 0, 0), (3, True, 1),
                                 (3.0, 1, 1), (3, 0, 1, True), (3, 3, 1, False),
                                 (3, 1, 0, True), (3, 1, 1, "unknown")])
def test_invalid_probability_inputs_are_unsupported(args):
    """Reject impossible conditioning instead of presenting fabricated probabilities."""
    result = draw_at_least_one(*args)
    assert result["supported"] is False and result["probability"] is None


def test_selected_action_check_is_partial_and_does_not_generate_action():
    """Only basic printed-cost arithmetic is supported, never card effect simulation."""
    action = {"type": "play_card", "card_index": 0, "target_index": 1}
    result = verify_selected_plan(observation(), action)
    assert result["status"] == "partial" and result["supported"] is False
    assert result["energy_after_cost"] == 2 and result["legal"] is True
    assert "action" not in result and "candidates" not in result
    assert verify_selected_plan(observation(energy=0), action)["legal"] is False
    assert verify_selected_plan(observation(cost=-1), action)["status"] == "unsupported"
    assert verify_selected_plan(observation(), {**action, "target_index": 8})["legal"] is False
    assert verify_selected_plan(observation(), {"type": "end_turn"})["status"] == "unsupported"


def test_memory_filters_private_fields_and_does_not_retain_order():
    """Only reviewed public facts enter memory, with no inferred card order."""
    memory = PublicMemory("run-one")
    state = observation()
    memory.observe(state, {"type": "play_card", "card_index": 0, "raw": "private"})
    context = memory.context()
    text = str(context)
    assert all(word not in text for word in ("seed", "hidden", "raw", "SECRET_TOP", "instance_id", "rng"))
    assert context["known_order"] == []
    assert context["events"][0]["source"] == "experienced_action"
    state["state"]["combat"]["player"]["energy"] = 0
    assert memory.context()["events"][0]["facts"]["player"]["energy"] == 3
    context["events"][0]["facts"]["player"]["energy"] = 88
    assert memory.context()["events"][0]["facts"]["player"]["energy"] == 3


def test_memory_ignores_read_tokens_and_injects_only_recent_events():
    """Duplicate reads do not grow history, while all meaningful events remain retained."""
    memory = PublicMemory("run-one")
    state = observation()
    memory.observe(state)
    state["observation_id"] = "read-two"
    memory.observe(state)
    assert len(memory.events) == 1
    for turn in range(2, 16):
        state["state"]["turn"] = turn
        memory.observe(state)
    assert len(memory.events) == 15
    assert len(memory.context()["events"]) == 12
    memory.set_plan("Improve draw consistency.")
    assert memory.context()["plan"] == "Improve draw consistency."
    assert memory.context()["known_order"] == []
    with pytest.raises(ValueError, match="mix"):
        memory.observe({"run_id": "another-run", "state": {}})


def test_reward_choices_change_public_history_without_private_fields():
    """Visible reward changes remain meaningful even when the screen is unchanged."""
    memory = PublicMemory("run-one")
    state = {"run_id": "run-one", "state": {"screen": "REWARD", "reward": {
        "cards": [{"i": 0, "card_id": "STRIKE", "line": "Strike", "raw": "private"}]}}}
    memory.observe(state)
    state["state"]["reward"]["cards"][0]["card_id"] = "DEFEND"
    memory.observe(state)
    assert len(memory.events) == 2
    assert "private" not in str(memory.context())
