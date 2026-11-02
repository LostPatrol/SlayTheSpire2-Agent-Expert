"""Exercise search imports through the real JSONL host, including the authorized private lane."""

from copy import deepcopy
import json

import pytest

from test_public_adapter import client, enemy, frame, public_card


# Mirror the bridge schema's complete stream set; absent cursors must fail explicitly.
STREAMS = ("up_front", "shuffle", "combat_card_generation", "combat_potion_generation",
           "combat_card_selection", "combat_energy_costs", "combat_targets", "monster_ai",
           "niche", "combat_orbs", "rewards", "shops", "transformations")


def search_frame(cards=None, *, character="IRONCLAD", enemy_hp=100):
    """Use an actual Nibbit HISS telegraph matching the pinned static AI definition."""
    state = frame(cards)
    state["run"]["character_id"] = character
    state["combat"]["players"] = [{"player_id": "fixture-player", "slot_index": 0,
                                      "is_local": True, "character_id": character}]
    state["combat"]["enemies"] = [enemy(hp=enemy_hp)]
    state["combat"]["enemies"][0]["instance_id"] = "fixture-enemy-1"
    return state


def pile_card(name, *, upgraded=False, values=None, cost=1):
    """Keep identical public instance features in grouped piles and internal order rows."""
    row = public_card(name, upgraded=upgraded, cost=cost)
    row["card_ids"] = [name]
    row["dynamic_values"] = deepcopy(values or [])
    return row


def internal_state(state, *, run_seed=777, draw=None, target_counter=0, movement="HISS_MOVE"):
    """Construct the real bridge schema without injecting private fields into the public frame."""
    counters = {name: 0 for name in STREAMS}
    counters["combat_targets"] = target_counter
    ordered = state["combat"]["draw"] if draw is None else draw
    private_cards = [{"card_id": row["card_ids"][0], "upgraded": row.get("upgraded", False),
                      "upgrade_level": int(row.get("upgraded", False)), "base_cost": row["energy_cost"],
                      "vars": {value["name"].lower(): value["base_value"] for value in row["dynamic_values"]},
                      "dynamic_values": deepcopy(row["dynamic_values"]), "affliction": row.get("affliction"),
                      "enchantment": row.get("enchantment"), "effective_keywords": row.get("effective_keywords", [])}
                     for row in ordered]
    return {"schema_version": 1, "read_access": "internal",
            "availability": {"rng": "available", "draw_order": "available", "enemy_state": "available",
                             "enemy_private_variables": "allowlist_only"},
            "rng": {"run_seed": run_seed, "counters": counters},
            "draw_order": private_cards,
            "draw_order_direction": "top_first",
            "enemies": [{"index": row["i"], "instance_id": row["instance_id"],
                         "monster_id": row["enemy_id"], "move_state": movement,
                         "next_move_id": movement, "move_history": [], "move_count": 0, "hidden_state": {}}
                        for row in state["combat"]["enemies"]],
            "unavailable_fields": ["enemy_state_machine_graph", "unlisted_enemy_private_variables"]}


def prefix(client, state, actions, *, private=None, read_access=None, seed=1771):
    """Apply exactly the declared actions before the shared MCTS continuation."""
    request = {"op": "evaluate", "rollout": True, "public_state": state,
               "candidate_plans": [{"id": "literal", "actions": actions}], "samples": 1,
               "max_simulations": 8, "max_rollout_depth": 8, "timeout_ms": 5000, "scenario_seed": seed}
    if private is not None:
        request["internal_state"] = private
    if read_access is not None:
        request["read_access"] = read_access
    result = client.request(request)
    assert result.get("ok"), result
    candidate = result["candidates"][0]
    assert candidate["completed_samples"] == 1, candidate
    return candidate["samples"][0]


def search(client, state, *, private=None, read_access="public", seed=1771):
    """Request a reproducible finite tree and retain its actual action statistics."""
    request = {"op": "search", "strategy": "mcts", "public_state": state,
               "read_access": read_access, "scenario_seed": seed, "max_simulations": 64,
               "max_rollout_depth": 24, "timeout_ms": 5000}
    if private is not None:
        request["internal_state"] = private
    return client.request(request)


