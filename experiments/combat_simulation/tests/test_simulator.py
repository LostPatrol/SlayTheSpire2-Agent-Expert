"""Check the isolated adapter's numeric rules, public boundary, and search completeness."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

# Import the experiment without adding it to the production package or its entrypoints.
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))
from run import HOST_DLL, SimulatorClient
from scenarios import enemy, position


@pytest.fixture(scope="module")
def client():
    """Share one already built offline host across behavioral checks."""
    if not HOST_DLL.exists():
        pytest.fail("Build the isolated host with run.py build before testing")
    with SimulatorClient() as simulator:
        yield simulator


def simulate(client, state, indices):
    """Play a sequence by stable original hand handles against the first enemy."""
    hand = state["combat"]["hand"]
    actions = []
    for index in indices:
        action = {"type": "play_card", "card_handle": f"h{index}"}
        if hand[index]["target"] == "enemies":
            action["target_index"] = 0
        actions.append(action)
    return client.request({
        "op": "simulate", "public_state": state,
        "actions": actions, "seed": 0,
    })


def test_strength_order_and_no_double_preview_bonus(client):
    """Two base-6 attacks gain the setup's +2 once each, even when previews are dynamic."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD", "INFLAME"])
    before = simulate(client, state, [2, 0, 1])
    after = simulate(client, state, [0, 1, 2])
    assert before["ok"] and after["ok"]
    assert before["state"]["enemies"][0]["hp"]["current"] == 84
    assert after["state"]["enemies"][0]["hp"]["current"] == 88
    state = position(["STRIKE_IRONCLAD"], powers=["STRENGTH_POWER 3"])
    state["combat"]["hand"][0]["line"] = "打击 [1费]：造成9点伤害。"
    preview = simulate(client, state, [0])
    assert preview["ok"]
    assert preview["state"]["enemies"][0]["hp"]["current"] == 91


def test_soar_uses_power_adjusted_damage_once_and_rounds_each_hit(client):
    """Flying targets halve powered hits after Strength and Vulnerable, before each truncation."""
    state = position(["TWIN_STRIKE"], powers=["STRENGTH_POWER 10"],
                     enemies=[enemy(powers=["SOAR_POWER 1", "VULNERABLE_POWER 2"], enemy_id="OWL_MAGISTRATE")])
    state["combat"]["hand"][0]["line"] = "Twin Strike [1]: Deal 15 damage twice."
    result = simulate(client, state, [0])
    assert result["ok"]
    assert result["state"]["enemies"][0]["hp"]["current"] == 78


@pytest.mark.parametrize("target_hp,expected_debuffs", [(15, []), (40, ["VULNERABLE_POWER 3 [debuff]", "WEAK_POWER 1 [debuff]"])])
def test_uppercut_debuff_stays_on_aimed_creature_after_lethal_damage(client, target_hp, expected_debuffs):
    """Killing the first target must not transfer Uppercut's debuffs to the next living enemy."""
    state = position(["UPPERCUT"], energy=2,
                     enemies=[enemy(hp=target_hp, powers=["VULNERABLE_POWER 2"]), enemy(hp=33)])
    result = simulate(client, state, [0])
    assert result["ok"]
    targets = result["state"]["enemies"]
    assert targets[1]["hp"]["current"] == 33
    assert targets[1]["powers"] == []
    if target_hp == 15:
        assert targets[0]["alive"] is False
    else:
        assert sorted(targets[0]["powers"]) == expected_debuffs


