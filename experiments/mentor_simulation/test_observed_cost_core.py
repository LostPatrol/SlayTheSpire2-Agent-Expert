"""Check actual resource payments through the core's current public cost quotes."""

from test_public_adapter import client, frame, public_card


def test_observed_energy_total_does_not_add_tangled_twice(client):
    """An authoritative current total is charged once, including existing global modifiers."""
    state = frame([public_card("STRIKE_IRONCLAD", cost=1, target="enemies")])
    state["combat"]["player"].update(energy=1, powers=["TANGLED_POWER 2 [debuff]"])
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["energy"] == 0
    assert len(result["events"]) == 1


def test_observed_star_total_is_used_by_the_gate_and_payment(client):
    """One observed star is enough for a currently discounted two-star card."""
    card = public_card("FALLING_STAR", cost=0, target="enemies")
    card["star_cost"] = 1
    state = frame([card])
    state["run"]["character_id"] = "REGENT"
    state["combat"]["player"].update(energy=0, stars=1)
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["stars"] == 0
    assert result["state"]["player"]["energy"] == 0


def test_current_quote_does_not_survive_an_unrelated_transition(client):
    """Unknown quote lifetimes require reobservation before a later discounted play."""
    state = frame([public_card("STRIKE_IRONCLAD", index=0, target="enemies"),
                   public_card("DEFEND_IRONCLAD", index=1, cost=0)])
    state["combat"]["player"]["energy"] = 1
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0},
                    {"type": "play_card", "card_index": 1}]})
    assert result.get("ok") is False, result
    assert len(result["events"]) == 1
    assert result["state"]["player"]["energy"] == 0
    assert result["state"]["player"]["block"] == 0
    assert "cost" in str(result["errors"]).lower()
