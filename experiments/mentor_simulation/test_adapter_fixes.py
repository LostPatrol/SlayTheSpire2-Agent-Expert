"""Check public instance metadata and evidence-backed history at the real host boundary."""

import json

import pytest

from test_public_adapter import client, frame, public_card


def test_map_only_winged_boots_does_not_block_current_combat(client):
    """A map-travel relic must not disable supported card transitions or search."""
    state = frame([public_card("STRIKE_IRONCLAD", target="enemies")])
    state["run"].update(relic_ids=["WINGED_BOOTS"], relic_stacks=[3])
    simulated = client.request({"op": "simulate", "public_state": state,
                               "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert simulated["ok"], simulated
    assert "relic_rule_not_reviewed:WINGED_BOOTS" not in json.dumps(simulated)
    searched = client.request({"op": "search", "public_state": state, "strategy": "beam", "max_depth": 1})
    assert searched["comparison_available"], searched


def test_legacy_dynamic_keyword_preserves_factory_keywords(client):
    """A public keyword addition must not discard the card's existing Exhaust behavior."""
    card = public_card("OFFERING", cost=0)
    card["mods"] = ["Ethereal"]
    result = client.request({"op": "inspect", "public_state": frame([card])})
    observed = result["state"]["hand"][0]
    assert observed["supported"], result
    assert set(observed["effective_keywords"]) == {"Ethereal", "Exhaust"}


def test_complete_keyword_record_can_remove_a_factory_keyword(client):
    """An explicitly complete empty set can remove Exhaust from a modified instance."""
    card = public_card("OFFERING", cost=0)
    card.update(effective_keywords=[], keyword_state_complete=True)
    result = client.request({"op": "simulate", "public_state": frame([card]),
                             "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["piles"]["exhaust"] == 0
    assert result["state"]["piles"]["discard"] == 1


def test_legacy_sharp_uses_enchanted_base_delta_without_doubling_strength(client):
    """The old displayed delta identifies Sharp independently of powered current damage."""
    card = public_card("STRIKE_IRONCLAD", target="enemies")
    card.update(mods=["Enchantment", "锋利"], dynamic_values=[
        {"name": "Damage", "base_value": 6, "enchanted_value": 8, "current_value": 11}])
    state = frame([card])
    state["combat"]["player"]["powers"] = ["STRENGTH_POWER 3"]
    state["combat"]["enemies"][0]["hp"] = "100/100"
    inspected = client.request({"op": "inspect", "public_state": state})
    assert "legacy_sharp" in json.dumps(inspected["state"]["hand"][0]["import_notes"])
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0, "target_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["enemies"][0]["hp"]["current"] == 89


@pytest.mark.parametrize("vital_spark", [False, True])
def test_legacy_tainted_recovers_only_the_explicit_displayed_amount(client, vital_spark):
    """Tainted identifies the card; a living VitalSpark owner supplies the actual reaction."""
    card = public_card("BLOODLETTING", cost=0)
    card["line"] = "放血 [0费]：失去3点生命。 获得[blue]2[/blue]层污染。"
    state = frame([card])
    if vital_spark:
        state["combat"]["enemies"][0]["powers"] = ["VITAL_SPARK_POWER 2"]
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    if vital_spark:
        assert "TAINTED_POWER 2 [debuff]" in result["state"]["player"]["powers"]
    else:
        assert not any("TAINTED_POWER" in power for power in result["state"]["player"]["powers"])


@pytest.mark.parametrize("kind", ["sharp", "tainted"])
def test_ambiguous_legacy_metadata_remains_a_local_gap(client, kind):
    """A named modification with insufficient or conflicting quantities is never guessed."""
    card = public_card("STRIKE_IRONCLAD", target="enemies")
    if kind == "sharp":
        card.update(mods=["Enchantment", "锋利"], dynamic_values=[
            {"name": "Damage", "base_value": 6, "current_value": 8}])
    else:
        card = public_card("BLOODLETTING", cost=0)
        card["line"] = "获得2层污染。 获得3层污染。"
    inspected = client.request({"op": "inspect", "public_state": frame([card])})
    assert inspected.get("ok"), inspected
    assert inspected["state"]["hand"][0]["supported"] is False


@pytest.mark.parametrize("used,expected_block", [(False, 7), (True, 0)])
def test_permafrost_uses_evidenced_combat_history(client, used, expected_block):
    """An observed use disables the latch, while complete evidence can establish an unused latch."""
    state = frame([public_card("INFLAME")])
    state["run"].update(relic_ids=["PERMAFROST"], relic_stacks=[None])
    state["public_history"] = {"schema_version": 1, "history_complete": True,
                                "permafrost_first_power_used": used}
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["block"] == expected_block


def test_permafrost_never_infers_unused_from_a_zero_turn_count(client):
    """Starting observation mid-combat cannot establish that a once-per-combat effect is unused."""
    state = frame([public_card("INFLAME")])
    state["run"].update(relic_ids=["PERMAFROST"], relic_stacks=[None])
    state["public_history"] = {"schema_version": 1, "history_complete": False,
                                "permafrost_first_power_used": False}
    state["combat"]["player"]["cards_played_this_turn"] = 0
    result = client.request({"op": "simulate", "public_state": state,
        "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok") is False
    assert "permafrost_first_power_latch_not_observed" in json.dumps(result)


def test_native_no_star_cost_sentinel_is_not_an_unobserved_discount(client):
    """Ordinary native cards report -1 for absent star cost rather than a negative modifier."""
    card = public_card("DEFEND_IRONCLAD")
    card["star_cost"] = -1
    result = client.request({"op": "simulate", "public_state": frame([card]),
        "actions": [{"type": "play_card", "card_index": 0}]})
    assert result.get("ok"), result
    assert result["state"]["player"]["block"] == 5