@pytest.mark.parametrize("vigor,remaining", [(0, 88), (8, 80)])
def test_akabeko_imports_observed_vigor_without_replaying_combat_start(client, vigor, remaining):
    """Current Vigor carries Akabeko's bonus and is consumed by the first attack only."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"],
                     powers=[f"VIGOR_POWER {vigor}"] if vigor else [])
    state["run"].update(relic_ids=["AKABEKO"], relic_stacks=[None])
    result = simulate(client, state, [0, 1])
    assert result["ok"]
    assert result["state"]["enemies"][0]["hp"]["current"] == remaining


def test_settled_turn_and_rest_relics_do_not_duplicate_resources(client):
    """Importing turn-start energy/block and noncombat healing must preserve current resources."""
    state = position(["STRIKE_IRONCLAD"], energy=4, block=14)
    state["turn"] = 3
    state["run"].update(relic_ids=["PAELS_FLESH", "HORN_CLEAT", "REGAL_PILLOW"], relic_stacks=[None] * 3)
    result = simulate(client, state, [0])
    assert result["ok"]
    assert result["state"]["player"]["energy"] == 3
    assert result["state"]["player"]["block"] == 14
    assert result["state"]["player"]["hp"]["current"] == 80


def test_iron_club_imports_public_run_counter_and_stops_before_unknown_draw(client):
    """Two non-triggering plays are comparable; the fourth-card draw cannot be projected."""
    state = position(["STRIKE_IRONCLAD"] * 3)
    state["run"].update(relic_ids=["IRON_CLUB"], relic_stacks=[1])
    result = simulate(client, state, [0, 1, 2])
    assert not result["ok"]
    assert len(result["events"]) == 2
    assert result["state"]["enemies"][0]["hp"]["current"] == 88
    assert "boundary" in str(result["errors"])
    state["run"]["relic_stacks"] = [None]
    missing = simulate(client, state, [0])
    assert not missing["ok"]
    assert "iron_club_run_counter_not_observed" in str(missing["errors"])


def test_rampage_imports_per_copy_base_growth_without_reusing_power_adjusted_preview(client):
    """Each public base maps to the core's actual growth field before applying Strength."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], powers=["STRENGTH_POWER 3"])
    for card, base in zip(state["combat"]["hand"], (14, 9)):
        card.update(card_id="RAMPAGE", line=f"Rampage [1]: Deal {base + 3} damage.",
                    dynamic_values=[{"name": "Damage", "base_value": base,
                                     "current_value": base + 3, "enchanted_value": base}])
    grown = simulate(client, state, [0])
    original = simulate(client, state, [1])
    assert grown["ok"] and original["ok"]
    assert grown["state"]["enemies"][0]["hp"]["current"] == 83
    assert original["state"]["enemies"][0]["hp"]["current"] == 88


@pytest.mark.parametrize("name,base,enchanted", [("Damage", 10, 10), ("Block", 5, 8)])
def test_unmapped_instance_values_block_only_affected_card(client, name, base, enchanted):
    """Unsupported per-copy changes cannot be labelled exact or hide valid sibling cards."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"])
    state["combat"]["hand"][0]["dynamic_values"] = [{
        "name": name, "base_value": base, "current_value": enchanted, "enchanted_value": enchanted,
    }]
    result = client.request({"op": "inspect", "public_state": state})
    assert result["support"]["level"] == "partial"
    assert "card_dynamic_" in str(result["support"]["reasons"])
    assert [action["card_handle"] for action in result["legal_actions"]
            if action["type"] == "play_card"] == ["h1"]
    assert not simulate(client, state, [0])["ok"]


def test_modified_power_amount_is_not_silently_replaced_by_canonical_amount(client):
    """A changed public scalar must be rejected when the fixed core hardcodes its value."""
    state = position(["INFLAME", "STRIKE_IRONCLAD"])
    state["combat"]["hand"][0]["dynamic_values"] = [{
        "name": "StrengthPower", "base_value": 4, "current_value": 4, "enchanted_value": 4,
    }]
    result = client.request({"op": "inspect", "public_state": state})
    assert "card_dynamic_base_not_imported:StrengthPower:2->4" in str(result["support"])
    assert [a["card_handle"] for a in result["legal_actions"] if a["type"] == "play_card"] == ["h1"]


def test_all_visible_attack_groups_react_to_debuffs_and_confirmed_stun(client):
    """Sum separate declared attacks and cancel the group after confirmed shield-break stun."""
    state = position(["UPPERCUT"], energy=2, enemies=[enemy(damage=6)])
    state["combat"]["enemies"][0]["intents"].append({
        "i": 1, "intent_type": "Attack", "damage": 7, "hits": 2, "total_damage": 14,
    })
    initial = client.request({"op": "inspect", "public_state": state})
    assert initial["state"]["projected_incoming_damage"] == 20
    weakened = simulate(client, state, [0])
    assert weakened["ok"]
    assert weakened["state"]["projected_incoming_damage"] == 14
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], energy=2,
                     enemies=[enemy(block=10, damage=6, powers=["BURROWED_POWER 1"], enemy_id="TUNNELER")])
    state["combat"]["enemies"][0]["intents"].append({
        "i": 1, "intent_type": "Attack", "damage": 7, "hits": 1, "total_damage": 7,
    })
    assert simulate(client, state, [0, 1])["state"]["projected_incoming_damage"] == 0


def test_vulnerable_and_multi_hit_slippery(client):
    """Check debuff order and per-hit consumption using explicit printed card effects."""
    state = position(["STRIKE_IRONCLAD", "BASH"])
    assert simulate(client, state, [1, 0])["state"]["enemies"][0]["hp"]["current"] == 83
    assert simulate(client, state, [0, 1])["state"]["enemies"][0]["hp"]["current"] == 86
    slippery = position(["TWIN_STRIKE", "STRIKE_IRONCLAD"], energy=2,
                        enemies=[enemy(powers=["SLIPPERY_POWER 2"])])
    result = simulate(client, slippery, [0, 1])
    assert result["ok"]
    assert result["state"]["enemies"][0]["hp"]["current"] == 92


def test_handles_do_not_shift_after_a_card_or_enemy_is_removed(client):
    """Frame-local indices become stable synthetic identities inside a branch."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], energy=2,
                     enemies=[enemy(hp=6), enemy(hp=10)])
    result = client.request({
        "op": "simulate", "public_state": state, "actions": [
            {"type": "play_card", "card_handle": "h0", "target_index": 0},
            {"type": "play_card", "card_handle": "h1", "target_index": 1},
        ],
    })
    assert result["ok"]
    alive = [item for item in result["state"]["enemies"] if item["alive"]]
    assert len(alive) == 1 and alive[0]["hp"]["current"] == 4