def decision_statistics(result):
    """Exclude only elapsed telemetry from deterministic action and rollout comparisons."""
    assert result.get("ok"), result
    return {key: result[key] for key in ("best", "alternatives", "mcts", "support")}


def test_internal_top_first_instance_order_and_public_privacy(client):
    """A same-ID upgraded/grown instance is matched by real features, then drawn first."""
    state = search_frame([public_card("SHRUG_IT_OFF")])
    upgraded = pile_card("STRIKE_IRONCLAD", upgraded=True,
                         values=[{"name": "Damage", "base_value": 12, "current_value": 12, "enchanted_value": 12}])
    regular = pile_card("STRIKE_IRONCLAD")
    state["combat"]["draw"] = [regular, upgraded]
    original = deepcopy(state)
    left = prefix(client, state, [{"type": "play_card", "card_index": 0}],
                  private=internal_state(state, draw=[upgraded, regular]), read_access="internal")
    right = prefix(client, state, [{"type": "play_card", "card_index": 0}],
                   private=internal_state(state, draw=[regular, upgraded]), read_access="internal")
    assert left["state"]["hand"][0]["upgraded"] is True
    assert right["state"]["hand"][0]["upgraded"] is False
    assert state == original
    for row in (left, right):
        serialized = json.dumps(row["state"])
        assert "run_seed" not in serialized and "draw_order" not in serialized and "move_state" not in serialized


def test_private_order_can_change_search_but_public_fixed_seed_is_invariant(client):
    """Privilege changes the known world; a public tree ignores the entire private sibling."""
    state = search_frame([public_card("SHRUG_IT_OFF"), public_card("DEFEND_IRONCLAD", index=1)], enemy_hp=8)
    state["combat"]["player"]["energy"] = 2
    attack = pile_card("ANGER", cost=0)
    block = pile_card("DEFEND_IRONCLAD")
    state["combat"]["draw"] = [attack, block]
    first = internal_state(state, draw=[attack, block])
    second = internal_state(state, draw=[block, attack])
    public_left = search(client, state, private=first)
    public_right = search(client, state, private=second)
    assert decision_statistics(public_left) == decision_statistics(public_right)
    private_left = search(client, state, private=first, read_access="internal")
    private_right = search(client, state, private=second, read_access="internal")
    assert decision_statistics(private_left) != decision_statistics(private_right)


def test_internal_restores_native_target_rng_instead_of_scenario_seed(client):
    """Restored stream cursors decide random orb targets, while request seeds do not."""
    state = search_frame([public_card("ZAP")], character="DEFECT")
    state["combat"]["player"].update(orb_capacity=1, orb_details=[
        {"orb_id": "LIGHTNING", "passive_value": 3, "evoke_value": 8}])
    state["combat"]["enemies"] = [enemy(), enemy()]
    for index, row in enumerate(state["combat"]["enemies"]):
        row["i"] = index
        row["instance_id"] = f"fixture-enemy-{index + 1}"
    observed = []
    for cursor in range(8):
        private = internal_state(state, target_counter=cursor)
        first = prefix(client, state, [{"type": "play_card", "card_index": 0}], private=private,
                       read_access="internal", seed=10)
        second = prefix(client, state, [{"type": "play_card", "card_index": 0}], private=private,
                        read_access="internal", seed=9999)
        hp = [row["hp"]["current"] for row in first["state"]["enemies"]]
        assert hp == [row["hp"]["current"] for row in second["state"]["enemies"]]
        assert sorted(hp) == [92, 100]
        observed.append(tuple(hp))
    assert len(set(observed)) > 1
    lineages = []
    for run_seed in range(777, 785):
        sample = prefix(client, state, [{"type": "play_card", "card_index": 0}],
                        private=internal_state(state, run_seed=run_seed), read_access="internal")
        lineages.append(tuple(row["hp"]["current"] for row in sample["state"]["enemies"]))
    assert len(set(lineages)) > 1


