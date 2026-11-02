"""Check imported current-turn rules at the production host boundary."""

from test_public_adapter import client, frame, public_card
from scenarios import enemy


def test_scarf_free_fifth_play_expires_in_the_same_continuation(client):
    """The fifth card is free; the sixth copy regains its ordinary energy cost."""
    state = frame([public_card("DEFEND_IRONCLAD", index=3, cost=0),
                   public_card("DEFEND_IRONCLAD", index=7, cost=0)])
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4])
    state["combat"]["player"].update(energy=2, cards_played_this_turn=4,
        attacks_played_this_turn=0, skills_played_this_turn=4)
    fifth = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 3}]})
    assert fifth.get("ok"), fifth
    assert fifth["state"]["player"]["energy"] == 2
    assert fifth["state"]["hand"][0]["cost"] == 1
    sixth = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 3},
                    {"type": "play_card", "card_index": 7}]})
    assert sixth.get("ok"), sixth
    assert sixth["state"]["player"]["energy"] == 1
    assert sixth["state"]["player"]["block"] == 10


def test_scarf_does_not_repeat_on_the_tenth_card(client):
    """Scarf gives one free fifth card each turn, rather than every fifth card."""
    state = frame([public_card("DEFEND_IRONCLAD")])
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[9])
    state["combat"]["player"].update(energy=2, cards_played_this_turn=9,
        attacks_played_this_turn=0, skills_played_this_turn=9)
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["energy"] == 1


def test_scarf_fifth_and_sixth_star_payments_use_prior_play_count(client):
    """Capture star cost before the current play increments first-in-series history."""
    cards = [public_card("FALLING_STAR", index=index, cost=0, target="enemies")
             for index in (3, 7)]
    state = frame(cards)
    state["run"].update(character="REGENT", relic_ids=["BRILLIANT_SCARF"], relic_stacks=[4])
    state["combat"]["player"].update(energy=3, stars=4, cards_played_this_turn=4,
        attacks_played_this_turn=4, skills_played_this_turn=0)
    fifth = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 3, "target_index": 0}]})
    assert fifth.get("ok"), fifth
    assert fifth["state"]["player"]["stars"] == 4
    sixth = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 3, "target_index": 0},
                    {"type": "play_card", "card_index": 7, "target_index": 0}]})
    assert sixth.get("ok"), sixth
    assert sixth["state"]["player"]["stars"] == 2


def test_plow_threshold_reaction_without_constructing_monster_ai(client):
    """Existing Plow damage hooks also run on a directly imported public enemy."""
    state = frame([public_card("STRIKE_IRONCLAD", target="enemies")])
    beast = enemy(hp=153, powers=["PLOW_POWER 150 [debuff]", "STRENGTH_POWER 2"],
                  enemy_id="CEREMONIAL_BEAST")
    beast["hp"] = "153/252"
    state["combat"]["enemies"] = [beast]
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok"), result
    observed = result["state"]["enemies"][0]
    assert observed["hp"]["current"] == 147
    assert observed["powers"] == []


def test_curl_up_damage_reaction_without_constructing_monster_ai(client):
    """A registered monster power reacts after its first powered card resolves."""
    state = frame([public_card("STRIKE_IRONCLAD", target="enemies")])
    state["combat"]["enemies"] = [enemy(hp=60, powers=["CURL_UP_POWER 14"],
        enemy_id="LOUSE_PROGENITOR")]
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok"), result
    observed = result["state"]["enemies"][0]
    assert observed["hp"]["current"] == 54
    assert observed["block"] == 14
    assert observed["powers"] == []