def test_burrowed_break_cancels_the_declared_attack_projection(client):
    """A confirmed shield-break stun must influence planning as well as immediate damage."""
    state = position(["DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], energy=2,
                     enemies=[enemy(block=10, damage=23, powers=["BURROWED_POWER 1"], enemy_id="TUNNELER")])
    result = simulate(client, state, [1, 2])
    assert result["ok"]
    assert result["state"]["enemies"][0]["hp"]["current"] == 98
    assert result["state"]["projected_incoming_damage"] == 0


def test_unknown_draw_is_an_explicit_boundary(client):
    """The host must decline a future draw instead of returning an invented known hand."""
    state = position(["SHRUG_IT_OFF", "STRIKE_IRONCLAD"])
    result = simulate(client, state, [0])
    assert not result["ok"]
    assert result["support"]["level"] != "exact"
    assert result["errors"]
    assert result["state"]["player"]["energy"] == 3


@pytest.mark.parametrize("relic", ["VENERABLE_TEA_SET", "RED_MASK", "PUMPKIN_CANDLE",
    "MERCURY_HOURGLASS", "JUZU_BRACELET", "MANGO", "LASTING_CANDY", "BELLOWS", "MEAT_ON_THE_BONE"])
def test_settled_or_outside_horizon_relics_do_not_block_current_turn(client, relic):
    """Import current resources without replaying start, room, or combat-end relic triggers."""
    state = position(["STRIKE_IRONCLAD"], energy=2)
    state["run"].update(relic_ids=[relic], relic_stacks=[4])
    result = simulate(client, state, [0])
    assert result["ok"]
    assert result["state"]["player"]["energy"] == 1
    assert result["state"]["enemies"][0]["hp"]["current"] == 94
    assert result["state"]["enemies"][0]["powers"] == []


def test_brilliant_scarf_requires_public_count_and_frees_only_fifth_card(client):
    """The observed card-play count determines future costs without guessing history."""
    state = position(["STRIKE_IRONCLAD", "STRIKE_IRONCLAD"], energy=2)
    state["run"].update(relic_ids=["BRILLIANT_SCARF"], relic_stacks=[0])
    state["combat"]["player"]["cards_played_this_turn"] = 3
    result = simulate(client, state, [0, 1])
    assert result["ok"] and result["state"]["player"]["energy"] == 1
    state["combat"]["player"].pop("cards_played_this_turn")
    rejected = simulate(client, state, [0])
    assert not rejected["ok"]
    assert "brilliant_scarf_cards_played_counter_not_observed" in str(rejected["support"])


def test_permafrost_only_blocks_the_power_trigger_with_missing_history(client):
    """Unknown first-Power status must not prevent unrelated attacks from being compared."""
    state = position(["STRIKE_IRONCLAD", "INFLAME"])
    state["run"].update(relic_ids=["PERMAFROST"], relic_stacks=[0])
    attack = simulate(client, state, [0])
    power = simulate(client, state, [1])
    assert attack["ok"] and attack["state"]["enemies"][0]["hp"]["current"] == 94
    assert not power["ok"]
    assert "permafrost_first_power_status_not_observed" in str(power["errors"])


def test_rupture_counter_uses_core_power_id_and_applies_per_hp_loss(client):
    """The reviewed power must match RupturePower rather than an unreachable enum alias."""
    state = position(["STRIKE_IRONCLAD"], powers=["RUPTURE_POWER 2"])
    state["combat"]["hand"][0].update(card_id="BLOODLETTING", energy_cost=0,
        line="Bloodletting [0]: Lose 3 HP. Gain 2 Energy.", target=None, targets=[])
    result = simulate(client, state, [0])
    assert result["ok"]
    assert result["state"]["player"]["hp"]["current"] == 77
    assert "STRENGTH_POWER 2" in result["state"]["player"]["powers"]


def test_next_turn_only_powers_preserve_current_values_and_report_unscored_hazards(client):
    """Do not replay already settled turn-start gains or invent Crimson Mantle's split values."""
    state = position(["STRIKE_IRONCLAD"], powers=["CRIMSON_MANTLE_POWER 8", "PYRE_POWER 1", "MIND_ROT_POWER 1"])
    result = simulate(client, state, [0])
    assert result["ok"]
    assert result["state"]["player"]["hp"]["current"] == 80
    assert result["state"]["player"]["energy"] == 2
    assert result["state"]["player"]["block"] == 0
    assert "CRIMSON_MANTLE_POWER 8" in result["state"]["player"]["powers"]
    assert "next_turn_hp_loss_and_block_not_scored" in str(result["score_support"])


@pytest.mark.parametrize("field,identifier", [("affliction", "BOUND"), ("enchantment", "SHARP")])
def test_structured_card_overlays_are_explicit_boundaries_until_reviewed(client, field, identifier):
    """New public modifier fields cannot silently disappear from the deterministic import."""
    state = position(["STRIKE_IRONCLAD"])
    state["combat"]["hand"][0][field] = {field + "_id": identifier, "amount": 1}
    result = client.request({"op": "search", "public_state": state, "strategy": "exhaustive"})
    assert result["comparison_available"] is False
    assert result["supported_initial_card_actions"] == 0
    assert result["optimal_within_supported_horizon"] is False
    assert field + "_not_reviewed:" + identifier in str(result["support"])


def test_hidden_metadata_and_pile_row_order_do_not_change_deterministic_result(client):
    """Public decisions must not acquire a real RNG position or draw order from extra keys."""
    state = position(["STRIKE_IRONCLAD"])
    changed = deepcopy(state)
    changed["run"]["seed"] = "not-a-real-seed"
    changed["combat"]["draw"].reverse()
    changed["combat"]["enemies"][0]["move_id"] = "HIDDEN_DIFFERENT_MOVE"
    one = simulate(client, state, [0])
    two = simulate(client, changed, [0])
    assert one["ok"] and two["ok"]
    assert one["state"] == two["state"]


def test_exhaustive_counts_all_instance_target_and_stop_sequences(client):
    """Two distinct one-cost cards and two living targets have 1+4+8 terminal plans."""
    state = position(["STRIKE_IRONCLAD", ("STRIKE_IRONCLAD", True)], energy=2,
                     enemies=[enemy(hp=100), enemy(hp=100)])
    result = client.request({
        "op": "search", "public_state": state, "strategy": "exhaustive",
        "max_depth": 2, "exhaustive_limit": 100, "beam_width": 2,
    })
    assert result["ok"] and not result["skipped"]
    assert result["metrics"]["terminal_plans"] == 13
    assert result["exact_within_horizon"]
    assert len(result["best"]["actions"]) >= 2
    assert result["best"]["state"]["player"]["energy"] == 0


def test_exhaustive_refuses_to_label_a_truncated_search_optimal(client):
    """The same 13-plan tree must be skipped when the preset limit is only 12."""
    state = position(["STRIKE_IRONCLAD", ("STRIKE_IRONCLAD", True)], energy=2,
                     enemies=[enemy(hp=100), enemy(hp=100)])
    result = client.request({
        "op": "search", "public_state": state, "strategy": "exhaustive",
        "max_depth": 2, "exhaustive_limit": 12,
    })
    assert result["skipped"]
    assert not result["exact_within_horizon"]
    assert not result.get("best")
