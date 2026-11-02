"""Check Hellraiser's finite-target domain without inventing private infinite-HP counters."""

from test_public_adapter import client, evaluate, frame, public_card
from scenarios import enemy


def hellraiser_frame(cards):
    """Represent an already applied public power with one ordinary finite-HP target."""
    state = frame(cards)
    state["combat"]["player"]["powers"] = ["HELLRAISER_POWER 1"]
    return state


def test_finite_hellraiser_does_not_block_an_unrelated_manual_play(client):
    """The infinite-target safety counter is irrelevant to an ordinary manual defense."""
    state = hellraiser_frame([public_card("DEFEND_IRONCLAD")])
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["block"] == 5


def test_hellraiser_card_applies_its_public_power(client):
    """Playing the power itself has no automatic card selection to answer."""
    state = frame([public_card("HELLRAISER", cost=2)])
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["energy"] == 1
    assert "HELLRAISER_POWER 1" in result["state"]["player"]["powers"]


def test_single_target_hellraiser_draw_is_free_and_deterministic(client):
    """A unique drawn Strike and sole target do not reveal hidden random information."""
    state = hellraiser_frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [{"line": "STRIKE_IRONCLAD [1]", "card_ids": ["STRIKE_IRONCLAD"]}]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["enemies"][0]["hp"]["current"] == 94
    assert result["state"]["player"]["energy"] == 2
    assert result["state"]["hand"] == []
    assert result["state"]["piles"]["discard"] == 2


def test_multiple_hellraiser_targets_remain_sampled_outcomes(client):
    """Distinct targets keep the exact API closed while fixed-plan evaluation still runs."""
    state = hellraiser_frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [{"line": "STRIKE_IRONCLAD [1]", "card_ids": ["STRIKE_IRONCLAD"]}]
    state["combat"]["enemies"] = [enemy(hp=70), enemy(hp=70)]
    state["combat"]["enemies"][1]["i"] = 1
    exact = client.request({"op": "simulate", "public_state": state,
                           "actions": [{"type": "play_card", "card_index": 0}]})
    assert exact.get("ok") is False
    sample = evaluate(client, state, [{"type": "play_card", "card_index": 0}])["samples"][0]
    assert sample["status"] == "completed", sample
    assert sorted(row["hp"]["current"] for row in sample["state"]["enemies"]) == [64, 70]


def test_hellraiser_does_not_invent_a_hardened_shell_cap_history(client):
    """A missing infinite-display counter remains a specific unsupported domain."""
    state = hellraiser_frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [{"line": "STRIKE_IRONCLAD [1]", "card_ids": ["STRIKE_IRONCLAD"]}]
    state["combat"]["enemies"][0]["powers"] = ["HARDENED_SHELL_POWER 10"]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok") is False
    assert "hellraiser_infinite_hp_cap_not_observed" in str(result)