@pytest.mark.parametrize("stream", STREAMS)
def test_internal_missing_rng_cursor_is_explicitly_rejected(client, stream):
    """A seed plus an incomplete stream block cannot become false exact restoration."""
    state = search_frame([public_card("STRIKE_IRONCLAD", target="enemies")])
    private = internal_state(state)
    del private["rng"]["counters"][stream]
    result = search(client, state, private=private, read_access="internal")
    assert result.get("ok") is False, result
    assert f"internal_rng_counter_missing:{stream}" in json.dumps(result)


@pytest.mark.parametrize("character", ["IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER"])
def test_five_character_upgraded_dynamic_strike_prefix(client, character):
    """Each character's upgraded starter deals its real visible nine damage in search import."""
    card = public_card(f"STRIKE_{character}", upgraded=True, target="enemies")
    card["dynamic_values"] = [{"name": "Damage", "base_value": 9, "current_value": 9, "enchanted_value": 9}]
    state = search_frame([card], character=character)
    sample = prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert sample["state"]["enemies"][0]["hp"]["current"] == 91
    assert sample["state"]["player"]["energy"] == 2


def test_defect_orb_capacity_queue_and_focus_survive_import(client):
    """A full two-slot queue evokes Lightning on channel and the next Frost twice."""
    state = search_frame([public_card("ZAP"), public_card("DUALCAST", index=1)], character="DEFECT")
    state["combat"]["player"].update(orb_capacity=2, powers=["FOCUS_POWER 2"], orb_details=[
        {"orb_id": "LIGHTNING", "passive_value": 5, "evoke_value": 10},
        {"orb_id": "FROST", "passive_value": 4, "evoke_value": 7}])
    sample = prefix(client, state, [{"type": "play_card", "card_index": 0},
                                     {"type": "play_card", "card_index": 1}])
    assert sample["state"]["enemies"][0]["hp"]["current"] == 90
    assert sample["state"]["player"]["block"] == 14
    assert sample["state"]["player"]["energy"] == 1


def test_osty_current_hp_and_dealer_power_survive_import(client):
    """Unleash uses imported living Osty HP and its own Strength, including the upgrade."""
    state = search_frame([public_card("UNLEASH", upgraded=True, target="enemies")], character="NECROBINDER")
    state["combat"]["player"]["pet_details"] = [{"i": 0, "pet_id": "OSTY", "current_hp": 10,
        "max_hp": 15, "block": 2, "power_details": [{"power_id": "STRENGTH_POWER", "amount": 3}]}]
    sample = prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0}])
    assert sample["state"]["enemies"][0]["hp"]["current"] == 78


def test_internal_selected_move_repairs_an_unmatched_public_telegraph(client):
    """An impossible public damage preview blocks public search but not restored native AI."""
    state = search_frame([public_card("DEFEND_IRONCLAD")])
    state["combat"]["enemies"] = [enemy(damage=999)]
    state["combat"]["enemies"][0]["instance_id"] = "fixture-enemy-1"
    public = search(client, state)
    assert public.get("ok") is False, public
    assert "public_intent_move_not_matched" in json.dumps(public)
    restored = search(client, state, private=internal_state(state), read_access="internal")
    assert restored.get("ok"), restored
    assert not restored["support"]["blocking_reasons"]


def test_native_cost_quote_pays_first_card_then_expires_for_following_cards(client):
    """The first zero quote pays zero; its sibling returns to the canonical one-energy cost."""
    state = search_frame([public_card("STRIKE_IRONCLAD", cost=0, target="enemies"),
                          public_card("DEFEND_IRONCLAD", index=1, cost=0)])
    sample = prefix(client, state, [{"type": "play_card", "card_index": 0, "target_index": 0},
                                     {"type": "play_card", "card_index": 1}])
    assert sample["state"]["player"]["energy"] == 2
    assert sample["state"]["player"]["block"] == 5
    assert sample["state"]["enemies"][0]["hp"]["current"] == 94
