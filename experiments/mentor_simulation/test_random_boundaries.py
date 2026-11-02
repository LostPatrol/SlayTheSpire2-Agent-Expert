"""Prove unique public draw outcomes while keeping distinct card instances uncertain."""

from test_public_adapter import client, frame, public_card


def test_identical_public_draw_cards_have_a_unique_exact_outcome(client):
    """A homogeneous public multiset yields the same drawn card under every ordering."""
    state = frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [{"line": "DEFEND_IRONCLAD*3 [1]", "card_ids": ["DEFEND_IRONCLAD"]}]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["block"] == 8
    assert [row["card_id"] for row in result["state"]["hand"]] == ["DEFEND_IRONCLAD"]
    assert result["state"]["piles"]["draw"] == 2


def test_identical_discard_reshuffle_has_a_unique_exact_outcome(client):
    """A shuffle consumes RNG without changing a multiset of identical instances."""
    state = frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["discard"] = [{"line": "DEFEND_IRONCLAD*3 [1]", "card_ids": ["DEFEND_IRONCLAD"]}]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert [row["card_id"] for row in result["state"]["hand"]] == ["DEFEND_IRONCLAD"]
    assert result["state"]["piles"] == {"draw": 2, "discard": 1, "exhaust": 0}


def test_different_upgrade_instances_do_not_become_one_exact_draw(client):
    """Canonical IDs alone do not prove equality when one physical card is upgraded."""
    state = frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [
        {"line": "DEFEND_IRONCLAD [1]", "card_ids": ["DEFEND_IRONCLAD"], "upgraded": False},
        {"line": "DEFEND_IRONCLAD+ [1]", "card_ids": ["DEFEND_IRONCLAD"], "upgraded": True},
    ]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok") is False
    assert not result.get("events")


def test_mixed_draw_remains_unknown_even_if_one_sample_happens_to_match(client):
    """A sampled representative never proves that two distinct cards are interchangeable."""
    state = frame([public_card("SHRUG_IT_OFF")])
    state["combat"]["draw"] = [
        {"line": "STRIKE_IRONCLAD [1]", "card_ids": ["STRIKE_IRONCLAD"]},
        {"line": "DEFEND_IRONCLAD [1]", "card_ids": ["DEFEND_IRONCLAD"]},
    ]
    result = client.request({"op": "simulate", "public_state": state,
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok") is False
    assert not result.get("events")
